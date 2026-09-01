import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from capabilities.skill_installer import (  # noqa: E402
    SkillPackageError,
    install_preflighted_skill,
    install_skill_package,
    preflight_skill_archive,
    preflight_skill_files,
)
from capabilities.skill_registry import SkillRegistry  # noqa: E402


def make_skill_zip(manifest: dict, files: dict[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("sample/manifest.json", json.dumps(manifest, ensure_ascii=False))
        for name, value in (files or {"sample/SKILL.md": "# Sample"}).items():
            archive.writestr(name, value)
    return buffer.getvalue()


class SkillInstallerTests(unittest.TestCase):
    def test_valid_package_is_installed_and_discovered(self):
        manifest = {
            "name": "sample-skill",
            "description": "示例技能",
            "entrypoint": "SKILL.md",
            "tools": ["search_web"],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = install_skill_package(make_skill_zip(manifest), root, {"search_web"})
            registry = SkillRegistry(root)

            self.assertEqual(result["id"], "sample-skill")
            self.assertEqual(registry.get_skill("sample-skill").description, "示例技能")

    def test_path_traversal_is_rejected(self):
        manifest = {"name": "sample-skill", "description": "示例技能"}
        content = make_skill_zip(manifest, {
            "sample/SKILL.md": "# Sample",
            "../outside.txt": "unsafe",
        })
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(SkillPackageError, "不安全路径"):
                install_skill_package(content, Path(directory), set())

    def test_unknown_tool_is_rejected(self):
        manifest = {
            "name": "sample-skill",
            "description": "示例技能",
            "tools": ["run_arbitrary_code"],
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(SkillPackageError, "未注册工具"):
                install_skill_package(make_skill_zip(manifest), Path(directory), {"search_web"})

    def test_existing_name_is_rejected(self):
        manifest = {"name": "sample-skill", "description": "示例技能"}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(SkillPackageError, "已存在"):
                install_skill_package(
                    make_skill_zip(manifest),
                    Path(directory),
                    set(),
                    {"sample-skill"},
                )

    def test_folder_package_is_preflighted_and_installed(self):
        manifest = {
            "name": "folder-skill",
            "description": "文件夹技能",
            "entrypoint": "SKILL.md",
            "scripts": ["scripts/run.py"],
        }
        files = {
            "folder-skill/manifest.json": json.dumps(manifest).encode(),
            "folder-skill/SKILL.md": b"# Folder skill",
            "folder-skill/scripts/run.py": b"print('ok')\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            candidate = preflight_skill_files(files, set())
            result = install_preflighted_skill(candidate, Path(directory))
            self.assertEqual(result["id"], "folder-skill")
            self.assertTrue((Path(directory) / "folder-skill" / "SKILL.md").is_file())

    def test_invalid_python_preflight_leaves_no_skill_directory(self):
        manifest = {"name": "broken-skill", "description": "损坏技能"}
        files = {
            "broken-skill/manifest.json": json.dumps(manifest).encode(),
            "broken-skill/SKILL.md": b"# Broken",
            "broken-skill/broken.py": b"def broken(:\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(SkillPackageError, "静态预检"):
                preflight_skill_files(files, set())
            self.assertEqual(list(root.iterdir()), [])

    def test_missing_declared_resource_is_rejected(self):
        manifest = {
            "name": "resource-skill",
            "description": "资源技能",
            "resources": ["references/missing.md"],
        }
        files = {
            "resource-skill/manifest.json": json.dumps(manifest).encode(),
            "resource-skill/SKILL.md": b"# Resource",
        }
        with self.assertRaisesRegex(SkillPackageError, "声明的文件"):
            preflight_skill_files(files, set())

    def test_manifest_is_optional_when_archive_contains_skill_md(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("light-skill/skill.md", "# 轻量技能\n\n用于提取资料重点并生成结构清晰的内容摘要。")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = preflight_skill_archive(buffer.getvalue(), set(), package_hint="light-skill.zip")
            result = install_preflighted_skill(candidate, root)
            registry = SkillRegistry(root)
            self.assertEqual(result["id"], "light-skill")
            self.assertEqual(registry.get_skill("light-skill").description, "用于提取资料重点并生成结构清晰的内容摘要。")
            self.assertTrue((root / "light-skill" / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
