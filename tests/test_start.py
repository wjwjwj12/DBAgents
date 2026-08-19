import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

import start  # noqa: E402


class UnifiedLauncherTests(unittest.TestCase):
    def test_backend_python_uses_active_environment(self):
        with patch.object(start.sys, "executable", "/opt/conda/envs/ai-ppt/bin/python"):
            backend_python = start.get_backend_python()

        self.assertEqual(backend_python, Path("/opt/conda/envs/ai-ppt/bin/python"))

    def test_development_commands_start_backend_and_frontend(self):
        with patch.object(start.shutil, "which", return_value="npm.cmd"):
            backend, frontend, backend_port, frontend_port = start.build_commands()

        self.assertIn("uvicorn", backend)
        self.assertIn("main:app", backend)
        self.assertIn("--reload", backend)
        self.assertEqual(frontend[1:3], ["run", "dev"])
        self.assertEqual(backend_port, "14499")
        self.assertEqual(frontend_port, "6477")

    def test_production_command_uses_next_start(self):
        with patch.object(start.shutil, "which", return_value="npm.cmd"):
            _backend, frontend, _backend_port, _frontend_port = start.build_commands(True)

        self.assertEqual(frontend[1:3], ["run", "start"])
        self.assertNotIn("--reload", _backend)


if __name__ == "__main__":
    unittest.main()
