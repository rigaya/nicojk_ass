"""syoboi.pyのキャッシュと、プロセスをまたぐ通信間隔のテスト(通信と時計は差し替える)。"""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nicojk_ass import syoboi  # noqa: E402

OK = "<R><Result><Code>200</Code></Result><V>{}</V></R>"


class SyoboiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = os.path.join(tmp.name, "cache")
        self.now = time.time()
        self.urls = []
        self.delays = []

    def client(self, fetch=None):
        def default_fetch(url):
            self.urls.append(url)
            return OK.format(url[-5:])

        def sleep(seconds):
            self.delays.append(seconds)
            self.now += seconds

        return syoboi.SyoboiClient(self.cache, fetch or default_fetch, lambda: self.now, sleep)

    def test_別のプロセスとも十秒の間隔を空ける(self):
        first, second = self.client(), self.client()  # プロセスが2つある想定(状態を共有するのはフォルダだけ)
        first.load("ProgLookup&TID=1")
        self.now += 3
        second.load("ProgLookup&TID=2")
        self.assertEqual(self.delays, [7])
        self.assertEqual(len(self.urls), 2)

    def test_十秒以上経っていれば待たない(self):
        first, second = self.client(), self.client()
        first.load("ProgLookup&TID=1")
        self.now += 10
        second.load("ProgLookup&TID=2")
        self.assertEqual(self.delays, [])

    def test_最初の通信は待たない(self):
        self.client().load("ProgLookup&TID=1")
        self.assertEqual(self.delays, [])

    def test_最後の通信の時刻はロックファイルに残る(self):
        self.client().load("ProgLookup&TID=1")
        with open(os.path.join(self.cache, "syoboi.lock"), "rb") as file:
            self.assertEqual(float(file.read().decode()), self.now)

    def test_時計が戻っていても十秒より長くは待たない(self):
        self.client().load("ProgLookup&TID=1")
        self.now -= 3600
        self.client().load("ProgLookup&TID=2")
        self.assertEqual(self.delays, [10])

    def test_キャッシュにあれば通信も待ちもしない(self):
        self.client().load("ProgLookup&TID=1")
        self.client().load("ProgLookup&TID=1")
        self.assertEqual(len(self.urls), 1)
        self.assertEqual(self.delays, [])

    def test_キャッシュの期限(self):
        client = self.client()
        client.load("ProgLookup&TID=1")
        client.load("TitleLookup&TID=1", True)
        files = {name: os.path.join(self.cache, "syoboi", name) for name in os.listdir(os.path.join(self.cache, "syoboi"))}
        self.assertEqual(len(files), 2)
        for path in files.values():
            os.utime(path, (self.now - 2 * 86400,) * 2)  # 2日前
        self.now += 20
        client.load("ProgLookup&TID=1")           # 1日を過ぎたので取り直す
        client.load("TitleLookup&TID=1", True)    # 30日以内なのでキャッシュ
        self.assertEqual(len(self.urls), 3)
        self.assertTrue(self.urls[-1].endswith("ProgLookup&TID=1"))

    def test_応答が正常でなければ例外にしてキャッシュしない(self):
        client = self.client(lambda url: "<R><Result><Code>500</Code></Result></R>")
        with self.assertRaises(OSError):
            client.load("ProgLookup&TID=1")
        self.assertFalse(os.path.exists(os.path.join(self.cache, "syoboi")) and os.listdir(os.path.join(self.cache, "syoboi")))

    def test_一時ファイルを残さない(self):
        self.client().load("ProgLookup&TID=1")
        names = os.listdir(os.path.join(self.cache, "syoboi"))
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith(".xml"))

    def test_同時に動いても通信は重ならない(self):
        state = {"inflight": 0, "max": 0}

        def fetch(url):
            state["inflight"] += 1
            state["max"] = max(state["max"], state["inflight"])
            time.sleep(0.05)
            state["inflight"] -= 1
            return OK.format(url[-5:])

        clients = [syoboi.SyoboiClient(self.cache, fetch, time.time, lambda s: None) for _ in range(4)]
        threads = [threading.Thread(target=c.load, args=(f"ProgLookup&TID={i}",)) for i, c in enumerate(clients)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(state["max"], 1)

    def test_既定のキャッシュのフォルダ(self):
        with mock.patch.object(syoboi.sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {"XDG_CACHE_HOME": "/x/cache"}):
            self.assertEqual(syoboi.default_cache_dir(), os.path.join("/x/cache", "nicojk_ass"))
        env = {k: v for k, v in os.environ.items() if k != "XDG_CACHE_HOME"}
        with mock.patch.object(syoboi.sys, "platform", "linux"), mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(syoboi.default_cache_dir(), os.path.join(os.path.expanduser("~"), ".cache", "nicojk_ass"))
        with mock.patch.object(syoboi.sys, "platform", "win32"), \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": "C:\\Users\\a\\AppData\\Local"}):
            self.assertEqual(syoboi.default_cache_dir(), os.path.join("C:\\Users\\a\\AppData\\Local", "nicojk_ass"))

    def test_User_Agentとタイムアウト(self):
        seen = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return "応答".encode("utf-8")

        def urlopen(request, timeout=None, context=None):
            seen["agent"] = request.get_header("User-agent")
            seen["timeout"] = timeout
            seen["url"] = request.full_url
            return Response()

        with mock.patch.object(syoboi.urllib.request, "urlopen", urlopen):
            self.assertEqual(syoboi._http_get("https://example.invalid/x"), "応答")
        self.assertRegex(seen["agent"], r"^nicojk_ass/\d")
        self.assertEqual(seen["timeout"], 10)


if __name__ == "__main__":
    unittest.main()
