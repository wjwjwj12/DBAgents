import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from harness.runner import AgentResult, HarnessEvent, RunCancelledError, sanitize_assistant_text
from harness.tools import PermissionDecision, ToolContext, ToolPermissionError, ToolRegistry


class GraphState(TypedDict, total=False):
    messages: List[Dict[str, Any]]
    turn: int
    engine: str
    tool_calls: List[Dict[str, Any]]
    final_text: str
    outputs: Dict[str, Any]
    finished: bool
    planned: bool


@dataclass
class LangGraphRunner:
    client: Any
    model: str
    tools: ToolRegistry
    engine: str = "react"
    max_turns: int = 6
    temperature: float = 0.5
    max_concurrency: int = 4
    checkpointer: Any = None
    should_cancel: Optional[Callable[[], bool]] = None
    result: AgentResult = field(default_factory=AgentResult, init=False)

    def __post_init__(self):
        self.should_cancel = self.should_cancel or (lambda: False)
        graph = StateGraph(GraphState)
        graph.add_node("planner", self._planner_node)
        graph.add_node("model", self._model_node)
        graph.add_node("tools", self._tool_node)
        graph.add_conditional_edges(START, self._start_node, {"planner": "planner", "model": "model"})
        graph.add_edge("planner", "model")
        graph.add_conditional_edges("model", self._after_model, {"tools": "tools", "end": END})
        graph.add_conditional_edges("tools", self._after_tools, {"model": "model", "end": END})
        self.graph = graph.compile(checkpointer=self.checkpointer)

    def _check_cancelled(self):
        if self.should_cancel and self.should_cancel():
            raise RunCancelledError("Run was cancelled")

    @staticmethod
    def _start_node(state: GraphState):
        return "planner" if state.get("engine") in {"plan_execute", "static_plan", "dag"} and not state.get("planned") else "model"

    async def _planner_node(self, state: GraphState):
        self._check_cancelled()
        writer = get_stream_writer()
        engine = state.get("engine", "plan_execute")
        planning_rule = {
            "plan_execute": "计划允许根据工具失败或新观察进行有限重规划。",
            "static_plan": "计划必须一次确定，执行阶段不得根据观察扩展步骤。",
            "dag": "为每个步骤给出 depends_on；没有依赖的只读步骤应当并行。",
        }[engine]
        user_query = next(
            (str(message.get("content", "")) for message in reversed(state.get("messages", [])) if message.get("role") == "user"),
            "当前任务",
        )
        if engine == "dag":
            normalized = [
                {"title": "并行收集独立信息", "depends_on": []},
                {"title": "汇总并分析结果", "depends_on": [1]},
                {"title": "形成最终交付", "depends_on": [2]},
            ]
        elif engine == "static_plan":
            normalized = [
                {"title": "确认固定输入与字段", "depends_on": []},
                {"title": "按同一规则批量执行", "depends_on": [1]},
                {"title": "汇总异常项与结果", "depends_on": [2]},
            ]
        else:
            normalized = [
                {"title": "理解目标与约束", "depends_on": []},
                {"title": "调用所需工具完成任务", "depends_on": [1]},
                {"title": "检查结果并形成交付", "depends_on": [2]},
            ]
        title = user_query[:32] or "任务计划"
        if engine == "static_plan" and self._context.allowed_tools is not None:
            self._context.allowed_tools.discard("update_plan")
        if self._context.plan_update:
            self._context.plan_update(title, normalized)
        writer({"event_type": "plan_created", "payload": {"title": title, "steps": normalized, "engine": engine}})
        messages = list(state.get("messages", []))
        messages.append({
            "role": "system",
            "content": f"已批准的执行计划如下：{json.dumps(normalized, ensure_ascii=False)}。{planning_rule}",
        })
        return {"messages": messages, "planned": True}

    @staticmethod
    def _after_model(state: GraphState):
        return "end" if state.get("finished") else "tools"

    @staticmethod
    def _after_tools(state: GraphState):
        return "end" if state.get("finished") else "model"

    async def _model_node(self, state: GraphState):
        self._check_cancelled()
        writer = get_stream_writer()
        turn = state.get("turn", 0) + 1
        writer({"event_type": "model_started", "payload": {"turn": turn}})
        messages = list(state.get("messages", []))
        response_stream = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=self.tools.openai_definitions(self._context.allowed_tools),
            stream=True,
            temperature=self.temperature,
        )
        content = ""
        reasoning = ""
        calls: Dict[int, Dict[str, str]] = {}
        async for chunk in response_stream:
            self._check_cancelled()
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if getattr(delta, "content", None):
                clean = sanitize_assistant_text(delta.content)
                content += clean
                if clean:
                    writer({"event_type": "model_delta", "payload": {"turn": turn, "text": clean}})
            if getattr(delta, "reasoning_content", None):
                reasoning += delta.reasoning_content
            for tool_call in getattr(delta, "tool_calls", None) or []:
                item = calls.setdefault(tool_call.index, {"id": "", "name": "", "arguments": ""})
                if tool_call.id:
                    item["id"] = tool_call.id
                if tool_call.function.name:
                    item["name"] = tool_call.function.name
                if tool_call.function.arguments:
                    item["arguments"] += tool_call.function.arguments

        if not calls:
            final = sanitize_assistant_text(content or reasoning).strip()
            if not final and turn < self.max_turns:
                messages.append({"role": "user", "content": "上一轮没有返回可用内容，请继续完成当前任务并给出明确结果。"})
                writer({"event_type": "model_empty", "payload": {"turn": turn}})
                return {"messages": messages, "turn": turn, "tool_calls": [], "finished": False}
            writer({"event_type": "assistant_completed", "payload": {"turn": turn, "content": final}})
            return {"messages": messages, "turn": turn, "final_text": final, "finished": True}

        parsed = []
        assistant_calls = []
        for call in calls.values():
            try:
                arguments = json.loads(call["arguments"] or "{}")
                if not isinstance(arguments, dict):
                    arguments = {}
            except json.JSONDecodeError:
                arguments = {}
            parsed.append({"id": call["id"], "name": call["name"], "arguments": arguments})
            assistant_calls.append({
                "id": call["id"], "type": "function",
                "function": {"name": call["name"], "arguments": call["arguments"]},
            })
        messages.append({"role": "assistant", "content": content or None, "tool_calls": assistant_calls})
        return {"messages": messages, "turn": turn, "tool_calls": parsed, "finished": False}

    async def _execute_call(self, call: Dict[str, Any], context: ToolContext, turn: int):
        definition = self.tools.get(call["name"])
        if definition and definition.permission == PermissionDecision.ASK and call["name"] not in context.approved_tools:
            decision = interrupt({
                "tool_call_id": call["id"], "name": call["name"], "arguments": call["arguments"],
                "message": f"工具 {call['name']} 需要你的确认后才能执行。",
                "allowed_tools": sorted(context.allowed_tools) if context.allowed_tools is not None else None,
                "loaded_skills": sorted(context.loaded_skills),
            })
            if not isinstance(decision, dict) or decision.get("decision") != "approve":
                return call, None, "用户拒绝了该工具调用。"
            context.approved_tools.add(call["name"])
        try:
            attempts = max(1, definition.max_attempts if definition else 1)
            last_error = None
            for attempt in range(1, attempts + 1):
                try:
                    result = await asyncio.wait_for(
                        self.tools.execute(call["name"], call["arguments"], context),
                        timeout=definition.timeout_seconds if definition else 60.0,
                    )
                    return call, result, None
                except (TimeoutError, RuntimeError, ValueError) as exc:
                    last_error = exc
                    if attempt < attempts:
                        await asyncio.sleep(min(0.2 * attempt, 1.0))
            return call, None, f"工具执行失败，已重试 {attempts} 次：{last_error}"
        except ToolPermissionError as exc:
            return call, None, str(exc)

    async def _tool_node(self, state: GraphState):
        self._check_cancelled()
        writer = get_stream_writer()
        messages = list(state.get("messages", []))
        calls = state.get("tool_calls", [])
        turn = state.get("turn", 1)
        context: ToolContext = self._context
        for call in calls:
            writer({"event_type": "tool_started", "payload": {"turn": turn, "tool_call_id": call["id"], "name": call["name"], "arguments": call["arguments"]}})

        is_parallel = (
            state.get("engine") == "dag"
            and len(calls) > 1
            and all(
                self.tools.get(call["name"]) is not None
                and self.tools.get(call["name"]).parallel_safe
                for call in calls
            )
        )
        if is_parallel:
            semaphore = asyncio.Semaphore(self.max_concurrency)
            async def limited(call):
                async with semaphore:
                    return await self._execute_call(call, context, turn)
            completed = await asyncio.gather(*(limited(call) for call in calls))
        else:
            completed = []
            for call in calls:
                completed.append(await self._execute_call(call, context, turn))

        outputs = dict(state.get("outputs", {}))
        for call, result, error in completed:
            content = error or result.content
            if result:
                for key, value in result.data.items():
                    if key == "artifacts":
                        outputs.setdefault("artifacts", []).extend(value)
                    else:
                        outputs[key] = value
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})
            writer({"event_type": "tool_completed", "payload": {"turn": turn, "tool_call_id": call["id"], "name": call["name"], "result": content}})

        if turn >= self.max_turns:
            if outputs:
                return {"messages": messages, "outputs": outputs, "finished": True}
            raise RuntimeError("Agent exceeded the maximum number of tool turns")
        return {"messages": messages, "outputs": outputs, "tool_calls": [], "finished": False}

    async def run(
        self,
        messages: List[Dict[str, Any]] | None,
        context: ToolContext,
        *,
        thread_id: str | None = None,
        resume: Dict[str, Any] | None = None,
    ):
        self._context = context
        graph_input: GraphState | Command
        if resume is None:
            graph_input = {"messages": messages or [], "turn": 0, "engine": self.engine, "outputs": {}, "finished": False, "planned": False}
        else:
            graph_input = Command(resume=resume)
        config = {"configurable": {"thread_id": thread_id}} if thread_id else None
        async for mode, payload in self.graph.astream(graph_input, config=config, stream_mode=["custom", "values"]):
            if mode == "custom":
                yield HarnessEvent(payload["event_type"], payload.get("payload", {}))
            elif mode == "values":
                if payload.get("__interrupt__"):
                    request = payload["__interrupt__"][0].value
                    yield HarnessEvent("tool_approval_required", request)
                if payload.get("final_text"):
                    self.result.text = payload["final_text"]
                if payload.get("outputs"):
                    self.result.outputs = payload["outputs"]
