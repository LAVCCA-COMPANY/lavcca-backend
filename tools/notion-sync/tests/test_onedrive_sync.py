# -*- coding: utf-8 -*-
"""Runner <-> OneDrive: lock (incl. orphan takeover), sha1-verified mirror restore, upload of exactly
what the engine changed, and failure handling. Fake Graph only; no network, no real vault."""
import os, sys, json, shutil, tempfile, unittest
from unittest import mock

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CODE_DIR)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ci import graph, onedrive_sync as ods  # noqa: E402
from test_ci import FakeDrive  # noqa: E402

R, ROOT = "Docs/Vault", "LAVCCA"
LOCK = f"{R}/{ROOT}/_sync/.sync.lock"
NOW = "2026-10-28T03:00:00Z"
CONFIG = {"notion_version": "2025-09-03", "natam_root_id": "a0" * 16, "owner_roots": {}, "excluded_ids": []}


def seed(drive):
    drive.put(f"{R}/{ROOT}/02/a.md", b"---\nmanaged: notion\n---\nA\n", mtime="2025-12-31T00:00:00Z")
    drive.put(f"{R}/{ROOT}/02/Attachments/x.docx", b"docx")
    drive.put(f"{R}/{ROOT}/_sync/state.json", b'{"items": {}}')
    drive.put(f"{R}/{ROOT}/_sync/backups/old/state.json", b"old backup")
    drive.put(f"{R}/{ROOT}/_sync/logs/old.json", b"{}")
    drive.put(f"{R}/{ROOT}/_sync/notion_sync.py", b"stale engine copy")
    drive.put(f"{R}/Daily/2026-10-28.md", b"personal note")
    drive.put(f"{R}/Daily/photo.png", b"png")
    drive.put(f"{R}/.obsidian/app.json", b"{}")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ods_")
        self.mirror = os.path.join(self.tmp, "mirror")
        self.drive = FakeDrive()
        seed(self.drive)
        self.g = graph.Graph("tok", http=self.drive, sleep=lambda s: None)
        self.scope = ods.Scope(ROOT)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def local(self, rel):
        return os.path.join(self.mirror, *rel.split("/"))

    def read(self, rel):
        with open(self.local(rel), "rb") as f:
            return f.read()

    def write(self, rel, data):
        os.makedirs(os.path.dirname(self.local(rel)), exist_ok=True)
        with open(self.local(rel), "wb") as f:
            f.write(data)

    def remote(self, rel):
        return self.drive.files[f"{R}/{rel}"]["data"]

    def uploads(self):
        return [u.split("/root:/", 1)[1].rsplit(":/createUploadSession", 1)[0]
                for m, u, h in self.drive.calls if u.endswith(":/createUploadSession")]


class TestLock(Base):
    def lock(self):
        return json.loads(self.drive.files[LOCK]["data"])

    def test_acquire_and_release(self):
        etag, note = ods.acquire_lock(self.g, LOCK, "run1", NOW)
        self.assertIsNone(note)
        self.assertEqual((self.lock()["host"], self.lock()["state"], self.lock()["run_id"]), ("github-actions", "running", "run1"))
        self.g.delete(LOCK, etag)
        self.assertNotIn(LOCK, self.drive.files)

    def test_windows_lock_is_never_taken(self):
        self.drive.put(LOCK, json.dumps({"pid": 33012, "host": "DESKTOP-EXAMPLE", "run_id": "20261019T151405Z"}).encode())
        before = self.drive.files[LOCK]["data"]
        with self.assertRaises(ods.Locked):
            ods.acquire_lock(self.g, LOCK, "run1", NOW)
        self.assertEqual(self.drive.files[LOCK]["data"], before)

    def test_orphan_running_lock_taken_over(self):
        self.drive.put(LOCK, json.dumps({"host": "github-actions", "run_id": "killed", "state": "running"}).encode())
        _, note = ods.acquire_lock(self.g, LOCK, "run2", NOW)
        self.assertIn("killed", note)
        self.assertEqual(self.lock()["run_id"], "run2")

    def test_upload_failed_lock_blocks(self):
        self.drive.put(LOCK, json.dumps({"host": "github-actions", "run_id": "r", "state": "upload_failed"}).encode())
        with self.assertRaises(ods.Locked):
            ods.acquire_lock(self.g, LOCK, "run3", NOW)

    def test_garbage_lock_blocks(self):
        self.drive.put(LOCK, b"not json")
        with self.assertRaises(ods.Locked):
            ods.acquire_lock(self.g, LOCK, "run4", NOW)


