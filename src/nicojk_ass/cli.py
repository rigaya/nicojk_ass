"""コマンドラインの入口。サブコマンドはmake、plan、align。"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation

from . import __version__, align_comment, decoder, nicojk_log, stations, tsinfo
from .comments import (DEFAULT_API_BASE, EXIT_CHANNEL_NOT_FOUND, EXIT_ERROR, EXIT_SUCCESS,
                       NicoJKExit, count_chats, fetch_api_comments, fetch_target_comments,
                       file_reference, format_server_comments, inspect_comments, log, parse_time,
                       read_comments_file, rebase_file_vpos)
from .finder import Finder
from .render import (DEFAULT_DURATION, DEFAULT_FONT, DEFAULT_OPACITY, default_fontsize,
                     fixup_ass, run_danmaku2ass)
from .syoboi import SyoboiClient, default_cache_dir
from .timemap import apply_timemap, compose_timemaps, merge_comments, read_timemap

REPORT_SCHEMA = 1


# ------------------------------------------------------------------ #
# 引数
# ------------------------------------------------------------------ #

def _time_arg(value):
    try:
        return parse_time(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error))


def _offset_arg(value):
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("specify a finite number of seconds")
    if not number.is_finite() or abs(number) >= Decimal(10) ** 12:
        raise argparse.ArgumentTypeError("specify a finite number of seconds")
    return number


def _channel_arg(value):
    text = value[2:] if value.startswith("jk") else value
    if not text.isdigit() or int(text) <= 0:
        raise argparse.ArgumentTypeError("a live-comment channel must be jkN, where N is a positive integer")
    return int(text)


def _decoder_arg(value):
    try:
        return decoder.parse_decoder(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error))


def read_spans(path):
    """本編の区間のJSONを読んで[(開始, 終了), ...]を返す。不正ならValueError。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("spans"), list):
        raise ValueError("spans is missing")
    spans = []
    for item in data["spans"]:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(f"a span must be [start, end]: {item!r}")
        start, end = item
        if isinstance(start, bool) or isinstance(end, bool) \
                or not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            raise ValueError(f"a span must be numeric: {item!r}")
        if not (start == start and end == end) or abs(start) == float("inf") or abs(end) == float("inf"):
            raise ValueError(f"a span must be finite: {item!r}")
        if not start < end:
            raise ValueError(f"a span start must be before its end: {item!r}")
        spans.append((float(start), float(end)))
    return spans


