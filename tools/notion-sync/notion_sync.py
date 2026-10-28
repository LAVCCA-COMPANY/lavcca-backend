# -*- coding: utf-8 -*-
"""
LAVCCA (라브카) Notion -> Obsidian one-way incremental sync engine, v1.

Default mode (no arguments) is a full read-only DRY RUN: it never creates,
modifies, deletes, or moves any Obsidian file, and it never writes to Notion
(only GET / POST-query read endpoints are called).

The ONLY write path is CANARY APPLY for exactly one explicitly named page:
    python notion_sync.py --apply --notion-id <32-hex id>
(see canary_apply.py). `--notion-id` without `--apply` is a single-page dry run.
`--apply` without `--notion-id` is refused. Full / multi-ID APPLY does not exist.

Safety boundaries:
- Reads NOTION_TOKEN via secret_loader (env var, then ~/.natam/secrets.env -- outside
  OneDrive; the vault .env is never used); never logs/prints/stores it.
- Only touches files under <managed root>/_sync/logs/ (output) -- everything else is
  read-only, including 06_KNOWLEDGE/, 99_ARCHIVE/, _migration/, and every
  managed:notion markdown file.
"""
import os, re, sys, json, time, hashlib, argparse, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone
from collections import defaultdict

SYNC_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SYNC_DIR)
import canary_apply  # no import-time side effects
import baseline as bl  # no import-time side effects
import production_apply as prod  # no import-time side effects
import attachment_sync  # no import-time side effects

# ------------------------------------------------------------
# CLI gating -- resolved BEFORE the token is read or any API call is made
# ------------------------------------------------------------
_ap = argparse.ArgumentParser(description="LAVCCA (라브카) Notion -> Obsidian sync (DRY-RUN by default)")
_ap.add_argument("--apply", action="store_true", help="CANARY APPLY; requires exactly one --notion-id")
_ap.add_argument("--notion-id", action="append", dest="notion_ids", metavar="ID")
_ap.add_argument("--adopt-baseline", action="store_true",
                 help="ADOPT BASELINE: register current Notion state in state.json only (never writes markdown); "
                      "add --dry-run for a preview")
_ap.add_argument("--apply-updates", action="store_true",
                 help="production incremental APPLY: write only UPDATE_CONFIRMED pages as one all-or-nothing batch; "
                      "add --dry-run for a preview")
_ap.add_argument("--dry-run", action="store_true", help="explicit dry run (default behaviour anyway)")
_ap.add_argument("--targets", metavar="FILE",
                 help="with --apply-updates: JSON list of notion_ids; skip the root crawl and check only these "
                      "already-synced pages. exit 9 = a target is not an active managed page (run a full crawl)")
_args = _ap.parse_args()
CLI_MODE, CLI_NOTION_ID, _refusal = canary_apply.resolve_cli_mode(
    _args.apply, _args.notion_ids, adopt_baseline=_args.adopt_baseline, dry_run=_args.dry_run,
    apply_updates=_args.apply_updates)
if CLI_MODE == "refuse":
    print(f"REFUSED: {_refusal}")
    sys.exit(3)
INCREMENTAL_TARGETS = None
if _args.targets:
    if CLI_MODE not in ("apply_updates_preview", "apply_updates_write"):
        print("REFUSED: --targets requires --apply-updates")
        sys.exit(3)
    with open(_args.targets, encoding="utf-8") as f:
        INCREMENTAL_TARGETS = {str(t).replace("-", "").lower() for t in json.load(f)}
with open(os.path.join(SYNC_DIR, "config.json"), encoding="utf-8") as f:
    CFG = canary_apply.normalize_config(json.load(f))
attachment_sync.configure_owner_roots(CFG["owner_roots"])

LAVCCA_ROOT_PATH = CFG["lavcca_root_path"]
# managed folder inside the vault, as configured ("NATAM" in vaults not yet migrated)
ROOT_REL = os.path.relpath(LAVCCA_ROOT_PATH, CFG["vault_root"]).replace(os.sep, "/")
LOGS_DIR = os.path.join(SYNC_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

# Single-instance lock for every mode that can write (markdown or state.json).
WRITE_MODES = ("canary_apply", "adopt_baseline_write", "apply_updates_write")
if CLI_MODE in WRITE_MODES:
    import atexit
    try:
        _LOCK, _lock_note = prod.acquire_lock(SYNC_DIR, RUN_ID)
    except prod.LockError as e:
        print(f"REFUSED ({e.code}): {e.detail}")
        sys.exit(7)
    if _lock_note:
        print(f"LOCK: {_lock_note}")
    atexit.register(prod.release_lock, SYNC_DIR, _LOCK)


def log(msg):
    print(msg)


# ============================================================
# 0. Token loading -- never logged, never returned in any report structure
# ============================================================
import secret_loader

try:
    TOKEN, TOKEN_SOURCE = secret_loader.load_notion_token()
except secret_loader.TokenNotConfigured as e:
    log(f"FATAL {e.code}: {e} -- aborting before any API call.")
    sys.exit(1)
log(f"NOTION_TOKEN source: {TOKEN_SOURCE}")  # the source only, never the value

# LAVCCA_NOTION_API_BASE (legacy NATAM_NOTION_API_BASE): E2E test harness only (a fake Notion server on 127.0.0.1). Any
# other value is ignored, so production always talks to api.notion.com.
_API_BASE_OVERRIDE = canary_apply.env("NOTION_API_BASE", "")
API_BASE = _API_BASE_OVERRIDE if re.match(r"^http://127\.0\.0\.1:\d+/v1$", _API_BASE_OVERRIDE) \
    else "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Notion-Version": CFG["notion_version"],
    "Content-Type": "application/json",
}
del TOKEN  # do not keep the raw token bound any longer than needed for HEADERS


# ============================================================
# 1. HTTP layer -- errors are recorded, never silently swallowed
# ============================================================
API_ERRORS = []
API_CALL_COUNT = 0
MAX_TRANSIENT_RETRIES = 3          # network errors and 502/503/504: 1s, 2s, 4s backoff
TRANSIENT_HTTP_CODES = {502, 503, 504}


def api_call(method, path, body=None, _retry=0, record_errors=True):
    """record_errors=False is used ONLY by the post-crawl direct-retrieve
    fallback, where a 403/404 is an expected classification outcome rather
    than a crawl-health failure. Those outcomes are still reported, via the
    fallback's own result list."""
    global API_CALL_COUNT
    API_CALL_COUNT += 1
    url = API_BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.getcode(), json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in TRANSIENT_HTTP_CODES and _retry < MAX_TRANSIENT_RETRIES:
            time.sleep(2 ** _retry)
            return api_call(method, path, body, _retry + 1, record_errors)
        if e.code == 429 and _retry < 5:
            retry_after = e.headers.get("Retry-After", "1")
            try:
                wait = float(retry_after)
            except ValueError:
                wait = 2.0  # Retry-After was an HTTP-date or unparseable; back off flatly
            time.sleep(wait + 0.25)
            return api_call(method, path, body, _retry + 1, record_errors)
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = {"message": str(e)}
        err = {"path": path, "method": method, "code": e.code,
               "notion_code": payload.get("code"), "notion_status": payload.get("status")}
        if record_errors:
            API_ERRORS.append(err)
        return e.code, payload
    except (urllib.error.URLError, OSError, ValueError) as e:
        # URLError: DNS/connection; OSError: socket timeouts / RemoteDisconnected;
        # ValueError: undecodable/non-JSON response body. Transient network failures
        # are retried a bounded number of times; the final failure is recorded, never swallowed.
        if not isinstance(e, ValueError) and _retry < MAX_TRANSIENT_RETRIES:
            time.sleep(2 ** _retry)
            return api_call(method, path, body, _retry + 1, record_errors)
        err = {"path": path, "method": method, "code": None,
               "message": f"network/parse error ({type(e).__name__})"}
        if record_errors:
            API_ERRORS.append(err)
        return None, {"message": str(e)}


# ============================================================
# 1b. Single-page canary (dry run or apply) -- does not run the full crawl
# ============================================================
if CLI_MODE in ("canary_dry_run", "canary_apply"):
    log(f"=== {'CANARY APPLY' if CLI_MODE == 'canary_apply' else 'CANARY DRY-RUN'} for {CLI_NOTION_ID} ===")
    _res = canary_apply.run_canary(CLI_NOTION_ID, CFG, api_call, SYNC_DIR, apply=(CLI_MODE == "canary_apply"))
    _res["api_call_count"] = API_CALL_COUNT
    _res["api_errors"] = API_ERRORS
    _out = os.path.join(LOGS_DIR, f"canary_{_res['run_id']}_{CLI_NOTION_ID}.json")
    with open(_out, "w", encoding="utf-8") as f:
        json.dump(_res, f, ensure_ascii=False, indent=2)
    log(json.dumps(_res, ensure_ascii=False, indent=2))
    log(f"Canary report: {_out}")
    sys.exit(0 if _res["status"] in ("APPLIED", "DRY_RUN_OK") else 4)


