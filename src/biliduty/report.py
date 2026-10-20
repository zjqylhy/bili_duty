"""排版：日报、归档、私信、评论。

写作这一层**故意不用模型** —— 字段已经有了（标题 / 链接 / 为什么值得 / 一句话），
模板拼出来又快又稳，也不会把"设置名、内部术语"漏进评论和私信里（原插件章程里
反复强调的那条红线，在这里由代码兜住）。
"""

from __future__ import annotations

import re
from datetime import datetime

VIDEO_URL = "https://www.bilibili.com/video/{bvid}"

#: 评论/私信里绝对不许出现的内部词（原章程的红线，这里做成硬闸门）
_FORBIDDEN = (
    "dmMode", "dmUid", "autoComment", "mentionName", "perDay", "prompt", "token",
    "判重表", "白名单", "复核", "一轮", "配额", "config", "setting", "设置页",
)

_MAX_COMMENT = 499   # 需求：含标点与 @昵称，总字数必须 < 500
_MAX_DM = 500


def human_count(value: int | None) -> str:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return "0"
    if number >= 100_000_000:
        return f"{number / 100_000_000:.1f}亿"
    if number >= 10_000:
        return f"{number / 10_000:.1f}万"
    return str(number)


def human_duration(value) -> str:
    """搜索接口给的是 "12:34"，详情接口给的是秒数。"""
    if isinstance(value, str) and re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", value.strip()):
        return value.strip()
    try:
        seconds = int(value or 0)
    except (TypeError, ValueError):
        return "?"
    if seconds <= 0:
        return "?"
    hours, rest = divmod(seconds, 3600)
    minutes, sec = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{sec:02d}" if hours else f"{minutes}:{sec:02d}"


def pick_section(pick: dict, level: str = "full") -> str:
    """一条挑中的视频 —— **第一行必须是 `### BV号`**（宿主靠它数配额，格式不能变）。"""
    bvid = pick["bvid"]
    title = pick.get("title") or ""
    url = VIDEO_URL.format(bvid=bvid)
    one_line = (pick.get("one_line") or "").strip()
    why = (pick.get("why") or "").strip()

    if level == "brief":
        tail = f" —— {one_line}" if one_line else ""
        return f"### {bvid}\n{title} · [看视频]({url}){tail}\n\n"

    meta = " · ".join(filter(None, [
        f"**{title}**" if title else "",
        str(pick.get("author") or ""),
        f"播放 {human_count(pick.get('play'))}",
        f"时长 {human_duration(pick.get('duration'))}",
        f"[看视频]({url})",
    ]))
    lines = [f"### {bvid}", meta]
    if why:
        lines.append(f"为什么值得：{why}")
    if one_line:
        lines.append(f"> {one_line}")
    return "\n".join(lines) + "\n\n"


def footer(reviewed: int, picked: int, no_subtitle: int, now: datetime) -> str:
    return (
        "---\n"
        f"本轮：看了 {reviewed} 条，挑中 {picked} 条；因无字幕跳过 {no_subtitle} 条 · "
        f"{now.strftime('%Y-%m-%d %H:%M')}\n\n"
    )


def digest_line(now: datetime, video: dict, verdict: dict | None, picked: bool) -> str:
    """归档一行：日期 · BV · 标题 · UP · 一句话结论 · 链接（跳过的把理由写进结论里）。"""
    verdict = verdict or {}
    if picked:
        conclusion = (verdict.get("one_line") or verdict.get("why") or "挑中").strip()
    else:
        reason = str(verdict.get("skip_reason") or "").strip()
        if not reason:
            reason = (verdict.get("one_line") or verdict.get("why") or "").strip()
        conclusion = f"跳过（{reason}）" if reason else "跳过"
    return (
        f"{now.strftime('%Y-%m-%d')} · {video['bvid']} · {video.get('title') or ''} · "
        f"{video.get('author') or ''} · {conclusion} · {VIDEO_URL.format(bvid=video['bvid'])}"
    )


def sanitize_comment(text: str, mention: str, limit: int = _MAX_COMMENT) -> str | None:
    """清一遍再发。过不了闸门就返回 None（宁可少发一条，也别拿使用者的号冒险）。"""
    mention = (mention or "").strip()
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    if not body:
        return None
    lowered = body.lower()
    if any(word.lower() in lowered for word in _FORBIDDEN):
        return None
    if len(body) < 4:
        return None
    # 纯文本 @：模型有时会写成 <@名字> 或 @[名字]，统一成 "@名字"（不吞掉后面的空格）
    if mention:
        body = re.sub(
            rf"(?:[<\[（(]\s*)?@{re.escape(mention)}(?:\s*[>\]）)])?",
            f"@{mention}",
            body,
        )
        if f"@{mention}" not in body:
            body = f"{body} @{mention}"
    if len(body) > limit:
        # 截断到 limit 以内：优先在句末标点处断，别把半句话发出去（limit=499 ⇒ 结果必 < 500 字）
        head = body[:limit]
        cut = ""
        for separator in ("。", "！", "？", "；", "…"):
            index = head.rfind(separator)
            if index >= int(limit * 0.6):
                cut = head[: index + 1]
                break
        if not cut:
            cut = head[: limit - 1].rstrip(" ，,、;；：:") + "…"
        body = cut
    if mention and f"@{mention}" not in body:
        # 极少数情况：@ 在结尾、刚被截掉 ⇒ 补回开头
        body = (f"@{mention} " + body)[:limit]
    body = body.strip()
    return body if len(body) >= 4 else None


def dm_text(picks: list[dict], limit: int = _MAX_DM) -> str:
    """一封私信 = 本轮全部值得的；控制在 500 字以内（超了就先砍条目，不砍整封）。"""
    if not picks:
        return ""
    header = f"今天挑到 {len(picks)} 条："
    entries: list[str] = []
    for index, pick in enumerate(picks, start=1):
        title = (pick.get("title") or "").strip()
        reason = (pick.get("one_line") or pick.get("why") or "").strip()
        url = VIDEO_URL.format(bvid=pick["bvid"])
        entry = f"{index}. {title}\n{url}"
        if reason:
            entry += f"\n{reason[:60]}"
        entries.append(entry)

    while entries:
        text = header + "\n\n" + "\n\n".join(entries)
        if len(text) <= limit:
            return text
        entries.pop()
    return header


def one_line_summary(picked: int, reviewed: int, no_subtitle: int, comment_count: int) -> str:
    parts = [f"看了 {reviewed} 条", f"挑中 {picked} 条"]
    if comment_count:
        parts.append(f"评论 {comment_count} 条")
    parts.append(f"无字幕跳过 {no_subtitle} 条")
    return "，".join(parts)


def webhook_text(day: str, picked: list[dict], summary: str) -> str:
    lines = [f"刷B站 · {day}：{summary}"]
    for pick in picked[:5]:
        lines.append(f"· {pick.get('title') or pick['bvid']} {VIDEO_URL.format(bvid=pick['bvid'])}")
    return "\n".join(lines)
