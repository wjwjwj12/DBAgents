import json
import os
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, List

from runtime_paths import DATA_DIR


READABLE_RESOURCE_SUFFIXES = {".md", ".json", ".txt", ".yaml", ".yml"}
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _relevance_terms(text: str) -> set[str]:
    normalized = text.casefold()
    terms = {
        token for token in re.findall(r"[a-z0-9]+", normalized)
        if len(token) >= 2
    }
    for block in re.findall(r"[\u4e00-\u9fff]+", normalized):
        terms.update(block[index:index + 2] for index in range(len(block) - 1))
    return terms


def _skill_intro(instructions: str, fallback: str, prefer_fallback: bool = False) -> str:
    text = instructions.strip()
    if text.startswith("---\n"):
        _front_matter, separator, remainder = text[4:].partition("\n---\n")
        if separator:
            text = remainder
    heading = ""
    paragraphs = []
    current = []
    in_code = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if line.startswith("#"):
            heading = heading or line.lstrip("# ").strip()
            continue
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        if line.startswith(("- ", "* ", "> ", "|", "<!--")) or re.match(r"^\d+[.)]\s", line):
            continue
        current.append(line)
    if current:
        paragraphs.append(" ".join(current))
    intro = next((paragraph for paragraph in paragraphs if len(paragraph) >= 12), "")
    secondary = fallback if prefer_fallback else heading
    tertiary = heading if prefer_fallback else fallback
    return (intro or secondary or tertiary).strip()[:180]


def _platform_description(root: Path) -> str:
    path = root / ".platform.json"
    if not path.is_file():
        return ""
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    return str(values.get("description") or "").strip()[:48] if isinstance(values, dict) else ""


def get_skill_root() -> Path:
    configured = os.getenv("APP_SKILLS_DIR", "").strip()
    if not configured:
        return PROJECT_ROOT / "skills"
    path = Path(configured).expanduser()
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()


def get_user_skill_root(tenant_id: str, user_id: str) -> Path:
    namespace = hashlib.sha256(f"{tenant_id}:{user_id}".encode("utf-8")).hexdigest()[:24]
    return DATA_DIR / "user_skills" / namespace


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    description: str
    instructions: str
    allowed_tools: FrozenSet[str]
    version: str = "1.0.0"
    root: Path | None = None
    aliases: FrozenSet[str] = frozenset()


