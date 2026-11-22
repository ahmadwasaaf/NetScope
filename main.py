"""NetScope – Internet Usage Monitor (entry point)."""
import os
import sys

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from netscope.ui.main_window import MainWindow

INSTANCE_KEY = "NetScope-single-instance"


def already_running() -> bool:
    """If NetScope is already running, ask it to show its window and return True."""
    sock = QLocalSocket()
    sock.connectToServer(INSTANCE_KEY)
    if sock.waitForConnected(300):
        sock.write(QByteArray(b"show"))
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return True
    return False


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("NetScope")
    app.setOrganizationName("NetScope")
    app.setQuitOnLastWindowClosed(False)          # closing the window keeps it in the tray
    if already_running():
        return 0
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

    win = MainWindow(start_hidden="--minimized" in sys.argv)
    server = QLocalServer()
    QLocalServer.removeServer(INSTANCE_KEY)
    server.listen(INSTANCE_KEY)
    server.newConnection.connect(lambda: (server.nextPendingConnection(), win.show_from_tray()))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
