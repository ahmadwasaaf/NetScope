"""Custom widgets: gauge, sparkline, usage rows, collapsible section, toast, spinner."""
import time
from collections import deque

from PySide6.QtCore import (Property, QEasingCurve, QEvent, QPointF, QPropertyAnimation, QRectF, QVariantAnimation,
                            Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtCore import QLocale
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QDoubleSpinBox, QFrame, QLayout, QScrollArea,
                               QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QSizePolicy, QToolButton,
                               QVBoxLayout, QWidget)

from ..theme import Palette

# Segoe Fluent Icons / Segoe MDL2 Assets glyphs (built into Windows 10 / 11)
ICON_FAMILIES = ["Segoe Fluent Icons", "Segoe MDL2 Assets"]
GLYPH = {
    "settings": "", "calendar": "", "chart": "", "globe": "",
    "network": "", "down": "", "up": "", "add": "",
    "pc": "", "phone": "", "tv": "", "laptop": "",
    "tablet": "", "other": "", "block": "", "router": "",
    "arrow_down": "", "arrow_up": "", "check": "", "cancel": "",
    "search": "", "pie": "", "wifi": "", "alarm": "",
}


def icon_font(px: int) -> QFont:
    f = QFont()
    f.setFamilies(ICON_FAMILIES)
    f.setPixelSize(px)
    return f


def icon_css(px: int, extra: str = "") -> str:
    # widget-level style beats the app-wide `QWidget { font-family … }` rule
    return f"font-family: 'Segoe Fluent Icons', 'Segoe MDL2 Assets'; font-size: {px}px; {extra}"


def icon_label(name: str, size: int = 14, obj: str = "", color: str = "") -> QLabel:
    lbl = QLabel(GLYPH.get(name, GLYPH["other"]))
    lbl.setFont(icon_font(size))
    lbl.setStyleSheet(icon_css(size, f"color: {color};" if color else ""))
    lbl.setAlignment(Qt.AlignCenter)
    if obj:
        lbl.setObjectName(obj)
    return lbl


def icon_button(name: str, size: int = 16) -> QToolButton:
    b = QToolButton()
    b.setObjectName("IconBtn")
    b.setText(GLYPH[name])
    b.setFont(icon_font(size))
    b.setStyleSheet(icon_css(size))
    b.setCursor(Qt.PointingHandCursor)
    return b


def fmt_gb(nbytes: float) -> str:
    return f"{nbytes / 1024 ** 3:,.2f}"


def fmt_size(nbytes: float) -> str:
    """1.25 GB · 460 MB · 820 KB – the unit that reads naturally."""
    if nbytes >= 1024 ** 3:
        return f"{nbytes / 1024 ** 3:,.2f} GB"
    if nbytes >= 1024 ** 2:
        mb = nbytes / 1024 ** 2
        return f"{mb:,.1f} MB" if mb < 10 else f"{mb:,.0f} MB"
    return f"{max(nbytes, 0) / 1024:,.0f} KB"


def fmt_speed(bps) -> str:
    if bps is None:
        return "—"
    mbit = bps * 8 / 1_000_000
    return f"{mbit:,.1f} Mbps" if mbit >= 1 else f"{bps * 8 / 1000:,.0f} Kbps"


