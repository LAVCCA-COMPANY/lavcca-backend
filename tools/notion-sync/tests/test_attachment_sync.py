# -*- coding: utf-8 -*-
"""Offline tests for Notion-hosted DOCX attachment sync (attachment_sync.py + the
production batch). Fixture vault, fake Notion API and fake HTTP transport only --
never touches real LAVCCA vault files and never calls the network."""
import io, os, re, sys, json, glob, zipfile, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_canary_apply import FakeAPI, blk, rt, ROOT, NOW, OLD_MTIME, T  # noqa: E402
from test_production_apply import ProdBase, RUN_AT  # noqa: E402
import canary_apply as ca  # noqa: E402
import baseline as bl  # noqa: E402
import production_apply as prod  # noqa: E402
import attachment_sync as att  # noqa: E402

OWNER_ROOTS = {"02_MEMBER_A": "김멤버", "03_MEMBER_B": "박멤버", "04_MEMBER_C": "이멤버"}  # pseudonymous fixture owners
att.configure_owner_roots(OWNER_ROOTS)

L = "7" * 32                       # 박멤버 owner page
FB = "f1" * 16                     # its new DOCX file block
DOCX_NAME = "박멤버 4분기 보고서.docx"
L_REL = "03_MEMBER_B/Imported-Notion/박멤버.md"
ATT_REL = "03_MEMBER_B/Imported-Notion/Attachments/" + DOCX_NAME
EDITED_2 = "2026-10-03T13:57:00.000Z"
SECRET = "SIGNATURE-SECRET"


def make_docx(text="Q4"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f"<w:document>{text}</w:document>")
    return buf.getvalue()


def owner_fm(nid, owner):
    return (f"---\nproject: LAVCCA\nowner: {owner}\nsource: notion\nmanaged: notion\nnotion_id: {nid}\n"
            f'notion_path: "LAVCCA/{owner} {nid}.md"\n---\n')


def file_blk(bid, name, ftype="file", t="file", edited=EDITED_2, url_name=None):
    """Notion file-like block. Listing URLs carry a (fixture) signature too, so a leak of
    any URL into markdown/state/logs is caught by the persistence tests."""
    path_name = url_name or name or "f.bin"
    data = {"type": ftype, "caption": [],
            ftype: {"url": f"https://prod-files-secure.s3.us-west-2.amazonaws.com/ws/{bid}/{path_name}"
                           f"?X-Amz-Expires=3600&X-Amz-Signature={SECRET}LIST",
                    "expiry_time": "2026-10-03T15:00:00.000Z"}}
    if name is not None:
        data["name"] = name
    b = blk(bid, t, data)
    b["last_edited_time"] = edited
    return b


def fd(name, url_name=None):
    """Block data (the `file` dict) for direct planner calls."""
    return file_blk("0" * 32, name, url_name=url_name)["file"]


class AttAPI(FakeAPI):
    """FakeAPI + GET /blocks/<id> (single block). Every fetch of a Notion-hosted file
    returns a fresh signed URL, as the real API does."""

    def __init__(self):
        super().__init__()
        self.url_seq = 0
        self.block_fetches = []

    def __call__(self, method, path, body=None):
        m = re.fullmatch(r"/blocks/([0-9a-f]{32})", path)
        if m and path not in self.overrides:
            self.calls.append((method, path))
            bid = m.group(1)
            for pid, blocks in self.children.items():
                for b in blocks:
                    if b["id"] == bid:
                        self.block_fetches.append(bid)
                        self.url_seq += 1
                        out = json.loads(json.dumps({k: v for k, v in b.items() if k != "_fixture_children"}))
                        d = out[out["type"]]
                        if d.get("type") == "file":  # same stable file path, fresh signature
                            d["file"]["url"] = (d["file"]["url"].split("?")[0] +
                                                f"?X-Amz-Expires=3600&X-Amz-Signature={SECRET}{self.url_seq}")
                        out["parent"] = {"type": "page_id", "page_id": pid}
                        return 200, out
            return 404, {}
        return super().__call__(method, path, body)


class FakeHTTP:
    """Signed-URL transport. responses: list consumed per call (last one repeats)."""

    def __init__(self, *responses):
        self.responses = list(responses) or [(200, None, make_docx())]
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        status, headers, data = r
        return status, headers if headers is not None else {"content-length": str(len(data))}, data


