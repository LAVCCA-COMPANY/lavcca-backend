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
from collections import Counter
from datetime import datetime, timezone

import canary_apply as ca
from ci import plan, run as cirun

STATE_DIR = ".notion-sync"
EXIT_CRAWL, EXIT_AUDIT = 2, cirun.EXIT_AUDIT_MISMATCH
# Larger files (e.g. interview recordings) stay in Notion and are linked by block: keeps the docs repo small.
MAX_ATTACHMENT = 50 * 1024 * 1024
BAD_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]')
# Report only (every page is published): which kinds of credential a page appears to contain, so a
# person can check before an apply. Kinds and counts only -- never the matched value.
CREDENTIAL_KINDS = {
    "notion_token": re.compile(r"(?:secret_|ntn_)[A-Za-z0-9]{30,}"),
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_\w{30,}"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "api_key": re.compile(r"sk-[A-Za-z0-9_-]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "password_value": re.compile(r"(?:비밀번호|비번|패스워드|password|passwd)\s*[:=：]\s*\S", re.I),
}


def credential_kinds(text):
    return sorted(k for k, rx in CREDENTIAL_KINDS.items() if rx.search(text))


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


class TooLarge(Exception):
    pass


class Attachments:
    """Download Notion-hosted files into _attachments/ (signed URLs expire after an hour). Plugs into
    canary_apply.render_file in place of attachment_sync.PageAttachments."""

    def __init__(self, root, md_rel, recorded, download, page_id=""):
        self.root, self.md_dir, self.recorded, self.download = root, os.path.dirname(md_rel), recorded, download
        self.page_id = page_id
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
            except TooLarge:  # stays in Notion: ids-only link, unchanged by renames or re-signed URLs
                return f"https://www.notion.so/{self.page_id}#{bid}", None
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
        if int(r.headers.get("Content-Length") or 0) > MAX_ATTACHMENT:
            raise TooLarge()  # decided from the header: the body is never read
        data = r.read(MAX_ATTACHMENT + 1)
    if len(data) > MAX_ATTACHMENT:
        raise TooLarge()
    return data


NOTION_FILE_URL = re.compile(r"^https://(?:prod-files-secure\.s3\.[a-z0-9-]+\.amazonaws\.com/"
                             r"|s3\.[a-z0-9-]+\.amazonaws\.com/secure\.notion-static\.com/|file\.notion\.so/)")


def hosted_links_as_files(blocks):
    """A bookmark/embed/link_preview pointing at a Notion-hosted file carries a signed URL that changes on
    every fetch and expires within the hour: turn it into a file block so it is downloaded like one."""
    for b in blocks:
        t = b.get("type")
        url = (b.get(t) or {}).get("url") or "" if t in ("bookmark", "embed", "link_preview") else ""
        if NOTION_FILE_URL.match(url):
            b["file"] = {"type": "file", "file": {"url": url}, "caption": (b[t].get("caption") or []),
                         "name": os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(url).path))}
            b["type"] = "file"
        hosted_links_as_files(b.get("_children") or [])


def child_refs(blocks):
    """child_page / child_database ids anywhere in a block tree, in document order."""
    for b in blocks:
        if b.get("type") in ("child_page", "child_database"):
            yield b["type"], ca.norm_id(b["id"]), (b.get(b["type"]) or {}).get("title", "")
        yield from child_refs(b.get("_children") or [])


DB_PLACEHOLDER = re.compile(r"^(?P<indent>[ \t>]*)(?P<line>.*<!-- notion-sync: child_database (?P<id>[0-9a-f]{32}); "
                            r"its rows are synced as separate pages -->)$", re.M)


def row_date(page):
    """ISO start date of a page's first date property (by name); '' when it has none. Shown, never invented."""
    props = (page or {}).get("properties") or {}
    for name in sorted(props):
        v = props[name] or {}
        if v.get("type") == "date" and (v.get("date") or {}).get("start"):
            return v["date"]["start"][:10]
    return ""


