# -*- coding: utf-8 -*-
"""
Production incremental APPLY: write ONLY pages classified UPDATE_CONFIRMED
against their baseline, as one all-or-nothing batch.

  preflight (live re-fetch per candidate, read-only)
    -> SAFE_APPLY_CANDIDATE list (unsafe ones excluded and reported)
  apply_batch
    -> backups/<ts>/<LAVCCA-relative path> for every target + backups/<ts>/state.json
    -> attachment commits (new DOCX files moved in without overwrite; replaced ones backed up)
    -> atomic markdown writes (canary writer)
    -> one atomic state.json commit (items + attachments records)
    -> any failure: remove/restore every committed attachment, restore every written
       markdown + state.json from the batch backup  =>  final result is ALL_APPLIED
       or ROLLED_BACK, never partial.

Attachments (attachment_sync.py): a candidate page's Notion-hosted DOCX blocks are
planned during rendering; in apply mode they are downloaded to temp files during
preflight, so a page whose download fails is excluded before anything is written.

No import-time side effects. The Notion API is injected (read-only GETs only).
"""
import os, sys, json, socket, hashlib
from datetime import datetime, timezone

import canary_apply as ca
import baseline as bl
import attachment_sync as att

SYNCED_RECORD_TYPE = "SYNCED"
# plan marker (notion_sync): Notion parent title differs from the export folder, title
# unchanged -> the existing Obsidian path stays canonical and content is updated in place.
PARENT_ONLY_PATH_POLICY = "parent_title_differs_title_unchanged"
LOCK_NAME = ".sync.lock"
LOCK_STALE_AFTER_S = 6 * 3600

# Statuses that are never written, whatever else is true.
NEVER_WRITE = {"UNCHANGED", "METADATA_ONLY_CHANGE", "CONTENT_HASH_ANOMALY", "LOCAL_MODIFICATION_CONFLICT",
               "RENDER_UNSUPPORTED", "CONTENT_FETCH_ERROR", "CREATE_CANDIDATE", "DELETE_REVIEW_REQUIRED",
               "PATH_POLICY_UNRESOLVED", "ARCHIVED", "TRASHED", "MOVED_OUTSIDE_LAVCCA", "PERMISSION_UNKNOWN",
               "NOT_FOUND_UNKNOWN", "DESCENDANT_UNKNOWN", "CONFLICT", "ERROR", "LOCAL_FILE_UNREADABLE"}


