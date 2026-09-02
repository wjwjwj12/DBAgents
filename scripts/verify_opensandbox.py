import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))
load_dotenv(PROJECT_ROOT / ".env", override=False)

from harness.tools import ToolContext, ToolRegistry
from langchain_core.messages import ToolMessage
from opensandbox.manager import SandboxManager
from orchestration.runner import DeepAgentRunner
from sandbox.factory import _opensandbox_config, get_thread_backend


def stream(*deltas):
    async def generate():
        for delta in deltas:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=delta)])
    return generate()


async def main() -> int:
    required = ("OPENSANDBOX_DOMAIN",)
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    os.environ["SANDBOX_PROVIDER"] = "opensandbox"

    token = uuid.uuid4().hex[:12]
    thread_a = f"opensandbox-e2e-a-{token}"
    thread_b = f"opensandbox-e2e-b-{token}"
    thread_agent = f"opensandbox-e2e-agent-{token}"
    sandbox_ids = set()
    clients = []
    results = []

    async def check(name, operation):
        started = time.monotonic()
        try:
            detail = await operation()
            results.append((name, True, detail, time.monotonic() - started))
        except Exception as exc:
            results.append((name, False, f"{type(exc).__name__}: {exc}", time.monotonic() - started))

    try:
        backend_a = await get_thread_backend(thread_a)
        clients.append(backend_a)

        async def shell_execution():
            response = await backend_a.aexecute("printf 'terminal-ready'")
            assert response.exit_code == 0 and response.output == "terminal-ready"
            return "shell command returned expected stdout"

        await check("01 shell command execution", shell_execution)
        original_backend_a_id = backend_a.id
        sandbox_ids.add(original_backend_a_id)

        async def file_round_trip():
            written = await backend_a.awrite("/workspace/e2e/marker.txt", "alpha")
            assert not written.error
            edited = await backend_a.aedit("/workspace/e2e/marker.txt", "alpha", "beta")
            assert not edited.error
            downloaded = await backend_a.adownload_files(["/workspace/e2e/marker.txt"])
            assert downloaded[0].content == b"beta"
            return "write, edit and binary download succeeded"

        await check("02 file write/edit/read", file_round_trip)

        await backend_a.aclose()
        clients.remove(backend_a)
        backend_a = await get_thread_backend(thread_a)
        clients.append(backend_a)

        async def thread_reuse():
            probe = await backend_a.aexecute("true")
            assert probe.exit_code == 0
            assert backend_a.id == original_backend_a_id
            downloaded = await backend_a.adownload_files(["/workspace/e2e/marker.txt"])
            assert downloaded[0].content == b"beta"
            return f"thread reused sandbox {backend_a.id} with state intact"

        await check("03 thread-scoped reuse", thread_reuse)

        backend_b = await get_thread_backend(thread_b)
        clients.append(backend_b)

        async def tenant_thread_isolation():
            assert backend_b.id != backend_a.id
            probe = await backend_b.aexecute("test ! -e /workspace/e2e/marker.txt")
            assert probe.exit_code == 0
            return "second thread received a different sandbox and no marker file"

        await check("04 cross-thread isolation", tenant_thread_isolation)
        sandbox_ids.add(backend_b.id)

        async def nonzero_exit():
            response = await backend_a.aexecute("sh -c 'exit 7'")
            assert response.exit_code == 7
            return "non-zero exit code 7 preserved"

        await check("05 command failure propagation", nonzero_exit)

        async def command_timeout():
            started = time.monotonic()
            try:
                response = await backend_a.aexecute("sleep 3", timeout=1)
                assert response.exit_code not in {None, 0}
            except RuntimeError:
                pass
            assert time.monotonic() - started < 5
            return "one-second timeout interrupted the long command"

        await check("06 command timeout", command_timeout)

        async def output_artifact_collection():
            runner = DeepAgentRunner(model=SimpleNamespace(), tools=ToolRegistry())
            runner.sandbox_backend = backend_a
            runner.sandbox_output_state = await runner._sandbox_outputs(backend_a)
            created = await backend_a.aexecute("mkdir -p /outputs && printf 'name,value\\nalpha,1\\n' > /outputs/e2e.csv")
            assert created.exit_code == 0
            await runner._collect_sandbox_artifacts()
            artifact = runner.result.outputs["artifacts"][0]
            assert artifact["artifact_type"] == "spreadsheet"
            assert artifact["content_bytes"].startswith(b"name,value")
            return "new /outputs CSV was downloaded as a platform artifact"

        await check("07 output artifact return", output_artifact_collection)

        async def agent_execute_tool():
            tool_call = SimpleNamespace(
                index=0,
                id="execute-e2e",
                function=SimpleNamespace(
                    name="execute",
                    arguments=json.dumps({"command": "printf 'agent-terminal-ready'"}),
                ),
            )
            create = SimpleNamespace(side_effect=[
                stream(SimpleNamespace(content=None, reasoning_content=None, tool_calls=[tool_call])),
                stream(SimpleNamespace(content="终端验证完成。", reasoning_content=None, tool_calls=None)),
            ])

            async def create_completion(**_kwargs):
                return create.side_effect.pop(0)

            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create_completion)))
            with tempfile.TemporaryDirectory() as skills_dir:
                runner = DeepAgentRunner(
                    model=client,
                    tools=ToolRegistry(),
                    skills_root=Path(skills_dir),
                    max_turns=3,
                )
                context = ToolContext(
                    run_id=f"run-{token}",
                    conversation_id=thread_agent,
                    thread_id=thread_agent,
                    tenant_id="e2e-tenant",
                    user_id="e2e-user",
                )
                [event async for event in runner.run([{"role": "user", "content": "执行终端验证"}], context)]
            tool_result = next(message for message in runner.messages if isinstance(message, ToolMessage))
            assert "agent-terminal-ready" in str(tool_result.content)
            assert runner.result.text == "终端验证完成。"
            cleanup_backend = await get_thread_backend(thread_agent)
            await cleanup_backend.aexecute("true")
            sandbox_ids.add(cleanup_backend.id)
            clients.append(cleanup_backend)
            return "Deep Agents execute tool ran inside OpenSandbox"

        await check("08 agent execute tool integration", agent_execute_tool)
    finally:
        for client in clients:
            try:
                await client.aclose()
            except Exception:
                pass
        manager = await SandboxManager.create(connection_config=_opensandbox_config())
        try:
            for sandbox_id in sandbox_ids:
                try:
                    await manager.kill_sandbox(sandbox_id)
                except Exception:
                    pass
        finally:
            await manager.close()

    for name, passed, detail, duration in results:
        print(f"{'PASS' if passed else 'FAIL'} {name} ({duration:.2f}s): {detail}")
    print(f"SUMMARY {sum(item[1] for item in results)}/{len(results)} scenarios passed")
    return 0 if results and all(item[1] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
