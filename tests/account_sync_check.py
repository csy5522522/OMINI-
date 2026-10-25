"""Real two-process Qt + Chromium account sync acceptance and screenshots."""
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
DEST = ROOT / "verification/account-sync"


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
                "resources": window.cloud_sync.snapshot(),
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
            if action == "sync": window.cloud_sync.sync_now()
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
    from playwright.sync_api import sync_playwright
    from account_client import AccountClient, DesktopSession
    from ai_assistants import AssistantStore
    from feature_config import DESKTOP_FEATURES
    from tests.test_account_sync import credential, schedule
    DEST.mkdir(parents=True, exist_ok=True)
    report = {"checks": [], "screenshots": []}
    agents, service = [], None
    with tempfile.TemporaryDirectory(prefix="omini-account-check-") as temporary:
        root = Path(temporary)
        try:
            service = Service(root)
            a = AccountClient(service.url); user = a.request("register", {"email": "account-sync@example.test", "password": "FixturePass-123"})["user"]
            b = AccountClient(service.url); b.request("login", {"email": user["email"], "password": "FixturePass-123"})
            features = dict.fromkeys(DESKTOP_FEATURES, False)
            for name in ("timetable", "notes", "ai_assistants"): features[name] = True
            for name, client in (("a", a), ("b", b)):
                directory = root / name; directory.mkdir()
                atomic(directory / "settings.json", {"account_server": service.url, "features": features,
                    "theme": "浅色", "concurrency": 2, "last_page": 8})
                DesktopSession(directory / "account-session.bin").save(service.url, client.token, user)
                store = AssistantStore(directory / "ai-assistants")
                store.data["pool"].update(resume_on_launch=False, background=False); store.save(store.data)
            atomic(root / "a/ai-assistants/auths/codex-native.json", credential("native"))
            native_a = Agent(root / "a"); agents.append(native_a)
            native_b = Agent(root / "b"); agents.append(native_b)
            native_a.send("schedule", {"schedule": schedule("离散数学"), "first_monday": "2026-09-07"})
            native_a.send("memo", {"value": "客户端与网页共享的笔记\n\n第二行 😀"})
            native_a.send("sync"); value = native_b.send("sync")
            assert len(value["resources"]["assistants"]["members"]) == 1, value["status"]
            assert value["resources"]["schedule"]["first_monday"] == "2026-09-07"
            assert "第二行 😀" in value["memos"][0]
            report["checks"].append("Two independent Qt clients share the same account timetable, members and memos")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                context = browser.new_context(viewport={"width": 1460, "height": 1100}, device_scale_factor=1)
                page = context.new_page(); errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(service.url + "/#ai_assistants")
                page.locator("#account-open").click()
                page.locator("#account-email").fill(user["email"]); page.locator("#account-password").fill("FixturePass-123")
                page.locator("#auth-submit").click()
                page.wait_for_function("() => cloudReady && !cloudBusy")
                page.locator("[data-member]").first.wait_for()
                assert page.locator("[data-member]").count() == 1
                native_a.send("week", {"value": 7}); native_a.send("sync")
                value = native_b.send("sync")
                assert value["resources"]["schedule"]["selected_week"] == 7
                page.evaluate("() => loadAccountSync(true)")
                page.evaluate("() => {location.hash='#timetable';route()}")
                page.wait_for_function("() => selectedWeek===7 && !cloudBusy")
                page.reload(); page.wait_for_function("() => selectedWeek===7 && cloudReady && !cloudBusy")
                page.locator("#week-select").select_option("0")
                page.wait_for_function("() => !cloudBusy && !cloudPending.schedule && selectedWeek===0")
                value = native_a.send("sync")
                assert value["resources"]["schedule"]["selected_week"] == 0
                native_b.send("sync")
                native_b.send("week", {"value": 12}); native_b.send("sync")
                page.evaluate("() => loadAccountSync(true)")
                page.wait_for_function("() => selectedWeek===12 && !cloudBusy")
                report["checks"].append("Selected weeks and all-semester view persist after browser reload and synchronize in both directions across two native clients")
                page.evaluate("() => {location.hash='#ai_assistants';route()}")
                path = root / "browser-credential.json"; atomic(path, credential("browser"))
                page.locator("#cloud-import").set_input_files(str(path))
                page.wait_for_function("() => Object.keys(cloudData('assistants').members||{}).length===2 && !cloudBusy")
                page.locator("#cloud-model").fill("fixture-model")
                page.locator("#cloud-strategy").select_option("balanced")
                page.locator("#cloud-pool-form button[type=submit]").click()
                page.wait_for_function("() => !cloudBusy && !cloudPending.assistants")
                value = native_a.send("sync"); native_b.send("sync")
                assert len(value["resources"]["assistants"]["members"]) == 2, value["status"]
                assert value["resources"]["assistants"]["pool"]["strategy"] == "balanced"
                report["checks"].append("Browser member import, model and scheduling edits reach both native clients")
                native_a.send("theme", {"value": "深色"}); native_a.send("sync")
                page.evaluate("() => loadAccountSync(true)")
                assert page.locator("html").get_attribute("data-theme") == "dark"
                assert native_b.send("sync")["resources"]["preferences"]["theme"] == "深色"
                page.screenshot(path=str(DEST / "web-dark.png"), full_page=True)
                report["screenshots"].append(str(DEST / "web-dark.png"))
                report["checks"].append("Theme and feature preferences synchronize across all three devices")
                # Delay a real POST while another device changes an independent field.
                # A second local edit must use the first response's new revision.
                previous = a.api("/api/sync")
                conflict_count = len(previous.get("conflicts", []))
                page.evaluate("""() => {
                    window.raceFetch = window.fetch;
                    window.raceBlocked = false;
                    window.fetch = async (...args) => {
                        if(args[0] === '/api/sync/preferences' && !window.raceBlocked) {
                            window.raceBlocked = true;
                            await new Promise(resolve => {window.releaseRace = resolve;});
                        }
                        return window.raceFetch(...args);
                    };
                    window.raceFirst = queueCloudData('preferences', {...cloudData('preferences'), theme:'浅色'});
                }""")
                page.wait_for_function("() => typeof window.releaseRace === 'function'")
                remote_prefs = previous["resources"]["preferences"]
                concurrency = 3 if remote_prefs["data"].get("concurrency") != 3 else 4
                a.api("/api/sync/preferences", {"revision": remote_prefs["revision"],
                    "data": {**remote_prefs["data"], "concurrency": concurrency}})
                page.evaluate("() => queueCloudData('preferences', {...cloudData('preferences'), theme:'深色'})")
                page.evaluate("() => window.releaseRace()")
                page.wait_for_function("() => !cloudBusy && !cloudPending.preferences && cloudData('preferences').theme==='深色'")
                page.evaluate("() => {window.fetch = window.raceFetch; delete window.releaseRace;}")
                after_race = a.api("/api/sync")
                assert after_race["resources"]["preferences"]["data"]["theme"] == "深色"
                assert after_race["resources"]["preferences"]["data"]["concurrency"] == concurrency
                assert len(after_race.get("conflicts", [])) == conflict_count
                report["checks"].append("Delayed real browser POST preserves the latest local edit and independent remote fields without a false conflict")
                context.set_offline(True)
                page.locator("#cloud-model").fill("offline-browser-model")
                page.locator("#cloud-pool-form button[type=submit]").click()
                page.wait_for_function("() => Boolean(cloudPending.assistants) && !cloudBusy && !cloudDraftSaving && document.querySelector('#cloud-status').textContent.includes('同步待重试')")
                assert page.evaluate("() => Object.keys(localStorage).every(k=>!String(localStorage.getItem(k)).includes('access-private'))")
                context.set_offline(False); page.reload()
                page.wait_for_function("() => cloudReady && !cloudBusy && !cloudPending.assistants")
                assert a.api("/api/sync")["resources"]["assistants"]["data"]["pool"]["model"] == "offline-browser-model"
                native_a.send("sync"); native_b.send("sync")
                report["checks"].append("Encrypted browser drafts survive reload and upload after reconnect without localStorage credentials")
                native_b.send("model", {"value": "newer-server-model"}); native_b.send("sync")
                native_a.send("model", {"value": "offline-native-model"})
                native_a.send("member", {"name": "codex-added-offline.json", "value": credential("offline")})
                value = native_a.send("sync")
                assert value["conflicts"] >= 1
                assert value["resources"]["assistants"]["pool"]["model"] == "newer-server-model"
                assert len(value["resources"]["assistants"]["members"]) == 3
                report["checks"].append("Concurrent same-field edits preserve an encrypted conflict backup and merge independent member additions")
                page.evaluate("() => loadAccountSync(true)")
                page.locator("#account-open").click()
                page.once("dialog", lambda dialog: dialog.accept())
                page.locator("[data-cloud-restore]").first.click()
                page.wait_for_function("() => !cloudBusy && !cloudPending.assistants && cloudData('assistants').pool.model==='offline-native-model'")
                assert native_b.send("sync")["resources"]["assistants"]["pool"]["model"] == "offline-native-model"
                page.locator('[data-close="settings-dialog"]').click()
                report["checks"].append("Conflict recovery restores chosen fields without discarding independently added members")
                page.locator('[data-member="codex-native.json"] [data-member-toggle]').click()
                page.wait_for_function("() => !cloudBusy && !cloudPending.assistants")
                assert native_b.send("sync")["resources"]["assistants"]["members"]["codex-native.json"]["disabled"]
                page.once("dialog", lambda dialog: dialog.accept())
                page.locator('[data-member="codex-native.json"] [data-member-delete]').click()
                page.wait_for_function("() => !cloudBusy && !cloudPending.assistants")
                assert "codex-native.json" not in native_a.send("sync")["resources"]["assistants"]["members"]
                report["checks"].append("Disabling and deleting a Codex member propagate to native credential files")
                page.evaluate("() => saveCloudTheme('light')")
                page.wait_for_function("() => !cloudBusy && !cloudPending.preferences")
                page.screenshot(path=str(DEST / "web-light.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(path=str(DEST / "web-mobile.png"), full_page=True)
                assert page.evaluate("() => document.documentElement.scrollWidth<=innerWidth")
                report["screenshots"].extend([str(DEST / "web-light.png"), str(DEST / "web-mobile.png")])
                report["checks"].append("Real browser light/dark themes and 390px layout render without horizontal overflow")
                assert not errors, errors
                browser.close()
            native_a.send("sync"); native_b.send("sync")
            other = AccountClient(service.url)
            other.request("register", {"email": "isolated@example.test", "password": "FixturePass-123"})
            isolated = native_a.send("login", {"email": "isolated@example.test"})
            assert not isolated["resources"]["assistants"]["members"]
            assert isolated["resources"]["schedule"]["schedule"] is None
            assert not isolated["memos"]
            native_a.send("login", {"email": user["email"]}); native_a.send("sync")
            report["checks"].append("Switching accounts isolates native timetable, Codex credentials, memos and sync baselines")
            native_a.send("screenshot", {"page": 6, "path": str(DEST / "desktop-pool.png")})
            native_a.send("screenshot", {"page": 4, "components": True, "path": str(DEST / "desktop-components.png")})
            report["screenshots"].extend([str(DEST / "desktop-pool.png"), str(DEST / "desktop-components.png")])
            native_b.close(); agents.remove(native_b)
            port = int(service.url.rsplit(":", 1)[-1]); service.close(); service = None
            native_b = Agent(root / "b"); agents.append(native_b)
            value = native_b.send("model", {"value": "offline-restart-model"})
            assert value["user"]["id"] == user["id"] and "account-workspaces" in value["root"]
            assert len(value["resources"]["assistants"]["members"]) == 2
            service = Service(root, port)
            value = native_b.send("sync")
            assert "离线" not in value["status"], value["status"]
            assert b.api("/api/sync")["resources"]["assistants"]["data"]["pool"]["model"] == "offline-restart-model"
            report["checks"].append("Offline desktop restart restores the cached account workspace and retries after server restart")
            assert b"access-private" not in (root / "server/accounts.sqlite3").read_bytes()
            report["checks"].append("Server database contains encrypted Codex credentials and survives restart with its original key")
            report["ok"] = True
        finally:
            for agent in agents: agent.close()
            if service: service.close()
            atomic(DEST / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--agent": desktop_agent(sys.argv[2])
    else: main()
