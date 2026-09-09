"""Bounded local cover preparation and an image-only drag/drop editor."""
import io
import threading
from pathlib import Path

from PyQt5 import QtCore, QtGui, QtWidgets

_preparations=threading.BoundedSemaphore(2)


def prepare_cover(path):
    from PIL import Image, ImageOps
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError('请选择不超过 20 MB 的图片')
    with Image.open(path) as source:
        if source.width * source.height > 40_000_000:
            raise ValueError('图片尺寸过大，请先缩小至 4000 万像素以内')
        source.draft('RGB', (720, 880));source.thumbnail((720, 880))
        image = ImageOps.exif_transpose(source).convert('RGBA')
        target = io.BytesIO();image.save(target, format='PNG')
        return target.getvalue()


class CoverDrop(QtWidgets.QLabel):
    prepared = QtCore.pyqtSignal(int, object, str)

    def __init__(self, picker, value, parent=None):
        super().__init__(parent)
        self.picker, self.value = picker, value
        self.data = None;self.clear_cover = False;self.busy = False;self.generation = 0
        self.setAcceptDrops(True);self.setAlignment(QtCore.Qt.AlignCenter)
        self.setMinimumHeight(130);self.setMaximumHeight(150)
        self.setObjectName('rhCoverDrop');self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setFocusPolicy(QtCore.Qt.StrongFocus);self.setAccessibleName('模型封面，点击或按回车选择图片')
        self.setToolTip('拖入图片或点击选择，保存收藏时替换原封面')
        self.prepared.connect(self._prepared, QtCore.Qt.QueuedConnection)
        self.picker.thumbs.changed.connect(self._remote_changed)
        self._preview()

    def _preview(self):
        info = self.picker.favorites.cover_info(self.value)
        if self.data:
            image = QtGui.QImage.fromData(self.data)
        elif info:
            reader=QtGui.QImageReader(str(info[1]));size=reader.size()
            image=reader.read() if 0<size.width()*size.height()<=2_000_000 else QtGui.QImage()
        else:
            url=self.value.get('thumbnail') or ''
            image=self.picker.thumbs.image(url)
            if image is None:
                image=QtGui.QImage()
                self.picker.thumbs.request(url)
        if image.isNull():
            self.setText('拖入封面图片\n或点击选择图片')
        else:
            self.setPixmap(QtGui.QPixmap.fromImage(image).scaled(180,140,QtCore.Qt.KeepAspectRatio,QtCore.Qt.SmoothTransformation))

    def _remote_changed(self, url):
        if not self.busy and self.data is None and url==self.value.get('thumbnail'):self._preview()

    def load(self, path):
        if self.busy:return
        if not _preparations.acquire(blocking=False):self.setText('正在处理其他封面，请稍后再试');return
        self.busy = True;self.generation += 1;generation = self.generation
        self.setText('正在处理封面…')
        def work():
            try:data,error=prepare_cover(path),''
            except Exception:data,error=None,'无法读取该图片，请选择有效且尺寸适中的图片'
            finally:_preparations.release()
            try:self.prepared.emit(generation,data,error)
            except RuntimeError:pass
        threading.Thread(target=work,name='rh-local-cover',daemon=True).start()

    def _prepared(self, generation, data, error):
        if generation != self.generation:return
        self.busy = False
        if error:self.setText(error);return
        self.data = data;self.clear_cover = False;self._preview()

    def choose_file(self):
        if self.busy:return
        path,_=QtWidgets.QFileDialog.getOpenFileName(self,'选择模型封面','','图片 (*.png *.jpg *.jpeg *.webp *.bmp);;所有文件 (*)')
        if path:self.load(path)

    def keyPressEvent(self, event):
        if event.key() in (QtCore.Qt.Key_Return,QtCore.Qt.Key_Enter,QtCore.Qt.Key_Space):
            self.choose_file();event.accept();return
        super().keyPressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button()==QtCore.Qt.LeftButton:self.choose_file()
        super().mouseReleaseEvent(event)

    def dragEnterEvent(self, event):
        urls=event.mimeData().urls()
        if not self.busy and len(urls)==1 and urls[0].isLocalFile():event.acceptProposedAction()

    def dropEvent(self, event):
        urls=event.mimeData().urls()
        if len(urls)==1 and urls[0].isLocalFile():
            self.load(urls[0].toLocalFile());event.acceptProposedAction()
