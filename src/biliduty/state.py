"""台账层：配额状态、判重表、归档、日报。

沿用原插件的数据形状，这样从 DSH 版迁过来时**同一批账能接着用**：

    data/
      config.json
      .env
      bili_credential.json     # 扫码登录写在这里（或改用 BILI_* 环境变量）
      state.json               # 今天的轮次 / 挑了几条 / 跑过哪个窗口
      seen.md                  # 判重表：评论记录 + 看过的视频
      out/2026-10-20.md        # 给人看的日报
      digest/2026-10.md        # 给"以后回头翻"用的归档
      duty.log

日报里 ``### BV号`` 的标题行就是"今天挑了几条"的唯一事实来源（与原插件一致），
state.json 只做兜底，取两者较大值。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Iterable

# ── 日报里"挑中"的标题行：##/### + 空格 + BV 号（顶格）──────────────────────
PICK_RE = re.compile(r"^#{2,3}[ \t]*(BV[0-9A-Za-z]{10})", re.MULTILINE)
_FENCE_RE = re.compile(r"```[\s\S]*?```")

SEEN_HEADER = "# 刷B站 · 判重表（宿主预建，那一轮只往后面追加）\n\n"
COMMENT_HEADER = (
    "## 评论记录（BV | rpid | root（楼中楼才填） | 时间 | 正文前 20 字）\n\n"
    "| BV | rpid | root（楼中楼才填） | 时间 | 正文前 20 字 |\n"
    "| --- | --- | --- | --- | --- |\n"
)
VIDEOS_HEADER = "\n## 看过的视频\n\n"

_VIDEO_LINE_RE = re.compile(r"^-\s*(?P<stamp>[^·]+?)\s*·\s*(?P<bv>BV[0-9A-Za-z]{10})\s*·\s*(?P<rest>.*)$")
_COMMENT_ROW_RE = re.compile(r"^\|\s*(?P<bv>BV[0-9A-Za-z]{10})\s*\|")


# ══════════════════════════════════════════════════════════════════════════
# 时间
# ══════════════════════════════════════════════════════════════════════════

def now_in(tz: tzinfo | None = None) -> datetime:
    return datetime.now(tz) if tz else datetime.now().astimezone()


def day_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%d %H:%M")


def minutes_of(hhmm: str) -> int | None:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(hhmm or ""))
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if not (0 <= hours <= 24 and 0 <= minutes < 60):
        return None
    return hours * 60 + minutes


def in_window(now: datetime, start: str, end: str) -> bool:
    """现在在不在这个窗口里（支持跨零点，例如 18:00–09:00）。"""
    begin, finish = minutes_of(start), minutes_of(end)
    if begin is None or finish is None:
        return False
    current = now.hour * 60 + now.minute
    if begin == finish:  # 24 小时窗口（begin=0,end=0 视为全天）
        return True
    return begin <= current < finish if begin < finish else (current >= begin or current < finish)


def next_window_start(now: datetime, windows: dict[str, dict], names: Iterable[str]) -> datetime | None:
    """下一次自动跑的大致时间（只用于 /status 展示，不做精确排程）。"""
    best: datetime | None = None
    for name in names:
        spec = windows.get(name) or {}
        begin = minutes_of(spec.get("start", ""))
        if begin is None:
            continue
        target = now.replace(hour=begin // 60, minute=begin % 60, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        if best is None or target < best:
            best = target
    return best


# ══════════════════════════════════════════════════════════════════════════
# 文件小工具
# ══════════════════════════════════════════════════════════════════════════

def atomic_write(path: Path, text: str) -> None:
    """先写临时文件再 replace —— 写到一半断电也不会毁掉旧账。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


