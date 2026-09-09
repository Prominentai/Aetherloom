"""Read one RH localStorage key from the default browser, without opening its DB.

Uses LevelDB's documented manifest/log/table formats, including tombstones.
Only live tables and logs are consulted; no profile copies or recovered secrets.
All browser files are read-only and credentials stay in memory.
"""
import json
import os
import re
import struct
import threading
import time
from pathlib import Path

from .rh_model_favorites import SITES

_LIMIT = 32 * 1024 * 1024
_CRC_TABLE = []
for _i in range(256):
    _n = _i
    for _j in range(8):
        _n = (_n >> 1) ^ (0x82F63B78 if _n & 1 else 0)
    _CRC_TABLE.append(_n)


def _crc(data):
    value = 0xffffffff
    for byte in data:
        value = _CRC_TABLE[(value ^ byte) & 255] ^ (value >> 8)
    value ^= 0xffffffff
    return (((value >> 15) | (value << 17)) + 0xa282ead8) & 0xffffffff


class _Reader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def take(self, size):
        if size < 0 or self.pos + size > len(self.data):
            raise ValueError('浏览器存储数据不完整，请刷新官网后重试')
        result = self.data[self.pos:self.pos + size]
        self.pos += size
        return result

    def varint(self):
        value = 0
        for shift in range(0, 70, 7):
            byte = self.take(1)[0]
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError('无法识别浏览器存储格式')

    def string(self):
        return self.take(self.varint())


