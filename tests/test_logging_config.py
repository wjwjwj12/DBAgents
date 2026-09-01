import logging
import sys
import tempfile
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from logging_config import close_file_log_handlers, configure_logging  # noqa: E402


class LoggingConfigTests(unittest.TestCase):
    def test_application_errors_are_written_to_persistent_log(self):
        with tempfile.TemporaryDirectory() as directory:
            try:
                log_file = configure_logging(Path(directory))
                logger = logging.getLogger("test.persistent.error")
                try:
                    raise RuntimeError("diagnostic marker")
                except RuntimeError:
                    logger.exception("request failed run_id=test-run")
                for handler in logging.getLogger().handlers:
                    handler.flush()

                content = log_file.read_text(encoding="utf-8")
                self.assertIn("request failed run_id=test-run", content)
                self.assertIn("RuntimeError: diagnostic marker", content)
            finally:
                close_file_log_handlers()


if __name__ == "__main__":
    unittest.main()
