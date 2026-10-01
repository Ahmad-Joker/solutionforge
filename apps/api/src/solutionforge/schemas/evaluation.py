from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from solutionforge.domain.evaluation import RunStatus

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateDatasetRequest(StrictModel):
    workflow_id: uuid.UUID
    name: Name
    description: str = Field(default="", max_length=2000)


class DatasetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workflow_id: uuid.UUID
    name: str
    description: str
    created_at: datetime


class CreateCaseRequest(StrictModel):
    name: Name
    input: dict[str, Any] = Field(default_factory=dict)
    expectations: dict[str, Any] = Field(default_factory=dict)
    tags: list[Tag] = Field(default_factory=list, max_length=10)


class CaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    input: dict[str, Any]
    expectations: dict[str, Any]
    tags: list[str]


class StartRunRequest(StrictModel):
    version: int = Field(ge=1)


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    dataset_id: uuid.UUID
    workflow_id: uuid.UUID
    version: int
    status: RunStatus
    metrics: dict[str, Any] | None
    created_at: datetime
    finished_at: datetime | None


class ResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    case_id: uuid.UUID
    execution_id: uuid.UUID | None
    passed: bool
    scores: dict[str, Any]
    failures: list[str]


class RunDetailOut(RunOut):
    results: list[ResultOut]


class DecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version: int
    candidate_run_id: uuid.UUID | None
    baseline_run_id: uuid.UUID | None
    passed: bool
    overridden: bool
    override_reason: str | None
    checks: list[dict[str, Any]]
    decided_by_user_id: uuid.UUID | None
    created_at: datetime
