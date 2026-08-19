import os
import shutil
import asyncio
import io
import logging
import re
import uuid
import httpx
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional

from database import init_db, SessionLocal
from harness.thread_state import ThreadRecorder, get_or_create_state, update_state
from models import ArtifactModel, AttachmentModel, ConversationModel, MessageModel, PlanTaskModel, RunEventModel, RunModel, ThreadEventModel, UserModel, utc_now
from security import create_download_url, create_preview_url, verify_download_token
from runtime_paths import ATTACHMENT_DIR as RUNTIME_ATTACHMENT_DIR


logger = logging.getLogger(__name__)


def _restore_run_groups(db, run_id: str) -> list[dict]:
    events = (
        db.query(RunEventModel)
        .filter(RunEventModel.run_id == run_id)
        .order_by(RunEventModel.sequence)
        .all()
    )
    groups = []

    def add_group(title, thought=None):
        group = {"id": f"{run_id}-{len(groups) + 1}", "title": title, "thoughts": [], "actions": []}
        if thought:
            group["thoughts"].append(thought)
        groups.append(group)
        return group

    def current_group():
        return groups[-1] if groups else add_group("任务执行")

    for event in events:
        payload = event.payload or {}
        if event.event_type == "plan_created":
            group = add_group(payload.get("title") or "任务计划", "已根据任务目标生成执行计划。")
            for step in payload.get("steps") or []:
                title = step.get("title") if isinstance(step, dict) else str(step)
                group["actions"].append({"text": title, "status": "loading"})
        elif event.event_type == "model_started":
            turn = int(payload.get("turn", 1))
            add_group(f"第 {turn} 轮思考", "正在分析当前上下文并决定下一步操作。")
        elif event.event_type == "tool_started":
            name = payload.get("name") or "工具"
            arguments = payload.get("arguments") or {}
            if name == "update_plan":
                group = add_group(arguments.get("title") or "动态任务计划")
                for step in arguments.get("steps") or []:
                    title = step.get("title") if isinstance(step, dict) else str(step)
                    group["actions"].append({"text": title, "status": "loading"})
            elif name in {"generate_ppt", "create_document"}:
                group = add_group("正在生成任务产物")
                group["actions"].append({"text": f"正在执行 {name}", "status": "loading"})
            else:
                current_group()["actions"].append({"text": f"正在执行 {name}", "status": "loading"})
        elif event.event_type == "tool_completed":
            name = payload.get("name") or "工具"
            current_group()["actions"].append({"text": f"{name} 执行完成", "status": "done"})
        elif event.event_type == "model_empty":
            current_group()["actions"].append({"text": "模型未返回有效内容，正在自动恢复", "status": "loading"})
    return groups


def _artifact_download_filename(title: str, storage_path: str) -> str:
    suffix = os.path.splitext(storage_path)[1]
    safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .") or "artifact"
    if suffix and not safe_title.lower().endswith(suffix.lower()):
        safe_title += suffix
    return safe_title

app = FastAPI(title="交数智航 API")

# Initialize SQLite/Database schema on startup
@app.on_event("startup")
def on_startup():
    init_db()


@app.on_event("shutdown")
async def on_shutdown():
    from orchestration.checkpoint import close_checkpointer
    await close_checkpointer()

DEFAULT_ALLOWED_ORIGINS = "http://localhost:6477,http://127.0.0.1:6477"
ALLOWED_ORIGINS = list(dict.fromkeys(
    origin.strip()
    for origin in f"{DEFAULT_ALLOWED_ORIGINS},{os.getenv('ALLOWED_ORIGINS', '')}".split(",")
    if origin.strip()
))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))
TEXT_UPLOAD_EXTENSIONS = {"pdf", "docx", "txt", "md", "markdown"}
BINARY_UPLOAD_EXTENSIONS = {"pptx", "mp3", "m4a", "wav", "aac", "ogg", "flac"}
ALLOWED_UPLOAD_EXTENSIONS = TEXT_UPLOAD_EXTENSIONS | BINARY_UPLOAD_EXTENSIONS

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FILE_PARSE_URL = os.getenv("FILE_PARSE_URL")
FILE_PARSE_TOKEN = os.getenv("FILE_PARSE_TOKEN")
FILE_PARSE_BACKEND = os.getenv("FILE_PARSE_BACKEND", "pipeline")
FILE_PARSE_TIMEOUT_SECONDS = float(os.getenv("FILE_PARSE_TIMEOUT_SECONDS", "600"))
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:6477").rstrip("/")
ATTACHMENT_DIR = str(RUNTIME_ATTACHMENT_DIR)
os.makedirs(ATTACHMENT_DIR, exist_ok=True)


