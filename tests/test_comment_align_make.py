"""make の方式D(コメント照合)の流れのテスト。コメントの取得は偽物にし、通信しない。"""

import contextlib
import io
import json
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))

from finder_fixture import EARLIER, TARGET  # noqa: E402
from nicojk_ass import cli, comments  # noqa: E402
from test_alt import AltRunner  # noqa: E402
from test_nicojk_ass import MakeRunner  # noqa: E402

KANA = "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん"
KANJI = "猫犬鳥魚空海山川森花雨雪風火水木金土日月星光影夢愛剣城王姫竜鬼神"
DURATION = 1500
OFFSET = 5400           # 基準の放送が対象より何秒後か
DELTA_CHANGE = 30       # 600秒のところで基準のずれが30秒小さくなる(CMの長さの違い)
SPLIT = 600


def katakana(text):
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in text)


def synthesize(target_start, offset, duration=DURATION, count=2):
    """場面ごとに固有の言葉を両局に書かせる。対象は「ｗｗｗ」、基準は「！！」を付けて区別する。
    SPLITから始まるDELTA_CHANGE秒は対象にだけあるCMで、コメントを置かない。SPLITより後ろは、基準のずれがDELTA_CHANGE秒小さい。"""
    rng = random.Random(1)
    target, reference = [], []
    position = 0.0
    while position < duration:
        if SPLIT <= position < SPLIT + DELTA_CHANGE:
            position += 4.0
            continue
        phrase = "".join(rng.choice(KANA + KANJI) for _ in range(8))
        delta = offset if position < SPLIT else offset - DELTA_CHANGE
        for _ in range(count):
            target.append((target_start + position + rng.uniform(0, 4), phrase + "ｗｗｗ"))
            reference.append((target_start + position + delta + rng.uniform(0, 4), katakana(phrase) + "！！"))
        position += 4.0
    # 基準の取得範囲の前後にも、どの時刻にも出る反応を置く
    for _ in range(300):
        reference.append((target_start + offset - 300 + rng.uniform(0, duration + 600), "w"))
        target.append((target_start + rng.uniform(0, duration), "草"))
    return sorted(target), sorted(reference)


def packet(rows, begin, end):
    chats = "".join(f'<chat date="{int(stamp)}" date_usec="{int(stamp % 1 * 1e6)}" no="{i}">{text}</chat>'
                    for i, (stamp, text) in enumerate(rows) if begin <= stamp <= end)
    return f"<packet>{chats}</packet>"


class Api:
    """チャンネルごとのコメントを範囲で切って返す偽のAPI。呼び出しも記録する。"""

    def __init__(self, rows_by_channel, failures=()):
        self.rows = rows_by_channel
        self.failures = set(failures)
        self.calls = []

    def __call__(self, channel, start, end, api_base):
        self.calls.append(channel)
        if channel in self.failures:
            raise comments.NicoJKExit(comments.EXIT_ERROR)
        return packet(self.rows.get(channel, []), start, end)


def finder_item(start):
    from finder_fixture import program
    import xml.etree.ElementTree as ET
    return ET.tostring(program(8028, 20, "2", start), encoding="unicode")


def texts(converted):
    return [chat.text for chat in converted[0][3]]


