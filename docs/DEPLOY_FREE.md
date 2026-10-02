# Free deployment: Render + Supabase ($0)

```
browser ──► Render: solutionforge-web  (Next.js dashboard + BFF, httpOnly cookies)
                └──► Render: solutionforge-api  (API + embedded worker loops)
                          └──► Supabase Postgres (pgvector) via the Session pooler
```

Both Render services come from one blueprint (`render.yaml`).

**Live since 2026-10-03:** [solutionforge-web-ap6j.onrender.com](https://solutionforge-web-ap6j.onrender.com)
(API: `solutionforge-api.onrender.com`). It runs on Render free + **Neon** free (Postgres 16,
AWS us-east-1), because the Supabase account already had its 2 free active projects in
use. The strict smoke test passed against the live API: worker execution, tool call,
metered LLM call, hidden `/metrics`, and tenant isolation.

**Easiest database setup (learned deploying it):** set `SF_DATABASE_URL` to the connection
string **without** the password, e.g.
`postgresql://neondb_owner@ep-xxx.us-east-1.aws.neon.tech/neondb?sslmode=require`, and paste
the password alone into `SF_DATABASE_PASSWORD`. Two traps came up in practice:
- an `https://` link (the console page, or the Data API tab) pasted as the URL;
- Neon's copy button copying `password@host/db?…` rather than the password alone.

The app now rejects the first with a clear message, and the separate password setting
avoids the second.

**Rehearsed on 2026-10-02** with the exact blueprint settings, locally in Docker, against
a Supabase-like Postgres. That database had Supabase's `anon`/`authenticated` roles and
default grants, and a dotted pooler username (`postgres.<ref>`).
- Both services served on Render's assigned `$PORT`.
- Migrations ran on start, **and closed the Data API automatically: 0 grants left**.
- The web server woke the API as it booted.
- Sign-up, login (cookies `Secure; HttpOnly; SameSite=lax`) and BFF calls all worked.
- The strict smoke test passed.
- Memory peaked at 87 MB (API) and 38 MB (web), against 512 MB each.

## What "free" costs you

| Platform | Free-tier behaviour (check current terms) | Effect |
|---|---|---|
| Render (×2 services) | Each sleeps after ~15 min idle; waking takes ~30–60 s; 512 MB RAM; free instance hours are shared across services | The first visit after idle is slow. The web server wakes the API in parallel as it starts, so you pay one cold start, not two in a row. Work queued while asleep runs on wake |
| Supabase | ~500 MB database; **the project pauses after ~1 week without activity** | If the demo has been unused for a week, open the Supabase dashboard and click **Restore** |

**Don't add a keep-alive pinger.** It burns the free instance hours.

The app uses the **mock** model, so there's no AI spend. Don't put real customer data or
real secrets in the demo.

## Steps

### 0. Put the code on GitHub

Create an **empty** repository on github.com (no README or license), then push this
project to it.

### 1. Database: Supabase

1. Sign up at supabase.com.
2. **New project**, with region **East US (North Virginia)**. Both Render services are
   pinned to Virginia in `render.yaml`.
   - Save the database password somewhere safe.
3. **Project Settings → Data API**: turn the Data API **off**. SolutionForge never uses it.
   Migration `0010` also revokes the API roles' access to every table on each deploy, so
   leaving it on isn't a data leak, but there's no reason to run it.
4. Click **Connect** and choose the **Session pooler** connection string. Don't use:
   - **"Direct"**: it's IPv6-only on the free tier, and Render can't reach it;
   - **"Transaction pooler"** (port 6543): it breaks the driver's prepared statements.

   The string looks like:
   ```
   postgresql://postgres.<project-ref>:[YOUR-PASSWORD]@aws-0-us-east-1.pooler.supabase.com:5432/postgres
   ```
5. Put your password in place of `[YOUR-PASSWORD]`.
   - **Percent-encode special characters**: `@` → `%40`, `#` → `%23`, `/` → `%2F`,
     `:` → `%3A`.
   - **Append `?sslmode=require`.** The app converts it for its driver; this is tested.
6. Optional belt and braces: run `infra/supabase/lockdown.sql` in the SQL editor. It's
   the same lockdown as migration `0010`, as a script you can re-run, and its last query
   lists any remaining grants (it should return none).

`pgvector` is enabled by the migrations themselves.

### 2. Generate the credentials-encryption key (on your machine)

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Keep the output for step 3. Paste it only into Render's settings.

### 3. Render (both services)

1. Sign up at render.com with GitHub.
2. Go to **New → Blueprint** and select the repository. Render shows `solutionforge-api`
   and `solutionforge-web`.
3. Fill in the prompted values:
   - `SF_DATABASE_URL`: the Supabase string from step 1;
   - `SF_CREDENTIALS_KEYS`: the key from step 2;
   - `SF_API_URL` (web service): leave it as a placeholder for now, e.g.
     `https://example.com`.
4. **Apply.** Wait until `solutionforge-api` is live, then open
   `https://<api>.onrender.com/readyz`. It should return
   `{"status": "ok", "checks": {"database": "ok"}}`.
5. In **solutionforge-web → Environment**, set `SF_API_URL` to the API's URL
   (`https://<api>.onrender.com`, no trailing slash) and save. Render redeploys the web
   service.
6. Open `https://<web>.onrender.com`. Create an account, create an organization, then
   click **Seed demo data**.

### 4. Verify

```bash
pip install httpx
python apps/api/scripts/smoke.py https://<api>.onrender.com --timeout 120
```

It should end with `smoke test passed`.

## Alternatives

- **Neon instead of Supabase:**
  - create the project in AWS us-east-1;
  - turn **Connection pooling off** and paste the *direct* string as-is (no pausing to
    manage; compute suspends when idle).
  - Migration `0010` is a no-op there.
- **Vercel for the dashboard instead of Render:**
  - import the repo with Root Directory `apps/web` and set `SF_API_URL`;
  - delete the `solutionforge-web` service from the blueprint.

  The BFF routes allow 60 s for a sleeping API and return a clean
  "API is starting up" 503 instead of failing.

## Security notes for this setup

- **Supabase Data API:** closed by code (migration `0010`, with a PostgreSQL regression
  test). Turning it off in settings is the second layer.
- **Public API URL:** the Render API URL is public, protected by auth, RBAC, rate limits and
  the request guard. Browsers only talk to the web service.
- **Forwarded IPs:** `FORWARDED_ALLOW_IPS=*` means a caller hitting the API URL directly can
  spoof their IP and evade **per-IP** rate limits. Per-account limits still apply. Fine for
  a demo; a real deployment keeps the API on a private network.
- **Rotation:**
  - **JWT secret:** regenerate it in Render. Everyone is signed out.
  - **Credentials key:** prepend a new key, comma-separated (`new,old`).
- **Metrics:** `/metrics` is hidden in production unless `SF_METRICS_TOKEN` is set.

## Tear down

Delete both Render services and the Supabase project. Nothing is billed.
