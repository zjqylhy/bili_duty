"""流水线的离线测试：用假的 B站 和假的模型，验证整条链的账与闸门。

一条都不联网 —— 真实接口的冒烟另外跑（见 README 里的 ``biliduty doctor``）。
"""

from __future__ import annotations

import json
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from biliduty import state as st
from biliduty.config import DEFAULTS, Config, deep_merge
from biliduty.llm import Usage
from biliduty.pipeline import run_round

from ._tmp import temp_dir

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 20, 12, 30, tzinfo=TZ)


def make_config(folder: str, **overrides) -> Config:
    data = deep_merge(DEFAULTS, {
        "perDay": 5,
        "perRound": 3,
        "candidatesPerRound": 10,
        "minScore": 60,
        "reportLevel": "full",
        **overrides,
    })
    cfg = Config(data, Path(folder) / "config.json", Path(folder))
    cfg._validate()
    return cfg


def video(bvid: str, title: str, *, play: int = 10000, age_days: int = 1, duration: str = "10:00") -> dict:
    return {
        "bvid": bvid,
        "title": title,
        "author": "某UP",
        "play": play,
        "review": 3,
        "duration": duration,
        "pubdate": int((NOW - timedelta(days=age_days)).timestamp()),
        "description": "",
    }


class FakeBili:
    def __init__(self, videos: list[dict], subtitles: dict[str, dict]):
        self.videos = videos
        self.subtitles = subtitles
        self.replies: list[tuple[str, str]] = []
        self.dms: list[tuple[int, str]] = []
        self.searches: list[str] = []

    async def search(self, keyword: str, num: int = 12, order: str = "pubdate"):
        self.searches.append(keyword)
        return self.videos[:num]

    async def subtitle(self, bvid: str):
        return self.subtitles.get(bvid, {"empty": "no_subtitle"})

    async def reply(self, bvid: str, text: str, rpid: int = 0, root: int = 0):
        self.replies.append((bvid, text))
        return {"success": True, "rpid": 1234567890}

    async def send_dm(self, uid: int, text: str):
        self.dms.append((uid, text))
        return {"success": True}


class FakeLLM:
    def __init__(self, table: dict[str, dict]):
        self.table = table
        self.usage = Usage()
        self.calls = 0

    async def chat_json(self, messages: list[dict], **kwargs) -> dict:
        self.calls += 1
        self.usage.add(Usage(prompt_tokens=800, completion_tokens=200, calls=1))
        text = messages[-1]["content"]
        bvids = re.findall(r"^\[\d+\] (BV[0-9A-Za-z]{10})$", text, re.MULTILINE)
        items = []
        for bvid in bvids:
            base = self.table.get(bvid, {"worth": False, "score": 10, "why": "无关", "one_line": "跳过"})
            items.append({"bvid": bvid, **base})
        return {"items": items}


def subtitle(text: str = "这是字幕原文，讲了具体的东西。" * 20) -> dict:
    return {"text": text, "language": "中文（自动生成）", "segments": 20}


BV1, BV2, BV3 = "BV1aaaaaaaa1", "BV1bbbbbbbb2", "BV1cccccccc3"


class RoundTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._ctx = temp_dir()
        self.data = self._ctx.__enter__()
        self.cfg = make_config(str(self.data))
        (self.data / "out").mkdir()
        (self.data / "digest").mkdir()

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    async def test_basic_round_writes_all_three_ledgers(self):
        bili = FakeBili(
            [video(BV1, "值得看的"), video(BV2, "水视频"), video(BV3, "没字幕的")],
            {BV1: subtitle(), BV2: subtitle()},
        )
        llm = FakeLLM({
            BV1: {"worth": True, "score": 90, "why": "讲清楚了新版本的改动。", "one_line": "新版本改动速览"},
            BV2: {"worth": False, "score": 20, "why": "没有信息量。", "one_line": "水"},
        })
        result = await run_round(
            self.cfg, why="test", bili=bili, llm=llm, now=NOW,
            keywords=["重返未来"],
        )

        self.assertEqual(result.picked[0]["bvid"], BV1)
        self.assertEqual(len(result.picked), 1)
        self.assertEqual(result.no_subtitle, 1)
        self.assertEqual(result.searched, 3)

        report_text = (self.data / "out" / "2026-10-20.md").read_text(encoding="utf-8")
        self.assertIn("### BV1aaaaaaaa1", report_text)
        self.assertIn("因无字幕跳过 1 条", report_text)
        self.assertNotIn("### BV1bbbbbbbb2", report_text)

        seen = st.SeenTable.load(self.data / "seen.md")
        self.assertEqual(len(seen.videos), 2)
        self.assertTrue(seen.seen(BV1))
        # 没字幕的不进判重表：等 B站 生成字幕后下一轮还能重判
        self.assertFalse(seen.seen(BV3))

        digest = (self.data / "digest" / "2026-10.md").read_text(encoding="utf-8")
        self.assertEqual(digest.count("https://www.bilibili.com/video/"), 2)
        self.assertNotIn("跳过（无字幕）", digest)

        state = json.loads((self.data / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["picked"], 1)
        self.assertEqual(bili.replies, [])

    async def test_no_subtitle_is_retried_later(self):
        """没字幕的不记判重表：B站 生成字幕之后，下一轮还能判到它。"""
        only = video(BV3, "当时还没字幕")
        first = await run_round(
            self.cfg, why="test", bili=FakeBili([only], {}),
            llm=FakeLLM({BV3: {"worth": True, "score": 90, "why": "好", "one_line": "好"}}), now=NOW,
        )
        self.assertEqual(first.picked, [])
        self.assertEqual(first.no_subtitle, 1)
        self.assertFalse(st.SeenTable.load(self.data / "seen.md").seen(BV3))

        second = await run_round(
            self.cfg, why="test", bili=FakeBili([only], {BV3: subtitle()}),
            llm=FakeLLM({BV3: {"worth": True, "score": 90, "why": "好", "one_line": "好"}}), now=NOW,
        )
        self.assertEqual([p["bvid"] for p in second.picked], [BV3])

    async def test_too_fresh_videos_are_skipped(self):
        """发布不满 minAgeHours 的先不看：给 B站 生成 AI 字幕留时间。"""
        fresh = video(BV1, "刚上传的")
        fresh["pubdate"] = int((NOW - timedelta(hours=1)).timestamp())
        result = await run_round(
            self.cfg, why="test", bili=FakeBili([fresh], {}), llm=FakeLLM({}), now=NOW,
        )
        self.assertEqual(result.candidates, 0)
        self.assertEqual(result.reviewed, 0)

    async def test_candidate_pool_is_capped(self):
        """候选池按 candidatePool 封顶：不要一轮读几百条字幕。"""
        self.cfg = make_config(str(self.data), candidatePool=2, candidatesPerRound=10)
        vids = [video(f"BV1aaaaaaaa{i}", f"视频{i}") for i in range(6)]
        subs = {v["bvid"]: subtitle() for v in vids}
        result = await run_round(
            self.cfg, why="test", bili=FakeBili(vids, subs), llm=FakeLLM({}), now=NOW,
        )
        self.assertEqual(result.candidates, 2)
        self.assertEqual(result.reviewed, 2)

    async def test_quota_is_per_day_not_per_round(self):
        self.cfg = make_config(str(self.data), perDay=1, perRound=3)
        bili = FakeBili([video(BV1, "A"), video(BV2, "B")], {BV1: subtitle(), BV2: subtitle()})
        llm = FakeLLM({
            BV1: {"worth": True, "score": 90, "why": "a", "one_line": "a"},
            BV2: {"worth": True, "score": 80, "why": "b", "one_line": "b"},
        })
        first = await run_round(self.cfg, why="test", bili=bili, llm=llm, now=NOW)
        self.assertEqual(len(first.picked), 1)
        self.assertEqual(first.quota_left, 0)

        # 第二次跑：额度已用完，不该再搜索
        second = await run_round(self.cfg, why="test", bili=bili, llm=llm, now=NOW)
        self.assertEqual(second.picked, [])
        self.assertIn("额度已经用完", second.summary)

    async def test_comment_gate_and_record(self):
        self.cfg = make_config(str(self.data), autoComment=True, mentionName="Tester")
        bili = FakeBili([video(BV1, "值得看的")], {BV1: subtitle()})
        llm = FakeLLM({BV1: {
            "worth": True, "score": 95, "why": "有细节。", "one_line": "值得看",
            "comment": "这条讲了具体的改动 @Tester",
        }})
        await run_round(self.cfg, why="test", bili=bili, llm=llm, now=NOW)

        self.assertEqual(len(bili.replies), 1)
        bvid, text = bili.replies[0]
        self.assertEqual(bvid, BV1)
        self.assertIn("@Tester", text)
        seen = st.SeenTable.load(self.data / "seen.md")
        self.assertTrue(seen.commented(BV1))
        self.assertEqual(seen.comments[0].rpid, "1234567890")

    async def test_second_round_does_not_comment_twice(self):
        self.cfg = make_config(str(self.data), autoComment=True, mentionName="Tester", perDay=5)
        bili = FakeBili([video(BV1, "值得看的")], {BV1: subtitle()})
        llm = FakeLLM({BV1: {
            "worth": True, "score": 95, "why": "有细节。", "one_line": "值得看",
            "comment": "这条值得 @Tester",
        }})
        await run_round(self.cfg, why="test", bili=bili, llm=llm, now=NOW)
        await run_round(self.cfg, why="test", bili=bili, llm=llm, now=NOW)
        self.assertEqual(len(bili.replies), 1)  # 第二轮判重表里已经有了

    async def test_dm_only_mode_sends_no_public_comment(self):
        self.cfg = make_config(
            str(self.data), autoComment=True, mentionName="Tester",
            dmMode="only", dmUid=123456789,
        )
        bili = FakeBili([video(BV1, "值得看的")], {BV1: subtitle()})
        llm = FakeLLM({BV1: {
            "worth": True, "score": 95, "why": "有细节。", "one_line": "值得看",
            "comment": "这条值得 @Tester",
        }})
        result = await run_round(self.cfg, why="test", bili=bili, llm=llm, now=NOW)

        self.assertEqual(bili.replies, [])
        self.assertEqual(len(bili.dms), 1)
        uid, text = bili.dms[0]
        self.assertEqual(uid, 123456789)
        self.assertIn("https://www.bilibili.com/video/BV1aaaaaaaa1", text)
        self.assertLessEqual(len(text), 500)
        self.assertTrue(result.dm["ok"])

    async def test_dry_run_touches_nothing_and_writes_preview(self):
        self.cfg = make_config(str(self.data), autoComment=True, mentionName="Tester")
        bili = FakeBili([video(BV1, "值得看的")], {BV1: subtitle()})
        llm = FakeLLM({BV1: {
            "worth": True, "score": 95, "why": "有细节。", "one_line": "值得看",
            "comment": "这条值得 @Tester",
        }})
        result = await run_round(self.cfg, why="test", dry_run=True, bili=bili, llm=llm, now=NOW)

        self.assertFalse((self.data / "seen.md").exists())
        self.assertFalse((self.data / "state.json").exists())
        self.assertFalse((self.data / "digest").exists() and any((self.data / "digest").iterdir()))
        self.assertEqual(bili.replies, [])
        preview = Path(result.report_file)
        self.assertTrue(preview.is_file())
        self.assertTrue(preview.name.startswith("preview-"))
        self.assertIn("### BV1aaaaaaaa1", preview.read_text(encoding="utf-8"))

    async def test_unreadable_seen_table_blocks_comments(self):
        self.cfg = make_config(str(self.data), autoComment=True, mentionName="Tester")
        (self.data / "seen.md").write_bytes(b"\xff\xfe broken")
        bili = FakeBili([video(BV1, "值得看的")], {BV1: subtitle()})
        llm = FakeLLM({BV1: {
            "worth": True, "score": 95, "why": "有细节。", "one_line": "值得看",
            "comment": "这条值得 @Tester",
        }})
        result = await run_round(self.cfg, why="test", bili=bili, llm=llm, now=NOW)
        self.assertEqual(bili.replies, [])
        self.assertTrue(any("判重表" in error for error in result.errors))

    async def test_old_videos_are_filtered_out(self):
        self.cfg = make_config(str(self.data), searchDays=7)
        bili = FakeBili([
            video(BV1, "新的", age_days=1),
            video(BV2, "旧的", age_days=30),
        ], {BV1: subtitle(), BV2: subtitle()})
        llm = FakeLLM({BV1: {"worth": True, "score": 90, "why": "a", "one_line": "a"}})
        result = await run_round(self.cfg, why="test", bili=bili, llm=llm, now=NOW)
        self.assertEqual(result.candidates, 1)
        self.assertEqual(result.picked[0]["bvid"], BV1)

    async def test_seen_videos_are_skipped(self):
        seen = st.SeenTable(self.data / "seen.md")
        seen.add_video(NOW, BV1, "看过了", "UP", "跳过")
        seen.save()
        bili = FakeBili([video(BV1, "看过了")], {BV1: subtitle()})
        llm = FakeLLM({})
        result = await run_round(self.cfg, why="test", bili=bili, llm=llm, now=NOW)
        self.assertEqual(result.candidates, 0)
        self.assertEqual(result.picked, [])

    async def test_report_appends_within_one_day(self):
        bili = FakeBili([video(BV1, "A")], {BV1: subtitle()})
        llm = FakeLLM({BV1: {"worth": True, "score": 90, "why": "a", "one_line": "a"}})
        await run_round(self.cfg, why="test", bili=bili, llm=llm, now=NOW)

        bili2 = FakeBili([video(BV2, "B")], {BV2: subtitle()})
        llm2 = FakeLLM({BV2: {"worth": True, "score": 90, "why": "b", "one_line": "b"}})
        await run_round(self.cfg, why="test", bili=bili2, llm=llm2, now=NOW + timedelta(hours=6))

        text = (self.data / "out" / "2026-10-20.md").read_text(encoding="utf-8")
        self.assertIn("### BV1aaaaaaaa1", text)
        self.assertIn("### BV1bbbbbbbb2", text)
        state = json.loads((self.data / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["picked"], 2)

    async def test_report_off_skips_file_but_keeps_ledgers(self):
        self.cfg = make_config(str(self.data), reportLevel="off")
        bili = FakeBili([video(BV1, "A")], {BV1: subtitle()})
        llm = FakeLLM({BV1: {"worth": True, "score": 90, "why": "a", "one_line": "a"}})
        result = await run_round(self.cfg, why="test", bili=bili, llm=llm, now=NOW)
        self.assertEqual(result.report_file, "")
        self.assertTrue((self.data / "seen.md").exists())
        self.assertTrue((self.data / "digest" / "2026-10.md").exists())

    async def test_llm_failure_still_writes_ledgers(self):
        class BrokenLLM:
            usage = Usage()

            async def chat_json(self, messages, **kwargs):
                from biliduty.llm import LLMError

                raise LLMError("模型挂了")

        bili = FakeBili([video(BV1, "A")], {BV1: subtitle()})
        result = await run_round(self.cfg, why="test", bili=bili, llm=BrokenLLM(), now=NOW)
        self.assertEqual(result.picked, [])
        report_text = (self.data / "out" / "2026-10-20.md").read_text(encoding="utf-8")
        # 模型挂了 ≠ 没有值得的：报告里必须说清楚是哪一种
        self.assertIn("模型这一轮没给出判断", report_text)
        self.assertTrue(any("判断失败" in error for error in result.errors))
        self.assertTrue((self.data / "seen.md").exists())


if __name__ == "__main__":
    unittest.main()