def db_row_links(md, dbs, rows_meta, id_map):
    """Under each inline-database placeholder, list the database's rows as wikilinks, so the rows are part
    of the graph. The placeholder line stays as it was. Only rows that have a page in this run or the
    last are listed (no links to missing files), each once, ordered by date, title, id (dateless last)."""
    def sub(m):
        rows = list(dict.fromkeys(r for r in (dbs.get(m["id"]) or {}).get("rows") or []
                                  if r in id_map and r in rows_meta))
        if not rows:
            return m.group(0)
        rows.sort(key=lambda r: (rows_meta[r][1] or "9999-99-99", rows_meta[r][0], r))
        lines = []
        for r in rows:
            title, date = rows_meta[r]
            label = title.replace("|", " ").replace("]]", "] ]")
            lines.append(f"{m['indent']}- [[{id_map[r][:-3]}|{label}]]" + (f" · {date}" if date else ""))
        return m.group(0) + "\n" + "\n".join(lines)
    return DB_PLACEHOLDER.sub(sub, md)


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
              "restored": [], "renamed": [], "render_warnings": {}, "credential_findings": {}}
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
        code, page = api("GET", f"/pages/{nid}")
        if code in (403, 404) or (code == 200 and (page.get("in_trash") or page.get("archived"))):
            return "gone"
        if code != 200:
            raise CrawlError(f"page {nid}: HTTP {code}")
        place(page, parent_folder)
        st = items[nid]
        # No "last_edited_time unchanged -> skip": it has minute precision, so a second edit within the
        # minute after a sync would be missed. Pages reach here only as seeds (webhook/search) or new
        # children, and the file is rewritten only when its bytes differ.
        try:
            blocks = ca.fetch_block_tree(api, nid, cfg)
        except ca.ContentFetchError as e:
            raise CrawlError(str(e))
        hosted_links_as_files(blocks)
        fetched[nid] = (page, blocks)
        kids = []
        for kind, cid, title in child_refs(blocks):
            if kind == "child_page":
                kids.append((cid, folder_of(nid)))
            else:
                dfolder = f"{folder_of(nid)}/{safe_name(title)}"
                rows = [ca.norm_id(r["id"]) for r in db_rows(api, cid, cfg)]
                dbs[cid] = {"folder": dfolder, "parent": nid, "rows": rows}  # rows: listed in the parent page
                kids += [(r, dfolder) for r in rows]
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

    # A database row that was added, renamed, re-dated or removed changes the row list in its parent page,
    # which an incremental run would not otherwise re-read: fetch those parents too.
    row_db = {r: cid for cid, db in dbs.items() for r in db.get("rows") or []}
    for n, (page, _) in fetched.items():  # a row created since the last run is not in the recorded list yet
        dbid = ca.norm_id((page.get("parent") or {}).get("database_id") or "")
        if dbid in dbs:
            row_db.setdefault(n, dbid)
    stale = {dbs[row_db[n]]["parent"] for n in report["deleted"] if n in row_db}
    for n, (page, _) in list(fetched.items()):
        if n in row_db and (items[n].get("status") != "active" or items[n].get("row_date", "") != row_date(page)
                            or items[n].get("title") != (ca.page_title(page) or ca.EMPTY_TITLE)):
            stale.add(dbs[row_db[n]]["parent"])
    try:
        for p in sorted(stale - set(fetched)):
            if items.get(p, {}).get("status") == "active":
                visit(p, os.path.dirname(items[p]["obsidian_path"]), False)
    except CrawlError as e:
        report["error"] = str(e)
        return EXIT_CRAWL, report
    row_db = {r: cid for cid, db in dbs.items() for r in db.get("rows") or []}

    id_map = {n: f"{root_dir}/{s['obsidian_path']}" for n, s in items.items() if s.get("status") != "deleted"}
    rows_meta = {}  # title and date shown in parent row lists: this run's Notion data, else the last recorded
    for n, s in items.items():
        if n in fetched:
            rows_meta[n] = (ca.page_title(fetched[n][0]) or ca.EMPTY_TITLE, row_date(fetched[n][0]))
        elif s.get("status") == "active":
            rows_meta[n] = (s.get("title") or ca.EMPTY_TITLE, s.get("row_date", ""))
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
        att = Attachments(root, st["obsidian_path"], state["attachments"], download, nid)
        ctx = ca.RenderContext(id_map, "", att)
        title = ca.page_title(page) or ca.EMPTY_TITLE
        props, md = ca.render_properties(page, ctx), ca.render_blocks(blocks, ctx)
        md = db_row_links(md, dbs, rows_meta, id_map)
        if row_date(page) and nid in row_db:
            st["row_date"] = row_date(page)
        else:
            st.pop("row_date", None)
        parts = [p for p in ("\n".join(props), md) if p]
        body = f"\n# {title}\n" + ("\n" + "\n\n".join(parts) + "\n" if parts else "")
        body = body.replace("\r\n", "\n").replace("\r", "\n")  # CR in Notion text: the audit parser rejects it
        kinds = credential_kinds(title + "\n" + body)
        if kinds:
            report["credential_findings"][nid] = kinds
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
            st["last_successful_sync"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")  # only on a write: no-op runs leave state as is
        used_atts.update(att.used)
        st.update(title=title, local_content_hash=ca.sha256_text(body), status="active")
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


def advance_cursor(state_path, before, started):
    """Move the cursor to `started` only if the run changed state.json (or there is no cursor yet): a run
    that found nothing new must leave the file byte-identical, or every scheduled run would commit. The
    older cursor stays correct -- pages edited since then are recorded and skipped as unchanged."""
    st = json.loads(_read(state_path))
    strip = lambda d: {k: v for k, v in d.items() if k != "cursor"}  # noqa: E731
    if st.get("cursor") and strip(st) == strip(before):
        return False
    st["cursor"] = started.isoformat().replace("+00:00", "Z")
    ca.atomic_write_bytes(state_path, (json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode())
    return True


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
    if code in (0, EXIT_AUDIT):  # never after an incomplete run: the next run retries the same window
        advance_cursor(state_path, state, started)
    # Actions logs of the public backend repo are world-readable: counts only.
    print(json.dumps({"full": report["full"], **{k: len(report[k]) for k in
                      ("created", "updated", "deleted", "restored", "renamed", "render_warnings")},
                      "audit_problems": len(report.get("audit") or []), "error": bool(report.get("error")),
                      "credential_pages": len(report["credential_findings"]),
                      "credential_kinds": dict(Counter(k for ks in report["credential_findings"].values() for k in ks)),
                      "exit": code}))
    sys.exit(code)


if __name__ == "__main__":
    main()
