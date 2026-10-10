# CLAUDE.md

Guidance for Claude Code (and other agents) working in this repository.

## What this repo is

NATAM: analyzes creator video/content for crisis ("controversy") risk, and
separately scans video for copyright infringement, then produces a combined
Markdown/PDF/JSON report.

## Active vs. legacy vs. orphaned code

- **`mvp 1_9_3/` is the active application.** All new work happens here
  unless told otherwise.
- **`mvp 1_9_2/` is legacy/reference code**, kept for history. Do not modify
  it unless explicitly requested, even if it looks like the "canonical"
  version by name.
- **Root-level `copyright_detector/`** (next to `mvp 1_9_2/` and
  `mvp 1_9_3/`) is an **orphaned leftover** — it only contains empty
  `models/` and `results/` scaffolding, no code. It is not used by anything.
  Do not treat it as the copyright detector.
- **The active copyright detector is `mvp 1_9_3/copyright_detector/`.** This
  is the only copyright-detection code that runs in practice.

## Current scope vs. planned expansion

- **`mvp 1_9_3/` is the current active application source of truth** — not
  a permanent ceiling on what these agents will ever work on. As code is
  actually added to this repo, agent scope expands with it.
- **Planned direction**: NATAM is expected to expand toward AWS (raw video
  storage, large-scale data processing, and large-scale AI training
  preprocessing) plus a new AI training/evaluation/inference pipeline, and
  toward Supabase for lightweight customer/application data.
- **Do not assume this planned AWS/Supabase architecture is already
  finalized or implemented** — it is a stated future direction, not a fact
  about the current codebase. Separately, do not assume the current
  codebase has *no* AWS-related code or dependencies either — check what's
  actually in the repo (files, imports, config) before claiming either way.
  Always keep what you've verified in the current code distinct from what
  is merely planned.
- **A future `cloud-infra-engineer` agent (Harness v2)** will own AWS/
  Supabase infrastructure itself (provisioning, network, IAM, cost,
  deployment). The existing agents (backend-architect, ai-pipeline-engineer,
  data-quality-engineer, qa-engineer) own the application/data/AI logic
  that runs on top of that infrastructure, not the infrastructure itself.
- Do not invent specific AWS services, resource names, or a Supabase
  schema that you haven't confirmed exist in this repo.

## Entry points

Run everything from inside `mvp 1_9_3/` (paths below are relative to it).

- **Web UI**: `python app.py` — Flask app on `0.0.0.0:5000`, serves
  `../templates/index.html`, drives analysis via `mvp_ver_1_9_3`.
- **CLI**: `python mvp_ver_1_9_3.py` — interactive loop; accepts a YouTube
  URL, Google Drive URL, local video/audio path, or raw text, and asks
  whether to run the copyright scan alongside the crisis analysis.
- **Copyright detector standalone**:
  - `python copyright_detector/main.py "video.mp4"` (add `--force` to
    re-analyze ignoring cache)
  - `python copyright_detector/main.py --db-admin` — local web UI
    (`127.0.0.1:8765`) for editing the learned-data DB
  - `python copyright_detector/tui.py` — terminal UI
  - See `mvp 1_9_3/copyright_detector/READ ME.md` for the fuller command list.
- **Similar-case engine** (build/maintenance commands): see
  `mvp 1_9_3/SIMILAR_CASE_ENGINE_NOTES.md` — do not guess at usage from code
  alone, that file documents current index state and known limitations.

## Two-process architecture

`mvp_ver_1_9_3.py` is the orchestrator. It:

1. Runs the NATAM crisis/controversy analysis **in-process**, by importing
   `mvp_ver_1_9_2.py` (the `CrisisConsultantSystem` engine — note there is a
   copy of this file inside `mvp 1_9_3/` itself; that copy is the one
   actually used, not the standalone script in `mvp 1_9_2/`).
