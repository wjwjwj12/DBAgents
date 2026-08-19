import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, List


READABLE_RESOURCE_SUFFIXES = {".md", ".json", ".txt", ".yaml", ".yml"}


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

    def __init__(self, skill_root: Path | None = None):
        self.skill_root = skill_root or Path(__file__).resolve().parent / "skills"
        self._skills, self._aliases = self._discover()

    def _discover(self) -> tuple[Dict[str, SkillDefinition], Dict[str, str]]:
        skills: Dict[str, SkillDefinition] = {}
        aliases: Dict[str, str] = {}
        if not self.skill_root.exists():
            return skills, aliases

        for manifest_path in sorted(self.skill_root.glob("*/manifest.json")):
            values = json.loads(manifest_path.read_text(encoding="utf-8"))
            root = manifest_path.parent
            entrypoint = root / values.get("entrypoint", "SKILL.md")
            instructions = entrypoint.read_text(encoding="utf-8").strip()
            name = values.get("name", root.name)
            skill_aliases = frozenset(str(item) for item in values.get("aliases", []))
            skills[name] = SkillDefinition(
                name=name,
                description=values.get("description", name),
                version=str(values.get("version", "1.0.0")),
                instructions=instructions,
                allowed_tools=frozenset(values.get("tools", [])),
                root=root,
                aliases=skill_aliases,
            )
            for alias in skill_aliases:
                aliases[alias] = name

        # Compatibility for standalone Skill declarations used by existing extensions.
        for path in sorted(self.skill_root.glob("*.md")):
            raw = path.read_text(encoding="utf-8")
            if not raw.startswith("---\n"):
                continue
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
                description=values.get("description", name),
                version=values.get("version", "1.0.0"),
                instructions=instructions.strip(),
                allowed_tools=tools,
                root=path.parent,
            ))
        return skills, aliases

    def get_skill(self, name: str) -> SkillDefinition | None:
        return self._skills.get(self._aliases.get(name, name))

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

    def list_skills(self) -> List[Dict[str, str]]:
        return [{"id": item.name, "description": item.description, "version": item.version} for item in self._skills.values()]

    def catalog_prompt(self) -> str:
        return "\n".join(f"- {skill.name}: {skill.description}" for skill in self._skills.values())
