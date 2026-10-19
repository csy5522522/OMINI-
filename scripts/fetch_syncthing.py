"""Fetch a pinned official Syncthing build and verify its published SHA-256."""
from pathlib import Path
import hashlib
import io
import re
import zipfile

import requests

VERSION = "v2.1.6"
BASE = f"https://github.com/syncthing/syncthing/releases/download/{VERSION}"
ARCHIVE = f"syncthing-windows-amd64-{VERSION}.zip"


def fetch(destination=None):
    destination = Path(destination or Path(__file__).resolve().parents[1] / "tools")
    destination.mkdir(parents=True, exist_ok=True)
    checksums = requests.get(f"{BASE}/sha256sum.txt.asc", timeout=60)
    checksums.raise_for_status()
    match = re.search(rf"^([a-f0-9]{{64}})\s+{re.escape(ARCHIVE)}\s*$", checksums.text, re.M)
    if not match:
        raise RuntimeError("Official release checksum is missing")
    response = requests.get(f"{BASE}/{ARCHIVE}", timeout=(15, 180))
    response.raise_for_status()
    if hashlib.sha256(response.content).hexdigest() != match[1]:
        raise RuntimeError("Syncthing archive checksum mismatch")
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        for name in ("syncthing.exe", "LICENSE.txt", "AUTHORS.txt"):
            member = next(p for p in archive.namelist() if p.endswith("/" + name))
            output = destination / (name if name.endswith(".exe") else "syncthing-" + name)
            output.write_bytes(archive.read(member))
    (destination / "syncthing-release.txt").write_text(
        f"Syncthing {VERSION}\n{BASE}/{ARCHIVE}\nSHA256: {match[1]}\n"
        "Source: https://github.com/syncthing/syncthing\nLicense: MPL-2.0\n", encoding="utf-8")
    print(f"Verified Syncthing {VERSION}: {destination / 'syncthing.exe'}")


if __name__ == "__main__":
    fetch()
