"""Webhook 通知（可选）：飞书 / 钉钉 / Telegram / 任意 JSON 端点。

不配 ``webhook.url`` 就什么都不发 —— 报告本来就在 HTTP 页和文件里。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Config

log = logging.getLogger("biliduty.notify")


def _payload(kind: str, text: str) -> dict[str, Any]:
    kind = (kind or "json").lower()
    if kind == "feishu":  # 飞书自定义机器人
        return {"msg_type": "text", "content": {"text": text}}
    if kind == "dingtalk":  # 钉钉自定义机器人
        return {"msgtype": "text", "text": {"content": text}}
    if kind == "telegram":  # URL 里已经带 chat_id 的情形
        return {"text": text}
    return {"text": text}


async def send(cfg: Config, text: str) -> dict:
    url = str(cfg.get("webhook", "url") or "").strip()
    if not url:
        return {"skipped": True}
    kind = str(cfg.get("webhook", "type") or "json")
    body = _payload(kind, text)
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.post(url, json=body)
        ok = response.status_code < 400
        if not ok:
            log.warning("Webhook 返回 %s：%s", response.status_code, response.text[:200])
        return {"ok": ok, "status": response.status_code}
    except httpx.HTTPError as exc:
        log.warning("Webhook 发送失败：%s", exc)
        return {"ok": False, "error": str(exc)}


async def notify_round(cfg: Config, result) -> dict:
    """一轮结束后推一句摘要 + 前几条链接。dry-run 不推。"""
    if getattr(result, "dry_run", False):
        return {"skipped": True}
    if not str(cfg.get("webhook", "url") or "").strip():
        return {"skipped": True}
    from . import report

    text = report.webhook_text(result.day, result.picked, result.summary or "")
    return await send(cfg, text)


__all__ = ["send", "notify_round"]