# ============================================================
# 2. Pagination-safe iterators (the specific defenses requested)
# ============================================================
class PaginationError(Exception):
    pass


def paginate(fetch_page_fn, max_pages):
    """fetch_page_fn(cursor) -> (ok: bool, results: list, has_more: bool, next_cursor: str|None)
    Yields all results. Raises PaginationError on: cursor repeat, missing cursor
    while has_more, exceeding max_pages, a page fetch failure, or the same
    object id appearing twice within this one paginated result set. No silent
    continue -- any of these aborts the whole collection as INCOMPLETE."""
    seen_cursors = set()
    seen_ids = set()
    cursor = None
    pages = 0
    while True:
        pages += 1
        if pages > max_pages:
            raise PaginationError(f"max_pagination_pages ({max_pages}) exceeded")
        ok, results, has_more, next_cursor = fetch_page_fn(cursor)
        if not ok:
            raise PaginationError("page fetch failed (API error)")
        for r in results:
            rid = (r.get("id") or "").replace("-", "").lower()
            if rid:
                if rid in seen_ids:
                    raise PaginationError(f"duplicate id {rid} within a single paginated result set")
                seen_ids.add(rid)
            yield r
        if not has_more:
            return
        if not next_cursor:
            raise PaginationError("has_more=true but next_cursor missing/empty")
        if next_cursor in seen_cursors:
            raise PaginationError(f"next_cursor repeated ({next_cursor}) -- would loop forever")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def fetch_block_children_page(block_id, cursor):
    path = f"/blocks/{block_id}/children?page_size={CFG['api_page_size']}"
    if cursor:
        path += f"&start_cursor={urllib.parse.quote(cursor)}"
    code, resp = api_call("GET", path)
    if code != 200:
        return False, [], False, None
    return True, resp.get("results", []), resp.get("has_more", False), resp.get("next_cursor")


def fetch_data_source_query_page(ds_id, cursor):
    body = {"page_size": CFG["api_page_size"]}
    if cursor:
        body["start_cursor"] = cursor
    code, resp = api_call("POST", f"/data_sources/{ds_id}/query", body)
    if code != 200:
        return False, [], False, None
    return True, resp.get("results", []), resp.get("has_more", False), resp.get("next_cursor")


# ============================================================
# 3. Recursive crawl
# ============================================================
def get_title_from_page_properties(props):
    for v in props.values():
        if isinstance(v, dict) and v.get("type") == "title":
            return "".join(p.get("plain_text", "") for p in v.get("title", []))
    return None


CRAWL_INCOMPLETE_REASONS = []
visited_pages = {}
visited_dbs = {}
visited_data_sources = {}   # ds_id -> {"database_id", "name"} or None on failure
non_page_query_results = []  # data source query results that are not pages (reported, never dropped silently)
attachments = []


def record_incomplete(where, err):
    CRAWL_INCOMPLETE_REASONS.append({"where": where, "error": str(err)})


def crawl_page(page_id, parent_hint=None):
    page_id = page_id.replace("-", "").lower()  # normalize BEFORE using as dict key (API accepts both forms)
    if page_id in visited_pages:
        return
    code, page = api_call("GET", f"/pages/{page_id}")
    if code != 200:
        record_incomplete(f"GET /pages/{page_id}", f"HTTP {code}")
        visited_pages[page_id] = None
        return
    node = {
        "notion_id": page["id"].replace("-", "").lower(),
        "object_type": "page",
        "title": get_title_from_page_properties(page.get("properties", {})),
        "parent": page.get("parent"),
        "last_edited_time": page.get("last_edited_time"),
        "archived": page.get("archived"),
        "in_trash": page.get("in_trash"),
        "properties": page.get("properties"),
    }
    visited_pages[page_id] = node
    crawl_block_children(page_id)


def crawl_database(db_id):
    db_id = db_id.replace("-", "").lower()  # normalize BEFORE using as dict key (API accepts both forms)
    if db_id in visited_dbs:
        return
    code, db = api_call("GET", f"/databases/{db_id}")
    if code != 200:
        record_incomplete(f"GET /databases/{db_id}", f"HTTP {code}")
        visited_dbs[db_id] = None
        return
    node = {
        "notion_id": db["id"].replace("-", "").lower(),
        "object_type": "database",
        "title": "".join(p.get("plain_text", "") for p in db.get("title", [])),
        "parent": db.get("parent"),
        "last_edited_time": db.get("last_edited_time"),
        "archived": db.get("archived"),
        "in_trash": db.get("in_trash"),
    }
    visited_dbs[db_id] = node
    # Notion-Version 2025-09-03: a database is a container of one or more data
    # sources; rows are queried per data source. Discovered from metadata only.
    data_sources = db.get("data_sources")
    if not isinstance(data_sources, list) or not data_sources:
        record_incomplete(f"database_metadata/{db_id}",
                          "database metadata has no data_sources list -- cannot enumerate rows")
        return
    node["data_source_ids"] = []
    for ds in data_sources:
        ds_id = (ds.get("id") or "").replace("-", "").lower()
        if not ds_id:
            record_incomplete(f"database_metadata/{db_id}", "data_sources entry without id")
            continue
        node["data_source_ids"].append(ds_id)
        crawl_data_source(ds_id, db_id, ds.get("name"))


def crawl_data_source(ds_id, db_id, name):
    if ds_id in visited_data_sources:
        return
    visited_data_sources[ds_id] = {"database_id": db_id, "name": name}
    try:
        for row in paginate(lambda c: fetch_data_source_query_page(ds_id, c), CFG["max_pagination_pages"]):
            if row.get("object") != "page":
                non_page_query_results.append({"data_source_id": ds_id, "object": row.get("object"),
                                               "id": (row.get("id") or "").replace("-", "").lower()})
                continue
            rid = row["id"].replace("-", "").lower()  # normalize BEFORE using as dict key
            if rid in visited_pages:
                continue
            rnode = {
                "notion_id": rid,
                "object_type": "page",
                "title": get_title_from_page_properties(row.get("properties", {})),
                "parent": row.get("parent"),
                "last_edited_time": row.get("last_edited_time"),
                "archived": row.get("archived"),
                "in_trash": row.get("in_trash"),
                "properties": row.get("properties"),
            }
            visited_pages[rid] = rnode
            crawl_block_children(rid)
    except PaginationError as e:
        visited_data_sources[ds_id] = None
        record_incomplete(f"data_source_query/{ds_id}", e)


ATTACHMENT_TYPES = {"file", "image", "pdf", "video", "audio"}


def crawl_block_children(block_id):
    block_id = block_id.replace("-", "").lower()  # normalize BEFORE use, consistent with visited_pages keys
    try:
        for child in paginate(lambda c: fetch_block_children_page(block_id, c), CFG["max_pagination_pages"]):
            t = child.get("type")
            child_id_norm = child["id"].replace("-", "").lower()
            if t == "child_page":
                crawl_page(child_id_norm, parent_hint=block_id)
            elif t == "child_database":
                crawl_database(child_id_norm)
            elif t in ATTACHMENT_TYPES:
                fobj = child.get(t, {})
                attachments.append({
                    "block_id": child_id_norm,
                    "block_type": t,
                    "url_type": fobj.get("type"),
                    "caption": "".join(p.get("plain_text", "") for p in fobj.get("caption", [])),
                    "parent_block_id": block_id,
                })
            else:
                if child.get("has_children"):
                    crawl_block_children(child_id_norm)
    except PaginationError as e:
        record_incomplete(f"block_children/{block_id}", e)


ROOT_ID = CFG["lavcca_root_id"].replace("-", "").lower()

# Network readiness: after Modern Standby / sleep the scheduler starts the task at once
# (StartWhenAvailable) while the network may still be coming up. Wait (bounded) for the
# root page to answer before crawling; probe failures are not crawl errors. If the
# network is still down the crawl fails closed as before (exit 2, nothing written).
NETWORK_WAIT_S = int(canary_apply.env("NETWORK_WAIT_S", "120") or 0)


def wait_for_network():
    waited = 0
    while True:
        code, _ = api_call("GET", f"/pages/{ROOT_ID}", record_errors=False)
        if code is not None and code < 500:
            return code, waited
        if waited >= NETWORK_WAIT_S:
            return code, waited
        time.sleep(10)
        waited += 10


_net_code, _net_waited = wait_for_network()
log(f"Network probe: HTTP {_net_code} after {_net_waited}s wait")

ROOT_PROBE_OK = False
if INCREMENTAL_TARGETS is not None:
    # Incremental: no tree crawl. The root must still answer (recorded as an API error otherwise).
    log(f"=== Incremental run: {len(INCREMENTAL_TARGETS)} target(s), root crawl skipped ===")
    ROOT_PROBE_OK = api_call("GET", f"/pages/{ROOT_ID}")[0] == 200
else:
    log("=== Crawling Notion LAVCCA tree (read-only) ===")
    try:
        crawl_page(ROOT_ID)
    except Exception as e:  # never silently continue past an unexpected crawl failure
        record_incomplete("crawl", f"unexpected exception: {type(e).__name__}: {e}")


