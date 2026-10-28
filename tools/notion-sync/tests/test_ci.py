# -*- coding: utf-8 -*-
"""Runner-side logic: change detection, engine fallback, mirror audit, verified OneDrive upload."""
import os, sys, json, hashlib, shutil, tempfile, unittest
from urllib.parse import unquote

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CODE_DIR)
import canary_apply as ca  # noqa: E402
from ci import plan, graph  # noqa: E402

A, B, NEW = "a1" * 16, "b1" * 16, "c9" * 16
T0, T1, CURSOR = "2026-10-28T01:00:00.000Z", "2026-10-28T02:00:00.000Z", "2026-10-28T01:30:00Z"
STATE = {A: {"notion_last_edited_time": T0, "last_seen_edited_time": T0},
         B: {"notion_last_edited_time": T0, "last_seen_edited_time": T0}}
CFG = {"excluded_ids": ["ee" * 16], "review_ids": []}


def pg(i, t, **kw):
    return {"object": "page", "id": i, "last_edited_time": t, **kw}


class TestPlanRun(unittest.TestCase):
    def run_plan(self, results, **kw):
        return plan.plan_run(results, STATE, kw.pop("cursor", CURSOR), CFG, **kw)

    def test_edited_known_page_is_incremental(self):
        self.assertEqual(self.run_plan([pg(A.upper(), T1)]), ("incremental", [A], []))

    def test_dashed_ids_and_duplicate_webhook_hints_collapse(self):
        dashed = f"{A[:8]}-{A[8:12]}-{A[12:16]}-{A[16:20]}-{A[20:]}"
        mode, targets, _ = self.run_plan([pg(dashed, T1)], hinted_ids=[A, A, dashed])
        self.assertEqual((mode, targets), ("incremental", [A]))

    def test_nothing_new_is_none(self):
        self.assertEqual(self.run_plan([pg(A, T0)])[0], "none")
        self.assertEqual(self.run_plan([])[0], "none")

    def test_already_synced_time_is_not_a_target(self):
        res = plan.plan_run([pg(A, T0)], {A: {"notion_last_edited_time": T0}}, "2026-10-28T01:02:00Z", CFG)
        self.assertEqual(res[0], "none")

    def test_overlap_window_rereads_minute_rounded_edit(self):
        # edited 3 min before the cursor (same rounded minute as the previous run): still checked
        self.assertEqual(self.run_plan([pg(A, "2026-10-28T01:27:00.000Z")])[1], [A])

    def test_stops_at_cursor_minus_overlap(self):
        old = pg(NEW, "2026-10-28T01:00:00.000Z")  # older than the window: must not force a full run
        self.assertEqual(self.run_plan([pg(A, T1), old])[:2], ("incremental", [A]))

    def test_new_trashed_database_and_structural_events_force_full(self):
        for results, kw in (([pg(NEW, T1)], {}),
                            ([pg(A, T1, in_trash=True)], {}),
                            ([{"object": "database", "id": NEW, "last_edited_time": T1}], {}),
                            ([], {"event_types": ["page.moved"]}),
                            ([], {"event_types": ["data_source.schema_updated"]}),
                            ([], {"hinted_ids": [NEW]})):
            self.assertEqual(self.run_plan(results, **kw)[0], "full", (results, kw))

    def test_no_cursor_is_full(self):
        self.assertEqual(self.run_plan([pg(A, T1)], cursor=None)[0], "full")

    def test_excluded_pages_ignored(self):
        self.assertEqual(self.run_plan([pg("ee" * 16, T1)])[0], "none")

    def test_too_many_targets_is_full(self):
        many = {f"{i:032x}": {"notion_last_edited_time": T0} for i in range(plan.MAX_TARGETS + 1)}
        res = plan.plan_run([pg(i, T1) for i in many], many, CURSOR, CFG)
        self.assertEqual(res[0], "full")


