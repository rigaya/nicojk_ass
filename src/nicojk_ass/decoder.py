"""デコーダーの呼び出し(設計書4.5)。TSの主音声をWAVにする。第3段は--decoder amatsukaze=<パス>だけ。"""

import json
import os
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


def decode_amatsukaze(exe, ts_path, sid, wav_path):
    """`<exe> --mode nicojk_wav -i <TS> [-s <ServiceId>] -o <WAV>`を実行し、WAVのJSONの内容を返す。
    子プロセスの出力は、nicojk_assのログ(標準エラー)へ流す。失敗したらDecodeErrorを送出する。
    exeが.pyで終わるときはPythonで動かす(テストの偽のデコーダー用)。"""
    command = [sys.executable, exe] if exe.lower().endswith(".py") else [exe]
    command += ["--mode", "nicojk_wav", "-i", ts_path]
    if sid is not None:
        command += ["-s", str(sid)]
    command += ["-o", wav_path]
    json_path = wav_path + ".json"
    if os.path.exists(json_path):
        os.unlink(json_path)  # 前回の結果を読まないようにする
    log("デコード: " + " ".join(command))
    began = time.monotonic()
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as error:
        raise DecodeError(f"デコーダーを起動できません: {error}")
    for line in process.stdout:
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        if text:
            log("[デコーダー] " + text)
    code = process.wait()
    if code != 0:
        raise DecodeError(f"デコーダーが終了コード{code}で終わりました")
    try:
        with open(json_path, "r", encoding="utf-8") as file:
            info = json.load(file)
        result = {"start": float(info["start"]), "start_rounded": float(info["start_rounded"]),
                  "end": float(info["end"]), "duration": float(info["duration"]),
                  "has_time": bool(info["has_time"]), "sid": int(info["sid"])}
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise DecodeError(f"デコーダーの結果のJSONを読めません: {error}")
    if not os.path.exists(wav_path):
        raise DecodeError("デコーダーがWAVを書きませんでした")
    result["seconds"] = round(time.monotonic() - began, 3)
    return result
