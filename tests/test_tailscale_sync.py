"""Address input, optional peer selection and read-only Tailscale inventory."""
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from tailscale_sync import TailscaleError, normalize_addresses, parse_status, read_status
from folder_sync_page import DeviceDialog


FIXTURE = {"BackendState": "Running", "PrivateKey": "should-not-be-returned", "Self": {
    "HostName": "my-computer", "TailscaleIPs": ["100.100.10.1"], "DNSName": "my-computer.example.ts.net."},
    "Peer": {"offline": {"HostName": "laptop", "TailscaleIPs": ["100.100.10.3"], "Online": False, "OS": "windows"},
             "online": {"HostName": "mac-mini", "TailscaleIPs": ["100.100.10.2", "fd7a:115c:a1e0::2"],
                        "DNSName": "mac-mini.example.ts.net.", "Online": True, "OS": "macOS"}}}


class AddressTest(unittest.TestCase):
    def test_ipv4_dns_ipv6_and_custom_port(self):
        for value, expected in {
            "100.100.10.2": "tcp://100.100.10.2:22000",
            "mac-mini.example.ts.net.": "tcp://mac-mini.example.ts.net:22000",
            "laptop:22001": "tcp://laptop:22001",
            "fd7a:115c:a1e0::2": "tcp://[fd7a:115c:a1e0::2]:22000",
            "[fd7a:115c:a1e0::2]:22001": "tcp://[fd7a:115c:a1e0::2]:22001",
            "quic://100.100.10.2": "quic://100.100.10.2:22000",
            "tcp://laptop:22001": "tcp://laptop:22001",
        }.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_addresses(value), [expected])

    def test_multiple_dedup_and_optional_discovery(self):
        self.assertEqual(normalize_addresses("100.100.10.2，tcp://100.100.10.2:22000; dynamic"),
                         ["tcp://100.100.10.2:22000", "dynamic"])
        self.assertEqual(normalize_addresses(""), ["dynamic"])
        self.assertEqual(normalize_addresses(["100.100.10.2"]), ["tcp://100.100.10.2:22000"])

    def test_invalid_inputs_are_not_saved_as_working_addresses(self):
        for value in ("https://example.com/page", "laptop:99999", "tcp://laptop:0", "laptop/files", "user:password@host", "two hosts"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_addresses(value)

    def test_inventory_is_filtered_sorted_and_no_private_data(self):
        data = parse_status(FIXTURE)
        self.assertEqual(data["peers"][0]["name"], "mac-mini")
        self.assertEqual(data["self"]["dns"], "my-computer.example.ts.net")
        self.assertNotIn("PrivateKey", str(data))
        self.assertNotIn("should-not-be-returned", str(data))

    def test_cli_is_read_only_and_handles_service_failure(self):
        with patch("tailscale_sync.shutil.which", return_value="tailscale.exe"), patch.object(Path, "is_file", return_value=True), patch("tailscale_sync.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, b"", b"error")
            with self.assertRaises(TailscaleError):
                read_status()
            self.assertEqual(run.call_args.args[0], ["tailscale.exe", "status", "--json"])


class TailscaleDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def test_user_selects_peer_and_identity_is_still_required(self):
        dialog = DeviceDialog(inventory=parse_status(FIXTURE))
        self.assertEqual(dialog.addresses.text(), "dynamic")
        self.assertEqual(dialog.device_id.text(), "")
        dialog.choose_tailscale(1)
        self.assertEqual(dialog.addresses.text(), "100.100.10.2")
        self.assertEqual(dialog.name.text(), "mac-mini")
        self.assertEqual(dialog.device_id.text(), "")
        dialog.device_id.setText("manually-pasted-sync-id")
        dialog.set_tailscale(parse_status(FIXTURE))
        self.assertEqual(dialog.addresses.text(), "100.100.10.2")
        self.assertEqual(dialog.device_id.text(), "manually-pasted-sync-id")
        dialog.deleteLater()

    def test_refresh_does_not_replace_manual_input_or_select_a_peer(self):
        dialog = DeviceDialog()
        dialog.name.setText("我自己的名称")
        dialog.addresses.setText("my-host.example.ts.net")
        dialog.set_tailscale(parse_status(FIXTURE))
        self.assertEqual(dialog.tailscale_peers.currentIndex(), 0)
        self.assertEqual(dialog.addresses.text(), "my-host.example.ts.net")
        dialog.choose_tailscale(2)
        self.assertEqual(dialog.name.text(), "我自己的名称")
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
