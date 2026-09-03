import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from sandbox.factory import _opensandbox_config, get_thread_backend
from sandbox.lazy_backend import LazySandboxBackend
from sandbox.opensandbox_backend import OpenSandboxBackend


class SandboxImageDefinitionTests(unittest.TestCase):
    def test_image_preinstalls_and_validates_curl(self):
        project_root = Path(__file__).resolve().parents[1]
        dockerfile = (project_root / "deploy" / "opensandbox" / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("apt-get install -y --no-install-recommends ca-certificates curl", dockerfile)
        self.assertIn("curl --version", dockerfile)


class FakeExecution:
    id = "execution-1"
    exit_code = 0
    error = None

    def __str__(self):
        return "command output"


class FakeCommands:
    def __init__(self):
        self.calls = []

    async def run(self, command, *, opts):
        self.calls.append((command, opts))
        return FakeExecution()

    async def get_command_status(self, execution_id):
        return SimpleNamespace(running=False, exit_code=0, error=None)

    async def get_background_command_logs(self, execution_id):
        return SimpleNamespace(content="command output")

    async def interrupt(self, execution_id):
        return None


class FakeFiles:
    def __init__(self):
        self.directories = []
        self.writes = []
        self.directory_batches = 0
        self.write_batches = 0

    async def create_directories(self, entries):
        self.directory_batches += 1
        self.directories.extend(entries)

    async def write_files(self, entries):
        self.write_batches += 1
        self.writes.extend(entries)

    async def read_bytes(self, path):
        if path.endswith(".status"):
            return b"0"
        if path.endswith(".stdout"):
            return b"command output"
        if path.endswith(".stderr"):
            return b""
        return f"download:{path}".encode()


class OpenSandboxBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_config_does_not_depend_on_cli_or_user_home(self):
        with patch.dict(
            os.environ,
            {"OPENSANDBOX_DOMAIN": "sandbox.internal:7499", "OPENSANDBOX_API_KEY": ""},
            clear=False,
        ):
            config = _opensandbox_config()

        self.assertEqual(config.domain, "sandbox.internal:7499")
        self.assertIsNone(config.api_key)

    async def test_command_execution_maps_to_deepagents_response(self):
        sandbox = SimpleNamespace(id="sandbox-1", commands=FakeCommands(), files=FakeFiles())
        backend = OpenSandboxBackend(sandbox)

        result = await backend.aexecute("printf ok", timeout=5)

        self.assertEqual(result.output, "command output")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(sandbox.commands.calls[0][0], "printf ok")
        self.assertTrue(sandbox.commands.calls[0][1].background)

    async def test_background_command_waits_for_official_status_and_reads_logs(self):
        class PollingCommands(FakeCommands):
            def __init__(self):
                super().__init__()
                self.statuses = [
                    SimpleNamespace(running=True, exit_code=None, error=None),
                    SimpleNamespace(running=False, exit_code=7, error=None),
                ]

            async def get_command_status(self, execution_id):
                return self.statuses.pop(0)

            async def get_background_command_logs(self, execution_id):
                return SimpleNamespace(content="failed output")

        sandbox = SimpleNamespace(id="sandbox-1", commands=PollingCommands(), files=FakeFiles())
        backend = OpenSandboxBackend(sandbox)

        with patch("sandbox.opensandbox_backend.asyncio.sleep", AsyncMock()) as sleep:
            result = await backend.aexecute("exit 7", timeout=5)

        sleep.assert_awaited_once_with(5.0)
        self.assertEqual(result.output, "failed output")
        self.assertEqual(result.exit_code, 7)

    async def test_unrelated_connection_failure_is_not_reported_as_completed(self):
        class FailedCommands(FakeCommands):
            async def run(self, command, *, opts):
                raise RuntimeError("connection refused")

        sandbox = SimpleNamespace(id="sandbox-1", commands=FailedCommands(), files=FakeFiles())
        backend = OpenSandboxBackend(sandbox)

        with self.assertRaisesRegex(RuntimeError, "OpenSandbox command execution failed"):
            await backend.aexecute("printf ok", timeout=5)

    async def test_hung_status_request_is_bounded_and_interrupted(self):
        class HungCommands(FakeCommands):
            async def get_command_status(self, execution_id):
                await asyncio.Event().wait()

        commands = HungCommands()
        sandbox = SimpleNamespace(id="sandbox-1", commands=commands, files=FakeFiles())
        backend = OpenSandboxBackend(sandbox)

        with self.assertRaisesRegex(RuntimeError, "status request timed out"):
            await backend.aexecute("printf ok", timeout=1)

        self.assertEqual(len(commands.calls), 1)

    async def test_upload_creates_parent_and_download_preserves_bytes(self):
        sandbox = SimpleNamespace(id="sandbox-1", commands=FakeCommands(), files=FakeFiles())
        backend = OpenSandboxBackend(sandbox)

        uploads = await backend.aupload_files([("/attachments/input.bin", b"binary")])
        downloads = await backend.adownload_files(["/outputs/result.bin"])

        self.assertIsNone(uploads[0].error)
        self.assertEqual(sandbox.files.directories[0].path, "/attachments")
        self.assertEqual(sandbox.files.writes[0].data, b"binary")
        self.assertEqual(downloads[0].content, b"download:/outputs/result.bin")

    async def test_upload_batches_multiple_files_into_one_request(self):
        sandbox = SimpleNamespace(id="sandbox-1", commands=FakeCommands(), files=FakeFiles())
        backend = OpenSandboxBackend(sandbox)

        uploads = await backend.aupload_files([
            ("/skills/a/SKILL.md", b"a"),
            ("/skills/a/scripts/run.py", b"b"),
            ("/skills/b/SKILL.md", b"c"),
        ])

        self.assertTrue(all(response.error is None for response in uploads))
        self.assertEqual(sandbox.files.directory_batches, 1)
        self.assertEqual(sandbox.files.write_batches, 1)
        self.assertEqual(len(sandbox.files.writes), 3)

    async def test_provider_defers_opensandbox_until_execute(self):
        expected_result = SimpleNamespace(output="ok", exit_code=0)
        remote = SimpleNamespace(
            id="remote-sandbox",
            aupload_files=AsyncMock(return_value=[]),
            aexecute=AsyncMock(return_value=expected_result),
            aclose=AsyncMock(),
        )
        with (
            patch.dict(os.environ, {"SANDBOX_PROVIDER": "opensandbox"}),
            patch("sandbox.factory._get_opensandbox_backend", AsyncMock(return_value=remote)) as create,
        ):
            actual = await get_thread_backend("tenant:user:conversation")
            self.assertIsInstance(actual, LazySandboxBackend)
            self.assertFalse(actual.is_active)
            create.assert_not_awaited()
            result = await actual.aexecute("printf ok")

        self.assertIs(result, expected_result)
        self.assertTrue(actual.is_active)
        create.assert_awaited_once_with("tenant:user:conversation")

    async def test_lazy_backend_queues_files_without_creating_sandbox(self):
        remote = SimpleNamespace(
            id="remote-sandbox",
            aupload_files=AsyncMock(return_value=[SimpleNamespace(error=None)]),
            aexecute=AsyncMock(return_value=SimpleNamespace(output="ok", exit_code=0)),
            aclose=AsyncMock(),
        )
        create = AsyncMock(return_value=remote)
        backend = LazySandboxBackend("thread", create)

        uploads = await backend.aupload_files([("/skills/example/run.py", b"print('ok')")])

        self.assertIsNone(uploads[0].error)
        create.assert_not_awaited()
        await backend.aexecute("python /skills/example/run.py")
        remote.aupload_files.assert_awaited_once()

    async def test_lazy_backend_lists_queued_files_without_graph_state(self):
        create = AsyncMock()
        backend = LazySandboxBackend("retry-thread", create)
        await backend.aupload_files([("/attachments/brief.md", b"brief")])

        root = await backend.als("/")
        attachments = await backend.als("/attachments")

        self.assertEqual([entry["path"] for entry in root.entries], ["/attachments/"])
        self.assertEqual([entry["path"] for entry in attachments.entries], ["/attachments/brief.md"])
        create.assert_not_awaited()

    async def test_lazy_backend_validates_preinstalled_ppt_runtime_before_first_command(self):
        remote = SimpleNamespace(
            id="remote-sandbox",
            aupload_files=AsyncMock(return_value=[SimpleNamespace(error=None)]),
            aexecute=AsyncMock(return_value=SimpleNamespace(output="ok", exit_code=0)),
            aclose=AsyncMock(),
        )
        backend = LazySandboxBackend("ppt-thread", AsyncMock(return_value=remote))
        await backend.aupload_files([("/skills/ppt-master/SKILL.md", b"ppt")])

        await backend.aexecute("python /skills/ppt-master/run.py")

        self.assertEqual(remote.aexecute.await_count, 2)
        setup_command = remote.aexecute.await_args_list[0].args[0]
        self.assertIn("import PIL, pptx, xlsxwriter, fitz, playwright", setup_command)
        self.assertNotIn("pip install", setup_command)
        self.assertEqual(remote.aexecute.await_args_list[1].args[0], "python /skills/ppt-master/run.py")

    async def test_lazy_backend_rejects_image_without_ppt_runtime(self):
        remote = SimpleNamespace(
            id="remote-sandbox",
            aupload_files=AsyncMock(return_value=[SimpleNamespace(error=None)]),
            aexecute=AsyncMock(return_value=SimpleNamespace(output="No module named pptx", exit_code=1)),
            aclose=AsyncMock(),
        )
        backend = LazySandboxBackend("broken-ppt-thread", AsyncMock(return_value=remote))
        await backend.aupload_files([("/skills/ppt-master/SKILL.md", b"ppt")])

        with self.assertRaisesRegex(RuntimeError, "OPENSANDBOX_IMAGE"):
            await backend.aexecute("python /skills/ppt-master/run.py")

        remote.aclose.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