# ---------------------------------------------------------------------------- Gauge
class Gauge(QWidget):
    """240° arc gauge. With a bundle (quota) the arc shows used / quota and changes colour
    when the bundle is nearly finished."""
    SPAN = 240

    def __init__(self, palette: Palette, parent=None):
        super().__init__(parent)
        self.p = palette
        self.caption = ""
        self.subcaption = ""
        self.quota_gb = 0.0
        self._value = 0.0
        self.target = 0.0
        self.setMinimumSize(280, 240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(250)
        self.anim = QPropertyAnimation(self, b"value", self)
        self.anim.setDuration(1100)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)

    def getValue(self):
        return self._value

    def setValue(self, v):
        self._value = v
        self.update()

    value = Property(float, getValue, setValue)

    def animate_to(self, gb: float):
        if abs(gb - self.target) < 1e-6 and self.anim.state() != QPropertyAnimation.Running:
            return
        self.target = gb
        self.anim.stop()
        self.anim.setStartValue(self._value)
        self.anim.setEndValue(gb)
        self.anim.start()

    @staticmethod
    def auto_max(v: float) -> float:
        for step in (10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000):
            if v * 1.25 <= step:
                return step
        return v * 1.25

    def fill_color(self, frac: float) -> QColor:
        if self.quota_gb > 0 and frac >= 0.95:
            return QColor(self.p.danger)
        if self.quota_gb > 0 and frac >= 0.8:
            return QColor(self.p.warn)
        return QColor(self.p.accent)

    def paintEvent(self, _):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        thick = max(14.0, min(w, h) * 0.07)
        pad = 6
        # a 240° arc reaches r above the centre and r·sin(30°)=0.5r below it
        r = max(20.0, min((w - thick - 2 * pad) / 2, (h - thick - 2 * pad) / 1.5))
        cx, cy = w / 2, pad + thick / 2 + r
        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        side = 2 * r
        start = 90 + self.SPAN / 2

        pen = QPen(QColor(self.p.track), thick, Qt.SolidLine, Qt.RoundCap)
        qp.setPen(pen)
        qp.drawArc(rect, int(start * 16), int(-self.SPAN * 16))

        scale = self.quota_gb if self.quota_gb > 0 else self.auto_max(max(self.target, 0.01))
        frac = min(1.0, self._value / scale) if scale else 0
        if frac > 0.002:
            col = self.fill_color(frac)
            grad = QLinearGradient(rect.topLeft(), rect.topRight())
            grad.setColorAt(0, col.darker(125))
            grad.setColorAt(1, col)
            pen.setBrush(grad)
            qp.setPen(pen)
            qp.drawArc(rect, int(start * 16), int(-self.SPAN * frac * 16))

        qp.setPen(QColor(self.p.text))
        f = QFont("Segoe UI", 1, QFont.Bold)
        f.setPixelSize(int(side * 0.2))
        qp.setFont(f)
        qp.drawText(QRectF(0, cy - side * 0.19, w, side * 0.24), Qt.AlignCenter, f"{self._value:,.2f}")
        f2 = QFont("Segoe UI")
        f2.setPixelSize(max(12, int(side * 0.065)))
        qp.setFont(f2)
        qp.setPen(QColor(self.p.subtext))
        qp.drawText(QRectF(0, cy + side * 0.06, w, side * 0.09), Qt.AlignCenter, self.caption)
        if self.subcaption:
            f3 = QFont("Segoe UI", 1, QFont.DemiBold)
            f3.setPixelSize(max(11, int(side * 0.05)))
            qp.setFont(f3)
            qp.setPen(self.fill_color(frac) if self.quota_gb else QColor(self.p.subtext))
            qp.drawText(QRectF(0, cy + side * 0.16, w, side * 0.08), Qt.AlignCenter, self.subcaption)


# ---------------------------------------------------------------------------- Waves
class Waves(QWidget):
    """Decorative live animation (not a chart): layered waves that keep flowing while the
    router is online, and calm down to a flat grey line when it is not."""

    def __init__(self, palette: Palette, color: str = "", phase: float = 0.0):
        super().__init__()
        self.p = palette
        self.color = QColor(color or palette.accent)
        self.grey = QColor(palette.subtext)
        self.phase = phase
        self.alive = True
        self.level = 1.0                      # 1 = full motion, 0 = flat
        self.t = phase * 3.7
        self._last = time.monotonic()
        self.setMinimumHeight(34)
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self._tick)

    def set_alive(self, alive: bool):
        self.alive = bool(alive)

    def showEvent(self, e):
        self._last = time.monotonic()
        self.timer.start(16)
        super().showEvent(e)

    def hideEvent(self, e):
        self.timer.stop()
        super().hideEvent(e)

    def _tick(self):
        now = time.monotonic()
        dt = min(0.1, now - self._last)
        self._last = now
        target = 1.0 if self.alive else 0.0
        self.level += (target - self.level) * (1 - pow(0.05, dt))
        self.t += dt * (0.35 + 0.65 * self.level)
        self.update()

    def _mix(self):
        c, g, k = self.color, self.grey, self.level
        return QColor(int(g.red() + (c.red() - g.red()) * k), int(g.green() + (c.green() - g.green()) * k),
                      int(g.blue() + (c.blue() - g.blue()) * k))

    def paintEvent(self, _):
        import math
        qp = QPainter(self)
        qp.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        mid = h * 0.55
        col = self._mix()
        t, lv = self.t, self.level
        layers = (  # (amplitude, wavelength px, speed, alpha, pen width)
            (0.40, 150, 2.8, 80, 0.0),
            (0.30, 95, -2.1, 65, 0.0),
            (0.44, 120, 3.6, 255, 1.8),
        )
        step = 3
        for amp, wl, sp, alpha, pen_w in layers:
            a = amp * h * lv
            k = 2 * math.pi / wl
            path = QPainterPath()
            for i, x in enumerate(range(0, w + step, step)):
                env = 0.55 + 0.45 * math.sin(x / max(w, 1) * math.pi * 1.3 + t * 0.7 + self.phase)
                y = mid - a * env * (math.sin(k * x - sp * t + self.phase) * 0.75
                                     + math.sin(k * 0.47 * x + sp * 0.6 * t) * 0.25)
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
            if pen_w:
                area = QPainterPath(path)
                area.lineTo(w, h)
                area.lineTo(0, h)
                grad = QLinearGradient(0, 0, 0, h)
                c1 = QColor(col)
                c1.setAlpha(int(85 * (0.4 + 0.6 * lv)))
                c2 = QColor(col)
                c2.setAlpha(0)
                grad.setColorAt(0, c1)
                grad.setColorAt(1, c2)
                qp.fillPath(area, grad)
                pen = QPen(col, pen_w)
                pen.setCapStyle(Qt.RoundCap)
                qp.setPen(pen)
                qp.drawPath(path)
            else:
                c = QColor(col)
                c.setAlpha(int(alpha * lv))
                qp.setPen(QPen(c, 1.2))
                qp.drawPath(path)


