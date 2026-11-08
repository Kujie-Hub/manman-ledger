"""DeepSeek OpenAI 兼容接口客户端。

官方依据：
- https://api-docs.deepseek.com/quick_start/pricing
- https://api-docs.deepseek.com/guides/vision
- https://api-docs.deepseek.com/guides/json_mode
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import httpx

from backend.config import DeepSeekConfig, load_deepseek_config


class DeepSeekError(RuntimeError):
    pass


class DeepSeekNotConfigured(DeepSeekError):
    pass


def _parse_json_content(content: str) -> dict:
    cleaned = content.strip()
    if not cleaned:
        raise DeepSeekError("DeepSeek 返回了空内容")
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                value = json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                raise DeepSeekError("DeepSeek 返回内容不是有效 JSON") from exc
        else:
            raise DeepSeekError("DeepSeek 返回内容不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise DeepSeekError("DeepSeek 返回 JSON 顶层必须是对象")
    return value


class DeepSeekClient:
    def __init__(self, config: DeepSeekConfig | None = None) -> None:
        self.config = config or load_deepseek_config()

    @property
    def configured(self) -> bool:
        return bool(self.config.api_key)

    async def _chat(self, messages: list[dict], max_tokens: int = 4096) -> dict:
        if not self.configured:
            raise DeepSeekNotConfigured(
                "未配置 DeepSeek API Key，请在设置中填写"
            )
        payload = {
            "model": self.config.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                response = await client.post(
                    f"{self.config.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            raise DeepSeekError(f"DeepSeek 请求失败（HTTP {exc.response.status_code}）：{detail}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise DeepSeekError(f"DeepSeek 请求失败：{exc}") from exc

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DeepSeekError("DeepSeek 响应缺少 choices[0].message.content") from exc
        return _parse_json_content(str(content))

    async def classify(self, prompt: str) -> dict:
        return await self._chat(
            [
                {"role": "system", "content": "你是严格的账单分类器，只输出有效 JSON。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=3072,
        )

    async def extract_screenshot(self, image_bytes: bytes, media_type: str, prompt: str) -> dict:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return await self._chat(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media_type};base64,{encoded}",
                                "detail": "original",
                            },
                        },
                    ],
                }
            ],
            max_tokens=4096,
        )

