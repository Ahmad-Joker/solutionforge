from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from solutionforge.api.deps import SessionDep, TenantDep
from solutionforge.schemas.audit import AuditEventOut
from solutionforge.services import audit_service

router = APIRouter(tags=["audit"])


@router.get("/orgs/{org_id}/audit-events", response_model=list[AuditEventOut])
async def list_audit_events(
    session: SessionDep,
    ctx: TenantDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[datetime | None, Query(description="Cursor: created_at of last item")] = None,
    event_type: Annotated[str | None, Query(max_length=64)] = None,
) -> list[AuditEventOut]:
    events = await audit_service.list_events(
        session, ctx, limit=limit, before=before, event_type=event_type
    )
    return [AuditEventOut.model_validate(e) for e in events]
