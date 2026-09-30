"""Thin, readable wrappers over the HTTP API for tests."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from httpx import AsyncClient, Response

DEFAULT_PASSWORD = "correct-horse-battery-staple"


@dataclass
class Session:
    user_id: str
    email: str
    access_token: str
    refresh_token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


class Api:
    def __init__(self, client: AsyncClient) -> None:
        self.c = client

    async def register(
        self, email: str | None = None, password: str = DEFAULT_PASSWORD
    ) -> Response:
        email = email or f"user-{uuid.uuid4().hex[:10]}@example.com"
        return await self.c.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "display_name": email.split("@")[0]},
        )

    async def login(self, email: str, password: str = DEFAULT_PASSWORD) -> Response:
        return await self.c.post("/api/v1/auth/login", json={"email": email, "password": password})

    async def user(self, email: str | None = None) -> Session:
        """Register + log in a fresh user."""
        r = await self.register(email)
        assert r.status_code == 201, r.text
        body = r.json()
        t = (await self.login(body["email"])).json()
        return Session(body["id"], body["email"], t["access_token"], t["refresh_token"])

    async def org(self, owner: Session, name: str = "Acme Corp", slug: str | None = None) -> str:
        payload: dict[str, Any] = {"name": name}
        if slug:
            payload["slug"] = slug
        r = await self.c.post("/api/v1/orgs", json=payload, headers=owner.headers)
        assert r.status_code == 201, r.text
        return str(r.json()["id"])

    async def invite(self, org_id: str, actor: Session, email: str, role: str) -> Response:
        return await self.c.post(
            f"/api/v1/orgs/{org_id}/invitations",
            json={"email": email, "role": role},
            headers=actor.headers,
        )

    async def accept(self, who: Session, token: str) -> Response:
        return await self.c.post(
            "/api/v1/invitations/accept", json={"token": token}, headers=who.headers
        )

    async def member(self, org_id: str, owner: Session, role: str) -> Session:
        """Create a new user and add them to ``org_id`` with ``role`` via invitation."""
        s = await self.user()
        r = await self.invite(org_id, owner, s.email, role)
        assert r.status_code == 201, r.text
        r = await self.accept(s, r.json()["token"])
        assert r.status_code == 200, r.text
        return s