def _output_arg(value):
    match = re.fullmatch(r"(\d+)x(\d+)=(.+)", value, re.DOTALL)
    if not match or int(match.group(1)) <= 0 or int(match.group(2)) <= 0:
        raise argparse.ArgumentTypeError("--output must be WxH=PATH")
    return int(match.group(1)), int(match.group(2)), match.group(3)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="nicojk_ass", description=f"ニコニコ実況コメントのASS生成ツール (版 {__version__})")
    parser.add_argument("--version", action="version", version=f"nicojk_ass {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    make = sub.add_parser("make", help="コメントを取得してASSを生成する")
    target = make.add_argument_group("対象")
    target.add_argument("--ts", help="対象の録画(TS)。先頭と末尾の時刻を見出しから求める。--wavがなければデコードする")
    target.add_argument("--sid", type=int, help="対象の局のServiceId(ch_sid.txtでjk番号を引く)")
    target.add_argument("--channel", type=_channel_arg, help="対象の実況チャンネル(例: jk1)。--sidより優先")
    target.add_argument("--start", type=_time_arg, help="対象の録画の先頭の時刻")
    target.add_argument("--end", type=_time_arg, help="対象の録画の末尾の時刻")
    target.add_argument("--program-start", type=_time_arg, help="録画した放送の開始時刻(省略時は--start)")
    target.add_argument("--program-title", help="録画した番組名(--altで時刻から特定できない場合に使う)")
    target.add_argument("--wav", help="対象の音声(PCMのWAV)。--ref-wavと一緒に使う")
    target.add_argument("--spans", help="本編の区間のJSON({\"spans\": [[開始, 終了], ...]}、対象の時刻での秒)。方式Dで使う")
    target.add_argument("--prefer-nicojk-log", action="store_true",
                        help="nicojk_ass.tomlに設定したNicoJKログを過去ログAPIより優先する")
    target.add_argument("--comments-file", help="ローカルのコメントファイル(XML/JKL)")
    target.add_argument("--comments-start", type=_time_arg,
                        help="コメントの時刻の基準(番組開始に当たる時刻。省略時は--program-start)")
    target.add_argument("--comments-offset", type=_offset_arg, default=Decimal(0),
                        help="取得範囲を後ろへずらす秒数(取得範囲の始まりから引く)")
    target.add_argument("--comments-file-auto", action="store_true",
                        help="--comments-fileは自動で見つけた同名ファイル。範囲内にコメントがなければ使わず通常の取得へ進む")

    ref = make.add_argument_group("基準")
    ref.add_argument("--ref-sid", type=int, help="基準の局のServiceId")
    ref.add_argument("--ref-channel", type=_channel_arg, help="基準の実況チャンネル。--ref-sidより優先")
    ref.add_argument("--ref-start", type=_time_arg, help="基準の録画の先頭の時刻")
    ref.add_argument("--ref-end", type=_time_arg, help="基準の録画の末尾の時刻")
    ref.add_argument("--ref-comments-file", help="基準のコメントファイル(XML/JKL)")
    ref.add_argument("--alt", choices=("replace", "merge"),
                     help="同じ回の別の局の放送(先・同時刻・あと)の録画を--search-dirから探して基準にする"
                          "(replaceは基準のコメントだけ、mergeは対象の局のコメントと統合する)")
    ref.add_argument("--search-dir", action="append", default=[], metavar="DIR",
                     help="--altで録画を探すフォルダ(繰り返し可。サブフォルダは見ない)")
    ref.add_argument("--exclude", action="append", default=[], metavar="FILE",
                     help="--altで探す対象から除くファイル(対象の録画自身。繰り返し可)")
    ref.add_argument("--ref-ts", help="基準の録画(TS)。デコードして音声照合する")
    ref.add_argument("--mode", choices=("replace", "merge"),
                     help="replaceは基準のコメントだけ、mergeは対象の局のコメントと統合する(既定はmerge)")
    ref.add_argument("--timemap", help="基準から対象への時刻対応表(JSON)")
    ref.add_argument("--ref-wav", help="基準の音声(PCMのWAV)。対象の音声と照合して対応表を作る")

    out = make.add_argument_group("出力")
    out.add_argument("--output", action="append", type=_output_arg, metavar="WxH=PATH",
                     help="解像度ごとのASSの出力先(繰り返し可)")
    out.add_argument("--comments-output", metavar="PATH",
                     help="統合などの結果を、ニコニコ実況の過去ログと同じXMLで書き出す。"
                          "他のアプリはdateとdate_usecを対象の録画の時刻として読む。"
                          "これだけを指定したときはASSを作らない")
    out.add_argument("--font", default=DEFAULT_FONT, help=f"フォント名 (既定: {DEFAULT_FONT!r})")
    out.add_argument("--fontsize", type=int, help="フォントサイズ(省略時は解像度から自動算出)")
    out.add_argument("--display-seconds", type=float, default=DEFAULT_DURATION,
                     help=f"スクロールコメントの表示秒数 (既定: {DEFAULT_DURATION})")
    out.add_argument("--opacity", type=float, default=DEFAULT_OPACITY,
                     help=f"不透明度 0.0〜1.0 (既定: {DEFAULT_OPACITY})")
    out.add_argument("--report", help="使った取得元や件数などをJSONで書き出すファイル")
    out.add_argument("--mark-reference-color", metavar="COLOR",
                     help="統合で、基準の局のコメントにこの色を足す(調査用)")

    make.add_argument("--api-url", default=DEFAULT_API_BASE,
                      help=f"過去ログAPIのベースURL (既定: {DEFAULT_API_BASE})")
    make.add_argument("--decoder", type=_decoder_arg, metavar="KIND=PATH",
                      help="録画の音声をデコードするプログラム。"
                           "amatsukaze=<AmatsukazeCLI>かffmpeg[=<パス>]。--tsで省略したときはffmpeg")
    make.add_argument("--cache-dir", help="しょぼいカレンダーの応答とTSの見出しのキャッシュの置き場所"
                                          "(既定は利用者ごとのキャッシュのフォルダ)")
    make.add_argument("--work-dir", help="デコードしたWAVの置き場所(既定はOSの一時フォルダに作り、終わったら消す)")
    make.add_argument("--keep-work", action="store_true", help="デコードしたWAVを残す(調査用)")

    plan = sub.add_parser("plan", help="別の局の録画を探す処理だけを行い、結果をJSONで出す(デコードと照合はしない)")
    plan.add_argument("--sid", type=int, help="対象の局のServiceId")
    plan.add_argument("--channel", type=_channel_arg, help="対象の実況チャンネル(放送の特定にはServiceIdが要る)")
    plan.add_argument("--start", required=True, type=_time_arg, help="対象の録画の先頭の時刻")
    plan.add_argument("--end", type=_time_arg, help="対象の録画の末尾の時刻")
    plan.add_argument("--program-start", type=_time_arg, help="録画した放送の開始時刻(省略時は--start)")
    plan.add_argument("--program-title", help="録画した番組名(時刻から特定できない場合に使う)")
    plan.add_argument("--search-dir", action="append", default=[], metavar="DIR", help="録画を探すフォルダ(繰り返し可)")
    plan.add_argument("--exclude", action="append", default=[], metavar="FILE", help="探す対象から除くファイル(繰り返し可)")
    plan.add_argument("--cache-dir", help="キャッシュの置き場所")
    plan.add_argument("--report", help="結果を書き出すファイル(省略時は標準出力)")

    align = sub.add_parser("align", help="音声照合だけを行う(開発用)")
    align.add_argument("--wav", required=True, help="対象の音声(PCMのWAV)")
    align.add_argument("--ref-wav", required=True, help="基準の音声(PCMのWAV)")
    align.add_argument("--report", help="結果をJSONで書き出すファイル")
    return parser


def _is_comment_align(args):
    """方式D(基準の局と時刻だけを渡された)か。--altと--ref-tsは別に扱う。"""
    if args.timemap or args.ref_wav or args.alt or args.ref_ts:
        return False
    return (args.ref_start is not None or args.ref_end is not None or args.ref_channel is not None
            or args.ref_sid is not None or bool(args.ref_comments_file))


def _validate_make(parser, args):
    if not args.output and not args.comments_output:
        parser.error("specify --output or --comments-output")
    if args.ts:
        if args.start is not None or args.end is not None:
            parser.error("--ts and --start/--end cannot be used together")
    else:
        if args.start is None or args.end is None:
            parser.error("the target time is missing; specify --start and --end, or --ts")
        elif args.end <= args.start:
            parser.error("--end must be after --start")
    if args.timemap and args.ref_wav:
        parser.error("--timemap and --ref-wav cannot be used together")
    if args.alt and args.mode:
        parser.error("--alt and --mode cannot be used together (--alt's value is used)")
    if args.alt and args.ref_ts:
        parser.error("--alt and --ref-ts cannot be used together")
    if (args.alt or args.ref_ts) and (args.timemap or args.ref_wav):
        parser.error("--alt/--ref-ts and --timemap/--ref-wav cannot be used together")
    if args.alt and not args.search_dir:
        parser.error("--alt requires --search-dir")
    if (args.search_dir or args.exclude) and not args.alt:
        parser.error("--search-dir/--exclude require --alt")
    args.mode = args.alt or args.mode or "merge"
    if args.ref_wav and not args.wav and not args.ts:
        parser.error("--ref-wav requires --wav or --ts")
    if args.timemap or args.ref_wav or _is_comment_align(args):
        option = "--timemap" if args.timemap else "--ref-wav" if args.ref_wav else "reference channel and time (method D)"
        if args.ref_start is None or args.ref_end is None:
            if option.startswith("reference channel"):
                parser.error("a reference needs --timemap, --ref-wav, --ref-ts, --alt, "
                             "or both --ref-start and --ref-end (method D)")
            parser.error(f"{option} requires --ref-start and --ref-end")
        if args.ref_end <= args.ref_start:
            parser.error("--ref-end must be after --ref-start")
        if args.ref_channel is None and args.ref_sid is None and not args.ref_comments_file:
            parser.error(f"{option} requires one of --ref-sid, --ref-channel, or --ref-comments-file")
        if args.comments_file or args.comments_start is not None or args.comments_offset != 0:
            parser.error(f"{option} and --comments-file/--comments-start/--comments-offset cannot be used together")
    elif args.alt or args.ref_ts:
        option = "--alt" if args.alt else "--ref-ts"
        if args.ref_start is not None or args.ref_end is not None or args.ref_channel is not None \
                or (args.alt and args.ref_sid is not None):
            parser.error(f"{option} and --ref-start/--ref-end/--ref-channel (and --ref-sid with --alt) cannot be used together")
        if args.ref_comments_file and args.alt:
            parser.error("--alt and --ref-comments-file cannot be used together")
        if args.comments_file or args.comments_start is not None or args.comments_offset != 0:
            parser.error(f"{option} and --comments-file/--comments-start/--comments-offset cannot be used together")
    if args.comments_file_auto and not args.comments_file:
        parser.error("--comments-file-auto requires --comments-file")
    if args.fontsize is not None and args.fontsize <= 0:
        parser.error("--fontsize must be a positive integer")


# ------------------------------------------------------------------ #
# make
# ------------------------------------------------------------------ #

def _normalize(value):
    """整数になる値はintにそろえる(APIのURLの表記を従来と同じにするため)。"""
    value = Decimal(str(value))
    return int(value) if value == value.to_integral_value() else value


def _json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"value is not JSON serializable: {value!r}")


