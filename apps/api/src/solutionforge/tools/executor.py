"""ToolExecutor: the only path from a workflow to a tool.

Order of operations for every invocation:

1. resolve the tool in the catalog;
2. load the tenant's installation (must exist and be enabled);
3. validate arguments against the tool's input model (before *any* side effect);
4. **policy gate** (deterministic, risk-based) — refusals are recorded and audited;
5. idempotency: a succeeded call with the same key returns its recorded result;
6. record the call as ``started`` (so a crash leaves evidence);
7. execute under a timeout; retry transient failures only if repeating is safe
   (``idempotent`` tool, or a tool that deduplicates by idempotency key);
8. validate the output against the tool's output model;
9. record the outcome; audit side-effecting calls.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from solutionforge.core.clock import utcnow
from solutionforge.core.logging import get_logger
from solutionforge.domain.audit import AuditEventType
from solutionforge.domain.tools import ToolCall, ToolCallStatus, ToolInstallation
from solutionforge.security.crypto import CredentialCipher, CredentialError
from solutionforge.services import audit_service
from solutionforge.services.audit_service import RequestMeta, sanitize_metadata
from solutionforge.tools import policy
from solutionforge.tools.catalog import ToolCatalog
from solutionforge.tools.spec import (
    Tool,
    ToolApprovalRequired,
    ToolConfigError,
    ToolContext,
    ToolDenied,
    ToolError,
    ToolInputInvalid,
    ToolModel,
    ToolNotEnabled,
    ToolOutputInvalid,
    ToolTimeout,
    ToolTransientError,
)

log = get_logger(__name__)
_SYSTEM = RequestMeta(ip_address=None, request_id=None)


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    tool_call_id: uuid.UUID
    output: dict[str, Any]
    replayed: bool
    attempts: int


class ToolExecutor:
    def __init__(
        self,
        catalog: ToolCatalog,
        sessionmaker: async_sessionmaker[AsyncSession],
        cipher: CredentialCipher,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        backoff_base_seconds: float = 0.2,
    ) -> None:
        self.catalog = catalog
        self.sessionmaker = sessionmaker
        self.cipher = cipher
        self.sleep = sleep
        self.backoff_base_seconds = backoff_base_seconds

    async def invoke(
        self,
        *,
        organization_id: uuid.UUID,
        tool_name: str,
        args: dict[str, Any],
        idempotency_key: str | None = None,
        execution_id: uuid.UUID | None = None,
        step_id: str | None = None,
    ) -> ToolInvocation:
        tool = self.catalog.get(tool_name)  # ToolNotFound
        spec = tool.spec
        installation = await self._installation(organization_id, tool_name)
        if installation is None or not installation.enabled:
            raise ToolNotEnabled(f"tool {tool_name} is not enabled for this organization")

        try:
            parsed = spec.input_model.model_validate(args)
        except ValidationError as exc:
            # Report locations and messages only; never echo the offending values.
            raise ToolInputInvalid(
                f"invalid arguments for {tool_name}",
                errors=[{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()],
            ) from None
        safe_args = sanitize_metadata(parsed.model_dump(mode="json"))

        decision = policy.evaluate(spec, installation)
        if decision.decision != policy.Decision.ALLOW:
            await self._record_refusal(
                organization_id,
                spec.name,
                spec.risk_level,
                safe_args,
                decision,
                execution_id,
                step_id,
                idempotency_key,
            )
            if decision.decision == policy.Decision.REQUIRE_APPROVAL:
                raise ToolApprovalRequired(
                    f"{tool_name} requires approval: {decision.reason}",
                    risk_level=spec.risk_level.value,
                    approver_permission=decision.approver_permission.value
                    if decision.approver_permission
                    else None,
                )
            raise ToolDenied(f"{tool_name} denied: {decision.reason}")

        call_id, replay = await self._begin(
            organization_id,
            spec.name,
            spec.risk_level,
            safe_args,
            idempotency_key,
            execution_id,
            step_id,
        )
        if replay is not None:
            log.info("tool_call_replayed", tool=tool_name, tool_call_id=str(call_id))
            return ToolInvocation(call_id, replay, replayed=True, attempts=0)

        ctx = ToolContext(
            organization_id=organization_id,
            execution_id=execution_id,
            step_id=step_id,
            idempotency_key=idempotency_key,
            config=dict(installation.config),
            credentials=self._credentials(spec.name, installation, spec.requires_credentials),
            session=self.sessionmaker,
        )
        safe_to_repeat = spec.idempotent or (spec.supports_idempotency_key and idempotency_key)
        max_attempts = spec.max_attempts if safe_to_repeat else 1

        t0 = time.monotonic()
        attempts = 0
        error: ToolError | None = None
        output: dict[str, Any] | None = None
        while True:
            attempts += 1
            try:
                output = await self._attempt(tool, parsed, ctx)
                error = None
                break
            except ToolError as exc:
                error = exc
            if not error.retryable or attempts >= max_attempts:
                break
            await self.sleep(self.backoff_base_seconds * 2 ** (attempts - 1))
        latency_ms = int((time.monotonic() - t0) * 1000)

        await self._finish(
            call_id,
            organization_id,
            spec.name,
            spec.risk_level.value,
            spec.has_side_effects,
            output,
            error,
            attempts,
            latency_ms,
            execution_id,
            safe_args,
        )
        log.info(
            "tool_call",
            tool=tool_name,
            risk=spec.risk_level.value,
            status="failed" if error else "succeeded",
            code=error.code if error else None,
            attempts=attempts,
            latency_ms=latency_ms,
        )
        if error is not None:
            raise error
        assert output is not None
        return ToolInvocation(call_id, output, replayed=False, attempts=attempts)

    # ------------------------------------------------------------------ internals

    async def _attempt(self, tool: Tool, parsed: ToolModel, ctx: ToolContext) -> dict[str, Any]:
        spec = tool.spec
        try:
            async with asyncio.timeout(spec.timeout_seconds):
                result = await tool.execute(parsed, ctx)
        except TimeoutError:
            raise ToolTimeout(f"{spec.name} timed out after {spec.timeout_seconds}s") from None
        except ToolError:
            raise
        except Exception as exc:  # connector bug or driver failure: contain, never leak text
            log.exception("tool_unhandled_exception", tool=spec.name)
            raise ToolTransientError(
                f"{spec.name} failed unexpectedly", exception_type=type(exc).__name__
            ) from None
        if not isinstance(result, spec.output_model):
            raise ToolOutputInvalid(f"{spec.name} returned {type(result).__name__}")
        try:  # re-validate: catches constraint violations constructed without validation
            return spec.output_model.model_validate(result.model_dump()).model_dump(mode="json")
        except ValidationError:
            raise ToolOutputInvalid(f"{spec.name} returned data violating its schema") from None

    async def _installation(self, org: uuid.UUID, name: str) -> ToolInstallation | None:
        async with self.sessionmaker() as s:
            return await s.scalar(
                sa.select(ToolInstallation).where(
                    ToolInstallation.organization_id == org, ToolInstallation.tool_name == name
                )
            )

    def _credentials(
        self, name: str, inst: ToolInstallation, required: bool
    ) -> dict[str, Any] | None:
        if inst.credentials_encrypted is None:
            if required:
                raise ToolConfigError(f"{name} requires credentials; none are configured")
            return None
        try:
            return self.cipher.decrypt(inst.credentials_encrypted)
        except CredentialError as exc:
            raise ToolConfigError(f"{name}: {exc}") from None

    async def _begin(
        self,
        org: uuid.UUID,
        name: str,
        risk: Any,
        safe_args: dict[str, Any],
        key: str | None,
        execution_id: uuid.UUID | None,
        step_id: str | None,
    ) -> tuple[uuid.UUID, dict[str, Any] | None]:
        """Create (or reuse, for a retried key) the call record. Returns a recorded output
        when the same key already succeeded."""
        async with self.sessionmaker() as s, s.begin():
            if key is not None:
                prior = await s.scalar(
                    sa.select(ToolCall)
                    .where(
                        ToolCall.organization_id == org,
                        ToolCall.tool_name == name,
                        ToolCall.idempotency_key == key,
                    )
                    .with_for_update()
                )
                if prior is not None:
                    if prior.status == ToolCallStatus.SUCCEEDED:
                        return prior.id, prior.output or {}
                    prior.status = ToolCallStatus.STARTED
                    prior.args = safe_args
                    prior.error = None
                    return prior.id, None
            call = ToolCall(
                organization_id=org,
                tool_name=name,
                risk_level=str(risk.value if hasattr(risk, "value") else risk),
                idempotency_key=key,
                execution_id=execution_id,
                step_id=step_id,
                status=ToolCallStatus.STARTED,
                args=safe_args,
            )
            s.add(call)
            try:
                await s.flush()
            except IntegrityError:
                raise ToolTransientError("concurrent invocation with the same key") from None
            return call.id, None

    async def _finish(
        self,
        call_id: uuid.UUID,
        org: uuid.UUID,
        name: str,
        risk: str,
        side_effects: bool,
        output: dict[str, Any] | None,
        error: ToolError | None,
        attempts: int,
        latency_ms: int,
        execution_id: uuid.UUID | None,
        safe_args: dict[str, Any],
    ) -> None:
        async with self.sessionmaker() as s, s.begin():
            await s.execute(
                sa.update(ToolCall)
                .where(ToolCall.id == call_id)
                .values(
                    status=ToolCallStatus.FAILED if error else ToolCallStatus.SUCCEEDED,
                    output=sanitize_metadata(output) if output is not None else None,
                    error={"code": error.code, "message": error.message} if error else None,
                    attempts=ToolCall.attempts + attempts,
                    latency_ms=latency_ms,
                    finished_at=utcnow(),
                )
            )
            if side_effects and error is None:
                audit_service.record(
                    s,
                    event_type=AuditEventType.TOOL_EXECUTED,
                    request=_SYSTEM,
                    organization_id=org,
                    resource_type="tool_call",
                    resource_id=call_id,
                    execution_id=execution_id,
                    metadata={"tool": name, "risk_level": risk, "args": safe_args},
                )

    async def _record_refusal(
        self,
        org: uuid.UUID,
        name: str,
        risk: Any,
        safe_args: dict[str, Any],
        decision: policy.PolicyResult,
        execution_id: uuid.UUID | None,
        step_id: str | None,
        key: str | None,
    ) -> None:
        async with self.sessionmaker() as s, s.begin():
            # Refusals are not keyed (a later approved run must not collide with them).
            s.add(
                ToolCall(
                    organization_id=org,
                    tool_name=name,
                    risk_level=risk.value,
                    idempotency_key=None,
                    execution_id=execution_id,
                    step_id=step_id,
                    status=ToolCallStatus.DENIED,
                    args=safe_args,
                    error={"code": decision.decision.value, "message": decision.reason},
                    finished_at=utcnow(),
                )
            )
            audit_service.record(
                s,
                event_type=AuditEventType.TOOL_DENIED,
                request=_SYSTEM,
                organization_id=org,
                resource_type="tool",
                resource_id=name,
                execution_id=execution_id,
                metadata={
                    "tool": name,
                    "risk_level": risk.value,
                    "decision": decision.decision.value,
                    "reason": decision.reason,
                    "idempotency_key": key,
                },
            )
