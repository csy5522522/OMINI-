"""Optional downloads preserve installed components on failure and cancellation."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from component_installer import install_ffmpeg


class ComponentInstallTests(unittest.TestCase):
    def test_digest_failure_never_extracts_or_replaces_existing_program(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "8.1.2/ffmpeg.exe"; original.parent.mkdir(); original.write_bytes(b"original")
            archive = root / "untrusted.7z"; archive.write_bytes(b"unexpected download")
            with patch("component_installer.subprocess.run") as execute:
                with self.assertRaisesRegex(ValueError, "SHA-256"):
                    install_ffmpeg(root, archive_path=archive)
                execute.assert_not_called()
            self.assertEqual(original.read_bytes(), b"original")
            self.assertFalse(list(root.glob("install-*")))

    def test_cancelled_download_is_cleaned_and_preserves_old_component(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            response = MagicMock()
            response.__enter__.return_value = response
            response.iter_content.return_value = [b"first chunk", b"next chunk"]
            with patch("component_installer.requests.get", return_value=response):
                with self.assertRaisesRegex(ValueError, "取消"):
                    install_ffmpeg(root, cancelled=lambda: True)
            self.assertEqual(list(root.iterdir()), [])

    def test_network_error_preserves_settings_and_cleans_staging(self):
        import requests
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("component_installer.requests.get", side_effect=requests.ConnectionError()):
                with self.assertRaisesRegex(ValueError, "下载失败"):
                    install_ffmpeg(root)
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__": unittest.main()
