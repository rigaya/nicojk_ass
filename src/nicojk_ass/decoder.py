"""デコーダーの呼び出し(設計書4.5)。TSの主音声をWAVにする。

amatsukaze=はAmatsukazeCLIの--mode nicojk_wav。ffmpegは外部向けで、主音声を8kHzのモノラルにし、
映像の先頭とのずれだけWAVの先頭を切るか無音を足す。ドロップはaresample=async=1が無音で埋める。
"""

import collections
import json
import math
import os
import re
import subprocess
import sys
import time
import wave

from .comments import log
from .tsinfo import TsInfoError, read_timeline


class DecodeError(Exception):
    pass


def parse_decoder(value):
    """--decoderの値を(種類, パス)にする。"""
    kind, sep, path = value.partition("=")
    if kind == "amatsukaze" and path:
        return kind, path
    if kind == "ffmpeg" and sep in ("", "="):
        return kind, path or "ffmpeg"
    raise ValueError("--decoderはamatsukaze=<パス>かffmpeg[=<パス>]の形で指定してください")


# AmatsukazeCLIの出力は「AMT [info] ...」の形で、警告とエラーだけをログに残す。
_PROBLEM_LINE = re.compile(r"^AMT \[(warn|warning|error|fatal)\]", re.IGNORECASE)
TAIL_LINES = 30
# AmatsukazeCLIは、WindowsではANSIのコードページ(日本語ならcp932)で、LinuxではUTF-8で出力する
CHILD_ENCODING = "mbcs" if sys.platform == "win32" else "utf-8"


def child_env():
    """子プロセスに渡す環境変数。

    PyInstallerのexe(Linux)は、同梱した共有ライブラリ(古いlibstdc++など)のフォルダをLD_LIBRARY_PATHに足して
    動く。そのまま子のAmatsukazeCLIに引き継ぐと、システムのlibstdc++を期待する子が起動できないので、
    exeが起動される前の値(LD_LIBRARY_PATH_ORIG)に戻す。もとがなければ消す。
    """
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        original = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if original is not None:
            env["LD_LIBRARY_PATH"] = original
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


def decode_amatsukaze(exe, ts_path, sid, wav_path):
    """`<exe> --mode nicojk_wav -i <TS> [-s <ServiceId>] -o <WAV>`を実行し、WAVのJSONの内容を返す。
    子プロセスの出力は、成功したときは警告・エラーの行と1行の要約だけ、失敗したときは要約と末尾30行を
    nicojk_assのログ(標準エラー)へ出す。失敗したらDecodeErrorを送出する。
    exeが.pyで終わるときはPythonで動かす(テストの偽のデコーダー用)。"""
    command = [sys.executable, exe] if exe.lower().endswith(".py") else [exe]
    command += ["--mode", "nicojk_wav", "-i", ts_path]
    if sid is not None:
        command += ["-s", str(sid)]
    command += ["-o", wav_path]
    json_path = wav_path + ".json"
    if os.path.exists(json_path):
        os.unlink(json_path)  # 前回の結果を読まないようにする
    began = time.monotonic()
    name = os.path.basename(ts_path)
    lines = collections.deque(maxlen=TAIL_LINES)  # 失敗したときに出す末尾だけを持つ

    def fail(reason):
        log(f"基準の音声のデコードに失敗しました: {name} / sid {sid} / {reason}")
        log("デコード: " + " ".join(command))
        if lines:
            log(f"[デコーダー] 出力の末尾{len(lines)}行:")
            for text in lines:
                log("[デコーダー] " + text)
        raise DecodeError(reason)

    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   env=child_env())
    except OSError as error:
        fail(f"デコーダーを起動できません: {error}")
    for line in process.stdout:
        text = line.decode(CHILD_ENCODING, "replace").rstrip("\r\n")
        if text:
            lines.append(text)
            if _PROBLEM_LINE.match(text):
                log("[デコーダー] " + text)
    code = process.wait()
    if code != 0:
        fail(f"デコーダーが終了コード{code}で終わりました")
    try:
        with open(json_path, "r", encoding="utf-8") as file:
            info = json.load(file)
        result = {"start": float(info["start"]), "start_rounded": float(info["start_rounded"]),
                  "end": float(info["end"]), "duration": float(info["duration"]),
                  "has_time": bool(info["has_time"]), "sid": int(info["sid"])}
    except (OSError, ValueError, KeyError, TypeError) as error:
        fail(f"デコーダーの結果のJSONを読めません: {error}")
    if not os.path.exists(wav_path):
        fail("デコーダーがWAVを書きませんでした")
    result["seconds"] = round(time.monotonic() - began, 3)
    log(f"基準の音声をデコードしました: {name} / sid {result['sid']} / 長さ {result['duration']:.1f}秒 / "
        f"{result['seconds']:.1f}秒")
    return result


# 音声の先頭と映像の先頭のずれがこれより大きいときは、PTSの読み違えとみて失敗にする。
MAX_AUDIO_SHIFT = 30.0
SAMPLE_RATE = 8000


def write_info(wav_path, info):
    """AmatsukazeCLIの--mode nicojk_wavと同じキーのJSONをWAVの隣に書く。"""
    payload = {"start": info["start"], "start_rounded": info["start_rounded"], "end": info["end"],
               "duration": info["duration"], "has_time": info["has_time"], "sid": info["sid"]}
    with open(wav_path + ".json", "w", encoding="utf-8") as file:
        json.dump(payload, file)
        file.write("\n")