def _resolve_jk(sid, channel):
    """(jk番号, 局名)を返す。jk番号がなければjkはNone。"""
    if channel is not None:
        return channel, None
    if sid is not None:
        found = stations.lookup_jk(sid)
        if found:
            return found
    return None, None


def _rebase_file_comments(root, start, end):
    """読み込み済みのコメントを[start, end]を基準にしたvposにして、XML文字列で返す。"""
    xml_str = rebase_file_vpos(root, start, end)
    log(f"comments fetched: {count_chats(xml_str)}")
    return xml_str


def _read_file_comments(path, start, end):
    try:
        return _rebase_file_comments(read_comments_file(path), start, end)
    except (OSError, UnicodeError, ET.ParseError, ValueError) as error:
        log(f"failed to read the comment file: {error}")
        raise NicoJKExit(EXIT_ERROR)


def _make_normal(args, report):
    """対象の局のコメントを、明示されたファイル、NicoJKログ、過去ログAPIの順に探す。"""
    start, end = _normalize(args.start), _normalize(args.end)
    program = _normalize(args.program_start if args.program_start is not None else args.start)
    comments_start = args.comments_start
    comments_file = args.comments_file
    relative = False
    root = None

    if comments_file:
        try:
            root = read_comments_file(comments_file)
            count, first, overlap = inspect_comments(root, start, end)
        except (OSError, UnicodeError, ET.ParseError, ValueError) as error:
            log(f"failed to read the comment file: {error}")
            raise NicoJKExit(EXIT_ERROR)
        if count == 0 and args.comments_file_auto:
            log("warning: ignoring the file because it is not a comment file")
            comments_file = None
            report["comments_file_ignored"] = True
        elif count and comments_start is None:
            comments_start = file_reference(program, first, count, overlap)
            relative = overlap < (count + 1) // 2
            if relative:
                log("warning: aligning the first comment to the program start. "
                    "Pass --comments-start to set it exactly")

    reference = Decimal(str(comments_start)) if comments_start is not None else Decimal(str(program))
    fetch_start = _normalize(Decimal(str(start)) + (reference - Decimal(str(program))) - args.comments_offset)
    fetch_end = _normalize(Decimal(str(fetch_start)) + (Decimal(str(end)) - Decimal(str(start))))

    jk, name = _resolve_jk(args.sid, args.channel)
    if not comments_file and jk is None:
        log("the target channel has no live comments")
        report["reason"] = "no_channel"
        raise NicoJKExit(EXIT_CHANNEL_NOT_FOUND)
    channel_text = f"jk{jk}" if jk else "no jk"
    source = 'NicoJK log or API' if args.nicojk_log is not None else 'API'
    log(f"comment source: {comments_file or source} / {channel_text} / "
        f"range {fetch_start} - {fetch_end} / program start {program} / "
        f"reference {reference} / offset {args.comments_offset}"
        + (" / aligning the start to the program start" if relative else ""))

    report["source"] = "file" if comments_file else "api"
    report["fetch"] = {"channel": f"jk{jk}" if jk else None, "file": comments_file,
                       "start": fetch_start, "end": fetch_end, "relative": relative}
    if comments_file:
        return _rebase_file_comments(root, fetch_start, fetch_end)
    return fetch_api_comments(f"jk{jk}", fetch_start, fetch_end, args.api_url, nicojk_log=args.nicojk_log)


