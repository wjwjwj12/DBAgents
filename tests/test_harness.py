import sys
import tempfile
import unittest
import json
import os
import subprocess
from unittest.mock import patch
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from harness.tools import (  # noqa: E402
    PermissionDecision,
    ToolContext,
    ToolDefinition,
    ToolPermissionError,
    ToolRegistry,
    ToolResult,
)
from capabilities.skill_registry import SkillRegistry  # noqa: E402


class ToolRegistryTests(unittest.IsolatedAsyncioTestCase):
    def test_dynamic_plan_tool_is_available(self):
        import agent

        definition_names = {
            item["function"]["name"]
            for item in agent.tool_registry.openai_definitions()
        }
        self.assertIn("update_plan", definition_names)

    def test_web_search_is_available_without_loading_a_skill(self):
        import agent

        initial_names = {item["function"]["name"] for item in agent.tool_registry.openai_definitions()}
        self.assertIn("search_web", initial_names)

    async def test_current_time_tool_is_free_and_available_by_default(self):
        import agent

        initial_names = {item["function"]["name"] for item in agent.tool_registry.openai_definitions()}
        self.assertIn("get_current_time", initial_names)

        result = await agent.tool_registry.execute(
            "get_current_time",
            {"utc_offset": "+08:00", "location": "上海"},
            ToolContext(),
        )
        payload = json.loads(result.content)
        self.assertEqual(payload["location"], "上海")
        self.assertEqual(payload["timezone"], "UTC+08:00")
        self.assertTrue(payload["datetime"].endswith("+08:00"))

    async def test_current_time_tool_rejects_invalid_offset(self):
        import agent

        result = await agent._execute_get_current_time({"utc_offset": "+25:00"}, None)
        self.assertIn("超出有效范围", result.content)

    async def test_bocha_search_request_and_normalized_result(self):
        import agent

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "code": 200,
                    "data": {
                        "webPages": {
                            "value": [{
                                "name": "官方结果",
                                "url": "https://example.com/source",
                                "siteName": "示例站点",
                                "siteIcon": "https://example.com/favicon.ico",
                                "dateLastCrawled": "2026-08-14T00:00:00+08:00",
                                "snippet": "结果摘要",
                                "summary": "模型友好摘要",
                            }]
                        }
                    },
                }

        class FakeClient:
            def __init__(self):
                self.request = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, url, **kwargs):
                self.request = (url, kwargs)
                return FakeResponse()

        client = FakeClient()
        with (
            patch.dict("os.environ", {"BOCHA_API_KEY": "test-key"}),
            patch.object(agent.httpx, "AsyncClient", return_value=client),
        ):
            result = json.loads(await agent._tool_search_web(
                "最新交通政策",
                freshness="oneWeek",
                count=3,
            ))

        self.assertEqual(client.request[0], agent.BOCHA_SEARCH_URL)
        self.assertEqual(client.request[1]["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(client.request[1]["json"]["freshness"], "oneWeek")
        self.assertEqual(result["results"][0]["title"], "官方结果")
        self.assertEqual(result["results"][0]["url"], "https://example.com/source")
        self.assertEqual(result["results"][0]["site_icon"], "https://example.com/favicon.ico")
        self.assertEqual(result["results"][0]["published_at"], "2026-08-14T00:00:00+08:00")
        self.assertEqual(result["result_count"], 1)

    async def test_deep_agents_replaces_custom_skill_loader(self):
        import agent
        from orchestration.runner import DeepAgentRunner

        names = agent.tool_registry.names()
        self.assertNotIn("load_skill", names)
        self.assertNotIn("load_skill_resource", names)
        self.assertNotIn("generate_ppt", names)
        runner = object.__new__(DeepAgentRunner)
        runner.skills_root = Path(__file__).resolve().parents[1] / "skills"
        backend = await runner._backend("test-thread")
        skill = backend.download_files(["/skills/ppt-master/SKILL.md"])[0]
        self.assertIsNone(skill.error)
        self.assertIn(b"name: ppt-master", skill.content)

        self.assertEqual(
            agent.tool_registry.get("apply_pptx_template_fill").permission,
            PermissionDecision.ASK,
        )
        self.assertEqual(
            agent.tool_registry.get("apply_pptx_enhancement").permission,
            PermissionDecision.ASK,
        )


    async def test_registered_tool_is_exposed_and_executed(self):
        async def echo(arguments, _context):
            return ToolResult(content=arguments["text"])

        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="echo",
            description="Echo text",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            handler=echo,
        ))

        self.assertEqual(registry.openai_definitions()[0]["function"]["name"], "echo")
        result = await registry.execute("echo", {"text": "hello"}, ToolContext())
        self.assertEqual(result.content, "hello")

    async def test_approval_policy_blocks_unapproved_tool(self):
        async def dangerous(_arguments, _context):
            return ToolResult(content="should not run")

        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="dangerous",
            description="Dangerous action",
            parameters={"type": "object", "properties": {}},
            handler=dangerous,
            permission=PermissionDecision.ASK,
        ))

        with self.assertRaises(ToolPermissionError):
            await registry.execute("dangerous", {}, ToolContext())

        result = await registry.execute(
            "dangerous",
            {},
            ToolContext(approved_tools={"dangerous"}),
        )
        self.assertEqual(result.content, "should not run")

    async def test_skill_allowlist_hides_and_blocks_other_tools(self):
        async def echo(_arguments, _context):
            return ToolResult(content="echo")

        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="echo",
            description="Echo",
            parameters={"type": "object", "properties": {}},
            handler=echo,
        ))

        context = ToolContext(allowed_tools={"search_web"})
        self.assertEqual(registry.openai_definitions(context.allowed_tools), [])
        with self.assertRaises(ToolPermissionError):
            await registry.execute("echo", {}, context)

    async def test_audit_only_records_authorized_tool_execution(self):
        async def echo(_arguments, _context):
            return ToolResult(content="ok")

        registry = ToolRegistry()
        registry.register(ToolDefinition(name="echo", description="Echo", parameters={"type": "object", "properties": {}}, handler=echo))
        audit = []
        await registry.execute("echo", {}, ToolContext(allowed_tools={"echo"}, tool_audit=lambda event, payload: audit.append((event, payload))))
        self.assertEqual([event for event, _payload in audit], ["tool_authorized", "tool_result"])

    def test_skill_registry_discovers_definition_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_file = Path(temp_dir) / "example.md"
            skill_file.write_text("---\nname: example\nversion: 1\ndescription: 示例\ntools: echo\n---\n按步骤执行。\n", encoding="utf-8")
            registry = SkillRegistry(Path(temp_dir))
        skill = registry.get_skill("example")
        self.assertIsNotNone(skill)
        self.assertEqual(skill.allowed_tools, {"echo"})

    def test_skill_registry_discovers_packages_and_blocks_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "example-skill"
            root.mkdir()
            (root / "manifest.json").write_text(json.dumps({
                "name": "example",
                "aliases": ["example-master"],
                "entrypoint": "SKILL.md",
                "tools": ["echo", "load_skill_resource"],
            }), encoding="utf-8")
            (root / "SKILL.md").write_text("执行入口。", encoding="utf-8")
            (root / "routing.md").write_text("路由说明。", encoding="utf-8")
            registry = SkillRegistry(Path(temp_dir))

            self.assertEqual(registry.get_skill("example-master").name, "example")
            self.assertEqual(registry.read_resource("example", "routing.md"), "路由说明。")
            with self.assertRaises(ValueError):
                registry.read_resource("example", "../secret.txt")

    def test_project_uses_ppt_master_package(self):
        registry = SkillRegistry()
        skill = registry.get_skill("ppt-master")
        self.assertIsNotNone(skill)
        self.assertEqual(skill.name, "ppt")
        self.assertEqual(skill.version, "4.7.0")
        self.assertEqual(skill.root.name, "ppt-master")
        self.assertFalse((Path(__file__).resolve().parents[1] / "guizang-ppt-skill-main").exists())

    def test_ppt_master_assets_can_be_loaded_from_external_home(self):
        skill_root = BACKEND_DIR.parent / "skills" / "ppt-master"
        with tempfile.TemporaryDirectory() as temp_dir:
            external = Path(temp_dir) / "ppt-master"
            icon = external / "templates" / "icons" / "tabler-outline" / "home.svg"
            icon.parent.mkdir(parents=True)
            icon.write_text('<svg viewBox="0 0 24 24"><path d="M0 0"/></svg>', encoding="utf-8")
            project = Path(temp_dir) / "project"
            project.mkdir()
            environment = os.environ.copy()
            environment["PPT_MASTER_HOME"] = str(external)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(skill_root / "scripts" / "icon_sync.py"),
                    str(project),
                    "tabler-outline/home",
                ],
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue((project / "icons" / "tabler-outline" / "home.svg").is_file())


if __name__ == "__main__":
    unittest.main()
