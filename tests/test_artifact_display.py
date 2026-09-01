import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from xmlrpc.client import Binary


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import artifact_display  # noqa: E402


class RemoteLibreOfficeTests(unittest.TestCase):
    def test_unoserver_converter_uses_remote_file_transfer(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "slides.pptx"
            source.write_bytes(b"pptx")

            proxy = Mock()
            proxy.__enter__ = Mock(return_value=proxy)
            proxy.__exit__ = Mock(return_value=False)
            proxy.info.return_value = {"unoserver": "3.7", "api": "3"}
            proxy.convert.return_value = Binary(b"%PDF-1.7\npreview")

            with (
                patch.dict("os.environ", {
                    "LIBREOFFICE_UNOSERVER_HOST": "10.126.13.149",
                    "LIBREOFFICE_UNOSERVER_PORT": "8849",
                    "LIBREOFFICE_UNOSERVER_PROTOCOL": "http",
                    "LIBREOFFICE_CONVERT_URL": "",
                }),
                patch.object(artifact_display, "ServerProxy", return_value=proxy) as server_proxy,
            ):
                preview = artifact_display.ensure_pptx_pdf_preview(str(source))

            self.assertEqual(server_proxy.call_args.args[0], "http://10.126.13.149:8849")
            arguments = proxy.convert.call_args.args
            self.assertEqual(arguments[1].data, b"pptx")
            self.assertEqual(arguments[3], "pdf")
            self.assertTrue(Path(preview).read_bytes().startswith(b"%PDF-"))

    def test_remote_converter_writes_valid_pdf_preview(self):
        response = Mock(status_code=200, content=b"%PDF-1.7\npreview")
        response.raise_for_status.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "slides.pptx"
            source.write_bytes(b"pptx")
            with (
                patch.dict("os.environ", {"LIBREOFFICE_CONVERT_URL": "http://converter:8849/convert"}),
                patch.object(artifact_display.httpx, "post", return_value=response) as post,
            ):
                preview = artifact_display.ensure_pptx_pdf_preview(str(source))

            self.assertEqual(Path(preview).read_bytes(), response.content)
            self.assertEqual(post.call_args.args[0], "http://converter:8849/convert")

    def test_service_root_uses_gotenberg_route_first(self):
        response = Mock(status_code=200, content=b"%PDF-1.7\npreview")
        response.raise_for_status.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "slides.pptx"
            source.write_bytes(b"pptx")
            with (
                patch.dict("os.environ", {"LIBREOFFICE_CONVERT_URL": "http://converter:8849"}),
                patch.object(artifact_display.httpx, "post", return_value=response) as post,
            ):
                artifact_display.ensure_pptx_pdf_preview(str(source))

            self.assertEqual(post.call_args.args[0], "http://converter:8849/forms/libreoffice/convert")

    def test_non_pdf_response_is_rejected(self):
        response = Mock(status_code=200, content=b'{"status":"ok"}')
        response.raise_for_status.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "slides.pptx"
            source.write_bytes(b"pptx")
            with (
                patch.dict("os.environ", {"LIBREOFFICE_CONVERT_URL": "http://converter:8849/convert"}),
                patch.object(artifact_display, "libreoffice_executable", return_value=None),
                patch.object(artifact_display.httpx, "post", return_value=response),
            ):
                with self.assertRaisesRegex(RuntimeError, "did not return a PDF"):
                    artifact_display.ensure_pptx_pdf_preview(str(source))


if __name__ == "__main__":
    unittest.main()
