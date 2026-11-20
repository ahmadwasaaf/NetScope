"""NetScope – Internet Usage Monitor (entry point)."""
import os
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from netscope.ui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("NetScope")
    app.setOrganizationName("NetScope")
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    icon = os.path.join(base, "icon.ico")
    if os.path.exists(icon):
        app.setWindowIcon(QIcon(icon))
    if sys.platform == "win32":   # own taskbar icon instead of Python's
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NetScope.App")
        except Exception:
            pass
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
