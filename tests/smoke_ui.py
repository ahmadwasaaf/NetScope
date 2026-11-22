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
errors = []
sys.excepthook = lambda *exc: errors.append("".join(traceback.format_exception(*exc)))


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
    win.resize(600, 1500)

    def expand():
        cards = list(win.cards.values())
        if cards:
            cards[0].toggle()
        if len(cards) > 1:
            cards[1].toggle()

    def shoot():
        win.grab().save(os.path.join(OUT, f"{tag}.png"))
        win.quit_app()

    QTimer.singleShot(3500, expand)
    QTimer.singleShot(6500, shoot)
    QTimer.singleShot(12000, app.quit)
    app.exec()
    print(f"{tag}: devices={len(win.cards)} used={win._used_bytes()/1024**3:.2f} GB")


run("dark_en")
run("dark_ar", language="ar")
run("light_en", theme="light", accent="#2979FF")
if errors:
    print("\n".join(errors))
    sys.exit(1)
print("UI smoke test passed")