def _align_audio(args, report):
    """--wavと--ref-wavを照合して対応表を返す。失敗したら警告を出してNoneを返す。"""
    try:
        from .align_audio import align_audio, make_timemap
        from .wav import read_wav
    except ImportError as error:
        log(f"audio alignment failed, falling back to the target channel: cannot import numpy: {error}")
        report["align"] = {"success": False, "reason": f"cannot import numpy: {error}", "segments": [], "seconds": 0}
        return None
    try:
        target, reference = read_wav(args.wav), read_wav(args.ref_wav)
    except (OSError, ValueError) as error:
        log(f"audio alignment failed, falling back to the target channel: {error}")
        report["align"] = {"success": False, "reason": str(error), "segments": [], "seconds": 0}
        return None
    result = align_audio(target, reference)
    report["align"] = {key: result[key] for key in ("success", "reason", "segments", "seconds")}
    log(f"audio alignment: reference {args.ref_wav} / took {result['seconds']:.3f}s / spans {len(result['segments'])}")
    for part in result["segments"]:
        log(f"audio alignment span: {part['start']:.3f}-{part['end']:.3f}s / reference-target {part['delta']:+.3f}s / "
            f"probes {part['probes']} / ratio {part['ratio']:.3f}")
    if not result["success"]:
        log(f"audio alignment failed, falling back to the target channel: {result['reason']}")
        return None
    # 対応表の長さは、WAVの長さ(これまでのネイティブ版が渡していた録画の長さと同じ)を使う。
    return make_timemap(result["segments"], len(target) / 8000, len(reference) / 8000)


class WorkDir:
    """デコードしたWAVの置き場所。--work-dirがなければ、OSの一時フォルダに作って終わったら消す。
    --work-dirがあれば、そこへ書いた自分のファイルだけを終わったら消す。--keep-workなら何も消さない。"""

    def __init__(self, path, keep):
        self.path = path
        self.keep = keep
        self.created = False
        self.files = []

    def file(self, name):
        if self.path is None:
            self.path = tempfile.mkdtemp(prefix="nicojk_ass_")
            self.created = True
        else:
            os.makedirs(self.path, exist_ok=True)
        path = os.path.join(self.path, name)
        self.files += [path, path + ".json"]
        return path

    def close(self):
        if self.keep:
            return
        if self.created:
            shutil.rmtree(self.path, ignore_errors=True)
            return
        for path in self.files:
            try:
                os.unlink(path)
            except OSError:
                pass


def _alt_plan(args, cache_dir):
    """--altの放送の特定と録画の探索(decodeと照合はしない)。planの辞書を返す。"""
    program_start = args.program_start if args.program_start is not None else args.start
    finder = Finder(cache_dir, SyoboiClient(cache_dir, log=lambda m: log(m)), log=log)
    return finder.find(args.sid, float(program_start), args.search_dir, args.exclude,
                       program_title=args.program_title)


def _apply_ts_target(args, report, work):
    """--tsから対象の時刻を決め、音声照合に使うときは対象のWAVも作る。"""
    if not args.ts:
        return
    if args.decoder is None:
        args.decoder = ("ffmpeg", "ffmpeg")
    try:
        timeline = tsinfo.read_timeline(args.ts, args.sid)
    except (OSError, tsinfo.TsInfoError) as error:
        log(f"cannot read the target recording: {error}")
        raise NicoJKExit(EXIT_ERROR)
    if args.sid is None:
        args.sid = timeline["sid"]
    report["target"]["sid"] = args.sid
    report["target"]["ts"] = args.ts
    if not timeline["has_time"]:
        log("cannot find the start time because the target recording has no TOT or PCR")
        raise NicoJKExit(EXIT_ERROR)
    args.start = _normalize(Decimal(f"{timeline['start']:.6f}"))
    args.end = _normalize(Decimal(f"{timeline['end']:.6f}"))
    report["target"]["start"] = args.start
    report["target"]["end"] = args.end
    if args.end <= args.start:
        log("cannot determine the length of the target recording")
        raise NicoJKExit(EXIT_ERROR)
    if args.program_start is None:
        program = None
        try:
            program = tsinfo.read_ts_info(args.ts).start_of(args.sid)
        except (OSError, tsinfo.TsInfoError) as error:
            log(f"warning: using the start of the recording because the broadcast start time cannot be read: {error}")
        args.program_start = _normalize(Decimal(f"{program:.6f}")) if program is not None else args.start
        report["target"]["program_start"] = args.program_start
    log(f"target recording: sid {args.sid} / start {args.start} / end {args.end}")
    if args.wav or not (args.alt or args.ref_ts or args.ref_wav):
        return
    wav = work.file("target.wav")
    try:
        info = decoder.decode(args.decoder[0], args.decoder[1], args.ts, args.sid, wav, timeline)
    except decoder.DecodeError as error:
        log(f"warning: cannot decode the target audio: {error}")
        report["target_decode"] = {"success": False, "reason": str(error)}
        if args.ref_wav:
            raise NicoJKExit(EXIT_ERROR)
        return
    report["target_decode"] = {"success": info["has_time"], "ts": args.ts, "wav": wav, **info}
    if not info["has_time"]:
        log("warning: skipping audio alignment because the target recording has no TOT")
        report["target_decode"]["reason"] = "no TOT"
        return
    args.wav = wav


