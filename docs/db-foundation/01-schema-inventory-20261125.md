# LAVCCA DB foundation — Phase 1: source-backed schema inventory (2026-11-25)

**Status:** Repository audit complete for the SQL files listed below. **Live Supabase catalog was not queried.** Do not treat this document as evidence of current production or dev DB schema equality. This document is a baseline and implementation contract for the remaining five phases, not an instruction to apply migrations.

## Evidence and precedence
- `infra/supabase/migrations/0001_jobs.sql`: five tables, five RLS ENABLE statements, four authenticated policies, GRANT/REVOKE.
- `infra/supabase/migrations/0002_service_role_grants.sql`: SELECT, INSERT, UPDATE, DELETE on five tables to service_role.
- `infra/README.md`: reports both migrations applied to **dev on 2026-10-21**, catalog checks **8/8**, RLS PASS and API checks **17/17** at that time.
- The header in `0001_jobs.sql` still says “not applied anywhere,” which **conflicts** with the newer `infra/README.md` operational record. The historical report is more recent but still requires fresh read-only catalog verification.
- `infra/supabase/tests/` and `infra/supabase/rollback/` contain existing checks/rollback scripts; their presence is **not** proof of recent restore or rollback success.

## Current schema represented in migrations

| Table | PK | Foreign keys / dependencies | Client policy |
|---|---|---|---|
| `public.jobs` | `id text` | `user_id → auth.users.id` ON DELETE CASCADE | authenticated SELECT own; writes server-side |
| `public.job_steps` | `id bigint identity` | `job_id → public.jobs.id` CASCADE | RLS enabled, no client policy; service role |
| `public.feedback` | `id bigint identity` | `job_id → jobs.id` CASCADE; `user_id → auth.users.id` CASCADE | authenticated SELECT own, INSERT own on completed jobs |
| `public.copyright_matches` | `id bigint identity` | `job_id → jobs.id` CASCADE | authenticated SELECT via owning job |
| `public.labels` | `id bigint identity` | `job_id → jobs.id` SET NULL; `labeler → auth.users.id` | RLS enabled, no client policy; service role |

`jobs` currently has text `pipeline_version` and `model_version` and estimated `llm_cost_usd`; these are **not** relational version lineage or agent execution audit records.

Existing permissions intended by SQL:
- `anon`: explicit `REVOKE ALL` on all five tables and four identity sequences.
- `authenticated`: `SELECT` on jobs/copyright_matches/feedback; column-restricted `INSERT` on feedback; RLS governs ownership.
- `service_role`: SELECT/INSERT/UPDATE/DELETE on all five tables per 0002; Supabase service_role typically bypasses RLS and must never be exposed to client code.
- `TRUNCATE` must not be granted to untrusted roles; RLS does not guard TRUNCATE. A test-only homemade parser is **not** an authoritative PostgreSQL permission validator.

## Missing or unverified versus the six-point master checklist

1. **Schema/migration reality:** The *repository definition* above is inventoried. Live `pg_catalog`/role privileges and applied migration versions are **not yet verified**. Compare dev catalog to the two committed migrations read-only and document drift.
2. **Final ERD:** Not finalized. Decide whether `jobs` remains the execution root or whether `analysis_jobs` becomes canonical; define compatibility for `jobs` API/worker before renaming anything.
3. **Version lineage:** No `model_versions` / `dataset_versions` entities found in the two baseline migrations. Design immutable version IDs, effective digests, model artifact/S3 references, vector index+embedding metadata, and explicit analysis job foreign keys; never retroactively rewrite historical rows.
4. **Agent run metadata:** No dedicated agent/task-run/audit tables in baseline migrations. Design agent registry, agent runs, attempts, orchestrator task ID, reviewed commit/diff digest, approval/verdict, bounded token-cost accounting, immutable event history, and access controls without copying auth secrets or full prompts by default.
5. **Migration/rollback/backup:** Existing files cover some verification and rollback. Prove **fresh** migrate-to-empty, re-apply rejection/idempotence, transaction failure behavior, down path in throwaway DB, logical backup/restore and catalog/data equality; never destructively rehearse on dev/prod.
6. **Auth/RLS/service-role:** Earlier test evidence is historical. In isolated PostgreSQL/Supabase-compatible roles, test positive and negative client access: two users, anon, authenticated, service_role; verify direct grants and effective RLS, including denial of TRUNCATE, unauthorized writes and cross-tenant reads. Do not place service key in CI logs.

## Delivery gates for the next phases

- **Gate A / baseline:** Read-only live dev catalog and migration ledger evidence (or explicitly mark unavailable), drift report, approved ERD.
- **Gate B / design:** model/data version lineage, analysis job relationship, and agent-run schema reviewed as an ERD before any migration applies.
- **Gate C / implementation:** forward-only migration(s) on a PR branch, isolated PostgreSQL migration/restore tests, and real grant/RLS integration assertions.
- **Gate D / deployment:** PM approves exact migration diff and backup/recovery plan. No automatic application to dev or production from the orchestration task.

## Non-goals / risk controls

Do not create a general PostgreSQL parser. Preserve the existing jobs API and worker contract until explicit migration compatibility is proved. Do not change `main`, alter Supabase dev/prod, rotate secrets, or provision paid infrastructure as part of this audit.
