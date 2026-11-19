"""finderのテストで使う、しょぼいカレンダーの応答とTSの見出しの偽物。

応答の項目は、NicoJKReferenceFinderTests.csと同じく、実際の応答(2026-11-05取得)から照合に使う項目だけを写した。
#ゾンビさがしてます#02: BS朝日(ChID 18)は10/11 01:00、テレ朝(ChID 6)などは10/10 23:30。
"""

import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nicojk_ass import finder, syoboi, tsinfo  # noqa: E402

JST = timezone(timedelta(hours=9))


def jst(year, month, day, hour=0, minute=0, second=0):
    return int(datetime(year, month, day, hour, minute, second, tzinfo=JST).timestamp())


def jst_text(unix):
    return datetime.fromtimestamp(unix, JST).strftime("%Y-%m-%d %H:%M:%S")


TARGET = jst(2026, 10, 11, 1)
EARLIER = jst(2026, 10, 10, 23, 30)


def program(tid, chid, count, st_time, st_offset=0):
    item = ET.Element("ProgItem")
    end = datetime.strptime(st_time, "%Y-%m-%d %H:%M:%S") + timedelta(minutes=30)
    for name, value in (("TID", tid), ("ChID", chid), ("Count", count), ("StTime", st_time),
                        ("EdTime", end.strftime("%Y-%m-%d %H:%M:%S")), ("StOffset", st_offset), ("Deleted", 0)):
        ET.SubElement(item, name).text = str(value)
    return item


def programs(*items):
    root = ET.Element("ProgLookupResponse")
    ET.SubElement(root, "ProgItems").extend(items)
    ET.SubElement(ET.SubElement(root, "Result"), "Code").text = "200"
    return ET.tostring(root, encoding="unicode")


def title_response(tid, title, short_title=""):
    root = ET.Element("TitleLookupResponse")
    item = ET.SubElement(ET.SubElement(root, "TitleItems"), "TitleItem")
    ET.SubElement(item, "TID").text = str(tid)
    ET.SubElement(item, "Title").text = title
    ET.SubElement(item, "ShortTitle").text = short_title
    ET.SubElement(ET.SubElement(root, "Result"), "Code").text = "200"
    return ET.tostring(root, encoding="unicode")


def change_programs(xml, change):
    root = ET.fromstring(xml)
    for item in root.iter("ProgItem"):
        change(item)
    return ET.tostring(root, encoding="unicode")


def make_ts(sid, start):
    return tsinfo.TsInfo([tsinfo.Program(sid, True, 256, start, 1800)], start)


class Fixture:
    def __init__(self, test):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, "rec")
        self.cache_dir = os.path.join(tmp.name, "cache")
        os.makedirs(self.root)
        self.source = os.path.join(self.root, "#ゾンビさがしてます_02_BS朝日.ts")
        Path(self.source).write_text("対象", encoding="utf-8")
        self.range = programs(program(8028, 18, "2", "2026-10-11 01:00:00"))
        self.episodes = programs(
            program(8028, 67, "2", "2026-10-10 23:30:00"),
            program(8028, 81, "2", "2026-10-10 23:30:00"),
            program(8028, 18, "2", "2026-10-11 01:00:00"),
            program(8028, 6, "2", "2026-10-10 23:30:00"),
            program(8028, 20, "2", "2026-10-12 21:00:00"),
            program(8028, 88, "2", "2026-10-10 23:30:00"),
            program(8028, 276, "2", "2026-10-11 00:00:00"))
        self.title = title_response(8028, "#ゾンビさがしてます")
        self.requests = []
        self.request_times = []
        self.delays = []
        self.now = datetime.now().timestamp()
        self.fetch_duration = 0
        self.logs = []
        self.fetch_failure = None
        self.read_failure = False
        self.recordings = {}
        self.reads = []
        self.channels = [(6, 1064, "テレビ朝日", True), (18, 151, "BS朝日", True), (20, 333, "AT-X", True)]
        self.services = {1064: 5, 151: 151, 333: 333}
        self.finder = self.new_finder()

    def new_finder(self):
        client = syoboi.SyoboiClient(self.cache_dir, self.fetch, lambda: self.now, self.sleep, self.logs.append)
        return finder.Finder(self.cache_dir, client, self.read_ts, self.channels, self.services, self.logs.append)

    def fetch(self, url):
        self.requests.append(url)
        self.request_times.append(self.now)
        self.now += self.fetch_duration
        if self.fetch_failure:
            raise self.fetch_failure
        if "Command=TitleLookup" in url:
            return self.title
        return self.range if "Range=" in url else self.episodes

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.now += seconds

    def read_ts(self, path):
        self.reads.append(path)
        if self.read_failure:
            raise OSError("TS読み込み失敗")
        return self.recordings[path]

    def set_affiliate(self):
        self.channels.append((67, 3088, "メ～テレ", False))
        self.services[3088] = 5
        self.range = programs(program(8028, 67, "2", "2026-10-11 01:00:00"))

    def set_title(self, title, short_title=""):
        self.title = title_response(8028, title, short_title)

    def add_recording(self, name, sid, start):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text("録画", encoding="utf-8")
        self.recordings[path] = make_ts(sid, start)
        return path

    def search_dirs(self):
        return [self.root, os.path.join(self.root, "succeeded"), os.path.join(self.root, "failed")]

    def plan(self, sid=151, start=TARGET):
        return self.finder.find(sid, start, self.search_dirs(), [self.source])

    def find(self, sid=151, start=TARGET):
        """見つかった基準の録画のパス(なければNone)。"""
        reference = self.plan(sid, start)["reference"]
        return reference["path"] if reference else None


class FinderTestCase(unittest.TestCase):
    def setUp(self):
        self.f = Fixture(self)