class TestMirror(Base):
    def test_restore_scope_hashes_and_mtime(self):
        manifest = ods.restore_mirror(self.g, R, self.mirror, self.scope)
        self.assertEqual(sorted(manifest), sorted([f"{ROOT}/02/a.md", f"{ROOT}/02/Attachments/x.docx",
                                                   f"{ROOT}/_sync/state.json", "Daily/2026-10-28.md"]))
        self.assertEqual(self.read(f"{ROOT}/02/a.md"), self.remote(f"{ROOT}/02/a.md"))
        self.assertEqual(os.path.getmtime(self.local(f"{ROOT}/02/a.md")), ods._iso_to_ts("2025-12-31T00:00:00Z"))
        for absent in (".obsidian/app.json", "Daily/photo.png", f"{ROOT}/_sync/backups/old/state.json",
                       f"{ROOT}/_sync/logs/old.json", f"{ROOT}/_sync/notion_sync.py"):
            self.assertFalse(os.path.exists(self.local(absent)), absent)

    def test_stale_cache_corrected_and_remote_deletions_applied(self):
        ods.restore_mirror(self.g, R, self.mirror, self.scope)
        self.write(f"{ROOT}/02/a.md", b"tampered cache")
        self.write(f"{ROOT}/02/gone.md", b"deleted on OneDrive since")
        self.write("Daily/photo.png", b"out of scope: left alone")
        gets = sum(1 for _, u, _ in self.drive.calls if u.startswith("https://dl/"))
        ods.restore_mirror(self.g, R, self.mirror, self.scope)
        self.assertEqual(self.read(f"{ROOT}/02/a.md"), self.remote(f"{ROOT}/02/a.md"))
        self.assertFalse(os.path.exists(self.local(f"{ROOT}/02/gone.md")))
        self.assertTrue(os.path.exists(self.local("Daily/photo.png")))
        self.assertEqual(sum(1 for _, u, _ in self.drive.calls if u.startswith("https://dl/")) - gets, 1)  # only a.md

    def test_quickxor_only_drive(self):
        self.drive.quickxor_only = True
        manifest = ods.restore_mirror(self.g, R, self.mirror, self.scope)
        self.assertTrue(all(m["hash"].startswith("quickxor:") for m in manifest.values()))
        self.drive.corrupt_download.add(f"{R}/{ROOT}/02/a.md")
        shutil.rmtree(self.mirror)
        with self.assertRaises(graph.VerifyError):
            ods.restore_mirror(self.g, R, self.mirror, self.scope)

    def test_corrupt_download_rejected(self):
        self.drive.corrupt_download.add(f"{R}/{ROOT}/02/a.md")
        with self.assertRaises(graph.VerifyError):
            ods.restore_mirror(self.g, R, self.mirror, self.scope)
        self.assertFalse(os.path.exists(self.local(f"{ROOT}/02/a.md")))


