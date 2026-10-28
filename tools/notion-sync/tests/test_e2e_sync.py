# -*- coding: utf-8 -*-
"""End-to-end tests: the REAL production command `notion_sync.py --apply-updates` runs as a
subprocess against a temporary vault and a fake Notion HTTP server on 127.0.0.1.

Covered user scenarios:
  A new DOCX on an existing page      E network failure, then recovery
  B new image                         F file synced but markdown link missing -> self-heal
  C DOCX + image together             G markdown link present but file missing -> self-heal
  D local DOCX locked (Word)          H overlapping run (lock held) -> no lost change
Every scenario also checks: second run is a no-op (no download), managed:obsidian and
06_KNOWLEDGE untouched, no signed URL anywhere outside the attachment bytes, no lock left.
"""
import io, os, re, sys, json, time, glob, shutil, socket, zipfile, tempfile, threading, subprocess, unittest
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote, quote

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CODE_DIR)
import canary_apply as ca  # noqa: E402

ROOT, CHOI, LEE = "a0" * 16, "c1" * 16, "e1" * 16
SECRET = "SIGSECRET"
OLD = "2026-09-01T00:00:00.000Z"
BOOTSTRAP = "2026-09-30T22:11:23+09:00"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def make_docx(text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f"<w:document>{text}</w:document>")
    return buf.getvalue()


def rt(text):
    a = {k: False for k in ("bold", "italic", "strikethrough", "underline", "code")}
    return {"type": "text", "plain_text": text, "href": None, "annotations": a, "text": {"content": text}}


def page(pid, title, parent, edited=OLD):
    return {"object": "page", "id": pid, "created_time": OLD, "last_edited_time": edited, "archived": False,
            "in_trash": False, "parent": parent,
            "properties": {"title": {"type": "title", "title": [{"plain_text": title}]}}}


def para(bid, text):
    return {"object": "block", "id": bid, "type": "paragraph", "created_time": OLD, "last_edited_time": OLD,
            "has_children": False, "paragraph": {"rich_text": [rt(text)]}}


