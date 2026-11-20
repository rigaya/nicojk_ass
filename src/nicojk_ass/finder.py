"""先行放送・同時刻の放送の特定と、その録画を探す処理(Amatsukazeのサーバーにあった
NicoJKReferenceFinderの移植)。結果はplanのJSONの形の辞書で返す。"""

import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from urllib.parse import quote
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from . import stations, tsinfo
from .syoboi import SyoboiClient

PLAN_SCHEMA = 1
SEARCH_RANGE_MINUTES = 30
TARGET_TOLERANCE_SECONDS = 10 * 60
REFERENCE_TOLERANCE_SECONDS = 5 * 60
MAX_TS_FILES = 78
FIRST_PASS_TS_FILES = 3
JST = timezone(timedelta(hours=9))
_PREFIX = "別局実況: "


def parse_jst(text):
    """しょぼいカレンダーの「yyyy-MM-dd HH:mm:ss」(JST)をUNIX秒にする。"""
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST).timestamp())


def _format_range(unix):
    return datetime.fromtimestamp(unix, JST).strftime("%Y%m%d_%H%M%S")


def _format_jst(unix):
    return datetime.fromtimestamp(unix, JST).strftime("%Y-%m-%d %H:%M:%S")


def normalize_name(name):
    """幅・大小文字をそろえ、文字と数字以外(空白、記号)を除く。"""
    folded = unicodedata.normalize("NFKC", name).lower()
    return "".join(c for c in folded if c.isalpha() or c.isdigit())


def get_name_keys(*titles):
    """作品名の鍵を作る。NFKC・小文字にして記号で区切り、2文字以上で数字だけでない区間の先頭5文字。"""
    keys = []
    for title in titles:
        folded = unicodedata.normalize("NFKC", title).lower()
        for part in re.split(r"[^\w]+|_+", folded):
            if len(part) >= 2 and not part.isdigit() and part[:5] not in keys:
                keys.append(part[:5])
    return keys


def _item_start(item):
    # StTimeは変更後の開始時刻なので、StOffsetは加算しない。
    return parse_jst(item.findtext("StTime"))


def _int(item, name):
    text = item.findtext(name)
    return int(text) if text not in (None, "") else None


def _deleted(item):
    return _int(item, "Deleted") == 1


