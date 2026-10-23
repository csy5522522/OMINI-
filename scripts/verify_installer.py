"""Install, upgrade, launch and uninstall the exact Setup EXE in an isolated folder."""
from __future__ import annotations

import argparse
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
VERIFICATION_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{7E2C2E2F-5237-43BE-B64A-AC0A193146B8}_is1"


def uninstall_entry(app_key=APP_KEY):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, app_key) as key:
            return {name: winreg.QueryValueEx(key, name)[0] for name in
                    ("DisplayName", "DisplayVersion", "InstallLocation", "UninstallString")}
    except FileNotFoundError:
        return None


def run(command, environment=None):
    result = subprocess.run(command, env=environment, timeout=180, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode != 0:
        detail = ""
        if environment and environment.get("OMINI_SELF_TEST_LOG"):
            path = Path(environment["OMINI_SELF_TEST_LOG"])
            if path.exists():
                detail = "\n" + path.read_text(encoding="utf-8")[-3500:]
        raise RuntimeError(f"Verification command exited with {result.returncode}: {command[0]}" + detail)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("installer", type=Path)
    parser.add_argument("--isolated-registration", action="store_true",
                        help="Use the installer's separate verification AppId, preserving existing installations")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    setup = args.installer.resolve()
    assert setup.is_file()
    version = re.search(r"(\d+\.\d+\.\d+)-Setup\.exe$", setup.name)[1]
    app_key = VERIFICATION_KEY if args.isolated_registration else APP_KEY
    original_installation = uninstall_entry()
    if uninstall_entry(app_key):
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
            if args.isolated_registration:
                command.extend(["/OMINIVERIFY=1", "/NOCLOSEAPPLICATIONS"])
            run(command, environment)
            installed = True
            entry = uninstall_entry(app_key)
            assert entry and Path(entry["InstallLocation"]).resolve() == target.resolve()
            report["installed_app_registration"] = entry["DisplayName"] == "Omini工具箱" and entry["DisplayVersion"] == version
            app = target / "Omini工具箱.exe"
            report["complete_install"] = all((target / item).is_file() for item in
                ("Omini工具箱.exe", "tools/syncthing.exe", "_internal/python311.dll", "安装版使用说明.txt", "unins000.exe"))
            assert report["complete_install"]
            report["no_browser_or_bundled_ffmpeg"] = not any(
                'webengine' in path.name.lower() or path.name.lower().startswith(('ffmpeg', 'ffprobe', 'ffplay'))
                for path in target.rglob('*'))
            assert report["no_browser_or_bundled_ffmpeg"]
            source = root / f"dist-sync-{version}/Omini工具箱"
            for name in ("Omini工具箱.exe", "tools/syncthing.exe"):
                with (source / name).open("rb") as original, (target / name).open("rb") as copied:
                    assert hashlib.file_digest(original, "sha256").digest() == hashlib.file_digest(copied, "sha256").digest()
            report["binary_integrity"] = True
            for name in ("account-sync.js", "account-sync.css", "memo-editor.js", "memos.js", "memos.css"):
                assert (target / "_internal/web" / name).read_bytes() == (root / "web" / name).read_bytes(), name
            report["bundled_account_sync_and_memo_resources"] = True
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
            run([str(app), "--self-test-notes"], environment)
            notes_root = Path(environment["APPDATA"]) / "Omini工具箱"
            notes_report = json.loads((notes_root / "notes-smoke.json").read_text(encoding="utf-8"))
            assert notes_report["ok"] and notes_report["encrypted_notes_saved"]
            note_file = next((notes_root / "text-notes").glob('*.ominote'))
            note_original = note_file.read_bytes()
            legacy_cache = notes_root / "icloud-notes.json"
            legacy_cache.write_text('legacy-encrypted-cache-sentinel', encoding='utf-8')
            report["installed_encrypted_notes_smoke"] = True
            run([str(app), "--self-test-settings"], environment)
            settings_report = json.loads((root / f"verification/settings-self-test-{version}.json").read_text(encoding="utf-8"))
            assert settings_report.get("ok") and settings_report.get("memo_images_sync_and_rollback")
            report["installed_account_memos_smoke"] = True
            report["installed_desktop_smoke"] = True
            ai_environment = environment.copy()
            ai_environment.pop("QT_QPA_PLATFORM", None)
            ai_environment["OMINI_SELF_TEST_LOG"] = str(temp_root / "ai-check.json")
            ai_environment["OMINI_AI_CAPTURE_DIR"] = str(root / "verification" / ("installed-ai-" + version))
            run([str(app), "--self-test-ai-assistants"], ai_environment)
            ai_report = json.loads((temp_root / "ai-check.json").read_text(encoding="utf-8"))
            assert ai_report.get("ok") and ai_report.get("quota_columns_and_batch_query") and ai_report.get("live_balanced_scheduling")
            assert all(ai_report.get(key) for key in ("offline_members_after_restart", "persistent_quotas_and_history",
                                                      "resume_running_service", "manual_stop_preserved"))
            report["installed_ai_quota_scheduling_smoke"] = True
            report["installed_ai_restart_history_smoke"] = True
            # Real encrypted user data lives outside the installation directory.
            sys.path.insert(0, str(root))
            from ai_assistants import AssistantStore, atomic_json
            from assistant_data import AssistantState, OperationHistory
            ai_data = notes_root / "ai-assistants"
            ai_store = AssistantStore(ai_data); ai_store.save(ai_store.data)
            atomic_json(ai_data / "auths/codex-sentinel.json", {"type": "codex", "email": "fixture@example.com",
                "access_token": "verification-only-access", "refresh_token": "verification-only-refresh"})
            state = AssistantState(ai_data); state.save({**state.data, "ui": {"tab": 4}, "quotas": {}})
            OperationHistory(ai_data).append("upgrade-and-uninstall-action-sentinel", "fixture.action")
            ai_original = {path.relative_to(ai_data): path.read_bytes() for path in ai_data.rglob('*') if path.is_file()}
            obsolete = ["_internal/omini_api/runtime.py", "_internal/omini_api_page.py", "_internal/brightdata_page.py", "tools/ffmpeg.exe", "tools/ffprobe.exe", "tools/ffmpeg-release.json",
                        "_internal/PySide6/Qt6WebEngineCore.dll", "_internal/PySide6/QtWebEngineCore.pyd",
                        "_internal/PySide6/QtWebEngineProcess.exe", "_internal/PySide6/resources/icudtl.dat",
                        "_internal/PySide6/translations/qtwebengine_locales/en-US.pak",
                        "_internal/PySide6/Qt6Quick.dll", "_internal/PySide6/Qt6Qml.dll",
                        "_internal/PySide6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll"]
            for relative in obsolete:
                path = target / relative
                assert not path.exists(), path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'old-version-managed-runtime-fixture')
            run(command, environment)
            report["upgrade_removes_obsolete_runtimes"] = all(not (target / name).exists() for name in obsolete)
            assert report["upgrade_removes_obsolete_runtimes"]
            assert user_file.read_text(encoding="utf-8") == "keep-extra-files"
            report["upgrade_preserves_user_files"] = True
            report["upgrade_preserves_ai_members_state_history"] = all((ai_data / path).read_bytes() == content
                                                                       for path, content in ai_original.items())
            uninstaller = target / "unins000.exe"
            assert uninstaller.resolve().parent == target.resolve()
            run([str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                 f"/LOG={root / 'verification/installer-uninstall.log'}"], environment)
            installed = False
            report["uninstalled_program"] = not app.exists() and not (target / "tools/syncthing.exe").exists()
            report["uninstall_registration_removed"] = uninstall_entry(app_key) is None
            report["shortcuts_removed"] = not (shortcut_root / "Omini工具箱.lnk").exists()
            report["personal_settings_preserved"] = "keep-personal-settings" in data.read_text(encoding="utf-8")
            report["synced_files_preserved"] = synced.read_text(encoding="utf-8") == "keep-synced-files"
            report["extra_files_preserved"] = user_file.read_text(encoding="utf-8") == "keep-extra-files"
            report["txt_notes_and_legacy_cache_preserved"] = (note_file.read_bytes() == note_original
                and legacy_cache.read_text(encoding='utf-8') == 'legacy-encrypted-cache-sentinel')
            report["uninstall_preserves_ai_members_state_history"] = all((ai_data / path).read_bytes() == content
                                                                         for path, content in ai_original.items())
            (root / "verification/installer-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            assert all(value is True for key, value in report.items() if key != "installer"), report
        finally:
            if installed and (target / "unins000.exe").exists():
                assert (target / "unins000.exe").resolve().parent == target.resolve()
                run([str(target / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], environment)
    if args.isolated_registration:
        report["existing_installation_preserved"] = uninstall_entry() == original_installation
        assert report["existing_installation_preserved"]
    (root / "verification/installer-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
