import json
import time
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProxyError(Exception):
    def __init__(self, message, status=502, code="upstream_error", param=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.param = param
        self.retry_after = retry_after

    def body(self):
        return {"error": {"message": str(self), "type": "invalid_request_error" if self.status == 400 else "api_error", "param": self.param, "code": self.code}}


class TextPart(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["text"]
    text: str = Field(max_length=200000)


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["system", "developer", "user", "assistant"]
    content: str | list[TextPart]

    def text(self):
        return self.content if isinstance(self.content, str) else "".join(part.text for part in self.content)


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    include_usage: bool = False


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: Literal["mercury-3", "mercury"] = "mercury-3"
    messages: list[Message] = Field(min_length=1, max_length=500)
    stream: bool = False
    stream_options: StreamOptions | None = None
    reasoning_effort: Literal["none", "instant", "low", "medium", "high"] = "medium"
    web_search: bool = False
    n: Literal[1] = 1

    @model_validator(mode="after")
    def validate_messages(self):
        if sum(len(message.text()) for message in self.messages) > 200000:
            raise ValueError("对话总长度不能超过 200000 个字符")
        if not any(message.role == "user" and message.text().strip() for message in self.messages):
            raise ValueError("至少需要一条非空的用户消息")
        if self.messages[-1].role != "user":
            raise ValueError("最后一条消息必须来自用户")
        return self

    def upstream(self, timezone):
        return {
            "id": uuid.uuid4().hex,
            "trigger": "submit-message",
            "messages": [
                {"id": uuid.uuid4().hex, "role": "system" if message.role == "developer" else message.role, "parts": [{"type": "text", "text": message.text()}]}
                for message in self.messages
            ],
            "reasoningEffort": "instant" if self.reasoning_effort == "none" else self.reasoning_effort,
            "webSearchEnabled": self.web_search,
            "voiceMode": False,
            "timezone": timezone,
            "language": "zh-CN",
        }


async def sse_data(chunks: AsyncIterator[str]):
    pending = ""
    data = []
    size = 0
    first = True
    async for chunk in chunks:
        if first and chunk:
            chunk = chunk.removeprefix("\ufeff")
            first = False
        pending += chunk
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            line = line.removesuffix("\r")
            if not line:
                if data:
                    yield "\n".join(data)
                data = []
                size = 0
            elif line.startswith("data:"):
                value = line[5:].removeprefix(" ")
                size += len(value)
                if size > 1048576:
                    raise ProxyError("网站返回的单个事件过大", code="invalid_upstream_stream")
                data.append(value)
        if len(pending) > 1048576:
            raise ProxyError("网站返回的单行数据过大", code="invalid_upstream_stream")
    if pending.startswith("data:"):
        data.append(pending[5:].removeprefix(" ").removesuffix("\r"))
    if data:
        yield "\n".join(data)


@dataclass
class Event:
    delta: dict | None = None
    finish_reason: str | None = None


async def translate(chunks):
    finished = False
    finish_reason = "stop"
    total = 0
    async for raw in sse_data(chunks):
        if raw == "[DONE]":
            finished = True
            break
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProxyError("网站返回了无法解析的流式数据", code="invalid_upstream_stream") from exc
        if not isinstance(item, dict):
            raise ProxyError("网站返回的事件格式不正确", code="invalid_upstream_stream")
        kind = item.get("type")
        if kind in {"text-delta", "reasoning-delta"}:
            value = item.get("delta")
            if not isinstance(value, str):
                raise ProxyError("网站返回的文本格式不正确", code="invalid_upstream_stream")
            total += len(value)
            if total > 2000000:
                raise ProxyError("网站回复超过了代理的长度限制", code="response_too_large")
            if value:
                yield Event(delta={"content" if kind == "text-delta" else "reasoning_content": value})
        elif kind == "error":
            message = item.get("errorText")
            raise ProxyError(f"网站生成失败：{message[:500]}" if isinstance(message, str) else "网站生成失败", code="upstream_generation_error")
        elif kind == "abort":
            raise ProxyError("网站中止了生成", code="upstream_aborted")
        elif kind == "finish":
            reason = item.get("finishReason", "stop")
            if reason == "error":
                raise ProxyError("网站未能完成生成", code="upstream_generation_error")
            if reason == "tool-calls":
                raise ProxyError("网站返回了当前代理不支持的工具调用", code="unsupported_upstream_event")
            finish_reason = {"content-filter": "content_filter", "length": "length"}.get(reason, "stop")
    if not finished:
        raise ProxyError("网站连接提前断开，回复可能不完整", code="upstream_stream_interrupted")
    if not total:
        raise ProxyError("网站没有返回文本内容，可能是网页协议已经变化", code="empty_upstream_response")
    yield Event(finish_reason=finish_reason)


class Completion:
    def __init__(self, model):
        self.id = "chatcmpl-" + uuid.uuid4().hex
        self.created = int(time.time())
        self.model = model

    def chunk(self, delta, finish_reason=None):
        return {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }

    def full(self, content, reasoning, finish_reason):
        message = {"role": "assistant", "content": content}
        if reasoning:
            message["reasoning_content"] = reasoning
        return {
            "id": self.id,
            "object": "chat.completion",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": None,
        }


def encode_sse(value):
    return "data: " + json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n\n"
