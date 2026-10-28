# -*- coding: utf-8 -*-
"""
Baseline adoption + incremental change classification for LAVCCA sync.

ADOPT BASELINE registers the CURRENT Notion state as the comparison baseline
for existing pages WITHOUT touching their Obsidian markdown. The migrated body
(ZIP-export renderer) and the API-rendered body (canary renderer) are different
representations, so both hashes are stored and are allowed to differ:

    api_rendered_content_hash  -- detects future REMOTE changes (same renderer each run)
    local_content_hash         -- detects future LOCAL edits to the managed file

No import-time side effects. No markdown writes anywhere in this module; the
only write primitive is adopt_baselines() for state.json (atomic, backed up).
"""
import os, json
from datetime import datetime, timezone

import canary_apply as ca

BASELINE_FIELDS = ("notion_id", "notion_last_edited_time", "api_rendered_content_hash", "local_content_hash",
                   "obsidian_path", "baseline_adopted_at", "last_successful_sync")
# Marks a record created by ADOPT BASELINE (nothing was written to the markdown).
# Canary/sync records do not carry it (old-style, backward compatible).
ADOPTED_RECORD_TYPE = "ADOPTED_BASELINE"

# incremental statuses for pages that already have a baseline
UNCHANGED = "UNCHANGED"
UPDATE_CONFIRMED = "UPDATE_CONFIRMED"
METADATA_ONLY_CHANGE = "METADATA_ONLY_CHANGE"
CONTENT_HASH_ANOMALY = "CONTENT_HASH_ANOMALY"
LOCAL_MODIFICATION_CONFLICT = "LOCAL_MODIFICATION_CONFLICT"


def baseline_view(item):
    """Uniform view over a state item. Canary-APPLY items (written before baseline
    adoption existed) store last_seen_edited_time / last_synced_content_hash; for
    them the rendered body IS the local body, so both hashes are that value.
    Returns None if the item carries no usable baseline."""
    if not item:
        return None
    edited = item.get("notion_last_edited_time") or item.get("last_seen_edited_time")
    api_hash = item.get("api_rendered_content_hash") or item.get("last_synced_content_hash")
    local_hash = item.get("local_content_hash") or item.get("last_synced_content_hash")
    if not (edited and api_hash and local_hash):
        return None
    return {"notion_last_edited_time": edited, "api_rendered_content_hash": api_hash, "local_content_hash": local_hash,
            "kind": "adopted" if (item.get("record_type") == ADOPTED_RECORD_TYPE or
                                  (item.get("baseline_adopted_at") and not item.get("last_synced_content_hash")))
                    else "synced"}


def classify_incremental(view, current_edited, current_api_hash, current_local_hash):
    """Policy for pages WITH a baseline. Local modification wins over everything:
    a managed file edited outside the engine is never overwritten automatically."""
    if current_local_hash != view["local_content_hash"]:
        return LOCAL_MODIFICATION_CONFLICT, "local body hash differs from baseline local_content_hash"
    edited_changed = current_edited != view["notion_last_edited_time"]
    hash_changed = current_api_hash != view["api_rendered_content_hash"]
    if not edited_changed and not hash_changed:
        return UNCHANGED, "last_edited_time and API-rendered hash equal baseline"
    if edited_changed and hash_changed:
        return UPDATE_CONFIRMED, "last_edited_time and API-rendered hash both changed since baseline"
    if edited_changed:
        return METADATA_ONLY_CHANGE, "last_edited_time changed but API-rendered body identical -- no overwrite"
    return CONTENT_HASH_ANOMALY, "API-rendered hash changed without last_edited_time change -- review, never auto-apply"