@app.get("/", include_in_schema=False)
async def frontend_entry():
    return RedirectResponse(FRONTEND_URL)

class ChatRequest(BaseModel):
    query: str = Field(min_length=1, max_length=20_000)
    context_text: str = Field(default="", max_length=2_000_000)
    conversation_id: str = Field(default="default_conv", min_length=1, max_length=36)
    attachment_name: Optional[str] = Field(default=None, max_length=200)
    attachment_id: Optional[str] = Field(default=None, max_length=36)
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)


class ConversationStateRequest(BaseModel):
    is_archived: Optional[bool] = None
    is_pinned: Optional[bool] = None


class ConversationUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class ApprovalRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")


def _parse_file_locally(file_ext: str, file_content: bytes) -> str:
    try:
        if file_ext == "pdf":
            try:
                import pymupdf
            except ImportError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="PDF 解析组件未安装，请安装项目 requirements.txt 中的 PyMuPDF。",
                ) from exc
            with pymupdf.open(stream=file_content, filetype="pdf") as pdf_doc:
                return "\n".join(page.get_text() for page in pdf_doc)
        if file_ext == "docx":
            from docx import Document
            doc = Document(io.BytesIO(file_content))
            blocks = [para.text for para in doc.paragraphs if para.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    cells = [cell.text.replace("\n", " ").strip() for cell in row.cells]
                    blocks.append("| " + " | ".join(cells) + " |")
            return "\n".join(blocks)
        return file_content.decode("utf-8")
    except HTTPException:
        raise
    except (UnicodeDecodeError, ValueError, RuntimeError) as exc:
        raise HTTPException(
            status_code=422,
            detail="文件内容无法解析，可能文件已损坏、加密或格式不正确。",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail="文件内容无法解析，可能文件已损坏、加密或格式不正确。",
        ) from exc

def _ensure_local_conversation(db, conversation_id: str, title: str = "新对话"):
    user = db.query(UserModel).filter(UserModel.username == "local-user").first()
    if user is None:
        user = UserModel(username="local-user")
        db.add(user)
        db.flush()
    conversation = db.get(ConversationModel, conversation_id)
    if conversation is None:
        conversation = ConversationModel(id=conversation_id, user_id=user.id, title=title)
        db.add(conversation)
        db.flush()
    get_or_create_state(db, conversation.id)
    return conversation


@app.post("/upload")
async def upload_file(file: UploadFile = File(...), conversation_id: str = Form(default="default_conv")):
    try:
        # Direct function calls in tests do not have FastAPI's dependency injection.
        if not isinstance(conversation_id, str):
            conversation_id = "default_conv"
        filename = file.filename or ""
        file_ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if file_ext not in ALLOWED_UPLOAD_EXTENSIONS:
            raise HTTPException(status_code=415, detail="Unsupported file type")

        file_content = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(file_content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File exceeds upload size limit")
            
        if file_ext in BINARY_UPLOAD_EXTENSIONS:
            fallback_text = ""
        elif not FILE_PARSE_URL or not FILE_PARSE_TOKEN or "your-file-parse" in FILE_PARSE_URL:
            fallback_text = await asyncio.to_thread(_parse_file_locally, file_ext, file_content)
        else:
            files = {"files": (filename, file_content, file.content_type)}
            data = {
                "backend": FILE_PARSE_BACKEND,
                "lang_list": "ch",
                "parse_method": "auto",
                "return_md": "true",
            }
            headers = {"Authorization": f"Bearer {FILE_PARSE_TOKEN}"}
            try:
                async with httpx.AsyncClient(timeout=FILE_PARSE_TIMEOUT_SECONDS) as client:
                    response = await client.post(FILE_PARSE_URL, files=files, data=data, headers=headers)
                    response.raise_for_status()
                    parsed = response.json()
                    fallback_text = next(
                        (str(value.get("md_content", "")) for value in parsed.get("results", {}).values()),
                        "",
                    )
                    if not fallback_text.strip():
                        fallback_text = await asyncio.to_thread(_parse_file_locally, file_ext, file_content)
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "Remote file parser returned HTTP %s: %s; falling back to local parsing",
                    exc.response.status_code,
                    exc.response.text[:1000],
                )
                fallback_text = await asyncio.to_thread(_parse_file_locally, file_ext, file_content)
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(
                    "Remote file parser failed with %s: %s; falling back to local parsing",
                    type(exc).__name__,
                    exc,
                )
                fallback_text = await asyncio.to_thread(_parse_file_locally, file_ext, file_content)

        attachment_path = os.path.join(ATTACHMENT_DIR, f"{uuid.uuid4()}_{os.path.basename(filename)}")
        with open(attachment_path, "wb") as output:
            output.write(file_content)
        db = SessionLocal()
        try:
            conversation = _ensure_local_conversation(db, conversation_id)
            attachment = AttachmentModel(
                conversation_id=conversation.id,
                file_name=filename,
                mime_type=file.content_type or "application/octet-stream",
                size_bytes=len(file_content),
                storage_path=attachment_path,
                extracted_text=fallback_text,
            )
            db.add(attachment)
            db.commit()
            ThreadRecorder(db, conversation.id).record("attachment_added", {
                "attachment_id": attachment.id,
                "file_name": filename,
                "mime_type": attachment.mime_type,
                "size_bytes": attachment.size_bytes,
            })
            res_json = {"results": {filename: {"md_content": fallback_text}}}
            return JSONResponse(content={
                "success": True,
                "attachment_id": attachment.id,
                "extracted_text": fallback_text,
                "result": res_json,
            })
        finally:
            db.close()

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="File processing failed") from e

