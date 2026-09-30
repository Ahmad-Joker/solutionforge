from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    event_type: str
    actor_user_id: uuid.UUID | None
    resource_type: str | None
    resource_id: str | None
    execution_id: uuid.UUID | None
    metadata: dict[str, Any] = Field(validation_alias="event_metadata")
    request_id: str | None