def render_current(api, nid, page, local_body, cfg, id_to_path, attachments=None, db_links=None):
    """API-render the page with the canary renderer (blocks + database-row properties).
    page: dict with title, parent, properties. attachments: optional
    attachment_sync.PageAttachments (production sync); db_links: this page's verified
    child_database -> local CSV links. Raises ca.ContentFetchError /
    ca.RenderUnsupported -- callers must exclude the page, never guess."""
    tree = ca.fetch_block_tree(api, nid, cfg)
    body = ca.render_body(page.get("title") or "", tree,
                          ca.RenderContext(id_to_path, local_body, attachments, db_links), page=page)
    return ca.sha256_text(body)


def evaluate_page(nid, target_abs_path, page, api, cfg, state_items, id_to_path, bootstrap_ts,
                  descendant_verified, now, verify_determinism=True, db_links=None):
    """Read-only evaluation of one active managed:notion page.
    page: dict with title, last_edited_time, archived, in_trash, parent, properties
    (crawl node or direct retrieve).
    Returns {"status", "reason", ...}; for ADOPT_CANDIDATE also "entry" (the proposed state item)."""
    out = {"notion_id": nid, "title": page.get("title"), "notion_last_edited_time": page.get("last_edited_time")}
    with open(target_abs_path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8")
        fm, _, local_body = ca.split_frontmatter_strict(text)
        ca.validate_identity(fm, nid)
    except (UnicodeDecodeError, ca.CanaryRefused) as e:
        return {**out, "status": "EXCLUDED_MALFORMED", "reason": str(e)}
    local_hash = ca.sha256_text(local_body)

    if page.get("in_trash"):
        return {**out, "status": "EXCLUDED_TRASHED", "reason": "in_trash=true"}
    if page.get("archived"):
        return {**out, "status": "EXCLUDED_ARCHIVED", "reason": "archived=true"}
    if not descendant_verified:
        return {**out, "status": "EXCLUDED_DESCENDANT_UNVERIFIED", "reason": "LAVCCA descendant not verified"}

    view = baseline_view(state_items.get(nid))
    if view is None and state_items.get(nid):
        return {**out, "status": "EXCLUDED_STATE_INVALID", "reason": "state entry exists but has no usable baseline"}

    if view is None:
        conflict = ca.detect_conflict(nid, local_body, fm, {}, os.path.getmtime(target_abs_path), bootstrap_ts)
        if conflict:
            return {**out, "status": "EXCLUDED_CONFLICT", "reason": conflict}

    try:
        api_hash = render_current(api, nid, page, local_body, cfg, id_to_path, db_links=db_links)
        # Freeze check: an independent second fetch + render must give the same hash,
        # otherwise the baseline would produce false UPDATE/ANOMALY signals.
        api_hash_2 = render_current(api, nid, page, local_body, cfg, id_to_path, db_links=db_links) \
            if verify_determinism else api_hash
    except ca.ContentFetchError as e:
        return {**out, "status": "EXCLUDED_CONTENT_FETCH_ERROR", "reason": str(e)}
    except ca.RenderUnsupported as e:
        return {**out, "status": "EXCLUDED_RENDER_ERROR", "reason": e.items}
    out.update(render_hash_run_1=api_hash, render_hash_run_2=api_hash_2)
    if api_hash != api_hash_2:
        return {**out, "status": "EXCLUDED_RENDER_NONDETERMINISTIC",
                "reason": "two consecutive renders produced different hashes"}

    if view is not None:
        sync_status, reason = classify_incremental(view, page.get("last_edited_time"), api_hash, local_hash)
        return {**out, "status": "ALREADY_BASELINED", "sync_status": sync_status, "reason": reason,
                "baseline_kind": view["kind"], "api_rendered_content_hash": api_hash, "local_content_hash": local_hash}

    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    lavcca_rel = os.path.relpath(target_abs_path, cfg["lavcca_root_path"]).replace(os.sep, "/")
    entry = {
        "notion_id": nid,
        "notion_last_edited_time": page.get("last_edited_time"),
        "api_rendered_content_hash": api_hash,
        "local_content_hash": local_hash,
        "obsidian_path": lavcca_rel,
        "baseline_adopted_at": stamp,
        # Adoption is not a content sync: nothing was written to the markdown.
        "last_successful_sync": None,
        "record_type": ADOPTED_RECORD_TYPE,
    }
    return {**out, "status": "ADOPT_CANDIDATE", "obsidian_path": lavcca_rel,
            "hashes_differ": api_hash != local_hash, "entry": entry,
            "reason": "baseline registrable; markdown body is NOT modified"}


def validate_entry(e):
    missing = [k for k in BASELINE_FIELDS if k not in e]
    if missing:
        raise ValueError(f"baseline entry {e.get('notion_id')} missing {missing}")
    if not ca.ID_RE.match(e["notion_id"] or ""):
        raise ValueError(f"invalid notion_id {e['notion_id']!r}")
    for k in ("api_rendered_content_hash", "local_content_hash"):
        if not (isinstance(e[k], str) and len(e[k]) == 64):
            raise ValueError(f"{e['notion_id']}: {k} is not a sha256 hex")
    if not e["notion_last_edited_time"]:
        raise ValueError(f"{e['notion_id']}: notion_last_edited_time missing")


def _canonical(item):
    return json.dumps(item, ensure_ascii=False, sort_keys=True)


def verify_written_state(state_path, expected_bytes, orig_state, entries):
    """Post-replace validation: bytes as intended, JSON parses, record count is
    exactly before + new, ids unique, every pre-existing record byte-equivalent,
    every new record equal to its entry. Raises on any violation."""
    with open(state_path, "rb") as f:
        got = f.read()
    if got != expected_bytes:
        raise ca.WriteVerificationError("state.json bytes differ from what was written")
    st = json.loads(got.decode("utf-8"))
    items = st.get("items")
    if not isinstance(items, dict):
        raise ca.WriteVerificationError("items missing after write")
    if len(items) != len(orig_state["items"]) + len(entries):
        raise ca.WriteVerificationError(f"record count {len(items)} != {len(orig_state['items'])} + {len(entries)}")
    for nid, item in orig_state["items"].items():
        if _canonical(items.get(nid)) != _canonical(item):
            raise ca.WriteVerificationError(f"pre-existing record {nid} changed")
    for e in entries:
        if items.get(e["notion_id"]) != e:
            raise ca.WriteVerificationError(f"new record {e['notion_id']} not stored as intended")
    for k, v in orig_state.items():
        if k != "items" and st.get(k) != v:
            raise ca.WriteVerificationError(f"top-level key {k} changed")


def adopt_baselines(state_path, entries, sync_dir, now=None):
    """State-only, all-or-nothing write. Order: load -> byte backup
    (backups/<ts>/state.json, never overwritten) -> build full new state in memory
    -> validate (schema, no duplicate ids, existing records untouched) -> temp file
    + flush/fsync -> atomic replace -> re-read/parse/count/uniqueness validation.
    Before the replace, any failure leaves state.json untouched; after it, a
    validation failure restores the backup bytes and re-raises.
    Markdown files are never opened for writing."""
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    with open(state_path, "rb") as f:
        orig_bytes = f.read()
    state = json.loads(orig_bytes.decode("utf-8"))
    if not isinstance(state.get("items"), dict):
        raise ValueError("state.json has no 'items' object")

    backup = os.path.join(sync_dir, "backups", stamp, "state.json")
    os.makedirs(os.path.dirname(backup), exist_ok=True)
    with open(backup, "xb") as f:
        f.write(orig_bytes)
        ca._fsync(f)
    with open(backup, "rb") as f:
        if f.read() != orig_bytes:
            raise ca.WriteVerificationError("state backup verification failed")

    ids = [e.get("notion_id") for e in entries]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate notion_id among baseline entries -- whole adoption aborted")
    new_state = json.loads(json.dumps(state))
    added, skipped = [], []
    for e in entries:
        validate_entry(e)
        if e["notion_id"] in new_state["items"]:
            skipped.append(e["notion_id"])  # existing records (e.g. canary) are never replaced
            continue
        new_state["items"][e["notion_id"]] = dict(e)
        added.append(e["notion_id"])
    for nid, item in state["items"].items():
        if _canonical(new_state["items"][nid]) != _canonical(item):
            raise ValueError(f"existing state item {nid} would change")
    data = (json.dumps(new_state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    json.loads(data.decode("utf-8"))  # round-trip validation before touching disk

    ca.atomic_write_bytes(state_path, data)  # failure here: original untouched, temp removed
    written = [e for e in entries if e["notion_id"] in added]
    try:
        verify_written_state(state_path, data, state, written)
    except Exception:
        ca.atomic_write_bytes(state_path, orig_bytes)
        raise
    return {"added": added, "skipped_existing": skipped, "state_backup": backup,
            "records_before": len(state["items"]), "records_after": len(new_state["items"])}


def prewrite_gate(rows, crawl_ok, api_errors):
    """Whole-run safety gate evaluated right before the state write. Any failure
    aborts the entire adoption (no partial baseline)."""
    fails = []
    if not crawl_ok:
        fails.append("crawl health not PASS")
    if api_errors:
        fails.append(f"{len(api_errors)} API error(s)")
    status = [r.get("status") for r in rows]
    for bad in ("EXCLUDED_CONTENT_FETCH_ERROR", "EXCLUDED_RENDER_NONDETERMINISTIC", "EXCLUDED_CONFLICT",
                "EXCLUDED_DUPLICATE_ID", "EXCLUDED_STATE_INVALID"):
        if bad in status:
            fails.append(f"{status.count(bad)} {bad}")
    lmc = sum(1 for r in rows if r.get("sync_status") == LOCAL_MODIFICATION_CONFLICT)
    if lmc:
        fails.append(f"{lmc} LOCAL_MODIFICATION_CONFLICT")
    cands = [r for r in rows if r.get("status") == "ADOPT_CANDIDATE"]
    ids = [r.get("notion_id") for r in cands]
    if len(ids) != len(set(ids)):
        fails.append("duplicate notion_id among candidates")
    paths = [r["entry"]["obsidian_path"] for r in cands]
    if len(paths) != len(set(paths)):
        fails.append("duplicate obsidian_path among candidates")
    for r in cands:
        if not r.get("render_hash_run_1") or r.get("render_hash_run_1") != r.get("render_hash_run_2"):
            fails.append(f"{r.get('notion_id')}: determinism not proven")
        if r["entry"]["api_rendered_content_hash"] != r.get("render_hash_run_1"):
            fails.append(f"{r.get('notion_id')}: entry hash != verified render hash")
    if not cands:
        fails.append("no ADOPT_CANDIDATE")
    return fails


def run_adoption(state_path, sync_dir, rows, crawl_ok, api_errors, md_snapshot, now=None):
    """Gate -> markdown snapshot -> state-only adoption -> markdown re-check.
    md_snapshot(): {path: sha256} over every markdown file that must stay untouched."""
    fails = prewrite_gate(rows, crawl_ok, api_errors)
    if fails:
        return {"status": "ABORTED_PREWRITE_GATE", "gate_failures": fails}
    md_before = md_snapshot()
    entries = [r["entry"] for r in rows if r.get("status") == "ADOPT_CANDIDATE"]
    try:
        res = adopt_baselines(state_path, entries, sync_dir, now=now)
    except Exception as e:
        return {"status": "FAILED_STATE_UNCHANGED", "detail": f"{type(e).__name__}: {e}"}
    md_after = md_snapshot()
    changed = sorted(k for k in set(md_before) | set(md_after) if md_before.get(k) != md_after.get(k))
    if changed:
        with open(res["state_backup"], "rb") as f:
            ca.atomic_write_bytes(state_path, f.read())
        return {"status": "FAILED_ROLLED_BACK", "detail": "markdown changed during adoption", "markdown_changed": changed,
                **res}
    return {"status": "ADOPTED", "markdown_changed": [], "markdown_checked": len(md_before), **res}
