# -*- coding: utf-8 -*-
"""Offline tests for baseline.py (ADOPT BASELINE + incremental classification).
Reuses the canary fixture vault / fake API -- no real LAVCCA vault files, no network."""
import os, sys, json, glob, unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_canary_apply import CanaryTestBase, blk, rt, T, A, NOW, OLD_MTIME, target_blocks  # noqa: E402
import canary_apply as ca  # noqa: E402
import baseline as bl  # noqa: E402

LATER = datetime(2026, 10, 3, 0, 0, 0, tzinfo=timezone.utc)


class BaselineBase(CanaryTestBase):
    def page_node(self, **over):
        p = self.api.pages[T]
        node = {"title": "대상", "last_edited_time": p["last_edited_time"],
                "archived": p["archived"], "in_trash": p["in_trash"]}
        node.update(over)
        return node

    def state_items(self):
        with open(self.state_path, encoding="utf-8") as f:
            return json.load(f)["items"]

    def evaluate(self, node=None, state_items=None, descendant=True, nid=T, path=None):
        id_map = ca.id_to_vault_path(ca.scan_vault_identities(self.cfg))
        bootstrap_ts = datetime.fromisoformat(self.cfg["bootstrap_migration_completed_at"]).timestamp()
        return bl.evaluate_page(nid, path or self.target, node or self.page_node(), self.api, self.cfg,
                                self.state_items() if state_items is None else state_items,
                                id_map, bootstrap_ts, descendant, NOW)

    def adopt_current(self):
        r = self.evaluate()
        self.assertEqual(r["status"], "ADOPT_CANDIDATE", r)
        bl.adopt_baselines(self.state_path, [r["entry"]], self.sync, now=NOW)
        return r


