import asyncio
import base64
import logging
import os
import uuid
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Awaitable, Callable, TypeVar

from deepagents.backends.protocol import ExecuteResponse, FileDownloadResponse, FileUploadResponse
from deepagents.backends.sandbox import BaseSandbox
from opensandbox import Sandbox
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.filesystem import WriteEntry


T = TypeVar("T")
logger = logging.getLogger(__name__)


def _is_command_stream_disconnect(exc: BaseException) -> bool:
    """Recognize SDK-wrapped failures raised after a command was submitted."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in {"ReadError", "RemoteProtocolError"}:
            return True
        cause = getattr(current, "cause", None)
        current = cause if isinstance(cause, BaseException) else current.__cause__
    return False


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
        execution_id = uuid.uuid4().hex
        state_dir = f"/tmp/ai-ppt-exec/{execution_id}"
        encoded_command = base64.b64encode(command.encode("utf-8")).decode("ascii")
        wrapped_command = (
            f"mkdir -p {state_dir}; "
            "if command -v bash >/dev/null 2>&1; then shell=bash; else shell=sh; fi; "
            f"printf %s {encoded_command} | base64 -d | \"$shell\" "
            f">{state_dir}/stdout 2>{state_dir}/stderr; "
            f"code=$?; printf %s \"$code\" >{state_dir}/status; exit \"$code\""
        )
        options = RunCommandOpts(
            timeout=timedelta(seconds=timeout) if timeout and timeout > 0 else None,
        )
        execution = None
        transport_error = None
        try:
            execution = await self._sandbox.commands.run(wrapped_command, opts=options)
        except Exception as exc:
            if not _is_command_stream_disconnect(exc):
                raise RuntimeError(f"OpenSandbox command execution failed: {exc}") from exc
            transport_error = exc

        deadline = asyncio.get_running_loop().time() + max(1, timeout or 30)
        status = None
        while status is None:
            try:
                status = (await self._sandbox.files.read_bytes(f"{state_dir}/status")).decode("ascii").strip()
            except Exception:
                if asyncio.get_running_loop().time() >= deadline:
                    if transport_error is not None:
                        raise RuntimeError(
                            "OpenSandbox command stream disconnected before execution status was recoverable"
                        ) from transport_error
                    break
                await asyncio.sleep(0.25)

        if status is None:
            exit_code = execution.exit_code
            if exit_code is None and execution.error is not None:
                exit_code = 1
            return ExecuteResponse(output=str(execution), exit_code=exit_code, truncated=False)

        try:
            stdout, stderr = await asyncio.gather(
                self._sandbox.files.read_bytes(f"{state_dir}/stdout"),
                self._sandbox.files.read_bytes(f"{state_dir}/stderr"),
            )
        except Exception as exc:
            raise RuntimeError("OpenSandbox command completed but its captured output could not be read") from exc
        output = b"\n".join(part for part in (stdout, stderr) if part).decode("utf-8", errors="replace")
        if transport_error is not None:
            logger.warning(
                "Recovered OpenSandbox command result after stream disconnect sandbox=%s execution=%s exit_code=%s",
                self.id,
                execution_id,
                status,
            )
        return ExecuteResponse(output=output, exit_code=int(status), truncated=False)

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
