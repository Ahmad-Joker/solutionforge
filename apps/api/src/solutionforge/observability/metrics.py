"""Prometheus metrics.

Label rules (enforced by review and tests):

- **Bounded cardinality only.** Labels are drawn from closed sets: route *templates*, never
  raw paths; tool names from the catalog; models from the price table; status and error
  codes defined in code.
- **No tenant identifiers or user data.** Per-organization usage lives in the database
  (usage API), not in a scrape that every operator can see.
"""

from __future__ import annotations

from collections.abc import Iterable

import sqlalchemy as sa
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from solutionforge.core.clock import utcnow
from solutionforge.domain.approvals import Approval, ApprovalStatus
from solutionforge.domain.evaluation import EvaluationRun, RunStatus
from solutionforge.domain.knowledge import Document, DocumentStatus
from solutionforge.domain.workflow import Execution, ExecutionStatus

REGISTRY = CollectorRegistry(auto_describe=True)

_FAST = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
_SLOW = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)

HTTP_REQUESTS = Counter(
    "sf_http_requests",
    "HTTP requests by route template and status.",
    ["method", "route", "status"],
    registry=REGISTRY,
)
HTTP_DURATION = Histogram(
    "sf_http_request_duration_seconds",
    "HTTP request latency.",
    ["method", "route"],
    buckets=_FAST,
    registry=REGISTRY,
)
EXECUTIONS_FINISHED = Counter(
    "sf_workflow_executions_finished",
    "Executions reaching a terminal status in the engine.",
    ["status"],
    registry=REGISTRY,
)
STEP_DURATION = Histogram(
    "sf_workflow_step_duration_seconds",
    "Step attempt duration by step type and outcome.",
    ["step_type", "status"],
    buckets=_SLOW,
    registry=REGISTRY,
)
LLM_CALLS = Counter(
    "sf_llm_calls",
    "LLM call attempts by model and outcome.",
    ["provider", "model", "outcome"],
    registry=REGISTRY,
)
LLM_TOKENS = Counter(
    "sf_llm_tokens",
    "Tokens consumed.",
    ["provider", "model", "direction"],
    registry=REGISTRY,
)
LLM_COST = Counter(
    "sf_llm_cost_usd",
    "Metered LLM spend in USD.",
    ["provider", "model"],
    registry=REGISTRY,
)
LLM_DURATION = Histogram(
    "sf_llm_call_duration_seconds",
    "LLM call attempt latency.",
    ["provider", "model"],
    buckets=_SLOW,
    registry=REGISTRY,
)
TOOL_CALLS = Counter(
    "sf_tool_calls",
    "Tool invocations by tool, risk level and outcome.",
    ["tool", "risk", "outcome"],
    registry=REGISTRY,
)
TOOL_DURATION = Histogram(
    "sf_tool_call_duration_seconds",
    "Tool invocation latency (including retries).",
    ["tool"],
    buckets=_FAST,
    registry=REGISTRY,
)
DEPLOY_DECISIONS = Counter(
    "sf_deployment_gate_decisions",
    "Gated deployment attempts.",
    ["result"],
    registry=REGISTRY,
)

# Sampled from the database by the background loop (queue depth and backlogs).
EXECUTIONS_BY_STATUS = Gauge(
    "sf_workflow_executions",
    "Non-terminal executions by status.",
    ["status"],
    registry=REGISTRY,
)
APPROVALS_PENDING = Gauge("sf_approvals_pending", "Pending approval requests.", registry=REGISTRY)
DOCUMENTS_BY_STATUS = Gauge(
    "sf_documents",
    "Documents not yet ready, by ingestion status.",
    ["status"],
    registry=REGISTRY,
)
EVAL_RUNS_RUNNING = Gauge(
    "sf_evaluation_runs_running", "Evaluation runs in progress.", registry=REGISTRY
)
OLDEST_QUEUED_SECONDS = Gauge(
    "sf_workflow_oldest_queued_seconds",
    "Age of the oldest runnable queued execution.",
    registry=REGISTRY,
)


def render() -> bytes:
    return generate_latest(REGISTRY)


def _set_all(gauge: Gauge, observed: Iterable[tuple[str, int]], expected: Iterable[str]) -> None:
    counts = dict(observed)
    for status in expected:  # reset statuses that drained to zero
        gauge.labels(status).set(counts.get(status, 0))


async def sample_backlogs(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Refresh queue-depth gauges with a few aggregate queries (no tenant labels)."""
    live = [ExecutionStatus.QUEUED, ExecutionStatus.RUNNING, ExecutionStatus.WAITING]
    pending_docs = [s for s in DocumentStatus if s != DocumentStatus.READY]
    async with sessionmaker() as s:
        rows = await s.execute(
            sa.select(Execution.status, sa.func.count())
            .where(Execution.status.in_(live))
            .group_by(Execution.status)
        )
        _set_all(EXECUTIONS_BY_STATUS, ((st.value, n) for st, n in rows), (x.value for x in live))
        now = utcnow()
        oldest = await s.scalar(
            sa.select(sa.func.min(Execution.run_after)).where(
                Execution.status == ExecutionStatus.QUEUED, Execution.run_after <= now
            )
        )
        OLDEST_QUEUED_SECONDS.set(max(0.0, (now - oldest).total_seconds()) if oldest else 0)
        APPROVALS_PENDING.set(
            await s.scalar(
                sa.select(sa.func.count()).where(Approval.status == ApprovalStatus.PENDING)
            )
            or 0
        )
        rows = await s.execute(
            sa.select(Document.status, sa.func.count())
            .where(Document.status.in_(pending_docs))
            .group_by(Document.status)
        )
        _set_all(
            DOCUMENTS_BY_STATUS, ((st.value, n) for st, n in rows), (x.value for x in pending_docs)
        )
        EVAL_RUNS_RUNNING.set(
            await s.scalar(
                sa.select(sa.func.count()).where(EvaluationRun.status == RunStatus.RUNNING)
            )
            or 0
        )
