import asyncio
import hashlib
import logging
from typing import Awaitable, Callable

from deepagents.backends import StoreBackend
from deepagents.backends.protocol import ExecuteResponse, FileDownloadResponse, FileUploadResponse
from deepagents.backends.sandbox import BaseSandbox
from langgraph.store.memory import InMemoryStore


logger = logging.getLogger(__name__)


class LazySandboxBackend(BaseSandbox):
    """Expose execute to the agent without provisioning a sandbox until it is used."""

    enable_capture_offload = True

    def __init__(self, thread_id: str, factory: Callable[[str], Awaitable[BaseSandbox]]):
        self._thread_id = thread_id
        self._factory = factory
        self._lazy_id = "lazy-" + hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:32]
        self._state = StoreBackend(
            store=InMemoryStore(),
            namespace=lambda _runtime: ("lazy-sandbox", self._lazy_id),
        )
        self._backend: BaseSandbox | None = None
        self._lock = asyncio.Lock()
        self._pending_files: dict[str, bytes] = {}

    async def _validate_ppt_runtime(self, backend: BaseSandbox) -> None:
        if not any(path.startswith("/skills/ppt-master/") for path in self._pending_files):
            return
        command = 'python -c "import PIL, pptx, xlsxwriter, fitz, playwright"'
        response = await backend.aexecute(command, timeout=30)
        if response.exit_code != 0:
            raise RuntimeError(
                "PPT dependencies are missing from the sandbox image. "
                "Build deploy/opensandbox/Dockerfile and configure OPENSANDBOX_IMAGE. "
                f"Validation output: {response.output}"
            )

    @property
    def id(self) -> str:
        return str(self._backend.id) if self._backend is not None else self._lazy_id

    @property
    def is_active(self) -> bool:
        return self._backend is not None

    async def _ensure_backend(self) -> BaseSandbox:
        if self._backend is not None:
            return self._backend
        async with self._lock:
            if self._backend is None:
                logger.info("Activating thread sandbox thread=%s", self._lazy_id.removeprefix("lazy-"))
                backend = await self._factory(self._thread_id)
                if self._pending_files:
                    responses = await backend.aupload_files(list(self._pending_files.items()))
                    errors = [response for response in responses if response.error]
                    if errors:
                        await backend.aclose()
                        raise RuntimeError(f"Failed to initialize sandbox files: {len(errors)} uploads failed")
                try:
                    await self._validate_ppt_runtime(backend)
                except Exception:
                    await backend.aclose()
                    raise
                self._backend = backend
        return self._backend

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        raise RuntimeError("LazySandboxBackend only supports asynchronous execution")

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        backend = await self._ensure_backend()
        return await backend.aexecute(command, timeout=timeout)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        if self._backend is not None:
            return self._backend.upload_files(files)
        self._pending_files.update(files)
        return self._state.upload_files(files)

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        if self._backend is not None:
            return await self._backend.aupload_files(files)
        self._pending_files.update(files)
        return await self._state.aupload_files(files)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        if self._backend is not None:
            return self._backend.download_files(paths)
        return [FileDownloadResponse(path=path, error="Sandbox has not been created") for path in paths]

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        if self._backend is not None:
            return await self._backend.adownload_files(paths)
        return await self._state.adownload_files(paths)

    async def als(self, path: str):
        if self._backend is not None:
            return await self._backend.als(path)
        return await self._state.als(path)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000):
        if self._backend is not None:
            return await self._backend.aread(file_path, offset=offset, limit=limit)
        return await self._state.aread(file_path, offset=offset, limit=limit)

    async def awrite(self, file_path: str, content: str):
        if self._backend is not None:
            return await self._backend.awrite(file_path, content)
        result = await self._state.awrite(file_path, content)
        if not result.error:
            self._pending_files[file_path] = content.encode("utf-8")
        return result

    async def aedit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False):
        if self._backend is not None:
            return await self._backend.aedit(file_path, old_string, new_string, replace_all)
        result = await self._state.aedit(file_path, old_string, new_string, replace_all)
        if not result.error:
            downloaded = await self._state.adownload_files([file_path])
            if downloaded and downloaded[0].content is not None:
                self._pending_files[file_path] = downloaded[0].content
        return result

    async def adelete(self, file_path: str):
        if self._backend is not None:
            return await self._backend.adelete(file_path)
        result = await self._state.adelete(file_path)
        if not result.error:
            self._pending_files.pop(file_path, None)
        return result

    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None, *, max_count: int | None = None):
        if self._backend is not None:
            return await self._backend.agrep(pattern, path, glob, max_count=max_count)
        return await self._state.agrep(pattern, path, glob, max_count=max_count)

    async def aglob(self, pattern: str, path: str | None = None):
        if self._backend is not None:
            return await self._backend.aglob(pattern, path)
        return await self._state.aglob(pattern, path)

    async def aclose(self) -> None:
        if self._backend is not None:
            await self._backend.aclose()
            self._backend = None
