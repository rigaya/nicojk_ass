"""make --alt / --ref-ts / plan の流れのテスト。しょぼいカレンダーの通信とTSの見出しは偽物にし、
デコーダーは、WAVとJSONを書くだけの小さなPythonスクリプトにする。"""

import contextlib
import io
import json
import os
import shutil
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

try:
    import numpy as np
except ImportError:
    np = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from finder_fixture import EARLIER, TARGET, Fixture, programs, program  # noqa: E402
from nicojk_ass import cli, comments, decoder, finder, syoboi  # noqa: E402
from test_nicojk_ass import MakeRunner  # noqa: E402

if np is not None:
    from test_align_audio import noise, write_wav  # noqa: E402

FAKE_DECODER = textwrap.dedent('''
    import json, shutil, sys
    argv = sys.argv[1:]
    ts = argv[argv.index("-i") + 1]
    out = argv[argv.index("-o") + 1]
    spec = json.load(open(ts + ".fake.json", encoding="utf-8"))
    with open(spec["argv_log"], "a", encoding="utf-8") as log:
        log.write(json.dumps(argv, ensure_ascii=False) + "\\n")
    print("AMT [info] 偽のデコーダー", argv[:3], file=sys.stderr)
    print("AMT [warn] 偽の警告", file=sys.stderr)
    for i in range(spec.get("lines", 0)):
        print(f"AMT [info] 行 {i}", file=sys.stderr)
    if spec.get("exit"):
        sys.exit(spec["exit"])
    if spec.get("wav"):
        import os, wave
        if os.path.exists(spec["wav"]):
            shutil.copy(spec["wav"], out)
        else:
            with wave.open(out, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(b"\\0\\0" * 8000)
    if spec.get("json") is not None:
        json.dump(spec["json"], open(out + ".json", "w"))
''')

REFERENCE_JSON = {"start": EARLIER - 0.4, "start_rounded": EARLIER, "end": EARLIER + 100, "duration": 100,
                  "has_time": True, "sid": 1064}


class AltRunner(MakeRunner):
    def setUp(self):
        super().setUp()
        self.f = Fixture(self)
        self.decoder = self.directory / "fake_decoder.py"
        self.decoder.write_text(FAKE_DECODER, encoding="utf-8")
        self.argv_log = self.directory / "decoder_argv.jsonl"
        self.cache = self.directory / "cache"

    def api(self, channel, start, end, api_base):
        self.api_calls.append((channel, start, end))
        if self.api_failure:
            raise comments.NicoJKExit(1)
        date = {"jk5": EARLIER + 30, "jk151": TARGET + 5}.get(channel, TARGET + 1)
        return f'<packet><chat date="{date}" no="1">{channel}のコメント</chat></packet>'

    def add_recording(self, name="ゾンビさがしてます_02.ts", sid=1064, start=EARLIER, spec=None):
        path = self.f.add_recording(name, sid, start)
        spec = {"argv_log": str(self.argv_log), "wav": str(self.directory / "ref.wav"), "json": dict(REFERENCE_JSON),
                **(spec or {})}
        Path(path + ".fake.json").write_text(json.dumps(spec), encoding="utf-8")
        return path

    def prepare_wavs(self, shift=10):
        """基準の先頭にshift秒の別の音声があるので、基準の30秒は対象の(30-shift)秒になる。"""
        audio = noise(90, 7)
        write_wav(self.directory / "target.wav", audio)
        write_wav(self.directory / "ref.wav", np.concatenate([noise(shift, 8), audio]))

    def run_alt(self, args, sid=151, wav=True, decoder_arg=True, report=True, duration=90):
        self.api_calls = []
        self.api_failure = False
        f = self.f
        argv = ["make", "--sid", str(sid), "--start", str(TARGET), "--end", str(TARGET + duration),
                "--cache-dir", str(self.cache)]
        if wav:
            argv += ["--wav", str(self.directory / "target.wav")]
        if decoder_arg:
            argv += ["--decoder", f"amatsukaze={self.decoder}"]
        if report:
            argv += ["--report", str(self.directory / "report.json")]
        argv += args + self.output()
        client = lambda cache_dir, log: syoboi.SyoboiClient(cache_dir, f.fetch, lambda: f.now, f.sleep, log)
        make_finder = lambda cache_dir, client, log: finder.Finder(cache_dir, client, f.read_ts, f.channels,
                                                                  f.services, log)
        with mock.patch.object(cli, "SyoboiClient", client), mock.patch.object(cli, "Finder", make_finder):
            code, converted, api, log = self.run_make(argv, fetch=self.api)
        report_path = self.directory / "report.json"
        data = json.loads(report_path.read_text(encoding="utf-8")) if report and report_path.exists() else None
        return code, converted, log, data

    def search(self, *extra):
        return ["--alt", "merge", "--search-dir", self.f.root, "--exclude", self.f.source, *extra]

    def decoder_calls(self):
        if not self.argv_log.exists():
            return []
        return [json.loads(line) for line in self.argv_log.read_text(encoding="utf-8").splitlines()]


