"""Offline native TXT editor; no cloud session or browser runtime."""
from __future__ import annotations

from datetime import datetime
import base64
import hashlib
import html
import json
from pathlib import Path
import re

from PySide6.QtCore import QTimer, Qt, QUrl, QThread, QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut, QImage, QTextDocument
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
    QVBoxLayout, QWidget, QTextBrowser, QDialog,
)

from text_notes import TextNotesStore, atomic_text, read_text_file


class MemoSyncWorker(QThread):
    def __init__(self, directory, url, token, parent):
        super().__init__(parent); self.directory, self.url, self.token = directory, url, token

    def run(self):
        try:
            from memo_sync import sync_memos
            self.result = sync_memos(self.directory, self.url, self.token)
        except Exception as error:
            self.result = {"error": str(error) if isinstance(error, ValueError) else "同步暂未完成，本机内容和历史版本均已保留。"}


class MemoEditor(QPlainTextEdit):
    def insertFromMimeData(self, source):
        if source.hasImage() and hasattr(self, "image_pasted"):
            raw = QByteArray(); buffer = QBuffer(raw); buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            QImage(source.imageData()).save(buffer, "PNG"); buffer.close()
            self.image_pasted(bytes(raw), "粘贴图片.png")
        else: super().insertFromMimeData(source)


class TextNotesPage(QWidget):
    def __init__(self, config_path, parent=None, account=None):
        super().__init__(parent)
        self.config_path = Path(config_path)
        self.worker = None
        self.dark = True
        self.current_path = None
        self.digest = ""
        self.dirty = False
        self.loading = False
        self.config = {}
        self.account = account
        self.context = None
        self.pending_account = False
        self.stopping = False
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
        title = QLabel("超级备忘录"); title.setObjectName("pageTitle"); layout.addWidget(title)
        subtitle = QLabel("文字与图片 · 自动保存 · 写入动作版本 · 历史回滚 · 同账号跨端同步")
        subtitle.setObjectName("muted"); layout.addWidget(subtitle)
        toolbar = QHBoxLayout()
        for label, callback in (("新建", self.new_note), ("导入 TXT", self.import_note),
                                ("保存", self.flush), ("另存为", self.save_as),
                                ("重命名", self.rename_note), ("移到回收目录", self.delete_note)):
            button = QPushButton(label); button.clicked.connect(callback); toolbar.addWidget(button)
        toolbar.addStretch(); layout.addLayout(toolbar)
        actions = QHBoxLayout()
        for label, callback in (("插入图片", self.insert_image), ("历史版本 / 回滚", self.show_history),
                                ("回收站", self.show_trash), ("导入本机备忘录", self.import_local)):
            button = QPushButton(label); button.clicked.connect(callback); actions.addWidget(button)
        self.sync_button = QPushButton("立即同步"); self.sync_button.clicked.connect(self.sync_now); actions.addWidget(self.sync_button)
        self.login_button = QPushButton("账号登录 / 注册"); self.login_button.clicked.connect(self.open_account); actions.addWidget(self.login_button)
        layout.addLayout(actions)
        self.account_label = QLabel("未登录 · 内容保存在本机，登录后可同步账号备忘录")
        self.account_label.setTextFormat(Qt.TextFormat.PlainText); layout.addWidget(self.account_label)
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
        self.editor = MemoEditor(); self.editor.setEnabled(False)
        self.editor.image_pasted = self.insert_raw_image
        self.editor.setPlaceholderText("记录文字，或粘贴图片。编辑后自动保存，历史版本随时可回滚。")
        self.editor.textChanged.connect(self.edited); editor_layout.addWidget(self.editor, 1)
        self.preview = QTextBrowser(); self.preview.setOpenExternalLinks(False); self.preview.setMaximumHeight(250)
        editor_layout.addWidget(self.preview)
        splitter.addWidget(editor_box); splitter.setSizes([230, 650]); layout.addWidget(splitter, 1)
        self.status = QLabel(startup_error or "编辑自动保存并保留动作版本；删除后可从回收站恢复。登录后可同步账号文字与图片。")
        self.status.setTextFormat(Qt.TextFormat.PlainText); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.save_shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        self.save_shortcut.activated.connect(self.flush)
        self.refresh_files(self.config.get("last_file", ""))
        self.sync_timer = QTimer(self); self.sync_timer.setInterval(20000); self.sync_timer.timeout.connect(self.sync_now)
        if self.account:
            self.account.account_changed.connect(self.switch_account)
            QTimer.singleShot(0, self.switch_account)

    def _remember(self):
        if self.context: return
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
        self.render_preview()
        try:
            self._remember()
        except OSError:
            self.status.setText("TXT 已读取；目录偏好保存失败，请检查配置目录权限。")

    def edited(self):
        if not self.loading and self.current_path:
            self.dirty = True
            self.status.setText("正在编辑，稍后自动保存…")
            self.timer.start()
            self.render_preview()

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
            self.store.rename(self.current_path, target)
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
        if self.context:
            self.status.setText("账号缓存目录按账号隔离；可用「导入 TXT」或「导入本机备忘录」添加其他目录中的内容。")
            return
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
        self.stopping = True
        self.flush(); self.timer.stop(); self.sync_timer.stop()

    def open_account(self):
        window = self.window()
        if hasattr(window, "open_account"): window.open_account()

    def switch_account(self, *_):
        if self.stopping: return
        if self.worker:
            self.pending_account = True; return
        if not self.flush(): return
        context = (self.account.url, self.account.user["id"]) if self.account and self.account.user else None
        if context == self.context: return
        self.context = context
        if context:
            key = hashlib.sha256((context[0] + "\n" + context[1]).encode()).hexdigest()[:32]
            directory = self.config_path.parent / "super-memos" / key
            self.account_label.setText("已登录：" + self.account.user["email"] + " · 账号内容与本机笔记分开保存")
            self.sync_timer.start()
        else:
            directory = Path(self.config.get("directory") or self.config_path.parent / "text-notes")
            self.account_label.setText("未登录 · 内容保存在本机，登录后可同步账号备忘录")
            self.sync_timer.stop()
        self.store = TextNotesStore(directory); self.current_path = None; self.dirty = False
        self.loading = True; self.editor.clear(); self.loading = False
        self.directory_label.setText(str(directory)); self.refresh_files()
        if context: self.sync_now()

    def sync_now(self):
        if self.stopping or not self.account or not self.account.user or not self.context or self.worker: return
        if not self.flush(): return
        self.worker = MemoSyncWorker(self.store.directory, self.account.url, self.account.token, self)
        self.worker.finished.connect(self.sync_finished)
        self.setEnabled(False); self.status.setText("正在同步文字、图片与历史动作…")
        self.worker.start()

    def sync_finished(self):
        result = self.worker.result; self.worker.deleteLater(); self.worker = None
        self.setEnabled(True)
        if self.stopping: return
        if self.pending_account:
            self.pending_account = False; self.switch_account(); return
        selected = self.current_path.name if self.current_path else ""
        self.current_path = None; self.refresh_files(selected)
        if "error" in result: self.status.setText(result["error"])
        else: self.status.setText(f"同步完成 · {result['count']} 篇备忘录" + (f" · 已保留 {result['conflicts']} 个冲突副本" if result["conflicts"] else ""))

    def insert_image(self):
        if not self.current_path: return
        filename, _ = QFileDialog.getOpenFileName(self, "插入图片", "", "图片 (*.png *.jpg *.jpeg *.gif *.webp)")
        if filename:
            path = Path(filename)
            if path.stat().st_size > 10 * 1024 * 1024:
                self.status.setText("图片最大为 10 MB。"); return
            self.insert_raw_image(path.read_bytes(), path.name)

    def insert_raw_image(self, raw, name):
        if not self.current_path: return
        try:
            asset = self.store.memos.put_asset(self.store.owner, name, base64.b64encode(raw).decode())
            safe_name = name.replace("]", "_").replace("\n", "_")
            self.editor.insertPlainText(f"\n![{safe_name}](omini-asset://{asset['id']})\n")
            self.flush()
        except (OSError, ValueError) as error: self.status.setText(str(error))

    def render_preview(self):
        if not hasattr(self, "preview"): return
        text = html.escape(self.editor.toPlainText())
        assets = re.findall(r"omini-asset://([a-f0-9]{64})", text)
        text = re.sub(r"!\[([^\]\n]*)\]\(omini-asset://([a-f0-9]{64})\)",
                      lambda m: f'<br><img src="memo://{m[2]}" width="400"><br>', text)
        self.preview.setHtml('<div style="white-space:pre-wrap">' + text + '</div>')
        for asset_id in set(assets):
            try:
                asset = self.store.memos.asset(self.store.owner, asset_id)
                image = QImage.fromData(asset["data"])
                if image.width() > 600: image = image.scaledToWidth(600, Qt.TransformationMode.SmoothTransformation)
                self.preview.document().addResource(QTextDocument.ResourceType.ImageResource, QUrl("memo://" + asset_id), image)
            except (OSError, ValueError): pass
        self.preview.viewport().update()

    def show_history(self):
        if not self.current_path or not self.flush(): return
        memo = self.store.for_path(self.current_path)
        versions = self.store.memos.history(self.store.owner, memo["id"])
        dialog = QDialog(self); dialog.setWindowTitle("历史动作与版本回滚"); dialog.resize(760, 580)
        layout = QVBoxLayout(dialog); layout.addWidget(QLabel("每次保存均保留快照；回滚生成新版本，原历史继续保留。"))
        listing = QListWidget(); layout.addWidget(listing)
        labels = {"create": "新建", "edit": "编辑", "rename": "重命名", "delete": "删除", "rollback": "回滚", "restore": "恢复", "sync": "云端更新", "import": "导入"}
        for version in versions:
            item = QListWidgetItem(f"v{version['version']} · {labels.get(version['action'], version['action'])} · {datetime.fromtimestamp(version['created']):%Y-%m-%d %H:%M:%S}")
            item.setData(Qt.ItemDataRole.UserRole, version); listing.addItem(item)
        preview = QPlainTextEdit(); preview.setReadOnly(True); layout.addWidget(preview, 1)
        listing.currentItemChanged.connect(lambda item, _: preview.setPlainText(item.data(Qt.ItemDataRole.UserRole)["content"]) if item else None)
        listing.setCurrentRow(0)
        button = QPushButton("回滚到所选版本"); layout.addWidget(button)
        def restore():
            item = listing.currentItem()
            if not item: return
            if QMessageBox.question(dialog, "确认回滚", "创建恢复所选内容的新版本？当前版本仍保留。") != QMessageBox.StandardButton.Yes: return
            try:
                version = item.data(Qt.ItemDataRole.UserRole)
                _, path = self.store.write_snapshot({**version, "id": memo["id"], "deleted": bool(version["deleted"])}, "rollback")
                self.current_path = None; self.refresh_files(path.name); dialog.accept(); self.status.setText("已回滚并保留原历史，下次同步会上传此动作。")
            except (OSError, ValueError) as error: self.status.setText(str(error))
        button.clicked.connect(restore); dialog.exec()

    def show_trash(self):
        if not self.flush(): return
        deleted = [m for m in self.store.memos.listing(self.store.owner) if m["deleted"]]
        if not deleted: self.status.setText("回收站为空。"); return
        labels = [f"{m['title']} · v{m['version']}" for m in deleted]
        choice, accepted = QInputDialog.getItem(self, "恢复备忘录", "选择要恢复的备忘录", labels, editable=False)
        if not accepted: return
        try:
            memo = deleted[labels.index(choice)]
            _, path = self.store.write_snapshot({**memo, "deleted": False}, "restore")
            self.current_path = None; self.refresh_files(path.name); self.status.setText("备忘录已恢复，全部版本继续保留。")
        except (OSError, ValueError) as error: self.status.setText(str(error))

    def import_local(self):
        if not self.flush(): return
        directory = QFileDialog.getExistingDirectory(self, "导入本机 TXT 备忘录副本", str(self.config_path.parent / "text-notes"))
        if not directory: return
        try:
            source = TextNotesStore(Path(directory))
            if source.directory == self.store.directory: return
            for path in source.files():
                text, _ = read_text_file(path)
                for asset_id in set(re.findall(r"omini-asset://([a-f0-9]{64})", text)):
                    asset = source.memos.asset(source.owner, asset_id)
                    self.store.memos.put_asset(self.store.owner, asset["name"], base64.b64encode(asset["data"]).decode())
                self.import_file(path)
            self.status.setText("本机备忘录副本已导入；原文件保留。")
        except (OSError, ValueError) as error: self.status.setText(str(error))
