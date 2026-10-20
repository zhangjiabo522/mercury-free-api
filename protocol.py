import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Literal

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
    model_config = ConfigDict(extra="allow", strict=True)
    type: Literal["text"]
    text: str = Field(max_length=200000)


class FunctionDefinition(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    description: str = Field(default="", max_length=20000)
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    strict: bool | None = None


class FunctionTool(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    type: Literal["function"]
    function: FunctionDefinition


class ToolChoiceName(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class NamedToolChoice(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    type: Literal["function"]
    function: ToolChoiceName


class MessageToolFunction(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    name: str = Field(max_length=64)
    arguments: str = Field(max_length=1000000)


class MessageToolCall(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    id: str = Field(min_length=1, max_length=256)
    type: Literal["function"] = "function"
    function: MessageToolFunction


class Message(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[TextPart] | None = None
    name: str | None = Field(default=None, max_length=64)
    tool_call_id: str | None = Field(default=None, max_length=256)
    tool_calls: list[MessageToolCall] | None = Field(default=None, max_length=128)

    def text(self):
        if self.content is None:
            return ""
        return self.content if isinstance(self.content, str) else "".join(part.text for part in self.content)

    def history_text(self):
        value = self.text()
        if self.tool_calls:
            calls = json.dumps([call.model_dump() for call in self.tool_calls], ensure_ascii=False)
            value = (value + "\n" if value else "") + "[已调用工具] " + calls
        return value


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    include_usage: bool = False


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    model: Literal["mercury-3", "mercury"] = "mercury-3"
    messages: list[Message] = Field(min_length=1, max_length=500)
    stream: bool = False
    stream_options: StreamOptions | None = None
    reasoning_effort: Literal["none", "instant", "low", "medium", "high"] = "medium"
    web_search: bool = False
    n: Literal[1] = 1
    tools: list[FunctionTool] | None = Field(default=None, max_length=128)
    tool_choice: Literal["none", "auto", "required"] | NamedToolChoice = "auto"
    parallel_tool_calls: bool = True

    @model_validator(mode="after")
    def validate_messages(self):
        for message in self.messages:
            if message.role == "tool" and (not message.tool_call_id or message.content is None):
                raise ValueError("工具结果消息必须包含 tool_call_id 和 content")
            if message.role != "assistant" and message.role != "tool" and message.content is None:
                raise ValueError("该角色的消息必须包含 content")
            if message.role == "assistant" and not message.text() and not message.tool_calls:
                raise ValueError("助手消息必须包含文字或工具调用")
            if message.tool_calls and message.role != "assistant":
                raise ValueError("只有助手消息可以包含 tool_calls")
        if sum(len(message.history_text()) for message in self.messages) > 200000:
            raise ValueError("对话总长度不能超过 200000 个字符")
        if not any(message.role == "user" and message.text().strip() for message in self.messages):
            raise ValueError("至少需要一条非空的用户消息")
        if self.messages[-1].role not in {"user", "tool"}:
            raise ValueError("最后一条消息必须来自用户或工具结果")
        if self.tools is not None:
            names = [tool.function.name for tool in self.tools]
            if len(names) != len(set(names)):
                raise ValueError("工具名称不能重复")
        return self

    @property
    def active_tools(self):
        return bool(self.tools) and self.tool_choice != "none"

    def tool_prompt(self):
        if not self.active_tools:
            return None
        definitions = [tool.function.model_dump(exclude_none=True) for tool in self.tools]
        first = definitions[0]
        properties = first.get("parameters", {}).get("properties", {})
        example_arguments = {name: "..." for name in properties}
        example = json.dumps({"tool_calls": [{"name": first["name"], "arguments": example_arguments}]}, ensure_ascii=False, separators=(",", ":"))
        if isinstance(self.tool_choice, NamedToolChoice):
            choice = f"本次必须调用函数 {self.tool_choice.function.name}。"
        elif self.tool_choice == "required":
            choice = "本次必须至少调用一个函数。"
        else:
            choice = "只在确实需要外部数据或操作时调用函数。"
        parallel = "可以一次调用多个函数。" if self.parallel_tool_calls else "每次最多调用一个函数。"
        return (
            "你可以调用以下外部函数。\n"
            "可用函数定义：\n"
            + json.dumps(definitions, ensure_ascii=False, separators=(",", ":"))
            + "\n调用规则：\n"
            + choice
            + parallel
            + "\n用户要求查询、获取、读取、计算或执行操作时，必须调用函数。即使你知道答案，也不能绕过函数直接回答。"
            + "\n需要调用函数时，只输出一个 <tool_calls>...</tool_calls> 块，块内必须是合法 JSON，格式为 "
            + example
            + "。"
            + "\narguments 必须是 JSON 对象。不要在工具调用块外解释，不要把工具调用写成普通文字。"
            + "\n不需要调用函数时正常回答。后续消息中出现的工具结果必须作为事实使用。"
        )

    def upstream_messages(self):
        messages = []
        prompt = self.tool_prompt()
        if prompt:
            messages.append({"id": uuid.uuid4().hex, "role": "system", "parts": [{"type": "text", "text": prompt}]})
        for message in self.messages:
            role = "system" if message.role == "developer" else message.role
            text = message.history_text()
            if message.role == "tool":
                role = "user"
                text = f"[工具结果 tool_call_id={message.tool_call_id}]\n{text}"
            messages.append({"id": uuid.uuid4().hex, "role": role, "parts": [{"type": "text", "text": text}]})
        return messages

    def upstream(self, timezone):
        return {
            "id": uuid.uuid4().hex,
            "trigger": "submit-message",
            "messages": self.upstream_messages(),
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

    def full(self, content, reasoning, finish_reason, tool_calls=None):
        message = {"role": "assistant", "content": content}
        if reasoning:
            message["reasoning_content"] = reasoning
        if tool_calls:
            message["tool_calls"] = tool_calls
        if tool_calls and not content:
            message["content"] = None
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
