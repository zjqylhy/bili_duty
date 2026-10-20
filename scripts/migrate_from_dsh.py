#!/usr/bin/env python3
"""从本机 DSH 插件的家目录把台账搬到本服务的数据目录。

    python scripts/migrate_from_dsh.py                       # 自动找 ~/.dsh/bili-duty
    python scripts/migrate_from_dsh.py --src C:\\Users\\you\\.dsh\\bili-duty --dst ./data

搬的东西：config.json（顺手改字段）、bili_credential.json、seen.md、out/、digest/。
**不搬** duty.md（新服务不使用章程）、mcp_server.py、.venv*、日志。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

# Windows 控制台默认 GBK：中文能过，✓ 这类符号会炸 —— 统一成 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

#: 新服务认的字段（其余一律不进新配置，免得留下一堆看不懂的键）
ALLOWED = {
    "timezone", "keywords", "searchDays", "searchPerKeyword", "searchOrder",
    "candidatesPerRound", "perDay", "perRound", "minScore", "maxRoundsPerDay",
    "windows", "enabledWindows", "weekendWindow",
    "autoComment", "mentionName", "commentPerRound", "dmMode", "dmUid",
    "transcribe", "asrPython", "asrModel", "asrScript",
    "reportLevel", "reportKeepDays", "personaFile", "llm", "webhook", "server", "delays",
}
#: 原字段 → 新字段（改名的）
RENAME = {
    # 人设文件：原插件是"工作区目录"，新服务直接指向 AGENTS.md
    "workspace": "personaFile",
}


def find_default_src() -> Path:
    home = Path.home()
    candidates = [
        home / ".dsh" / "bili-duty",
        Path.home() / "Documents" / "bili-duty",
    ]
    for path in candidates:
        if (path / "config.json").is_file():
            return path
    return candidates[0]


def convert_config(raw: dict, src: Path) -> tuple[dict, list[str]]:
    notes: list[str] = []
    out: dict = {}
    dropped: list[str] = []
    for key, value in raw.items():
        if key == "workspace":
            continue  # 单独处理：那是个目录，不是人设文件
        target = RENAME.get(key, key)
        if target not in ALLOWED or key in ("noon", "night", "peakValley"):
            dropped.append(key)
            continue
        out[target] = value

    # 原插件的 noon / night 子开关 → 新服务的启用窗口
    if "peakValley" in raw or "noon" in raw or "night" in raw:
        if raw.get("peakValley") is False:
            out["enabledWindows"] = []
            notes.append("原 peakValley=false ⇒ enabledWindows 置空（只手动跑）")
        else:
            out["enabledWindows"] = [
                name for name in ("noon", "night") if raw.get(name, True)
            ]
            notes.append("窗口：" + ("、".join(out["enabledWindows"]) or "无"))

    if raw.get("replyToComments"):
        notes.append("原插件开着「回复评论（楼中楼）」—— v1 未实现，已忽略")

    # workspace 原来是个目录；把里面的 AGENTS.md **拷进数据目录**，
    # 配置里写相对名 —— 这样换到服务器上、目录不一样也能读到。
    if raw.get("workspace"):
        agents = Path(str(raw["workspace"])).expanduser() / "AGENTS.md"
        if agents.is_file():
            out["personaFile"] = "AGENTS.md"
            notes.append("人设：把 AGENTS.md 拷进了数据目录（personaFile = AGENTS.md）")
        else:
            out.pop("personaFile", None)
            notes.append("原「工作区」里没有 AGENTS.md ⇒ 按中性说话")
    if out.get("personaFile") == "":
        out.pop("personaFile", None)

    # 这些新字段留空就好（Config 会补默认值）
    for key in ("dmMode", "dmUid", "reportLevel", "transcribe", "asrModel"):
        if key in out and out[key] in ("", None):
            out.pop(key)
    if dropped:
        notes.append("已忽略原插件的字段：" + "、".join(dropped))
    return out, notes


def main() -> int:
    parser = argparse.ArgumentParser(description="从 DSH 插件迁移台账")
    parser.add_argument("--src", help="插件家目录（默认自动找 ~/.dsh/bili-duty）")
    parser.add_argument("--dst", default="data", help="数据目录（默认 ./data）")
    parser.add_argument("--force", action="store_true", help="目标已存在也覆盖")
    args = parser.parse_args()

    src = Path(args.src).expanduser().resolve() if args.src else find_default_src()
    dst = Path(args.dst).expanduser().resolve()
    if not (src / "config.json").is_file():
        print(f"✗ 在 {src} 找不到 config.json（用 --src 指一下插件家目录）", file=sys.stderr)
        return 1
    dst.mkdir(parents=True, exist_ok=True)

    raw = json.loads((src / "config.json").read_text(encoding="utf-8"))
    config, notes = convert_config(raw, src)
    target_config = dst / "config.json"
    if target_config.exists() and not args.force:
        print(f"· {target_config} 已存在，跳过（要覆盖加 --force）")
    else:
        target_config.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"✓ 配置 → {target_config}")

    for name in ("bili_credential.json", "seen.md"):
        source = src / name
        if source.is_file():
            shutil.copyfile(source, dst / name)
            print(f"✓ {name} → {dst / name}")

    if config.get("personaFile") == "AGENTS.md":
        agents = Path(str(raw.get("workspace"))).expanduser() / "AGENTS.md"
        if agents.is_file():
            shutil.copyfile(agents, dst / "AGENTS.md")
            print(f"✓ AGENTS.md（人设）→ {dst / 'AGENTS.md'}")

    for folder in ("out", "digest"):
        source = src / folder
        if source.is_dir():
            shutil.copytree(source, dst / folder, dirs_exist_ok=True)
            print(f"✓ {folder}/ → {dst / folder}/")

    for note in notes:
        print(f"· {note}")
    print()
    print("下一步：")
    print(f"  1) 在 {dst}/.env 里填 DEEPSEEK_API_KEY、BILIDUTY_TOKEN")
    print("  2) biliduty --data <数据目录> doctor")
    print("  3) biliduty --data <数据目录> run --dry-run   # 预览一轮（不发评论）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
