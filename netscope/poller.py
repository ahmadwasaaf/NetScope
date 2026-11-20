"""Background worker: polls the router and executes admin actions off the UI thread."""
from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from .backends import RouterBackend, Snapshot


class Worker(QObject):
    snapshot = Signal(object)                     # Snapshot
    action_done = Signal(str, str, bool, str)     # action, target, ok, error
    call_done = Signal(str, object, str)          # method, result, error

    def __init__(self, backend: RouterBackend, interval_sec: int):
        super().__init__()
        self.backend = backend
        self.interval = max(backend.min_interval, interval_sec) * 1000
        self.timer = None

    @Slot()
    def start(self):
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(self.interval)
        self.poll()

    @Slot()
    def poll(self):
        try:
            snap = self.backend.poll()
        except PermissionError as e:
            # wrong password: stop retrying so the router doesn't lock the admin account
            if self.timer:
                self.timer.stop()
            self.backend.close()
            snap = Snapshot(online=False, error=str(e))
        except Exception as e:  # router unreachable …
            self.backend.close()
            snap = Snapshot(online=False, error=str(e))
        snap.capabilities = set(self.backend.capabilities)
        snap.router_name = snap.router_name or self.backend.router_name
        self.snapshot.emit(snap)

    @Slot(str, str)
    def run_action(self, action: str, target: str):
        try:
            getattr(self.backend, action)(target)
            self.action_done.emit(action, target, True, "")
        except Exception as e:
            self.action_done.emit(action, target, False, str(e))
        self.poll()

    @Slot(str)
    def run_call(self, method: str):
        try:
            self.call_done.emit(method, getattr(self.backend, method)(), "")
        except Exception as e:
            self.call_done.emit(method, None, str(e) or e.__class__.__name__)

    @Slot()
    def stop(self):
        if self.timer:
            self.timer.stop()
        self.backend.close()


class Poller(QObject):
    """Owns the worker thread. Use .request(action, target) to block/unblock."""
    snapshot = Signal(object)
    action_done = Signal(str, str, bool, str)
    _do = Signal(str, str)
    _call = Signal(str)
    _stop = Signal()

    def __init__(self, backend: RouterBackend, interval_sec: int):
        super().__init__()
        self.thread = QThread()
        self.worker = Worker(backend, interval_sec)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.start)
        self.worker.snapshot.connect(self.snapshot)
        self.worker.action_done.connect(self.action_done)
        self._do.connect(self.worker.run_action)
        self._call.connect(self.worker.run_call)
        self._callbacks = {}
        self.worker.call_done.connect(self._on_call_done)
        self._stop.connect(self.worker.stop)
        self.thread.start()

    def request(self, action: str, target: str):
        self._do.emit(action, target)

    def call(self, method: str, done, failed):
        """Run a backend method on the worker thread, reusing the live router session."""
        self._callbacks[method] = (done, failed)
        self._call.emit(method)

    def _on_call_done(self, method, result, err):
        done, failed = self._callbacks.pop(method, (None, None))
        if err and failed:
            failed(err)
        elif done:
            done(result)

    def shutdown(self):
        self._stop.emit()
        self.thread.quit()
        self.thread.wait(4000)


class Task(QThread):
    """Run one blocking function (detection, connection test) in the background."""
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args = fn, args

    def run(self):
        try:
            self.done.emit(self.fn(*self.args))
        except Exception as e:
            self.failed.emit(str(e) or e.__class__.__name__)