2. Runs the copyright scan as a **separate subprocess**
   (`run_copyright_detection()` in `mvp_ver_1_9_3.py`), invoking
   `copyright_detector/main.py` with `cwd=copyright_detector/` and
   `--format json`. This is deliberate, not incidental: the detector assumes
   its own working directory for its `.env`, its SQLite DB, and other
   relative paths, so it cannot simply be imported.
3. Reads the result back by **polling `copyright_detector/results/` for the
   newest `result_*.json`** written after the subprocess started — there is
   no in-memory/IPC channel between the two processes.

If you change the subprocess invocation, the result-file contract, or the
copyright detector's CLI args, both sides (`mvp_ver_1_9_3.py` and
`copyright_detector/main.py`) need to stay in sync.

## Environment configuration (two separate `.env` files)

- **Main system**: `.env` at the repo root (copy from root `.env.example`) —
  `GEMINI_API_KEY`, `NAVER_CLIENT_ID`/`NAVER_CLIENT_SECRET`, optional Clova
  NEST ASR keys.
- **Copyright detector**: its own `.env` inside
  `mvp 1_9_3/copyright_detector/` (copy from
  `mvp 1_9_3/copyright_detector/.env.example`) — ACRCloud/AudD (music),
  Google Cloud Vision (logo/image), YouTube Data API, AWS
  (S3/ECS/Rekognition), and its local SQLite `DATABASE_URL`.

These are not interchangeable — because the detector runs with its own
`cwd`, it will not pick up the root `.env` and vice versa.

## Safety rules

- **Never commit**: API keys, secrets, `.env` files, generated reports,
  databases (including `.sqlite`/`.sqlite.bak`), the `casedb/` case
  database, `downloads/`, `transcripts/`, embeddings/model artifacts (e.g.
  `.npy`, `.joblib`), or other large generated output. Check `.gitignore`
  (root and `mvp 1_9_3/copyright_detector/.gitignore`) before adding files
  outside of it.
- **Do not modify business-rule YAML files** (`rules.yaml`,
  `controversy_labels.yaml`, `worst_actions_map.yaml`) **or report
  templates** (`report_template_v1_9_3.md`, `mvp 1_9_2/report template.md`)
  unless explicitly requested — these encode reviewed policy/output
  contracts, not incidental config.
- **Do not modify legacy code** (`mvp 1_9_2/`) or the orphaned root
  `copyright_detector/` unless explicitly requested.
- **Before making substantial changes**, inspect the relevant code first
  and explain the plan rather than jumping straight to edits.
- **Prefer small, reviewable changes** over broad refactors.
- **No destructive Git operations** (force-push, hard reset, history
  rewrite, branch deletion, etc.) without explicit user instruction.

## Windows / PowerShell / Korean-path considerations

- The repo lives under a Korean-named path (`바탕 화면`, i.e. "Desktop"),
  and the active app folder name contains a space (`mvp 1_9_3`). Always
  quote paths in commands.
- `similar_case_engine.py` deliberately does **not** persist a FAISS index
  file — `faiss.write_index` fails on this Windows/Korean-path setup. It
  reconstructs an in-memory `IndexFlatIP` from the `.npy` embeddings on
  every load instead. Don't "fix" this by reintroducing a saved FAISS index
  without checking `SIMILAR_CASE_ENGINE_NOTES.md` first.
- The copyright detector subprocess is launched with `sys.executable`
  and an explicit `cwd`, which matters more on Windows (no shell-relative
  path resolution the way a POSIX shell might do it) — keep that pattern if
  you touch `run_copyright_detection()`.
- README notes the project targets **Python 3.14**.

## Testing

There is currently **no conventional automated test suite** (no
pytest/unittest suite wired to a runner). `copyright_detector/tools/`
contains an eval harness and sample labels, but that's a manual evaluation
tool, not CI. Do not claim something is "tested" or "verified" unless you
actually ran it in this session — describe what you ran and its output.

## Reference

- Similar-case engine (architecture, index state, rebuild/resume commands,
  known limitations): `mvp 1_9_3/SIMILAR_CASE_ENGINE_NOTES.md`.
