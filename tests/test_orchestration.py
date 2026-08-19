import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from harness.tools import ToolContext, ToolDefinition, ToolRegistry, ToolResult
from harness.tools import PermissionDecision
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from orchestration.router import select_engine
from orchestration.runner import LangGraphRunner


def stream_with_calls(*calls):
    async def generator():
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content=None,
            reasoning_content=None,
            tool_calls=list(calls),
        ))])
    return generator()


def stream_with_text(text):
    async def generator():
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content=text,
            reasoning_content=None,
            tool_calls=None,
        ))])
    return generator()


class RouterTests(unittest.TestCase):
    def test_default_routing_rules_are_explainable(self):
        self.assertEqual(select_engine("解释一下这个概念").engine, "react")
        self.assertEqual(select_engine("生成一份项目汇报PPT，然后检查内容").engine, "plan_execute")
        self.assertEqual(select_engine("将这50份发票按固定字段批量提取").engine, "static_plan")
        decision = select_engine("分别查询苹果、微软、谷歌财报并对比")
        self.assertEqual(decision.engine, "dag")
        self.assertTrue(decision.reasons)

    def test_short_artifact_followup_uses_planned_execution(self):
        decision = select_engine("还是很大", has_artifact_context=True)
        self.assertEqual(decision.engine, "plan_execute")
        self.assertIn("历史产物", decision.reasons[0])


class DagExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_safe_calls_execute_concurrently(self):
        active = 0
        maximum_active = 0
        async def slow(arguments, _context):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.08)
            active -= 1
            return ToolResult(content=arguments["value"])

        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="read",
            description="read",
            parameters={"type": "object", "properties": {"value": {"type": "string"}}},
            handler=slow,
            parallel_safe=True,
        ))
        calls = [SimpleNamespace(
            index=index,
            id=f"call-{index}",
            function=SimpleNamespace(name="read", arguments=f'{{"value":"{index}"}}'),
        ) for index in range(2)]
        create = AsyncMock(side_effect=[stream_with_calls(*calls), stream_with_text("done")])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        runner = LangGraphRunner(client=client, model="test", tools=registry, engine="dag")
        events = [event async for event in runner.run([{"role": "user", "content": "并行读取"}], ToolContext())]
        self.assertEqual(maximum_active, 2)
        self.assertEqual(runner.result.text, "done")
        self.assertEqual(sum(event.event_type == "tool_completed" for event in events), 2)


class ApprovalResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_ask_tool_pauses_and_resumes_from_checkpoint(self):
        executed = []
        async def write(arguments, _context):
            executed.append(arguments["value"])
            return ToolResult(content="written")

        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="write",
            description="write",
            parameters={"type": "object", "properties": {"value": {"type": "string"}}},
            handler=write,
            permission=PermissionDecision.ASK,
        ))
        call = SimpleNamespace(
            index=0,
            id="write-1",
            function=SimpleNamespace(name="write", arguments='{"value":"ok"}'),
        )
        create = AsyncMock(side_effect=[stream_with_calls(call), stream_with_text("completed")])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        runner = LangGraphRunner(client=client, model="test", tools=registry, checkpointer=MemorySaver())
        first = [event async for event in runner.run(
            [{"role": "user", "content": "执行写入"}],
            ToolContext(),
            thread_id="approval-run",
        )]
        self.assertTrue(any(event.event_type == "tool_approval_required" for event in first))
        self.assertEqual(executed, [])
        second = [event async for event in runner.run(
            None,
            ToolContext(),
            thread_id="approval-run",
            resume={"decision": "approve"},
        )]
        self.assertEqual(executed, ["ok"])
        self.assertEqual(runner.result.text, "completed")
        self.assertTrue(any(event.event_type == "tool_completed" for event in second))

    async def test_sqlite_checkpoint_can_persist_and_resume(self):
        async def write(_arguments, _context):
            return ToolResult(content="written")

        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="write",
            description="write",
            parameters={"type": "object", "properties": {}},
            handler=write,
            permission=PermissionDecision.ASK,
        ))
        call = SimpleNamespace(index=0, id="write-sqlite", function=SimpleNamespace(name="write", arguments="{}"))
        create = AsyncMock(side_effect=[stream_with_calls(call), stream_with_text("sqlite completed")])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with tempfile.TemporaryDirectory() as temp_dir:
            async with AsyncSqliteSaver.from_conn_string(str(Path(temp_dir) / "checkpoint.db")) as saver:
                await saver.setup()
                runner = LangGraphRunner(client=client, model="test", tools=registry, checkpointer=saver)
                first = [event async for event in runner.run(
                    [{"role": "user", "content": "执行写入"}], ToolContext(), thread_id="sqlite-run",
                )]
                self.assertTrue(any(event.event_type == "tool_approval_required" for event in first))
                [event async for event in runner.run(
                    None, ToolContext(), thread_id="sqlite-run", resume={"decision": "approve"},
                )]
                self.assertEqual(runner.result.text, "sqlite completed")


if __name__ == "__main__":
    unittest.main()
