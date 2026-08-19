import io
import json
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from docx.oxml.ns import qn

from starlette.datastructures import UploadFile


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import agent
import main
from rag.chunker import DocumentChunker
from security import create_download_token, verify_download_token
from capabilities.skill_registry import SkillRegistry
from database import Base
from models import ArtifactModel, AttachmentModel, ConversationModel, MessageModel, RunEventModel, RunModel, ThreadEventModel, UserModel
from exporters.docx_exporter import export_markdown_to_docx


class UploadTests(unittest.IsolatedAsyncioTestCase):
    def test_default_cors_supports_localhost_and_loopback(self):
        self.assertIn("http://localhost:6477", main.ALLOWED_ORIGINS)
        self.assertIn("http://127.0.0.1:6477", main.ALLOWED_ORIGINS)

    def test_local_pdf_parser_extracts_text(self):
        import pymupdf

        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((72, 72), "PDF parser works")
        content = document.tobytes()
        document.close()

        self.assertIn("PDF parser works", main._parse_file_locally("pdf", content))

    async def test_root_redirects_to_frontend(self):
        response = await main.frontend_entry()
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], main.FRONTEND_URL)

    async def test_unsupported_extension_keeps_415_status(self):
        upload = UploadFile(file=io.BytesIO(b"data"), filename="payload.exe")
        with self.assertRaises(main.HTTPException) as raised:
            await main.upload_file(upload)
        self.assertEqual(raised.exception.status_code, 415)

    async def test_upload_rejects_file_above_configured_limit(self):
        upload = UploadFile(file=io.BytesIO(b"1234"), filename="brief.txt")
        with patch.object(main, "MAX_UPLOAD_BYTES", 3):
            with self.assertRaises(main.HTTPException) as raised:
                await main.upload_file(upload)
        self.assertEqual(raised.exception.status_code, 413)

    async def test_remote_failure_uses_local_text_parser(self):
        class FailingClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, *_args, **_kwargs):
                raise main.httpx.HTTPError("offline")

        upload = UploadFile(file=io.BytesIO("有效文本".encode()), filename="brief.txt")
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        with (
            patch.object(main, "FILE_PARSE_URL", "https://parser.invalid"),
            patch.object(main, "FILE_PARSE_TOKEN", "token"),
            patch.object(main.httpx, "AsyncClient", return_value=FailingClient()),
            patch.object(main, "SessionLocal", session_factory),
        ):
            response = await main.upload_file(upload)

        payload = json.loads(response.body)
        self.assertEqual(payload["extracted_text"], "有效文本")
        self.assertEqual(payload["result"]["results"]["brief.txt"]["md_content"], "有效文本")

    async def test_remote_parser_uses_configured_pipeline_and_timeout(self):
        captured = {}

        class SuccessfulResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"results": {"brief.txt": {"md_content": "远程解析结果"}}}

        class CapturingClient:
            def __init__(self, timeout):
                captured["timeout"] = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, *_args, **kwargs):
                captured["data"] = kwargs["data"]
                return SuccessfulResponse()

        upload = UploadFile(file=io.BytesIO("本地内容".encode()), filename="brief.txt")
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        with (
            patch.object(main, "FILE_PARSE_URL", "https://parser.example/file_parse"),
            patch.object(main, "FILE_PARSE_TOKEN", "token"),
            patch.object(main, "FILE_PARSE_BACKEND", "pipeline"),
            patch.object(main, "FILE_PARSE_TIMEOUT_SECONDS", 600.0),
            patch.object(main.httpx, "AsyncClient", CapturingClient),
            patch.object(main, "SessionLocal", session_factory),
        ):
            response = await main.upload_file(upload)

        payload = json.loads(response.body)
        self.assertEqual(payload["extracted_text"], "远程解析结果")
        self.assertEqual(captured["timeout"], 600.0)
        self.assertEqual(captured["data"]["backend"], "pipeline")
        self.assertEqual(captured["data"]["lang_list"], "ch")
        self.assertEqual(captured["data"]["parse_method"], "auto")

    def test_docx_export_preserves_chinese_and_markdown_content(self):
        import tempfile
        from docx import Document

        content = "# 总结\n正文含 **重点内容**。\n- 条目一\n1. 条目二\n\n| 项目 | 状态 |\n| --- | --- |\n| 交付 | 完成 |"
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "report.docx"
            export_markdown_to_docx(content, str(output), title="测试文档")
            document = Document(output)

        full_text = "\n".join(paragraph.text for paragraph in document.paragraphs)
        self.assertIn("重点内容", full_text)
        self.assertIn("条目一", full_text)
        self.assertEqual(document.styles["Normal"].font.name, "宋体")
        self.assertEqual(document.styles["Normal"]._element.rPr.rFonts.get(qn("w:eastAsia")), "宋体")
        self.assertEqual(document.tables[0].cell(1, 0).text, "交付")

    def test_local_docx_parser_keeps_table_cells(self):
        from docx import Document

        document = Document()
        document.add_paragraph("正文段落")
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "项目"
        table.cell(0, 1).text = "状态"
        stream = io.BytesIO()
        document.save(stream)

        extracted = main._parse_file_locally("docx", stream.getvalue())
        self.assertIn("正文段落", extracted)
        self.assertIn("| 项目 | 状态 |", extracted)


