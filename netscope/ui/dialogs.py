"""Start-date picker and Settings dialog."""
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import QDate, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QCalendarWidget, QCheckBox, QColorDialog, QComboBox, QDialog,
                               QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QSpinBox, QTabWidget,
                               QVBoxLayout, QWidget)

from ..i18n import Translator
from ..settings import Settings
from ..theme import ACCENT_PRESETS


def npcap_version() -> str:
    """Installed Npcap version ('' when not installed)."""
    try:
        import winreg
    except ImportError:
        return ""
    for key in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\NpcapInst",
                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\NpcapInst"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                return str(winreg.QueryValueEx(k, "DisplayVersion")[0])
        except OSError:
            continue
    import os
    dll = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "Npcap", "wpcap.dll")
    return "✓" if os.path.exists(dll) else ""


def desktop_dir() -> Path:
    """The real Desktop – on many PCs it is redirected into OneDrive."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0 and buf.value:
            return Path(buf.value)          # CSIDL_DESKTOPDIRECTORY
    except Exception:
        pass
    for p in (Path.home() / "OneDrive" / "Desktop", Path.home() / "Desktop"):
        if p.exists():
            return p
    return Path.home()


class DateDialog(QDialog):
    """Pick the start of counting. Today (or "Start from now") = from this very minute,
    an earlier day = from that day's 00:00."""

    def __init__(self, tr: Translator, current, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("pick_start"))
        self.result_value = None
        v = QVBoxLayout(self)
        self.cal = QCalendarWidget()
        self.cal.setGridVisible(False)
        self.cal.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        self.cal.setMaximumDate(QDate.currentDate())
        self.cal.setSelectedDate(QDate(current.year, current.month, current.day))
        v.addWidget(self.cal)
        hint = QLabel(tr("start_hint"))
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        v.addWidget(hint)
        row = QHBoxLayout()
        now_btn = QPushButton(tr("start_now"))
        now_btn.clicked.connect(self._now)
        row.addWidget(now_btn)
        row.addStretch(1)
        cancel = QPushButton(tr("cancel"))
        ok = QPushButton(tr("ok"))
        ok.setObjectName("Primary")
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        v.addLayout(row)

    def _now(self):
        self.result_value = datetime.now().replace(second=0, microsecond=0)
        self.accept()

    def value(self) -> datetime:
        if self.result_value:
            return self.result_value
        d = self.cal.selectedDate()
        picked = date(d.year(), d.month(), d.day())
        if picked >= date.today():
            return datetime.now().replace(second=0, microsecond=0)
        return datetime(picked.year, picked.month, picked.day)


class Swatch(QPushButton):
    def __init__(self, color: str, selected: bool):
        super().__init__()
        self.setFixedSize(28, 28)
        self.setCursor(Qt.PointingHandCursor)
        border = "3px solid #FFFFFF" if selected else "2px solid transparent"
        self.setStyleSheet(f"background:{color}; border-radius:14px; border:{border};")