def _snappy(data):
    reader = _Reader(data)
    size = reader.varint()
    if size > _LIMIT:
        raise ValueError('浏览器存储块过大，已停止读取')
    out = bytearray()
    while reader.pos < len(data):
        tag = reader.take(1)[0]
        kind = tag & 3
        if kind == 0:
            count = tag >> 2
            count = count + 1 if count < 60 else int.from_bytes(reader.take(count - 59), 'little') + 1
            if len(out) + count > size:
                raise ValueError('浏览器压缩数据异常')
            out.extend(reader.take(count))
        else:
            if kind == 1:
                count = 4 + ((tag >> 2) & 7)
                offset = ((tag & 224) << 3) | reader.take(1)[0]
            else:
                count = 1 + (tag >> 2)
                offset = int.from_bytes(reader.take(2 if kind == 2 else 4), 'little')
            if not 0 < offset <= len(out) or len(out) + count > size:
                raise ValueError('浏览器压缩数据异常')
            # Repeating copies may overlap the bytes being appended.
            part = out[len(out) - offset:len(out) - offset + min(offset, count)]
            out.extend((part * ((count + offset - 1) // offset))[:count])
    if len(out) != size:
        raise ValueError('浏览器压缩数据不完整')
    return bytes(out)


def _logs(data):
    partial = None
    for start in range(0, len(data), 32768):
        block = data[start:start + 32768]
        pos = 0
        while pos + 7 <= len(block):
            crc, size, kind = struct.unpack_from('<IHB', block, pos)
            pos += 7
            if kind == size == crc == 0:
                break
            if pos + size > len(block):
                raise ValueError('浏览器正在更新登录状态，请稍后重试')
            value = block[pos:pos + size]
            pos += size
            if _crc(bytes([kind]) + value) != crc:
                raise ValueError('浏览器正在更新登录状态，请稍后重试')
            if kind == 1 and partial is None:
                yield value
            elif kind == 2 and partial is None:
                partial = bytearray(value)
            elif kind in (3, 4) and partial is not None:
                partial.extend(value)
                if len(partial) > _LIMIT:
                    raise ValueError('浏览器存储记录过大')
                if kind == 4:
                    yield bytes(partial)
                    partial = None
            else:
                raise ValueError('无法识别浏览器日志格式')
        if any(block[pos:]):
            raise ValueError('浏览器正在更新登录状态，请稍后重试')
    if partial is not None:
        raise ValueError('浏览器正在更新登录状态，请稍后重试')


def _manifest(data):
    tables, log, previous = {}, 0, 0
    for record in _logs(data):
        r = _Reader(record)
        while r.pos < len(record):
            tag = r.varint()
            if tag == 1:
                if r.string() != b'leveldb.BytewiseComparator':
                    raise ValueError('暂不支持该浏览器存储格式')
            elif tag in (2, 3, 4, 9):
                value = r.varint()
                if tag == 2: log = value
                if tag == 9: previous = value
            elif tag == 5:
                r.varint();r.string()
            elif tag == 6:
                level, number = r.varint(), r.varint()
                tables.pop((level, number), None)
            elif tag == 7:
                level, number, size = r.varint(), r.varint(), r.varint()
                smallest, largest = r.string(), r.string()
                if len(smallest) < 8 or len(largest) < 8:
                    raise ValueError('浏览器存储索引异常')
                tables[level, number] = (size, smallest[:-8], largest[:-8])
            else:
                raise ValueError('暂不支持该浏览器存储版本')
    return tables, log, previous


def _entries(data):
    if len(data) < 4:
        raise ValueError('浏览器存储块不完整')
    restarts = int.from_bytes(data[-4:], 'little')
    end = len(data) - 4 - restarts * 4
    if restarts < 1 or end < 0:
        raise ValueError('浏览器存储块索引异常')
    r, last = _Reader(data[:end]), b''
    while r.pos < end:
        shared, size, vsize = r.varint(), r.varint(), r.varint()
        if shared > len(last):
            raise ValueError('浏览器存储块索引异常')
        last = last[:shared] + r.take(size)
        yield last, r.take(vsize)


def _table_values(path, keys, check):
    with path.open('rb') as file:
        size = file.seek(0, 2)
        if size < 48:
            raise ValueError('浏览器存储表不完整')
        file.seek(-48, 2)
        footer = file.read(48)
        if footer[-8:] != bytes.fromhex('57fb808b247547db'):
            raise ValueError('暂不支持该浏览器存储表格式')
        r = _Reader(footer[:40]);r.varint();r.varint()

        def block(offset, length):
            check()
            if length > _LIMIT or offset + length + 5 > size - 48:
                raise ValueError('浏览器存储块大小异常')
            file.seek(offset)
            raw = file.read(length + 5)
            if len(raw) != length + 5 or _crc(raw[:-4]) != int.from_bytes(raw[-4:], 'little'):
                raise ValueError('浏览器存储块校验失败，请刷新官网后重试')
            if raw[-5] == 0:return raw[:-5]
            if raw[-5] == 1:return _snappy(raw[:-5])
            raise ValueError('暂不支持该浏览器存储压缩格式')

        index = block(r.varint(), r.varint())
        lower = None
        for upper, handle in _entries(index):
            if len(upper) < 8:raise ValueError('浏览器存储索引异常')
            upper = upper[:-8]
            if any((lower is None or key >= lower) and key <= upper for key in keys):
                h = _Reader(handle)
                for key, value in _entries(block(h.varint(), h.varint())):
                    if len(key) < 8:raise ValueError('浏览器存储键异常')
                    if key[:-8] in keys:
                        suffix = int.from_bytes(key[-8:], 'little')
                        yield suffix >> 8, suffix & 255, value
            lower = upper


def read_login_key(folder, site, stop=None):
    """Return only the newest live RH access token, never an older/deleted value."""
    if site not in SITES:raise ValueError('无效站点')
    stop = stop or threading.Event()
    deadline = time.monotonic() + 20

    def check():
        if stop.is_set():raise InterruptedError()
        if time.monotonic() > deadline:raise ValueError('读取浏览器登录状态超时，请稍后重试')

    def read(path, limit=_LIMIT):
        check()
        with path.open('rb') as file:
            data = file.read(limit + 1)
        if len(data) > limit:raise ValueError('浏览器存储过大，已停止读取')
        return data

    key = b'_' + site.encode('ascii') + b'\0'
    keys = {key + b'\1Rh-Accesstoken', key + b'\0' + 'Rh-Accesstoken'.encode('utf-16-le')}
    folder = Path(folder)
    for attempt in range(3):
        check()
        try:
            current = read(folder / 'CURRENT', 128)
            name = current.decode('ascii').strip()
            if not re.fullmatch(r'MANIFEST-\d+', name):raise ValueError('浏览器存储索引异常')
            manifest = read(folder / name)
            tables, log, previous = _manifest(manifest)
            latest = (-1, 0, b'')

            def accept(seq, kind, value):
                nonlocal latest
                if kind not in (0, 1):raise ValueError('浏览器存储记录类型异常')
                if seq > latest[0]:latest = (seq, kind, value)

            for (_, number), (_, smallest, largest) in tables.items():
                check()
                if not any(smallest <= key <= largest for key in keys):continue
                path = folder / f'{number:06}.ldb'
                if not path.exists():path = folder / f'{number:06}.sst'
                for entry in _table_values(path, keys, check):accept(*entry)
            logs = sorted(p for p in folder.glob('*.log') if p.stem.isdigit() and
                          (int(p.stem) >= log or int(p.stem) == previous))
            if len(logs) > 64:raise ValueError('浏览器日志数量异常，请重新打开官网后重试')
            stamps = []
            for path in logs:
                stat = path.stat();stamps.append((path, stat.st_size, stat.st_mtime_ns))
                for record in _logs(read(path)):
                    check()
                    r = _Reader(record)
                    seq, count = struct.unpack('<QI', r.take(12))
                    for i in range(count):
                        kind = r.take(1)[0];key = r.string()
                        if kind not in (0, 1):raise ValueError('浏览器日志记录类型异常')
                        value = r.string() if kind == 1 else b''
                        if key in keys:accept(seq + i, kind, value)
                    if r.pos != len(record):raise ValueError('浏览器日志记录不完整')
            if read(folder / 'CURRENT', 128) != current or read(folder / name) != manifest:
                raise OSError('changed')
            if any((p.stat().st_size, p.stat().st_mtime_ns) != (size, modified) for p, size, modified in stamps):
                raise OSError('changed')
            if logs != sorted(p for p in folder.glob('*.log') if p.stem.isdigit() and
                              (int(p.stem) >= log or int(p.stem) == previous)):
                raise OSError('changed')
            if latest[1] == 0 or not latest[2]:return None
            value = latest[2]
            if len(value) > 32000:raise ValueError('官网登录状态异常，请重新登录')
            if value[0] not in (0, 1):raise ValueError('无法识别官网登录状态')
            token = value[1:].decode('utf-16-le' if value[0] == 0 else 'latin1')
            if not token or token in ('null', 'undefined'):return None
            if len(token) > 15900 or any(ord(c) < 33 or ord(c) > 126 for c in token):
                raise ValueError('官网登录状态异常，请重新登录')
            return token
        except (OSError, ValueError, UnicodeError):
            if attempt == 2:raise
            if stop.wait(.2):raise InterruptedError()


def default_profile():
    """Resolve Windows HTTPS default, then its most recently used profile only."""
    if os.name != 'nt':raise ValueError('当前仅支持读取 Windows 默认浏览器的登录状态')
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice') as key:
            progid = winreg.QueryValueEx(key, 'ProgId')[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + r'\shell\open\command') as key:
            command = winreg.QueryValueEx(key, None)[0]
    except OSError:
        raise ValueError('无法识别默认浏览器，请在 Windows 设置中选择默认浏览器') from None
    choices = {'chrome.exe': ('Chrome', 'Google/Chrome/User Data'),
               'msedge.exe': ('Edge', 'Microsoft/Edge/User Data'),
               'brave.exe': ('Brave', 'BraveSoftware/Brave-Browser/User Data'),
               'vivaldi.exe': ('Vivaldi', 'Vivaldi/User Data')}
    match = re.match(r'\s*(?:"([^"]+)"|(\S+))', command)
    exe = Path(os.path.expandvars(next((s for s in match.groups() if s), ''))).name.lower() if match else ''
    if exe not in choices:
        raise ValueError('当前默认浏览器暂不支持登录状态读取；支持 Chrome、Edge、Brave 和 Vivaldi')
    label, relative = choices[exe]
    base = Path(os.environ.get('LOCALAPPDATA') or '') / relative
    override = re.search(r'--user-data-dir(?:=|\s+)(?:"([^"]+)"|(\S+))', command)
    if override:base = Path(os.path.expandvars(override[1] or override[2]))
    try:
        with (base / 'Local State').open('rb') as file:
            raw = file.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:raise ValueError()
        profile = json.loads(raw).get('profile', {}).get('last_used') or 'Default'
    except (OSError, ValueError):
        raise ValueError('请先在默认浏览器打开并登录 RunningHub 官网，再点击读取') from None
    selected = re.search(r'--profile-directory(?:=|\s+)(?:"([^"]+)"|(\S+))', command)
    if selected:profile = selected[1] or selected[2]
    if not isinstance(profile, str) or not re.fullmatch(r'Default|Profile \d+', profile):
        raise ValueError('无法识别默认浏览器当前配置，请使用普通浏览器窗口登录官网')
    folder = base / profile / 'Local Storage' / 'leveldb'
    if not folder.is_dir():
        raise ValueError('请先在默认浏览器登录 RunningHub 官网，再点击读取')
    return label, folder


def default_session(site, stop=None):
    label, folder = default_profile()
    try:token = read_login_key(folder, site, stop)
    except OSError:
        raise ValueError('暂时无法读取默认浏览器登录状态，请刷新官网后重试') from None
    if not token:
        raise ValueError('默认浏览器尚未登录当前站点，请点击“在默认浏览器登录”，登录后返回此处重新读取')
    return dict(site=site, authorization='Bearer ' + token, browser=label)
