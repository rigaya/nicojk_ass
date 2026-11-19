"""しょぼいカレンダーの問い合わせとキャッシュ。

応答はキャッシュのフォルダにURLごとのファイルで保存する(ProgLookupは1日、TitleLookupは30日)。
通信は、並行する別のプロセスとも合わせて10秒以上の間隔を空ける。そのため、キャッシュのフォルダの
ロックファイルをロックしてから待って通信し、最後の通信の時刻をそのファイルに書く。
"""

import contextlib
import hashlib
import os
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET

from . import __version__

HTTP_TIMEOUT_SECONDS = 10
REQUEST_INTERVAL_SECONDS = 10
PROGRAM_CACHE_DAYS = 1
TITLE_CACHE_DAYS = 30
BASE_URL = "https://cal.syoboi.jp/db.php?Command="

# ロックする1バイトの位置。時刻を書く先頭付近と重ならないようにする(Windowsのロックは読み書きも止めるため)。
_LOCK_POSITION = 1 << 20


def default_cache_dir():
    """利用者ごとのキャッシュのフォルダ。Windowsは%LOCALAPPDATA%、Linuxは$XDG_CACHE_HOME(なければ~/.cache)。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "nicojk_ass")


def _http_get(url):
    request = urllib.request.Request(url, headers={"User-Agent": f"nicojk_ass/{__version__}"})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8")


@contextlib.contextmanager
def _locked(path):
    """ファイルをロックして、ファイルオブジェクトを渡す。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+b") as file:
        if sys.platform == "win32":
            import msvcrt
            file.seek(_LOCK_POSITION)
            while True:
                try:
                    msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)  # 10回(約10秒)待って失敗するので繰り返す
                    break
                except OSError:
                    continue
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        try:
            yield file
        finally:
            if sys.platform == "win32":
                import msvcrt
                file.seek(_LOCK_POSITION)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _read_last_request(file):
    file.seek(0)
    try:
        return float(file.read(64).decode("ascii").strip())
    except ValueError:
        return None


def _write_last_request(file, value):
    file.seek(0)
    file.truncate(0)
    file.write(repr(value).encode("ascii"))
    file.flush()


def _write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def check_result(root):
    code = root.find("Result/Code")
    if code is None or (code.text or "").strip() != "200":
        raise OSError("しょぼいカレンダーの応答が正常ではありません")


class SyoboiClient:
    """fetch・clock・sleepは、テストで通信と時計を差し替えるための引数。"""

    def __init__(self, cache_dir, fetch=None, clock=time.time, sleep=time.sleep, log=lambda message: None):
        self.cache_dir = cache_dir
        self.fetch = fetch or _http_get
        self.clock = clock
        self.sleep = sleep
        self.log = log

    def load(self, query, title=False):
        """問い合わせの結果のXML(ElementTreeの根)を返す。失敗したらOSErrorかET.ParseErrorを送出する。"""
        url = BASE_URL + query
        name = hashlib.sha256(url.encode("utf-8")).hexdigest().upper() + ".xml"
        path = os.path.join(self.cache_dir, "syoboi", name)
        days = TITLE_CACHE_DAYS if title else PROGRAM_CACHE_DAYS
        xml = self._read_cache(path, days)
        if xml is not None:
            return ET.fromstring(xml)
        with _locked(os.path.join(self.cache_dir, "syoboi.lock")) as lock:
            # ロックを待つ間に、ほかのプロセスが取得したかもしれない
            xml = self._read_cache(path, days)
            if xml is not None:
                return ET.fromstring(xml)
            last = _read_last_request(lock)
            if last is not None:
                wait = min(last + REQUEST_INTERVAL_SECONDS - self.clock(), REQUEST_INTERVAL_SECONDS)
                if wait > 0:
                    self.log("別局実況: しょぼいカレンダーへの問い合わせ間隔を待機")
                    self.sleep(wait)
            _write_last_request(lock, self.clock())
            xml = self.fetch(url)
            root = ET.fromstring(xml)
            check_result(root)
            _write_atomic(path, xml)
            return root

    def _read_cache(self, path, days):
        try:
            if self.clock() - os.path.getmtime(path) >= days * 86400:
                return None
            with open(path, "r", encoding="utf-8") as file:
                return file.read()
        except OSError:
            return None
