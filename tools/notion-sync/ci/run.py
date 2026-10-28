"""Runner entry around the unchanged engine: detect changes -> engine (incremental, else full) ->
audit state.json against the files -> advance the cursor. The OneDrive mirror restore before
and the verified upload after this step live in the workflow wrapper (ci/graph.py).

usage: python -m ci.run --sync-dir <mirror>/<managed root>/_sync [--event-path FILE] [--full]
exit:  0 ok | engine exit code (2 crawl/API, 7 locked, 8 apply failed, ...) = upload nothing
       10  engine ok but some file disagrees with state.json -> upload, then fail the job (alert)
"""
import os, re, sys, json, time, argparse, subprocess, urllib.request, urllib.error
from datetime import datetime, timezone

import canary_apply as ca
from ci import plan

CURSOR_FILE = "incremental_cursor.json"  # lives next to state.json and travels with it
EXIT_AUDIT_MISMATCH = 10
SEARCH_MAX_PAGES = 10


def notion_post(url, token, version, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {token}", "Notion-Version": version, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except OSError:
        return None, {}


def search_since(token, version, since, post=notion_post, sleep=time.sleep):
    """Pages/data sources edited at or after `since` (datetime), newest first.
    None = could not list them reliably -> caller runs a full crawl."""
    out, cursor = [], None
    for _ in range(SEARCH_MAX_PAGES):
        body = {"sort": {"direction": "descending", "timestamp": "last_edited_time"}, "page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        for attempt in range(4):
            code, data = post("https://api.notion.com/v1/search", token, version, body)
            if code == 200:
                break
            if attempt < 3:
                sleep(2 ** attempt)
        else:
            return None
        for obj in data.get("results", []):
            out.append(obj)
            if plan._ts(obj["last_edited_time"]) < since:
                return out
        if not data.get("has_more"):
            return out
        cursor = data.get("next_cursor")
        if not cursor:
            return None
    return None  # more edits than SEARCH_MAX_PAGES pages: not worth paging, crawl instead


def engine_runner(sync_dir):
    def run(args):
        # Actions logs of a public repo are world-readable: engine output (titles, paths) goes to
        # the private OneDrive run log instead.
        os.makedirs(os.path.join(sync_dir, "logs"), exist_ok=True)
        log = os.path.join(sync_dir, "logs", f"engine_{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.log")
        with open(log, "w", encoding="utf-8") as f:
            return subprocess.run([sys.executable, os.path.join(sync_dir, "notion_sync.py"), *args], cwd=sync_dir,
                                  stdout=f, stderr=subprocess.STDOUT,
                                  env=dict(os.environ, PYTHONIOENCODING="utf-8")).returncode
    return run


HINT_ID = re.compile(r"^[0-9a-fA-F-]{32,36}$")
HINT_TYPE = re.compile(r"^[a-z_]+\.[a-z_]+$")


def webhook_hint(event):
    """Page ids / event types forwarded by the webhook Worker as the workflow_dispatch input "hint".
    Anyone able to dispatch could put anything there: keep only well-formed values, never fail on it."""
    raw = ((event or {}).get("inputs") or {}).get("hint") or ""
    try:
        hint = json.loads(raw) if raw else {}
    except ValueError:
        return [], []
    if not isinstance(hint, dict):
        return [], []
    ids = [i for i in hint.get("ids") or [] if isinstance(i, str) and HINT_ID.match(i)][:plan.MAX_TARGETS + 1]
    types = [t for t in hint.get("types") or [] if isinstance(t, str) and HINT_TYPE.match(t)][:20]
    return ids, types


def _load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def run(sync_dir, event, force_full=False, search=None, run_engine=None, now=None):
    """Returns (exit_code, report dict)."""
    started = (now or (lambda: datetime.now(timezone.utc)))()
    cfg = ca.normalize_config(_load(os.path.join(sync_dir, "config.json")))
    state = _load(os.path.join(sync_dir, "state.json"))
    cursor_doc = _load(os.path.join(sync_dir, CURSOR_FILE), {})
    hint_ids, hint_types = webhook_hint(event)
    report = {"started_at": started.isoformat(), "cursor": cursor_doc.get("cursor")}
    if force_full:
        mode, targets, reasons = "full", [], ["forced full crawl"]
    else:
        results = None
        if cursor_doc.get("cursor"):
            results = search(plan._ts(cursor_doc["cursor"]) - plan.OVERLAP)
        if cursor_doc.get("cursor") and results is None:
            mode, targets, reasons = "full", [], ["notion search unavailable or too many edits"]
        else:
            mode, targets, reasons = plan.plan_run(results or [], state["items"], cursor_doc.get("cursor"), cfg,
                                                   hint_ids, hint_types)
    report.update(planned_mode=mode, targets=targets, reasons=reasons)
    code, used = plan.execute(mode, targets, run_engine or engine_runner(sync_dir))
    report.update(engine_exit=code, mode=used)
    if code != 0:
        return code, report  # nothing to upload; cursor stays so the next run retries the same window
    state = _load(os.path.join(sync_dir, "state.json"))
    report["audit"] = plan.audit_mirror(cfg["lavcca_root_path"], state)
    tmp = os.path.join(sync_dir, CURSOR_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"cursor": started.isoformat().replace("+00:00", "Z"), "mode": used}, f)
    os.replace(tmp, os.path.join(sync_dir, CURSOR_FILE))
    return (EXIT_AUDIT_MISMATCH if report["audit"] else 0), report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync-dir", required=True)
    ap.add_argument("--event-path", default=os.environ.get("GITHUB_EVENT_PATH"))
    ap.add_argument("--full", action="store_true")
    a = ap.parse_args()
    sys.path.insert(0, a.sync_dir)
    cfg = ca.normalize_config(_load(os.path.join(a.sync_dir, "config.json")))
    token = os.environ["NOTION_TOKEN"]
    code, report = run(a.sync_dir, _load(a.event_path) if a.event_path else None, a.full,
                       search=lambda since: search_since(token, cfg["notion_version"], since))
    # Actions logs are readable by every repo reader: counts only. Paths/ids go to the private run log.
    os.makedirs(os.path.join(a.sync_dir, "logs"), exist_ok=True)
    with open(os.path.join(a.sync_dir, "logs", f"ci_run_{report['started_at'][:19].replace(':', '')}.json"),
              "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps({"mode": report.get("mode"), "planned_mode": report["planned_mode"],
                      "targets": len(report["targets"]), "engine_exit": report.get("engine_exit"),
                      "audit_problems": len(report.get("audit") or []), "exit": code}))
    sys.exit(code)


if __name__ == "__main__":
    main()
