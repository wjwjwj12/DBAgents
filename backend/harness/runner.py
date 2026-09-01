import re
from dataclasses import dataclass, field
from typing import Any, Dict


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
