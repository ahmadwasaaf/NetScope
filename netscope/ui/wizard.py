"""First-run / change-router wizard: find router -> sign in -> show what works."""
from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QSpinBox, QStackedWidget,
                               QVBoxLayout, QWidget)

from ..backends import ADAPTERS, Cap, create_backend, detect
from ..i18n import Translator
from ..poller import Task
from ..settings import Settings
from ..theme import Palette
from .widgets import Spinner, icon_label

CAP_ORDER = [Cap.TOTAL_USAGE, Cap.LIVE_SPEED, Cap.DEVICE_LIST, Cap.DEVICE_USAGE,
             Cap.DEVICE_BLOCK, Cap.SITE_USAGE, Cap.SITE_BLOCK]
DEFAULT_USER = {"tplink": "admin", "huawei": "admin", "openwrt": "root", "generic": "", "demo": ""}


def caps_widget(tr: Translator, caps, tested=None) -> QWidget:
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(6)
    for c in CAP_ORDER:
        row = QHBoxLayout()
        on = c in caps
        ic = icon_label("check" if on else "cancel", 12, "CapOn" if on else "CapOff")
        row.addWidget(ic)
        lbl = QLabel(tr(f"cap_{c}"))
        lbl.setObjectName("CapOn" if on else "CapOff")
        row.addWidget(lbl)
        if on and tested is not None:
            t = QLabel(tr("cap_tested") if c in tested else tr("cap_untested"))
            t.setObjectName("Muted")
            t.setStyleSheet("font-size: 11px;")
            row.addWidget(t)
        row.addStretch(1)
        v.addLayout(row)
    return w


