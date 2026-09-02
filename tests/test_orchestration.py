import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from harness.tools import ToolContext, ToolDefinition, ToolRegistry, ToolResult
from harness.tools import PermissionDecision
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from orchestration.router import select_engine
from orchestration.runner import DeepAgentRunner


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
        runner = DeepAgentRunner(model=client, tools=registry, engine="dag")
        context = ToolContext(conversation_id="dag-thread", run_id="dag-run")
        events = [event async for event in runner.run([{"role": "user", "content": "并行读取"}], context)]
        self.assertEqual(maximum_active, 2)
        self.assertEqual(runner.result.text, "done")
        self.assertEqual(sum(event.event_type == "tool_completed" for event in events), 2)
        self.assertTrue(any(
            event.event_type == "model_completed" and not event.payload["has_tool_calls"]
            for event in events
        ))


class RuntimeCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def test_runtime_contract_explains_skill_execution_boundary(self):
        contract = DeepAgentRunner._runtime_contract(
            ToolContext(),
            ["create_document"],
            has_execute=True,
        )

        self.assertIn("使用沙箱终端执行已加载 Skill 的脚本", contract)
        self.assertIn("不得因为业务工具列表中没有与工作流同名的工具", contract)

    async def test_plain_answer_does_not_create_opensandbox(self):
        create = AsyncMock(side_effect=[stream_with_text("普通回答")])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        sandbox_create = AsyncMock()
        runner = DeepAgentRunner(model=client, tools=ToolRegistry())
        context = ToolContext(conversation_id="plain-conversation", run_id="plain-run")

        with (
            patch.dict(os.environ, {"SANDBOX_PROVIDER": "opensandbox"}),
            patch("sandbox.factory._get_opensandbox_backend", sandbox_create),
        ):
            events = [event async for event in runner.run(
                [{"role": "user", "content": "你好"}],
                context,
                thread_id="plain-run",
            )]

        self.assertEqual(runner.result.text, "普通回答")
        self.assertTrue(any(event.event_type == "model_completed" for event in events))
        sandbox_create.assert_not_awaited()

    async def test_sandbox_outputs_are_returned_as_binary_artifacts(self):
        payload = b"PK\x03\x04pptx-data"

        class FakeSandbox:
            async def aglob(self, pattern, path):
                matches = []
                if pattern == "*.pptx":
                    matches = [{"path": "/outputs/deck.pptx", "size": len(payload), "modified_at": "now"}]
                return SimpleNamespace(error=None, matches=matches)

            async def adownload_files(self, paths):
                return [SimpleNamespace(path=paths[0], content=payload, error=None)]

        runner = DeepAgentRunner(model=SimpleNamespace(), tools=ToolRegistry())
        runner.sandbox_backend = FakeSandbox()

        await runner._collect_sandbox_artifacts()

        artifact = runner.result.outputs["artifacts"][0]
        self.assertEqual(artifact["artifact_type"], "ppt")
        self.assertEqual(artifact["extension"], "pptx")
        self.assertEqual(artifact["content_bytes"], payload)

    async def test_skill_files_are_synced_into_execution_sandbox(self):
        uploaded = []

        class FakeSandbox:
            def __init__(self, sandbox_id):
                self.id = sandbox_id

            async def aupload_files(self, files):
                uploaded.extend(files)
                return [SimpleNamespace(error=None) for _item in files]

            async def aglob(self, _pattern, _path):
                return SimpleNamespace(error=None, matches=[])

        with tempfile.TemporaryDirectory() as temp_dir:
            skill = Path(temp_dir) / "example" / "scripts" / "build.py"
            skill.parent.mkdir(parents=True)
            skill.write_text("print('ok')", encoding="utf-8")
            (Path(temp_dir) / "example" / "SKILL.md").write_text("# Example", encoding="utf-8")
            runner = DeepAgentRunner(model=SimpleNamespace(), tools=ToolRegistry(), skills_root=Path(temp_dir))
            context = ToolContext(conversation_id="sandbox-thread", run_id="sandbox-run")
            with (
                patch("orchestration.runner.get_thread_backend", AsyncMock(return_value=FakeSandbox(temp_dir))),
                patch("orchestration.runner.supports_execution", return_value=True),
            ):
                await runner._backend("sandbox-thread", context)
                self.assertEqual(uploaded, [])
                await runner._sync_requested_skills([{
                    "name": "read_file",
                    "args": {"file_path": "/skills/example/SKILL.md"},
                }])

        self.assertIn("/skills/example/scripts/build.py", [path for path, _content in uploaded])
        self.assertIn("/skills/example/SKILL.md", [path for path, _content in uploaded])

    async def test_attachment_is_available_in_thread_virtual_filesystem(self):
        read_call = SimpleNamespace(
            index=0,
            id="read-attachment",
            function=SimpleNamespace(
                name="read_file",
                arguments='{"file_path":"/attachments/source.extracted.md"}',
            ),
        )
        create = AsyncMock(side_effect=[stream_with_calls(read_call), stream_with_text("done")])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        runner = DeepAgentRunner(model=client, tools=ToolRegistry(), max_turns=3)
        context = ToolContext(
            conversation_id="attachment-thread",
            run_id="attachment-run",
            workspace_files={"/attachments/source.extracted.md": "完整附件内容".encode()},
        )

        [event async for event in runner.run([{"role": "user", "content": "读取附件"}], context)]

        self.assertEqual(runner.result.text, "done")
        second_request = create.await_args_list[1].kwargs["messages"]
        tool_message = next(message for message in second_request if message["role"] == "tool")
        self.assertIn("完整附件内容", tool_message["content"])
        system_text = "\n".join(
            str(message.get("content", "")) for message in create.await_args_list[0].kwargs["messages"]
            if message.get("role") == "system"
        )
        self.assertIn("当前未启用 execute", system_text)
        self.assertIn("/attachments/source.extracted.md", system_text)

    def test_web_limits_are_configurable(self):
        with patch.dict(os.environ, {
            "AGENT_MODEL_CALL_LIMIT": "24",
            "AGENT_TOOL_CALL_LIMIT": "96",
        }):
            runner = DeepAgentRunner(model=SimpleNamespace(), tools=ToolRegistry())
        self.assertEqual(runner.max_turns, 24)
        self.assertEqual(runner.max_tool_calls, 96)

    async def test_model_limit_ends_without_raising(self):
        async def noop(_arguments, _context):
            return ToolResult(content="ok")

        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="noop",
            description="noop",
            parameters={"type": "object", "properties": {}},
            handler=noop,
        ))
        call = SimpleNamespace(index=0, id="noop-1", function=SimpleNamespace(name="noop", arguments="{}"))
        create = AsyncMock(side_effect=[stream_with_calls(call)])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        runner = DeepAgentRunner(model=client, tools=registry, max_turns=1)

        [event async for event in runner.run(
            [{"role": "user", "content": "执行"}],
            ToolContext(conversation_id="limit-thread", run_id="limit-run"),
        )]

        self.assertIn("Model call limits exceeded", runner.result.text)

    async def test_model_retry_failure_is_not_treated_as_a_normal_answer(self):
        create = AsyncMock(return_value=stream_with_text(
            "Model call failed after 3 attempts with OpenAIAPIError: Error code: 502"
        ))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        runner = DeepAgentRunner(model=client, tools=ToolRegistry())

        with self.assertRaisesRegex(RuntimeError, "Error code: 502"):
            [event async for event in runner.run(
                [{"role": "user", "content": "生成文件"}],
                ToolContext(conversation_id="failure-thread", run_id="failure-run"),
            )]


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
        runner = DeepAgentRunner(model=client, tools=registry, checkpointer=MemorySaver())
        context = ToolContext(conversation_id="approval-thread", run_id="approval-run")
        first = [event async for event in runner.run(
            [{"role": "user", "content": "执行写入"}],
            context,
            thread_id="approval-run",
        )]
        self.assertTrue(any(event.event_type == "tool_approval_required" for event in first))
        self.assertEqual(executed, [])
        second = [event async for event in runner.run(
            None,
            context,
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
                runner = DeepAgentRunner(model=client, tools=registry, checkpointer=saver)
                context = ToolContext(conversation_id="sqlite-thread", run_id="sqlite-run")
                first = [event async for event in runner.run(
                    [{"role": "user", "content": "执行写入"}], context, thread_id="sqlite-run",
                )]
                self.assertTrue(any(event.event_type == "tool_approval_required" for event in first))
                [event async for event in runner.run(
                    None, context, thread_id="sqlite-run", resume={"decision": "approve"},
                )]
                self.assertEqual(runner.result.text, "sqlite completed")


if __name__ == "__main__":
    unittest.main()