def _decode_reference(args, report, work, ts_path, sid):
    """基準のTSをデコードし、args.ref_wav・ref_start・ref_end・ref_sidを設定する。
    使えないときは理由を警告に出してFalseを返す。"""
    if not args.wav:
        log("warning: not using the reference recording because the target audio (--wav) is missing")
        report["decode"] = {"success": False, "reason": "--wav is missing"}
        return False
    if args.decoder is None:
        log("warning: cannot decode the reference recording without --decoder")
        report["decode"] = {"success": False, "reason": "--decoder is missing"}
        return False
    wav = work.file("reference.wav")
    try:
        info = decoder.decode(args.decoder[0], args.decoder[1], ts_path, sid, wav)
    except decoder.DecodeError as error:
        log(f"warning: failed to decode the reference recording: {error}")
        report["decode"] = {"success": False, "reason": str(error), "ts": ts_path}
        return False
    report["decode"] = {"success": True, "ts": ts_path, "wav": wav, **info}
    if not info["has_time"]:
        log("warning: not using the reference because it has no TOT and the start time is unknown")
        report["decode"]["success"] = False
        report["decode"]["reason"] = "no TOT"
        return False
    # 今のAmatsukazeの方式Aと同じ値: 丸めた先頭の時刻と、そこから音声の長さ分
    args.ref_wav = wav
    args.ref_start = _normalize(info["start_rounded"])
    args.ref_end = _normalize(info["start_rounded"] + info["duration"])
    args.ref_sid = info["sid"]
    return True


def _prepare_reference(args, report, work):
    """--altか--ref-tsの基準を決める。録画をデコードして方式Aを使えるときは"audio"、
    録画がない(または対象の音声がない)ので候補のコメントで方式Dを使うときは"comment"、使えないときはNoneを返す。
    "comment"のときは、args.ref_candidatesに候補(放送の早い順)を設定する。"""
    if args.ref_ts:
        report["alt"] = None
        return "audio" if _decode_reference(args, report, work, args.ref_ts, args.ref_sid) else None
    if args.sid is None:
        log("warning: --alt requires --sid; using the target channel's comments")
        report["alt"] = {"error": "--sid is missing"}
        return None
    plan = _alt_plan(args, args.cache_dir or default_cache_dir())
    report["alt"] = plan
    unrelated = plan["target"]["comments"] == "unrelated"
    if unrelated:
        args.mode = "replace"  # 遅れネットでは、対象の局のコメントは別の番組のもの
    reference = plan["reference"]
    if reference is not None and args.wav:
        return "audio" if _decode_reference(args, report, work, reference["path"], reference["sid"]) else None
    if unrelated or not plan["candidates"]:
        # 遅れネットでは対象の局のコメントが別の番組のものなので、方式Dは使えない。
        if reference is not None:
            log("warning: not using the reference recording because the target audio (--wav) is missing")
            report["decode"] = {"success": False, "reason": "--wav is missing"}
        return None
    log(("the reference recording exists but the target audio (--wav) is missing, so "
         if reference is not None else "no reference recording, so ")
        + "using method D on the candidate comments")
    args.ref_candidates = plan["candidates"]
    return "comment"


def _align_comments(args, report, spans):
    """方式D: 基準の局のコメントを、対象の局のコメントとの本文の一致で対象の時刻へ写す。
    対象の局のコメントは推定にも統合にも使い、取得は1回で済ませる。
    推定できなかったときと基準のコメントを取れないときは、対象の局のコメントだけを返す。"""
    start, end = _normalize(args.start), _normalize(args.end)
    program = args.program_start if args.program_start is not None else args.start
    jk, _ = _resolve_jk(args.sid, args.channel)
    if jk is None:
        log("the target channel has no live comments")
        report["reason"] = "no_channel"
        raise NicoJKExit(EXIT_CHANNEL_NOT_FOUND)
    source = "NicoJK log or API" if args.nicojk_log is not None else "API"
    log(f"comment source: {source} / jk{jk} / range {start} - {end} / method D")
    target_xml = fetch_api_comments(f"jk{jk}", start, end, args.api_url, nicojk_log=args.nicojk_log)
    report["fetch"] = {"channel": f"jk{jk}", "file": None, "start": start, "end": end, "relative": False}
    try:
        target_rows = align_comment.comments_from_xml(target_xml, float(start), float(end))
    except (ET.ParseError, ValueError) as error:
        log(f"failed to read comments: {error}")
        raise NicoJKExit(EXIT_ERROR)

    # --altの候補は放送の早い順。直接指定(--ref-sidなど)の候補は1つだけ。
    candidates = getattr(args, "ref_candidates", None)
    if candidates is None:
        candidates = [{"sid": args.ref_sid, "start": args.ref_start, "end": args.ref_end}]
    detail = report["comment_align"] = {}
    result = _estimate_comment_map(args, detail, target_rows, start, end, program, spans, candidates)
    if result is None:
        log(f"warning: comment alignment failed, using only the target channel's comments: {detail['reason']}")
        report["source"] = "api"
        report["reference_fallback"] = True
        args.mode = "replace"
        return target_xml
    timemap, reference_xml, candidate = result
    args.ref_sid, args.ref_start, args.ref_end = candidate["sid"], candidate["start"], candidate["end"]
    return _make_timemap(args, report, timemap, "comment_align", reference_xml, target_xml)


