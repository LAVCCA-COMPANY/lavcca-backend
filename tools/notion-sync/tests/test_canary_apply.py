# -*- coding: utf-8 -*-
"""Offline tests for canary_apply.py. Uses a temporary fixture vault and a fake
Notion API -- never touches the real LAVCCA vault files and never calls the network."""
import os, sys, json, glob, hashlib, shutil, tempfile, subprocess, unittest
from datetime import datetime, timezone
from unittest import mock

SYNC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SYNC_DIR)
import canary_apply as ca  # noqa: E402

ROOT = "a" * 32
DB = "d" * 32
DS = "e" * 32
T = "1" * 32          # active target
C = "2" * 32          # active child page (wikilink target)
A = "3" * 32          # archive
O = "4" * 32          # managed: obsidian
M = "5" * 32          # malformed frontmatter
P = "6" * 32          # managed:notion inside 06_KNOWLEDGE (protected)
NOW = datetime(2026, 10, 2, 3, 0, 0, tzinfo=timezone.utc)
OLD_MTIME = datetime(2025, 12, 31, tzinfo=timezone.utc).timestamp()


def rt(text, href=None, **ann):
    a = {k: False for k in ("bold", "italic", "strikethrough", "underline", "code")}
    a.update(ann)
    return {"type": "text", "plain_text": text, "href": href, "annotations": a, "text": {"content": text}}


def blk(bid, t, data=None, children=None):
    return {"object": "block", "id": bid, "type": t, t: data or {}, "has_children": bool(children),
            "_fixture_children": children or []}


def fm(nid, managed="notion", extra=""):
    return (f'---\nproject: LAVCCA\nowner: shared\nsource: notion\nmanaged: {managed}\nnotion_id: {nid}\n'
            f'notion_path: "LAVCCA/일정/대상 {nid}.md"\n{extra}---\n')


TARGET_TEXT = fm(T, extra="aliases: keep-me\n") + "\n# 대상\n\n옛 본문\n\n[회의록 첨부.docx](회의록_첨부.docx)\n"


