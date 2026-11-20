"""NetScope main window – responsive: two columns on wide windows, one column on narrow."""
import ctypes
import re
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout,
                               QInputDialog, QLabel, QMainWindow, QMenu, QMessageBox,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .. import APP_NAME, APP_VERSION
from ..backends import Cap, Snapshot, clean_domain, create_backend
from ..i18n import Translator
from ..backends import detect
from ..poller import Poller, Task
from ..settings import Settings, data_dir
from ..store import UsageStore
from ..theme import make_palette, stylesheet
from .dialogs import DateDialog, SettingsDialog
from ..monitor.dnsmon import DnsMonitor
from ..monitor.pcap import UNRESOLVED, PcMonitor, _primary_ipv4
from .widgets import (GLYPH, CollapsibleSection, Gauge, Segmented, SpeedTile, Toast, UsageRow,
                      icon_button, icon_label)
from .wizard import SetupWizard, caps_widget

WIDE = 1000
INVERSE = {"block_device": "unblock_device", "unblock_device": "block_device",
           "block_domain": "unblock_domain", "unblock_domain": "block_domain"}


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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.s = Settings.load()
        self.poller = None
        self.snap = Snapshot(online=False)
        self.connected_once = False
        self.caps = set()
        self.blocked_macs: set = set()
        self.blocked_domains: set = set()
        self.wide = None
        self.resize(1120, 780)
        self.setMinimumWidth(480)
        self._open_store()
        self._apply_look()
        self.pc_mon = PcMonitor()               # monitors live for the whole app run
        if self.s.pc_monitor:
            self.pc_mon.start()
        self.dns_mon = None
        if self.s.dns_monitor:
            self._start_dns()
        self._build()
        self._start_poller()
        self.mon_timer = QTimer(self)
        self.mon_timer.timeout.connect(self._collect_monitors)
        self.mon_timer.start(2000)
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

    # ================================================================ layout
    def _build(self):
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(16, 14, 16, 0)
        outer.setSpacing(12)
        outer.addWidget(self._header())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll, 1)
        body = QWidget()
        body.setObjectName("ScrollBody")
        scroll.setWidget(body)
        self.grid = QGridLayout(body)
        self.grid.setContentsMargins(0, 0, 0, 16)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(12)

        self.left = QWidget()
        lv = QVBoxLayout(self.left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(12)
        lv.addWidget(self._gauge_card())
        lv.addWidget(self._router_card())
        lv.addStretch(1)

        self.right = QWidget()
        rv = QVBoxLayout(self.right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(12)
        self.dev_section = CollapsibleSection(self.tr("connected_devices"), "network", self.p,
                                              open_=self.s.devices_open,
                                              search_placeholder=self.tr("search_devices"))
        self.dev_section.toggled.connect(lambda o: self._remember("devices_open", o))
        self.dev_section.search.textChanged.connect(lambda _: self._refresh_view())
        rv.addWidget(self.dev_section)
        self.site_section = CollapsibleSection(self.tr("website_usage"), "globe", self.p,
                                               badge=self.tr("highest_first"), open_=self.s.sites_open)
        self.add_site_btn = icon_button("add", 13)
        self.add_site_btn.setToolTip(self.tr("add_site"))
        self.add_site_btn.clicked.connect(self._add_site)
        self.site_section.extra.addWidget(self.add_site_btn)
        self.site_section.toggled.connect(lambda o: self._remember("sites_open", o))
        self.sites_seg = Segmented([("pc", self.tr("this_pc")), ("others", self.tr("other_devices"))],
                                   self.s.sites_tab)
        self.sites_seg.changed.connect(self._switch_sites_tab)
        self.site_section.add_top(self.sites_seg)
        self.site_section.action_btn.clicked.connect(self._sites_action)
        rv.addWidget(self.site_section)
        rv.addStretch(1)

        self.toast = Toast(root)
        self.wide = None
        self._relayout()
        self._refresh_view()

    def _relayout(self):
        wide = self.width() >= WIDE
        if wide == self.wide:
            return
        self.wide = wide
        self.grid.removeWidget(self.left)
        self.grid.removeWidget(self.right)
        if wide:
            self.grid.addWidget(self.left, 0, 0)
            self.grid.addWidget(self.right, 0, 1)
            self.grid.setColumnStretch(0, 5)
            self.grid.setColumnStretch(1, 6)
        else:
            self.grid.addWidget(self.left, 0, 0)
            self.grid.addWidget(self.right, 1, 0)
            self.grid.setColumnStretch(0, 1)
            self.grid.setColumnStretch(1, 0)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "grid"):
            self._relayout()
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
        v.setSpacing(10)
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
        v.addWidget(self.gauge, 1)

        self.speed_row = QWidget()
        sr = QHBoxLayout(self.speed_row)
        sr.setContentsMargins(0, 0, 0, 0)
        sr.setSpacing(10)
        self.down_tile = SpeedTile(self.p, self.tr("download"), "arrow_down")
        self.up_tile = SpeedTile(self.p, self.tr("upload"), "arrow_up", "#00B8D4")
        sr.addWidget(self.down_tile)
        sr.addWidget(self.up_tile)
        v.addWidget(self.speed_row)

        div = QFrame()
        div.setObjectName("Divider")
        v.addWidget(div)
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        for text, glyph, slot in ((self.tr("set_date"), "calendar", self._pick_date),
                                  (self.tr("set_quota"), "pie", self._set_quota)):
            b = QPushButton(f"{text}  {GLYPH[glyph]}")
            b.setObjectName("Ghost")
            b.setStyleSheet("font-family: 'Segoe UI', 'Segoe Fluent Icons', 'Segoe MDL2 Assets';")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(slot)
            bottom.addWidget(b)
        bottom.addStretch(1)
        dcol = QVBoxLayout()
        dcol.setSpacing(0)
        since = QLabel(self.tr("tracking_since"))
        since.setObjectName("Muted")
        self.date_lbl = QLabel()
        self.date_lbl.setObjectName("DateValue")
        dcol.addWidget(since, 0, Qt.AlignTrailing)
        dcol.addWidget(self.date_lbl, 0, Qt.AlignTrailing)
        bottom.addLayout(dcol)
        v.addLayout(bottom)
        return card

    def _router_card(self) -> QWidget:
        card = QFrame()
        card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 14, 18, 16)
        top = QHBoxLayout()
        ic = icon_label("wifi", 16, "IconTile")
        ic.setFixedSize(34, 34)
        top.addWidget(ic)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.router_name = QLabel()
        self.router_name.setObjectName("SectionTitle")
        self.router_ip = QLabel()
        self.router_ip.setObjectName("Muted")
        col.addWidget(self.router_name)
        col.addWidget(self.router_ip)
        top.addLayout(col, 1)
        v.addLayout(top)
        lbl = QLabel(self.tr("what_works"))
        lbl.setObjectName("Muted")
        v.addSpacing(6)
        v.addWidget(lbl)
        self.caps_holder = QVBoxLayout()
        v.addLayout(self.caps_holder)
        self._caps_shown = None
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
            if Cap.DEVICE_BLOCK in self.caps:
                self.blocked_macs = set(snap.blocked_macs)
            if Cap.SITE_BLOCK in self.caps:
                self.blocked_domains = set(snap.blocked_domains)
            if Cap.LIVE_SPEED in self.caps:
                self.down_tile.set_speed(snap.down_bps)
                self.up_tile.set_speed(snap.up_bps)
        self._refresh_view()

    def _display_name(self, mac, router_name):
        name = self.s.nicknames.get(mac) or router_name
        if not name or name.lower() == mac.lower():
            return self.tr("unknown_device")
        return name

    def _refresh_view(self):
        tr, start, caps = self.tr, self.s.start, self.caps
        online = self.snap.online
        # ---- status chip
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
        self.date_lbl.setText(tr.date(start))
        self.speed_row.setVisible(Cap.LIVE_SPEED in caps)

        # ---- router card
        self.router_name.setText(self.snap.router_name or self.s.router_label or tr("router"))
        self.router_ip.setText(self.s.host if self.s.backend != "demo" else tr("demo_note"))
        if self._caps_shown != caps:
            self._caps_shown = set(caps)
            while self.caps_holder.count():
                w = self.caps_holder.takeAt(0).widget()
                if w:
                    w.deleteLater()
            self.caps_holder.addWidget(caps_widget(tr, caps))

        # ---- devices
        meta = self.store.device_meta()
        dev_tot = self.store.totals("dev", start)
        live = {d.mac: d for d in self.snap.devices}
        macs = {m for m in set(dev_tot) | set(live) | self.blocked_macs if m in meta}
        has_usage = Cap.DEVICE_USAGE in caps

        wan_total = self.store.totals("wan", start).get("wan", 0)
        dev_sum = sum(dev_tot.get(m, 0) for m in macs)
        total = wan_total if (Cap.TOTAL_USAGE in caps and wan_total) else dev_sum
        q = self.s.quota_gb
        self.gauge.quota_gb = q
        used_gb = total / 1024 ** 3
        if q > 0:
            self.gauge.caption = tr("of_quota", q=f"{q:,.0f}")
            left = q - used_gb
            self.gauge.subcaption = tr("remaining", r=f"{left:,.2f}") if left >= 0 else \
                tr("over_quota", r=f"{-left:,.2f}")
        else:
            self.gauge.caption, self.gauge.subcaption = tr("gb_consumed"), ""
        self.gauge.animate_to(used_gb)

        query = self.dev_section.search.text().strip().lower()
        if query:
            macs = {m for m in macs if query in self._display_name(m, meta[m][0]).lower()
                    or query in meta[m][1] or query in m}
        is_on = lambda m: m in live and live[m].online
        order = sorted(macs, key=lambda m: (m in self.blocked_macs, -dev_tot.get(m, 0), not is_on(m),
                                            self._display_name(m, meta[m][0]).lower()))
        peak = max([dev_tot.get(m, 0) for m in macs] + [1])

        def make_dev(mac):
            dtype = meta[mac][2] if meta[mac][2] in GLYPH else "other"
            row = UsageRow(mac, self.p, icon=dtype)
            row.context.connect(self._device_menu)
            row.activated.connect(self._rename)
            return row

        self.dev_section.set_rows(order, make_dev)
        for mac in order:
            name, ip, _ = meta[mac]
            used = dev_tot.get(mac, 0) if has_usage else None
            sub = ip if is_on(mac) else f"{ip} · {tr('offline_dev')}"
            if self._display_name(mac, name) == tr("unknown_device"):
                sub = f"{sub} · {mac.upper()}"
            self.dev_section.rows[mac].set_data(self._display_name(mac, name), sub, used,
                                                (used or 0) / peak, mac in self.blocked_macs,
                                                tr("blocked"), online=is_on(mac))
        self.dev_section.badge.setText(f"{tr('devices')} {len(order)}")
        self.dev_section.set_empty("" if order or not self.connected_once else tr("no_data"))

        # ---- websites (This PC: real GB  |  Other devices: ranked names)
        self._refresh_sites()

    def _refresh_sites(self):
        tr, start, caps = self.tr, self.s.start, self.caps
        tab = self.s.sites_tab
        self.add_site_btn.setVisible(Cap.SITE_BLOCK in caps)
        empty, action = "", ""
        if tab == "pc":
            totals = self.store.totals("pcsite", start)
            show_bytes = True
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
            if Cap.SITE_USAGE in caps:                   # router reports bytes itself (OpenWrt)
                totals, show_bytes = self.store.totals("site", start), True
            else:
                totals, show_bytes = self.store.totals("dnssite", start), False
                mon = self.dns_mon
                if mon is None:
                    empty, action = tr("dns_intro"), tr("dns_enable")
                elif mon.status.startswith("error:"):
                    empty, action = tr("dns_error", err=mon.status[6:]), tr("retry")
                elif not totals and not mon.receiving:
                    empty = tr("dns_setup", ip=_primary_ipv4() or "?", gw=self.s.host) + "<br><br>" + \
                        tr("dns_status", n=mon.total_queries, d=len(mon.clients))
        sites = set(totals) | (self.blocked_domains if Cap.SITE_BLOCK in caps else set())
        order = sorted(sites, key=lambda d: (d in self.blocked_domains, d == UNRESOLVED, -totals.get(d, 0), d))
        if empty and not totals:
            order = sorted(self.blocked_domains) if Cap.SITE_BLOCK in caps else []
        peak = max([totals.get(d, 0) for d in sites] + [1])

        def make_site(domain):
            row = UsageRow(domain, self.p)
            row.context.connect(self._site_menu)
            return row

        self.site_section.set_rows(order, make_site)
        for i, d in enumerate(order, 1):
            used = totals.get(d, 0) if show_bytes else None
            label = tr("unresolved") if d == UNRESOLVED else d
            self.site_section.rows[d].set_data(label, "", used, (used or 0) / peak, d in self.blocked_domains,
                                               tr("blocked"), index=f"{i:02d}")
        self.site_section.badge.setText(tr("highest_first") if show_bytes else tr("most_active_first"))
        self.site_section.set_empty(empty, action)

    def _switch_sites_tab(self, tab):
        self._remember("sites_tab", tab)
        self._refresh_sites()

    def _sites_action(self):
        if self.s.sites_tab == "pc" and self.pc_mon.status == "no_npcap":
            self._save_npcap_installer()
            return
        if self.s.sites_tab == "pc":
            self._remember("pc_monitor", True)
            self.pc_mon.start()
        else:
            self._remember("dns_monitor", True)
            self._start_dns()
        self._refresh_sites()

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
            d = self.dns_mon.drain()
            if d:
                self.store.add("dnssite", d)
                changed = True
        if changed or self.site_section.empty.isVisible():
            self._refresh_sites()

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
                                        text=self._display_name(mac, meta[0]))
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
            self.s.start_date = dlg.value().isoformat()
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
        self._apply_look()
        self._build()
        if dlg.change_router:
            self._run_wizard()
        elif restart:
            self._start_poller()

    def closeEvent(self, e):
        if self.poller:
            self.poller.shutdown()
        self.pc_mon.stop()
        if self.dns_mon:
            self.dns_mon.stop()
        super().closeEvent(e)