def _estimate_comment_map(args, detail, target_rows, start, end, program, spans, candidates, strict=False):
    """取得済みの対象コメントと、最初に十分なコメントが取れた候補を照合する。"""
    params = align_comment.DEFAULT_PARAMS
    search = params["search_seconds"]
    detail.update(cm=bool(spans), segments=[], seconds=0, candidates=[])
    if len(target_rows) < params["min_comments"]:
        detail["target_comments"] = len(target_rows)
        detail["reason"] = f"too few comments on the target channel ({len(target_rows)})"
        return None
    skipped = []
    reference_xml = None
    for candidate in candidates:
        sid, ref_start, ref_end = candidate["sid"], candidate["start"], candidate["end"]
        ref_origin = _normalize(Decimal(str(ref_start)) - search)
        ref_limit = _normalize(Decimal(str(ref_end)) + search)
        entry = {"sid": sid, "start": ref_start, "end": ref_end, "skipped": None}
        detail["candidates"].append(entry)
        reason = None
        try:
            if args.ref_comments_file:
                reference_xml = _read_file_comments(args.ref_comments_file, ref_origin, ref_limit)
            else:
                ref_jk, _ = _resolve_jk(sid, args.ref_channel)
                if ref_jk is None:
                    reason = "the reference channel has no live comments"
                else:
                    reference_xml = fetch_api_comments(f"jk{ref_jk}", ref_origin, ref_limit, args.api_url,
                                                       nicojk_log=args.nicojk_log)
            if reason is None:
                reference_rows = align_comment.comments_from_xml(reference_xml, float(ref_origin), float(ref_limit))
                entry["comments"] = len(reference_rows)
                if not align_comment.enough_comments(target_rows, reference_rows, params):
                    reason = f"too few comments (target {len(target_rows)}, reference {len(reference_rows)})"
        except (NicoJKExit, ET.ParseError, ValueError) as error:
            reason = f"could not fetch reference channel comments: {error}"
        if reason is None:
            break
        entry["skipped"] = reason
        skipped.append(reason)
        reference_xml = None
        if len(candidates) > 1:
            log(f"method D: skipping candidate sid {sid}: {reason}")
    if reference_xml is None:
        detail["reason"] = "; ".join(skipped)
        return None
    center = float(ref_start) - float(program)
    detail.update(center=center, target_comments=len(target_rows), reference_comments=len(reference_rows),
                  reference={"sid": sid, "start": ref_start, "end": ref_end})

    began = time.perf_counter()
    duration = float(end) - float(start)
    vectors, index = align_comment.prepare(target_rows, reference_rows, params)
    if spans:
        found, _ = align_comment.find_segments_cm(vectors, index, center, float(start), duration, spans, params)
    else:
        found, _ = align_comment.find_segments(vectors, index, center, float(start), duration, params)
    # 対応表の基準の時刻は、取得範囲の先頭(ref_origin)からの秒。区間のずれもその原点に直す。
    shift = float(ref_origin) - float(start)
    timemap = align_comment.make_timemap([dict(part, delta=part["delta"] - shift) for part in found], duration,
                                         float(ref_limit) - float(ref_origin), "strict" if strict else params["fill"])
    detail["seconds"] = round(time.perf_counter() - began, 3)
    detail["segments"] = [{"start": part["start"], "end": part["end"], "delta": part["delta"],
                           "ratio": part["ratio"], "support": part.get("support"), "windows": part.get("windows")}
                          for part in found]
    log(f"comment alignment: target {len(target_rows)} / reference {len(reference_rows)} / program spans "
        + ("present" if spans else "absent") + f" / took {detail['seconds']:.3f}s / spans {len(found)}")
    for part in found:
        log(f"comment alignment span: {part['start']:.3f}-{part['end']:.3f}s / reference-target {part['delta']:+.3f}s / "
            f"peak ratio {part['ratio']:.2f} / matches {part.get('support', 0)}")
    if not found or not timemap:
        detail["reason"] = "could not estimate a shift for any span"
        return None
    return timemap, reference_xml, candidate


def _bridge_spans(segments, duration):
    """音声の一致区間を間の録画の時刻に直し、重なりをつなぐ。"""
    spans = sorted((max(0.0, part["start"] + part["delta"]),
                    min(duration, part["end"] + part["delta"])) for part in segments)
    merged = []
    for begin, end in spans:
        if begin >= end:
            continue
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((begin, end))
    return merged


def _make_bridge(args, report, audio_map, recording_xml, start, end):
    plan = report["alt"]
    candidate = plan["bridge"]
    detail = report["bridge"] = {"comment_align": {}}
    alignment = detail["comment_align"]
    segments = report["align"]["segments"]
    duration = float(end) - float(start)
    spans = _bridge_spans(segments, duration)
    if spans:
        log(f"bridge: aligning jk{candidate['jk']} to the reference recording")
        try:
            rows = align_comment.comments_from_xml(recording_xml, float(start), float(end))
            program = plan["candidates"][plan["reference"]["candidate"]]["start"]
            result = _estimate_comment_map(args, alignment, rows, start, end, program, spans, [candidate], strict=True)
            if result is not None:
                comment_map, xml, _ = result
                # 先頭候補は音声の一致区間だけに写す。間の局のコメントは従来どおり補間する。
                matched_audio = align_comment.make_timemap(segments, audio_map[-1][3], audio_map[-1][1], "strict")
                combined = compose_timemaps(comment_map, matched_audio)
                if combined:
                    mapped = apply_timemap(xml, combined)
                    detail.update(success=True, comments=count_chats(mapped))
                    detail["fetch_start"] = _normalize(Decimal(str(candidate["start"]))
                                                       - align_comment.DEFAULT_PARAMS["search_seconds"])
                    detail["timemap"] = combined
                    return mapped
                alignment["reason"] = "the time maps do not overlap"
        except (NicoJKExit, ET.ParseError, ValueError, TypeError, InvalidOperation) as error:
            alignment["reason"] = str(error)
    else:
        alignment["reason"] = "no matched audio spans in the reference recording"
    detail["success"] = False
    log(f"warning: bridge comment alignment failed: {alignment['reason']}")
    return None


