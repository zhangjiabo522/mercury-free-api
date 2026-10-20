import json
import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from protocol import ChatRequest, Message, ProxyError


class ReasoningOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    effort: Literal["none", "instant", "low", "medium", "high"] | None = None
    summary: Literal["auto", "concise", "detailed"] | None = None


class ResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: Literal["mercury-3", "mercury"] = "mercury-3"
    input: str | list[dict] = Field(default_factory=list)
    instructions: str | None = Field(default=None, max_length=200000)
    previous_response_id: str | None = None
    stream: bool = False
    store: bool = True
    reasoning: ReasoningOptions | None = None
    web_search: bool | None = None
    metadata: dict[str, str] = Field(default_factory=dict, max_length=16)

    def chat(self, store):
        history = []
        if self.previous_response_id:
            previous, history = store.response(self.previous_response_id)
            if previous["status"] not in {"completed", "incomplete"}:
                raise ProxyError("前一个响应未完成，不能接续对话", 400, "invalid_previous_response")
        incoming = [{"role": "user", "content": self.input}] if isinstance(self.input, str) else self.input
        converted = []
        for item in incoming:
            kind = item.get("type", "message")
            if kind == "reasoning" and "encrypted_content" not in item:
                continue
            if kind != "message":
                raise ProxyError("Responses 输入目前只支持文本消息", 400, "unsupported_input")
            content = item.get("content")
            if isinstance(content, list):
                values = []
                for part in content:
                    if not isinstance(part, dict) or part.get("type") not in {"input_text", "output_text", "text"} or not isinstance(part.get("text"), str):
                        raise ProxyError("Responses 输入目前只支持文本内容", 400, "unsupported_input")
                    values.append(part["text"])
                content = "".join(values)
            converted.append(Message.model_validate({"role": item.get("role"), "content": content}))
        history = history + [message.model_dump() for message in converted]
        messages = [Message.model_validate(item) for item in history]
        if self.instructions:
            messages.insert(0, Message(role="system", content=self.instructions))
        effort = self.reasoning.effort if self.reasoning and self.reasoning.effort else store.get("reasoning_effort")
        search = self.web_search if self.web_search is not None else store.get("web_search") == "true"
        chat = ChatRequest(model=self.model, messages=messages, stream=self.stream, reasoning_effort=effort, web_search=search)
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
            return {"id": self.items[kind]["id"], "type": "message", "role": "assistant", "status": status, "content": [{"type": "output_text", "text": self.run.text if status != "in_progress" else "", "annotations": []}]}
        return {"id": self.items[kind]["id"], "type": "reasoning", "status": status, "summary": [{"type": "summary_text", "text": self.run.thinking if status != "in_progress" else ""}]}

    def full(self, status="completed", error=None):
        output = []
        kinds = list(self.items) if self.items else ([] if status in {"in_progress", "queued"} else (["reasoning"] if self.run.thinking else []) + (["text"] if self.run.text else []))
        for kind in kinds:
            if kind not in self.items:
                self.items[kind] = {"id": ("rs_" if kind == "reasoning" else "msg_") + uuid.uuid4().hex, "index": len(self.items)}
            output.append(self.item(kind, "completed" if status == "completed" else "in_progress" if status == "in_progress" else "incomplete"))
        reason = None
        if status == "completed" and self.run.finish_reason in {"length", "content_filter"}:
            status = "incomplete"
            reason = "max_output_tokens" if self.run.finish_reason == "length" else "content_filter"
        return {
            "id": self.id, "object": "response", "created_at": self.created, "status": status,
            "error": error, "incomplete_details": {"reason": reason} if reason else None,
            "model": self.request.model, "output": output, "instructions": self.request.instructions,
            "previous_response_id": self.request.previous_response_id, "store": self.request.store,
            "reasoning": {"effort": self.run.payload.reasoning_effort, "summary": self.request.reasoning.summary if self.request.reasoning else None},
            "text": {"format": {"type": "text"}}, "tools": [], "tool_choice": "auto", "parallel_tool_calls": False,
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
                result.append(self.event("response.output_text.done", content_index=0, text=self.run.text, logprobs=[], **common))
                result.append(self.event("response.content_part.done", content_index=0, part=item["content"][0], **common))
            else:
                result.append(self.event("response.reasoning_summary_text.done", summary_index=0, text=self.run.thinking, **common))
                result.append(self.event("response.reasoning_summary_part.done", summary_index=0, part=item["summary"][0], **common))
            result.append(self.event("response.output_item.done", output_index=info["index"], item=item))
        return result

    def history(self, input_history):
        return input_history + [{"role": "assistant", "content": self.run.text}]
