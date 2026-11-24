"""NetScope main window – one column of cards and expandable sections:

    header (name · Live · settings)
    usage gauge · line-speed tiles · Start date / Bundle / Alarm me
    ▸ Router                                  (expandable)
    ▸ Connected Devices & Website Usage       (expandable; each device expands to its sites)

Closing the window keeps NetScope running in the tray; it starts with Windows and, when it
was not running, adds the usage the router counted meanwhile.
"""
import ctypes
import re
import sys
import time

from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QCursor, QDesktopServices, QGuiApplication, QIcon
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QFrame, QHBoxLayout, QInputDialog, QLabel,
                               QLayout, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QSizePolicy, QSystemTrayIcon, QTextEdit,
                               QVBoxLayout, QWidget)

from .. import APP_NAME, APP_VERSION
from ..backends import Cap, Snapshot, clean_domain, create_backend, detect
from ..i18n import Translator
from ..monitor.dnsmon import DnsMonitor
from ..monitor.domains import is_system
from ..monitor.pcap import UNRESOLVED, PcMonitor, _primary_ipv4

SYSTEM_KEY = "__system__"
from ..poller import Poller, Task
from ..settings import Settings, data_dir
from ..store import UsageStore
from ..theme import make_palette, stylesheet
from .. import profiles, winsetup
from ..backends.verified import tested as tested_caps
from .dialogs import DateDialog, SettingsDialog, npcap_version
from .widgets import (GLYPH, CollapsibleSection, DeviceCard, Gauge, SpeedTile, Switch, Toast, ask_number, fmt_size,
                      icon_label,
                      icon_button, icon_label)
from .wizard import SetupWizard, caps_widget

INVERSE = {"block_device": "unblock_device", "unblock_device": "block_device",
           "block_domain": "unblock_domain", "unblock_domain": "block_domain"}
BLOCKED_KEY = "__blocked_sites__"
GB = 1024 ** 3


def set_dark_titlebar(win, dark: bool):
    """Windows 10/11: match the native title bar to the theme."""
    if sys.platform != "win32":
        return
    try:
        hwnd = int(win.winId())
        val = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), 4) == 0:
                break
    except Exception:
        pass


