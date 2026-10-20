import json
import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from protocol import ChatRequest, Message, ProxyError


class ReasoningOptions(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    effort: Literal["none", "instant", "low", "medium", "high"] | None = None
    summary: Literal["auto", "concise", "detailed"] | None = None


class ResponseFunctionTool(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    type: Literal["function"]
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    description: str = Field(default="", max_length=20000)
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})
    strict: bool | None = None


class ResponseToolChoiceName(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class ResponseNamedToolChoice(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    type: Literal["function"]
    name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    function: ResponseToolChoiceName | None = None


class ResponseRequest(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    model: str = Field(default="mercury-3", min_length=1, max_length=128)
    input: str | list[Any] = Field(default_factory=list)
    instructions: str | None = Field(default=None, max_length=200000)
    previous_response_id: str | None = None
    stream: bool = False
    store: bool = True
    reasoning: ReasoningOptions | None = None
    web_search: bool | None = None
    metadata: dict[str, str] = Field(default_factory=dict, max_length=16)
    tools: list[Any] | None = Field(default=None, max_length=128)
    tool_choice: str | dict[str, Any] | None = "auto"
    parallel_tool_calls: bool = True
    include: list[str] | None = Field(default=None, max_length=20)

    def chat(self, store):
        history = []
        if self.previous_response_id:
            previous, history = store.response(self.previous_response_id)
            if previous["status"] not in {"completed", "incomplete"}:
                raise ProxyError("前一个响应未完成，不能接续对话", 400, "invalid_previous_response")
        incoming = [{"role": "user", "content": self.input}] if isinstance(self.input, str) else self.input
        converted = []
        for item in incoming:
            if not isinstance(item, dict):
                continue
            kind = item.get("type", "message")
            if kind == "reasoning" and "encrypted_content" not in item:
                continue
            if kind == "function_call_output":
                output = item.get("output")
                if isinstance(output, list):
                    output = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in output)
                if not isinstance(output, str):
                    output = json.dumps(output, ensure_ascii=False)
                converted.append(Message(role="tool", tool_call_id=item.get("call_id"), content=output))
                continue
            if kind == "function_call":
                if item.get("call_id") and item.get("name"):
                    converted.append(Message(role="assistant", content=None, tool_calls=[{"id": item.get("call_id"), "type": "function", "function": {"name": item.get("name"), "arguments": item.get("arguments", "{}")}}]))
                continue
            if kind != "message":
                continue
            content = item.get("content")
            message = Message.model_validate({"role": item.get("role"), "content": content})
            if not message.text().strip() and message.role in {"user", "system", "developer"}:
                continue
            converted.append(message)
        history = history + [message.model_dump() for message in converted]
        messages = [Message.model_validate(item) for item in history]
        if self.instructions:
            messages.insert(0, Message(role="system", content=self.instructions))
        effort = self.reasoning.effort if self.reasoning and self.reasoning.effort else store.get("reasoning_effort")
        search = self.web_search if self.web_search is not None else store.get("web_search") == "true"
        chat = ChatRequest(model=self.model, messages=messages, stream=self.stream, reasoning_effort=effort, web_search=search, tools=self.tools, tool_choice=self.tool_choice, parallel_tool_calls=self.parallel_tool_calls)
        return chat, history


class ResponseBuilder:
    def __init__(self, request, generation):
        self.request = request
        self.run = generation
        self.id = "resp_" + uuid.uuid4().hex
        self.created = int(time.time())
        self.sequence = 0
        self.items = {}

    def item(self, kind, status="in_progress"):
        if kind == "text":
            text = self.run.final_content if self.run.final_content is not None else self.run.text
            return {"id": self.items[kind]["id"], "type": "message", "role": "assistant", "status": status, "content": [{"type": "output_text", "text": text if status != "in_progress" else "", "annotations": []}]}
        if kind.startswith("tool:"):
            call = self.run.tool_calls[int(kind.split(":", 1)[1])]
            return {"id": self.items[kind]["id"], "type": "function_call", "status": status, "arguments": call.arguments if status != "in_progress" else "", "call_id": call.id, "name": call.name}
        return {"id": self.items[kind]["id"], "type": "reasoning", "status": status, "summary": [{"type": "summary_text", "text": self.run.thinking if status != "in_progress" else ""}]}

    def full(self, status="completed", error=None):
        output = []
        auto_kinds = (["reasoning"] if self.run.thinking else []) + [f"tool:{index}" for index in range(len(self.run.tool_calls))] + (["text"] if self.run.final_content else [])
        kinds = list(self.items) if self.items else ([] if status in {"in_progress", "queued"} else auto_kinds)
        for kind in kinds:
            if kind not in self.items:
                prefix = "rs_" if kind == "reasoning" else "msg_" if kind == "text" else "fc_"
                self.items[kind] = {"id": prefix + uuid.uuid4().hex, "index": len(self.items)}
            output.append(self.item(kind, "completed" if status == "completed" else "in_progress" if status == "in_progress" else "incomplete"))
        reason = None
        if status == "completed" and self.run.finish_reason in {"length", "content_filter"}:
            status = "incomplete"
            reason = "max_output_tokens" if self.run.finish_reason == "length" else "content_filter"
        tools = [tool for tool in self.request.tools or [] if isinstance(tool, dict) and tool.get("type") in {None, "function"}]
        return {
            "id": self.id, "object": "response", "created_at": self.created, "status": status,
            "error": error, "incomplete_details": {"reason": reason} if reason else None,
            "model": self.request.model, "output": output, "instructions": self.request.instructions,
            "previous_response_id": self.request.previous_response_id, "store": self.request.store,
            "reasoning": {"effort": self.run.payload.reasoning_effort, "summary": self.request.reasoning.summary if self.request.reasoning else None},
            "text": {"format": {"type": "text"}}, "tools": tools, "tool_choice": self.request.tool_choice, "parallel_tool_calls": self.request.parallel_tool_calls,
            "metadata": self.request.metadata, "usage": self.run.response_usage() if status not in {"in_progress", "queued"} else None,
            "usage_estimated": True, "usage_method": "cl100k_base",
        }

    def event(self, name, **data):
        value = {"type": name, "sequence_number": self.sequence, **data}
        self.sequence += 1
        return "event: " + name + "\ndata: " + json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n\n"

    def start(self):
        body = self.full("in_progress")
        body["output"] = []
        return [self.event("response.created", response=body), self.event("response.in_progress", response=body)]

    def delta(self, event):
        if not event.delta:
            return []
        kind = "text" if "content" in event.delta else "reasoning"
        value = event.delta.get("content", event.delta.get("reasoning_content", ""))
        result = []
        if kind not in self.items:
            self.items[kind] = {"id": ("msg_" if kind == "text" else "rs_") + uuid.uuid4().hex, "index": len(self.items)}
            item = self.item(kind)
            item["content" if kind == "text" else "summary"] = []
            result.append(self.event("response.output_item.added", output_index=self.items[kind]["index"], item=item))
            if kind == "text":
                result.append(self.event("response.content_part.added", item_id=item["id"], output_index=self.items[kind]["index"], content_index=0, part={"type": "output_text", "text": "", "annotations": []}))
            else:
                result.append(self.event("response.reasoning_summary_part.added", item_id=item["id"], output_index=self.items[kind]["index"], summary_index=0, part={"type": "summary_text", "text": ""}))
        common = {"item_id": self.items[kind]["id"], "output_index": self.items[kind]["index"], "delta": value}
        if kind == "text":
            result.append(self.event("response.output_text.delta", content_index=0, logprobs=[], **common))
        else:
            result.append(self.event("response.reasoning_summary_text.delta", summary_index=0, **common))
        return result

    def finish(self):
        result = []
        for kind, info in self.items.items():
            item = self.item(kind, "completed")
            common = {"item_id": info["id"], "output_index": info["index"]}
            if kind == "text":
                result.append(self.event("response.output_text.done", content_index=0, text=self.run.final_content if self.run.final_content is not None else self.run.text, logprobs=[], **common))
                result.append(self.event("response.content_part.done", content_index=0, part=item["content"][0], **common))
            elif kind == "reasoning":
                result.append(self.event("response.reasoning_summary_text.done", summary_index=0, text=self.run.thinking, **common))
                result.append(self.event("response.reasoning_summary_part.done", summary_index=0, part=item["summary"][0], **common))
            else:
                call = self.run.tool_calls[int(kind.split(":", 1)[1])]
                result.append(self.event("response.function_call_arguments.done", arguments=call.arguments, **common))
            result.append(self.event("response.output_item.done", output_index=info["index"], item=item))
        return result

    def history(self, input_history):
        assistant = {"role": "assistant", "content": self.run.final_content}
        if self.run.tool_calls:
            assistant["tool_calls"] = [call.chat_dict() for call in self.run.tool_calls]
        return input_history + [assistant]

    def tool_events(self):
        result = []
        for index, call in enumerate(self.run.tool_calls):
            key = f"tool:{index}"
            self.items[key] = {"id": "fc_" + uuid.uuid4().hex, "index": len(self.items)}
            item = self.item(key)
            common = {"item_id": self.items[key]["id"], "output_index": self.items[key]["index"]}
            result.append(self.event("response.output_item.added", output_index=self.items[key]["index"], item=item))
            result.append(self.event("response.function_call_arguments.delta", delta=call.arguments, **common))
        return result