class SpeedTile(QFrame):
    """Big number = the line's speed from the router (e.g. DSL sync rate).
    Small line + sparkline = what is being used right now."""

    def __init__(self, palette: Palette, title: str, glyph: str, color: str = "", now_label: str = ""):
        super().__init__()
        self.setObjectName("Tile")
        self.now_label = now_label
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 8)
        v.setSpacing(2)
        top = QHBoxLayout()
        top.addWidget(icon_label(glyph, 12, color=color or palette.accent))
        t = QLabel(title)
        t.setObjectName("Muted")
        top.addWidget(t)
        top.addStretch(1)
        v.addLayout(top)
        self.value = QLabel("—")
        self.value.setObjectName("TileValue")
        v.addWidget(self.value)
        self.now = QLabel()
        self.now.setObjectName("Muted")
        v.addWidget(self.now)
        self.waves = Waves(palette, color, phase=1.3 if color else 0.0)
        v.addWidget(self.waves)

    def set_values(self, line_kbps, now_bps):
        if line_kbps:
            self.value.setText(fmt_speed(line_kbps * 1000 / 8))
            self.now.setText(f"{self.now_label}: {fmt_speed(now_bps)}" if now_bps is not None else "")
            self.now.setVisible(now_bps is not None)
        else:                                  # router doesn't report its line speed
            self.value.setText(fmt_speed(now_bps))
            self.now.hide()

    def set_alive(self, alive: bool):
        self.waves.set_alive(alive)


# ---------------------------------------------------------------------------- Bar
class Bar(QWidget):
    def __init__(self, palette: Palette, parent=None):
        super().__init__(parent)
        self.p = palette
        self._frac = 0.0
        self.dim = False
        self.setFixedHeight(4)
        self.anim = QPropertyAnimation(self, b"frac", self)
        self.anim.setDuration(700)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)

    def getFrac(self):
        return self._frac

    def setFrac(self, v):
        self._frac = v
        self.update()

    frac = Property(float, getFrac, setFrac)

    def animate_to(self, f: float):
        f = max(0.0, min(1.0, f))
        if abs(f - self._frac) < 1e-4:
            return
        self.anim.stop()
        self.anim.setStartValue(self._frac)
        self.anim.setEndValue(f)
        self.anim.start()

    def paintEvent(self, _):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        qp.setPen(Qt.NoPen)
        qp.setBrush(QColor(self.p.track))
        qp.drawRoundedRect(r, 2, 2)
        if self._frac > 0:
            fw = max(4.0, r.width() * self._frac)
            x = r.width() - fw if self.layoutDirection() == Qt.RightToLeft else 0
            qp.setBrush(QColor(self.p.subtext if self.dim else self.p.accent))
            qp.drawRoundedRect(QRectF(x, 0, fw, r.height()), 2, 2)


