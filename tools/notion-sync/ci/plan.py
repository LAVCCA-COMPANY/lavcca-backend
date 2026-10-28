"""Runner-side decisions around the unchanged engine: what to sync, how to fall back, and
whether what state.json claims is actually on disk. Pure logic + subprocess; no Graph here."""
import os, json, tempfile
from datetime import datetime, timedelta

import canary_apply as ca

# last_edited_time is rounded to the minute and Notion search lags its index: re-read a window.
OVERLAP = timedelta(minutes=5)
MAX_TARGETS = 30  # beyond this a full crawl is cheaper than per-page direct retrieval
# Anything that can add, move or remove a page needs the root crawl to place it.
FULL_EVENT_PREFIXES = ("database.", "data_source.")
FULL_EVENT_TYPES = {"page.created", "page.moved", "page.deleted", "page.undeleted", "page.locked", "page.unlocked"}
EXIT_NEEDS_FULL = 9  # engine: a --targets id is not an active managed page


def norm_id(i):
    return str(i).replace("-", "").lower()


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def plan_run(search_results, state_items, cursor, cfg, hinted_ids=(), event_types=()):
    """Decide the next run.

    search_results: Notion /v1/search objects sorted by last_edited_time descending (caller pages
                    until an object is older than cursor - OVERLAP or results end).
    cursor:         ISO time of the last successful run's start, or None.
    hinted_ids / event_types: from webhook payloads (may be duplicated, coalesced or missing).
    Returns (mode, targets, reasons) with mode in "none" | "incremental" | "full".
    """
    if cursor is None:
        return "full", [], ["no cursor: first run or cursor lost"]
    full = [f"event {t}" for t in sorted(set(event_types))
            if t in FULL_EVENT_TYPES or t.startswith(FULL_EVENT_PREFIXES)]
    ignored = {norm_id(i) for i in (cfg.get("excluded_ids") or []) + (cfg.get("review_ids") or [])}
    since = _ts(cursor) - OVERLAP
    targets, seen = set(), set()
    for obj in search_results:
        if _ts(obj["last_edited_time"]) < since:
            break
        nid = norm_id(obj["id"])
        seen.add(nid)
        if nid in ignored:
            continue
        if obj.get("object") != "page":
            full.append(f"{obj.get('object')} {nid} edited")  # new rows / schema change
            continue
        st = state_items.get(nid)
        if st is None:
            # shortcut: a page edited outside LAVCCA also lands here and costs one full crawl;
            # keep a known-outside id cache if that shows up in Actions minutes.
            full.append(f"page {nid} not in state (new, moved in, or outside LAVCCA)")
        elif obj.get("in_trash") or obj.get("archived"):
            full.append(f"page {nid} trashed/archived")
        elif obj["last_edited_time"] not in (st.get("notion_last_edited_time"), st.get("last_seen_edited_time")):
            targets.add(nid)
    for nid in map(norm_id, hinted_ids):  # webhook ids the search index has not caught up with yet
        if nid in seen or nid in ignored:
            continue
        if nid in state_items:
            targets.add(nid)
        else:
            full.append(f"webhook page {nid} not in state")
    if full:
        return "full", [], full
    if len(targets) > MAX_TARGETS:
        return "full", [], [f"{len(targets)} targets > {MAX_TARGETS}"]
    if not targets:
        return "none", [], ["no page edited since cursor"]
    return "incremental", sorted(targets), []


def execute(mode, targets, run_engine):
    """Run the engine for a plan_run decision. run_engine(args) -> exit code.
    Returns (exit_code, mode_actually_run). Any non-zero code means: upload nothing, keep the cursor."""
    if mode == "none":
        return 0, "none"
    if mode == "incremental":
        fd, path = tempfile.mkstemp(suffix=".json", prefix="targets_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(targets, f)
            code = run_engine(["--apply-updates", "--targets", path])
        finally:
            os.remove(path)
        if code != EXIT_NEEDS_FULL:
            return code, "incremental"
    return run_engine(["--apply-updates"]), "full"


def audit_mirror(lavcca_root, state):
    """Files state.json says were synced but whose bytes say otherwise (e.g. an upload that
    'succeeded' without changing the file). Returns a list of {path, problem}; empty = consistent."""
    bad = []
    for nid, st in (state.get("items") or {}).items():
        rel = st.get("obsidian_path")
        want = st.get("local_content_hash")
        if not rel or not want:
            continue
        p = os.path.join(lavcca_root, *rel.split("/"))
        try:
            with open(p, "rb") as f:
                body = ca.split_frontmatter_strict(f.read().decode("utf-8"))[2]
        except FileNotFoundError:
            bad.append({"path": rel, "problem": "missing"})
            continue
        except (OSError, UnicodeDecodeError, ca.CanaryRefused) as e:
            bad.append({"path": rel, "problem": f"unreadable: {type(e).__name__}"})
            continue
        if ca.sha256_text(body) != want:
            bad.append({"path": rel, "problem": "body hash != state local_content_hash"})
    for bid, a in (state.get("attachments") or {}).items():
        rel, want = a.get("local_path"), a.get("content_sha256")
        if not rel or not want:
            continue
        p = os.path.join(lavcca_root, *rel.split("/"))
        if not os.path.isfile(p):
            bad.append({"path": rel, "problem": "missing"})
            continue
        with open(p, "rb") as f:
            if ca.sha256_bytes(f.read()) != want:
                bad.append({"path": rel, "problem": "attachment hash != state content_sha256"})
    return bad