class SetupWizard(QDialog):
    def __init__(self, tr: Translator, palette: Palette, settings: Settings, parent=None):
        super().__init__(parent)
        self.tr, self.p = tr, palette
        self.s = replace(settings, nicknames=dict(settings.nicknames))
        self.task = None
        self.setWindowTitle("NetScope")
        self.setMinimumSize(460, 520)
        v = QVBoxLayout(self)
        v.setContentsMargins(28, 28, 28, 22)
        self.stack = QStackedWidget()
        v.addWidget(self.stack)
        self.pg_welcome = self._welcome()
        self.pg_search = self._searching()
        self.pg_login = self._login()
        self.pg_done = self._done()
        for pg in (self.pg_welcome, self.pg_search, self.pg_login, self.pg_done):
            self.stack.addWidget(pg)

    # ---------------------------------------------------------------- pages
    def _hero(self, glyph: str) -> QLabel:
        ic = icon_label(glyph, 30, "AppIcon")
        ic.setFixedSize(64, 64)
        return ic

    def _welcome(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addStretch(1)
        v.addWidget(self._hero("chart"), 0, Qt.AlignHCenter)
        t = QLabel(self.tr("welcome"))
        t.setObjectName("Hero")
        t.setAlignment(Qt.AlignCenter)
        v.addWidget(t)
        lead = QLabel(self.tr("welcome_text"))
        lead.setObjectName("Lead")
        lead.setWordWrap(True)
        lead.setAlignment(Qt.AlignCenter)
        v.addWidget(lead)
        v.addStretch(1)
        go = QPushButton(self.tr("find_router"))
        go.setObjectName("Primary")
        go.setMinimumHeight(40)
        go.clicked.connect(self.start_detect)
        v.addWidget(go)
        demo = QPushButton(self.tr("try_demo"))
        demo.clicked.connect(self.use_demo)
        v.addWidget(demo)
        return w

    def _searching(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addStretch(1)
        v.addWidget(Spinner(self.p, 54), 0, Qt.AlignHCenter)
        self.search_lbl = QLabel(self.tr("searching"))
        self.search_lbl.setObjectName("Lead")
        self.search_lbl.setAlignment(Qt.AlignCenter)
        v.addWidget(self.search_lbl)
        v.addStretch(1)
        return w

    def _login(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(self._hero("wifi"), 0, Qt.AlignHCenter)
        self.found_lbl = QLabel()
        self.found_lbl.setObjectName("Hero")
        self.found_lbl.setStyleSheet("font-size: 18px;")
        self.found_lbl.setAlignment(Qt.AlignCenter)
        self.found_lbl.setWordWrap(True)
        v.addWidget(self.found_lbl)
        self.hint_lbl = QLabel()
        self.hint_lbl.setObjectName("Lead")
        self.hint_lbl.setWordWrap(True)
        self.hint_lbl.setAlignment(Qt.AlignCenter)
        v.addWidget(self.hint_lbl)
        form = QFormLayout()
        form.setVerticalSpacing(10)
        self.adapter = QComboBox()
        for k, name in ADAPTERS.items():
            if k != "demo":
                self.adapter.addItem(name, k)
        self.adapter.currentIndexChanged.connect(self._adapter_changed)
        form.addRow(self.tr("type"), self.adapter)
        self.host = QLineEdit()
        form.addRow(self.tr("address"), self.host)
        self.user = QLineEdit()
        self.user_lbl = QLabel(self.tr("username"))
        form.addRow(self.user_lbl, self.user)
        self.pwd = QLineEdit()
        self.pwd.setEchoMode(QLineEdit.Password)
        self.pwd.returnPressed.connect(self.try_connect)
        self.pwd_lbl = QLabel(self.tr("password"))
        form.addRow(self.pwd_lbl, self.pwd)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(22)
        self.port_lbl = QLabel(self.tr("port"))
        form.addRow(self.port_lbl, self.port)
        v.addLayout(form)
        self.err = QLabel()
        self.err.setObjectName("Error")
        self.err.setWordWrap(True)
        v.addWidget(self.err)
        v.addStretch(1)
        row = QHBoxLayout()
        again = QPushButton(self.tr("redetect"))
        again.clicked.connect(self.start_detect)
        row.addWidget(again)
        row.addStretch(1)
        self.connect_btn = QPushButton(self.tr("connect"))
        self.connect_btn.setObjectName("Primary")
        self.connect_btn.setMinimumHeight(38)
        self.connect_btn.clicked.connect(self.try_connect)
        row.addWidget(self.connect_btn)
        v.addLayout(row)
        return w

    def _done(self):
        w = QWidget()
        self.done_layout = QVBoxLayout(w)
        return w

    # ---------------------------------------------------------------- flow
    def start_detect(self):
        self.stack.setCurrentWidget(self.pg_search)
        self.search_lbl.setText(self.tr("searching"))
        self.task = Task(detect, "")
        self.task.done.connect(self._detected)
        self.task.failed.connect(lambda e: self._detected(None))
        self.task.start()

    def _detected(self, det):
        if det is None:
            self.found_lbl.setText(self.tr("found_unknown", ip=self.s.host))
            self.host.setText(self.s.host)
            adapter = "generic"
        else:
            self.found_lbl.setText(self.tr("found", brand=det.display, ip=det.gateway) if det.display
                                   else self.tr("found_unknown", ip=det.gateway))
            self.host.setText(det.gateway)
            adapter = det.adapter
        self.adapter.setCurrentIndex(max(0, self.adapter.findData(adapter)))
        self._adapter_changed()
        self.err.clear()
        self.stack.setCurrentWidget(self.pg_login)
        (self.pwd if self.pwd.isVisible() else self.connect_btn).setFocus()

    def _adapter_changed(self):
        a = self.adapter.currentData()
        login = a in ("tplink", "openwrt", "huawei")
        for wdg in (self.user, self.user_lbl, self.pwd, self.pwd_lbl):
            wdg.setVisible(login)
        for wdg in (self.port, self.port_lbl):
            wdg.setVisible(a == "openwrt")
        if not self.user.text() or self.user.text() in DEFAULT_USER.values():
            self.user.setText(DEFAULT_USER.get(a, "admin"))
        self.hint_lbl.setText(self.tr("enter_login") if login else self.tr("no_login"))

    def _collect(self) -> Settings:
        s = self.s
        s.backend = self.adapter.currentData()
        s.host = self.host.text().strip()
        s.username = self.user.text().strip()
        s.port = self.port.value()
        s.set_password(self.pwd.text())
        return s

    def try_connect(self):
        s = self._collect()
        self.connect_btn.setEnabled(False)
        self.connect_btn.setText(self.tr("connecting"))
        self.err.clear()

        def probe():
            b = create_backend(s)
            try:
                b.connect()
                b.poll()
                return b.router_name, set(b.capabilities)
            finally:
                b.close()

        self.task = Task(probe)
        self.task.done.connect(self._connected)
        self.task.failed.connect(self._connect_failed)
        self.task.start()

    def _connect_failed(self, err):
        self.connect_btn.setEnabled(True)
        self.connect_btn.setText(self.tr("connect"))
        self.err.setText(self.tr("conn_fail", err=err))

    def _connected(self, result):
        name, caps = result
        self.s.router_label = name
        self.s.onboarded = True
        lay = self.done_layout
        while lay.count():
            item = lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        lay.addWidget(self._hero("check"), 0, Qt.AlignHCenter)
        t = QLabel(self.tr("connected_to", name=name or self.s.host))
        t.setObjectName("Hero")
        t.setStyleSheet("font-size: 18px;")
        t.setAlignment(Qt.AlignCenter)
        t.setWordWrap(True)
        lay.addWidget(t)
        sub = QLabel(self.tr("what_works"))
        sub.setObjectName("Muted")
        lay.addSpacing(10)
        lay.addWidget(sub)
        lay.addWidget(caps_widget(self.tr, caps))
        lay.addStretch(1)
        go = QPushButton(self.tr("start"))
        go.setObjectName("Primary")
        go.setMinimumHeight(40)
        go.clicked.connect(self.accept)
        lay.addWidget(go)
        self.stack.setCurrentWidget(self.pg_done)

    def use_demo(self):
        self.s.backend = "demo"
        self.s.router_label = "Demo Router"
        self.s.onboarded = True
        self.accept()

    def result_settings(self) -> Settings:
        return self.s
