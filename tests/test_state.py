"""台账层的测试：窗口、配额计数、判重表回写、跨天清零。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from biliduty import state as st

from ._tmp import temp_dir

TZ = ZoneInfo("Asia/Shanghai")


def at(hour: int, minute: int = 0, day: int = 20) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


class WindowTest(unittest.TestCase):
    def test_cross_midnight(self):
        window = ("18:00", "09:00")
        self.assertTrue(st.in_window(at(20), *window))
        self.assertTrue(st.in_window(at(3), *window))
        self.assertTrue(st.in_window(at(8, 59), *window))
        self.assertFalse(st.in_window(at(9), *window))
        self.assertFalse(st.in_window(at(12), *window))

    def test_normal_window(self):
        window = ("12:00", "14:00")
        self.assertFalse(st.in_window(at(11, 59), *window))
        self.assertTrue(st.in_window(at(12), *window))
        self.assertTrue(st.in_window(at(13, 59), *window))
        self.assertFalse(st.in_window(at(14), *window))

    def test_bad_time_falls_back_to_false(self):
        self.assertFalse(st.in_window(at(12), "25:00", "14:00"))
        self.assertFalse(st.in_window(at(12), "", "14:00"))

    def test_minutes_of(self):
        self.assertEqual(st.minutes_of("18:30"), 18 * 60 + 30)
        self.assertIsNone(st.minutes_of("1:2:3"))


class ReportCountTest(unittest.TestCase):
    def test_counts_pick_headings_only(self):
        with temp_dir() as folder:
            path = Path(folder) / "2026-10-20.md"
            path.write_text(
                "# 刷B站 · 2026-10-20\n\n"
                "### BV1aaaaaaaa1\n标题 A\n\n"
                "## BV1bbbbbbbb2\n标题 B\n\n"
                "提过一嘴 BV1cccccccc3 但没有标题行\n\n"
                "```\n### BV1dddddddd4 （围栏里不算）\n```\n",
                encoding="utf-8",
            )
            self.assertEqual(st.count_picks(path), 2)

    def test_missing_file_is_zero(self):
        self.assertEqual(st.count_picks(Path("does-not-exist")), 0)


class SeenTableTest(unittest.TestCase):
    def test_parse_existing_file_and_append(self):
        with temp_dir() as folder:
            path = Path(folder) / "seen.md"
            path.write_text(
                "# 刷B站 · 判重表（宿主预建，那一轮只往后面追加）\n\n"
                "## 评论记录（BV | rpid | root（楼中楼才填） | 时间 | 正文前 20 字）\n\n"
                "| BV | rpid | root（楼中楼才填） | 时间 | 正文前 20 字 |\n"
                "| --- | --- | --- | --- | --- |\n"
                "| BV1aaaaaaaa1 | 1234567890 |  | 2026-10-20 06:47 | @Tester 10月22日… |\n\n"
                "## 看过的视频\n\n"
                "- 2026-10-20 06:47 · BV1aaaaaaaa1 · 旧标题 · 旧UP · 挑中（已评论）\n",
                encoding="utf-8",
            )
            table = st.SeenTable.load(path)
            self.assertEqual(table.error, "")
            self.assertTrue(table.seen("BV1aaaaaaaa1"))
            self.assertTrue(table.commented("BV1aaaaaaaa1"))
            self.assertEqual(table.comments[0].rpid, "1234567890")

            table.add_video(at(12), "BV1bbbbbbbb2", "新标题", "新UP", "跳过（无字幕）")
            table.add_comment(at(12), "BV1bbbbbbbb2", "999", "", "@Tester 看看这个")
            table.save()

            reread = st.SeenTable.load(path)
            self.assertEqual(len(reread.videos), 2)
            self.assertEqual(len(reread.comments), 2)
            self.assertEqual(reread.comments[1].rpid, "999")
            self.assertIn("| BV1bbbbbbbb2 | 999 |", path.read_text(encoding="utf-8"))

    def test_unreadable_table_is_never_overwritten(self):
        with temp_dir() as folder:
            path = Path(folder) / "seen.md"
            path.write_bytes(b"\xff\xfe not utf8")
            table = st.SeenTable.load(path)
            self.assertEqual(table.error, "unreadable")
            table.add_video(at(12), "BV1bbbbbbbb2", "t", "u", "挑中")
            table.save()
            self.assertEqual(path.read_bytes(), b"\xff\xfe not utf8")

    def test_render_keeps_section_order(self):
        with temp_dir() as folder:
            table = st.SeenTable(Path(folder) / "seen.md")
            table.add_video(at(12), "BV1bbbbbbbb2", "标题", "UP", "挑中")
            table.save()
            text = (Path(folder) / "seen.md").read_text(encoding="utf-8")
            self.assertLess(text.index("## 评论记录"), text.index("## 看过的视频"))
            self.assertIn("BV1bbbbbbbb2", text)


class StateTest(unittest.TestCase):
    def test_rollover_to_new_day(self):
        with temp_dir() as folder:
            path = Path(folder) / "state.json"
            path.write_text(json.dumps({
                "day": "2026-10-19", "count": 2, "picked": 4, "windows": {"noon": True},
            }), encoding="utf-8")
            state = st.State.load(path, "2026-10-20")
            self.assertEqual(state.day, "2026-10-20")
            self.assertEqual(state.count, 0)
            self.assertEqual(state.picked, 0)
            self.assertEqual(state.windows, {})

    def test_same_day_kept(self):
        with temp_dir() as folder:
            path = Path(folder) / "state.json"
            path.write_text(json.dumps({
                "day": "2026-10-20", "count": 1, "picked": 2, "windows": {"night": True},
            }), encoding="utf-8")
            state = st.State.load(path, "2026-10-20")
            self.assertEqual(state.count, 1)
            self.assertEqual(st.picked_today(Path(folder), state, at(20)), 2)

    def test_picked_today_takes_max_of_report_and_state(self):
        with temp_dir() as folder:
            data = Path(folder)
            (data / "out").mkdir()
            (data / "out" / "2026-10-20.md").write_text(
                "### BV1aaaaaaaa1\na\n### BV1bbbbbbbb2\nb\n", encoding="utf-8",
            )
            state = st.State(path=data / "state.json", day="2026-10-20", picked=1)
            self.assertEqual(st.picked_today(data, state, at(20)), 2)


if __name__ == "__main__":
    unittest.main()
