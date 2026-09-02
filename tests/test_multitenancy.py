import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import main
from auth import RequestIdentity, get_or_create_user, get_request_identity, validate_auth_configuration
from database import Base
from models import ConversationModel


class MultiTenantTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.session_factory = sessionmaker(bind=engine)
        db = self.session_factory()
        self.alpha = RequestIdentity("tenant-a", "user-1", "甲")
        self.beta = RequestIdentity("tenant-b", "user-1", "乙")
        alpha_user = get_or_create_user(db, self.alpha)
        beta_user = get_or_create_user(db, self.beta)
        db.add_all([
            ConversationModel(id="conv-alpha", user_id=alpha_user.id, title="A"),
            ConversationModel(id="conv-beta", user_id=beta_user.id, title="B"),
        ])
        db.commit()
        db.close()

    async def test_conversation_access_is_scoped_by_tenant_and_user(self):
        with patch.object(main, "SessionLocal", self.session_factory):
            own = await main.get_conversation("conv-alpha", self.alpha)
            self.assertEqual(own["title"], "A")
            with self.assertRaises(HTTPException) as denied:
                await main.get_conversation("conv-alpha", self.beta)
            self.assertEqual(denied.exception.status_code, 404)

    async def test_conversation_list_does_not_cross_tenants(self):
        with patch.object(main, "SessionLocal", self.session_factory):
            alpha = await main.list_conversations(False, self.alpha)
            beta = await main.list_conversations(False, self.beta)
        self.assertEqual([item["id"] for item in alpha], ["conv-alpha"])
        self.assertEqual([item["id"] for item in beta], ["conv-beta"])

    async def test_trusted_headers_require_gateway_secret(self):
        with patch.dict("os.environ", {
            "AUTH_MODE": "trusted_headers",
            "TRUSTED_PROXY_AUTH_SECRET": "gateway-secret",
        }, clear=False):
            with self.assertRaises(HTTPException):
                await get_request_identity("tenant-a", "user-1", "甲", "user", "wrong")
            identity = await get_request_identity(
                "tenant-a", "user-1", "甲", "admin", "gateway-secret",
            )
        self.assertEqual(identity.tenant_id, "tenant-a")
        self.assertEqual(identity.role, "admin")

    def test_production_rejects_development_auth(self):
        with patch.dict("os.environ", {
            "APP_ENV": "production",
            "AUTH_MODE": "development",
        }, clear=False):
            with self.assertRaises(RuntimeError):
                validate_auth_configuration()

    def test_production_allows_explicitly_disabled_auth(self):
        with patch.dict("os.environ", {
            "APP_ENV": "production",
            "AUTH_MODE": "disabled",
        }, clear=False):
            validate_auth_configuration()


if __name__ == "__main__":
    unittest.main()
