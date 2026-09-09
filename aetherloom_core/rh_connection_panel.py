"""Shared connection editor and bounded, read-only account queries."""
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
import hashlib
import re
import threading
import time

from PyQt5 import QtCore, QtGui, QtWidgets, sip


def identity(host, key):
    return host, hashlib.sha256(key.encode('utf8')).hexdigest()


def account_text(data):
    def number(name):
        try:
            raw = str(data.get(name))
            if len(raw) > 64:
                return '—'
            value = Decimal(raw)
            return format(value, ',f') if value.is_finite() and -12 <= value.as_tuple().exponent <= 12 and value.adjusted() <= 30 else '—'
        except (InvalidOperation, ValueError):
            return '—'
    currency = str(data.get('currency') or '')
    currency = currency if re.fullmatch('[A-Z]{3}', currency) else ''
    kind = str(data.get('apiType') or '—')
    kind = kind if re.fullmatch(r'[A-Za-z0-9_-]{1,40}', kind) else '—'
    return (f"RH 币 {number('remainCoins')}  ·  钱包 {number('remainMoney')} {currency}\n"
            f"当前任务 {number('currentTaskCounts')}  ·  Key 类型 {kind}")


class AccountQueries(QtCore.QObject):
    changed = QtCore.pyqtSignal()
    finished = QtCore.pyqtSignal(object, object)

    def __init__(self, settings):
        super().__init__(settings)
        self.settings = settings
        self.pending = set()
        self.results = OrderedDict()
        self.finished.connect(self._finished, QtCore.Qt.QueuedConnection)
        settings.changed.connect(self._prune)

    def _valid(self):
        return {identity(host, key) for host, keys in self.settings.site_keyrings().items() for key in keys}

    def _prune(self):
        valid = self._valid()
        for token in list(self.results):
            if token not in valid:
                self.results.pop(token, None)
        self.changed.emit()

    def request(self, host, key):
        token = identity(host, key)
        if token in self.pending:
            return ''
        if len(self.pending) >= 2:
            return '已有两个账户正在查询，请稍候再试。'
        self.pending.add(token)
        self.changed.emit()

        def work():
            from api_calls.call_rh import get_account_status
            try:
                data = get_account_status(key, base_url=host, timeout=15)
                result = (True, account_text(data), time.time())
            except Exception as exc:
                code = str(getattr(exc, 'code', ''))
                http = getattr(getattr(exc, 'response', None), 'status_code', None)
                detail = ('错误码 ' + code if re.fullmatch(r'\d{1,6}', code) else
                          'HTTP ' + str(http) if isinstance(http, int) else '网络或响应异常')
                result = (False, '查询失败：' + detail + '，可重新查询。', time.time())
            try:
                self.finished.emit(token, result)
            except RuntimeError:
                pass  # Owner may have closed while the bounded request finished.

        threading.Thread(target=work, name='rh-account-query', daemon=True).start()
        return ''

    @QtCore.pyqtSlot(object, object)
    def _finished(self, token, result):
        self.pending.discard(token)
        if token in self._valid():
            self.results[token] = result
            self.results.move_to_end(token)
            while len(self.results) > 128:
                self.results.popitem(last=False)
        self.changed.emit()


def button(text, action, parent=None):
    value = QtWidgets.QPushButton(text, parent)
    value.setAutoDefault(False)
    value.setCursor(QtCore.Qt.PointingHandCursor)
    value.clicked.connect(action)
    return value


