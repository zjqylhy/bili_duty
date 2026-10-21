"""一轮流水线。

跟原来「起一个 DSH agent，把 duty.md 交给它自己看着办」的区别，全在这张图上：

    搜索（关键词 × 近 N 天）
      → 判重（seen.md 里看过的一律跳过）
      → 逐条读字幕原文（没字幕才考虑本地转写）
      → 模型一次性打分 + 写依据（结构化 JSON，不做自由发挥）
      → 按当天额度挑前 N 条
      → 写日报 / 归档 / 判重表
      → （可选）发一条评论 @ 你、（可选）私信、Webhook

好处是每一步都可解释、可重跑、可测试；代价是它不会自己"临场发挥"去找别的视频 ——
需要那种能力时用 ``biliduty run --keyword ...`` 临时指定主题。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import prompts, report
from . import state as st
from .bilibili import Bili, BiliError, load_credential
from .config import Config
from .llm import LLMClient, LLMError, Usage

log = logging.getLogger("biliduty.pipeline")

#: 一次送给模型几条候选。太多会稀释注意力，太少则调用次数变多。
JUDGE_CHUNK = 5


@dataclass
class Candidate:
    bvid: str
    title: str
    author: str
    play: int = 0
    duration: Any = ""
    pubdate: int = 0
    description: str = ""
    subtitle: str = ""
    empty: str = ""
    transcribe_failed: bool = False

    def material(self, now: datetime, tz) -> dict:
        published = ""
        if self.pubdate:
            published = datetime.fromtimestamp(self.pubdate, tz).strftime("%m-%d %H:%M")
        return {
            "bvid": self.bvid,
            "title": self.title,
            "author": self.author,
            "play": self.play,
            "duration": self.duration,
            "pubdate_str": published,
            "subtitle": self.subtitle,
            "empty": self.empty,
        }


@dataclass
class Verdict:
    bvid: str
    worth: bool = False
    score: int = 0
    why: str = ""
    one_line: str = ""
    comment: str = ""
    skip_reason: str = ""


@dataclass
class RoundResult:
    why: str
    day: str
    started_at: str
    searched: int = 0
    candidates: int = 0
    reviewed: int = 0
    no_subtitle: int = 0
    picked: list[dict] = field(default_factory=list)
    comments: list[dict] = field(default_factory=list)
    dm: dict = field(default_factory=dict)
    report_file: str = ""
    digest_file: str = ""
    quota_left: int = 0
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    summary: str = ""
    llm: Usage = field(default_factory=Usage)

    def as_dict(self) -> dict:
        data = {
            "why": self.why,
            "day": self.day,
            "startedAt": self.started_at,
            "searched": self.searched,
            "candidates": self.candidates,
            "reviewed": self.reviewed,
            "noSubtitle": self.no_subtitle,
            "picked": self.picked,
            "comments": self.comments,
            "dm": self.dm,
            "reportFile": self.report_file,
            "digestFile": self.digest_file,
            "quotaLeft": self.quota_left,
            "dryRun": self.dry_run,
            "errors": self.errors,
            "durationSeconds": round(self.duration_s, 1),
            "summary": self.summary,
            "llm": self.llm.as_dict(),
        }
        return data


# ══════════════════════════════════════════════════════════════════════════
# 找视频
# ══════════════════════════════════════════════════════════════════════════

def _keywords(cfg: Config) -> list[str]:
    items = [str(k).strip() for k in (cfg.get("keywords") or []) if str(k).strip()]
    return items or ["DeepSeek Harness", "DSH 插件", "dsh 教程"]


async def collect_candidates(
    cfg: Config,
    bili: Bili,
    seen: st.SeenTable,
    now: datetime,
    keywords: list[str] | None = None,
    limit: int | None = None,
) -> tuple[list[Candidate], int]:
    """搜索 + 判重 + 按发布时间/播放量排序。返回 (候选, 搜索命中总数)。"""
    tz = cfg.tz()
    days = int(cfg.get("searchDays", default=7))
    per_keyword = int(cfg.get("searchPerKeyword", default=12))
    min_age_hours = float(cfg.get("minAgeHours", default=12) or 0)
    order = str(cfg.get("searchOrder", default="pubdate"))
    cutoff = (now.timestamp() - days * 86400) if days > 0 else 0
    # 太新的先不看：B站 的 AI 字幕常常要几个小时才生成，刚上传的放进池子只会白读
    freshest = (now.timestamp() - min_age_hours * 3600) if min_age_hours > 0 else 0

    found: dict[str, Candidate] = {}
    hits = 0
    for keyword in (keywords or _keywords(cfg)):
        try:
            videos = await bili.search(keyword, num=per_keyword, order=order)
        except BiliError as exc:
            log.warning("搜索失败（%s）：%s", keyword, exc)
            continue
        for item in videos:
            hits += 1
            bvid = item["bvid"]
            if bvid in found or seen.seen(bvid):
                continue
            pubdate = int(item.get("pubdate") or 0)
            if cutoff and pubdate and pubdate < cutoff:
                continue
            if freshest and pubdate and pubdate > freshest:
                continue
            found[bvid] = Candidate(
                bvid=bvid,
                title=item.get("title") or "",
                author=item.get("author") or "",
                play=int(item.get("play") or 0),
                duration=item.get("duration") or "",
                pubdate=pubdate,
                description=item.get("description") or "",
            )

    ordered = sorted(found.values(), key=lambda c: (c.pubdate, c.play), reverse=True)
    # ⭐ 默认按 candidatePool 封顶：每条候选都要读一次字幕，池子太大这一轮会跑很久
    if limit is None:
        return ordered[: max(0, int(cfg.get("candidatePool", default=60)))], hits
    return ordered[: max(0, int(limit))], hits


async def read_subtitles(cfg: Config, bili: Bili, data_dir: Path, candidates: list[Candidate]) -> None:
    """逐条读字幕原文；没字幕时按设置决定要不要本地转写。"""
    transcribe_on = bool(cfg.get("transcribe"))
    for candidate in candidates:
        try:
            result = await bili.subtitle(candidate.bvid)
        except BiliError as exc:
            candidate.empty = f"error: {exc}"
            log.warning("读字幕失败 %s：%s", candidate.bvid, exc)
            continue

        if result.get("text"):
            candidate.subtitle = str(result["text"])
            continue

        candidate.empty = str(result.get("empty") or "no_subtitle")
        if transcribe_on and candidate.empty == "no_subtitle":
            text = await transcribe(cfg, data_dir, candidate.bvid)
            if text:
                candidate.subtitle = text
                candidate.empty = ""
            else:
                candidate.transcribe_failed = True
                candidate.empty = "no_subtitle_and_transcribe_failed"
        log.info("无字幕：%s（%s）", candidate.bvid, candidate.empty)


async def transcribe(cfg: Config, data_dir: Path, bvid: str) -> str:
    """调外部 ASR 环境转写。没装好就返回空串，不抛错（少一条比整轮崩强）。"""
    import sys

    python = str(cfg.get("asrPython") or "").strip() or sys.executable
    script = str(cfg.get("asrScript") or "").strip() or str(data_dir / "asr.py")
    if not Path(python).exists() or not Path(script).is_file():
        log.info("转写不可用（python=%s，script=%s）", python, script)
        return ""
    model = str(cfg.get("asrModel") or "small")
    try:
        proc = await asyncio.create_subprocess_exec(
            python, script, bvid, "--model", model,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=900)
    except (OSError, asyncio.TimeoutError) as exc:
        log.warning("转写失败 %s：%s", bvid, exc)
        return ""
    text = out.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        log.warning("转写退出码 %s：%s", proc.returncode, err.decode("utf-8", errors="replace")[-300:])
        return ""
    return text


# ══════════════════════════════════════════════════════════════════════════
# 判断
# ══════════════════════════════════════════════════════════════════════════

def _verdict_from_item(item: dict, min_score: int) -> Verdict:
    def as_int(value: Any) -> int:
        try:
            return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return 0

    score = as_int(item.get("score"))
    worth = bool(item.get("worth")) and score >= min_score
    return Verdict(
        bvid=str(item.get("bvid") or "").strip(),
        worth=worth,
        score=score,
        why=str(item.get("why") or "").strip(),
        one_line=str(item.get("one_line") or "").strip(),
        comment=str(item.get("comment") or "").strip(),
        skip_reason=str(item.get("skip_reason") or "").strip(),
    )


async def judge(
    cfg: Config,
    llm: LLMClient,
    candidates: list[Candidate],
    now: datetime,
    remaining_today: int,
    comment_on: bool,
) -> tuple[dict[str, Verdict], int]:
    """分批打分。任何一批失败都只影响那一批 —— 剩下的照常出报告。

    返回 (判词, 失败的批次数)：失败了要说出来，别把"模型挂了"写成"没有值得的"。
    """
    tz = cfg.tz()
    min_score = int(cfg.get("minScore", default=60))
    verdicts: dict[str, Verdict] = {}
    failed = 0

    for start in range(0, len(candidates), JUDGE_CHUNK):
        chunk = candidates[start:start + JUDGE_CHUNK]
        messages = prompts.build_judge_messages(
            cfg,
            [c.material(now, tz) for c in chunk],
            comment_on=comment_on,
            remaining_today=max(0, remaining_today),
        )
        try:
            payload = await llm.chat_json(messages)
        except LLMError as exc:
            failed += 1
            log.error("判断第 %d 批失败：%s", start // JUDGE_CHUNK + 1, exc)
            continue

        items = payload.get("items")
        if not isinstance(items, list):
            failed += 1
            log.error("模型返回里没有 items 数组，跳过这一批")
            continue
        allowed = {c.bvid for c in chunk}
        for item in items:
            if not isinstance(item, dict):
                continue
            verdict = _verdict_from_item(item, min_score)
            if verdict.bvid in allowed:
                verdicts[verdict.bvid] = verdict
    return verdicts, failed


# ══════════════════════════════════════════════════════════════════════════
# 一轮
# ══════════════════════════════════════════════════════════════════════════

def _comment_enabled(cfg: Config, seen: st.SeenTable) -> bool:
    if not cfg.get("autoComment"):
        return False
    if not str(cfg.get("mentionName") or "").strip():
        return False
    if str(cfg.get("dmMode") or "off").lower() == "only":
        return False
    if seen.error == "unreadable":
        # 判重表读不出来就绝不能发 —— 否则同一批人会被重复打扰
        return False
    return True


async def run_round(
    cfg: Config,
    *,
    why: str = "manual",
    dry_run: bool = False,
    keywords: list[str] | None = None,
    bili: Bili | None = None,
    llm: LLMClient | None = None,
    now: datetime | None = None,
) -> RoundResult:
    started = time.monotonic()
    tz = cfg.tz()
    now = now or datetime.now(tz)
    today = st.day_key(now)
    data_dir = cfg.data_dir

    result = RoundResult(why=why, day=today, started_at=now.isoformat(), dry_run=dry_run)

    state = st.State.load(data_dir / "state.json", today)
    seen = st.SeenTable.load(data_dir / "seen.md")
    if seen.error == "unreadable":
        result.errors.append("判重表读不出来 —— 本轮不发评论")

    quota = int(cfg.get("perDay", default=0))
    already = st.picked_today(data_dir, state, now)
    remaining = max(0, quota - already)
    per_round = int(cfg.get("perRound", default=3))
    # 预览（--dry-run）不占额度，也不该被"今天已经挑满"挡住；它只看这一轮
    round_cap = per_round if dry_run else min(per_round, remaining) if quota > 0 else per_round

    if quota > 0 and remaining <= 0 and not dry_run:
        result.summary = f"今天的额度已经用完了（{already}/{quota}），这一轮不跑"
        result.quota_left = 0
        result.duration_s = time.monotonic() - started
        log.info(result.summary)
        return result

    bili = bili or Bili(load_credential(data_dir), cfg.get("delays") or {})
    llm = llm or LLMClient(cfg)

    # ① 搜索 + 判重 ─────────────────────────────────────────────────────
    candidates, hits = await collect_candidates(cfg, bili, seen, now, keywords=keywords)
    result.searched = hits
    result.candidates = len(candidates)
    log.info("搜索命中 %d 条，判重后剩 %d 条候选（本轮额度 %d）", hits, len(candidates), round_cap)

    if not candidates:
        result.summary = "这一轮没有新视频（都看过了）"
        result.quota_left = remaining
        result.duration_s = time.monotonic() - started
        if not dry_run:
            state.lastResult = result.summary
            state.save()
        return result

    # ② 读字幕原文 ─────────────────────────────────────────────────────
    await read_subtitles(cfg, bili, data_dir, candidates)
    result.no_subtitle = sum(1 for c in candidates if not c.subtitle)
    # ⭐ 只把「读到了字幕」的送进判断：没字幕的这轮等于没看到内容，
    #    既不该占判断额度，更不能记进判重表 —— B站 的 AI 字幕常常晚几个小时才生成，
    #    记进去就等于把这条永久划掉（2026-10-21 的教训）。
    readable = [c for c in candidates if c.subtitle]
    judge_cap = int(cfg.get("candidatesPerRound", default=10))
    to_judge = readable[: max(0, judge_cap)]
    result.reviewed = len(to_judge)

    # ③ 模型判断 ───────────────────────────────────────────────────────
    comment_on = _comment_enabled(cfg, seen)
    verdicts, failed_batches = await judge(cfg, llm, to_judge, now, round_cap, comment_on)
    result.llm = llm.usage
    if failed_batches:
        result.errors.append(f"{failed_batches} 批判断失败（模型/网络），这些条目没有判词")

    ranked = sorted(verdicts.values(), key=lambda v: v.score, reverse=True)
    chosen = [v for v in ranked if v.worth][: max(0, round_cap)]

    by_bvid = {c.bvid: c for c in candidates}
    picks: list[dict] = []
    for verdict in chosen:
        candidate = by_bvid[verdict.bvid]
        picks.append({
            "bvid": verdict.bvid,
            "title": candidate.title,
            "author": candidate.author,
            "play": candidate.play,
            "duration": candidate.duration,
            "url": report.VIDEO_URL.format(bvid=verdict.bvid),
            "score": verdict.score,
            "why": verdict.why,
            "one_line": verdict.one_line,
            "comment": verdict.comment,
            "commented": False,
        })
    result.picked = picks

    # ④ 判重表先落一步：哪怕后面崩了，"这些看过了"也得留下 ───────────────
    #    ⚠️ 只记「真正判过」的；没字幕的与超额的都不记 —— 下一轮还能重判
    if not dry_run:
        for candidate in to_judge:
            verdict = verdicts.get(candidate.bvid)
            if verdict and verdict.worth and candidate.bvid in {p["bvid"] for p in picks}:
                mark = "挑中"
            else:
                mark = f"跳过（{verdict.skip_reason or verdict.one_line or '不值得'}）" if verdict else "跳过（未判断）"
            seen.add_video(now, candidate.bvid, candidate.title, candidate.author, mark)
        seen.save()

    # ⑤ 说话：评论（可选）────────────────────────────────────────────────
    if picks and comment_on and not dry_run:
        mention = str(cfg.get("mentionName") or "").strip()
        limit = int(cfg.get("commentPerRound", default=1))
        for pick in picks:
            if len(result.comments) >= limit:
                break
            if seen.commented(pick["bvid"]):
                continue
            text = report.sanitize_comment(pick.get("comment") or pick.get("one_line") or "", mention)
            if not text:
                log.info("评论没过闸门（%s），跳过", pick["bvid"])
                continue
            response = await bili.reply(pick["bvid"], text)
            if response.get("success"):
                pick["commented"] = True
                rpid = str(response.get("rpid") or "未取到 rpid")
                seen.add_comment(now, pick["bvid"], rpid, str(response.get("root") or ""), text)
                seen.save()
                result.comments.append({"bvid": pick["bvid"], "rpid": rpid, "text": text})
                log.info("已评论 %s（rpid=%s）", pick["bvid"], rpid)
            else:
                result.errors.append(f"评论失败 {pick['bvid']}：{response.get('error')}")
                log.warning("评论失败 %s：%s", pick["bvid"], response.get("error"))

    # ⑥ 日报 / 归档 ─────────────────────────────────────────────────────
    level = str(cfg.get("reportLevel") or "full").lower()
    if level != "off":
        target = (
            data_dir / "out" / f"preview-{now.strftime('%Y-%m-%d-%H%M%S')}.md"
            if dry_run else st.report_path(data_dir, now)
        )
        st.ensure_report_header(target, now, _keywords(cfg))
        body = "".join(report.pick_section(pick, level) for pick in picks)
        if not body:
            if failed_batches:
                body = (
                    f"⚠️ 模型这一轮没给出判断（{failed_batches} 批调用失败），所以没有挑中任何条目。\n"
                    "—— 这不是「没有值得的」，是判断没跑完；看 duty.log 里的报错。\n\n"
                )
            else:
                body = "这一轮没有挑中新的（都看过了或不值得）。\n\n"
        st.append_text(target, body + report.footer(len(to_judge), len(picks), result.no_subtitle, now))
        result.report_file = str(target)

    digest_lines = []
    picked_ids = {p["bvid"] for p in picks}
    for candidate in to_judge:
        verdict = verdicts.get(candidate.bvid)
        data = dict(vars(verdict)) if verdict else {}
        digest_lines.append(report.digest_line(now, {
            "bvid": candidate.bvid, "title": candidate.title, "author": candidate.author,
        }, data, candidate.bvid in picked_ids))
    if digest_lines and not dry_run:
        path = st.append_digest(data_dir, now, "\n".join(digest_lines), _keywords(cfg))
        result.digest_file = str(path)

    # ⑦ 私信（可选）─────────────────────────────────────────────────────
    dm_mode = str(cfg.get("dmMode") or "off").lower()
    dm_uid = int(cfg.get("dmUid") or 0)
    if picks and dm_mode in {"only", "both"} and dm_uid > 0 and not dry_run:
        text = report.dm_text(picks)
        if text:
            response = await bili.send_dm(dm_uid, text)
            result.dm = {"ok": bool(response.get("success")), "uid": dm_uid, "error": response.get("error", "")}
            if not response.get("success"):
                result.errors.append(f"私信失败：{response.get('error')}")
                log.warning("私信失败：%s", response.get("error"))

    # ⑧ 记账 ───────────────────────────────────────────────────────────
    result.quota_left = max(0, remaining - len(picks))
    result.summary = report.one_line_summary(len(picks), len(to_judge), result.no_subtitle, len(result.comments))
    result.duration_s = time.monotonic() - started

    if not dry_run:
        # ⚠️ 重新读一遍再写：调度器在开跑前已经把 count / windows 记进去了，
        #    这里如果用开跑时那份内存状态覆盖，轮次记录会丢。
        fresh = st.State.load(data_dir / "state.json", today)
        fresh.picked = already + len(picks)
        fresh.lastResult = result.summary
        fresh.save()
        st.prune_reports(data_dir, int(cfg.get("reportKeepDays", default=90)), now)

    log.info("这一轮结束：%s", result.summary)
    return result
