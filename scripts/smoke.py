#!/usr/bin/env python3
"""只读冒烟：确认 B站 凭证 / 搜索 / 字幕都通。**不发任何东西。**

    python scripts/smoke.py --data ./data --keyword "重返未来：1999"

部署完成后先跑这个 —— 它不会发评论、不会动台账，只打印结果。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Windows 控制台默认 GBK：✓ 会炸 —— 统一成 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from biliduty.bilibili import Bili, BiliError, load_credential  # noqa: E402
from biliduty.config import Config, ConfigError  # noqa: E402


async def run(data_dir: Path, keyword: str, num: int) -> int:
    credential = load_credential(data_dir)
    if not credential:
        print("✗ 没有 B站 凭证：先跑 `biliduty login`，或设好 BILI_SESSDATA 等环境变量", file=sys.stderr)
        return 1

    bili = Bili(credential, {"search": 0.2, "subtitle": 0.2})
    try:
        me = await bili.identity()
    except Exception as exc:
        print(f"✗ 凭证不可用：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"✓ 已登录：{me['name']}（uid {me['uid']}）")

    try:
        videos = await bili.search(keyword, num=num, order="pubdate")
    except BiliError as exc:
        print(f"✗ 搜索失败：{exc}", file=sys.stderr)
        return 1
    print(f"✓ 搜索「{keyword}」拿到 {len(videos)} 条")
    for video in videos[:3]:
        print(f"   · {video['bvid']} {video['title'][:40]} · 播放 {video['play']}")

    if not videos:
        return 0
    with_text = 0
    for video in videos[:num]:
        result = await bili.subtitle(video["bvid"])
        if result.get("text"):
            with_text += 1
            print(f"✓ 字幕可用：{video['bvid']}（{len(result['text'])} 字）")
        else:
            print(f"· 无字幕：{video['bvid']}（{result.get('empty')}）")
    print(f"\n小结：{len(videos)} 条里 {with_text} 条有字幕。都没问题就能起服务了。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="只读冒烟测试")
    parser.add_argument("--data", default="data", help="数据目录（默认 ./data）")
    parser.add_argument("--keyword", default="DeepSeek Harness")
    parser.add_argument("--num", type=int, default=3)
    args = parser.parse_args()

    data_dir = Path(args.data).expanduser().resolve()
    try:
        Config.load(data_dir / "config.json")
    except ConfigError as exc:
        print(f"✗ 配置有问题：{exc}", file=sys.stderr)
        return 2
    return asyncio.run(run(data_dir, args.keyword, args.num))


if __name__ == "__main__":
    raise SystemExit(main())