# ══════════════════════════════════════════════════════════════════════════
# state.json —— 今天的轮次与配额
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class State:
    path: Path
    day: str = ""
    count: int = 0            # 今天自动/手动一共跑了几轮
    picked: int = 0           # 今天挑中几条（兜底，事实以日报为准）
    windows: dict[str, bool] = field(default_factory=dict)  # 今天跑过哪些窗口
    lastRunAt: str = ""
    lastWhy: str = ""
    lastResult: str = ""

    @classmethod
    def load(cls, path: Path, today: str) -> "State":
        raw: dict[str, Any] = {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, ValueError):
            pass
        state = cls(
            path=path,
            day=str(raw.get("day") or ""),
            count=int(raw.get("count") or 0),
            picked=int(raw.get("picked") or 0),
            windows=dict(raw.get("windows") or {}),
            lastRunAt=str(raw.get("lastRunAt") or ""),
            lastWhy=str(raw.get("lastWhy") or ""),
            lastResult=str(raw.get("lastResult") or ""),
        )
        if state.day != today:  # 跨天清零
            state.day = today
            state.count = 0
            state.picked = 0
            state.windows = {}
        return state

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "count": self.count,
            "picked": self.picked,
            "windows": dict(self.windows),
            "lastRunAt": self.lastRunAt,
            "lastWhy": self.lastWhy,
            "lastResult": self.lastResult,
        }

    def save(self) -> None:
        atomic_write(self.path, json.dumps(self.as_dict(), ensure_ascii=False, indent=2) + "\n")

    def mark_window(self, name: str, now: datetime, why: str) -> None:
        self.count += 1
        self.windows[name] = True
        self.lastRunAt = now.isoformat()
        self.lastWhy = why

    def reset(self, today: str) -> None:
        self.day = today
        self.count = 0
        self.picked = 0
        self.windows = {}
        self.lastRunAt = ""
        self.lastWhy = ""
        self.lastResult = ""


# ══════════════════════════════════════════════════════════════════════════
# 日报
# ══════════════════════════════════════════════════════════════════════════

def report_path(data_dir: Path, now: datetime) -> Path:
    return data_dir / "out" / f"{day_key(now)}.md"


def count_picks(path: Path) -> int:
    """数日报里的 ``### BV…`` 标题行（代码围栏里的不算）。读不出来就是 0。"""
    text = _FENCE_RE.sub("", read_text(path))
    return len({m.group(1) for m in PICK_RE.finditer(text)})


def picked_today(data_dir: Path, state: State, now: datetime) -> int:
    """日报是事实来源，state 兜住"报告被删 / 手改"那一下，取两者较大值。"""
    return max(int(state.picked or 0), count_picks(report_path(data_dir, now)))


def ensure_report_header(path: Path, now: datetime, keywords: list[str]) -> None:
    if path.exists():
        return
    title = f"# 刷B站 · {day_key(now)}"
    if keywords:
        title += "（关键词：" + "、".join(keywords) + "）"
    atomic_write(path, title + "\n\n")


def prune_reports(data_dir: Path, keep_days: int, now: datetime) -> int:
    """删掉太旧的日报，返回删了几个。0 = 不清。"""
    if keep_days <= 0:
        return 0
    folder = data_dir / "out"
    if not folder.is_dir():
        return 0
    cutoff = (now - timedelta(days=keep_days)).date()
    removed = 0
    for item in folder.glob("*.md"):
        try:
            file_day = date.fromisoformat(item.stem)
        except ValueError:
            continue
        if file_day < cutoff:
            try:
                item.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# ══════════════════════════════════════════════════════════════════════════
# seen.md —— 判重表
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class SeenEntry:
    stamp: str
    bvid: str
    title: str
    author: str
    verdict: str


@dataclass
class CommentEntry:
    bvid: str
    rpid: str
    root: str = ""
    stamp: str = ""
    preview: str = ""


