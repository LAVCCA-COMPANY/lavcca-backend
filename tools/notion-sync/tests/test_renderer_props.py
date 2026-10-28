# -*- coding: utf-8 -*-
"""Offline tests: database-row properties, markdown link parsing with brackets,
attachment mapping, empty file blocks, render determinism."""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import canary_apply as ca  # noqa: E402

DB_PARENT = {"type": "data_source_id", "data_source_id": "e" * 32, "database_id": "d" * 32}


def title(t):
    return {"type": "title", "title": [{"plain_text": t}]}


def text(t):
    return {"type": "rich_text", "rich_text": [{"plain_text": t}] if t else []}


def date(start, end=None, tz=None):
    return {"type": "date", "date": {"start": start, "end": end, "time_zone": tz} if start else None}


def msel(*names):
    return {"type": "multi_select", "multi_select": [{"name": n} for n in names]}


def row(props, parent=DB_PARENT):
    return {"parent": parent, "properties": props}


def file_block(bid, name, url="https://prod-files-secure.s3.us-west-2.amazonaws.com/x/y?X-Amz-Expires=3600&sig=1",
               ftype="file"):
    data = {"type": ftype, "caption": [], "name": name}
    data[ftype] = {"url": url, "expiry_time": "2026-10-01T17:47:28.057Z"} if ftype == "file" else {"url": url}
    return {"object": "block", "id": bid, "type": "file", "file": data, "has_children": False, "_children": []}


class TestProperties(unittest.TestCase):
    def render(self, page, blocks=(), ttl="T"):
        return ca.render_body(ttl, list(blocks), ca.RenderContext({}, ""), page=page)

    def test_aws_style_single_date_matches_migrated_layout(self):
        body = self.render(row({"이름": title("AWS 상담"), "날짜": date("2026-05-13"), "태그": msel()}), ttl="AWS 상담")
        # migrated file: "\n# AWS 상담\n\n날짜: 2026년 5월 13일" (+ trailing newline from renderer)
        self.assertEqual(body, "\n# AWS 상담\n\n날짜: 2026년 5월 13일\n")

    def test_range_and_text_order_matches_migrated_file(self):
        body = self.render(row({"Name": title("휴가/여행"), "Text": text("회의 참여는 어렵고"),
                                "Date": date("2026-03-25", "2026-04-01")}), ttl="휴가/여행")
        self.assertEqual(body, "\n# 휴가/여행\n\nDate: 2026년 3월 25일 → 2026년 4월 1일\nText: 회의 참여는 어렵고\n")

    def test_empty_properties_omitted_title_skipped(self):
        body = self.render(row({"Name": title("x"), "Text": text(""), "Date": date(None), "Tags": msel()}))
        self.assertEqual(body, "\n# T\n")

    def test_multi_select_values(self):
        body = self.render(row({"Name": title("x"), "Tags": msel("a", "b")}))
        self.assertIn("Tags: a, b", body)

    def test_properties_then_blocks(self):
        blocks = [{"object": "block", "id": "1" * 32, "type": "paragraph",
                   "paragraph": {"rich_text": [{"type": "text", "plain_text": "본문", "annotations": {}}]},
                   "has_children": False, "_children": []}]
        body = self.render(row({"날짜": date("2026-03-02")}), blocks)
        self.assertEqual(body, "\n# T\n\n날짜: 2026년 3월 2일\n\n본문\n")

    def test_non_database_page_gets_no_property_lines(self):
        page = row({"title": title("x"), "Text": text("ignored")}, parent={"type": "page_id", "page_id": "a" * 32})
        self.assertEqual(self.render(page), "\n# T\n")
        self.assertEqual(self.render(None), "\n# T\n")

    def test_unsupported_property_type_is_critical(self):
        with self.assertRaises(ca.RenderUnsupported) as cm:
            self.render(row({"Name": title("x"), "Done": {"type": "checkbox", "checkbox": False}}))
        self.assertEqual(cm.exception.items[0]["type"], "UNSUPPORTED_PROPERTY_TYPE:checkbox")

    def test_date_with_time_is_critical(self):
        for d in (date("2026-03-25T10:00:00.000+09:00"), date("2026-03-25", tz="Asia/Seoul"),
                  date("2026-03-25", "2026-03-26T09:00:00.000Z")):
            with self.assertRaises(ca.RenderUnsupported) as cm:
                self.render(row({"Date": d}))
            self.assertEqual(cm.exception.items[0]["type"], "UNSUPPORTED_PROPERTY_VALUE:date_with_time")

    def test_property_order_is_deterministic(self):
        a = row({"B": text("2"), "A": text("1"), "C": text("3")})
        b = row({"C": text("3"), "A": text("1"), "B": text("2")})
        self.assertEqual(self.render(a), self.render(b))
        self.assertIn("A: 1\nB: 2\nC: 3", self.render(a))


