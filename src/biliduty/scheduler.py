"""调度：什么时候自动跑一轮。

规则跟原插件一致（谷时窗口 + 每天总量 + 每个窗口只跑一次），另外补了两条它没有的：

- **周末单独一档**（DeepSeek 周末全天谷时，不必等到中午/傍晚）
- **错过窗口不补跑**：只在窗口内起跑，免得半夜补跑一次还发评论

窗口时间、每天的额度、跑不跑，全在 ``config.json`` 里，不用改代码。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from . import notify, state as st
from .config import Config
from .pipeline import RoundResult, run_round

log = logging.getLogger("biliduty.scheduler")

TICK_SECONDS = 60


class Scheduler:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()
        self.running = False
        self.last: RoundResult | None = None
        self.last_error = ""
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()

    # ── 判断"该不该跑"─────────────────────────────────────────────────
    def due(self, now: datetime) -> tuple[str, str] | None:
        cfg = self.cfg
        if int(cfg.get("perDay", default=0)) <= 0:
            return None

        today = st.day_key(now)
        state = st.State.load(cfg.data_dir / "state.json", today)
        if state.count >= int(cfg.get("maxRoundsPerDay", default=2)):
            return None
        if st.picked_today(cfg.data_dir, state, now) >= int(cfg.get("perDay", default=0)):
            return None

        names = list(cfg.enabled_windows())
        if cfg.get("weekendWindow") and now.weekday() >= 5:
            names.append("weekend")

        windows = cfg.get("windows") or {}
        for name in names:
            spec = windows.get(name)
            if not spec:
                continue
            if state.windows.get(name):
                continue
            if st.in_window(now, spec.get("start", ""), spec.get("end", "")):
                return name, f"window:{name}"
        return None

    def next_run_text(self, now: datetime | None = None) -> str:
        now = now or datetime.now(self.cfg.tz())
        today = st.day_key(now)
        state = st.State.load(self.cfg.data_dir / "state.json", today)
        names = list(self.cfg.enabled_windows())
        if self.cfg.get("weekendWindow") and now.weekday() >= 5:
            names.append("weekend")
        windows = self.cfg.get("windows") or {}
        if int(self.cfg.get("perDay", default=0)) <= 0:
            return "自动跑是关的（perDay = 0）"
        pending = [name for name in names if name in windows and not state.windows.get(name)]
        if not pending:
            return "今天的窗口都跑过了"
        target = st.next_window_start(now, windows, pending)
        if not target:
            return "没有可用的窗口"
        return target.strftime("%m-%d %H:%M")

    # ── 跑一轮（带互斥）──────────────────────────────────────────────
    async def run_once(self, why: str, *, keywords: list[str] | None = None, dry_run: bool = False) -> RoundResult:
        if self._lock.locked():
            log.warning("上一轮还在跑，这次跳过（why=%s）", why)
            raise RuntimeError("上一轮还在跑")
        async with self._lock:
            self.running = True
            try:
                log.info("开跑（%s）", why)
                result = await run_round(self.cfg, why=why, keywords=keywords, dry_run=dry_run)
                self.last = result
                self._dump(result)
                await notify.notify_round(self.cfg, result)
                log.info("跑完（%s）：%s", why, result.summary)
                return result
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("这一轮炸了（%s）", why)
                raise
            finally:
                self.running = False

    def _dump(self, result: RoundResult) -> None:
        path = self.cfg.data_dir / "last-round.json"
        try:
            path.write_text(json.dumps(result.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            log.warning("写 last-round.json 失败：%s", exc)

    # ── 常驻循环 ─────────────────────────────────────────────────────
    async def run_forever(self) -> None:
        self.loop = asyncio.get_running_loop()
        log.info(
            "调度器起来了：窗口=%s，每天最多 %s 轮 / %s 条，周末档=%s",
            ",".join(self.cfg.enabled_windows()) or "无",
            self.cfg.get("maxRoundsPerDay"),
            self.cfg.get("perDay"),
            "开" if self.cfg.get("weekendWindow") else "关",
        )
        while not self._stop.is_set():
            try:
                now = datetime.now(self.cfg.tz())
                due = self.due(now)
                if due:
                    name, why = due
                    if not self._lock.locked():
                        state = st.State.load(self.cfg.data_dir / "state.json", st.day_key(now))
                        state.mark_window(name, now, why)
                        state.save()
                        # 不 await 在这里等它跑完：跑一轮可能十几分钟，别挡住 tick
                        asyncio.create_task(self._guarded(why))
                self._cleanup_tasks()
            except Exception:
                log.exception("tick 出错（不影响下一分钟）")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _guarded(self, why: str) -> None:
        try:
            await self.run_once(why)
        except Exception:
            pass  # 已经记过日志；别让 task 把异常抛到事件循环

    def _cleanup_tasks(self) -> None:
        self._tasks = {task for task in self._tasks if not task.done()}

    def stop(self) -> None:
        self._stop.set()

    # ── HTTP /run 用：HTTP 在自己的线程里，必须 call_soon_threadsafe 回到事件循环
    def submit_threadsafe(self, why: str, *, keywords: list[str] | None = None, dry_run: bool = False) -> None:
        loop = self.loop
        if loop is None or not loop.is_running():
            raise RuntimeError("调度器还没起来（事件循环不可用）")

        def _schedule() -> None:
            task = loop.create_task(self.run_once(why, keywords=keywords, dry_run=dry_run))
            self._tasks.add(task)
            task.add_done_callback(lambda _: self._cleanup_tasks())

        loop.call_soon_threadsafe(_schedule)


def load_last_round(data_dir: Path) -> dict:
    try:
        data = json.loads((data_dir / "last-round.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