@unittest.skipIf(np is None, "numpyがありません")
class AltFoundTests(AltRunner):
    def test_見つけた録画をデコードして照合し基準と対象のコメントを統合する(self):
        self.prepare_wavs()
        reference = self.add_recording()
        code, converted, log, report = self.run_alt(self.search())
        self.assertEqual(code, 0, log)
        # 基準のコメント(基準の30秒)は対象の20秒、対象の局のコメントは5秒
        chats = {chat.text: chat.get("vpos") for chat in converted[0][3]}
        self.assertEqual(chats, {"jk5のコメント": "2000", "jk151のコメント": "500"})
        # 今のAmatsukazeの方式Aと同じ値: 丸めた先頭の時刻と、そこから音声の長さ分
        self.assertEqual(self.api_calls, [("jk5", EARLIER, EARLIER + 100), ("jk151", TARGET, TARGET + 90)])
        self.assertEqual(self.decoder_calls(), [["--mode", "nicojk_wav", "-i", reference, "-s", "1064", "-o",
                                                  report["decode"]["wav"]]])
        self.assertEqual(report["alt"]["reference"]["path"], reference)
        self.assertEqual(report["alt"]["target"]["comments"], "ok")
        self.assertEqual(report["decode"]["sid"], 1064)
        self.assertEqual(report["decode"]["start_rounded"], EARLIER)
        self.assertTrue(report["align"]["success"])
        self.assertTrue(report["merged"])
        self.assertEqual(report["schema"], 1)
        # 成功したときは警告の行と1行の要約だけで、AMT [info]の行は出さない
        self.assertIn("[デコーダー] AMT [warn] 偽の警告", log)
        self.assertNotIn("偽のデコーダー", log)
        self.assertIn("基準の音声をデコードしました: ゾンビさがしてます_02.ts / sid 1064 / 長さ 100.0秒 / ", log)

    def test_置き換えでは基準のコメントだけを使う(self):
        self.prepare_wavs()
        self.add_recording()
        code, converted, log, report = self.run_alt(["--alt", "replace", "--search-dir", self.f.root,
                                                     "--exclude", self.f.source])
        self.assertEqual(code, 0, log)
        self.assertEqual([chat.text for chat in converted[0][3]], ["jk5のコメント"])
        self.assertEqual([c[0] for c in self.api_calls], ["jk5"])
        self.assertFalse(report["merged"])

    def test_遅れネットの録画は統合を指定しても置き換える(self):
        self.prepare_wavs()
        f = self.f
        f.set_affiliate()
        self.add_recording()
        code, converted, log, report = self.run_alt(self.search(), sid=3088)
        self.assertEqual(code, 0, log)
        self.assertEqual(report["alt"]["target"]["comments"], "unrelated")
        self.assertEqual([chat.text for chat in converted[0][3]], ["jk5のコメント"])
        self.assertEqual([c[0] for c in self.api_calls], ["jk5"])

    def test_作業フォルダは終わったら消える(self):
        self.prepare_wavs()
        self.add_recording()
        code, _, log, report = self.run_alt(self.search())
        self.assertEqual(code, 0, log)
        self.assertFalse(os.path.exists(report["decode"]["wav"]))
        self.assertFalse(os.path.exists(os.path.dirname(report["decode"]["wav"])))

    def test_keep_workではWAVを残す(self):
        self.prepare_wavs()
        self.add_recording()
        code, _, log, report = self.run_alt(self.search("--keep-work"))
        self.assertEqual(code, 0, log)
        work = os.path.dirname(report["decode"]["wav"])
        self.addCleanup(shutil.rmtree, work, True)  # --keep-workで残った作業フォルダを片付ける
        self.assertTrue(os.path.exists(report["decode"]["wav"]))
        self.assertTrue(os.path.exists(report["decode"]["wav"] + ".json"))

    def test_work_dirを指定したら自分のファイルだけ消す(self):
        self.prepare_wavs()
        self.add_recording()
        work = self.directory / "work"
        work.mkdir()
        (work / "other.txt").write_text("消さない", encoding="utf-8")
        code, _, log, report = self.run_alt(self.search("--work-dir", str(work)))
        self.assertEqual(code, 0, log)
        self.assertEqual(report["decode"]["wav"], str(work / "reference.wav"))
        self.assertEqual(sorted(os.listdir(work)), ["other.txt"])

    def test_ref_tsはデコードして照合する(self):
        self.prepare_wavs()
        reference = self.add_recording()
        code, converted, log, report = self.run_alt(["--ref-ts", reference, "--ref-sid", "1064", "--mode", "merge"])
        self.assertEqual(code, 0, log)
        self.assertEqual({chat.text: chat.get("vpos") for chat in converted[0][3]},
                         {"jk5のコメント": "2000", "jk151のコメント": "500"})
        self.assertEqual(self.decoder_calls()[0][:5], ["--mode", "nicojk_wav", "-i", reference, "-s"])
        self.assertIsNone(report["alt"])
        self.assertEqual(self.f.requests, [])  # しょぼいカレンダーは使わない

    def test_ref_tsでref_sidを省くとsオプションを付けずJSONのServiceIdを使う(self):
        self.prepare_wavs()
        reference = self.add_recording()
        code, _, log, report = self.run_alt(["--ref-ts", reference])
        self.assertEqual(code, 0, log)
        self.assertNotIn("-s", self.decoder_calls()[0])
        self.assertEqual([c[0] for c in self.api_calls], ["jk5"])

    def test_照合に失敗したら対象局のコメントへ戻る(self):
        self.prepare_wavs()
        write_wav(self.directory / "ref.wav", noise(100, 99))
        self.add_recording()
        code, converted, log, report = self.run_alt(self.search())
        self.assertEqual(code, 0, log)
        self.assertEqual([chat.text for chat in converted[0][3]], ["jk151のコメント"])
        self.assertFalse(report["align"]["success"])
        self.assertTrue(report["reference_fallback"])

    def test_遅れネットで照合に失敗したら実況を付けない(self):
        self.prepare_wavs()
        write_wav(self.directory / "ref.wav", noise(100, 99))
        self.f.set_affiliate()
        self.add_recording()
        code, converted, log, report = self.run_alt(self.search(), sid=3088)
        self.assertEqual(code, 100, log)
        self.assertEqual(converted, [])
        self.assertEqual(self.api_calls, [])
        self.assertEqual(report["exit_code"], 100)