class TestLinkParsing(unittest.TestCase):
    def test_nested_brackets_in_text(self):
        links = ca.parse_md_links("[사업 계획서 [자동 저장].pptx](../01_SHARED/Business/사업_계획서_자동_저장.pptx)")
        self.assertEqual(links, [(False, "사업 계획서 [자동 저장].pptx", "../01_SHARED/Business/사업_계획서_자동_저장.pptx")])

    def test_escaped_brackets(self):
        links = ca.parse_md_links(r"[a \[b\] c](x.docx)")
        self.assertEqual(links, [(False, "a [b] c", "x.docx")])

    def test_parens_in_target_and_image_and_angle(self):
        text = "![img](a (1).png) and [d](<b c.pdf>) and [e](x_(v1.0).docx)"
        self.assertEqual(ca.parse_md_links(text), [(True, "img", "a (1).png"), (False, "d", "b c.pdf"),
                                                   (False, "e", "x_(v1.0).docx")])

    def test_wikilinks_and_plain_brackets_are_not_links(self):
        self.assertEqual(ca.parse_md_links("[[LAVCCA/x|y]] [not a link] [z]"), [])

    def test_existing_simple_links_unchanged(self):
        self.assertEqual(ca.parse_md_links("[리포트 목차.docx](리포트_목차.docx)"),
                         [(False, "리포트 목차.docx", "리포트_목차.docx")])


class TestAttachments(unittest.TestCase):
    LOCAL = "\n# NATAM\n\n[사업 계획서 [자동 저장].pptx](../01_SHARED/Business/사업_계획서_자동_저장.pptx)\n"

    def test_bracket_name_maps_to_existing_local_attachment(self):
        ctx = ca.RenderContext({}, self.LOCAL)
        out = ca.render_blocks([file_block("1" * 32, "사업 계획서 [자동 저장].pptx")], ctx)
        self.assertEqual(ctx.critical, [])
        self.assertEqual(out, "[사업 계획서 [자동 저장].pptx](../01_SHARED/Business/사업_계획서_자동_저장.pptx)")
        self.assertNotIn("amazonaws", out)

    def test_signed_url_does_not_affect_hash(self):
        b1 = ca.render_body("N", [file_block("1" * 32, "사업 계획서 [자동 저장].pptx", url="https://s3/x?sig=AAA")],
                            ca.RenderContext({}, self.LOCAL))
        b2 = ca.render_body("N", [file_block("1" * 32, "사업 계획서 [자동 저장].pptx", url="https://s3/x?sig=BBB")],
                            ca.RenderContext({}, self.LOCAL))
        self.assertEqual(ca.sha256_text(b1), ca.sha256_text(b2))

    def test_ambiguous_local_match_is_critical(self):
        local = self.LOCAL + "[사업 계획서 [자동 저장].pptx](other/사업_계획서.pptx)\n"
        ctx = ca.RenderContext({}, local)
        ca.render_blocks([file_block("1" * 32, "사업 계획서 [자동 저장].pptx")], ctx)
        self.assertEqual(ctx.critical[0]["type"], "file:notion_hosted_unresolved")

    def test_empty_external_file_block_is_critical_not_dropped(self):
        ctx = ca.RenderContext({}, "")
        out = ca.render_blocks([file_block("2" * 32, "", url="", ftype="external")], ctx)
        self.assertEqual(ctx.critical, [{"type": "file:empty_external_block", "id": "2" * 32}])
        self.assertIn("without url", out)
        with self.assertRaises(ca.RenderUnsupported):
            ca.render_body("x", [file_block("2" * 32, "", url="", ftype="external")], ca.RenderContext({}, ""))


class TestDeterminism(unittest.TestCase):
    def test_same_input_same_hash(self):
        page = row({"날짜": date("2026-05-13"), "태그": msel("x"), "이름": title("t")})
        blocks = [file_block("1" * 32, "사업 계획서 [자동 저장].pptx")]
        h = {ca.sha256_text(ca.render_body("t", blocks, ca.RenderContext({}, TestAttachments.LOCAL), page=page))
             for _ in range(5)}
        self.assertEqual(len(h), 1)


class TestRootPageInScope(unittest.TestCase):
    def test_lavcca_root_page_itself_passes_descendant_check(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_canary_apply import CanaryTestBase, ROOT, fm

        class _T(CanaryTestBase):
            def runTest(self):
                p = os.path.join(self.lavcca, "00_HOME", "NATAM.md")
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8", newline="\n") as f:
                    f.write(fm(ROOT) + "\n# NATAM\n")
                os.utime(p, (0, 0))
                self.api.pages[ROOT].update({"id": ROOT, "archived": False, "in_trash": False,
                                             "last_edited_time": "t", "properties": {}})
                self.api.children[ROOT] = []
                res = self.run_canary(ROOT, apply=False)
                self.assertEqual(res["status"], "DRY_RUN_OK", res)
                self.assertIn("LAVCCA root itself", res["parent_chain"][0])

        r = unittest.TextTestRunner(stream=open(os.devnull, "w")).run(_T())
        self.assertTrue(r.wasSuccessful(), r.failures + r.errors)


if __name__ == "__main__":
    unittest.main(verbosity=2)