class RoutingAndRetrievalTests(unittest.TestCase):
    def test_llm_base_url_collapses_duplicate_path_slashes(self):
        normalized = agent._normalize_base_url("http://gateway.example//v1/")
        self.assertEqual(normalized, "http://gateway.example/v1")

    def test_general_agent_has_no_fixed_intent_router(self):
        self.assertFalse(hasattr(agent, "IntentRouter"))
        self.assertIn("普通问答", agent._base_system_prompt(""))

    def test_report_uses_report_skill(self):
        self.assertEqual(SkillRegistry().get_skill("report").name, "report")

    def test_chinese_query_retrieves_relevant_chunk(self):
        chunks = [
            {"content": "开篇无关内容", "token_count": 10},
            {"content": "项目质量与安全措施详述", "token_count": 10},
        ]
        result = DocumentChunker.retrieve_relevant_chunks(
            chunks, "请分析质量与安全措施", top_k=1
        )
        self.assertEqual(result, ["项目质量与安全措施详述"])

    def test_theme_palette_changes_template_variables(self):
        html = ":root{--ink:#000;--ink-rgb:0,0,0;--paper:#fff;--paper-rgb:255,255,255;--paper-tint:#eee;--ink-tint:#111;}"
        themed = agent._apply_theme(html, "森林墨")
        self.assertIn("--ink:#1a2e1f;", themed)
        self.assertIn("--paper:#f5f1e8;", themed)

    def test_ppt_markup_requires_visible_slide_sections(self):
        markup = agent._extract_slide_markup(
            '```html\n<section class="slide"><h1>项目标题</h1><p>这里包含足够的正文内容，用于验证幻灯片确实具有可见信息。</p></section>\n```'
        )
        self.assertIn("<h1>项目标题</h1>", markup)
        with self.assertRaises(ValueError):
            agent._extract_slide_markup("<div>只有背景</div>")
        with self.assertRaises(ValueError):
            agent._extract_slide_markup('<section class="slide"></section>')

    def test_download_tokens_are_artifact_specific(self):
        expires = int(time.time()) + 60
        token = create_download_token("artifact-a", expires)
        self.assertTrue(verify_download_token("artifact-a", token, expires))
        self.assertFalse(verify_download_token("artifact-b", token, expires))
        self.assertFalse(verify_download_token("artifact-a", token, 0))

    def test_artifact_download_filename_uses_model_title_and_storage_extension(self):
        self.assertEqual(
            main._artifact_download_filename("季度汇报", "storage/run-1.pptx"),
            "季度汇报.pptx",
        )
        self.assertEqual(
            main._artifact_download_filename("季度/汇报.pptx", "storage/run-1.pptx"),
            "季度_汇报.pptx",
        )


class RunControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_running_run_can_be_cancelled_and_queried(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        conversation_id = str(uuid.uuid4())

        db = session_factory()
        user = UserModel(username="run-control-user")
        db.add(user)
        db.flush()
        db.add(ConversationModel(id=conversation_id, user_id=user.id))
        run = RunModel(conversation_id=conversation_id, status="running")
        db.add(run)
        db.commit()
        run_id = run.id
        db.close()

        with patch.object(main, "SessionLocal", session_factory):
            cancelled = await main.cancel_run(run_id)
            fetched = await main.get_run(run_id)

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(fetched["status"], "cancelled")
        self.assertEqual(fetched["events"], [])


class ConversationHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_conversation_api_hides_corrupted_artifact_source_message(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        conversation_id = str(uuid.uuid4())
        db = session_factory()
        user = UserModel(username="local-user")
        db.add(user)
        db.flush()
        db.add(ConversationModel(id=conversation_id, user_id=user.id, title="异常输出"))
        run = RunModel(conversation_id=conversation_id, status="completed")
        db.add(run)
        db.flush()
        db.add(MessageModel(
            conversation_id=conversation_id,
            run_id=run.id,
            role="assistant",
            content="PPT已修改。\n\n[历史产物: 建设方案]\n<!DOCTYPE html><style>body{color:red}</style>",
        ))
        db.commit()
        db.close()

        with patch.object(main, "SessionLocal", session_factory):
            detail = await main.get_conversation(conversation_id)

        self.assertEqual(detail["messages"][0]["content"], "该轮任务未生成有效产物，请重新执行。")

    async def test_conversation_api_restores_messages_and_artifact_preview(self):
        import tempfile

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        conversation_id = str(uuid.uuid4())

        with tempfile.TemporaryDirectory() as temp_dir:
            preview_file = Path(temp_dir) / "deck.html"
            preview_file.write_text("<section class='slide'>历史预览内容足够完整</section>", encoding="utf-8")
            db = session_factory()
            user = UserModel(username="local-user")
            db.add(user)
            db.flush()
            conversation = ConversationModel(id=conversation_id, user_id=user.id, title="历史对话")
            db.add(conversation)
            db.flush()
            run = RunModel(conversation_id=conversation_id, status="completed")
            db.add(run)
            db.flush()
            db.add_all([
                MessageModel(conversation_id=conversation_id, run_id=run.id, role="user", content="生成演示文稿"),
                MessageModel(conversation_id=conversation_id, run_id=run.id, role="assistant", content="已经生成。"),
                RunEventModel(run_id=run.id, sequence=1, event_type="model_started", payload={"turn": 1}),
                RunEventModel(run_id=run.id, sequence=2, event_type="tool_started", payload={"name": "generate_ppt", "arguments": {}}),
                RunEventModel(run_id=run.id, sequence=3, event_type="tool_completed", payload={"name": "generate_ppt"}),
                ArtifactModel(
                    run_id=run.id,
                    title="历史演示文稿",
                    artifact_type="ppt",
                    mime_type="text/html",
                    storage_path=str(preview_file),
                ),
            ])
            db.commit()
            db.close()

            with patch.object(main, "SessionLocal", session_factory):
                summaries = await main.list_conversations()
                detail = await main.get_conversation(conversation_id)

        self.assertEqual(summaries[0]["title"], "历史对话")
        self.assertEqual([item["role"] for item in detail["messages"]], ["user", "assistant"])
        artifact = detail["messages"][1]["artifacts"][0]
        self.assertEqual(artifact["title"], "历史演示文稿")
        self.assertIn("历史预览内容", artifact["html"])
        self.assertIn("/download?", artifact["download_url"])
        self.assertIn("/preview?", artifact["preview_url"])
        groups = detail["messages"][1]["groups"]
        self.assertEqual([group["title"] for group in groups], ["第 1 轮思考", "正在生成任务产物"])
        self.assertEqual(groups[1]["actions"][-1], {"text": "generate_ppt 执行完成", "status": "done"})

    async def test_thread_events_and_state_are_persistent(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        conversation_id = str(uuid.uuid4())
        db = session_factory()
        user = UserModel(username="local-user")
        db.add(user)
        db.flush()
        db.add(ConversationModel(id=conversation_id, user_id=user.id, title="可恢复对话"))
        db.commit()
        db.close()

        with patch.object(main, "SessionLocal", session_factory):
            state = await main.set_conversation_state(conversation_id, main.ConversationStateRequest(is_pinned=True))
            events = await main.get_conversation_events(conversation_id)

        self.assertTrue(state["is_pinned"])
        self.assertEqual(events["events"][0]["type"], "thread_state_changed")
        self.assertEqual(events["events"][0]["sequence"], 1)

    async def test_conversation_can_be_renamed_and_deleted(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        conversation_id = str(uuid.uuid4())
        db = session_factory()
        user = UserModel(username="local-user")
        db.add(user)
        db.flush()
        db.add(ConversationModel(id=conversation_id, user_id=user.id, title="旧任务名"))
        db.commit()
        db.close()

        with patch.object(main, "SessionLocal", session_factory):
            renamed = await main.rename_conversation(
                conversation_id,
                main.ConversationUpdateRequest(title="新任务名"),
            )
            deleted = await main.delete_conversation(conversation_id)

        self.assertEqual(renamed["title"], "新任务名")
        self.assertTrue(deleted["deleted"])
        db = session_factory()
        self.assertIsNone(db.get(ConversationModel, conversation_id))
        db.close()

    async def test_upload_keeps_original_attachment_and_timeline_event(self):
        import tempfile

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        conversation_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as temp_dir:
            upload = UploadFile(file=io.BytesIO("原始文件内容".encode()), filename="source.txt")
            with (
                patch.object(main, "SessionLocal", session_factory),
                patch.object(main, "ATTACHMENT_DIR", temp_dir),
            ):
                response = await main.upload_file(upload, conversation_id)
                payload = json.loads(response.body)
                events = await main.get_conversation_events(conversation_id)

            db = session_factory()
            attachment = db.get(AttachmentModel, payload["attachment_id"])
            self.assertIsNotNone(attachment)
            self.assertEqual(attachment.extracted_text, "原始文件内容")
            self.assertTrue(Path(attachment.storage_path).exists())
            self.assertEqual(Path(attachment.storage_path).read_text(encoding="utf-8"), "原始文件内容")
            self.assertEqual(events["events"][0]["type"], "attachment_added")
            db.close()


if __name__ == "__main__":
    unittest.main()
