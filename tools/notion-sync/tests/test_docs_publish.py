# -*- coding: utf-8 -*-
"""ci/docs_publish.py against an in-memory fake Notion: create, no-op rerun, edit, rename, delete/restore,
incremental new page, database rows, attachments, link integrity."""
import os, re, sys, json, shutil, tempfile, unittest

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
        if kind == "databases":
            return 200, {"id": nid, "data_sources": [{"id": DS}]}
        return 200, {"results": [p for p in self.pages.values()
                                 if (p["parent"] or {}).get("database_id") == DB], "has_more": False}


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