class DirectTests(MakeRunner):
    """--ref-sidと--ref-start/--ref-endを直接渡す方式D。"""

    def make(self, api, extra=(), spans=None, duration=DURATION):
        report = self.directory / "report.json"
        argv = ["make", "--sid", "151", "--start", str(TARGET), "--end", str(TARGET + duration),
                "--program-start", str(TARGET), "--ref-sid", "1064",
                "--ref-start", str(TARGET + OFFSET), "--ref-end", str(TARGET + OFFSET + duration),
                "--report", str(report), *extra]
        if spans is not None:
            path = self.directory / "spans.json"
            path.write_text(json.dumps({"spans": spans}), encoding="utf-8")
            argv += ["--spans", str(path)]
        code, converted, _, log = self.run_make(argv + self.output(), fetch=api)
        return code, converted, log, json.loads(report.read_text(encoding="utf-8"))

    def api(self, **kwargs):
        target, reference = synthesize(TARGET, OFFSET)
        return Api({"jk151": target, "jk5": reference}, **kwargs)

    def test_本編の区間ありは区間ごとにずれを推定して写す(self):
        api = self.api()
        code, converted, log, report = self.make(api, ["--mode", "merge"], spans=[(0, SPLIT), (SPLIT + 30, DURATION)])
        self.assertEqual(code, 0, log)
        detail = report["comment_align"]
        self.assertTrue(detail["cm"])
        self.assertEqual([round(part["delta"]) for part in detail["segments"]], [OFFSET, OFFSET - DELTA_CHANGE])
        self.assertEqual(report["source"], "comment_align")
        self.assertTrue(report["merged"])
        self.assertIn("コメント照合区間", log)
        self.assertEqual(api.calls, ["jk151", "jk5"])  # 対象の局の取得は1回
        # 統合: 基準の局(！！)も対象の局(ｗｗｗ)も出る
        self.assertTrue(any(t.endswith("！！") for t in texts(converted)))
        self.assertTrue(any(t.endswith("ｗｗｗ") for t in texts(converted)))

    def test_本編の区間なしは窓から区間を決める(self):
        code, converted, log, report = self.make(self.api())
        self.assertEqual(code, 0, log)
        detail = report["comment_align"]
        self.assertFalse(detail["cm"])
        self.assertEqual([round(part["delta"]) for part in detail["segments"]], [OFFSET, OFFSET - DELTA_CHANGE])

    def test_空の本編の区間は区間なしと同じ(self):
        code, _, log, report = self.make(self.api(), spans=[])
        self.assertEqual(code, 0, log)
        self.assertFalse(report["comment_align"]["cm"])
        self.assertEqual(len(report["comment_align"]["segments"]), 2)

    def test_置き換えでは基準のコメントだけで写った時刻が合う(self):
        code, converted, log, report = self.make(self.api(), spans=[(0, SPLIT), (SPLIT + DELTA_CHANGE, DURATION)])
        self.assertEqual(code, 0, log)
        self.assertFalse(report["merged"])
        self.assertTrue(texts(converted))
        self.assertTrue(all(t.endswith("！！") or t == "w" for t in texts(converted)))
        # 基準の「！！」のコメントは、対象の局が同じ場面を書いた時刻(±4秒)に写る
        target, _ = synthesize(TARGET, OFFSET)
        times = {t[:-3]: s - TARGET for s, t in target if t.endswith("ｗｗｗ")}
        checked = 0
        for chat in converted[0][3]:
            if chat.text.endswith("！！"):
                phrase = "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in chat.text[:-2])
                self.assertAlmostEqual(int(chat.get("vpos")) / 100, times[phrase], delta=6)
                checked += 1
        self.assertGreater(checked, 100)

    def test_推定できる区間が全くなければ対象局のコメントだけ(self):
        target, same = synthesize(TARGET, OFFSET)
        # 基準は、時刻は同じで本文の一致がないコメントにする
        rng = random.Random(9)
        reference = [(s, "".join(rng.choice(KANJI) for _ in range(6))) for s, _ in same]
        api = Api({"jk151": target, "jk5": reference})
        code, converted, log, report = self.make(api, ["--mode", "merge"], spans=[(0, DURATION)])
        self.assertEqual(code, 0, log)
        self.assertEqual(report["comment_align"]["segments"], [])
        self.assertIn("推定できませんでした", report["comment_align"]["reason"])
        self.assertTrue(report["reference_fallback"])
        self.assertEqual(report["source"], "api")
        self.assertTrue(all(t.endswith("ｗｗｗ") or t == "草" for t in texts(converted)))
        self.assertEqual(api.calls, ["jk151", "jk5"])  # 対象の局を取り直さない

    def test_コメントが少なすぎれば推定しない(self):
        target, reference = synthesize(TARGET, OFFSET)
        api = Api({"jk151": target[:20], "jk5": reference})
        code, converted, log, report = self.make(api)
        self.assertEqual(code, 0, log)
        self.assertIn("コメントが少なすぎます", report["comment_align"]["reason"])
        self.assertEqual(len(texts(converted)), 20)
        self.assertEqual(report["comment_align"]["segments"], [])

    def test_基準のコメントを取れなければ対象局のコメントだけ(self):
        api = self.api(failures={"jk5"})
        code, converted, log, report = self.make(api, ["--mode", "merge"], spans=[(0, DURATION)])
        self.assertEqual(code, 0, log)
        self.assertIn("基準の局のコメントを取得できません", report["comment_align"]["reason"])
        self.assertTrue(report["reference_fallback"])
        self.assertTrue(all(t.endswith("ｗｗｗ") or t == "草" for t in texts(converted)))

    def test_対象の局のコメントを取れなければエラー(self):
        code, _, log, report = self.make(self.api(failures={"jk151"}))
        self.assertEqual(code, 1, log)
        self.assertEqual(report["exit_code"], 1)

    def test_取得範囲は基準の放送の前後に探索範囲を足す(self):
        api = self.api()
        calls = []
        original = api.__call__

        def spy(channel, start, end, api_base):
            calls.append((channel, start, end))
            return original(channel, start, end, api_base)

        code, _, log, _ = self.make(spy)
        self.assertEqual(code, 0, log)
        self.assertEqual(calls, [("jk151", TARGET, TARGET + DURATION),
                                 ("jk5", TARGET + OFFSET - 300, TARGET + OFFSET + DURATION + 300)])

    def test_基準の局と時刻の指定の誤りを拒否する(self):
        for argv, message in ((["--ref-sid", "1064"], "--ref-start"),
                              (["--ref-start", "100", "--ref-end", "200"], "--ref-sid"),
                              (["--ref-sid", "1064", "--ref-start", "200", "--ref-end", "100"], "より後")):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    cli.main(["make", "--sid", "151", "--start", "100", "--end", "200", *argv] + self.output())
            self.assertIn(message, err.getvalue())


