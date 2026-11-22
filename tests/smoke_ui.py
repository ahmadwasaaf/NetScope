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
    win = mw.MainWindow()
    win.resize(600, 1600)

    def expand():
        cards = list(win.cards.values())
        if cards:
            cards[0].toggle()
        if len(cards) > 1:
            cards[1].toggle()

    def shoot():
        img = win.grab().toImage()
        img.save(os.path.join(OUT, f"{tag}.jpg"), "JPG", 70)
        win.quit_app()

    QTimer.singleShot(3500, expand)
    QTimer.singleShot(6500, shoot)
    QTimer.singleShot(12000, app.quit)
    app.exec()
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