class TestExecute(unittest.TestCase):
    def fake(self, codes):
        calls = []

        def run(args):
            if "--targets" in args:
                with open(args[args.index("--targets") + 1], encoding="utf-8") as f:
                    calls.append(("targets", json.load(f)))
            else:
                calls.append(("full",))
            return codes.pop(0)
        return run, calls

    def test_none_runs_nothing(self):
        run, calls = self.fake([])
        self.assertEqual(plan.execute("none", [], run), (0, "none"))
        self.assertEqual(calls, [])

    def test_incremental_success(self):
        run, calls = self.fake([0])
        self.assertEqual(plan.execute("incremental", [A], run), (0, "incremental"))
        self.assertEqual(calls, [("targets", [A])])

    def test_exit_9_falls_back_to_full(self):
        run, calls = self.fake([9, 0])
        self.assertEqual(plan.execute("incremental", [A], run), (0, "full"))
        self.assertEqual(calls, [("targets", [A]), ("full",)])

    def test_incremental_error_stops_without_full_run(self):
        for code in (2, 7, 8):  # crawl/API failure, lock held, apply failure
            run, calls = self.fake([code])
            self.assertEqual(plan.execute("incremental", [A], run), (code, "incremental"))
            self.assertEqual(len(calls), 1)

    def test_targets_file_removed_even_if_engine_raises(self):
        seen = []

        def boom(args):
            seen.append(args[args.index("--targets") + 1])
            raise RuntimeError("engine crashed")
        with self.assertRaises(RuntimeError):
            plan.execute("incremental", [A], boom)
        self.assertFalse(os.path.exists(seen[0]))


class TestAuditMirror(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="audit_")
        self.body = "\n본문\n"
        os.makedirs(os.path.join(self.root, "02", "Attachments"))
        with open(os.path.join(self.root, "02", "p.md"), "w", encoding="utf-8", newline="\n") as f:
            f.write("---\nmanaged: notion\n---\n" + self.body)
        with open(os.path.join(self.root, "02", "Attachments", "a.docx"), "wb") as f:
            f.write(b"docx")
        self.state = {"items": {A: {"obsidian_path": "02/p.md", "local_content_hash": ca.sha256_text(self.body)}},
                      "attachments": {"x": {"local_path": "02/Attachments/a.docx",
                                            "content_sha256": ca.sha256_bytes(b"docx")}}}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_consistent(self):
        self.assertEqual(plan.audit_mirror(self.root, self.state), [])

    def test_state_claims_sync_but_file_unchanged(self):
        self.state["items"][A]["local_content_hash"] = ca.sha256_text("\n새 본문\n")
        self.state["attachments"]["x"]["content_sha256"] = ca.sha256_bytes(b"new")
        problems = {p["path"]: p["problem"] for p in plan.audit_mirror(self.root, self.state)}
        self.assertIn("body hash", problems["02/p.md"])
        self.assertIn("attachment hash", problems["02/Attachments/a.docx"])

    def test_missing_and_malformed(self):
        os.remove(os.path.join(self.root, "02", "Attachments", "a.docx"))
        with open(os.path.join(self.root, "02", "p.md"), "w", encoding="utf-8") as f:
            f.write("no frontmatter")
        problems = {p["path"]: p["problem"] for p in plan.audit_mirror(self.root, self.state)}
        self.assertEqual(problems["02/Attachments/a.docx"], "missing")
        self.assertTrue(problems["02/p.md"].startswith("unreadable"))


