"""模型层：OpenAI 兼容的 ``/chat/completions``。

默认指向 DeepSeek 官方（``https://api.deepseek.com`` + ``deepseek-chat``）。
换成中转站 / 本地 vLLM 只要改 ``config.json`` 里的 ``llm.baseUrl`` 和 ``llm.model``。

这里只做一件事：**把一段材料换成一份结构化判断**（JSON）。写作、排版、发评论的
文字都由流水线按模板生成 —— 不让模型自由发挥，成本和结果都更可控。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Config

log = logging.getLogger("biliduty.llm")

_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """模型调用失败（重试完还是不行）。"""


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.calls += other.calls

    def as_dict(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
        }


def extract_json(text: str) -> dict:
    """从模型输出里抠出第一个 JSON 对象（容忍 ```json 围栏和前后废话）。"""
    raw = (text or "").strip()
    if not raw:
        raise LLMError("模型返回了空内容")
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    start = raw.find("{")
    if start < 0:
        raise LLMError(f"模型输出里没有 JSON：{raw[:200]!r}")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(raw)):
        char = raw[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(raw[start:index + 1])
                except ValueError as exc:
                    raise LLMError(f"JSON 解析失败：{exc}") from exc
                if not isinstance(parsed, dict):
                    raise LLMError("模型输出的 JSON 顶层不是对象")
                return parsed
    raise LLMError(f"JSON 不完整：{raw[:200]!r}")


class LLMClient:
    def __init__(self, cfg: Config, api_key: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = str(cfg.get("llm", "baseUrl") or "https://api.deepseek.com").rstrip("/")
        self.model = str(cfg.get("llm", "model") or "deepseek-chat")
        self.api_key = (api_key if api_key is not None else cfg.api_key()).strip()
        self.temperature = float(cfg.get("llm", "temperature", default=0.3))
        self.max_tokens = int(cfg.get("llm", "maxTokens", default=4096))
        self.timeout = float(cfg.get("llm", "timeout", default=180))
        self.max_retries = int(cfg.get("llm", "maxRetries", default=3))
        self.usage = Usage()
        self._transport = transport

    @property
    def url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return self.base_url + "/chat/completions"

    def _require_key(self) -> None:
        if not self.api_key:
            raise LLMError(
                "没有 DEEPSEEK_API_KEY：在 .env 或环境变量里填上（https://platform.deepseek.com 申请）"
            )

    async def chat(
        self,
        messages: list[dict],
        *,
        json_mode: bool = True,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        self._require_key()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else float(temperature),
            "max_tokens": int(max_tokens or self.max_tokens),
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(max(1, self.max_retries)):
            try:
                async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
                    response = await client.post(self.url, headers=headers, json=payload)
                if response.status_code in _RETRY_STATUS:
                    raise LLMError(f"HTTP {response.status_code}: {response.text[:300]}")
                if response.status_code >= 400:
                    raise LLMError(f"HTTP {response.status_code}: {response.text[:300]}")
                data = response.json()
                usage = (data.get("usage") or {})
                self.usage.add(Usage(
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    calls=1,
                ))
                choices = data.get("choices") or []
                if not choices:
                    raise LLMError(f"响应里没有 choices：{str(data)[:300]}")
                content = (choices[0].get("message") or {}).get("content") or ""
                if not str(content).strip():
                    raise LLMError("模型返回空内容")
                return str(content)
            except (httpx.TransportError, httpx.TimeoutException, LLMError) as exc:
                last_error = exc
                if attempt + 1 >= max(1, self.max_retries):
                    break
                wait = min(30, 2 ** attempt * 3)
                log.warning("模型调用失败（第 %d 次）：%s —— %.0f 秒后重试", attempt + 1, exc, wait)
                import asyncio

                await asyncio.sleep(wait)
        raise LLMError(f"模型调用失败：{last_error}")

    async def chat_json(self, messages: list[dict], **kwargs: Any) -> dict:
        text = await self.chat(messages, json_mode=True, **kwargs)
        return extract_json(text)


# TODO(perf): 批量判断时若单条字幕特别长，可按 prompt_tokens 预算自动降档截断（当前在 prompts 里固定截断）
__all__ = ["LLMClient", "LLMError", "Usage", "extract_json"]
