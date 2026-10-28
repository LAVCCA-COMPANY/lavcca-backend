# -*- coding: utf-8 -*-
"""Offline tests: child_database -> verified local CSV link (migration link map).
Regression for the 박멤버.md case where an UPDATE replaced a working local CSV link
with the child_database placeholder."""
import os, sys, csv, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_canary_apply import blk, rt  # noqa: E402
from test_production_apply import ProdBase, U, RUN_AT  # noqa: E402
import canary_apply as ca  # noqa: E402
import baseline as bl  # noqa: E402

DBID = "d1" * 16
U_REL = "01_SHARED/u.md"
HEADER = ["status", "source_obsidian_path", "source_notion_path", "link_text", "raw_href_or_wikilink_target",
          "target_obsidian_path", "target_notion_id", "note"]


class LinkMapBase(ProdBase):
    def write_map(self, rows, csv_files=("01_SHARED/제목 없음.csv",)):
        os.makedirs(os.path.join(self.lavcca, "_migration"), exist_ok=True)
        with open(os.path.join(self.lavcca, "_migration", "link-mapping.csv"), "w", encoding="utf-8-sig",
                  newline="") as f:
            w = csv.writer(f)
            w.writerow(HEADER)
            for r in rows:
                w.writerow([r.get(k, "") for k in HEADER])
        for rel in csv_files:
            p = os.path.join(self.lavcca, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write("a,b\n")

    @staticmethod
    def row(**kw):
        r = {"status": "resolved", "source_obsidian_path": U_REL, "link_text": "제목 없음",
             "raw_href_or_wikilink_target": "제목 없음.csv", "target_obsidian_path": "01_SHARED/제목 없음.csv",
             "target_notion_id": DBID}
        r.update(kw)
        return r

    def links(self):
        return ca.load_child_database_links(self.lavcca)


class TestLoader(LinkMapBase):
    def test_resolved_existing_csv_is_mapped(self):
        self.write_map([self.row()])
        self.assertEqual(self.links(), {U_REL: {DBID: ("제목 없음", "제목 없음.csv")}})

    def test_excluded_or_missing_target_not_mapped(self):
        # 이멤버 case: status=excluded, no target file -> never repaired
        self.write_map([self.row(status="excluded", target_obsidian_path="")], csv_files=())
        self.assertEqual(self.links(), {})
        self.write_map([self.row()], csv_files=())  # resolved but the CSV does not exist
        self.assertEqual(self.links(), {})

    def test_href_must_resolve_to_target(self):
        self.write_map([self.row(raw_href_or_wikilink_target="other.csv")])
        self.assertEqual(self.links(), {})
        self.write_map([self.row(raw_href_or_wikilink_target="https://x/제목 없음.csv")])
        self.assertEqual(self.links(), {})

    def test_percent_encoded_href_resolving_to_target_is_kept_verbatim(self):
        self.write_map([self.row(raw_href_or_wikilink_target="%EC%A0%9C%EB%AA%A9%20%EC%97%86%EC%9D%8C.csv")])
        self.assertEqual(self.links()[U_REL][DBID][1], "%EC%A0%9C%EB%AA%A9%20%EC%97%86%EC%9D%8C.csv")

    def test_ambiguous_duplicate_not_mapped(self):
        self.write_map([self.row(), self.row(link_text="다른 이름")])
        self.assertEqual(self.links(), {})

    def test_non_csv_or_bad_id_not_mapped(self):
        self.write_map([self.row(target_obsidian_path="01_SHARED/x.md", raw_href_or_wikilink_target="x.md")])
        self.assertEqual(self.links(), {})
        self.write_map([self.row(target_notion_id="not-an-id")])
        self.assertEqual(self.links(), {})

    def test_no_map_file(self):
        self.assertEqual(self.links(), {})


class TestRender(unittest.TestCase):
    def db_block(self):
        return blk(DBID, "child_database", {"title": "New database"})

    def test_mapped_database_renders_local_csv_link(self):
        ctx = ca.RenderContext({}, "", db_links={DBID: ("제목 없음", "제목 없음.csv")})
        out = ca.render_blocks([self.db_block()], ctx)
        self.assertEqual(out, "[제목 없음](제목 없음.csv)")
        self.assertEqual(ctx.placeholders, [])

    def test_unmapped_database_keeps_placeholder(self):
        for ctx in (ca.RenderContext({}, ""), ca.RenderContext({}, "", db_links={"0" * 32: ("x", "x.csv")})):
            out = ca.render_blocks([self.db_block()], ctx)
            self.assertIn("<!-- notion-sync: child_database", out)
            self.assertEqual(ctx.placeholders[0]["kind"], "child_database")


class TestPipeline(LinkMapBase):
    """Page with a mapped child_database: an UPDATE keeps the CSV link, the next run is
    UNCHANGED, and a later Notion edit (UPDATE) still does not turn it into a placeholder."""

    def setUp(self):
        super().setUp()
        self.write_map([self.row()])

    def classify(self):
        st = self.state()
        body = ca.split_frontmatter_strict(self._read(self.u_path).decode("utf-8"))[2]
        id_map = ca.id_to_vault_path(ca.scan_vault_identities(self.cfg))
        h = bl.render_current(self.api, U, self.node(U), body, self.cfg, id_map,
                              db_links=self.links().get(U_REL))
        return bl.classify_incremental(bl.baseline_view(st["items"][U]), self.api.pages[U]["last_edited_time"], h,
                                       ca.sha256_text(body))[0]

    def test_update_keeps_link_then_unchanged_then_update_again(self):
        self.api.children[U] = list(self.api.children[U]) + [blk(DBID, "child_database", {"title": "New database"})]
        self.edit_remote(U, "첫 수정")
        res = self.run_prod(self.upd(U))
        self.assertEqual(res["status"], "ALL_APPLIED", res)
        text = self._read(self.u_path).decode("utf-8")
        self.assertIn("\n[제목 없음](제목 없음.csv)\n", text)
        self.assertNotIn("child_database", text)
        self.assertEqual(self.classify(), bl.UNCHANGED)
        # next Notion UPDATE: link must survive
        self.edit_remote(U, "두번째 수정")
        self.api.pages[U]["last_edited_time"] = "2026-10-05T00:00:00.000Z"
        self.assertEqual(self.classify(), bl.UPDATE_CONFIRMED)
        res = prod_run(self, RUN_AT.replace(hour=5))
        self.assertEqual(res["status"], "ALL_APPLIED", res)
        text = self._read(self.u_path).decode("utf-8")
        self.assertIn("\n[제목 없음](제목 없음.csv)\n", text)
        self.assertIn("두번째 수정", text)
        self.assertNotIn("child_database", text)

    def test_csv_removed_falls_back_to_placeholder(self):
        os.remove(os.path.join(self.lavcca, "01_SHARED", "제목 없음.csv"))
        self.api.children[U] = list(self.api.children[U]) + [blk(DBID, "child_database", {"title": "New database"})]
        self.edit_remote(U)
        res = self.run_prod(self.upd(U))
        self.assertEqual(res["status"], "ALL_APPLIED", res)
        self.assertIn("child_database", self._read(self.u_path).decode("utf-8"))


def prod_run(t, now):
    import production_apply as prod
    return prod.run_production(t.upd(U), True, [], t.api, t.cfg, t.state_path, t.sync,
                               ca.scan_vault_identities(t.cfg), now, apply=True, md_snapshot=t.vault_md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