def align_wav(path, shift_seconds, duration_seconds):
    """WAVのサンプル0を映像の先頭に合わせ、長さを映像の長さにする。

    shift_secondsが正なら音声が映像より遅れて始まるので、先頭に無音を足す。負なら先頭を切る。
    足りない分は無音、余る分は切り捨てる。16bit・8kHz・モノラルでないWAVはDecodeError。
    """
    try:
        with wave.open(path, "rb") as source:
            channels, width, rate = source.getnchannels(), source.getsampwidth(), source.getframerate()
            if source.getcomptype() != "NONE" or width != 2 or rate != SAMPLE_RATE or channels != 1:
                raise DecodeError(f"デコード結果が16bit・8kHz・モノラルではありません: {path}")
            pcm = source.readframes(source.getnframes())
    except wave.Error as error:
        raise DecodeError(f"デコード結果のWAVを読めません: {error}")
    if not math.isfinite(shift_seconds) or abs(shift_seconds) > MAX_AUDIO_SHIFT:
        raise DecodeError(f"映像と音声の先頭のずれが大きすぎます({shift_seconds:.3f}秒)")
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise DecodeError("映像の長さを求められません")
    shift = int(round(shift_seconds * SAMPLE_RATE))
    if shift > 0:
        pcm = b"\x00\x00" * shift + pcm
    elif shift < 0:
        pcm = pcm[(-shift) * 2:]
    target = math.ceil(duration_seconds * SAMPLE_RATE)
    frames = len(pcm) // 2
    if frames > target:
        pcm = pcm[:target * 2]
    elif frames < target:
        pcm += b"\x00\x00" * (target - frames)
    with wave.open(path, "wb") as dest:
        dest.setnchannels(1)
        dest.setsampwidth(2)
        dest.setframerate(SAMPLE_RATE)
        dest.writeframes(pcm)


def decode_ffmpeg(exe, ts_path, sid, wav_path, timeline=None):
    """ffmpegで主音声を8kHzのモノラルのWAVにし、映像の先頭に合わせる。

    サービスはtimeline(なければTSの見出し)の音声PIDで選ぶ。TOTがなければffmpegは起動せず、
    has_timeがfalseの情報だけを返す(WAVは書かない)。失敗したらDecodeErrorを送出する。
    """
    json_path = wav_path + ".json"
    if os.path.exists(json_path):
        os.unlink(json_path)
    try:
        info = timeline if timeline is not None else read_timeline(ts_path, sid)
    except (OSError, TsInfoError) as error:
        raise DecodeError(str(error))
    result = {"start": float(info["start"]), "start_rounded": float(info["start_rounded"]),
              "end": float(info["end"]), "duration": float(info["duration"]),
              "has_time": bool(info["has_time"]), "sid": int(info["sid"])}
    if not result["has_time"]:
        result["seconds"] = 0.0
        log(f"警告: {os.path.basename(ts_path)} にTOTまたはPCRがないため先頭の時刻を求められません")
        return result
    if info.get("audio_pid") is None or info.get("audio_shift") is None:
        raise DecodeError("主音声(AAC)がありません")
    if os.path.exists(wav_path):
        os.unlink(wav_path)
    command = [exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", ts_path,
               "-map", f"0:i:{info['audio_pid']}", "-ac", "1", "-ar", str(SAMPLE_RATE),
               "-c:a", "pcm_s16le", "-af", "aresample=async=1", "-y", wav_path]
    began = time.monotonic()
    lines = collections.deque(maxlen=TAIL_LINES)

    def fail(reason):
        log(f"音声のデコードに失敗しました: {os.path.basename(ts_path)} / sid {result['sid']} / {reason}")
        log("デコード: " + " ".join(command))
        if lines:
            log(f"[ffmpeg] 出力の末尾{len(lines)}行:")
            for text in lines:
                log("[ffmpeg] " + text)
        raise DecodeError(reason)

    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   env=child_env())
    except OSError as error:
        fail(f"ffmpegを起動できません: {error}")
    for line in process.stdout:
        text = line.decode(CHILD_ENCODING, "replace").rstrip("\r\n")
        if text:
            lines.append(text)
    code = process.wait()
    if code != 0:
        fail(f"ffmpegが終了コード{code}で終わりました")
    if not os.path.exists(wav_path):
        fail("ffmpegがWAVを書きませんでした")
    try:
        align_wav(wav_path, info["audio_shift"], result["duration"])
    except DecodeError as error:
        fail(str(error))
    write_info(wav_path, result)
    result["seconds"] = round(time.monotonic() - began, 3)
    log(f"音声をデコードしました: {os.path.basename(ts_path)} / sid {result['sid']} / "
        f"長さ {result['duration']:.1f}秒 / 映像とのずれ {info['audio_shift']:+.3f}秒 / "
        f"{result['seconds']:.1f}秒")
    return result


def decode(kind, exe, ts_path, sid, wav_path, timeline=None):
    """種類に応じてTSの主音声をWAVにする。"""
    if kind == "ffmpeg":
        return decode_ffmpeg(exe, ts_path, sid, wav_path, timeline)
    return decode_amatsukaze(exe, ts_path, sid, wav_path)
