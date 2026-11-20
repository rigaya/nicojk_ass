import contextlib
import io
import json
import sys
import unittest
import wave
from pathlib import Path

try:
    import numpy as np
except ImportError:  # numpyがなければ音声照合のテストは飛ばす
    np = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nicojk_ass import cli  # noqa: E402
from test_nicojk_ass import MakeRunner, TempDirTestCase  # noqa: E402

if np is not None:
    from nicojk_ass import align_audio, wav  # noqa: E402

RATE = 8000


def noise(seconds, seed):
    return np.random.RandomState(seed).uniform(-0.5, 0.5, seconds * RATE).astype(np.float32)


def write_wav(path, samples, channels=1, rate=RATE, width=2):
    """floatの±1の目盛りの配列を、16bitのWAVにして書く(channels=2なら左右に同じ値)。"""
    pcm = np.clip(np.round(samples * 32768), -32768, 32767).astype("<i2")
    if channels == 2:
        pcm = np.repeat(pcm, 2)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.setframerate(rate)
        target.writeframes(pcm.tobytes())


@unittest.skipIf(np is None, "numpyがありません")
class AlignTests(unittest.TestCase):
    def test_同一音声は1区間でずれ0(self):
        audio = noise(210, 123)
        result = align_audio.align_audio(audio, audio)
        self.assertTrue(result["success"])
        self.assertEqual(len(result["segments"]), 1)
        part = result["segments"][0]
        self.assertLess(abs(part["delta"]), 0.01)
        self.assertLess(part["start"], 0.1)
        self.assertGreater(part["end"], 209.9)

    def test_一律のずれを照合する(self):
        audio = noise(210, 123)
        result = align_audio.align_audio(audio[2240:], audio)
        self.assertTrue(result["success"])
        self.assertEqual(len(result["segments"]), 1)
        self.assertLess(abs(result["segments"][0]["delta"] - 0.28), 0.01)

    def test_差し込み付き音声は3区間になり境界の誤差は0_1秒以内(self):
        audio, insertion = noise(210, 123), noise(30, 456)
        reference = np.concatenate([audio[:60 * RATE], insertion[:10 * RATE], audio[60 * RATE:120 * RATE],
                                    insertion[10 * RATE:], audio[120 * RATE:]])
        result = align_audio.align_audio(audio, reference)
        self.assertTrue(result["success"])
        self.assertEqual(len(result["segments"]), 3)
        for i, part in enumerate(result["segments"]):
            self.assertLess(abs(part["delta"] - (0, 10, 30)[i]), 0.01)
            self.assertLess(abs(part["start"] - i * 60.0), 0.1)
            self.assertLess(abs(part["end"] - (210 if i == 2 else (i + 1) * 60.0)), 0.1)

    def test_境目が8秒の切り出しの外側点でも1秒ずつ歩いて見つける(self):
        audio, insertion = noise(210, 123), noise(30, 456)
        for boundary in (64.7, 65.3):
            split = int(round(boundary * RATE))
            reference = np.concatenate([audio[:split], insertion[:10 * RATE], audio[split:]])
            result = align_audio.align_audio(audio, reference)
            self.assertTrue(result["success"])
            self.assertEqual(len(result["segments"]), 2)
            self.assertLess(abs(result["segments"][0]["end"] - boundary), 0.1)
            self.assertLess(abs(result["segments"][1]["start"] - boundary), 0.1)

    def test_失敗する場合(self):
        audio = noise(210, 123)
        self.assertFalse(align_audio.align_audio(audio, audio[:60 * RATE])["success"])
        self.assertFalse(align_audio.align_audio(noise(90, 1), noise(90, 2))["success"])
        silence = np.zeros(90 * RATE, dtype=np.float32)
        self.assertFalse(align_audio.align_audio(silence, silence)["success"])
        empty = align_audio.align_audio(np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32))
        self.assertFalse(empty["success"])
        self.assertTrue(empty["reason"])

    def test_長尺でもブロックの境界をまたいで照合できる(self):
        for duration in (1800, 3600):
            audio = noise(duration, duration)
            result = align_audio.align_audio(audio, audio)
            self.assertTrue(result["success"])
            self.assertEqual(len(result["segments"]), 1)
            self.assertLess(abs(result["segments"][0]["delta"]), 0.01)
            self.assertLess(result["segments"][0]["start"], 0.1)
            self.assertGreater(result["segments"][0]["end"], duration - 0.1)
            self.assertLess(result["seconds"], 10)


