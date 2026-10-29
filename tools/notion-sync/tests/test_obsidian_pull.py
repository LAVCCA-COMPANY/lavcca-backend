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

    def test_second_run_changes_nothing(self):
        self.run_pull()
        code, lines = self.run_pull()
        self.assertIn('"create": 0, "update": 0, "delete": 0', lines[0])
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

    def test_git_failure_stops_before_any_write(self):
        with mock.patch.object(op, "git_update", return_value="git merge failed: not possible to fast-forward"):
            code = op.run(self.vault, self.repo, self.state, True, fetch=True, out=lambda s: None)
        self.assertEqual(code, 3)
        self.assertFalse(os.path.exists(self.dst))


if __name__ == "__main__":
    unittest.main()