@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    from agent import run_agent
    async def event_generator():
        async for chunk in run_agent(
            query=request.query,
            context_text=request.context_text,
            conversation_id=request.conversation_id,
            attachment_name=request.attachment_name,
            attachment_id=request.attachment_id,
            attachment_ids=request.attachment_ids,
        ):
            yield chunk

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/v1/conversations")
async def list_conversations(include_archived: bool = False):
    db = SessionLocal()
    try:
        local_user = db.query(UserModel).filter(UserModel.username == "local-user").first()
        if local_user is None:
            return []
        conversations = (
            db.query(ConversationModel)
            .filter(ConversationModel.user_id == local_user.id)
            .order_by(ConversationModel.updated_at.desc())
            .all()
        )
        result = []
        for conversation in conversations:
            state = get_or_create_state(db, conversation.id)
            if state.is_archived and not include_archived:
                continue
            last_message = (
            db.query(MessageModel)
            .filter(MessageModel.conversation_id == conversation.id)
            .order_by(MessageModel.created_at.desc(), MessageModel.role.desc(), MessageModel.id.desc())
                .first()
            )
            result.append({
                "id": conversation.id,
                "title": conversation.title,
                "created_at": conversation.created_at,
                "updated_at": conversation.updated_at,
                "last_message": last_message.content[:80] if last_message else "",
                "is_archived": state.is_archived,
                "is_pinned": state.is_pinned,
            })
        return sorted(result, key=lambda item: (item["is_pinned"], item["updated_at"]), reverse=True)
    finally:
        db.close()