class AltFallbackTests(AltRunner):
    def test_録画が見つからなければ対象局のコメントだけ(self):
        code, converted, log, report = self.run_alt(self.search())
        self.assertEqual(code, 0, log)
        self.assertEqual([chat.text for chat in converted[0][3]], ["jk151のコメント"])
        self.assertEqual(self.decoder_calls(), [])
        self.assertIsNone(report["alt"]["reference"])
        self.assertEqual(report["alt"]["target"]["comments"], "ok")
        self.assertNotIn("decode", report)

    def test_遅れネットで録画が見つからなければ終了コード100(self):
        self.f.set_affiliate()
        code, converted, log, report = self.run_alt(self.search(), sid=3088)
        self.assertEqual(code, 100, log)
        self.assertEqual(self.api_calls, [])
        self.assertEqual(report["alt"]["target"]["comments"], "unrelated")
        self.assertEqual(report["reason"], "unrelated_no_reference")

    def test_放送を特定できなければ対象局のコメントだけ(self):
        self.f.range = programs()
        code, converted, log, report = self.run_alt(self.search())
        self.assertEqual(code, 0, log)
        self.assertEqual(report["alt"]["target"]["comments"], "unknown")
        self.assertEqual([chat.text for chat in converted[0][3]], ["jk151のコメント"])

    def test_しょぼいカレンダーの失敗は警告して対象局のコメントだけ(self):
        self.f.fetch_failure = OSError("通信失敗")
        code, converted, log, report = self.run_alt(self.search())
        self.assertEqual(code, 0, log)
        self.assertIn("通信失敗", report["alt"]["error"])
        self.assertEqual([chat.text for chat in converted[0][3]], ["jk151のコメント"])

    def test_対象のWAVがなければ警告して対象局のコメントだけ(self):
        self.add_recording()
        code, converted, log, report = self.run_alt(self.search(), wav=False)
        self.assertEqual(code, 0, log)
        self.assertIn("--wav", log)
        self.assertEqual(self.decoder_calls(), [])
        self.assertIsNotNone(report["alt"]["reference"])
        # 録画があっても対象の音声がなければ、最初の候補で方式Dを試す(コメントが少ないので対象局のコメントだけになる)
        self.assertIn("方式D", log)
        self.assertNotIn("decode", report)
        self.assertIn("コメントが少なすぎます", report["comment_align"]["reason"])
        self.assertEqual([chat.text for chat in converted[0][3]], ["jk151のコメント"])

    def test_decoderがなければ警告して対象局のコメントだけ(self):
        self.add_recording()
        code, converted, log, report = self.run_alt(self.search(), decoder_arg=False)
        self.assertEqual(code, 0, log)
        self.assertIn("--decoderがない", log)
        self.assertEqual([chat.text for chat in converted[0][3]], ["jk151のコメント"])

    def test_デコードの失敗は警告して対象局のコメントだけ(self):
        for spec, reason in (({"exit": 3}, "終了コード3"), ({"json": None}, "JSON"), ({"wav": None}, "WAV")):
            with self.subTest(spec=spec):
                self.setUp()
                self.add_recording(spec=spec)
                code, converted, log, report = self.run_alt(self.search())
                self.assertEqual(code, 0, log)
                self.assertFalse(report["decode"]["success"])
                self.assertIn(reason, report["decode"]["reason"])
                self.assertEqual([chat.text for chat in converted[0][3]], ["jk151のコメント"])

    def test_デコードの失敗では要約と出力の末尾30行を出す(self):
        self.add_recording(spec={"exit": 3, "lines": 50})
        code, converted, log, report = self.run_alt(self.search())
        self.assertEqual(code, 0, log)
        self.assertIn("基準の音声のデコードに失敗しました: ゾンビさがしてます_02.ts / sid 1064 / デコーダーが終了コード3で終わりました", log)
        self.assertIn("出力の末尾30行", log)
        self.assertIn("行 49", log)
        self.assertIn("行 20", log)
        self.assertNotIn("行 19", log)
        self.assertNotIn("基準の音声をデコードしました", log)

    def test_TOTがない基準は使わない(self):
        self.add_recording(spec={"json": {**REFERENCE_JSON, "start": 0, "start_rounded": 0, "has_time": False}})
        code, converted, log, report = self.run_alt(self.search())
        self.assertEqual(code, 0, log)
        self.assertFalse(report["decode"]["success"])
        self.assertEqual([chat.text for chat in converted[0][3]], ["jk151のコメント"])

    def test_遅れネットでデコードに失敗したら実況を付けない(self):
        self.f.set_affiliate()
        self.add_recording(spec={"exit": 1})
        code, converted, log, report = self.run_alt(self.search(), sid=3088)
        self.assertEqual(code, 100, log)
        self.assertEqual(converted, [])

    def test_ref_tsのデコード失敗は対象局のコメントだけ(self):
        reference = self.add_recording(spec={"exit": 1})
        code, converted, log, report = self.run_alt(["--ref-ts", reference])
        self.assertEqual(code, 0, log)
        self.assertEqual([chat.text for chat in converted[0][3]], ["jk151のコメント"])
        self.assertTrue(report["reference_fallback"])

    def test_作業フォルダは失敗しても消える(self):
        self.add_recording(spec={"exit": 1})
        _, _, log, report = self.run_alt(self.search())
        self.assertFalse(os.path.exists(os.path.join(self.f.root, "reference.wav")))
        self.assertEqual(len(report["alt"]["probed"]), 1)


