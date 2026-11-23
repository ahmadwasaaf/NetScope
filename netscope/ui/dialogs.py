"""Start-date picker and Settings dialog."""
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import QDate, Qt, QUrl
from PySide6.QtGui import QColor, QCursor, QDesktopServices
from PySide6.QtWidgets import (QCalendarWidget, QCheckBox, QColorDialog, QComboBox, QDialog,
                               QDoubleSpinBox, QFormLayout, QFrame, QHBoxLayout, QInputDialog, QLabel,
                               QMenu, QMessageBox, QPushButton, QSpinBox, QTabWidget,
                               QVBoxLayout, QWidget)

from ..i18n import Translator
from ..settings import Settings
from ..theme import ACCENT_PRESETS
from .widgets import NumberSpin, icon_label


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


class DnsLogDialog(QDialog):
    """Recent DNS lookups NetScope's helper received – to see exactly what reaches it."""

    def __init__(self, tr, fetch, name_of, shown_as, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem
        self._Item = QTableWidgetItem
        self.tr, self.fetch, self.name_of, self.shown_as = tr, fetch, name_of, shown_as
        self.setWindowTitle(tr("dns_log"))
        self.resize(760, 560)
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        t = QLabel(tr("dns_log_title"))
        t.setObjectName("SectionTitle")
        top.addWidget(t, 1)
        self.dev = QComboBox()
        self.dev.currentIndexChanged.connect(self._fill)
        top.addWidget(self.dev)
        again = QPushButton("⟳")
        again.setFixedWidth(40)
        again.clicked.connect(self._load)
        top.addWidget(again)
        v.addLayout(top)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([tr("dns_log_time"), tr("dns_log_device"),
                                              tr("dns_log_name"), tr("dns_log_shown")])
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        v.addWidget(self.table, 1)
        self.empty = QLabel(tr("dns_log_empty"))
        self.empty.setObjectName("Muted")
        v.addWidget(self.empty)
        self._load()

    def _load(self):
        self.rows = [(t, ip, self.name_of(ip), name) for t, ip, name, _ in reversed(self.fetch())]
        cur = self.dev.currentData()
        self.dev.blockSignals(True)
        self.dev.clear()
        self.dev.addItem(self.tr("dns_log_all"), "")
        for dev in sorted({r[2] for r in self.rows}):
            self.dev.addItem(dev, dev)
        i = self.dev.findData(cur) if cur else 0
        self.dev.setCurrentIndex(max(0, i))
        self.dev.blockSignals(False)
        self._fill()

    def _fill(self):
        want = self.dev.currentData()
        rows = [r for r in self.rows if not want or r[2] == want][:400]
        self.table.setRowCount(len(rows))
        for i, (t, ip, dev, name) in enumerate(rows):
            for j, val in enumerate((datetime.fromtimestamp(t).strftime("%H:%M:%S"), dev, name, self.shown_as(name))):
                self.table.setItem(i, j, self._Item(val))
        self.empty.setVisible(not rows)


class RouterRow(QFrame):
    """One saved router in Settings › Router: icon, name, model, status, ⋯ menu."""

    def __init__(self, tr, prof: dict, is_active: bool, status: str, on_pick, on_rename, on_remove):
        super().__init__()
        self.setObjectName("RouterRowActive" if is_active else "RouterRow")
        self.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 10, 10, 10)
        h.setSpacing(12)
        h.addWidget(icon_label("wifi", 16))
        col = QVBoxLayout()
        col.setSpacing(1)
        name = QLabel(prof.get("name") or prof.get("router_label") or prof.get("host", ""))
        name.setStyleSheet("font-size: 13px; font-weight: 700;")
        col.addWidget(name)
        model = prof.get("model") or prof.get("router_label") or ""
        sub = QLabel(" · ".join(x for x in (model, prof.get("host", "")) if x))
        sub.setObjectName("Muted")
        col.addWidget(sub)
        st = QLabel(status)
        st.setObjectName("Muted")
        st.setStyleSheet("font-size: 11px;")
        col.addWidget(st)
        h.addLayout(col, 1)
        if is_active:
            h.addWidget(icon_label("check", 14, "CapOn"))
        more = QPushButton("⋮")
        more.setObjectName("MoreBtn")
        more.setFixedSize(32, 32)
        more.setCursor(Qt.PointingHandCursor)
        more.setToolTip(tr("options"))
        menu = QMenu(more)
        menu.addAction(tr("rename")).triggered.connect(lambda: on_rename(prof["id"]))
        menu.addAction(tr("remove_router")).triggered.connect(lambda: on_remove(prof["id"]))
        more.clicked.connect(lambda: menu.exec(QCursor.pos()))
        h.addWidget(more)
        self._pick = lambda: on_pick(prof["id"])

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._pick()