# ---------------------------------------------------------------------------- Row
class UsageRow(QFrame):
    """Name side: icon, name, sub-line. Other side: usage. Thin bar underneath."""
    context = Signal(str, object)     # key, global pos
    activated = Signal(str)           # double click

    def __init__(self, key: str, palette: Palette, icon: str = ""):
        super().__init__()
        self.key = key
        self.p = palette
        self.setObjectName("Row")
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda pos: self.context.emit(self.key, self.mapToGlobal(pos)))

        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.icon = None
        if icon:
            self.icon = icon_label(icon, 15, "IconTile")
            self.icon.setFixedSize(32, 32)
            top.addWidget(self.icon)
        self.idx = QLabel()
        self.idx.setObjectName("Muted")
        self.idx.hide()
        top.addWidget(self.idx)
        names = QVBoxLayout()
        names.setSpacing(0)
        nrow = QHBoxLayout()
        nrow.setSpacing(6)
        self.name = QLabel()
        self.name.setStyleSheet("font-size: 13px; font-weight: 600;")
        nrow.addWidget(self.name)
        self.dot = QLabel("●")
        self.dot.setStyleSheet("font-size: 8px;")
        self.dot.hide()
        nrow.addWidget(self.dot)
        nrow.addStretch(1)
        names.addLayout(nrow)
        self.sub = QLabel()
        self.sub.setObjectName("Muted")
        names.addWidget(self.sub)
        top.addLayout(names, 1)
        self.chip = QLabel()
        self.chip.setObjectName("Badge")
        self.chip.hide()
        top.addWidget(self.chip)
        self.usage = QLabel()
        self.usage.setStyleSheet("font-size: 13px; font-weight: 600;")
        top.addWidget(self.usage)
        v.addLayout(top)
        self.bar = Bar(palette)
        v.addWidget(self.bar)

        self._dim = None

    def mouseDoubleClickEvent(self, e):
        self.activated.emit(self.key)

    def set_data(self, name, sub, nbytes, frac, blocked, blocked_text, index="", online=None):
        self.idx.setText(index)
        self.idx.setVisible(bool(index))
        self.name.setText(name)
        self.sub.setText(sub)
        self.sub.setVisible(bool(sub))
        if online is None:
            self.dot.hide()
        else:
            self.dot.show()
            self.dot.setStyleSheet(f"font-size: 8px; color: {self.p.success if online else self.p.track};")
        self.usage.setText(fmt_size(nbytes) if nbytes is not None else "")
        self.bar.setVisible(nbytes is not None)
        self.chip.setText(blocked_text)
        self.chip.setVisible(blocked)
        self.bar.dim = blocked
        if nbytes is not None:
            self.bar.animate_to(frac)
        dim = blocked or online is False
        if dim != self._dim:                      # restyle only when it changes
            self._dim = dim
            col = self.p.subtext if dim else self.p.text
            self.name.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {col};")
            self.usage.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {col};")


