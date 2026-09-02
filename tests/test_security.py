import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import security


class SigningSecretTests(unittest.TestCase):
    def test_production_generates_and_reuses_persistent_secret(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            secret_file = Path(temporary_dir) / ".app_secret"
            with (
                patch.object(security, "_SECRET_FILE", secret_file),
                patch.dict(os.environ, {"APP_ENV": "production", "APP_SECRET_KEY": ""}, clear=False),
            ):
                first = security._load_signing_secret()
                second = security._load_signing_secret()

            self.assertTrue(first)
            self.assertEqual(second, first)
            self.assertEqual(secret_file.read_text(encoding="utf-8"), first)


if __name__ == "__main__":
    unittest.main()
