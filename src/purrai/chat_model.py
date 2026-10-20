"""Small HTTP client for vendor OpenAI-compatible chat APIs."""

import json
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


@dataclass(frozen=True)
class ChatReply:
    text: str | None = None
    sticker: str | None = None
    total_tokens: int | None = None
    should_reply: bool = True


class ChatModelError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class ChatModel:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.transport, self.client = transport, None

    async def reply(self, system, context):
        settings = self.settings
        url = settings.chat_api_base.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        # DeepSeek enables thinking by default; ordinary group chat should not pay for it.
        defaults = {}
        if urlsplit(url).hostname == "api.deepseek.com":
            defaults = {"thinking": {"type": "disabled"}, "response_format": {"type": "json_object"}}
        # Explicit vendor options override defaults. Model, messages and streaming stay fixed.
        body = {
            "max_tokens": settings.chat_max_tokens,
            **defaults,
            **settings.chat_api_extra,
            "model": settings.chat_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": context}],
            "stream": False,
        }
        if "max_completion_tokens" in body:
            body.pop("max_tokens", None)
            body["max_completion_tokens"] = settings.chat_max_tokens
        else:
            body["max_tokens"] = settings.chat_max_tokens
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=settings.chat_timeout, transport=self.transport)
        try:
            response = await self.client.post(
                url, headers={"Authorization": f"Bearer {settings.chat_api_key}"}, json=body
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ChatModelError("聊天 API 连接超时或暂时不可用", retryable=True) from exc
        if response.is_error:
            code = response.status_code
            reasons = {401: "API Key 无效", 403: "API 无权访问", 404: "API 地址或模型不存在", 429: "API 限流或额度不足"}
            raise ChatModelError(
                reasons.get(code, f"聊天 API 返回 HTTP {code}，请检查厂商配置"),
                retryable=code == 429 or code >= 500,
            )
        try:
            data = response.json()
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ChatModelError("模型输出被截断，可提高 CHAT_MAX_TOKENS 或关闭模型思考模式")
            content = choice["message"]["content"].strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            result = json.loads(content)
            if not isinstance(result, dict) or type(result.get("should_reply")) is not bool:
                raise ValueError
            usage = data.get("usage") or {}
            total_tokens = usage.get("total_tokens")
            if type(total_tokens) is not int or total_tokens < 0:
                total_tokens = None
            if not result["should_reply"]:
                return ChatReply(total_tokens=total_tokens, should_reply=False)
            reply = result.get("reply")
            if reply is not None and not isinstance(reply, str):
                raise ValueError
            sticker = result.get("sticker")
            if sticker is not None and (not isinstance(sticker, str) or not sticker.strip() or len(sticker) > 64):
                raise ValueError
            return ChatReply((reply.strip()[:800] or None) if reply else None,
                             sticker.strip() if sticker else None, total_tokens)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise ChatModelError("模型未返回约定的聊天结果，请使用支持指令和 JSON 输出的聊天模型") from exc

    async def close(self):
        if self.client:
            await self.client.aclose()