class TestAdoption(BaselineBase):
    def test_existing_page_without_baseline_is_candidate_and_body_untouched(self):
        orig = self._read(self.target)
        r = self.evaluate()
        self.assertEqual(r["status"], "ADOPT_CANDIDATE", r)
        e = r["entry"]
        self.assertEqual(set(e), set(bl.BASELINE_FIELDS) | {"record_type"})
        self.assertEqual(e["record_type"], bl.ADOPTED_RECORD_TYPE)
        self.assertEqual(e["notion_last_edited_time"], "2026-09-30T10:00:00.000Z")
        self.assertEqual(e["obsidian_path"], "01_SHARED/Meetings/대상.md")
        self.assertIsNone(e["last_successful_sync"])
        # API-rendered and local hashes differ (different renderers) -- still adoptable
        self.assertTrue(r["hashes_differ"])
        self.assertNotEqual(e["api_rendered_content_hash"], e["local_content_hash"])
        local_body = ca.split_frontmatter_strict(orig.decode("utf-8"))[2]
        self.assertEqual(e["local_content_hash"], ca.sha256_text(local_body))
        self.assertEqual(self._read(self.target), orig)
        self.assert_nothing_changed()

    def test_adopt_writes_state_only_markdown_byte_identical(self):
        md_before = self._hashes()
        r = self.evaluate()
        res = bl.adopt_baselines(self.state_path, [r["entry"]], self.sync, now=NOW)
        self.assertEqual(res["added"], [T])
        self.assertEqual(self._hashes(), md_before)  # zero markdown writes
        self.assertEqual(self.state_items()[T], r["entry"])
        self.assertTrue(os.path.exists(res["state_backup"]))
        self.assertEqual(glob.glob(os.path.join(self.tmp, "**", ".notion_sync_tmp_*"), recursive=True), [])

    def test_archive_not_adoptable(self):
        # archive files never reach evaluate_page; selection refuses them
        with self.assertRaises(ca.CanaryRefused) as cm:
            ca.select_target(A, ca.scan_vault_identities(self.cfg))
        self.assertEqual(cm.exception.code, "ARCHIVE_TARGET")

    def test_remote_archived_or_trashed_excluded(self):
        self.assertEqual(self.evaluate(self.page_node(archived=True))["status"], "EXCLUDED_ARCHIVED")
        self.assertEqual(self.evaluate(self.page_node(in_trash=True))["status"], "EXCLUDED_TRASHED")

    def test_descendant_unverified_excluded(self):
        self.assertEqual(self.evaluate(descendant=False)["status"], "EXCLUDED_DESCENDANT_UNVERIFIED")

    def test_renderer_error_excluded(self):
        self.api.set_blocks(T, target_blocks() + [blk("f" * 32, "ai_block", {})])
        r = self.evaluate()
        self.assertEqual(r["status"], "EXCLUDED_RENDER_ERROR", r)
        self.assert_nothing_changed()

    def test_network_failure_excluded(self):
        self.api.overrides[f"/blocks/{T}/children?page_size=100"] = (None, {"message": "network"})
        r = self.evaluate()
        self.assertEqual(r["status"], "EXCLUDED_CONTENT_FETCH_ERROR", r)
        self.assert_nothing_changed()

    def test_bootstrap_conflict_excluded(self):
        os.utime(self.target, None)
        self.assertEqual(self.evaluate()["status"], "EXCLUDED_CONFLICT")

    def test_malformed_excluded(self):
        p = os.path.join(self.lavcca, "01_SHARED", "bad.md")
        r = self.evaluate(nid="5" * 32, path=p)
        self.assertEqual(r["status"], "EXCLUDED_MALFORMED")

    def test_canary_state_is_already_baselined_and_untouched(self):
        res = self.run_canary(T)  # real canary apply in the fixture
        self.assertEqual(res["status"], "APPLIED")
        canary_item = dict(self.state_items()[T])
        r = self.evaluate(self.page_node())
        self.assertEqual(r["status"], "ALREADY_BASELINED", r)
        self.assertEqual(r["sync_status"], bl.UNCHANGED, r)
        self.assertEqual(r["baseline_kind"], "synced")
        # adoption never replaces an existing item
        fake = dict(canary_item, notion_id=T, notion_last_edited_time="x", api_rendered_content_hash="0" * 64,
                    local_content_hash="0" * 64, obsidian_path="x", baseline_adopted_at="x", last_successful_sync=None)
        out = bl.adopt_baselines(self.state_path, [fake], self.sync, now=LATER)
        self.assertEqual(out["skipped_existing"], [T])
        self.assertEqual(self.state_items()[T], canary_item)

    def test_state_write_failure_keeps_original_state(self):
        with open(self.state_path, "rb") as f:
            orig_state = f.read()
        r = self.evaluate()
        with mock.patch.object(ca, "_replace", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                bl.adopt_baselines(self.state_path, [r["entry"]], self.sync, now=NOW)
        self.assertEqual(self._read(self.state_path), orig_state)
        self.assertEqual(glob.glob(os.path.join(self.tmp, "**", ".notion_sync_tmp_*"), recursive=True), [])

    def test_state_verify_failure_restores_original_state(self):
        with open(self.state_path, "rb") as f:
            orig_state = f.read()
        r = self.evaluate()
        real_atomic = ca.atomic_write_bytes
        calls = {"n": 0}

        def corrupting_atomic(path, data):
            calls["n"] += 1
            if calls["n"] == 1:  # first write lands corrupted -> verification must fail and restore
                return real_atomic(path, data + b"corrupt")
            return real_atomic(path, data)

        with mock.patch.object(ca, "atomic_write_bytes", side_effect=corrupting_atomic):
            with self.assertRaises(ca.WriteVerificationError):
                bl.adopt_baselines(self.state_path, [r["entry"]], self.sync, now=NOW)
        self.assertEqual(self._read(self.state_path), orig_state)

    def test_invalid_entry_rejected_before_write(self):
        with open(self.state_path, "rb") as f:
            orig_state = f.read()
        with self.assertRaises(ValueError):
            bl.adopt_baselines(self.state_path, [{"notion_id": T}], self.sync, now=NOW)
        self.assertEqual(self._read(self.state_path), orig_state)


class TestIncrementalAfterAdoption(BaselineBase):
    def setUp(self):
        super().setUp()
        self.adopted = self.adopt_current()["entry"]
        self.md_after_adopt = self._hashes()

    def status(self, node):
        r = self.evaluate(node)
        self.assertEqual(r["status"], "ALREADY_BASELINED", r)
        self.assertEqual(r["baseline_kind"], "adopted")
        return r["sync_status"]

    def edit_remote(self):
        self.api.set_blocks(T, target_blocks() + [blk("f" * 32, "paragraph", {"rich_text": [rt("새 문장")]})])

    def test_same_hash_same_time_unchanged(self):
        self.assertEqual(self.status(self.page_node()), bl.UNCHANGED)

    def test_time_and_hash_changed_update_confirmed(self):
        self.edit_remote()
        self.assertEqual(self.status(self.page_node(last_edited_time="2026-10-02T00:00:00.000Z")), bl.UPDATE_CONFIRMED)

    def test_time_only_metadata_only_change(self):
        self.assertEqual(self.status(self.page_node(last_edited_time="2026-10-02T00:00:00.000Z")), bl.METADATA_ONLY_CHANGE)

    def test_hash_only_content_hash_anomaly(self):
        self.edit_remote()
        self.assertEqual(self.status(self.page_node()), bl.CONTENT_HASH_ANOMALY)

    def test_local_edit_conflict(self):
        with open(self.target, "a", encoding="utf-8", newline="\n") as f:
            f.write("\n사람이 고침\n")
        self.edit_remote()
        self.assertEqual(self.status(self.page_node(last_edited_time="2026-10-02T00:00:00.000Z")),
                         bl.LOCAL_MODIFICATION_CONFLICT)

    def test_evaluation_never_writes_markdown(self):
        for node in (self.page_node(), self.page_node(last_edited_time="z")):
            self.evaluate(node)
        self.edit_remote()
        self.evaluate(self.page_node(last_edited_time="z"))
        self.assertEqual(self._hashes(), self.md_after_adopt)

    def test_canary_conflict_check_respects_adopted_local_hash(self):
        with open(self.target, "a", encoding="utf-8", newline="\n") as f:
            f.write("\n사람이 고침\n")
        res = self.run_canary(T, now=LATER)
        self.assertEqual((res["status"], res.get("refusal_code")), ("REFUSED", "CONFLICT"))


class TestClassifierPure(unittest.TestCase):
    V = {"notion_last_edited_time": "t0", "api_rendered_content_hash": "a" * 64, "local_content_hash": "l" * 64}

    def test_matrix(self):
        c = bl.classify_incremental
        self.assertEqual(c(self.V, "t0", "a" * 64, "l" * 64)[0], bl.UNCHANGED)
        self.assertEqual(c(self.V, "t1", "b" * 64, "l" * 64)[0], bl.UPDATE_CONFIRMED)
        self.assertEqual(c(self.V, "t1", "a" * 64, "l" * 64)[0], bl.METADATA_ONLY_CHANGE)
        self.assertEqual(c(self.V, "t0", "b" * 64, "l" * 64)[0], bl.CONTENT_HASH_ANOMALY)
        self.assertEqual(c(self.V, "t0", "a" * 64, "x" * 64)[0], bl.LOCAL_MODIFICATION_CONFLICT)

    def test_legacy_canary_item_view(self):
        v = bl.baseline_view({"notion_id": T, "last_seen_edited_time": "t", "last_synced_content_hash": "h" * 64,
                              "obsidian_path": "p", "last_successful_sync": "s"})
        self.assertEqual(v["api_rendered_content_hash"], "h" * 64)
        self.assertEqual(v["local_content_hash"], "h" * 64)
        self.assertEqual(v["kind"], "synced")
        self.assertIsNone(bl.baseline_view({}))
        self.assertIsNone(bl.baseline_view({"notion_id": T}))


class TestCli(unittest.TestCase):
    def test_adopt_modes(self):
        r = ca.resolve_cli_mode
        self.assertEqual(r(False, None, adopt_baseline=True, dry_run=True)[0], "adopt_baseline_preview")
        self.assertEqual(r(False, None, adopt_baseline=True, dry_run=False)[0], "adopt_baseline_write")
        self.assertEqual(r(True, [T], adopt_baseline=True, dry_run=True)[0], "refuse")
        self.assertEqual(r(False, [T], adopt_baseline=True, dry_run=True)[0], "refuse")
        self.assertEqual(r(False, None, dry_run=True)[0], "full_dry_run")
        self.assertEqual(r(False, None)[0], "full_dry_run")


if __name__ == "__main__":
    unittest.main(verbosity=2)
