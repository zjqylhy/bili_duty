"""一次性演示：真实搜索 + 真实字幕，"判断"这一步用规则代替模型。

不联网写任何东西：dry-run（不发评论、不动判重表 / state / digest）。
报告落在 demo/out/preview-*.md，看完可以整个 demo/ 目录删掉。

    python demo/run_demo.py

想让它变成"真的"：在 data/.env 里填 DEEPSEEK_API_KEY，然后
    biliduty --data ./data run --dry-run
"""

from __future__ import annotations

import asyncio
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from biliduty.bilibili import Bili, load_credential  # noqa: E402
from biliduty.config import DEFAULTS, Config, deep_merge  # noqa: E402
from biliduty.llm import Usage  # noqa: E402
from biliduty.pipeline import run_round  # noqa: E402

DEMO = ROOT / "demo"
BAD_TITLE = re.compile("预告|录屏|合集|切片|直播|广告|搬运|教程")


class DemoLLM:
    """演示用的"假模型"：把规则的结论伪装成模型返回的 JSON。"""

    usage = Usage()

    async def chat_json(self, messages, **kwargs):
        text = messages[-1]["content"]
        blocks = re.split(r"^\[\d+\] ", text, flags=re.M)[1:]
        items = []
        for block in blocks:
            bvid = block.split()[0].strip()
            title = block.split("标题：", 1)[-1].split("\n", 1)[0]
            body = block.split("字幕原文", 1)[-1]
            parts = body.split('"""')
            body = parts[1] if len(parts) > 1 else body
            match = re.search(r"播放 (\d+)", block)
            play = int(match.group(1)) if match else 0
            worth = len(body) >= 80 and not BAD_TITLE.search(title)
            first = re.sub(r"\s+", " ", body).strip()[:70]
            items.append({
                "bvid": bvid,
                "worth": worth,
                "score": 78 if worth else 30,
                "why": ("字幕开头说：" + first) if worth else "按演示规则判为不值得",
                "one_line": title[:28],
                "comment": "",
            })
        print(f"[演示规则] 判了 {len(items)} 条")
        return {"items": items}


def main() -> int:
    credential = ROOT / "data" / "bili_credential.json"
    if not credential.is_file():
        print("✗ 没有 data/bili_credential.json —— 先跑 biliduty login", file=sys.stderr)
        return 1
    DEMO.mkdir(exist_ok=True)
    (DEMO / "bili_credential.json").write_bytes(credential.read_bytes())

    cfg = Config(deep_merge(DEFAULTS, {
        "keywords": ["重返未来：1999"],
        "searchPerKeyword": 6,
        "candidatesPerRound": 4,
        "perDay": 5,
        "perRound": 2,
        "reportLevel": "full",
        "autoComment": False,
    }), DEMO / "config.json", DEMO)

    bili = Bili(load_credential(DEMO), {"search": 0.4, "subtitle": 0.6})
    result = asyncio.run(run_round(cfg, why="demo", dry_run=True, bili=bili, llm=DemoLLM()))

    if result.report_file:
        path = pathlib.Path(result.report_file)
        note = (
            "> ⚠️ 演示轮：搜索与字幕是真实的，**「判断」那一列是规则模拟的**（本机还没有模型 key）。\n"
            "> 填好 DEEPSEEK_API_KEY 后，同一份材料会交给 deepseek-chat 判断。\n\n"
        )
        path.write_text(note + path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"\n报告：{path}")
    print(result.summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
