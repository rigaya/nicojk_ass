"""コマンドラインの入口。サブコマンドはmakeとalign。"""

import argparse
import json
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation

from . import __version__, stations
from .comments import (DEFAULT_API_BASE, EXIT_CHANNEL_NOT_FOUND, EXIT_ERROR, EXIT_SUCCESS,
                       NicoJKExit, count_chats, fetch_api_comments, fetch_target_comments,
                       file_reference, inspect_comments, log, parse_time, read_comments_file,
                       rebase_file_vpos)
from .render import (DEFAULT_DURATION, DEFAULT_FONT, DEFAULT_OPACITY, default_fontsize,
                     fixup_ass, run_danmaku2ass)
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
    ref.add_argument("--mode", choices=("replace", "merge"), default="replace",
                     help="replaceは基準のコメントだけ、mergeは対象の局のコメントと統合する")
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
    elif args.ref_comments_file or args.ref_channel is not None or args.ref_sid is not None:
        parser.error("基準の指定には--timemapか--ref-wavが必要です")
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
        xml_str = fetch_api_comments(f"jk{ref_jk}", ref_start, ref_end, args.api_url)
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
        if timemap is not None:
            xml_str = _make_timemap(args, report, timemap)
        if xml_str is None:
            if args.timemap or args.ref_wav:
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
    if args.command == "make":
        _validate_make(parser, args)
        try:
            return run_make(args)
        except NicoJKExit as error:
            return error.code
    parser.error("不明なコマンドです")


if __name__ == "__main__":
    sys.exit(main())
