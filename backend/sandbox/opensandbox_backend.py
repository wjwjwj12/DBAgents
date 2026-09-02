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
        state_prefix = f"/tmp/ai-ppt-exec-{execution_id}"
        status_path = f"{state_prefix}.status"
        stdout_path = f"{state_prefix}.stdout"
        stderr_path = f"{state_prefix}.stderr"
        encoded_command = base64.b64encode(command.encode("utf-8")).decode("ascii")
        wrapped_command = (
            "if command -v bash >/dev/null 2>&1; then shell=bash; else shell=sh; fi; "
            f"printf %s {encoded_command} | base64 -d | \"$shell\" "
            f">{stdout_path} 2>{stderr_path}; "
            f"code=$?; printf %s \"$code\" >{status_path}; exit \"$code\""
        )
        try:
            await self._sandbox.files.write_files([
                WriteEntry(path=status_path, data=b"running", mode=600),
                WriteEntry(path=stdout_path, data=b"", mode=600),
                WriteEntry(path=stderr_path, data=b"", mode=600),
            ])
        except Exception as exc:
            raise RuntimeError("OpenSandbox command state could not be initialized") from exc
        options = RunCommandOpts(
            background=True,
            timeout=timedelta(seconds=timeout) if timeout and timeout > 0 else None,
        )
        transport_error = None
        try:
            await self._sandbox.commands.run(wrapped_command, opts=options)
        except Exception as exc:
            if not _is_command_stream_disconnect(exc):
                raise RuntimeError(f"OpenSandbox command execution failed: {exc}") from exc
            transport_error = exc

        default_timeout = max(1, int(os.getenv("SANDBOX_COMMAND_TIMEOUT_SECONDS", "600")))
        deadline = asyncio.get_running_loop().time() + max(1, timeout or default_timeout)
        status = "running"
        while status == "running":
            try:
                status = (await self._sandbox.files.read_bytes(status_path)).decode("ascii").strip()
            except Exception as exc:
                raise RuntimeError("OpenSandbox command status could not be read") from exc
            if status == "running":
                if asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError("OpenSandbox command did not complete before the timeout")
                await asyncio.sleep(0.25)
        try:
            exit_code = int(status)
        except ValueError as exc:
            raise RuntimeError(f"OpenSandbox returned an invalid command status: {status!r}") from exc

        try:
            stdout, stderr = await asyncio.gather(
                self._sandbox.files.read_bytes(stdout_path),
                self._sandbox.files.read_bytes(stderr_path),
            )
        except Exception as exc:
            raise RuntimeError("OpenSandbox command completed but its captured output could not be read") from exc
        output = b"\n".join(part for part in (stdout, stderr) if part).decode("utf-8", errors="replace")
        if transport_error is not None:
            logger.warning(
                "Recovered OpenSandbox command result after stream disconnect sandbox=%s execution=%s exit_code=%s",
                self.id,
                execution_id,
                exit_code,
            )
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
