"""Compact wrapping controls for native desktop pages."""
from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QSizePolicy


class FlowLayout(QLayout):
    def __init__(self, parent=None, spacing=6):
        super().__init__(parent)
        self.items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def addItem(self, item):
        self.items.append(item)
        if item.widget():
            item.widget().setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def count(self):
        return len(self.items)

    def itemAt(self, index):
        return self.items[index] if 0 <= index < len(self.items) else None

    def takeAt(self, index):
        return self.items.pop(index) if 0 <= index < len(self.items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self.items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _arrange(self, rect, apply):
        left, top, right, bottom = self.getContentsMargins()
        area = rect.adjusted(left, top, -right, -bottom)
        x, y, height = area.x(), area.y(), 0
        for item in self.items:
            if item.isEmpty():
                continue
            hint = item.sizeHint()
            width = min(hint.width(), max(1, area.width()))
            if x > area.x() and x + width > area.right() + 1:
                x, y, height = area.x(), y + height + self.spacing(), 0
            if apply:
                item.setGeometry(QRect(x, y, width, hint.height()))
            x += width + self.spacing()
            height = max(height, hint.height())
        return y + height - rect.y() + bottom
