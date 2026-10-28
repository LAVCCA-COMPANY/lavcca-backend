"""Notion -> LAVCCA-COMPANY/docs publisher (the team vault target; replaces the OneDrive path).

  <docs>/<vault>/<root>/...md    one file per Notion page (vault = what Obsidian opens)
  <docs>/<vault>/<root>/_attachments/<block id>.<ext>
  <docs>/<vault>/<root>/_deleted/...  pages gone from Notion (moved here, never removed)
  <docs>/.notion-sync/state.json  {nid: path, hashes, ...} + cursor (outside the vault)

The bot owns <root> alone: members have read access to docs, so nobody else writes there and git
history is the backup. A page's path is chosen once from its title and never changes, so wikilinks
survive renames and moves. Unchanged content is not rewritten, so a run without edits commits nothing.
Reuses the engine's renderer (canary_apply) and the runner's change search (ci.run, ci.plan).

usage: python -m ci.docs_publish --docs <checkout> [--vault obsidian-vault] [--root Notion] [--full]
       [--event-path FILE]   (needs NOTION_TOKEN and NOTION_SYNC_CONFIG or --config)
exit:  0 ok | 2 Notion unreachable / crawl incomplete (nothing written) | 10 audit mismatch after write
"""
import os, re, sys, json, time, shutil, argparse, unicodedata, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone

import canary_apply as ca
from ci import plan, run as cirun

STATE_DIR = ".notion-sync"
EXIT_CRAWL, EXIT_AUDIT = 2, cirun.EXIT_AUDIT_MISMATCH
MAX_ATTACHMENT = 50 * 1024 * 1024  # GitHub rejects files over 100 MB
BAD_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]')
# The docs repo is shared with the whole team: a page that looks like it holds credentials is not
# published (reported as withheld, retried every run) until the secret is removed in Notion.
SENSITIVE = re.compile(
    r"(?:secret_|ntn_)[A-Za-z0-9]{30,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_\w{30,}|AKIA[0-9A-Z]{16}"
    r"|sk-[A-Za-z0-9_-]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?:비밀번호|비번|패스워드|password|passwd|pw)\s*[:=：]\s*\S", re.I)


class CrawlError(Exception):
    pass


def make_api(token, version, sleep=time.sleep):
    def api(method, path, body=None):
        url = path if path.startswith("http") else "https://api.notion.com/v1" + path
        for attempt in range(5):
            req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Authorization": f"Bearer {token}", "Notion-Version": version,
                                                  "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code != 429 and e.code < 500:
                    return e.code, {}
                wait = float(e.headers.get("Retry-After") or 2 ** attempt)
            except OSError:
                wait = 2 ** attempt
            sleep(min(wait, 30))
        return None, {}
    return api


def safe_name(title):
    name = BAD_CHARS.sub(" ", unicodedata.normalize("NFC", title or ca.EMPTY_TITLE))
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")[:100].rstrip(". ")
    return name or "Untitled"


class Attachments:
    """Download Notion-hosted files into _attachments/ (signed URLs expire after an hour). Plugs into
    canary_apply.render_file in place of attachment_sync.PageAttachments."""

    def __init__(self, root, md_rel, recorded, download):
        self.root, self.md_dir, self.recorded, self.download = root, os.path.dirname(md_rel), recorded, download
        self.used = {}

    def is_recorded(self, bid):
        return True  # never reuse links from an existing file: we render from Notion only

    def legacy_allowed(self, block):
        return False

    def handles(self, t, bid, d):
        return True

    def resolve(self, t, bid, d, block):
        url = (d.get("file") or {}).get("url") or ""
        ext = os.path.splitext(urllib.parse.unquote(urllib.parse.urlparse(url).path))[1].lower()
        rel = f"_attachments/{bid}{ext if re.fullmatch(r'\.[a-z0-9]{1,8}', ext) else ''}"
        edited = (block or {}).get("last_edited_time")
        rec = self.recorded.get(bid) or {}
        path = os.path.join(self.root, *rel.split("/"))
        if not (rec.get("edited") == edited and rec.get("local_path") == rel and os.path.isfile(path)):
            try:
                data = self.download(url)
            except Exception as e:  # noqa: BLE001  any failure -> placeholder, page retried next run
                return None, {"type": f"{t}:download_failed", "id": bid, "error": type(e).__name__}
            os.makedirs(os.path.dirname(path), exist_ok=True)
            ca.atomic_write_bytes(path, data)
        with open(path, "rb") as f:
            sha = ca.sha256_bytes(f.read())
        self.used[bid] = {"local_path": rel, "edited": edited, "content_sha256": sha}
        return os.path.relpath(rel, self.md_dir or ".").replace(os.sep, "/"), None


