import asyncio
import ipaddress
import os
import shlex
import socket
import uuid
from urllib.parse import urlsplit


class SkillDownloadError(RuntimeError):
    pass


def _allowed_host(host: str) -> bool:
    configured = {
        item.strip().casefold()
        for item in os.getenv("SKILL_INSTALL_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    }
    return not configured or host.casefold() in configured


def _validate_source_url(url: str) -> str:
    value = url.strip()
    parts = urlsplit(value)
    allow_http = os.getenv("SKILL_INSTALL_ALLOW_HTTP", "").strip().lower() in {"1", "true", "yes"}
    if parts.scheme not in ({"http", "https"} if allow_http else {"https"}):
        raise SkillDownloadError("Skill 下载地址必须使用 HTTPS")
    if not parts.hostname or parts.username or parts.password or parts.fragment:
        raise SkillDownloadError("Skill 下载地址格式无效")
    host = parts.hostname.casefold()
    if not _allowed_host(host):
        raise SkillDownloadError(f"Skill 下载域名未获允许: {host}")

    explicitly_allowed = bool(os.getenv("SKILL_INSTALL_ALLOWED_HOSTS", "").strip())
    if not explicitly_allowed:
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
            }
        except socket.gaierror as exc:
            raise SkillDownloadError(f"无法解析 Skill 下载域名: {host}") from exc
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise SkillDownloadError("默认只允许从公网地址下载 Skill；内网源需配置白名单")
    return value


async def download_skill_archive(backend, url: str, *, max_bytes: int, timeout_seconds: int = 60) -> bytes:
    if backend is None or not getattr(backend, "is_active", True):
        raise SkillDownloadError("当前任务没有可用的执行沙箱")
    source_url = await asyncio.to_thread(_validate_source_url, url)
    target = f"/tmp/ai-ppt-skill-install/{uuid.uuid4().hex}.zip"
    protocol = urlsplit(source_url).scheme
    command = " ".join((
        "mkdir -p /tmp/ai-ppt-skill-install && curl",
        "--fail --silent --show-error",
        f"--proto '={protocol}' --proto-redir '={protocol}' --max-redirs 0",
        f"--connect-timeout 10 --max-time {int(timeout_seconds)} --max-filesize {int(max_bytes)}",
        f"--output {shlex.quote(target)} --url {shlex.quote(source_url)}",
    ))
    try:
        result = await asyncio.wait_for(
            backend.aexecute(command, timeout=timeout_seconds + 5),
            timeout=timeout_seconds + 15,
        )
        if result.exit_code != 0:
            detail = str(result.output or "").strip()[-500:]
            raise SkillDownloadError(f"沙箱下载 Skill 失败{f'：{detail}' if detail else ''}")
        responses = await asyncio.wait_for(backend.adownload_files([target]), timeout=30)
        response = responses[0] if responses else None
        if response is None or response.error or response.content is None:
            raise SkillDownloadError("无法从沙箱取回 Skill 压缩包")
        if not response.content or len(response.content) > max_bytes:
            raise SkillDownloadError("Skill 压缩包为空或超过上传大小限制")
        return response.content
    finally:
        try:
            await asyncio.wait_for(backend.aexecute(f"rm -f -- {shlex.quote(target)}", timeout=10), timeout=15)
        except Exception:
            pass