def _make_direct_bridge(args, detail, candidate, target_xml, start, end, spans):
    """間の録画のコメントで照合できなければ、対象の局のコメントと直接照合する。"""
    log(f"bridge fallback: aligning jk{candidate['jk']} directly to the target channel")
    try:
        rows = align_comment.comments_from_xml(target_xml, float(start), float(end))
        program = args.program_start if args.program_start is not None else args.start
        result = _estimate_comment_map(args, detail, rows, start, end, program, spans, [candidate])
        if result is not None:
            timemap, reference_xml, _ = result
            return apply_timemap(reference_xml, timemap)
    except (NicoJKExit, ET.ParseError, ValueError, TypeError, InvalidOperation) as error:
        detail["reason"] = str(error)
    return None


def _make_timemap(args, report, timemap, source="timemap", reference_xml=None, target_xml=None, *, spans=None):
    """基準の局のコメントを対応表で対象の時刻へ写す。基準の局がなければNoneを返す。
    reference_xmlとtarget_xmlは、取得済みのものを渡すと取得しない(方式D)。"""
    ref_start, ref_end = _normalize(args.ref_start), _normalize(args.ref_end)

    ref_jk = None
    if reference_xml is not None:
        xml_str = reference_xml
        ref_jk, _ = _resolve_jk(args.ref_sid, args.ref_channel)
        if args.ref_comments_file:
            ref_jk = None
    elif args.ref_comments_file:
        xml_str = _read_file_comments(args.ref_comments_file, ref_start, ref_end)
    else:
        ref_jk, _ = _resolve_jk(args.ref_sid, args.ref_channel)
        if ref_jk is None:
            log("warning: not using the reference because the reference channel has no live comments")
            return None
        try:
            xml_str = fetch_api_comments(f"jk{ref_jk}", ref_start, ref_end, args.api_url,
                                         nicojk_log=args.nicojk_log)
        except (NicoJKExit, ET.ParseError, ValueError) as error:
            # 基準のコメントを取れないときは、ジョブを失敗させずに対象の局のコメントへ戻る。
            log(f"warning: not using the reference because its comments could not be fetched: {error}")
            return None
    report["source"] = source
    report["reference"] = {"channel": f"jk{ref_jk}" if ref_jk else None,
                           "file": args.ref_comments_file, "start": ref_start, "end": ref_end}

    try:
        bridge_xml = None
        fallback = False
        plan = report.get("alt") or {}
        candidate = plan.get("bridge") or {}
        if timemap and report.get("align", {}).get("success") and candidate.get("use"):
            bridge_xml = _make_bridge(args, report, timemap, xml_str, ref_start, ref_end)
            fallback = bridge_xml is None and plan["target"]["comments"] != "unrelated"
        xml_str = apply_timemap(xml_str, timemap)

        start, end = _normalize(args.start), _normalize(args.end)
        target_error = None
        if (args.mode == "merge" or fallback) and target_xml is None:
            jk, _ = _resolve_jk(args.sid, args.channel)
            if jk is None:
                target_error = "the target channel has no live comments"
            else:
                try:
                    target_xml = fetch_target_comments(f"jk{jk}", start, end, args.api_url, nicojk_log=args.nicojk_log)
                except (NicoJKExit, ET.ParseError, ValueError) as error:
                    # API側の400/404でも基準のコメントは使い続ける。
                    if isinstance(error, NicoJKExit) and error.code == EXIT_CHANNEL_NOT_FOUND:
                        target_error = "the target channel is unsupported"
                    elif fallback:
                        target_error = f"could not fetch target channel comments: {error}"
                    else:
                        raise

        if fallback:
            detail = report["bridge"]["fallback"] = {}
            if target_error is None:
                bridge_xml = _make_direct_bridge(args, detail, candidate, target_xml, start, end, spans)
            else:
                detail["reason"] = target_error
            detail["success"] = bridge_xml is not None
            if bridge_xml is None:
                log(f"warning: direct comment alignment failed, keeping the reference recording's comments: {detail['reason']}")
        elif target_error is not None:
            log(f"warning: using only reference comments because {target_error}")
        if args.mode != "merge":
            target_xml = None
        report["merged"] = target_xml is not None
        if bridge_xml is not None:
            xml_str = merge_comments(xml_str, target_xml, args.mark_reference_color, bridge_xml)
        elif target_xml is not None:
            xml_str = merge_comments(xml_str, target_xml, args.mark_reference_color)
    except (OSError, UnicodeError, ET.ParseError, ValueError, TypeError, InvalidOperation) as error:
        log(f"failed to convert and merge comments: {error}")
        raise NicoJKExit(EXIT_ERROR)
    return xml_str


