"""コメントの取得、ファイルの読み込み、dateによる配置、時刻の読み取り。"""

import math
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation

from . import __version__
from .net import ssl_context

DEFAULT_API_BASE = "https://jikkyo.tsukumijima.net"
MAX_FETCH_SECONDS = 3 * 24 * 3600

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_CHANNEL_NOT_FOUND = 100


class NicoJKExit(Exception):
    """処理を打ち切って、指定の終了コードで終わるための例外。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def log(message):
    print(f"[nicojk_ass] {message}", file=sys.stderr)


# ------------------------------------------------------------------ #
# 時刻の読み取り
# ------------------------------------------------------------------ #

_COMPACT_RE = re.compile(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})")
_ISO_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})?")


def _days_from_civil(y, m, d):
    y -= m <= 2
    era = y // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def parse_time(value):
    """UNIX秒(小数可)、ISO 8601(タイムゾーン省略時はJST)、14桁のJST(YYYYMMDDhhmmss)を
    UNIX秒に直す。整数ならint、小数ならDecimalを返す。不正ならValueError。"""
    value = value.strip()
    match = _COMPACT_RE.fullmatch(value)
    fraction = Decimal(0)
    zone = 9 * 3600
    if match is None:
        match = _ISO_RE.fullmatch(value)
        if match is not None:
            if match.group(7):
                fraction = Decimal(match.group(7))
            if match.group(8):
                z = match.group(8)
                zone = 0
                if z != "Z":
                    zh, zm = int(z[1:3]), int(z[4:6])
                    if zh > 23 or zm > 59:
                        raise ValueError("時差が不正です")
                    zone = (zh * 3600 + zm * 60) * (-1 if z[0] == "-" else 1)
    if match is not None:
        y, mo, d, h, mi, s = (int(match.group(i)) for i in range(1, 7))
        leap = y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
        days = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        if y < 1 or not 1 <= mo <= 12 or not 1 <= d <= days[mo - 1] or h > 23 or mi > 59 or s > 59:
            raise ValueError("日時が不正です")
        seconds = _days_from_civil(y, mo, d) * 86400 + h * 3600 + mi * 60 + s - zone
        return seconds if fraction == 0 else Decimal(seconds) + fraction
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"時刻が不正です: {value}") from None
    if not number.is_finite() or abs(number) >= Decimal(10) ** 12:
        raise ValueError(f"時刻が不正です: {value}")
    return int(number) if number == number.to_integral_value() else number


# ------------------------------------------------------------------ #
# コメントXML取得
# ------------------------------------------------------------------ #

def fetch_comments_xml(channel, starttime, endtime, api_base):
    """tsukumijima 過去ログ API からXML文字列を取得する。
    期間が MAX_FETCH_SECONDS を超える場合は複数回に分割してリクエストする。"""
    segments = []
    cur = starttime

    while cur < endtime:
        seg_end = min(cur + MAX_FETCH_SECONDS, endtime)
        url = (f"{api_base}/api/kakolog/{channel}"
               f"?starttime={cur}&endtime={seg_end}&format=xml")
        log(f"GET {url}")
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": f"nicojk_ass/{__version__}",
            })
            with urllib.request.urlopen(req, timeout=60, context=ssl_context()) as resp:
                data = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                log(f"チャンネル '{channel}' は存在しません (HTTP {e.code})")
                raise NicoJKExit(EXIT_CHANNEL_NOT_FOUND)
            log(f"HTTPエラー: {e}")
            raise NicoJKExit(EXIT_ERROR)
        except urllib.error.URLError as e:
            log(f"接続エラー: {e}")
            raise NicoJKExit(EXIT_ERROR)

        segments.append(data)
        cur = seg_end

    return merge_xml_segments(segments)


def merge_xml_segments(segments):
    """複数のXMLセグメントを1つのXML文字列にマージする。"""
    if len(segments) == 1:
        return segments[0]
    root = ET.fromstring(segments[0])
    for seg in segments[1:]:
        try:
            other = ET.fromstring(seg)
        except ET.ParseError:
            continue
        for chat in other.findall("chat"):
            root.append(chat)
    return ET.tostring(root, encoding="unicode")


def comment_time(chat):
    """dateとdate_usecからコメントの絶対時刻を精度を落とさずに得る。"""
    date = chat.get("date", "")
    usec = chat.get("date_usec", "0")
    if not re.fullmatch(r"-?\d+", date) or not re.fullmatch(r"\d+", usec):
        raise ValueError("コメントのdate/date_usecが不正です")
    microseconds = int(usec)
    if not 0 <= microseconds < 1000000:
        raise ValueError("コメントのdate_usecが範囲外です")
    return Decimal(date) + Decimal(microseconds) / Decimal(1000000)


def rebase_vpos(xml_str, starttime):
    """XMLのvposを番組開始時刻(starttime)基準の相対値に変換する。

    tsukumijima APIのvposは実況スレッド開始（4:00 JST）からのセンチ秒。
    danmaku2assはvposをそのまま出力時刻にするため、相対化が必要。
    ただしvposは投稿者側の再生位置で、端末ごとに数秒ずれるうえ、0始まりや
    別の基準で振られた外れ値も混じる。そこでvposは使わず、サーバーの受信時刻である
    date/date_usec からstarttime基準のvposを振り直す(ファイル入力の経路と同じ)。"""
    root = ET.fromstring(xml_str)
    chats = root.findall("chat")
    if not chats:
        return xml_str

    start = Decimal(str(starttime))
    removed = 0
    invalid = 0
    for chat in list(chats):
        try:
            stamp = comment_time(chat)
        except ValueError:
            # 絶対時刻の分からないコメントは位置を決められないので捨てる
            root.remove(chat)
            invalid += 1
            continue
        if stamp < start:
            root.remove(chat)
            removed += 1
        else:
            chat.set("vpos", str(round((stamp - start) * 100)))

    if removed > 0:
        log(f"vpos相対化: {removed}件の番組開始前コメントを除外")
    if invalid > 0:
        log(f"vpos相対化: date/date_usecが不正なコメントを{invalid}件除外")

    return ET.tostring(root, encoding="unicode")


def fetch_api_comments(channel, starttime, endtime, api_base):
    """APIから[starttime, endtime]のコメントを取得し、starttime基準のvposにして文字列で返す。
    取得は整数秒の範囲で行い、小数の指定のときだけ端数を追加で調整する。"""
    fetch_start, fetch_end = math.floor(starttime), math.ceil(endtime)
    xml_str = fetch_comments_xml(channel, fetch_start, fetch_end, api_base)
    log(f"取得コメント数: {count_chats(xml_str)}件")
    xml_str = rebase_vpos(xml_str, fetch_start)
    if starttime != fetch_start or endtime != fetch_end:
        root = ET.fromstring(xml_str)
        shift = (Decimal(str(starttime)) - fetch_start) * 100
        duration = (Decimal(str(endtime)) - Decimal(str(starttime))) * 100
        for chat in list(root.findall("chat")):
            if chat.get("vpos") is None:
                continue
            position = Decimal(chat.get("vpos")) - shift
            if position < 0 or position > duration:
                root.remove(chat)
            else:
                chat.set("vpos", str(round(position)))
        xml_str = ET.tostring(root, encoding="unicode")
    return xml_str


def fetch_target_comments(channel, starttime, endtime, api_base):
    """統合する対象の局のコメントを、整数秒の範囲で取得してstarttime基準のvposにする。"""
    xml_str = fetch_comments_xml(channel, math.floor(starttime), math.ceil(endtime), api_base)
    return rebase_vpos(xml_str, starttime)


def count_chats(xml_str):
    try:
        return len(ET.fromstring(xml_str).findall("chat"))
    except ET.ParseError:
        return 0


# ------------------------------------------------------------------ #
# コメントファイル
# ------------------------------------------------------------------ #

def read_comments_file(path):
    """XMLまたは見出しコメント付きのNicoJKログを読み込む。"""
    with open(path, "r", encoding="utf-8-sig") as source:
        contents = source.read()
    if str(path).lower().endswith(".jkl"):
        # ログの各行をpacketにまとめ、属性や本文のエスケープもXMLとして検証する。
        root = ET.fromstring("<packet>" + contents + "</packet>")
    else:
        root = ET.fromstring(contents)
    if root.tag != "packet":
        return ET.Element("packet")
    for chat in root.findall("chat"):
        comment_time(chat)
    return root


def inspect_comments(root, starttime, endtime):
    """全件数・最初の時刻・録画範囲内件数を返す。"""
    times = [comment_time(chat) for chat in root.findall("chat")]
    start, end = Decimal(str(starttime)), Decimal(str(endtime))
    return (len(times), min(times) if times else Decimal(0),
            sum(start <= stamp <= end for stamp in times))


def file_reference(program, first, count, overlap):
    """ファイルのコメントの時刻の基準を決める。録画の範囲に半数以上のコメントが入っていれば
    番組開始、そうでなければ最初のコメントの時刻を基準にする。"""
    return program if count and overlap >= (count + 1) // 2 else first


def rebase_file_vpos(root, starttime, endtime):
    """ファイルのvposを信用せず、絶対時刻から取得範囲内だけを振り直す。"""
    start, end = Decimal(str(starttime)), Decimal(str(endtime))
    removed = 0
    for number, chat in enumerate(list(root.findall("chat")), 1):
        stamp = comment_time(chat)
        if stamp < start or stamp > end:
            root.remove(chat)
            removed += 1
        else:
            chat.set("vpos", str(round((stamp - start) * 100)))
            # NicoJKログにはnoがない場合もあるが、変換器では必須なので元の順序で補う。
            if chat.get("no") is None:
                chat.set("no", str(number))
    if removed:
        log(f"範囲外のコメントを{removed}件除外")
    if not root.findall("chat"):
        log("警告: 範囲内のコメントは0件です")
    return ET.tostring(root, encoding="unicode")