class FakeNotion:
    """Minimal Notion API + signed file host. Each fetch hands out a fresh signature."""

    def __init__(self):
        self.pages, self.children, self.files = {}, {}, {}
        self.file_gets, self.expire_first = [], set()
        self.requests = []  # every GET path, for "which pages did a run touch" assertions
        self.root_503 = 0  # answer the root page with 503 this many times (network coming up)
        self.seq = 0
        self.lock = threading.Lock()
        self.server = None

    # ---- data helpers ----
    def file_block(self, bid, name, t="file", url_name=None, created="2026-10-04T12:57:00.000Z"):
        d = {"type": "file", "caption": [], "file": {"url": "", "expiry_time": "2026-10-04T14:00:00.000Z"},
             "_url_name": url_name or name}
        if name is not None:
            d["name"] = name
        return {"object": "block", "id": bid, "type": t, "created_time": created, "last_edited_time": created,
                "has_children": False, t: d}

    def _signed(self, b):
        b = json.loads(json.dumps(b))
        d = b.get(b["type"])
        if isinstance(d, dict) and d.get("type") == "file":
            self.seq += 1
            d["file"]["url"] = (f"http://127.0.0.1:{self.port}/files/{b['id']}/{quote(d.pop('_url_name'))}"
                                f"?X-Amz-Signature={SECRET}{self.seq}")
        return b

    # ---- server ----
    def start(self):
        api = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send_json(self, code, obj):
                data = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                p = urlparse(self.path)
                parts = [unquote(x) for x in p.path.split("/") if x]
                with api.lock:
                    api.requests.append(p.path)
                    if parts[:2] == ["v1", "pages"] and parts[2] == ROOT and api.root_503 > 0:
                        api.root_503 -= 1
                        return self.send_json(503, {"message": "unavailable"})
                    if parts[:2] == ["v1", "pages"] and parts[2] in api.pages:
                        return self.send_json(200, api.pages[parts[2]])
                    if parts[:2] == ["v1", "blocks"] and len(parts) == 4 and parts[3] == "children":
                        kids = [api._signed(b) for b in api.children.get(parts[2], [])]
                        return self.send_json(200, {"results": kids, "has_more": False, "next_cursor": None})
                    if parts[:2] == ["v1", "blocks"] and len(parts) == 3:
                        for pid, kids in api.children.items():
                            for b in kids:
                                if b["id"] == parts[2]:
                                    out = api._signed(b)
                                    out["parent"] = {"type": "page_id", "page_id": pid}
                                    return self.send_json(200, out)
                    if parts[:1] == ["files"]:
                        bid = parts[1]
                        api.file_gets.append(bid)
                        if bid in api.expire_first:
                            api.expire_first.discard(bid)
                            return self.send_json(403, {"message": "Request has expired"})
                        data = api.files[bid]
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        return self.wfile.write(data)
                return self.send_json(404, {"code": "object_not_found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


class Win32Lock:
    """Opens a file with no sharing, like Word does: other processes get PermissionError."""

    def __init__(self, path):
        import ctypes
        from ctypes import wintypes
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.CreateFileW.restype = wintypes.HANDLE
        self.k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.h = self.k32.CreateFileW(path, 0x80000000, 0, None, 3, 0x80, None)
        if self.h in (None, wintypes.HANDLE(-1).value):
            raise OSError("could not lock")

    def release(self):
        self.k32.CloseHandle(self.h)


class E2EBase(unittest.TestCase):
    ROOT_DIR, PROJECT, ENV_PREFIX = "LAVCCA", "LAVCCA", "LAVCCA_"  # legacy layout: TestE2ELegacyLayout

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lavcca_e2e_")
        self.vault = os.path.join(self.tmp, "vault")
        self.lavcca = os.path.join(self.vault, self.ROOT_DIR)
        self.sync = os.path.join(self.lavcca, "_sync")
        os.makedirs(self.sync)
        for f in glob.glob(os.path.join(CODE_DIR, "*.py")):
            shutil.copy(f, self.sync)
        self.notion = FakeNotion()
        self.notion.pages = {ROOT: page(ROOT, self.ROOT_DIR, {"type": "workspace", "workspace": True}),
                             CHOI: page(CHOI, "김멤버", {"type": "page_id", "page_id": ROOT}),
                             LEE: page(LEE, "박멤버", {"type": "page_id", "page_id": ROOT})}
        self.notion.children = {
            ROOT: [{"object": "block", "id": CHOI, "type": "child_page", "has_children": True, "created_time": OLD,
                    "last_edited_time": OLD, "child_page": {"title": "김멤버"}},
                   {"object": "block", "id": LEE, "type": "child_page", "has_children": True, "created_time": OLD,
                    "last_edited_time": OLD, "child_page": {"title": "박멤버"}}],
            CHOI: [para("c2" * 16, "김멤버 본문")],
            LEE: [para("e2" * 16, "박멤버 본문")],
        }
        self.notion.start()
        self.pages = {CHOI: ("02_MEMBER_A/Imported-Notion/김멤버.md", "김멤버"),
                      LEE: ("03_MEMBER_B/Imported-Notion/박멤버.md", "박멤버")}
        items = {}
        for nid, (rel, owner) in self.pages.items():
            blocks = [dict(b, _children=[]) for b in self.notion.children[nid]]
            body = ca.render_body(owner, blocks, ca.RenderContext({}, ""), page={"parent": {"type": "page_id"}})
            h = ca.sha256_text(body)
            fm = (f"---\nproject: {self.PROJECT}\nowner: {owner}\nsource: notion\nmanaged: notion\nnotion_id: {nid}\n"
                  f'notion_path: "{self.ROOT_DIR}/{owner} {nid}.md"\nnotion_last_edited_time: "{OLD}"\n'
                  f'last_synced_content_hash: "{h}"\nlast_synced_at: "2026-10-01T00:00:00Z"\n---\n')
            self.write(rel, fm + body)
            items[nid] = {"notion_id": nid, "notion_last_edited_time": OLD, "api_rendered_content_hash": h,
                          "local_content_hash": h, "obsidian_path": rel, "last_successful_sync": "2026-10-01T00:00:00Z",
                          "last_seen_edited_time": OLD, "last_synced_content_hash": h, "record_type": "SYNCED"}
        self.write("01_SHARED/obsidian-note.md", "---\nmanaged: obsidian\n---\n\n사람이 쓴 노트\n")
        self.write("06_KNOWLEDGE/k.md", "---\nmanaged: obsidian\n---\n\n지식\n")
        with open(os.path.join(self.sync, "state.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": 1, "items": items}, f)
        with open(os.path.join(self.sync, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"notion_version": "2025-09-03", "lavcca_root_id": ROOT, "review_ids": [], "excluded_ids": [],
                       "first_sync_ids": [], "bootstrap_migration_completed_at": BOOTSTRAP,
                       "vault_root": self.vault, "lavcca_root_path": self.lavcca,
                       "protected_relative_paths": ["06_KNOWLEDGE", "99_ARCHIVE", "_migration", "_sync"],
                       "protected_vault_root_paths": [], "max_pagination_pages": 50, "api_page_size": 100,
                       "owner_roots": {"02_MEMBER_A": "김멤버", "03_MEMBER_B": "박멤버", "04_MEMBER_C": "이멤버"}}, f)
        self.protected = {p: self.read_bytes(p) for p in ("01_SHARED/obsidian-note.md", "06_KNOWLEDGE/k.md")}

    def tearDown(self):
        self.notion.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    @staticmethod
    def clean_env():  # no inherited LAVCCA_/NATAM_ overrides leak into the engine under test
        return {k: v for k, v in os.environ.items() if not k.startswith(("LAVCCA_", "NATAM_"))}

    def abs(self, rel):
        return os.path.join(self.lavcca, *rel.split("/"))

    def write(self, rel, text):
        os.makedirs(os.path.dirname(self.abs(rel)), exist_ok=True)
        with open(self.abs(rel), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        old = time.mktime((2025, 12, 31, 0, 0, 0, 0, 0, 0))
        os.utime(self.abs(rel), (old, old))

    def read_bytes(self, rel):
        with open(self.abs(rel), "rb") as f:
            return f.read()

    def md(self, nid):
        return self.read_bytes(self.pages[nid][0]).decode("utf-8")

    def state(self):
        with open(os.path.join(self.sync, "state.json"), encoding="utf-8") as f:
            return json.load(f)

    def touch(self, nid, when):
        self.notion.pages[nid]["last_edited_time"] = when

    def add(self, nid, block, data=None, when="2026-10-04T12:57:00.000Z"):
        self.notion.children[nid].append(block)
        if data is not None:
            self.notion.files[block["id"]] = data
        self.touch(nid, when)

    def run_sync(self, port=None, expect=0, args=()):
        time.sleep(1.1)  # distinct RUN_ID / backup stamp per run
        env = dict(self.clean_env(), NOTION_TOKEN="dummy-token", PYTHONIOENCODING="utf-8",
                   **{self.ENV_PREFIX + "E2E_LOOPBACK": "1", self.ENV_PREFIX + "NETWORK_WAIT_S": "0",
                      self.ENV_PREFIX + "NOTION_API_BASE": f"http://127.0.0.1:{port or self.notion.port}/v1"})
        p = subprocess.run([sys.executable, os.path.join(self.sync, "notion_sync.py"), "--apply-updates", *args],
                           cwd=self.sync, env=env, capture_output=True, text=True, encoding="utf-8", timeout=300)
        out = p.stdout + p.stderr
        self.assertNotIn("Traceback", out, out[-3000:])
        self.assertEqual(p.returncode, expect, out[-3000:])
        m = re.search(r"Production report: (.*)", out)
        report = None
        if m:
            with open(m.group(1).strip(), encoding="utf-8") as f:
                report = json.load(f)
        return report, out

    def assert_invariants(self):
        for p, data in self.protected.items():
            self.assertEqual(self.read_bytes(p), data, p)
        self.assertFalse(os.path.exists(os.path.join(self.sync, ".sync.lock")))
        for f in glob.glob(os.path.join(self.vault, "**", "*"), recursive=True):
            if os.path.isfile(f) and not f.endswith((".docx", ".png", ".jpg")):
                with open(f, "rb") as fh:
                    blob = fh.read().decode("utf-8", errors="replace")
                self.assertNotIn(SECRET, blob, f)
        self.assertEqual(glob.glob(os.path.join(self.vault, "**", ".notion_att_*"), recursive=True), [])

    def assert_attachment(self, bid, rel, data, link):
        f = self.abs(rel)
        self.assertTrue(os.path.isfile(f), rel)
        self.assertEqual(self.read_bytes(rel), data)
        rec = self.state()["attachments"][bid]
        self.assertEqual((rec["local_path"], rec["content_sha256"], rec["size_bytes"]),
                         (rel, ca.sha256_bytes(data), len(data)))
        md_nid = rec["parent_notion_id"]
        self.assertIn(link, self.md(md_nid))
        import posixpath
        target = posixpath.relpath(rel, posixpath.dirname(self.pages[md_nid][0]))
        self.assertIn(target, self.md(md_nid))  # the markdown link points at the committed file
        self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(self.abs(self.pages[md_nid][0])),
                                                    *target.split("/"))))

    def assert_noop_rerun(self):
        gets = len(self.notion.file_gets)
        before = {p: self.read_bytes(self.pages[p][0]) for p in self.pages}
        report, _ = self.run_sync()
        self.assertEqual(report["status"], "NOTHING_TO_APPLY", report.get("status"))
        self.assertEqual(report["attachment_download_candidates"], [])
        self.assertEqual(len(self.notion.file_gets), gets, "duplicate download")
        self.assertEqual({p: self.read_bytes(self.pages[p][0]) for p in self.pages}, before)
        self.assert_invariants()


