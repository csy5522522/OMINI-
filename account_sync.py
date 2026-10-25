"""Durable account workspaces and asynchronous desktop cloud synchronization."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import shutil
import time

from PySide6.QtCore import QObject, QThread, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl

from account_client import AccountClient
from account_data import RESOURCES, merge_documents, validate_resource
from ai_assistants import AssistantStore, atomic_json
from assistant_data import (AssistantState, OperationHistory, backup_credentials,
                            read_protected, write_protected, settings_fingerprint)


def account_key(url, user_id):
    return hashlib.sha256((url + "\n" + user_id).encode()).hexdigest()[:32]


def preferences(settings):
    return copy.deepcopy({k: settings[k] for k in ("theme", "concurrency", "features") if k in settings})


def read_assistants(root):
    root = Path(root)
    store, state = AssistantStore(root), AssistantState(root)
    if not store.writable or not state.writable:
        raise ValueError(store.error or state.error)
    members = {}
    for path in sorted((root / "auths").glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict) and raw.get("type") == "codex":
            members[path.name] = raw
    history = {}
    for record in OperationHistory(root).records(limit=None):
        key = hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        history[key] = record
    return validate_resource("assistants", {
        "profiles": {name: {k: v for k, v in profile.items() if k not in {"program", "cwd"}}
                     for name, profile in store.data["profiles"].items()},
        "pool": {k: v for k, v in store.data["pool"].items() if k in {"strategy", "session_affinity", "member_rules", "model"}},
        "members": members, "quotas": state.data.get("quotas", {}), "models": state.data.get("models", []), "history": history,
    })


def write_assistants(root, value):
    root = Path(root)
    value = validate_resource("assistants", value)
    store, state = AssistantStore(root), AssistantState(root)
    previous_settings = copy.deepcopy(store.data)
    data = copy.deepcopy(store.data)
    for name, profile in value.get("profiles", {}).items():
        data["profiles"][name].update(profile)
    data["pool"].update(value.get("pool", {}))
    if data != store.data or not store.path.exists():
        store.save(data)
    members = value.get("members", {})
    before = {p.name: p for p in (root / "auths").glob("*.json")}
    changed = any(name not in before or json.loads(before[name].read_text(encoding="utf-8-sig")) != raw for name, raw in members.items())
    removed = [p for name, p in before.items() if name not in members and json.loads(p.read_text(encoding="utf-8-sig")).get("type") == "codex"]
    if changed or removed:
        backup_credentials(root)
        for name, raw in members.items():
            if name not in before or json.loads(before[name].read_text(encoding="utf-8-sig")) != raw:
                atomic_json(root / "auths" / name, raw)
        for path in removed:
            path.unlink()
    saved = copy.deepcopy(state.data)
    for key in ("models", "quotas"):
        if key in value: saved[key] = value[key]
    # Refresh clean form drafts while retaining incomplete edits on this device.
    draft = saved.get("draft", {})
    for name, raw in draft.get("profiles", {}).items():
        if raw == previous_settings["profiles"].get(name):
            draft["profiles"][name] = copy.deepcopy(data["profiles"][name])
    pool_draft = draft.get("pool", {})
    if pool_draft and all(v == previous_settings["pool"].get(k) for k, v in pool_draft.items()):
        draft["pool"] = {k: data["pool"].get(k) for k in pool_draft}
    saved["draft"] = draft
    saved["settings_fingerprint"] = settings_fingerprint(data)
    state.save(saved)
    history = OperationHistory(root)
    existing = {hashlib.sha256(json.dumps(r, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                for r in history.records(limit=None)}
    from assistant_data import encode_protected
    for key, record in value.get("history", {}).items():
        if key not in existing:
            # Keep original timestamps and IDs when importing another device's records.
            destination = history.root / "operations-0000-synced.jsonl"
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("ab") as stream:
                stream.write(encode_protected(record) + b"\n")


class SyncWorker(QThread):
    def __init__(self, url, token, snapshot, ack, generation, parent=None):
        super().__init__(parent)
        self.url, self.token = url, token
        self.snapshot, self.ack = copy.deepcopy(snapshot), copy.deepcopy(ack)
        self.generation = generation
        self.result, self.error = None, ""

    def run(self):
        try:
            client = AccountClient(self.url, self.token)
            from urllib.parse import quote
            known = {name: item["revision"] for name, item in self.ack.items()}
            remote = client.api("/api/sync?revisions=" + quote(json.dumps(known), safe=""))
            result = {}
            self.result = {"resources": result, "conflicts": remote.get("conflicts", [])}
            for name in RESOURCES:
                if self.isInterruptionRequested(): return
                previous = self.ack.get(name, {"revision": 0, "data": {}})
                local = self.snapshot[name]
                if local != previous["data"]:
                    result[name] = client.api("/api/sync/" + name, {"revision": previous["revision"], "data": local})
                else:
                    result[name] = previous if remote["resources"][name].get("unchanged") else remote["resources"][name]
        except Exception as error:
            # Credential documents never enter error text or operation logs.
            self.error = str(error) if isinstance(error, ValueError) else "账号同步失败，本机修改已保留，将自动重试。"
        finally:
            self.token = ""


class DesktopAccountSync(QObject):
    def __init__(self, root, window, save_settings):
        super().__init__(window)
        self.base, self.window, self.save_settings = Path(root), window, save_settings
        self.root = self.base
        self.panel = window.settings_page.account
        self.context = None
        self.generation = 0
        self.worker = None
        self.stopping = False
        self.guest_settings = copy.deepcopy(window.settings)
        self.timer = QTimer(self)
        self.timer.setInterval(15000)
        self.timer.timeout.connect(self.sync_now)
        self.timer.start()
        self.panel.account_changed.connect(self.switch_account)
        self.panel.sync_button.clicked.connect(self.sync_now)
        self.panel.backups_button.clicked.connect(self.open_backups)
        if self.panel.user:
            window.update_account_label(self.panel.user)
            QTimer.singleShot(0, self.switch_account)

    def busy(self):
        return any(getattr(getattr(self.window, name, None), "worker", None)
                   for name in ("ai_assistants_page", "timetable_page"))

    def snapshot(self):
        page = getattr(self.window, "ai_assistants_page", None)
        if page and not page.save_session():
            raise ValueError("助手配置保存未完成，请检查操作记录。")
        settings = self.window.settings
        timetable = self.root / "school-timetable.json"
        schedule = json.loads(timetable.read_text(encoding="utf-8")) if timetable.exists() else None
        monday = settings.get("school_timetable", {}).get("start_date", "")
        week = settings.get("school_timetable", {}).get("selected_week", 0)
        return {"preferences": preferences(settings), "schedule": {"schedule": schedule, "first_monday": monday, "selected_week": week},
                "assistants": read_assistants(self.root / "ai-assistants")}

    def persist(self):
        if self.context:
            write_protected(self.root / "desktop.json", {"preferences": preferences(self.window.settings),
                "school_timetable": self.window.settings.get("school_timetable", {})})

    def prepare_switch(self):
        if self.busy():
            self.panel.sync_status.setText("后台操作正在完成，请稍后切换账号。")
            return False
        page = getattr(self.window, "ai_assistants_page", None)
        if page and not page.save_session(): return False
        self.persist()
        return True

    def switch_account(self, *_):
        context = (self.panel.url, self.panel.user["id"]) if self.panel.user else None
        if context == self.context:
            if context and self.panel.token: QTimer.singleShot(0, self.sync_now)
            return
        if getattr(self, "backup_dialog", None): self.backup_dialog.reject()
        self.persist()
        if not self.context:
            self.guest_settings = copy.deepcopy(self.window.settings)
            write_protected(self.base / "guest-desktop.json", self.guest_settings)
        self.generation += 1
        self.retire_pages()
        self.context = context
        self.window.settings["account_workspace_active"] = bool(context)
        self.root = self.base / "account-workspaces" / account_key(*context) if context else self.base
        self.root.mkdir(parents=True, exist_ok=True)
        if context:
            migration = self.base / "account-workspaces" / "migration.json"
            if not migration.exists():
                self.import_guest()
                atomic_json(migration, {"account": account_key(*context)})
            saved, _ = read_protected(self.root / "desktop.json", {})
            portable = saved.get("preferences", preferences(self.window.settings))
            self.window.settings.pop("school_timetable", None)
            self.window.settings["school_timetable"] = saved.get("school_timetable", {})
            self.window.settings.update(portable)
        else:
            for key in ("theme", "concurrency", "features", "school_timetable"):
                if key in self.guest_settings: self.window.settings[key] = copy.deepcopy(self.guest_settings[key])
                else: self.window.settings.pop(key, None)
        self.save_settings(self.window.settings)
        self.refresh_settings()
        self.window.apply_features()
        self.window.apply_theme()
        self.panel.sync_status.setText("正在同步账号工作区…" if context else "登录后自动同步课表、备忘录、偏好与 Codex 成员。")
        QTimer.singleShot(0, self.sync_now)

    def import_guest(self):
        # Only the first account on this installation adopts existing local data.
        # Other accounts start in independent workspaces; original files survive.
        for relative in ("school-timetable.json", "ai-assistants/settings.json", "ai-assistants/state.json"):
            source, target = self.base / relative, self.root / relative
            if source.is_file() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        for relative in ("ai-assistants/auths", "ai-assistants/history"):
            source, target = self.base / relative, self.root / relative
            if source.is_dir(): shutil.copytree(source, target, dirs_exist_ok=True)
        write_protected(self.root / "desktop.json", {"preferences": preferences(self.guest_settings),
            "school_timetable": self.guest_settings.get("school_timetable", {})})

    def retire_pages(self):
        from feature_config import PAGE_FEATURES
        for feature in ("timetable", "ai_assistants"):
            page = getattr(self.window, feature + "_page", None)
            if page:
                page.stop()
                from PySide6.QtWidgets import QWidget
                self.window.replace_page(PAGE_FEATURES.index(feature), QWidget())
                setattr(self.window, feature + "_page", None)

    def refresh_settings(self):
        page = self.window.settings_page
        from feature_config import enabled_features
        for key, flag in enabled_features(self.window.settings).items():
            page.feature_checks[key].setChecked(flag)
        page.concurrency.setValue(self.window.settings.get("concurrency", 2))
        page.theme.setCurrentText(self.window.settings.get("theme", "深色"))

    def sync_now(self):
        if self.stopping or not self.context or self.worker or self.busy(): return
        if not self.panel.token:
            self.panel.sync_status.setText("登录已过期，本机修改已保留；重新登录后继续同步。")
            return
        try:
            snapshot = self.snapshot()
            self.persist()
            ack, _ = read_protected(self.root / "sync.json", {})
            self.worker = SyncWorker(self.panel.url, self.panel.token, snapshot, ack, self.generation, self)
            self.worker.finished.connect(self.finished)
            self.panel.sync_status.setText("正在同步课表、偏好与 Codex 成员…")
            self.worker.start()
            notes = getattr(self.window, "notes_page", None)
            if notes: notes.sync_now()
        except (OSError, ValueError, RuntimeError) as error:
            self.panel.sync_status.setText(str(error))

    def finished(self):
        worker = self.worker
        if worker is None: return
        if self.busy() and not self.stopping and worker.generation == self.generation:
            QTimer.singleShot(100, self.finished)
            return
        self.worker = None
        worker.deleteLater()
        if self.stopping: return
        if worker.generation != self.generation:
            QTimer.singleShot(0, self.sync_now)
            return
        if worker.error and (not worker.result or not worker.result["resources"]):
            self.panel.sync_status.setText("离线修改已保留 · " + worker.error)
            return
        if not worker.result: return
        try:
            current = self.snapshot()
            applied = copy.deepcopy(current)
            ack, _ = read_protected(self.root / "sync.json", {})
            conflicts = 0
            for name, result in worker.result["resources"].items():
                ack[name] = {"revision": result["revision"], "data": result["data"]}
                # Preserve edits made on this device while its request was in flight.
                applied[name], _ = merge_documents(worker.snapshot[name], result["data"], current[name])
                if result.get("conflict_id"):
                    conflicts += 1
                    write_protected(self.root / "conflicts" / (result["conflict_id"] + ".json"),
                                    {"name": name, "data": worker.snapshot[name], "paths": result["conflicts"]})
            self.apply(applied, current)
            write_protected(self.root / "sync.json", ack)
            self.persist()
            count = len(applied["assistants"].get("members", {}))
            self.panel.sync_status.setText(f"已同步 · {time.strftime('%H:%M:%S')} · {count} 个 Codex 成员" +
                (f" · {conflicts} 项并发修改已保留加密备份" if conflicts else ""))
            if worker.error: self.panel.sync_status.setText("部分数据已同步，其余修改保留待重试 · " + worker.error)
            elif any(applied[name] != ack.get(name, {}).get("data") for name in applied):
                self.panel.sync_status.setText("同步期间的新编辑已保存在本机，正在继续同步…")
                QTimer.singleShot(500, self.sync_now)
        except (OSError, ValueError, RuntimeError) as error:
            self.panel.sync_status.setText("同步内容已保留 · " + str(error))

    def apply(self, value, before):
        from feature_config import enabled_features
        requested = enabled_features({**self.window.settings, **value["preferences"]})
        if not self.window.prepare_features(requested, warn=False):
            raise ValueError("当前笔记尚未保存，云端功能设置将在保存后重试。")
        self.window.settings.update(value["preferences"])
        if value["schedule"] != before["schedule"]:
            schedule = value["schedule"]
            if schedule.get("schedule") is not None:
                atomic_json(self.root / "school-timetable.json", schedule["schedule"])
            else:
                (self.root / "school-timetable.json").unlink(missing_ok=True)
            self.window.settings.setdefault("school_timetable", {})["start_date"] = schedule.get("first_monday", "")
            if "selected_week" in schedule:
                self.window.settings["school_timetable"]["selected_week"] = schedule["selected_week"]
            page = getattr(self.window, "timetable_page", None)
            if page: page.apply_cloud(schedule)
        if value["assistants"] != before["assistants"]:
            write_assistants(self.root / "ai-assistants", value["assistants"])
            page = getattr(self.window, "ai_assistants_page", None)
            if page: page.apply_cloud()
        self.window.manager.settings = self.window.settings
        if value["preferences"] != before["preferences"] or value["schedule"] != before["schedule"]:
            self.save_settings(self.window.settings)
        if value["preferences"] != before["preferences"]:
            self.refresh_settings()
        if value["preferences"].get("features") != before["preferences"].get("features"):
            self.window.apply_features()
        if value["preferences"].get("theme") != before["preferences"].get("theme"):
            self.window.apply_theme()

    def open_backups(self):
        generation = self.generation
        path = self.root / "conflicts"
        path.mkdir(parents=True, exist_ok=True)
        from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout
        from ai_assistants import redact
        dialog = QDialog(self.window); dialog.setWindowTitle("同步冲突备份与恢复"); dialog.resize(700, 520)
        layout = QVBoxLayout(dialog)
        note = QLabel("选择本机并发编辑备份，恢复冲突字段后会重新同步。其他设备的备份可在网页偏好设置中查看。")
        note.setWordWrap(True); layout.addWidget(note)
        choices = QComboBox(); layout.addWidget(choices)
        for file in sorted(path.glob("*.json"), reverse=True): choices.addItem(file.stem, file)
        preview = QPlainTextEdit(); preview.setReadOnly(True); layout.addWidget(preview, 1)
        row = QHBoxLayout(); restore = QPushButton("恢复选中的冲突编辑"); directory = QPushButton("打开加密备份目录")
        row.addWidget(restore); row.addWidget(directory); layout.addLayout(row)
        directory.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))
        def show():
            file = choices.currentData()
            if file:
                value, _ = read_protected(file, {})
                preview.setPlainText(redact(json.dumps(value, ensure_ascii=False, indent=2)))
            else: preview.setPlainText("本机尚无同步冲突备份。")
            restore.setEnabled(bool(file) and self.context is not None)
        def recover():
            if generation != self.generation:
                dialog.reject(); return
            if self.worker or self.busy():
                note.setText("当前后台操作正在完成，请稍后恢复。"); return
            try:
                value, _ = read_protected(choices.currentData(), {})
                before = self.snapshot(); restored = copy.deepcopy(before)
                name = value["name"]
                restored[name] = restore_conflict_fields(before[name], value["data"], value["paths"])
                validate_resource(name, restored[name])
                self.apply(restored, before); self.persist(); self.sync_now(); dialog.accept()
            except (OSError, ValueError, KeyError, RuntimeError):
                note.setText("备份恢复未完成，原文件和当前内容已保留。")
        choices.currentIndexChanged.connect(show); restore.clicked.connect(recover)
        show(); self.backup_dialog = dialog; dialog.open()

    def stop(self):
        self.stopping = True
        self.timer.stop()
        self.persist()
        if self.worker: self.worker.requestInterruption()


def restore_conflict_fields(current, incoming, paths):
    result = copy.deepcopy(current)
    for path in paths:
        if path == "document": return copy.deepcopy(incoming)
        keys = path.split("/")
        source, target = incoming, result
        for key in keys[:-1]:
            source = source.get(key, {})
            target = target.setdefault(key, {})
        if keys[-1] in source: target[keys[-1]] = copy.deepcopy(source[keys[-1]])
        else: target.pop(keys[-1], None)
    return result
