import asyncio
import hashlib
import json
import sys
import uuid
import zipfile
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
load_dotenv(PROJECT_ROOT / ".env", override=False)
sys.path.insert(0, str(BACKEND_DIR))

from agent import run_agent
from database import SessionLocal
from models import ArtifactModel, RunEventModel, RunModel
from opensandbox.manager import SandboxManager
from orchestration.checkpoint import close_checkpointer
from opensandbox.models.sandboxes import SandboxFilter
from sandbox.factory import _opensandbox_config


async def main() -> int:
    conversation_id = str(uuid.uuid4())
    thread_id = f"local:local-user:{conversation_id}"
    thread_key = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:32]
    query = (
        "请使用 ppt-master 技能，以明确的 Quick 快速模式制作一份 3 页中文 PPTX，"
        "主题为 OpenSandbox 智能体沙箱接入验收。第 1 页是标题与结论，第 2 页展示线程作用域架构，"
        "第 3 页汇总命令执行、文件读写、线程复用、线程隔离、失败码、超时、产物回传、Agent execute 共 8 项通过结果。"
        "采用专业蓝色科技风。我已明确确认上述内容、结构和风格，授权直接执行，不需要再次询问。"
        "最终文件必须是可编辑 PPTX，并写入 /outputs/opensandbox-sandbox-acceptance.pptx。"
    )
    received_error = None
    artifact_id = None
    run_id = None
    try:
        async for raw_event in run_agent(query=query, conversation_id=conversation_id):
            payload = json.loads(raw_event.removeprefix("data: "))
            run_id = payload.get("run_id") or run_id
            if payload.get("type") == "error":
                received_error = payload.get("msg")
            if payload.get("artifact_type") == "ppt" and payload.get("artifact_id"):
                artifact_id = payload["artifact_id"]

        if received_error:
            raise RuntimeError(received_error)
        if not artifact_id:
            raise RuntimeError("Agent run completed without a PPT artifact")

        db = SessionLocal()
        try:
            artifact = db.get(ArtifactModel, artifact_id)
            if artifact is None:
                raise RuntimeError("PPT artifact record was not persisted")
            path = Path(artifact.storage_path)
            if path.suffix.lower() != ".pptx" or not path.is_file():
                raise RuntimeError(f"Expected a persisted PPTX file, got: {path}")
            with zipfile.ZipFile(path) as package:
                names = package.namelist()
                slide_count = sum(
                    name.startswith("ppt/slides/slide") and name.endswith(".xml")
                    for name in names
                )
                if "ppt/presentation.xml" not in names or slide_count < 3:
                    raise RuntimeError(f"Invalid PPTX package or too few slides: {slide_count}")
            events = db.query(RunEventModel).filter(RunEventModel.run_id == run_id).all()
            event_payloads = [(event.event_type, event.payload or {}) for event in events]
            loaded = [payload.get("name") for event_type, payload in event_payloads if event_type == "skill_loaded"]
            tools = [payload.get("name") for event_type, payload in event_payloads if event_type == "tool_started"]
            if "ppt-master" not in loaded:
                raise RuntimeError(f"PPTX was created but ppt-master load was not observed: {loaded}")
            if "execute" not in tools:
                raise RuntimeError(f"PPTX was created without an observed sandbox execute call: {tools}")
            print(f"PASS ppt-master loaded: {loaded}")
            print(f"PASS sandbox tools observed: {sorted(set(filter(None, tools)))}")
            print(f"PASS PPTX artifact: {path} ({path.stat().st_size} bytes, {slide_count} slides)")
        finally:
            db.close()
    finally:
        manager = await SandboxManager.create(connection_config=_opensandbox_config())
        try:
            page = await manager.list_sandbox_infos(SandboxFilter(
                metadata={"app": "ai-ppt", "thread": thread_key},
                page_size=100,
            ))
            for sandbox in page.sandbox_infos:
                try:
                    await manager.kill_sandbox(sandbox.id)
                except Exception:
                    pass
        finally:
            await manager.close()
        await close_checkpointer()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
