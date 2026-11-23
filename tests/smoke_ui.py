"""UI smoke test (runs on the GitHub Windows builder, offscreen, demo router):
starts NetScope, lets it poll, expands sections, saves screenshots, fails on any exception."""
import json
import os
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "screens")
os.makedirs(OUT, exist_ok=True)
import faulthandler
faulthandler.enable()
errors = []


def hook(*exc):
    text = "".join(traceback.format_exception(*exc))
    errors.append(text)
    print(text, file=sys.stderr, flush=True)


sys.excepthook = hook


def run(tag, **settings):
    os.environ["APPDATA"] = tempfile.mkdtemp()
    os.makedirs(os.path.join(os.environ["APPDATA"], "NetScope"), exist_ok=True)
    base = dict(onboarded=True, backend="demo", devices_open=True, router_open=True,
                language="en", theme="dark", quota_gb=200, alarm_gb=100, autostart=False,
                start_date="2026-11-01")
    base.update(settings)
    with open(os.path.join(os.environ["APPDATA"], "NetScope", "settings.json"), "w") as f:
        json.dump(base, f)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    import importlib
    import netscope.settings, netscope.ui.main_window as mw
    importlib.reload(netscope.settings)
    importlib.reload(mw)
    mw._primary_ipv4 = lambda: "192.168.1.2"            # demo "Desktop PC" is this PC
    win = mw.MainWindow()
    win.resize(600, 860)
    win.store.add("pcsite", {"youtube.com": 900 * 1024**2, "x.com": 470 * 1024**2, "claude.ai": 140 * 1024**2,
                             "google.com": 61 * 1024**2, "whatsapp.com": 41 * 1024**2, "github.com": 30 * 1024**2,
                             "wikipedia.org": 20 * 1024**2, "Other / unresolved": 12 * 1024**2})
    win.store.add("devsite", {"aa:bb:cc:00:00:03|tiktok.com": 120, "aa:bb:cc:00:00:03|instagram.com": 80,
                              "aa:bb:cc:00:00:03|youtube.com": 40,
                              "aa:bb:cc:00:00:03|3gppnetwork.org": 90, "aa:bb:cc:00:00:03|gstatic.com": 60,
                              "aa:bb:cc:00:00:03|samsungcloud.com": 20})
    win._sys_open.add("aa:bb:cc:00:00:03")

    import socket
    from netscope import winsetup
    try:
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.connect(("8.8.8.8", 53)); real_ip = u.getsockname()[0]; u.close()
    except OSError:
        real_ip = ""
    print(f"{tag}: real windows state for {real_ip} = {winsetup.read_state(real_ip)}", flush=True)
    if tag == "dark_en" and sys.platform == "win32":
        code = winsetup.allow_firewall()
        st = winsetup.read_state(real_ip)
        print(f"{tag}: allow_firewall exit={code} fw={st.get('fw')} err={winsetup.last_error()!r}", flush=True)
        if code != 0 or not st.get("fw"):
            errors.append(f"firewall rule not created (exit {code}, fw {st.get('fw')})")

    win._read_win_state = lambda: None     # keep the sample state below on screen

    def expand():                      # like a user: open the type, connected devices open by themselves
        win.win_state = {"ok": True, "if": 7, "profile": "Public", "dns": ["192.168.1.2"],
                         "self_dns": True, "public_dns": False, "fw": False}
        win.lan_state = {"supported": True, "active": False}
        win._setup_sig = None
        win._refresh_dns_info()
        for key in ("grp:pc", "grp:phone"):
            if key in win.cards and not win.cards[key].is_open:
                win.cards[key].toggle()

    def shoot():
        card = win.dev_cards.get("aa:bb:cc:00:00:02")
        if card is None or not card.is_open:
            errors.append(f"{tag}: This PC did not open automatically")
        else:
            need = card.header.sizeHint().height()
            if card.header.height() < need - 2:
                errors.append(f"{tag}: device header squashed ({card.header.height()} < {need})")
            sc = card.sites_scroll
            if sc.verticalScrollBar().maximum() <= 0:
                errors.append(f"{tag}: >5 websites but no inner scrollbar (h={sc.height()})")
            rows = [r for r in card.rows.values() if r.isVisible()]
            if not rows or min(r.height() for r in rows) < 20:
                errors.append(f"{tag}: website rows not visible ({[r.height() for r in rows]})")
        if win.setup_box.count() < 2:
            errors.append(f"{tag}: phone setup checklist missing ({win.setup_box.count()} rows)")
        print(f"{tag}: windows state = {win.win_state}", flush=True)
        active = [k for k, c in win._all_cards() if c.active]
        if active != ["grp:phone"]:
            errors.append(f"{tag}: highlight should be only on the last opened card, got {active}")
        win.grab().toImage().save(os.path.join(OUT, f"{tag}.jpg"), "JPG", 70)
        win.scroll_body.grab().toImage().save(os.path.join(OUT, f"{tag}_page.jpg"), "JPG", 60)
        if tag == "dark_ar" or tag == "dark_en":       # saved routers list in Settings
            from netscope.ui.dialogs import SettingsDialog
            win.s.routers = [
                {"id": "a1", "name": "Home Router", "model": "Huawei DN8245V-56", "host": "192.168.1.1", "backend": "huawei"},
                {"id": "b2", "name": "Office Router", "model": "ZTE H188A", "host": "192.168.1.1", "backend": "zte"},
                {"id": "c3", "name": "Second Home", "model": "TP-Link Archer C6", "host": "192.168.0.1", "backend": "tplink"}]
            win.s.active_router, win.s.backend = "a1", "huawei"
            dlg = SettingsDialog(win.tr, win.s, win, tab=1, online=True)
            dlg.resize(520, 620)
            dlg.show()
            app.processEvents()
            dlg.grab().toImage().save(os.path.join(OUT, f"{tag}_routers.jpg"), "JPG", 75)
            if dlg.rows_box.count() != 3:
                errors.append(f"{tag}: routers list shows {dlg.rows_box.count()} rows")
            dlg.reject()
        if tag == "dark_en":                            # wide window: two columns
            win.resize(1240, 860)
            app.processEvents()
            win._fit_body()
            app.processEvents()
            if not win._wide or not win.scroll2.isVisible():
                errors.append("wide window did not switch to two columns")
            win.grab().toImage().save(os.path.join(OUT, "wide.jpg"), "JPG", 70)
            from netscope.ui.dialogs import DnsLogDialog
            import time as _t
            fake = [(_t.time() - i * 7, "192.168.1.3", n, 1) for i, n in enumerate(
                ["i.instagram.com", "rr3---sn-x.googlevideo.com", "youtubei.googleapis.com",
                 "connectivitycheck.gstatic.com", "graph.facebook.com"])]
            d2 = DnsLogDialog(win.tr, lambda: fake, lambda ip: "Ahmed-s-A15", lambda h: h, win)
            d2.show()
            app.processEvents()
            d2.grab().toImage().save(os.path.join(OUT, "dnslog.jpg"), "JPG", 75)
            if d2.table.rowCount() != 5:
                errors.append(f"DNS log shows {d2.table.rowCount()} rows")
            d2.reject()
        win.quit_app()

    timers = []
    for ms, fn in ((3500, expand), (6500, shoot), (12000, app.quit)):
        t = QTimer()
        t.setSingleShot(True)
        t.timeout.connect(fn)
        t.start(ms)
        timers.append(t)
    app.exec()
    for t in timers:          # never let a timer of this run fire in the next one
        t.stop()
    print(f"{tag}: devices={len(win.cards)} used={win._used_bytes()/1024**3:.2f} GB")


for tag, kw in (("dark_en", {}), ("dark_ar", {"language": "ar"}),
                ("light_en", {"theme": "light", "accent": "#2979FF"})):
    try:
        run(tag, **kw)
    except Exception:
        hook(*sys.exc_info())
if errors:
    print("\n".join(errors))
    sys.exit(1)
import base64
for name in sorted(os.listdir(OUT)):
    with open(os.path.join(OUT, name), "rb") as f:
        data = base64.b64encode(f.read()).decode()
    for i in range(0, len(data), 60000):          # short log lines
        print(f"==IMG {name} {i // 60000}==" + data[i:i + 60000] + "==END==")
print("UI smoke test passed")