class SettingsDialog(QDialog):
    def __init__(self, tr: Translator, settings: Settings, parent=None, poller=None):
        super().__init__(parent)
        self.tr = tr
        self.poller = poller
        self.s = replace(settings, nicknames=dict(settings.nicknames))
        self.change_router = False
        self.setWindowTitle(tr("settings"))
        self.setMinimumWidth(460)
        v = QVBoxLayout(self)
        tabs = QTabWidget()
        v.addWidget(tabs)

        # ---------------- General
        g = QWidget()
        gf = QFormLayout(g)
        gf.setVerticalSpacing(14)
        self.lang = QComboBox()
        self.lang.addItem("English", "en")
        self.lang.addItem("العربية", "ar")
        self.lang.setCurrentIndex(0 if self.s.language == "en" else 1)
        gf.addRow(tr("language"), self.lang)
        self.theme = QComboBox()
        self.theme.addItem(tr("dark"), "dark")
        self.theme.addItem(tr("light"), "light")
        self.theme.setCurrentIndex(0 if self.s.theme == "dark" else 1)
        gf.addRow(tr("theme"), self.theme)
        self.swatch_row = QHBoxLayout()
        sw = QWidget()
        sw.setLayout(self.swatch_row)
        gf.addRow(tr("accent"), sw)
        self._build_swatches()
        self.quota = QDoubleSpinBox()
        self.quota.setRange(0, 100000)
        self.quota.setDecimals(0)
        self.quota.setValue(self.s.quota_gb)
        gf.addRow(tr("quota"), self.quota)
        # Npcap (needed for "This PC" website monitoring)
        ver = npcap_version()
        np_row = QHBoxLayout()
        np_lbl = QLabel(tr("npcap_installed", v=ver) if ver else tr("npcap_not_installed"))
        np_lbl.setObjectName("Muted")
        np_row.addWidget(np_lbl, 1)
        np_btn = QPushButton(tr("npcap_download"))
        np_btn.setToolTip("https://npcap.com/#download")
        np_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://npcap.com/#download")))
        np_row.addWidget(np_btn)
        np_w = QWidget()
        np_w.setLayout(np_row)
        gf.addRow("Npcap", np_w)
        self.autostart = QCheckBox(tr("autostart"))
        self.autostart.setChecked(self.s.autostart)
        gf.addRow("", self.autostart)
        tabs.addTab(g, tr("general"))

        # ---------------- Router
        r = QWidget()
        rf = QFormLayout(r)
        rf.setVerticalSpacing(14)
        name = QLabel(self.s.router_label or self.s.host)
        name.setObjectName("SectionTitle")
        rf.addRow(tr("router_card"), name)
        rf.addRow(tr("address"), QLabel(self.s.host if self.s.backend != "demo" else "—"))
        self.refresh = QSpinBox()
        self.refresh.setRange(2, 600)
        self.refresh.setValue(self.s.refresh_sec)
        rf.addRow(tr("refresh"), self.refresh)
        change = QPushButton(tr("change_router"))
        change.clicked.connect(self._change_router)
        rf.addRow("", change)
        if self.s.backend == "huawei" and self.poller is not None:
            self.diag_btn = QPushButton(tr("diag"))
            self.diag_btn.setToolTip(tr("diag_tip"))
            self.diag_btn.clicked.connect(self._diagnostics)
            rf.addRow("", self.diag_btn)
            self.undo_btn = QPushButton(tr("undo_all"))
            self.undo_btn.setToolTip(tr("undo_all_tip"))
            self.undo_btn.clicked.connect(self._undo_all)
            rf.addRow("", self.undo_btn)
        tabs.addTab(r, tr("router"))

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton(tr("cancel"))
        save = QPushButton(tr("save"))
        save.setObjectName("Primary")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(save)
        v.addLayout(row)

    def _build_swatches(self):
        while self.swatch_row.count():
            w = self.swatch_row.takeAt(0).widget()
            if w:
                w.deleteLater()
        colors = list(ACCENT_PRESETS)
        if self.s.accent.upper() not in [c.upper() for c in colors]:
            colors.append(self.s.accent)
        for c in colors:
            b = Swatch(c, c.upper() == self.s.accent.upper())
            b.clicked.connect(lambda _=False, col=c: self._pick(col))
            self.swatch_row.addWidget(b)
        custom = QPushButton(self.tr("custom"))
        custom.clicked.connect(self._custom)
        self.swatch_row.addWidget(custom)
        self.swatch_row.addStretch(1)

    def _pick(self, color: str):
        self.s.accent = color
        self._build_swatches()

    def _custom(self):
        c = QColorDialog.getColor(QColor(self.s.accent), self, self.tr("accent"))
        if c.isValid():
            self._pick(c.name().upper())

    def _diagnostics(self):
        self.diag_btn.setEnabled(False)
        self.diag_btn.setText(self.tr("connecting"))

        def done(text):
            path = desktop_dir() / "NetScope-router-report.txt"
            path.write_text(text, encoding="utf-8")
            self.diag_btn.setEnabled(True)
            self.diag_btn.setText(self.tr("diag"))
            QMessageBox.information(self, "NetScope", self.tr("diag_saved", path=str(path)))
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

        def failed(err):
            self.diag_btn.setEnabled(True)
            self.diag_btn.setText(self.tr("diag"))
            QMessageBox.warning(self, "NetScope", err)

        # same router session as the live monitor – a second login would kick it out
        self.poller.call("diagnostics", done, failed)

    def _undo_all(self):
        if QMessageBox.question(self, "NetScope", self.tr("undo_all_confirm")) != QMessageBox.Yes:
            return
        self.undo_btn.setEnabled(False)

        def done(text):
            self.undo_btn.setEnabled(True)
            QMessageBox.information(self, "NetScope", text)

        def failed(err):
            self.undo_btn.setEnabled(True)
            QMessageBox.warning(self, "NetScope", err)

        self.poller.call("undo_all", done, failed)

    def _change_router(self):
        self.change_router = True
        self.accept()

    def result_settings(self) -> Settings:
        s = self.s
        s.language = self.lang.currentData()
        s.theme = self.theme.currentData()
        s.quota_gb = self.quota.value()
        s.autostart = self.autostart.isChecked()
        s.refresh_sec = self.refresh.value()
        return s