def _write_comments_output(args, xml_str):
    """相対vposのコメントを、過去ログと同じXMLで書く。"""
    try:
        text = format_server_comments(xml_str, args.start)
        with open(args.comments_output, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except (OSError, ET.ParseError, ValueError) as error:
        log(f"failed to write comments: {error}")
        raise NicoJKExit(EXIT_ERROR)
    log(f"wrote comments: {args.comments_output}")


def _render_outputs(args, xml_str):
    """XMLを一時ファイルに書き、解像度ごとにdanmaku2assで配置してASSを後処理する。"""
    with tempfile.NamedTemporaryFile(
            mode="w", suffix=".xml", encoding="utf-8", delete=False) as tmp:
        tmp.write(xml_str)
        tmp_xml = tmp.name
    try:
        for width, height, path in args.output:
            fontsize = args.fontsize if args.fontsize is not None else default_fontsize(height)
            log(f"resolution={width}x{height} font={args.font!r} fontsize={fontsize} "
                f"duration={args.display_seconds} opacity={args.opacity}")
            run_danmaku2ass(tmp_xml, path, width, height, args.font, fontsize,
                            args.opacity, args.display_seconds)
            fixup_ass(path, width, height, fontsize, args.font)
            log(f"wrote ASS: {path}")
    finally:
        os.unlink(tmp_xml)


def run_make(args):
    work = WorkDir(args.work_dir, args.keep_work)
    try:
        return _run_make(args, work)
    finally:
        work.close()


def _run_make(args, work):
    report = {"schema": REPORT_SCHEMA, "version": __version__, "exit_code": EXIT_SUCCESS,
              "target": {"sid": args.sid, "channel": f"jk{args.channel}" if args.channel else None,
                         "start": args.start, "end": args.end,
                         "program_start": args.program_start},
              "outputs": [path for _, _, path in args.output] if args.output else []}
    if args.comments_output:
        report["comments_output"] = args.comments_output
    try:
        args.nicojk_log = None
        if args.prefer_nicojk_log:
            attempts = report["nicojk_log"] = []
            try:
                args.nicojk_log = nicojk_log.NicoJKLog(nicojk_log.load_dirs(), attempts)
            except (OSError, ValueError) as error:
                log(f"cannot read nicojk_ass.toml: {error}")
                raise NicoJKExit(EXIT_ERROR) from error
        _apply_ts_target(args, report, work)
        spans = None
        if args.spans:
            try:
                spans = read_spans(args.spans)
            except (OSError, UnicodeError, ValueError) as error:
                log(f"cannot read the program spans: {error}")
                raise NicoJKExit(EXIT_ERROR)
            report["spans"] = {"count": len(spans)}
        xml_str = None
        timemap = None
        if args.timemap:
            try:
                timemap = read_timemap(args.timemap)
            except (OSError, UnicodeError, ValueError, InvalidOperation) as error:
                log(f"failed to read the time map: {error}")
                raise NicoJKExit(EXIT_ERROR)
        elif args.ref_wav:
            timemap = _align_audio(args, report)
        elif _is_comment_align(args):
            xml_str = _align_comments(args, report, spans)
        elif args.alt or args.ref_ts:
            method = _prepare_reference(args, report, work)
            if method == "audio":
                timemap = _align_audio(args, report)
            elif method == "comment":
                xml_str = _align_comments(args, report, spans)
        if timemap is not None:
            xml_str = _make_timemap(args, report, timemap, spans=spans)
        if xml_str is None:
            if (report.get("alt") or {}).get("target", {}).get("comments") == "unrelated":
                # 遅れネット・独自枠で基準を使えないとき、対象の局のコメントは別の番組のものなので付けない。
                log("not adding live comments because a delayed or local broadcast has no usable reference recording")
                report["reason"] = "unrelated_no_reference"
                raise NicoJKExit(EXIT_CHANNEL_NOT_FOUND)
            if args.timemap or args.ref_wav or args.alt or args.ref_ts:
                # 基準を使えないときは、基準の指定を外した通常の取得に戻る。
                report["reference_fallback"] = True
                args.mode = "replace"
            xml_str = _make_normal(args, report)
        report["comments"] = count_chats(xml_str)
        if args.comments_output:
            _write_comments_output(args, xml_str)
        if args.output:
            _render_outputs(args, xml_str)
    except NicoJKExit as error:
        report["exit_code"] = error.code
        report.setdefault("reason", "channel_not_found" if error.code == EXIT_CHANNEL_NOT_FOUND else "error")
        raise
    finally:
        if args.report:
            try:
                with open(args.report, "w", encoding="utf-8") as f:
                    json.dump(report, f, ensure_ascii=False, indent=2, default=_json_default)
            except OSError as error:
                log(f"cannot write the report: {error}")
    return EXIT_SUCCESS


def run_plan(args):
    """別の局の録画を探す処理だけを行い、結果をJSONで出す。終了コードは0(見つからなくても0)、エラーは1。"""
    if args.sid is None:
        log("warning: cannot identify the broadcast because --sid is missing")
    plan = _alt_plan(args, args.cache_dir or default_cache_dir())
    text = json.dumps(plan, ensure_ascii=False, indent=1)
    try:
        if args.report:
            with open(args.report, "w", encoding="utf-8") as f:
                f.write(text)
        else:
            print(text)
    except OSError as error:
        log(f"cannot write the result: {error}")
        return 1
    return 0


def run_align(args):
    """音声照合だけを行い、結果をJSONで出す。終了コードは成功0、照合の失敗1、その他のエラー2。"""
    try:
        from .align_audio import align_audio, align_result_json
        from .wav import read_wav
    except ImportError as error:
        log(f"cannot import numpy: {error}")
        return 2
    try:
        target, reference = read_wav(args.wav), read_wav(args.ref_wav)
    except (OSError, ValueError) as error:
        log(f"cannot read WAV: {error}")
        return 2
    result = align_audio(target, reference)
    text = json.dumps(align_result_json(result, len(target) / 8000, len(reference) / 8000),
                      ensure_ascii=False, indent=1)
    try:
        if args.report:
            with open(args.report, "w", encoding="utf-8") as f:
                f.write(text)
        else:
            print(text)
    except OSError as error:
        log(f"cannot write the result: {error}")
        return 2
    log(f"audio alignment: {result['seconds']:.3f}s / spans {len(result['segments'])} / "
        + ("success" if result["success"] else "failed: " + result["reason"]))
    return 0 if result["success"] else 1


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "align":
        return run_align(args)
    if args.command == "plan":
        return run_plan(args)
    if args.command == "make":
        _validate_make(parser, args)
        try:
            return run_make(args)
        except NicoJKExit as error:
            return error.code
    parser.error("unknown command")


if __name__ == "__main__":
    sys.exit(main())
