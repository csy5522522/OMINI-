"""Native text/image editor with a lossless adapter for the existing TXT format."""
from __future__ import annotations

import re

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QMimeData, QSizeF, Qt, Signal
from PySide6.QtGui import (
    QColor, QImage, QPainter, QPainterPath, QTextCharFormat, QTextCursor, QTextBlockFormat,
    QTextFormat, QTextImageFormat, QPyTextObject,
)
from PySide6.QtWidgets import QTextEdit

ASSET_PATTERN = re.compile(r"!\[([^\]\n]*)\]\(omini-asset://([a-f0-9]{64})\)")
TOKEN_PROPERTY = int(QTextFormat.Property.UserProperty) + 1
EOL_PROPERTY = TOKEN_PROPERTY + 1
IMAGE_OBJECT = int(QTextFormat.ObjectTypes.UserObject) + 1


class MemoImageRenderer(QPyTextObject):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def intrinsicSize(self, document, position, char_format):
        image = self.editor.image_for(char_format.property(QTextFormat.Property.ImageName))
        available = max(40, self.editor.viewport().width() - 12)
        if image.isNull():
            return QSizeF(min(available, 320), 72)
        width = min(image.width(), available, 800)
        return QSizeF(width, image.height() * width / image.width())

    def drawObject(self, painter, rect, document, position, char_format):
        image = self.editor.image_for(char_format.property(QTextFormat.Property.ImageName))
        painter.save()
        if image.isNull():
            painter.setPen(QColor("#8a98a5"))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 8, 8)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "图片暂未加载 · 双击重试")
        else:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            clip = QPainterPath(); clip.addRoundedRect(rect, 9, 9); painter.setClipPath(clip)
            painter.drawImage(rect, image)
        painter.restore()