class SettingsDialog(QDialog):
    def __init__(self, tr: Translator, settings: Settings, parent=None, poller=None, tab=0,
                 online=False, router_name=""):
        super().__init__(parent)
        self.tr = tr
        self.poller = poller
        self.s = replace(settings, nicknames=dict(settings.nicknames),
                         routers=[dict(p) for p in settings.routers])
        self.change_router = False
        self.switch_to = ""
        self.remove_id = ""
        self.online = online
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
        self.quota = NumberSpin()
        self.quota.setRange(0, 100000)
        self.quota.setDecimals(2)
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

        # ---------------- Router: saved routers + options of the one in use
        r = QWidget()
        rv = QVBoxLayout(r)
        rv.setSpacing(10)
        title = QLabel(tr("my_routers"))
        title.setObjectName("SectionTitle")
        rv.addWidget(title)
        self.rows_box = QVBoxLayout()
        self.rows_box.setSpacing(8)
        rv.addLayout(self.rows_box)
        self._fill_routers()
        add = QPushButton(tr("add_router") + "  +")
        add.clicked.connect(self._change_router)
        rv.addWidget(add)
        rf = QFormLayout()
        rf.setVerticalSpacing(12)
        self.refresh = QSpinBox()
        self.refresh.setRange(2, 600)
        self.refresh.setValue(self.s.refresh_sec)
        rf.addRow(tr("refresh"), self.refresh)
        if self.s.backend == "huawei" and self.poller is not None:
            self.diag_btn = QPushButton(tr("diag"))
            self.diag_btn.setToolTip(tr("diag_tip"))
            self.diag_btn.clicked.connect(self._diagnostics)
            rf.addRow("", self.diag_btn)
            self.undo_btn = QPushButton(tr("undo_all"))
            self.undo_btn.setToolTip(tr("undo_all_tip"))
            self.undo_btn.clicked.connect(self._undo_all)
            rf.addRow("", self.undo_btn)
        rv.addLayout(rf)
        rv.addStretch(1)
        tabs.addTab(r, tr("router"))
        tabs.setCurrentIndex(tab)

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

    def _fill_routers(self):
        while self.rows_box.count():
            w = self.rows_box.takeAt(0).widget()
            if w:
                w.deleteLater()
        if not self.s.routers:
            empty = QLabel(self.tr("no_routers"))
            empty.setObjectName("Muted")
            self.rows_box.addWidget(empty)
        for p in self.s.routers:
            act = p["id"] == self.s.active_router and self.s.backend != "demo"
            status = self.tr("rt_connected" if act and self.online else ("rt_in_use" if act else "rt_saved"))
            self.rows_box.addWidget(RouterRow(self.tr, p, act, status, self._pick_router,
                                              self._rename_router, self._remove_router))

    def _pick_router(self, pid):
        if pid != self.s.active_router or self.s.backend == "demo":
            self.switch_to = pid
            self.accept()

    def _rename_router(self, pid):
        p = next((x for x in self.s.routers if x["id"] == pid), None)
        if not p:
            return
        text, ok = QInputDialog.getText(self, "NetScope", self.tr("rename"),
                                        text=p.get("name") or p.get("router_label") or "")
        if ok and text.strip():
            p["name"] = text.strip()[:40]
            self._fill_routers()

    def _remove_router(self, pid):
        self.remove_id = pid
        self.accept()

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
