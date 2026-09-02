import asyncio
import os
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Awaitable, Callable, TypeVar

from deepagents.backends.protocol import ExecuteResponse, FileDownloadResponse, FileUploadResponse
from deepagents.backends.sandbox import BaseSandbox
from opensandbox import Sandbox
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.filesystem import WriteEntry


T = TypeVar("T")


class OpenSandboxBackend(BaseSandbox):
    """Deep Agents execution backend backed by a remote OpenSandbox instance."""

    enable_capture_offload = True

    def __init__(self, sandbox: Sandbox):
        self._sandbox = sandbox

    @property
    def id(self) -> str:
        return self._sandbox.id

    @staticmethod
    def _run_sync(factory: Callable[[], Awaitable[T]]) -> T:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(factory())
        raise RuntimeError("Synchronous sandbox operations cannot run inside an active event loop")

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return self._run_sync(lambda: self.aexecute(command, timeout=timeout))

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        options = RunCommandOpts(
            background=True,
            timeout=timedelta(seconds=timeout) if timeout and timeout > 0 else None,
        )
        try:
            execution = await self._sandbox.commands.run(command, opts=options)
        except Exception as exc:
            raise RuntimeError(f"OpenSandbox command execution failed: {exc}") from exc
        if not execution.id:
            raise RuntimeError("OpenSandbox did not return a background command ID")

        default_timeout = max(1, int(os.getenv("SANDBOX_COMMAND_TIMEOUT_SECONDS", "600")))
        deadline = asyncio.get_running_loop().time() + max(1, timeout or default_timeout)
        while True:
            try:
                status = await self._sandbox.commands.get_command_status(execution.id)
            except Exception as exc:
                raise RuntimeError("OpenSandbox command status could not be read") from exc
            if not status.running:
                break
            if asyncio.get_running_loop().time() >= deadline:
                try:
                    await self._sandbox.commands.interrupt(execution.id)
                finally:
                    raise RuntimeError("OpenSandbox command did not complete before the timeout")
            await asyncio.sleep(1)

        try:
            logs = await self._sandbox.commands.get_background_command_logs(execution.id)
        except Exception as exc:
            raise RuntimeError("OpenSandbox command completed but its logs could not be read") from exc
        output = logs.content
        if status.error and status.error not in output:
            output = f"{output}\n{status.error}".strip()
        exit_code = status.exit_code if status.exit_code is not None else (1 if status.error else 0)
        return ExecuteResponse(output=output, exit_code=exit_code, truncated=False)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self._run_sync(lambda: self.aupload_files(files))

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        if not files:
            return []
        entries = [WriteEntry(path=path, data=content, mode=644) for path, content in files]
        directories = sorted({
            str(PurePosixPath(path).parent)
            for path, _content in files
            if str(PurePosixPath(path).parent) not in {"", ".", "/"}
        })
        try:
            if directories:
                await self._sandbox.files.create_directories([
                    WriteEntry(path=directory, mode=755) for directory in directories
                ])
        except Exception as exc:
            return [FileUploadResponse(path=path, error=str(exc)) for path, _content in files]

        max_files = max(1, int(os.getenv("SANDBOX_UPLOAD_BATCH_FILES", "100")))
        max_bytes = max(1, int(os.getenv("SANDBOX_UPLOAD_BATCH_BYTES", str(20 * 1024 * 1024))))
        responses = []
        batch: list[WriteEntry] = []
        batch_bytes = 0

        async def flush() -> None:
            nonlocal batch, batch_bytes
            if not batch:
                return
            try:
                await self._sandbox.files.write_files(batch)
                responses.extend(FileUploadResponse(path=entry.path) for entry in batch)
            except Exception as exc:
                responses.extend(FileUploadResponse(path=entry.path, error=str(exc)) for entry in batch)
            batch = []
            batch_bytes = 0

        for entry in entries:
            entry_bytes = len(entry.data or b"")
            if batch and (len(batch) >= max_files or batch_bytes + entry_bytes > max_bytes):
                await flush()
            batch.append(entry)
            batch_bytes += entry_bytes
        await flush()
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self._run_sync(lambda: self.adownload_files(paths))

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses = []
        for path in paths:
            try:
                content = await self._sandbox.files.read_bytes(path)
                responses.append(FileDownloadResponse(path=path, content=content))
            except Exception as exc:
                responses.append(FileDownloadResponse(path=path, error=str(exc)))
        return responses

    async def aclose(self) -> None:
        await self._sandbox.close()
