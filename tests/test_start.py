import sys
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

import start  # noqa: E402


class UnifiedLauncherTests(unittest.TestCase):
    def test_backend_python_uses_active_environment(self):
        with patch.object(start.sys, "executable", "/opt/conda/envs/ai-ppt/bin/python"):
            backend_python = start.get_backend_python()

        self.assertEqual(backend_python, Path("/opt/conda/envs/ai-ppt/bin/python"))

    def test_default_commands_start_production_backend_and_frontend(self):
        with patch.object(start.shutil, "which", return_value="npm.cmd"):
            backend, frontend, backend_port, frontend_port = start.build_commands()

        self.assertIn("uvicorn", backend)
        self.assertIn("main:app", backend)
        self.assertNotIn("--reload", backend)
        self.assertEqual(frontend[1:3], ["run", "start"])
        self.assertEqual(backend_port, "14499")
        self.assertEqual(frontend_port, "6477")

    def test_development_commands_enable_reload_and_next_dev(self):
        with patch.object(start.shutil, "which", return_value="npm.cmd"):
            backend, frontend, _backend_port, _frontend_port = start.build_commands(False)

        self.assertEqual(frontend[1:3], ["run", "dev"])
        self.assertIn("--reload", backend)

    def test_linux_service_starts_in_an_independent_process_group(self):
        with (
            patch.object(start.os, "name", "posix"),
            patch.object(start.subprocess, "Popen") as popen,
        ):
            start.start_service(["service"], Path("/srv/app"))

        self.assertEqual(popen.call_args.args, (["service"],))
        self.assertEqual(popen.call_args.kwargs["cwd"].as_posix(), "/srv/app")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_stop_processes_terminates_then_kills_entire_process_tree(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("service", 5), None]

        with patch.object(start, "signal_process_tree") as signal_tree:
            start.stop_processes([("frontend", process)])

        self.assertEqual(signal_tree.call_args_list, [call(process), call(process, force=True)])


if __name__ == "__main__":
    unittest.main()
