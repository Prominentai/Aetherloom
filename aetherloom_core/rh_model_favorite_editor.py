"""Create or edit an offline favorite for an existing cloud model name."""
from PyQt5 import QtCore, QtWidgets

from .rh_model_favorites import TYPES
from .rh_parameters import RhEnumComboBox
from .rh_model_covers import CoverDrop
from .rh_model_dialogs import ModelDialog, section, label


class FavoriteEditor(ModelDialog):
    saved = QtCore.pyqtSignal(object)

    def __init__(self, picker, value=None):
        bucket=(value or {}).get('bucket') or picker.destination();uploaded=bucket=='uploads'
        title=(('编辑上传记录' if value else '登记上传模型') if uploaded else ('编辑收藏' if value else '自建模型收藏'))
        super().__init__(picker,title,'登记已上传的 RH 模型，保存为本地记录。' if uploaded else '保存常用模型及私有模型的快捷项。',(720,730))
        self.bucket=bucket;self.store=picker.store_for(self.bucket)
        self.picker, self.value = picker, dict(value or {})
        self.columns=QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.LeftToRight);self.columns.setSpacing(14)
        self.body.addLayout(self.columns);self.body.addStretch()
        info,info_box=section('模型信息');display,display_box=section('封面与备注')
        self.columns.addWidget(info,3);self.columns.addWidget(display,2)
        form=QtWidgets.QFormLayout();form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.WrapAllRows);form.setVerticalSpacing(8)
        info_box.addLayout(form)
        self.origin=self.value.get('site') or picker.settings.host
        self.site=QtWidgets.QLabel('国际站' if self.origin.endswith('.ai') else '中文站')
        self.site.setObjectName('rhModelBadge')
        form.addRow('所属站点',self.site)
        self.kind=RhEnumComboBox();self.kind.addItems(TYPES)
        self.kind.setCurrentText(self.value.get('resource_type') or picker.resource_type)
        self.kind.setEnabled(picker.library)
        form.addRow('模型类型',self.kind)
        self.fields={}
        for name,caption,placeholder in [('title','显示名称','便于识别的名称'),('model_name','Model name · 必填','例如 my_private_model.safetensors'),
                ('version','版本','自定义'),
                ('trigger_words','触发词','可选')]:
            editor=QtWidgets.QLineEdit(str(self.value.get(name) or ''));editor.setPlaceholderText(placeholder)
            editor.setMinimumWidth(0);self.fields[name]=editor;form.addRow(caption,editor)
        from .rh_model_bases import BaseModelCombo
        self.base_model=BaseModelCombo(picker.base_models,self.origin,self.value.get('base_model') or '',self)
        form.addRow('基础模型',self.base_model)
        info_box.addStretch()
        self.cover=CoverDrop(picker,self.value,self);self.cover.setMinimumHeight(180);self.cover.setMaximumHeight(220);display_box.addWidget(self.cover)
        display_box.addWidget(label('拖入或点击选择图片，保存时替换封面。'))
        display_box.addWidget(label('备注','rhModelFieldLabel'))
        self.notes=QtWidgets.QPlainTextEdit(str(self.value.get('notes') or ''));self.notes.setFixedHeight(120)
        self.notes.setPlaceholderText('用途、适用工作流等');display_box.addWidget(self.notes)
        self.pinned=QtWidgets.QCheckBox('置顶模型' if uploaded else '置顶收藏');self.pinned.setChecked(bool(self.value.get('pinned',True)));display_box.addWidget(self.pinned);display_box.addStretch()
        self.error=self.message
        self.button('取消',self.reject)
        self.save_button=self.button('保存到我的上传' if uploaded else '保存收藏',self.save,True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self,'columns'):
            self.columns.setDirection(QtWidgets.QBoxLayout.TopToBottom if self.width()<640 else QtWidgets.QBoxLayout.LeftToRight)

    def save(self):
        if not self.fields['model_name'].text().strip():
            self.error.setText('请填写 Model name，它应与 RH 中可调用的模型文件名一致。')
            self.fields['model_name'].setFocus();self.scroll.ensureWidgetVisible(self.fields['model_name']);return
        if self.cover.busy:self.error.setText('封面正在处理，请稍候再保存');return
        value=dict(self.value,site=self.origin,resource_type=self.kind.currentText(),custom=self.value.get('custom',True),
                   notes=self.notes.toPlainText(),pinned=self.pinned.isChecked(),bucket=self.bucket,base_model=self.base_model.currentText().strip())
        value.update({name:editor.text().strip() for name,editor in self.fields.items()})
        try:
            identity=self.store.save(value,self.value.get('id'),cover_bytes=self.cover.data)
        except Exception as error:
            self.error.setText('保存失败：'+str(error));return
        self.saved.emit(dict(value,id=identity));self.accept()