class KeyRow(QtWidgets.QFrame):
    def __init__(self, panel, key, index, count, draft=None):
        super().__init__()
        self.panel, self.key, self.host = panel, key, panel.settings.host
        self.setObjectName('rhKeyRow')
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        editor = QtWidgets.QHBoxLayout()
        order = QtWidgets.QLabel(f'{index + 1:02}')
        order.setObjectName('rhKeyOrder')
        order.setToolTip('首选密钥' if index == 0 else '重试顺序 ' + str(index + 1))
        editor.addWidget(order)
        self.edit = QtWidgets.QLineEdit(key if draft is None else draft)
        self.edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.edit.setMinimumWidth(60)
        self.edit.setAccessibleName(f'第 {index + 1} 个 API Key')
        editor.addWidget(self.edit, 1)
        self.reveal = button('显示', self._reveal)
        self.reveal.setCheckable(True)
        editor.addWidget(self.reveal)
        self.save = button('保存', self._save)
        self.save.setObjectName('rhConnectionPrimary')
        editor.addWidget(self.save)
        layout.addLayout(editor)
        actions = QtWidgets.QHBoxLayout()
        self.query = button('查询账户', self._query)
        actions.addWidget(self.query)
        actions.addStretch(1)
        self.up = button('↑', lambda: panel.move(self, -1))
        self.down = button('↓', lambda: panel.move(self, 1))
        self.up.setToolTip('提前使用此 Key'); self.down.setToolTip('延后使用此 Key')
        self.up.setEnabled(index > 0); self.down.setEnabled(index < count - 1)
        actions.addWidget(self.up); actions.addWidget(self.down)
        actions.addWidget(button('删除', lambda: panel.remove(self)))
        layout.addLayout(actions)
        self.account = QtWidgets.QLabel()
        self.account.setObjectName('rhAccountResult')
        self.account.setTextFormat(QtCore.Qt.PlainText)
        self.account.setWordWrap(True)
        self.account.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(self.account)
        self.edit.textChanged.connect(self._edited)
        self.edit.returnPressed.connect(self._save)
        self._edited()
        self.refresh_account()

    def _reveal(self):
        show = self.reveal.isChecked()
        self.edit.setEchoMode(QtWidgets.QLineEdit.Normal if show else QtWidgets.QLineEdit.Password)
        self.reveal.setText('隐藏' if show else '显示')

    def _edited(self):
        dirty = self.edit.text().strip() != self.key
        self.save.setEnabled(dirty)
        self.query.setEnabled(not dirty and identity(self.host, self.key) not in self.panel.queries.pending)
        self.query.setToolTip('请先保存修改后的 Key' if dirty else '查询此 Key 所属站点的账户信息')

    def _save(self):
        self.panel.replace(self, self.edit.text().strip())

    def _query(self):
        self.panel.message.setText(self.panel.queries.request(self.host, self.key))

    def refresh_account(self):
        token = identity(self.host, self.key)
        pending = token in self.panel.queries.pending
        result = self.panel.queries.results.get(token)
        self.query.setText('查询中…' if pending else '刷新账户' if result else '查询账户')
        self._edited()
        suffix = '尾号 ' + self.key[-4:] if len(self.key) > 8 else '短密钥'
        text = (result[1] + '\n' + suffix + ' · 更新于 ' + time.strftime('%H:%M:%S', time.localtime(result[2]))
                if result else suffix + ' · 尚未查询账户')
        self.account.setText(text)
        self.account.setProperty('failed', bool(result and not result[0]))
        self.account.style().unpolish(self.account); self.account.style().polish(self.account)


