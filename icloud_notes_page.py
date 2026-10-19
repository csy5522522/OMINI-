"""Official iCloud web session and a separate, encrypted offline notes workspace."""
from __future__ import annotations

from datetime import datetime, timezone
import uuid

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal, QSize
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QSplitter, QTabWidget,
    QTextEdit, QVBoxLayout, QWidget,
)

from icloud_notes import NOTES_URL, NotesStore, import_notes_har


class ImportWorker(QThread):
    def __init__(self, path, parent):
        super().__init__(parent)
        self.path = path; self.result = None; self.error = ""

    def run(self):
        try:
            self.result = import_notes_har(self.path)
        except (OSError, ValueError, TypeError):
            self.error = "HAR 导入失败，请检查文件是否包含 iCloud 备忘录的有效响应。"


class ICloudNotesPage(QWidget):
    def __init__(self, cache_path, parent=None):
        super().__init__(parent)
        self.store = NotesStore(cache_path)
        self.worker = None; self.web = None; self.profile = None
        self.current_id = None; self.stopping = False; self.dark = True
        self.save_timer = QTimer(self); self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(450); self.save_timer.timeout.connect(self.flush)
        self.dirty = False
        layout = QVBoxLayout(self); layout.setContentsMargins(24, 20, 24, 20)
        header = QHBoxLayout()
        title = QLabel("iCloud 备忘录"); title.setObjectName("pageTitle")
        header.addWidget(title); header.addStretch()
        self.import_button = QPushButton("导入 HAR 本地副本")
        self.import_button.clicked.connect(self.choose_har); header.addWidget(self.import_button)
        layout.addLayout(header)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1)
        self.online = QWidget(); self.tabs.addTab(self.online, "官方在线")
        self.local = QWidget(); self.local.setObjectName("notesWorkspace")
        self.tabs.addTab(self.local, "本地副本")
        self._build_online(); self._build_local()
        self.status = QLabel(self.store.error or "官方页面实时同步 iCloud；本地副本单独保存，编辑仅保存在本机。")
        self.status.setWordWrap(True); self.status.setObjectName("muted"); layout.addWidget(self.status)
        self.refresh_folders()

    def _build_online(self):
        self.online_layout = QVBoxLayout(self.online)
        row = QHBoxLayout()
        self.open_button = QPushButton("打开 iCloud 备忘录"); self.open_button.setObjectName("primary")
        self.open_button.clicked.connect(self.open_online)
        self.reload_button = QPushButton("刷新"); self.reload_button.clicked.connect(self.reload_online)
        self.reload_button.setEnabled(False)
        external = QPushButton("在浏览器打开"); external.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(NOTES_URL)))
        for button in (self.open_button, self.reload_button, external): row.addWidget(button)
        row.addStretch(); self.online_layout.addLayout(row)
        self.online_status = QLabel("在官方页面登录 Apple 账户，即可查看、编辑和同步备忘录。内置浏览器使用独立临时会话。")
        self.online_status.setWordWrap(True); self.online_layout.addWidget(self.online_status)
        self.online_empty = QLabel("☁\n\niCloud 备忘录\n\n点击上方按钮打开官方页面\n关闭工具箱后，内置登录会话随之结束")
        self.online_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.online_layout.addWidget(self.online_empty, 1)

    def _build_local(self):
        outer = QVBoxLayout(self.local); outer.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        for text, attr, callback in (("新建备忘录", "new_button", self.new_note),
                                     ("置顶", "pin_button", self.toggle_pin),
                                     ("移到最近删除", "delete_button", self.delete_note),
                                     ("恢复", "restore_button", self.restore_note),
                                     ("导出文本", "export_button", self.export_note)):
            button = QPushButton(text); button.clicked.connect(callback)
            setattr(self, attr, button); bar.addWidget(button)
        bar.addStretch(); outer.addLayout(bar)
        self.splitter = QSplitter()
        folder_panel = QWidget(); folder_panel.setObjectName("notesFolders")
        folder_layout = QVBoxLayout(folder_panel); folder_layout.setContentsMargins(10, 12, 10, 12)
        label = QLabel("文件夹"); label.setObjectName("notesSection"); folder_layout.addWidget(label)
        self.folders = QListWidget(); self.folders.setObjectName("notesFoldersList")
        self.folders.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.folders.currentRowChanged.connect(self.refresh_notes); folder_layout.addWidget(self.folders, 1)
        add_folder = QPushButton("＋ 新建文件夹"); add_folder.clicked.connect(self.new_folder)
        folder_layout.addWidget(add_folder); self.splitter.addWidget(folder_panel)
        list_panel = QWidget(); list_panel.setObjectName("notesListPanel")
        list_layout = QVBoxLayout(list_panel); list_layout.setContentsMargins(10, 12, 10, 12)
        self.search = QLineEdit(); self.search.setPlaceholderText("搜索所有备忘录")
        self.search.setClearButtonEnabled(True); self.search.textChanged.connect(self.refresh_notes)
        list_layout.addWidget(self.search)
        self.count = QLabel(); self.count.setObjectName("muted"); list_layout.addWidget(self.count)
        self.notes = QListWidget(); self.notes.setObjectName("notesList")
        self.notes.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.notes.currentItemChanged.connect(self.select_note); list_layout.addWidget(self.notes, 1)
        self.splitter.addWidget(list_panel)
        editor_panel = QWidget(); editor_panel.setObjectName("notesEditorPanel")
        editor_layout = QVBoxLayout(editor_panel); editor_layout.setContentsMargins(24, 18, 24, 18)
        self.note_meta = QLabel("本地副本 · Windows 本机加密")
        self.note_meta.setObjectName("muted"); self.note_meta.setWordWrap(True); editor_layout.addWidget(self.note_meta)
        self.title_edit = QLineEdit(); self.title_edit.setObjectName("notesTitle")
        self.title_edit.setPlaceholderText("标题"); self.title_edit.textEdited.connect(self.edit_note)
        editor_layout.addWidget(self.title_edit)
        self.editor = QTextEdit(); self.editor.setAcceptRichText(False); self.editor.setObjectName("notesEditor")
        self.editor.setFont(QFont("Microsoft YaHei UI", 12))
        self.editor.setPlaceholderText("导入 HAR 查看本地副本，或新建一条备忘录。")
        self.editor.textChanged.connect(self.edit_note); editor_layout.addWidget(self.editor, 1)
        self.splitter.addWidget(editor_panel)
        for i, width in enumerate((150, 240, 480)):
            self.splitter.widget(i).setMinimumWidth(115 if i == 0 else 180)
        self.splitter.setSizes([150, 240, 480]); self.splitter.setChildrenCollapsible(False)
        self.splitter.setStretchFactor(2, 1); outer.addWidget(self.splitter, 1)

    def open_online(self):
        if self.stopping: return
        self.tabs.setCurrentWidget(self.online)
        if self.web:
            self.web.setFocus(); return
        try:
            from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
            from PySide6.QtWebEngineWidgets import QWebEngineView
            # Profile has no storage name, keeping login cookies and web data in memory.
            self.profile = QWebEngineProfile(self)
            self.web = QWebEngineView(self.online)
            self.web.setPage(QWebEnginePage(self.profile, self.web))
            self.web.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, True)
            self.web.page().newWindowRequested.connect(self._new_window)
            self.web.loadFinished.connect(self._online_loaded)
            self.web.loadStarted.connect(lambda: self.online_status.setText("正在加载 iCloud 官方页面…"))
            self.online_layout.addWidget(self.web, 1); self.online_empty.hide()
            self.reload_button.setEnabled(True)
            self.apply_theme(self.dark)
            self.web.load(QUrl(NOTES_URL))
        except (ImportError, RuntimeError):
            self.online_status.setText("内置浏览器启动失败，可点击「在浏览器打开」继续使用官方备忘录。")

    def _new_window(self, request):
        # Login and Apple verification popups stay inside the same temporary profile.
        from PySide6.QtWebEngineCore import QWebEnginePage
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWidgets import QDialog
        dialog = QDialog(self); dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle("iCloud · 官方页面"); dialog.resize(860, 680)
        view = QWebEngineView(dialog); view.setPage(QWebEnginePage(self.profile, view))
        layout = QVBoxLayout(dialog); layout.addWidget(view)
        view.page().windowCloseRequested.connect(dialog.close)
        request.openIn(view.page()); dialog.show()

    def _online_loaded(self, ok):
        self.online_status.setText("官方在线 · 在页面中登录并使用 iCloud 备忘录；本次登录状态保留至工具箱退出。" if ok
                                   else "官网加载失败，请检查网络后刷新，或点击「在浏览器打开」。本地副本仍可使用。")

    def reload_online(self):
        if self.web: self.web.reload()

    def choose_har(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入 iCloud 备忘录 HAR", "", "HAR 文件 (*.har)")
        if path: self.start_import(path)

    def start_import(self, path):
        if self.worker or self.stopping: return
        if not self.flush(): return
        self.import_button.setEnabled(False); self.status.setText("正在解析备忘录记录与正文…")
        self.worker = ImportWorker(path, self); self.worker.finished.connect(self._import_finished)
        self.worker.start()

    def _import_finished(self):
        worker = self.worker; self.worker = None; self.import_button.setEnabled(True)
        if not self.stopping:
            if worker.error:
                self.status.setText(worker.error)
            else:
                try:
                    count, kept = self.store.merge(worker.result)
                    self.refresh_folders(); self.tabs.setCurrentWidget(self.local)
                    complete = sum(n["complete"] for n in worker.result["notes"])
                    locked = sum(n["locked"] for n in worker.result["notes"])
                    self.status.setText(f"已导入 {count} 条；完整正文 {complete} 条，锁定 {locked} 条；保留 {kept} 条本地编辑。副本已加密保存。")
                except (OSError, ValueError, RuntimeError):
                    self.status.setText("本地副本保存失败，请检查缓存目录和 Windows 本机加密服务。")
        worker.deleteLater()

    def refresh_folders(self):
        selected = self.folders.currentItem()
        key = selected.data(Qt.ItemDataRole.UserRole) if selected else ("all", "")
        self.folders.blockSignals(True); self.folders.clear()
        groups = [("所有备忘录", ("all", ""))]
        groups += [(name, ("folder", name)) for name in self.store.data["folders"]]
        groups += [("最近删除", ("trash", ""))]
        row = 0
        for i, (label, value) in enumerate(groups):
            item = QListWidgetItem("▱  " + label); item.setData(Qt.ItemDataRole.UserRole, value)
            item.setToolTip(label); self.folders.addItem(item)
            if value == key: row = i
        self.folders.setCurrentRow(row); self.folders.blockSignals(False); self.refresh_notes()

    def refresh_notes(self, *_):
        selected_id = self.current_id
        item = self.folders.currentItem()
        kind, folder = item.data(Qt.ItemDataRole.UserRole) if item else ("all", "")
        query = self.search.text().strip().casefold()
        values = [n for n in self.store.data["notes"] if bool(n.get("deleted")) == (kind == "trash")
                  and (kind != "folder" or n["folder"] == folder)
                  and (not query or query in (n["title"] + "\n" + n["body"]).casefold())]
        values.sort(key=lambda n: (n.get("pinned", False), n.get("modified", "")), reverse=True)
        self.notes.blockSignals(True); self.notes.clear(); row = 0
        for i, note in enumerate(values):
            summary = "已锁定" if note["locked"] else " ".join(note["body"].split())[:46] or "暂无正文"
            title = ("● " if note.get("pinned") else "") + ("🔒 " if note["locked"] else "") + note["title"]
            item = QListWidgetItem(title[:28] + "\n" + summary + "\n▱ " + note["folder"])
            item.setData(Qt.ItemDataRole.UserRole, note["id"]); item.setSizeHint(QSize(180, 88))
            item.setToolTip(note["title"]); self.notes.addItem(item)
            if note["id"] == selected_id: row = i
        self.notes.setCurrentRow(row if values else -1); self.notes.blockSignals(False)
        self.count.setText(f"{len(values)} 条备忘录 · 本地副本")
        self.select_note(self.notes.currentItem(), None)

    def current_note(self):
        return next((n for n in self.store.data["notes"] if n["id"] == self.current_id), None)

    def select_note(self, item, _previous):
        self.current_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        note = self.current_note()
        self.title_edit.blockSignals(True); self.editor.blockSignals(True)
        self.title_edit.setText(note["title"] if note else "")
        self.editor.setPlainText(("这条备忘录已锁定，请在官方在线页面解锁查看。" if note["locked"] else note["body"]) if note else "")
        editable = bool(note and not note["locked"] and not note["deleted"] and not self.store.error)
        self.title_edit.setReadOnly(not editable); self.editor.setReadOnly(not editable)
        self.title_edit.blockSignals(False); self.editor.blockSignals(False)
        self.pin_button.setEnabled(editable)
        self.delete_button.setEnabled(bool(note and not note["deleted"]))
        self.restore_button.setEnabled(bool(note and note["deleted"]))
        self.export_button.setEnabled(bool(note and not note["locked"]))
        self.pin_button.setText("取消置顶" if note and note.get("pinned") else "置顶")
        if note:
            state = "已锁定" if note["locked"] else "正文副本" if note["complete"] else "仅摘要 · HAR 未包含正文"
            try:
                modified = datetime.fromisoformat(note.get("modified", "")).astimezone().strftime("%Y-%m-%d %H:%M")
            except ValueError:
                modified = ""
            self.note_meta.setText(f"{modified} · {state} · 编辑保存在本机")
        else:
            self.note_meta.setText("本地副本 · Windows 本机加密")

    def edit_note(self, *_):
        note = self.current_note()
        if not note or note["locked"] or note["deleted"]: return
        note.update(title=self.title_edit.text() or "未命名备忘录", body=self.editor.toPlainText(),
                    modified=datetime.now(timezone.utc).isoformat(), edited=True)
        item = self.notes.currentItem()
        if item:
            item.setText(("● " if note.get("pinned") else "") + note["title"][:28] + "\n" +
                         " ".join(note["body"].split())[:46] + "\n▱ " + note["folder"])
        self.mark_dirty()

    def mark_dirty(self):
        self.dirty = True; self.save_timer.start(); self.status.setText("正在保存本地副本…")

    def flush(self):
        self.save_timer.stop()
        if not self.dirty: return True
        try:
            self.store.save(); self.dirty = False
            self.status.setText("本地副本已加密保存 · 云端内容请在官方在线页面编辑。")
            return True
        except (OSError, ValueError, RuntimeError):
            self.status.setText("保存失败，编辑仍在内存中。请检查磁盘空间与缓存目录后重试。")
            return False

    def new_note(self):
        if self.store.error: return
        item = self.folders.currentItem()
        kind, folder = item.data(Qt.ItemDataRole.UserRole) if item else ("all", "")
        folder = folder if kind == "folder" else "备忘录"
        self.store.data["notes"].append(dict(id="local:" + uuid.uuid4().hex, title="新建备忘录", body="",
            snippet="", folder=folder, modified=datetime.now(timezone.utc).isoformat(), pinned=False,
            locked=False, complete=True, deleted=False, edited=True))
        self.current_id = self.store.data["notes"][-1]["id"]
        self.search.clear(); self.folders.setCurrentRow(0); self.tabs.setCurrentWidget(self.local)
        self.refresh_notes(); self.title_edit.setFocus(); self.title_edit.selectAll(); self.mark_dirty()

    def new_folder(self):
        name, ok = QInputDialog.getText(self, "新建本地文件夹", "文件夹名称")
        name = name.strip()
        if ok and name and name not in self.store.data["folders"] and not self.store.error:
            self.store.data["folders"].append(name); self.refresh_folders(); self.mark_dirty()

    def toggle_pin(self):
        note = self.current_note()
        if note and not note["locked"] and not note["deleted"]:
            note.update(pinned=not note.get("pinned"), edited=True)
            self.refresh_notes(); self.mark_dirty()

    def delete_note(self):
        note = self.current_note()
        if note and not note["deleted"]:
            note.update(deleted=True, edited=True); self.refresh_notes(); self.mark_dirty()

    def restore_note(self):
        note = self.current_note()
        if note and note["deleted"]:
            note.update(deleted=False, edited=True); self.refresh_notes(); self.mark_dirty()

    def export_note(self):
        note = self.current_note()
        if not note or note["locked"]: return
        path, _ = QFileDialog.getSaveFileName(self, "导出备忘录文本", "备忘录.txt", "文本文件 (*.txt)")
        if path:
            from pathlib import Path
            try:
                Path(path).write_text(note["title"] + "\n\n" + note["body"], encoding="utf-8")
                self.status.setText("已导出备忘录文本。")
            except OSError:
                self.status.setText("导出失败，请选择可写入的文件夹。")

    def apply_theme(self, dark):
        self.dark = dark
        bg, panel, line, fg, selected, accent = (("#1c1c1e", "#222224", "#38383a", "#eeeeef", "#3a3a3c", "#ffd60a") if dark
            else ("#ffffff", "#f5f5f7", "#d8d8dc", "#1d1d1f", "#ffe9a5", "#8a6200"))
        self.local.setStyleSheet(f"""
            QWidget#notesWorkspace, QWidget#notesEditorPanel {{ background: {bg}; color: {fg}; }}
            QWidget#notesFolders, QWidget#notesListPanel {{ background: {panel}; }}
            QLabel {{ background: transparent; }}
            QListWidget#notesFoldersList, QListWidget#notesList {{ background: {panel}; border: 0; outline: none; }}
            QListWidget#notesFoldersList::item {{ padding: 10px 5px; border-radius: 7px; }}
            QListWidget#notesList::item {{ padding: 8px 12px; border-radius: 8px; border-bottom: 1px solid {line}; }}
            QListWidget::item:selected {{ background: {selected}; color: {fg}; }}
            QLabel#notesSection {{ color: {accent}; font-weight: 700; background: transparent; }}
            QLineEdit#notesTitle {{ background: {bg}; border: 0; color: {fg}; font-size: 23px; font-weight: 700; padding: 10px 0; }}
            QTextEdit#notesEditor {{ background: {bg}; color: {fg}; border: 0; padding: 8px 0; font-size: 16px; }}
            QSplitter::handle {{ background: {line}; width: 1px; }}
        """)
        if self.web:
            from PySide6.QtWebEngineCore import QWebEngineSettings
            attribute = getattr(QWebEngineSettings.WebAttribute, "ForceDarkMode", None)
            if attribute is not None: self.web.settings().setAttribute(attribute, dark)

    def stop(self):
        self.stopping = True; self.flush()
        if self.web: self.web.stop()
        if self.worker: self.worker.requestInterruption()