class MemoEditor(QTextEdit):
    compositionChanged = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setUndoRedoEnabled(True)
        self.setFrameStyle(0)
        self.setTabChangesFocus(False)
        self.document().setDocumentMargin(2)
        self.composing = False
        self.asset_loader = None
        self.image_pasted = None
        self._images = {}
        self._renderer = MemoImageRenderer(self)
        self.document().documentLayout().registerHandler(IMAGE_OBJECT, self._renderer)

    def set_asset_loader(self, loader):
        self.asset_loader = loader
        self._images.clear()

    def image_for(self, name):
        asset_id = name.removeprefix("memo://")
        if asset_id not in self._images:
            image = QImage()
            if self.asset_loader:
                try:
                    image = QImage.fromData(self.asset_loader(asset_id))
                except (OSError, ValueError, KeyError):
                    pass
            self._images[asset_id] = image
        return self._images[asset_id]

    def _insert_text(self, cursor, text):
        def spacing():
            block_format = cursor.blockFormat()
            block_format.setLineHeight(100 if '\ufffc' in cursor.block().text() else 180, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
            cursor.setBlockFormat(block_format)
        start = 0
        for match in re.finditer(r"\r\n|\r|\n", text):
            spacing()
            cursor.insertText(text[start:match.start()], QTextCharFormat())
            block_format = cursor.blockFormat()
            block_format.setProperty(EOL_PROPERTY, match[0])
            cursor.setBlockFormat(block_format)
            cursor.insertBlock()
            start = match.end()
        spacing()
        cursor.insertText(text[start:], QTextCharFormat())

    def _insert_content(self, cursor, text):
        start = 0
        for match in ASSET_PATTERN.finditer(text):
            self._insert_text(cursor, text[start:match.start()])
            image_format = QTextImageFormat()
            image_format.setName("memo://" + match[2])
            image_format.setObjectType(IMAGE_OBJECT)
            image_format.setProperty(TOKEN_PROPERTY, match[0])
            image_format.setToolTip(match[1] or "备忘录图片")
            cursor.insertText("\ufffc", image_format)
            block_format = cursor.blockFormat()
            block_format.setLineHeight(100, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
            cursor.setBlockFormat(block_format)
            start = match.end()
        self._insert_text(cursor, text[start:])
        cursor.setCharFormat(QTextCharFormat())

    def load_content(self, text):
        self.blockSignals(True)
        self.clear()
        cursor = QTextCursor(self.document())
        block_format = cursor.blockFormat()
        block_format.setLineHeight(180, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        cursor.setBlockFormat(block_format)
        self._insert_content(cursor, text)
        self.document().clearUndoRedoStacks()
        self.document().setModified(False)
        cursor.setPosition(0)
        self.setTextCursor(cursor)
        self.blockSignals(False)
        self.viewport().update()

    def get_content(self, start=0, end=None):
        """Serialize image objects as their original tokens, never U+FFFC or HTML."""
        if end is None:
            end = self.document().characterCount() - 1
        parts = []
        block = self.document().findBlock(start)
        while block.isValid() and block.position() <= end:
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                if fragment.isValid():
                    left, right = max(start, fragment.position()), min(end, fragment.position() + fragment.length())
                    if left < right:
                        char_format = fragment.charFormat()
                        # Qt positions use UTF-16 code units, including emoji surrogates.
                        raw = fragment.text().encode("utf-16-le", errors="surrogatepass")
                        value = raw[(left-fragment.position())*2:(right-fragment.position())*2].decode("utf-16-le", errors="surrogatepass")
                        if char_format.hasProperty(TOKEN_PROPERTY):
                            value = value.replace("\ufffc", char_format.property(TOKEN_PROPERTY))
                        parts.append(value)
                iterator += 1
            separator = block.position() + block.length() - 1
            if block.next().isValid() and start <= separator < end:
                parts.append(block.blockFormat().property(EOL_PROPERTY) or "\n")
            block = block.next()
        return "".join(parts)

    def insert_asset(self, asset, raw=None):
        if self.isReadOnly():
            return
        if raw:
            self._images[asset["id"]] = QImage.fromData(raw)
        alt = re.sub(r"[\]\n]", "_", asset.get("name") or "图片")
        cursor = self.textCursor()
        cursor.beginEditBlock()
        self._insert_content(cursor, f"![{alt}](omini-asset://{asset['id']})")
        cursor.endEditBlock()
        self.setTextCursor(cursor)
        self.setCurrentCharFormat(QTextCharFormat())
        self.setFocus()

    def insertFromMimeData(self, source):
        if self.isReadOnly():
            return
        if source.hasImage() and self.image_pasted:
            raw = QByteArray()
            buffer = QBuffer(raw)
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            QImage(source.imageData()).save(buffer, "PNG")
            buffer.close()
            self.image_pasted(bytes(raw), "粘贴图片.png")
        elif source.hasText():
            cursor = self.textCursor()
            cursor.beginEditBlock()
            self._insert_content(cursor, source.text())
            cursor.endEditBlock()
            self.setTextCursor(cursor)

    def createMimeDataFromSelection(self):
        source = QMimeData()
        cursor = self.textCursor()
        source.setText(self.get_content(cursor.selectionStart(), cursor.selectionEnd()))
        return source

    def inputMethodEvent(self, event):
        self.composing = bool(event.preeditString())
        self.compositionChanged.emit(self.composing)
        super().inputMethodEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.document().markContentsDirty(0, self.document().characterCount())

    def mouseDoubleClickEvent(self, event):
        cursor = self.cursorForPosition(event.position().toPoint())
        for position in (cursor.position(), cursor.position() - 1):
            if 0 <= position < self.document().characterCount() - 1:
                selection = QTextCursor(self.document())
                selection.setPosition(position)
                selection.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor)
                if selection.charFormat().hasProperty(TOKEN_PROPERTY):
                    self._images.pop(selection.charFormat().property(QTextFormat.Property.ImageName).removeprefix("memo://"), None)
                    self.document().markContentsDirty(position, 1)
                    self.viewport().update()
                    return
        super().mouseDoubleClickEvent(event)