class AltArgumentTests(AltRunner):
    def error(self, args):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            cli.main(["make", "--start", "100", "--end", "200", "--output", f"1280x720={self.directory}/o.ass", *args])
        return err.getvalue()

    def test_組み合わせの誤りを拒否する(self):
        d = str(self.f.root)
        cases = [(["--alt", "merge", "--search-dir", d, "--timemap", "t.json"], "同時に指定できません"),
                 (["--alt", "merge", "--search-dir", d, "--ref-wav", "r.wav"], "同時に指定できません"),
                 (["--ref-ts", "r.ts", "--timemap", "t.json"], "同時に指定できません"),
                 (["--alt", "merge", "--search-dir", d, "--ref-ts", "r.ts"], "同時に指定できません"),
                 (["--alt", "merge"], "--altには--search-dirが必要です"),
                 (["--search-dir", d], "--altが必要です"),
                 (["--alt", "merge", "--search-dir", d, "--mode", "replace"], "同時に指定できません"),
                 (["--alt", "merge", "--search-dir", d, "--comments-file", "c.xml"], "同時に指定できません"),
                 (["--ref-ts", "r.ts", "--ref-start", "1"], "同時に指定できません"),
                 (["--ref-sid", "1064"], "--ref-ts")]
        for args, message in cases:
            with self.subTest(args=args):
                self.assertIn(message, self.error(args))

    def test_decoderの形式を検査する(self):
        self.assertIn("amatsukaze=", self.error(["--decoder", "ffprobe"]))
        self.assertIn("同時に指定できません", self.error(["--ts", "a.ts"]))
        self.assertEqual(decoder.parse_decoder("amatsukaze=/a b/c"), ("amatsukaze", "/a b/c"))
        self.assertEqual(decoder.parse_decoder("ffmpeg"), ("ffmpeg", "ffmpeg"))
        self.assertEqual(decoder.parse_decoder("ffmpeg=/bin/ffmpeg"), ("ffmpeg", "/bin/ffmpeg"))