def _ts_cache_path(cache_dir, path, stat):
    key = f"{os.path.abspath(path)}\n{stat.st_size}\n{stat.st_mtime_ns}"
    return os.path.join(cache_dir, "tsinfo", hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")


class Finder:
    """read_tsとclient(SyoboiClient)は、テストで差し替えるための引数。
    read_tsは(path)を受け取り、TsInfoを返す。"""

    def __init__(self, cache_dir, client=None, read_ts=None, channels=None, comment_services=None,
                 log=lambda message: print(message, file=sys.stderr)):
        self.cache_dir = cache_dir
        self.log_func = log
        self.client = client or SyoboiClient(cache_dir, log=self.log)
        self.read_ts = read_ts
        self.channels = channels if channels is not None else stations.read_syoboi_channels()
        self.comment_services = (comment_services if comment_services is not None
                                 else stations.read_comment_services())
        self._ts_cache = {}
        self.plan = None

    def log(self, message):
        self.plan["log"].append(message)
        self.log_func(message)

    def _cached_info(self, path, stat):
        """読み込み済みのTsInfo(メモリ、なければキャッシュのフォルダのJSON)。なければNone。"""
        key = (path, stat.st_size, stat.st_mtime_ns)
        if key in self._ts_cache:
            return self._ts_cache[key]
        try:
            with open(_ts_cache_path(self.cache_dir, path, stat), "r", encoding="utf-8") as file:
                info = tsinfo.TsInfo.from_dict(json.load(file)["info"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        self._ts_cache[key] = info
        return info

    def _read_info(self, path, stat):
        """TSを読んでキャッシュに保存する。"""
        info = (self.read_ts or tsinfo.read_ts_info)(path)
        self._ts_cache[(path, stat.st_size, stat.st_mtime_ns)] = info
        cache = _ts_cache_path(self.cache_dir, path, stat)
        try:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(cache), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump({"path": os.path.abspath(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                           "info": info.to_dict()}, file, ensure_ascii=False)
            os.replace(tmp, cache)
        except OSError:
            pass  # キャッシュに書けなくても探索は続ける
        return info

    def find(self, sid, program_start, search_dirs, exclude=()):
        """放送を特定して基準の候補を作り、録画を探す。planの辞書を返す。
        失敗してもplan["error"]に理由を書いて返す(例外は送出しない)。
        plan["reference"]に見つかった録画のパスとServiceIdが入る(なければNone)。"""
        self.plan = {"schema": PLAN_SCHEMA, "program": None,
                     "target": {"sid": sid, "jk": None, "name": None, "comments": "unknown"},
                     "candidates": [], "probed": [], "reference": None, "error": None, "log": []}
        try:
            self._find(sid, program_start, search_dirs, exclude)
        except Exception as error:  # 通信・応答の解析・TSの読み込みなど、どこで失敗しても戻る
            self.plan["error"] = str(error)
            self.log(_PREFIX + "検索に失敗したため"
                     + ("実況を付けません" if self.plan["target"]["comments"] == "unrelated" else "対象局のコメントを使います")
                     + f" ({error})")
        return self.plan

    def _find(self, sid, start, search_dirs, exclude):
        plan, channels, services = self.plan, self.channels, self.comment_services
        plan["target"]["jk"] = services.get(sid)
        recorded = next((c for c in channels if c[1] == sid), None)
        if start is None or start <= 946684800:  # 2000-01-01
            self.log(_PREFIX + "番組開始時刻がないため検索しません")
            return
        if recorded is None:
            self.log(_PREFIX + "対象局が局対応表にありません")
            return
        channel, _, name, reference = recorded
        plan["target"]["name"] = name
        target = self._find_broadcast(channel, start)
        if target is None:
            self.log(_PREFIX + "開始時刻が10分以内の放送がありません")
            return
        tid = _int(target, "TID")
        count = target.findtext("Count")
        plan["program"] = {"tid": tid, "count": count, "title": None, "start": _item_start(target),
                           "end": parse_jst(target.findtext("EdTime"))}
        if not count:
            self.log(_PREFIX + "話数がないため検索しません")
            return
        broadcasts = self.client.load(f"ProgLookup&TID={tid}&Count={quote(count, safe='')}")
        if reference:
            plan["target"]["comments"] = "ok"
        else:
            # キー局: 基準1の局のうち、録画した局と同じjk番号を持つ局。
            target_jk = services.get(sid)
            key_channels = {c[0] for c in channels if c[3] and services.get(c[1]) == target_jk}
            simulcast = any(
                not _deleted(item) and _int(item, "ChID") in key_channels
                and abs(_item_start(item) - _item_start(target)) <= REFERENCE_TOLERANCE_SECONDS
                for item in broadcasts.iter("ProgItem"))
            plan["target"]["comments"] = "ok" if simulcast else "unrelated"
            self.log(_PREFIX + ("キー局の同じ回と開始差が5分以内の同時ネットです" if simulcast else
                                "キー局の同じ回が開始差5分以内にないため遅れネット・独自枠と判断しました"))
        candidates = self._select_candidates(broadcasts, target, channel, tid, count, sid)
        for item in candidates:
            ch = next(c for c in channels if c[0] == _int(item, "ChID") and c[3])
            plan["candidates"].append({
                "sid": ch[1], "jk": services.get(ch[1]), "name": ch[2],
                "start": _item_start(item), "end": parse_jst(item.findtext("EdTime")),
                "same_time": abs(_item_start(item) - _item_start(target)) <= REFERENCE_TOLERANCE_SECONDS,
                "recording": None})
        if not candidates:
            self.log(_PREFIX + "実況のある別局の先行放送・同時刻の放送がありません")
            return
        title_xml = self.client.load(f"TitleLookup&TID={tid}", True)
        title_item = next(i for i in title_xml.iter("TitleItem") if _int(i, "TID") == tid)
        title = title_item.findtext("Title") or ""
        plan["program"]["title"] = title
        names = get_name_keys(title, title_item.findtext("ShortTitle") or "")
        self.log(_PREFIX + f"{title} 第{count}話を特定しました")
        self._find_recording(names, search_dirs, exclude)

    def _find_broadcast(self, channel, start):
        span = f"{_format_range(start - SEARCH_RANGE_MINUTES * 60)}-{_format_range(start + SEARCH_RANGE_MINUTES * 60)}"
        nearby = self.client.load(f"ProgLookup&ChID={channel}&Range={span}")
        target, nearest = None, float("inf")
        for item in nearby.iter("ProgItem"):
            if _deleted(item) or _int(item, "ChID") != channel:
                continue
            difference = abs(_item_start(item) - start)
            if difference <= TARGET_TOLERANCE_SECONDS and difference < nearest:
                nearest, target = difference, item
        return target

    def _select_candidates(self, broadcasts, target, channel, tid, count, sid):
        target_start = _item_start(target)
        target_jk = self.comment_services.get(sid)
        candidates = []
        for item in broadcasts.iter("ProgItem"):
            if _deleted(item) or _int(item, "ChID") == channel or _int(item, "TID") != tid \
                    or item.findtext("Count") != count:
                continue
            candidate = next((c for c in self.channels if c[0] == _int(item, "ChID") and c[3]), None)
            if candidate is None:
                continue
            difference = _item_start(item) - target_start
            # 同時刻に同じjk番号を使うと、同じコメントを二重に付けることになる。
            if difference < -REFERENCE_TOLERANCE_SECONDS or (
                    abs(difference) <= REFERENCE_TOLERANCE_SECONDS
                    and self.comment_services.get(candidate[1]) != target_jk):
                candidates.append(item)
        candidates.sort(key=_item_start)  # 安定ソート(応答の順を保つ)
        return candidates

    def _list_files(self, search_dirs, exclude):
        excluded = {os.path.normcase(os.path.abspath(p)) for p in exclude}
        seen, files = set(), []
        for directory in search_dirs:
            try:
                names = os.listdir(directory)
            except OSError:
                continue
            for name in names:
                path = os.path.join(directory, name)
                if os.path.splitext(name)[1].lower() not in (".ts", ".m2ts") or not os.path.isfile(path):
                    continue
                key = os.path.normcase(os.path.abspath(path))
                if key in excluded or key in seen:
                    continue
                seen.add(key)
                files.append(path)
        return files

    def _find_recording(self, names, search_dirs, exclude):
        plan = self.plan
        files = [p for p in self._list_files(search_dirs, exclude)
                 if any(key in normalize_name(os.path.splitext(os.path.basename(p))[0]) for key in names)]
        files.sort()
        self.log(_PREFIX + f"作品名の鍵が一致する録画 {len(files)}ファイル")
        reads = 0
        probed = set()
        for limit in (FIRST_PASS_TS_FILES, None):
            for index, candidate in enumerate(plan["candidates"]):
                self.log(_PREFIX + f"候補 {candidate['name']} {_format_jst(candidate['start'])}")
                ordered = sorted(files, key=lambda p: abs(os.path.getmtime(p) - candidate["end"]))
                for path in (ordered if limit is None else ordered[:limit]):
                    try:
                        stat = os.stat(path)
                    except OSError:
                        continue
                    info = self._cached_info(path, stat)
                    if info is None:
                        if reads >= MAX_TS_FILES:
                            self.log(_PREFIX + f"TS読み込みの上限{MAX_TS_FILES}ファイルに達しました")
                            return
                        reads += 1
                        try:
                            info = self._read_info(path, stat)
                        except Exception as error:
                            self.log(_PREFIX + f"TS情報の読み込みに失敗: {path} ({error})")
                            if (path, index) not in probed:
                                probed.add((path, index))
                                plan["probed"].append({"path": path, "sids": [], "eit_start": None,
                                                       "result": "read_error"})
                            continue
                    start = info.start_of(candidate["sid"])
                    sids = info.service_ids
                    if candidate["sid"] not in sids:
                        result = "sid_mismatch"
                    elif start is None or abs(start - candidate["start"]) > REFERENCE_TOLERANCE_SECONDS:
                        result = "time_mismatch"
                    else:
                        result = "match"
                    if (path, index) not in probed:
                        probed.add((path, index))
                        plan["probed"].append({"path": path, "sids": sids, "eit_start": start, "result": result})
                    if result == "match":
                        candidate["recording"] = path
                        plan["reference"] = {"path": path, "sid": candidate["sid"], "candidate": index}
                        self.log(_PREFIX + f"基準録画 {path}")
                        return
                self.log(_PREFIX + "この候補の録画が見つかりません")
        self.log(_PREFIX + "利用できる先行放送・同時刻の放送の録画がありません")
