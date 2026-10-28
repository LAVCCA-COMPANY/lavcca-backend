# -*- coding: utf-8 -*-
"""Offline tests for the real ADOPT BASELINE write path (baseline.run_adoption /
adopt_baselines / prewrite_gate). Fixture vault + fake API only."""
import os, sys, json, glob, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_baseline import BaselineBase, NOW, LATER  # noqa: E402
from test_canary_apply import T  # noqa: E402
import canary_apply as ca  # noqa: E402
import baseline as bl  # noqa: E402

CANARY_ID = "7" * 32
CANARY_RECORD = {"notion_id": CANARY_ID, "last_seen_edited_time": "2026-10-01T16:01:00.000Z",
                 "last_synced_content_hash": "c" * 64, "obsidian_path": "x/canary.md",
                 "last_successful_sync": "2026-10-01T16:08:08Z"}


class AdoptionBase(BaselineBase):
    def setUp(self):
        super().setUp()
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump({"schema_version": 1, "description": "d", "items": {CANARY_ID: CANARY_RECORD}}, f,
                      ensure_ascii=False, indent=2)
        with open(self.state_path, "rb") as f:
            self.state_bytes = f.read()
        self.rows = [self.evaluate()]
        self.assertEqual(self.rows[0]["status"], "ADOPT_CANDIDATE", self.rows[0])

    def md_snapshot(self):
        return {k: v for k, v in self._hashes().items() if k.endswith(".md")}

    def adopt(self, rows=None, crawl_ok=True, api_errors=(), md_snapshot=None):
        return bl.run_adoption(self.state_path, self.sync, self.rows if rows is None else rows, crawl_ok,
                               list(api_errors), md_snapshot or self.md_snapshot, now=NOW)

    def assert_state_unchanged(self):
        self.assertEqual(self._read(self.state_path), self.state_bytes)
        self.assertEqual(glob.glob(os.path.join(self.tmp, "**", ".notion_sync_tmp_*"), recursive=True), [])


class TestSuccessfulAdoption(AdoptionBase):
    def test_atomic_state_adoption(self):
        md_before = self.md_snapshot()
        res = self.adopt()
        self.assertEqual(res["status"], "ADOPTED", res)
        self.assertEqual((res["records_before"], res["records_after"]), (1, 2))
        self.assertEqual(res["added"], [T])
        # backup: backups/<ts>/state.json, byte-identical to the previous state
        self.assertEqual(res["state_backup"], os.path.join(self.sync, "backups", "20261002T030000Z", "state.json"))
        self.assertEqual(self._read(res["state_backup"]), self.state_bytes)
        st = json.loads(self._read(self.state_path))
        rec = st["items"][T]
        self.assertEqual(rec["record_type"], bl.ADOPTED_RECORD_TYPE)
        self.assertIsNone(rec["last_successful_sync"])  # adoption never claims a content sync
        self.assertTrue(set(bl.BASELINE_FIELDS) <= set(rec))
        self.assertEqual(bl.baseline_view(rec)["kind"], "adopted")
        # markdown untouched (zero writes); other top-level keys untouched
        self.assertEqual(self.md_snapshot(), md_before)
        self.assertEqual((st["schema_version"], st["description"]), (1, "d"))

    def test_existing_canary_record_byte_equivalent(self):
        self.adopt()
        st = json.loads(self._read(self.state_path))
        self.assertEqual(json.dumps(st["items"][CANARY_ID], sort_keys=True, ensure_ascii=False),
                         json.dumps(CANARY_RECORD, sort_keys=True, ensure_ascii=False))
        self.assertEqual(bl.baseline_view(st["items"][CANARY_ID])["kind"], "synced")

    def test_after_adoption_page_is_unchanged(self):
        self.adopt()
        r = self.evaluate()
        self.assertEqual((r["status"], r["sync_status"]), ("ALREADY_BASELINED", bl.UNCHANGED), r)


