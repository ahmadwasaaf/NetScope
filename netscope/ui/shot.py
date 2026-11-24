"""Full-window screenshots, re-drawn at 1x-4x (not an enlarged capture).

The window's widgets paint themselves again onto an image whose device-pixel-ratio is the
chosen scale, so text, icons, gauges and borders are drawn at that resolution. Scrolling
pages are drawn whole (their full content, not the visible part) and scroll bars are left
out, because the content widgets are painted instead of the scroll areas around them.
"""
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from .widgets import icon_label

SCALES = ((1, "shot_standard"), (2, "shot_high"), (3, "shot_ultra"), (4, "shot_max"))


def _pos_in(widget: QWidget, top: QWidget) -> QPoint:
    return widget.mapTo(top, QPoint(0, 0))


def expand_inner_scrolls(top: QWidget):
    """Lists that scroll inside a card are opened to full height for the capture."""
    undo = []
    for sa in top.findChildren(QScrollArea):
        if sa.property("page") or not sa.isVisible() or sa.widget() is None:
            continue
        need = sa.widget().sizeHint().height()
        if need > sa.height():
            undo.append((sa, sa.minimumHeight(), sa.maximumHeight(), sa.verticalScrollBarPolicy()))
            sa.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            sa.setFixedHeight(need)
    return undo


def restore(undo):
    for sa, mn, mx, pol in undo:
        sa.setMinimumHeight(mn)
        sa.setMaximumHeight(mx)
        sa.setVerticalScrollBarPolicy(pol)


def render_window(top: QWidget, scale: int, background: str, pages=()) -> QImage:
    """Draw `top` at `scale`. `pages` = [(scroll area, content widget)]: each is drawn with its
    whole content (taller than the window if needed) and without scroll bars."""
    pages = [(sa, body) for sa, body in pages if sa.isVisible()]
    extra = max([body.height() - sa.viewport().height() for sa, body in pages] + [0])
    w, h = top.width(), top.height() + extra
    img = QImage(w * scale, h * scale, QImage.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(scale)
    img.fill(QColor(background))
    p = QPainter(img)
    p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing | QPainter.SmoothPixmapTransform)
    top.render(p, QPoint(0, 0))
    for sa, body in pages:
        at = _pos_in(sa, top)
        p.fillRect(QRect(at.x(), at.y(), sa.width(), max(body.height(), sa.height())), QColor(background))
        body.render(p, at)
    p.end()
    return img


def save_png(img: QImage, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"NetScope-{datetime.now():%Y-%m-%d-%H-%M-%S}.png"
    img.save(str(path), "PNG")
    return path


class ShotDialog(QDialog):
    """Pick the quality; the dialog itself is not part of the picture."""

    def __init__(self, tr, logical_width: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("shot_title"))
        self.scale = 0
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 12)
        v.setSpacing(6)
        head = QHBoxLayout()
        t = QLabel(tr("shot_title"))
        t.setObjectName("SectionTitle")
        head.addWidget(t)
        head.addWidget(icon_label("camera", 13))
        head.addStretch(1)
        v.addLayout(head)
        line = QFrame()
        line.setObjectName("VSep")
        line.setFixedHeight(1)
        v.addWidget(line)
        for scale, key in SCALES:
            b = QPushButton()
            b.setObjectName("ShotRow")
            b.setCursor(Qt.PointingHandCursor)
            row = QHBoxLayout(b)
            row.setContentsMargins(10, 6, 10, 6)
            k = QLabel(f"{scale}x")
            k.setObjectName("ShotScale")
            row.addWidget(k)
            row.addWidget(QLabel(tr(key)))
            row.addStretch(1)
            px = QLabel(f"{logical_width * scale:,} px")
            px.setObjectName("Muted")
            row.addWidget(px)
            b.setMinimumHeight(38)
            b.clicked.connect(lambda _=False, s=scale: self._pick(s))
            v.addWidget(b)
        foot = QLabel(tr("shot_where"))
        foot.setObjectName("Muted")
        foot.setStyleSheet("font-size: 11px;")
        v.addSpacing(4)
        v.addWidget(foot)
        self.setMinimumWidth(300)

    def _pick(self, scale):
        self.scale = scale
        self.accept()
