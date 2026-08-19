import sys
import tempfile
import unittest
import json
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

        initial_names = {
            item["function"]["name"]
            for item in agent.tool_registry.openai_definitions(set(agent.BOOTSTRAP_TOOLS))
        }
        self.assertIn("search_web", initial_names)

    async def test_current_time_tool_is_free_and_available_by_default(self):
        import agent

        initial_names = {
            item["function"]["name"]
            for item in agent.tool_registry.openai_definitions(set(agent.BOOTSTRAP_TOOLS))
        }
        self.assertIn("get_current_time", initial_names)

        result = await agent.tool_registry.execute(
            "get_current_time",
            {"utc_offset": "+08:00", "location": "上海"},
            ToolContext(allowed_tools=set(agent.BOOTSTRAP_TOOLS)),
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

    async def test_loading_skill_enables_its_tools_at_runtime(self):
        import agent

        context = ToolContext(allowed_tools=set(agent.BOOTSTRAP_TOOLS))
        initial_names = {
            item["function"]["name"]
            for item in agent.tool_registry.openai_definitions(context.allowed_tools)
        }
        self.assertNotIn("generate_ppt", initial_names)

        await agent.tool_registry.execute(
            "load_skill",
            {"skill_name": "ppt"},
            context,
        )
        loaded_names = {
            item["function"]["name"]
            for item in agent.tool_registry.openai_definitions(context.allowed_tools)
        }
        self.assertIn("generate_ppt", loaded_names)
        self.assertIn("load_skill_resource", loaded_names)
        self.assertIn("analyze_pptx_template", loaded_names)
        self.assertIn("apply_pptx_template_fill", loaded_names)
        self.assertIn("prepare_pptx_enhancement", loaded_names)
        self.assertIn("apply_pptx_enhancement", loaded_names)
        self.assertIn("ppt", context.loaded_skills)

        self.assertEqual(
            agent.tool_registry.get("apply_pptx_template_fill").permission,
            PermissionDecision.ASK,
        )
        self.assertEqual(
            agent.tool_registry.get("apply_pptx_enhancement").permission,
            PermissionDecision.ASK,
        )

        resource = await agent.tool_registry.execute(
            "load_skill_resource",
            {"skill_name": "ppt-master", "resource": "workflows/routing.md"},
            context,
        )
        self.assertIn("Generate PPTX", resource.content)

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
        self.assertIn("generate_ppt", skill.allowed_tools)
        self.assertEqual(skill.root.name, "ppt-master")
        self.assertFalse((Path(__file__).resolve().parents[1] / "guizang-ppt-skill-main").exists())


if __name__ == "__main__":
    unittest.main()
