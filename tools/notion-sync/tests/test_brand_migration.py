# -*- coding: utf-8 -*-
"""NATAM -> LAVCCA rename: pre-rename config keys, environment variables and frontmatter
values stay valid so an unmigrated vault keeps syncing."""
import os, sys, unittest
from unittest import mock

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CODE_DIR)
import canary_apply as ca  # noqa: E402

NID = "ab" * 16


class TestNormalizeConfig(unittest.TestCase):
    def test_legacy_keys_mapped(self):
        out = ca.normalize_config({"natam_root_path": "V/NATAM", "natam_root_id": NID, "vault_root": "V", "owner_roots": {}})
        self.assertEqual(out, {"lavcca_root_path": "V/NATAM", "lavcca_root_id": NID, "vault_root": "V", "owner_roots": {}})

    def test_new_keys_untouched_and_input_not_mutated(self):
        cfg = {"lavcca_root_path": "V/LAVCCA", "lavcca_root_id": NID, "owner_roots": {}}
        self.assertEqual(ca.normalize_config(cfg), cfg)
        legacy = {"natam_root_path": "V/NATAM", "owner_roots": {}}
        ca.normalize_config(legacy)
        self.assertEqual(legacy, {"natam_root_path": "V/NATAM", "owner_roots": {}})

    def test_both_equal_ok_both_different_refused(self):
        self.assertEqual(ca.normalize_config({"lavcca_root_id": NID, "natam_root_id": NID, "owner_roots": {}}),
                         {"lavcca_root_id": NID, "owner_roots": {}})
        with self.assertRaises(ValueError):
            ca.normalize_config({"lavcca_root_path": "V/LAVCCA", "natam_root_path": "V/NATAM", "owner_roots": {}})

    def test_owner_roots_required(self):
        with self.assertRaises(ValueError):
            ca.normalize_config({"lavcca_root_path": "V/LAVCCA"})


class TestEnvFallback(unittest.TestCase):
    def test_precedence(self):
        with mock.patch.dict(os.environ, {"NATAM_NETWORK_WAIT_S": "5"}, clear=False):
            os.environ.pop("LAVCCA_NETWORK_WAIT_S", None)
            self.assertEqual(ca.env("NETWORK_WAIT_S"), "5")
            with mock.patch.dict(os.environ, {"LAVCCA_NETWORK_WAIT_S": "0"}):
                self.assertEqual(ca.env("NETWORK_WAIT_S"), "0")   # new name wins, even when falsy
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ca.env("NETWORK_WAIT_S", "120"), "120")


class TestProjectValue(unittest.TestCase):
    def fm(self, project):
        return {"project": project, "owner": "x", "source": "notion", "managed": "notion",
                "notion_id": NID, "notion_path": "p"}

    def test_both_brands_accepted_other_refused(self):
        for p in ("LAVCCA", "NATAM"):
            ca.validate_identity(self.fm(p), NID)
        with self.assertRaises(ca.CanaryRefused):
            ca.validate_identity(self.fm("OTHER"), NID)


if __name__ == "__main__":
    unittest.main(verbosity=2)
