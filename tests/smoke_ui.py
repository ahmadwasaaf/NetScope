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
                              "aa:bb:cc:00:00:03|youtube.com": 40})

    def expand():                      # like a user: open the type, connected devices open by themselves
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
        win.grab().toImage().save(os.path.join(OUT, f"{tag}.jpg"), "JPG", 70)
        win.scroll_body.grab().toImage().save(os.path.join(OUT, f"{tag}_page.jpg"), "JPG", 60)
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
