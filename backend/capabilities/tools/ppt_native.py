import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from database import SessionLocal
from models import ArtifactModel, AttachmentModel, RunModel

from ..skill_registry import get_skill_root
from .registry import ToolContext, ToolResult


PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
BACKEND_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = get_skill_root() / "ppt-master"
SCRIPTS_ROOT = SKILL_ROOT / "scripts"
NATIVE_STORAGE_ROOT = BACKEND_ROOT / "storage" / "ppt_native"
MAX_SOURCE_BYTES = 100 * 1024 * 1024
PROJECT_ID_RE = re.compile(r"^(template|enhance)_[0-9a-f]{32}$")
AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac"}


def _safe_segment(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_-]+", "_", value).strip("_")
    return cleaned[:80] or fallback


def _display_title(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", value).strip()
    return cleaned[:160] or fallback


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根节点必须是对象: {path.name}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_source(context: ToolContext, source_id: str = "") -> tuple[Path, str]:
    db = SessionLocal()
    try:
        attachment = None
        artifact = None
        if source_id:
            attachment = db.query(AttachmentModel).filter(
                AttachmentModel.id == source_id,
                AttachmentModel.conversation_id == context.conversation_id,
            ).first()
            if attachment is None:
                artifact = db.query(ArtifactModel).join(RunModel).filter(
                    ArtifactModel.id == source_id,
                    RunModel.conversation_id == context.conversation_id,
                ).first()
        else:
            attachment = db.query(AttachmentModel).filter(
                AttachmentModel.conversation_id == context.conversation_id,
                AttachmentModel.file_name.ilike("%.pptx"),
            ).order_by(AttachmentModel.created_at.desc()).first()
            if attachment is None:
                artifact = db.query(ArtifactModel).join(RunModel).filter(
                    RunModel.conversation_id == context.conversation_id,
                    ArtifactModel.mime_type == PPTX_MIME,
                ).order_by(ArtifactModel.created_at.desc()).first()
        record = attachment or artifact
        if record is None:
            raise ValueError("当前对话中没有可用的 PPTX 源文件")
        path = Path(record.storage_path).resolve()
        name = getattr(record, "file_name", None) or getattr(record, "title", None) or path.name
    finally:
        db.close()
    if path.suffix.lower() != ".pptx" or not path.is_file():
        raise ValueError("源文件必须是存在的 .pptx 文件")
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("PPTX 源文件不能超过 100MB")
    return path, str(name)


def _conversation_root(context: ToolContext) -> Path:
    if not context.conversation_id:
        raise ValueError("缺少对话上下文")
    return NATIVE_STORAGE_ROOT / _safe_segment(context.conversation_id, "conversation")


def _project_path(context: ToolContext, project_id: str) -> Path:
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise ValueError("无效的 PPTX 项目 ID")
    path = (_conversation_root(context) / project_id).resolve()
    root = _conversation_root(context).resolve()
    if root not in path.parents or not path.is_dir():
        raise ValueError("PPTX 项目不存在或不属于当前对话")
    metadata = _read_json(path / "platform.json")
    if metadata.get("conversation_id") != context.conversation_id:
        raise ValueError("PPTX 项目不属于当前对话")
    return path


def _run_script_sync(script: str, arguments: list[str], timeout: float = 180.0, check: bool = True):
    script_path = (SCRIPTS_ROOT / script).resolve()
    if SCRIPTS_ROOT.resolve() not in script_path.parents or not script_path.is_file():
        raise ValueError("未登记的 PPT Master 脚本")
    completed = subprocess.run(
        [sys.executable, str(script_path), *arguments],
        cwd=str(SKILL_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "未知错误").strip()[-3000:]
        raise RuntimeError(f"PPTX 受控工具执行失败: {details}")
    return completed


async def _run_script(script: str, arguments: list[str], timeout: float = 180.0, check: bool = True):
    return await asyncio.to_thread(_run_script_sync, script, arguments, timeout, check)


def _artifact_candidate(path: Path, title: str, preview: str) -> dict[str, Any]:
    return {
        "artifact_type": "ppt",
        "title": title,
        "mime_type": PPTX_MIME,
        "extension": "pptx",
        "preview_kind": "markdown",
        "preview_content": preview,
        "file_path": str(path),
        "missing_fields": [],
        "sources": [],
    }


async def execute_analyze_pptx_template(arguments, context: ToolContext) -> ToolResult:
    source, source_name = _resolve_source(context, str(arguments.get("source_id", "")).strip())
    slides = str(arguments.get("slides", "")).strip()
    if slides and not re.fullmatch(r"[0-9,\-\s]+", slides):
        raise ValueError("slides 只能使用页码、逗号和短横线")
    project_id = f"template_{uuid.uuid4().hex}"
    project = _conversation_root(context) / project_id
    for name in ("sources", "analysis", "exports", "validation"):
        (project / name).mkdir(parents=True, exist_ok=True)
    archived = project / "sources" / "source.pptx"
    shutil.copy2(source, archived)
    metadata = {
        "schema": "platform_pptx_project.v1",
        "kind": "template_fill",
        "conversation_id": context.conversation_id,
        "source_name": source_name,
        "source_sha256": _file_hash(source),
        "ready_for_approval": False,
    }
    _write_json(project / "platform.json", metadata)
    library = project / "analysis" / "slide_library.json"
    plan = project / "analysis" / "fill_plan.json"
    await _run_script("template_fill_pptx.py", ["analyze", str(archived), "-o", str(library)])
    scaffold_args = ["scaffold", str(library), "-o", str(plan)]
    if slides:
        scaffold_args.extend(["--slides", slides.replace(" ", "")])
    await _run_script("template_fill_pptx.py", scaffold_args)
    draft = _read_json(plan)
    response = {
        "project_id": project_id,
        "source_name": source_name,
        "draft_plan": draft,
        "next_step": "修改 draft_plan 中的 text/表格/图表字段后调用 prepare_pptx_template_fill",
    }
    return ToolResult(content=json.dumps(response, ensure_ascii=False))


async def execute_prepare_pptx_template_fill(arguments, context: ToolContext) -> ToolResult:
    project_id = str(arguments.get("project_id", "")).strip()
    project = _project_path(context, project_id)
    metadata = _read_json(project / "platform.json")
    if metadata.get("kind") != "template_fill":
        raise ValueError("项目不是模板填充项目")
    plan = arguments.get("fill_plan")
    if not isinstance(plan, dict) or plan.get("schema") != "template_fill_pptx_plan.v1":
        raise ValueError("fill_plan 不符合 template_fill_pptx_plan.v1")
    plan = dict(plan)
    plan["status"] = "draft"
    plan["source_pptx"] = str(project / "sources" / "source.pptx")
    plan_path = project / "analysis" / "fill_plan.json"
    _write_json(plan_path, plan)
    report_path = project / "analysis" / "check_report.json"
    await _run_script(
        "template_fill_pptx.py",
        ["check-plan", str(project / "analysis" / "slide_library.json"), str(plan_path), "-o", str(report_path)],
        check=False,
    )
    report = _read_json(report_path)
    summary = report.get("summary") or {}
    errors = int(summary.get("error", 0))
    warnings = int(summary.get("warn", 0))
    accepted = bool(arguments.get("accept_warnings", False))
    ready = errors == 0 and (warnings == 0 or accepted)
    metadata.update({
        "ready_for_approval": ready,
        "title": _display_title(str(arguments.get("title", "模板填充版")), "模板填充版"),
        "check_summary": summary,
    })
    _write_json(project / "platform.json", metadata)
    response = {
        "project_id": project_id,
        "ready_for_approval": ready,
        "check_summary": summary,
        "confirmation_summary": f"模板填充：{len(plan.get('slides', []))} 页，{warnings} 项警告，{errors} 项错误",
        "next_step": "通过校验后调用 apply_pptx_template_fill 并等待用户审批",
    }
    return ToolResult(content=json.dumps(response, ensure_ascii=False))


async def execute_apply_pptx_template_fill(arguments, context: ToolContext) -> ToolResult:
    project_id = str(arguments.get("project_id", "")).strip()
    project = _project_path(context, project_id)
    metadata = _read_json(project / "platform.json")
    if metadata.get("kind") != "template_fill" or not metadata.get("ready_for_approval"):
        raise ValueError("模板填充计划尚未通过校验")
    plan_path = project / "analysis" / "fill_plan.json"
    plan = _read_json(plan_path)
    plan["status"] = "confirmed"
    _write_json(plan_path, plan)
    output_stem = _safe_segment(str(metadata.get("title", "")), "template_filled")
    output_base = project / "exports" / f"{output_stem}.pptx"
    await _run_script(
        "template_fill_pptx.py",
        ["apply", str(project / "sources" / "source.pptx"), str(plan_path), "-o", str(output_base)],
        timeout=300,
    )
    await _run_script("template_fill_pptx.py", ["validate", str(project)], timeout=180)
    outputs = sorted((project / "exports").glob("*.pptx"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not outputs:
        raise RuntimeError("模板填充未生成 PPTX 产物")
    output = outputs[0]
    if _file_hash(project / "sources" / "source.pptx") != metadata["source_sha256"]:
        raise RuntimeError("源 PPTX 在执行过程中发生变化")
    preview = f"# {metadata.get('title', '模板填充版')}\n\n已按审批计划填充模板并通过回读校验。\n\n- 源文件：{metadata.get('source_name')}\n- 填充页数：{len(plan.get('slides', []))}\n- 源文件未被覆盖"
    return ToolResult(
        content="PPTX 模板填充已完成并通过校验。",
        data={"artifacts": [_artifact_candidate(output, str(metadata.get("title") or "模板填充版"), preview)]},
    )


def _copy_numbered_audio(project: Path, context: ToolContext) -> int:
    db = SessionLocal()
    try:
        attachments = db.query(AttachmentModel).filter(
            AttachmentModel.conversation_id == context.conversation_id,
        ).order_by(AttachmentModel.created_at).all()
        copied = 0
        for attachment in attachments:
            source = Path(attachment.storage_path)
            match = re.fullmatch(r"([1-9]\d{0,2})", source.stem)
            if not match or source.suffix.lower() not in AUDIO_SUFFIXES or not source.is_file():
                continue
            target = project / "audio" / f"{int(match.group(1)):03d}{source.suffix.lower()}"
            shutil.copy2(source, target)
            copied += 1
        return copied
    finally:
        db.close()


async def execute_prepare_pptx_enhancement(arguments, context: ToolContext) -> ToolResult:
    source, source_name = _resolve_source(context, str(arguments.get("source_id", "")).strip())
    transition = str(arguments.get("transition", "preserve")).strip() or "preserve"
    allowed_transitions = {"preserve", "none", "fade", "push", "wipe", "split", "reveal", "randomBars"}
    if transition not in allowed_transitions:
        raise ValueError("不支持的 PPTX 切换效果")
    duration = float(arguments.get("transition_duration", 0.5))
    if not 0.05 <= duration <= 10:
        raise ValueError("切换时长必须介于 0.05 到 10 秒")
    project_id = f"enhance_{uuid.uuid4().hex}"
    project = _conversation_root(context) / project_id
    project.parent.mkdir(parents=True, exist_ok=True)
    init_transition = "fade" if transition == "preserve" else transition
    await _run_script(
        "native_enhance_pptx.py",
        ["init", str(source), "--project-dir", str(project), "--name", project_id,
         "--transition", init_transition, "--transition-duration", str(duration)],
        timeout=300,
    )
    platform = {
        "schema": "platform_pptx_project.v1",
        "kind": "native_enhance",
        "conversation_id": context.conversation_id,
        "source_name": source_name,
        "source_sha256": _file_hash(source),
        "title": _display_title(str(arguments.get("title", "原生增强版")), "原生增强版"),
        "ready_for_approval": False,
    }
    _write_json(project / "platform.json", platform)
    project_config = _read_json(project / "project.json")
    slide_count = int(project_config.get("slide_count", 0))
    notes = arguments.get("notes") or []
    if not isinstance(notes, list):
        raise ValueError("notes 必须是数组")
    note_slides: set[int] = set()
    for item in notes:
        if not isinstance(item, dict):
            raise ValueError("每条备注必须包含 slide 和 text")
        slide = int(item.get("slide", 0))
        text = str(item.get("text", "")).strip()
        if slide < 1 or slide > slide_count or not text or slide in note_slides:
            raise ValueError("备注页码、内容或重复项无效")
        (project / "notes" / f"{slide:03d}.md").write_text(text + "\n", encoding="utf-8")
        note_slides.add(slide)
    use_audio = bool(arguments.get("use_uploaded_audio", False))
    audio_count = _copy_numbered_audio(project, context) if use_audio else 0
    plan_path = project / "analysis" / "enhancement_plan.json"
    plan = _read_json(plan_path)
    modules = plan["modules"]
    notes_enabled = bool(notes) or use_audio
    modules["notes"]["enabled"] = notes_enabled
    modules["audio"]["enabled"] = use_audio
    modules["timings"]["enabled"] = use_audio
    modules["transitions"]["enabled"] = transition != "preserve"
    for name in ("notes", "audio", "timings"):
        if not modules[name]["enabled"]:
            modules[name]["status"] = "disabled"
    if transition == "preserve":
        modules["transitions"]["status"] = "disabled"
    _write_json(plan_path, plan)
    await _run_script("native_enhance_pptx.py", ["plan", str(project)], check=True)
    plan = _read_json(plan_path)
    notes_complete = not notes_enabled or len(note_slides) == slide_count
    audio_complete = not use_audio or audio_count == slide_count
    ffprobe_ready = not use_audio or shutil.which("ffprobe") is not None
    ready = notes_complete and audio_complete and ffprobe_ready and (notes_enabled or transition != "preserve")
    platform.update({
        "ready_for_approval": ready,
        "notes_count": len(note_slides),
        "audio_count": audio_count,
        "slide_count": slide_count,
        "transition": transition,
        "uses_audio": use_audio,
    })
    _write_json(project / "platform.json", platform)
    blockers = []
    if not notes_complete:
        blockers.append("启用备注时必须覆盖全部页面")
    if not audio_complete:
        blockers.append("音频文件需按 001.mp3、002.mp3 覆盖全部页面")
    if not ffprobe_ready:
        blockers.append("音频时长校验需要 ffprobe")
    if not notes_enabled and transition == "preserve":
        blockers.append("未选择任何增强模块")
    response = {
        "project_id": project_id,
        "ready_for_approval": ready,
        "slide_count": slide_count,
        "notes_count": len(note_slides),
        "audio_count": audio_count,
        "transition": transition,
        "blockers": blockers,
        "confirmation_summary": f"原生增强：{slide_count} 页，备注 {len(note_slides)} 页，音频 {audio_count} 页，切换 {transition}",
        "next_step": "通过预检后调用 apply_pptx_enhancement 并等待用户审批",
    }
    return ToolResult(content=json.dumps(response, ensure_ascii=False))


async def execute_apply_pptx_enhancement(arguments, context: ToolContext) -> ToolResult:
    project_id = str(arguments.get("project_id", "")).strip()
    project = _project_path(context, project_id)
    platform = _read_json(project / "platform.json")
    if platform.get("kind") != "native_enhance" or not platform.get("ready_for_approval"):
        raise ValueError("PPTX 增强计划尚未通过预检")
    plan_path = project / "analysis" / "enhancement_plan.json"
    plan = _read_json(plan_path)
    plan["status"] = "confirmed"
    _write_json(plan_path, plan)
    materials = "all" if platform.get("uses_audio") else "notes"
    await _run_script("native_enhance_pptx.py", ["validate", str(project), "--materials", materials], timeout=180)
    output_stem = _safe_segment(str(platform.get("title", "")), "native_enhanced")
    output = project / "exports" / f"{output_stem}.pptx"
    await _run_script(
        "native_enhance_pptx.py",
        ["apply", str(project), "-o", str(output)],
        timeout=300,
    )
    await _run_script("native_enhance_pptx.py", ["validate", str(project), "--materials", materials], timeout=180)
    outputs = sorted((project / "exports").glob("*.pptx"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not outputs:
        raise RuntimeError("PPTX 增强未生成产物")
    source = next((project / "sources").glob("*.pptx"))
    if _file_hash(source) != platform["source_sha256"]:
        raise RuntimeError("源 PPTX 在执行过程中发生变化")
    preview = (
        f"# {platform.get('title', '原生增强版')}\n\n已完成原生 PPTX 增强并通过包结构回读校验。\n\n"
        f"- 页数：{platform.get('slide_count')}\n- 演讲备注：{platform.get('notes_count')} 页\n"
        f"- 音频：{platform.get('audio_count')} 页\n- 切换：{platform.get('transition')}\n- 源文件未被覆盖"
    )
    return ToolResult(
        content="PPTX 原生增强已完成并通过校验。",
        data={"artifacts": [_artifact_candidate(outputs[0], str(platform.get("title") or "原生增强版"), preview)]},
    )