def set_autostart(enabled: bool):
    """Start with Windows (current user) – only for the installed / built NetScope.exe."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{sys.executable}" --minimized')
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except OSError:
                pass
        winreg.CloseKey(key)
    except OSError:
        pass


class MainWindow(QMainWindow):
    def __init__(self, start_hidden: bool = False):
        super().__init__()
        self.s = Settings.load()
        if self.s.ui_version < 2:
            self.s.devices_open = self.s.router_open = True
            self.s.dns_monitor = True
            self.s.ui_version = 2
            self.s.save()
        if profiles.migrate(self.s):                # single router of older versions -> first profile
            self.s.save()
        self.poller = None
        self.snap = Snapshot(online=False)
        self.connected_once = False
        self.caps = set()
        self.blocked_macs: set = set()
        self.blocked_domains: set = set()
        self.open_devices: set = set()
        self._sys_open: set = set()            # cards whose "System services" row is open
        self.active_key = None                 # the one card the user picked last (highlighted)
        self._v6_macs, self._v6_wait, self._v6_at = {}, {}, 0.0
        QApplication.instance().installEventFilter(self)          # right-click -> Screenshot
        self.user_closed: set = set()          # devices the user closed by hand (not re-opened automatically)
        self.cards = {}
        self._quitting = False
        self._tray_hint_shown = False
        avail = QGuiApplication.primaryScreen().availableGeometry() if QGuiApplication.primaryScreen() else None
        aw, ah = (avail.width(), avail.height()) if avail else (1280, 960)
        w, h = self.s.win_w or min(1120, aw - 80), self.s.win_h or min(900, ah - 60)
        self.resize(max(520, min(w, aw)), max(480, min(h, ah - 20)))   # never bigger than the screen
        self._win_size = None
        self.setMinimumWidth(520)
        self._open_store()
        self._apply_look()
        self.pc_mon = PcMonitor()               # monitors live for the whole app run
        if self.s.pc_monitor:
            self.pc_mon.start()
        self.dns_mon = None
        if self.s.dns_monitor:
            self._start_dns()
        self._build()
        self._setup_tray()
        set_autostart(self.s.autostart)
        self._start_poller()
        self.mon_timer = QTimer(self)
        self.mon_timer.timeout.connect(self._collect_monitors)
        self.mon_timer.start(2000)
        self.win_timer = QTimer(self)
        self.win_timer.timeout.connect(self._read_win_state)
        self.win_timer.start(60000)
        QTimer.singleShot(1500, self._read_win_state)
        self._net_task = None
        self._unknown_macs: set = set()
        self.net_timer = QTimer(self)
        self.net_timer.timeout.connect(self._check_network)
        self.net_timer.start(20000)
        QTimer.singleShot(6000, self._check_network)
        if not start_hidden:
            self.show()
        if not self.s.onboarded:
            QTimer.singleShot(250, self._run_wizard)
        elif self.s.backend == "generic":
            QTimer.singleShot(1500, self._suggest_full_control)

    # ================================================================ setup
    def _open_store(self):
        old = getattr(self, "store", None)
        self.store = UsageStore(data_dir() / f"usage_{profiles.store_tag(self.s)}.db")
        if old is not None:
            try:
                old.db.close()
            except Exception:
                pass

    def _apply_look(self):
        self.tr = Translator(self.s.language)
        self.p = make_palette(self.s.theme, self.s.accent)
        app = QApplication.instance()
        app.setLayoutDirection(Qt.RightToLeft if self.tr.rtl else Qt.LeftToRight)
        app.setStyleSheet(stylesheet(self.p))
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        set_dark_titlebar(self, self.s.theme == "dark")

    def _start_poller(self):
        if self.poller:
            self.poller.shutdown()
        self.connected_once = False
        backend = create_backend(self.s, seed_demo=self.store.is_empty())
        self.poller = Poller(backend, self.s.refresh_sec)
        self.poller.snapshot.connect(self._on_snapshot)
        self.poller.action_done.connect(self._on_action_done)

    # ================================================================ tray / background
    def _setup_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = QApplication.instance().windowIcon()
        self.tray = QSystemTrayIcon(icon if not icon.isNull() else QIcon(), self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        self._tray_open = QAction(self.tr("tray_open"), menu)
        self._tray_open.triggered.connect(self.show_from_tray)
        self._tray_quit = QAction(self.tr("tray_quit"), menu)
        self._tray_quit.triggered.connect(self.quit_app)
        menu.addAction(self._tray_open)
        menu.addSeparator()
        menu.addAction(self._tray_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.show_from_tray() if r in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self.tray.show()

    def show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def notify(self, title: str, text: str):
        if self.tray:
            self.tray.showMessage(title, text, QSystemTrayIcon.Information, 10000)

    def quit_app(self):
        self._quitting = True
        self.close()
        QApplication.instance().quit()

    # ================================================================ layout
    def _build(self):
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 14, 14, 0)
        outer.setSpacing(12)
        outer.addWidget(self._header())

        def column():
            sc = QScrollArea()
            sc.setProperty("page", True)             # screenshots draw its whole content
            sc.setWidgetResizable(True)
            sc.setFrameShape(QFrame.NoFrame)
            sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)   # content follows the window width
            b = QWidget()
            b.setObjectName("ScrollBody")
            sc.setWidget(b)
            lay = QVBoxLayout(b)
            lay.setContentsMargins(0, 0, 0, 80)      # room to scroll the last card fully into view
            lay.setSpacing(12)
            return sc, b, lay

        # wide window: overview + router on one side, devices & websites on the other
        cols = QHBoxLayout()
        cols.setSpacing(14)
        outer.addLayout(cols, 1)
        scroll, body, col = column()
        self.scroll2, self.scroll_body2, self.col2 = column()
        self.col2.setContentsMargins(0, 0, 0, 24)
        cols.addWidget(scroll, 1)
        cols.addWidget(self.scroll2, 1)
        self.scroll, self.scroll_body, self.col1 = scroll, body, col
        self._wide = None
        col.addWidget(self._gauge_card())

        # ---- router (expandable)
        self.router_section = CollapsibleSection(self.tr("router"), "wifi", self.p, open_=self.s.router_open)
        self.router_section.toggled.connect(lambda o: self._remember("router_open", o))
        self.router_section.toggled.connect(lambda o: o and self._read_win_state())
        self.router_section.anim.finished.connect(self._schedule_fit)
        self.router_section.badge.setCursor(Qt.PointingHandCursor)
        self.router_section.badge.mousePressEvent = lambda e: self._router_menu()
        self.router_box = QWidget()
        rv = QVBoxLayout(self.router_box)
        rv.setContentsMargins(10, 0, 10, 4)
        self.router_ip = QLabel()
        self.router_ip.setObjectName("Muted")
        rv.addWidget(self.router_ip)
        self.line_info = QLabel()
        self.line_info.setObjectName("Muted")
        rv.addWidget(self.line_info)
        works = QLabel(self.tr("what_works"))
        works.setObjectName("Muted")
        rv.addSpacing(4)
        rv.addWidget(works)
        self.caps_holder = QVBoxLayout()
        rv.addLayout(self.caps_holder)
        rv.addSpacing(10)
        self.setup_title = QLabel("<b>" + self.tr("ps_title") + "</b>")
        rv.addWidget(self.setup_title)
        self.setup_box = QVBoxLayout()
        self.setup_box.setSpacing(8)
        rv.addLayout(self.setup_box)
        log_btn = QPushButton(self.tr("dns_log"))
        log_btn.setObjectName("Ghost")
        log_btn.setCursor(Qt.PointingHandCursor)
        log_btn.clicked.connect(self._show_dns_log)
        lrow = QHBoxLayout()
        lrow.addWidget(log_btn)
        lrow.addStretch(1)
        rv.addSpacing(4)
        rv.addLayout(lrow)
        self.win_state = {}
        self.lan_state = {}
        self._lan_busy = False
        self._v6_resynced = False
        self._win_task = None
        self._setup_sig = None
        self._caps_shown = None
        self.router_section.list.addWidget(self.router_box)
        col.addWidget(self.router_section)

        # ---- devices & websites (expandable)
        self.dev_section = CollapsibleSection(self.tr("devices_sites"), "network", self.p,
                                              open_=self.s.devices_open,
                                              search_placeholder=self.tr("search_devices"))
        self.dev_section.badge.setObjectName("CountBadge")
        self.dev_section.toggled.connect(lambda o: self._remember("devices_open", o))
        self.dev_section.anim.finished.connect(self._schedule_fit)
        self.dev_section.search.textChanged.connect(lambda _: self._refresh_devices())
        self.add_site_btn = icon_button("add", 13)
        self.add_site_btn.setToolTip(self.tr("add_site"))
        self.add_site_btn.clicked.connect(self._add_site)
        self.dev_section.extra.addWidget(self.add_site_btn)
        self.dev_section.list.setSpacing(8)
        self.offline_btn = QPushButton()
        self.offline_btn.setObjectName("Ghost")
        self.offline_btn.setCursor(Qt.PointingHandCursor)
        self.offline_btn.clicked.connect(self._toggle_offline)
        self.dev_section.body_layout.addWidget(self.offline_btn)
        self._card_order = None
        col.addWidget(self.dev_section)
        col.addStretch(1)
        self.col2.addStretch(1)
        self._layout_columns()

        self.cards = {}
        self.dev_cards = {}
        self.toast = Toast(root)
        self._refresh_view()

    # ---------------------------------------------------------------- screenshots
    def eventFilter(self, obj, e):
        if e.type() == QEvent.ContextMenu and self.s.screenshot_enabled and isinstance(obj, QWidget):
            w = obj
            while w is not None:                      # widgets with their own right-click menu
                if w.contextMenuPolicy() == Qt.CustomContextMenu or isinstance(
                        w, (QLineEdit, QAbstractSpinBox, QTextEdit, QPlainTextEdit, QMenu)):
                    return False
                if w.isWindow():
                    break
                w = w.parentWidget()
            top = obj.window()
            if top is None or isinstance(top, QMenu):
                return False
            m = self._menu()
            self._add(m, self.tr("shot_menu"), lambda: self._screenshot(top))
            m.exec(e.globalPos())
            return True
        return super().eventFilter(obj, e)

    def _shot_action(self, menu):
        if self.s.screenshot_enabled:
            menu.addSeparator()
            self._add(menu, self.tr("shot_menu"), lambda: self._screenshot(self))

    def _screenshot(self, top):
        from .shot import ShotDialog, expand_inner_scrolls, render_window, restore, save_png
        from .dialogs import desktop_dir
        dlg = ShotDialog(self.tr, top.width(), top)
        if not dlg.exec() or not dlg.scale:
            return
        QApplication.processEvents()
        if top is self:
            # lists that scroll inside a card are laid out in full for the picture
            # (fit_inner decides that, so it must not cap them again while capturing)
            keep = DeviceCard.VISIBLE_ITEMS
            DeviceCard.VISIBLE_ITEMS = 10 ** 6
            self._fit_body()
            QApplication.processEvents()
            self._fit_body()
            QApplication.processEvents()
            try:
                img = render_window(self, dlg.scale, self.p.window,
                                    [(self.scroll, self.scroll_body), (self.scroll2, self.scroll_body2)])
            finally:
                DeviceCard.VISIBLE_ITEMS = keep
                self._fit_body()
        else:
            img = render_window(top, dlg.scale, self.p.card)
        try:
            path = save_png(img, Path(desktop_dir()))
        except OSError as ex:
            self.toast.show_message(f"{self.tr('ps_failed')}\n{ex}", error=True)
            return
        self.toast.show_message(self.tr("shot_saved", name=path.name, w=img.width(), h=img.height()),
                                self.tr("shot_open"),
                                lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))), ms=12000)

    WIDE_AT = 1000          # px: from this width on, devices & websites get their own column

    def _layout_columns(self):
        if not hasattr(self, "scroll2"):
            return
        wide = self.width() >= self.WIDE_AT
        if wide == self._wide:
            return
        self._wide = wide
        if wide:
            self.col1.removeWidget(self.dev_section)
            self.col2.insertWidget(0, self.dev_section)
        else:
            self.col2.removeWidget(self.dev_section)
            self.col1.insertWidget(self.col1.count() - 1, self.dev_section)
        self.scroll2.setVisible(wide)
        self._schedule_fit()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "scroll_body"):
            self._layout_columns()
            self._schedule_fit()
            if self.isVisible() and not self.isMaximized():
                self._win_size = (self.width(), self.height())
        if hasattr(self, "toast") and self.toast.isVisible():
            self.toast.reposition()

    def _header(self) -> QWidget:
        h = QFrame()
        h.setObjectName("Header")
        lay = QHBoxLayout(h)
        lay.setContentsMargins(14, 10, 10, 10)
        icon = icon_label("chart", 16, "AppIcon")
        icon.setFixedSize(36, 36)
        lay.addWidget(icon)
        col = QVBoxLayout()
        col.setSpacing(0)
        t = QHBoxLayout()
        t.setSpacing(4)
        title = QLabel(APP_NAME)
        title.setObjectName("Title")
        ver = QLabel(f"v{APP_VERSION}")
        ver.setObjectName("Version")
        t.addWidget(title)
        t.addWidget(ver, 0, Qt.AlignBottom)
        t.addStretch(1)
        col.addLayout(t)
        sub = QLabel(self.tr("subtitle"))
        sub.setObjectName("Sub")
        col.addWidget(sub)
        lay.addLayout(col, 1)
        self.status = QLabel()
        lay.addWidget(self.status)
        gear = icon_button("settings", 17)
        gear.setToolTip(self.tr("settings"))
        gear.clicked.connect(self._open_settings)
        lay.addWidget(gear)
        return h

    def _gauge_card(self) -> QWidget:
        card = QFrame()
        card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 8, 18, 16)
        v.setSpacing(12)
        top = QHBoxLayout()
        top.addStretch(1)
        self.note = QLabel(self.tr("demo_note"))
        self.note.setObjectName("Badge")
        top.addWidget(self.note)
        v.addLayout(top)
        self.gauge = Gauge(self.p)
        self.gauge.caption = self.tr("gb_consumed")
        v.addWidget(self.gauge)

        self.speed_row = QWidget()
        sr = QHBoxLayout(self.speed_row)
        sr.setContentsMargins(0, 0, 0, 0)
        sr.setSpacing(10)
        self.down_tile = SpeedTile(self.p, self.tr("download"), "arrow_down", now_label=self.tr("in_use"))
        self.up_tile = SpeedTile(self.p, self.tr("upload"), "arrow_up", "#00B8D4", now_label=self.tr("in_use"))
        sr.addWidget(self.down_tile)
        sr.addWidget(self.up_tile)
        v.addWidget(self.speed_row)

        div = QFrame()
        div.setObjectName("Divider")
        v.addWidget(div)
        btns = QHBoxLayout()
        btns.setSpacing(8)
        for text, glyph, slot in ((self.tr("set_date"), "calendar", self._pick_date),
                                  (self.tr("set_quota"), "pie", self._set_quota),
                                  (self.tr("alarm_me"), "alarm", self._set_alarm)):
            b = QPushButton(f"{text}  {GLYPH[glyph]}")
            b.setObjectName("Ghost")
            b.setStyleSheet("font-family: 'Segoe UI', 'Segoe Fluent Icons', 'Segoe MDL2 Assets';")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(slot)
            b.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)   # share the row, never widen it
            b.setMinimumWidth(60)
            btns.addWidget(b, 1)
        v.addLayout(btns)
        since_row = QHBoxLayout()
        since = QLabel(self.tr("tracking_since"))
        since.setObjectName("Muted")
        self.date_lbl = QLabel()
        self.date_lbl.setObjectName("DateValue")
        self.alarm_lbl = QLabel()
        self.alarm_lbl.setObjectName("Muted")
        since_row.addWidget(since)
        since_row.addWidget(self.date_lbl)
        since_row.addStretch(1)
        since_row.addWidget(self.alarm_lbl)
        v.addLayout(since_row)
        return card

    # ================================================================ data
    def _on_snapshot(self, snap: Snapshot):
        self.snap = snap
        if snap.online:
            self.connected_once = True
            self.caps = set(snap.capabilities)
            self.store.remember_devices((d.mac, d.name, d.ip, d.dtype) for d in snap.devices)
            self.store.add("dev", snap.device_deltas)
            self.store.add("site", snap.site_deltas)
            if snap.wan_delta:
                self.store.add("wan", {"wan": snap.wan_delta})
            if snap.catch_up_bytes > 1024 * 1024:
                msg = self.tr("catch_up", gb=fmt_size(snap.catch_up_bytes))
                self.toast.show_message(msg, ms=12000)
                if not self.isVisible():
                    self.notify(APP_NAME, msg)
            if Cap.DEVICE_BLOCK in self.caps:
                self.blocked_macs = set(snap.blocked_macs)
            if Cap.SITE_BLOCK in self.caps:
                self.blocked_domains = set(snap.blocked_domains)
            if Cap.LIVE_SPEED in self.caps or Cap.LINE_RATE in self.caps:
                self.down_tile.set_values(snap.line_down_kbps, snap.down_bps)
                self.up_tile.set_values(snap.line_up_kbps, snap.up_bps)
        if snap.online and not getattr(self, "_lan_asked", False):
            self._lan_asked = True
            QTimer.singleShot(500, self._read_lan_state)
        alive = bool(snap.online and not snap.error)      # waves flow while the router is reachable
        self.down_tile.set_alive(alive)
        self.up_tile.set_alive(alive)
        self._refresh_view()

    def _display_name(self, mac, router_name):
        name = self.s.nicknames.get(mac) or router_name
        if not name or name.lower() == mac.lower():
            return self.tr("unknown_device")
        return name

    def _used_bytes(self) -> int:
        start, caps = self.s.start, self.caps
        wan_total = self.store.totals("wan", start).get("wan", 0)
        if Cap.TOTAL_USAGE in caps and wan_total:
            return wan_total
        return sum(self.store.totals("dev", start).values())

    def _refresh_view(self):
        tr, caps = self.tr, self.caps
        online = self.snap.online
        if online:
            self.status.setObjectName("LiveBadge")
            self.status.setText(f"●  {tr('monitoring')}")
        elif self.connected_once or self.snap.error:
            self.status.setObjectName("OffBadge")
            self.status.setText(f"●  {tr('offline')}")
        else:
            self.status.setObjectName("Badge")
            self.status.setText(tr("connecting"))
        self.status.setToolTip(self.snap.error)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.note.setVisible(self.s.backend == "demo")
        self.date_lbl.setText(tr.date(self.s.start))
        self.alarm_lbl.setText(tr("alarm_at", gb=f"{self.s.alarm_gb:,.0f}") if self.s.alarm_gb else "")
        self.speed_row.setVisible(bool({Cap.LIVE_SPEED, Cap.LINE_RATE} & caps))

        # ---- gauge + bundle + alarm
        used_gb = self._used_bytes() / GB
        q = self.s.quota_gb
        self.gauge.quota_gb = q
        if q > 0:
            self.gauge.caption = tr("of_quota", q=f"{q:,.0f}")
            left = q - used_gb
            self.gauge.subcaption = tr("remaining", r=f"{left:,.2f}") if left >= 0 else \
                tr("over_quota", r=f"{-left:,.2f}")
        else:
            self.gauge.caption, self.gauge.subcaption = tr("gb_consumed"), ""
        self.gauge.animate_to(used_gb)
        self._check_alarm(used_gb)

        # ---- router
        prof = profiles.active(self.s)
        label = (prof or {}).get("name") or self.snap.router_name or self.s.router_label or ""
        self.router_section.badge.setText(f"{label}  ▾" if len(self.s.routers) > 1 else label)
        self.router_ip.setText(self.s.host if self.s.backend != "demo" else tr("demo_note"))
        if self.snap.line_down_kbps:
            self.line_info.setText(tr("line_info", d=f"{self.snap.line_down_kbps / 1000:,.1f}",
                                      u=f"{(self.snap.line_up_kbps or 0) / 1000:,.1f}"))
            self.line_info.show()
        else:
            self.line_info.hide()
        if self._caps_shown != (caps, self.snap.router_name):
            self._caps_shown = (set(caps), self.snap.router_name)
            while self.caps_holder.count():
                w = self.caps_holder.takeAt(0).widget()
                if w:
                    w.deleteLater()
            self.caps_holder.addWidget(caps_widget(tr, caps, tested_caps(self.s.backend, self.snap.router_name)))

        self._refresh_dns_info()
        self._refresh_devices()

    def _refresh_dns_info(self):
        """Phone-websites checklist in the Router section (rebuilt only when something changed)."""
        tr, mon, st = self.tr, self.dns_mon, self.win_state
        ip = _primary_ipv4() or "?"
        npv = npcap_version()
        rows = []          # (ok, text, kind)
        rows.append((bool(npv), tr("ps_npcap_ok", v=npv) if npv else tr("ps_npcap_no"), "npcap"))
        if sys.platform == "win32":
            if not st:
                rows.append((None, tr("ps_checking"), ""))
            elif st.get("ok"):
                priv = st.get("profile") in ("Private", "DomainAuthenticated")
                rows.append((priv, tr("ps_private_ok") if priv else tr("ps_private_no"), "private"))
                rows.append((st.get("fw"), tr("ps_fw_ok") if st.get("fw") else tr("ps_fw_no"), "fw"))
                dns_ok = st.get("public_dns") and not st.get("self_dns")
                rows.append((dns_ok, tr("ps_dns_ok") if dns_ok else tr("ps_dns_no"), "dns"))
        lan = self.lan_state
        if mon and mon.status.startswith("error:"):
            rows.append((False, tr("ps_helper_off", err=mon.status[6:]), ""))
        tested = tested_caps(self.s.backend, self.snap.router_name)
        mark = lambda key: f"  <span style='font-size:11px'>{tr('cap_tested')}</span>" if key in tested else ""
        if lan.get("active"):
            rows.append((True, tr("ps_router_set", ip=ip) + mark("lan_dns"), "router_undo"))
            if lan.get("v6_active"):
                rows.append((True, tr("ps_v6_ok") + mark("lan_dns_v6"), ""))
            elif st.get("v6") and "v6_type" in lan:
                rows.append((False, tr("ps_v6_no"), "router_v6"))
            if mon and mon.receiving:
                rows.append((True, tr("ps_router_ok", d=len(mon.clients)), ""))
            else:
                rows.append((None, tr("ps_router_wait"), ""))
        elif mon and mon.receiving:
            rows.append((True, tr("ps_router_ok", d=len(mon.clients)), ""))
        elif lan.get("supported"):
            rows.append((False, tr("ps_router_auto"), "router_setup"))
        else:
            rows.append((False, tr("ps_router_no", ip=ip, gw=self.s.host), ""))
        sig = (tuple(rows), bool(self._win_task), self._lan_busy)
        if sig == self._setup_sig:
            return
        self._setup_sig = sig
        while self.setup_box.count():
            it = self.setup_box.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for ok, text, kind in rows:
            self.setup_box.addWidget(self._setup_row(ok, text, kind))
        self._schedule_fit()

    def _setup_row(self, ok, text, kind) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        icon = "check" if ok else ("cancel" if ok is False else "refresh")
        ic = icon_label(icon if icon in GLYPH else "check", 12, color=self.p.success if ok else
                        (self.p.warn if ok is False else self.p.subtext))
        ic.setFixedWidth(16)
        h.addWidget(ic, 0, Qt.AlignTop)
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.RichText)
        lbl.setObjectName("CapOn" if ok else "Muted")
        h.addWidget(lbl, 1)
        busy = bool(self._win_task)
        if kind == "npcap":
            b = QPushButton(self.tr("npcap_download"))
            b.setObjectName("Ghost")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://npcap.com/#download")))
            h.addWidget(b, 0, Qt.AlignVCenter)
        elif kind == "private":
            sw = Switch(self.p, bool(ok))
            sw.setEnabled(not busy)
            sw.toggled.connect(lambda on: self._win_action(
                lambda: winsetup.set_private(self.win_state.get("if", 0), on)))
            h.addWidget(sw, 0, Qt.AlignVCenter)
        elif kind == "router_v6":
            b = QPushButton(self.tr("ps_v6_btn"))
            b.setObjectName("Primary")
            b.setCursor(Qt.PointingHandCursor)
            b.setEnabled(not self._lan_busy)
            b.clicked.connect(self._router_v6)
            h.addWidget(b, 0, Qt.AlignVCenter)
        elif kind in ("router_setup", "router_undo"):
            b = QPushButton(self.tr("ps_router_btn" if kind == "router_setup" else "ps_router_undo"))
            b.setObjectName("Primary" if kind == "router_setup" else "Ghost")
            b.setCursor(Qt.PointingHandCursor)
            b.setEnabled(not self._lan_busy)
            b.clicked.connect(self._router_setup if kind == "router_setup" else self._router_undo)
            h.addWidget(b, 0, Qt.AlignVCenter)
        elif kind in ("fw", "dns") and not ok:
            b = QPushButton(self.tr("ps_fw_btn" if kind == "fw" else "ps_dns_btn"))
            b.setObjectName("Primary")
            b.setCursor(Qt.PointingHandCursor)
            b.setEnabled(not busy)
            b.clicked.connect(lambda: self._win_action(
                winsetup.allow_firewall if kind == "fw" else winsetup.use_public_dns))
            h.addWidget(b, 0, Qt.AlignVCenter)
        return w

    def _pc_ids(self):
        live = {d.mac: d for d in self.snap.devices}
        return _primary_ipv4() or "", self._this_pc_mac(self.store.device_meta(), live) or ""

    def _read_lan_state(self):
        if not self.poller or self._lan_busy or self.s.backend == "demo":
            return
        ip, mac = self._pc_ids()
        v6 = self.win_state.get("v6", "")

        def got(st):
            self.lan_state = st or {}
            self._refresh_dns_info()
            # this PC's IPv6 address changed since setup -> keep the router pointing at it
            if st and st.get("v6_ours") and not st.get("v6_active") and v6 and not self._v6_resynced:
                self._v6_resynced = True
                self.poller.call("lan_dns_v6", lambda r: self._read_lan_state(), lambda e: None, v6)
        self.poller.call("lan_dns_status", got, lambda e: None, ip, mac, v6)

    def _router_v6(self):
        v6 = self.win_state.get("v6", "")
        if self.poller is None or not v6:
            return
        if QMessageBox.question(self, APP_NAME, self.tr("ps_v6_confirm", ip=v6)) != QMessageBox.Yes:
            return
        self._lan_busy = True
        self._refresh_dns_info()

        def done(res):
            self._lan_busy = False
            key = {"ok": "ps_v6_done", "rejected": "ps_v6_rejected"}.get(res, "ps_v6_unsupported")
            self.toast.show_message(self.tr(key), error=res != "ok", ms=12000)
            self._read_lan_state()

        def bad(err):
            self._lan_busy = False
            self.toast.show_message(f"{self.tr('ps_failed')}\n{err}", error=True, ms=15000)
            self._read_lan_state()
        self.poller.call("lan_dns_v6", done, bad, v6)

    def _router_setup(self):
        if self.poller is None:
            return
        ip, mac = self._pc_ids()
        if not mac:
            self.toast.show_message(self.tr("ps_failed"), error=True)
            return
        if QMessageBox.question(self, APP_NAME, self.tr("ps_router_confirm", ip=ip)) != QMessageBox.Yes:
            return
        self._lan_busy = True
        self._refresh_dns_info()

        def ok(_):
            self._lan_busy = False
            self.toast.show_message(self.tr("ps_router_ok_msg"), ms=9000)
            if not self.dns_mon or self.dns_mon.status != "running":
                self._start_dns()
            self._read_lan_state()

        def bad(err):
            self._lan_busy = False
            msg = self.tr("ps_router_unsupported") if "unsupported" in err else f"{self.tr('ps_failed')}\n{err}"
            self.toast.show_message(msg, error=True, ms=15000)
            self._read_lan_state()
        self.poller.call("lan_dns_setup", ok, bad, ip, mac, self.win_state.get("v6", ""))

    def _router_undo(self):
        if self.poller is None:
            return
        self._lan_busy = True
        self._refresh_dns_info()

        def ok(_):
            self._lan_busy = False
            self.toast.show_message(self.tr("ps_router_undone"), ms=8000)
            self._read_lan_state()

        def bad(err):
            self._lan_busy = False
            self.toast.show_message(f"{self.tr('ps_failed')}\n{err}", error=True, ms=15000)
            self._read_lan_state()
        self.poller.call("lan_dns_undo", ok, bad)

    def _read_win_state(self):
        self._read_lan_state()
        if sys.platform != "win32" or self._win_task:
            return
        task = Task(winsetup.read_state, _primary_ipv4())
        self._win_read = task

        def got(st):
            self.win_state = st or {"ok": False}
            if self.dns_mon and self.win_state.get("v6"):
                self.dns_mon.exclude.add(self.win_state["v6"])
            self._refresh_dns_info()
        task.done.connect(got)
        task.start()

    def _win_action(self, fn):
        if self._win_task:
            return
        self.toast.show_message(self.tr("ps_admin"), ms=6000)
        self._win_task = Task(fn)

        def finished(code):
            self._win_task = None
            if code == 0:
                self.toast.show_message(self.tr("ps_done"))
            elif code == -1223:
                self.toast.show_message(self.tr("ps_cancel"))
            else:
                err = winsetup.last_error()
                self.toast.show_message(self.tr("ps_failed") + (f"\n{err}" if err else ""), error=True, ms=15000)
            self._setup_sig = None
            self._read_win_state()
            if code == 0 and self.dns_mon:          # make sure the helper (re)binds after a change
                self._start_dns()
        self._win_task.done.connect(finished)
        self._win_task.failed.connect(lambda e: finished(-1))
        self._win_task.start()
        self._setup_sig = None
        self._refresh_dns_info()

    def _toggle_offline(self):
        self._remember("show_offline", not self.s.show_offline)
        self._refresh_devices()

    # ---------------------------------------------------------------- devices & sites
    def _this_pc_mac(self, meta, live):
        ips = {_primary_ipv4()}
        if self.pc_mon.acct:
            ips |= self.pc_mon.acct.local_ips
        for mac, d in live.items():
            if d.ip in ips:
                return mac
        for mac, (_, ip, _) in meta.items():
            if ip in ips:
                return mac
        return None

    GROUPS = [("pc", "grp_pc"), ("phone", "grp_phone"), ("tv", "grp_tv"),
              ("laptop", "grp_laptop"), ("tablet", "grp_tablet"), ("other", "grp_other")]
    ALWAYS = ("pc", "phone", "tv", "laptop")          # shown even when empty

    def _refresh_devices(self):
        tr, start, caps = self.tr, self.s.start, self.caps
        meta = self.store.device_meta()
        live = {d.mac: d for d in self.snap.devices}
        dev_tot = self.store.totals("dev", start)
        pc_sites = self.store.totals("pcsite", start)
        dns_sites = self.store.totals("devsite", start)
        pc_mac = self._this_pc_mac(meta, live)
        has_dev_usage = Cap.DEVICE_USAGE in caps
        group_ids = [g for g, _ in self.GROUPS]

        macs = {m for m in set(dev_tot) | set(live) | self.blocked_macs if m in meta}
        if pc_mac:
            macs.add(pc_mac)
        query = self.dev_section.search.text().strip().lower()
        if query:
            macs = {m for m in macs if query in self._display_name(m, meta.get(m, ("",))[0]).lower()
                    or query in meta.get(m, ("", "", ""))[1] or query in m}

        def usage(m):
            if m == pc_mac and pc_sites:
                return sum(pc_sites.values())
            return dev_tot.get(m, 0) if has_dev_usage else 0

        def kind(m):
            t = live[m].dtype if m in live else meta.get(m, ("", "", "other"))[2]
            return t if t in group_ids else "other"

        is_on = lambda m: (m in live and live[m].online) or m == pc_mac
        self._group_members = {}
        groups = {g: [] for g in group_ids}
        for m in macs:
            groups[kind(m)].append(m)
        for g in groups.values():
            g.sort(key=lambda m: (m in self.blocked_macs, m != pc_mac, not is_on(m), -usage(m),
                                  self._display_name(m, meta.get(m, ("",))[0]).lower()))

        # ---- top level: one card per device type (+ blocked websites)
        top = [f"grp:{g}" for g in group_ids if groups[g] or (g in self.ALWAYS and not query)]
        if Cap.SITE_BLOCK in caps and self.blocked_domains:
            top.append(BLOCKED_KEY)
        self._sync_cards(top)

        # ---- device cards live inside their type card
        for m in macs:
            if m not in self.dev_cards:
                card = DeviceCard(m, self.p, kind(m) if kind(m) in GLYPH else "other",
                                  open_=m in self.open_devices)
                card.set_active(m == self.active_key)
                card.toggled.connect(self._card_toggled)
                card.site_context.connect(self._site_menu)
                card.site_clicked.connect(self._site_clicked)
                card.action.connect(self._card_action)
                card.context.connect(self._device_menu)
                card.activated.connect(self._rename)
                self.dev_cards[m] = card
        for m in list(self.dev_cards):
            if m not in macs:
                self.dev_cards.pop(m).deleteLater()

        for g, label_key in self.GROUPS:
            key = f"grp:{g}"
            if key not in self.cards:
                continue
            members = groups[g]
            online_n = sum(1 for m in members if is_on(m))
            total = sum(usage(m) for m in members)
            sub = (tr("grp_count1", o=online_n) if len(members) == 1 else
                   tr("grp_count", n=len(members), o=online_n)) if members else tr("grp_none")
            self.cards[key].set_header(tr(label_key), sub, online_n > 0,
                                       tr("online") if online_n else tr("offline_dev"),
                                       fmt_size(total) if total else "", False, "")
            self._group_members[key] = (members, [m for m in members if is_on(m)])
            self.cards[key].set_children([self.dev_cards[m] for m in members],
                                         "" if members else tr("grp_empty"))
            for m in members:
                name, ip, _ = meta.get(m, ("", "", ""))
                card = self.dev_cards[m]
                label = self._display_name(m, name)
                dsub = ip
                if m == pc_mac:
                    label = f"{label}  ·  {tr('this_pc')}"
                elif label == tr("unknown_device"):
                    dsub = f"{ip} · {m.upper()}"
                u = usage(m)
                card.set_header(label, dsub, is_on(m), tr("online") if is_on(m) else tr("offline_dev"),
                                fmt_size(u) if (u and (m == pc_mac or has_dev_usage)) else "",
                                m in self.blocked_macs, tr("blocked"))
                if card.is_open:
                    self._fill_sites(card, m, m == pc_mac, pc_sites, dns_sites)

        if BLOCKED_KEY in self.cards:
            card = self.cards[BLOCKED_KEY]
            card.set_header(tr("blocked_sites"), "", None, "", str(len(self.blocked_domains)), False, "")
            if card.is_open:
                items = [(d, d, None, 0, True, tr("blocked"), f"{i:02d}")
                         for i, d in enumerate(sorted(self.blocked_domains), 1)]
                card.set_sites(items)
        online_total = sum(1 for m in macs if is_on(m))
        self.dev_section.badge.setText(f"{online_total}/{len(macs)}  {tr('devices_word')}")
        self.offline_btn.hide()
        self.dev_section.set_empty("")
        self._schedule_fit()

    # ---- keep the scroll page as tall as its content (nested cards change height at runtime)
    def _schedule_fit(self):
        if not getattr(self, "_fit_pending", False):
            self._fit_pending = True
            QTimer.singleShot(0, self._fit_body)

    def _fit_body(self):
        self._fit_pending = False
        if getattr(self, "scroll_body", None) is None:
            return
        pairs = [(self.scroll, self.scroll_body), (self.scroll2, self.scroll_body2)]
        for _, body in pairs:
            for lay in body.findChildren(QLayout):     # drop every cached size, deepest included
                lay.invalidate()
        for card in list(self.dev_cards.values()) + list(self.cards.values()):   # devices first, then types
            if card.is_open:
                card.fit_inner()
        for sc, body in pairs:
            lay = body.layout()
            lay.activate()
            w = sc.viewport().width()
            h = lay.totalHeightForWidth(w) if lay.hasHeightForWidth() else lay.totalSizeHint().height()
            if h > 0 and h != body.minimumHeight():
                body.setMinimumHeight(h)
            body.updateGeometry()

    def _sync_cards(self, keys):
        for k in list(self.cards):
            if k not in keys:
                self.cards.pop(k).deleteLater()
        for k in keys:
            if k not in self.cards:
                icon = "block" if k == BLOCKED_KEY else (k[4:] if k[4:] in GLYPH else "other")
                card = DeviceCard(k, self.p, icon, open_=k in self.open_devices)
                card.set_active(k == self.active_key)
                card.toggled.connect(self._card_toggled)
                card.site_context.connect(self._site_menu)
                card.site_clicked.connect(self._site_clicked)
                self.cards[k] = card
        if keys == self._card_order:
            return
        self._card_order = list(keys)
        lay = self.dev_section.list
        while lay.count():
            lay.takeAt(0)
        for k in keys:
            lay.addWidget(self.cards[k])

    def _all_cards(self):
        return list(self.cards.items()) + list(self.dev_cards.items())

    def _set_active(self, key):
        self.active_key = key
        for k, c in self._all_cards():
            c.set_active(k == key)

    def _card_toggled(self, key, is_open):
        (self.open_devices.add if is_open else self.open_devices.discard)(key)
        if is_open:
            self._set_active(key)
        elif getattr(self, "active_key", None) == key:
            self._set_active(None)
        if not key.startswith("grp:"):
            (self.user_closed.discard if is_open else self.user_closed.add)(key)
        elif is_open:                       # opening a type opens its connected devices
            _, online = getattr(self, "_group_members", {}).get(key, ([], []))
            for m in online:
                if m not in self.user_closed and m in self.dev_cards:
                    self.dev_cards[m].set_open(True)
                    self.open_devices.add(m)
        self._refresh_devices()

    def _fill_sites(self, card, mac, is_pc, pc_sites, dns_sites):
        tr = self.tr
        empty, action = "", ""
        if is_pc:
            totals, show_bytes = pc_sites, True
            st = self.pc_mon.status
            if not self.s.pc_monitor:
                empty, action = tr("pc_off"), tr("enable")
            elif st == "no_npcap":
                empty, action = tr("npcap_missing"), tr("npcap_get")
            elif st.startswith("error:"):
                empty = tr("pc_error", err=st[6:])
            elif not totals:
                empty = tr("pc_waiting")
        else:
            prefix = mac + "|"
            totals = {k[len(prefix):]: v for k, v in dns_sites.items() if k.startswith(prefix)}
            show_bytes = False
            mon = self.dns_mon
            if not totals:
                empty = tr("no_sites_yet") if (mon and mon.receiving) else tr("sites_need_dns")
        key_of = lambda d: (d in self.blocked_domains, d == UNRESOLVED, -totals[d], d)
        system = sorted((d for d in totals if is_system(d) and d not in self.blocked_domains), key=key_of)
        order = sorted((d for d in totals if d not in system), key=key_of)[:60]
        sys_total = sum(totals[d] for d in system)
        peak = max([totals[d] for d in order] + [sys_total if system else 0, 1])
        items = []
        for i, d in enumerate(order, 1):
            label = tr("unresolved") if d == UNRESOLVED else d
            used = totals[d] if show_bytes else None
            items.append((d, label, used, (used or 0) / peak, d in self.blocked_domains, tr("blocked"), f"{i:02d}"))
        if system:                       # background services: one row at the bottom, opens on click
            open_ = mac in self._sys_open
            arrow = GLYPH["up" if open_ else "down"]
            used = sys_total if show_bytes else None
            items.append((SYSTEM_KEY, f"{tr('sys_group')} · {len(system)}  {arrow}", used,
                          (used or 0) / peak, False, "", "⚙"))
            if open_:
                for d in system[:60]:
                    used = totals[d] if show_bytes else None
                    items.append((d, d, used, (used or 0) / peak, False, "", "  ↳"))
        card.set_sites(items, empty, action)

    def _site_clicked(self, card_key, row_key):
        if row_key != SYSTEM_KEY:
            return
        (self._sys_open.discard if card_key in self._sys_open else self._sys_open.add)(card_key)
        self._refresh_devices()

    def _card_action(self, key):
        mac_is_pc = key == self._this_pc_mac(self.store.device_meta(), {d.mac: d for d in self.snap.devices})
        if mac_is_pc:
            if self.pc_mon.status == "no_npcap":
                self._save_npcap_installer()
                return
            self._remember("pc_monitor", True)
            self.pc_mon.start()
        else:
            self._remember("dns_monitor", True)
            self._start_dns()
        self._refresh_devices()

    def _save_npcap_installer(self):
        from ..monitor.npcap_installer import write_installer
        from .dialogs import desktop_dir
        try:
            path = write_installer(desktop_dir())
        except OSError as e:
            self.toast.show_message(str(e), error=True)
            return
        self.toast.show_message(self.tr("npcap_saved"), ms=12000)
        if sys.platform == "win32":
            import subprocess
            subprocess.Popen(["explorer", "/select,", str(path)])

    def _start_dns(self):
        if self.dns_mon:
            self.dns_mon.stop()
        me = _primary_ipv4()
        self.dns_mon = DnsMonitor(None, exclude={me, "127.0.0.1"})      # public resolvers, UDP + TCP
        self.dns_mon.start()

    def _collect_monitors(self):
        changed = False
        d = self.pc_mon.drain()
        if d:
            self.store.add("pcsite", d)
            changed = True
        if self.dns_mon:
            counts = self.dns_mon.drain()
            for k, n in self._v6_wait.items():          # IPv6 askers whose device wasn't known yet
                counts[k] = counts.get(k, 0) + n
            self._v6_wait = {}
            if counts:
                ip_to_mac = {dev.ip: dev.mac for dev in self.snap.devices}
                unknown6 = False
                for (ip, site), n in counts.items():
                    mac = ip_to_mac.get(ip) or self._v6_macs.get(ip.lower())
                    if mac:
                        self.store.add("devsite", {f"{mac}|{site}": n})
                        changed = True
                    elif ":" in ip and len(self._v6_wait) < 2000:
                        self._v6_wait[(ip, site)] = n
                        unknown6 = True
                if unknown6:
                    self._refresh_v6_macs()
        if changed and self.isVisible():
            self._refresh_devices()
        if self.isVisible() and self.dns_mon and not self.dns_mon.receiving:
            self._refresh_dns_info()

    def _show_dns_log(self):
        from .dialogs import DnsLogDialog
        from ..monitor.domains import site_of
        self._refresh_v6_macs()
        meta = self.store.device_meta()
        by_ip = {d.ip: d.mac for d in self.snap.devices}

        def name_of(ip):
            mac = by_ip.get(ip) or self._v6_macs.get(ip.lower())
            if not mac:
                return ip
            return self._display_name(mac, meta.get(mac, ("",))[0]) or ip

        def shown_as(host):
            site = site_of(host)
            if not site:
                return "—"
            return f"{self.tr('sys_group')} · {site}" if is_system(site) else site
        DnsLogDialog(self.tr, lambda: self.dns_mon.recent() if self.dns_mon else [], name_of, shown_as, self).exec()

    def _refresh_v6_macs(self):
        """Which device is behind an IPv6 address (phones asking over IPv6) – Windows' neighbour table."""
        if getattr(self, "_v6_task", None) is not None or time.monotonic() - self._v6_at < 15:
            return
        self._v6_at = time.monotonic()
        self._v6_task = Task(winsetup.neighbors_v6)

        def got(table):
            self._v6_task = None
            self._v6_macs.update(table or {})
        self._v6_task.done.connect(got)
        self._v6_task.failed.connect(lambda e: setattr(self, "_v6_task", None))
        self._v6_task.start()

    # ================================================================ alarm
    def _alarm_key(self):
        return f"{self.s.start_date}|{self.s.alarm_gb}"

    def _check_alarm(self, used_gb):
        a = self.s.alarm_gb
        if a > 0 and used_gb >= a and self.s.alarm_fired != self._alarm_key():
            self.s.alarm_fired = self._alarm_key()
            self.s.save()
            msg = self.tr("alarm_msg", used=f"{used_gb:,.2f}", gb=f"{a:,.2f}".rstrip("0").rstrip("."))
            self.notify(self.tr("alarm_title"), msg)
            self.toast.show_message(msg, ms=15000)

    def _set_alarm(self):
        val, ok = ask_number(self, APP_NAME, self.tr("alarm_prompt"), self.s.alarm_gb, 2,
                             self.tr("usage_now", used=f"{self._used_bytes() / 1024**3:,.2f}"))
        if ok:
            self.s.alarm_gb = val
            self.s.save()
            self._refresh_view()
            if val and not self.tray:
                self.toast.show_message(self.tr("no_tray"), error=True)

    # ================================================================ actions
    def _menu(self) -> QMenu:
        m = QMenu(self)
        m.setToolTipsVisible(True)
        m.setLayoutDirection(Qt.RightToLeft if self.tr.rtl else Qt.LeftToRight)
        return m

    def _add(self, menu, text, slot, enabled=True):
        act = QAction(text, menu)
        act.triggered.connect(slot)
        act.setEnabled(enabled)
        if not enabled:
            act.setToolTip(self.tr("not_supported"))
            act.setText(f"{text}  —  {self.tr('not_supported')}")
        menu.addAction(act)

    def _device_menu(self, mac, pos):
        meta = self.store.device_meta().get(mac, (mac, "", ""))
        name = self._display_name(mac, meta[0])
        can = Cap.DEVICE_BLOCK in self.caps
        m = self._menu()
        if mac in self.blocked_macs:
            self._add(m, self.tr("restore_internet"), lambda: self._do("unblock_device", mac, name), can)
        else:
            self._add(m, self.tr("cut_internet"), lambda: self._confirm_cut(mac, name), can)
        m.addSeparator()
        self._add(m, self.tr("rename"), lambda: self._rename(mac))
        self._add(m, self.tr("copy_ip"), lambda: QGuiApplication.clipboard().setText(meta[1]))
        self._add(m, self.tr("copy_mac"), lambda: QGuiApplication.clipboard().setText(mac.upper()))
        self._shot_action(m)
        m.exec(pos)

    def _confirm_cut(self, mac, name):
        if QMessageBox.question(self, APP_NAME, self.tr("confirm_cut", name=name)) == QMessageBox.Yes:
            self._do("block_device", mac, name)

    def _rename(self, mac):
        meta = self.store.device_meta().get(mac, (mac, "", ""))
        text, ok = QInputDialog.getText(self, APP_NAME, self.tr("rename_prompt"),
                                        QLineEdit.Normal, self._display_name(mac, meta[0]))
        if ok:
            if text.strip() and text.strip() != meta[0]:
                self.s.nicknames[mac] = text.strip()
            else:
                self.s.nicknames.pop(mac, None)
            self.s.save()
            self._refresh_view()

    def _site_menu(self, domain, pos):
        if domain == SYSTEM_KEY:
            if self.s.screenshot_enabled:
                m = self._menu()
                self._shot_action(m)
                m.exec(pos)
            return
        try:
            clean_domain(domain)
            real = True
        except ValueError:                      # "Other / unresolved", "(CDN)" groups
            real = False
        can = Cap.SITE_BLOCK in self.caps and real
        m = self._menu()
        if domain in self.blocked_domains:
            self._add(m, self.tr("unblock_site"), lambda: self._do("unblock_domain", domain, domain), can)
        else:
            self._add(m, self.tr("block_site"), lambda: self._do("block_domain", domain, domain), can)
        self._shot_action(m)
        m.exec(pos)

    def _add_site(self):
        text, ok = QInputDialog.getText(self, APP_NAME, self.tr("add_site_prompt"))
        if ok and text.strip():
            try:
                d = clean_domain(text)
                self._do("block_domain", d, d)
            except ValueError as e:
                self.toast.show_message(str(e), error=True)

    def _do(self, action: str, target: str, label: str, undo: bool = True):
        if self.poller is None:
            self.toast.show_message(self.tr("other_router_here"), error=True)
            return
        # optimistic update – the next router poll confirms the real state
        {"block_device": self.blocked_macs.add, "unblock_device": self.blocked_macs.discard,
         "block_domain": self.blocked_domains.add, "unblock_domain": self.blocked_domains.discard
         }[action](target)
        self._refresh_view()
        self.poller.request(action, target)
        msg = {"block_device": "cut_done", "unblock_device": "restore_done",
               "block_domain": "site_blocked", "unblock_domain": "site_unblocked"}[action]
        cb = (lambda: self._do(INVERSE[action], target, label, undo=False)) if undo else None
        self.toast.show_message(self.tr(msg, name=label), self.tr("undo") if undo else "", cb)

    def _on_action_done(self, action, target, ok, err):
        if not ok:
            self.toast.show_message(self.tr("action_fail", err=err), error=True, ms=8000)

    # ================================================================ settings
    def _remember(self, key, value):
        setattr(self.s, key, value)
        self.s.save()

    def _pick_date(self):
        dlg = DateDialog(self.tr, self.s.start, self)
        if dlg.exec():
            self.s.start_date = dlg.value().isoformat(timespec="minutes")
            self.s.save()
            self.gauge.setValue(0)
            self._refresh_view()

    def _set_quota(self):
        val, ok = ask_number(self, APP_NAME, self.tr("quota"), self.s.quota_gb, 2)
        if ok:
            self.s.quota_gb = val
            self.s.save()
            self._refresh_view()

    def _suggest_full_control(self):
        """Running in universal mode – check whether a full adapter fits this router."""
        self._detect_task = Task(detect, self.s.host)

        def found(det):
            if det and det.adapter not in ("generic", self.s.backend):
                self.toast.show_message(self.tr("better_adapter", brand=det.display),
                                        self.tr("connect"), self._run_wizard, ms=20000)
        self._detect_task.done.connect(found)
        self._detect_task.start()

    def _run_wizard(self):
        """Connect a router: it is ADDED as a new saved router (the current one is kept)."""
        draft = profiles.begin_new(self.s, "")
        wiz = SetupWizard(self.tr, self.p, draft, self)
        if not wiz.exec():
            return
        new = wiz.result_settings()
        profiles.commit_new(new)
        new.save()
        self._apply_settings(new)

    def _apply_settings(self, new):
        """Start working with another router (or demo): fresh session, its own history."""
        self.s = new
        self.caps, self.blocked_macs, self.blocked_domains = set(), set(), set()
        self.snap = Snapshot(online=False)
        self.lan_state, self._lan_asked, self._caps_shown = {}, False, None
        self.open_devices.clear()
        self._open_store()
        self._build()
        self._start_poller()
        if self.dns_mon:
            self._start_dns()

    # ---------------------------------------------------------------- saved routers
    def switch_router(self, pid, auto=False):
        if pid == self.s.active_router and self.s.backend != "demo":
            return
        profiles.save_active(self.s)
        if not profiles.activate(self.s, pid):
            return
        self.s.save()
        self._apply_settings(self.s)
        p = profiles.find(self.s, pid) or {}
        self.toast.show_message(self.tr("switched_auto" if auto else "switched",
                                        name=p.get("name") or p.get("router_label") or p.get("host", "")))

    def _router_menu(self):
        if not self.s.routers:
            return
        m = QMenu(self)
        for p in self.s.routers:
            name = p.get("name") or p.get("router_label") or p.get("host", "")
            act = m.addAction(("✓  " if p["id"] == self.s.active_router else "     ") + name)
            act.triggered.connect(lambda _=False, pid=p["id"]: self.switch_router(pid))
        m.addSeparator()
        m.addAction(self.tr("add_router")).triggered.connect(self._run_wizard)
        m.addAction(self.tr("manage_routers")).triggered.connect(lambda: self._open_settings(tab=1))
        m.exec(QCursor.pos())

    def _check_network(self):
        """Which router is this network's gateway? Remember it, switch to a known one,
        offer to add an unknown one. Reading/connecting is automatic; changing a router's
        settings never is."""
        if self._net_task is not None or self.s.backend == "demo" and not self.s.routers:
            return
        from ..backends.detect import default_gateway
        from ..backends.generic import arp_table

        def probe():
            gw = default_gateway()
            return gw, arp_table().get(gw, "")
        self._net_task = Task(probe)

        def done(res):
            self._net_task = None
            gw, mac = res or ("", "")
            if not mac:
                return
            prof = profiles.active(self.s)
            if prof and gw == self.s.host and not prof.get("gw_mac") and self.snap.online:
                profiles.set_identity(self.s, prof["id"], mac, self.snap.router_name)
                self.s.save()
                return
            if prof and prof.get("gw_mac", "").lower() == mac.lower():
                if self.poller is None:                 # back on this router's network
                    self._start_poller()
                return
            other = profiles.by_mac(self.s, mac)
            if prof and prof.get("gw_mac") and gw == self.s.host and not other and self.poller is not None:
                # same address, different router: don't try the saved password on it
                self.poller.shutdown()
                self.poller = None
                self.snap = Snapshot(online=False, error=self.tr("other_router_here"))
                self._refresh_view()
            if other and other["id"] != self.s.active_router:
                self.switch_router(other["id"], auto=True)
            elif not other and mac not in self._unknown_macs and (prof is None or prof.get("gw_mac")):
                self._unknown_macs.add(mac)
                self.toast.show_message(self.tr("new_router_here", ip=gw), self.tr("add_router"),
                                        self._run_wizard, ms=20000)
        self._net_task.done.connect(done)
        self._net_task.failed.connect(lambda e: setattr(self, "_net_task", None))
        self._net_task.start()

    def remove_router(self, pid):
        p = profiles.find(self.s, pid)
        if not p:
            return
        name = p.get("name") or p.get("router_label") or p.get("host", "")
        pending = profiles.pending_changes(p)
        is_active = pid == self.s.active_router
        if pending and is_active and self.snap.online:
            ans = QMessageBox.question(self, APP_NAME, self.tr("remove_pending_active", name=name),
                                       QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if ans == QMessageBox.Cancel:
                return
            if ans == QMessageBox.Yes:
                def undone(text):
                    self._finish_remove(pid)
                self.poller.call("undo_all", undone,
                                 lambda e: self.toast.show_message(f"{self.tr('ps_failed')}\n{e}", error=True, ms=15000))
                return
        elif pending:
            if QMessageBox.warning(self, APP_NAME, self.tr("remove_pending_other", name=name),
                                   QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
                return
        elif QMessageBox.question(self, APP_NAME, self.tr("remove_confirm", name=name)) != QMessageBox.Yes:
            return
        self._finish_remove(pid)

    def _finish_remove(self, pid):
        if pid == self.s.active_router:
            others = [p for p in self.s.routers if p["id"] != pid]
            if others:
                profiles.activate(self.s, others[0]["id"])
            else:                                   # last router: back to demo until a new one is added
                self.s.active_router = ""
                self.s.backend = "demo"
            self._apply_settings(self.s)            # closes the removed router's history file
        profiles.remove(self.s, pid)
        self.s.save()
        self.toast.show_message(self.tr("router_removed"))
        if not self.s.routers:
            QTimer.singleShot(300, self._run_wizard)

    def _open_settings(self, tab=0):
        profiles.save_active(self.s)
        dlg = SettingsDialog(self.tr, self.s, self, poller=self.poller, tab=tab,
                             online=self.snap.online, router_name=self.snap.router_name)
        if not dlg.exec():
            return
        new = dlg.result_settings()
        restart = new.refresh_sec != self.s.refresh_sec
        new.save()
        self.s = new
        set_autostart(self.s.autostart)
        self._apply_look()
        self._build()
        if self.tray:
            self._tray_open.setText(self.tr("tray_open"))
            self._tray_quit.setText(self.tr("tray_quit"))
        if dlg.change_router:
            self._run_wizard()
        elif dlg.switch_to:
            self.switch_router(dlg.switch_to)
        elif dlg.remove_id:
            self.remove_router(dlg.remove_id)
        elif restart:
            self._start_poller()

    # ================================================================ close = keep running
    def closeEvent(self, e):
        if getattr(self, "_win_size", None) and (self.s.win_w, self.s.win_h) != self._win_size:
            self.s.win_w, self.s.win_h = self._win_size
            self.s.save()
        if self.tray and not self._quitting:
            e.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.notify(APP_NAME, self.tr("tray_hint"))
            return
        if self.poller:
            self.poller.shutdown()
        self.pc_mon.stop()
        if self.dns_mon:
            self.dns_mon.stop()
        if self.tray:
            self.tray.hide()
        super().closeEvent(e)
        QApplication.instance().quit()          # quit also when there is no tray icon
