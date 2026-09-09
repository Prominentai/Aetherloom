"""Public cover images: visible cards only, bounded HTTP, decoding and cache."""
import io
import threading
import time
from collections import OrderedDict
from urllib.parse import urlsplit

from PyQt5 import QtCore, QtGui

from .thumbnail_resources import BudgetCache, MIB

_slots = threading.BoundedSemaphore(2)


def fetch_thumbnail(url):
    import requests
    from PIL import Image
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
        return QtGui.QImage()
    with requests.get(url, stream=True, timeout=(5, 10)) as response:
        response.raise_for_status()
        if int(response.headers.get('Content-Length') or 0) > 3 * MIB:
            return QtGui.QImage()
        data = bytearray()
        for chunk in response.iter_content(32768):
            data.extend(chunk)
            if len(data) > 3 * MIB:
                return QtGui.QImage()
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 8_000_000:
            return QtGui.QImage()
        image.draft('RGB', (360, 440))
        image.thumbnail((360, 440))
        image = image.convert('RGBA')
        pixels = image.tobytes()
        return QtGui.QImage(pixels, image.width, image.height, image.width * 4,
                            QtGui.QImage.Format_RGBA8888).copy()


class ModelThumbnails(QtCore.QObject):
    changed = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal(str, object)

    def __init__(self, parent):
        super().__init__(parent)
        self.cache = BudgetCache(24 * MIB, 96, lambda image: image.sizeInBytes())
        self.pending = set()
        self.failed = OrderedDict()
        self.finished.connect(self._finished, QtCore.Qt.QueuedConnection)

    def image(self, url):
        image = self.cache.get(url)
        if image is not None:
            self.cache.move_to_end(url)
        return image

    def request(self, url):
        self._request(url,lambda:fetch_thumbnail(url))

    def request_local(self, key, path):
        def load():
            from .rh_model_covers import prepare_cover
            return QtGui.QImage.fromData(prepare_cover(path))
        self._request(key,load)

    def _request(self, url, loader):
        if not url or url in self.cache or url in self.pending:
            return
        # Keep completed-but-not-yet-delivered images bounded as well as threads.
        if len(self.pending)>=2:return
        if self.failed.get(url, 0) > time.monotonic() or not _slots.acquire(blocking=False):
            return
        self.pending.add(url)

        def work():
            try:
                result = loader()
            except Exception:
                result = QtGui.QImage()
            finally:
                _slots.release()
            try:
                self.finished.emit(url, result)
            except RuntimeError:
                pass

        threading.Thread(target=work, name='rh-model-cover', daemon=True).start()

    @QtCore.pyqtSlot(str, object)
    def _finished(self, url, image):
        self.pending.discard(url)
        if image.isNull():
            self.failed[url] = time.monotonic() + 60
            self.failed.move_to_end(url)
            while len(self.failed) > 128:
                self.failed.popitem(last=False)
        else:
            self.cache[url] = image
            self.failed.pop(url, None)
        self.changed.emit(url)


def thumbnails(owner):
    value = getattr(owner, '_rh_model_thumbnails', None)
    if value is None:
        value = owner._rh_model_thumbnails = ModelThumbnails(owner)
    return value
