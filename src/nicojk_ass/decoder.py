"""デコーダーの呼び出し(設計書4.5)。TSの主音声をWAVにする。第3段は--decoder amatsukaze=<パス>だけ。"""

import collections
import json
import os
import re
import subprocess
import sys
import time

from .comments import log


class DecodeError(Exception):
    pass


def parse_decoder(value):
    """--decoderの値を(種類, パス)にする。ffmpegは第6段。"""
    kind, _, path = value.partition("=")
    if kind != "amatsukaze" or not path:
        raise ValueError("--decoderはamatsukaze=<AmatsukazeCLIのパス>の形で指定してください")
    return kind, path


# AmatsukazeCLIの出力は「AMT [info] ...」の形で、警告とエラーだけをログに残す。
_PROBLEM_LINE = re.compile(r"^AMT \[(warn|warning|error|fatal)\]", re.IGNORECASE)
TAIL_LINES = 30


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
        text = line.decode("utf-8", "replace").rstrip("\r\n")
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
