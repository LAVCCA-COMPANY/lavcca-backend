# -*- coding: utf-8 -*-
"""ci/docs_publish.py against an in-memory fake Notion: create, no-op rerun, edit, rename, delete/restore,
incremental new page, database rows, attachments, link integrity."""
import io, os, re, sys, json, shutil, tempfile, unittest

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CODE_DIR)
from ci import docs_publish as dp  # noqa: E402

ROOT, A, B, C, DB, DS, ROW = ("10" * 16, "a1" * 16, "b2" * 16, "c3" * 16, "d4" * 16, "e5" * 16, "f6" * 16)
CFG = {"lavcca_root_id": ROOT, "api_page_size": 100, "max_pagination_pages": 10}


def para(text):
    return {"type": "paragraph", "id": "99" * 16, "has_children": False,
            "paragraph": {"rich_text": [{"type": "text", "plain_text": text}]}}


def child(nid, title):
    return {"type": "child_page", "id": nid, "has_children": True, "child_page": {"title": title}}


class FakeNotion:
    def __init__(self):
        self.pages, self.blocks, self.calls = {}, {}, []
        self.add(ROOT, "LAVCCA", {"type": "workspace"}, [child(A, "회의록"), child(B, "기획/설계")])
        self.add(A, "회의록", {"type": "page_id", "page_id": ROOT}, [para("첫 회의")])
        self.add(B, "기획/설계", {"type": "page_id", "page_id": ROOT}, [para("설계 문서")])

    def add(self, nid, title, parent, blocks, t="2026-10-28T01:00:00.000Z", **kw):
        self.pages[nid] = {"object": "page", "id": nid, "url": f"https://www.notion.so/{nid}", "parent": parent,
                           "last_edited_time": t, "in_trash": False, "archived": False,
                           "properties": {"title": {"type": "title", "title": [{"plain_text": title}]}}, **kw}
        self.blocks[nid] = blocks

    def edit(self, nid, blocks=None, title=None, t="2026-10-28T02:00:00.000Z"):
        p = self.pages[nid]
        p["last_edited_time"] = t
        if title is not None:
            p["properties"]["title"]["title"] = [{"plain_text": title}]
        if blocks is not None:
            self.blocks[nid] = blocks

    def __call__(self, method, path, body=None):
        self.calls.append(path)
        m = re.match(r"/(pages|blocks|databases|data_sources)/([0-9a-f]{32})(/children|/query)?", path)
        kind, nid = m.group(1), m.group(2)
        if kind == "pages":
            return (200, self.pages[nid]) if nid in self.pages else (404, {})
        if kind == "blocks":
            return (200, {"results": self.blocks.get(nid, []), "has_more": False})
        if kind == "databases":  # one data source per database, queried by the database's own id
            return 200, {"id": nid, "data_sources": [{"id": nid}]}
        return 200, {"results": [p for p in self.pages.values() if not p.get("in_trash")
                                 and (p["parent"] or {}).get("database_id") == nid], "has_more": False}


