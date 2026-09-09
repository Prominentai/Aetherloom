"""Lazy standalone model library, sharing the picker and local favorite model."""
from PyQt5 import QtCore, QtWidgets

from .rh_connections import ensure_connections
from .rh_model_picker import ModelPicker


class ModelLibraryPage(QtWidgets.QWidget):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner, self.gallery = owner, None
        self.box = QtWidgets.QVBoxLayout(self);self.box.setContentsMargins(0,0,0,0)

    def showEvent(self, event):
        super().showEvent(event)
        if self.gallery is None:
            self.gallery=ModelPicker(ensure_connections(self.owner),'CHECKPOINT',parent=self,library=True)
            self.box.addWidget(self.gallery)
            self.gallery.show()


def install_model_library(owner):
    owner.rh_model_library_page=ModelLibraryPage(owner)
    owner.pages.addWidget(owner.rh_model_library_page)
    def select():
        for button in owner._sidebar_buttons:button.setChecked(button is owner.rh_models_btn)
        owner.pages.setCurrentWidget(owner.rh_model_library_page)
    owner.rh_models_btn.clicked.connect(select)
    owner.pages.currentChanged.connect(lambda unused:owner.rh_models_btn.setChecked(owner.pages.currentWidget() is owner.rh_model_library_page))