class RhConnectionPanel(QtWidgets.QWidget):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        from .rh_connections import SITES, _HostCombo
        self.settings = settings
        if not hasattr(settings, 'account_queries'):
            settings.account_queries = AccountQueries(settings)
        self.queries = settings.account_queries
        self.rows = []
        self._drafts = {}
        self._host = settings.host
        self._new_drafts = {}
        self.setObjectName('rhConnectionPanel')
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        title = QtWidgets.QLabel('RunningHub 连接')
        title.setObjectName('rhConnectionTitle'); layout.addWidget(title)
        hint = QtWidgets.QLabel('主页与画布同步 · .cn / .ai 密钥分别管理')
        hint.setObjectName('rhConnectionMuted'); hint.setWordWrap(True); layout.addWidget(hint)
        self.host_combo = _HostCombo()
        for host, unused, label in SITES:
            self.host_combo.addItem(label, host)
        layout.addWidget(self.host_combo)
        self.summary = QtWidgets.QLabel()
        self.summary.setObjectName('rhConnectionMuted'); self.summary.setWordWrap(True); layout.addWidget(self.summary)
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.container = QtWidgets.QWidget()
        self.items = QtWidgets.QVBoxLayout(self.container)
        self.items.setContentsMargins(0, 0, 3, 0); self.items.setSpacing(10)
        self.scroll.setWidget(self.container); layout.addWidget(self.scroll, 1)
        self.empty = QtWidgets.QLabel('本站还没有 API Key\n在下方粘贴密钥即可添加')
        self.empty.setAlignment(QtCore.Qt.AlignCenter); self.empty.setObjectName('rhConnectionMuted')
        layout.addWidget(self.empty)
        self.key_edit = QtWidgets.QLineEdit()
        self.key_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.key_edit.setPlaceholderText('粘贴新 Key；多个 Key 可用空格分隔')
        self.key_edit.setClearButtonEnabled(True)
        self.key_edit.setMinimumWidth(60)
        entry = QtWidgets.QHBoxLayout(); entry.addWidget(self.key_edit, 1)
        self.reveal_button = button('显示', self._reveal_new)
        self.reveal_button.setCheckable(True)
        entry.addWidget(self.reveal_button)
        entry.addWidget(button('添加', self._add)); layout.addLayout(entry)
        self.message = QtWidgets.QLabel()
        self.message.setTextFormat(QtCore.Qt.PlainText); self.message.setWordWrap(True)
        layout.addWidget(self.message)
        footer = QtWidgets.QHBoxLayout()
        footer.addWidget(button('获取 API Key', lambda: QtGui.QDesktopServices.openUrl(
            QtCore.QUrl(self.settings.host + '/enterprise-api/sharedApi'))))
        footer.addStretch(1)
        layout.addLayout(footer)
        self.key_edit.returnPressed.connect(self._add)
        self.host_combo.currentIndexChanged.connect(self._host_changed)
        settings.changed.connect(self._refresh)
        settings.error.connect(self.message.setText)
        self.queries.changed.connect(self._accounts_changed)
        self._refresh(); self.apply_theme()

    def _host_changed(self, index):
        self.settings._guard(lambda: self.settings.set_host(self.host_combo.itemData(index)))
        self._refresh()

    def _reveal_new(self):
        visible = self.reveal_button.isChecked()
        self.key_edit.setEchoMode(QtWidgets.QLineEdit.Normal if visible else QtWidgets.QLineEdit.Password)
        self.reveal_button.setText('隐藏' if visible else '显示')

    def _refresh(self):
        if self._host != self.settings.host:
            self._new_drafts[self._host] = self.key_edit.text()
            self._host = self.settings.host
            self.key_edit.setText(self._new_drafts.get(self._host, ''))
            self.reveal_button.setChecked(False); self._reveal_new()
            self.message.clear()
        for row in self.rows:
            self._drafts[identity(row.host, row.key)] = row.edit.text()
        valid = {identity(host, key) for host, keys in self.settings.site_keyrings().items() for key in keys}
        self._drafts = {token: value for token, value in self._drafts.items() if token in valid}
        self.rows = []
        while self.items.count():
            item = self.items.takeAt(0)
            if item.widget():
                item.widget().hide(); item.widget().deleteLater()
        blocker = QtCore.QSignalBlocker(self.host_combo)
        self.host_combo.setCurrentIndex(self.host_combo.findData(self.settings.host))
        del blocker
        keys = self.settings.keys_for()
        self.summary.setText(f'{len(keys)} 个密钥 · 按列表顺序重试，第一项优先')
        for index, key in enumerate(keys):
            row = KeyRow(self, key, index, len(keys), self._drafts.get(identity(self.settings.host, key)))
            self.items.addWidget(row); self.rows.append(row)
        self.items.addStretch(1)
        self.empty.setVisible(not keys); self.scroll.setVisible(bool(keys))

    def _accounts_changed(self):
        for row in self.rows:
            row.refresh_account()

    def _add(self):
        values = list(dict.fromkeys(re.split(r'[\s,;，；]+', self.key_edit.text().strip())))
        values = [value for value in values if value]
        if not values:
            self.message.setText('请先粘贴 API Key。'); return
        keys = self.settings.keys_for()
        added = [value for value in values if value not in keys]
        if not added:
            self.message.setText('这些 Key 已在本站列表中。'); return
        if self.settings._guard(lambda: self.settings.set_keys(keys + added)):
            self.key_edit.clear(); self.message.setText(f'已添加 {len(added)} 个 Key。')
            QtCore.QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))

    def replace(self, row, value):
        keys = self.settings.keys_for(row.host)
        if not value or re.search(r'\s', value):
            self.message.setText('请输入一个完整的 API Key，不要包含空格。'); return
        if value != row.key and value in keys:
            self.message.setText('这个 Key 已在本站列表中。'); return
        if row.key in keys:
            keys[keys.index(row.key)] = value
            if self.settings._guard(lambda: self.settings.set_keys(keys, row.host)):
                self.message.setText('修改已保存。')

    def remove(self, row):
        keys = self.settings.keys_for(row.host)
        if row.key in keys:
            keys.remove(row.key)
            if self.settings._guard(lambda: self.settings.set_keys(keys, row.host)):
                self.message.setText('Key 已删除。')

    def move(self, row, offset):
        keys = self.settings.keys_for(row.host)
        if row.key not in keys: return
        index = keys.index(row.key); target = index + offset
        if 0 <= target < len(keys):
            keys[index], keys[target] = keys[target], keys[index]
            if self.settings._guard(lambda: self.settings.set_keys(keys, row.host)):
                self.message.setText('使用顺序已保存。')

    def showEvent(self, event):
        self.apply_theme(); super().showEvent(event)

    def hideEvent(self, event):
        self.reveal_button.setChecked(False); self._reveal_new()
        for row in self.rows:
            row.reveal.setChecked(False); row._reveal()
        super().hideEvent(event)

    def apply_theme(self):
        from .rh_ui import palette
        p = palette(getattr(self.settings.owner, '_theme_mode', 'dark'))
        self.setStyleSheet(f'''
            QWidget#rhConnectionPanel {{ background: {p['canvas']}; color: {p['text']}; }}
            QWidget#rhConnectionPanel QWidget {{ color: {p['text']}; font-size: 12px; }}
            QWidget#rhConnectionPanel QScrollArea, QWidget#rhConnectionPanel QScrollArea QWidget {{ background: transparent; }}
            QWidget#rhConnectionPanel QFrame#rhKeyRow {{ background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 10px; }}
            QWidget#rhConnectionPanel QLabel {{ background: transparent; border: none; padding: 0; }}
            QWidget#rhConnectionPanel QLabel#rhConnectionTitle {{ font-size: 20px; font-weight: 700; }}
            QWidget#rhConnectionPanel QLabel#rhConnectionMuted, QWidget#rhConnectionPanel QLabel#rhAccountResult {{ color: {p['muted']}; }}
            QWidget#rhConnectionPanel QLabel[failed="true"] {{ color: {p['danger']}; }}
            QWidget#rhConnectionPanel QLineEdit, QWidget#rhConnectionPanel QComboBox {{ background: {p['input']}; color: {p['text']}; border: 1px solid {p['border']}; border-radius: 6px; padding: 8px 6px; selection-background-color: {p['accent']}; }}
            QWidget#rhConnectionPanel QLineEdit:focus {{ border-color: {p['accent']}; }}
            QWidget#rhConnectionPanel QPushButton {{ background: {p['surface']}; color: {p['text']}; border: 1px solid {p['border']}; border-radius: 6px; padding: 6px 9px; }}
            QWidget#rhConnectionPanel QPushButton:hover {{ background: {p['hover']}; border-color: {p['accent']}; }}
            QWidget#rhConnectionPanel QPushButton:disabled {{ color: {p['muted']}; background: {p['input']}; }}
            QWidget#rhConnectionPanel QPushButton#rhConnectionPrimary:enabled {{ background: {p['accent']}; color: white; border: 1px solid {p['accent']}; }}
            QWidget#rhConnectionPanel QComboBox QAbstractItemView {{ background: {p['surface']}; color: {p['text']}; selection-background-color: {p['accent_soft']}; }}
        ''')