def segment(start, end, delta):
    return {"start": start, "end": end, "delta": delta, "probes": 2, "ratio": 2}


def map_time(entries, time):
    for reference_start, reference_end, target_start, target_end in entries:
        if reference_start <= time < reference_end:
            return target_start + (time - reference_start) * (target_end - target_start) / (
                reference_end - reference_start)
    return None


class TimeMapTests(unittest.TestCase):
    # make_timemapはnumpyを使わないが、align_audioをimportするためnumpyが要る。
    @unittest.skipIf(np is None, "numpyがありません")
    def test_区間の写像(self):
        entries = align_audio.make_timemap([segment(10, 40, 0), segment(70, 100, -20)], 120, 100)
        self.assertAlmostEqual(map_time(entries, 5), 5)
        self.assertAlmostEqual(map_time(entries, 20), 20)
        self.assertAlmostEqual(map_time(entries, 45), 55)
        self.assertAlmostEqual(map_time(entries, 50), 70)
        self.assertAlmostEqual(map_time(entries, 90), 110)
        self.assertIsNone(map_time(entries, -0.01))
        self.assertIsNone(map_time(entries, 100))

    @unittest.skipIf(np is None, "numpyがありません")
    def test_重なる区間は削り録画の範囲で切り詰める(self):
        overlap = align_audio.make_timemap([segment(10, 50, 0), segment(60, 90, -20)], 100, 100)
        for before, after in zip(overlap, overlap[1:]):
            self.assertGreaterEqual(after[0], before[1])
            self.assertGreaterEqual(after[2], before[3])
        self.assertAlmostEqual(map_time(overlap, 50), 70)
        clipped = align_audio.make_timemap([segment(0, 90, -10)], 90, 70)
        self.assertEqual(map_time(clipped, 0), 10)
        self.assertEqual(map_time(clipped, 69), 79)
        self.assertIsNone(map_time(clipped, 70))
        positive = align_audio.make_timemap([segment(20, 40, 10)], 50, 60)
        self.assertEqual(map_time(positive, 10), 0)
        self.assertEqual(map_time(positive, 55), 45)
        self.assertEqual(align_audio.make_timemap([], 10, 10), [])