class PlanCommandTests(AltRunner):
    def run_plan(self, *args):
        f = self.f
        client = lambda cache_dir, log: syoboi.SyoboiClient(cache_dir, f.fetch, lambda: f.now, f.sleep, log)
        make_finder = lambda cache_dir, client, log: finder.Finder(cache_dir, client, f.read_ts, f.channels,
                                                                  f.services, log)
        argv = ["plan", "--sid", "151", "--start", str(TARGET), "--end", str(TARGET + 90),
                "--cache-dir", str(self.cache), "--search-dir", f.root, "--exclude", f.source, *args]
        out = io.StringIO()
        with mock.patch.object(cli, "SyoboiClient", client), mock.patch.object(cli, "Finder", make_finder), \
                contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(out):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_標準出力にJSONを出す(self):
        reference = self.f.add_recording("ゾンビさがしてます_02.ts", 1064, EARLIER)
        code, out, err = self.run_plan()
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertEqual(plan["schema"], 1)
        self.assertEqual(plan["reference"]["path"], reference)
        self.assertEqual(plan["probed"][0]["result"], "match")
        self.assertIn("別局実況", err)

    def test_reportへ書く(self):
        report = self.directory / "plan.json"
        code, out, _ = self.run_plan("--report", str(report))
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIsNone(json.loads(report.read_text(encoding="utf-8"))["reference"])

    def test_デコードも照合もしない(self):
        self.f.add_recording("ゾンビさがしてます_02.ts", 1064, EARLIER)
        with mock.patch.object(decoder, "decode_amatsukaze", side_effect=AssertionError("呼ばない")):
            code, _, _ = self.run_plan()
        self.assertEqual(code, 0)


class DecoderTests(AltRunner):
    def test_子プロセスの出力をログへ流し結果のJSONを読む(self):
        ts = self.add_recording()
        wav = str(self.directory / "out.wav")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            info = decoder.decode_amatsukaze(str(self.decoder), ts, 1064, wav)
        self.assertEqual(info["sid"], 1064)
        self.assertEqual(info["start_rounded"], EARLIER)
        self.assertTrue(info["has_time"])
        self.assertIn("[デコーダー] AMT [warn] 偽の警告", stderr.getvalue())
        self.assertIn("基準の音声をデコードしました", stderr.getvalue())

    def test_起動できなければDecodeError(self):
        with self.assertRaises(decoder.DecodeError):
            decoder.decode_amatsukaze(str(self.directory / "none"), "a.ts", None, str(self.directory / "o.wav"))

    def test_前回のJSONを読まない(self):
        ts = self.add_recording(spec={"json": None})
        wav = self.directory / "out.wav"
        (self.directory / "out.wav.json").write_text(json.dumps(REFERENCE_JSON), encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(decoder.DecodeError):
            decoder.decode_amatsukaze(str(self.decoder), ts, 1064, str(wav))


if __name__ == "__main__":
    unittest.main()


class ChildEnvTests(unittest.TestCase):
    """exeのときは、子プロセスのLD_LIBRARY_PATHをexeの起動前の値に戻す。"""

    def test_ソースのままなら環境変数をそのまま渡す(self):
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/a"}, clear=False):
            self.assertEqual(decoder.child_env()["LD_LIBRARY_PATH"], "/a")

    def test_exeではLD_LIBRARY_PATH_ORIGに戻す(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/tmp/_MEI1", "LD_LIBRARY_PATH_ORIG": "/usr/local/lib"}):
            env = decoder.child_env()
        self.assertEqual(env["LD_LIBRARY_PATH"], "/usr/local/lib")
        self.assertNotIn("LD_LIBRARY_PATH_ORIG", env)

    def test_exeでもとの値がなければ消す(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/tmp/_MEI1"}):
            os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
            env = decoder.child_env()
        self.assertNotIn("LD_LIBRARY_PATH", env)
