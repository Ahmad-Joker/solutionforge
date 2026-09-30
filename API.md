# API

Interactive OpenAPI docs: `GET /docs` (schema at `/openapi.json`). All business routes live under `/api/v1`.

## Conventions

- **Auth:** `Authorization: Bearer <access_token>`.
- **Tenancy:** tenant resources are `/api/v1/orgs/{org_id}/…`. If you aren't a member, the
  response is `404 Organization not found`, identical to a non-existent org.
- **Errors:** every non-2xx response has this shape:
  ```json
  {"error": {"code": "permission_denied", "message": "...", "details": {}, "request_id": "..."}}
  ```
  Codes: `validation_failed` (422), `authentication_failed` (401), `permission_denied` (403),
  `not_found` (404), `conflict` (409), `internal_error` (500).
- **Request IDs:** send `X-Request-ID` (8–64 chars of `[A-Za-z0-9._-]`) or one is generated.
  It is echoed back in the response header and stored on audit events.
- **Unknown fields** in request bodies are rejected (422).

## Endpoints

| Method | Path | Permission | Notes |
|---|---|---|---|
| POST | `/auth/register` | public | 201 → user |
| POST | `/auth/login` | public | → access + refresh tokens |
| POST | `/auth/refresh` | refresh token | rotates; replaying a rotated token revokes the session |
| POST | `/auth/logout` | refresh token | 204, idempotent |
| GET | `/auth/me` | authenticated | |
| POST | `/orgs` | authenticated | caller becomes OWNER; slug derived if omitted |
| GET | `/orgs` | authenticated | orgs you belong to, with your role |
| GET | `/orgs/{org_id}` | `org:read` | |
| PATCH | `/orgs/{org_id}` | `org:manage` | |
| GET | `/orgs/{org_id}/members` | `member:read` | |
| PATCH | `/orgs/{org_id}/members/{user_id}` | `member:manage` + rank rules | |
| DELETE | `/orgs/{org_id}/members/{user_id}` | `member:manage`, or self (leave) | last owner protected |
| POST | `/orgs/{org_id}/invitations` | `member:manage` | response includes `token` once |
| GET | `/orgs/{org_id}/invitations` | `member:manage` | pending only |
| DELETE | `/orgs/{org_id}/invitations/{id}` | `member:manage` | |
| POST | `/invitations/accept` | authenticated, email must match | |
| GET | `/orgs/{org_id}/audit-events` | `audit:read` | `limit` ≤ 200, `before` cursor, `event_type` filter |
| GET | `/healthz`, `/readyz` | public | liveness / readiness (DB) |

## Walkthrough

```bash
B=http://localhost:8000/api/v1
curl -s -X POST $B/auth/register -H 'content-type: application/json' \
  -d '{"email":"owner@example.com","password":"a-long-enough-password","display_name":"Owner"}'
TOKEN=$(curl -s -X POST $B/auth/login -H 'content-type: application/json' \
  -d '{"email":"owner@example.com","password":"a-long-enough-password"}' | jq -r .access_token)
ORG=$(curl -s -X POST $B/orgs -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"name":"Acme Support"}' | jq -r .id)
curl -s -X POST $B/orgs/$ORG/invitations -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"email":"agent@example.com","role":"operator"}'
curl -s $B/orgs/$ORG/audit-events -H "authorization: Bearer $TOKEN"
```
