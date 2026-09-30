"""Request/response models for auth, organizations, members and invitations."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

from solutionforge.security.rbac import Role

Password = Annotated[str, Field(min_length=12, max_length=128)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
OrgName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=120)]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$")]
OpaqueToken = Annotated[str, Field(min_length=16, max_length=256)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ auth


class RegisterRequest(StrictModel):
    email: EmailStr
    password: Password
    display_name: DisplayName


class LoginRequest(StrictModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=128)]


class RefreshRequest(StrictModel):
    refresh_token: OpaqueToken


class TokenResponse(BaseModel):
    token_type: str = "bearer"  # noqa: S105 (OAuth token type, not a secret)
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    display_name: str
    created_at: datetime


# ------------------------------------------------------------------ orgs


class CreateOrganizationRequest(StrictModel):
    name: OrgName
    slug: Slug | None = None


class UpdateOrganizationRequest(StrictModel):
    name: OrgName


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    created_at: datetime


class MyOrganizationOut(OrganizationOut):
    role: Role


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    role: Role
    joined_at: datetime


class ChangeRoleRequest(StrictModel):
    role: Role


class CreateInvitationRequest(StrictModel):
    email: EmailStr
    role: Role = Role.VIEWER


class InvitationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    role: Role
    created_at: datetime
    expires_at: datetime


class CreatedInvitationOut(InvitationOut):
    token: str = Field(description="Shown once. Deliver it to the invitee out of band.")


class AcceptInvitationRequest(StrictModel):
    token: OpaqueToken
