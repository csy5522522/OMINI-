"""Offline native TXT editor; no cloud session or browser runtime."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
    QVBoxLayout, QWidget,
)

from text_notes import TextNotesStore, atomic_text, read_text_file


class TextNotesPage(QWidget):
    def __init__(self, config_path, parent=None):
        super().__init__(parent)
        self.config_path = Path(config_path)
        self.worker = None
        self.dark = True
        self.current_path = None
        self.digest = ""
        self.dirty = False
        self.loading = False
        self.config = {}
        startup_error = ""
        try:
            if self.config_path.is_file():
                self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
                if not isinstance(self.config, dict):
                    raise ValueError("笔记设置格式错误")
            folder = self.config.get("directory") or str(self.config_path.parent / "text-notes")
            self.store = TextNotesStore(Path(folder))
        except (OSError, ValueError, TypeError):
            self.config = {}
            self.store = TextNotesStore(self.config_path.parent / "text-notes")
            startup_error = "上次笔记目录读取失败，当前使用默认目录；可点击「选择目录」重新选择。"
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(800)
        self.timer.timeout.connect(self.flush)
        layout = QVBoxLayout(self); layout.setContentsMargins(28, 24, 28, 20)
        title = QLabel("离线 TXT 笔记"); title.setObjectName("pageTitle"); layout.addWidget(title)
        subtitle = QLabel("直接保存为本机 .txt 文件 · 编辑后自动保存为 UTF-8 · 可用记事本打开")
        subtitle.setObjectName("muted"); layout.addWidget(subtitle)
        toolbar = QHBoxLayout()
        for label, callback in (("新建", self.new_note), ("导入 TXT", self.import_note),
                                ("保存", self.flush), ("另存为", self.save_as),
                                ("重命名", self.rename_note), ("移到回收目录", self.delete_note)):
            button = QPushButton(label); button.clicked.connect(callback); toolbar.addWidget(button)
        toolbar.addStretch(); layout.addLayout(toolbar)
        row = QHBoxLayout()
        self.directory_label = QLabel(str(self.store.directory))
        self.directory_label.setTextFormat(Qt.TextFormat.PlainText)
        self.directory_label.setWordWrap(True); self.directory_label.setObjectName("muted")
        row.addWidget(self.directory_label, 1)
        for label, callback in (("选择目录", self.choose_directory), ("打开目录", self.open_directory),
                                ("重新读取", self.reload)):
            button = QPushButton(label); button.clicked.connect(callback); row.addWidget(button)
        layout.addLayout(row)
        self.search = QLineEdit(); self.search.setPlaceholderText("搜索文件名或正文")
        self.search.textChanged.connect(self.filter_notes); layout.addWidget(self.search)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.notes = QListWidget(); self.notes.currentItemChanged.connect(self.select_note)
        splitter.addWidget(self.notes)
        editor_box = QWidget(); editor_layout = QVBoxLayout(editor_box); editor_layout.setContentsMargins(10, 0, 0, 0)
        self.filename = QLabel("新建或选择一个 TXT 文件"); self.filename.setTextFormat(Qt.TextFormat.PlainText)
        editor_layout.addWidget(self.filename)
        self.editor = QPlainTextEdit(); self.editor.setEnabled(False)
        self.editor.setPlaceholderText("在这里记录，内容只保存在本机所选目录。")
        self.editor.textChanged.connect(self.edited); editor_layout.addWidget(self.editor, 1)
        splitter.addWidget(editor_box); splitter.setSizes([230, 650]); layout.addWidget(splitter, 1)
        self.status = QLabel(startup_error or "所有笔记均为本地 TXT。删除的文件会保留在当前目录的 .trash 文件夹。")
        self.status.setTextFormat(Qt.TextFormat.PlainText); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.save_shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        self.save_shortcut.activated.connect(self.flush)
        self.refresh_files(self.config.get("last_file", ""))

    def _remember(self):
        self.config.update(directory=str(self.store.directory), last_file=self.current_path.name if self.current_path else "")
        atomic_text(self.config_path, json.dumps(self.config, ensure_ascii=False, indent=2))

    def refresh_files(self, selected=""):
        self.notes.blockSignals(True)
        self.notes.clear()
        try:
            for path in self.store.files():
                item = QListWidgetItem(path.name); item.setData(Qt.ItemDataRole.UserRole, str(path)); self.notes.addItem(item)
                if path.name == selected:
                    self.notes.setCurrentItem(item)
        except OSError as error:
            self.status.setText(f"读取目录失败：{error}")
        self.notes.blockSignals(False)
        if not self.notes.currentItem() and self.notes.count():
            self.notes.setCurrentRow(0)
        else:
            self.select_note(self.notes.currentItem(), None)
        self.filter_notes(self.search.text())

    def select_note(self, item, previous):
        path = Path(item.data(Qt.ItemDataRole.UserRole)) if item else None
        if path == self.current_path:
            return
        if not self.flush():
            self.notes.blockSignals(True); self.notes.setCurrentItem(previous); self.notes.blockSignals(False)
            return
        try:
            text, digest = read_text_file(path) if path else ("", "")
        except (OSError, ValueError, UnicodeError) as error:
            self.status.setText(f"读取 TXT 失败：{error}")
            self.notes.blockSignals(True); self.notes.setCurrentItem(previous); self.notes.blockSignals(False)
            return
        self.current_path, self.digest = path, digest
        self.loading = True; self.editor.setPlainText(text); self.loading = False
        self.dirty = False; self.editor.setEnabled(path is not None)
        self.filename.setText(path.name if path else "新建或选择一个 TXT 文件")
        try:
            self._remember()
        except OSError:
            self.status.setText("TXT 已读取；目录偏好保存失败，请检查配置目录权限。")

    def edited(self):
        if not self.loading and self.current_path:
            self.dirty = True
            self.status.setText("正在编辑，稍后自动保存…")
            self.timer.start()

    def flush(self):
        self.timer.stop()
        if not self.dirty or not self.current_path:
            return True
        try:
            self.digest = self.store.save(self.current_path, self.editor.toPlainText(), self.digest)
            self.dirty = False
            self.status.setText(f"已保存 {self.current_path.name} · {datetime.now():%H:%M:%S} · UTF-8")
            return True
        except (OSError, ValueError) as error:
            self.status.setText(f"保存失败：{error}")
            return False

    def new_note(self):
        if not self.flush(): return
        title, accepted = QInputDialog.getText(self, "新建 TXT", "文件名", text=f"笔记 {datetime.now():%Y%m%d-%H%M%S}")
        if not accepted: return
        try:
            path = self.store.create(title)
            self.search.clear(); self.refresh_files(path.name); self.editor.setFocus()
        except (OSError, ValueError) as error:
            self.status.setText(f"新建失败：{error}")

    def import_note(self):
        if not self.flush(): return
        source, _ = QFileDialog.getOpenFileName(self, "导入 TXT 副本", "", "纯文本 (*.txt);;全部文件 (*)")
        if source: self.import_file(Path(source))

    def import_file(self, source):
        if not self.flush(): return False
        try:
            source = Path(source)
            if source.resolve().parent == self.store.directory:
                self.search.clear(); self.refresh_files(source.name); return True
            text, _ = read_text_file(source)
            stem = source.stem
            index = 1
            while self.store.target(stem).exists():
                stem = f"{source.stem} ({index})"; index += 1
            path = self.store.create(stem, text)
            self.search.clear(); self.refresh_files(path.name)
            self.status.setText("已导入 UTF-8 副本，原始文件保留。")
            return True
        except (OSError, ValueError, UnicodeError) as error:
            self.status.setText(f"导入失败：{error}"); return False

    def save_as(self):
        if not self.current_path: return
        destination, _ = QFileDialog.getSaveFileName(self, "另存为 TXT", str(self.current_path), "纯文本 (*.txt)")
        if not destination: return
        path = Path(destination)
        if not path.suffix: path = path.with_suffix(".txt")
        if path.resolve() == self.current_path.resolve():
            self.flush(); return
        try:
            atomic_text(path, self.editor.toPlainText())
            self.status.setText(f"已另存为：{path}")
        except OSError as error:
            self.status.setText(f"另存为失败：{error}")

    def rename_note(self):
        if not self.current_path or not self.flush(): return
        title, accepted = QInputDialog.getText(self, "重命名 TXT", "文件名", text=self.current_path.stem)
        if not accepted: return
        try:
            target = self.store.target(title)
            if target == self.current_path: return
            if target.exists(): raise ValueError("同名文件已存在。")
            self.current_path.rename(target)
            self.current_path = None
            self.search.clear(); self.refresh_files(target.name)
        except (OSError, ValueError) as error:
            self.status.setText(f"重命名失败：{error}")

    def delete_note(self):
        if not self.current_path or not self.flush(): return
        try:
            destination = self.store.trash(self.current_path)
            self.current_path = None; self.digest = ""
            self.loading = True; self.editor.clear(); self.loading = False; self.editor.setEnabled(False)
            self.filename.setText("新建或选择一个 TXT 文件")
            self.refresh_files(); self.status.setText(f"已移到回收目录：{destination}")
        except OSError as error:
            self.status.setText(f"移动失败：{error}")

    def filter_notes(self, text):
        query = text.casefold().strip()
        for index in range(self.notes.count()):
            item = self.notes.item(index)
            content = ""
            if query and query not in item.text().casefold():
                try:
                    content, _ = read_text_file(Path(item.data(Qt.ItemDataRole.UserRole)))
                except (OSError, ValueError, UnicodeError):
                    pass
            item.setHidden(bool(query and query not in item.text().casefold() and query not in content.casefold()))

    def choose_directory(self):
        if not self.flush(): return
        directory = QFileDialog.getExistingDirectory(self, "选择 TXT 笔记目录", str(self.store.directory))
        if not directory: return
        try:
            store = TextNotesStore(Path(directory))
            store.files()  # Check access before switching.
            self.store = store; self.current_path = None
            self.directory_label.setText(str(store.directory))
            self.loading = True; self.editor.clear(); self.loading = False
            self.editor.setEnabled(False); self.filename.setText("新建或选择一个 TXT 文件")
            self._remember(); self.search.clear(); self.refresh_files()
        except (OSError, ValueError) as error:
            self.status.setText(f"选择目录失败：{error}")

    def open_directory(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.store.directory)))

    def reload(self):
        if self.dirty and QMessageBox.question(self, "重新读取 TXT", "放弃当前未保存编辑并读取磁盘内容？") != QMessageBox.StandardButton.Yes:
            return
        self.timer.stop(); self.dirty = False
        selected = self.current_path.name if self.current_path else ""
        self.current_path = None; self.editor.setEnabled(False)
        self.refresh_files(selected)

    def apply_theme(self, dark):
        self.dark = dark

    def stop(self):
        self.flush(); self.timer.stop()