@app.get("/api/v1/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    from agent import _contains_artifact_source, _sanitize_artifact_final_text, read_artifact_preview

    db = SessionLocal()
    try:
        conversation = db.get(ConversationModel, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        messages = (
            db.query(MessageModel)
            .filter(MessageModel.conversation_id == conversation_id)
            .order_by(MessageModel.created_at, MessageModel.role.desc(), MessageModel.id)
            .all()
        )
        serialized_messages = []
        for message in messages:
            artifacts = []
            if message.role == "assistant" and message.run_id:
                stored_artifacts = (
                    db.query(ArtifactModel)
                    .filter(ArtifactModel.run_id == message.run_id)
                    .order_by(ArtifactModel.created_at)
                    .all()
                )
                for artifact in stored_artifacts:
                    preview_kind, preview = read_artifact_preview(artifact)
                    artifacts.append({
                        "artifact_id": artifact.id,
                        "artifact_type": artifact.artifact_type,
                        "title": artifact.title,
                        "preview_kind": preview_kind,
                        preview_kind: preview,
                        "download_url": create_download_url(artifact.id),
                        "preview_url": create_preview_url(artifact.id),
                        "missing_fields": artifact.missing_fields,
                    })
            content = message.content
            if message.role == "assistant" and _contains_artifact_source(content):
                content = _sanitize_artifact_final_text(content) if artifacts else "该轮任务未生成有效产物，请重新执行。"
            serialized_messages.append({
                "id": message.id,
                "role": message.role,
                "content": content,
                "attachment_name": message.attachment_name,
                "created_at": message.created_at,
                "artifacts": artifacts,
                "groups": _restore_run_groups(db, message.run_id) if message.role == "assistant" and message.run_id else [],
            })
        return {
            "id": conversation.id,
            "title": conversation.title,
            "created_at": conversation.created_at,
            "updated_at": conversation.updated_at,
            "is_archived": get_or_create_state(db, conversation_id).is_archived,
            "is_pinned": get_or_create_state(db, conversation_id).is_pinned,
            "messages": serialized_messages,
        }
    finally:
        db.close()


@app.get("/api/v1/conversations/{conversation_id}/events")
async def get_conversation_events(conversation_id: str, after: int = 0):
    db = SessionLocal()
    try:
        if db.get(ConversationModel, conversation_id) is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        events = ThreadRecorder(db, conversation_id).events_after(max(0, after))
        return {
            "conversation_id": conversation_id,
            "events": [{
                "sequence": event.sequence,
                "type": event.event_type,
                "run_id": event.run_id,
                "payload": event.payload,
                "created_at": event.created_at,
            } for event in events],
        }
    finally:
        db.close()


@app.post("/api/v1/conversations/{conversation_id}/state")
async def set_conversation_state(conversation_id: str, state: ConversationStateRequest):
    db = SessionLocal()
    try:
        conversation = db.get(ConversationModel, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        result = update_state(
            db,
            conversation_id,
            is_archived=state.is_archived,
            is_pinned=state.is_pinned,
        )
        ThreadRecorder(db, conversation_id).record("thread_state_changed", {
            "is_archived": result.is_archived,
            "is_pinned": result.is_pinned,
        })
        return {"id": conversation_id, "is_archived": result.is_archived, "is_pinned": result.is_pinned}
    finally:
        db.close()


@app.patch("/api/v1/conversations/{conversation_id}")
async def rename_conversation(conversation_id: str, request: ConversationUpdateRequest):
    db = SessionLocal()
    try:
        conversation = db.get(ConversationModel, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        conversation.title = request.title.strip()
        conversation.updated_at = utc_now()
        ThreadRecorder(db, conversation_id).record("thread_renamed", {"title": conversation.title})
        db.commit()
        return {"id": conversation_id, "title": conversation.title}
    finally:
        db.close()


@app.delete("/api/v1/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    db = SessionLocal()
    try:
        conversation = db.get(ConversationModel, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        storage_paths = [attachment.storage_path for attachment in conversation.attachments]
        for run in conversation.runs:
            storage_paths.extend(artifact.storage_path for artifact in run.artifacts)
        db.delete(conversation)
        db.commit()
        for storage_path in storage_paths:
            if not storage_path:
                continue
            try:
                if os.path.isdir(storage_path):
                    shutil.rmtree(storage_path)
                elif os.path.exists(storage_path):
                    os.remove(storage_path)
                preview_path = f"{storage_path}.preview.md"
                if os.path.exists(preview_path):
                    os.remove(preview_path)
            except OSError:
                logger.warning("Failed to remove conversation file: %s", storage_path, exc_info=True)
        return {"id": conversation_id, "deleted": True}
    finally:
        db.close()


@app.get("/api/v1/attachments/{attachment_id}/download")
async def download_attachment(attachment_id: str):
    db = SessionLocal()
    try:
        attachment = db.get(AttachmentModel, attachment_id)
        if attachment is None or not os.path.exists(attachment.storage_path):
            raise HTTPException(status_code=404, detail="Attachment not found")
        return FileResponse(
            path=attachment.storage_path,
            filename=attachment.file_name,
            media_type=attachment.mime_type,
        )
    finally:
        db.close()

@app.get("/api/v1/artifacts/{artifact_id}/download")
async def download_artifact(artifact_id: str, token: str, expires: int):
    if not verify_download_token(artifact_id, token, expires):
        raise HTTPException(status_code=403, detail="Invalid download token")
    db = SessionLocal()
    try:
        art = db.query(ArtifactModel).filter(ArtifactModel.id == artifact_id).first()
        if not art or not os.path.exists(art.storage_path):
            raise HTTPException(status_code=404, detail="Artifact file not found")
        
        filename = _artifact_download_filename(art.title, art.storage_path)
        return FileResponse(path=art.storage_path, filename=filename, media_type=art.mime_type)
    finally:
        db.close()


@app.get("/api/v1/artifacts/{artifact_id}/preview")
async def preview_artifact(artifact_id: str, token: str, expires: int):
    if not verify_download_token(artifact_id, token, expires):
        raise HTTPException(status_code=403, detail="Invalid preview token")
    db = SessionLocal()
    try:
        art = db.query(ArtifactModel).filter(ArtifactModel.id == artifact_id).first()
        if not art or not os.path.exists(art.storage_path):
            raise HTTPException(status_code=404, detail="Artifact file not found")
        if art.mime_type not in {"application/pdf"} and not art.mime_type.startswith("image/"):
            raise HTTPException(status_code=415, detail="This artifact type has no native browser preview")
        return FileResponse(
            path=art.storage_path,
            media_type=art.mime_type,
            headers={"Content-Disposition": "inline"},
        )
    finally:
        db.close()


@app.get("/api/v1/runs/{run_id}")
async def get_run(run_id: str):
    db = SessionLocal()
    try:
        run = db.get(RunModel, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        events = (
            db.query(RunEventModel)
            .filter(RunEventModel.run_id == run_id)
            .order_by(RunEventModel.sequence)
            .all()
        )
        return {
            "id": run.id,
            "conversation_id": run.conversation_id,
            "intent_type": run.intent_type,
            "engine": run.engine,
            "router_confidence": run.router_confidence,
            "router_reasons": run.router_reasons or [],
            "status": run.status,
            "pending_approval": run.pending_approval,
            "error_message": run.error_message,
            "created_at": run.created_at,
            "completed_at": run.completed_at,
            "events": [
                {
                    "sequence": event.sequence,
                    "type": event.event_type,
                    "payload": event.payload,
                    "created_at": event.created_at,
                }
                for event in events
            ],
            "plan_tasks": [{
                "id": task.id,
                "position": task.position,
                "title": task.title,
                "status": task.status,
                "depends_on": task.depends_on or [],
                "attempt": task.attempt,
                "error_message": task.error_message,
            } for task in db.query(PlanTaskModel).filter(PlanTaskModel.run_id == run_id).order_by(PlanTaskModel.position).all()],
        }
    finally:
        db.close()


@app.post("/api/v1/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    db = SessionLocal()
    try:
        run = db.get(RunModel, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        if run.status not in {"pending", "running", "awaiting_approval"}:
            raise HTTPException(status_code=409, detail=f"Run is already {run.status}")
        run.status = "cancelled"
        run.completed_at = utc_now()
        db.commit()
        return {"id": run.id, "status": run.status}
    finally:
        db.close()


@app.post("/api/v1/runs/{run_id}/approval")
async def decide_run_approval(run_id: str, request: ApprovalRequest):
    db = SessionLocal()
    try:
        run = db.get(RunModel, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        if run.status != "awaiting_approval" or not run.pending_approval:
            raise HTTPException(status_code=409, detail="Run is not awaiting approval")
    finally:
        db.close()
    from agent import resume_agent
    return StreamingResponse(
        resume_agent(run_id, request.decision),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=os.getenv("APP_HOST", "127.0.0.1"),
        port=int(os.getenv("APP_PORT", "14499")),
    )
