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
- **Rate limits:** `/auth/login`, `/auth/register` and `/auth/refresh` return `429 rate_limited` with a `Retry-After` header.

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
| POST | `/orgs/{org_id}/workflows` | `workflow:write` | |
| GET | `/orgs/{org_id}/workflows[/{id}]` | `workflow:read` | includes `latest_version`, `deployed_version` |
| POST | `/orgs/{org_id}/workflows/{id}/versions` | `workflow:write` | body `{definition, changelog}`; 422 `invalid_workflow_definition` lists every error |
| GET | `/orgs/{org_id}/workflows/{id}/versions[/{n}]` | `workflow:read` | versions are immutable (no PUT/PATCH/DELETE) |
| POST | `/orgs/{org_id}/workflows/{id}/deployments` | `workflow:deploy` | `{version, reason}`; deploying an older version = rollback |
| GET | `/orgs/{org_id}/workflows/{id}/deployments` | `workflow:read` | newest first |
| POST | `/orgs/{org_id}/workflows/{id}/executions` | `workflow:execute` (+`workflow:write` for non-deployed `version`) | 202 queued; with `idempotency_key`, a repeat returns 200 with the original |
| GET | `/orgs/{org_id}/executions` | `workflow:read` | filters: `workflow_id`, `status`, `before`, `limit` |
| GET | `/orgs/{org_id}/executions/{id}` | `workflow:read` | includes the ordered step trail |
| POST | `/orgs/{org_id}/executions/{id}/cancel` | `workflow:execute` | running executions stop before their next step |
| POST | `/orgs/{org_id}/executions/{id}/resume` | `approval:decide` | `{payload: {approved, comment?, data?}}` for approval steps |
| GET | `/orgs/{org_id}/usage/summary` | `usage:read` | per model: calls, failures, tokens, cost, avg latency; `since`/`until`/`execution_id` |
| GET | `/orgs/{org_id}/usage/records` | `usage:read` | every provider attempt; `execution_id`, `before`, `limit` ≤ 500 |
| GET | `/orgs/{org_id}/budget` | `usage:read` | `null` = unlimited |
| PUT | `/orgs/{org_id}/budget` | `org:manage` | `{daily_limit_usd, monthly_limit_usd, per_execution_limit_usd}` (decimal strings, ≤ 6 dp); audited |
| GET | `/orgs/{org_id}/tools` | `tool:read` | catalog with risk, permission, MCP descriptor, installation (`has_credentials` only) |
| PUT | `/orgs/{org_id}/tools/{name}` | `tool:manage` | `{enabled, auto_approve_low_risk, config, credentials}`; credentials: omit = keep, `null` = clear |
| GET | `/orgs/{org_id}/tool-calls` | `tool:read` | call trail; filters `execution_id`, `tool_name`, `before`, `limit` |
| POST | `/orgs/{org_id}/demo-data` | `org:manage` | seed simulated systems and install simulated tools (idempotent) |
| GET | `/orgs/{org_id}/simulated/activity` | `tool:read` | tickets, emails, refunds created by tools |
| POST | `/orgs/{org_id}/knowledge-bases` | `knowledge:write` | `{name, description, chunk_size, chunk_overlap}` |
| GET | `/orgs/{org_id}/knowledge-bases[/{id}]` | `knowledge:read` | |
| DELETE | `/orgs/{org_id}/knowledge-bases/{id}` | `knowledge:write` | cascades documents and chunks |
| POST | `/orgs/{org_id}/knowledge-bases/{id}/documents` | `knowledge:write` | `{title, content, metadata, source_uri}` → 202 (queued); identical content → 200 with the existing doc |
| GET | `/orgs/{org_id}/knowledge-bases/{id}/documents` | `knowledge:read` | `status` filter |
| GET/DELETE | `/orgs/{org_id}/documents/{id}` | read / write | status, attempts, last_error |
| POST | `/orgs/{org_id}/documents/{id}/retry` | `knowledge:write` | re-queue a dead-lettered document |
| POST | `/orgs/{org_id}/knowledge-bases/{id}/search` | `knowledge:read` | `{query, top_k, strategy: dense\|keyword\|hybrid, filters, min_dense_score}` |
| GET | `/orgs/{org_id}/tool-policy` | `tool:read` | |
| PUT | `/orgs/{org_id}/tool-policy` | `org:manage` | `{auto_allow_up_to, blocked_tools, blocked_risk_levels}`; audited |
| GET | `/orgs/{org_id}/approvals` | `approval:read` | `status`, `execution_id`, `before`, `limit` |
| GET | `/orgs/{org_id}/approvals/{id}` | `approval:read` | |
| POST | `/orgs/{org_id}/approvals/{id}/decision` | `approval:decide` + the request's `required_permission`; four-eyes for high risk | `{decision: approve\|reject, args?, comment?}` |
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
