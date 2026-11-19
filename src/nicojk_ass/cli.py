"""コマンドラインの入口。サブコマンドはmake、plan、align。"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation

from . import __version__, decoder, stations
from .comments import (DEFAULT_API_BASE, EXIT_CHANNEL_NOT_FOUND, EXIT_ERROR, EXIT_SUCCESS,
                       NicoJKExit, count_chats, fetch_api_comments, fetch_target_comments,
                       file_reference, inspect_comments, log, parse_time, read_comments_file,
                       rebase_file_vpos)
from .finder import Finder
from .render import (DEFAULT_DURATION, DEFAULT_FONT, DEFAULT_OPACITY, default_fontsize,
                     fixup_ass, run_danmaku2ass)
from .syoboi import SyoboiClient, default_cache_dir
from .timemap import apply_timemap, merge_comments, read_timemap

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
        raise argparse.ArgumentTypeError("有限の秒数を指定してください")
    if not number.is_finite() or abs(number) >= Decimal(10) ** 12:
        raise argparse.ArgumentTypeError("有限の秒数を指定してください")
    return number


def _channel_arg(value):
    text = value[2:] if value.startswith("jk") else value
    if not text.isdigit() or int(text) <= 0:
        raise argparse.ArgumentTypeError("実況チャンネルはjkN(Nは正の整数)で指定してください")
    return int(text)


def _decoder_arg(value):
    try:
        return decoder.parse_decoder(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error))


def _output_arg(value):
    match = re.fullmatch(r"(\d+)x(\d+)=(.+)", value, re.DOTALL)
    if not match or int(match.group(1)) <= 0 or int(match.group(2)) <= 0:
        raise argparse.ArgumentTypeError("--outputはWxH=PATHの形で指定してください")
    return int(match.group(1)), int(match.group(2)), match.group(3)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="nicojk_ass", description="ニコニコ実況コメントのASS生成ツール")
    parser.add_argument("--version", action="version", version=f"nicojk_ass {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    make = sub.add_parser("make", help="コメントを取得してASSを生成する")
    target = make.add_argument_group("対象")
    target.add_argument("--sid", type=int, help="対象の局のServiceId(ch_sid.txtでjk番号を引く)")
    target.add_argument("--channel", type=_channel_arg, help="対象の実況チャンネル(例: jk1)。--sidより優先")
    target.add_argument("--start", required=True, type=_time_arg, help="対象の録画の先頭の時刻")
    target.add_argument("--end", required=True, type=_time_arg, help="対象の録画の末尾の時刻")
    target.add_argument("--program-start", type=_time_arg, help="録画した放送の開始時刻(省略時は--start)")
    target.add_argument("--wav", help="対象の音声(8kHz・16bitのWAV)。--ref-wavと一緒に使う")
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
                     help="先行放送・同時刻の放送の録画を--search-dirから探して基準にする"
                          "(replaceは基準のコメントだけ、mergeは対象の局のコメントと統合する)")
    ref.add_argument("--search-dir", action="append", default=[], metavar="DIR",
                     help="--altで録画を探すフォルダ(繰り返し可。サブフォルダは見ない)")
    ref.add_argument("--exclude", action="append", default=[], metavar="FILE",
                     help="--altで探す対象から除くファイル(対象の録画自身。繰り返し可)")
    ref.add_argument("--ref-ts", help="基準の録画(TS)。デコードして音声照合する")
    ref.add_argument("--mode", choices=("replace", "merge"),
                     help="replaceは基準のコメントだけ、mergeは対象の局のコメントと統合する(既定はreplace)")
    ref.add_argument("--timemap", help="基準から対象への時刻対応表(JSON)")
    ref.add_argument("--ref-wav", help="基準の音声(8kHz・16bitのWAV)。対象の音声と照合して対応表を作る")

    out = make.add_argument_group("出力")
    out.add_argument("--output", action="append", type=_output_arg, required=True, metavar="WxH=PATH",
                     help="解像度ごとのASSの出力先(繰り返し可)")
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
    make.add_argument("--decoder", type=_decoder_arg, metavar="amatsukaze=PATH",
                      help="基準の録画の音声をデコードするプログラム")
    make.add_argument("--cache-dir", help="しょぼいカレンダーの応答とTSの見出しのキャッシュの置き場所"
                                          "(既定は利用者ごとのキャッシュのフォルダ)")
    make.add_argument("--work-dir", help="デコードしたWAVの置き場所(既定はOSの一時フォルダに作り、終わったら消す)")
    make.add_argument("--keep-work", action="store_true", help="デコードしたWAVを残す(調査用)")

    plan = sub.add_parser("plan", help="先行放送の録画を探す処理だけを行い、結果をJSONで出す(デコードと照合はしない)")
    plan.add_argument("--sid", type=int, help="対象の局のServiceId")
    plan.add_argument("--channel", type=_channel_arg, help="対象の実況チャンネル(放送の特定にはServiceIdが要る)")
    plan.add_argument("--start", required=True, type=_time_arg, help="対象の録画の先頭の時刻")
    plan.add_argument("--end", type=_time_arg, help="対象の録画の末尾の時刻")
    plan.add_argument("--program-start", type=_time_arg, help="録画した放送の開始時刻(省略時は--start)")
    plan.add_argument("--search-dir", action="append", default=[], metavar="DIR", help="録画を探すフォルダ(繰り返し可)")
    plan.add_argument("--exclude", action="append", default=[], metavar="FILE", help="探す対象から除くファイル(繰り返し可)")
    plan.add_argument("--cache-dir", help="キャッシュの置き場所")
    plan.add_argument("--report", help="結果を書き出すファイル(省略時は標準出力)")

    align = sub.add_parser("align", help="音声照合だけを行う(開発用)")
    align.add_argument("--wav", required=True, help="対象の音声(8kHz・16bitのWAV)")
    align.add_argument("--ref-wav", required=True, help="基準の音声(8kHz・16bitのWAV)")
    align.add_argument("--report", help="結果をJSONで書き出すファイル")
    return parser


def _validate_make(parser, args):
    if args.end <= args.start:
        parser.error("--endは--startより後にしてください")
    if args.timemap and args.ref_wav:
        parser.error("--timemapと--ref-wavは同時に指定できません")
    if args.alt and args.mode:
        parser.error("--altと--modeは同時に指定できません(--altの値が使われます)")
    if args.alt and args.ref_ts:
        parser.error("--altと--ref-tsは同時に指定できません")
    if (args.alt or args.ref_ts) and (args.timemap or args.ref_wav):
        parser.error("--alt/--ref-tsと--timemap/--ref-wavは同時に指定できません")
    if args.alt and not args.search_dir:
        parser.error("--altには--search-dirが必要です")
    if (args.search_dir or args.exclude) and not args.alt:
        parser.error("--search-dir/--excludeには--altが必要です")
    args.mode = args.alt or args.mode or "replace"
    if args.ref_wav and not args.wav:
        parser.error("--ref-wavには--wavが必要です")
    if args.timemap or args.ref_wav:
        option = "--timemap" if args.timemap else "--ref-wav"
        if args.ref_start is None or args.ref_end is None:
            parser.error(f"{option}には--ref-startと--ref-endが必要です")
        if args.ref_end <= args.ref_start:
            parser.error("--ref-endは--ref-startより後にしてください")
        if args.ref_channel is None and args.ref_sid is None and not args.ref_comments_file:
            parser.error(f"{option}には--ref-sid、--ref-channel、--ref-comments-fileのどれかが必要です")
        if args.comments_file or args.comments_start is not None or args.comments_offset != 0:
            parser.error(f"{option}と--comments-file/--comments-start/--comments-offsetは同時に指定できません")
    elif args.alt or args.ref_ts:
        option = "--alt" if args.alt else "--ref-ts"
        if args.ref_start is not None or args.ref_end is not None or args.ref_channel is not None \
                or (args.alt and args.ref_sid is not None):
            parser.error(f"{option}と--ref-start/--ref-end/--ref-channel(--altでは--ref-sidも)は同時に指定できません")
        if args.ref_comments_file and args.alt:
            parser.error("--altと--ref-comments-fileは同時に指定できません")
        if args.comments_file or args.comments_start is not None or args.comments_offset != 0:
            parser.error(f"{option}と--comments-file/--comments-start/--comments-offsetは同時に指定できません")
    elif args.ref_comments_file or args.ref_channel is not None or args.ref_sid is not None:
        parser.error("基準の指定には--timemap、--ref-wav、--ref-tsのどれかが必要です")
    if args.comments_file_auto and not args.comments_file:
        parser.error("--comments-file-autoには--comments-fileが必要です")
    if args.fontsize is not None and args.fontsize <= 0:
        parser.error("--fontsizeは正の整数にしてください")


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
    raise TypeError(f"JSONにできない値です: {value!r}")


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
    log(f"取得コメント数: {count_chats(xml_str)}件")
    return xml_str


def _read_file_comments(path, start, end):
    try:
        return _rebase_file_comments(read_comments_file(path), start, end)
    except (OSError, UnicodeError, ET.ParseError, ValueError) as error:
        log(f"コメントファイルの読み込み失敗: {error}")
        raise NicoJKExit(EXIT_ERROR)


def _make_normal(args, report):
    """対象の局のコメント(API)か、コメントファイルから取得する。"""
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
            log(f"コメントファイルの読み込み失敗: {error}")
            raise NicoJKExit(EXIT_ERROR)
        if count == 0 and args.comments_file_auto:
            log("警告: 同名ファイルはコメントファイルではないため無視します")
            comments_file = None
            report["comments_file_ignored"] = True
        elif count and comments_start is None:
            comments_start = file_reference(program, first, count, overlap)
            relative = overlap < (count + 1) // 2
            if relative:
                log("警告: 最初のコメントを番組開始に合わせます。"
                    "正確に合わせるには--comments-startを指定してください")

    reference = Decimal(str(comments_start)) if comments_start is not None else Decimal(str(program))
    fetch_start = _normalize(Decimal(str(start)) + (reference - Decimal(str(program))) - args.comments_offset)
    fetch_end = _normalize(Decimal(str(fetch_start)) + (Decimal(str(end)) - Decimal(str(start))))

    jk, name = _resolve_jk(args.sid, args.channel)
    if not comments_file and jk is None:
        log("対象の局に実況チャンネルがありません")
        report["reason"] = "no_channel"
        raise NicoJKExit(EXIT_CHANNEL_NOT_FOUND)
    channel_text = f"jk{jk}" if jk else "jkなし"
    log(f"コメント取得元: {comments_file or 'API'} / {channel_text} / "
        f"取得範囲 {fetch_start} ～ {fetch_end} / 番組開始 {program} / "
        f"基準 {reference} / offset {args.comments_offset}"
        + (" / 先頭を番組開始に合わせる" if relative else ""))

    report["source"] = "file" if comments_file else "api"
    report["fetch"] = {"channel": f"jk{jk}" if jk else None, "file": comments_file,
                       "start": fetch_start, "end": fetch_end, "relative": relative}
    if comments_file:
        return _rebase_file_comments(root, fetch_start, fetch_end)
    return fetch_api_comments(f"jk{jk}", fetch_start, fetch_end, args.api_url)


def _align_audio(args, report):
    """--wavと--ref-wavを照合して対応表を返す。失敗したら警告を出してNoneを返す。"""
    try:
        from .align_audio import align_audio, make_timemap
        from .wav import read_wav
    except ImportError as error:
        log(f"実況音声照合に失敗したため対象局へ戻ります: numpyを読み込めません: {error}")
        report["align"] = {"success": False, "reason": f"numpyを読み込めません: {error}", "segments": [], "seconds": 0}
        return None
    try:
        target, reference = read_wav(args.wav), read_wav(args.ref_wav)
    except (OSError, ValueError) as error:
        log(f"実況音声照合に失敗したため対象局へ戻ります: {error}")
        report["align"] = {"success": False, "reason": str(error), "segments": [], "seconds": 0}
        return None
    result = align_audio(target, reference)
    report["align"] = {key: result[key] for key in ("success", "reason", "segments", "seconds")}
    log(f"実況音声照合: 基準 {args.ref_wav} / 照合 {result['seconds']:.3f}秒 / 区間 {len(result['segments'])}")
    for part in result["segments"]:
        log(f"実況照合区間: {part['start']:.3f}～{part['end']:.3f}秒 / 基準－対象 {part['delta']:+.3f}秒 / "
            f"プローブ{part['probes']}本 / 相関比{part['ratio']:.3f}")
    if not result["success"]:
        log(f"実況音声照合に失敗したため対象局へ戻ります: {result['reason']}")
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
    return finder.find(args.sid, float(program_start), args.search_dir, args.exclude)


def _decode_reference(args, report, work, ts_path, sid):
    """基準のTSをデコードし、args.ref_wav・ref_start・ref_end・ref_sidを設定する。
    使えないときは理由を警告に出してFalseを返す。"""
    if not args.wav:
        log("警告: 対象の音声(--wav)がないため基準の録画を使いません(方式Dは未対応)")
        report["decode"] = {"success": False, "reason": "--wavがありません"}
        return False
    if args.decoder is None:
        log("警告: --decoderがないため基準の録画をデコードできません")
        report["decode"] = {"success": False, "reason": "--decoderがありません"}
        return False
    wav = work.file("reference.wav")
    try:
        info = decoder.decode_amatsukaze(args.decoder[1], ts_path, sid, wav)
    except decoder.DecodeError as error:
        log(f"警告: 基準の録画のデコードに失敗しました: {error}")
        report["decode"] = {"success": False, "reason": str(error), "ts": ts_path}
        return False
    report["decode"] = {"success": True, "ts": ts_path, "wav": wav, **info}
    if not info["has_time"]:
        log("警告: 基準の録画にTOTがなく先頭の時刻を求められないため基準を使いません")
        report["decode"]["success"] = False
        report["decode"]["reason"] = "TOTがありません"
        return False
    # 今のAmatsukazeの方式Aと同じ値: 丸めた先頭の時刻と、そこから音声の長さ分
    args.ref_wav = wav
    args.ref_start = _normalize(info["start_rounded"])
    args.ref_end = _normalize(info["start_rounded"] + info["duration"])
    args.ref_sid = info["sid"]
    return True


def _prepare_reference(args, report, work):
    """--altか--ref-tsの基準の録画を決めてデコードする。使えるときだけTrueを返す。"""
    if args.ref_ts:
        report["alt"] = None
        return _decode_reference(args, report, work, args.ref_ts, args.ref_sid)
    if args.sid is None:
        log("警告: --altには--sidが必要です。対象の局のコメントを使います")
        report["alt"] = {"error": "--sidがありません"}
        return False
    plan = _alt_plan(args, args.cache_dir or default_cache_dir())
    report["alt"] = plan
    if plan["target"]["comments"] == "unrelated":
        args.mode = "replace"  # 遅れネットでは、対象の局のコメントは別の番組のもの
    reference = plan["reference"]
    if reference is None:
        return False
    return _decode_reference(args, report, work, reference["path"], reference["sid"])


def _make_timemap(args, report, timemap):
    """基準の局のコメントを対応表で対象の時刻へ写す。基準の局がなければNoneを返す。"""
    ref_start, ref_end = _normalize(args.ref_start), _normalize(args.ref_end)

    ref_jk = None
    if args.ref_comments_file:
        xml_str = _read_file_comments(args.ref_comments_file, ref_start, ref_end)
    else:
        ref_jk, _ = _resolve_jk(args.ref_sid, args.ref_channel)
        if ref_jk is None:
            log("警告: 基準の局に実況チャンネルがないため基準を使いません")
            return None
        try:
            xml_str = fetch_api_comments(f"jk{ref_jk}", ref_start, ref_end, args.api_url)
        except (NicoJKExit, ET.ParseError, ValueError) as error:
            # 基準のコメントを取れないときは、ジョブを失敗させずに対象の局のコメントへ戻る(設計書4.2)。
            log(f"警告: 基準の局のコメントを取得できないため基準を使いません: {error}")
            return None
    report["source"] = "timemap"
    report["reference"] = {"channel": f"jk{ref_jk}" if ref_jk else None,
                           "file": args.ref_comments_file, "start": ref_start, "end": ref_end}

    try:
        xml_str = apply_timemap(xml_str, timemap)
        report["merged"] = False
        if args.mode == "merge":
            jk, _ = _resolve_jk(args.sid, args.channel)
            if jk is None:
                log("警告: 対象の局に実況チャンネルがないため基準のコメントだけを使います")
            else:
                try:
                    target_xml = fetch_target_comments(f"jk{jk}", _normalize(args.start),
                                                       _normalize(args.end), args.api_url)
                except NicoJKExit as error:
                    # API側の400/404でも基準のコメントは使い続ける。
                    if error.code != EXIT_CHANNEL_NOT_FOUND:
                        raise
                    log("警告: 対象局のチャンネルが未対応のため基準のコメントだけを使います")
                else:
                    xml_str = merge_comments(xml_str, target_xml, args.mark_reference_color)
                    report["merged"] = True
    except (OSError, UnicodeError, ET.ParseError, ValueError, TypeError, InvalidOperation) as error:
        log(f"コメントの時刻変換・統合失敗: {error}")
        raise NicoJKExit(EXIT_ERROR)
    return xml_str


def _render_outputs(args, xml_str):
    """XMLを一時ファイルに書き、解像度ごとにdanmaku2assで配置してASSを後処理する。"""
    with tempfile.NamedTemporaryFile(
            mode="w", suffix=".xml", encoding="utf-8", delete=False) as tmp:
        tmp.write(xml_str)
        tmp_xml = tmp.name
    try:
        for width, height, path in args.output:
            fontsize = args.fontsize if args.fontsize is not None else default_fontsize(height)
            log(f"解像度={width}x{height} フォント={args.font!r} フォントサイズ={fontsize} "
                f"表示秒数={args.display_seconds} 不透明度={args.opacity}")
            run_danmaku2ass(tmp_xml, path, width, height, args.font, fontsize,
                            args.opacity, args.display_seconds)
            fixup_ass(path, width, height, fontsize, args.font)
            log(f"ASS出力完了: {path}")
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
              "outputs": [path for _, _, path in args.output]}
    try:
        xml_str = None
        timemap = None
        if args.timemap:
            try:
                timemap = read_timemap(args.timemap)
            except (OSError, UnicodeError, ValueError, InvalidOperation) as error:
                log(f"対応表の読み込み失敗: {error}")
                raise NicoJKExit(EXIT_ERROR)
        elif args.ref_wav:
            timemap = _align_audio(args, report)
        elif args.alt or args.ref_ts:
            if _prepare_reference(args, report, work):
                timemap = _align_audio(args, report)
        if timemap is not None:
            xml_str = _make_timemap(args, report, timemap)
        if xml_str is None:
            if (report.get("alt") or {}).get("target", {}).get("comments") == "unrelated":
                # 遅れネット・独自枠で基準を使えないとき、対象の局のコメントは別の番組のものなので付けない。
                log("遅れネット・独自枠で基準の録画を使えないため実況を付けません")
                report["reason"] = "unrelated_no_reference"
                raise NicoJKExit(EXIT_CHANNEL_NOT_FOUND)
            if args.timemap or args.ref_wav or args.alt or args.ref_ts:
                # 基準を使えないときは、基準の指定を外した通常の取得に戻る。
                report["reference_fallback"] = True
                args.mode = "replace"
            xml_str = _make_normal(args, report)
        report["comments"] = count_chats(xml_str)
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
                log(f"レポートを書けません: {error}")
    return EXIT_SUCCESS


def run_plan(args):
    """先行放送の録画を探す処理だけを行い、結果をJSONで出す。終了コードは0(見つからなくても0)、エラーは1。"""
    if args.sid is None:
        log("警告: --sidがないため放送を特定できません")
    program_start = args.program_start if args.program_start is not None else args.start
    cache_dir = args.cache_dir or default_cache_dir()
    finder = Finder(cache_dir, SyoboiClient(cache_dir, log=lambda m: log(m)), log=log)
    plan = finder.find(args.sid, float(program_start), args.search_dir, args.exclude)
    text = json.dumps(plan, ensure_ascii=False, indent=1)
    try:
        if args.report:
            with open(args.report, "w", encoding="utf-8") as f:
                f.write(text)
        else:
            print(text)
    except OSError as error:
        log(f"結果を書けません: {error}")
        return 1
    return 0


def run_align(args):
    """音声照合だけを行い、結果をJSONで出す。終了コードは成功0、照合の失敗1、その他のエラー2。"""
    try:
        from .align_audio import align_audio, align_result_json
        from .wav import read_wav
    except ImportError as error:
        log(f"numpyを読み込めません: {error}")
        return 2
    try:
        target, reference = read_wav(args.wav), read_wav(args.ref_wav)
    except (OSError, ValueError) as error:
        log(f"WAVを読めません: {error}")
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
        log(f"結果を書けません: {error}")
        return 2
    log(f"音声照合: {result['seconds']:.3f}秒 / 区間 {len(result['segments'])} / "
        + ("成功" if result["success"] else "失敗: " + result["reason"]))
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
    parser.error("不明なコマンドです")


if __name__ == "__main__":
    sys.exit(main())
