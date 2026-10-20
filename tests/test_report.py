"""排版 / 闸门的测试：评论清洗、私信长度、日报段首格式。"""

from __future__ import annotations

import unittest

from biliduty import report

PICK = {
    "bvid": "BV1aaaaaaaa1",
    "title": "标题 & <标签>",
    "author": "某UP",
    "play": 123456,
    "duration": "12:34",
    "why": "说了具体的事。",
    "one_line": "一句话结论",
}


class PickSectionTest(unittest.TestCase):
    def test_full_section_starts_with_pick_heading(self):
        text = report.pick_section(PICK, "full")
        self.assertTrue(text.startswith("### BV1aaaaaaaa1\n"))
        self.assertIn("为什么值得：", text)
        self.assertIn("https://www.bilibili.com/video/BV1aaaaaaaa1", text)
        self.assertIn("12.3万", text)

    def test_brief_is_short(self):
        text = report.pick_section(PICK, "brief")
        self.assertTrue(text.startswith("### BV1aaaaaaaa1\n"))
        self.assertLess(len(text.splitlines()), 4)
        self.assertNotIn("为什么值得：", text)

    def test_footer_reports_no_subtitle_count(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        text = report.footer(8, 2, 3, datetime(2026, 10, 20, 12, 3, tzinfo=ZoneInfo("Asia/Shanghai")))
        self.assertIn("因无字幕跳过 3 条", text)
        self.assertIn("2026-10-20 12:03", text)


class CommentGateTest(unittest.TestCase):
    def test_adds_mention_when_missing(self):
        text = report.sanitize_comment("这条讲得挺清楚", "Tester")
        self.assertEqual(text, "这条讲得挺清楚 @Tester")

    def test_keeps_plain_mention(self):
        self.assertEqual(report.sanitize_comment("@Tester 这条讲得清楚", "Tester"), "@Tester 这条讲得清楚")

    def test_normalizes_markup_mention(self):
        self.assertIn("@Tester", report.sanitize_comment("<@Tester> 看看", "Tester"))

    def test_rejects_internal_terms(self):
        for text in ("判重表已记", "本轮 token 用了 123", "设置页里写着", "dmMode=only"):
            self.assertIsNone(report.sanitize_comment(text, "Tester"), text)

    def test_truncates_but_keeps_mention(self):
        long_text = "很" * 400
        result = report.sanitize_comment(long_text, "Tester")
        self.assertIsNotNone(result)
        self.assertLessEqual(len(result), report._MAX_COMMENT)
        self.assertLess(len(result), 500)
        self.assertIn("@Tester", result)

    def test_truncates_at_sentence_end(self):
        long_text = "@Tester " + "讲" * 400 + "。" + "很" * 200 + "。"
        result = report.sanitize_comment(long_text, "Tester")
        self.assertIsNotNone(result)
        self.assertLess(len(result), 500)
        self.assertTrue(result.endswith("。"), result[-20:])
        self.assertIn("@Tester", result)

    def test_comment_allows_near_500(self):
        body = "@Tester " + "讲" * 480
        result = report.sanitize_comment(body, "Tester")
        self.assertIsNotNone(result)
        self.assertLess(len(result), 500)
        self.assertGreaterEqual(len(result), 480)

    def test_empty_rejected(self):
        self.assertIsNone(report.sanitize_comment("   ", "Tester"))
        self.assertIsNone(report.sanitize_comment("好", "Tester"))


class DmTextTest(unittest.TestCase):
    def picks(self, count: int) -> list[dict]:
        return [
            {
                "bvid": f"BV1aaaaaaaa{i}",
                "title": "很长的标题" * 6,
                "one_line": "一句话理由" * 5,
            }
            for i in range(count)
        ]

    def test_under_limit(self):
        text = report.dm_text(self.picks(3))
        self.assertLessEqual(len(text), 500)
        self.assertIn("今天挑到", text)

    def test_drops_entries_instead_of_overflowing(self):
        text = report.dm_text(self.picks(12))
        self.assertLessEqual(len(text), 500)
        self.assertGreaterEqual(text.count("https://"), 1)


class SummaryTest(unittest.TestCase):
    def test_one_line(self):
        self.assertEqual(
            report.one_line_summary(2, 9, 3, 1),
            "看了 9 条，挑中 2 条，评论 1 条，无字幕跳过 3 条",
        )


if __name__ == "__main__":
    unittest.main()