class TestE2E(E2EBase):
    DOCX = "ACCT chapter 1 homework.docx"
    ATT = "02_MEMBER_A/Imported-Notion/Attachments/"

    def test_A_new_docx_on_existing_page(self):
        data = make_docx("acct")
        self.add(CHOI, self.notion.file_block("d1" * 16, self.DOCX), data)
        report, _ = self.run_sync()
        self.assertEqual((report["status"], report["applied"]), ("ALL_APPLIED", [CHOI]))
        self.assert_attachment("d1" * 16, self.ATT + self.DOCX, data, f"[{self.DOCX}](<Attachments/{self.DOCX}>)")
        self.assert_invariants()
        self.assert_noop_rerun()

    def test_B_new_image(self):
        self.add(CHOI, self.notion.file_block("1a" * 16, None, t="image", url_name="photo.png"), PNG)
        report, _ = self.run_sync()
        self.assertEqual(report["status"], "ALL_APPLIED")
        name = f"notion-image-{'1a' * 16}.png"
        self.assert_attachment("1a" * 16, self.ATT + name, PNG, f"![](Attachments/{name})")
        self.assert_noop_rerun()

    def test_C_docx_and_image_together(self):
        data = make_docx("both")
        self.add(CHOI, self.notion.file_block("d1" * 16, self.DOCX), data)
        self.add(CHOI, self.notion.file_block("1a" * 16, None, t="image", url_name="p.png"), PNG)
        report, _ = self.run_sync()
        self.assertEqual(report["status"], "ALL_APPLIED")
        self.assertEqual(len(report["attachments_written"]), 2)
        self.assert_attachment("d1" * 16, self.ATT + self.DOCX, data, f"(<Attachments/{self.DOCX}>)")
        self.assert_attachment("1a" * 16, self.ATT + f"notion-image-{'1a' * 16}.png", PNG, "![](Attachments/")
        self.assert_noop_rerun()

    @unittest.skipUnless(sys.platform == "win32", "uses Windows share-mode file locking")
    def test_D_locked_local_docx_isolated_then_recovers(self):
        first = make_docx("q4")
        self.add(CHOI, self.notion.file_block("d0" * 16, "Q4.docx"), first)
        self.run_sync()
        lock = Win32Lock(self.abs(self.ATT + "Q4.docx"))
        try:
            second = make_docx("ir")
            self.add(CHOI, self.notion.file_block("d2" * 16, "IR.docx"), second, when="2026-10-04T13:30:00.000Z")
            self.notion.children[LEE].append(para("e3" * 16, "박멤버 새 문단"))
            self.touch(LEE, "2026-10-04T13:30:00.000Z")
            md_before = self.md(CHOI)
            report, _ = self.run_sync()                       # no crash while locked
            self.assertEqual(report["status"], "ALL_APPLIED")
            self.assertEqual(report["applied"], [LEE])        # other page keeps syncing
            self.assertEqual(report["attachment_only_pages"], [CHOI])
            self.assertIn("박멤버 새 문단", self.md(LEE))
            self.assertEqual(self.md(CHOI), md_before)        # locked page not written
            self.assertTrue(os.path.isfile(self.abs(self.ATT + "IR.docx")))  # new file still synced
        finally:
            lock.release()
        report, _ = self.run_sync()                           # lock released: automatic recovery
        self.assertEqual(report["applied"], [CHOI])
        self.assert_attachment("d2" * 16, self.ATT + "IR.docx", second, "(Attachments/IR.docx)")
        self.assertIn("(Attachments/Q4.docx)", self.md(CHOI))
        self.assert_noop_rerun()

    def test_E_network_failure_then_recovery(self):
        data = make_docx("net")
        self.add(CHOI, self.notion.file_block("d1" * 16, self.DOCX), data)
        snap = {p: self.read_bytes(self.pages[p][0]) for p in self.pages}
        state = self.state()
        with socket.socket() as s:                            # a port nobody listens on
            s.bind(("127.0.0.1", 0))
            dead = s.getsockname()[1]
        report, out = self.run_sync(port=dead, expect=2)
        self.assertIn("INCOMPLETE_CRAWL", out)
        self.assertEqual({p: self.read_bytes(self.pages[p][0]) for p in self.pages}, snap)
        self.assertEqual(self.state(), state)
        self.assertFalse(os.path.exists(self.abs(self.ATT + self.DOCX)))
        report, _ = self.run_sync()                           # next healthy run
        self.assertEqual(report["status"], "ALL_APPLIED")
        self.assert_attachment("d1" * 16, self.ATT + self.DOCX, data, f"(<Attachments/{self.DOCX}>)")
        self.assert_noop_rerun()

    def test_E2_expired_signed_url_retried(self):
        data = make_docx("exp")
        self.notion.expire_first.add("d1" * 16)
        self.add(CHOI, self.notion.file_block("d1" * 16, self.DOCX), data)
        report, _ = self.run_sync()
        self.assertEqual(report["status"], "ALL_APPLIED")
        self.assertEqual(self.notion.file_gets.count("d1" * 16), 2)
        self.assert_invariants()

    def test_F_file_synced_but_link_missing_self_heals(self):
        data = make_docx("f")
        self.add(CHOI, self.notion.file_block("d1" * 16, self.DOCX), data)
        self.add(CHOI, self.notion.file_block("bb" * 16, "표.xlsx"), b"xlsx")   # unsupported sibling
        md_before = self.md(CHOI)
        report, _ = self.run_sync()
        self.assertEqual(report["attachment_only_pages"], [CHOI])
        self.assertEqual(self.md(CHOI), md_before)            # page cannot render safely: not written
        self.assertIn("d1" * 16, self.state()["attachments"])  # file + record exist, link missing
        self.notion.children[CHOI] = [b for b in self.notion.children[CHOI] if b["id"] != "bb" * 16]
        self.touch(CHOI, "2026-10-04T14:00:00.000Z")           # user removes the unsupported block
        gets = len(self.notion.file_gets)
        report, _ = self.run_sync()
        self.assertEqual(report["applied"], [CHOI])
        self.assertEqual(len(self.notion.file_gets), gets)     # link healed without re-download
        self.assert_attachment("d1" * 16, self.ATT + self.DOCX, data, f"(<Attachments/{self.DOCX}>)")
        self.assert_noop_rerun()

    def test_G_link_present_but_file_missing_self_heals(self):
        data = make_docx("g")
        self.add(CHOI, self.notion.file_block("d1" * 16, self.DOCX), data)
        self.run_sync()
        md = self.md(CHOI)
        os.remove(self.abs(self.ATT + self.DOCX))              # file lost locally
        report, _ = self.run_sync()
        self.assertEqual(report["status"], "ALL_APPLIED")
        self.assertEqual(report["attachment_only_pages"], [CHOI])
        self.assertEqual(self.md(CHOI), md)                    # markdown untouched
        self.assert_attachment("d1" * 16, self.ATT + self.DOCX, data, f"(<Attachments/{self.DOCX}>)")
        self.assert_noop_rerun()

    def test_H_overlapping_run_loses_no_change(self):
        data = make_docx("h")
        self.add(CHOI, self.notion.file_block("d1" * 16, self.DOCX), data)
        with open(os.path.join(self.sync, ".sync.lock"), "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "host": socket.gethostname(), "run_id": "other-run"}, f)
        md_before = self.md(CHOI)
        _, out = self.run_sync(expect=7)                       # another sync holds the lock
        self.assertIn("REFUSED (LOCKED)", out)
        self.assertEqual(self.md(CHOI), md_before)
        os.remove(os.path.join(self.sync, ".sync.lock"))       # the long run finishes
        report, _ = self.run_sync()
        self.assertEqual(report["applied"], [CHOI])
        self.assert_attachment("d1" * 16, self.ATT + self.DOCX, data, f"(<Attachments/{self.DOCX}>)")
        self.assert_noop_rerun()


class TestE2EResilience(E2EBase):
    @unittest.skipUnless(sys.platform == "win32", "uses Windows share-mode file locking")
    def test_J_locked_markdown_aborts_cleanly_then_recovers(self):
        data = make_docx("j")
        self.add(CHOI, self.notion.file_block("d1" * 16, "J.docx"), data)
        lock = Win32Lock(self.abs(self.pages[LEE][0]))
        try:
            report, out = self.run_sync(expect=2)              # no traceback, nothing written
            self.assertIn("LOCAL_INVENTORY_UNREADABLE", out)
            self.assertFalse(os.path.exists(self.abs("02_MEMBER_A/Imported-Notion/Attachments/J.docx")))
        finally:
            lock.release()
        report, _ = self.run_sync()
        self.assertEqual(report["applied"], [CHOI])
        self.assert_invariants()

    def test_K_network_comes_up_late_waits_then_syncs(self):
        data = make_docx("k")
        self.add(CHOI, self.notion.file_block("d1" * 16, "K.docx"), data)
        self.notion.root_503 = 4   # the first probe (1 try + 3 api retries) sees 503 -> wait 10 s
        time.sleep(1.1)
        env = dict(self.clean_env(), NOTION_TOKEN="dummy-token", PYTHONIOENCODING="utf-8", LAVCCA_E2E_LOOPBACK="1",
                   LAVCCA_NETWORK_WAIT_S="30", LAVCCA_NOTION_API_BASE=f"http://127.0.0.1:{self.notion.port}/v1")
        p = subprocess.run([sys.executable, os.path.join(self.sync, "notion_sync.py"), "--apply-updates"],
                           cwd=self.sync, env=env, capture_output=True, text=True, encoding="utf-8", timeout=300)
        self.assertEqual(p.returncode, 0, p.stdout[-2000:])
        self.assertRegex(p.stdout, r"Network probe: HTTP 200 after [1-9]\d*s wait")
        self.assertIn("(Attachments/K.docx)", self.md(CHOI))
        self.assert_invariants()


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestE2EIncremental(E2EBase):
    """--targets: only the listed already-synced pages are fetched; anything else waits for a full run."""

    def targets(self, ids):
        path = os.path.join(self.tmp, "targets.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ids, f)
        return ["--targets", path]

    def touched(self, nid):
        return [r for r in self.notion.requests if nid in r]

    def test_L_only_target_updated_full_run_recovers_the_rest(self):
        self.add(CHOI, para("c3" * 16, "김멤버 새 문단"), when="2026-10-28T01:00:00.000Z")
        self.add(LEE, para("e3" * 16, "박멤버 새 문단"), when="2026-10-28T01:00:00.000Z")
        lee_before = self.md(LEE)
        report, _ = self.run_sync(args=self.targets([CHOI]))
        self.assertEqual(report["status"], "ALL_APPLIED")
        self.assertEqual(report["applied"], [CHOI])
        self.assertEqual(report["incremental_targets"], [CHOI])
        self.assertIn("김멤버 새 문단", self.md(CHOI))
        self.assertEqual(self.md(LEE), lee_before)
        self.assertEqual(self.touched(LEE), [], "incremental run fetched a non-target page")
        self.assert_invariants()
        report, _ = self.run_sync()                            # reconcile (full crawl) picks up the missed page
        self.assertEqual(report["applied"], [LEE])
        self.assertIn("박멤버 새 문단", self.md(LEE))
        self.assert_noop_rerun()

    def test_L2_unchanged_target_is_noop(self):
        before = {p: self.read_bytes(self.pages[p][0]) for p in self.pages}
        report, _ = self.run_sync(args=self.targets([CHOI.upper()]))
        self.assertEqual(report["status"], "NOTHING_TO_APPLY")
        self.assertEqual({p: self.read_bytes(self.pages[p][0]) for p in self.pages}, before)
        self.assert_invariants()

    def test_M_unknown_target_requires_full_crawl(self):
        new = "f0" * 16
        snap, state = {p: self.read_bytes(self.pages[p][0]) for p in self.pages}, self.state()
        _, out = self.run_sync(expect=9, args=self.targets([CHOI, new]))
        self.assertIn("INCREMENTAL_NEEDS_FULL_CRAWL", out)
        self.assertEqual({p: self.read_bytes(self.pages[p][0]) for p in self.pages}, snap)
        self.assertEqual(self.state(), state)
        self.assert_invariants()

    def test_M2_targets_without_apply_updates_refused(self):
        env = dict(os.environ, NOTION_TOKEN="dummy-token", PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, os.path.join(self.sync, "notion_sync.py")] + self.targets([CHOI]),
                           cwd=self.sync, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertEqual(self.notion.requests, [])

    def test_N_root_unreachable_aborts_without_writes(self):
        self.add(CHOI, para("c3" * 16, "김멤버 새 문단"), when="2026-10-28T01:00:00.000Z")
        self.notion.root_503 = 10 ** 6
        snap, state = self.md(CHOI), self.state()
        _, out = self.run_sync(expect=2, args=self.targets([CHOI]))
        self.assertIn("INCOMPLETE_CRAWL", out)
        self.assertEqual(self.md(CHOI), snap)
        self.assertEqual(self.state(), state)
        self.assert_invariants()
        self.notion.root_503 = 0
        report, _ = self.run_sync(args=self.targets([CHOI]))   # retry once Notion is back
        self.assertEqual(report["applied"], [CHOI])


class TestE2ELegacyLayout(E2EBase):
    """A vault not yet migrated (docs/brand-migration.md): NATAM/ folder, project: NATAM,
    natam_* config keys and NATAM_* environment variables must keep syncing unchanged."""
    ROOT_DIR, PROJECT, ENV_PREFIX = "NATAM", "NATAM", "NATAM_"

    def setUp(self):
        super().setUp()
        path = os.path.join(self.sync, "config.json")
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["natam_root_path"], cfg["natam_root_id"] = cfg.pop("lavcca_root_path"), cfg.pop("lavcca_root_id")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        self.legacy_cfg = cfg

    def test_legacy_vault_syncs_and_keeps_identity(self):
        self.add(CHOI, para("c3" * 16, "김멤버 새 문단"), when="2026-10-28T01:00:00.000Z")
        state_paths = {n: v["obsidian_path"] for n, v in self.state()["items"].items()}
        report, _ = self.run_sync()
        self.assertEqual(report["applied"], [CHOI])
        md = self.md(CHOI)
        self.assertIn("김멤버 새 문단", md)
        self.assertIn("\nproject: NATAM\n", md)                 # frontmatter value preserved, not rebranded
        self.assertIn(f"\nnotion_id: {CHOI}\n", md)
        self.assertEqual({n: v["obsidian_path"] for n, v in self.state()["items"].items()}, state_paths)
        with open(os.path.join(self.sync, "config.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), self.legacy_cfg)     # engine never rewrites config.json
        self.assert_noop_rerun()

    def test_legacy_vault_incremental(self):
        self.add(LEE, para("e3" * 16, "박멤버 새 문단"), when="2026-10-28T01:00:00.000Z")
        path = os.path.join(self.tmp, "targets.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([LEE], f)
        report, _ = self.run_sync(args=["--targets", path])
        self.assertEqual(report["applied"], [LEE])
        self.assert_invariants()


class TestE2EConfiguredRoot(E2EBase):
    def test_O_migration_copy_ignored_under_configured_root_name(self):
        # a stale managed copy in <root>/_migration must not count as a second identity, whatever the root is called
        with open(self.abs(self.pages[CHOI][0]), encoding="utf-8") as f:
            self.write("_migration/old export/김멤버.md", f.read())
        self.add(CHOI, para("c3" * 16, "김멤버 새 문단"), when="2026-10-28T01:00:00.000Z")
        report, _ = self.run_sync()
        self.assertEqual(report["applied"], [CHOI])
        self.assert_noop_rerun()
