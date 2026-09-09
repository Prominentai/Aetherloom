"""Default-browser login entry point and a cancellable, memory-only RH session."""
import threading

from PyQt5 import QtCore, QtGui

from .rh_model_browser_storage import default_session

_captures = threading.BoundedSemaphore(1)


class ModelBrowser(QtCore.QObject):
    state = QtCore.pyqtSignal(object)
    session_ready = QtCore.pyqtSignal(object)
    error = QtCore.pyqtSignal(str)
    captured = QtCore.pyqtSignal(int, object, str)

    def __init__(self, site, parent=None):
        super().__init__(parent)
        from .rh_model_favorites import SITES
        if site not in SITES:raise ValueError('无效站点')
        self.site = site
        self.closed = False
        self.session_revision = 0
        self.stop_event = threading.Event()
        self.captured.connect(self._captured, QtCore.Qt.QueuedConnection)
        app = QtCore.QCoreApplication.instance()
        if app:app.aboutToQuit.connect(self.close)

    def open(self):
        if self.closed:return
        if not QtGui.QDesktopServices.openUrl(QtCore.QUrl(self.site + '/user-center')):
            self.error.emit('无法打开默认浏览器，请自行打开当前站点的 RunningHub 官网并登录')
        else:
            self.state.emit(dict(opened=True))

    def capture_session(self):
        if self.closed:return
        self.cancel_capture()
        revision = self.session_revision
        stop = self.stop_event = threading.Event()

        def work():
            result, error = None, ''
            acquired = _captures.acquire(blocking=False)
            try:
                if not acquired:raise ValueError('正在读取浏览器登录状态，请稍后重试')
                result = default_session(self.site, stop)
            except InterruptedError:
                return
            except ValueError as exc:
                error = str(exc)
            except Exception:
                error = '无法读取默认浏览器登录状态，请在默认浏览器刷新官网后重试'
            finally:
                if acquired:_captures.release()
            if stop.is_set():
                if result:result.clear()
                return
            try:self.captured.emit(revision, result, error)
            except RuntimeError:
                if result:result.clear()

        threading.Thread(target=work, name='rh-default-browser-session', daemon=True).start()

    def _captured(self, revision, session, error):
        if self.closed or revision != self.session_revision:
            if session:session.clear()
            return
        if error:self.error.emit(error)
        elif session:self.session_ready.emit(session)

    def cancel_capture(self):
        self.session_revision += 1
        self.stop_event.set()

    def close(self):
        self.closed = True
        self.cancel_capture()