class TestDocsPublish(unittest.TestCase):
    def setUp(self):
        self.docs = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.docs, True)
        self.n = FakeNotion()
        self.root = os.path.join(self.docs, "obsidian-vault", "Notion")

    def pub(self, seeds=None, download=lambda url: b"PNG"):
        code, rep = dp.publish(self.n, CFG, self.docs, seeds=seeds, download=download)
        self.assertEqual(rep.get("audit"), [], rep)
        return code, rep

    def read(self, rel):
        with open(os.path.join(self.root, *rel.split("/")), encoding="utf-8") as f:
            return f.read()

    def files(self):
        return sorted(os.path.relpath(os.path.join(d, f), self.root).replace(os.sep, "/")
                      for d, _, fs in os.walk(self.root) for f in fs)

    def state(self):
        with open(os.path.join(self.docs, ".notion-sync", "state.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_first_run_creates_tree_with_safe_korean_names_and_links(self):
        code, rep = self.pub()
        self.assertEqual(code, 0)
        self.assertEqual(self.files(), ["LAVCCA.md", "LAVCCA/기획 설계.md", "LAVCCA/회의록.md"])
        self.assertEqual(len(rep["created"]), 3)
        root_md = self.read("LAVCCA.md")
        self.assertIn("[[Notion/LAVCCA/회의록|회의록]]", root_md)
        self.assertIn(f"notion_id: {A}", self.read("LAVCCA/회의록.md"))
        self.assertIn("첫 회의", self.read("LAVCCA/회의록.md"))

    def test_rerun_without_edits_writes_nothing(self):
        self.pub()
        before = {f: self.read(f) for f in self.files()}
        _, rep = self.pub()
        self.assertEqual((rep["created"], rep["updated"]), ([], []))
        self.assertEqual({f: self.read(f) for f in self.files()}, before)

    def test_edit_updates_and_rename_keeps_path(self):
        self.pub()
        self.n.edit(A, [para("수정된 회의")], title="회의록 v2")
        _, rep = self.pub(seeds=[A])
        self.assertEqual(rep["updated"], [A])
        self.assertEqual(rep["renamed"], [A])
        md = self.read("LAVCCA/회의록.md")  # same path: links elsewhere stay valid
        self.assertIn("수정된 회의", md)
        self.assertIn('aliases: ["회의록 v2"]', md)

    def test_second_edit_within_the_same_minute_is_not_missed(self):
        # last_edited_time has minute precision: edit -> sync -> edit again in the same minute keeps the
        # same timestamp. A webhook makes that common, so a seeded page is always re-read.
        self.pub()
        self.n.edit(A, [para("첫 수정")], t="2026-10-28T02:00:00.000Z")
        self.pub(seeds=[A])
        self.n.edit(A, [para("같은 분 두 번째 수정")], t="2026-10-28T02:00:00.000Z")
        _, rep = self.pub(seeds=[A])
        self.assertEqual(rep["updated"], [A])
        self.assertIn("같은 분 두 번째 수정", self.read("LAVCCA/회의록.md"))
        _, rep = self.pub(seeds=[A])  # re-read but identical: nothing written
        self.assertEqual(rep["updated"], [])

    def test_incremental_new_child_page_is_created_under_parent(self):
        self.pub()
        self.n.add(C, "새 문서", {"type": "page_id", "page_id": A}, [para("new")])
        self.n.edit(A, [para("첫 회의"), child(C, "새 문서")])
        _, rep = self.pub(seeds=[C, A])
        self.assertIn("LAVCCA/회의록/새 문서.md", self.files())
        self.assertEqual(sorted(rep["created"]), [C])

    def test_unknown_page_outside_tree_is_ignored(self):
        self.pub()
        self.n.add(C, "남의 페이지", {"type": "workspace"}, [para("x")])
        _, rep = self.pub(seeds=[C])
        self.assertEqual(rep["created"], [])

    def test_trashed_page_moves_to_deleted_and_comes_back(self):
        self.pub()
        self.n.pages[A]["in_trash"] = True
        self.n.edit(ROOT, [child(B, "기획/설계")])
        _, rep = self.pub()
        self.assertEqual(rep["deleted"], [A])
        self.assertIn("_deleted/LAVCCA/회의록.md", self.files())
        self.assertNotIn("LAVCCA/회의록.md", self.files())
        self.n.pages[A]["in_trash"] = False
        self.n.edit(ROOT, [child(A, "회의록"), child(B, "기획/설계")], t="2026-10-28T03:00:00.000Z")
        _, rep = self.pub()
        self.assertEqual(rep["restored"], [A])
        self.assertIn("LAVCCA/회의록.md", self.files())
        self.assertNotIn("_deleted/LAVCCA/회의록.md", self.files())

    def test_unreachable_page_still_live_is_not_deleted(self):
        self.pub()
        self.n.edit(ROOT, [child(B, "기획/설계")])  # A still a descendant per its parent pointer
        _, rep = self.pub()
        self.assertEqual(rep["deleted"], [])

    def test_database_rows_land_in_database_folder(self):
        self.n.edit(B, [para("설계"), {"type": "child_database", "id": DB, "has_children": False,
                                       "child_database": {"title": "작업 목록"}}])
        self.n.add(ROW, "작업 1", {"type": "data_source_id", "data_source_id": DS, "database_id": DB}, [para("r")])
        self.pub()
        self.assertIn("LAVCCA/기획 설계/작업 목록/작업 1.md", self.files())

    def test_attachment_downloaded_once_and_linked_relatively(self):
        img = {"type": "image", "id": C, "has_children": False, "last_edited_time": "2026-10-28T01:00:00.000Z",
               "image": {"type": "file", "caption": [], "file": {"url": "https://s3/x/pic.PNG?sig=1"}}}
        self.n.edit(A, [img])
        got = []
        self.pub(download=lambda url: got.append(url) or b"PNG")
        self.assertIn(f"_attachments/{C}.png", self.files())
        self.assertIn(f"![](../_attachments/{C}.png)", self.read("LAVCCA/회의록.md"))
        self.n.edit(A, [img, para("more")], t="2026-10-28T05:00:00.000Z")
        self.pub(download=lambda url: got.append(url) or b"PNG")
        self.assertEqual(len(got), 1)

    def test_failed_download_publishes_placeholder_and_retries(self):
        img = {"type": "image", "id": C, "has_children": False, "last_edited_time": "t",
               "image": {"type": "file", "caption": [], "file": {"url": "https://s3/pic.png"}}}
        self.n.edit(A, [img])

        def boom(url):
            raise OSError("down")
        _, rep = self.pub(download=boom)
        self.assertIn(A, rep["render_warnings"])
        self.assertNotIn("notion_last_edited_time", self.state()["items"][A])

    def test_crawl_error_writes_nothing(self):
        def broken(method, path, body=None):
            return (500, {}) if path.startswith(f"/blocks/{A}") else self.n(method, path, body)
        code, rep = dp.publish(broken, CFG, self.docs)
        self.assertEqual(code, dp.EXIT_CRAWL)
        self.assertFalse(os.path.exists(self.root))

    def test_sibling_name_collision_gets_id_suffix(self):
        self.n.edit(ROOT, [child(A, "같은 이름"), child(B, "같은 이름")])
        self.n.edit(A, title="같은 이름")
        self.n.edit(B, title="같은 이름")
        self.pub()
        self.assertEqual(self.files(), ["LAVCCA.md", "LAVCCA/같은 이름 (b2b2b2b2).md", "LAVCCA/같은 이름.md"])

    def test_same_title_siblings_with_shared_id_prefix_get_distinct_paths(self):
        # regression (Actions run 40712670458): ids created together share the first 8 hex, so the old
        # "(nid[:8])" suffix collided from the third sibling on -> overwritten file, audit exit 10
        ids = ["2d2de6a22dbe80" + s for s in ("4c9ba3efc53daff988", "cab668e0a0b4698e63", "d4b5b2e73d8c0d32c4")]
        self.n.edit(ROOT, [child(i, "") for i in ids])
        for i in ids:
            self.n.add(i, "", {"type": "page_id", "page_id": ROOT}, [para("본문 " + i)])
        code, rep = self.pub()
        self.assertEqual((code, len(rep["created"]), rep["updated"]), (0, 4, []))  # root + 3
        paths = {self.state()["items"][i]["obsidian_path"] for i in ids}
        self.assertEqual(len(paths), 3)
        for i in ids:
            self.assertIn("본문 " + i, self.read(self.state()["items"][i]["obsidian_path"]))

    def test_audit_flags_two_pages_on_one_path(self):
        self.pub()
        st = self.state()
        st["items"][B]["obsidian_path"] = st["items"][A]["obsidian_path"]
        st["items"][B].pop("notion_last_edited_time")
        with open(os.path.join(self.docs, ".notion-sync", "state.json"), "w", encoding="utf-8") as f:
            json.dump(st, f)
        code, rep = dp.publish(self.n, CFG, self.docs)
        self.assertEqual(code, dp.EXIT_AUDIT)
        self.assertIn("path shared by two pages", [p["problem"] for p in rep["audit"]])

    def test_carriage_returns_are_normalised(self):
        self.n.edit(A, [para("줄1\r\n줄2\r줄3")])
        self.pub()
        self.assertIn("줄1\n줄2\n줄3", self.read("LAVCCA/회의록.md"))

    def test_engine_exclusion_lists_do_not_drop_pages(self):
        # PM decision: everything under the LAVCCA root is published; the OneDrive engine's
        # excluded_ids/review_ids (still present in older configs) must not hide pages from docs
        code, rep = dp.publish(self.n, {**CFG, "excluded_ids": [A], "review_ids": [B]}, self.docs)
        self.assertEqual(code, 0)
        self.assertEqual(self.files(), ["LAVCCA.md", "LAVCCA/기획 설계.md", "LAVCCA/회의록.md"])

    def test_credentials_are_reported_by_kind_but_page_is_still_published(self):
        self.n.edit(A, [para("계정 비밀번호: hunter2 / key AKIAABCDEFGHIJKLMNOP")])
        self.n.edit(B, [para("계정 정보는 비밀번호 관리자에 있음")])  # mention without a value: not a finding
        code, rep = self.pub()
        self.assertEqual(code, 0)
        self.assertEqual(rep["credential_findings"], {A: ["aws_access_key", "password_value"]})
        self.assertIn("LAVCCA/회의록.md", self.files())
        self.assertEqual(self.state()["items"][A]["status"], "active")
        self.assertNotIn("hunter2", json.dumps(rep["credential_findings"]))

    def test_full_rerun_without_edits_leaves_state_byte_identical(self):
        # regression (docs run 40721699709): last_successful_sync was restamped on every page each run
        self.pub()
        path = os.path.join(self.docs, ".notion-sync", "state.json")
        before = open(path, "rb").read()
        dp.publish(self.n, CFG, self.docs, now=dp.datetime(2030, 1, 1, tzinfo=dp.timezone.utc))
        self.assertEqual(open(path, "rb").read(), before)

    def test_cursor_advances_only_when_state_changed(self):
        self.pub()
        path = os.path.join(self.docs, ".notion-sync", "state.json")
        t1, t2 = dp.datetime(2026, 10, 29, 1, tzinfo=dp.timezone.utc), dp.datetime(2026, 10, 29, 2, tzinfo=dp.timezone.utc)
        self.assertTrue(dp.advance_cursor(path, {}, t1))  # first cursor
        before = self.state()
        bytes_before = open(path, "rb").read()
        self.assertFalse(dp.advance_cursor(path, before, t2))  # nothing changed: file untouched
        self.assertEqual(open(path, "rb").read(), bytes_before)
        self.n.edit(A, [para("새 내용")])
        dp.publish(self.n, CFG, self.docs, seeds=[A])
        self.assertTrue(dp.advance_cursor(path, before, t2))
        self.assertEqual(self.state()["cursor"], "2026-10-29T02:00:00Z")

    def test_link_to_notion_hosted_file_is_downloaded_not_linked(self):
        # regression: an embed of an uploaded .aac kept its signed S3 URL -> changed every run, expired in 1 h
        def embed(sig):
            return {"type": "embed", "id": C, "has_children": False, "last_edited_time": "2026-10-28T01:00:00.000Z",
                    "embed": {"caption": [], "url": "https://prod-files-secure.s3.us-west-2.amazonaws.com/ws/f/"
                                                     f"%EC%9D%B8%ED%84%B0%EB%B7%B0.aac?X-Amz-Signature={sig}"}}
        self.n.edit(A, [embed("1")])
        got = []
        self.pub(download=lambda url: got.append(url) or b"AAC")
        md = self.read("LAVCCA/회의록.md")
        self.assertIn(f"[인터뷰.aac](../_attachments/{C}.aac)", md)
        self.assertNotIn("X-Amz", md)
        self.n.edit(A, [embed("2")], t="2026-10-28T09:00:00.000Z")
        _, rep = self.pub(download=lambda url: got.append(url) or b"AAC")
        self.assertEqual((len(got), rep["updated"]), (1, []))

    def test_recording_over_the_limit_stays_in_notion(self):
        # run 40813421566: a 61,491,649-byte interview recording -> kept in Notion, linked by block
        self.assertLess(dp.MAX_ATTACHMENT, 61_491_649)
        self.assertEqual(dp.MAX_ATTACHMENT, 50 * 1024 * 1024)  # existing attachments keep the same rule

    def test_http_download_refuses_large_files(self):
        from unittest import mock

        class Resp(io.BytesIO):
            def __init__(self, data, length=None):
                super().__init__(data)
                self.headers = {} if length is None else {"Content-Length": str(length)}
                self.reads = 0

            def read(self, *a):
                self.reads += 1
                return super().read(*a)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.close()
        big = Resp(b"", length=61_491_649)  # header says too large: body never read
        with mock.patch.object(dp.urllib.request, "urlopen", lambda url, timeout: big):
            with self.assertRaises(dp.TooLarge):
                dp.http_download("https://x")
        self.assertEqual(big.reads, 0)
        with mock.patch.object(dp, "MAX_ATTACHMENT", 4),                 mock.patch.object(dp.urllib.request, "urlopen", lambda url, timeout: Resp(b"12345")):
            with self.assertRaises(dp.TooLarge):  # no Content-Length: the size cap still holds
                dp.http_download("https://x")
        with mock.patch.object(dp, "MAX_ATTACHMENT", 5),                 mock.patch.object(dp.urllib.request, "urlopen", lambda url, timeout: Resp(b"12345", 5)):
            self.assertEqual(dp.http_download("https://x"), b"12345")

    def test_large_file_links_to_notion_block_and_survives_url_and_title_changes(self):
        def rec(sig):
            return {"type": "audio", "id": C, "has_children": False, "last_edited_time": "t",
                    "audio": {"type": "file", "caption": [], "name": "rec.aac",
                              "file": {"url": f"https://prod-files-secure.s3.us-west-2.amazonaws.com/w/f/rec.aac?X-Amz-Signature={sig}"}}}
        self.n.edit(A, [rec("1")])

        def too_big(url):
            raise dp.TooLarge()
        _, rep = self.pub(download=too_big)
        md = self.read("LAVCCA/회의록.md")
        self.assertIn(f"[rec.aac](https://www.notion.so/{A}#{C})", md)
        self.assertNotIn("X-Amz", md)
        self.assertNotIn("notion-sync:", md)  # no placeholder
        self.assertEqual(rep["render_warnings"], {})
        self.assertNotIn(C, self.state()["attachments"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "_attachments")))
        self.n.edit(A, [rec("2")], t="2026-10-28T09:00:00.000Z")  # Notion re-signed the URL
        _, rep = self.pub(download=too_big)
        self.assertEqual(rep["updated"], [])
        self.n.edit(A, title="회의록 v2", t="2026-10-28T10:00:00.000Z")  # rename: page URL slug changes too
        self.n.pages[A]["url"] = "https://www.notion.so/v2-" + A
        self.pub(download=too_big)
        self.assertIn(f"[rec.aac](https://www.notion.so/{A}#{C})", self.read("LAVCCA/회의록.md"))

    def test_small_attachments_still_download(self):
        img = {"type": "image", "id": C, "has_children": False, "last_edited_time": "t",
               "image": {"type": "file", "caption": [], "file": {"url": "https://s3/pic.png"}}}
        self.n.edit(A, [img])
        self.pub(download=lambda url: b"x" * 1024)
        self.assertIn(f"_attachments/{C}.png", self.files())

    def test_ordinary_embed_stays_a_link(self):
        self.n.edit(A, [{"type": "embed", "id": C, "has_children": False,
                         "embed": {"caption": [], "url": "https://www.youtube.com/watch?v=x"}}])
        self.pub(download=lambda url: self.fail("must not download"))
        self.assertIn("(https://www.youtube.com/watch?v=x)", self.read("LAVCCA/회의록.md"))

    # ---- inline databases: rows listed as links in the parent page ----
    def db_setup(self, rows, db=DB, parent=B, title="근무/훈련"):
        """rows: [(nid, title, date or None)] in the database `db` placed on page `parent`."""
        self.n.edit(parent, [para("본문"), {"type": "child_database", "id": db, "has_children": False,
                                            "child_database": {"title": title}}])
        for nid, t, date in rows:
            self.n.add(nid, t, {"type": "data_source_id", "data_source_id": db, "database_id": db}, [para("행")])
            if date:
                self.n.pages[nid]["properties"]["날짜"] = {"type": "date", "date": {"start": date}}

    def row_lines(self, rel="LAVCCA/기획 설계.md"):
        return [l for l in self.read(rel).split("\n") if l.startswith("- [[")]

    def links_resolve(self):
        for rel in self.files():
            if not rel.endswith(".md") or rel.startswith("_"):
                continue
            for t in re.findall(r"\[\[Notion/([^\]|]+)\|", self.read(rel)):
                self.assertTrue(os.path.isfile(os.path.join(self.root, *(t + ".md").split("/"))), t)

    def test_inline_database_rows_are_listed_in_the_parent_page(self):
        R1, R2, R3 = "e1" * 16, "e2" * 16, "e3" * 16
        self.db_setup([(R1, "당직", "2026-01-12"), (R2, "혹한기", "2026-01-05"), (R3, "메모", None)])
        code, _ = self.pub()
        md = self.read("LAVCCA/기획 설계.md")
        self.assertIn(f"근무/훈련 <!-- notion-sync: child_database {DB}; its rows are synced as separate pages -->", md)
        self.assertEqual(self.row_lines(), [  # by date, dateless last; date only where Notion has one
            "- [[Notion/LAVCCA/기획 설계/근무 훈련/혹한기|혹한기]] · 2026-01-05",
            "- [[Notion/LAVCCA/기획 설계/근무 훈련/당직|당직]] · 2026-01-12",
            "- [[Notion/LAVCCA/기획 설계/근무 훈련/메모|메모]]"])
        self.links_resolve()

    def test_empty_database_keeps_only_the_placeholder(self):
        self.db_setup([])
        self.pub()
        self.assertIn(f"<!-- notion-sync: child_database {DB};", self.read("LAVCCA/기획 설계.md"))
        self.assertEqual(self.row_lines(), [])

    def test_duplicate_row_titles_link_to_their_own_files(self):
        R1, R2 = "2f2de6a22dbe80aa1111111111111111", "2f2de6a22dbe80aa2222222222222222"
        self.db_setup([(R1, "당직", "2026-02-01"), (R2, "당직", "2026-02-08")])
        self.pub()
        lines = self.row_lines()
        self.assertEqual(len(lines), 2)
        targets = [re.search(r"\[\[([^|]+)\|", l).group(1) for l in lines]
        self.assertEqual(len(set(targets)), 2)  # never two links to one file
        self.links_resolve()

    def test_korean_special_characters_and_nested_placeholder(self):
        R1 = "e4" * 16
        self.n.edit(B, [{"type": "toggle", "id": "88" * 16, "has_children": True,
                         "toggle": {"rich_text": [{"type": "text", "plain_text": "토글"}]},
                         "_children": []}])
        self.n.blocks["88" * 16] = [{"type": "child_database", "id": DB, "has_children": False,
                                     "child_database": {"title": "근무/훈련 (A) [x]"}}]
        self.n.add(R1, "경계지원파견 (1차)", {"type": "data_source_id", "data_source_id": DB, "database_id": DB}, [para("행")])
        self.pub()
        md = self.read("LAVCCA/기획 설계.md")
        placeholder = next(l for l in md.split("\n") if "child_database" in l)
        indent = placeholder[:len(placeholder) - len(placeholder.lstrip(" \t>"))]
        self.assertIn(f"\n{indent}- [[Notion/LAVCCA/기획 설계/근무 훈련 (A) x/경계지원파견 (1차)|경계지원파견 (1차)]]", md)
        self.links_resolve()

    def test_row_title_change_updates_the_parent_list_incrementally(self):
        R1 = "e5" * 16
        self.db_setup([(R1, "당직", "2026-01-12")])
        self.pub()
        self.n.edit(R1, title="당직 (변경)", t="2026-10-29T01:00:00.000Z")
        _, rep = self.pub(seeds=[R1])  # only the row is seeded, as a webhook would
        self.assertIn(B, rep["updated"])
        self.assertEqual(self.row_lines(), ["- [[Notion/LAVCCA/기획 설계/근무 훈련/당직|당직 (변경)]] · 2026-01-12"])

    def test_row_added_and_removed_incrementally(self):
        R1, R2 = "e6" * 16, "e7" * 16
        self.db_setup([(R1, "당직", "2026-01-12")])
        self.pub()
        self.n.add(R2, "혹한기", {"type": "data_source_id", "data_source_id": DB, "database_id": DB}, [para("행")])
        self.pub(seeds=[R2])
        self.assertEqual(len(self.row_lines()), 2)
        self.n.pages[R1]["in_trash"] = True
        _, rep = self.pub(seeds=[R1])
        self.assertEqual(rep["deleted"], [R1])
        self.assertEqual(self.row_lines(), ["- [[Notion/LAVCCA/기획 설계/근무 훈련/혹한기|혹한기]]"])
        self.links_resolve()

    def test_row_without_a_page_is_never_linked(self):
        R1, GONE = "e8" * 16, "e9" * 16
        self.db_setup([(R1, "당직", None), (GONE, "사라진 행", None)])
        real_call = self.n.__call__

        def no_gone(method, path, body=None):  # the query lists it but the page is not readable
            return (404, {}) if path == f"/pages/{GONE}" else real_call(method, path, body)
        dp.publish(no_gone, CFG, self.docs, download=lambda u: b"")
        self.assertEqual(self.row_lines(), ["- [[Notion/LAVCCA/기획 설계/근무 훈련/당직|당직]]"])
        self.links_resolve()

    def test_row_lists_are_stable_on_rerun(self):
        self.db_setup([("ea" * 16, "당직", "2026-01-12"), ("eb" * 16, "혹한기", "2026-01-05")])
        self.pub()
        before = {f: self.read(f) for f in self.files()}
        _, rep = self.pub()
        self.assertEqual((rep["created"], rep["updated"]), ([], []))
        self.assertEqual({f: self.read(f) for f in self.files()}, before)
        self.assertEqual(self.state()["databases"][DB]["rows"], ["ea" * 16, "eb" * 16])

    def test_plan_seeds(self):
        self.assertIsNone(dp.plan_seeds({}, CFG, None, lambda s: []))
        st = {"cursor": "2026-10-28T01:00:00Z"}
        self.assertIsNone(dp.plan_seeds(st, CFG, None, lambda s: None))
        ev = {"inputs": {"hint": json.dumps({"ids": [B], "types": ["page.content_updated"]})}}
        self.assertEqual(dp.plan_seeds(st, CFG, ev, lambda s: [{"object": "page", "id": A}]), [A, B])

    def test_safe_name(self):
        self.assertEqual(dp.safe_name('a/b:c?"d".'), "a b c d")
        self.assertEqual(dp.safe_name(""), dp.safe_name(None))


if __name__ == "__main__":
    unittest.main()
