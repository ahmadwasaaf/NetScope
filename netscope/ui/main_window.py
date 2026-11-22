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

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication, QIcon
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QInputDialog, QLabel, QSizePolicy,
                               QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton,
                               QScrollArea, QSystemTrayIcon, QVBoxLayout, QWidget)

from .. import APP_NAME, APP_VERSION
from ..backends import Cap, Snapshot, clean_domain, create_backend, detect
from ..i18n import Translator
from ..monitor.dnsmon import DnsMonitor
from ..monitor.pcap import UNRESOLVED, PcMonitor, _primary_ipv4
from ..poller import Poller, Task
from ..settings import Settings, data_dir
from ..store import UsageStore
from ..theme import make_palette, stylesheet
from .dialogs import DateDialog, SettingsDialog
from .widgets import (GLYPH, CollapsibleSection, DeviceCard, Gauge, SpeedTile, Toast, fmt_size,
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
        self.poller = None
        self.snap = Snapshot(online=False)
        self.connected_once = False
        self.caps = set()
        self.blocked_macs: set = set()
        self.blocked_domains: set = set()
        self.open_devices: set = set()
        self.cards = {}
        self._quitting = False
        self._tray_hint_shown = False
        self.resize(600, 940)
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
        if not start_hidden:
            self.show()
        if not self.s.onboarded:
            QTimer.singleShot(250, self._run_wizard)
        elif self.s.backend == "generic":
            QTimer.singleShot(1500, self._suggest_full_control)

    # ================================================================ setup
    def _open_store(self):
        tag = "demo" if self.s.backend == "demo" else re.sub(r"[^\w.-]", "_", f"{self.s.backend}_{self.s.host}")
        self.store = UsageStore(data_dir() / f"usage_{tag}.db")

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

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)   # content follows the window width
        outer.addWidget(scroll, 1)
        body = QWidget()
        body.setObjectName("ScrollBody")
        scroll.setWidget(body)
        col = QVBoxLayout(body)
        col.setContentsMargins(0, 0, 0, 16)
        col.setSpacing(12)
        col.addWidget(self._gauge_card())

        # ---- router (expandable)
        self.router_section = CollapsibleSection(self.tr("router"), "wifi", self.p, open_=self.s.router_open)
        self.router_section.toggled.connect(lambda o: self._remember("router_open", o))
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
        self.dns_info = QLabel()
        self.dns_info.setObjectName("Muted")
        self.dns_info.setWordWrap(True)
        self.dns_info.setTextFormat(Qt.RichText)
        self.dns_info.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.dns_info.setMinimumWidth(10)
        rv.addSpacing(6)
        rv.addWidget(self.dns_info)
        self._caps_shown = None
        self.router_section.list.addWidget(self.router_box)
        col.addWidget(self.router_section)

        # ---- devices & websites (expandable)
        self.dev_section = CollapsibleSection(self.tr("devices_sites"), "network", self.p,
                                              open_=self.s.devices_open,
                                              search_placeholder=self.tr("search_devices"))
        self.dev_section.badge.setObjectName("CountBadge")
        self.dev_section.toggled.connect(lambda o: self._remember("devices_open", o))
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

        self.cards = {}
        self.toast = Toast(root)
        self._refresh_view()

    def resizeEvent(self, e):
        super().resizeEvent(e)
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
        v.setContentsMargins(18, 14, 18, 16)
        v.setSpacing(12)
        top = QHBoxLayout()
        cap = QLabel(self.tr("total_usage"))
        cap.setObjectName("CardCaption")
        top.addWidget(cap)
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
        self.router_section.badge.setText(self.snap.router_name or self.s.router_label or "")
        self.router_ip.setText(self.s.host if self.s.backend != "demo" else tr("demo_note"))
        if self.snap.line_down_kbps:
            self.line_info.setText(tr("line_info", d=f"{self.snap.line_down_kbps / 1000:,.1f}",
                                      u=f"{(self.snap.line_up_kbps or 0) / 1000:,.1f}"))
            self.line_info.show()
        else:
            self.line_info.hide()
        if self._caps_shown != caps:
            self._caps_shown = set(caps)
            while self.caps_holder.count():
                w = self.caps_holder.takeAt(0).widget()
                if w:
                    w.deleteLater()
            self.caps_holder.addWidget(caps_widget(tr, caps))

        self._refresh_dns_info()
        self._refresh_devices()

    def _refresh_dns_info(self):
        tr, mon = self.tr, self.dns_mon
        if mon is None:
            text = ""
        elif mon.status.startswith("error:"):
            text = tr("dns_error", err=mon.status[6:])
        elif mon.receiving:
            text = tr("dns_ok", d=len(mon.clients))
        else:
            text = "<b>" + tr("dns_title") + "</b><br>" + \
                tr("dns_setup", ip=_primary_ipv4() or "?", gw=self.s.host)
        self.dns_info.setText(text)
        self.dns_info.setVisible(bool(text))

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

    def _refresh_devices(self):
        tr, start, caps = self.tr, self.s.start, self.caps
        meta = self.store.device_meta()
        live = {d.mac: d for d in self.snap.devices}
        dev_tot = self.store.totals("dev", start)
        pc_sites = self.store.totals("pcsite", start)
        dns_sites = self.store.totals("devsite", start)
        pc_mac = self._this_pc_mac(meta, live)
        has_dev_usage = Cap.DEVICE_USAGE in caps

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

        is_on = lambda m: (m in live and live[m].online) or m == pc_mac
        offline = [m for m in macs if not is_on(m) and m not in self.blocked_macs]
        if not self.s.show_offline:
            macs = macs - set(offline)
        self.offline_btn.setText(tr("hide_offline") if self.s.show_offline
                                 else tr("show_offline", n=len(offline)))
        self.offline_btn.setVisible(bool(offline))
        order = sorted(macs, key=lambda m: (m in self.blocked_macs, m != pc_mac, not is_on(m), -usage(m),
                                            self._display_name(m, meta.get(m, ("",))[0]).lower()))
        keys = list(order)
        if Cap.SITE_BLOCK in caps and self.blocked_domains:
            keys.append(BLOCKED_KEY)
        self._sync_cards(keys, meta)

        for m in order:
            name, ip, _ = meta.get(m, ("", "", ""))
            card = self.cards[m]
            label = self._display_name(m, name)
            sub = ip
            if m == pc_mac:
                label = f"{label}  ·  {tr('this_pc')}"
            elif label == tr("unknown_device"):
                sub = f"{ip} · {m.upper()}"
            u = usage(m)
            usage_text = fmt_size(u) if (u and (m == pc_mac or has_dev_usage)) else ""
            card.set_header(label, sub, is_on(m), tr("online") if is_on(m) else tr("offline_dev"),
                            usage_text, m in self.blocked_macs, tr("blocked"))
            if card.is_open:
                self._fill_sites(card, m, m == pc_mac, pc_sites, dns_sites)

        if BLOCKED_KEY in self.cards:
            card = self.cards[BLOCKED_KEY]
            card.set_header(tr("blocked_sites"), "", None, "", str(len(self.blocked_domains)), False, "")
            if card.is_open:
                items = [(d, d, None, 0, True, tr("blocked"), f"{i:02d}")
                         for i, d in enumerate(sorted(self.blocked_domains), 1)]
                card.set_sites(items)
        self.dev_section.badge.setText(f"{len(order) + (0 if self.s.show_offline else len(offline))}  {tr('devices_word')}")
        self.dev_section.set_empty("" if order or not self.connected_once else tr("no_data"))

    def _sync_cards(self, keys, meta):
        for k in list(self.cards):
            if k not in keys:
                self.cards.pop(k).deleteLater()
        for k in keys:
            if k not in self.cards:
                if k == BLOCKED_KEY:
                    icon = "block"
                else:
                    t = meta.get(k, ("", "", "other"))[2]
                    icon = t if t in GLYPH else "other"
                card = DeviceCard(k, self.p, icon, open_=k in self.open_devices)
                card.toggled.connect(self._card_toggled)
                card.site_context.connect(self._site_menu)
                card.action.connect(self._card_action)
                if k != BLOCKED_KEY:
                    card.context.connect(self._device_menu)
                    card.activated.connect(self._rename)
                self.cards[k] = card
        if keys == self._card_order:
            return
        self._card_order = list(keys)
        lay = self.dev_section.list
        while lay.count():
            lay.takeAt(0)
        for k in keys:
            lay.addWidget(self.cards[k])

    def _card_toggled(self, key, is_open):
        (self.open_devices.add if is_open else self.open_devices.discard)(key)
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
        order = sorted(totals, key=lambda d: (d in self.blocked_domains, d == UNRESOLVED, -totals[d], d))[:60]
        peak = max([totals[d] for d in order] + [1])
        items = []
        for i, d in enumerate(order, 1):
            label = tr("unresolved") if d == UNRESOLVED else d
            used = totals[d] if show_bytes else None
            items.append((d, label, used, (used or 0) / peak, d in self.blocked_domains, tr("blocked"), f"{i:02d}"))
        card.set_sites(items, empty, action)

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
        upstream = self.s.host if self.s.backend != "demo" else ".".join(me.split(".")[:3] + ["1"])
        self.dns_mon = DnsMonitor(upstream, exclude={me, "127.0.0.1"})
        self.dns_mon.start()

    def _collect_monitors(self):
        changed = False
        d = self.pc_mon.drain()
        if d:
            self.store.add("pcsite", d)
            changed = True
        if self.dns_mon:
            counts = self.dns_mon.drain()
            if counts:
                ip_to_mac = {dev.ip: dev.mac for dev in self.snap.devices}
                for (ip, site), n in counts.items():
                    mac = ip_to_mac.get(ip)
                    if mac:
                        self.store.add("devsite", {f"{mac}|{site}": n})
                changed = True
        if changed and self.isVisible():
            self._refresh_devices()
        if self.isVisible() and self.dns_mon and not self.dns_mon.receiving:
            self._refresh_dns_info()

    # ================================================================ alarm
    def _alarm_key(self):
        return f"{self.s.start_date}|{self.s.alarm_gb}"

    def _check_alarm(self, used_gb):
        a = self.s.alarm_gb
        if a > 0 and used_gb >= a and self.s.alarm_fired != self._alarm_key():
            self.s.alarm_fired = self._alarm_key()
            self.s.save()
            msg = self.tr("alarm_msg", used=f"{used_gb:,.2f}", gb=f"{a:,.0f}")
            self.notify(self.tr("alarm_title"), msg)
            self.toast.show_message(msg, ms=15000)

    def _set_alarm(self):
        val, ok = QInputDialog.getDouble(self, APP_NAME, self.tr("alarm_prompt"), self.s.alarm_gb, 0, 100000, 1)
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
        val, ok = QInputDialog.getDouble(self, APP_NAME, self.tr("quota"), self.s.quota_gb, 0, 100000, 0)
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
        wiz = SetupWizard(self.tr, self.p, self.s, self)
        if not wiz.exec():
            return
        new = wiz.result_settings()
        new.save()
        self.s = new
        self.caps, self.blocked_macs, self.blocked_domains = set(), set(), set()
        self.snap = Snapshot(online=False)
        self._open_store()
        self._build()
        self._start_poller()
        if self.dns_mon:
            self._start_dns()

    def _open_settings(self):
        dlg = SettingsDialog(self.tr, self.s, self, poller=self.poller)
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
        elif restart:
            self._start_poller()

    # ================================================================ close = keep running
    def closeEvent(self, e):
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