# ============================================================
# 4. Crawl health check (structural only -- no expected row counts)
#    Query-visibility gaps are a Notion-side phenomenon, so a fixed per-database
#    row count is NOT a valid health signal. Pages that exist but are hidden from
#    query results are handled by the direct-retrieve fallback (section 6b).
#    Pagination failures (has_more without cursor, cursor repeat, max-page guard,
#    page fetch error) and duplicate ids within one result set are raised by
#    paginate() and land in CRAWL_INCOMPLETE_REASONS.
# ============================================================
def crawl_health():
    checks = {
        "root_reached": visited_pages.get(ROOT_ID) is not None or ROOT_PROBE_OK,
        "api_errors_zero": len(API_ERRORS) == 0,
        "pagination_and_structure_clean": len(CRAWL_INCOMPLETE_REASONS) == 0,
    }
    return all(checks.values()), checks


def abort_incomplete_crawl(stage, checks):
    report = {
        "run_id": RUN_ID,
        "mode": "DRY_RUN",
        "status": "INCOMPLETE_CRAWL",
        "stage": stage,
        "crawl_complete": False,
        "crawl_health": {"pass": False, "checks": checks},
        "api_call_count": API_CALL_COUNT,
        "api_errors": API_ERRORS,
        "incomplete_reasons": CRAWL_INCOMPLETE_REASONS,
        "pages_found": sum(1 for n in visited_pages.values() if n),
        "databases_found": sum(1 for n in visited_dbs.values() if n),
        "data_sources_found": sum(1 for n in visited_data_sources.values() if n),
    }
    out_path = os.path.join(LOGS_DIR, f"dry_run_{RUN_ID}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    log(f"INCOMPLETE_CRAWL ({stage}): change plan NOT generated. See " + out_path)
    log(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(2)


crawl_complete, health_checks = crawl_health()
log(f"Crawl health: {'PASS' if crawl_complete else 'FAIL'} {health_checks}")

# ============================================================
# HARD GATE: unhealthy crawl -> do not proceed to change plan
# ============================================================
if not crawl_complete:
    abort_incomplete_crawl("tree_crawl", health_checks)

QUERY_DISCOVERED_PAGE_IDS = {nid for nid, n in visited_pages.items() if n}

log(f"Crawl complete. pages={sum(1 for n in visited_pages.values() if n)} "
    f"databases={sum(1 for n in visited_dbs.values() if n)} "
    f"data_sources={sum(1 for n in visited_data_sources.values() if n)} attachments={len(attachments)} "
    f"api_calls={API_CALL_COUNT} api_errors=0")

# ============================================================
# 5. Canonical text extraction from Notion blocks (best-effort content hash)
# ============================================================
class ContentFetchError(Exception):
    pass


def notion_canonical_text(page_id, _depth=0, _max_depth=25):
    """Deterministic plain-text rendering of a page's block tree (this engine's
    own renderer). Raises ContentFetchError if any part of the tree cannot be
    fetched or the depth guard is hit -- a partial body is never returned, so a
    page is never judged UPDATE/UNCHANGED from incomplete content."""
    if _depth > _max_depth:
        raise ContentFetchError(f"{page_id}: block tree deeper than {_max_depth}")
    parts = []
    try:
        for child in paginate(lambda c: fetch_block_children_page(page_id, c), CFG["max_pagination_pages"]):
            t = child.get("type")
            obj = child.get(t, {})
            rt = obj.get("rich_text") or obj.get("caption") or []
            text = "".join(p.get("plain_text", "") for p in rt)
            if text:
                parts.append(text)
            if t not in ("child_page", "child_database") and child.get("has_children"):
                parts.append(notion_canonical_text(child["id"], _depth + 1, _max_depth))
    except PaginationError as e:
        raise ContentFetchError(f"{page_id}: {e}")
    return "\n".join(parts)


# ============================================================
# 6. Obsidian inventory (managed:notion markdown only)
# ============================================================
FRONTMATTER_RE = re.compile(r'^---\n(.*?)\n---\n(.*)$', re.DOTALL)
ID_TITLE_RE = re.compile(r'^(?P<title>.*?)\s?\((?P<id>[0-9a-f]{32})\)?$')


def parse_frontmatter(text):
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None, None
    fm = {}
    for line in m.group(1).splitlines():
        if ':' in line:
            k, v = line.split(':', 1)
            fm[k.strip()] = v.strip().strip('"')
    return fm, m.group(2)


def delinkify(body):
    """Strip markdown/wikilink syntax down to visible text, for a best-effort
    comparison against Notion's canonical plain text. See README limitations."""
    body = re.sub(r'!?\[\[([^\]|]+)\|([^\]]*)\]\]', r'\2', body)   # [[target|alias]] -> alias
    body = re.sub(r'!?\[\[([^\]]+)\]\]', r'\1', body)               # [[target]] -> target
    body = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', body)          # [text](href) -> text
    body = re.sub(r'</?aside>', '', body)
    body = re.sub(r'[#>*`_-]', '', body)
    body = re.sub(r'\s+', ' ', body).strip()
    return body


LOCAL_UNREADABLE = []  # vault markdown that could not be read this run (e.g. locked)


def abort_local_unreadable(stage):
    """A markdown file could not be read: identities / wikilinks would be incomplete, so the
    run stops before any write (no crash, nothing written) and the next run retries."""
    report = {"run_id": RUN_ID, "mode": CLI_MODE, "status": "LOCAL_INVENTORY_UNREADABLE", "stage": stage,
              "unreadable": LOCAL_UNREADABLE, "api_call_count": API_CALL_COUNT}
    with open(os.path.join(LOGS_DIR, f"dry_run_{RUN_ID}.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    log(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(2)


obsidian_rows = []
for root, dirs, files in os.walk(LAVCCA_ROOT_PATH):
    rel_root = os.path.relpath(root, LAVCCA_ROOT_PATH).replace(os.sep, '/')
    if any(rel_root == p or rel_root.startswith(p + '/') for p in CFG["protected_relative_paths"]):
        dirs[:] = []
        continue
    for fn in files:
        if not fn.endswith('.md'):
            continue
        full = os.path.join(root, fn)
        rel = os.path.relpath(full, LAVCCA_ROOT_PATH).replace(os.sep, '/')
        try:
            with open(full, 'rb') as f:
                data = f.read()
        except OSError as e:
            LOCAL_UNREADABLE.append({"path": f"{ROOT_REL}/{rel}", "error": type(e).__name__})
            continue
        text = data.decode('utf-8', errors='replace')
        fm, body = parse_frontmatter(text)
        if not fm or fm.get('managed') != 'notion':
            continue
        bucket = 'active'
        if rel.startswith('99_ARCHIVE/Notion-Raw/Former-Team/'):
            bucket = 'former-team'
        elif rel.startswith('99_ARCHIVE/Notion-Raw/Legacy-NATAM/'):
            bucket = 'legacy'
        obsidian_rows.append({
            'notion_id': (fm.get('notion_id') or '').lower(),
            'notion_path': fm.get('notion_path', ''),
            'obsidian_path': rel,
            'owner': fm.get('owner', ''),
            'sha256': hashlib.sha256(data).hexdigest(),
            'bucket': bucket,
            'canonical_text': delinkify(body),
            'body': body,
        })

duplicate_id_groups = defaultdict(list)
for r in obsidian_rows:
    if r['notion_id']:
        duplicate_id_groups[r['notion_id']].append(r['obsidian_path'])
DUPLICATE_IDS = {nid: paths for nid, paths in duplicate_id_groups.items() if len(paths) > 1}
if DUPLICATE_IDS:
    log(f"WARNING: {len(DUPLICATE_IDS)} notion_id(s) appear on more than one Obsidian file -- "
        f"these will be reported as DUPLICATE_ID and are NOT deduplicated silently.")

obs_by_id = {r['notion_id']: r for r in obsidian_rows if r['notion_id']}

# Identity-only inventory over the whole vault (frontmatter notion_id, nothing else).
# Used so that pages already present in 99_ARCHIVE are not proposed as CREATE.
# Reads only; 99_ARCHIVE content is never modified.
VAULT_ROOT = CFG["vault_root"]
ARCHIVE_PREFIX = f"{ROOT_REL}/99_ARCHIVE/"
IDENTITY_SKIP_DIRS = {".obsidian", ".git", ".trash"}
identity_rows = []
for root, dirs, files in os.walk(VAULT_ROOT):
    rel_root = os.path.relpath(root, VAULT_ROOT).replace(os.sep, '/')
    if rel_root.split('/')[0] in IDENTITY_SKIP_DIRS or rel_root in (f"{ROOT_REL}/_sync", f"{ROOT_REL}/_migration") \
            or rel_root.startswith((f"{ROOT_REL}/_sync/", f"{ROOT_REL}/_migration/")):
        dirs[:] = []
        continue
    for fn in files:
        if not fn.endswith('.md'):
            continue
        full = os.path.join(root, fn)
        rel = os.path.relpath(full, VAULT_ROOT).replace(os.sep, '/')
        try:
            with open(full, 'rb') as f:
                text = f.read().decode('utf-8', errors='replace').replace('\r\n', '\n')
        except OSError as e:
            LOCAL_UNREADABLE.append({"path": rel, "error": type(e).__name__})
            continue
        fm, _ = parse_frontmatter(text)
        if not fm or fm.get('managed') != 'notion':
            continue
        if rel.startswith(ARCHIVE_PREFIX):
            loc = 'archive'
        elif rel.startswith(ROOT_REL + '/') and rel[len(ROOT_REL) + 1:] in {r['obsidian_path'] for r in obsidian_rows}:
            loc = 'active'
        else:
            loc = 'other'  # managed:notion outside both active scope and 99_ARCHIVE -- reported, never synced
        identity_rows.append({'notion_id': (fm.get('notion_id') or '').lower(), 'vault_path': rel, 'location': loc})

if LOCAL_UNREADABLE:
    abort_local_unreadable("vault_inventory")

ARCHIVE_IDS = defaultdict(list)
for r in identity_rows:
    if r['location'] == 'archive' and r['notion_id']:
        ARCHIVE_IDS[r['notion_id']].append(r['vault_path'])
IDENTITY_OTHER = [r for r in identity_rows if r['location'] == 'other']
ID_TO_VAULT_PATH = {r['notion_id']: r['vault_path'] for r in identity_rows
                    if r['notion_id'] and r['location'] in ('active', 'archive')}
CROSS_BUCKET_IDS = {nid: [obs_by_id[nid]['obsidian_path']] + paths
                    for nid, paths in ARCHIVE_IDS.items() if nid in obs_by_id}
log(f"Vault notion_id identity inventory: active={sum(1 for r in identity_rows if r['location']=='active')}, "
    f"archive={sum(1 for r in identity_rows if r['location']=='archive')} "
    f"(with notion_id: {sum(len(v) for v in ARCHIVE_IDS.values())}), other={len(IDENTITY_OTHER)}, "
    f"active+archive same id={len(CROSS_BUCKET_IDS)}")

# Per-notion_id baseline from previous real APPLY runs (read-only here; empty until first APPLY)
with open(os.path.join(SYNC_DIR, "state.json"), encoding="utf-8") as f:
    _STATE = json.load(f)
STATE_ITEMS = _STATE.get("items", {})
# Notion-hosted DOCX attachments: planned (never downloaded) while classifying, so the
# classification hash equals the hash preflight/apply will produce.
ATTACHMENT_PLANNER = attachment_sync.AttachmentPlanner(LAVCCA_ROOT_PATH, attachment_sync.state_attachments(_STATE),
                                                       CFG.get("bootstrap_migration_completed_at"))
ATTACHMENT_PLANS = {}  # notion_id -> plans made while rendering that page
# child_database -> verified local CSV export links (migration link map), per markdown path
DB_LINKS = canary_apply.load_child_database_links(LAVCCA_ROOT_PATH)
log(f"Obsidian managed:notion markdown inventory: {len(obsidian_rows)} "
    f"(active={sum(1 for r in obsidian_rows if r['bucket']=='active')}, "
    f"former-team={sum(1 for r in obsidian_rows if r['bucket']=='former-team')}, "
    f"legacy={sum(1 for r in obsidian_rows if r['bucket']=='legacy')})")

# ============================================================
# 6b. Direct-retrieve fallback for existing Obsidian notion_ids missing from the crawl
#     Only notion_ids that ALREADY exist in the active managed:notion inventory are
#     checked -- this protects known pages from query-visibility gaps. It cannot
#     discover brand-new pages that no query/tree listing returns (see README).
#     Read-only: GET /pages, /databases, /blocks (and /data_sources only when a
#     parent carries no database_id) for parent-chain verification.
# ============================================================
MAX_PARENT_HOPS = 50


def verify_lavcca_descendant(parent):
    """Walk the parent chain up to LAVCCA root. Returns (verdict, chain) where
    verdict is DESCENDANT / OUTSIDE / UNKNOWN. HTTP 200 on the page itself is
    never treated as proof of membership; any unresolvable hop -> UNKNOWN."""
    chain = []
    seen = set()
    cur = parent
    for _ in range(MAX_PARENT_HOPS):
        if not cur:
            return "UNKNOWN", chain + ["<parent missing>"]
        t = cur.get("type")
        if t == "workspace":
            return "OUTSIDE", chain + ["workspace"]
        if t == "page_id":
            kind, pid = "page", cur.get("page_id")
        elif t in ("database_id", "data_source_id"):
            if cur.get("database_id"):
                kind, pid = "database", cur.get("database_id")
            else:
                kind, pid = "data_source", cur.get("data_source_id")
        elif t == "block_id":
            kind, pid = "block", cur.get("block_id")
        else:
            return "UNKNOWN", chain + [f"<unsupported parent type {t!r}>"]
        if not pid:
            return "UNKNOWN", chain + [f"<{t} without id>"]
        pid = pid.replace("-", "").lower()
        chain.append(f"{kind}:{pid}")
        if pid == ROOT_ID:
            return "DESCENDANT", chain
        if pid in seen:
            return "UNKNOWN", chain + ["<parent cycle>"]
        seen.add(pid)
        # An ancestor already reached by the root-anchored crawl of THIS run is,
        # by construction, under LAVCCA root.
        if (kind == "page" and visited_pages.get(pid)) or (kind == "database" and visited_dbs.get(pid)):
            return "DESCENDANT", chain + ["<ancestor reached by root crawl>"]
        endpoint = {"page": f"/pages/{pid}", "database": f"/databases/{pid}",
                    "data_source": f"/data_sources/{pid}", "block": f"/blocks/{pid}"}[kind]
        code, obj = api_call("GET", endpoint, record_errors=False)
        if code != 200:
            return "UNKNOWN", chain + [f"<HTTP {code} on {kind}>"]
        cur = obj.get("parent")
    return "UNKNOWN", chain + [f"<exceeded {MAX_PARENT_HOPS} hops>"]


if INCREMENTAL_TARGETS is not None:
    _active_ids = {r['notion_id'] for r in obsidian_rows if r['bucket'] == 'active' and r['notion_id']}
    _unknown = sorted(INCREMENTAL_TARGETS - _active_ids)
    if _unknown:  # new, moved-in or unmanaged page: only a full crawl can place it safely
        log(f"INCREMENTAL_NEEDS_FULL_CRAWL: not active managed pages: {_unknown}")
        sys.exit(9)

fallback_results = []
hidden_active_pages = {}
for r in obsidian_rows:
    if INCREMENTAL_TARGETS is not None and r['notion_id'] not in INCREMENTAL_TARGETS:
        continue
    if r['bucket'] != 'active' or not r['notion_id'] or r['notion_id'] in QUERY_DISCOVERED_PAGE_IDS:
        continue
    nid = r['notion_id']
    res = {"notion_id": nid, "obsidian_path": r['obsidian_path'], "title": None, "chain": None}
    code, page = api_call("GET", f"/pages/{nid}", record_errors=False)
    res["http_status"] = code
    if code == 200:
        res["title"] = get_title_from_page_properties(page.get('properties', {}))
        res["archived"], res["in_trash"] = page.get('archived'), page.get('in_trash')
        if page.get('in_trash'):
            res["status"] = "TRASHED"
        elif page.get('archived'):
            res["status"] = "ARCHIVED"
        else:
            verdict, chain = verify_lavcca_descendant(page.get('parent'))
            res["chain"] = chain
            res["status"] = {"DESCENDANT": "QUERY_HIDDEN_ACTIVE",
                             "OUTSIDE": "MOVED_OUTSIDE_LAVCCA",
                             "UNKNOWN": "DESCENDANT_UNKNOWN"}[verdict]
            if verdict == "DESCENDANT":
                hidden_active_pages[nid] = {
                    "notion_id": (page.get("id") or nid).replace("-", "").lower(),
                    "object_type": "page",
                    "title": res["title"],
                    "parent": page.get("parent"),
                    "last_edited_time": page.get("last_edited_time"),
                    "archived": page.get("archived"),
                    "in_trash": page.get("in_trash"),
                    "properties": page.get("properties"),
                }
    elif code == 403:
        res["status"] = "PERMISSION_UNKNOWN"
    elif code == 404:
        res["status"] = "NOT_FOUND_UNKNOWN"
    else:
        res["status"] = "ERROR"
    fallback_results.append(res)
    log(f"Fallback direct retrieve {nid}: HTTP {code} -> {res['status']}")

log(f"Direct-retrieve fallback: {len(fallback_results)} notion_id(s) checked, "
    f"{len(hidden_active_pages)} QUERY_HIDDEN_ACTIVE")

# Query-hidden pages stay active sources; crawl their own subtrees (read-only) so
# descendants of a hidden page are not silently lost, then re-apply the health gate.
for hid in ([] if INCREMENTAL_TARGETS is not None else hidden_active_pages):  # targets' children are own targets
    try:
        crawl_block_children(hid)
    except Exception as e:
        record_incomplete(f"hidden_subtree/{hid}", f"unexpected exception: {type(e).__name__}: {e}")
crawl_complete, health_checks = crawl_health()
if not crawl_complete:
    abort_incomplete_crawl("query_hidden_subtree_crawl", health_checks)

# ============================================================
# 6c. ADOPT BASELINE preview -- writes NOTHING except its own logs.
#     Markdown bodies and state.json are only read here; the before/after
#     hashes of both are verified and reported.
# ============================================================
def _vault_md_hashes():
    out = {}
    for r in identity_rows:
        pth = os.path.join(VAULT_ROOT, r['vault_path'].replace('/', os.sep))
        with open(pth, 'rb') as f:
            out[r['vault_path']] = hashlib.sha256(f.read()).hexdigest()
    return out


if CLI_MODE in ("adopt_baseline_preview", "adopt_baseline_write"):
    _state_path = os.path.join(SYNC_DIR, "state.json")
    with open(_state_path, "rb") as f:
        _state_pre = hashlib.sha256(f.read()).hexdigest()
    _md_pre = _vault_md_hashes()
    _now = datetime.now(timezone.utc)
    _bootstrap = CFG.get("bootstrap_migration_completed_at")
    _bootstrap_ts = datetime.fromisoformat(_bootstrap).timestamp() if _bootstrap else None
    _review, _excluded = set(CFG["review_ids"]), set(CFG["excluded_ids"])

    bl_rows = []
    _pages = [(nid, n, "query") for nid, n in visited_pages.items() if n] + \
             [(nid, n, "query_hidden") for nid, n in hidden_active_pages.items()]
    for _pid, _node, _via in _pages:
        nid = _node['notion_id']
        base = {"notion_id": nid, "title": _node.get('title'), "discovered_via": _via,
                "notion_last_edited_time": _node.get('last_edited_time')}
        if nid in _review:
            row = {**base, "status": "EXCLUDED_REVIEW", "reason": "review_ids"}
        elif nid in _excluded:
            row = {**base, "status": "EXCLUDED_LIST", "reason": "excluded_ids (config)"}
        elif nid in DUPLICATE_IDS or nid in CROSS_BUCKET_IDS:
            row = {**base, "status": "EXCLUDED_DUPLICATE_ID", "reason": "multiple vault files share this notion_id"}
        elif nid not in obs_by_id:
            if nid in ARCHIVE_IDS:
                row = {**base, "status": "EXCLUDED_ARCHIVE", "reason": "present only in 99_ARCHIVE (ARCHIVED_LOCAL_PRESENT)",
                       "obsidian_path": " | ".join(ARCHIVE_IDS[nid])}
            elif _node.get('in_trash') or _node.get('archived'):
                row = {**base, "status": "EXCLUDED_ARCHIVED" if _node.get('archived') else "EXCLUDED_TRASHED",
                       "reason": "remote archived/in_trash"}
            else:
                row = {**base, "status": "CREATE_CANDIDATE", "reason": "not in vault; CREATE is not automated",
                       "parent_title": None}
        else:
            obs = obs_by_id[nid]
            # crawl-discovered pages are under root by construction; hidden pages were
            # admitted only after the fallback verified their parent chain.
            row = {**base, **bl.evaluate_page(
                nid, os.path.join(LAVCCA_ROOT_PATH, obs['obsidian_path'].replace('/', os.sep)), _node, api_call, CFG,
                STATE_ITEMS, ID_TO_VAULT_PATH, _bootstrap_ts, descendant_verified=True, now=_now,
                db_links=DB_LINKS.get(obs['obsidian_path']))}
            row.setdefault("obsidian_path", obs['obsidian_path'])
            if _via == "query_hidden":
                row["query_hidden_active"] = True
                row["adoptable"] = row["status"] == "ADOPT_CANDIDATE"
        bl_rows.append(row)
    for res in fallback_results:
        if res["status"] != "QUERY_HIDDEN_ACTIVE":
            bl_rows.append({"notion_id": res["notion_id"], "title": res["title"], "obsidian_path": res["obsidian_path"],
                            "status": f"EXCLUDED_{res['status']}", "reason": "direct-retrieve fallback outcome"})
    for r in obsidian_rows:
        if r['bucket'] == 'active' and not r['notion_id']:
            bl_rows.append({"notion_id": None, "obsidian_path": r['obsidian_path'], "status": "EXCLUDED_NO_NOTION_ID"})

    _md_post = _vault_md_hashes()
    with open(_state_path, "rb") as f:
        _state_post = hashlib.sha256(f.read()).hexdigest()

    def _bucket(row):
        if row.get("query_hidden_active"):
            return "QUERY_HIDDEN_ACTIVE"
        return row["status"]

    counts = defaultdict(int)
    sync_counts = defaultdict(int)
    for row in bl_rows:
        counts[_bucket(row)] += 1
        if row["status"] == "ALREADY_BASELINED":
            sync_counts[row["sync_status"]] += 1
    adopt = [r for r in bl_rows if r["status"] == "ADOPT_CANDIDATE" and not r.get("query_hidden_active")]
    hidden = [r for r in bl_rows if r.get("query_hidden_active")]
    preview = {
        "run_id": RUN_ID, "mode": "ADOPT_BASELINE_PREVIEW", "notion_version": CFG["notion_version"],
        "crawl_health": {"pass": True, "checks": health_checks},
        "api_call_count": API_CALL_COUNT, "api_errors": API_ERRORS,
        "counts": dict(counts), "already_baselined_sync_status": dict(sync_counts),
        "adopt_candidate_count": len(adopt),
        "adopt_candidates_hashes_differ": sum(1 for r in adopt if r.get("hashes_differ")),
        "query_hidden_active": [{k: r.get(k) for k in ("notion_id", "title", "obsidian_path", "status", "adoptable", "reason")}
                                for r in hidden],
        "markdown_files_checked": len(_md_pre),
        "markdown_changed": sorted(k for k in _md_pre if _md_post.get(k) != _md_pre[k]),
        "state_json_changed": _state_pre != _state_post,
        "rows": bl_rows,
    }
    _out_json = os.path.join(LOGS_DIR, f"adopt_baseline_preview_{RUN_ID}.json")
    with open(_out_json, "w", encoding="utf-8") as f:
        json.dump(preview, f, ensure_ascii=False, indent=2)
    import csv as _csv
    _out_csv = os.path.join(LOGS_DIR, f"adopt_candidates_{RUN_ID}.csv")
    with open(_out_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["notion_id", "title", "obsidian_path", "notion_last_edited_time",
                    "api_rendered_content_hash", "local_content_hash", "hashes_differ"])
        for r in sorted(adopt, key=lambda x: x["obsidian_path"]):
            w.writerow([r["notion_id"], r["title"], r["obsidian_path"], r["notion_last_edited_time"],
                        r["entry"]["api_rendered_content_hash"], r["entry"]["local_content_hash"], r["hashes_differ"]])
    _out_freeze = os.path.join(LOGS_DIR, f"render_freeze_{RUN_ID}.csv")
    with open(_out_freeze, "w", encoding="utf-8-sig", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["notion_id", "title", "obsidian_path", "render_hash_run_1", "render_hash_run_2", "deterministic"])
        for r in bl_rows:
            if r.get("render_hash_run_1"):
                w.writerow([r["notion_id"], r.get("title"), r.get("obsidian_path"), r["render_hash_run_1"],
                            r["render_hash_run_2"], r["render_hash_run_1"] == r["render_hash_run_2"]])
    log(json.dumps({k: v for k, v in preview.items() if k not in ("rows", "api_errors")}, ensure_ascii=False, indent=2))
    log(f"Render freeze check: {_out_freeze}")
    log(f"Preview: {_out_json}")
    log(f"ADOPT_CANDIDATE report: {_out_csv}")
    if preview["markdown_changed"] or preview["state_json_changed"]:
        log("ABORT: markdown or state.json changed during evaluation")
        sys.exit(5)
    if CLI_MODE == "adopt_baseline_preview":
        sys.exit(0)

    # ---- real adoption: state.json only ----
    def _all_markdown_hashes():
        """Every .md in the vault outside .obsidian and _sync (managed:notion,
        managed:obsidian, 06_KNOWLEDGE, daily notes ...): none may change."""
        out = {}
        for root, dirs, files in os.walk(VAULT_ROOT):
            rel_root = os.path.relpath(root, VAULT_ROOT).replace(os.sep, "/")
            if rel_root.split("/")[0] in (".obsidian", ".git", ".trash") or                     rel_root == f"{ROOT_REL}/_sync" or rel_root.startswith(f"{ROOT_REL}/_sync/"):
                dirs[:] = []
                continue
            for fn in files:
                if fn.endswith(".md"):
                    pth = os.path.join(root, fn)
                    with open(pth, "rb") as f:
                        out[os.path.relpath(pth, VAULT_ROOT).replace(os.sep, "/")] = hashlib.sha256(f.read()).hexdigest()
        return out

    adoption = bl.run_adoption(_state_path, SYNC_DIR, bl_rows, crawl_ok=crawl_complete, api_errors=API_ERRORS,
                               md_snapshot=_all_markdown_hashes, now=_now)
    adoption["api_call_count"] = API_CALL_COUNT
    adoption["query_hidden_adopted"] = [r["notion_id"] for r in hidden if r["status"] == "ADOPT_CANDIDATE"]
    adoption["render_excluded"] = [{"notion_id": r["notion_id"], "obsidian_path": r.get("obsidian_path"),
                                    "reason": r.get("reason")} for r in bl_rows if r["status"] == "EXCLUDED_RENDER_ERROR"]
    _out_adopt = os.path.join(LOGS_DIR, f"adopt_baseline_write_{RUN_ID}.json")
    with open(_out_adopt, "w", encoding="utf-8") as f:
        json.dump(adoption, f, ensure_ascii=False, indent=2)
    log(json.dumps({k: v for k, v in adoption.items() if k != "added"}, ensure_ascii=False, indent=2))
    log(f"Adoption report: {_out_adopt}")
    sys.exit(0 if adoption["status"] == "ADOPTED" else 6)


# ============================================================
# 7. Change plan classification (Notion pages only -- databases are containers,
#    not individually mapped to a single Obsidian file)
# ============================================================
EXCLUDED_IDS = set(CFG["excluded_ids"])
REVIEW_IDS = set(CFG["review_ids"])

id_to_title_map = {nid: (n.get('title') or '') for nid, n in visited_pages.items() if n}
id_to_title_map.update({nid: (n.get('title') or '') for nid, n in visited_dbs.items() if n})
id_to_title_map.update({nid: (n.get('title') or '') for nid, n in hidden_active_pages.items()})


def immediate_parent_title(parent):
    if not parent:
        return None
    t = parent.get('type')
    if t == 'page_id':
        return id_to_title_map.get(parent.get('page_id', '').replace('-', '').lower())
    if t in ('database_id', 'data_source_id'):  # 2025-09-03 rows carry data_source_id + database_id
        return id_to_title_map.get((parent.get('database_id') or '').replace('-', '').lower())
    if t == 'workspace':
        return '(workspace root)'
    return None  # block_id parents (e.g. pages inside column blocks) have no folder-equivalent title


# Filename normalization used by the original Notion export + migration.
# The migration script itself was not preserved, so the rule is derived from data:
#  - all 138 id-bearing managed:notion files satisfy: filename stem == export title
#    (title part of frontmatter notion_path), plus " (<32-hex notion_id>)" when the
#    migration had to disambiguate a destination collision;
#  - live title -> export title pairs observed in DRY-RUN 20261001T145834Z:
#    "Django / FastAPI" -> "Django FastAPI", "회복/악화" -> "회복 악화",
#    "회의: 실패분석" -> "회의 실패분석", "초기 시장 " -> "초기 시장", "" -> "제목 없음".
WINDOWS_INVALID_FILENAME_CHARS = set('\\/:*?"<>|')
EMPTY_TITLE_FILENAME = "제목 없음"


def export_filename_title(title):
    t = ''.join(' ' if (ch in WINDOWS_INVALID_FILENAME_CHARS or ord(ch) < 32) else ch for ch in (title or ''))
    t = re.sub(r'\s+', ' ', t)
    prev = None
    while prev != t:  # trailing dots and spaces, in any order
        prev = t
        t = t.strip().rstrip('.')
    return t or EMPTY_TITLE_FILENAME


def content_signature(text):
    """Same normalization applied to both renderers before cross-renderer comparison."""
    return delinkify(text)


def classify_content(page_id, nid, obs, live_title, live_edited=None, live_node=None):
    """Returns (status, reason, notion_content_sha256)."""
    view = bl.baseline_view(STATE_ITEMS.get(nid))
    pa = ATTACHMENT_PLANNER.for_page(nid, obs['obsidian_path'], obs.get('owner'))
    ATTACHMENT_PLANS[nid] = pa.plans
    if view is not None:
        # Baseline present: compare API render (same renderer as the baseline) and
        # local body against the stored hashes; policy in baseline.classify_incremental.
        try:
            h = bl.render_current(api_call, page_id, live_node, obs['body'], CFG, ID_TO_VAULT_PATH, pa,
                                  DB_LINKS.get(obs['obsidian_path']))
        except canary_apply.ContentFetchError as e:
            return ("CONTENT_FETCH_ERROR",
                    f"body fetch failed after retries ({e}) -- not judged UPDATE/UNCHANGED; must never be overwritten", None)
        except canary_apply.RenderUnsupported as e:
            return "RENDER_UNSUPPORTED", f"renderer cannot represent page safely: {e.items}", None
        except OSError as e:  # a local file could not be read (e.g. locked by Word): this page only
            return "LOCAL_FILE_UNREADABLE", f"local file unreadable ({type(e).__name__}, errno {e.errno})", None
        status, reason = bl.classify_incremental(view, live_edited, h, canary_apply.sha256_text(obs['body']))
        return status, reason, h
    # No baseline: render with the production renderer so unsupported content is
    # surfaced explicitly; without a baseline a difference cannot be confirmed.
    try:
        h = bl.render_current(api_call, page_id, live_node or {"title": live_title}, obs['body'], CFG, ID_TO_VAULT_PATH,
                              pa, DB_LINKS.get(obs['obsidian_path']))
    except canary_apply.ContentFetchError as e:
        return ("CONTENT_FETCH_ERROR",
                f"body fetch failed after retries ({e}) -- not judged UPDATE/UNCHANGED; must never be overwritten", None)
    except canary_apply.RenderUnsupported as e:
        return "RENDER_UNSUPPORTED", f"renderer cannot represent page safely: {e.items}", None
    except OSError as e:
        return "LOCAL_FILE_UNREADABLE", f"local file unreadable ({type(e).__name__}, errno {e.errno})", None
    if h == canary_apply.sha256_text(obs['body']):
        return "UNCHANGED", "no baseline; API render is byte-identical to the local body", h
    return ("UPDATE_UNVERIFIED",
            "no baseline in state.json; API render differs from ZIP-export markdown -- "
            "may be a real edit or only a renderer difference", h)


def baseline_title_and_folder(notion_path):
    m = re.search(r'([^/]+?)\s[0-9a-f]{32}(?:_all)?\.(?:md|csv)$', notion_path or '')
    stored_title = m.group(1) if m else None
    stored_folder = notion_path.split('/')[-2] if notion_path and '/' in notion_path else None
    return stored_title, stored_folder


plan = []
for page_id, node in visited_pages.items():
    if node is None:
        continue
    nid = node['notion_id']

    if nid in REVIEW_IDS:
        plan.append({"notion_id": nid, "status": "REVIEW", "title": node['title'],
                     "obsidian_path": obs_by_id.get(nid, {}).get('obsidian_path'), "reason": "known review item"})
        continue
    if nid in EXCLUDED_IDS:
        plan.append({"notion_id": nid, "status": "EXCLUDED", "title": node['title'],
                     "obsidian_path": obs_by_id.get(nid, {}).get('obsidian_path'), "reason": "excluded_ids (config)"})
        continue

    if nid in DUPLICATE_IDS or nid in CROSS_BUCKET_IDS:
        paths = DUPLICATE_IDS.get(nid) or CROSS_BUCKET_IDS[nid]
        plan.append({"notion_id": nid, "status": "DUPLICATE_ID", "title": node['title'],
                     "obsidian_path": " | ".join(paths),
                     "reason": f"{len(paths)} vault files share this notion_id -- not deduplicated automatically"})
        continue

    obs = obs_by_id.get(nid)

    if node.get('in_trash'):
        plan.append({"notion_id": nid, "status": "TRASHED", "title": node['title'],
                     "obsidian_path": obs['obsidian_path'] if obs else None, "reason": "in_trash=true"})
        continue
    if node.get('archived'):
        plan.append({"notion_id": nid, "status": "ARCHIVED", "title": node['title'],
                     "obsidian_path": obs['obsidian_path'] if obs else None, "reason": "archived=true"})
        continue

    if obs is None:
        if nid in ARCHIVE_IDS:
            plan.append({"notion_id": nid, "status": "ARCHIVED_LOCAL_PRESENT", "title": node['title'],
                         "obsidian_path": " | ".join(ARCHIVE_IDS[nid]),
                         "reason": "notion_id already present in Obsidian 99_ARCHIVE (local archive, NOT Notion archived=true) "
                                   "-- not proposed for CREATE; archive file untouched"})
            continue
        plan.append({"notion_id": nid, "status": "CREATE_CANDIDATE", "title": node['title'],
                     "obsidian_path": None,
                     "parent_title": immediate_parent_title(node.get('parent')),
                     "parent": node.get('parent'),
                     "reason": "notion_id not found anywhere in vault identity inventory (active or 99_ARCHIVE)"})
        continue

    # ---- existing active page: path checks against the migration baseline ----
    stored_title, stored_folder = baseline_title_and_folder(obs['notion_path'])
    live_title = node.get('title') or ''
    live_parent_title = immediate_parent_title(node.get('parent'))

    cur_dir, cur_file = os.path.split(obs['obsidian_path'])
    collision_suffix = f" ({nid})" if cur_file[:-3].endswith(f" ({nid})") else ""

    title_changed = None
    if stored_title is not None:
        title_changed = export_filename_title(live_title) != export_filename_title(stored_title)
    # Notion hierarchy != Obsidian taxonomy: a parent-title difference is never an automatic MOVE.
    parent_changed = (stored_folder is not None and live_parent_title is not None
                      and export_filename_title(stored_folder) != export_filename_title(live_parent_title))

    content_status, content_reason, content_hash = classify_content(page_id, nid, obs, live_title, node.get('last_edited_time'), node)
    entry = {"notion_id": nid, "title": live_title, "obsidian_path": obs['obsidian_path'],
             "live_last_edited_time": node.get('last_edited_time'),
             "content_status": content_status, "content_reason": content_reason,
             "notion_content_sha256": content_hash,
             "stored_title": stored_title, "stored_folder": stored_folder, "live_parent_title": live_parent_title}
    if ATTACHMENT_PLANS.get(nid):
        entry["attachment_plans"] = [attachment_sync.public_plan(p) for p in ATTACHMENT_PLANS[nid]]

    if content_status in ("CONTENT_FETCH_ERROR", "RENDER_UNSUPPORTED", "LOCAL_MODIFICATION_CONFLICT",
                          "CONTENT_HASH_ANOMALY", "LOCAL_FILE_UNREADABLE"):
        entry.update(status=content_status, reason=content_reason)
    elif title_changed is None:
        entry.update(status="PATH_POLICY_UNRESOLVED",
                     reason=f"baseline title not parseable from notion_path {obs['notion_path']!r}")
    elif title_changed and parent_changed:
        entry.update(status="PATH_POLICY_UNRESOLVED",
                     proposed_path=f"{cur_dir}/{export_filename_title(live_title)}{collision_suffix}.md",
                     reason=f"title changed ({stored_title!r} -> {live_title!r}) AND Notion parent title differs "
                            f"({stored_folder!r} -> {live_parent_title!r}); no path policy defined -- not a MOVE candidate")
    elif title_changed:
        proposed = f"{cur_dir}/{export_filename_title(live_title)}{collision_suffix}.md"
        entry.update(status="RENAME", proposed_path=proposed,
                     reason=f"normalized export title changed: {export_filename_title(stored_title)!r} -> "
                            f"{export_filename_title(live_title)!r} (folder kept per canonical migration mapping)"
                            + (" ; WARNING proposed path already exists"
                               if os.path.exists(os.path.join(LAVCCA_ROOT_PATH, proposed.replace('/', os.sep))) else ""))
    elif parent_changed:
        # title unchanged: the existing Obsidian path stays canonical; content may still be
        # updated in place (never moved) -- see production_apply.select_update_candidates
        entry["path_policy"] = prod.PARENT_ONLY_PATH_POLICY
        entry.update(status="PATH_POLICY_UNRESOLVED",
                     reason=f"Notion parent title {live_parent_title!r} differs from export folder {stored_folder!r}; "
                            f"existing Obsidian path is canonical, no path policy defined -- not a MOVE candidate")
    else:
        entry.update(status=content_status, reason=content_reason)
    plan.append(entry)

# Active Obsidian entries without a notion_id cannot be matched at all
for r in obsidian_rows:
    if r['bucket'] == 'active' and not r['notion_id']:
        plan.append({"notion_id": None, "status": "ERROR", "title": None,
                     "obsidian_path": r['obsidian_path'],
                     "reason": "Obsidian file has no notion_id in frontmatter -- not a Notion-sourced page"})

# Direct-retrieve fallback outcomes (section 6b). 403/404 are never treated as deletions.
FALLBACK_REASONS = {
    "QUERY_HIDDEN_ACTIVE": "exists, archived=false, in_trash=false, parent chain verified under LAVCCA root, "
                           "but absent from query/tree listing -- kept as active source",
    "ARCHIVED": "direct retrieve: archived=true",
    "TRASHED": "direct retrieve: in_trash=true",
    "MOVED_OUTSIDE_LAVCCA": "active page, but parent chain reaches workspace without passing LAVCCA root",
    "DESCENDANT_UNKNOWN": "active page, but parent chain could not be resolved -- not assumed to be under LAVCCA",
    "PERMISSION_UNKNOWN": "direct retrieve HTTP 403 -- access lost or restricted; not treated as deletion",
    "NOT_FOUND_UNKNOWN": "direct retrieve HTTP 404 -- deleted or not shared with integration; not treated as deletion",
    "ERROR": "direct retrieve failed with an unexpected status",
}
for res in fallback_results:
    reason = FALLBACK_REASONS[res["status"]] + f" (HTTP {res['http_status']})"
    if res.get("chain"):
        reason += " chain=" + " -> ".join(res["chain"])
    entry = {"notion_id": res["notion_id"], "status": res["status"], "title": res["title"],
             "obsidian_path": res["obsidian_path"], "reason": reason}
    hnode = hidden_active_pages.get(res["notion_id"])
    if hnode is not None and res["notion_id"] in obs_by_id:
        # discovery status stays QUERY_HIDDEN_ACTIVE; content is still classified (read-only)
        cs, cr, ch = classify_content(res["notion_id"], res["notion_id"], obs_by_id[res["notion_id"]],
                                      hnode.get("title") or "", hnode.get("last_edited_time"), hnode)
        entry.update(content_status=cs, content_reason=cr, notion_content_sha256=ch,
                     live_last_edited_time=hnode.get("last_edited_time"))
        if ATTACHMENT_PLANS.get(res["notion_id"]):
            entry["attachment_plans"] = [attachment_sync.public_plan(p) for p in ATTACHMENT_PLANS[res["notion_id"]]]
    if res["status"] in ("ARCHIVED", "TRASHED", "NOT_FOUND_UNKNOWN", "PERMISSION_UNKNOWN", "MOVED_OUTSIDE_LAVCCA"):
        entry["delete_review_required"] = True  # never deleted/moved automatically
    plan.append(entry)

status_counts = defaultdict(int)
for p in plan:
    status_counts[p['status']] += 1

# ============================================================
# 7b. Production incremental APPLY (--apply-updates [--dry-run])
# ============================================================
def all_vault_markdown_hashes():
    out = {}
    for root, dirs, files in os.walk(VAULT_ROOT):
        rel_root = os.path.relpath(root, VAULT_ROOT).replace(os.sep, "/")
        if rel_root.split("/")[0] in (".obsidian", ".git", ".trash") or \
                rel_root == f"{ROOT_REL}/_sync" or rel_root.startswith(f"{ROOT_REL}/_sync/"):
            dirs[:] = []
            continue
        for fn in files:
            if fn.endswith(".md"):
                pth = os.path.join(root, fn)
                with open(pth, "rb") as f:
                    out[os.path.relpath(pth, VAULT_ROOT).replace(os.sep, "/")] = hashlib.sha256(f.read()).hexdigest()
    return out


if CLI_MODE in ("apply_updates_preview", "apply_updates_write"):
    _state_file = os.path.join(SYNC_DIR, "state.json")
    with open(_state_file, "rb") as f:
        _st_pre = hashlib.sha256(f.read()).hexdigest()
    _md_pre = all_vault_markdown_hashes()
    try:
        _identities = canary_apply.scan_vault_identities(CFG)
    except OSError as e:
        LOCAL_UNREADABLE.append({"path": "(identity scan)", "error": type(e).__name__})
        abort_local_unreadable("identity_scan")
    prod_res = prod.run_production(plan, crawl_ok=crawl_complete, api_errors=list(API_ERRORS), api=api_call, cfg=CFG,
                                   state_path=_state_file, sync_dir=SYNC_DIR,
                                   identities=_identities,
                                   now=datetime.now(timezone.utc), apply=(CLI_MODE == "apply_updates_write"),
                                   md_snapshot=all_vault_markdown_hashes)
    _md_post = all_vault_markdown_hashes()
    with open(_state_file, "rb") as f:
        _st_post = hashlib.sha256(f.read()).hexdigest()
    effective = defaultdict(int)
    for p in plan:
        effective[p.get("content_status") if p['status'] in ("PATH_POLICY_UNRESOLVED", "QUERY_HIDDEN_ACTIVE")
                  and p.get("content_status") else p['status']] += 1
    prod_report = {
        "run_id": RUN_ID, "mode": CLI_MODE,
        "incremental_targets": sorted(INCREMENTAL_TARGETS) if INCREMENTAL_TARGETS is not None else None,
        "crawl_health": {"pass": crawl_complete, "checks": health_checks},
        "api_call_count": API_CALL_COUNT, "api_errors": API_ERRORS,
        "status_counts": dict(status_counts), "effective_content_status": dict(effective),
        "query_hidden_active": sum(1 for p in plan if p['status'] == "QUERY_HIDDEN_ACTIVE"),
        "delete_review_required": sum(1 for p in plan if p.get("delete_review_required")),
        # every planned DOCX attachment, whatever its page's status (only pages that pass
        # preflight are written: see attachment_download_candidates)
        "attachments_planned_all_pages": [{"page_status": p['status'], **a} for p in plan
                                          for a in p.get("attachment_plans", [])],
        "markdown_changed": sorted(k for k in set(_md_pre) | set(_md_post) if _md_pre.get(k) != _md_post.get(k)),
        "state_json_changed": _st_pre != _st_post,
        **prod_res,
    }
    _out = os.path.join(LOGS_DIR, f"apply_updates_{RUN_ID}.json")
    with open(_out, "w", encoding="utf-8") as f:
        json.dump(prod_report, f, ensure_ascii=False, indent=2)
    log(json.dumps({k: v for k, v in prod_report.items() if k not in ("api_errors",)}, ensure_ascii=False, indent=2))
    log(f"Production report: {_out}")
    ok = prod_res["status"] in ("PREVIEW", "NOTHING_TO_APPLY", "ALL_APPLIED")
    if CLI_MODE == "apply_updates_preview" and (prod_report["markdown_changed"] or prod_report["state_json_changed"]):
        ok = False
    sys.exit(0 if ok else 8)

# ============================================================
# 8. Attachment inventory (existence/identity only, no download)
# ============================================================
manifest_path = os.path.join(LAVCCA_ROOT_PATH, "_migration", "manifest.csv")
manifest_attachment_paths = []
if os.path.exists(manifest_path):
    import csv
    with open(manifest_path, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            if row.get('ext') not in ('md', 'csv'):
                manifest_attachment_paths.append(row.get('notion_path', ''))

attachment_report = []
for a in attachments:
    parent_page = visited_pages.get(a['parent_block_id'])
    parent_notion_id = parent_page['notion_id'] if parent_page else None
    local_candidate = None
    if parent_page:
        for mp in manifest_attachment_paths:
            if parent_page.get('title') and parent_page['title'] in mp:
                local_candidate = mp
                break
    attachment_report.append({
        "block_id": a["block_id"], "block_type": a["block_type"], "url_type": a["url_type"],
        "parent_page_notion_id": parent_notion_id,
        "local_candidate_notion_path": local_candidate,
        "change_needed": "unknown (metadata-only comparison in v1; no hash of remote binary computed)",
    })

# ============================================================
# 9. Pre/post hash proof (managed:notion untouched by this dry run)
# ============================================================
pre_hashes = {r['obsidian_path']: r['sha256'] for r in obsidian_rows}
post_changed = []
post_check_errors = []
for r in obsidian_rows:
    full = os.path.join(LAVCCA_ROOT_PATH, r['obsidian_path'].replace('/', os.sep))
    try:
        with open(full, 'rb') as f:
            newhash = hashlib.sha256(f.read()).hexdigest()
    except OSError as e:
        post_check_errors.append({"path": r['obsidian_path'], "error": str(e)})
        continue
    if newhash != r['sha256']:
        post_changed.append(r['obsidian_path'])

# ============================================================
# 10. Report
# ============================================================
report = {
    "run_id": RUN_ID,
    "mode": "DRY_RUN",
    # Crawl API errors abort before this point; any error here came from the
    # change-plan phase (canonical-text fetches) and is surfaced, not hidden.
    "status": "OK" if not API_ERRORS else "PLAN_PHASE_API_ERRORS",
    "crawl_complete": True,
    "crawl_health": {"pass": True, "checks": health_checks},
    "api_call_count": API_CALL_COUNT,
    "api_errors": API_ERRORS,
    "query_discovered_page_count": len(QUERY_DISCOVERED_PAGE_IDS),
    "query_hidden_subtree_page_count": sum(1 for n in visited_pages.values() if n) - len(QUERY_DISCOVERED_PAGE_IDS),
    "direct_retrieve_fallback_count": len(fallback_results),
    "direct_retrieve_fallback_results": fallback_results,
    "notion_pages_found": sum(1 for n in visited_pages.values() if n),
    "notion_version": CFG["notion_version"],
    "notion_databases_found": sum(1 for n in visited_dbs.values() if n),
    "notion_data_sources_found": sum(1 for n in visited_data_sources.values() if n),
    "non_page_query_results": non_page_query_results,
    "notion_attachments_found": len(attachments),
    "obsidian_active_markdown_count": sum(1 for r in obsidian_rows if r['bucket'] == 'active'),
    "obsidian_former_team_count": sum(1 for r in obsidian_rows if r['bucket'] == 'former-team'),
    "obsidian_legacy_count": sum(1 for r in obsidian_rows if r['bucket'] == 'legacy'),
    "identity_active_count": sum(1 for r in identity_rows if r['location'] == 'active'),
    "identity_archive_count": sum(1 for r in identity_rows if r['location'] == 'archive'),
    "identity_archive_with_notion_id": sum(len(v) for v in ARCHIVE_IDS.values()),
    "identity_other": IDENTITY_OTHER,
    "cross_bucket_duplicate_ids": CROSS_BUCKET_IDS,
    "status_counts": dict(status_counts),
    "managed_notion_files_checked": len(pre_hashes),
    "managed_notion_files_changed_by_this_run": len(post_changed),
    "managed_notion_files_changed_paths": post_changed,
    "post_check_errors": post_check_errors,
    "duplicate_id_groups": DUPLICATE_IDS,
    "attachment_report_count": len(attachment_report),
    "plan": plan,
    "attachment_report": attachment_report,
}

out_json = os.path.join(LOGS_DIR, f"dry_run_{RUN_ID}.json")
with open(out_json, "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)

md_lines = [f"# LAVCCA Notion Sync -- DRY RUN {RUN_ID}", ""]
md_lines.append(f"- crawl_complete: {report['crawl_complete']}")
md_lines.append(f"- api_call_count: {API_CALL_COUNT}, api_errors: {len(API_ERRORS)}")
md_lines.append(f"- crawl health: PASS {health_checks}")
md_lines.append(f"- query-discovered pages: {report['query_discovered_page_count']}, "
                f"direct-retrieve fallback checked: {report['direct_retrieve_fallback_count']}")
md_lines.append(f"- Notion pages: {report['notion_pages_found']}, databases: {report['notion_databases_found']}, data sources: {report['notion_data_sources_found']}, attachments: {report['notion_attachments_found']}")
md_lines.append(f"- Obsidian active managed:notion: {report['obsidian_active_markdown_count']} "
                 f"(former-team: {report['obsidian_former_team_count']}, legacy: {report['obsidian_legacy_count']})")
md_lines.append(f"- managed:notion files hash-checked: {report['managed_notion_files_checked']}, changed by this run: {report['managed_notion_files_changed_by_this_run']}")
md_lines.append("")
md_lines.append("## Status counts")
for k in ["UNCHANGED","CREATE_CANDIDATE","ARCHIVED_LOCAL_PRESENT","UPDATE_CONFIRMED","METADATA_ONLY_CHANGE",
          "CONTENT_HASH_ANOMALY","LOCAL_MODIFICATION_CONFLICT","UPDATE_UNVERIFIED","CONTENT_FETCH_ERROR","RENDER_UNSUPPORTED",
          "RENAME","MOVE","RENAME_AND_MOVE","PATH_POLICY_UNRESOLVED","QUERY_HIDDEN_ACTIVE","ARCHIVED","TRASHED",
          "MOVED_OUTSIDE_LAVCCA","DESCENDANT_UNKNOWN","PERMISSION_UNKNOWN","NOT_FOUND_UNKNOWN",
          "EXCLUDED","REVIEW","DUPLICATE_ID","CONFLICT","ERROR"]:
    md_lines.append(f"- {k}: {status_counts.get(k, 0)}")
md_lines.append("")
for k in ["CREATE_CANDIDATE","UPDATE_CONFIRMED","METADATA_ONLY_CHANGE","CONTENT_HASH_ANOMALY",
          "LOCAL_MODIFICATION_CONFLICT","UPDATE_UNVERIFIED","CONTENT_FETCH_ERROR","RENDER_UNSUPPORTED","RENAME","MOVE","RENAME_AND_MOVE",
          "PATH_POLICY_UNRESOLVED","QUERY_HIDDEN_ACTIVE","ARCHIVED","TRASHED",
          "MOVED_OUTSIDE_LAVCCA","DESCENDANT_UNKNOWN","PERMISSION_UNKNOWN","NOT_FOUND_UNKNOWN",
          "DUPLICATE_ID","CONFLICT","ERROR"]:
    items = [p for p in plan if p['status'] == k]
    if items:
        md_lines.append(f"## {k} ({len(items)})")
        for it in items:
            md_lines.append(f"- `{it['notion_id']}` | title={it['title']!r} | obsidian_path={it['obsidian_path']!r}"
                            + (f" | proposed_path={it['proposed_path']!r}" if it.get('proposed_path') else "")
                            + f" | {it['reason']}")
        md_lines.append("")

out_md = os.path.join(LOGS_DIR, f"dry_run_{RUN_ID}.md")
with open(out_md, "w", encoding="utf-8") as f:
    f.write("\n".join(md_lines))

log("=== DRY RUN COMPLETE ===")
log(json.dumps({k: v for k, v in report.items() if k not in ("plan", "attachment_report", "api_errors")},
                ensure_ascii=False, indent=2))
log(f"Full report: {out_json}")
log(f"Markdown report: {out_md}")