class FakeDrive:
    """Graph transport: upload sessions, chunked PUT, item GET with sha1Hash + eTag."""

    def __init__(self):
        self.files, self.calls = {}, []      # path -> {"id","data","etag"}
        self.sessions, self.seq = {}, 0
        self.ignore_writes = False            # simulate "success" that does not change the file
        self.fail_first = []                  # statuses to answer before serving normally
        self.no_sha1 = False
        self.fail_upload = set()              # paths whose upload session always answers 503
        self.corrupt_download = set()         # paths whose downloadUrl serves wrong bytes

    def put(self, path, data, mtime="2026-01-01T00:00:00Z"):
        self.seq += 1
        self.files[path] = {"id": f"id{self.seq}", "data": data, "etag": f"e{self.seq}", "mtime": mtime}

    def meta(self, path):
        f = self.files[path]
        hashes = {} if self.no_sha1 else {"sha1Hash": hashlib.sha1(f["data"]).hexdigest().upper()}
        return {"id": f["id"], "name": path.rsplit("/", 1)[-1], "eTag": f["etag"], "size": len(f["data"]),
                "file": {"hashes": hashes}, "fileSystemInfo": {"lastModifiedDateTime": f.get("mtime", "2026-01-01T00:00:00Z")},
                "@microsoft.graph.downloadUrl": f"https://dl/{f['id']}"}

    def children(self, folder):
        prefix = folder.strip("/") + "/" if folder.strip("/") else ""
        names = {}
        for p in self.files:
            if p.startswith(prefix):
                head, sep, _ = p[len(prefix):].partition("/")
                names[head] = {"name": head, "folder": {}} if sep else self.meta(p)
        return list(names.values())

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers)))
        if self.fail_first:
            return self.fail_first.pop(0), {"Retry-After": "1"}, b'{"error":"busy"}'
        js = lambda code, obj: (code, {}, json.dumps(obj).encode())  # noqa: E731
        if url.startswith("https://dl/"):
            assert "Authorization" not in headers
            for p, f in self.files.items():
                if f["id"] == url.rsplit("/", 1)[1]:
                    return 200, {}, (b"tampered" if p in self.corrupt_download else f["data"])
            return 404, {}, b""
        if url.endswith("/root/children?$top=200"):
            return js(200, {"value": self.children("")})
        if ":/children?" in url:
            return js(200, {"value": self.children(unquote(url.split("/root:/", 1)[1].rsplit(":/children", 1)[0]))})
        if url.startswith("https://up/"):
            assert "Authorization" not in headers
            sid = url.rsplit("/", 1)[1]
            s = self.sessions[sid]
            rng = headers["Content-Range"].split()[1]
            start, total = int(rng.split("-")[0]), int(rng.split("/")[1])
            assert start == len(s["buf"]), "fragments out of order"
            s["buf"] += body
            if len(s["buf"]) < total:
                return js(202, {"nextExpectedRanges": [f"{len(s['buf'])}-"]})
            path = s["path"]
            if path not in self.files:
                self.seq += 1
                self.files[path] = {"id": f"id{self.seq}", "data": b"", "etag": "e0"}
            if not self.ignore_writes:
                f = self.files[path]
                f["data"], f["etag"] = bytes(s["buf"]), f"e{self.seq}{len(s['buf'])}{f['etag']}"
            return js(201, self.meta(path))
        path = unquote(url.split("/root:/", 1)[1].rsplit(":", 1)[0])
        if method == "GET":
            return js(200, self.meta(path)) if path in self.files else js(404, {"error": "itemNotFound"})
        if method == "DELETE":
            if path not in self.files:
                return js(404, {})
            if headers.get("If-Match") != self.files[path]["etag"]:
                return js(412, {"error": {"code": "resourceModified"}})
            del self.files[path]
            return 204, {}, b""
        if path in self.fail_upload:
            return js(503, {"error": "unavailable"})
        if url.endswith(":/createUploadSession"):
            cb = json.loads(body)["item"]["@microsoft.graph.conflictBehavior"]
            if path in self.files and cb == "fail":
                return js(409, {"error": {"code": "nameAlreadyExists"}})
            if "If-Match" in headers and (path not in self.files or self.files[path]["etag"] != headers["If-Match"]):
                return js(412, {"error": {"code": "resourceModified"}})
            sid = str(len(self.sessions))
            self.sessions[sid] = {"path": path, "buf": bytearray()}
            return js(200, {"uploadUrl": f"https://up/{sid}"})
        return js(400, {})