def http_download(url):
    with urllib.request.urlopen(url, timeout=120) as r:
        data = r.read(MAX_ATTACHMENT + 1)
    if len(data) > MAX_ATTACHMENT:
        raise ValueError("attachment too large")
    return data


def child_refs(blocks):
    """child_page / child_database ids anywhere in a block tree, in document order."""
    for b in blocks:
        if b.get("type") in ("child_page", "child_database"):
            yield b["type"], ca.norm_id(b["id"]), (b.get(b["type"]) or {}).get("title", "")
        yield from child_refs(b.get("_children") or [])


def db_rows(api, db_id, cfg):
    code, db = api("GET", f"/databases/{db_id}")
    if code != 200:
        raise CrawlError(f"database {db_id}: HTTP {code}")
    rows = []
    for ds in db.get("data_sources") or []:
        def page(cursor, _ds=ds["id"]):
            body = {"page_size": cfg["api_page_size"], **({"start_cursor": cursor} if cursor else {})}
            c, r = api("POST", f"/data_sources/{_ds}/query", body)
            return (c == 200), r.get("results", []), r.get("has_more", False), r.get("next_cursor")
        try:
            rows += [p for p in ca.paginate(page, cfg["max_pagination_pages"]) if p.get("object") == "page"]
        except ca.PaginationError as e:
            raise CrawlError(f"database {db_id}: {e}")
    return rows