def target_blocks():
    n = iter(range(100, 999))
    bid = lambda: f"{next(n):032d}"
    return [
        blk(bid(), "paragraph", {"rich_text": [rt("plain "), rt("bold", bold=True), rt(" "), rt("ital", italic=True),
                                              rt(" "), rt("gone", strikethrough=True), rt(" "), rt("x=1", code=True),
                                              rt(" "), rt("link", href="https://example.com")]}),
        blk(bid(), "heading_1", {"rich_text": [rt("H1")]}),
        blk(bid(), "heading_2", {"rich_text": [rt("H2")]}),
        blk(bid(), "heading_3", {"rich_text": [rt("H3")]}),
        blk(bid(), "bulleted_list_item", {"rich_text": [rt("b1")]},
            children=[blk(bid(), "bulleted_list_item", {"rich_text": [rt("nested")]})]),
        blk(bid(), "bulleted_list_item", {"rich_text": [rt("b2")]}),
        blk(bid(), "numbered_list_item", {"rich_text": [rt("n1")]}),
        blk(bid(), "to_do", {"rich_text": [rt("done")], "checked": True}),
        blk(bid(), "to_do", {"rich_text": [rt("todo")], "checked": False}),
        blk(bid(), "toggle", {"rich_text": [rt("tog")]}, children=[blk(bid(), "paragraph", {"rich_text": [rt("inside")]})]),
        blk(bid(), "quote", {"rich_text": [rt("q")]}),
        blk(bid(), "callout", {"rich_text": [rt("note")], "icon": {"type": "emoji", "emoji": "💡"}}),
        blk(bid(), "code", {"rich_text": [rt("print(1)")], "language": "python", "caption": []}),
        blk(bid(), "divider"),
        blk(bid(), "bookmark", {"url": "https://bm.example", "caption": []}),
        blk(bid(), "image", {"type": "external", "external": {"url": "https://img.example/a.png"}, "caption": [rt("cap")]}),
        blk(bid(), "file", {"type": "file", "file": {"url": "https://s3.example/x/%ED%9A%8C.docx?sig=1"},
                            "name": "회의록 첨부.docx", "caption": []}),
        blk(bid(), "pdf", {"type": "external", "external": {"url": "https://pdf.example/doc.pdf"}, "caption": []}),
        blk(C, "child_page", {"title": "자식 페이지"}),
        blk(bid(), "table", {"table_width": 2, "has_column_header": True}, children=[
            blk(bid(), "table_row", {"cells": [[rt("h|1")], [rt("h2")]]}),
            blk(bid(), "table_row", {"cells": [[rt("c1")], [rt("c2", bold=True)]]}),
        ]),
    ]


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.overrides = {}
        self.pages = {
            T: {"object": "page", "id": T, "archived": False, "in_trash": False,
                "last_edited_time": "2026-09-30T10:00:00.000Z",
                "parent": {"type": "data_source_id", "data_source_id": DS, "database_id": DB},
                "properties": {"이름": {"type": "title", "title": [{"plain_text": "대상"}]}}},
            C: {"object": "page", "id": C, "archived": False, "in_trash": False,
                "parent": {"type": "page_id", "page_id": ROOT}, "properties": {}},
            ROOT: {"object": "page", "id": ROOT, "parent": {"type": "workspace", "workspace": True}},
        }
        self.dbs = {DB: {"object": "database", "id": DB, "parent": {"type": "page_id", "page_id": ROOT}}}
        self.children = {T: target_blocks()}

    def set_blocks(self, page_id, blocks):
        self.children[page_id] = blocks

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        assert method == "GET", f"canary must only GET, got {method} {path}"
        if path in self.overrides:
            return self.overrides[path]
        if path.startswith("/pages/"):
            pid = path.split("/")[2]
            return (200, self.pages[pid]) if pid in self.pages else (404, {"code": "object_not_found"})
        if path.startswith("/databases/"):
            did = path.split("/")[2]
            return (200, self.dbs[did]) if did in self.dbs else (404, {})
        if path.startswith("/blocks/") and "/children" in path:
            bid = path.split("/")[2]
            blocks = self.children.get(bid)
            if blocks is None:  # nested children
                blocks = self._find_nested(bid)
            out = []
            for b in blocks:
                b2 = {k: v for k, v in b.items() if k != "_fixture_children"}
                out.append(b2)
            return 200, {"results": out, "has_more": False, "next_cursor": None}
        return 404, {}

    def _find_nested(self, bid):
        stack = [b for bl in self.children.values() for b in bl]
        while stack:
            b = stack.pop()
            if b["id"] == bid:
                return b["_fixture_children"]
            stack.extend(b["_fixture_children"])
        return []


class CanaryTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="canary_test_")
        self.vault = os.path.join(self.tmp, "vault")
        self.lavcca = os.path.join(self.vault, "LAVCCA")
        self.sync = os.path.join(self.lavcca, "_sync")
        files = {
            "01_SHARED/Meetings/대상.md": TARGET_TEXT,
            "01_SHARED/Meetings/회의록_첨부.docx": "binary-ish",
            "02_X/Imported-Notion/자식 페이지.md": fm(C) + "\n# 자식 페이지\n",
            "99_ARCHIVE/Notion-Raw/Legacy-NATAM/old.md": fm(A) + "\n# old\n",
            "01_SHARED/obs.md": fm(O, managed="obsidian") + "\n# obs\n",
            "01_SHARED/bad.md": "---\nproject: LAVCCA\nowner: shared\nsource: notion\nmanaged: notion\n"
                                f"notion_id: {M}\n---\n\n# bad\n",
            "06_KNOWLEDGE/k.md": fm(P) + "\n# k\n",
        }
        for rel, content in files.items():
            path = os.path.join(self.lavcca, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            os.utime(path, (OLD_MTIME, OLD_MTIME))
        os.makedirs(self.sync)
        self.state_path = os.path.join(self.sync, "state.json")
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump({"schema_version": 1, "items": {}}, f)
        self.cfg = {
            "vault_root": self.vault, "lavcca_root_path": self.lavcca, "lavcca_root_id": ROOT,
            "protected_relative_paths": ["06_KNOWLEDGE", "99_ARCHIVE", "_migration", "_sync"],
            "api_page_size": 100, "max_pagination_pages": 50,
            "bootstrap_migration_completed_at": "2026-01-01T00:00:00+00:00",
        }
        self.api = FakeAPI()
        self.target = os.path.join(self.lavcca, "01_SHARED", "Meetings", "대상.md")
        self.snapshot = self._hashes()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _hashes(self):
        out = {}
        for root, _, files in os.walk(self.lavcca):
            if os.path.join("LAVCCA", "_sync") in root:
                continue
            for fn in files:
                p = os.path.join(root, fn)
                with open(p, "rb") as f:
                    out[os.path.relpath(p, self.lavcca)] = hashlib.sha256(f.read()).hexdigest()
        return out

    def _read(self, p):
        with open(p, "rb") as f:
            return f.read()

    def run_canary(self, nid, apply=True, now=NOW):
        return ca.run_canary(nid, self.cfg, self.api, self.sync, apply=apply, now=now)

    def assert_nothing_changed(self):
        self.assertEqual(self._hashes(), self.snapshot)
        with open(self.state_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["items"], {})
        self.assertEqual(glob.glob(os.path.join(self.lavcca, "**", ".notion_sync_tmp_*"), recursive=True), [])


class TestCliGating(unittest.TestCase):
    def test_modes(self):
        self.assertEqual(ca.resolve_cli_mode(True, None)[0], "refuse")
        self.assertEqual(ca.resolve_cli_mode(True, [])[0], "refuse")
        self.assertEqual(ca.resolve_cli_mode(False, None)[0], "full_dry_run")
        self.assertEqual(ca.resolve_cli_mode(False, [T])[:2], ("canary_dry_run", T))
        self.assertEqual(ca.resolve_cli_mode(True, [T])[:2], ("canary_apply", T))
        hyphenated = f"{T[:8]}-{T[8:12]}-{T[12:16]}-{T[16:20]}-{T[20:]}"
        self.assertEqual(ca.resolve_cli_mode(True, [hyphenated])[:2], ("canary_apply", T))
        self.assertEqual(ca.resolve_cli_mode(True, [T, C])[0], "refuse")          # multiple ids
        self.assertEqual(ca.resolve_cli_mode(True, [f"{T},{C}"])[0], "refuse")   # comma list
        self.assertEqual(ca.resolve_cli_mode(True, ["all"])[0], "refuse")

    def test_real_cli_apply_without_id_refused_before_token(self):
        # Refusal happens before .env is read or any API call is made.
        for argv in (["--apply"], ["--apply", "--notion-id", T, "--notion-id", C], ["--apply", "--notion-id", "xyz"]):
            r = subprocess.run([sys.executable, os.path.join(SYNC_DIR, "notion_sync.py")] + argv,
                               capture_output=True, text=True, encoding="utf-8", timeout=60,
                               env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(r.returncode, 3, (argv, r.stdout, r.stderr))
            self.assertIn("REFUSED", r.stdout)
            self.assertNotIn("Crawling", r.stdout)


class TestRefusals(CanaryTestBase):
    def assert_refused(self, res, code):
        self.assertEqual(res["status"], "REFUSED", res)
        self.assertEqual(res["refusal_code"], code, res)
        self.assert_nothing_changed()
        self.assertFalse(os.path.exists(os.path.join(self.sync, "backups")))

    def test_dry_run_single_id_writes_nothing(self):
        res = self.run_canary(T, apply=False)
        self.assertEqual(res["status"], "DRY_RUN_OK", res)
        self.assert_nothing_changed()
        self.assertFalse(os.path.exists(os.path.join(self.sync, "backups")))
        self.assertTrue(os.path.exists(res["preview_path"]))

    def test_nonexistent_local(self):
        self.assert_refused(self.run_canary("9" * 32), "NOT_FOUND_LOCAL")

    def test_nonexistent_remote(self):
        self.api.overrides[f"/pages/{T}"] = (404, {})
        self.assert_refused(self.run_canary(T), "NOT_FOUND_REMOTE")

    def test_invalid_id(self):
        self.assert_refused(self.run_canary("not-an-id"), "INVALID_NOTION_ID")

    def test_managed_obsidian(self):
        self.assert_refused(self.run_canary(O), "MANAGED_NOT_NOTION")

    def test_archive(self):
        self.assert_refused(self.run_canary(A), "ARCHIVE_TARGET")

    def test_knowledge_protected(self):
        self.assert_refused(self.run_canary(P), "PROTECTED_TARGET")

    def test_malformed_frontmatter_missing_key(self):
        self.assert_refused(self.run_canary(M), "MALFORMED_FRONTMATTER")

    def test_malformed_frontmatter_unclosed(self):
        p = os.path.join(self.lavcca, "01_SHARED", "bad.md")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"---\nproject: LAVCCA\nnotion_id: {M}\nmanaged: notion\n\n# no closing\n")
        os.utime(p, (OLD_MTIME, OLD_MTIME))
        self.snapshot = self._hashes()
        self.assert_refused(self.run_canary(M), "MALFORMED_FRONTMATTER")

    def test_remote_archived(self):
        self.api.pages[T]["archived"] = True
        self.assert_refused(self.run_canary(T), "ARCHIVED_REMOTE")

    def test_remote_trashed(self):
        self.api.pages[T]["in_trash"] = True
        self.assert_refused(self.run_canary(T), "TRASHED_REMOTE")

    def test_not_descendant(self):
        self.api.dbs[DB]["parent"] = {"type": "workspace", "workspace": True}
        self.assert_refused(self.run_canary(T), "NOT_LAVCCA_DESCENDANT")

    def test_descendant_unknown_on_parent_403(self):
        self.api.overrides[f"/databases/{DB}"] = (403, {})
        self.assert_refused(self.run_canary(T), "NOT_LAVCCA_DESCENDANT")

    def test_content_fetch_error(self):
        self.api.overrides[f"/blocks/{T}/children?page_size=100"] = (None, {"message": "network"})
        self.assert_refused(self.run_canary(T), "CONTENT_FETCH_ERROR")

    def test_conflict_bootstrap_local_edit(self):
        os.utime(self.target, None)  # mtime = now, after migration baseline
        self.snapshot = self._hashes()
        self.assert_refused(self.run_canary(T), "CONFLICT")

    def test_conflict_state_baseline(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump({"items": {T: {"last_synced_content_hash": "0" * 64}}}, f)
        res = self.run_canary(T)
        self.assertEqual((res["status"], res["refusal_code"]), ("REFUSED", "CONFLICT"))
        self.assertEqual(self._hashes(), self.snapshot)

    def test_unsupported_critical_block(self):
        self.api.set_blocks(T, target_blocks() + [blk("f" * 32, "ai_block", {})])
        res = self.run_canary(T)
        self.assert_refused(res, "RENDER_UNSUPPORTED")
        self.assertEqual(res["detail"][0]["type"], "ai_block")

    def test_unresolved_notion_hosted_attachment(self):
        self.api.set_blocks(T, [blk("f" * 32, "file", {"type": "file", "file": {"url": "https://s3/x.zip"},
                                                       "name": "없는파일.zip", "caption": []})])
        res = self.run_canary(T)
        self.assert_refused(res, "RENDER_UNSUPPORTED")

    def test_state_unreadable_refused_before_write(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            f.write("{not json")
        res = self.run_canary(T)
        self.assertEqual((res["status"], res["refusal_code"]), ("REFUSED", "STATE_UNREADABLE"))
        self.assertEqual(self._hashes(), self.snapshot)


class TestRenderer(unittest.TestCase):
    def setUp(self):
        api = FakeAPI()
        tree = ca.fetch_block_tree(api, T, {"api_page_size": 100, "max_pagination_pages": 50})
        ctx = ca.RenderContext({C: "LAVCCA/02_X/Imported-Notion/자식 페이지.md"},
                               "[회의록 첨부.docx](회의록_첨부.docx)")
        self.md = ca.render_body("대상", tree, ctx)
        self.ctx = ctx

    def test_layout_and_blocks(self):
        md = self.md
        self.assertTrue(md.startswith("\n# 대상\n\n"))
        for expected in [
            "plain **bold** *ital* ~~gone~~ `x=1` [link](https://example.com)",
            "# H1", "## H2", "### H3",
            "- b1\n    - nested\n- b2",
            "1. n1",
            "- [x] done\n- [ ] todo",
            "- tog\n    inside",
            "> q",
            "> [!note]\n> 💡 note",
            "```python\nprint(1)\n```",
            "\n---\n",
            "[https://bm.example](https://bm.example)",
            "![cap](https://img.example/a.png)",
            "[회의록 첨부.docx](회의록_첨부.docx)",       # Notion-hosted file -> existing local attachment
            "[doc.pdf](https://pdf.example/doc.pdf)",
            "[[LAVCCA/02_X/Imported-Notion/자식 페이지|자식 페이지]]",
            "| h\\|1 | h2 |\n| --- | --- |\n| c1 | **c2** |",
        ]:
            self.assertIn(expected, md)
        self.assertNotIn("s3.example", md)  # signed URL never written
        self.assertEqual(self.ctx.critical, [])

    def test_unmapped_child_page_is_visible_placeholder(self):
        ctx = ca.RenderContext({}, "")
        out = ca.render_blocks([blk(C, "child_page", {"title": "x"})], ctx)
        self.assertIn("notion-sync: page", out)
        self.assertEqual(ctx.placeholders[0]["kind"], "unmapped_page_link")

    def test_unknown_type_is_critical(self):
        ctx = ca.RenderContext({}, "")
        with self.assertRaises(ca.RenderUnsupported):
            ca.render_body("x", [blk("f" * 32, "unsupported", {})], ctx)


class TestWritePath(CanaryTestBase):
    def test_happy_path_backup_atomic_replace_state(self):
        orig = self._read(self.target)
        res = self.run_canary(T)
        self.assertEqual(res["status"], "APPLIED", res)

        # backup: byte-for-byte, relative structure preserved
        backup = os.path.join(self.sync, "backups", "20261002T030000Z", "01_SHARED", "Meetings", "대상.md")
        self.assertEqual(res["backup_path"], backup)
        self.assertEqual(self._read(backup), orig)

        # target replaced; frontmatter identity preserved verbatim and in order
        new = self._read(self.target).decode("utf-8")
        fmd, lines, body = ca.split_frontmatter_strict(new)
        orig_fm, orig_lines, _ = ca.split_frontmatter_strict(orig.decode("utf-8"))
        self.assertEqual(lines[:len(orig_lines)], orig_lines)  # every original line kept, same order
        for k in ca.IDENTITY_KEYS + ("aliases",):
            self.assertEqual(fmd[k], orig_fm[k])
        self.assertEqual(fmd["notion_last_edited_time"], "2026-09-30T10:00:00.000Z")
        self.assertEqual(fmd["last_synced_at"], "2026-10-02T03:00:00Z")
        self.assertEqual(fmd["last_synced_content_hash"], ca.sha256_text(body))
        self.assertIn("**bold**", body)

        # state written only after success, with required fields
        with open(self.state_path, encoding="utf-8") as f:
            item = json.load(f)["items"][T]
        self.assertEqual(item, {"notion_id": T, "last_seen_edited_time": "2026-09-30T10:00:00.000Z",
                                "last_synced_content_hash": ca.sha256_text(body),
                                "obsidian_path": "01_SHARED/Meetings/대상.md",
                                "last_successful_sync": "2026-10-02T03:00:00Z"})

        # nothing else in the vault changed; no temp files left
        after = self._hashes()
        changed = {k for k in after if after[k] != self.snapshot.get(k)}
        self.assertEqual(changed, {os.path.join("01_SHARED", "Meetings", "대상.md")})
        self.assertEqual(glob.glob(os.path.join(self.tmp, "**", ".notion_sync_tmp_*"), recursive=True), [])

        # second run: baseline now exists, local untouched -> no conflict, identical body
        res2 = self.run_canary(T, now=datetime(2026, 10, 2, 4, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(res2["status"], "APPLIED", res2)
        self.assertEqual(res2["new_body_sha256"], item["last_synced_content_hash"])

        # local edit after sync -> CONFLICT, file untouched
        with open(self.target, "a", encoding="utf-8", newline="\n") as f:
            f.write("\n로컬 수정\n")
        edited = self._read(self.target)
        res3 = self.run_canary(T, now=datetime(2026, 10, 2, 5, 0, 0, tzinfo=timezone.utc))
        self.assertEqual((res3["status"], res3.get("refusal_code")), ("REFUSED", "CONFLICT"))
        self.assertEqual(self._read(self.target), edited)

    def test_temp_write_failure_keeps_original(self):
        orig = self._read(self.target)
        with mock.patch.object(ca, "_fsync", side_effect=OSError("disk full (injected)")):
            res = self.run_canary(T)
        self.assertEqual(res["status"], "FAILED", res)
        self.assertEqual(self._read(self.target), orig)
        self.assertEqual(glob.glob(os.path.join(self.tmp, "**", ".notion_sync_tmp_*"), recursive=True), [])
        with open(self.state_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["items"], {})

    def test_replace_failure_keeps_original(self):
        orig = self._read(self.target)
        with mock.patch.object(ca, "_replace", side_effect=PermissionError("locked (injected)")):
            res = self.run_canary(T)
        self.assertEqual(res["status"], "FAILED", res)
        self.assertEqual(self._read(self.target), orig)
        self.assertEqual(glob.glob(os.path.join(self.tmp, "**", ".notion_sync_tmp_*"), recursive=True), [])

    def test_state_write_failure_reported_and_rolled_back(self):
        orig = self._read(self.target)
        real_replace = ca._replace

        def failing_replace(src, dst):
            if os.path.basename(dst) == "state.json":
                raise OSError("state write failed (injected)")
            return real_replace(src, dst)

        with mock.patch.object(ca, "_replace", side_effect=failing_replace):
            res = self.run_canary(T)
        self.assertEqual(res["status"], "STATE_WRITE_FAILED_ROLLED_BACK", res)
        self.assertEqual(res["rollback"], "OK")
        self.assertEqual(self._read(self.target), orig)  # markdown restored byte-for-byte
        with open(self.state_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["items"], {})  # failure never recorded as success
        self.assertEqual(glob.glob(os.path.join(self.tmp, "**", ".notion_sync_tmp_*"), recursive=True), [])

    def test_concurrent_modification_refused(self):
        real_render = ca.render_body

        def render_and_touch(*a, **k):
            out = real_render(*a, **k)
            with open(self.target, "a", encoding="utf-8") as f:
                f.write("x")
            return out

        with mock.patch.object(ca, "render_body", side_effect=render_and_touch):
            res = self.run_canary(T)
        self.assertEqual((res["status"], res.get("refusal_code")), ("REFUSED", "CONCURRENT_MODIFICATION"))
        with open(self.state_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["items"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