class SeenTable:
    """读 / 追加 / 回写判重表。保持原插件那种「两张表」的形状。"""

    def __init__(self, path: Path):
        self.path = path
        self.videos: list[SeenEntry] = []
        self.comments: list[CommentEntry] = []
        self.bvids: set[str] = set()
        self.error: str = ""

    # ── 读写 ────────────────────────────────────────────────────────────
    @classmethod
    def load(cls, path: Path) -> "SeenTable":
        table = cls(path)
        if not path.exists():
            table.error = "missing"
            return table
        text = read_text(path)
        if not text:
            table.error = "unreadable"
            return table
        section = ""
        for raw in text.splitlines():
            line = raw.rstrip()
            if line.startswith("## "):
                section = "comments" if "评论记录" in line else ("videos" if "看过的视频" in line else "")
                continue
            if section == "comments":
                if not _COMMENT_ROW_RE.match(line):
                    continue
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) < 5:
                    continue
                entry = CommentEntry(bvid=cells[0], rpid=cells[1], root=cells[2], stamp=cells[3], preview=cells[4])
                table.comments.append(entry)
            elif section == "videos":
                match = _VIDEO_LINE_RE.match(line.strip())
                if not match:
                    continue
                rest = [part.strip() for part in match.group("rest").split("·")]
                table.videos.append(SeenEntry(
                    stamp=match.group("stamp").strip(),
                    bvid=match.group("bv"),
                    title=rest[0] if rest else "",
                    author=rest[1] if len(rest) > 1 else "",
                    verdict="·".join(rest[2:]) if len(rest) > 2 else "",
                ))
        table.bvids = {entry.bvid for entry in table.videos} | {entry.bvid for entry in table.comments}
        return table

    def render(self) -> str:
        parts = [SEEN_HEADER, COMMENT_HEADER]
        for entry in self.comments:
            parts.append(
                f"| {entry.bvid} | {entry.rpid} | {entry.root} | {entry.stamp} | {entry.preview} |\n"
            )
        parts.append(VIDEOS_HEADER)
        for entry in self.videos:
            parts.append(f"- {entry.stamp} · {entry.bvid} · {entry.title} · {entry.author} · {entry.verdict}\n")
        return "".join(parts)

    def save(self) -> None:
        if self.error == "unreadable":
            # 原来那份读不出来：**绝不覆盖**（覆盖掉就等于把"我看过什么"清零，会重复推送）
            return
        atomic_write(self.path, self.render())

    # ── 查询 ────────────────────────────────────────────────────────────
    def seen(self, bvid: str) -> bool:
        return bvid in self.bvids

    def commented(self, bvid: str) -> bool:
        return any(entry.bvid == bvid for entry in self.comments)

    def comment_rpids(self) -> list[CommentEntry]:
        return list(self.comments)

    # ── 追加 ────────────────────────────────────────────────────────────
    def add_video(self, when: datetime, bvid: str, title: str, author: str, verdict: str) -> None:
        self.videos.append(SeenEntry(
            stamp=stamp(when), bvid=bvid, title=_clean_cell(title), author=_clean_cell(author),
            verdict=_clean_cell(verdict),
        ))
        self.bvids.add(bvid)

    def add_comment(self, when: datetime, bvid: str, rpid: str, root: str, text: str) -> None:
        self.comments.append(CommentEntry(
            bvid=bvid, rpid=str(rpid or "未取到 rpid"), root=str(root or ""),
            stamp=stamp(when), preview=_clean_cell(text)[:20],
        ))
        self.bvids.add(bvid)


def _clean_cell(text: str) -> str:
    """表格 / 单行里的分隔符要清掉，否则回读会错位。"""
    return str(text or "").replace("|", "／").replace("·", "•").replace("\n", " ").strip()


# ══════════════════════════════════════════════════════════════════════════
# digest —— 归档（给"以后回头翻"用）
# ══════════════════════════════════════════════════════════════════════════

def append_digest(data_dir: Path, when: datetime, line: str, keywords: list[str]) -> Path:
    folder = data_dir / "digest"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{day_key(when)[:7]}.md"
    if not path.exists():
        title = f"# {day_key(when)[:7]} · 归档"
        if keywords:
            title += "（关键词：" + "、".join(keywords) + "）"
        atomic_write(path, title + "\n\n")
    append_text(path, line.rstrip("\n") + "\n")
    return path
