"""Real two-process Qt acceptance for automatic tools and manual assistant sync."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "artifacts/verification/account-sync"


def atomic(path, data):
    from ai_assistants import atomic_json
    atomic_json(Path(path), data)


def desktop_agent(root):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    import app
    root = Path(root)
    for key, relative in {"APP_DIR": ".", "SETTINGS_FILE": "settings.json", "TASKS_FILE": "tasks.json",
                          "LOG_DIR": "logs", "TEMP_COOKIE_DIR": "runtime-cookies"}.items():
        setattr(app, key, root / relative)
    qt = QApplication([])
    window = app.MainWindow(); window.show()
    window.cloud_sync.timer.stop()
    pending = None
    last_id = None

    def inspect():
        return {"root": str(window.cloud_sync.root), "user": window.settings_page.account.user,
                "status": window.settings_page.account.sync_status.text(),
                "resources": window.cloud_sync.snapshot(include_assistants=True),
                "memos": [window.notes_page.store.for_path(p)["content"] for p in window.notes_page.store.files()] if window.notes_page else [],
                "conflicts": len(list((window.cloud_sync.root / "conflicts").glob("*.json")))}

    def tick():
        nonlocal pending, last_id
        notes = window.notes_page
        if notes: notes.sync_timer.stop()
        if pending:
            if window.cloud_sync.worker or window.cloud_sync.busy() or window.settings_page.account.worker or notes and notes.worker:
                return
            try: result = inspect()
            except Exception as error: result = {"error": repr(error)}
            atomic(root / "reply.json", {"id": pending["id"], **result})
            pending = None
        if not (root / "ready.json").exists() and window.cloud_sync.context and not window.cloud_sync.worker and not window.settings_page.account.worker:
            atomic(root / "ready.json", {"ok": True})
        try: command = json.loads((root / "command.json").read_text(encoding="utf-8"))
        except (OSError, ValueError): return
        if command["id"] == last_id: return
        last_id = command["id"]
        try:
            action, data = command["action"], command.get("data", {})
            if action == "sync": window.cloud_sync.panel.sync_button.click()
            elif action == "auto": window.cloud_sync.sync_now()
            elif action == "model":
                window.ai_assistants_page.model.setCurrentText(data["value"])
                window.ai_assistants_page.save_session()
            elif action == "member":
                atomic(window.cloud_sync.root / "ai-assistants/auths" / data["name"], data["value"])
                window.ai_assistants_page.apply_cloud()
            elif action == "schedule":
                atomic(window.cloud_sync.root / "school-timetable.json", data["schedule"])
                window.timetable_page.apply_cloud(data)
            elif action == "week":
                window.timetable_page.week.setCurrentIndex(window.timetable_page.week.findData(data["value"]))
            elif action == "theme":
                window.settings["theme"] = data["value"]; window.apply_theme()
            elif action == "memo":
                path = window.notes_page.store.create("跨设备备忘录", data["value"])
                window.notes_page.refresh_files(path.name)
            elif action == "screenshot":
                window.nav.setCurrentRow(data["page"])
                if data.get("components"):
                    from PySide6.QtWidgets import QScrollArea
                    window.settings_page.findChild(QScrollArea).ensureWidgetVisible(window.settings_page.components)
                qt.processEvents(); window.grab().save(data["path"])
            elif action == "logout": window.settings_page.account.request("logout")
            elif action == "login":
                panel = window.settings_page.account
                panel.email.setText(data["email"]); panel.password.setText("FixturePass-123")
                panel.request("login")
            elif action == "quit":
                atomic(root / "reply.json", {"id": last_id, "ok": True})
                window.quitting = True; window.close(); return
            window.cloud_sync.persist()
            pending = command
        except Exception as error:
            atomic(root / "reply.json", {"id": last_id, "error": repr(error)})
    timer = QTimer(); timer.timeout.connect(tick); timer.start(40)
    qt.exec()


class Agent:
    def __init__(self, root):
        self.root, self.sequence = Path(root), 0
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "ready.json").unlink(missing_ok=True)
        (self.root / "command.json").unlink(missing_ok=True)
        self.log = (self.root / "agent.log").open("w", encoding="utf-8")
        self.process = subprocess.Popen([sys.executable, "-m", "tests.account_sync_check", "--agent", str(self.root)],
            cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.wait(lambda: (self.root / "ready.json").exists(), 30)

    def wait(self, condition, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition(): return
            if self.process.poll() is not None:
                raise RuntimeError((self.root / "agent.log").read_text(encoding="utf-8"))
            time.sleep(.05)
        raise AssertionError("Desktop operation timed out: " + (self.root / "agent.log").read_text(encoding="utf-8"))

    def send(self, action, data=None):
        self.sequence += 1
        atomic(self.root / "command.json", {"id": self.sequence, "action": action, "data": data or {}})
        result = {}
        def received():
            nonlocal result
            try: result = json.loads((self.root / "reply.json").read_text(encoding="utf-8"))
            except (OSError, ValueError): return False
            return result.get("id") == self.sequence
        self.wait(received)
        assert "error" not in result, result.get("error")
        return result

    def close(self):
        if self.process.poll() is None:
            try: self.send("quit"); self.process.wait(timeout=20)
            except Exception: self.process.kill(); self.process.wait(timeout=10)
        self.log.close()


class Service:
    def __init__(self, root, port=0):
        self.root = Path(root)
        env = os.environ.copy(); env.update(OMINI_WEB_PASSWORD="", PYTHONIOENCODING="utf-8")
        self.process = subprocess.Popen([sys.executable, "web_server.py", "--port", str(port), "--data-dir", str(self.root / "server")], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ready, lines = threading.Event(), []
        def output():
            for line in self.process.stdout:
                lines.append(line)
                match = re.search(r"Local URL: (http://\S+)", line)
                if match: self.url = match[1]; ready.set()
        threading.Thread(target=output, daemon=True).start()
        if not ready.wait(20): self.close(); raise RuntimeError("".join(lines))

    def close(self):
        if self.process.poll() is None: self.process.terminate(); self.process.wait(timeout=15)
        self.process.stdout.close()


def main():
    from account_client import AccountClient, DesktopSession
    from ai_assistants import AssistantStore
    from feature_config import DESKTOP_FEATURES
    from tests.test_account_sync import credential, schedule
    destination = Path(tempfile.gettempdir()) / "omini-desktop-manual-sync-20261028"
    destination.mkdir(exist_ok=True)
    report = {"checks": [], "ok": False}
    agents, service = [], None
    with tempfile.TemporaryDirectory(prefix="omini-manual-sync-") as temporary:
        root = Path(temporary)
        try:
            service = Service(root)
            a = AccountClient(service.url)
            user = a.request("register", {"email": "manual-sync@example.test", "password": "FixturePass-123"})["user"]
            b = AccountClient(service.url)
            b.request("login", {"email": user["email"], "password": "FixturePass-123"})
            for name, client in (("a", a), ("b", b)):
                directory = root / name; directory.mkdir()
                atomic(directory / "settings.json", {"account_server": service.url, "features": dict.fromkeys(DESKTOP_FEATURES, True), "theme": "浅色"})
                DesktopSession(directory / "account-session.bin").save(service.url, client.token, user)
                store = AssistantStore(directory / "ai-assistants")
                store.data["pool"].update(resume_on_launch=False, background=False); store.save(store.data)
            atomic(root / "a/ai-assistants/auths/codex-native.json", credential("native"))
            native_a = Agent(root / "a"); agents.append(native_a)
            native_b = Agent(root / "b"); agents.append(native_b)
            native_a.send("schedule", {"schedule": schedule("自动同步课表"), "first_monday": "2026-09-07"})
            native_a.send("memo", {"value": "备忘录继续自动同步"})
            native_a.send("auto")
            automatic = native_b.send("auto")
            assert automatic["resources"]["schedule"]["schedule"]["courses"][0]["name"] == "自动同步课表"
            assert not automatic["resources"]["assistants"]["members"]
            assert a.api("/api/sync")["resources"]["assistants"]["revision"] == 0
            report["checks"].append("Two Qt clients auto-sync timetable and memos while keeping assistant credentials local")
            native_a.send("sync")
            manual = native_b.send("sync")
            assert "codex-native.json" in manual["resources"]["assistants"]["members"]
            assert "备忘录继续自动同步" in manual["memos"][0]
            revision = a.api("/api/sync")["resources"]["assistants"]["revision"]
            report["checks"].append("Clicking the real manual-sync button uploads and downloads assistant members")
            native_a.send("model", {"value": "manual-only-model"})
            for _ in range(3):
                native_a.send("auto"); native_b.send("auto")
            assert a.api("/api/sync")["resources"]["assistants"]["revision"] == revision
            assert native_b.send("auto")["resources"]["assistants"]["pool"]["model"] != "manual-only-model"
            native_a.send("sync")
            assert native_b.send("sync")["resources"]["assistants"]["pool"]["model"] == "manual-only-model"
            report["checks"].append("Repeated automatic passes leave assistant revisions unchanged; the next manual click transfers edits")
            native_b.close(); agents.remove(native_b)
            native_b = Agent(root / "b"); agents.append(native_b)
            restarted = native_b.send("auto")
            assert restarted["resources"]["assistants"]["pool"]["model"] == "manual-only-model"
            report["checks"].append("Restart preserves local assistant state and resumes automatic tools without assistant uploads")
            report["ok"] = True
        finally:
            for agent in agents: agent.close()
            if service: service.close()
            (destination / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--agent": desktop_agent(sys.argv[2])
    else: main()