class SpansOptionTests(MakeRunner):
    """--spansは、方式Dを使わない経路では読むだけで無視する。不正なら終了コード1。"""

    def run_normal(self, content):
        spans = self.directory / "spans.json"
        spans.write_text(content, encoding="utf-8")
        report = self.directory / "report.json"
        argv = ["make", "--sid", "151", "--start", str(TARGET), "--end", str(TARGET + 60), "--spans", str(spans),
                "--report", str(report)] + self.output()
        fetch = lambda channel, start, end, base: '<packet><chat date="%d" no="1">あ</chat></packet>' % (TARGET + 1)
        code, _, _, log = self.run_make(argv, fetch=fetch)
        return code, json.loads(report.read_text(encoding="utf-8")), log

    def test_使わない経路では件数だけレポートに残す(self):
        code, report, log = self.run_normal('{"spans": [[0, 10.5], [20, 30]]}')
        self.assertEqual(code, 0, log)
        self.assertEqual(report["spans"], {"count": 2})
        self.assertNotIn("comment_align", report)

    def test_空の並びは受け付ける(self):
        code, report, log = self.run_normal('{"spans": []}')
        self.assertEqual(code, 0, log)
        self.assertEqual(report["spans"], {"count": 0})

    def test_不正な内容は終了コード1(self):
        for content in ("{", "[]", '{"spans": 1}', '{"spans": [[1]]}', '{"spans": [[5, 5]]}',
                        '{"spans": [[6, 5]]}', '{"spans": [["a", 5]]}', '{"spans": [[true, 5]]}',
                        '{"spans": [[0, NaN]]}', '{"spans": [[0, Infinity]]}'):
            with self.subTest(content=content):
                code, report, log = self.run_normal(content)
                self.assertEqual(code, 1, log)
                self.assertEqual(report["exit_code"], 1)