# ---------------------------------------------------------------------------- Section
class CollapsibleSection(QFrame):
    toggled = Signal(bool)

    def __init__(self, title: str, icon: str, palette: Palette, badge: str = "", open_=True,
                 search_placeholder: str = ""):
        super().__init__()
        self.setObjectName("Card")
        self.p = palette
        self.is_open = open_
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 10)
        outer.setSpacing(4)

        self.header = QFrame()
        self.header.setObjectName("SectionHeader")
        self.header.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(self.header)
        h.setContentsMargins(8, 8, 8, 8)
        ic = icon_label(icon, 16, color=palette.accent)
        h.addWidget(ic)
        t = QLabel(title)
        t.setObjectName("SectionTitle")
        t.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        t.setMinimumWidth(40)
        h.addWidget(t, 1)
        self.extra = QHBoxLayout()
        h.addLayout(self.extra)
        self.badge = QLabel(badge)
        self.badge.setObjectName("Badge")
        h.addWidget(self.badge)
        self.chev = icon_label("up" if open_ else "down", 12)
        h.addWidget(self.chev)
        self.header.mousePressEvent = lambda e: self.toggle()
        outer.addWidget(self.header)

        self.body = QWidget()
        self.body.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        bl = QVBoxLayout(self.body)
        self.body_layout = bl
        bl.setContentsMargins(0, 4, 0, 0)
        bl.setSpacing(4)
        self.search = None
        if search_placeholder:
            self.search = QLineEdit()
            self.search.setObjectName("Search")
            self.search.setPlaceholderText(search_placeholder)
            self.search.setClearButtonEnabled(True)
            bl.addWidget(self.search)
        self.list_w = QWidget()
        self.list = QVBoxLayout(self.list_w)
        self.list.setContentsMargins(0, 0, 0, 0)
        self.list.setSpacing(2)
        bl.addWidget(self.list_w)
        self.empty = QLabel()
        self.empty.setObjectName("Muted")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setContentsMargins(10, 18, 10, 18)
        self.empty.setTextFormat(Qt.RichText)
        self.empty.setOpenExternalLinks(True)
        self.empty.hide()
        bl.addWidget(self.empty)
        self.action_btn = QPushButton()
        self.action_btn.setObjectName("Primary")
        self.action_btn.setCursor(Qt.PointingHandCursor)
        self.action_btn.hide()
        bl.addWidget(self.action_btn, 0, Qt.AlignHCenter)
        outer.addWidget(self.body)

        self.anim = QPropertyAnimation(self.body, b"maximumHeight", self)
        self.anim.setDuration(320)
        self.anim.setEasingCurve(QEasingCurve.InOutCubic)
        self.anim.finished.connect(self._unlock)
        self.body.setMaximumHeight(16777215 if open_ else 0)
        self.rows = {}

    def toggle(self):
        self.is_open = not self.is_open
        self.chev.setText(GLYPH["up" if self.is_open else "down"])
        self.anim.stop()
        self.anim.setStartValue(self.body.height() if self.body.maximumHeight() else 0)
        self.anim.setEndValue(self.body.sizeHint().height() if self.is_open else 0)
        self.anim.start()
        self.toggled.emit(self.is_open)

    def _unlock(self):
        if self.is_open:
            self.body.setMaximumHeight(16777215)

    def set_empty(self, text: str, action: str = ""):
        self.empty.setText(text.replace("\n", "<br>"))
        self.empty.setVisible(bool(text))
        self.action_btn.setText(action)
        self.action_btn.setVisible(bool(action))

    def add_top(self, widget: QWidget):
        self.body_layout.insertWidget(0, widget)

    def set_rows(self, ordered_keys, factory):
        """Reuse row widgets, create new ones, remove stale ones, keep the order."""
        for k in list(self.rows):
            if k not in ordered_keys:
                self.rows.pop(k).deleteLater()
        for k in ordered_keys:
            if k not in self.rows:
                self.rows[k] = factory(k)
        while self.list.count():
            self.list.takeAt(0)
        for k in ordered_keys:
            self.list.addWidget(self.rows[k])


# ---------------------------------------------------------------------------- Device card
class InnerScroll(QScrollArea):
    """Scroll box inside a card. At its top/bottom (or when there is nothing to scroll)
    the mouse wheel goes on to the page, so the page never 'sticks'."""

    def __init__(self, content: QWidget):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.viewport().setAutoFillBackground(False)
        self.setWidget(content)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def wheelEvent(self, e):
        bar = self.verticalScrollBar()
        dy = e.angleDelta().y()
        if bar.maximum() == 0 or (dy > 0 and bar.value() <= bar.minimum()) or \
                (dy < 0 and bar.value() >= bar.maximum()):
            e.ignore()
            return
        super().wheelEvent(e)