@unittest.skipIf(np is None, "numpyがありません")
class WavTests(TempDirTestCase):
    def test_モノラルはint16を32768で割る(self):
        path = self.directory / "mono.wav"
        write_wav(path, np.array([0.5, -0.25, 0], dtype=np.float32))
        np.testing.assert_allclose(wav.read_wav(path), [0.5, -0.25, 0])

    def test_ステレオは左右の平均(self):
        path = self.directory / "stereo.wav"
        pcm = np.array([1000, 3000, -2000, 0], dtype="<i2")
        with wave.open(str(path), "wb") as target:
            target.setnchannels(2)
            target.setsampwidth(2)
            target.setframerate(RATE)
            target.writeframes(pcm.tobytes())
        np.testing.assert_allclose(wav.read_wav(path), [2000 / 32768, -1000 / 32768])

    def test_形式が違うWAVはエラー(self):
        write_wav(self.directory / "rate.wav", noise(1, 1), rate=16000)
        with self.assertRaises(ValueError):
            wav.read_wav(self.directory / "rate.wav")
        with wave.open(str(self.directory / "bit8.wav"), "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(1)
            target.setframerate(RATE)
            target.writeframes(b"\x80" * 100)
        with self.assertRaises(ValueError):
            wav.read_wav(self.directory / "bit8.wav")
        (self.directory / "broken.wav").write_bytes(b"not a wav")
        with self.assertRaises(ValueError):
            wav.read_wav(self.directory / "broken.wav")

    def test_f32は生のfloatのPCM(self):
        path = self.directory / "raw.f32"
        values = noise(1, 5)
        values.tofile(path)
        np.testing.assert_array_equal(wav.read_wav(path), values)


@unittest.skipIf(np is None, "numpyがありません")
class MakeRefWavTests(MakeRunner):
    def prepare(self, reference_audio):
        audio = noise(90, 7)
        write_wav(self.directory / "target.wav", audio)
        write_wav(self.directory / "ref.wav", reference_audio(audio))
        comments = self.write('<packet><chat date="1030" no="42">基準</chat></packet>', "ref.xml")
        return ["make", "--start", "200", "--end", "290", "--ref-start", "1000", "--ref-end", "1100",
                "--wav", str(self.directory / "target.wav"), "--ref-wav", str(self.directory / "ref.wav"),
                "--ref-comments-file", str(comments), "--report", str(self.directory / "report.json")] \
            + self.output()

    def test_照合に成功したら対応表で基準のコメントを写す(self):
        # 基準の先頭に10秒の別の音声があるので、基準の30秒は対象の20秒になる。
        argv = self.prepare(lambda audio: np.concatenate([noise(10, 8), audio]))
        code, converted, api, log = self.run_make(argv, fetch=AssertionError("API呼び出し禁止"))
        self.assertEqual(code, 0)
        self.assertEqual([chat.get("vpos") for chat in converted[0][3]], ["2000"])
        api.assert_not_called()
        report = json.loads((self.directory / "report.json").read_text(encoding="utf-8"))
        self.assertTrue(report["align"]["success"])
        self.assertEqual(len(report["align"]["segments"]), 1)
        self.assertAlmostEqual(report["align"]["segments"][0]["delta"], 10, delta=0.01)
        self.assertIn("実況音声照合:", log)

    def test_照合に失敗したら警告して対象局の通常の取得へ戻る(self):
        argv = self.prepare(lambda audio: noise(90, 9)) + ["--sid", "211"]
        code, converted, api, log = self.run_make(
            argv, fetch=lambda *a: '<packet><chat date="201" no="1">対象</chat></packet>')
        self.assertEqual(code, 0)
        self.assertIn("実況音声照合に失敗したため対象局へ戻ります: 一致した区間が対象の長さの5割未満です", log)
        api.assert_called_once()
        self.assertEqual(api.call_args.args[0], "jk211")
        self.assertEqual([chat.text for chat in converted[0][3]], ["対象"])
        report = json.loads((self.directory / "report.json").read_text(encoding="utf-8"))
        self.assertFalse(report["align"]["success"])
        self.assertTrue(report["reference_fallback"])

    def test_形式の違うWAVも対象局へ戻る(self):
        argv = self.prepare(lambda audio: audio) + ["--sid", "211"]
        write_wav(self.directory / "ref.wav", noise(5, 1), rate=16000)
        code, converted, api, log = self.run_make(argv, fetch=lambda *a: "<packet/>")
        self.assertEqual(code, 0)
        self.assertIn("実況音声照合に失敗したため対象局へ戻ります", log)
        api.assert_called_once()

    def test_引数の組み合わせの誤りを拒否する(self):
        base = self.prepare(lambda audio: audio)
        for extra, message in ((["--timemap", "x.json"], "同時に指定できません"),
                               (["--comments-file", "c.xml"], "同時に指定できません")):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit):
                    cli.main(base + extra)
            self.assertIn(message, err.getvalue())
        without_wav = [value for i, value in enumerate(base) if value != "--wav" and base[i - 1] != "--wav"]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit):
                cli.main(without_wav)
        self.assertIn("--ref-wavには--wavか--tsが必要です", err.getvalue())


@unittest.skipIf(np is None, "numpyがありません")
class AlignCommandTests(TempDirTestCase):
    def run_align(self, *args):
        with contextlib.redirect_stderr(io.StringIO()):
            return cli.main(["align", *args])

    def test_成功は0で対応表を含む結果を書く(self):
        audio = noise(90, 3)
        write_wav(self.directory / "t.wav", audio[RATE:])
        write_wav(self.directory / "r.wav", audio)
        report = self.directory / "result.json"
        code = self.run_align("--wav", str(self.directory / "t.wav"), "--ref-wav", str(self.directory / "r.wav"),
                              "--report", str(report))
        self.assertEqual(code, 0)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertTrue(data["success"])
        self.assertEqual(len(data["segments"]), 1)
        self.assertAlmostEqual(data["segments"][0]["delta"], 1, delta=0.01)
        self.assertEqual(len(data["timemap"][0]), 4)

    def test_照合の失敗は1_読めないWAVは2(self):
        write_wav(self.directory / "a.wav", noise(90, 1))
        write_wav(self.directory / "b.wav", noise(90, 2))
        report = self.directory / "result.json"
        self.assertEqual(self.run_align("--wav", str(self.directory / "a.wav"),
                                        "--ref-wav", str(self.directory / "b.wav"), "--report", str(report)), 1)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertFalse(data["success"])
        self.assertTrue(data["reason"])
        self.assertEqual(self.run_align("--wav", str(self.directory / "none.wav"),
                                        "--ref-wav", str(self.directory / "b.wav")), 2)


if __name__ == "__main__":
    unittest.main()