class TestGraphUpload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="graph_")
        self.drive = FakeDrive()
        self.sleeps = []
        self.g = graph.Graph("tok", http=self.drive, sleep=self.sleeps.append, chunk=5)
        self.remote = "문서/Obsidian Vault/LAVCCA/02 폴더/p.md"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def local(self, data):
        p = os.path.join(self.tmp, "f.bin")
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_create_chunked_and_verified(self):
        item = self.g.upload_verified(self.remote, self.local(b"12345678901"))   # 3 fragments of <=5 bytes
        self.assertEqual(self.drive.files[self.remote]["data"], b"12345678901")
        self.assertEqual(item["file"]["hashes"]["sha1Hash"], graph.sha1_file(self.local(b"12345678901")))
        self.assertEqual(sum(1 for c in self.drive.calls if c[0] == "PUT"), 3)

    def test_replace_with_current_etag(self):
        self.g.upload_verified(self.remote, self.local(b"old"))
        etag = self.drive.files[self.remote]["etag"]
        self.g.upload_verified(self.remote, self.local(b"new!"), if_match=etag)
        self.assertEqual(self.drive.files[self.remote]["data"], b"new!")

    def test_stale_etag_412_writes_nothing(self):
        self.g.upload_verified(self.remote, self.local(b"old"))
        with self.assertRaises(graph.PreconditionFailed):
            self.g.upload_verified(self.remote, self.local(b"new"), if_match="stale")
        self.assertEqual(self.drive.files[self.remote]["data"], b"old")

    def test_create_over_existing_409(self):
        self.g.upload_verified(self.remote, self.local(b"old"))
        with self.assertRaises(graph.Conflict):
            self.g.upload_verified(self.remote, self.local(b"new"))
        self.assertEqual(self.drive.files[self.remote]["data"], b"old")

    def test_success_response_but_file_unchanged_detected(self):
        self.g.upload_verified(self.remote, self.local(b"old"))
        self.drive.ignore_writes = True
        with self.assertRaises(graph.VerifyError):
            self.g.upload_verified(self.remote, self.local(b"new"), if_match=self.drive.files[self.remote]["etag"])

    def test_missing_sha1_fails_closed(self):
        self.drive.no_sha1 = True
        with self.assertRaises(graph.VerifyError):
            self.g.upload_verified(self.remote, self.local(b"x"))

    def test_throttled_then_ok(self):
        self.drive.fail_first = [429, 503]
        self.g.upload_verified(self.remote, self.local(b"x"))
        self.assertEqual(self.sleeps, [1, 1])

    def test_persistent_5xx_raises(self):
        self.drive.fail_first = [503] * 4
        with self.assertRaises(graph.GraphError):
            self.g.upload_verified(self.remote, self.local(b"x"))
        self.assertNotIn(self.remote, self.drive.files)

    def test_token_only_on_graph_calls(self):
        self.g.upload_verified(self.remote, self.local(b"123456"))
        for method, url, h in self.drive.calls:
            self.assertEqual("Authorization" in h, not url.startswith("https://up/"), url)


if __name__ == "__main__":
    unittest.main(verbosity=2)


from ci import run as cirun  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

NOW = datetime(2026, 10, 28, 3, 0, tzinfo=timezone.utc)


class TestSearchSince(unittest.TestCase):
    def post_seq(self, responses):
        calls = []

        def post(url, token, version, body):
            calls.append(body)
            return responses.pop(0)
        return post, calls

    def test_pages_until_older_than_since(self):
        since = plan._ts("2026-10-28T01:00:00Z")
        post, calls = self.post_seq([
            (200, {"results": [pg(A, T1)], "has_more": True, "next_cursor": "c2"}),
            (200, {"results": [pg(B, T0), pg(NEW, "2026-10-27T00:00:00.000Z")], "has_more": True, "next_cursor": "c3"})])
        out = cirun.search_since("t", "v", since, post=post, sleep=lambda s: None)
        self.assertEqual([o["id"] for o in out], [A, B, NEW])
        self.assertEqual(calls[1]["start_cursor"], "c2")
        self.assertEqual(len(calls), 2)                       # stopped without fetching c3

    def test_retries_then_gives_up_with_none(self):
        post, _ = self.post_seq([(429, {}), (503, {}), (200, {"results": [], "has_more": False})])
        self.assertEqual(cirun.search_since("t", "v", NOW, post=post, sleep=lambda s: None), [])
        post, _ = self.post_seq([(None, {})] * 4)
        self.assertIsNone(cirun.search_since("t", "v", NOW, post=post, sleep=lambda s: None))

    def test_too_many_pages_is_none(self):
        page = (200, {"results": [pg(A, T1)], "has_more": True, "next_cursor": "x"})
        post, _ = self.post_seq([page] * cirun.SEARCH_MAX_PAGES)
        self.assertIsNone(cirun.search_since("t", "v", plan._ts(T0), post=post, sleep=lambda s: None))


class TestRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cirun_")
        self.lavcca = os.path.join(self.tmp, "LAVCCA")
        self.sync = os.path.join(self.lavcca, "_sync")
        os.makedirs(os.path.join(self.lavcca, "02"))
        os.makedirs(self.sync)
        body = "\n본문\n"
        with open(os.path.join(self.lavcca, "02", "a.md"), "w", encoding="utf-8", newline="\n") as f:
            f.write("---\nmanaged: notion\n---\n" + body)
        self.state = {"items": {A: {"obsidian_path": "02/a.md", "local_content_hash": ca.sha256_text(body),
                                    "notion_last_edited_time": T0, "last_seen_edited_time": T0}}}
        self.dump("state.json", self.state)
        self.dump("config.json", {"lavcca_root_path": self.lavcca, "excluded_ids": [], "review_ids": [], "owner_roots": {}})
        self.engine_calls = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def dump(self, name, obj):
        with open(os.path.join(self.sync, name), "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def cursor(self):
        return cirun._load(os.path.join(self.sync, cirun.CURSOR_FILE), {}).get("cursor")

    def go(self, results=(), codes=(0,), event=None, full=False):
        codes = list(codes)

        def engine(args):
            self.engine_calls.append("--targets" if "--targets" in args else "full")
            return codes.pop(0)
        return cirun.run(self.sync, event, full, search=lambda since: list(results) if results is not None else None,
                         run_engine=engine, now=lambda: NOW)

    def test_first_run_full_then_cursor(self):
        code, rep = self.go()
        self.assertEqual((code, rep["mode"]), (0, "full"))
        self.assertEqual(self.cursor(), "2026-10-28T03:00:00Z")

    def test_incremental_from_search_and_webhook_hint(self):
        self.dump(cirun.CURSOR_FILE, {"cursor": "2026-10-28T01:30:00Z"})
        code, rep = self.go(results=[], event={"inputs": {"hint": json.dumps({"ids": [A], "types": ["page.content_updated"]})}})
        self.assertEqual((code, rep["mode"], rep["targets"]), (0, "incremental", [A]))
        self.assertEqual(self.engine_calls, ["--targets"])

    def test_engine_failure_keeps_cursor(self):
        self.dump(cirun.CURSOR_FILE, {"cursor": "2026-10-28T01:30:00Z"})
        code, _ = self.go(results=[pg(A, T1)], codes=[2])
        self.assertEqual(code, 2)
        self.assertEqual(self.cursor(), "2026-10-28T01:30:00Z")

    def test_search_unavailable_runs_full(self):
        self.dump(cirun.CURSOR_FILE, {"cursor": "2026-10-28T01:30:00Z"})
        code, rep = self.go(results=None)
        self.assertEqual((code, rep["mode"]), (0, "full"))

    def test_nothing_changed_runs_no_engine_but_advances_cursor(self):
        self.dump(cirun.CURSOR_FILE, {"cursor": "2026-10-28T01:30:00Z"})
        code, rep = self.go(results=[], codes=[])
        self.assertEqual((code, rep["mode"], self.engine_calls), (0, "none", []))
        self.assertEqual(self.cursor(), "2026-10-28T03:00:00Z")

    def test_audit_mismatch_exit_10(self):
        self.state["items"][A]["local_content_hash"] = ca.sha256_text("다른 본문")
        self.dump("state.json", self.state)
        code, rep = self.go(full=True)
        self.assertEqual(code, cirun.EXIT_AUDIT_MISMATCH)
        self.assertEqual(rep["audit"][0]["path"], "02/a.md")


class TestWebhookHint(unittest.TestCase):
    def test_valid_hint(self):
        ev = {"inputs": {"hint": json.dumps({"ids": [A, "0ef104cd-477e-80e1-8571-cfd10e92339a"], "types": ["page.moved"]})}}
        self.assertEqual(cirun.webhook_hint(ev), ([A, "0ef104cd-477e-80e1-8571-cfd10e92339a"], ["page.moved"]))

    def test_hostile_or_broken_hint_ignored(self):
        for raw in ("{", "[1,2]", json.dumps({"ids": ["../../etc", 5, "a" * 40], "types": ["$(rm -rf)", "page.created"]})):
            ids, types = cirun.webhook_hint({"inputs": {"hint": raw}})
            self.assertEqual(ids, [])
            self.assertIn(types, ([], ["page.created"]))
        self.assertEqual(cirun.webhook_hint(None), ([], []))
        self.assertEqual(cirun.webhook_hint({"inputs": {}}), ([], []))

    def test_engine_output_goes_to_log_file_not_stdout(self):
        d = tempfile.mkdtemp(prefix="eng_")
        try:
            with open(os.path.join(d, "notion_sync.py"), "w", encoding="utf-8") as f:
                f.write("print('비공개 페이지 제목')\nimport sys; sys.exit(4)\n")
            import subprocess as sp
            out = sp.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {CODE_DIR!r}); from ci import run; "
                          f"sys.exit(run.engine_runner({d!r})(['--apply-updates']))"], capture_output=True, text=True)
            self.assertEqual(out.returncode, 4)
            self.assertNotIn("비공개", out.stdout + out.stderr)
            logs = os.listdir(os.path.join(d, "logs"))
            with open(os.path.join(d, "logs", logs[0]), encoding="utf-8") as f:
                self.assertIn("비공개 페이지 제목", f.read())
        finally:
            shutil.rmtree(d, ignore_errors=True)
