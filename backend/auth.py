import hmac
import os
from dataclasses import dataclass

from fastapi import Header, HTTPException

from models import UserModel


@dataclass(frozen=True)
class RequestIdentity:
    tenant_id: str
    user_id: str
    username: str
    role: str = "user"


LOCAL_IDENTITY = RequestIdentity("local", "local-user", "local-user", "admin")


def validate_auth_configuration() -> None:
    if os.getenv("APP_ENV", "development").lower() == "production":
        if os.getenv("AUTH_MODE", "development").lower() != "trusted_headers":
            raise RuntimeError("Production requires AUTH_MODE=trusted_headers")
        if not os.getenv("TRUSTED_PROXY_AUTH_SECRET", "").strip():
            raise RuntimeError("Production requires TRUSTED_PROXY_AUTH_SECRET")


async def get_request_identity(
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-ID"),
    x_user_id: str | None = Header(default=None, alias="X-User-ID"),
    x_user_name: str | None = Header(default=None, alias="X-User-Name"),
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_auth_secret: str | None = Header(default=None, alias="X-Auth-Secret"),
) -> RequestIdentity:
    if os.getenv("AUTH_MODE", "development").lower() != "trusted_headers":
        return LOCAL_IDENTITY
    expected = os.getenv("TRUSTED_PROXY_AUTH_SECRET", "")
    if not expected or not x_auth_secret or not hmac.compare_digest(expected, x_auth_secret):
        raise HTTPException(status_code=401, detail="Untrusted authentication gateway")
    if not x_tenant_id or not x_user_id:
        raise HTTPException(status_code=401, detail="Missing tenant or user identity")
    return RequestIdentity(
        tenant_id=x_tenant_id[:100],
        user_id=x_user_id[:100],
        username=(x_user_name or x_user_id)[:100],
        role=(x_user_role or "user")[:20],
    )


def normalize_identity(identity) -> RequestIdentity:
    return identity if isinstance(identity, RequestIdentity) else LOCAL_IDENTITY


def get_or_create_user(db, identity: RequestIdentity) -> UserModel:
    user = db.query(UserModel).filter(
        UserModel.tenant_id == identity.tenant_id,
        UserModel.external_id == identity.user_id,
    ).first()
    if user is None:
        scoped_username = f"{identity.tenant_id}:{identity.user_id}"[:200]
        user = UserModel(
            tenant_id=identity.tenant_id,
            external_id=identity.user_id,
            username=scoped_username,
            display_name=identity.username,
            role=identity.role,
        )
        db.add(user)
        db.flush()
    return user


def owned_conversation(db, conversation_id: str, identity: RequestIdentity):
    from models import ConversationModel

    return (
        db.query(ConversationModel)
        .join(UserModel, ConversationModel.user_id == UserModel.id)
        .filter(
            ConversationModel.id == conversation_id,
            UserModel.tenant_id == identity.tenant_id,
            UserModel.external_id == identity.user_id,
        )
        .first()
    )


def owned_run(db, run_id: str, identity: RequestIdentity):
    from models import ConversationModel, RunModel

    return (
        db.query(RunModel)
        .join(ConversationModel, RunModel.conversation_id == ConversationModel.id)
        .join(UserModel, ConversationModel.user_id == UserModel.id)
        .filter(
            RunModel.id == run_id,
            UserModel.tenant_id == identity.tenant_id,
            UserModel.external_id == identity.user_id,
        )
        .first()
    )


def owned_attachment(db, attachment_id: str, identity: RequestIdentity):
    from models import AttachmentModel, ConversationModel

    return (
        db.query(AttachmentModel)
        .join(ConversationModel, AttachmentModel.conversation_id == ConversationModel.id)
        .join(UserModel, ConversationModel.user_id == UserModel.id)
        .filter(
            AttachmentModel.id == attachment_id,
            UserModel.tenant_id == identity.tenant_id,
            UserModel.external_id == identity.user_id,
        )
        .first()
    )


def owned_artifact(db, artifact_id: str, identity: RequestIdentity):
    from models import ArtifactModel, ConversationModel, RunModel

    return (
        db.query(ArtifactModel)
        .join(RunModel, ArtifactModel.run_id == RunModel.id)
        .join(ConversationModel, RunModel.conversation_id == ConversationModel.id)
        .join(UserModel, ConversationModel.user_id == UserModel.id)
        .filter(
            ArtifactModel.id == artifact_id,
            UserModel.tenant_id == identity.tenant_id,
            UserModel.external_id == identity.user_id,
        )
        .first()
    )