class DeviceCard(QFrame):
    """One device: icon | name (+ip) · status pill · usage · chevron. Expands to its websites."""
    context = Signal(str, object)          # mac, global pos (right-click on the header)
    site_context = Signal(str, object)     # domain, global pos
    activated = Signal(str)                # double-click header -> rename
    toggled = Signal(str, bool)
    action = Signal(str)                   # setup button inside the card

    def __init__(self, key: str, palette: Palette, icon: str, open_=False):
        super().__init__()
        self.key, self.p, self.is_open = key, palette, open_
        self.setObjectName("DevCard")
        self.active = False
        self._hover = 0.0
        self.hover_anim = QVariantAnimation(self)
        self.hover_anim.setDuration(170)
        self.hover_anim.setEasingCurve(QEasingCurve.OutCubic)
        self.hover_anim.valueChanged.connect(self._set_hover)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(self.INSET, self.INSET, self.INSET, self.INSET)
        outer.setSpacing(0)

        self.header = QFrame()
        self.header.setObjectName("DevHeader")
        self.header.setCursor(Qt.PointingHandCursor)
        self.header.setContextMenuPolicy(Qt.CustomContextMenu)
        self.header.customContextMenuRequested.connect(
            lambda pos: self.context.emit(self.key, self.header.mapToGlobal(pos)))
        h = QHBoxLayout(self.header)
        h.setContentsMargins(12, 10, 14, 10)
        h.setSpacing(12)
        self.icon = icon_label(icon, 17, "IconTile", color=palette.text)
        self.icon.setFixedSize(40, 40)
        h.addWidget(self.icon)
        sep = QFrame()
        sep.setObjectName("VSep")
        sep.setFixedWidth(1)
        h.addWidget(sep)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.name = QLabel()
        self.name.setObjectName("DevName")
        self.sub = QLabel()
        self.sub.setObjectName("Muted")
        col.addWidget(self.name)
        col.addWidget(self.sub)
        h.addLayout(col, 1)
        self.pill = QLabel()
        self.pill.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        h.addWidget(self.pill, 0, Qt.AlignVCenter)
        self.chip = QLabel()
        self.chip.setObjectName("Badge")
        self.chip.hide()
        h.addWidget(self.chip)
        self.usage = QLabel()
        self.usage.setObjectName("DevUsage")
        self.usage.setMinimumWidth(70)
        self.usage.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        h.addWidget(self.usage)
        self.chev = icon_label("up" if open_ else "down", 12)
        h.addWidget(self.chev)
        self.header.mousePressEvent = lambda e: self.toggle() if e.button() == Qt.LeftButton else None
        self.header.mouseDoubleClickEvent = lambda e: self.activated.emit(self.key)
        self.header.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.header.installEventFilter(self)
        outer.addWidget(self.header)

        self.body = QWidget()
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(14, 0, 14, 12)
        bl.setSpacing(2)
        self.sites_w = QWidget()
        self.sites = QVBoxLayout(self.sites_w)
        self.sites.setContentsMargins(0, 0, 0, 0)
        self.sites.setSpacing(2)
        bl.addWidget(self.sites_w)                     # <= 5 items: plain list
        self.sites_scroll = InnerScroll(QWidget())     # > 5 items: the list moves in here
        self.sites_scroll.hide()
        bl.addWidget(self.sites_scroll)
        self._scrolled = False
        self.empty = QLabel()
        self.empty.setObjectName("Muted")
        self.empty.setWordWrap(True)
        self.empty.setTextFormat(Qt.RichText)
        self.empty.setOpenExternalLinks(True)
        self.empty.setContentsMargins(4, 6, 4, 6)
        bl.addWidget(self.empty)
        self.action_btn = QPushButton()
        self.action_btn.setObjectName("Primary")
        self.action_btn.setCursor(Qt.PointingHandCursor)
        self.action_btn.clicked.connect(lambda: self.action.emit(self.key))
        bl.addWidget(self.action_btn, 0, Qt.AlignHCenter)
        outer.addWidget(self.body)
        self.body.setVisible(open_)
        self.rows = {}
        self._dim = None

    INSET = 3          # the box sits 3 px inside the widget; on hover it grows into that space

    def eventFilter(self, obj, e):
        if obj is self.header and e.type() in (QEvent.Enter, QEvent.Leave):
            self.hover_anim.stop()
            self.hover_anim.setStartValue(self._hover)
            self.hover_anim.setEndValue(1.0 if e.type() == QEvent.Enter else 0.0)
            self.hover_anim.start()
        return False

    def _set_hover(self, v):
        self._hover = float(v)
        self.update()

    def set_active(self, active: bool):
        if self.active != bool(active):
            self.active = bool(active)
            self.update()

    def paintEvent(self, _):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.Antialiasing)
        hv = self._hover
        inset = self.INSET * (1 - hv)
        r = QRectF(self.rect()).adjusted(inset + 0.5, inset + 0.5, -inset - 0.5, -inset - 0.5)
        if hv > 0.01:
            bg = QColor(self.p.hover)
            bg.setAlphaF(min(1.0, 0.9 * hv))
            qp.setBrush(bg)
        else:
            qp.setBrush(Qt.NoBrush)
        if self.active:
            pen = QPen(QColor(self.p.accent), 2)
        else:
            base, lit = QColor(self.p.border), QColor(self.p.subtext)
            c = QColor(int(base.red() + (lit.red() - base.red()) * hv * 0.6),
                       int(base.green() + (lit.green() - base.green()) * hv * 0.6),
                       int(base.blue() + (lit.blue() - base.blue()) * hv * 0.6))
            pen = QPen(c, 1)
        qp.setPen(pen)
        rad = 14 + 2 * hv
        qp.drawRoundedRect(r, rad, rad)

    def set_open(self, open_: bool):
        """Open/close without emitting `toggled` (used for automatic opening)."""
        open_ = bool(open_)
        if open_ == self.is_open:
            return
        self.is_open = open_
        self.chev.setText(GLYPH["up" if open_ else "down"])
        self.body.setVisible(open_)

    def set_nested(self, nested: bool):
        pass

    def toggle(self):
        self.set_open(not self.is_open)
        self.toggled.emit(self.key, self.is_open)

    def set_header(self, name, sub, online, status_text, usage_text, blocked, blocked_text):
        self.name.setText(name)
        self.sub.setText(sub)
        self.sub.setVisible(bool(sub))
        if online is None:
            self.pill.hide()
        else:
            self.pill.show()
            name = "OnPill" if online else "OffPill"
            if self.pill.objectName() != name:
                self.pill.setObjectName(name)
                self.pill.style().unpolish(self.pill)
                self.pill.style().polish(self.pill)
            self.pill.setText(f"●  {status_text}")
        self.usage.setText(usage_text)
        self.usage.setVisible(bool(usage_text))
        self.chip.setText(blocked_text)
        self.chip.setVisible(blocked)
        dim = blocked or online is False
        if dim != self._dim:
            self._dim = dim
            col = self.p.subtext if dim else self.p.text
            self.name.setStyleSheet(f"color: {col};")
            self.usage.setStyleSheet(f"color: {col};")

    VISIBLE_ITEMS = 5

    def fit_inner(self):
        """Up to 5 items: a plain list. More: the list scrolls inside the card (5½ items tall)."""
        lay = self.sites
        items = [x for x in (lay.itemAt(i).widget() for i in range(lay.count())) if x is not None]
        n = self.VISIBLE_ITEMS
        bl = self.body.layout()
        if len(items) > n:
            if not self._scrolled:
                self._scrolled = True
                bl.removeWidget(self.sites_w)
                self.sites_scroll.takeWidget()
                self.sites_scroll.setWidget(self.sites_w)
                self.sites_w.setAutoFillBackground(False)       # setWidget turns it on (white box)
                self.sites_scroll.viewport().setAutoFillBackground(False)
                self.sites_w.show()
                self.sites_scroll.show()
            hint = lambda x: x.sizeHint().height()
            h = sum(hint(x) for x in items[:n]) + lay.spacing() * n + hint(items[n]) // 2
            if self.sites_scroll.minimumHeight() != h or self.sites_scroll.maximumHeight() != h:
                self.sites_scroll.setFixedHeight(h)
        elif self._scrolled:
            self._scrolled = False
            self.sites_scroll.takeWidget()
            self.sites_scroll.setWidget(QWidget())
            self.sites_scroll.hide()
            bl.insertWidget(0, self.sites_w)
            self.sites_w.setAutoFillBackground(False)
            self.sites_w.show()

    def set_children(self, widgets, empty_text=""):
        """Put other cards (the devices of a type group) inside this card."""
        ids = [id(w) for w in widgets]
        if ids != getattr(self, "_child_ids", None):
            self._child_ids = ids
            while self.sites.count():
                self.sites.takeAt(0)
            for w in widgets:
                w.set_nested(True)
                self.sites.addWidget(w)
            self.fit_inner()
        self.empty.setText(empty_text)
        self.empty.setVisible(bool(empty_text))
        self.action_btn.hide()

    def set_sites(self, items, empty_text="", action_text=""):
        """items: [(key, label, bytes_or_None, frac, blocked, blocked_text, index)]"""
        keys = [i[0] for i in items]
        for k in list(self.rows):
            if k not in keys:
                self.rows.pop(k).deleteLater()
        while self.sites.count():
            self.sites.takeAt(0)
        for key, label, nbytes, frac, blocked, btext, index in items:
            row = self.rows.get(key)
            if row is None:
                row = UsageRow(key, self.p)
                row.context.connect(self.site_context)
                self.rows[key] = row
            row.set_data(label, "", nbytes, frac, blocked, btext, index=index)
            self.sites.addWidget(row)
        self.fit_inner()
        self.empty.setText(empty_text.replace("\n", "<br>"))
        self.empty.setVisible(bool(empty_text))
        self.action_btn.setText(action_text)
        self.action_btn.setVisible(bool(action_text))


