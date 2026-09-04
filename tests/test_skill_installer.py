import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


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
from capabilities.remote_skill_installer import SkillDownloadError, download_skill_archive  # noqa: E402
from harness.tools import ToolContext  # noqa: E402
import agent  # noqa: E402


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

    def test_installed_directory_uses_name_declared_by_skill_md(self):
        original = "---\nname: aligned-skill\ndescription: 对齐名称\n---\n按说明执行。"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("vendor-package-1-2-3/SKILL.md", original)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = preflight_skill_archive(buffer.getvalue(), set(), package_hint="vendor-package-1-2-3.zip")
            result = install_preflighted_skill(candidate, root)

            self.assertEqual(result["id"], "aligned-skill")
            self.assertTrue((root / "aligned-skill" / "SKILL.md").is_file())
            self.assertFalse((root / "vendor-package-1-2-3").exists())
            self.assertEqual((root / "aligned-skill" / "SKILL.md").read_text(encoding="utf-8"), original)

    def test_remote_skill_url_rejects_plain_http_by_default(self):
        with self.assertRaisesRegex(SkillDownloadError, "HTTPS"):
            from capabilities.remote_skill_installer import _validate_source_url

            _validate_source_url("http://example.com/skill.zip")


class RemoteSkillInstallerTests(unittest.IsolatedAsyncioTestCase):
    async def test_archive_is_downloaded_inside_sandbox(self):
        content = make_skill_zip({"name": "remote-skill", "description": "远程技能"})

        class FakeSandbox:
            is_active = True

            def __init__(self):
                self.commands = []

            async def aexecute(self, command, timeout=None):
                self.commands.append(command)
                return SimpleNamespace(exit_code=0, output="")

            async def adownload_files(self, _paths):
                return [SimpleNamespace(error=None, content=content)]

        sandbox = FakeSandbox()
        with patch.dict("os.environ", {"SKILL_INSTALL_ALLOWED_HOSTS": "skills.internal", "SKILL_INSTALL_ALLOW_HTTP": "1"}, clear=False):
            downloaded = await download_skill_archive(
                sandbox,
                "http://skills.internal/remote.zip",
                max_bytes=1024 * 1024,
            )

        self.assertEqual(downloaded, content)
        self.assertIn("curl", sandbox.commands[0])
        self.assertIn("--max-redirs 0", sandbox.commands[0])

    async def test_agent_installs_preflighted_archive_for_current_user(self):
        content = make_skill_zip({"name": "remote-skill", "description": "远程技能"})
        context = ToolContext(tenant_id="tenant-a", user_id="user-a", sandbox_backend=object())
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(agent, "get_user_skill_root", return_value=Path(directory)), \
                patch.object(agent, "download_skill_archive", return_value=content):
            result = await agent._execute_install_skill_from_url(
                {"url": "https://skills.example/remote.zip"},
                context,
            )

        self.assertIn("已通过预检并安装", result.content)
        self.assertEqual(result.data["installed_skill"]["id"], "remote-skill")


if __name__ == "__main__":
    unittest.main()