class AltTests(AltRunner):
    """--altで録画がない(または対象の音声がない)ときに、最初の候補で方式Dを使う。"""

    def setUp(self):
        super().setUp()
        self.target_rows, self.reference_rows = synthesize(TARGET, EARLIER - TARGET, 1800)
        self.atx_rows = []

    def api(self, channel, start, end, api_base):
        self.api_calls.append((channel, start, end))
        rows = {"jk151": self.target_rows, "jk5": self.reference_rows, "jk333": self.atx_rows}.get(channel, [])
        return packet(rows, start, end)

    def add_atx_first(self, comments):
        """AT-X(jk333)の放送を、テレ朝より早い最初の候補にする。コメントはcomments件。"""
        self.f.episodes = self.f.episodes.replace(
            "</ProgItems>", finder_item("2026-10-10 23:00:00") + "</ProgItems>")
        self.atx_rows = self.reference_rows[:comments]

    def test_録画がなければ最初の候補のコメントで方式D(self):
        code, converted, log, report = self.run_alt(self.search(), duration=1800)
        self.assertEqual(code, 0, log)
        self.assertEqual(self.decoder_calls(), [])
        self.assertEqual(report["source"], "comment_align")
        self.assertEqual(report["reference"]["channel"], "jk5")
        self.assertEqual(report["reference"]["start"], EARLIER)
        self.assertEqual(report["comment_align"]["center"], EARLIER - TARGET)
        self.assertGreaterEqual(len(report["comment_align"]["segments"]), 1)
        self.assertTrue(any(t.endswith("！！") for t in texts(converted)))
        self.assertTrue(any(t.endswith("ｗｗｗ") for t in texts(converted)))
        self.assertEqual([c[0] for c in self.api_calls], ["jk151", "jk5"])

    def test_録画があっても対象の音声がなければ方式D(self):
        self.add_recording()
        code, converted, log, report = self.run_alt(self.search(), wav=False, duration=1800)
        self.assertEqual(code, 0, log)
        self.assertEqual(self.decoder_calls(), [])
        self.assertEqual(report["source"], "comment_align")
        self.assertIsNotNone(report["alt"]["reference"])

    def test_最初の候補のコメントが少なければ次の候補で推定する(self):
        self.add_atx_first(1)
        code, converted, log, report = self.run_alt(self.search(), duration=1800)
        self.assertEqual(code, 0, log)
        detail = report["comment_align"]
        self.assertEqual([c["sid"] for c in detail["candidates"]], [333, 1064])
        self.assertIn("コメントが少なすぎます", detail["candidates"][0]["skipped"])
        self.assertIsNone(detail["candidates"][1]["skipped"])
        self.assertEqual(report["reference"]["channel"], "jk5")
        self.assertEqual(detail["reference"]["sid"], 1064)
        self.assertEqual(detail["center"], EARLIER - TARGET)
        self.assertIn("方式D: 候補 sid 333 を飛ばします", log)
        self.assertGreaterEqual(len(detail["segments"]), 1)
        self.assertEqual([c[0] for c in self.api_calls], ["jk151", "jk333", "jk5"])  # 対象は1回だけ

    def test_どの候補もコメントが足りなければ対象局のコメントだけ(self):
        self.add_atx_first(1)
        self.reference_rows = self.reference_rows[:5]
        code, converted, log, report = self.run_alt(self.search(), duration=1800)
        self.assertEqual(code, 0, log)
        detail = report["comment_align"]
        self.assertEqual([c["sid"] for c in detail["candidates"]], [333, 1064])
        self.assertTrue(all(c["skipped"] for c in detail["candidates"]))
        self.assertTrue(report["reference_fallback"])
        self.assertTrue(all(t.endswith("ｗｗｗ") or t == "草" for t in texts(converted)))

    def test_最初の候補で区間がなければ次の候補へは進まない(self):
        self.add_atx_first(5000)  # 件数は足りるが、時刻が合わないので一致しない
        rng = random.Random(3)
        self.atx_rows = [(s, "".join(rng.choice(KANJI) for _ in range(6))) for s, _ in self.reference_rows]
        code, converted, log, report = self.run_alt(self.search(), duration=1800)
        self.assertEqual(code, 0, log)
        detail = report["comment_align"]
        self.assertEqual([c["sid"] for c in detail["candidates"]], [333])
        self.assertEqual(detail["segments"], [])
        self.assertIn("推定できませんでした", detail["reason"])
        self.assertNotIn("jk5", [c[0] for c in self.api_calls])
        self.assertTrue(report["reference_fallback"])

    def test_遅れネットでは方式Dを使わず終了コード100(self):
        self.f.set_affiliate()
        code, converted, log, report = self.run_alt(self.search(), sid=3088, duration=1800)
        self.assertEqual(code, 100, log)
        self.assertEqual(self.api_calls, [])
        self.assertNotIn("comment_align", report)

    def test_遅れネットで録画があっても対象の音声がなければ終了コード100(self):
        self.f.set_affiliate()
        self.add_recording()
        code, _, log, report = self.run_alt(self.search(), sid=3088, wav=False, duration=1800)
        self.assertEqual(code, 100, log)
        self.assertEqual(report["reason"], "unrelated_no_reference")


if __name__ == "__main__":
    unittest.main()
