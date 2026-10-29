# -*- coding: utf-8 -*-
"""client/obsidian_pull.py: docs obsidian-vault/Notion -> <vault>/LAVCCA, one way, never outside LAVCCA/."""
import os, sys, shutil, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))
import obsidian_pull as op  # noqa: E402


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data.encode("utf-8") if isinstance(data, str) else data)


def read(path):
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8")


class TestObsidianPull(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "docs")
        self.src = os.path.join(self.repo, "obsidian-vault", "Notion")
        self.vault = os.path.join(self.tmp, "Obsidian Vault")
        self.state = os.path.join(self.tmp, "state")
        self.dst = os.path.join(self.vault, "LAVCCA")
        write(os.path.join(self.src, "LAVCCA.md"), "---\nnotion_id: r\n---\n\n# LAVCCA\n\n[[Notion/LAVCCA/회의록|회의록]]\n")
        write(os.path.join(self.src, "LAVCCA", "회의록.md"), "# 회의록\n\n![](../_attachments/b1.png)\n")
        write(os.path.join(self.src, "_attachments", "b1.png"), b"\x89PNG")
        # personal vault content that must never change
        self.personal = {os.path.join(self.vault, ".obsidian", "app.json"): "{}",
                         os.path.join(self.vault, "1. 일간", "2026-10-29.md"): "오늘",
                         os.path.join(self.vault, "NATAM", "00_HOME", "home.md"): "old"}
        for p, d in self.personal.items():
            write(p, d)

    def run_pull(self, do_apply=True):
        lines = []
        code = op.run(self.vault, self.repo, self.state, do_apply, fetch=False, out=lines.append)
        return code, lines

    def assert_personal_untouched(self):
        for p, d in self.personal.items():
            self.assertEqual(read(p), d)

    def test_dry_run_lists_changes_and_writes_nothing(self):
        code, lines = self.run_pull(do_apply=False)
        self.assertEqual(code, 0)
        self.assertIn('"create": 3', lines[0])
        self.assertIn("create\tLAVCCA/LAVCCA/회의록.md", lines)
        self.assertFalse(os.path.exists(self.dst))

    def test_apply_copies_and_rewrites_wikilinks_to_lavcca(self):
        self.run_pull()
        root = read(os.path.join(self.dst, "LAVCCA.md"))
        self.assertIn("[[LAVCCA/LAVCCA/회의록|회의록]]", root)
        self.assertNotIn("[[Notion/", root)
        page = read(os.path.join(self.dst, "LAVCCA", "회의록.md"))
        self.assertIn("](../_attachments/b1.png)", page)  # relative attachment link still resolves
        self.assertTrue(os.path.isfile(os.path.normpath(os.path.join(self.dst, "LAVCCA", "../_attachments/b1.png"))))
        with open(os.path.join(self.dst, "_attachments", "b1.png"), "rb") as fh:
            self.assertEqual(fh.read(), b"\x89PNG")  # binaries are copied byte for byte
        self.assert_personal_untouched()

    def test_attachments_named_md_are_copied_byte_for_byte(self):
        # a UTF-16 .md uploaded to Notion lives in _attachments/: no link rewrite, no decoding
        utf16 = "﻿[[Notion/x]]".encode("utf-16")
        write(os.path.join(self.src, "_attachments", "b2.md"), utf16)
        self.run_pull()
        with open(os.path.join(self.dst, "_attachments", "b2.md"), "rb") as fh:
            self.assertEqual(fh.read(), utf16)

    def test_git_update_turns_autocrlf_off_before_merging(self):
        calls = []
        with mock.patch.object(op.subprocess, "run", side_effect=lambda a, **k: calls.append(a) or
                               mock.Mock(returncode=0, stderr="")):
            self.assertIsNone(op.git_update(self.repo))
        self.assertEqual(calls[0][3:], ["config", "core.autocrlf", "false"])
        self.assertEqual([c[3] for c in calls[1:]], ["fetch", "merge"])
        self.assertIn("--ff-only", calls[2])

    def test_second_run_changes_nothing_and_logs_nothing(self):
        self.run_pull()
        code, lines = self.run_pull()
        self.assertEqual((code, lines), (0, []))  # every-minute runs must not grow the log
        self.assertFalse(os.path.isdir(os.path.join(self.state, "trash")))

    def test_update_and_delete_keep_previous_versions_in_trash(self):
        self.run_pull()
        write(os.path.join(self.dst, "LAVCCA", "회의록.md"), "로컬에서 고친 내용")  # LAVCCA/ is Notion-owned
        write(os.path.join(self.dst, "직접 만든 메모.md"), "메모")
        os.remove(os.path.join(self.src, "_attachments", "b1.png"))
        code, lines = self.run_pull()
        self.assertIn('"update": 1, "delete": 2', lines[0])
        self.assertIn("# 회의록", read(os.path.join(self.dst, "LAVCCA", "회의록.md")))
        self.assertFalse(os.path.exists(os.path.join(self.dst, "직접 만든 메모.md")))
        self.assertFalse(os.path.exists(os.path.join(self.dst, "_attachments")))  # emptied folder removed
        trash = os.path.join(self.state, "trash")
        run_dir = os.path.join(trash, os.listdir(trash)[0])
        self.assertEqual(read(os.path.join(run_dir, "LAVCCA", "회의록.md")), "로컬에서 고친 내용")
        self.assertEqual(read(os.path.join(run_dir, "직접 만든 메모.md")), "메모")
        self.assert_personal_untouched()

    def test_failed_write_leaves_old_file_and_no_temp(self):
        self.run_pull()
        write(os.path.join(self.src, "LAVCCA", "회의록.md"), "# 회의록 v2\n")
        target = os.path.join(self.dst, "LAVCCA", "회의록.md")
        before = read(target)
        with mock.patch.object(op.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.run_pull()
        self.assertEqual(read(target), before)
        self.assertEqual([f for f in os.listdir(os.path.dirname(target)) if f.endswith(".lavcca-tmp")], [])
        self.assertFalse(os.path.exists(os.path.join(self.state, "pull.lock")))  # lock released on error
        code, _ = self.run_pull()  # next run converges
        self.assertEqual((code, read(target)), (0, "# 회의록 v2\n"))

    def test_refuses_a_folder_that_is_not_a_vault(self):
        shutil.rmtree(os.path.join(self.vault, ".obsidian"))
        code, lines = self.run_pull()
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(self.dst))

    def test_path_escaping_lavcca_is_refused(self):
        with self.assertRaises(ValueError):
            op.apply(self.dst, os.path.join(self.state, "t"), {"../NATAM/x.md": b"x"}, ["../NATAM/x.md"], [], [])
        self.assert_personal_untouched()

    def test_concurrent_run_is_refused_and_stale_lock_taken_over(self):
        os.makedirs(self.state)
        lock = os.path.join(self.state, "pull.lock")
        write(lock, "123")
        self.assertEqual(self.run_pull()[0], 4)
        os.utime(lock, (0, 0))  # older than an hour: crashed run
        self.assertEqual(self.run_pull()[0], 0)

    def test_main_runs_without_stdout_like_pythonw_and_logs_to_file(self):
        # regression: the scheduled task runs pythonw, where sys.stdout is None; reconfigure() crashed with exit 1
        log = os.path.join(self.state, "logs", "pull.log")
        argv = ["obsidian_pull.py", "--vault", self.vault, "--repo", self.repo, "--state", self.state,
                "--no-fetch", "--apply", "--log", log]
        with mock.patch.object(op.sys, "argv", argv), mock.patch.object(op.sys, "stdout", None):
            with self.assertRaises(SystemExit) as e:
                op.main()
        self.assertEqual(e.exception.code, 0)
        self.assertIn('"create": 3', read(log))
        self.assertTrue(os.path.isfile(os.path.join(self.dst, "LAVCCA.md")))

    # ---- every-minute change detection (git mocked) ----
    def run_fetch(self, remote="sha1", t=1_000_000.0, me="PC-A", local=None, update_err=None):
        lines, calls = [], []
        with mock.patch.object(op, "remote_head", return_value=(remote, None)), \
                mock.patch.object(op, "local_head", return_value=local or remote), \
                mock.patch.object(op, "git_update", side_effect=lambda r: calls.append(r) or update_err):
            code = op.run(self.vault, self.repo, self.state, True, fetch=True, out=lines.append,
                          now=lambda: t, me=me)
        return code, lines, calls

    def test_unchanged_docs_and_vault_end_without_reading_files(self):
        self.run_fetch(t=1000.0)
        with mock.patch.object(op, "plan", side_effect=AssertionError("full compare must not run")), \
                mock.patch.object(op, "desired_files", side_effect=AssertionError("no reads")):
            code, lines, _ = self.run_fetch(t=1060.0)
        self.assertEqual((code, lines), (0, []))
        self.assertEqual(op.load_state(self.state)["checked_at"], 1060.0)

    def test_new_docs_commit_is_fetched_and_applied(self):
        self.run_fetch(remote="sha1", t=1000.0)
        write(os.path.join(self.src, "LAVCCA", "회의록.md"), "# 회의록 v2\n")  # what the fetch brought
        code, lines, calls = self.run_fetch(remote="sha2", local="sha1", t=1060.0)
        self.assertEqual(calls, [self.repo])
        self.assertIn('"update": 1', lines[0])
        self.assertEqual(read(os.path.join(self.dst, "LAVCCA", "회의록.md")), "# 회의록 v2\n")

    def test_local_edit_is_detected_within_a_minute_and_overwritten(self):
        self.run_fetch(t=1000.0)
        p = os.path.join(self.dst, "LAVCCA", "회의록.md")
        write(p, "로컬 수정")
        code, lines, _ = self.run_fetch(t=1060.0)
        self.assertIn('"update": 1', lines[0])
        self.assertIn("# 회의록", read(p))

    def test_full_byte_check_runs_hourly_even_when_stat_is_unchanged(self):
        self.run_fetch(t=1000.0)
        p = os.path.join(self.dst, "LAVCCA", "회의록.md")
        st = os.stat(p)
        write(p, "X" * st.st_size)  # same size, then same mtime: invisible to the quick check
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(self.run_fetch(t=1000.0 + 3000)[1], [])
        code, lines, _ = self.run_fetch(t=1000.0 + op.FULL_CHECK_EVERY + 1)
        self.assertIn('"update": 1', lines[0])

    def test_sign_in_failure_is_logged_once_and_writes_nothing(self):
        err = "GitHub sign-in needed (ls-remote). Run once in PowerShell: python x --login"
        lines = []
        with mock.patch.object(op, "remote_head", return_value=(None, err)):
            for t in (1000.0, 1060.0):
                self.assertEqual(op.run(self.vault, self.repo, self.state, True, out=lines.append,
                                        now=lambda: t, me="PC-A"), 3)
        self.assertEqual(lines, [err])  # not repeated every minute
        self.assertFalse(os.path.exists(self.dst))
        self.assertEqual(op.load_state(self.state)["last_error"], err)

    def test_remote_failure_leaves_an_existing_lavcca_untouched(self):
        self.run_fetch(remote="sha1", t=1000.0)
        snapshot = lambda: {r: open(p, "rb").read() for r, p in op.existing_files(self.dst).items()}  # noqa: E731
        before, marker = snapshot(), read(os.path.join(self.dst, op.OWNER_FILE))
        write(os.path.join(self.src, "LAVCCA", "회의록.md"), "# v2\n")  # even with newer content on disk
        for err in ("git ls-remote failed (network or GitHub, retried next run): Could not resolve host",
                    "GitHub sign-in needed (ls-remote). Run once in PowerShell: python x --login"):
            with mock.patch.object(op, "remote_head", return_value=(None, err)):
                code = op.run(self.vault, self.repo, self.state, True, out=lambda s: None,
                              now=lambda: 1000.0 + op.OWNER_REFRESH + 60, me="PC-A")
            self.assertEqual(code, 3)
            self.assertEqual(snapshot(), before)
            self.assertEqual(read(os.path.join(self.dst, op.OWNER_FILE)), marker)  # not even the marker
        # fetch/merge failing after a successful ls-remote is the same: nothing written
        code, _, _ = self.run_fetch(remote="sha2", local="sha1", t=1000.0 + 120, update_err="git merge failed")
        self.assertEqual((code, snapshot()), (3, before))
        self.assertFalse(os.path.isdir(os.path.join(self.state, "trash")))
        self.assert_personal_untouched()

    def test_trash_keeps_30_days_and_prunes_older_runs(self):
        trash = os.path.join(self.state, "trash")
        old, recent = os.path.join(trash, "20260901T000000Z"), os.path.join(trash, "20261020T000000Z")
        for d in (old, recent):
            write(os.path.join(d, "LAVCCA", "x.md"), "이전 판")
        now = 2_000_000_000.0
        os.utime(old, (now - 31 * 86400, now - 31 * 86400))
        os.utime(recent, (now - 29 * 86400, now - 29 * 86400))
        op._prune_trash(trash, now)
        self.assertEqual(sorted(os.listdir(trash)), ["20261020T000000Z"])

    def test_git_runs_without_a_console_window(self):
        # regression: under pythonw every minute's `git ls-remote` opened a visible terminal window
        seen = {}

        def fake(cmd, **kw):
            seen.update(kw)
            return mock.Mock(returncode=0, stdout="abc\trefs/heads/main\n", stderr="")
        with mock.patch.object(op.subprocess, "run", side_effect=fake):
            self.assertEqual(op.remote_head(self.repo), ("abc", None))
        self.assertEqual(seen["creationflags"], getattr(op.subprocess, "CREATE_NO_WINDOW", 0))
        if os.name == "nt":
            self.assertEqual(seen["creationflags"], 0x08000000)

    def test_git_never_prompts_and_auth_errors_point_to_login(self):
        seen = {}

        def fake(cmd, **kw):
            seen.update(kw["env"])
            return mock.Mock(returncode=128, stdout="", stderr="fatal: could not read Username: terminal prompts disabled")
        with mock.patch.object(op.subprocess, "run", side_effect=fake):
            sha, err = op.remote_head(self.repo)
        self.assertIsNone(sha)
        self.assertEqual((seen["GIT_TERMINAL_PROMPT"], seen["GCM_INTERACTIVE"]), ("0", "never"))
        self.assertIn("--login", err)
        with mock.patch.object(op.subprocess, "run", return_value=mock.Mock(returncode=128, stdout="",
                                                                            stderr="Could not resolve host: github.com")):
            self.assertIn("network", op.remote_head(self.repo)[1])

    # ---- one writer per OneDrive vault ----
    def owner(self):
        return op.read_owner(self.dst)

    def test_second_pc_stands_by_while_the_owner_is_fresh(self):
        self.run_fetch(t=1000.0, me="PC-A")
        snapshot = lambda: {r: open(p, "rb").read() for r, p in op.existing_files(self.dst).items()}  # noqa: E731
        before = snapshot()
        os.remove(os.path.join(self.state, op.STATE_FILE))  # PC-B has its own state
        write(os.path.join(self.src, "LAVCCA", "회의록.md"), "# v2\n")
        with mock.patch.object(op, "plan", side_effect=AssertionError("standby must not compare or write")):
            code, lines, _ = self.run_fetch(t=1000.0 + 60, me="PC-B")
        self.assertEqual((code, lines), (0, ["standby: PC-A keeps this vault in sync"]))
        self.assertEqual(snapshot(), before)
        self.assertEqual(self.owner()["host"], "PC-A")

    def test_stale_owner_is_taken_over(self):
        self.run_fetch(t=1000.0, me="PC-A")
        code, lines, _ = self.run_fetch(t=1000.0 + op.OWNER_STALE + 1, me="PC-B")
        self.assertEqual(code, 0)
        self.assertTrue(lines[0].startswith("took over from PC-A"))
        self.assertEqual(self.owner()["host"], "PC-B")

    def test_owner_marker_is_refreshed_every_10_minutes_not_every_run(self):
        self.run_fetch(t=1000.0)
        self.run_fetch(t=1000.0 + 300)
        self.assertEqual(self.owner()["updated_at"], 1000.0)
        self.run_fetch(t=1000.0 + op.OWNER_REFRESH + 1)
        self.assertEqual(self.owner()["updated_at"], 1000.0 + op.OWNER_REFRESH + 1)

    def test_owner_marker_is_not_synced_but_its_conflict_copy_is_trashed(self):
        self.run_fetch(t=1000.0)
        marker = os.path.join(self.dst, op.OWNER_FILE)
        self.assertTrue(os.path.isfile(marker))
        conflict = os.path.join(self.dst, ".lavcca-sync-owner-DESKTOP-B.json")  # OneDrive conflict copy
        write(conflict, '{"host": "PC-B"}')
        code, lines, _ = self.run_fetch(t=1060.0)
        self.assertIn('"delete": 1', lines[0])
        self.assertFalse(os.path.exists(conflict))
        self.assertTrue(os.path.isfile(marker))

    def test_damaged_owner_marker_counts_as_no_owner(self):
        write(os.path.join(self.dst, op.OWNER_FILE), "{not json")
        code, _, _ = self.run_fetch(t=1000.0, me="PC-A")
        self.assertEqual((code, self.owner()["host"]), (0, "PC-A"))

    def test_dry_run_writes_no_marker_and_no_state(self):
        self.run_pull(do_apply=False)
        self.assertFalse(os.path.exists(self.dst))
        self.assertFalse(os.path.exists(os.path.join(self.state, op.STATE_FILE)))

    # ---- status ----
    def test_status_reports_and_warns_after_a_day_without_success(self):
        self.run_fetch(t=1000.0)
        lines = []
        self.assertEqual(op.status(self.vault, self.state, out=lines.append, now=lambda: 2000.0), 0)
        self.assertTrue(any(l.startswith("docs commit    : sha1") for l in lines))
        self.assertTrue(any("vault owner    : PC-A" in l for l in lines))
        self.assertEqual(op.status(self.vault, self.state, out=lines.append, now=lambda: 1000.0 + 90000), 1)
        self.assertIn("WARNING: no successful sync in the last 24 hours", lines)

    def test_installer_runs_every_minute_and_on_battery(self):
        with open(os.path.join(os.path.dirname(op.__file__), "install.ps1"), encoding="utf-8") as fh:
            ps = fh.read()
        for needle in ("-RepetitionInterval (New-TimeSpan -Minutes 1)", "-AllowStartIfOnBatteries",
                       "-DontStopIfGoingOnBatteries", "-MultipleInstances IgnoreNew", "-StartWhenAvailable",
                       "[switch]$Status", "[switch]$Uninstall", "[switch]$Repair", "[switch]$Login"):
            self.assertIn(needle, ps)
        code = "\n".join(l.split(" #")[0] for l in ps.splitlines() if not l.lstrip().startswith("#"))  # no comments
        for forbidden in ("reset", "clean", "Remove-Item"):  # repair sets the docs copy aside, never resets/deletes
            self.assertNotIn(forbidden, code)

    def test_git_failure_stops_before_any_write(self):
        with mock.patch.object(op, "git_update", return_value="git merge failed: not possible to fast-forward"):
            code = op.run(self.vault, self.repo, self.state, True, fetch=True, out=lambda s: None)
        self.assertEqual(code, 3)
        self.assertFalse(os.path.exists(self.dst))


if __name__ == "__main__":
    unittest.main()
