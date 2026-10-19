"""Install, upgrade, launch and uninstall the exact Setup EXE in an isolated folder."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
import winreg

APP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{87C65C31-3F6B-4D75-A698-ED691D22FA37}_is1"


def uninstall_entry():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APP_KEY) as key:
            return {name: winreg.QueryValueEx(key, name)[0] for name in
                    ("DisplayName", "DisplayVersion", "InstallLocation", "UninstallString")}
    except FileNotFoundError:
        return None


def run(command, environment=None):
    result = subprocess.run(command, env=environment, timeout=180, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode != 0:
        raise RuntimeError(f"Verification command exited with {result.returncode}: {command[0]}")


def main():
    root = Path(__file__).resolve().parents[1]
    setup = Path(sys.argv[1]).resolve()
    assert setup.is_file()
    version = re.search(r"(\d+\.\d+\.\d+)-Setup\.exe$", setup.name)[1]
    if uninstall_entry():
        raise RuntimeError("A user installation already exists; verification stopped before changing it.")
    report = {"installer": str(setup)}
    with tempfile.TemporaryDirectory(prefix="omini-installer-verification-") as directory:
        temp_root = Path(directory).resolve()
        # Verify the scope before either the uninstaller or temporary cleanup runs.
        assert temp_root.parent == Path(tempfile.gettempdir()).resolve()
        assert temp_root.name.startswith("omini-installer-verification-")
        target = temp_root / "installed"
        assert target.resolve().parent == temp_root
        group = "Omini-Install-Verification-" + uuid.uuid4().hex[:10]
        shortcut_root = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs" / group
        environment = os.environ.copy()
        environment["APPDATA"] = str(temp_root / "user-appdata")
        environment["QT_QPA_PLATFORM"] = "offscreen"
        environment["OMINI_SELF_TEST_LOG"] = str(temp_root / "app-check.json")
        data = Path(environment["APPDATA"]) / "Omini工具箱/sync/settings.json"
        data.parent.mkdir(parents=True)
        data.write_text('{"verification_sentinel":"keep-personal-settings"}', encoding="utf-8")
        synced = temp_root / "my-synced-folder/document.txt"
        synced.parent.mkdir()
        synced.write_text("keep-synced-files", encoding="utf-8")
        installed = False
        try:
            command = [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/TASKS=",
                       f"/DIR={target}", f"/GROUP={group}", f"/LOG={root / 'verification/installer-install.log'}"]
            run(command, environment)
            installed = True
            entry = uninstall_entry()
            assert entry and Path(entry["InstallLocation"]).resolve() == target.resolve()
            report["installed_app_registration"] = entry["DisplayName"] == "Omini工具箱" and entry["DisplayVersion"] == version
            app = target / "Omini工具箱.exe"
            report["complete_install"] = all((target / item).is_file() for item in
                ("Omini工具箱.exe", "tools/syncthing.exe", "_internal/python311.dll", "安装版使用说明.txt", "unins000.exe"))
            assert report["complete_install"]
            source = root / f"dist-sync-{version}/Omini工具箱"
            for name in ("Omini工具箱.exe", "tools/syncthing.exe"):
                with (source / name).open("rb") as original, (target / name).open("rb") as copied:
                    assert hashlib.file_digest(original, "sha256").digest() == hashlib.file_digest(copied, "sha256").digest()
            report["binary_integrity"] = True
            report["start_menu_shortcut"] = (shortcut_root / "Omini工具箱.lnk").is_file()
            user_file = target / "my-personal-file.txt"
            user_file.write_text("keep-extra-files", encoding="utf-8")
            run([str(app), "--self-test-sync"], environment)
            app_report = json.loads((temp_root / "app-check.json").read_text(encoding="utf-8"))
            assert app_report and all(value is True for value in app_report.values())
            report["installed_sync_smoke"] = True
            run([str(app), "--self-test-update"], environment)
            update_report = json.loads((temp_root / "app-check.json").read_text(encoding="utf-8"))
            assert update_report and all(value is True for value in update_report.values())
            report["installed_update_smoke"] = True
            run(command, environment)
            assert user_file.read_text(encoding="utf-8") == "keep-extra-files"
            report["upgrade_preserves_user_files"] = True
            uninstaller = target / "unins000.exe"
            assert uninstaller.resolve().parent == target.resolve()
            run([str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                 f"/LOG={root / 'verification/installer-uninstall.log'}"], environment)
            installed = False
            report["uninstalled_program"] = not app.exists() and not (target / "tools/syncthing.exe").exists()
            report["uninstall_registration_removed"] = uninstall_entry() is None
            report["shortcuts_removed"] = not (shortcut_root / "Omini工具箱.lnk").exists()
            report["personal_settings_preserved"] = "keep-personal-settings" in data.read_text(encoding="utf-8")
            report["synced_files_preserved"] = synced.read_text(encoding="utf-8") == "keep-synced-files"
            report["extra_files_preserved"] = user_file.read_text(encoding="utf-8") == "keep-extra-files"
            (root / "verification/installer-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            assert all(value is True for key, value in report.items() if key != "installer"), report
        finally:
            if installed and (target / "unins000.exe").exists():
                assert (target / "unins000.exe").resolve().parent == target.resolve()
                run([str(target / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], environment)
    (root / "verification/installer-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
