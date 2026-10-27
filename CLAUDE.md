# CLAUDE.md — lavcca-backend

Agent rules (roles, branches, loop limits, approvals): @AGENTS.md

## What this repo is

Backend and AWS infrastructure for LAVCCA: the worker entry point that runs one analysis job
on ECS, the API/dispatcher Lambdas, Supabase migrations, CloudFormation, and the worker image
build/deploy pipeline. Since 2026-10-27 the **analysis engine lives in ai** and the
training/eval **data in data** (history before the split stays here; `git log --follow`).
Architecture and cross-repo contracts: docs.

## Layout

- `apps/worker/worker.py` — ECS entry point (`python worker.py --job-id J --input <file|s3://...>
  --output-dir <dir> [--output-s3 s3://...] [--no-copyright]`). It imports the engine
  (`mvp_ver_1_9_3`) lazily inside `run_job`; tests replace it with a fake module.
- `apps/worker/Dockerfile`, `.dockerignore` — the worker image. The build context is
  `apps/worker/` **with ai overlaid on it** at the commit/tag in
  `apps/worker/ai-engine.ref` (`.github/actions/build-worker`). Image `/app` = engine root +
  `worker.py`, same as before the split.
- `apps/lambdas/` — API (`api.py`), SQS → ECS dispatcher (`dispatcher.py`), `supa.py`; drafts,
  not deployed.
- `infra/aws/` (CloudFormation, IAM, S3 lifecycle), `infra/supabase/` (migrations, rollback,
  tests). Procedures and applied state: `infra/README.md`.
- `.github/workflows/worker-image.yml` — PR: build + smoke + Trivy (no AWS creds). main push /
  manual: `lavcca-dev` environment approval → OIDC → ECR push. `auth_check_only=true` only
  checks OIDC → AWS. `tests.yml` — pytest.

## Rules

- Only this repo deploys to AWS. Never create AWS roles for other repos.
- Changing the engine version = a PR that edits `apps/worker/ai-engine.ref` (ai tag).
  If the engine interface changes (`analyze_only`, `finalize_reports`, `CopyrightRunner`,
  `run_logging`, report file names), update `worker.py`, its tests and docs together.
- AWS changes go through CloudFormation Change Sets reviewed and approved by the PM; never edit
  IAM policies by hand. The OIDC trust uses the immutable subject
  (`repo:LAVCCA-COMPANY@298266724/lavcca-backend@1285873680:environment:lavcca-dev:ref:...`).
- Never commit secrets, `.env`, databases or generated output. Small, reviewable changes; no
  destructive Git operations.

## Testing

    pip install --require-hashes -r requirements-dev.txt
    python -m pytest apps/worker/tests apps/lambdas/tests .claude/hooks -q

Tests use mocks only (no AWS, Supabase or API keys). The image build needs the repository
secret `LAVCCA_AI_READ_TOKEN` (read-only access to ai). Python 3.11.
