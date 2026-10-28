# -*- coding: utf-8 -*-
"""Offline tests for secret_loader (fake tokens only)."""
import os, sys, tempfile, shutil, unittest

SYNC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SYNC_DIR)
import secret_loader as sl  # noqa: E402

FAKE = "ntn_FAKE_TEST_TOKEN_0000"


class TestSecretLoader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="secret_test_")
        self.secret = os.path.join(self.tmp, "home", ".lavcca", "secrets.env")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_secret(self, content):
        os.makedirs(os.path.dirname(self.secret), exist_ok=True)
        with open(self.secret, "w", encoding="utf-8") as f:
            f.write(content)

    def test_environment_variable_wins(self):
        self.write_secret("NOTION_TOKEN=from_file\n")
        self.assertEqual(sl.load_notion_token({"NOTION_TOKEN": FAKE}, self.secret), (FAKE, "environment"))

    def test_secret_file_used_when_env_absent_or_empty(self):
        self.write_secret(f'# comment\nOTHER=x\nNOTION_TOKEN="{FAKE}"\n')
        self.assertEqual(sl.load_notion_token({}, self.secret), (FAKE, self.secret))
        self.assertEqual(sl.load_notion_token({"NOTION_TOKEN": "  "}, self.secret), (FAKE, self.secret))

    def test_not_configured_error(self):
        with self.assertRaises(sl.TokenNotConfigured) as cm:
            sl.load_notion_token({}, self.secret)
        self.assertEqual(cm.exception.code, "TOKEN_NOT_CONFIGURED")

    def test_empty_value_in_file_is_not_configured(self):
        self.write_secret("NOTION_TOKEN=\n")
        with self.assertRaises(sl.TokenNotConfigured):
            sl.load_notion_token({}, self.secret)

    def test_vault_env_is_never_a_fallback(self):
        vault_env = os.path.join(self.tmp, "vault", ".env")
        os.makedirs(os.path.dirname(vault_env))
        with open(vault_env, "w", encoding="utf-8") as f:
            f.write(f"NOTION_TOKEN={FAKE}\n")
        cwd = os.getcwd()
        try:
            os.chdir(os.path.dirname(vault_env))  # even when running from the vault root
            with self.assertRaises(sl.TokenNotConfigured):
                sl.load_notion_token({}, self.secret)
        finally:
            os.chdir(cwd)

    def test_error_message_never_contains_a_token(self):
        self.write_secret("NOTION_TOKEN=\n")
        with self.assertRaises(sl.TokenNotConfigured) as cm:
            sl.load_notion_token({}, self.secret)
        self.assertNotIn(FAKE, str(cm.exception))

    def test_default_path_is_home_relative(self):
        import inspect
        src = inspect.getsource(sl)
        self.assertIn('home / ".lavcca" / "secrets.env"', src)
        self.assertNotIn("C:\\\\Users", src)
        self.assertNotIn("USER", src.replace("NOTION_TOKEN", ""))

    def test_new_location_preferred_legacy_location_still_read(self):
        home = os.path.join(self.tmp, "home")
        legacy = os.path.join(home, ".natam", "secrets.env")
        os.makedirs(os.path.dirname(legacy))
        with open(legacy, "w", encoding="utf-8") as f:
            f.write("NOTION_TOKEN=legacy_token\n")
        self.assertEqual(sl.load_notion_token({}, home=home), ("legacy_token", legacy))
        new = os.path.join(home, ".lavcca", "secrets.env")
        os.makedirs(os.path.dirname(new))
        with open(new, "w", encoding="utf-8") as f:
            f.write(f"NOTION_TOKEN={FAKE}\n")
        self.assertEqual(sl.load_notion_token({}, home=home), (FAKE, new))
        with self.assertRaises(sl.TokenNotConfigured) as cm:
            sl.load_notion_token({}, home=os.path.join(self.tmp, "empty"))
        self.assertIn(".lavcca", str(cm.exception))

    def test_sync_engine_does_not_reference_vault_env(self):
        with open(os.path.join(SYNC_DIR, "notion_sync.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("secret_loader.load_notion_token()", src)
        self.assertNotIn('CFG["env_path"]', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
