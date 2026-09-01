import asyncio
import hashlib
import logging
import os
import weakref
from datetime import timedelta

from deepagents.backends import StateBackend


logger = logging.getLogger(__name__)
_THREAD_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


def _opensandbox_config():
    from opensandbox.config import ConnectionConfig

    domain = os.getenv("OPENSANDBOX_DOMAIN", "").strip()
    api_key = os.getenv("OPENSANDBOX_API_KEY", "").strip()
    if not domain or not api_key:
        raise RuntimeError("OPENSANDBOX_DOMAIN and OPENSANDBOX_API_KEY must be configured")
    return ConnectionConfig(
        domain=domain,
        protocol=os.getenv("OPENSANDBOX_PROTOCOL", "http").strip() or "http",
        api_key=api_key,
        request_timeout=timedelta(seconds=_positive_int("SANDBOX_REQUEST_TIMEOUT_SECONDS", 180)),
    )


async def _get_opensandbox_backend(thread_id: str):
    from opensandbox import Sandbox
    from opensandbox.manager import SandboxManager
    from opensandbox.models.sandboxes import SandboxFilter

    from .opensandbox_backend import OpenSandboxBackend

    thread_key = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:32]
    image = os.getenv("OPENSANDBOX_IMAGE", "ai-ppt-sandbox:2026.09").strip() or "ai-ppt-sandbox:2026.09"
    lock = _THREAD_LOCKS.setdefault(thread_key, asyncio.Lock())
    async with lock:
        metadata = {
            "app": "ai-ppt",
            "thread": thread_key,
            "runtime": hashlib.sha256(image.encode("utf-8")).hexdigest()[:12],
        }
        manager = await SandboxManager.create(connection_config=_opensandbox_config())
        try:
            page = await manager.list_sandbox_infos(SandboxFilter(metadata=metadata, page_size=100))
            running = [
                item for item in page.sandbox_infos
                if str(item.status.state).upper() == "RUNNING"
            ]
        finally:
            await manager.close()

        ttl = timedelta(seconds=max(300, _positive_int("SANDBOX_IDLE_TTL_SECONDS", 3600)))
        if running:
            selected = min(running, key=lambda item: item.created_at)
            sandbox = await Sandbox.connect(selected.id, connection_config=_opensandbox_config())
            await sandbox.renew(ttl)
            if len(running) > 1:
                logger.warning("Multiple OpenSandbox instances found for thread=%s count=%d", thread_key, len(running))
        else:
            sandbox = await Sandbox.create(
                image,
                timeout=ttl,
                ready_timeout=timedelta(seconds=_positive_int("SANDBOX_READY_TIMEOUT_SECONDS", 120)),
                metadata=metadata,
                connection_config=_opensandbox_config(),
            )
        return OpenSandboxBackend(sandbox)


async def get_thread_backend(thread_id: str):
    """Return a thread-scoped execution backend without exposing the web host."""
    provider = os.getenv("SANDBOX_PROVIDER", "disabled").strip().lower()
    if provider in {"", "disabled", "none"}:
        return StateBackend()
    if provider == "opensandbox":
        from .lazy_backend import LazySandboxBackend

        return LazySandboxBackend(thread_id, _get_opensandbox_backend)
    if provider != "langsmith":
        raise RuntimeError(f"Unsupported SANDBOX_PROVIDER: {provider}")

    from deepagents.backends.langsmith import LangSmithSandbox
    from langsmith.sandbox import SandboxClient

    sandbox_name = "thread-" + hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:32]
    client = SandboxClient()
    sandboxes = await asyncio.to_thread(client.list_sandboxes)
    sandbox = next((item for item in sandboxes if getattr(item, "name", None) == sandbox_name), None)
    if sandbox is None:
        ttl = max(300, int(os.getenv("SANDBOX_IDLE_TTL_SECONDS", "3600")))
        sandbox = await asyncio.to_thread(
            client.create_sandbox,
            name=sandbox_name,
            idle_ttl_seconds=ttl,
        )
    return LangSmithSandbox(sandbox=sandbox)