# ---------------------------------------------------------------------------- Segmented
class Segmented(QFrame):
    """Two (or more) pill buttons; emits the key of the selected one."""
    changed = Signal(str)

    def __init__(self, items, current: str):
        super().__init__()
        self.setObjectName("Seg")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(3)
        self.buttons = {}
        for key, text in items:
            b = QPushButton(text)
            b.setObjectName("SegBtn")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setChecked(key == current)
            b.clicked.connect(lambda _=False, k=key: self.select(k))
            lay.addWidget(b, 1)
            self.buttons[key] = b
        self.current = current

    def select(self, key: str):
        for k, b in self.buttons.items():
            b.setChecked(k == key)
        if key != self.current:
            self.current = key
            self.changed.emit(key)


# ---------------------------------------------------------------------------- Toast
class Toast(QFrame):
    """Small message that slides in at the bottom, with an optional action (e.g. Undo)."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("Toast")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 10, 8, 10)
        self.text = QLabel()
        lay.addWidget(self.text, 1)
        self.btn = QPushButton()
        self.btn.setObjectName("ToastBtn")
        self.btn.setCursor(Qt.PointingHandCursor)
        lay.addWidget(self.btn)
        self.fx = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.fx)
        self.fade = QPropertyAnimation(self.fx, b"opacity", self)
        self.fade.setDuration(220)
        self.timer = QTimer(self, singleShot=True)
        self.timer.timeout.connect(self.dismiss)
        self._cb = None
        self.btn.clicked.connect(self._action)
        self.hide()

    def show_message(self, text: str, action: str = "", callback=None, error=False, ms=5000):
        self.setObjectName("ToastErr" if error else "Toast")
        self.style().unpolish(self)
        self.style().polish(self)
        self.text.setText(text)
        self.btn.setText(action)
        self.btn.setVisible(bool(action))
        self._cb = callback
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        self.fade.stop()
        self.fade.setStartValue(0.0)
        self.fade.setEndValue(1.0)
        self.fade.start()
        self.timer.start(ms)

    def reposition(self):
        par = self.parentWidget()
        w = min(460, par.width() - 40)
        self.setFixedWidth(w)
        self.adjustSize()
        self.move((par.width() - w) // 2, par.height() - self.height() - 24)

    def _action(self):
        cb, self._cb = self._cb, None
        self.dismiss()
        if cb:
            cb()

    def dismiss(self):
        self.timer.stop()
        self.hide()


# ---------------------------------------------------------------------------- Spinner
class Spinner(QWidget):
    def __init__(self, palette: Palette, size: int = 46):
        super().__init__()
        self.p = palette
        self.angle = 0
        self.setFixedSize(size, size)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(16)

    def _tick(self):
        self.angle = (self.angle + 6) % 360
        self.update()

    def paintEvent(self, _):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.Antialiasing)
        r = QRectF(4, 4, self.width() - 8, self.height() - 8)
        qp.setPen(QPen(QColor(self.p.track), 4))
        qp.drawEllipse(r)
        qp.setPen(QPen(QColor(self.p.accent), 4, Qt.SolidLine, Qt.RoundCap))
        qp.drawArc(r, -self.angle * 16, 100 * 16)


# ---------------------------------------------------------------------------- number input
class NumberSpin(QDoubleSpinBox):
    """Decimal input that always accepts both '.' and ',' as the decimal separator."""

    def __init__(self):
        super().__init__()
        self.setLocale(QLocale.c())
        self.setGroupSeparatorShown(False)

    def validate(self, text, pos):
        return super().validate(text.replace(",", "."), pos)

    def valueFromText(self, text):
        return super().valueFromText(text.replace(",", "."))


def ask_number(parent, title, label, value, decimals=2, hint="", maximum=100000.0):
    """Small dialog with a decimal field. Returns (value, ok)."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    v = QVBoxLayout(dlg)
    v.setContentsMargins(16, 14, 16, 12)
    v.setSpacing(8)
    v.addWidget(QLabel(label))
    spin = NumberSpin()
    spin.setDecimals(decimals)
    spin.setRange(0, maximum)
    spin.setValue(value)
    spin.setMinimumWidth(260)
    v.addWidget(spin)
    if hint:
        h = QLabel(hint)
        h.setObjectName("Muted")
        v.addWidget(h)
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    v.addWidget(bb)
    spin.selectAll()
    spin.setFocus()
    ok = bool(dlg.exec())
    return spin.value(), ok
