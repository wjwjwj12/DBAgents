import io
import json
import re
import shutil
import stat
import uuid
import zipfile
import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping


SKILL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
MAX_ARCHIVE_MEMBERS = 2_000
MAX_EXTRACTED_BYTES = 200 * 1024 * 1024


class SkillPackageError(ValueError):
    pass


@dataclass(frozen=True)
class PreflightedSkill:
    metadata: dict[str, object]
    files: Mapping[PurePosixPath, bytes]
    checks: tuple[str, ...]


def _safe_member_path(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise SkillPackageError(f"技能包包含不安全路径: {name}")
    if path.parts[0].endswith(":"):
        raise SkillPackageError(f"技能包包含绝对路径: {name}")
    return path


def _validate_identifier(value: object, field: str) -> str:
    identifier = str(value or "").strip()
    if not SKILL_NAME_PATTERN.fullmatch(identifier):
        raise SkillPackageError(f"{field} 只能使用小写字母、数字、短横线或下划线，长度为 2-64")
    return identifier


def _normalize_package_files(files: Mapping[str, bytes]) -> tuple[dict[PurePosixPath, bytes], str]:
    if not files or len(files) > MAX_ARCHIVE_MEMBERS:
        raise SkillPackageError("技能包为空或文件数量超过限制")
    if sum(len(value) for value in files.values()) > MAX_EXTRACTED_BYTES:
        raise SkillPackageError("技能包解压后的大小超过限制")
    normalized: dict[PurePosixPath, bytes] = {}
    for name, content in files.items():
        path = _safe_member_path(name)
        if path in normalized:
            raise SkillPackageError(f"技能包包含重复文件: {name}")
        normalized[path] = content

    manifests = [path for path in normalized if path.name.lower() == "manifest.json"]
    entrypoints = [path for path in normalized if path.name.lower() == "skill.md"]
    if len(manifests) > 1:
        raise SkillPackageError("技能包最多只能包含一个 manifest.json")
    if not manifests and len(entrypoints) != 1:
        raise SkillPackageError("技能包未包含 manifest.json 时，必须且只能包含一个 SKILL.md")
    prefix = (manifests[0] if manifests else entrypoints[0]).parent
    if len(prefix.parts) > 1:
        raise SkillPackageError("manifest.json 只能位于根目录或单个顶层文件夹内")
    if prefix.parts and any(path.parts[:1] != prefix.parts for path in normalized):
        raise SkillPackageError("技能包只能包含技能顶层文件夹内的文件")
    package_name = prefix.name if prefix.parts else ""
    return {
        PurePosixPath(*path.parts[len(prefix.parts):]): content
        for path, content in normalized.items()
    }, package_name


def _fallback_skill_name(hint: str, files: Mapping[PurePosixPath, bytes]) -> str:
    stem = Path(hint).stem.lower()
    slug = re.sub(r"[^a-z0-9_-]+", "-", stem).strip("-_")
    if slug and not slug[0].isalpha():
        slug = f"skill-{slug}"
    if len(slug) < 2:
        digest = hashlib.sha256(b"".join(files[path] for path in sorted(files))).hexdigest()[:10]
        slug = f"skill-{digest}"
    return slug[:64]


def _bare_skill_description(instructions: str, name: str) -> str:
    if instructions.startswith("---\n"):
        _front_matter, separator, remainder = instructions[4:].partition("\n---\n")
        if separator:
            instructions = remainder
    heading = ""
    paragraph = []
    for line in instructions.splitlines():
        text = line.strip()
        if text.startswith("#"):
            heading = heading or text.lstrip("# ").strip()
            continue
        if not text:
            if paragraph:
                break
            continue
        if text != "---" and not text.startswith(("- ", "* ", "> ", "|", "```")):
            paragraph.append(text)
    return (" ".join(paragraph) or heading or name)[:180]


def _validate_declared_paths(manifest: dict, files: Mapping[PurePosixPath, bytes], field: str) -> None:
    declared = manifest.get(field, [])
    if declared is None:
        return
    if not isinstance(declared, list) or any(not isinstance(item, str) for item in declared):
        raise SkillPackageError(f"{field} 必须是字符串数组")
    for item in declared:
        path = _safe_member_path(item)
        if path not in files:
            raise SkillPackageError(f"技能包缺少 manifest 声明的文件: {item}")


def preflight_skill_files(
    files: Mapping[str, bytes],
    registered_tools: Iterable[str],
    existing_identifiers: Iterable[str] = (),
    package_hint: str = "",
) -> PreflightedSkill:
    package_files, folder_hint = _normalize_package_files(files)
    manifest_path = next((path for path in package_files if path.name.lower() == "manifest.json"), None)
    if manifest_path is not None:
        try:
            manifest = json.loads(package_files[manifest_path].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SkillPackageError("manifest.json 不是有效的 UTF-8 JSON") from exc
        if not isinstance(manifest, dict):
            raise SkillPackageError("manifest.json 顶层必须是对象")
    else:
        manifest = {}

    name = _validate_identifier(manifest.get("name"), "name") if manifest else _fallback_skill_name(package_hint or folder_hint, package_files)
    version = str(manifest.get("version") or "1.0.0").strip()
    if not version or len(version) > 50:
        raise SkillPackageError("version 不能为空且不能超过 50 个字符")

    aliases_value = manifest.get("aliases", [])
    tools_value = manifest.get("tools", [])
    if not isinstance(aliases_value, list) or not isinstance(tools_value, list):
        raise SkillPackageError("aliases 和 tools 必须是字符串数组")
    aliases = {_validate_identifier(item, "alias") for item in aliases_value}
    tools = {str(item).strip() for item in tools_value}
    if any(not item for item in tools):
        raise SkillPackageError("tools 不能包含空值")
    unknown_tools = tools - set(registered_tools)
    if unknown_tools:
        raise SkillPackageError(f"技能包引用了未注册工具: {', '.join(sorted(unknown_tools))}")

    conflicts = ({name} | aliases) & set(existing_identifiers)
    if conflicts:
        raise SkillPackageError(f"技能名称或别名已存在: {', '.join(sorted(conflicts))}")

    entrypoint = _safe_member_path(str(manifest.get("entrypoint") or "SKILL.md"))
    if entrypoint not in package_files:
        entrypoint = next((path for path in package_files if path.name.lower() == "skill.md"), entrypoint)
    if entrypoint.suffix.lower() != ".md":
        raise SkillPackageError("entrypoint 必须是 Markdown 文件")
    if entrypoint not in package_files:
        raise SkillPackageError("技能包缺少 entrypoint 文件")
    try:
        instructions = package_files[entrypoint].decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise SkillPackageError("entrypoint 不是有效的 UTF-8 文本") from exc
    if not instructions:
        raise SkillPackageError("entrypoint 不能为空")
    if manifest_path is None and entrypoint != PurePosixPath("SKILL.md"):
        package_files[PurePosixPath("SKILL.md")] = package_files.pop(entrypoint)
        entrypoint = PurePosixPath("SKILL.md")
    description = str(manifest.get("description") or _bare_skill_description(instructions, name)).strip()
    if not description or len(description) > 500:
        raise SkillPackageError("description 不能为空且不能超过 500 个字符")

    _validate_declared_paths(manifest, package_files, "resources")
    _validate_declared_paths(manifest, package_files, "scripts")
    for path, content in package_files.items():
        try:
            if path.suffix.lower() == ".py":
                compile(content.decode("utf-8"), path.as_posix(), "exec")
            elif path.suffix.lower() == ".json":
                json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, SyntaxError, json.JSONDecodeError) as exc:
            raise SkillPackageError(f"文件无法通过静态预检: {path.as_posix()} ({exc})") from exc

    metadata = {
        "id": name,
        "package": name,
        "description": description,
        "version": version,
        "aliases": sorted(aliases),
        "tools": sorted(tools),
    }
    return PreflightedSkill(
        metadata=metadata,
        files=package_files,
        checks=("目录结构", "主文件", "工具权限", "资源声明", "脚本语法"),
    )


def preflight_skill_archive(
    content: bytes,
    registered_tools: Iterable[str],
    existing_identifiers: Iterable[str] = (),
    package_hint: str = "",
) -> PreflightedSkill:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, ValueError) as exc:
        raise SkillPackageError("上传文件不是有效的 ZIP 技能包") from exc
    with archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if not members or len(members) > MAX_ARCHIVE_MEMBERS:
            raise SkillPackageError("技能包为空或文件数量超过限制")
        if sum(item.file_size for item in members) > MAX_EXTRACTED_BYTES:
            raise SkillPackageError("技能包解压后的大小超过限制")
        files: dict[str, bytes] = {}
        for member in members:
            if member.flag_bits & 0x1:
                raise SkillPackageError("不支持加密的技能包")
            mode = member.external_attr >> 16
            if mode and stat.S_ISLNK(mode):
                raise SkillPackageError("技能包不能包含符号链接")
            if member.filename in files:
                raise SkillPackageError(f"技能包包含重复文件: {member.filename}")
            files[member.filename] = archive.read(member)
    return preflight_skill_files(files, registered_tools, existing_identifiers, package_hint)


def install_preflighted_skill(candidate: PreflightedSkill, skill_root: Path) -> dict[str, object]:
    name = str(candidate.metadata["id"])
    skill_root.mkdir(parents=True, exist_ok=True)
    target = skill_root / name
    if target.exists():
        raise SkillPackageError(f"技能目录已存在: {name}")
    temporary = skill_root / f".upload-{uuid.uuid4().hex}"
    temporary.mkdir()
    try:
        for path, content in candidate.files.items():
            destination = temporary.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        (temporary / ".platform.json").write_text(
            json.dumps({"description": candidate.metadata.get("description", "")}, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.rename(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {**candidate.metadata, "preflight": list(candidate.checks)}


def install_skill_package(
    content: bytes,
    skill_root: Path,
    registered_tools: Iterable[str],
    existing_identifiers: Iterable[str] = (),
) -> dict[str, object]:
    candidate = preflight_skill_archive(content, registered_tools, existing_identifiers)
    return install_preflighted_skill(candidate, skill_root)