class AttBase(ProdBase):
    def setUp(self):
        self.api_cls = AttAPI
        super().setUp()
        api = AttAPI()
        api.pages.update(self.api.pages)
        api.children.update(self.api.children)
        self.api = api
        self.l_path = os.path.join(self.lavcca, *L_REL.split("/"))
        os.makedirs(os.path.dirname(self.l_path), exist_ok=True)
        with open(self.l_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(owner_fm(L, "박멤버") + "\n# 박멤버\n\n첫 문단\n")
        os.utime(self.l_path, (OLD_MTIME, OLD_MTIME))
        self.api.pages[L] = {"object": "page", "id": L, "archived": False, "in_trash": False,
                             "last_edited_time": "2026-09-30T10:54:00.000Z",
                             "parent": {"type": "page_id", "page_id": ROOT},
                             "properties": {"title": {"type": "title", "title": [{"plain_text": "박멤버"}]}}}
        self.api.children[L] = [blk("9" * 31 + "7", "paragraph", {"rich_text": [rt("첫 문단")]})]
        id_map = ca.id_to_vault_path(ca.scan_vault_identities(self.cfg))
        r = bl.evaluate_page(L, self.l_path, self.node(L), self.api, self.cfg, {}, id_map,
                             OLD_MTIME + 86400 * 2, True, NOW)
        self.assertEqual(r["status"], "ADOPT_CANDIDATE", r)
        bl.adopt_baselines(self.state_path, [r["entry"]], self.sync, now=NOW.replace(hour=4))
        self.state_after_adopt = self._read(self.state_path)
        self.md_after_adopt = self.md()

    def path_of(self, nid):
        return self.l_path if nid == L else super().path_of(nid)

    def upload_docx(self, name=DOCX_NAME, bid=FB, **kw):
        """Team member uploads a file into the owner page (page last_edited_time moves)."""
        self.api.children[L] = list(self.api.children[L]) + [file_blk(bid, name, **kw)]
        self.api.pages[L]["last_edited_time"] = EDITED_2

    def run_sync(self, rows=None, apply=True, http=None, **kw):
        self.http = http or FakeHTTP()
        return prod.run_production(rows or self.upd(L), kw.get("crawl_ok", True), list(kw.get("api_errors", ())),
                                   self.api, self.cfg, self.state_path, self.sync, ca.scan_vault_identities(self.cfg),
                                   kw.get("now", RUN_AT), apply=apply, md_snapshot=self.vault_md, http_get=self.http)

    def classify(self, nid=L, rel=L_REL, owner="박멤버"):
        st = self.state()
        planner = att.AttachmentPlanner(self.lavcca, att.state_attachments(st))
        path = os.path.join(self.lavcca, *rel.split("/"))
        body = ca.split_frontmatter_strict(self._read(path).decode("utf-8"))[2]
        id_map = ca.id_to_vault_path(ca.scan_vault_identities(self.cfg))
        h = bl.render_current(self.api, nid, self.node(nid), body, self.cfg, id_map, planner.for_page(nid, rel, owner))
        return bl.classify_incremental(bl.baseline_view(st["items"][nid]), self.api.pages[nid]["last_edited_time"],
                                       h, ca.sha256_text(body))[0]

    def att_abs(self, rel=ATT_REL):
        return os.path.join(self.lavcca, *rel.split("/"))

    def att_dir_files(self):
        d = os.path.dirname(self.att_abs())
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def assert_nothing_written(self):
        self.assertEqual(self.md(), self.md_after_adopt)
        self.assertEqual(self._read(self.state_path), self.state_after_adopt)
        self.assertFalse(os.path.exists(os.path.dirname(self.att_abs())), "Attachments dir must not exist")
        self.assertEqual(glob.glob(os.path.join(self.tmp, "**", ".notion_att_*"), recursive=True), [])
        self.assertEqual(glob.glob(os.path.join(self.tmp, "**", ".notion_sync_tmp_*"), recursive=True), [])


# 1 + 3 (+ DRY-RUN semantics)
class TestCandidate(AttBase):
    def test_docx_block_is_download_candidate_preview_writes_nothing(self):
        self.upload_docx()
        self.assertEqual(self.classify(), bl.UPDATE_CONFIRMED)
        res = self.run_sync(apply=False)
        self.assertEqual(res["status"], "PREVIEW", res)
        self.assertEqual(res["attachment_download_candidates"], [{
            "owner": "박멤버", "attachment_type": "docx", "original_filename": DOCX_NAME, "block_id": FB,
            "local_path": ATT_REL,
            "parent_markdown": L_REL, "supported_type": "docx", "action": "download",
            "link_target": "Attachments/" + DOCX_NAME}])
        self.assertEqual(self.http.urls, [])          # DRY-RUN: no download at all
        self.assertEqual(self.api.block_fetches, [])
        self.assert_nothing_written()

    def test_owner_roots(self):
        planner = att.AttachmentPlanner(self.lavcca, {})
        for root, owner in att.OWNER_ROOTS.items():
            pa = planner.for_page(L, f"{root}/Imported-Notion/{owner}.md", owner)
            target, problem = pa.resolve("file", FB, fd("a.docx"), {"last_edited_time": EDITED_2})
            self.assertIsNone(problem)
            self.assertEqual(pa.plans[-1]["local_path"], f"{root}/Imported-Notion/Attachments/a.docx")
            self.assertEqual(target, "Attachments/a.docx")
            planner.claims.clear()

    def test_non_owner_area_or_owner_mismatch_fails_closed(self):
        planner = att.AttachmentPlanner(self.lavcca, {})
        for rel, owner in (("01_SHARED/x.md", "shared"), ("03_MEMBER_B/Imported-Notion/박멤버.md", "김멤버")):
            _, problem = planner.for_page(L, rel, owner).resolve("file", FB, fd("a.docx"), {})
            self.assertEqual(problem["type"], "file:attachment_owner_unresolved")


# 4 + 5 + 2
class TestDownload(AttBase):
    def test_successful_download_local_link_and_state(self):
        self.upload_docx()
        data = make_docx("4분기")
        res = self.run_sync(http=FakeHTTP((200, None, data)))
        self.assertEqual(res["status"], "ALL_APPLIED", res)
        self.assertEqual(res["applied"], [L])
        self.assertEqual(self._read(self.att_abs()), data)
        text = self._read(self.l_path).decode("utf-8")
        self.assertIn(f"[{DOCX_NAME}](<Attachments/{DOCX_NAME}>)", text)
        rec = self.state()[att.STATE_KEY][FB]
        self.assertEqual(rec, {"block_id": FB, "block_type": "file", "attachment_type": "docx",
                               "parent_notion_id": L, "owner": "박멤버",
                               "original_filename": DOCX_NAME, "local_path": ATT_REL,
                               "content_sha256": ca.sha256_bytes(data), "size_bytes": len(data),
                               "block_last_edited_time": EDITED_2, "last_synced_at": "2026-10-04T00:00:00Z"})
        self.assertEqual(self.state()["items"][L]["record_type"], prod.SYNCED_RECORD_TYPE)
        self.assertEqual(glob.glob(os.path.join(self.tmp, "**", ".notion_att_*"), recursive=True), [])
        self.assertFalse(os.path.exists(os.path.join(self.sync, "tmp_attachments")))

    def test_relative_link_from_nested_markdown(self):
        planner = att.AttachmentPlanner(self.lavcca, {})
        md = "03_MEMBER_B/AI/Deep/page.md"
        pa = planner.for_page(L, md, "박멤버")
        target, _ = pa.resolve("file", FB, fd(DOCX_NAME), {})
        self.assertEqual(target, f"../../Imported-Notion/Attachments/{DOCX_NAME}")
        resolved = os.path.normpath(os.path.join(self.lavcca, *md.split("/")[:-1], *target.split("/")))
        self.assertEqual(resolved, os.path.normpath(self.att_abs()))
        self.assertNotIn(":", target)  # never a Windows absolute path

    def test_signed_url_never_persisted(self):
        self.upload_docx()
        res = self.run_sync(http=FakeHTTP((403, {}, b""), (200, None, make_docx())))
        self.assertEqual(res["status"], "ALL_APPLIED", res)
        self.assertTrue(all(SECRET in u for u in self.http.urls))  # transport did use signed URLs
        blobs = [json.dumps(res, ensure_ascii=False)]
        for p in glob.glob(os.path.join(self.tmp, "**", "*"), recursive=True):
            if os.path.isfile(p) and not p.endswith(".docx"):
                blobs.append(self._read(p).decode("utf-8", errors="replace"))
        for b in blobs:
            self.assertNotIn(SECRET, b)
            self.assertNotIn("X-Amz", b)
            self.assertNotIn("amazonaws", b)


# 6 + 7 + 8
class TestDownloadFailures(AttBase):
    def setUp(self):
        super().setUp()
        self.upload_docx()

    def test_empty_download_rejected_nothing_written(self):
        res = self.run_sync(http=FakeHTTP((200, {"content-length": "0"}, b"")))
        self.assertEqual(res["status"], "NOTHING_TO_APPLY", res)
        self.assertEqual(res["excluded"][0]["status"], "EXCLUDED_ATTACHMENT_DOWNLOAD_FAILED")
        self.assertIn("EMPTY_DOWNLOAD", res["excluded"][0]["reason"])
        self.assert_nothing_written()

    def test_http_failure_bounded_retries_no_writes(self):
        with mock.patch.object(att.time, "sleep"):
            res = self.run_sync(http=FakeHTTP((500, {}, b"")))
        self.assertEqual(res["excluded"][0]["status"], "EXCLUDED_ATTACHMENT_DOWNLOAD_FAILED")
        self.assertIn("DOWNLOAD_FAILED", res["excluded"][0]["reason"])
        self.assertEqual(len(self.http.urls), att.MAX_DOWNLOAD_ATTEMPTS)  # bounded, not infinite
        self.assert_nothing_written()

    def test_network_error_then_not_a_docx_rejected(self):
        with mock.patch.object(att.time, "sleep"):
            res = self.run_sync(http=FakeHTTP(OSError("reset"), (200, None, b"PK not really a zip")))
        self.assertIn("NOT_A_DOCX", res["excluded"][0]["reason"])
        self.assert_nothing_written()

    def test_content_length_mismatch_retried(self):
        data = make_docx()
        with mock.patch.object(att.time, "sleep"):
            res = self.run_sync(http=FakeHTTP((200, {"content-length": str(len(data) + 5)}, data), (200, None, data)))
        self.assertEqual(res["status"], "ALL_APPLIED", res)
        self.assertEqual(len(self.http.urls), 2)

    def test_expired_url_refetches_block_for_fresh_url(self):
        with mock.patch.object(att.time, "sleep"):
            res = self.run_sync(http=FakeHTTP((403, {}, b""), (200, None, make_docx())))
        self.assertEqual(res["status"], "ALL_APPLIED", res)
        self.assertEqual(self.api.block_fetches, [FB, FB])     # fresh block fetch per attempt
        self.assertEqual(len(set(self.http.urls)), 2)           # a different (fresh) signed URL

    def test_block_changed_since_planning_not_retried(self):
        self.api.overrides[f"/blocks/{FB}"] = (404, {})
        res = self.run_sync()
        self.assertIn("BLOCK_FETCH_FAILED", res["excluded"][0]["reason"])
        self.assertEqual(self.http.urls, [])
        self.assert_nothing_written()


# 9 + 10 + 11
class TestCollisionReuseReplace(AttBase):
    def test_filename_collision_uses_collision_safe_name(self):
        os.makedirs(os.path.dirname(self.att_abs()))
        with open(self.att_abs(), "wb") as f:
            f.write(b"someone else's file")
        self.upload_docx()
        self.md_after_adopt = self.md()
        res = self.run_sync()
        self.assertEqual(res["status"], "ALL_APPLIED", res)
        self.assertEqual(self._read(self.att_abs()), b"someone else's file")  # never overwritten
        safe_name = f"박멤버 4분기 보고서 ({FB}).docx"
        self.assertEqual(self.att_dir_files(), sorted([DOCX_NAME, safe_name]))
        self.assertIn(f"(<Attachments/{safe_name}>)", self._read(self.l_path).decode("utf-8"))

    def test_both_names_taken_fails_closed(self):
        os.makedirs(os.path.dirname(self.att_abs()))
        for n in (DOCX_NAME, f"박멤버 4분기 보고서 ({FB}).docx"):
            with open(os.path.join(os.path.dirname(self.att_abs()), n), "wb") as f:
                f.write(b"x")
        self.upload_docx()
        res = self.run_sync()
        self.assertEqual(res["excluded"][0]["status"], "RENDER_UNSUPPORTED")
        self.assertEqual(res["excluded"][0]["reason"][0]["type"], "file:attachment_collision")
        self.assertEqual(self.md(), self.md_after_adopt)
        self.assertEqual(self.http.urls, [])

    def test_same_block_same_hash_reused_no_duplicate_download(self):
        self.upload_docx()
        self.assertEqual(self.run_sync()["status"], "ALL_APPLIED")
        self.assertEqual(self.classify(), bl.UNCHANGED)              # next sync: UNCHANGED
        res = self.run_sync()
        self.assertEqual(res["status"], "NOTHING_TO_APPLY", res)
        self.assertEqual(res["excluded"][0]["status"], bl.UNCHANGED)
        self.assertEqual(self.http.urls, [])                         # no second download
        self.assertEqual(self.att_dir_files(), [DOCX_NAME])          # no duplicate file

    def test_changed_content_same_block_replaced_with_backup(self):
        self.upload_docx()
        old = make_docx("v1")
        self.assertEqual(self.run_sync(http=FakeHTTP((200, None, old)))["status"], "ALL_APPLIED")
        # file replaced in the same Notion block + page text edited
        self.api.children[L][-1]["last_edited_time"] = "2026-10-05T00:00:00.000Z"
        self.api.children[L].insert(0, blk("9" * 31 + "8", "paragraph", {"rich_text": [rt("추가 문장")]}))
        self.api.pages[L]["last_edited_time"] = "2026-10-05T00:00:00.000Z"
        new = make_docx("v2")
        later = RUN_AT.replace(hour=1)
        res = self.run_sync(http=FakeHTTP((200, None, new)), now=later)
        self.assertEqual(res["status"], "ALL_APPLIED", res)
        self.assertEqual(res["attachments_written"][0]["action"], "replace")
        self.assertEqual(self._read(self.att_abs()), new)
        backup = os.path.join(self.sync, "backups", later.strftime("%Y%m%dT%H%M%SZ"), *ATT_REL.split("/"))
        self.assertEqual(self._read(backup), old)
        self.assertEqual(self.state()[att.STATE_KEY][FB]["content_sha256"], ca.sha256_bytes(new))
        self.assertEqual(self.att_dir_files(), [DOCX_NAME])

    def test_refresh_with_identical_content_is_state_only(self):
        self.upload_docx()
        data = make_docx("same")
        self.run_sync(http=FakeHTTP((200, None, data)))
        self.api.children[L][-1]["last_edited_time"] = "2026-10-05T00:00:00.000Z"
        self.api.children[L].insert(0, blk("9" * 31 + "8", "paragraph", {"rich_text": [rt("추가")]}))
        self.api.pages[L]["last_edited_time"] = "2026-10-05T00:00:00.000Z"
        res = self.run_sync(http=FakeHTTP((200, None, data)), now=RUN_AT.replace(hour=2))
        self.assertEqual(res["status"], "ALL_APPLIED", res)
        self.assertEqual(res["attachments_written"], [])
        self.assertEqual(self.state()[att.STATE_KEY][FB]["block_last_edited_time"], "2026-10-05T00:00:00.000Z")

    def test_locally_modified_attachment_fails_closed(self):
        self.upload_docx()
        self.run_sync()
        with open(self.att_abs(), "ab") as f:
            f.write(b"local edit")
        with self.assertRaises(ca.RenderUnsupported) as cm:
            self.classify()
        self.assertEqual(cm.exception.items[0]["type"], "file:attachment_local_modified")


# 12 + 13 (+ partial-failure guarantees)
class TestRollback(AttBase):
    def setUp(self):
        super().setUp()
        self.upload_docx()

    def test_state_commit_failure_rolls_back_attachment(self):
        real = prod._write_verified

        def fail_state(path, data):
            if os.path.basename(path) == "state.json" and b"SYNCED" in data:
                raise OSError("state commit failed (injected)")
            return real(path, data)

        with mock.patch.object(prod, "_write_verified", side_effect=fail_state):
            res = self.run_sync()
        self.assertEqual(res["status"], "ROLLED_BACK", res)
        self.assertTrue(res["rollback"]["ok"], res["rollback"])
        self.assertEqual(res["attachments_rolled_back"], [ATT_REL])
        self.assert_nothing_written()

    def test_markdown_commit_failure_rolls_back_attachment(self):
        real = prod._write_verified

        def fail_md(path, data):
            # the markdown commit fails; the rollback's restore of the same file succeeds
            if path.endswith(".md") and data != self.md_bytes_before:
                raise OSError("markdown commit failed (injected)")
            return real(path, data)

        self.md_bytes_before = self._read(self.l_path)

        with mock.patch.object(prod, "_write_verified", side_effect=fail_md):
            res = self.run_sync()
        self.assertEqual(res["status"], "ROLLED_BACK", res)
        self.assertTrue(res["rollback"]["ok"], res["rollback"])
        self.assert_nothing_written()

    def test_attachment_commit_failure_writes_nothing(self):
        with mock.patch.object(att, "commit_new_file", side_effect=OSError("rename failed (injected)")):
            res = self.run_sync()
        self.assertEqual(res["status"], "ROLLED_BACK", res)
        self.assert_nothing_written()

    def test_destination_appearing_after_preflight_aborts_before_write(self):
        real = att.download_to_temp

        def racing(*a, **kw):
            out = real(*a, **kw)
            os.makedirs(os.path.dirname(self.att_abs()), exist_ok=True)
            with open(self.att_abs(), "wb") as f:
                f.write(b"appeared")
            return out

        with mock.patch.object(att, "download_to_temp", side_effect=racing):
            res = self.run_sync()
        self.assertEqual(res["status"], "ABORTED_BEFORE_WRITE", res)
        self.assertEqual(self._read(self.att_abs()), b"appeared")   # never overwritten
        self.assertEqual(self.md(), self.md_after_adopt)
        self.assertEqual(self._read(self.state_path), self.state_after_adopt)

    def test_crawl_or_api_failure_downloads_nothing(self):
        self.assertEqual(self.run_sync(crawl_ok=False)["status"], "ABORTED_CRAWL_HEALTH")
        self.assertEqual(self.run_sync(api_errors=[{"code": 502}])["status"], "ABORTED_API_ERRORS")
        self.assertEqual(self.http.urls, [])
        self.assert_nothing_written()


# 14 + scope
class TestScope(AttBase):
    def test_unsupported_extension_stays_fail_closed(self):
        for name in ("보고서.pdf", "표.xlsx", "발표.pptx", "그림.png"):
            self.api.children[L] = [c for c in self.api.children[L] if c["id"] != FB]
            self.upload_docx(name=name)
            res = self.run_sync()
            self.assertEqual(res["excluded"][0]["status"], "RENDER_UNSUPPORTED", name)
            self.assertEqual(res["excluded"][0]["reason"][0]["type"], "file:notion_hosted_unresolved")
            self.assertEqual(self.http.urls, [])
        self.assert_nothing_written()

    def test_docx_in_pdf_or_image_block_type_not_handled(self):
        self.api.children[L] = list(self.api.children[L]) + [file_blk(FB, "x.docx", t="pdf")]
        self.api.pages[L]["last_edited_time"] = EDITED_2
        res = self.run_sync()
        self.assertEqual(res["excluded"][0]["reason"][0]["type"], "pdf:notion_hosted_unresolved")

    def test_existing_local_link_still_preferred_over_download(self):
        # migrated attachment link already in the local body -> unchanged legacy behaviour
        with open(self.l_path, "a", encoding="utf-8", newline="\n") as f:
            f.write("\n[옛 문서.docx](옛_문서.docx)\n")
        pa = att.AttachmentPlanner(self.lavcca, {}).for_page(L, L_REL, "박멤버")
        body = ca.split_frontmatter_strict(self._read(self.l_path).decode("utf-8"))[2]
        ctx = ca.RenderContext({}, body, pa)
        out = ca.render_blocks([file_blk(FB, "옛 문서.docx")], ctx)
        self.assertEqual((out, ctx.critical, pa.plans), ("[옛 문서.docx](옛_문서.docx)", [], []))

    def test_without_planner_behaviour_is_unchanged(self):
        ctx = ca.RenderContext({}, "")
        ca.render_blocks([file_blk(FB, DOCX_NAME)], ctx)
        self.assertEqual(ctx.critical[0]["type"], "file:notion_hosted_unresolved")

    def test_sanitize_filename(self):
        s = att.sanitize_filename
        self.assertEqual(s(DOCX_NAME), DOCX_NAME)
        self.assertEqual(s('a:b*c?"d<e>f|g#h.docx'), "a_b_c__d_e_f_g_h.docx")
        self.assertEqual(s("  보고서 .docx. "), "보고서 .docx")
        self.assertEqual(s("CON.docx"), "_CON.docx")
        self.assertEqual(s(".docx"), "")
        self.assertEqual(s("x.pdf"), "")
        import unicodedata
        self.assertEqual(s(unicodedata.normalize("NFD", DOCX_NAME)), DOCX_NAME)


# One-time FIRST SYNC (explicit allow-list, no baseline)
F = "c" * 32                        # 김멤버 owner page, no state record
FB2 = "f2" * 16
F_REL = "02_MEMBER_A/Imported-Notion/김멤버.md"
F_DOCX = "김멤버_Q4_문서.docx"
F_ATT = "02_MEMBER_A/Imported-Notion/Attachments/" + F_DOCX


class TestFirstSync(AttBase):
    def setUp(self):
        super().setUp()
        self.f_path = os.path.join(self.lavcca, *F_REL.split("/"))
        os.makedirs(os.path.dirname(self.f_path), exist_ok=True)
        with open(self.f_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(owner_fm(F, "김멤버") + "\n# 김멤버\n\n기존 문단\n\n[https://www.notion.so](https://www.notion.so)\n")
        os.utime(self.f_path, (OLD_MTIME, OLD_MTIME))  # untouched since the migration
        self.api.pages[F] = {"object": "page", "id": F, "archived": False, "in_trash": False,
                             "last_edited_time": EDITED_2, "parent": {"type": "page_id", "page_id": ROOT},
                             "properties": {"title": {"type": "title", "title": [{"plain_text": "김멤버"}]}}}
        self.api.children[F] = [blk("9" * 31 + "c", "paragraph", {"rich_text": [rt("기존 문단")]}),
                                file_blk(FB2, F_DOCX)]
        self.cfg["first_sync_ids"] = [F]
        self.md_after_adopt = self.md()
        self.f_before = self._read(self.f_path)

    @staticmethod
    def fs_row(nid=F):
        return [{"notion_id": nid, "status": "UPDATE_UNVERIFIED"}]

    def test_first_sync_writes_markdown_attachment_and_synced_state(self):
        res = self.run_sync(self.fs_row())
        self.assertEqual(res["status"], "ALL_APPLIED", res)
        self.assertEqual(res["first_sync_candidates"], [F])
        self.assertEqual(res["applied"], [F])
        text = self._read(self.f_path).decode("utf-8")
        fmd, lines, body = ca.split_frontmatter_strict(text)
        self.assertEqual(body, f"\n# 김멤버\n\n기존 문단\n\n[{F_DOCX}](Attachments/{F_DOCX})\n")
        self.assertEqual(fmd["owner"], "김멤버")
        self.assertEqual(fmd["last_synced_content_hash"], ca.sha256_text(body))
        self.assertTrue(os.path.isfile(self.att_abs(F_ATT)))
        rec = self.state()["items"][F]
        self.assertEqual((rec["record_type"], rec["first_sync_at"], rec["obsidian_path"]),
                         (prod.SYNCED_RECORD_TYPE, "2026-10-04T00:00:00Z", F_REL))
        self.assertEqual(rec["local_content_hash"], ca.sha256_text(body))
        self.assertEqual(self.state()[att.STATE_KEY][FB2]["owner"], "김멤버")
        # original backed up in the batch backup
        bk = os.path.join(self.sync, "backups", "20261004T000000Z", *F_REL.split("/"))
        self.assertEqual(self._read(bk), self.f_before)

    def test_first_sync_never_repeats_then_normal_pipeline(self):
        self.assertEqual(self.run_sync(self.fs_row())["status"], "ALL_APPLIED")
        after = self._read(self.f_path)
        # a later run offering the same row again: not selected (state record exists)
        res = self.run_sync(self.fs_row(), now=RUN_AT.replace(hour=3))
        self.assertEqual((res["status"], res["first_sync_candidates"]), ("NOTHING_TO_APPLY", []), res)
        self.assertEqual(self._read(self.f_path), after)
        self.assertEqual(self.http.urls, [])
        # preflight guard itself also refuses
        st = self.state()
        r = prod.preflight_first_sync(F, self.api, self.cfg, st["items"], ca.scan_vault_identities(self.cfg), RUN_AT)
        self.assertEqual(r["status"], "EXCLUDED_FIRST_SYNC_ALREADY_DONE")
        # normal pipeline: UNCHANGED, then UPDATE_CONFIRMED after a Notion edit
        self.assertEqual(self.classify(F, F_REL, "김멤버"), bl.UNCHANGED)
        self.api.children[F].append(blk("9" * 31 + "d", "paragraph", {"rich_text": [rt("새 내용")]}))
        self.api.pages[F]["last_edited_time"] = "2026-10-06T00:00:00.000Z"
        self.assertEqual(self.classify(F, F_REL, "김멤버"), bl.UPDATE_CONFIRMED)

    def test_not_allow_listed_update_unverified_never_written(self):
        self.cfg["first_sync_ids"] = []
        res = self.run_sync(self.fs_row())
        self.assertEqual((res["status"], res["first_sync_candidates"]), ("NOTHING_TO_APPLY", []))
        self.cfg["first_sync_ids"] = ["0" * 32]
        self.assertEqual(self.run_sync(self.fs_row())["first_sync_candidates"], [])
        self.assertEqual(self._read(self.f_path), self.f_before)
        self.assert_nothing_written_f()

    def test_allow_listed_but_other_status_not_selected(self):
        for st in ("UNCHANGED", "RENDER_UNSUPPORTED", "PATH_POLICY_UNRESOLVED", "QUERY_HIDDEN_ACTIVE", "CREATE_CANDIDATE"):
            self.assertEqual(prod.select_first_sync_candidates([{"notion_id": F, "status": st}], [F], {}), [])

    def test_local_modification_conflict_refused(self):
        with open(self.f_path, "a", encoding="utf-8", newline="\n") as f:
            f.write("\n사람이 고침\n")  # mtime now after the migration
        edited = self._read(self.f_path)
        res = self.run_sync(self.fs_row())
        self.assertEqual(res["excluded"][0]["status"], bl.LOCAL_MODIFICATION_CONFLICT)
        self.assertEqual(self._read(self.f_path), edited)
        self.assert_nothing_written_f()

    def test_not_descendant_refused(self):
        self.api.pages[F]["parent"] = {"type": "workspace", "workspace": True}
        res = self.run_sync(self.fs_row())
        self.assertEqual(res["excluded"][0]["status"], "MOVED_OUTSIDE_LAVCCA")
        self.assert_nothing_written_f()

    def test_nondeterministic_render_refused(self):
        real = self.api.__call__
        n = {"i": 0}

        def flaky(method, path, body=None):
            code, resp = real(method, path, body)
            if path.startswith(f"/blocks/{F}/children"):
                n["i"] += 1
                resp = json.loads(json.dumps(resp))
                resp["results"][0]["paragraph"]["rich_text"][0]["plain_text"] = f"변동 {n['i']}"
            return code, resp

        self.api = mock.Mock(side_effect=flaky)
        res = self.run_sync(self.fs_row())
        self.assertEqual(res["excluded"][0]["status"], "EXCLUDED_RENDER_NONDETERMINISTIC")
        self.assert_nothing_written_f()

    def test_placeholder_refused(self):
        self.api.children[F].append(blk("9" * 31 + "e", "table_of_contents", {}))
        res = self.run_sync(self.fs_row())
        self.assertEqual(res["excluded"][0]["status"], "EXCLUDED_FIRST_SYNC_PLACEHOLDERS")
        self.assert_nothing_written_f()

    def test_unsupported_block_refused(self):
        self.api.children[F].append(file_blk("e" * 32, "", ftype="external"))
        self.api.children[F][-1]["file"]["external"] = {"url": ""}
        res = self.run_sync(self.fs_row())
        self.assertEqual(res["excluded"][0]["status"], "RENDER_UNSUPPORTED")
        self.assert_nothing_written_f()

    def test_state_commit_failure_rolls_back_first_sync(self):
        real = prod._write_verified

        def fail_state(path, data):
            if os.path.basename(path) == "state.json" and b"SYNCED" in data:
                raise OSError("state commit failed (injected)")
            return real(path, data)

        with mock.patch.object(prod, "_write_verified", side_effect=fail_state):
            res = self.run_sync(self.fs_row())
        self.assertEqual(res["status"], "ROLLED_BACK", res)
        self.assertEqual(self._read(self.f_path), self.f_before)
        self.assert_nothing_written_f()

    def test_first_sync_and_updates_in_one_batch(self):
        self.upload_docx()
        res = self.run_sync(self.upd(L) + self.fs_row())
        self.assertEqual(res["status"], "ALL_APPLIED", res)
        self.assertEqual(sorted(res["applied"]), sorted([L, F]))
        self.assertEqual(sorted(a["owner"] for a in res["attachments_written"]), sorted(["박멤버", "김멤버"]))
        self.assertTrue(os.path.isfile(self.att_abs()) and os.path.isfile(self.att_abs(F_ATT)))

    def test_batch_refuses_first_sync_if_record_appeared(self):
        pre = prod.preflight_first_sync(F, self.api, self.cfg, {}, ca.scan_vault_identities(self.cfg), RUN_AT,
                                        att.AttachmentPlanner(self.lavcca, {}), None)
        pre["attachments"] = []
        st = self.state()
        st["items"][F] = {"notion_id": F}
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(st, f)
        res = prod.apply_batch([pre], self.state_path, self.sync, self.vault, RUN_AT, lavcca_root=self.lavcca)
        self.assertEqual(res["status"], "ABORTED_BEFORE_WRITE", res)
        self.assertEqual(self._read(self.f_path), self.f_before)

    def assert_nothing_written_f(self):
        self.assertEqual(self._read(self.state_path), self.state_after_adopt)
        self.assertFalse(os.path.exists(os.path.dirname(self.att_abs(F_ATT))))
        self.assertEqual(glob.glob(os.path.join(self.tmp, "**", ".notion_att_*"), recursive=True), [])
        self.assertEqual(glob.glob(os.path.join(self.tmp, "**", ".notion_sync_tmp_*"), recursive=True), [])


# Notion-hosted images
IB = "1b" * 16
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 20
IMG_REL = f"03_MEMBER_B/Imported-Notion/Attachments/notion-image-{IB}.jpg"


def image_blk(bid=IB, url_name="378db9a1.jpg", name=None):
    return file_blk(bid, name, t="image", url_name=url_name)


class TestImage(AttBase):
    def add_image(self, **kw):
        self.api.children[L] = list(self.api.children[L]) + [image_blk(**kw)]
        self.api.pages[L]["last_edited_time"] = EDITED_2

    def test_image_downloaded_embedded_and_recorded(self):
        self.add_image()
        res = self.run_sync(http=FakeHTTP((200, None, JPEG)))
        self.assertEqual(res["status"], "ALL_APPLIED", res)
        self.assertEqual(self._read(self.att_abs(IMG_REL)), JPEG)
        self.assertIn(f"\n![](Attachments/notion-image-{IB}.jpg)\n", self._read(self.l_path).decode("utf-8"))
        rec = self.state()[att.STATE_KEY][IB]
        self.assertEqual((rec["block_type"], rec["attachment_type"], rec["original_filename"], rec["local_path"],
                          rec["content_sha256"]), ("image", "image", None, IMG_REL, ca.sha256_bytes(JPEG)))
        self.assertEqual(self.classify(), bl.UNCHANGED)          # reuse on the next run
        self.assertEqual(self.run_sync()["status"], "NOTHING_TO_APPLY")
        self.assertEqual(self.http.urls, [])

    def test_image_api_name_and_formats(self):
        planner = att.AttachmentPlanner(self.lavcca, {})
        pa = planner.for_page(L, L_REL, "박멤버")
        for i, (url_name, name, expect) in enumerate((("a.png", None, f"notion-image-{IB[:30]}0{0}.png"),
                                                      ("x.webp", "사진 1.webp", "사진 1.webp"),
                                                      ("x.jpeg", None, None))):
            bid = IB[:30] + f"0{i}"
            target, problem = pa.resolve("image", bid, image_blk(bid, url_name, name)["image"], {})
            self.assertIsNone(problem)
            if expect:
                self.assertEqual(target, "Attachments/" + expect.replace(IB[:30] + "00", bid))
        for url_name in ("a.gif", "a.svg", "a"):
            ctx = ca.RenderContext({}, "", planner.for_page(L, L_REL, "박멤버"))
            ca.render_blocks([image_blk("9" * 32, url_name)], ctx)
            self.assertEqual(ctx.critical[0]["type"], "image:notion_hosted_unresolved")

    def test_image_content_validation(self):
        self.add_image(url_name="p.png")
        for data, code in ((b"not an image at all", "NOT_AN_IMAGE"), (JPEG, "IMAGE_FORMAT_MISMATCH")):
            res = self.run_sync(http=FakeHTTP((200, None, data)))
            self.assertEqual(res["excluded"][0]["status"], "EXCLUDED_ATTACHMENT_DOWNLOAD_FAILED")
            self.assertIn(code, res["excluded"][0]["reason"])
            self.assert_nothing_written()
        self.assertEqual(att.image_format(PNG), "png")
        self.assertEqual(att.image_format(WEBP), "webp")

    def test_image_and_docx_on_one_page_both_synced(self):
        self.upload_docx()
        self.add_image()
        http = FakeHTTP((200, None, make_docx()), (200, None, JPEG))
        res = self.run_sync(http=http)
        self.assertEqual(res["status"], "ALL_APPLIED", res)
        self.assertEqual(sorted(a["attachment_type"] for a in res["attachments_written"]), ["docx", "image"])
        text = self._read(self.l_path).decode("utf-8")
        self.assertIn(f"(<Attachments/{DOCX_NAME}>)", text)
        self.assertIn(f"![](Attachments/notion-image-{IB}.jpg)", text)

    def test_image_signed_url_never_persisted(self):
        self.add_image()
        self.assertEqual(self.run_sync(http=FakeHTTP((403, {}, b""), (200, None, JPEG)))["status"], "ALL_APPLIED")
        for p in glob.glob(os.path.join(self.tmp, "**", "*"), recursive=True):
            if os.path.isfile(p) and not p.endswith((".docx", ".jpg")):
                blob = self._read(p).decode("utf-8", errors="replace")
                self.assertNotIn(SECRET, blob, p)
                self.assertNotIn("amazonaws", blob, p)


# Failure isolation: locked/unreadable local attachment, unsupported sibling block
class TestIsolation(AttBase):
    def setUp(self):
        super().setUp()
        self.upload_docx()
        self.assertEqual(self.run_sync()["status"], "ALL_APPLIED")      # 박멤버 Q4 DOCX synced
        self.state_after_adopt = self._read(self.state_path)
        self.md_after_adopt = self.md()
        self.locked = os.path.normcase(self.att_abs())

    def locked_sha(self):
        real = att.sha256_file

        def fake(path):
            if os.path.normcase(path) == self.locked:
                raise PermissionError(13, "Permission denied", path)  # Word holds the file
            return real(path)
        return mock.patch.object(att, "sha256_file", side_effect=fake)

    def test_locked_attachment_fails_closed_for_that_page_only_no_crash(self):
        with self.locked_sha():
            with self.assertRaises(ca.RenderUnsupported) as cm:
                self.classify()
            item = cm.exception.items[0]
            self.assertEqual(item["type"], "file:attachment_local_unreadable")
            self.assertNotIn(self.lavcca, item["detail"])                     # no absolute path
            self.assertIn("PermissionError", item["detail"])
            # a full production run with the locked page AND another page: no crash,
            # the other page is applied, the locked page untouched
            self.edit_remote(T)
            res = self.run_sync(self.upd(L, T), now=RUN_AT.replace(hour=5))
        self.assertEqual(res["status"], "ALL_APPLIED", res)
        self.assertEqual(res["applied"], [T])
        self.assertEqual(res["excluded"][0]["status"], "RENDER_UNSUPPORTED")
        self.assertEqual(self.md()[L_REL.replace("/", os.sep)], self.md_after_adopt[L_REL.replace("/", os.sep)])
        self.assertEqual(self.state()[att.STATE_KEY][FB], json.loads(self.state_after_adopt)[att.STATE_KEY][FB])
        self.assertEqual(self._read(self.att_abs()), make_docx())          # never overwritten/deleted
        # lock released -> next run verifies normally
        self.assertEqual(self.classify(), bl.UNCHANGED)

    def test_locked_page_new_attachments_committed_attachment_only(self):
        self.add_more = file_blk("ab" * 16, "NATAM IR 피칭 슬라이드.docx")
        self.api.children[L] = list(self.api.children[L]) + [self.add_more, image_blk()]
        self.api.pages[L]["last_edited_time"] = "2026-10-05T00:00:00.000Z"
        rows = [{"notion_id": L, "status": "RENDER_UNSUPPORTED",
                 "attachment_plans": [{"action": "download"}]}]
        with self.locked_sha():
            res = self.run_sync(rows, http=FakeHTTP((200, None, make_docx("IR")), (200, None, JPEG)),
                                now=RUN_AT.replace(hour=6))
        self.assertEqual(res["status"], "ALL_APPLIED", res)
        self.assertEqual((res["applied"], res["attachment_only_pages"]), ([], [L]))
        self.assertEqual(res["safe"][0]["page_render_blocked_by"][0]["type"], "file:attachment_local_unreadable")
        self.assertEqual(self.md(), self.md_after_adopt)                     # markdown NOT written
        st = self.state()
        self.assertIn("ab" * 16, st[att.STATE_KEY])
        self.assertIn(IB, st[att.STATE_KEY])
        self.assertEqual(st["items"][L], json.loads(self.state_after_adopt)["items"][L])  # page baseline untouched
        self.assertTrue(os.path.isfile(self.att_abs("03_MEMBER_B/Imported-Notion/Attachments/NATAM IR 피칭 슬라이드.docx")))
        self.assertTrue(os.path.isfile(self.att_abs(IMG_REL)))
        # lock released: the page renders, reuses both new files (no second download) and gets its links
        self.assertEqual(self.classify(), bl.UPDATE_CONFIRMED)
        res = self.run_sync(now=RUN_AT.replace(hour=7))
        self.assertEqual(res["status"], "ALL_APPLIED", res)
        self.assertEqual(self.http.urls, [])
        text = self._read(self.l_path).decode("utf-8")
        self.assertIn("(<Attachments/NATAM IR 피칭 슬라이드.docx>)", text)
        self.assertIn(f"![](Attachments/notion-image-{IB}.jpg)", text)
        self.assertEqual(self.classify(), bl.UNCHANGED)

    def test_unsupported_sibling_block_does_not_block_supported_docx(self):
        self.api.children[L] = list(self.api.children[L]) + [file_blk("ab" * 16, "새 문서.docx"),
                                                             file_blk("cd" * 16, "표.xlsx")]
        self.api.pages[L]["last_edited_time"] = "2026-10-05T00:00:00.000Z"
        rows = [{"notion_id": L, "status": "RENDER_UNSUPPORTED", "attachment_plans": [{"action": "download"}]}]
        res = self.run_sync(rows, now=RUN_AT.replace(hour=6))
        self.assertEqual(res["status"], "ALL_APPLIED", res)
        self.assertEqual(res["attachment_only_pages"], [L])
        self.assertEqual(res["safe"][0]["page_render_blocked_by"], [{"type": "file:notion_hosted_unresolved",
                                                                     "id": "cd" * 16}])
        self.assertTrue(os.path.isfile(self.att_abs("03_MEMBER_B/Imported-Notion/Attachments/새 문서.docx")))
        self.assertEqual(self.md(), self.md_after_adopt)    # page with an unsupported block is not written

    def test_attachment_only_selection_is_status_independent_but_needs_a_transfer(self):
        # attachment discovery must not depend on the page being written this run
        for st in ("UNCHANGED", "PATH_POLICY_UNRESOLVED", "LOCAL_MODIFICATION_CONFLICT", "CONTENT_HASH_ANOMALY",
                   "METADATA_ONLY_CHANGE", "RENDER_UNSUPPORTED", "UPDATE_UNVERIFIED"):
            for action in ("download", "refresh", "redownload"):
                rows = [{"notion_id": L, "status": st, "attachment_plans": [{"action": action}]}]
                self.assertEqual(prod.select_attachment_only_candidates(rows), [L], (st, action))
        self.assertEqual(prod.select_attachment_only_candidates(
            [{"notion_id": L, "status": "RENDER_UNSUPPORTED", "attachment_plans": [{"action": "reuse"}]}]), [])

    def test_unreadable_original_during_refresh_is_page_scoped(self):
        self.api.children[L][-1]["last_edited_time"] = "2026-10-05T00:00:00.000Z"
        self.api.children[L].insert(0, blk("9" * 31 + "8", "paragraph", {"rich_text": [rt("추가")]}))
        self.api.pages[L]["last_edited_time"] = "2026-10-05T00:00:00.000Z"
        real_open = open

        def guarded_open(p, *a, **kw):
            if os.path.normcase(str(p)) == self.locked and a and "r" in a[0] and "b" in a[0]:
                raise PermissionError(13, "Permission denied")
            return real_open(p, *a, **kw)
        real_sha = att.sha256_file
        recorded = ca.sha256_bytes(make_docx())

        def sha(p):  # the locked file still verifies as intact at planning (reuse -> refresh)
            return recorded if os.path.normcase(p) == self.locked else real_sha(p)
        with mock.patch("builtins.open", side_effect=guarded_open), \
                mock.patch.object(att, "sha256_file", side_effect=sha):
            res = self.run_sync(http=FakeHTTP((200, None, make_docx("v2"))), now=RUN_AT.replace(hour=8))
        self.assertEqual(res["excluded"][0]["status"], "EXCLUDED_ATTACHMENT_DOWNLOAD_FAILED", res)
        self.assertIn("LOCAL_UNREADABLE", res["excluded"][0]["reason"])
        self.assertEqual(self._read(self.att_abs()), make_docx())


# Architecture-audit regressions (D1, D2, D3, D5)
class TestAudit(AttBase):
    def test_D1_parent_title_only_difference_updates_in_place(self):
        self.api.children[L].append(blk("9" * 31 + "a", "paragraph", {"rich_text": [rt("DB 행 수정")]}))
        self.api.pages[L]["last_edited_time"] = EDITED_2
        row = {"notion_id": L, "status": "PATH_POLICY_UNRESOLVED", "path_policy": prod.PARENT_ONLY_PATH_POLICY,
               "content_status": "UPDATE_CONFIRMED"}
        self.assertEqual(prod.select_update_candidates([dict(row, path_policy=None)]), [])   # title changed etc.
        self.assertEqual(prod.select_update_candidates([dict(row, content_status="UNCHANGED")]), [])
        res = self.run_sync([row])
        self.assertEqual((res["status"], res["applied"]), ("ALL_APPLIED", [L]), res)
        self.assertIn("DB 행 수정", self._read(self.l_path).decode("utf-8"))   # same file, updated in place
        self.assertEqual(self.state()["items"][L]["obsidian_path"], L_REL)

    def test_D2_conflict_page_still_receives_new_attachment_file(self):
        with open(self.l_path, "a", encoding="utf-8", newline="\n") as f:
            f.write("\n사람이 고친 문장\n")
        edited = self._read(self.l_path)
        self.upload_docx()
        rows = [{"notion_id": L, "status": "LOCAL_MODIFICATION_CONFLICT", "attachment_plans": [{"action": "download"}]}]
        res = self.run_sync(rows)
        self.assertEqual((res["status"], res["attachment_only_pages"]), ("ALL_APPLIED", [L]), res)
        self.assertEqual(self._read(self.l_path), edited)          # the local edit is never overwritten
        self.assertTrue(os.path.isfile(self.att_abs()))
        self.assertIn(FB, self.state()[att.STATE_KEY])

    def test_D3_same_name_file_replaced_in_block_is_refreshed_with_backup(self):
        self.upload_docx()
        old = make_docx("v1")
        self.assertEqual(self.run_sync(http=FakeHTTP((200, None, old)))["status"], "ALL_APPLIED")
        md = self._read(self.l_path)
        self.api.children[L][-1]["last_edited_time"] = "2026-10-06T00:00:00.000Z"   # file replaced in Notion
        self.api.pages[L]["last_edited_time"] = "2026-10-06T00:00:00.000Z"
        self.assertEqual(self.classify(), bl.METADATA_ONLY_CHANGE)                   # page body identical
        new = make_docx("v2")
        rows = [{"notion_id": L, "status": "METADATA_ONLY_CHANGE", "live_last_edited_time": "2026-10-06T00:00:00.000Z",
                 "attachment_plans": [{"action": "refresh"}]}]
        later = RUN_AT.replace(hour=9)
        res = self.run_sync(rows, http=FakeHTTP((200, None, new)), now=later)
        self.assertEqual((res["status"], res["attachment_only_pages"]), ("ALL_APPLIED", [L]), res)
        self.assertEqual(self._read(self.att_abs()), new)
        self.assertEqual(self._read(self.l_path), md)                               # link unchanged
        backup = os.path.join(self.sync, "backups", later.strftime("%Y%m%dT%H%M%SZ"), *ATT_REL.split("/"))
        self.assertEqual(self._read(backup), old)
        self.assertEqual(self.state()[att.STATE_KEY][FB]["content_sha256"], ca.sha256_bytes(new))

    def test_D5_name_reuse_only_for_blocks_that_existed_at_migration(self):
        body = "\n# 박멤버\n\n[옛 문서.docx](옛_문서.docx)\n"
        planner = att.AttachmentPlanner(self.lavcca, {}, "2026-09-30T22:11:23+09:00")
        old_block = file_blk("0a" * 16, "옛 문서.docx", edited="2026-09-01T00:00:00.000Z")
        old_block["created_time"] = "2026-09-01T00:00:00.000Z"
        new_block = file_blk("0b" * 16, "옛 문서.docx")
        new_block["created_time"] = "2026-10-04T12:57:00.000Z"
        pa = planner.for_page(L, L_REL, "박멤버")
        out = ca.render_blocks([old_block, new_block], ca.RenderContext({}, body, pa))
        self.assertEqual(out.split("\n\n"), ["[옛 문서.docx](옛_문서.docx)",                 # migrated: legacy link
                                             "[옛 문서.docx](<Attachments/옛 문서.docx>)"])  # new upload: own file
        self.assertEqual([p["block_id"] for p in pa.plans], ["0b" * 16])


# 16
class TestSchedulerCompatibility(unittest.TestCase):
    SYNC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_production_command_unchanged(self):
        self.assertEqual(ca.resolve_cli_mode(False, None, apply_updates=True)[0], "apply_updates_write")
        with open(os.path.join(self.SYNC, "notion_sync.py"), encoding="utf-8") as f:
            src = f.read()
        flags = set(re.findall(r'add_argument\("(--[a-z-]+)"', src))
        self.assertEqual(flags, {"--apply", "--notion-id", "--adopt-baseline", "--apply-updates", "--dry-run",
                                 "--targets"})  # optional incremental mode; the scheduled command is unchanged
        self.assertIn("import attachment_sync", src)

    def test_wrapper_still_runs_apply_updates(self):
        ps1 = os.path.join(self.SYNC, "run_sync.ps1")
        if not os.path.exists(ps1):
            self.skipTest("run_sync.ps1 not present next to the code")
        with open(ps1, encoding="utf-8") as f:
            self.assertIn('@("notion_sync.py", "--apply-updates")', f.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
