"""Native encrypted memo editor with isolated account workspaces."""
from __future__ import annotations

from datetime import datetime
import base64
import hashlib
import json
from pathlib import Path
import re

from PySide6.QtCore import QTimer, Qt, QUrl, QThread, QSize, QRectF, QEvent
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut, QIcon, QPixmap, QPainter, QColor, QFont
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QSplitter,
    QVBoxLayout, QWidget, QDialog, QToolButton, QMenu, QFrame, QSizePolicy, QStyledItemDelegate,
)

from text_notes import TextNotesStore, atomic_text, read_text_file
from memo_editor import MemoEditor, ASSET_PATTERN


class MemoSyncWorker(QThread):
    def __init__(self, directory, url, token, parent):
        super().__init__(parent); self.directory, self.url, self.token = directory, url, token
        self.protected = lambda memo_id: memo_id == parent._editing_memo_id

    def run(self):
        try:
            from memo_sync import sync_memos
            self.result = sync_memos(self.directory, self.url, self.token,
                                     protected=self.protected)
        except Exception as error:
            self.result = {"error": str(error) if isinstance(error, ValueError) else "同步暂未完成，本机内容和历史版本均已保留。"}


class MemoListDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        page = self.parent()
        memo = index.data(int(Qt.ItemDataRole.UserRole) + 1) or {}
        selected = page.notes.currentIndex() == index
        painter.save()
        rect = option.rect.adjusted(3, 3, -3, -3)
        if selected:
            painter.setPen(QColor(page.colors["line"]))
            painter.setBrush(QColor(page.colors["surface"]))
            painter.drawRoundedRect(rect, 9, 9)
        left, width = rect.left() + 13, rect.width() - 26
        font = QFont(option.font); font.setPixelSize(13); font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font); painter.setPen(QColor(page.colors["text"]))
        painter.drawText(left, rect.top() + 24, painter.fontMetrics().elidedText(memo.get("title", index.data()), Qt.TextElideMode.ElideRight, width))
        font.setPixelSize(11); font.setWeight(QFont.Weight.Normal); painter.setFont(font)
        painter.setPen(QColor(page.colors["muted"]))
        snippet = re.sub(r"\s+", " ", ASSET_PATTERN.sub(" [图片] ", memo.get("content", ""))).strip() or "空白备忘录"
        painter.drawText(left, rect.top() + 46, painter.fontMetrics().elidedText(snippet, Qt.TextElideMode.ElideRight, width))
        font.setPixelSize(10); painter.setFont(font)
        updated = datetime.fromtimestamp(memo.get("updated", 0))
        date = updated.strftime("今天 %H:%M") if updated.date() == datetime.now().date() else f"{updated.month}月{updated.day}日"
        painter.drawText(left, rect.top() + 68, date)
        painter.restore()

    def sizeHint(self, option, index):
        return QSize(220, 86)


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
        self.checkpoint_timer = QTimer(self)
        self.checkpoint_timer.setInterval(3000)
        self.checkpoint_timer.timeout.connect(self.flush)
        self.checkpoint_timer.start()
        self.current_memo = None
        self._editing_memo_id = None
        self.trash_mode = False
        self.title_dirty = False
        self.title_composing = False
        self._saved_content = ""
        self._document_open = False
        self._build_ui()
        self.apply_theme(True)
        self.set_status(startup_error or "自动保存 · 本机存储已加密")
        self.save_shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        self.save_shortcut.activated.connect(self.flush)
        self.refresh_files(self.config.get("last_file", ""))
        self.sync_timer = QTimer(self); self.sync_timer.setInterval(20000); self.sync_timer.timeout.connect(self.sync_now)
        if self.account:
            self.account.account_changed.connect(self.switch_account)
            QTimer.singleShot(0, self.switch_account)

    def _build_ui(self):
        self.setObjectName("memoPage")
        layout = QVBoxLayout(self); layout.setContentsMargins(26, 20, 26, 20); layout.setSpacing(18)
        heading = QHBoxLayout()
        title = QLabel("超级备忘录"); title.setObjectName("pageTitle"); heading.addWidget(title)
        heading.addStretch()
        caption = QLabel("留一点空间，给想法。"); caption.setObjectName("memoCaption"); heading.addWidget(caption)
        layout.addLayout(heading)
        self.workbench = QSplitter(Qt.Orientation.Horizontal); self.workbench.setObjectName("memoWorkbench")
        self.workbench.setChildrenCollapsible(False); self.workbench.setHandleWidth(1)
        self.library = QFrame(); self.library.setObjectName("memoLibrary")
        side = QVBoxLayout(self.library); side.setContentsMargins(14, 18, 14, 12); side.setSpacing(10)
        row = QHBoxLayout(); row.setContentsMargins(7, 0, 3, 4)
        name = QLabel("我的备忘录"); name.setObjectName("memoLibraryTitle"); row.addWidget(name); row.addStretch()
        self._icon_buttons = []
        self.new_button = self._tool("plus", "新建备忘录", self.new_note); row.addWidget(self.new_button); side.addLayout(row)
        self.search = QLineEdit(); self.search.setObjectName("memoSearch"); self.search.setPlaceholderText("搜索备忘录")
        self.search.setAccessibleName("搜索备忘录"); self.search.textChanged.connect(self.filter_notes); side.addWidget(self.search)
        self.active_button = QPushButton("全部备忘录"); self.active_button.setObjectName("memoNavigation")
        self.active_button.setCheckable(True); self.active_button.setChecked(True); self.active_button.clicked.connect(lambda: self.set_folder(False)); side.addWidget(self.active_button)
        self.notes = QListWidget(); self.notes.setObjectName("memoList"); self.notes.setItemDelegate(MemoListDelegate(self))
        self.notes.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.notes.currentItemChanged.connect(self.select_note); side.addWidget(self.notes, 1)
        bottom = QHBoxLayout()
        self.trash_button = QPushButton("回收站"); self.trash_button.setObjectName("memoNavigation"); self.trash_button.setCheckable(True)
        self.trash_button.clicked.connect(self.show_trash); bottom.addWidget(self.trash_button, 1)
        self.workspace_button = self._tool("folder", "工作区与目录", None)
        workspace_menu = QMenu(self.workspace_button)
        for label, callback in (("选择目录", self.choose_directory), ("打开目录", self.open_directory), ("重新读取", self.reload), ("导入本机备忘录", self.import_local)):
            workspace_menu.addAction(label, callback)
        self.workspace_button.setMenu(workspace_menu); self.workspace_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        bottom.addWidget(self.workspace_button); side.addLayout(bottom)
        self.directory_label = QLabel(str(self.store.directory), self.library); self.directory_label.hide()
        account_row = QHBoxLayout(); account_row.setSpacing(3)
        self.account_label = QLabel("本机工作区"); self.account_label.setObjectName("memoAccount")
        self.account_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred); account_row.addWidget(self.account_label, 1)
        self.sync_button = self._tool("refresh", "立即同步", self.sync_now); account_row.addWidget(self.sync_button)
        self.login_button = self._tool("user", "账号登录 / 注册", self.open_account); account_row.addWidget(self.login_button)
        side.addLayout(account_row)
        self.document_shell = QFrame(); self.document_shell.setObjectName("memoDocumentShell")
        main = QVBoxLayout(self.document_shell); main.setContentsMargins(0, 0, 0, 0); main.setSpacing(0)
        self.back_button = QPushButton("‹  返回列表"); self.back_button.setObjectName("memoBack"); self.back_button.clicked.connect(lambda: self._open_document(False)); main.addWidget(self.back_button, 0, Qt.AlignmentFlag.AlignLeft)
        centered = QHBoxLayout(); centered.setContentsMargins(0, 0, 0, 0); centered.setSpacing(0); centered.addStretch()
        self.document_column = QWidget(); self.document_column.setObjectName("memoDocument"); self.document_column.setMaximumWidth(860)
        document = QVBoxLayout(self.document_column); document.setContentsMargins(42, 38, 42, 22); document.setSpacing(0)
        self.document_kind = QLabel("PERSONAL NOTE"); self.document_kind.setObjectName("memoEyebrow"); document.addWidget(self.document_kind)
        self.filename = QLineEdit(); self.filename.setObjectName("memoTitle"); self.filename.setPlaceholderText("备忘录标题"); self.filename.setMaxLength(240)
        self.filename.setAccessibleName("备忘录标题"); self.filename.textEdited.connect(self.title_edited); document.addWidget(self.filename)
        self.filename.installEventFilter(self)
        self.editor = MemoEditor(); self.editor.setObjectName("memoNativeEditor"); self.editor.setEnabled(False)
        self.editor.setAccessibleName("备忘录正文"); self.editor.image_pasted = self.insert_raw_image
        self.editor.set_asset_loader(lambda asset_id: self.store.memos.asset(self.store.owner, asset_id)["data"])
        self.editor.setPlaceholderText("写下想记住的事…")
        self.editor.textChanged.connect(self.edited); self.editor.compositionChanged.connect(self.composition_changed)
        document.addWidget(self.editor, 1)
        self.empty = QLabel("让想法在这里停留\n\n从左侧选择一篇备忘录，或点击 + 新建一页。")
        self.empty.setObjectName("memoEmpty"); self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter); self.empty.setWordWrap(True); document.addWidget(self.empty, 1)
        centered.addWidget(self.document_column, 1); centered.addStretch(); main.addLayout(centered, 1)
        self.notice_box = QWidget(); self.notice_box.setObjectName("memoNotice")
        notice_row = QHBoxLayout(self.notice_box); notice_row.setContentsMargins(18, 12, 18, 12)
        self.notice = QLabel(); self.notice.setWordWrap(True); self.notice.setTextFormat(Qt.TextFormat.PlainText); notice_row.addWidget(self.notice, 1)
        self.retry_button = QPushButton("重试保存"); self.retry_button.clicked.connect(self.flush); notice_row.addWidget(self.retry_button)
        self.notice_box.hide(); main.addWidget(self.notice_box)
        footer = QFrame(); footer.setObjectName("memoFooter"); footer_row = QHBoxLayout(footer); footer_row.setContentsMargins(20, 10, 16, 10); footer_row.setSpacing(8)
        dot = QLabel("●"); dot.setObjectName("memoStateDot"); footer_row.addWidget(dot)
        self.status = QLabel(); self.status.setTextFormat(Qt.TextFormat.PlainText); self.status.setObjectName("memoStatus")
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred); footer_row.addWidget(self.status, 1)
        self.restore_button = QPushButton("恢复备忘录"); self.restore_button.clicked.connect(self.restore_note); footer_row.addWidget(self.restore_button); self.restore_button.hide()
        self.image_button = self._tool("image", "插入图片", self.insert_image); footer_row.addWidget(self.image_button)
        self.more_button = self._tool("more", "更多操作", None)
        self.more_menu = QMenu(self.more_button)
        self.save_action = self.more_menu.addAction("立即保存\tCtrl+S", self.flush)
        self.history_action = self.more_menu.addAction("历史版本", self.show_history)
        self.rename_action = self.more_menu.addAction("重命名", self.rename_note)
        self.import_action = self.more_menu.addAction("导入 TXT", self.import_note)
        self.save_as_action = self.more_menu.addAction("另存为 TXT", self.save_as)
        self.more_menu.addSeparator(); self.delete_action = self.more_menu.addAction("移到回收站", self.delete_note)
        self.more_button.setMenu(self.more_menu); self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup); footer_row.addWidget(self.more_button)
        main.addWidget(footer)
        self.workbench.addWidget(self.library); self.workbench.addWidget(self.document_shell)
        self.workbench.setSizes([255, 800]); self.workbench.setStretchFactor(1, 1); layout.addWidget(self.workbench, 1)

    def _tool(self, icon, tooltip, callback):
        button = QToolButton(); button.setObjectName("memoTool"); button.setToolTip(tooltip); button.setAccessibleName(tooltip)
        button.setFixedSize(32, 32); button.setIconSize(QSize(18, 18)); self._icon_buttons.append((button, icon))
        if callback: button.clicked.connect(callback)
        return button

    def _icon(self, name):
        paths = {"plus": "M12 5v14M5 12h14", "folder": "M3 5h6l2 3h10v12H3Z", "refresh": "M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 14 6M4 12a8 8 0 0 0 14 6", "user": "M5 21v-2a7 7 0 0 1 14 0v2M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8", "image": "M3 3h18v18H3ZM3 17l6-6 4 4 3-3 5 5M8 7h.1", "more": "M5 12h.1M12 12h.1M19 12h.1"}
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="{paths[name]}" fill="none" stroke="{self.colors["muted"]}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'
        pixmap = QPixmap(36, 36); pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap); QSvgRenderer(svg.encode()).render(painter, QRectF(0, 0, 36, 36)); painter.end()
        return QIcon(pixmap)

    def set_status(self, text):
        self.status.setToolTip(text)
        problem = any(word in text for word in ("失败", "暂未", "冲突", "错误", "已被其他", "最大为", "最多", "请选择", "请输入", "同名文件"))
        self.notice_box.setVisible(problem)
        if problem: self.notice.setText(text)
        self.retry_button.setVisible(problem and self.dirty)
        self.status.setText(text if len(text) < 54 else text.split("；")[0][:48] + "…")
        self.status.setProperty("problem", problem); self.status.style().unpolish(self.status); self.status.style().polish(self.status)

    def _open_document(self, opened=True):
        self._document_open = opened
        narrow = self.width() < 760
        self.library.setMaximumWidth(16777215 if narrow else 300)
        self.library.setMinimumWidth(220)
        self.library.setVisible(not narrow or not opened)
        self.document_shell.setVisible(not narrow or opened)
        self.back_button.setVisible(narrow and opened)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "library"): self._open_document(self._document_open)

    def composition_changed(self, composing):
        if composing: self.timer.stop()
        elif self.dirty: self.timer.start()

    def eventFilter(self, watched, event):
        if watched is self.filename and event.type() == QEvent.Type.InputMethod:
            self.title_composing = bool(event.preeditString())
            self.composition_changed(self.title_composing)
        return super().eventFilter(watched, event)

    def title_edited(self):
        if not self.loading and self.current_path:
            self.title_dirty = True; self.dirty = True; self.set_status("正在编辑 · 自动保存"); self.timer.start()

    def _remember(self):
        if self.context or getattr(self, "workspace_error", False): return
        self.config.update(directory=str(self.store.directory), last_file=self.current_path.name if self.current_path else "")
        atomic_text(self.config_path, json.dumps(self.config, ensure_ascii=False, indent=2))

    def refresh_files(self, selected="", *, preserve_editor=False):
        selected_id = self.current_memo["id"] if self.current_memo else None
        self.notes.blockSignals(True)
        self.notes.clear()
        try:
            if self.trash_mode:
                entries = [(None, memo) for memo in self.store.memos.listing(self.store.owner) if memo["deleted"]]
            else:
                entries = [(path, self.store.for_path(path)) for path in self.store.files()]
            entries.sort(key=lambda entry: entry[1]["updated"], reverse=True)
            for path, memo in entries:
                item = QListWidgetItem(memo["title"])
                item.setData(Qt.ItemDataRole.UserRole, str(path) if path else None)
                item.setData(int(Qt.ItemDataRole.UserRole) + 1, memo)
                item.setToolTip(memo["title"])
                self.notes.addItem(item)
                if path and path.name == selected or not selected and memo["id"] == selected_id:
                    self.notes.setCurrentItem(item)
            all_memos = self.store.memos.listing(self.store.owner)
            self.active_button.setText(f"全部备忘录    {sum(not m['deleted'] for m in all_memos)}")
            self.trash_button.setText(f"回收站    {sum(m['deleted'] for m in all_memos)}")
        except (OSError, ValueError) as error:
            self.set_status(f"读取目录失败：{error}")
        self.notes.blockSignals(False)
        if preserve_editor and self.current_memo:
            self.filter_notes(self.search.text())
            return
        if not self.notes.currentItem() and self.notes.count():
            self.notes.setCurrentRow(0)
        else:
            self.select_note(self.notes.currentItem(), None)
        self.filter_notes(self.search.text())

    def select_note(self, item, previous):
        value = item.data(Qt.ItemDataRole.UserRole) if item else None
        path = Path(value) if value else None
        memo = item.data(int(Qt.ItemDataRole.UserRole) + 1) if item else None
        if memo and self.current_memo and memo["id"] == self.current_memo["id"] and path == self.current_path:
            return
        if not self.flush():
            self.notes.blockSignals(True); self.notes.setCurrentItem(previous); self.notes.blockSignals(False)
            return
        try:
            text, digest = read_text_file(path) if path else (memo["content"] if memo else "", "")
        except (OSError, ValueError, UnicodeError) as error:
            self.set_status(f"读取 TXT 失败：{error}")
            self.notes.blockSignals(True); self.notes.setCurrentItem(previous); self.notes.blockSignals(False)
            return
        self.current_path, self.digest, self.current_memo = path, digest, memo
        self._editing_memo_id = memo["id"] if memo and path else None
        self.loading = True; self.editor.load_content(text); self.loading = False
        self.dirty = False; self.title_dirty = False; self._saved_content = text
        writable = bool(path) and not self.trash_mode
        self.editor.setEnabled(bool(memo)); self.editor.setReadOnly(not writable)
        self.filename.setText(memo["title"] if memo else "")
        self.filename.setReadOnly(not writable)
        self.document_kind.setText("RECYCLE BIN" if self.trash_mode else "PERSONAL NOTE")
        self.restore_button.setVisible(bool(memo) and self.trash_mode)
        for action in (self.save_action, self.rename_action, self.delete_action): action.setEnabled(writable)
        self.history_action.setEnabled(bool(memo)); self.save_as_action.setEnabled(bool(memo))
        self.image_button.setEnabled(writable)
        for widget in (self.document_kind, self.filename, self.editor): widget.setVisible(bool(memo))
        self.empty.setVisible(not memo)
        if memo:
            self._open_document(True)
            self.set_status("回收站 · 内容与历史均已保留" if self.trash_mode else "本机加密保存 · 自动保存")
        else:
            self.set_status("回收站为空" if self.trash_mode else "选择或新建备忘录")
        try:
            self._remember()
        except OSError:
            self.set_status("TXT 已读取；目录偏好保存失败，请检查配置目录权限。")

    def edited(self):
        if not self.loading and self.current_path:
            self.dirty = True
            self.set_status("正在编辑，稍后自动保存…")
            if not self.editor.composing: self.timer.start()

    def flush(self):
        self.timer.stop()
        if self.editor.composing or self.title_composing:
            self.timer.start(); return False
        if not self.dirty or not self.current_path:
            return True
        with self.store.lock:
            return self._flush_local()

    def _flush_local(self):
        try:
            target = self.store.target(self.filename.text()) if self.title_dirty else self.current_path
            if target != self.current_path and target.exists(): raise ValueError("同名文件已存在。")
            content = self.editor.get_content()
            if content != self._saved_content:
                self.digest = self.store.save(self.current_path, content, self.digest)
                self._saved_content = content
            elif read_text_file(self.current_path)[1] != self.digest:
                raise ValueError("文件已被其他程序修改，请先另存为或重新读取。")
            if target != self.current_path:
                self.store.rename(self.current_path, target)
                self.current_path = target
            self.digest = read_text_file(self.current_path)[1]
            self.current_memo = self.store.for_path(self.current_path)
            self.dirty = False; self.title_dirty = False
            self._update_current_item()
            self._remember()
            self.set_status(f"本机加密保存 · {datetime.now():%H:%M}")
            return True
        except (OSError, ValueError) as error:
            self.set_status(f"保存失败：{error}")
            return False

    def _update_current_item(self):
        item = self.notes.currentItem()
        if item and self.current_memo:
            item.setText(self.current_memo["title"])
            item.setData(Qt.ItemDataRole.UserRole, str(self.current_path) if self.current_path else None)
            item.setData(int(Qt.ItemDataRole.UserRole) + 1, self.current_memo)
            self.notes.viewport().update()

    def new_note(self):
        if not self.flush(): return
        title, accepted = QInputDialog.getText(self, "新建备忘录", "文件名", text=f"笔记 {datetime.now():%Y%m%d-%H%M%S}")
        if not accepted: return
        try:
            path = self.store.create(title)
            self.trash_mode = False; self.active_button.setChecked(True); self.trash_button.setChecked(False)
            self.search.clear(); self.refresh_files(path.name); self.editor.setFocus()
        except (OSError, ValueError) as error:
            self.set_status(f"新建失败：{error}")

    def import_note(self):
        if not self.flush(): return
        source, _ = QFileDialog.getOpenFileName(self, "导入 TXT 副本", "", "纯文本 (*.txt);;全部文件 (*)")
        if source: self.import_file(Path(source))

    def import_file(self, source):
        if not self.flush(): return False
        try:
            self.trash_mode = False; self.active_button.setChecked(True); self.trash_button.setChecked(False)
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
            self.set_status("已导入 UTF-8 副本，原始文件保留。")
            return True
        except (OSError, ValueError, UnicodeError) as error:
            self.set_status(f"导入失败：{error}"); return False

    def save_as(self):
        if not self.current_memo: return
        source = Path.home() / "Documents" / (self.current_memo["title"] + ".txt")
        destination, _ = QFileDialog.getSaveFileName(self, "另存为 TXT", str(source), "纯文本 (*.txt)")
        if not destination: return
        path = Path(destination)
        if not path.suffix: path = path.with_suffix(".txt")
        if self.current_path and path.resolve() == self.current_path.resolve():
            self.flush(); return
        try:
            atomic_text(path, self.editor.get_content())
            self.set_status(f"已另存为：{path}")
        except OSError as error:
            self.set_status(f"另存为失败：{error}")

    def rename_note(self):
        if not self.current_path or not self.flush(): return
        title, accepted = QInputDialog.getText(self, "重命名备忘录", "文件名", text=self.current_memo["title"])
        if not accepted: return
        try:
            target = self.store.target(title)
            if target == self.current_path: return
            if target.exists(): raise ValueError("同名文件已存在。")
            self.store.rename(self.current_path, target)
            self.current_path = None
            self.search.clear(); self.refresh_files(target.name)
        except (OSError, ValueError) as error:
            self.set_status(f"重命名失败：{error}")

    def delete_note(self):
        if not self.current_path or not self.flush(): return
        if QMessageBox.question(self, "移到回收站", "将这篇备忘录移到回收站？内容和全部历史版本仍可恢复。") != QMessageBox.StandardButton.Yes: return
        try:
            destination = self.store.trash(self.current_path)
            self.current_path = None; self.current_memo = None; self.digest = ""
            self.loading = True; self.editor.clear(); self.loading = False; self.editor.setEnabled(False)
            self.filename.setText("新建或选择一个 TXT 文件")
            self.refresh_files(); self.set_status(f"已移到回收目录：{destination}")
        except OSError as error:
            self.set_status(f"移动失败：{error}")

    def filter_notes(self, text):
        query = text.casefold().strip()
        for index in range(self.notes.count()):
            item = self.notes.item(index)
            memo = item.data(int(Qt.ItemDataRole.UserRole) + 1) or {}
            content = memo.get("content", "")
            if not self.trash_mode and query and query not in item.text().casefold():
                try:
                    content, _ = read_text_file(Path(item.data(Qt.ItemDataRole.UserRole)))
                except (OSError, ValueError, UnicodeError):
                    pass
            item.setHidden(bool(query and query not in item.text().casefold() and query not in content.casefold()))

    def choose_directory(self):
        if self.context:
            self.set_status("账号缓存目录按账号隔离；可用「导入 TXT」或「导入本机备忘录」添加其他目录中的内容。")
            return
        if not self.flush(): return
        directory = QFileDialog.getExistingDirectory(self, "选择 TXT 笔记目录", str(self.store.directory))
        if not directory: return
        try:
            store = TextNotesStore(Path(directory))
            store.files()  # Check access before switching.
            self.store = store; self.current_path = None; self.current_memo = None; self.trash_mode = False
            self.editor.set_asset_loader(lambda asset_id: self.store.memos.asset(self.store.owner, asset_id)["data"])
            self.directory_label.setText(str(store.directory))
            self.loading = True; self.editor.clear(); self.loading = False
            self.editor.setEnabled(False); self.filename.setText("新建或选择一个 TXT 文件")
            self._remember(); self.search.clear(); self.refresh_files()
        except (OSError, ValueError) as error:
            self.set_status(f"选择目录失败：{error}")

    def open_directory(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.store.directory)))

    def reload(self):
        if self.dirty and QMessageBox.question(self, "重新读取 TXT", "放弃当前未保存编辑并读取磁盘内容？") != QMessageBox.StandardButton.Yes:
            return
        self.timer.stop(); self.dirty = False
        selected = self.current_path.name if self.current_path else ""
        self.current_path = None; self.current_memo = None; self.editor.setEnabled(False)
        self.refresh_files(selected)

    def apply_theme(self, dark):
        self.dark = dark
        self.colors = ({"surface":"#172131", "side":"#141e2d", "line":"#29364a", "text":"#e3e9f1", "muted":"#8c9cb2", "soft":"#21344f", "brand":"#8db6f5"} if dark else
                       {"surface":"#ffffff", "side":"#f7f9fc", "line":"#e7ecf3", "text":"#243248", "muted":"#8a96a7", "soft":"#eaf1fc", "brand":"#426eb5"})
        c = self.colors
        self.setStyleSheet(f"""
            #memoPage {{ background: {c['side']}; color: {c['text']}; font-family: 'Microsoft YaHei UI', 'Segoe UI'; }}
            #memoCaption {{ color:{c['muted']}; background:transparent; font-size:12px; }}
            #memoPage QLabel#pageTitle {{ color:{c['text']}; background:transparent; font-size:25px; font-weight:600; }}
            #memoWorkbench {{ background:{c['line']}; border:1px solid {c['line']}; border-radius:12px; }}
            #memoWorkbench::handle {{ background:{c['line']}; }}
            #memoWorkbench QLabel {{ background:transparent; color:{c['muted']}; }}
            #memoLibrary {{ background:{c['side']}; border:0; }}
            #memoLibraryTitle {{ color:{c['text']}; font-weight:600; font-size:13px; }}
            #memoSearch {{ background:{c['surface']}; color:{c['text']}; border:1px solid {c['line']}; border-radius:8px; padding:9px 10px; font-size:12px; }}
            #memoSearch:focus {{ border-color:{c['brand']}; }}
            #memoNavigation {{ text-align:left; background:transparent; color:{c['muted']}; border:0; border-radius:7px; padding:10px 12px; font-size:12px; }}
            #memoNavigation:checked {{ background:{c['soft']}; color:{c['brand']}; font-weight:600; }}
            #memoList {{ background:transparent; border:0; outline:0; padding:0; }}
            #memoAccount {{ font-size:10px; color:{c['muted']}; }}
            #memoDocumentShell, #memoDocument {{ background:{c['surface']}; border:0; }}
            #memoEyebrow {{ font-size:9px; color:{c['muted']}; padding-bottom:18px; }}
            #memoTitle {{ background:transparent; color:{c['text']}; border:0; padding:0 0 23px 0; font-size:29px; font-weight:600; selection-background-color:{c['soft']}; }}
            #memoNativeEditor {{ background:{c['surface']}; color:{c['text']}; border:0; padding:0; font-size:15px; selection-background-color:{c['soft']}; selection-color:{c['text']}; }}
            #memoEmpty {{ font-size:14px; color:{c['muted']}; background:transparent; }}
            #memoFooter {{ background:{c['surface']}; border:0; border-top:1px solid {c['line']}; }}
            #memoFooter #memoStatus {{ font-size:11px; color:{c['muted']}; }}
            #memoFooter #memoStatus[problem=true] {{ color:#c47f6e; }}
            #memoFooter #memoStateDot {{ color:{c['brand']}; font-size:7px; }}
            #memoTool {{ background:transparent; border:0; padding:6px; border-radius:7px; }}
            #memoTool:hover, #memoTool:focus {{ background:{c['soft']}; }}
            #memoTool:disabled {{ background:transparent; }}
            #memoTool::menu-indicator {{ image:none; }}
            #memoBack {{ color:{c['muted']}; background:transparent; border:0; padding:12px 22px 0; font-size:12px; }}
            #memoNotice {{ background:{c['soft']}; border-top:1px solid {c['line']}; }}
            #memoNotice QLabel {{ color:{c['text']}; font-size:12px; }}
            #memoPage QMenu {{ background:{c['surface']}; color:{c['text']}; border:1px solid {c['line']}; padding:6px; border-radius:9px; }}
            #memoPage QMenu::item {{ padding:11px 22px; border-radius:6px; }}
            #memoPage QMenu::item:selected {{ background:{c['soft']}; }}
            #memoPage QMenu::separator {{ height:1px; background:{c['line']}; margin:5px; }}
        """)
        for button, name in self._icon_buttons: button.setIcon(self._icon(name))
        self.notes.viewport().update()

    def stop(self):
        if not self.flush(): return False
        self.stopping = True
        self.timer.stop(); self.checkpoint_timer.stop(); self.sync_timer.stop()
        return True

    def open_account(self):
        window = self.window()
        if hasattr(window, "open_account"): window.open_account()

    def switch_account(self, *_):
        if self.stopping: return
        if self.worker:
            self.pending_account = True; return
        if not self.flush(): return
        context = (self.account.url, self.account.user["id"]) if self.account and self.account.user else None
        if context == self.context and not getattr(self, "workspace_error", False):
            if context and self.account.token: QTimer.singleShot(0, self.sync_now)
            return
        if context:
            key = hashlib.sha256((context[0] + "\n" + context[1]).encode()).hexdigest()[:32]
            directory = self.config_path.parent / "super-memos" / key
            self.account_label.setText(self.account.user["email"])
            self.account_label.setToolTip("已登录：" + self.account.user["email"] + " · 账号内容与本机笔记分开保存")
            self.sync_timer.start()
        else:
            directory = Path(self.config.get("directory") or self.config_path.parent / "text-notes")
            self.account_label.setText("本机工作区")
            self.account_label.setToolTip("内容保存在本机，登录后可同步账号备忘录")
            self.sync_timer.stop()
        try:
            store = TextNotesStore(directory)
            imported = 0
            if context:
                from memo_import import adopt_local_workspace
                imported = adopt_local_workspace(self.config_path, self.config, key, store)
        except (OSError, ValueError) as error:
            self.context = None
            self.workspace_error = True
            self.current_path = None; self.current_memo = None; self.dirty = False
            self.loading = True; self.editor.clear(); self.loading = False
            self.filename.clear()
            self.notes.blockSignals(True); self.notes.clear(); self.notes.blockSignals(False)
            self.setEnabled(False)
            self.sync_timer.stop()
            self.set_status("备忘录工作区打开失败：" + str(error))
            return
        self.setEnabled(True)
        self.workspace_error = False
        self.context = context
        self.store = store; self.current_path = None; self.current_memo = None; self.dirty = False; self.trash_mode = False
        self.active_button.setChecked(True); self.trash_button.setChecked(False)
        self.editor.set_asset_loader(lambda asset_id: self.store.memos.asset(self.store.owner, asset_id)["data"])
        self.loading = True; self.editor.clear(); self.loading = False
        self.directory_label.setText(str(directory)); self.refresh_files()
        self.workspace_button.setToolTip("工作区与目录\n" + str(directory))
        if context: self.sync_now()
        if context and imported and not self.worker:
            self.set_status(f"已合并 {imported} 篇本机备忘录，原文件和历史保留；登录后继续同步。")

    def sync_now(self):
        if self.stopping or not self.account or not self.account.user or not self.context or self.worker: return
        if self.context != (self.account.url, self.account.user["id"]): return
        if not self.account.token:
            self.set_status("登录已过期，本机账号备忘录和编辑已保留；重新登录后继续同步。")
            return
        if not self.flush(): return
        self.worker = MemoSyncWorker(self.store.directory, self.account.url, self.account.token, self)
        self.worker.finished.connect(self.sync_finished)
        self.set_status("本机内容已加密保存 · 后台同步中，可继续编辑")
        self.worker.start()

    def sync_finished(self):
        result = self.worker.result; self.worker.deleteLater(); self.worker = None
        if self.stopping: return
        if self.pending_account:
            self.pending_account = False; self.switch_account(); return
        self.refresh_files(self.current_path.name if self.current_path else "", preserve_editor=True)
        if "error" in result: self.set_status(result["error"])
        elif self.dirty: self.set_status("继续编辑 · 稍后加密保存到本机")
        elif result.get("deferred"): self.set_status("后台同步完成 · 当前文档的云端更新将在切换后合并")
        else: self.set_status(f"本机加密保存 · 后台同步完成 · {result['count']} 篇备忘录" + (f" · 已保留 {result['conflicts']} 个冲突副本" if result["conflicts"] else ""))

    def insert_image(self):
        if not self.current_path: return
        filename, _ = QFileDialog.getOpenFileName(self, "插入图片", "", "图片 (*.png *.jpg *.jpeg *.gif *.webp)")
        if filename:
            path = Path(filename)
            if path.stat().st_size > 10 * 1024 * 1024:
                self.set_status("图片最大为 10 MB。"); return
            self.insert_raw_image(path.read_bytes(), path.name)

    def insert_raw_image(self, raw, name):
        if not self.current_path: return
        try:
            asset = self.store.memos.put_asset(self.store.owner, name, base64.b64encode(raw).decode())
            self.editor.insert_asset(asset, raw)
            self.flush()
        except (OSError, ValueError) as error: self.set_status(str(error))

    def show_history(self):
        if not self.current_memo or not self.flush(): return
        memo = self.current_memo
        versions = self.store.memos.history(self.store.owner, memo["id"])
        dialog = QDialog(self); dialog.setWindowTitle("历史动作与版本回滚"); dialog.resize(760, 580)
        layout = QVBoxLayout(dialog); layout.addWidget(QLabel("每次保存均保留快照；回滚生成新版本，原历史继续保留。"))
        listing = QListWidget(); layout.addWidget(listing)
        labels = {"create": "新建", "edit": "编辑", "rename": "重命名", "delete": "删除", "rollback": "回滚", "restore": "恢复", "sync": "云端更新", "import": "导入"}
        for version in versions:
            item = QListWidgetItem(f"v{version['version']} · {labels.get(version['action'], version['action'])} · {datetime.fromtimestamp(version['created']):%Y-%m-%d %H:%M:%S}")
            item.setData(Qt.ItemDataRole.UserRole, version); listing.addItem(item)
        preview = MemoEditor(); preview.setReadOnly(True)
        preview.set_asset_loader(lambda asset_id: self.store.memos.asset(self.store.owner, asset_id)["data"]); layout.addWidget(preview, 1)
        listing.currentItemChanged.connect(lambda item, _: preview.load_content(item.data(Qt.ItemDataRole.UserRole)["content"]) if item else None)
        listing.setCurrentRow(0)
        button = QPushButton("回滚到所选版本"); layout.addWidget(button)
        def restore():
            item = listing.currentItem()
            if not item: return
            if QMessageBox.question(dialog, "确认回滚", "创建恢复所选内容的新版本？当前版本仍保留。") != QMessageBox.StandardButton.Yes: return
            try:
                version = item.data(Qt.ItemDataRole.UserRole)
                _, path = self.store.write_snapshot({**version, "id": memo["id"], "deleted": bool(version["deleted"])}, "rollback")
                self.current_path = None; self.current_memo = None; self.trash_mode = bool(version["deleted"])
                self.active_button.setChecked(not self.trash_mode); self.trash_button.setChecked(self.trash_mode)
                self.refresh_files(path.name if not self.trash_mode else ""); dialog.accept(); self.set_status("已回滚并保留原历史，下次同步会上传此动作。")
            except (OSError, ValueError) as error: self.set_status(str(error))
        button.clicked.connect(restore); dialog.exec()

    def show_trash(self):
        self.set_folder(True)

    def set_folder(self, trash):
        if not self.flush(): return
        self.trash_mode = trash; self.current_path = None; self.current_memo = None
        self.active_button.setChecked(not trash); self.trash_button.setChecked(trash)
        self.search.clear(); self.refresh_files(); self._open_document(False)

    def restore_note(self):
        if not self.trash_mode or not self.current_memo: return
        try:
            memo = self.current_memo
            _, path = self.store.write_snapshot({**memo, "deleted": False}, "restore")
            self.current_path = None; self.current_memo = None; self.trash_mode = False
            self.active_button.setChecked(True); self.trash_button.setChecked(False)
            self.refresh_files(path.name); self.set_status("备忘录已恢复，全部版本继续保留。")
        except (OSError, ValueError) as error: self.set_status(str(error))

    def import_local(self):
        if not self.flush(): return
        directory = QFileDialog.getExistingDirectory(self, "导入本机备忘录及历史副本", str(self.config_path.parent / "text-notes"))
        if not directory: return
        try:
            source = TextNotesStore(Path(directory))
            if source.directory == self.store.directory: return
            from memo_import import import_workspace
            count = import_workspace(source, self.store)
            self.refresh_files()
            self.set_status(f"已合并 {count} 篇本机备忘录及历史；原文件保留。")
            self.sync_now()
        except (OSError, ValueError) as error: self.set_status(str(error))
