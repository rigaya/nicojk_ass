"""外部向けの--tsとffmpegデコード。"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import wave
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nicojk_ass import cli, decoder  # noqa: E402


def write_wav(path, frames, rate=8000):
    with wave.open(path, "wb") as dest:
        dest.setnchannels(1)
        dest.setsampwidth(2)
        dest.setframerate(rate)
        dest.writeframes(b"\x01\x00" * frames)


class AlignWavTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_遅れて始まる音声には無音を足し映像の長さに揃える(self):
        path = os.path.join(self.tmp.name, "a.wav")
        write_wav(path, 8000)
        decoder.align_wav(path, 0.5, 2.0)
        with wave.open(path, "rb") as source:
            self.assertEqual(source.getnframes(), 16000)
            data = source.readframes(16000)
        self.assertEqual(data[:8000], b"\x00" * 8000)
        self.assertEqual(data[8000:8002], b"\x01\x00")

    def test_先行する音声は先頭を切る(self):
        path = os.path.join(self.tmp.name, "a.wav")
        write_wav(path, 8000)
        decoder.align_wav(path, -0.25, 0.5)
        with wave.open(path, "rb") as source:
            self.assertEqual(source.getnframes(), 4000)
            self.assertEqual(source.readframes(1), b"\x01\x00")

    def test_ずれが大きすぎると失敗する(self):
        path = os.path.join(self.tmp.name, "a.wav")
        write_wav(path, 800)
        with self.assertRaises(decoder.DecodeError):
            decoder.align_wav(path, 31, 1)


class DecodeFfmpegTests(unittest.TestCase):
    def test_ffmpegの出力を映像の先頭に合わせる(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wav = os.path.join(tmp.name, "out.wav")
        timeline = {"sid": 211, "start": 1000.25, "start_rounded": 1000, "end": 1002.25,
                    "duration": 2.0, "has_time": True, "audio_pid": 0x110, "audio_shift": 0.5}

        def fake_popen(command, **kwargs):
            write_wav(command[-1], 8000)
            process = mock.Mock()
            process.stdout = []
            process.wait.return_value = 0
            return process

        with mock.patch.object(decoder.subprocess, "Popen", side_effect=fake_popen):
            info = decoder.decode_ffmpeg("ffmpeg", "a.ts", 211, wav, timeline)
        self.assertEqual(info["sid"], 211)
        self.assertTrue(info["has_time"])
        with wave.open(wav, "rb") as source:
            self.assertEqual(source.getnframes(), 16000)
        saved = json.loads(Path(wav + ".json").read_text(encoding="utf-8"))
        self.assertEqual(saved["start_rounded"], 1000)
        self.assertEqual(saved["duration"], 2.0)

    def test_TOTがなければffmpegを起動しない(self):
        timeline = {"sid": 1, "start": 0, "start_rounded": 0, "end": 1, "duration": 1,
                    "has_time": False, "audio_pid": 1, "audio_shift": 0}
        with mock.patch.object(decoder.subprocess, "Popen", side_effect=AssertionError("起動しない")):
            info = decoder.decode_ffmpeg("ffmpeg", "a.ts", None, os.path.join("unused", "a.wav"), timeline)
        self.assertFalse(info["has_time"])


class TsCommandTests(unittest.TestCase):
    def test_tsから時刻と対象の音声を決める(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        timeline = {"sid": 211, "start": 1000.5, "start_rounded": 1001, "end": 1300.5,
                    "duration": 300.0, "has_time": True, "video_pid": 0x100, "audio_pid": 0x110,
                    "audio_shift": 0.0}
        seen = {}

        def fake_decode(kind, exe, ts, sid, wav, timeline=None):
            seen["call"] = (kind, exe, sid, wav)
            write_wav(wav, 8000)
            return {"start": 1000.5, "start_rounded": 1001, "end": 1300.5, "duration": 300.0,
                    "has_time": True, "sid": 211, "seconds": 0.1}

        args = cli.build_parser().parse_args(
            ["make", "--ts", "show.ts", "--alt", "merge", "--search-dir", tmp.name,
             "--decoder", "ffmpeg=/bin/ffmpeg", "--output", f"1280x720={tmp.name}/o.ass"])
        report = {"target": {"sid": None, "channel": None, "start": None, "end": None,
                              "program_start": None}}
        work = cli.WorkDir(tmp.name, True)
        with mock.patch.object(cli.tsinfo, "read_timeline", return_value=timeline), \
                mock.patch.object(cli.tsinfo, "read_ts_info", side_effect=cli.tsinfo.TsInfoError("ない")), \
                mock.patch.object(cli.decoder, "decode", side_effect=fake_decode), \
                contextlib.redirect_stderr(io.StringIO()):
            cli._apply_ts_target(args, report, work)
        self.assertEqual(args.sid, 211)
        self.assertEqual(args.start, Decimal("1000.5"))
        self.assertEqual(args.program_start, args.start)
        self.assertEqual(seen["call"][0], "ffmpeg")
        self.assertEqual(seen["call"][1], "/bin/ffmpeg")
        self.assertTrue(args.wav.endswith("target.wav"))


if __name__ == "__main__":
    unittest.main()
