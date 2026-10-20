import json
import re
import uuid
from dataclasses import dataclass, field

from protocol import ChatRequest, ProxyError


@dataclass
class ToolInvocation:
    id: str
    name: str
    arguments: str
    arguments_object: dict = field(default_factory=dict)

    def chat_dict(self):
        return {"id": self.id, "type": "function", "function": {"name": self.name, "arguments": self.arguments}}


@dataclass
class ParsedOutput:
    content: str | None
    tool_calls: list[ToolInvocation]

    @property
    def finish_reason(self):
        return "tool_calls" if self.tool_calls else "stop"


def _json_value(value):
    cleaned = value.strip()
    cleaned = re.sub(r"^(?:<tool_calls>\s*)+", "", cleaned, flags=re.I)
    cleaned = re.sub(r"(?:\s*</tool_calls>)+$", "", cleaned, flags=re.I)
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        cleaned = cleaned[start:end + 1]
    return json.loads(cleaned)


def _payload(entries):
    if isinstance(entries, dict):
        if isinstance(entries.get("tool_calls"), list):
            entries = entries["tool_calls"]
        elif isinstance(entries.get("calls"), list):
            entries = entries["calls"]
        elif isinstance(entries.get("function_calls"), list):
            entries = entries["function_calls"]
        elif isinstance(entries.get("name"), str):
            entries = [entries]
        else:
            return []
    if not isinstance(entries, list):
        return []
    result = []
    for entry in entries[:128]:
        if not isinstance(entry, dict):
            continue
        function = entry.get("function") if isinstance(entry.get("function"), dict) else {}
        name = entry.get("name") or function.get("name")
        arguments = entry.get("arguments", function.get("arguments", {}))
        if arguments is None:
            arguments = {}
        if not isinstance(name, str) or not name:
            continue
        if isinstance(arguments, dict):
            result.append((name, arguments, json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))))
            continue
        if not isinstance(arguments, str):
            continue
        try:
            parsed = _json_value(arguments)
        except (json.JSONDecodeError, TypeError):
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        result.append((name, parsed, json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))))
    return result


def parse_tool_output(text, request: ChatRequest):
    if not request.active_tools:
        return ParsedOutput(text, [])
    names = {tool.function.name for tool in request.normalized_tools()}
    patterns = [
        r"<tool_calls>\s*(.*?)\s*</tool_calls>",
        r"<\|\s*tool_calls\s*\|>\s*(.*?)\s*<\|\s*/\s*tool_calls\s*\|>",
    ]
    entries = []
    matches = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I | re.S):
            matches.append(match)
            try:
                entries.extend(_payload(_json_value(match.group(1))))
            except (json.JSONDecodeError, TypeError):
                continue
    if not entries:
        fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.I | re.S)
        if fenced:
            try:
                entries = _payload(_json_value(fenced.group(1)))
            except (json.JSONDecodeError, TypeError):
                entries = []
    if not entries:
        stripped = text.strip()
        if stripped.startswith(("{", "[")):
            try:
                entries = _payload(_json_value(stripped))
            except (json.JSONDecodeError, TypeError):
                entries = []
    entries = [entry for entry in entries if entry[0] in names]
    if request.forced_tool_name:
        entries = [entry for entry in entries if entry[0] == request.forced_tool_name]
    if not request.parallel_tool_calls:
        entries = entries[:1]
    calls = [ToolInvocation("call_" + uuid.uuid4().hex, name, arguments_text, arguments) for name, arguments, arguments_text in entries]
    if request.tool_mode == "required" and not calls:
        raise ProxyError("模型没有按要求返回工具调用", code="tool_call_required_missing")
    if request.forced_tool_name and not calls:
        raise ProxyError(f"模型没有按要求调用函数 {request.forced_tool_name}", code="tool_call_required_missing")
    content = text
    for match in matches:
        content = content.replace(match.group(0), "")
    content = content.strip()
    return ParsedOutput(content or None, calls)