class TestSync(Base):
    """Whole job with a fake ci.run that edits the mirror the way the engine would."""

    def fake_ci(self, code=0, edit=True, delete=None):
        def run_ci(sync_dir, event, full, search=None, run_engine=None):
            self.seen_engine = run_engine
            if edit:
                self.write(f"{ROOT}/02/a.md", b"---\nmanaged: notion\n---\nA2\n")
                self.write(f"{ROOT}/_sync/backups/20261028T030000Z/02/a.md", b"---\nmanaged: notion\n---\nA\n")
                self.write(f"{ROOT}/_sync/state.json", b'{"items": {"a": 1}}')
                self.write(f"{ROOT}/_sync/incremental_cursor.json", b'{"cursor": "2026-10-28T03:00:00Z"}')
            if delete:
                os.remove(self.local(delete))
            return code, {"mode": "incremental", "targets": ["a"], "engine_exit": code, "audit": []}
        return run_ci

    def go(self, **kw):
        ci_kw = {k: kw.pop(k) for k in ("code", "edit", "delete") if k in kw}
        with mock.patch.object(ods.cirun, "engine_runner", lambda d: (lambda args: 0)):
            return ods.sync(self.g, R, self.mirror, ROOT, CODE_DIR, "run1", CONFIG, run_ci=self.fake_ci(**ci_kw), now=NOW, **kw)

    def test_changes_uploaded_in_order_and_lock_released(self):
        code, summary = self.go()
        self.assertEqual(code, 0, summary)
        self.assertEqual(self.remote(f"{ROOT}/02/a.md"), b"---\nmanaged: notion\n---\nA2\n")
        self.assertEqual(self.remote(f"{ROOT}/_sync/state.json"), b'{"items": {"a": 1}}')
        order = [u for u in self.uploads() if not u.endswith(".sync.lock")]
        self.assertEqual(order[0], f"{R}/{ROOT}/_sync/backups/20261028T030000Z/02/a.md")
        self.assertEqual(order[1], f"{R}/{ROOT}/02/a.md")
        self.assertTrue(order[2].startswith(f"{R}/{ROOT}/_sync/logs/ci_run_"))
        self.assertEqual(set(order[3:]), {f"{R}/{ROOT}/_sync/incremental_cursor.json", f"{R}/{ROOT}/_sync/state.json"})
        self.assertNotIn(LOCK, self.drive.files)
        self.assertNotIn(f"{R}/{ROOT}/_sync/config.json", self.drive.files)   # runner config never uploaded
        self.assertEqual(self.remote(f"{ROOT}/_sync/notion_sync.py"), b"stale engine copy")

    def test_no_change_run_writes_only_the_lock(self):
        code, summary = self.go(edit=False)
        self.assertEqual((code, summary["uploaded"]), (0, 1))                  # the ci_run log only
        self.assertEqual([u for u in self.uploads() if "/logs/" not in u], [LOCK])

    def test_concurrent_pc_edit_412_stops_and_keeps_lock(self):
        real = self.fake_ci()

        def run_ci(*a, **k):
            res = real(*a, **k)
            self.drive.put(f"{R}/{ROOT}/02/a.md", b"edited on a PC meanwhile")  # new eTag on OneDrive
            return res
        with mock.patch.object(ods.cirun, "engine_runner", lambda d: (lambda args: 0)):
            code, summary = ods.sync(self.g, R, self.mirror, ROOT, CODE_DIR, "run1", CONFIG, run_ci=run_ci, now=NOW)
        self.assertEqual(code, ods.EXIT_UPLOAD_PARTIAL, summary)
        self.assertEqual(self.remote(f"{ROOT}/02/a.md"), b"edited on a PC meanwhile")
        self.assertEqual(self.remote(f"{ROOT}/_sync/state.json"), b'{"items": {}}')     # state never advanced
        self.assertEqual(json.loads(self.drive.files[LOCK]["data"])["state"], "upload_failed")
        code, _ = self.go()                                                    # next run refuses
        self.assertEqual(code, ods.EXIT_LOCKED)

    def test_upload_outage_partial(self):
        self.drive.fail_upload.add(f"{R}/{ROOT}/02/a.md")
        code, _ = self.go()
        self.assertEqual(code, ods.EXIT_UPLOAD_PARTIAL)
        self.assertEqual(self.remote(f"{ROOT}/_sync/state.json"), b'{"items": {}}')

    def test_engine_failure_uploads_nothing(self):
        code, _ = self.go(code=2)
        self.assertEqual(code, 2)
        self.assertEqual(self.uploads(), [LOCK])
        self.assertNotIn(LOCK, self.drive.files)

    def test_engine_deleting_a_file_uploads_nothing(self):
        code, _ = self.go(delete=f"{ROOT}/02/Attachments/x.docx")
        self.assertEqual(code, ods.EXIT_ENGINE_DELETED)
        self.assertEqual(self.uploads(), [LOCK])
        self.assertIn(f"{R}/{ROOT}/02/Attachments/x.docx", self.drive.files)
        self.assertNotIn(LOCK, self.drive.files)

    def test_dry_run_uploads_nothing_and_engine_gets_dry_run_flag(self):
        code, summary = self.go(dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.uploads(), [LOCK])
        calls = []
        with mock.patch.object(ods.cirun, "engine_runner", lambda d: (lambda args: calls.append(args) or 0)):
            ods.sync(self.g, R, self.mirror, ROOT, CODE_DIR, "run2", CONFIG, run_ci=self.fake_ci(edit=False), now=NOW,
                     dry_run=True)
        self.seen_engine(["--apply-updates"])
        self.assertEqual(calls, [["--apply-updates", "--dry-run"]])

    def test_restore_failure_is_transient_and_releases_lock(self):
        self.drive.corrupt_download.add(f"{R}/{ROOT}/02/a.md")
        code, _ = self.go()
        self.assertEqual(code, ods.EXIT_TRANSIENT)
        self.assertNotIn(LOCK, self.drive.files)

    def test_runner_config_points_into_mirror_with_legacy_keys_normalized(self):
        self.go(edit=False)
        with open(os.path.join(self.mirror, ROOT, "_sync", "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertEqual(cfg["vault_root"], self.mirror)
        self.assertEqual(cfg["lavcca_root_path"], os.path.join(self.mirror, ROOT))
        self.assertNotIn("natam_root_path", cfg)


class TestTokenRefresh(unittest.TestCase):
    def http_seq(self, responses):
        def http(method, url, headers, body):
            self.assertNotIn(b"Authorization", body or b"")
            return responses.pop(0)
        return http

    def test_ok_rotates(self):
        http = self.http_seq([(200, {}, b'{"access_token": "AT", "refresh_token": "RT2"}')])
        self.assertEqual(graph.refresh_access_token("cid", "RT1", http=http), ("AT", "RT2"))

    def test_revoked_needs_reauth_without_retry(self):
        body = b'{"error": "invalid_grant", "error_codes": [700082], "error_description": "expired RT1"}'
        http = self.http_seq([(400, {}, body)])
        with self.assertRaises(graph.ReauthRequired) as cm:
            graph.refresh_access_token("cid", "RT1", http=http, sleep=lambda s: None)
        self.assertIn("700082", str(cm.exception))
        self.assertNotIn("RT1", str(cm.exception))

    def test_outage_is_transient(self):
        http = self.http_seq([(None, {}, b"")] * 2 + [(503, {}, b"{}")] * 2)
        with self.assertRaises(graph.Transient):
            graph.refresh_access_token("cid", "RT1", http=http, sleep=lambda s: None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
