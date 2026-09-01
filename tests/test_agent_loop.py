import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import agent
from database import Base
from harness.tools import ToolContext
from models import ArtifactModel, AttachmentModel, ConversationModel, MessageModel, RunEventModel, RunModel, UserModel
from orchestration.checkpoint import close_checkpointer


def make_stream(*deltas):
    async def generator():
        for delta in deltas:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=delta)])
    return generator()


def message_text(content):
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content if isinstance(block, dict))


class AgentToolLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await close_checkpointer()

    def test_retry_history_stops_before_original_prompt_across_retry_chain(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        db = session_factory()
        conversation_id = "00000000-0000-0000-0000-000000000133"
        user = UserModel(username="local-user")
        db.add(user)
        db.flush()
        db.add(ConversationModel(id=conversation_id, user_id=user.id, title="branch"))
        prior_run = RunModel(conversation_id=conversation_id, status="completed")
        source_run = RunModel(conversation_id=conversation_id, status="completed")
        retry_run = RunModel(conversation_id=conversation_id, status="failed")
        db.add_all([prior_run, source_run, retry_run])
        db.flush()
        started = datetime(2026, 9, 1, 8, 0, 0)
        db.add_all([
            MessageModel(conversation_id=conversation_id, run_id=prior_run.id, role="user", content="前置问题", created_at=started),
            MessageModel(conversation_id=conversation_id, run_id=prior_run.id, role="assistant", content="前置回答", created_at=started + timedelta(seconds=1)),
            MessageModel(conversation_id=conversation_id, run_id=source_run.id, role="user", content="制作PPT", created_at=started + timedelta(seconds=2)),
            MessageModel(conversation_id=conversation_id, run_id=source_run.id, role="assistant", content="错误新闻简报", created_at=started + timedelta(seconds=3)),
            RunEventModel(run_id=retry_run.id, sequence=1, event_type="run_started", payload={"retry_of_run_id": source_run.id}),
        ])
        db.commit()

        history = agent._conversation_history(db, conversation_id, retry_run.id)

        self.assertEqual(history, [
            {"role": "user", "content": "前置问题"},
            {"role": "assistant", "content": "前置回答"},
        ])
        db.close()

    async def test_history_keeps_artifact_metadata_without_html_source(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        conversation_id = "00000000-0000-0000-0000-000000000128"
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "deck.html"
            source_path.write_text("<!DOCTYPE html><style>.h-hero{font-size:10vw}</style>", encoding="utf-8")
            db = session_factory()
            user = UserModel(username="local-user")
            db.add(user)
            db.flush()
            db.add(ConversationModel(id=conversation_id, user_id=user.id, title="PPT修改"))
            run = RunModel(conversation_id=conversation_id, status="completed")
            db.add(run)
            db.flush()
            db.add(MessageModel(conversation_id=conversation_id, run_id=run.id, role="assistant", content="PPT已生成。"))
            artifact = ArtifactModel(run_id=run.id, title="建设方案", artifact_type="ppt", mime_type="text/html", storage_path=str(source_path))
            db.add(artifact)
            db.commit()

            bad_run = RunModel(conversation_id=conversation_id, status="completed")
            db.add(bad_run)
            db.flush()
            db.add(MessageModel(
                conversation_id=conversation_id,
                run_id=bad_run.id,
                role="assistant",
                content="PPT已修改。\n\n[历史产物: 建设方案]\n<!DOCTYPE html><style>.h-hero{font-size:10vw}</style>",
            ))
            db.commit()

            history = agent._conversation_history(db, conversation_id)
            content = next(item["content"] for item in history if f"产物ID: {artifact.id}" in item["content"])
            self.assertIn(f"产物ID: {artifact.id}", content)
            self.assertTrue(any(item["content"] == "上一轮任务未生成有效产物。" for item in history))
            self.assertFalse(any("<!DOCTYPE html>" in item["content"] for item in history))
            self.assertFalse(any("font-size:10vw" in item["content"] for item in history))
            db.close()

    async def test_edit_ppt_scales_latest_artifact_titles(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        conversation_id = "00000000-0000-0000-0000-000000000129"
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "deck.html"
            source_path.write_text("<html><head></head><body><h1 class=\"h-hero\">标题</h1></body></html>", encoding="utf-8")
            db = session_factory()
            user = UserModel(username="local-user")
            db.add(user)
            db.flush()
            db.add(ConversationModel(id=conversation_id, user_id=user.id, title="PPT修改"))
            run = RunModel(conversation_id=conversation_id, status="completed")
            db.add(run)
            db.flush()
            db.add(ArtifactModel(run_id=run.id, title="建设方案", artifact_type="ppt", mime_type="text/html", storage_path=str(source_path)))
            db.commit()
            db.close()

            with patch.object(agent, "SessionLocal", session_factory):
                result = await agent._execute_edit_ppt({"scale": 0.8}, ToolContext(conversation_id=conversation_id))

            html = result.data["artifacts"][0]["content"]
            self.assertIn('id="ppt-title-scale"', html)
            self.assertIn(".h-hero{font-size:8vw!important}", html)

    def test_delivery_claim_requires_real_artifact(self):
        with self.assertRaises(RuntimeError):
            agent._validate_final_delivery("还是很大", "PPT已修改完成，可以下载。", [], expects_artifact=True)
        agent._validate_final_delivery("润色这句话", "文本已调整如下。", [], expects_artifact=False)

    def test_future_delivery_plan_is_not_mistaken_for_completed_artifact(self):
        agent._validate_final_delivery(
            "做个ppt",
            "请先确认用途。确认后我会生成可预览、可下载的 PPTX 文件。",
            [],
            expects_artifact=False,
        )

    def test_artifact_request_detection_excludes_how_to_questions(self):
        self.assertTrue(agent._expects_artifact_delivery("帮我做个ppt", False))
        self.assertTrue(agent._expects_artifact_delivery("生成一份项目报告", False))
        self.assertFalse(agent._expects_artifact_delivery("如何生成PPT", False))

    async def test_missing_artifact_is_silently_recovered_to_file(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        load_call = SimpleNamespace(
            index=0,
            id="load-document",
            function=SimpleNamespace(name="load_skill", arguments='{"skill_name":"document"}'),
        )
        create_call = SimpleNamespace(
            index=0,
            id="create-document",
            function=SimpleNamespace(
                name="create_document",
                arguments=json.dumps({
                    "title": "项目报告",
                    "markdown": "# 项目报告\n恢复生成的正文。",
                    "artifact_type": "report",
                }, ensure_ascii=False),
            ),
        )
        create = AsyncMock(side_effect=[
            make_stream(SimpleNamespace(content=None, reasoning_content="我会先加载文档技能。", tool_calls=[load_call])),
            make_stream(SimpleNamespace(content="报告已经生成，可在下方下载。", reasoning_content=None, tool_calls=None)),
            make_stream(SimpleNamespace(content=None, reasoning_content=None, tool_calls=[create_call])),
            make_stream(SimpleNamespace(content="恢复完成。", reasoning_content=None, tool_calls=None)),
        ])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        with tempfile.TemporaryDirectory() as temp_dir:
            def fake_export(_content, output_path, title=""):
                Path(output_path).write_bytes(b"docx")
                return output_path

            with (
                patch.object(agent, "SessionLocal", session_factory),
                patch.object(agent, "STORAGE_DIR", temp_dir),
                patch.object(agent, "_get_llm_client", return_value=client),
                patch.object(agent, "export_markdown_to_docx", side_effect=fake_export),
            ):
                events = [event async for event in agent.run_agent(
                    query="生成一份项目报告",
                    conversation_id="00000000-0000-0000-0000-000000000132",
                )]

        payloads = [json.loads(event.removeprefix("data: ")) for event in events]
        self.assertTrue(any(payload.get("type") == "content_delta" for payload in payloads))
        self.assertTrue(any(payload.get("type") == "content_reset" for payload in payloads))
        self.assertFalse(any(payload.get("artifact_type") == "text" for payload in payloads))
        self.assertTrue(any(payload.get("artifact_type") == "report" for payload in payloads))
        self.assertTrue(any(
            payload.get("type") == "thought_delta" and payload.get("text") == "我会先加载文档技能。"
            for payload in payloads
        ))
        self.assertTrue({"run_started", "task_group", "thought_delta", "content"}.issubset(
            {payload.get("type") for payload in payloads}
        ))
        db = session_factory()
        run = db.query(RunModel).one()
        self.assertEqual(run.status, "completed")
        event_types = [event.event_type for event in db.query(RunEventModel).order_by(RunEventModel.sequence)]
        self.assertIn("artifact_recovery_started", event_types)
        self.assertIn("artifact_recovery_completed", event_types)
        self.assertEqual(db.query(ArtifactModel).count(), 1)
        db.close()

    async def test_run_failure_is_logged_and_returns_traceable_run_id(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        with (
            patch.object(agent, "SessionLocal", session_factory),
            patch.object(agent, "_get_llm_client", side_effect=RuntimeError("provider unavailable")),
            self.assertLogs("agent", level="ERROR") as captured,
        ):
            events = [event async for event in agent.run_agent(
                query="分析当前问题",
                conversation_id="00000000-0000-0000-0000-000000000131",
            )]

        payload = json.loads(events[-1].removeprefix("data: "))
        self.assertEqual(payload["type"], "error")
        self.assertTrue(payload["run_id"])
        self.assertIn(payload["run_id"], payload["msg"])
        self.assertIn("provider unavailable", "\n".join(captured.output))

        db = session_factory()
        run = db.query(RunModel).one()
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_message, "provider unavailable")
        db.close()

    async def test_short_followup_edits_latest_ppt_and_streams_final_answer(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        conversation_id = "00000000-0000-0000-0000-000000000130"
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "deck.html"
            source_path.write_text("<html><head></head><body><h1 class=\"h-hero\">标题</h1></body></html>", encoding="utf-8")
            db = session_factory()
            user = UserModel(username="local-user")
            db.add(user)
            db.flush()
            db.add(ConversationModel(id=conversation_id, user_id=user.id, title="PPT修改"))
            old_run = RunModel(conversation_id=conversation_id, status="completed")
            db.add(old_run)
            db.flush()
            db.add(MessageModel(conversation_id=conversation_id, run_id=old_run.id, role="assistant", content="PPT已生成。"))
            db.add(ArtifactModel(run_id=old_run.id, title="建设方案", artifact_type="ppt", mime_type="text/html", storage_path=str(source_path)))
            db.commit()
            db.close()

            edit_call = SimpleNamespace(
                index=0,
                id="edit-call",
                function=SimpleNamespace(name="edit_ppt", arguments='{"scale":0.8}'),
            )
            create = AsyncMock(side_effect=[
                make_stream(SimpleNamespace(content=None, reasoning_content=None, tool_calls=[edit_call])),
                make_stream(SimpleNamespace(content="PPT标题已缩小并生成新版本。", reasoning_content=None, tool_calls=None)),
            ])
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            with (
                patch.object(agent, "SessionLocal", session_factory),
                patch.object(agent, "STORAGE_DIR", temp_dir),
                patch.object(agent, "_get_llm_client", return_value=client),
            ):
                events = [event async for event in agent.run_agent(query="还是很大", conversation_id=conversation_id)]

            payloads = [json.loads(event.removeprefix("data: ")) for event in events]
            self.assertEqual(payloads[0]["engine"], "plan_execute")
            self.assertTrue(any(payload.get("type") == "content_delta" for payload in payloads))
            db = session_factory()
            artifacts = db.query(ArtifactModel).order_by(ArtifactModel.created_at).all()
            self.assertEqual(len(artifacts), 2)
            self.assertIn('id="ppt-title-scale"', Path(artifacts[-1].storage_path).read_text(encoding="utf-8"))
            db.close()

    async def test_attachment_text_is_loaded_by_attachment_id(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        conversation_id = "00000000-0000-0000-0000-000000000127"
        db = session_factory()
        user = UserModel(username="local-user")
        db.add(user)
        db.flush()
        db.add(ConversationModel(id=conversation_id, user_id=user.id, title="附件问答"))
        attachment = AttachmentModel(
            conversation_id=conversation_id,
            file_name="交通简报.txt",
            mime_type="text/plain",
            size_bytes=24,
            storage_path="unused.txt",
            extracted_text="项目通车日期为 2026 年 9 月 30 日。",
        )
        db.add(attachment)
        db.commit()
        attachment_id = attachment.id
        db.close()

        create = AsyncMock(return_value=make_stream(SimpleNamespace(
            content="已读取附件。",
            reasoning_content=None,
            tool_calls=None,
        )))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        with (
            patch.object(agent, "SessionLocal", session_factory),
            patch.object(agent, "_get_llm_client", return_value=client),
        ):
            [event async for event in agent.run_agent(
                query="项目什么时候通车？",
                context_text="",
                conversation_id=conversation_id,
                attachment_name="交通简报.txt",
                attachment_id=attachment_id,
            )]

        messages = create.await_args.kwargs["messages"]
        self.assertIn("2026 年 9 月 30 日", message_text(messages[0]["content"]))

    async def test_multiple_attachment_texts_are_loaded_by_attachment_ids(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        conversation_id = "00000000-0000-0000-0000-000000000129"
        db = session_factory()
        user = UserModel(username="local-user")
        db.add(user)
        db.flush()
        db.add(ConversationModel(id=conversation_id, user_id=user.id, title="多附件问答"))
        attachments = [
            AttachmentModel(
                conversation_id=conversation_id,
                file_name="工期.txt",
                mime_type="text/plain",
                size_bytes=12,
                storage_path="unused-1.txt",
                extracted_text="计划工期为 18 个月。",
            ),
            AttachmentModel(
                conversation_id=conversation_id,
                file_name="预算.txt",
                mime_type="text/plain",
                size_bytes=12,
                storage_path="unused-2.txt",
                extracted_text="项目预算为 3.2 亿元。",
            ),
        ]
        db.add_all(attachments)
        db.commit()
        attachment_ids = [attachment.id for attachment in attachments]
        db.close()

        create = AsyncMock(return_value=make_stream(SimpleNamespace(
            content="已读取多个附件。",
            reasoning_content=None,
            tool_calls=None,
        )))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        with (
            patch.object(agent, "SessionLocal", session_factory),
            patch.object(agent, "_get_llm_client", return_value=client),
        ):
            [event async for event in agent.run_agent(
                query="汇总工期和预算",
                conversation_id=conversation_id,
                attachment_name="工期.txt、预算.txt",
                attachment_ids=attachment_ids,
            )]

        messages = create.await_args.kwargs["messages"]
        self.assertIn("计划工期为 18 个月", message_text(messages[0]["content"]))
        self.assertIn("项目预算为 3.2 亿元", message_text(messages[0]["content"]))

    async def test_model_can_create_a_dynamic_plan_without_fixed_route_steps(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        tool_call = SimpleNamespace(
            index=0,
            id="plan-call",
            function=SimpleNamespace(
                name="update_plan",
                arguments='{"title":"按需分析","steps":["理解目标","形成结论"]}',
            ),
        )
        create = AsyncMock(side_effect=[
            make_stream(SimpleNamespace(content="我会先整理执行步骤。", tool_calls=[tool_call])),
            make_stream(SimpleNamespace(content="动态任务已完成。", tool_calls=None)),
        ])
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        with (
            patch.object(agent, "SessionLocal", session_factory),
            patch.object(agent, "_get_llm_client", return_value=client),
        ):
            events = [event async for event in agent.run_agent(
                query="分析当前问题",
                conversation_id="00000000-0000-0000-0000-000000000125",
            )]

        payloads = [json.loads(event.removeprefix("data: ")) for event in events]
        self.assertEqual(payloads[0]["type"], "run_started")
        self.assertTrue(payloads[0]["run_id"])
        self.assertTrue(any(payload.get("title") == "按需分析" for payload in payloads))
        self.assertTrue(any(payload.get("text") == "理解目标" for payload in payloads))
        self.assertTrue(any(payload.get("title") == "第 1 轮思考" for payload in payloads))
        self.assertTrue(any(payload.get("title") == "第 2 轮思考" for payload in payloads))
        self.assertTrue(any(
            payload.get("type") == "turn_commit" and payload.get("text") == "我会先整理执行步骤。"
            for payload in payloads
        ))
        self.assertFalse(any(
            payload.get("type") == "thought" and "正在分析当前上下文" in payload.get("text", "")
            for payload in payloads
        ))
        self.assertTrue(any(
            payload.get("type") == "content_delta" and payload.get("text") == "动态任务已完成。"
            for payload in payloads
        ))
        self.assertFalse(any(payload.get("type") == "content_reset" for payload in payloads))
        self.assertTrue(any(
            payload.get("type") == "content" and payload.get("text") == "动态任务已完成。"
            for payload in payloads
        ))
        self.assertFalse(any("任务路由" in payload.get("title", "") for payload in payloads))

    def test_output_router_moves_process_preamble_out_of_streamed_answer(self):
        router = agent._OutputDisplayRouter(stream_answer=True)
        payloads = []
        for chunk in [
            "我来分析这段代码，找出潜在原因。\n\n",
            "让我先梳理一下代码逻辑，然后给出分析。\n\n",
            "## 原因分析\n\n服务退出是因为信号处理存在问题。",
        ]:
            payloads.extend(router.feed(chunk))
        payloads.extend(router.finish())

        self.assertEqual(
            [payload["text"] for payload in payloads if payload["type"] == "thought"],
            ["我来分析这段代码，找出潜在原因。", "让我先梳理一下代码逻辑，然后给出分析。"],
        )
        self.assertEqual(
            "".join(payload["text"] for payload in payloads if payload["type"] == "content_delta"),
            "## 原因分析\n\n服务退出是因为信号处理存在问题。",
        )

    def test_output_router_streams_direct_answer_without_reset(self):
        router = agent._OutputDisplayRouter(stream_answer=True)
        payloads = router.feed("结") + router.feed("论如下。") + router.finish()

        self.assertEqual("".join(item["text"] for item in payloads if item["type"] == "content_delta"), "结论如下。")
        self.assertEqual([item["type"] for item in payloads[:2]], ["turn_delta", "turn_delta"])
        self.assertEqual(payloads[2]["type"], "turn_clear")

    def test_output_router_never_streams_tool_turn_as_final_answer(self):
        router = agent._OutputDisplayRouter(stream_answer=True)

        provisional = router.feed("先检查文件内容，再决定使用哪个工具。")
        boundary = router.tool_boundary()

        self.assertEqual(provisional, [{"type": "turn_delta", "text": "先检查文件内容，再决定使用哪个工具。"}])
        self.assertEqual(boundary, [{"type": "turn_commit", "text": "先检查文件内容，再决定使用哪个工具。"}])

    def test_generic_confirmation_tool_is_a_real_interrupt_boundary(self):
        definition = agent.tool_registry.get("request_user_confirmation")

        self.assertIsNotNone(definition)
        self.assertEqual(definition.permission.value, "ask")
        self.assertIn("request_user_confirmation", agent._base_system_prompt(""))

    def test_confirmation_request_does_not_trigger_missing_artifact_failure(self):
        text = "请您确认以上 Stage-1 方案。确认后我将继续制作 PPT。"

        self.assertRegex(text, agent._CONFIRMATION_REQUEST)
        agent._validate_final_delivery(
            "把报告制作成 PPT",
            text,
            [],
            expects_artifact=True,
            awaiting_confirmation=True,
        )

    async def test_general_task_uses_reasoning_content_when_provider_content_is_empty(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        response_stream = make_stream(SimpleNamespace(
            content=None,
            reasoning_content="这是一个可用的通用回答。",
            tool_calls=None,
        ))
        create = AsyncMock(return_value=response_stream)
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        with (
            patch.object(agent, "SessionLocal", session_factory),
            patch.object(agent, "_get_llm_client", return_value=client),
        ):
            events = [event async for event in agent.run_agent(
                query="解释这个概念",
                conversation_id="00000000-0000-0000-0000-000000000124",
            )]

        payloads = [json.loads(event.removeprefix("data: ")) for event in events]
        self.assertTrue(any(
            payload.get("type") == "thought_delta" and payload.get("text") == "这是一个可用的通用回答。"
            for payload in payloads
        ))

    async def test_streamed_answer_removes_decorative_logos(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        response_stream = make_stream(
            SimpleNamespace(content="🚀 结论", reasoning_content=None, tool_calls=None),
            SimpleNamespace(content="![lo", reasoning_content=None, tool_calls=None),
            SimpleNamespace(content="go](https://example.com/logo.png)如下。", reasoning_content=None, tool_calls=None),
        )
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=response_stream))))

        with (
            patch.object(agent, "SessionLocal", session_factory),
            patch.object(agent, "_get_llm_client", return_value=client),
        ):
            events = [event async for event in agent.run_agent(
                query="给出结论",
                conversation_id="00000000-0000-0000-0000-000000000126",
            )]

        payloads = [json.loads(event.removeprefix("data: ")) for event in events]
        self.assertGreaterEqual(sum(payload.get("type") == "content_delta" for payload in payloads), 1)
        final_text = next(payload["text"] for payload in payloads if payload.get("type") == "content" and payload.get("artifact_type") == "text")
        self.assertEqual(final_text, "结论如下。")
        self.assertNotIn("🚀", "".join(event for event in events))
        self.assertNotIn("![logo]", "".join(event for event in events))

    async def test_search_result_is_followed_by_second_model_turn(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)

        load_call = SimpleNamespace(
            index=0,
            id="load-call",
            function=SimpleNamespace(
                name="load_skill",
                arguments='{"skill_name":"document"}',
            ),
        )
        search_call = SimpleNamespace(
            index=0,
            id="search-call",
            function=SimpleNamespace(
                name="search_web",
                arguments='{"query":"测试资料"}',
            ),
        )
        document_call = SimpleNamespace(
            index=0,
            id="document-call",
            function=SimpleNamespace(
                name="create_document",
                arguments=json.dumps({
                    "title": "检索结论",
                    "markdown": "# 检索结论\n已使用搜索资料。",
                    "artifact_type": "document",
                }, ensure_ascii=False),
            ),
        )
        create = AsyncMock(side_effect=[
            make_stream(SimpleNamespace(content=None, tool_calls=[load_call])),
            make_stream(SimpleNamespace(content=None, tool_calls=[search_call])),
            make_stream(SimpleNamespace(content=None, tool_calls=[document_call])),
            make_stream(SimpleNamespace(content="文档已完成。", tool_calls=None)),
        ])
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            def fake_export(_content, output_path, title=""):
                Path(output_path).write_bytes(b"docx")
                return output_path

            search = AsyncMock(return_value='[{"title":"source"}]')
            with (
                patch.object(agent, "SessionLocal", session_factory),
                patch.object(agent, "STORAGE_DIR", temp_dir),
                patch.object(agent, "_get_llm_client", return_value=client),
                patch.object(agent, "_tool_search_web", new=search),
                patch.object(agent, "export_markdown_to_docx", side_effect=fake_export),
            ):
                events = [event async for event in agent.run_agent(
                    query="生成一份文档并搜索资料",
                    conversation_id="00000000-0000-0000-0000-000000000123",
                )]

        search.assert_awaited_once_with("测试资料")
        self.assertEqual(create.await_count, 4)
        payloads = [json.loads(event.removeprefix("data: ")) for event in events]
        self.assertTrue(any(
            payload.get("markdown") == "# 检索结论\n已使用搜索资料。"
            for payload in payloads
        ))

        db = session_factory()
        self.assertEqual(db.query(UserModel).count(), 1)
        self.assertEqual(db.query(ConversationModel).count(), 1)
        self.assertEqual(db.query(RunModel).filter_by(status="completed").count(), 1)
        self.assertEqual(db.query(ArtifactModel).count(), 1)
        self.assertEqual(db.query(MessageModel).count(), 2)
        self.assertEqual(
            [message.role for message in db.query(MessageModel).order_by(MessageModel.created_at).all()],
            ["user", "assistant"],
        )
        run_events = db.query(RunEventModel).order_by(RunEventModel.sequence).all()
        event_types = [event.event_type for event in run_events]
        self.assertEqual(event_types[0], "run_started")
        self.assertIn("tool_started", event_types)
        self.assertIn("tool_completed", event_types)
        self.assertIn("artifact_created", event_types)
        self.assertEqual(event_types[-1], "run_completed")
        artifact_event = next(
            event for event in run_events if event.event_type == "artifact_created"
        )
        self.assertEqual(artifact_event.payload["status"], "ready")
        self.assertGreater(artifact_event.payload["size_bytes"], 0)
        self.assertIn("download_url", artifact_event.payload)
        db.close()


if __name__ == "__main__":
    unittest.main()