class SkillRegistry:
    """Discovers packaged local Skills and their progressively loaded resources."""

    def __init__(
        self,
        skill_root: Path | None = None,
        additional_roots: List[Path] | None = None,
        load_instructions: bool = True,
    ):
        self.skill_root = skill_root or get_skill_root()
        self.skill_roots = [self.skill_root, *(additional_roots or [])]
        self.load_instructions = load_instructions
        self._skills, self._aliases = self._discover()

    def refresh(self) -> None:
        skills, aliases = self._discover()
        self._skills = skills
        self._aliases = aliases

    def _discover(self) -> tuple[Dict[str, SkillDefinition], Dict[str, str]]:
        skills: Dict[str, SkillDefinition] = {}
        aliases: Dict[str, str] = {}
        for skill_root in self.skill_roots:
            if not skill_root.exists():
                continue
            for manifest_path in sorted(skill_root.glob("*/manifest.json")):
                self._load_manifest(manifest_path, skills, aliases, self.load_instructions)

            for package_root in sorted(path for path in skill_root.iterdir() if path.is_dir()):
                if (package_root / "manifest.json").is_file():
                    continue
                entrypoint = next((path for path in package_root.iterdir() if path.is_file() and path.name.lower() == "skill.md"), None)
                if entrypoint is not None:
                    self._load_bare_package(entrypoint, skills, self.load_instructions)

            # Compatibility for standalone Skill declarations used by existing extensions.
            for path in sorted(skill_root.glob("*.md")):
                self._load_standalone(path, skills, self.load_instructions)
        return skills, aliases

    @staticmethod
    def _load_manifest(
        manifest_path: Path,
        skills: Dict[str, SkillDefinition],
        aliases: Dict[str, str],
        load_instructions: bool,
    ) -> None:
        values = json.loads(manifest_path.read_text(encoding="utf-8"))
        root = manifest_path.parent
        entrypoint = root / values.get("entrypoint", "SKILL.md")
        with entrypoint.open("r", encoding="utf-8") as source:
            entrypoint_text = source.read() if load_instructions else source.read(64 * 1024)
        instructions = entrypoint_text.strip() if load_instructions else ""
        name = values.get("name", root.name)
        skill_aliases = frozenset(str(item) for item in values.get("aliases", []))
        skills[name] = SkillDefinition(
            name=name,
            description=_platform_description(root) or str(values.get("description") or "").strip() or _skill_intro(entrypoint_text, name),
            version=str(values.get("version", "1.0.0")),
            instructions=instructions,
            allowed_tools=frozenset(values.get("tools", [])),
            root=root,
            aliases=skill_aliases,
        )
        for alias in skill_aliases:
            aliases[alias] = name

    @staticmethod
    def _load_standalone(path: Path, skills: Dict[str, SkillDefinition], load_instructions: bool) -> None:
        with path.open("r", encoding="utf-8") as source:
            raw = source.read() if load_instructions else source.read(64 * 1024)
        if not raw.startswith("---\n"):
            return
        front_matter, _, instructions = raw[4:].partition("\n---\n")
        values = {}
        for line in front_matter.splitlines():
            key, separator, value = line.partition(":")
            if separator:
                values[key.strip()] = value.strip()
        name = values.get("name", path.stem)
        tools = frozenset(item.strip() for item in values.get("tools", "").split(",") if item.strip())
        skills.setdefault(name, SkillDefinition(
            name=name,
            description=_skill_intro(instructions, values.get("description", name), prefer_fallback=True),
            version=values.get("version", "1.0.0"),
            instructions=instructions.strip() if load_instructions else "",
            allowed_tools=tools,
            root=path.parent,
        ))

    @staticmethod
    def _load_bare_package(path: Path, skills: Dict[str, SkillDefinition], load_instructions: bool) -> None:
        with path.open("r", encoding="utf-8") as source:
            preview = source.read() if load_instructions else source.read(64 * 1024)
        instructions = preview.strip()
        skills.setdefault(path.parent.name, SkillDefinition(
            name=path.parent.name,
            description=_platform_description(path.parent) or _skill_intro(instructions, path.parent.name),
            instructions=instructions if load_instructions else "",
            allowed_tools=frozenset(),
            root=path.parent,
        ))

    def get_skill(self, name: str) -> SkillDefinition | None:
        return self._skills.get(self._aliases.get(name, name))

    def recommend_skills(
        self,
        query: str,
        *,
        exclude: set[str] | None = None,
        limit: int = 2,
    ) -> List[SkillDefinition]:
        """Return task-relevant Skills without package-specific routing rules."""
        normalized_query = query.casefold()
        query_terms = _relevance_terms(query)
        excluded = {self._aliases.get(name, name) for name in (exclude or set())}
        ranked = []
        for skill in self._skills.values():
            if skill.name in excluded:
                continue
            names = {skill.name, *skill.aliases}
            name_hits = sum(
                1 for name in names
                if len(name) >= 2 and name.casefold() in normalized_query
            )
            searchable = "\n".join((
                skill.name,
                " ".join(skill.aliases),
                skill.description,
            ))
            overlap = len(query_terms & _relevance_terms(searchable))
            score = name_hits * 10 + overlap * 2
            if score >= 2:
                ranked.append((score, skill.name, skill))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        if not ranked:
            return []
        minimum_score = max(2, (ranked[0][0] + 1) // 2)
        return [
            item[2] for item in ranked
            if item[0] >= minimum_score
        ][:max(0, limit)]

    def read_resource(self, skill_name: str, resource: str) -> str:
        skill = self.get_skill(skill_name)
        if skill is None or skill.root is None:
            raise ValueError(f"Skill 不存在: {skill_name}")
        relative = Path(resource)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("资源路径必须位于 Skill 目录内")
        target = (skill.root / relative).resolve()
        root = skill.root.resolve()
        if target != root and root not in target.parents:
            raise ValueError("资源路径必须位于 Skill 目录内")
        if target.suffix.lower() not in READABLE_RESOURCE_SUFFIXES or not target.is_file():
            raise ValueError("资源不存在或不允许加载")
        return target.read_text(encoding="utf-8")

    def list_skills(self) -> List[Dict[str, object]]:
        return [
            {
                "id": item.name,
                "package": item.root.name if item.root else item.name,
                "description": item.description[:72],
                "version": item.version,
                "aliases": sorted(item.aliases),
                "tools": sorted(item.allowed_tools),
            }
            for item in self._skills.values()
        ]

    def catalog_prompt(self) -> str:
        return "\n".join(f"- {skill.name}: {skill.description}" for skill in self._skills.values())
