from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Optional, Set


class PermissionDecision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ToolPermissionError(RuntimeError):
    def __init__(self, tool_name: str, decision: PermissionDecision):
        self.tool_name = tool_name
        self.decision = decision
        super().__init__(f"Tool '{tool_name}' is not approved ({decision.value})")


@dataclass
class ToolContext:
    run_id: str = ""
    conversation_id: str = ""
    thread_id: str = ""
    tenant_id: str = "local"
    user_id: str = "local-user"
    allowed_tools: Optional[Set[str]] = None
    approved_tools: Set[str] = field(default_factory=set)
    loaded_skills: Set[str] = field(default_factory=set)
    tool_audit: Optional[Callable[[str, Dict[str, Any]], None]] = None
    plan_update: Optional[Callable[[str, list[str]], None]] = None
    workspace_files: Dict[str, bytes] = field(default_factory=dict)
    sandbox_file_paths: Dict[str, str] = field(default_factory=dict)


@dataclass
class ToolResult:
    content: str
    data: Dict[str, Any] = field(default_factory=dict)


ToolHandler = Callable[[Dict[str, Any], ToolContext], Awaitable[ToolResult]]


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: ToolHandler
    permission: PermissionDecision = PermissionDecision.ALLOW
    parallel_safe: bool = False
    timeout_seconds: float = 60.0
    max_attempts: int = 2

    def to_openai_definition(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._tools:
            raise ValueError(f"Tool '{definition.name}' is already registered")
        self._tools[definition.name] = definition

    def get(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def names(self) -> Set[str]:
        return set(self._tools)

    def requires_checkpoint(self) -> bool:
        return any(tool.permission == PermissionDecision.ASK for tool in self._tools.values())

    def openai_definitions(self, allowed_tools: Optional[Set[str]] = None):
        return [
            tool.to_openai_definition()
            for tool in self._tools.values()
            if allowed_tools is None or tool.name in allowed_tools
        ]

    async def execute(self, name: str, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        definition = self.get(name)
        if definition is None:
            return ToolResult(content=f"Unsupported tool: {name}")
        if context.allowed_tools is not None and name not in context.allowed_tools:
            raise ToolPermissionError(name, PermissionDecision.DENY)
        if definition.permission == PermissionDecision.DENY:
            raise ToolPermissionError(name, PermissionDecision.DENY)
        if definition.permission == PermissionDecision.ASK and name not in context.approved_tools:
            raise ToolPermissionError(name, PermissionDecision.ASK)
        if context.tool_audit:
            context.tool_audit("tool_authorized", {"name": name, "permission": definition.permission.value})
        result = await definition.handler(arguments, context)
        if context.tool_audit:
            context.tool_audit("tool_result", {"name": name, "has_artifacts": bool(result.data.get("artifacts"))})
        return result