class TestGateAndFailures(AdoptionBase):
    def test_gate_api_errors_abort(self):
        res = self.adopt(api_errors=[{"path": "/x", "code": 500}])
        self.assertEqual(res["status"], "ABORTED_PREWRITE_GATE")
        self.assert_state_unchanged()

    def test_gate_crawl_not_ok_abort(self):
        self.assertEqual(self.adopt(crawl_ok=False)["status"], "ABORTED_PREWRITE_GATE")
        self.assert_state_unchanged()

    def test_gate_bad_rows_abort_whole_run(self):
        for bad in ({"notion_id": "9" * 32, "status": "EXCLUDED_CONTENT_FETCH_ERROR"},
                    {"notion_id": "9" * 32, "status": "EXCLUDED_RENDER_NONDETERMINISTIC"},
                    {"notion_id": "9" * 32, "status": "EXCLUDED_CONFLICT"},
                    {"notion_id": "9" * 32, "status": "ALREADY_BASELINED",
                     "sync_status": bl.LOCAL_MODIFICATION_CONFLICT}):
            res = self.adopt(rows=self.rows + [bad])
            self.assertEqual(res["status"], "ABORTED_PREWRITE_GATE", bad)
            self.assert_state_unchanged()

    def test_render_excluded_page_does_not_block_others(self):
        res = self.adopt(rows=self.rows + [{"notion_id": "9" * 32, "status": "EXCLUDED_RENDER_ERROR"}])
        self.assertEqual(res["status"], "ADOPTED", res)

    def test_gate_unproven_determinism_abort(self):
        row = dict(self.rows[0], render_hash_run_2="0" * 64)
        self.assertEqual(self.adopt(rows=[row])["status"], "ABORTED_PREWRITE_GATE")
        self.assert_state_unchanged()

    def test_duplicate_notion_id_abort(self):
        self.assertEqual(self.adopt(rows=self.rows + [dict(self.rows[0])])["status"], "ABORTED_PREWRITE_GATE")
        self.assert_state_unchanged()
        with self.assertRaises(ValueError):
            bl.adopt_baselines(self.state_path, [self.rows[0]["entry"], dict(self.rows[0]["entry"])], self.sync, now=LATER)
        self.assert_state_unchanged()

    def test_partial_write_impossible(self):
        good = self.rows[0]["entry"]
        bad = dict(good, notion_id="8" * 32, api_rendered_content_hash="short")
        with self.assertRaises(ValueError):
            bl.adopt_baselines(self.state_path, [good, bad], self.sync, now=NOW)
        self.assert_state_unchanged()
        bad_row = dict(self.rows[0], notion_id="8" * 32, entry=bad, render_hash_run_1="short", render_hash_run_2="short")
        res = self.adopt(rows=self.rows + [bad_row])
        self.assertIn(res["status"], ("ABORTED_PREWRITE_GATE", "FAILED_STATE_UNCHANGED"))
        self.assert_state_unchanged()

    def test_validation_failure_rolls_back(self):
        with mock.patch.object(bl, "verify_written_state", side_effect=ca.WriteVerificationError("injected")):
            res = self.adopt()
        self.assertEqual(res["status"], "FAILED_STATE_UNCHANGED", res)
        self.assert_state_unchanged()

    def test_replace_failure_keeps_state(self):
        with mock.patch.object(ca, "_replace", side_effect=OSError("injected")):
            res = self.adopt()
        self.assertEqual(res["status"], "FAILED_STATE_UNCHANGED", res)
        self.assert_state_unchanged()

    def test_markdown_change_detected_rolls_back_state(self):
        snaps = iter([{"a.md": "1"}, {"a.md": "2"}])
        res = self.adopt(md_snapshot=lambda: next(snaps))
        self.assertEqual(res["status"], "FAILED_ROLLED_BACK", res)
        self.assert_state_unchanged()

    def test_markdown_write_zero_across_all_paths(self):
        md_before = self.md_snapshot()
        self.adopt(api_errors=[{"x": 1}])
        with mock.patch.object(bl, "verify_written_state", side_effect=ca.WriteVerificationError("x")):
            self.adopt()
        self.adopt()
        self.assertEqual(self.md_snapshot(), md_before)

    def test_backup_never_overwritten(self):
        self.adopt()
        with self.assertRaises(FileExistsError):
            bl.adopt_baselines(self.state_path, [], self.sync, now=NOW)  # same timestamp -> backup exists


if __name__ == "__main__":
    unittest.main(verbosity=2)
