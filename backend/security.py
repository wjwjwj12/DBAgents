import hashlib
import hmac
import logging
import os
import secrets
import time
from urllib.parse import urlencode

from runtime_paths import SECRET_FILE


logger = logging.getLogger(__name__)

_SECRET_FILE = SECRET_FILE


def _load_signing_secret() -> str:
    configured_secret = os.getenv("APP_SECRET_KEY")
    if configured_secret:
        return configured_secret
    if _SECRET_FILE.exists():
        persisted_secret = _SECRET_FILE.read_text(encoding="utf-8").strip()
        if persisted_secret:
            return persisted_secret

    generated_secret = secrets.token_urlsafe(48)
    try:
        _SECRET_FILE.write_text(generated_secret, encoding="utf-8")
        if os.name != "nt":
            _SECRET_FILE.chmod(0o600)
        logger.info("Generated persistent download signing key file=%s", _SECRET_FILE)
    except OSError:
        logger.warning(
            "Could not persist the generated download signing key; signed links will reset after restart"
        )
    return generated_secret


_SIGNING_SECRET = _load_signing_secret()


def create_download_token(artifact_id: str, expires_at: int) -> str:
    return hmac.new(
        _SIGNING_SECRET.encode("utf-8"),
        f"{artifact_id}:{expires_at}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_download_token(artifact_id: str, token: str, expires_at: int) -> bool:
    if expires_at < int(time.time()):
        return False
    expected_token = create_download_token(artifact_id, expires_at)
    return hmac.compare_digest(expected_token, token)


def create_download_url(artifact_id: str) -> str:
    expires_at = int(time.time()) + int(os.getenv("DOWNLOAD_URL_TTL_SECONDS", "3600"))
    query = urlencode({
        "token": create_download_token(artifact_id, expires_at),
        "expires": expires_at,
    })
    return f"/api/v1/artifacts/{artifact_id}/download?{query}"


def create_preview_url(artifact_id: str) -> str:
    expires_at = int(time.time()) + int(os.getenv("DOWNLOAD_URL_TTL_SECONDS", "3600"))
    query = urlencode({
        "token": create_download_token(artifact_id, expires_at),
        "expires": expires_at,
    })
    return f"/api/v1/artifacts/{artifact_id}/preview?{query}"
