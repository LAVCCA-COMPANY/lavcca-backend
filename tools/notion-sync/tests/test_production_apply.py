# -*- coding: utf-8 -*-
"""Offline tests for production_apply.py (incremental batch APPLY, rollback, lock).
Fixture vault + fake API only -- never touches real LAVCCA vault files."""
import os, sys, json, glob, unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_canary_apply import CanaryTestBase, blk, rt, fm, T, ROOT, DB, NOW, OLD_MTIME, target_blocks  # noqa: E402
import canary_apply as ca  # noqa: E402
import baseline as bl  # noqa: E402
import production_apply as prod  # noqa: E402
import attachment_sync as att  # noqa: E402

att.configure_owner_roots({"02_MEMBER_A": "김멤버", "03_MEMBER_B": "박멤버", "04_MEMBER_C": "이멤버"})

U = "8" * 32
RUN_AT = datetime(2026, 10, 4, 0, 0, 0, tzinfo=timezone.utc)
EDITED = "2026-10-03T09:00:00.000Z"


class ProdBase(CanaryTestBase):
    def setUp(self):
        super().setUp()
        self.u_path = os.path.join(self.lavcca, "01_SHARED", "u.md")
        with open(self.u_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(fm(U, extra="custom_key: keep\n") + "\n# 두번째\n\n옛 내용\n")
        os.utime(self.u_path, (OLD_MTIME, OLD_MTIME))
        self.api.pages[U] = {"object": "page", "id": U, "archived": False, "in_trash": False,
                             "last_edited_time": "2026-09-30T11:00:00.000Z",
                             "parent": {"type": "page_id", "page_id": ROOT},
                             "properties": {"title": {"type": "title", "title": [{"plain_text": "두번째"}]}}}
        self.api.children[U] = [blk("9" * 31 + "1", "paragraph", {"rich_text": [rt("첫 문단")]})]
        self.snapshot = self._hashes()
        self.adopt([T, U])
        self.state_after_adopt = self._read(self.state_path)
        self.md_after_adopt = self.md()

    # ---- helpers ----
    def node(self, nid):
        p = self.api.pages[nid]
        return {"title": ca.page_title(p), "last_edited_time": p["last_edited_time"], "archived": p["archived"],
                "in_trash": p["in_trash"], "parent": p["parent"], "properties": p.get("properties")}

    def path_of(self, nid):
        return self.target if nid == T else self.u_path

    def adopt(self, ids):
        id_map = ca.id_to_vault_path(ca.scan_vault_identities(self.cfg))
        bts = datetime.fromisoformat(self.cfg["bootstrap_migration_completed_at"]).timestamp()
        entries = []
        for nid in ids:
            r = bl.evaluate_page(nid, self.path_of(nid), self.node(nid), self.api, self.cfg, {}, id_map, bts, True, NOW)
            self.assertEqual(r["status"], "ADOPT_CANDIDATE", r)
            entries.append(r["entry"])
        bl.adopt_baselines(self.state_path, entries, self.sync, now=NOW)

    def edit_remote(self, nid, text="새 문장", touch_time=True):
        self.api.children[nid] = list(self.api.children[nid]) + [blk("f" * 31 + str(len(self.api.children[nid]) % 10),
                                                                     "paragraph", {"rich_text": [rt(text)]})]
        if touch_time:
            self.api.pages[nid]["last_edited_time"] = EDITED

    def md(self):
        return {k: v for k, v in self._hashes().items() if k.endswith(".md")}

    def state(self):
        with open(self.state_path, encoding="utf-8") as f:
            return json.load(f)

    def run_prod(self, rows, apply=True, crawl_ok=True, api_errors=()):
        return prod.run_production(rows, crawl_ok, list(api_errors), self.api, self.cfg, self.state_path, self.sync,
                                   ca.scan_vault_identities(self.cfg), RUN_AT, apply=apply, md_snapshot=self.vault_md)

    def vault_md(self):
        out = {}
        for root, _, files in os.walk(self.vault):
            if os.path.join("LAVCCA", "_sync") in root:
                continue
            for fn in files:
                if fn.endswith(".md"):
                    p = os.path.join(root, fn)
                    out[os.path.relpath(p, self.vault).replace(os.sep, "/")] = ca.sha256_bytes(self._read(p))
        return out

    def assert_untouched(self):
        self.assertEqual(self.md(), self.md_after_adopt)
        self.assertEqual(self._read(self.state_path), self.state_after_adopt)
        self.assertEqual(glob.glob(os.path.join(self.tmp, "**", ".notion_sync_tmp_*"), recursive=True), [])

    @staticmethod
    def upd(*ids):
        return [{"notion_id": i, "status": "UPDATE_CONFIRMED"} for i in ids]


class TestApplySuccess(ProdBase):
    def test_single_update_applied(self):
        self.edit_remote(T)
        res = self.run_prod(self.upd(T))
        self.assertEqual(res["status"], "ALL_APPLIED", res)
        self.assertEqual(res["applied"], [T])
        text = self._read(self.target).decode("utf-8")
        self.assertIn("새 문장", text)
        f, lines, body = ca.split_frontmatter_strict(text)
        self.assertIn("aliases: keep-me", lines)  # unknown metadata kept
        self.assertEqual((f["notion_id"], f["owner"], f["notion_last_edited_time"]), (T, "shared", EDITED))
        rec = self.state()["items"][T]
        self.assertEqual(rec["record_type"], prod.SYNCED_RECORD_TYPE)
        self.assertEqual(rec["local_content_hash"], ca.sha256_text(body))       # state hash == real body hash
        self.assertEqual(rec["api_rendered_content_hash"], ca.sha256_text(body))
        self.assertEqual(rec["last_successful_sync"], "2026-10-04T00:00:00Z")
        self.assertEqual(rec["notion_last_edited_time"], EDITED)
        # batch backup: original markdown + state.json
        bdir = os.path.join(self.sync, "backups", "20261004T000000Z")
        self.assertTrue(os.path.exists(os.path.join(bdir, "01_SHARED", "Meetings", "대상.md")))
        self.assertEqual(self._read(os.path.join(bdir, "state.json")), self.state_after_adopt)
        # U untouched
        self.assertEqual(self.md()[os.path.join("01_SHARED", "u.md")], self.md_after_adopt[os.path.join("01_SHARED", "u.md")])

    def test_multiple_updates_applied(self):
        self.edit_remote(T)
        self.edit_remote(U)
        res = self.run_prod(self.upd(T, U))
        self.assertEqual(res["status"], "ALL_APPLIED", res)
        self.assertEqual(sorted(res["applied"]), sorted([T, U]))
        self.assertIn("새 문장", self._read(self.u_path).decode("utf-8"))
        self.assertIn("custom_key: keep", self._read(self.u_path).decode("utf-8"))

    def test_rerun_after_success_is_unchanged(self):
        self.edit_remote(T)
        self.assertEqual(self.run_prod(self.upd(T))["status"], "ALL_APPLIED")
        id_map = ca.id_to_vault_path(ca.scan_vault_identities(self.cfg))
        r = bl.evaluate_page(T, self.target, self.node(T), self.api, self.cfg, self.state()["items"], id_map, None,
                             True, RUN_AT)
        self.assertEqual((r["status"], r["sync_status"]), ("ALREADY_BASELINED", bl.UNCHANGED), r)
        # a second production run finds nothing to do
        self.assertEqual(self.run_prod(self.upd(T))["status"], "NOTHING_TO_APPLY")

    def test_preview_writes_nothing(self):
        self.edit_remote(T)
        res = self.run_prod(self.upd(T), apply=False)
        self.assertEqual(res["status"], "PREVIEW")
        self.assertEqual([s["notion_id"] for s in res["safe"]], [T])
        self.assert_untouched()


class TestNeverWritten(ProdBase):
    def test_unchanged_write_zero(self):
        res = self.run_prod(self.upd(T) + [{"notion_id": U, "status": "UNCHANGED"}])
        self.assertEqual(res["status"], "NOTHING_TO_APPLY", res)
        self.assertEqual(res["excluded"][0]["status"], bl.UNCHANGED)
        self.assert_untouched()

    def test_metadata_only_change_markdown_write_zero(self):
        self.api.pages[T]["last_edited_time"] = EDITED
        rows = [{"notion_id": T, "status": "METADATA_ONLY_CHANGE", "live_last_edited_time": EDITED}]
        res = self.run_prod(rows)
        self.assertEqual(res["status"], "ALL_APPLIED", res)   # state-only observation commit
        self.assertEqual(res["applied"], [])
        self.assertEqual(self.md(), self.md_after_adopt)       # markdown write 0
        before = json.loads(self.state_after_adopt)["items"][T]
        after = self.state()["items"][T]
        self.assertEqual(after["observed_last_edited_time"], EDITED)
        for k in bl.BASELINE_FIELDS + ("record_type",):         # baseline untouched, not a sync
            self.assertEqual(after[k], before[k])
        # erroneously selected as UPDATE_CONFIRMED -> preflight refuses it
        res2 = self.run_prod(self.upd(T))
        self.assertEqual(res2["excluded"][0]["status"], bl.METADATA_ONLY_CHANGE)
        self.assertEqual(self.md(), self.md_after_adopt)

    def test_content_hash_anomaly_write_zero(self):
        self.edit_remote(T, touch_time=False)
        res = self.run_prod(self.upd(T))
        self.assertEqual(res["excluded"][0]["status"], bl.CONTENT_HASH_ANOMALY)
        self.assert_untouched()

    def test_local_modification_conflict_write_zero(self):
        with open(self.target, "a", encoding="utf-8", newline="\n") as f:
            f.write("\n사람이 고침\n")
        edited_local = self._read(self.target)
        self.edit_remote(T)
        res = self.run_prod(self.upd(T))
        self.assertEqual(res["excluded"][0]["status"], bl.LOCAL_MODIFICATION_CONFLICT)
        self.assertEqual(self._read(self.target), edited_local)
        self.assertEqual(self._read(self.state_path), self.state_after_adopt)

    def test_render_unsupported_write_zero(self):
        self.edit_remote(T)
        self.api.children[T].append(blk("e" * 32, "ai_block", {}))
        res = self.run_prod(self.upd(T))
        self.assertEqual(res["excluded"][0]["status"], "RENDER_UNSUPPORTED")
        self.assert_untouched()

    def test_never_write_statuses_not_selected(self):
        rows = [{"notion_id": T, "status": s} for s in sorted(prod.NEVER_WRITE)]
        rows += [{"notion_id": T, "status": "QUERY_HIDDEN_ACTIVE", "content_status": "UNCHANGED"}]
        self.assertEqual(prod.select_update_candidates(rows), [])

    def test_frontmatter_identity_mismatch_excluded_others_applied(self):
        text = self._read(self.target).decode("utf-8").replace("project: LAVCCA", "project: OTHER")
        with open(self.target, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        bad = self._read(self.target)
        self.edit_remote(T)
        self.edit_remote(U)
        res = self.run_prod(self.upd(T, U))
        self.assertEqual(res["status"], "ALL_APPLIED", res)
        self.assertEqual(res["applied"], [U])
        self.assertEqual(res["excluded"][0]["status"], "EXCLUDED_MALFORMED_FRONTMATTER")
        self.assertEqual(self._read(self.target), bad)

    def test_destination_mismatch_excluded(self):
        st = self.state()
        st["items"][T]["obsidian_path"] = "elsewhere/x.md"
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(st, f)
        self.edit_remote(T)
        res = self.run_prod(self.upd(T))
        self.assertEqual(res["excluded"][0]["status"], "EXCLUDED_DESTINATION_MISMATCH")
        self.assertEqual(self.md(), self.md_after_adopt)


class TestQueryHidden(ProdBase):
    def test_hidden_update_confirmed_with_direct_verification_applied(self):
        self.edit_remote(T)
        rows = [{"notion_id": T, "status": "QUERY_HIDDEN_ACTIVE", "content_status": "UPDATE_CONFIRMED"}]
        res = self.run_prod(rows)
        self.assertEqual((res["status"], res["applied"]), ("ALL_APPLIED", [T]), res)

    def test_hidden_direct_verification_failure_write_zero(self):
        self.edit_remote(T)
        self.api.overrides[f"/databases/{DB}"] = (403, {})
        rows = [{"notion_id": T, "status": "QUERY_HIDDEN_ACTIVE", "content_status": "UPDATE_CONFIRMED"}]
        res = self.run_prod(rows)
        self.assertEqual(res["excluded"][0]["status"], "DESCENDANT_UNKNOWN")
        self.assert_untouched()


class TestAbortAndRollback(ProdBase):
    def setUp(self):
        super().setUp()
        self.edit_remote(T)
        self.edit_remote(U)

    def test_api_failure_aborts_batch(self):
        res = self.run_prod(self.upd(T, U), api_errors=[{"path": "/x", "code": 502}])
        self.assertEqual(res["status"], "ABORTED_API_ERRORS")
        self.assert_untouched()

    def test_crawl_health_failure_aborts_batch(self):
        self.assertEqual(self.run_prod(self.upd(T, U), crawl_ok=False)["status"], "ABORTED_CRAWL_HEALTH")
        self.assert_untouched()

    def test_backup_failure_aborts_before_any_write(self):
        with mock.patch.object(ca, "write_backup", side_effect=OSError("disk full (injected)")):
            res = self.run_prod(self.upd(T, U))
        self.assertEqual(res["status"], "ABORTED_BEFORE_WRITE", res)
        self.assert_untouched()

    def test_second_markdown_write_failure_rolls_back_first(self):
        real = prod._write_verified
        calls = {"n": 0}

        def flaky(path, data):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("second write failed (injected)")
            return real(path, data)

        with mock.patch.object(prod, "_write_verified", side_effect=flaky):
            res = self.run_prod(self.upd(T, U))
        self.assertEqual(res["status"], "ROLLED_BACK", res)
        self.assertTrue(res["rollback"]["ok"])
        self.assert_untouched()

    def test_state_commit_failure_rolls_back_all_markdown(self):
        real = prod._write_verified

        def fail_state(path, data):
            if os.path.basename(path) == "state.json" and b"SYNCED" in data:
                raise OSError("state commit failed (injected)")
            return real(path, data)

        with mock.patch.object(prod, "_write_verified", side_effect=fail_state):
            res = self.run_prod(self.upd(T, U))
        self.assertEqual(res["status"], "ROLLED_BACK", res)
        self.assertEqual(len(res["rollback"]["files"]), 2)
        self.assert_untouched()

    def test_temp_file_failure_rolls_back(self):
        real = ca._fsync
        calls = {"n": 0}

        def flaky_fsync(f):
            if isinstance(f.name, int):  # temp files opened via mkstemp/fdopen
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("fsync failed (injected)")
            return real(f)

        with mock.patch.object(ca, "_fsync", side_effect=flaky_fsync):
            res = self.run_prod(self.upd(T, U))
        self.assertEqual(res["status"], "ROLLED_BACK", res)
        self.assert_untouched()

    def test_post_commit_invariant_failure_rolls_back(self):
        snaps = {"n": 0}
        real = self.vault_md

        def drifting():
            snaps["n"] += 1
            out = real()
            if snaps["n"] == 2:
                out["LAVCCA/stray.md"] = "x"  # a non-target markdown "changed"
            return out

        res = prod.run_production(self.upd(T, U), True, [], self.api, self.cfg, self.state_path, self.sync,
                                  ca.scan_vault_identities(self.cfg), RUN_AT, apply=True, md_snapshot=drifting)
        self.assertEqual(res["status"], "ROLLED_BACK", res)
        self.assert_untouched()


class TestLock(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="lock_test_")
        self.path = os.path.join(self.dir, prod.LOCK_NAME)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_lock(self, pid, host, age_s=0):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"pid": pid, "host": host, "run_id": "old"}, f)
        t = datetime.now(timezone.utc).timestamp() - age_s
        os.utime(self.path, (t, t))

    def test_second_instance_refused(self):
        tok, _ = prod.acquire_lock(self.dir, "run1")
        with self.assertRaises(prod.LockError) as cm:
            prod.acquire_lock(self.dir, "run2")
        self.assertEqual(cm.exception.code, "LOCKED")
        self.assertTrue(prod.release_lock(self.dir, tok))
        tok2, _ = prod.acquire_lock(self.dir, "run3")  # free again
        prod.release_lock(self.dir, tok2)

    def test_stale_lock_dead_pid_same_host_reclaimed(self):
        import socket
        self.write_lock(pid=999999, host=socket.gethostname())
        self.assertFalse(prod.pid_alive(999999))
        tok, note = prod.acquire_lock(self.dir, "run1")
        self.assertIn("reclaimed stale lock", note)
        prod.release_lock(self.dir, tok)

    def test_live_pid_same_host_refused(self):
        import socket
        self.write_lock(pid=os.getpid(), host=socket.gethostname(), age_s=10 * 3600)
        with self.assertRaises(prod.LockError) as cm:
            prod.acquire_lock(self.dir, "run1")
        self.assertEqual(cm.exception.code, "LOCKED")

    def test_other_host_fresh_refused_old_requires_manual(self):
        self.write_lock(pid=1, host="other-pc")
        with self.assertRaises(prod.LockError) as cm:
            prod.acquire_lock(self.dir, "run1")
        self.assertEqual(cm.exception.code, "LOCKED")
        self.write_lock(pid=1, host="other-pc", age_s=7 * 3600)
        with self.assertRaises(prod.LockError) as cm:
            prod.acquire_lock(self.dir, "run1")
        self.assertEqual(cm.exception.code, "STALE_UNVERIFIED")
        self.assertTrue(os.path.exists(self.path))  # never auto-deleted

    def test_release_only_by_owner(self):
        tok, _ = prod.acquire_lock(self.dir, "run1")
        self.assertFalse(prod.release_lock(self.dir, {"pid": tok["pid"], "run_id": "someone-else"}))
        self.assertTrue(os.path.exists(self.path))
        self.assertTrue(prod.release_lock(self.dir, tok))


class TestCli(unittest.TestCase):
    def test_modes(self):
        r = ca.resolve_cli_mode
        self.assertEqual(r(False, None, apply_updates=True)[0], "apply_updates_write")
        self.assertEqual(r(False, None, apply_updates=True, dry_run=True)[0], "apply_updates_preview")
        self.assertEqual(r(True, [T], apply_updates=True)[0], "refuse")
        self.assertEqual(r(False, None, adopt_baseline=True, apply_updates=True)[0], "refuse")
        self.assertEqual(r(False, None)[0], "full_dry_run")
        self.assertEqual(r(True, [T])[0], "canary_apply")


if __name__ == "__main__":
    unittest.main(verbosity=2)
