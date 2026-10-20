"""命令行入口：``biliduty <命令>``。

    biliduty init          铺一份 config.json
    biliduty login         扫码登录（服务器上把二维码图拉下来扫）
    biliduty doctor        体检：配置 / 凭证 / 模型 key / 目录
    biliduty run           立刻跑一轮（--dry-run 只出预览报告，不发言）
    biliduty serve         常驻：调度 + 控制页（容器和 systemd 跑的就是这个）
    biliduty status        看今天挑了几条、下次什么时候跑
    biliduty report        打印今天的日报（--path 只给路径）
    biliduty log           看日志尾部
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from . import state as st
from .bilibili import Bili, BiliError, load_credential, qr_login
from .config import Config, ConfigError, init_config
from .pipeline import RoundResult, run_round
from .scheduler import Scheduler, load_last_round
from .server import ApiServer

log = logging.getLogger("biliduty")


# ══════════════════════════════════════════════════════════════════════════
# 环境准备
# ══════════════════════════════════════════════════════════════════════════

def ensure_home(cfg: Config) -> None:
    """建目录、把包内的 asr.py 铺到数据目录（保证缓存/临时文件都落在数据卷里）。"""
    for sub in ("out", "digest", "tmp/asr"):
        (cfg.data_dir / sub).mkdir(parents=True, exist_ok=True)
    try:
        source = Path(__file__).parent / "data" / "asr.py"
        target = cfg.data_dir / "asr.py"
        if source.is_file() and (not target.is_file() or source.read_bytes() != target.read_bytes()):
            shutil.copyfile(source, target)
    except OSError as exc:
        log.debug("铺 asr.py 跳过：%s", exc)


def setup_logging(cfg: Config, verbose: bool = False) -> None:
    logger = logging.getLogger("biliduty")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.addHandler(console)

    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            cfg.data_dir / "duty.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning("写不了日志文件：%s", exc)


def _load(args) -> Config:
    _apply_env(args)
    return Config.load()


def _apply_env(args) -> None:
    """把 --data / --config 变成环境变量（Config.load 只认环境变量与默认值）。"""
    if getattr(args, "data", None):
        os.environ["BILIDUTY_DATA"] = str(Path(args.data).expanduser().resolve())
    if getattr(args, "config", None):
        os.environ["BILIDUTY_CONFIG"] = str(Path(args.config).expanduser().resolve())


# ══════════════════════════════════════════════════════════════════════════
# 命令
# ══════════════════════════════════════════════════════════════════════════

def cmd_init(args) -> int:
    _apply_env(args)
    path = init_config(args.config or os.environ.get("BILIDUTY_CONFIG"), force=args.force)
    print(f"配置在 {path}")
    if args.force:
        print("（--force：已有的内容被默认值覆盖了）")
    else:
        print("（已存在就不动它；要重置用 --force）")
    print("下一步：把 DEEPSEEK_API_KEY 写进同目录的 .env，再跑 `biliduty login` 扫码。")
    return 0


def cmd_login(args) -> int:
    cfg = _load(args)
    ensure_home(cfg)
    try:
        asyncio.run(qr_login(cfg.data_dir, timeout_seconds=args.timeout))
    except BiliError as exc:
        print(f"登录失败：{exc}", file=sys.stderr)
        return 1
    return 0


def cmd_run(args) -> int:
    cfg = _load(args)
    ensure_home(cfg)
    setup_logging(cfg, args.verbose)
    try:
        result = asyncio.run(run_round(
            cfg,
            why="manual",
            dry_run=args.dry_run,
            keywords=args.keyword or None,
        ))
        if not args.dry_run:
            asyncio.run(_notify(cfg, result))
            state = st.State.load(cfg.data_dir / "state.json", st.day_key(datetime.now(cfg.tz())))
            state.lastRunAt = datetime.now(cfg.tz()).isoformat()
            state.lastWhy = "manual"
            state.lastResult = result.summary
            state.save()
    except Exception as exc:
        log.exception("这一轮失败了")
        print(f"失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(result.summary)
    if result.report_file:
        print(f"日报：{result.report_file}")
    for error in result.errors:
        print(f"⚠️ {error}", file=sys.stderr)
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0


async def _notify(cfg: Config, result: RoundResult) -> None:
    from . import notify as notify_mod

    await notify_mod.notify_round(cfg, result)


def cmd_status(args) -> int:
    cfg = _load(args)
    now = datetime.now(cfg.tz())
    today = st.day_key(now)
    state = st.State.load(cfg.data_dir / "state.json", today)
    picked = st.picked_today(cfg.data_dir, state, now)
    quota = int(cfg.get("perDay", default=0))
    scheduler = Scheduler(cfg)
    last = load_last_round(cfg.data_dir)

    print(f"刷B站 v{__version__} · {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"配置：{cfg.path}")
    print(f"数据：{cfg.data_dir}")
    print(f"额度：{picked}/{quota} 条（今天跑了 {state.count} 轮，最多 {cfg.get('maxRoundsPerDay')} 轮）")
    print(f"窗口：{', '.join(cfg.enabled_windows()) or '无'}"
          + ("（周末档开着）" if cfg.get('weekendWindow') else ""))
    print(f"下次：{scheduler.next_run_text(now)}")
    print(f"模型：{cfg.get('llm', 'model')} @ {cfg.get('llm', 'baseUrl')}"
          + ("" if cfg.api_key() else "  ⚠️ 没配 DEEPSEEK_API_KEY"))
    cred = load_credential(cfg.data_dir)
    print(f"B站：{'已登录' if cred else '⚠️ 未登录（先跑 biliduty login）'}")
    if last:
        print(f"上一轮：{last.get('summary')}（{last.get('startedAt', '')[:16]}，{last.get('durationSeconds')}s）")
        if last.get("picked"):
            for pick in last["picked"][:5]:
                print(f"   · {pick.get('bvid')} {pick.get('title')}")
    return 0


def cmd_report(args) -> int:
    cfg = _load(args)
    now = datetime.now(cfg.tz())
    date_text = args.date or st.day_key(now)
    path = cfg.data_dir / "out" / f"{date_text}.md"
    if not path.is_file():
        print(f"{date_text} 还没有日报（{path}）", file=sys.stderr)
        return 1
    if args.path:
        print(path)
        return 0
    print(path.read_text(encoding="utf-8"))
    return 0


def cmd_log(args) -> int:
    cfg = _load(args)
    path = cfg.data_dir / "duty.log"
    if not path.is_file():
        print(f"还没有日志：{path}", file=sys.stderr)
        return 1
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-max(1, args.lines):]))
    return 0


def cmd_doctor(args) -> int:
    cfg = _load(args)
    ensure_home(cfg)
    failures = 0

    def check(label: str, ok: bool, detail: str = "", hard: bool = True) -> None:
        nonlocal failures
        mark = "✓" if ok else ("✗" if hard else "·")
        if not ok and hard:
            failures += 1
        print(f"{mark} {label}" + (f"：{detail}" if detail else ""))

    check("Python 版本", sys.version_info >= (3, 10), sys.version.split()[0])
    try:
        import bilibili_api  # noqa: F401
        check("bilibili-api 已安装", True)
    except Exception as exc:
        check("bilibili-api 已安装", False, str(exc))
    try:
        import httpx  # noqa: F401
        check("httpx 已安装", True)
    except Exception as exc:
        check("httpx 已安装", False, str(exc))

    check("配置能读", True, str(cfg.path))
    try:
        probe = cfg.data_dir / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        check("数据目录可写", True, str(cfg.data_dir))
    except OSError as exc:
        check("数据目录可写", False, str(exc))

    cred = load_credential(cfg.data_dir)
    check("B站凭证", bool(cred), "已配置" if cred else "没有：跑 `biliduty login`，或填 BILI_SESSDATA 等环境变量")
    if cred and not args.offline:
        try:
            identity = asyncio.run(Bili(cred).identity())
            check("凭证可用", True, f"{identity.get('name')}（uid {identity.get('uid')}）")
        except Exception as exc:
            check("凭证可用", False, f"{type(exc).__name__}: {exc}")
    check("模型 key", bool(cfg.api_key()), "已配置" if cfg.api_key() else "没有 DEEPSEEK_API_KEY")
    if cfg.api_key() and args.llm:
        try:
            from .llm import LLMClient

            client = LLMClient(cfg)
            text = asyncio.run(client.chat(
                [{"role": "user", "content": '只回复 JSON：{"ok":true}'}], max_tokens=32,
            ))
            check("模型连通", "ok" in text, text[:80])
        except Exception as exc:
            check("模型连通", False, f"{type(exc).__name__}: {exc}")

    persona = str(cfg.get("personaFile") or "").strip()
    check("人设文件", (not persona) or Path(persona).expanduser().is_file(),
          persona or "没配（按中性说话）", hard=False)
    check("自动评论", not cfg.get("autoComment") or bool(str(cfg.get("mentionName") or "").strip()),
          "开着，@ " + str(cfg.get("mentionName")) if cfg.get("autoComment") else "关着", hard=False)
    if bool(cfg.get("transcribe")):
        python = str(cfg.get("asrPython") or "").strip() or sys.executable
        script = cfg.data_dir / "asr.py"
        ok = Path(python).exists() and script.is_file()
        detail = f"python={python} script={script}"
        if ok:
            try:
                probe = subprocess.run(
                    [python, "-c", "import faster_whisper, yt_dlp"],
                    capture_output=True, timeout=60,
                )
                ok = probe.returncode == 0
                if not ok:
                    detail += "（这个解释器里没有 faster_whisper / yt_dlp）"
            except (OSError, subprocess.TimeoutExpired) as exc:
                ok = False
                detail += f"（探针失败：{exc}）"
        check("本地转写", ok, detail, hard=False)
    else:
        check("本地转写", True, "关着（没字幕的视频会跳过）", hard=False)
    url = str(cfg.get("webhook", "url") or "")
    check("Webhook", True, "已配" if url else "没配（只出文件和网页）", hard=False)

    print()
    if failures:
        print(f"有 {failures} 项要处理，先别急着部署。")
        return 1
    print("体检通过。")
    return 0


def cmd_serve(args) -> int:
    cfg = _load(args)
    ensure_home(cfg)
    setup_logging(cfg, args.verbose)
    log.info("刷B站 v%s 常驻启动 · 数据目录 %s", __version__, cfg.data_dir)
    if not cfg.api_key():
        log.warning("⚠️ 没有 DEEPSEEK_API_KEY —— 自动跑会失败，先补上")
    if not load_credential(cfg.data_dir):
        log.warning("⚠️ 没有 B站 凭证 —— 先跑 `biliduty login`")
    try:
        asyncio.run(_serve(cfg, args))
    except KeyboardInterrupt:
        log.info("收到中断，退出")
    return 0


async def _serve(cfg: Config, args) -> None:
    scheduler = Scheduler(cfg)
    scheduler.loop = asyncio.get_running_loop()

    server = None
    if cfg.get("server", "enabled") and not args.no_http:
        try:
            server = ApiServer(cfg, scheduler)
            server.start()
        except OSError as exc:
            log.error("控制页起不来（端口被占？）：%s —— 只跑调度", exc)
            server = None

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    installed = False
    for signal_name in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(__import__("signal"), signal_name), stop.set)
            installed = True
        except (NotImplementedError, AttributeError, ValueError, RuntimeError):
            pass
    if not installed and os.name == "nt":
        log.info("Windows 下没有信号处理，按 Ctrl+C 退出")

    runner = asyncio.create_task(scheduler.run_forever(), name="biliduty-scheduler")
    if args.run_now:
        asyncio.create_task(scheduler._guarded("startup"))  # noqa: SLF001（同一个包里的内部方法）

    try:
        await stop.wait()
    finally:
        scheduler.stop()
        try:
            await asyncio.wait_for(runner, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            runner.cancel()
        for task in list(scheduler._tasks):  # noqa: SLF001
            task.cancel()
        if server:
            server.stop()
        log.info("已退出")


# ══════════════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="biliduty",
        description="刷B站 · 不依赖 DeepSeek Harness 的独立常驻服务",
    )
    parser.add_argument("--version", action="version", version=f"biliduty {__version__}")
    parser.add_argument("--config", help="config.json 路径（默认 <数据目录>/config.json）")
    parser.add_argument("--data", help="数据目录（默认 $BILIDUTY_DATA 或 ./data）")
    parser.add_argument("-v", "--verbose", action="store_true", help="更啰嗦的日志")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="铺一份默认 config.json")
    p_init.add_argument("--force", action="store_true", help="覆盖已有配置")
    p_init.set_defaults(func=cmd_init)

    p_login = sub.add_parser("login", help="扫码登录 B站")
    p_login.add_argument("--timeout", type=int, default=180)
    p_login.set_defaults(func=cmd_login)

    p_run = sub.add_parser("run", help="立刻跑一轮")
    p_run.add_argument("--dry-run", action="store_true", help="只出预览报告：不发评论/私信/通知，不动台账")
    p_run.add_argument("--keyword", action="append", help="临时指定关键词（可重复）")
    p_run.add_argument("--json", action="store_true", help="额外打印结构化结果")
    p_run.set_defaults(func=cmd_run)

    p_serve = sub.add_parser("serve", help="常驻运行（调度 + 控制页）")
    p_serve.add_argument("--no-http", action="store_true", help="不起控制页")
    p_serve.add_argument("--run-now", action="store_true", help="启动后立刻补跑一轮")
    p_serve.set_defaults(func=cmd_serve)

    p_status = sub.add_parser("status", help="看状态")
    p_status.set_defaults(func=cmd_status)

    p_report = sub.add_parser("report", help="打印日报")
    p_report.add_argument("--date", help="YYYY-MM-DD（默认今天）")
    p_report.add_argument("--path", action="store_true", help="只打印路径")
    p_report.set_defaults(func=cmd_report)

    p_log = sub.add_parser("log", help="看日志尾部")
    p_log.add_argument("-n", "--lines", type=int, default=50)
    p_log.set_defaults(func=cmd_log)

    p_doc = sub.add_parser("doctor", help="体检：配置 / 凭证 / key / 目录")
    p_doc.add_argument("--offline", action="store_true", help="不访问网络")
    p_doc.add_argument("--llm", action="store_true", help="真的调一次模型")
    p_doc.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK：✓ / ⚠️ 这类符号会 UnicodeEncodeError —— 统一成 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"配置有问题：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
