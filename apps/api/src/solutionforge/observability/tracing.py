"""OpenTelemetry tracing.

One trace follows a unit of work across processes:

    HTTP request ──► workflow.execution (worker, possibly much later) ──► workflow.step
                                                                          ├─► llm.generate
                                                                          └─► tool.invoke

The request's W3C ``traceparent`` is stored on the execution row when it is created; the
worker continues that trace when it picks the execution up (and again after each resume).

Spans carry IDs, names, statuses, token counts and costs — never prompts, model outputs,
tool arguments or customer data. Without an exporter configured, the OpenTelemetry API is
a no-op and costs next to nothing.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_configured: TracerProvider | None = None


def tracer() -> trace.Tracer:
    return trace.get_tracer("solutionforge")


def configure_tracing(
    service_name: str,
    *,
    otlp_endpoint: str | None = None,
    extra_processor: SpanProcessor | None = None,
) -> TracerProvider | None:
    """Install the SDK provider once per process. ``extra_processor`` is for tests."""
    global _configured  # process-wide OTel provider
    if otlp_endpoint is None and extra_processor is None:
        return None
    if _configured is None:
        _configured = TracerProvider(resource=Resource.create({"service.name": service_name}))
        trace.set_tracer_provider(_configured)
    if otlp_endpoint is not None:
        _configured.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces"))
        )
    if extra_processor is not None:
        _configured.add_span_processor(extra_processor)
    return _configured


def current_traceparent() -> str | None:
    """The active span's W3C traceparent (to persist with queued work), if any."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    value = carrier.get("traceparent")
    return value if value and len(value) <= 64 else None


def context_from_traceparent(value: str | None) -> Context | None:
    return propagate.extract({"traceparent": value}) if value else None


def log_trace_ids(
    _: Any, __: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: correlate log lines with traces."""
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict
