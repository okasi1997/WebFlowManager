from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class PlaceholderPage(QWidget):
    def __init__(self, title: str, subtitle: str) -> None:
        super().__init__()
        self.setObjectName('pageRoot')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 22)
        title_label = QLabel(title)
        title_label.setProperty('pageTitle', True)
        layout.addWidget(title_label)
        subtitle_label = QLabel(subtitle)
        subtitle_label.setProperty('muted', True)
        layout.addWidget(subtitle_label)
        layout.addStretch(1)
        note = QLabel('Qt 页面迁移中')
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        note.setProperty('muted', True)
        layout.addWidget(note)
        layout.addStretch(2)
