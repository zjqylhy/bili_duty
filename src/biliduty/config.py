"""配置层：``data/config.json`` + ``.env`` / 环境变量。

优先级（后者覆盖前者）：内置默认值 → ``config.json`` → ``.env`` → 真实环境变量。

设计上的一条硬规矩：**密钥不进 config.json**。
B站的 cookie 走 ``BILI_SESSDATA`` 等环境变量（或 ``data/bili_credential.json``，扫码登录会自动写），
模型 key 走 ``DEEPSEEK_API_KEY``。这样配置文件可以随便备份、贴进聊天记录也不出事。
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

#: 内置默认值。每一项都可以在 config.json 里覆盖。
DEFAULTS: dict[str, Any] = {
    # ── 时间 ──────────────────────────────────────────────────────────────
    # 服务器一般跑 UTC，这里显式钉住北京时间：窗口判断、日期分档、日报文件名都按它算。
    "timezone": "Asia/Shanghai",
    # ── 刷什么 ────────────────────────────────────────────────────────────
    "keywords": [
        "DeepSeek Harness",
        "DSH 插件",
        "dsh 教程",
        "meow-memory",
        "DeepSeek Harness 桌面端",
    ],
    # 只看最近几天发布的（搜索接口不带时间过滤，本地按 pubdate 筛）
    "searchDays": 7,
    "searchPerKeyword": 12,
    # 发布不满这么多小时的先不看：B站 的 AI 字幕常常晚几个小时才生成
    "minAgeHours": 12,
    # 一轮最多把多少条候选送进"读字幕"（每条 1~2 秒，池子太大会拖很久）
    "candidatePool": 60,
    # pubdate=最新 / click=播放 / totalrank=综合
    "searchOrder": "pubdate",
    # 一轮最多读多少条字幕（每条都要花钱，这个数是成本闸门）
    "candidatesPerRound": 10,
    # ── 每天挑几条 ────────────────────────────────────────────────────────
    "perDay": 5,
    # 一轮最多挑几条（perDay 是全天总量，这个是单轮闸门）
    "perRound": 3,
    # 打分低于这个值不挑（0-100）
    "minScore": 60,
    # 每天最多跑几轮（自动跑）
    "maxRoundsPerDay": 2,
    # ── 什么时候跑（北京时间，跨零点窗口写法与原来一致）──────────────────
    "windows": {
        "noon": {"start": "12:00", "end": "14:00", "label": "中午谷时"},
        "night": {"start": "18:00", "end": "09:00", "label": "晚上/夜间谷时"},
        "weekend": {"start": "09:00", "end": "23:00", "label": "周末白天"},
    },
    "enabledWindows": ["noon", "night"],
    # 周末（周六/周日）额外允许 "weekend" 这一档；DeepSeek 周末全天谷时
    "weekendWindow": True,
    # 手动 ``biliduty run`` 不受窗口限制；这里只管自动跑
    # ── 说不说话 ──────────────────────────────────────────────────────────
    "autoComment": False,
    "mentionName": "",
    "commentPerRound": 1,
    "dmMode": "off",  # off | only | both
    "dmUid": 0,
    # ── 没字幕时要不要本地转写（Whisper，几百 MB，默认关）────────────────
    "transcribe": False,
    "asrPython": "",  # 装了 faster-whisper 的解释器；留空用 sys.executable
    "asrModel": "small",
    "asrScript": "",  # 留空用包内 data/asr.py
    # ── 报告 ──────────────────────────────────────────────────────────────
    "reportLevel": "full",  # full | brief | off
    "reportKeepDays": 90,  # out/ 下超过这么多天的日报自动清掉（0=不清）
    # ── 人设 ──────────────────────────────────────────────────────────────
    # 指向你自己工作区里的 AGENTS.md；留空 = 不读设定、按中性说话（不猜）
    "personaFile": "",
    # ── 模型 ──────────────────────────────────────────────────────────────
    "llm": {
        "baseUrl": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "temperature": 0.3,
        "maxTokens": 4096,
        "timeout": 180,
        "maxRetries": 3,
        # 每轮最多花多少钱的软闸门：估算超过它就少读几条字幕（0 = 不限制）
        "maxRoundsCostCny": 0,
    },
    # ── 通知 ──────────────────────────────────────────────────────────────
    "webhook": {"url": "", "type": "json"},  # json | telegram | feishu
    # ── HTTP 控制页 ───────────────────────────────────────────────────────
    "server": {"enabled": True, "host": "127.0.0.1", "port": 8787},
    # ── 礼貌延迟（秒），别把 B站 接口敲出风控 ───────────────────────────────
    "delays": {"search": 0.5, "subtitle": 0.8, "comments": 0.5},
}

#: 只从环境变量读、永远不落盘的项
SECRET_ENV = {
    "BILI_SESSDATA": "B站 cookie sessdata",
    "BILI_JCT": "B站 cookie bili_jct（csrf）",
    "BILI_BUVID3": "B站 cookie buvid3",
    "BILI_DEDEUSERID": "B站 uid",
    "DEEPSEEK_API_KEY": "模型 API key",
    "BILIDUTY_TOKEN": "HTTP 控制页的写操作口令",
}


class ConfigError(RuntimeError):
    """配置有问题（不是"没配"，是"配错了"）。"""


def load_dotenv(path: Path) -> None:
    """极简 .env 解析（KEY=VALUE / # 注释 / 引号）。已存在的环境变量不覆盖。"""
    if not path or not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def deep_merge(base: dict, override: dict) -> dict:
    """递归合并；override 里的 None 表示"删掉这一项"（回落到再下一层）。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def default_data_dir() -> Path:
    env = os.environ.get("BILIDUTY_DATA", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return (Path.cwd() / "data").resolve()


class Config:
    """一份不可变视图 + 少量校验，别处只读它。"""

    def __init__(self, data: dict[str, Any], path: Path, data_dir: Path):
        self.data = data
        self.path = path
        self.data_dir = data_dir

    # ── 构造 ────────────────────────────────────────────────────────────
    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        cfg_path = Path(path or os.environ.get("BILIDUTY_CONFIG") or default_data_dir() / "config.json")
        cfg_path = cfg_path.expanduser().resolve()
        data_dir = Path(os.environ.get("BILIDUTY_DATA") or cfg_path.parent).expanduser().resolve()

        # .env 放在数据目录里（Docker 里就是 /data/.env），只补不覆盖已有环境变量
        load_dotenv(data_dir / ".env")

        raw: dict[str, Any] = {}
        if cfg_path.is_file():
            try:
                raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ConfigError(f"{cfg_path} 读不出来：{exc}") from exc
            if not isinstance(raw, dict):
                raise ConfigError(f"{cfg_path} 顶层应该是一个 JSON 对象")

        cfg = cls(deep_merge(DEFAULTS, raw), cfg_path, data_dir)
        cfg._validate()
        return cfg

    def _validate(self) -> None:
        mode = str(self.get("dmMode", default="off")).lower()
        if mode not in {"off", "only", "both"}:
            raise ConfigError(f"dmMode 只能是 off / only / both，现在是 {mode!r}")
        level = str(self.get("reportLevel", default="full")).lower()
        if level not in {"full", "brief", "off"}:
            raise ConfigError(f"reportLevel 只能是 full / brief / off，现在是 {level!r}")
        for key in ("perDay", "perRound", "candidatesPerRound", "commentPerRound", "maxRoundsPerDay"):
            value = self.get(key)
            if not isinstance(value, int) or value < 0:
                raise ConfigError(f"{key} 应该是 >=0 的整数，现在是 {value!r}")
        windows = self.get("windows") or {}
        for name in self.enabled_windows():
            if name not in windows:
                raise ConfigError(f"enabledWindows 里的 {name!r} 在 windows 里没有定义")
        if not isinstance(self.get("keywords", default=[]), list):
            raise ConfigError("keywords 应该是字符串数组")
        for value in self.get("keywords") or []:
            if not isinstance(value, str):
                raise ConfigError("keywords 里只能放字符串")

    # ── 读 ──────────────────────────────────────────────────────────────
    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self.data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def enabled_windows(self) -> list[str]:
        names = [str(n) for n in (self.get("enabledWindows") or [])]
        return names

    def tz(self):
        return _zone(self.get("timezone", default="Asia/Shanghai"))

    def api_key(self) -> str:
        return (os.environ.get("DEEPSEEK_API_KEY") or "").strip()

    def http_token(self) -> str:
        return (os.environ.get("BILIDUTY_TOKEN") or "").strip()

    def redacted(self) -> dict[str, Any]:
        """给 /status 用的安全视图：只报"配没配"，不报值。"""
        return {
            "path": str(self.path),
            "dataDir": str(self.data_dir),
            "llm": {
                "baseUrl": self.get("llm", "baseUrl"),
                "model": self.get("llm", "model"),
                "apiKeyConfigured": bool(self.api_key()),
            },
            "biliCredential": {
                "file": str(self.data_dir / "bili_credential.json"),
                "fileExists": (self.data_dir / "bili_credential.json").is_file(),
            },
            "httpTokenConfigured": bool(self.http_token()),
            "perDay": self.get("perDay"),
            "perRound": self.get("perRound"),
            "autoComment": bool(self.get("autoComment")) and bool(str(self.get("mentionName") or "").strip()),
            "dmMode": self.get("dmMode"),
            "dmUid": self.get("dmUid"),
            "reportLevel": self.get("reportLevel"),
            "transcribe": bool(self.get("transcribe")),
            "keywords": self.get("keywords"),
            "enabledWindows": self.enabled_windows(),
            "weekendWindow": bool(self.get("weekendWindow")),
        }

    # ── 写（只在 init / 设置接口里用）────────────────────────────────────
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.data, ensure_ascii=False, indent=2) + "\n"
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)

    def update(self, patch: dict[str, Any]) -> None:
        """浅合并一层（嵌套 dict 走递归）。"""
        self.data = deep_merge(self.data, patch)
        self._validate()
        self.save()


def _zone(name: str):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(str(name or "Asia/Shanghai"))
    except (ZoneInfoNotFoundError, ValueError):
        # 有些精简镜像没带 tzdata，退回 UTC 也比崩掉强；日志里会提示
        from datetime import timezone

        return timezone.utc


def init_config(path: str | Path | None = None, force: bool = False) -> Path:
    """铺一份可编辑的 config.json（默认值），返回路径。"""
    cfg_path = Path(path or default_data_dir() / "config.json").expanduser().resolve()
    if cfg_path.exists() and not force:
        return cfg_path
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(DEFAULTS, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return cfg_path
