# -*- coding: utf-8 -*-
"""ci/brand_migration.py is read-only: counts, resolution, state consistency, planned hashes."""
import os, sys, json, hashlib, shutil, tempfile, unittest

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CODE_DIR)
import canary_apply as ca  # noqa: E402
from ci import brand_migration as bm  # noqa: E402

FM = "---\nproject: NATAM\nmanaged: notion\nnotion_id: {nid}\nnotion_path: \"NATAM/x {nid}.md\"\n---\n"
A, B = "a1" * 16, "b1" * 16


def tree_hash(root):
    h = hashlib.sha256()
    for dp, dn, fn in sorted(os.walk(root)):
        for n in sorted(fn):
            p = os.path.join(dp, n)
            h.update(p.encode())
            with open(p, "rb") as f:
                h.update(f.read())
    return h.hexdigest()


class TestBrandMigrationDryRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bm_")
        self.vault = os.path.join(self.tmp, "vault")
        self.body_a = "\n[[NATAM/02/b|B]] and ![[NATAM/02/Attachments/x.png]] and [[NATAM/02/missing|M]]\n"
        self.body_b = "\n# B\n"
        self.w("NATAM/02/a.md", FM.format(nid=A) + self.body_a)
        self.w("NATAM/02/b.md", FM.format(nid=B) + self.body_b)
        self.w("NATAM/02/Attachments/x.png", "png")
        self.w("NATAM/06_KNOWLEDGE/human.md", "---\nmanaged: obsidian\n---\nsee [[NATAM/02/a#h|A]]\n")
        self.w("Daily/note.md", "[[NATAM/02/b]] plus plain NATAM/02/b text\n")
        self.w("NATAM/_sync/logs/r.md", "[[NATAM/02/a]] in a log: not counted\n")
        self.w(".obsidian/workspace.json", '{"file": "NATAM/02/a.md"}')
        self.state = {"items": {A: {"obsidian_path": "02/a.md", "local_content_hash": ca.sha256_text(self.body_a),
                                    "api_rendered_content_hash": ca.sha256_text(self.body_a)},
                                B: {"obsidian_path": "02/b.md", "local_content_hash": ca.sha256_text(self.body_b),
                                    "api_rendered_content_hash": "other"}}}
        self.w("NATAM/_sync/state.json", json.dumps(self.state))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def w(self, rel, text):
        p = os.path.join(self.vault, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def test_counts_resolution_and_kinds(self):
        res = bm.scan(self.vault, state=self.state)
        t = res["totals"]
        self.assertEqual(t["links"], 5)                       # 3 in a.md, 1 human, 1 daily; logs excluded
        self.assertEqual(t["links_unresolved"], 1)            # 02/missing
        self.assertEqual((t["managed_files_with_links"], t["human_files_with_links"]), (1, 2))
        by = {f["path"]: f for f in res["files"]}
        self.assertEqual(by["NATAM/02/a.md"]["unresolved_targets"], ["02/missing"])
        self.assertFalse(by["Daily/note.md"]["managed"])

    def test_state_consistent_and_planned_hash(self):
        res = bm.scan(self.vault, state=self.state)
        self.assertEqual(res["state_inconsistent"], [])
        chk = {c["notion_id"]: c for c in res["state_checks"]}
        self.assertEqual(chk[A]["new_body_hash"], ca.sha256_text(self.body_a.replace("[[NATAM/", "[[LAVCCA/")))
        self.assertNotIn("new_body_hash", chk[B])             # no links: hash unchanged by the rename
        self.assertFalse(chk[B]["api_hash_matches_local"])
        self.assertEqual(res["totals"]["state_items_needing_new_hash"], 1)

    def test_detects_state_drift_and_missing_files(self):
        self.state["items"][B]["local_content_hash"] = "stale"
        self.state["items"]["c1" * 16] = {"obsidian_path": "02/gone.md", "local_content_hash": "x"}
        res = bm.scan(self.vault, state=self.state)
        problems = {(c["notion_id"], c.get("problem")) for c in res["state_inconsistent"]}
        self.assertEqual(problems, {(B, None), ("c1" * 16, "file missing")})

    def test_rewrite_only_touches_wikilinks(self):
        text = "[[NATAM/a|x]] ![[NATAM/b.png]] [[NATAMX/c]] NATAM/d [t](NATAM/e)"
        self.assertEqual(bm.rewrite_links(text, "NATAM", "LAVCCA"),
                         "[[LAVCCA/a|x]] ![[LAVCCA/b.png]] [[NATAMX/c]] NATAM/d [t](NATAM/e)")

    def test_cli_never_writes_the_vault_and_refuses_report_inside(self):
        before = tree_hash(self.vault)
        out = os.path.join(self.tmp, "report.json")
        self.assertEqual(bm.main(["--vault", self.vault, "--report", out, "--expect-links", "5"]), 0)
        self.assertEqual(bm.main(["--vault", self.vault, "--report", out, "--expect-links", "538"]), 1)
        self.assertEqual(bm.main(["--vault", self.vault, "--report", os.path.join(self.vault, "r.json")]), 2)
        self.assertEqual(tree_hash(self.vault), before)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["totals"]["links"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
