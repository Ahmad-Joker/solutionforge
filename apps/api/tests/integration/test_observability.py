"""Metrics and tracing: what is emitted, what is never emitted, and who can scrape it."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

from solutionforge.core.config import Environment
from solutionforge.llm.providers.mock import MockProvider, MockReply
from solutionforge.observability import metrics
from solutionforge.observability.metrics import REGISTRY, sample_backlogs
from solutionforge.observability.tracing import configure_tracing, log_trace_ids, tracer
from solutionforge.workflows.engine import Engine
from tests.helpers import Api
from tests.workflow_support import get_exec, make_workflow, start, step

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_EXPORTER = InMemorySpanExporter()

WF = {
    "start": "lookup",
    "inputs": {"customer": {"type": "string"}},
    "steps": [
        step(
            "lookup",
            "tool",
            {"tool": "crm.get_customer", "args": {"customer_ref": "$.input.customer"}},
            next="summarize",
        ),
        step(
            "summarize",
            "llm",
            {
                "model": "mock:mock-1",
                "prompt": "Summarize {{ $.steps.lookup.result.name }}",
                "output_schema": {
                    "type": "object",
                    "required": ["summary"],
                    "properties": {"summary": {"type": "string"}},
                },
            },
        ),
    ],
    "output": {"summary": "$.steps.summarize.json.summary"},
}


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    configure_tracing("test", extra_processor=_PROCESSOR)
    _EXPORTER.clear()
    yield _EXPORTER
    _EXPORTER.clear()


_PROCESSOR = SimpleSpanProcessor(_EXPORTER)


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def test_one_trace_spans_request_worker_step_tool_and_llm(
    api: Api,
    client: AsyncClient,
    engine: Engine,
    mock_llm: MockProvider,
    spans: InMemorySpanExporter,
) -> None:
    s = await make_workflow(api, WF)
    await client.post(s.url("/demo-data"), headers=s.owner.headers)
    mock_llm.script(MockReply.json({"summary": "VIP customer"}))
    spans.clear()
    eid = await start(api, s, {"customer": "C-1001"})
    await engine.run_until_idle()
    assert (await get_exec(api, s, eid))["status"] == "succeeded"

    finished: tuple[ReadableSpan, ...] = spans.get_finished_spans()
    by_name = {sp.name: sp for sp in finished}
    route = "/api/v1/orgs/{org_id}/workflows/{workflow_id}/executions"
    [http] = [sp for sp in finished if sp.name == f"POST {route}"]  # exactly one server span
    endpoint = next(
        sp
        for sp in finished
        if sp.name == "fastapi.endpoint" and sp.parent and sp.parent.span_id == http.context.span_id
    )
    execution = by_name["workflow.execution"]
    tool_step = by_name["workflow.step tool"]
    llm_step = by_name["workflow.step llm"]
    tool = by_name["tool.invoke"]
    llm = by_name["llm.generate"]

    # Same trace from the HTTP request through the worker, even across the queue.
    trace_ids = {
        sp.context.trace_id for sp in (http, endpoint, execution, tool_step, llm_step, tool, llm)
    }
    assert len(trace_ids) == 1
    # Queued work continues the trace from inside the endpoint that created it.
    assert execution.parent is not None and execution.parent.span_id == endpoint.context.span_id
    assert tool_step.parent.span_id == execution.context.span_id  # type: ignore[union-attr]
    assert tool.parent.span_id == tool_step.context.span_id  # type: ignore[union-attr]
    assert llm.parent.span_id == llm_step.context.span_id  # type: ignore[union-attr]
    assert http.attributes["http.route"] == route
    assert tool.attributes["sf.tool.outcome"] == "succeeded"
    assert llm.attributes["sf.llm.model"] == "mock:mock-1"
    assert any(e.name == "llm.attempt" for e in llm.events)

    # Spans carry IDs and metadata only: no inputs, tool args, prompts or outputs.
    leaked = {"C-1001", "VIP customer", "Summarize"}
    for sp in finished:
        values = [str(v) for v in (sp.attributes or {}).values()]
        values += [str(v) for e in sp.events for v in (e.attributes or {}).values()]
        assert not any(secret in v for v in values for secret in leaked), sp.name


async def test_metrics_endpoint_counts_by_route_template_without_ids(
    api: Api, client: AsyncClient, engine: Engine, mock_llm: MockProvider, app: FastAPI
) -> None:
    before_ok = sample("sf_workflow_executions_finished_total", status="succeeded")
    before_tool = sample(
        "sf_tool_calls_total", tool="crm.get_customer", risk="read_only", outcome="succeeded"
    )
    before_bad = sample(
        "sf_tool_calls_total",
        tool="crm.get_customer",
        risk="read_only",
        outcome="tool_business_error",
    )
    s = await make_workflow(api, WF)
    await client.post(s.url("/demo-data"), headers=s.owner.headers)
    mock_llm.script(MockReply.json({"summary": "ok"}))
    await start(api, s, {"customer": "C-1001"})
    await start(api, s, {"customer": "C-9999"})  # unknown customer → business error
    await engine.run_until_idle()

    assert sample("sf_workflow_executions_finished_total", status="succeeded") == before_ok + 1
    assert (
        sample(
            "sf_tool_calls_total", tool="crm.get_customer", risk="read_only", outcome="succeeded"
        )
        == before_tool + 1
    )
    assert (
        sample(
            "sf_tool_calls_total",
            tool="crm.get_customer",
            risk="read_only",
            outcome="tool_business_error",
        )
        == before_bad + 1
    )
    assert sample("sf_llm_calls_total", provider="mock", model="mock-1", outcome="ok") >= 1

    # Queue-depth gauges come from the sampler.
    await start(api, s, {"customer": "C-1002"})
    await sample_backlogs(app.state.sessionmaker)
    assert sample("sf_workflow_executions", status="queued") >= 1

    r = await client.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert 'route="/api/v1/orgs/{org_id}/workflows/{workflow_id}/executions"' in body
    assert "sf_http_request_duration_seconds_bucket" in body
    # Bounded cardinality and no tenant data: no UUIDs, emails or org names in any label.
    assert not UUID_RE.search(body)
    assert "@example.com" not in body and "Acme" not in body
    await client.get(f"/api/v1/no/such/{s.org}")
    assert 'route="unmatched"' in (await client.get("/metrics")).text


async def test_metrics_endpoint_requires_token_or_is_hidden(
    app: FastAPI, client: AsyncClient
) -> None:
    base = app.state.settings
    app.state.settings = base.model_copy(update={"metrics_token": SecretStr("scrape-secret")})
    assert (await client.get("/metrics")).status_code == 404
    bad = {"Authorization": "Bearer nope"}
    assert (await client.get("/metrics", headers=bad)).status_code == 404
    ok = {"Authorization": "Bearer scrape-secret"}
    assert (await client.get("/metrics", headers=ok)).status_code == 200

    app.state.settings = base.model_copy(update={"environment": Environment.PRODUCTION})
    assert (await client.get("/metrics")).status_code == 404  # no token configured → hidden
    app.state.settings = base


def test_log_lines_carry_trace_ids_inside_spans(spans: InMemorySpanExporter) -> None:
    assert "trace_id" not in log_trace_ids(None, "info", {})
    with tracer().start_as_current_span("unit") as sp:
        event = log_trace_ids(None, "info", {"event": "x"})
    assert event["trace_id"] == format(sp.get_span_context().trace_id, "032x")
    assert len(event["span_id"]) == 16


def test_tracing_is_off_without_an_exporter() -> None:
    assert configure_tracing("svc") is None


def test_metric_definitions_have_no_tenant_labels() -> None:
    forbidden = {"org", "org_id", "organization_id", "user", "user_id", "email", "tenant"}
    for collector in list(REGISTRY._collector_to_names):
        labels = set(getattr(collector, "_labelnames", ()))
        assert not labels & forbidden, collector
    assert metrics.HTTP_REQUESTS._labelnames == ("method", "route", "status")
