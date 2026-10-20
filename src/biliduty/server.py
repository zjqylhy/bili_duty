"""HTTP 控制页：看一眼今天挑了什么、还差几条、下一次什么时候跑；也能手动叫一轮。

用标准库 ``http.server`` 实现 —— 少一个依赖，镜像小一圈，对一台只服务自己的机器够了。
``GET`` 全部只读；``POST /run`` 需要 ``BILIDUTY_TOKEN``（没配就一律拒绝）。
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from . import state as st
from .config import Config
from .scheduler import Scheduler, load_last_round

log = logging.getLogger("biliduty.server")


def _json(status: int, payload: dict) -> tuple[int, str, bytes]:
    return status, "application/json; charset=utf-8", (
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def _text(status: int, body: str, content_type: str = "text/plain; charset=utf-8") -> tuple[int, str, bytes]:
    return status, content_type, body.encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server_version = "biliduty/" + __version__
    cfg: Config
    scheduler: Scheduler

    # ── 路由 ────────────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802（标准库的命名）
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if path == "/healthz":
                status, ctype, body = _json(200, {"ok": True, "running": self.scheduler.running})
            elif path == "/status":
                status, ctype, body = _json(200, self._status())
            elif path == "/report":
                status, ctype, body = self._report(query)
            elif path == "/":
                status, ctype, body = _text(200, self._page(), "text/html; charset=utf-8")
            else:
                status, ctype, body = _json(404, {"error": "没有这个路径"})
        except Exception as exc:  # 控制页自己不能把进程带崩
            log.exception("GET %s 出错", path)
            status, ctype, body = _json(500, {"error": f"{type(exc).__name__}: {exc}"})
        self._send(status, ctype, body)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path != "/run":
            self._send(*_json(404, {"error": "没有这个路径"}))
            return
        if not self._authorized(parsed.query):
            self._send(*_json(401, {
                "error": "需要口令：请求头 Authorization: Bearer <BILIDUTY_TOKEN>"
                         if self.cfg.http_token() else "服务器没配 BILIDUTY_TOKEN，写操作已禁用",
            }))
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        payload: dict = {}
        if raw:
            try:
                parsed_body = json.loads(raw.decode("utf-8"))
                if isinstance(parsed_body, dict):
                    payload = parsed_body
            except ValueError:
                self._send(*_json(400, {"error": "请求体不是 JSON"}))
                return
        keywords = payload.get("keywords")
        if keywords is not None and (not isinstance(keywords, list) or not all(isinstance(k, str) for k in keywords)):
            self._send(*_json(400, {"error": "keywords 应该是字符串数组"}))
            return
        dry_run = bool(payload.get("dryRun"))
        try:
            self.scheduler.submit_threadsafe("http", keywords=keywords, dry_run=dry_run)
        except RuntimeError as exc:
            self._send(*_json(503, {"error": str(exc)}))
            return
        self._send(*_json(202, {"accepted": True, "dryRun": dry_run}))

    # ── 内容 ────────────────────────────────────────────────────────────
    def _authorized(self, query_string: str) -> bool:
        expected = self.cfg.http_token()
        if not expected:
            return False
        header = self.headers.get("Authorization") or ""
        provided = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not provided:
            provided = (parse_qs(query_string).get("token") or [""])[0]
        return bool(provided) and hmac.compare_digest(provided, expected)

    def _state(self, now: datetime) -> tuple[st.State, int, int]:
        state = st.State.load(self.cfg.data_dir / "state.json", st.day_key(now))
        picked = st.picked_today(self.cfg.data_dir, state, now)
        quota = int(self.cfg.get("perDay", default=0))
        return state, picked, max(0, quota - picked) if quota else 0

    def _status(self) -> dict:
        now = datetime.now(self.cfg.tz())
        state, picked, left = self._state(now)
        windows = self.cfg.get("windows") or {}
        names = list(self.cfg.enabled_windows())
        if self.cfg.get("weekendWindow") and now.weekday() >= 5:
            names.append("weekend")
        return {
            "ok": True,
            "version": __version__,
            "now": now.isoformat(timespec="seconds"),
            "day": st.day_key(now),
            "running": self.scheduler.running,
            "quota": {
                "perDay": int(self.cfg.get("perDay", default=0)),
                "pickedToday": picked,
                "left": left,
                "rounds": state.count,
                "maxRoundsPerDay": int(self.cfg.get("maxRoundsPerDay", default=0)),
            },
            "windows": {
                name: {
                    "start": (windows.get(name) or {}).get("start"),
                    "end": (windows.get(name) or {}).get("end"),
                    "doneToday": bool(state.windows.get(name)),
                    "active": st.in_window(now, (windows.get(name) or {}).get("start", ""),
                                           (windows.get(name) or {}).get("end", "")) if name in windows else False,
                }
                for name in names if name in windows
            },
            "nextRun": self.scheduler.next_run_text(now),
            "lastRound": load_last_round(self.cfg.data_dir),
            "config": self.cfg.redacted(),
            "lastError": self.scheduler.last_error,
        }

    def _report(self, query: dict) -> tuple[int, str, bytes]:
        now = datetime.now(self.cfg.tz())
        date_text = (query.get("date") or [""])[0].strip()
        try:
            if date_text:
                datetime.strptime(date_text, "%Y-%m-%d")
            else:
                date_text = st.day_key(now)
        except ValueError:
            return _json(400, {"error": "date 要写成 YYYY-MM-DD"})
        path = self.cfg.data_dir / "out" / f"{date_text}.md"
        if not path.is_file():
            return _json(404, {"error": f"{date_text} 还没有日报"})
        return _text(200, path.read_text(encoding="utf-8"), "text/markdown; charset=utf-8")

    def _page(self) -> str:
        now = datetime.now(self.cfg.tz())
        state, picked, left = self._state(now)
        quota = int(self.cfg.get("perDay", default=0))
        last = load_last_round(self.cfg.data_dir)
        report_path = st.report_path(self.cfg.data_dir, now)
        report_text = report_path.read_text(encoding="utf-8") if report_path.is_file() else "（今天还没有日报）"
        last_summary = (last.get("summary") or "还没跑过") if last else "还没跑过"
        next_run = self.scheduler.next_run_text(now)
        rows = [
            ("今天", st.day_key(now)),
            ("额度", f"{picked} / {quota}（还剩 {left}）"),
            ("今天跑过", f"{state.count} 轮"),
            ("上一轮", last_summary),
            ("下一次自动跑", next_run),
            ("正在跑", "是" if self.scheduler.running else "否"),
            ("版本", __version__),
        ]
        table = "".join(
            f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
            for key, value in rows
        )
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>刷B站 · {html.escape(st.day_key(now))}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.7 -apple-system, "Segoe UI", "Noto Sans SC", sans-serif; margin: 0 auto; max-width: 860px; padding: 28px 20px 60px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .sub {{ opacity: .65; font-size: 13px; margin-bottom: 20px; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 24px; }}
  th, td {{ text-align: left; padding: 7px 10px; border-bottom: 1px solid rgba(128,128,128,.28); vertical-align: top; }}
  th {{ width: 38%; font-weight: 600; opacity: .8; }}
  pre {{ white-space: pre-wrap; word-break: break-word; background: rgba(128,128,128,.10); padding: 16px; border-radius: 10px; font: 13px/1.65 ui-monospace, Consolas, monospace; }}
  code {{ font: 12.5px ui-monospace, Consolas, monospace; }}
  .hint {{ opacity: .6; font-size: 12.5px; margin-top: 22px; }}
</style></head>
<body>
  <h1>刷B站 · {html.escape(st.day_key(now))}</h1>
  <div class="sub">不依赖 DeepSeek Harness 的独立常驻服务 · 本页每 60 秒自动刷新</div>
  <table>{table}</table>
  <h2 style="font-size:16px;margin:0 0 8px">今天的日报</h2>
  <pre>{html.escape(report_text)}</pre>
  <div class="hint">
    手动跑一轮：<code>curl -X POST -H "Authorization: Bearer $BILIDUTY_TOKEN" http://127.0.0.1:{self.cfg.get("server", "port")}/run</code><br>
    只看数据：<code>GET /status</code> · <code>GET /report?date=YYYY-MM-DD</code> · <code>GET /healthz</code>
  </div>
</body></html>"""

    # ── 发送 ────────────────────────────────────────────────────────────
    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt: str, *args) -> None:  # 别把访问日志刷进 duty.log
        log.debug("%s - %s", self.address_string(), fmt % args)


class ApiServer:
    def __init__(self, cfg: Config, scheduler: Scheduler):
        self.cfg = cfg
        self.scheduler = scheduler
        handler = type("Handler", (_Handler,), {"cfg": cfg, "scheduler": scheduler})
        self.httpd = ThreadingHTTPServer(
            (str(cfg.get("server", "host") or "127.0.0.1"), int(cfg.get("server", "port") or 8787)),
            handler,
        )
        self.httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="biliduty-http", daemon=True)
        self._thread.start()
        log.info("控制页在 %s（GET / 看今天，POST /run 手动跑一轮）", self.address)

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