# ============================================================
# Single-instance lock
#   - created with O_CREAT|O_EXCL (atomic on the local filesystem)
#   - same host + holder PID provably dead  -> stale, reclaimed automatically
#   - same host + holder alive              -> refused
#   - other host / unreadable lock          -> refused; if older than 6h it is
#     reported as STALE_UNVERIFIED and must be removed by a human (a PID on
#     another OneDrive-synced machine cannot be checked from here)
# ============================================================
class LockError(Exception):
    def __init__(self, code, detail):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: exists but not ours
        try:
            code = wintypes.DWORD()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _lock_info(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def acquire_lock(sync_dir, run_id, now=None, _pid=None, _host=None):
    """Returns (token, note). Raises LockError if another sync may be running."""
    now = now or datetime.now(timezone.utc)
    path = os.path.join(sync_dir, LOCK_NAME)
    me = {"pid": _pid or os.getpid(), "host": _host or socket.gethostname(), "run_id": run_id,
          "started_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    note = None
    for attempt in (1, 2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            info = _lock_info(path)
            age = now.timestamp() - os.path.getmtime(path)
            if attempt == 1 and info and info.get("host") == me["host"] and not pid_alive(info.get("pid")):
                note = f"reclaimed stale lock of dead pid {info.get('pid')} (run {info.get('run_id')})"
                os.remove(path)
                continue
            if info and info.get("host") == me["host"]:
                raise LockError("LOCKED", f"sync already running: pid {info.get('pid')} run {info.get('run_id')}")
            if age > LOCK_STALE_AFTER_S:
                raise LockError("STALE_UNVERIFIED", f"lock {path} is {int(age)}s old from host "
                                f"{(info or {}).get('host')!r}; holder cannot be verified -- remove it manually")
            raise LockError("LOCKED", f"lock held by host {(info or {}).get('host')!r} ({int(age)}s old)")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(me, f)
            ca._fsync(f)
        return me, note
    raise LockError("LOCKED", "could not acquire lock")


def release_lock(sync_dir, token):
    path = os.path.join(sync_dir, LOCK_NAME)
    info = _lock_info(path)
    if info and info.get("pid") == token["pid"] and info.get("run_id") == token["run_id"]:
        os.remove(path)
        return True
    return False


# ============================================================
# Preflight (read-only)
# ============================================================
def _discard_temps(atts):
    for a in atts:
        tp = a.pop("tmp_path", None)
        if tp:
            try:
                os.remove(tp)
            except OSError:
                pass


def _prepare_attachments(pa, planner, download):
    """Turn the page's attachment plans into batch items. Apply mode (download given):
    download/refresh items are fetched to temp files now; a refresh whose content is
    unchanged becomes state_only, a changed one becomes replace (current bytes kept
    for the backup). Raises att.AttachmentError after discarding this page's temps."""
    items = []
    try:
        for p in (pa.plans if pa else []):
            a = dict(p)
            items.append(a)
            if a["action"] not in ("download", "refresh", "redownload") or download is None:
                continue
            a.update(download(a))
            if a["action"] == "refresh":
                rec = planner.recorded[a["block_id"]]
                if a["sha256"] == rec.get("content_sha256"):
                    a["action"] = "state_only"
                    _discard_temps([a])
                else:
                    a["action"] = "replace"
                    try:
                        with open(planner._abs(a["local_path"]), "rb") as f:
                            a["orig_bytes"] = f.read()
                    except OSError as e:  # e.g. locked: this page only, retried next run
                        raise att.AttachmentError("LOCAL_UNREADABLE", f"{a['local_path']}: {type(e).__name__}")
    except BaseException:
        _discard_temps(items)
        raise
    return items


def preflight_candidate(nid, api, cfg, state_items, identities, now, planner=None, download=None, db_links_map=None):
    """Live re-verification of one UPDATE_CONFIRMED candidate. Returns a dict with
    status SAFE_APPLY_CANDIDATE (+ prepared bytes) or an exclusion status.
    planner: attachment_sync.AttachmentPlanner (None = attachments stay fail-closed);
    download: callable(plan) -> temp file info, given only in apply mode."""
    out = {"notion_id": nid}
    view = bl.baseline_view(state_items.get(nid))
    if view is None:
        return {**out, "status": "EXCLUDED_NO_BASELINE"}
    try:
        target = ca.select_target(nid, identities)
        abs_path = target["abs_path"]
        lavcca_real = os.path.realpath(cfg["lavcca_root_path"])
        if os.path.islink(abs_path) or not os.path.realpath(abs_path).startswith(lavcca_real + os.sep):
            raise ca.CanaryRefused("PROTECTED_TARGET", "outside LAVCCA root or symlink")
        lavcca_rel = os.path.relpath(abs_path, cfg["lavcca_root_path"]).replace(os.sep, "/")
        out["obsidian_path"] = lavcca_rel
        if lavcca_rel != state_items[nid].get("obsidian_path"):
            return {**out, "status": "EXCLUDED_DESTINATION_MISMATCH",
                    "reason": f"state obsidian_path {state_items[nid].get('obsidian_path')!r} != {lavcca_rel!r}"}
        with open(abs_path, "rb") as f:
            orig_bytes = f.read()
        fm, fm_lines, local_body = ca.split_frontmatter_strict(orig_bytes.decode("utf-8"))
        ca.validate_identity(fm, nid)
    except UnicodeDecodeError as e:
        return {**out, "status": "EXCLUDED_INVALID_UTF8", "reason": str(e)}
    except ca.CanaryRefused as e:
        return {**out, "status": f"EXCLUDED_{e.code}", "reason": e.detail}

    code, page = api("GET", f"/pages/{nid}")
    if code != 200:
        return {**out, "status": {404: "NOT_FOUND_UNKNOWN", 403: "PERMISSION_UNKNOWN"}.get(code, "ERROR"),
                "reason": f"direct retrieve HTTP {code}"}
    if ca.norm_id(page.get("id")) != nid:
        return {**out, "status": "ERROR", "reason": "remote id mismatch"}
    if page.get("in_trash"):
        return {**out, "status": "TRASHED"}
    if page.get("archived"):
        return {**out, "status": "ARCHIVED"}
    root_id = ca.norm_id(cfg["lavcca_root_id"])
    if nid == root_id:
        verdict, chain = "DESCENDANT", ["(LAVCCA root itself)"]
    else:
        verdict, chain = ca.verify_descendant(api, page.get("parent"), root_id)
    out["parent_chain"] = chain
    if verdict != "DESCENDANT":
        return {**out, "status": "MOVED_OUTSIDE_LAVCCA" if verdict == "OUTSIDE" else "DESCENDANT_UNKNOWN"}

    node = {"title": ca.page_title(page), "parent": page.get("parent"), "properties": page.get("properties")}
    pa = planner.for_page(nid, lavcca_rel, fm.get("owner")) if planner is not None else None
    try:
        tree = ca.fetch_block_tree(api, nid, cfg)
        ctx = ca.RenderContext(ca.id_to_vault_path(identities), local_body, pa, (db_links_map or {}).get(lavcca_rel))
        new_body = ca.render_body(node["title"], tree, ctx, page=node)
    except ca.ContentFetchError as e:
        return {**out, "status": "CONTENT_FETCH_ERROR", "reason": str(e)}
    except ca.RenderUnsupported as e:
        return {**out, "status": "RENDER_UNSUPPORTED", "reason": e.items}
    except OSError as e:  # a local file could not be read (e.g. locked): this page only
        return {**out, "status": "LOCAL_FILE_UNREADABLE", "reason": f"{type(e).__name__} (errno {e.errno})"}
    api_hash = ca.sha256_text(new_body)
    status, reason = bl.classify_incremental(view, page.get("last_edited_time"), api_hash,
                                             ca.sha256_text(local_body))
    if status != bl.UPDATE_CONFIRMED:
        return {**out, "status": status, "reason": f"no longer UPDATE_CONFIRMED at preflight: {reason}"}

    synced_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        _, new_bytes, new_hash = ca.compose_synced_file(fm, fm_lines, nid, page, new_body, synced_at)
    except ca.CanaryRefused as e:
        return {**out, "status": "EXCLUDED_VALIDATION_FAILED", "reason": e.detail}
    try:
        attachments = _prepare_attachments(pa, planner, download)
    except att.AttachmentError as e:
        return {**out, "status": "EXCLUDED_ATTACHMENT_DOWNLOAD_FAILED", "reason": str(e),
                "attachments": [att.public_plan(p) for p in pa.plans]}
    return {**out, "status": "SAFE_APPLY_CANDIDATE", "abs_path": abs_path, "orig_bytes": orig_bytes,
            "new_bytes": new_bytes, "new_hash": new_hash, "notion_last_edited_time": page.get("last_edited_time"),
            "synced_at": synced_at, "title": node["title"], "attachments": attachments}


def select_update_candidates(plan_rows):
    """UPDATE_CONFIRMED rows only. A QUERY_HIDDEN_ACTIVE row qualifies only through its
    content_status; its own discovery status is never a write reason."""
    ids = []
    for r in plan_rows:
        if r.get("status") == "UPDATE_CONFIRMED":
            ids.append(r["notion_id"])
        elif r.get("status") == "QUERY_HIDDEN_ACTIVE" and r.get("content_status") == "UPDATE_CONFIRMED":
            ids.append(r["notion_id"])
        elif r.get("status") == "PATH_POLICY_UNRESOLVED" and r.get("path_policy") == PARENT_ONLY_PATH_POLICY \
                and r.get("content_status") == "UPDATE_CONFIRMED":
            ids.append(r["notion_id"])  # in place: preflight requires the state path == current path
    return ids


# ============================================================
# One-time FIRST SYNC for explicitly allow-listed pages without a baseline
#   cfg["first_sync_ids"]: notion_ids allowed ONCE. A page qualifies only while it has
#   no state record at all; the successful batch writes a SYNCED record, so the same id
#   can never be first-synced again (it then follows UNCHANGED / UPDATE_CONFIRMED).
#   Any other UPDATE_UNVERIFIED page is never selected.
# ============================================================
FIRST_SYNC_STATUS = "UPDATE_UNVERIFIED"


def select_first_sync_candidates(plan_rows, first_sync_ids, state_items):
    allowed = {ca.norm_id(i) for i in first_sync_ids or [] if ca.ID_RE.match(ca.norm_id(i))}
    return [r["notion_id"] for r in plan_rows
            if r.get("status") == FIRST_SYNC_STATUS and r.get("notion_id") in allowed
            and r["notion_id"] not in state_items]


def preflight_first_sync(nid, api, cfg, state_items, identities, now, planner=None, download=None,
                         db_links_map=None):
    """Live verification of one allow-listed FIRST SYNC page: no state record, local file
    unmodified since migration (detect_conflict), LAVCCA descendant, deterministic render
    (two independent fetch+render passes), no unsupported block, no placeholder."""
    out = {"notion_id": nid, "first_sync": True}
    if nid in state_items:
        return {**out, "status": "EXCLUDED_FIRST_SYNC_ALREADY_DONE", "reason": "state record exists"}
    try:
        target = ca.select_target(nid, identities)
        abs_path = target["abs_path"]
        lavcca_real = os.path.realpath(cfg["lavcca_root_path"])
        if os.path.islink(abs_path) or not os.path.realpath(abs_path).startswith(lavcca_real + os.sep):
            raise ca.CanaryRefused("PROTECTED_TARGET", "outside LAVCCA root or symlink")
        lavcca_rel = os.path.relpath(abs_path, cfg["lavcca_root_path"]).replace(os.sep, "/")
        out["obsidian_path"] = lavcca_rel
        with open(abs_path, "rb") as f:
            orig_bytes = f.read()
        mtime = os.path.getmtime(abs_path)
        fm, fm_lines, local_body = ca.split_frontmatter_strict(orig_bytes.decode("utf-8"))
        ca.validate_identity(fm, nid)
    except UnicodeDecodeError as e:
        return {**out, "status": "EXCLUDED_INVALID_UTF8", "reason": str(e)}
    except ca.CanaryRefused as e:
        return {**out, "status": f"EXCLUDED_{e.code}", "reason": e.detail}

    bootstrap = cfg.get("bootstrap_migration_completed_at")
    bootstrap_ts = datetime.fromisoformat(bootstrap).timestamp() if bootstrap else None
    conflict = ca.detect_conflict(nid, local_body, fm, state_items, mtime, bootstrap_ts)
    if conflict:
        return {**out, "status": bl.LOCAL_MODIFICATION_CONFLICT, "reason": conflict}

    code, page = api("GET", f"/pages/{nid}")
    if code != 200:
        return {**out, "status": {404: "NOT_FOUND_UNKNOWN", 403: "PERMISSION_UNKNOWN"}.get(code, "ERROR"),
                "reason": f"direct retrieve HTTP {code}"}
    if ca.norm_id(page.get("id")) != nid:
        return {**out, "status": "ERROR", "reason": "remote id mismatch"}
    if page.get("in_trash"):
        return {**out, "status": "TRASHED"}
    if page.get("archived"):
        return {**out, "status": "ARCHIVED"}
    verdict, chain = ca.verify_descendant(api, page.get("parent"), ca.norm_id(cfg["lavcca_root_id"]))
    out["parent_chain"] = chain
    if verdict != "DESCENDANT":
        return {**out, "status": "MOVED_OUTSIDE_LAVCCA" if verdict == "OUTSIDE" else "DESCENDANT_UNKNOWN"}

    node = {"title": ca.page_title(page), "parent": page.get("parent"), "properties": page.get("properties")}
    id_map = ca.id_to_vault_path(identities)
    renders = []
    try:
        for _ in range(2):
            pa = planner.for_page(nid, lavcca_rel, fm.get("owner")) if planner is not None else None
            ctx = ca.RenderContext(id_map, local_body, pa, (db_links_map or {}).get(lavcca_rel))
            body = ca.render_body(node["title"], ca.fetch_block_tree(api, nid, cfg), ctx, page=node)
            renders.append((body, pa, ctx.placeholders))
    except ca.ContentFetchError as e:
        return {**out, "status": "CONTENT_FETCH_ERROR", "reason": str(e)}
    except ca.RenderUnsupported as e:
        return {**out, "status": "RENDER_UNSUPPORTED", "reason": e.items}
    except OSError as e:
        return {**out, "status": "LOCAL_FILE_UNREADABLE", "reason": f"{type(e).__name__} (errno {e.errno})"}
    (new_body, pa, placeholders), (body_2, pa_2, _) = renders
    if new_body != body_2 or [att.public_plan(p) for p in (pa.plans if pa else [])] != \
            [att.public_plan(p) for p in (pa_2.plans if pa_2 else [])]:
        return {**out, "status": "EXCLUDED_RENDER_NONDETERMINISTIC", "reason": "two renders differ"}
    if placeholders:
        return {**out, "status": "EXCLUDED_FIRST_SYNC_PLACEHOLDERS", "reason": placeholders}

    synced_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        _, new_bytes, new_hash = ca.compose_synced_file(fm, fm_lines, nid, page, new_body, synced_at)
    except ca.CanaryRefused as e:
        return {**out, "status": "EXCLUDED_VALIDATION_FAILED", "reason": e.detail}
    try:
        attachments = _prepare_attachments(pa, planner, download)
    except att.AttachmentError as e:
        return {**out, "status": "EXCLUDED_ATTACHMENT_DOWNLOAD_FAILED", "reason": str(e),
                "attachments": [att.public_plan(p) for p in pa.plans]}
    return {**out, "status": "SAFE_APPLY_CANDIDATE", "abs_path": abs_path, "orig_bytes": orig_bytes,
            "new_bytes": new_bytes, "new_hash": new_hash, "notion_last_edited_time": page.get("last_edited_time"),
            "synced_at": synced_at, "title": node["title"], "attachments": attachments}


# ============================================================
# Attachment-only commit (attachment isolation)
#   A page whose markdown cannot be rendered safely (RENDER_UNSUPPORTED: an unsupported
#   block, an unreadable/locked local attachment, ...) is never written. Supported
#   attachments of OTHER blocks on that page that are new (action "download") are still
#   downloaded, validated and committed with their state records, so one bad block does
#   not hold back independent files. The markdown link follows once the page renders
#   again (the recorded block is then reused: same path, no second download).
# ============================================================
ATTACHMENT_ONLY_ACTIONS = ("download", "refresh", "redownload")


class _Plans:
    def __init__(self, plans):
        self.plans = plans


def select_attachment_only_candidates(plan_rows):
    """Pages (any status) whose render planned an attachment transfer. Pages that are page
    candidates this run are removed by the caller; their attachments go with the page."""
    return [r["notion_id"] for r in plan_rows
            if r.get("notion_id") and any(p.get("action") in ATTACHMENT_ONLY_ACTIONS
                                          for p in r.get("attachment_plans") or [])]


def preflight_attachment_only(nid, api, cfg, state_items, identities, now, planner=None, download=None,
                              db_links_map=None):
    out = {"notion_id": nid, "attachment_only": True}
    if planner is None:
        return {**out, "status": "EXCLUDED_NO_ATTACHMENT_PLANNER"}
    try:
        target = ca.select_target(nid, identities)
        abs_path = target["abs_path"]
        lavcca_real = os.path.realpath(cfg["lavcca_root_path"])
        if os.path.islink(abs_path) or not os.path.realpath(abs_path).startswith(lavcca_real + os.sep):
            raise ca.CanaryRefused("PROTECTED_TARGET", "outside LAVCCA root or symlink")
        lavcca_rel = os.path.relpath(abs_path, cfg["lavcca_root_path"]).replace(os.sep, "/")
        out["obsidian_path"] = lavcca_rel
        with open(abs_path, "rb") as f:
            fm, _, local_body = ca.split_frontmatter_strict(f.read().decode("utf-8"))
        ca.validate_identity(fm, nid)
    except (UnicodeDecodeError, OSError) as e:
        return {**out, "status": "EXCLUDED_LOCAL_UNREADABLE", "reason": type(e).__name__}
    except ca.CanaryRefused as e:
        return {**out, "status": f"EXCLUDED_{e.code}", "reason": e.detail}
    code, page = api("GET", f"/pages/{nid}")
    if code != 200 or ca.norm_id(page.get("id")) != nid:
        return {**out, "status": "ERROR", "reason": f"direct retrieve HTTP {code}"}
    if page.get("in_trash") or page.get("archived"):
        return {**out, "status": "TRASHED" if page.get("in_trash") else "ARCHIVED"}
    verdict, chain = ca.verify_descendant(api, page.get("parent"), ca.norm_id(cfg["lavcca_root_id"]))
    out["parent_chain"] = chain
    if verdict != "DESCENDANT":
        return {**out, "status": "MOVED_OUTSIDE_LAVCCA" if verdict == "OUTSIDE" else "DESCENDANT_UNKNOWN"}
    node = {"title": ca.page_title(page), "parent": page.get("parent"), "properties": page.get("properties")}
    pa = planner.for_page(nid, lavcca_rel, fm.get("owner"))
    ctx = ca.RenderContext(ca.id_to_vault_path(identities), local_body, pa, (db_links_map or {}).get(lavcca_rel))
    try:
        ca.render_body(node["title"], ca.fetch_block_tree(api, nid, cfg), ctx, page=node)
        blocked_by = []  # page renders, but it is not a page candidate this run (status gate)
    except ca.RenderUnsupported as e:
        blocked_by = [{k: i.get(k) for k in ("type", "id")} for i in e.items]
    except ca.ContentFetchError as e:
        return {**out, "status": "CONTENT_FETCH_ERROR", "reason": str(e)}
    except OSError as e:
        return {**out, "status": "LOCAL_FILE_UNREADABLE", "reason": f"{type(e).__name__} (errno {e.errno})"}
    downloads = [p for p in pa.plans if p["action"] in ATTACHMENT_ONLY_ACTIONS]
    if not downloads:
        return {**out, "status": "EXCLUDED_NO_ATTACHMENT_DOWNLOAD", "page_render_blocked_by": blocked_by}
    try:
        attachments = _prepare_attachments(_Plans(downloads), planner, download)
    except att.AttachmentError as e:
        return {**out, "status": "EXCLUDED_ATTACHMENT_DOWNLOAD_FAILED", "reason": str(e),
                "attachments": [att.public_plan(p) for p in downloads]}
    return {**out, "status": "SAFE_APPLY_CANDIDATE", "abs_path": abs_path, "title": node["title"],
            "synced_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "attachments": attachments,
            "page_render_blocked_by": blocked_by, "new_hash": None}


def metadata_observations(plan_rows, now):
    """METADATA_ONLY_CHANGE: markdown is never written and the baseline fields are
    never touched. Only observed_last_edited_time / observed_at are recorded so the
    observation is visible without being mistaken for a content sync."""
    obs = {}
    for r in plan_rows:
        cs = r.get("content_status") if r.get("status") in ("QUERY_HIDDEN_ACTIVE", "PATH_POLICY_UNRESOLVED") else r.get("status")
        if cs == bl.METADATA_ONLY_CHANGE and r.get("live_last_edited_time"):
            obs[r["notion_id"]] = {"observed_last_edited_time": r["live_last_edited_time"],
                                   "observed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    return obs


# ============================================================
# Batch transaction
# ============================================================
def _canonical(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True)


def _write_verified(path, data):
    ca.atomic_write_bytes(path, data)
    with open(path, "rb") as f:
        if f.read() != data:
            raise ca.WriteVerificationError(f"read-back mismatch for {path}")


ATTACHMENT_WRITE_ACTIONS = ("download", "redownload", "replace")
ATTACHMENT_RECORD_ACTIONS = ("download", "redownload", "replace", "state_only")
NEW_FILE_ACTIONS = ("download", "redownload")  # destination must not exist; never overwritten


def _att_abs(lavcca_root, a):
    return os.path.join(lavcca_root, a["local_path"].replace("/", os.sep))


def _ensure_dirs(path):
    """Create the missing directories of `path`. Returns the ones created,
    outermost first, so a rollback removes exactly those."""
    missing, cur = [], path
    while not os.path.isdir(cur):
        missing.append(cur)
        cur = os.path.dirname(cur)
    created = []
    for d in reversed(missing):
        os.mkdir(d)
        created.append(d)
    return created


def _attachment_record(a, synced_at):
    return {"block_id": a["block_id"], "block_type": a.get("block_type", "file"),
            "attachment_type": a.get("attachment_type", att.SUPPORTED_TYPE),
            "parent_notion_id": a["parent_notion_id"], "owner": a["owner"],
            "original_filename": a["original_filename"], "local_path": a["local_path"],
            "content_sha256": a["sha256"], "size_bytes": a["size_bytes"],
            "block_last_edited_time": a["block_last_edited_time"], "last_synced_at": synced_at}


def apply_batch(safe, state_path, sync_dir, vault_root, now, observations=None, md_snapshot=None, lavcca_root=None):
    """All-or-nothing. Returns a result dict with status
    NOTHING_TO_APPLY | ABORTED_BEFORE_WRITE | ALL_APPLIED | ROLLED_BACK | ROLLBACK_FAILED."""
    observations = observations or {}
    if not safe and not observations:
        return {"status": "NOTHING_TO_APPLY", "applied": [], "observations": 0}
    if not lavcca_root:  # never guess the managed folder name: it differs before/after the rebrand
        raise ValueError("apply_batch: lavcca_root is required")
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    bdir = os.path.join(sync_dir, "backups", stamp)
    res = {"backup_dir": bdir, "applied": [], "written": []}
    atts = [a for c in safe for a in c.get("attachments") or []]
    # attachment-only candidates commit attachments + their state records, never markdown/items
    pages = [c for c in safe if not c.get("attachment_only")]
    att_written, created_dirs = [], []

    # ---- 1. backups + attachment pre-checks (any failure: nothing written) ----
    try:
        with open(state_path, "rb") as f:
            state_bytes = f.read()
        state = json.loads(state_bytes.decode("utf-8"))
        for c in pages:
            if c.get("first_sync") and c["notion_id"] in state["items"]:
                raise ca.WriteVerificationError(f"FIRST SYNC {c['notion_id']}: state record already exists")
            if not c.get("first_sync") and c["notion_id"] not in state["items"]:
                raise ca.WriteVerificationError(f"{c['notion_id']}: no state record for an UPDATE")
        paths = [a["local_path"].lower() for a in atts]
        if len(paths) != len(set(paths)):
            raise ca.WriteVerificationError("two attachments in this batch share a local path")
        lavcca_real = os.path.realpath(lavcca_root)
        for a in atts:
            dst = _att_abs(lavcca_root, a)
            if not os.path.realpath(dst).startswith(lavcca_real + os.sep):
                raise ca.WriteVerificationError(f"attachment {a['local_path']} resolves outside LAVCCA root")
            if a["action"] in ATTACHMENT_RECORD_ACTIONS and not a.get("sha256"):
                raise ca.WriteVerificationError(f"attachment {a['local_path']} was not downloaded")
            if a["action"] in NEW_FILE_ACTIONS and os.path.lexists(dst):
                raise ca.WriteVerificationError(f"attachment destination {a['local_path']} already exists")
        os.makedirs(bdir, exist_ok=False)  # never reuse/overwrite an existing batch backup
        for c in pages:
            ca.write_backup(sync_dir, stamp, c["obsidian_path"], c["orig_bytes"])
        for a in atts:
            if a["action"] == "replace":
                ca.write_backup(sync_dir, stamp, a["local_path"], a["orig_bytes"])
        with open(os.path.join(bdir, "state.json"), "xb") as f:
            f.write(state_bytes)
            ca._fsync(f)
        with open(os.path.join(bdir, "state.json"), "rb") as f:
            if f.read() != state_bytes:
                raise ca.WriteVerificationError("state backup verification failed")
        # originals must be exactly what preflight read
        for c in pages:
            with open(c["abs_path"], "rb") as f:
                if f.read() != c["orig_bytes"]:
                    raise ca.WriteVerificationError(f"{c['obsidian_path']} changed after preflight")
        for a in atts:
            if a["action"] == "replace":
                with open(_att_abs(lavcca_root, a), "rb") as f:
                    if f.read() != a["orig_bytes"]:
                        raise ca.WriteVerificationError(f"{a['local_path']} changed after preflight")
        md_before = md_snapshot() if md_snapshot else None
    except Exception as e:
        return {**res, "status": "ABORTED_BEFORE_WRITE", "detail": f"{type(e).__name__}: {e}"}

    # ---- 2. attachment commits + markdown writes + state commit, rolled back as one unit ----
    att_recorded = {}
    try:
        for a in atts:
            if a["action"] not in ATTACHMENT_WRITE_ACTIONS:
                continue
            dst = _att_abs(lavcca_root, a)
            created_dirs.extend(_ensure_dirs(os.path.dirname(dst)))
            if a["action"] in NEW_FILE_ACTIONS:
                att.commit_new_file(a["tmp_path"], dst)  # refuses to overwrite
                att_written.append(a)                    # registered only once the file is ours
            else:
                att_written.append(a)                    # restoring the original is always safe
                ca._replace(a["tmp_path"], dst)
            a.pop("tmp_path", None)
            if att.sha256_file(dst) != a["sha256"]:
                raise ca.WriteVerificationError(f"attachment {a['local_path']} hash mismatch after commit")
        for c in pages:
            res["written"].append(c)  # registered before the write: a failed replace is restorable too
            _write_verified(c["abs_path"], c["new_bytes"])
        new_state = json.loads(state_bytes.decode("utf-8"))
        for c in pages:
            item = dict(new_state["items"].get(c["notion_id"]) or {})
            if c.get("first_sync"):
                item["first_sync_at"] = c["synced_at"]  # marks the one-time FIRST SYNC
            item.update({
                "notion_id": c["notion_id"],
                "notion_last_edited_time": c["notion_last_edited_time"],
                "api_rendered_content_hash": c["new_hash"],
                "local_content_hash": c["new_hash"],
                "obsidian_path": c["obsidian_path"],
                "last_successful_sync": c["synced_at"],
                # canary-compatible fields (the same values)
                "last_seen_edited_time": c["notion_last_edited_time"],
                "last_synced_content_hash": c["new_hash"],
                "record_type": SYNCED_RECORD_TYPE,
            })
            item.pop("observed_last_edited_time", None)
            item.pop("observed_at", None)
            new_state["items"][c["notion_id"]] = item
        for c in safe:
            for a in c.get("attachments") or []:
                if a["action"] in ATTACHMENT_RECORD_ACTIONS:
                    att_recorded[a["block_id"]] = _attachment_record(a, c["synced_at"])
        if att_recorded:
            new_state.setdefault(att.STATE_KEY, {}).update(att_recorded)
        applied_ids = {c["notion_id"] for c in pages}
        for nid, ob in observations.items():
            if nid in new_state["items"] and nid not in applied_ids:
                new_state["items"][nid] = {**new_state["items"][nid], **ob}
        data = (json.dumps(new_state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        _write_verified(state_path, data)
        # post-commit invariants
        with open(state_path, "rb") as f:
            st = json.loads(f.read().decode("utf-8"))
        if len(st["items"]) != len(state["items"]) + sum(1 for c in pages if c.get("first_sync")):
            raise ca.WriteVerificationError("record count is not previous + FIRST SYNC records")
        for nid, item in state["items"].items():
            if nid not in applied_ids and nid not in observations and _canonical(st["items"][nid]) != _canonical(item):
                raise ca.WriteVerificationError(f"untouched record {nid} changed")
        for k, v in state.items():
            if k not in ("items", att.STATE_KEY) and _canonical(st.get(k)) != _canonical(v):
                raise ca.WriteVerificationError(f"top-level state key {k} changed")
        old_att, new_att = att.state_attachments(state), att.state_attachments(st)
        if set(new_att) != set(old_att) | set(att_recorded):
            raise ca.WriteVerificationError("attachment records are not exactly previous + this batch")
        for bid, rec in old_att.items():
            if bid not in att_recorded and _canonical(new_att[bid]) != _canonical(rec):
                raise ca.WriteVerificationError(f"untouched attachment record {bid} changed")
        for c in safe:
            if c.get("attachment_only"):
                for a in c.get("attachments") or []:
                    if att.sha256_file(_att_abs(lavcca_root, a)) != new_att[a["block_id"]]["content_sha256"]:
                        raise ca.WriteVerificationError(f"{a['local_path']}: file hash != state record")
                continue
            with open(c["abs_path"], "rb") as f:
                body = ca.split_frontmatter_strict(f.read().decode("utf-8"))[2]
            if ca.sha256_text(body) != st["items"][c["notion_id"]]["local_content_hash"]:
                raise ca.WriteVerificationError(f"{c['notion_id']}: state hash != local body hash")
            for a in c.get("attachments") or []:
                # the markdown link must resolve to the committed attachment file
                link_abs = os.path.normpath(os.path.join(os.path.dirname(c["abs_path"]),
                                                         a["link_target"].replace("/", os.sep)))
                if os.path.normcase(link_abs) != os.path.normcase(os.path.normpath(_att_abs(lavcca_root, a))) \
                        or not os.path.isfile(link_abs):
                    raise ca.WriteVerificationError(f"link target {a['link_target']} of {c['obsidian_path']} invalid")
                if a["link_target"] not in body:
                    raise ca.WriteVerificationError(f"{c['obsidian_path']} lacks a link to {a['local_path']}")
                if a["action"] in ATTACHMENT_RECORD_ACTIONS and \
                        att.sha256_file(link_abs) != new_att[a["block_id"]]["content_sha256"]:
                    raise ca.WriteVerificationError(f"{a['local_path']}: file hash != state record")
        if md_snapshot:
            md_after = md_snapshot()
            targets = {os.path.relpath(c["abs_path"], vault_root).replace(os.sep, "/") for c in pages}
            stray = sorted(k for k in set(md_before) | set(md_after)
                           if md_before.get(k) != md_after.get(k) and k not in targets)
            if stray:
                raise ca.WriteVerificationError(f"non-target markdown changed: {stray}")
    except Exception as e:
        rb = _rollback(res["written"], state_path, state_bytes, att_written, created_dirs, lavcca_root)
        res.update(status="ROLLED_BACK" if rb["ok"] else "ROLLBACK_FAILED", detail=f"{type(e).__name__}: {e}",
                   rollback=rb)
        res["written"] = [c["obsidian_path"] for c in res["written"]]
        res["attachments_rolled_back"] = [a["local_path"] for a in att_written]
        return res
    res.update(status="ALL_APPLIED", applied=[c["notion_id"] for c in pages],
               attachment_only_pages=[c["notion_id"] for c in safe if c.get("attachment_only")],
               written=[c["obsidian_path"] for c in res["written"]], observations=len(observations),
               attachments_written=[att.public_plan(a) for a in att_written],
               attachments_recorded=sorted(att_recorded))
    return res


def _rollback(written, state_path, state_bytes, att_written=(), created_dirs=(), lavcca_root=None):
    out = {"ok": True, "files": [], "attachments": []}
    for a in reversed(list(att_written)):
        dst = _att_abs(lavcca_root, a)
        try:
            if a["action"] in NEW_FILE_ACTIONS:
                # only the file this batch moved in (same hash) is removed
                if os.path.exists(dst) and att.sha256_file(dst) == a["sha256"]:
                    os.remove(dst)
                if os.path.exists(dst):
                    raise ca.WriteVerificationError("file at destination is not the one this batch wrote")
            else:
                _write_verified(dst, a["orig_bytes"])
            out["attachments"].append({"path": a["local_path"], "restored": True})
        except Exception as e:
            out["ok"] = False
            out["attachments"].append({"path": a["local_path"], "restored": False,
                                       "error": f"{type(e).__name__}: {e}"})
    for d in reversed(list(created_dirs)):
        try:
            os.rmdir(d)  # only (empty) directories created by this batch
        except OSError as e:
            out["ok"] = False
            out.setdefault("dir_errors", []).append(f"{d}: {type(e).__name__}")
    for c in reversed(written):
        try:
            _write_verified(c["abs_path"], c["orig_bytes"])
            out["files"].append({"path": c["obsidian_path"], "restored": True})
        except Exception as e:
            out["ok"] = False
            out["files"].append({"path": c["obsidian_path"], "restored": False, "error": f"{type(e).__name__}: {e}"})
    try:
        _write_verified(state_path, state_bytes)
        out["state_restored"] = True
    except Exception as e:
        out["ok"] = False
        out["state_restored"] = False
        out["state_error"] = f"{type(e).__name__}: {e}"
    return out


def _public_candidate(p):
    q = {k: v for k, v in p.items() if k not in ("orig_bytes", "new_bytes", "attachments")}
    if p.get("attachments"):
        q["attachments"] = [att.public_plan(a) for a in p["attachments"]]
    return q


def run_production(plan_rows, crawl_ok, api_errors, api, cfg, state_path, sync_dir, identities, now,
                   apply=False, md_snapshot=None, http_get=None):
    """Orchestrator: health gate -> candidate selection -> live preflight -> (apply).
    http_get: transport for attachment downloads in apply mode (default: urllib)."""
    if not crawl_ok:
        return {"status": "ABORTED_CRAWL_HEALTH"}
    if api_errors:
        return {"status": "ABORTED_API_ERRORS", "api_error_count": len(api_errors)}
    with open(state_path, encoding="utf-8") as f:
        state = json.load(f)
    state_items = state["items"]
    planner = att.AttachmentPlanner(cfg["lavcca_root_path"], att.state_attachments(state),
                                    cfg.get("bootstrap_migration_completed_at"))
    db_links_map = ca.load_child_database_links(cfg["lavcca_root_path"])
    tmp_dir = os.path.join(sync_dir, "tmp_attachments")
    download = None
    if apply:
        def download(plan):
            return att.download_to_temp(api, plan, tmp_dir, http_get=http_get or att.http_get_file)
    candidates = select_update_candidates(plan_rows)
    first_sync = select_first_sync_candidates(plan_rows, cfg.get("first_sync_ids"), state_items)
    pre = []
    try:
        for nid in candidates:
            pre.append(preflight_candidate(nid, api, cfg, state_items, identities, now, planner, download,
                                           db_links_map))
        for nid in first_sync:
            pre.append(preflight_first_sync(nid, api, cfg, state_items, identities, now, planner, download,
                                            db_links_map))
        attachment_only = [n for n in select_attachment_only_candidates(plan_rows)
                           if n not in candidates and n not in first_sync]
        for nid in attachment_only:
            pre.append(preflight_attachment_only(nid, api, cfg, state_items, identities, now, planner, download,
                                                 db_links_map))
        safe = [p for p in pre if p["status"] == "SAFE_APPLY_CANDIDATE"]
        excluded = [_public_candidate(p) for p in pre if p["status"] != "SAFE_APPLY_CANDIDATE"]
        observations = metadata_observations(plan_rows, now)
        summary = {"candidates": candidates, "first_sync_candidates": first_sync,
                   "attachment_only_candidates": attachment_only,
                   "safe": [{**{k: p.get(k) for k in ("notion_id", "obsidian_path", "title", "new_hash")},
                             "first_sync": bool(p.get("first_sync")), "attachment_only": bool(p.get("attachment_only")),
                             **({"page_render_blocked_by": p["page_render_blocked_by"]}
                                if p.get("attachment_only") else {})} for p in safe],
                   "excluded": excluded, "metadata_only_observations": observations,
                   "attachment_download_candidates": [att.public_plan(a) for p in safe for a in p["attachments"]
                                                      if a["action"] in ("download", "redownload", "refresh",
                                                                         "replace")],
                   "attachment_plans": [att.public_plan(a) for p in safe for a in p["attachments"]]}
        if not apply:
            return {"status": "PREVIEW", **summary}
        return {**summary, **apply_batch(safe, state_path, sync_dir, cfg["vault_root"], now,
                                         observations=observations, md_snapshot=md_snapshot,
                                         lavcca_root=cfg["lavcca_root_path"])}
    finally:
        for p in pre:
            _discard_temps(p.get("attachments") or [])
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
