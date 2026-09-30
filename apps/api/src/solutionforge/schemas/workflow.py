from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from solutionforge.domain.workflow import ExecutionStatus, StepStatus

WorkflowName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=2, max_length=120)
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateWorkflowRequest(StrictModel):
    name: WorkflowName
    description: str = Field(default="", max_length=2000)


class WorkflowOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    latest_version: int | None
    deployed_version: int | None
    created_at: datetime


class CreateVersionRequest(StrictModel):
    definition: dict[str, Any] = Field(description="WorkflowDefinition (see ARCHITECTURE.md §6)")
    changelog: str = Field(default="", max_length=2000)


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workflow_id: uuid.UUID
    version: int
    definition: dict[str, Any]
    definition_hash: str
    changelog: str
    created_at: datetime


class DeployRequest(StrictModel):
    version: int = Field(ge=1)
    reason: str = Field(default="", max_length=2000)


class DeploymentOut(BaseModel):
    id: uuid.UUID
    version: int
    reason: str
    deployed_by_user_id: uuid.UUID | None
    created_at: datetime


class CreateExecutionRequest(StrictModel):
    input: dict[str, Any] = Field(default_factory=dict)
    version: int | None = Field(default=None, ge=1, description="Omit to run the deployed version")
    idempotency_key: str | None = Field(
        default=None, min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    )


class ResumeRequest(StrictModel):
    payload: dict[str, Any]


class ExecutionStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    step_id: str
    step_type: str
    attempt: int
    status: StepStatus
    output: dict[str, Any] | None
    error: dict[str, Any] | None
    started_at: datetime
    finished_at: datetime
    duration_ms: int


class ExecutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workflow_id: uuid.UUID
    workflow_version_id: uuid.UUID
    status: ExecutionStatus
    input: dict[str, Any]
    state: dict[str, Any]
    output: dict[str, Any] | None
    error: dict[str, Any] | None
    waiting_on: dict[str, Any] | None
    current_step: str | None
    steps_used: int
    active_ms: int
    cancel_requested: bool
    idempotency_key: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ExecutionDetailOut(ExecutionOut):
    steps: list[ExecutionStepOut]
