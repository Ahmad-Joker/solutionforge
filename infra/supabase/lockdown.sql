-- SolutionForge on Supabase: close the auto-generated Data API over our tables.
--
-- Supabase exposes the `public` schema through PostgREST and, by default, grants the
-- `anon` and `authenticated` roles access to every new table. SolutionForge never uses that
-- API: it enforces tenancy and RBAC in its own service layer and connects as the owner role.
-- Left as-is, anyone holding the project's (public-by-design) anon key could read and write
-- these tables directly, bypassing all of that.
--
-- Run once in the Supabase SQL editor BEFORE the first deploy (so the default-privilege
-- change also covers tables the migrations create), and re-run any time; it is idempotent.
-- Also turn off the Data API in Project Settings → Data API.

revoke all on all tables    in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
revoke all on all routines  in schema public from anon, authenticated;

alter default privileges in schema public revoke all on tables    from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;
alter default privileges in schema public revoke all on routines  from anon, authenticated;

-- Verify: should return zero rows.
select table_name, grantee, privilege_type
from information_schema.role_table_grants
where table_schema = 'public' and grantee in ('anon', 'authenticated');