def publish(api, cfg, docs, vault="obsidian-vault", root_dir="Notion", seeds=None, download=http_download, now=None):
    """seeds None = full crawl from the root page. Otherwise only those page ids (and new pages found
    under them) are re-read. Returns (exit_code, report)."""
    now = now or datetime.now(timezone.utc)
    root = os.path.join(docs, vault, root_dir)
    state_path = os.path.join(docs, STATE_DIR, "state.json")
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        state = {"items": {}, "attachments": {}, "databases": {}}
    items, dbs = state["items"], state.setdefault("databases", {})
    root_id = ca.norm_id(cfg["lavcca_root_id"])
    report = {"started_at": now.isoformat(), "full": seeds is None, "created": [], "updated": [], "deleted": [],
              "restored": [], "renamed": [], "render_warnings": {}, "excluded": [], "withheld": []}
    skip = {ca.norm_id(i) for i in (cfg.get("excluded_ids") or []) + (cfg.get("review_ids") or [])}
    fetched = {}  # nid -> (page, blocks, folder of parent)

    def folder_of(nid):
        return items[nid]["obsidian_path"][:-3]

    def place(page, parent_folder):
        nid = ca.norm_id(page["id"])
        if nid in items:
            return
        taken = {p["obsidian_path"].lower() for p in items.values()}
        name = safe_name(ca.page_title(page))
        # ids share a time-based prefix (siblings created together often match in the first 8 hex),
        # so the short suffix uses the random tail and every candidate is re-checked.
        for cand in (name, f"{name} ({nid[-8:]})", f"{name} ({nid})"):
            rel = f"{parent_folder}/{cand}.md".lstrip("/")
            if rel.lower() not in taken:
                break
        items[nid] = {"notion_id": nid, "obsidian_path": rel, "status": "new"}

    def visit(nid, parent_folder, recurse_all):
        if nid in skip:  # config excluded/review: the page and everything below it stay out of docs
            report["excluded"].append(nid)
            return "excluded"
        code, page = api("GET", f"/pages/{nid}")
        if code in (403, 404) or (code == 200 and (page.get("in_trash") or page.get("archived"))):
            return "gone"
        if code != 200:
            raise CrawlError(f"page {nid}: HTTP {code}")
        place(page, parent_folder)
        st = items[nid]
        if st.get("notion_last_edited_time") == page["last_edited_time"] and st.get("status") == "active" \
                and not recurse_all:
            return "unchanged"
        try:
            blocks = ca.fetch_block_tree(api, nid, cfg)
        except ca.ContentFetchError as e:
            raise CrawlError(str(e))
        fetched[nid] = (page, blocks)
        kids = []
        for kind, cid, title in child_refs(blocks):
            if kind == "child_page":
                kids.append((cid, folder_of(nid)))
            else:
                dfolder = f"{folder_of(nid)}/{safe_name(title)}"
                dbs[cid] = {"folder": dfolder, "parent": nid}
                kids += [(ca.norm_id(r["id"]), dfolder) for r in db_rows(api, cid, cfg)]
        st["children"] = [k for k, _ in kids]
        # shortcut: a full crawl re-fetches every block tree (~2 calls/page, ~1 min per 100 pages);
        # reuse st["children"] for unchanged pages if reconcile runs get slow.
        for cid, cfolder in kids:
            if recurse_all or cid not in items or items[cid].get("status") != "active":
                visit(cid, cfolder, recurse_all)
        return "visited"

    def parent_folder_for(page):
        p = page.get("parent") or {}
        pid = ca.norm_id(p.get("page_id") or "")
        if pid and pid in items:
            return folder_of(pid)
        dbid = ca.norm_id(p.get("database_id") or "")
        if dbid in dbs:
            return dbs[dbid]["folder"]
        return None

    try:
        if seeds is None:
            if visit(root_id, "", True) == "gone":
                raise CrawlError("root page not readable")
        else:
            for nid in seeds:
                nid = ca.norm_id(nid)
                if nid == root_id or nid in items:
                    folder = os.path.dirname(items[nid]["obsidian_path"]) if nid in items else ""
                    if visit(nid, folder, False) == "gone" and nid in items:
                        _mark_gone(api, nid, items, root, root_id, report)
                    continue
                code, page = api("GET", f"/pages/{nid}")
                if code == 200 and not page.get("in_trash"):
                    folder = parent_folder_for(page)
                    if folder is not None:
                        visit(nid, folder, False)
    except CrawlError as e:
        report["error"] = str(e)
        return EXIT_CRAWL, report

    if seeds is None:  # anything in state the full crawl no longer reaches: confirm, then move aside
        reached = set(_reachable(root_id, items))
        for nid in [n for n in items if n not in reached and items[n].get("status") != "deleted"]:
            _mark_gone(api, nid, items, root, root_id, report)

    id_map = {n: f"{root_dir}/{s['obsidian_path']}" for n, s in items.items() if s.get("status") != "deleted"}
    used_atts = {}
    for nid, (page, blocks) in fetched.items():
        st = items[nid]
        if st.get("status") == "deleted":  # came back: restore the file before rewriting it
            src = os.path.join(root, "_deleted", *st["obsidian_path"].split("/"))
            if os.path.isfile(src):
                dst = os.path.join(root, *st["obsidian_path"].split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.replace(src, dst)
            report["restored"].append(nid)
        att = Attachments(root, st["obsidian_path"], state["attachments"], download)
        ctx = ca.RenderContext(id_map, "", att)
        title = ca.page_title(page) or ca.EMPTY_TITLE
        props, md = ca.render_properties(page, ctx), ca.render_blocks(blocks, ctx)
        parts = [p for p in ("\n".join(props), md) if p]
        body = f"\n# {title}\n" + ("\n" + "\n\n".join(parts) + "\n" if parts else "")
        body = body.replace("\r\n", "\n").replace("\r", "\n")  # CR in Notion text: the audit parser rejects it
        if SENSITIVE.search(title + "\n" + body):
            report["withheld"].append(nid)
            st.update(status="withheld", title=title)
            st.pop("notion_last_edited_time", None)
            for bid, a in att.used.items():  # its freshly downloaded files stay out too
                if bid not in state["attachments"]:
                    os.remove(os.path.join(root, *a["local_path"].split("/")))
            continue
        fm = "\n".join([
            "---",
            f"notion_id: {nid}",
            f"notion_url: {json.dumps(page.get('url') or '')}",
            f"title: {json.dumps(title, ensure_ascii=False)}",
            f"aliases: {json.dumps([title], ensure_ascii=False)}",
            "managed: notion",
            "---",
        ]) + "\n"
        data = (fm + body).encode("utf-8")
        path = os.path.join(root, *st["obsidian_path"].split("/"))
        old = _read(path) if os.path.isfile(path) else None
        if old != data:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            ca.atomic_write_bytes(path, data)
            report["created" if old is None else "updated"].append(nid)
        if st.get("title") and st["title"] != title:
            report["renamed"].append(nid)
        used_atts.update(att.used)
        st.update(title=title, local_content_hash=ca.sha256_text(body), status="active",
                  last_successful_sync=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        if ctx.critical:  # published with visible placeholders; leave the edit time unrecorded -> retried next run
            report["render_warnings"][nid] = ctx.critical[:10]
            st.pop("notion_last_edited_time", None)
        else:
            st["notion_last_edited_time"] = page["last_edited_time"]
    state["attachments"].update(used_atts)

    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    active = {n: s for n, s in items.items() if s.get("status") == "active"}
    report["audit"] = plan.audit_mirror(root, {"items": active, "attachments": state["attachments"]})
    seen = set()
    for s in active.values():  # two pages on one path: one of them silently overwrote the other
        key = unicodedata.normalize("NFC", s["obsidian_path"]).lower()
        if key in seen:
            report["audit"].append({"path": s["obsidian_path"], "problem": "path shared by two pages"})
        seen.add(key)
    ca.atomic_write_bytes(state_path, (json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode())
    return (EXIT_AUDIT if report["audit"] else 0), report


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _reachable(root_id, items):
    todo, seen = [root_id], set()
    while todo:
        n = todo.pop()
        if n in seen or n not in items:
            continue
        seen.add(n)
        todo += items[n].get("children") or []
    return seen


def _mark_gone(api, nid, items, root, root_id, report):
    """Trashed, inaccessible or moved outside the root: move the file to _deleted/ (git keeps history)."""
    code, page = api("GET", f"/pages/{nid}")
    if code is None or (code >= 500):
        return  # cannot tell: leave it, the next reconcile decides
    if code == 200 and not (page.get("in_trash") or page.get("archived")):
        verdict, _ = ca.verify_descendant(api, page.get("parent"), root_id)
        if verdict != "OUTSIDE":
            return
    st = items[nid]
    src = os.path.join(root, *st["obsidian_path"].split("/"))
    if os.path.isfile(src):
        dst = os.path.join(root, "_deleted", *st["obsidian_path"].split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(src, dst)
    st["status"] = "deleted"
    st.pop("notion_last_edited_time", None)
    report["deleted"].append(nid)


def plan_seeds(state, cfg, event, search):
    """None = full crawl. Otherwise page ids to re-read: edited since the cursor (search) + webhook hints."""
    cursor = state.get("cursor")
    if not cursor:
        return None
    ids, _types = cirun.webhook_hint(event)
    results = search(plan._ts(cursor) - plan.OVERLAP)
    if results is None:
        return None
    seeds = [r["id"] for r in results if r.get("object") == "page"] + ids
    return list(dict.fromkeys(ca.norm_id(s) for s in seeds))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", required=True)
    ap.add_argument("--vault", default="obsidian-vault")
    ap.add_argument("--root", default="Notion")
    ap.add_argument("--config", help="config.json path (default: NOTION_SYNC_CONFIG env)")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--event-path", default=os.environ.get("GITHUB_EVENT_PATH"))
    a = ap.parse_args()
    if a.config:
        with open(a.config, encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = json.loads(os.environ["NOTION_SYNC_CONFIG"])
    cfg = {"api_page_size": 100, "max_pagination_pages": 500, **cfg}
    if os.environ.get("NOTION_SYNC_ROOT_ID"):  # test runs point at a small subtree
        cfg["lavcca_root_id"] = os.environ["NOTION_SYNC_ROOT_ID"]
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        import secret_loader
        token = secret_loader.load_notion_token()[0]
    api = make_api(token, cfg["notion_version"])
    state_path = os.path.join(a.docs, STATE_DIR, "state.json")
    state = json.loads(_read(state_path)) if os.path.isfile(state_path) else {}
    event = json.loads(_read(a.event_path)) if a.event_path and os.path.isfile(a.event_path) else None
    started = datetime.now(timezone.utc)
    seeds = None if a.full else plan_seeds(state, cfg, event,
                                           lambda since: cirun.search_since(token, cfg["notion_version"], since))
    try:
        code, report = publish(api, cfg, a.docs, a.vault, a.root, seeds)
    except Exception as e:  # noqa: BLE001  public log: exception type only (messages can carry titles/paths)
        print(json.dumps({"error": type(e).__name__, "exit": 1}))
        sys.exit(1)
    if code in (0, EXIT_AUDIT):  # advance the cursor only after a complete run
        with open(state_path, encoding="utf-8") as f:
            st = json.load(f)
        st["cursor"] = started.isoformat().replace("+00:00", "Z")
        ca.atomic_write_bytes(state_path, (json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode())
    # Actions logs of the public backend repo are world-readable: counts only.
    print(json.dumps({"full": report["full"], **{k: len(report[k]) for k in
                      ("created", "updated", "deleted", "restored", "renamed", "render_warnings", "excluded", "withheld")},
                      "audit_problems": len(report.get("audit") or []), "error": bool(report.get("error")),
                      "exit": code}))
    sys.exit(code)


if __name__ == "__main__":
    main()
