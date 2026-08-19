import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .tools import ToolContext, ToolPermissionError, ToolRegistry


_DECORATIVE_SYMBOLS = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U00002300-\U000023FF"
    "\u200d\ufe0e\ufe0f"
    "]+"
)
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_IMAGE = re.compile(r"<\s*(?:img|svg)\b[^>]*>(?:.*?<\s*/\s*svg\s*>)?", re.IGNORECASE | re.DOTALL)


def sanitize_assistant_text(text: str) -> str:
    """Remove decorative marks and embedded image logos from chat text."""
    text = _MARKDOWN_IMAGE.sub("", text)
    text = _HTML_IMAGE.sub("", text)
    return _DECORATIVE_SYMBOLS.sub("", text)


class RunCancelledError(RuntimeError):
    pass


@dataclass
class HarnessEvent:
    event_type: str
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    text: str = ""
    outputs: Dict[str, Any] = field(default_factory=dict)


class AgentRunner:
    def __init__(
        self,
        client,
        model: str,
        tools: ToolRegistry,
        max_turns: int = 4,
        temperature: float = 0.5,
        should_cancel: Optional[Callable[[], bool]] = None,
    ):
        self.client = client
        self.model = model
        self.tools = tools
        self.max_turns = max_turns
        self.temperature = temperature
        self.should_cancel = should_cancel or (lambda: False)
        self.result = AgentResult()

    def _check_cancelled(self) -> None:
        if self.should_cancel():
            raise RunCancelledError("Run was cancelled")

    async def run(self, messages: List[Dict[str, Any]], context: ToolContext):
        for turn in range(1, self.max_turns + 1):
            self._check_cancelled()
            yield HarnessEvent("model_started", {"turn": turn})
            response_stream = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=self.tools.openai_definitions(context.allowed_tools),
                stream=True,
                temperature=self.temperature,
            )

            turn_content = ""
            reasoning_content = ""
            tool_calls: Dict[int, Dict[str, str]] = {}
            async for chunk in response_stream:
                self._check_cancelled()
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if getattr(delta, "content", None):
                    clean_delta = sanitize_assistant_text(delta.content)
                    turn_content += clean_delta
                    if clean_delta:
                        yield HarnessEvent("model_delta", {"turn": turn, "text": clean_delta})
                if getattr(delta, "reasoning_content", None):
                    reasoning_content += delta.reasoning_content
                if getattr(delta, "tool_calls", None):
                    for tool_call in delta.tool_calls:
                        entry = tool_calls.setdefault(
                            tool_call.index,
                            {"id": tool_call.id, "name": tool_call.function.name, "arguments": ""},
                        )
                        if tool_call.id:
                            entry["id"] = tool_call.id
                        if tool_call.function.name:
                            entry["name"] = tool_call.function.name
                        if tool_call.function.arguments:
                            entry["arguments"] += tool_call.function.arguments

            if not tool_calls:
                self.result.text = sanitize_assistant_text(turn_content or reasoning_content).strip()
                if not self.result.text and turn < self.max_turns:
                    yield HarnessEvent("model_empty", {"turn": turn})
                    messages.append({
                        "role": "user",
                        "content": "上一轮没有返回可用内容，请继续完成当前任务并给出明确结果。",
                    })
                    continue
                yield HarnessEvent(
                    "assistant_completed",
                    {"turn": turn, "content": self.result.text},
                )
                return

            calls = list(tool_calls.values())
            messages.append({
                "role": "assistant",
                "content": turn_content or None,
                "tool_calls": [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": call["arguments"],
                        },
                    }
                    for call in calls
                ],
            })

            for call in calls:
                self._check_cancelled()
                try:
                    arguments = json.loads(call["arguments"] or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("Tool arguments must be an object")
                except (json.JSONDecodeError, ValueError):
                    arguments = {}

                yield HarnessEvent(
                    "tool_started",
                    {"turn": turn, "tool_call_id": call["id"], "name": call["name"], "arguments": arguments},
                )
                try:
                    tool_result = await self.tools.execute(call["name"], arguments, context)
                except ToolPermissionError as exc:
                    yield HarnessEvent(
                        "tool_approval_required",
                        {"tool_call_id": call["id"], "name": exc.tool_name, "decision": exc.decision.value},
                    )
                    raise

                for key, value in tool_result.data.items():
                    if key == "artifacts":
                        self.result.outputs.setdefault("artifacts", []).extend(value)
                    else:
                        self.result.outputs[key] = value
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": tool_result.content,
                })
                yield HarnessEvent(
                    "tool_completed",
                    {
                        "turn": turn,
                        "tool_call_id": call["id"],
                        "name": call["name"],
                        "result": tool_result.content,
                    },
                )

        if not self.result.outputs:
            raise RuntimeError("Agent exceeded the maximum number of tool turns")
