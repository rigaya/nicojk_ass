import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nicojk_ass import cli, comments, render, stations, timemap  # noqa: E402

ENTRY = ROOT / "nicojk_ass.py"


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.directory = Path(self.tempdir.name)

    def write(self, content, name="comments.xml"):
        path = self.directory / name
        path.write_text(content, encoding="utf-8-sig")
        return path


class MakeRunner(TempDirTestCase):
    """cli.mainをプロセス内で動かし、danmaku2assへ渡るXMLを集める。"""

    def run_make(self, args, fetch=None, real_render=False):
        converted = []  # 解像度ごとの(幅, 高さ, フォントサイズ, chatの並び)

        def convert(xml_path, output_path, width, height, font, fontsize, opacity, duration):
            converted.append((width, height, fontsize, ET.parse(xml_path).getroot().findall("chat")))
            Path(output_path).write_text("[Events]\n", encoding="utf-8")

        patches = [mock.patch.object(comments, "fetch_comments_xml", side_effect=fetch)]
        if not real_render:
            patches += [mock.patch.object(cli, "run_danmaku2ass", side_effect=convert),
                        mock.patch.object(cli, "fixup_ass")]
        with contextlib.ExitStack() as stack:
            api = stack.enter_context(patches[0])
            for patch in patches[1:]:
                stack.enter_context(patch)
            log = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            code = cli.main(args)
        return code, converted, api, log.getvalue()

    def output(self, width=1280, height=720, name="out.ass"):
        return ["--output", f"{width}x{height}={self.directory / name}"]


class TimeParseTests(unittest.TestCase):
    def test_UNIX秒は整数と小数を受け付ける(self):
        self.assertEqual(str(comments.parse_time("1700000000")), "1700000000")
        self.assertEqual(comments.parse_time("100.125"), Decimal("100.125"))
        self.assertEqual(comments.parse_time("-5"), -5)

    def test_14桁はJSTとして読む(self):
        # 2026-11-19 12:34:56 JST = 03:34:56 UTC
        self.assertEqual(comments.parse_time("20261119123456"), comments.parse_time("2026-11-19T03:34:56Z"))

    def test_ISO8601はタイムゾーン省略時JST(self):
        jst = comments.parse_time("2026-11-19T12:34:56")
        self.assertEqual(jst, comments.parse_time("2026-11-19T12:34:56+09:00"))
        self.assertEqual(jst, comments.parse_time("2026-11-19T03:34:56Z"))
        self.assertEqual(comments.parse_time("2026-11-19T12:34:56-05:00") - jst, 14 * 3600)
        self.assertEqual(comments.parse_time("1970-01-01T09:00:00"), 0)
        self.assertEqual(comments.parse_time("2026-11-19T12:34:56.5"), Decimal(jst) + Decimal("0.5"))

    def test_不正な時刻を拒否する(self):
        for value in ("NaN", "inf", "abc", "2026-02-30T00:00:00", "20261319000000",
                      "2026-11-19T24:00:00", "2026-11-19T00:00:00+24:00", "1e30"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                comments.parse_time(value)


class StationsTests(unittest.TestCase):
    def test_ServiceIdからjk番号と局名を引く(self):
        jk, name = stations.lookup_jk(211)
        self.assertEqual(jk, 211)
        self.assertIn("ＢＳ１１", name)

    def test_表にないServiceIdはNone(self):
        self.assertIsNone(stations.lookup_jk(99999))


class LocalCommentsTests(TempDirTestCase):
    def test_XMLの本文と属性を維持してマイクロ秒から振り直す(self):
        path = self.write('<packet><chat date="1700000001" date_usec="125000" '
                          'vpos="999999" mail="ue red">あ&amp;い</chat></packet>')
        result = comments.rebase_file_vpos(comments.read_comments_file(path),
                                           Decimal("1700000000.25"), 1700000010)
        chat = ET.fromstring(result).find("chat")
        self.assertEqual(chat.get("vpos"), "88")
        self.assertEqual(chat.get("mail"), "ue red")
        self.assertEqual(chat.text, "あ&い")

    def test_JKLの見出しコメントと複数行を読み込む(self):
        path = self.write('<!-- NicoJKログ -->\n'
                          '<chat date="100" vpos="5">先頭</chat>\n'
                          '<chat date="110">末尾</chat>\n', "comments.jkl")
        root = comments.read_comments_file(path)
        self.assertEqual(comments.inspect_comments(root, 100, 110), (2, 100, 2))
        result = ET.fromstring(comments.rebase_file_vpos(root, 100, 110))
        self.assertEqual([chat.get("vpos") for chat in result], ["0", "1000"])

    def test_noがないコメントだけに元順序の連番を補う(self):
        root = ET.fromstring('<packet><chat date="99">範囲外</chat>'
                             '<chat date="100" no="42">既存番号</chat>'
                             '<chat date="101">番号なし</chat></packet>')
        result = ET.fromstring(comments.rebase_file_vpos(root, 100, 110))
        self.assertEqual([chat.get("no") for chat in result], ["42", "3"])

    def test_範囲の両端を残し丸める前に範囲外を除く(self):
        root = ET.fromstring('<packet>' + ''.join(
            '<chat date="100" date_usec="{}">{}</chat>'.format(usec, usec)
            for usec in (249999, 250000, 749999, 750000, 750001)) + '</packet>')
        result = ET.fromstring(comments.rebase_file_vpos(root, "100.25", "100.75"))
        self.assertEqual([chat.text for chat in result], ["250000", "749999", "750000"])
        self.assertEqual([chat.get("vpos") for chat in result], ["0", "50", "50"])

    def test_検査は最古の時刻と範囲内件数を返す(self):
        root = ET.fromstring('<packet><chat date="110"/><chat date="100" '
                             'date_usec="500000"/><chat date="130"/></packet>')
        self.assertEqual(comments.inspect_comments(root, 100, 110), (3, Decimal("100.5"), 2))

    def test_無関係なXMLと空packetはコメントなしになる(self):
        for content in ('<settings><chat date="100"/></settings>', '<packet/>',
                        '<packet><nested><chat date="100"/></nested></packet>'):
            with self.subTest(content=content):
                root = comments.read_comments_file(self.write(content))
                self.assertEqual(comments.inspect_comments(root, 100, 110), (0, 0, 0))

    def test_不正なXMLと時刻を拒否する(self):
        for content in ('<packet>', '<packet><chat/></packet>',
                        '<packet><chat date="NaN"/></packet>',
                        '<packet><chat date="100" date_usec="1000000"/></packet>',
                        '<packet><chat date="100" date_usec="-1"/></packet>'):
            with self.subTest(content=content):
                with self.assertRaises((ET.ParseError, ValueError)):
                    comments.read_comments_file(self.write(content))

    def test_ファイルの時刻の基準は半数以上が範囲内なら番組開始(self):
        # CLI(C++)にあったNicoJKFileReferenceのテストを移したもの
        self.assertEqual(comments.file_reference(2000, 1000, 4, 2), 2000)
        self.assertEqual(comments.file_reference(2000, 1000, 5, 2), 1000)
        self.assertEqual(comments.file_reference(2000, 1000, 0, 0), 1000)


class RebaseVposTests(unittest.TestCase):
    """API経路のvpos相対化は投稿者側のvposではなくdateを基準にする。"""

    @staticmethod
    def _positions(xml_str):
        return [(chat.text, int(chat.get("vpos"))) for chat in ET.fromstring(xml_str).findall("chat")]

    def test_先頭コメントのvposが外れ値でもdateから位置を決める(self):
        xml_str = """<packet>
<chat no="1" date="1000" date_usec="500000" vpos="0">a</chat>
<chat no="2" date="1010" date_usec="0" vpos="7200000">b</chat>
<chat no="3" date="1012" date_usec="250000" vpos="7200100">c</chat>
</packet>"""
        self.assertEqual(self._positions(comments.rebase_vpos(xml_str, 1000)),
                         [("a", 50), ("b", 1000), ("c", 1225)])

    def test_開始前と時刻不正のコメントを除外する(self):
        xml_str = """<packet>
<chat no="1" date="999" date_usec="990000" vpos="10">early</chat>
<chat no="2" vpos="20">nodate</chat>
<chat no="3" date="1001" vpos="30">ok</chat>
</packet>"""
        self.assertEqual(self._positions(comments.rebase_vpos(xml_str, 1000)), [("ok", 100)])


class SourceRangeTests(MakeRunner):
    """取得範囲の始まり = start + (comments-start - program-start) - comments-offset。
    CLI(C++)にあったNicoJKSourceStartのテストを移したもの。"""

    def fetch_range(self, extra):
        report = self.directory / "report.json"
        args = ["make", "--channel", "jk9", "--report", str(report)] + extra + self.output()
        code, _, api, _ = self.run_make(args, fetch=lambda *a: "<packet/>")
        self.assertEqual(code, 0)
        return json.loads(report.read_text(encoding="utf-8"))["fetch"], api

    def test_基準と番組開始と範囲のずれを足し引きする(self):
        fetch, api = self.fetch_range(["--start", "1970", "--end", "2030", "--program-start", "2000",
                                       "--comments-start", "1000", "--comments-offset", "1.25"])
        self.assertEqual((fetch["start"], fetch["end"]), (968.75, 1028.75))
        api.assert_called_once_with("jk9", 968, 1029, comments.DEFAULT_API_BASE)

    def test_負のoffsetは後ろへずらす(self):
        fetch, _ = self.fetch_range(["--start", "1970", "--end", "2030", "--program-start", "2000",
                                     "--comments-offset=-0.5"])
        self.assertEqual(fetch["start"], 1970.5)

    def test_番組開始の省略時は録画の先頭を番組開始とみなす(self):
        fetch, _ = self.fetch_range(["--start", "1970", "--end", "2030", "--comments-start", "1000"])
        self.assertEqual(fetch["start"], 1000)
        fetch, _ = self.fetch_range(["--start", "1970", "--end", "2030"])
        self.assertEqual(fetch["start"], 1970)

    def test_ISOと14桁の時刻も指定できる(self):
        fetch, _ = self.fetch_range(["--start", "2026-11-19T12:00:00", "--end", "20261119123000",
                                     "--comments-start", "20261119120000"])
        self.assertEqual(fetch["start"], 1795057200)
        self.assertEqual(fetch["end"], 1795057200 + 1800)


class FileSourceTests(MakeRunner):
    def converted_vpos(self, converted):
        return [[chat.get("vpos") for chat in item[3]] for item in converted]

    def test_ファイル経路はAPIも既存の相対化も呼ばない(self):
        path = self.write('<packet><chat date="101" vpos="9999">本文</chat></packet>')
        output = self.directory / "result.ass"
        with mock.patch.object(comments, "rebase_vpos") as rebase:
            code, converted, api, _ = self.run_make(
                ["make", "--comments-file", str(path), "--start", "100", "--end", "110",
                 "--comments-start", "100", "--output", f"1280x720={output}"],
                fetch=AssertionError("API呼び出し禁止"))
        self.assertEqual(code, 0)
        api.assert_not_called()
        rebase.assert_not_called()
        self.assertEqual(self.converted_vpos(converted), [["100"]])
        self.assertIn("[Events]", output.read_text(encoding="utf-8"))

    def test_存在しない入力ファイルは一般エラーになる(self):
        result = subprocess.run([sys.executable, str(ENTRY), "make", "--comments-file",
                                 str(self.directory / "missing.xml"), "--start", "100", "--end", "110",
                                 "--output", f"1280x720={self.directory / 'result.ass'}"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("読み込み失敗", result.stderr)

    def test_comments_startがなければ録画の範囲に半数以上入るとき番組開始に合わせる(self):
        path = self.write('<packet><chat date="1005">a</chat><chat date="1006">b</chat>'
                          '<chat date="9999">c</chat></packet>')
        code, converted, _, log = self.run_make(
            ["make", "--comments-file", str(path), "--start", "1000", "--end", "1010"] + self.output())
        self.assertEqual(code, 0)
        self.assertEqual(self.converted_vpos(converted), [["500", "600"]])
        self.assertNotIn("最初のコメントを番組開始に合わせます", log)

    def test_comments_startがなく範囲外ばかりなら最初のコメントを番組開始に合わせて警告する(self):
        path = self.write('<packet><chat date="5000">a</chat><chat date="5003" date_usec="500000">b</chat>'
                          '<chat date="5004">c</chat></packet>')
        code, converted, _, log = self.run_make(
            ["make", "--comments-file", str(path), "--start", "1000", "--end", "1010"] + self.output())
        self.assertEqual(code, 0)
        # 取得範囲は最初のコメント(5000)から10秒
        self.assertEqual(self.converted_vpos(converted), [["0", "350", "400"]])
        self.assertIn("最初のコメントを番組開始に合わせます", log)

    def test_comments_startとoffsetで取得範囲がずれる(self):
        path = self.write('<packet><chat date="1000">a</chat><chat date="1001">b</chat></packet>')
        # 取得範囲の始まり = 1000 + (900 - 1000) - (-100.5) = 1000.5
        code, converted, _, _ = self.run_make(
            ["make", "--comments-file", str(path), "--start", "1000", "--end", "1010",
             "--program-start", "1000", "--comments-start", "900", "--comments-offset=-100.5"] + self.output())
        self.assertEqual(code, 0)
        self.assertEqual(self.converted_vpos(converted), [["50"]])

    def test_自動で見つけたファイルにコメントがなければ通常の取得へ進む(self):
        path = self.write('<packet/>')
        source = '<packet><chat date="101" no="1">本文</chat></packet>'
        code, converted, api, log = self.run_make(
            ["make", "--comments-file", str(path), "--comments-file-auto", "--channel", "jk9",
             "--start", "100", "--end", "110"] + self.output(), fetch=lambda *a: source)
        self.assertEqual(code, 0)
        api.assert_called_once_with("jk9", 100, 110, comments.DEFAULT_API_BASE)
        self.assertEqual(self.converted_vpos(converted), [["100"]])
        self.assertIn("無視します", log)

    def test_自動で見つけたファイルでもコメントがあれば使う(self):
        path = self.write('<packet><chat date="101">本文</chat></packet>')
        code, converted, api, _ = self.run_make(
            ["make", "--comments-file", str(path), "--comments-file-auto", "--channel", "jk9",
             "--start", "100", "--end", "110"] + self.output(), fetch=AssertionError("API呼び出し禁止"))
        self.assertEqual(code, 0)
        api.assert_not_called()
        self.assertEqual(self.converted_vpos(converted), [["100"]])

    def test_自動でないファイルはコメントがなくても使いAPIへ進まない(self):
        path = self.write('<packet/>')
        code, converted, api, log = self.run_make(
            ["make", "--comments-file", str(path), "--channel", "jk9",
             "--start", "100", "--end", "110"] + self.output(), fetch=AssertionError("API呼び出し禁止"))
        self.assertEqual(code, 0)
        api.assert_not_called()
        self.assertEqual(self.converted_vpos(converted), [[]])
        self.assertIn("0件", log)

    def test_自動のファイルを捨てて通常の取得に進んでも局がなければ100(self):
        path = self.write('<packet/>')
        code, converted, api, _ = self.run_make(
            ["make", "--comments-file", str(path), "--comments-file-auto", "--sid", "99999",
             "--start", "100", "--end", "110"] + self.output(), fetch=AssertionError("API呼び出し禁止"))
        self.assertEqual(code, 100)
        self.assertEqual(converted, [])

    def test_ファイルで局がなくてもASSを作れる(self):
        path = self.write('<packet><chat date="101">本文</chat></packet>')
        code, converted, _, _ = self.run_make(
            ["make", "--comments-file", str(path), "--sid", "99999", "--comments-start", "100",
             "--start", "100", "--end", "110"] + self.output())
        self.assertEqual(code, 0)
        self.assertEqual(len(converted), 1)


class ApiSourceTests(MakeRunner):
    def test_APIは整数範囲を取得し小数指定時だけ追加調整する(self):
        source = ('<packet><chat date="101" vpos="1100">本文</chat>'
                  '<chat date="102" vpos="1200">末尾</chat></packet>')
        for start, end, expected in (("100", "102", ["100", "200"]),
                                     ("100.25", "101.75", ["75"])):
            with self.subTest(start=start):
                with mock.patch.object(comments, "rebase_vpos", wraps=comments.rebase_vpos) as rebase:
                    code, converted, api, _ = self.run_make(
                        ["make", "--channel", "jk9", "--start", start, "--end", end] + self.output(),
                        fetch=lambda *a: source)
                self.assertEqual(code, 0)
                api.assert_called_once_with("jk9", 100, 102, comments.DEFAULT_API_BASE)
                rebase.assert_called_once_with(source, 100)
                self.assertEqual([[chat.get("vpos") for chat in converted[0][3]]], [expected])

    def test_sidからjk番号を引く(self):
        code, _, api, _ = self.run_make(
            ["make", "--sid", "211", "--start", "100", "--end", "110"] + self.output(),
            fetch=lambda *a: "<packet/>")
        self.assertEqual(code, 0)
        api.assert_called_once_with("jk211", 100, 110, comments.DEFAULT_API_BASE)

    def test_channelはsidより優先する(self):
        _, _, api, _ = self.run_make(
            ["make", "--sid", "211", "--channel", "jk3", "--start", "100", "--end", "110"] + self.output(),
            fetch=lambda *a: "<packet/>")
        api.assert_called_once_with("jk3", 100, 110, comments.DEFAULT_API_BASE)

    def test_jk番号がなくファイルもなければ100でAPIを呼ばない(self):
        report = self.directory / "report.json"
        code, converted, api, _ = self.run_make(
            ["make", "--sid", "99999", "--start", "100", "--end", "110", "--report", str(report)] + self.output(),
            fetch=AssertionError("API呼び出し禁止"))
        self.assertEqual(code, 100)
        api.assert_not_called()
        self.assertEqual(converted, [])
        self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["exit_code"], 100)

    def test_APIの404は100その他のエラーは1(self):
        for error, expected in ((comments.NicoJKExit(100), 100), (comments.NicoJKExit(1), 1)):
            with self.subTest(code=expected):
                code, converted, _, _ = self.run_make(
                    ["make", "--channel", "jk9", "--start", "100", "--end", "110"] + self.output(),
                    fetch=error)
                self.assertEqual(code, expected)
                self.assertEqual(converted, [])

    def test_HTTPの400と404はチャンネルなしとして100を送出する(self):
        import urllib.error
        for status, code in ((400, 100), (404, 100), (500, 1)):
            with self.subTest(status=status):
                error = urllib.error.HTTPError("http://x", status, "err", {}, None)
                with mock.patch.object(comments.urllib.request, "urlopen", side_effect=error), \
                        contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(comments.NicoJKExit) as raised:
                    comments.fetch_comments_xml("jk9", 100, 110, comments.DEFAULT_API_BASE)
                self.assertEqual(raised.exception.code, code)

    def test_複数のoutputでも取得は1回で解像度ごとにフォントサイズが決まる(self):
        source = '<packet><chat date="101">本文</chat></packet>'
        code, converted, api, _ = self.run_make(
            ["make", "--channel", "jk9", "--start", "100", "--end", "110",
             "--output", f"1280x720={self.directory / 'a.ass'}",
             "--output", f"1920x1080={self.directory / 'b.ass'}"], fetch=lambda *a: source)
        self.assertEqual(code, 0)
        api.assert_called_once()
        self.assertEqual([item[:3] for item in converted], [(1280, 720, 37), (1920, 1080, 55)])
        self.assertTrue((self.directory / "a.ass").exists() and (self.directory / "b.ass").exists())

    def test_fontsizeを指定すれば解像度によらず使う(self):
        _, converted, _, _ = self.run_make(
            ["make", "--channel", "jk9", "--start", "100", "--end", "110", "--fontsize", "20",
             "--output", f"1280x720={self.directory / 'a.ass'}",
             "--output", f"1920x1080={self.directory / 'b.ass'}"], fetch=lambda *a: "<packet/>")
        self.assertEqual([item[2] for item in converted], [20, 20])

    def test_レポートに版と取得元と件数が入る(self):
        report = self.directory / "report.json"
        source = '<packet><chat date="101">本文</chat></packet>'
        self.run_make(["make", "--channel", "jk9", "--start", "100", "--end", "110",
                       "--report", str(report)] + self.output(), fetch=lambda *a: source)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], 1)
        self.assertEqual(data["source"], "api")
        self.assertEqual(data["comments"], 1)
        self.assertEqual(data["exit_code"], 0)
        self.assertEqual(data["fetch"]["channel"], "jk9")


class TimeMapTests(MakeRunner):
    def write_map(self, entries):
        path = self.directory / "timemap.json"
        path.write_text(json.dumps(entries), encoding="utf-8-sig")
        return path

    def write_comments(self):
        return self.write('<packet><chat date="1001" no="42" user_id="same">同じ本文</chat>'
                          '<chat date="1003" no="99">後半</chat></packet>')

    def args(self, extra):
        return ["make", "--start", "200", "--end", "210", "--ref-start", "1000", "--ref-end", "1010"] \
            + extra + self.output()

    def test_対応表をfloatの4つ組として読む(self):
        result = timemap.read_timemap(self.write_map([[0, 1.25, 0, 2.5], [1.25, 3, 2.5, 4]]))
        self.assertEqual(result[0], (0.0, 1.25, 0.0, 2.5))
        self.assertEqual(timemap.read_timemap(self.write_map([])), [])

    def test_半開区間で写して隙間と範囲外を捨て属性を保つ(self):
        entries = timemap.read_timemap(self.write_map([[1, 2, 10, 12], [3, 4, 15, 16]]))
        xml = '<packet>' + ''.join('<chat vpos="{}" no="42" date="1000" mail="ue">{}</chat>'.format(
            value, value) for value in (99, 100, 199, 200, 250, 300, 399, 400)) + '<chat>位置なし</chat></packet>'
        result = ET.fromstring(timemap.apply_timemap(xml, entries)).findall("chat")
        self.assertEqual([chat.text for chat in result], ["100", "199", "300", "399"])
        self.assertEqual([chat.get("vpos") for chat in result], ["1000", "1198", "1500", "1599"])
        self.assertTrue(all(chat.get("no") == "42" and chat.get("date") == "1000"
                            and chat.get("mail") == "ue" for chat in result))

    def test_隣接境界のコメントを二重に写さない(self):
        entries = timemap.read_timemap(self.write_map([[0, 1, 0, 2], [1, 2, 5, 6]]))
        result = ET.fromstring(timemap.apply_timemap(
            '<packet><chat vpos="-1"/><chat vpos="100"/><chat vpos="200"/></packet>', entries))
        self.assertEqual([chat.get("vpos") for chat in result], ["500"])
        self.assertEqual(len(ET.fromstring(timemap.apply_timemap('<packet><chat vpos="0"/></packet>', []))), 0)

    def test_統合は時刻順で既存番号と重複を残す(self):
        reference = '<packet><chat vpos="300" no="42" user_id="same">同じ本文</chat>' \
                    '<chat vpos="100" no="7">基準</chat></packet>'
        target = '<packet><chat vpos="100" no="42" user_id="same">同じ本文</chat></packet>'
        result = ET.fromstring(timemap.merge_comments(reference, target)).findall("chat")
        self.assertEqual([chat.get("vpos") for chat in result], ["100", "100", "300"])
        self.assertEqual([chat.get("no") for chat in result], ["7", "42", "42"])
        self.assertEqual(sum(chat.text == "同じ本文" for chat in result), 2)

    def test_基準の色の指定で基準だけ末尾に色を足す(self):
        reference = '<packet><chat vpos="100" mail="red ue big blue2 #112233">基準</chat>' \
                    '<chat vpos="200">色なし基準</chat></packet>'
        target = '<packet><chat vpos="300" mail="shita small cyan">対象</chat></packet>'
        chats = ET.fromstring(timemap.merge_comments(reference, target, "yellow")).findall("chat")
        self.assertEqual([chat.get("mail") for chat in chats],
                         ["red ue big blue2 #112233 yellow", "yellow", "shita small cyan"])
        self.assertEqual([chat.text for chat in chats], ["基準", "色なし基準", "対象"])

    def test_色の指定がなければ両局の色を保持する(self):
        reference = '<packet><chat vpos="100" mail="ue red">基準</chat></packet>'
        target = '<packet><chat vpos="200" mail="blue">対象</chat></packet>'
        chats = ET.fromstring(timemap.merge_comments(reference, target)).findall("chat")
        self.assertEqual([chat.get("mail") for chat in chats], ["ue red", "blue"])

    def test_mark_reference_colorは統合のときだけ効く(self):
        target = '<packet><chat date="201" no="88" mail="blue">対象</chat></packet>'
        merged = self.args(["--ref-comments-file", str(self.write_comments()), "--timemap",
                            str(self.write_map([[0, 10, 2, 12]])), "--mode", "merge", "--channel", "jk1",
                            "--mark-reference-color", "yellow"])
        _, converted, _, _ = self.run_make(merged, fetch=lambda *a: target)
        self.assertEqual([chat.get("mail") for chat in converted[0][3]], ["blue", "yellow", "yellow"])
        replaced = self.args(["--ref-comments-file", str(self.write_comments()), "--timemap",
                              str(self.write_map([[0, 10, 2, 12]])), "--mark-reference-color", "yellow"])
        _, converted, _, _ = self.run_make(replaced, fetch=AssertionError("API呼び出し禁止"))
        self.assertEqual([chat.get("mail") for chat in converted[0][3]], [None, None])

    def test_ファイルと対応表だけならAPIを使わず配置は1回(self):
        code, converted, api, _ = self.run_make(
            self.args(["--ref-comments-file", str(self.write_comments()),
                       "--timemap", str(self.write_map([[0, 2, 2, 6]]))]),
            fetch=AssertionError("API呼び出し禁止"))
        self.assertEqual(code, 0)
        self.assertEqual([chat.get("vpos") for chat in converted[0][3]], ["400"])
        self.assertEqual(converted[0][3][0].get("no"), "42")
        api.assert_not_called()
        self.assertEqual(len(converted), 1)

    def test_基準のAPIも既存の相対化の後で対応表を使う(self):
        source = '<packet><chat date="1001" vpos="1100" no="42">本文</chat></packet>'
        with mock.patch.object(comments, "rebase_vpos", wraps=comments.rebase_vpos) as rebase:
            code, converted, api, _ = self.run_make(
                self.args(["--ref-channel", "jk9", "--timemap", str(self.write_map([[0, 2, 10, 12]]))]),
                fetch=lambda *a: source)
        self.assertEqual(code, 0)
        self.assertEqual([chat.get("vpos") for chat in converted[0][3]], ["1100"])
        api.assert_called_once_with("jk9", 1000, 1010, comments.DEFAULT_API_BASE)
        rebase.assert_called_once_with(source, 1000)

    def test_基準の局のコメントを取れなければ対象の局へ戻る(self):
        def fetch(channel, *rest):
            if channel == "jk9":
                raise comments.NicoJKExit(comments.EXIT_ERROR)
            return '<packet><chat date="201" vpos="0">対象</chat></packet>'
        code, converted, api, log = self.run_make(
            self.args(["--channel", "jk1", "--ref-channel", "jk9",
                       "--timemap", str(self.write_map([[0, 2, 10, 12]]))]), fetch=fetch)
        self.assertEqual(code, 0)
        self.assertEqual([chat.text for chat in converted[0][3]], ["対象"])
        self.assertEqual([call.args[0] for call in api.call_args_list], ["jk9", "jk1"])
        self.assertIn("基準の局のコメントを取得できない", log)

    def test_ref_sidで基準の局を引く(self):
        _, _, api, _ = self.run_make(
            self.args(["--ref-sid", "211", "--timemap", str(self.write_map([[0, 2, 10, 12]]))]),
            fetch=lambda *a: "<packet/>")
        api.assert_called_once_with("jk211", 1000, 1010, comments.DEFAULT_API_BASE)

    def test_統合する対象も既存API経路を使い配置前に混ぜる(self):
        target = ('<packet><chat date="201" vpos="500" no="42" user_id="same">同じ本文</chat>'
                  '<chat date="203" vpos="700" no="88">対象後半</chat></packet>')
        argv = self.args(["--ref-comments-file", str(self.write_comments()),
                          "--timemap", str(self.write_map([[0, 10, 2, 12]])),
                          "--mode", "merge", "--channel", "jk1"])
        with mock.patch.object(comments, "rebase_vpos", wraps=comments.rebase_vpos) as rebase:
            code, converted, api, _ = self.run_make(argv, fetch=lambda *a: target)
        chats = converted[0][3]
        self.assertEqual(code, 0)
        self.assertEqual([chat.get("vpos") for chat in chats], ["100", "300", "300", "500"])
        self.assertEqual([chat.get("no") for chat in chats], ["42", "42", "88", "99"])
        self.assertEqual(sum(chat.text == "同じ本文" for chat in chats), 2)
        api.assert_called_once_with("jk1", 200, 210, comments.DEFAULT_API_BASE)
        rebase.assert_called_once_with(target, 200)

    def test_対象局未対応だけは警告して基準だけを使う(self):
        argv = self.args(["--ref-comments-file", str(self.write_comments()),
                          "--timemap", str(self.write_map([[0, 10, 0, 10]])),
                          "--mode", "merge", "--channel", "jk1"])
        code, converted, _, log = self.run_make(argv, fetch=comments.NicoJKExit(100))
        self.assertEqual(code, 0)
        self.assertEqual([chat.get("vpos") for chat in converted[0][3]], ["100", "300"])
        self.assertIn("未対応", log)
        code, converted, _, _ = self.run_make(argv, fetch=comments.NicoJKExit(1))
        self.assertEqual(code, 1)
        self.assertEqual(converted, [])

    def test_対象局にjk番号がなければ警告して基準だけを使う(self):
        argv = self.args(["--ref-comments-file", str(self.write_comments()),
                          "--timemap", str(self.write_map([[0, 10, 0, 10]])),
                          "--mode", "merge", "--sid", "99999"])
        code, converted, api, log = self.run_make(argv, fetch=AssertionError("API呼び出し禁止"))
        self.assertEqual(code, 0)
        api.assert_not_called()
        self.assertEqual(len(converted[0][3]), 2)
        self.assertIn("対象の局に実況チャンネルがない", log)

    def test_基準の局にjk番号がなければ通常の取得に戻る(self):
        source = '<packet><chat date="201" no="1">対象</chat></packet>'
        argv = self.args(["--ref-sid", "99999", "--timemap", str(self.write_map([[0, 10, 0, 10]])),
                          "--channel", "jk1", "--mode", "merge"])
        code, converted, api, log = self.run_make(argv, fetch=lambda *a: source)
        self.assertEqual(code, 0)
        api.assert_called_once_with("jk1", 200, 210, comments.DEFAULT_API_BASE)
        self.assertEqual([chat.get("vpos") for chat in converted[0][3]], ["100"])
        self.assertIn("基準を使いません", log)

    def test_対応表の読み込み失敗は1(self):
        code, converted, _, _ = self.run_make(
            self.args(["--ref-comments-file", str(self.write_comments()),
                       "--timemap", str(self.directory / "none.json")]))
        self.assertEqual(code, 1)
        self.assertEqual(converted, [])

    def test_対応表で写したコメントも実変換で指定時刻に出る(self):
        output = self.directory / "result.ass"
        code, _, _, _ = self.run_make(
            ["make", "--start", "1000", "--end", "1010", "--ref-start", "1000", "--ref-end", "1010",
             "--ref-comments-file", str(self.write_comments()),
             "--timemap", str(self.write_map([[0, 2, 2, 4]])), "--output", f"1280x720={output}"],
            fetch=AssertionError("API呼び出し禁止"), real_render=True)
        self.assertEqual(code, 0)
        dialogues = [line for line in output.read_text(encoding="utf-8").splitlines()
                     if line.startswith("Dialogue:")]
        self.assertEqual(len(dialogues), 1)
        self.assertIn(",0:00:03.00,", dialogues[0])
        self.assertIn("同じ本文", dialogues[0])


class RealRenderTests(MakeRunner):
    def test_noなしXMLとJKLも実変換でASSに残る(self):
        for suffix in ("xml", "jkl"):
            with self.subTest(suffix=suffix):
                body = '<chat date="101">番号なしのコメント</chat>'
                contents = '<packet>' + body + '</packet>' if suffix == "xml" else '<!-- NicoJK -->\n' + body
                path = self.write(contents, "comments." + suffix)
                output = self.directory / (suffix + ".ass")
                code, _, api, _ = self.run_make(
                    ["make", "--comments-file", str(path), "--start", "100", "--end", "110",
                     "--comments-start", "100", "--output", f"1280x720={output}"],
                    fetch=AssertionError("API呼び出し禁止"), real_render=True)
                self.assertEqual(code, 0)
                dialogue = [line for line in output.read_text(encoding="utf-8").splitlines()
                            if line.startswith("Dialogue:")]
                self.assertEqual(len(dialogue), 1)
                self.assertIn(",0:00:01.00,", dialogue[0])
                self.assertIn("番号なしのコメント", dialogue[0])

    def test_複数のoutputがそれぞれの解像度のASSになる(self):
        path = self.write('<packet><chat date="101">本文</chat></packet>')
        small, large = self.directory / "s.ass", self.directory / "l.ass"
        code, _, _, _ = self.run_make(
            ["make", "--comments-file", str(path), "--start", "100", "--end", "110", "--comments-start", "100",
             "--output", f"1280x720={small}", "--output", f"1920x1080={large}"],
            fetch=AssertionError("API呼び出し禁止"), real_render=True)
        self.assertEqual(code, 0)
        self.assertIn("PlayResX: 1280", small.read_text(encoding="utf-8"))
        self.assertIn("PlayResX: 1920", large.read_text(encoding="utf-8"))
        self.assertIn(",55,", large.read_text(encoding="utf-8"))


class ServerFormatTests(MakeRunner):
    """過去ログと同じXML。4:00 JSTはUNIX秒1729969200(2024-10-27)。"""

    THREAD = 1729969200

    def test_スレッド開始は4時JSTで前日の深夜も含む(self):
        self.assertEqual(comments.jikkyo_thread_start(self.THREAD), self.THREAD)
        self.assertEqual(comments.jikkyo_thread_start(self.THREAD + 86400), self.THREAD + 86400)
        self.assertEqual(comments.jikkyo_thread_start(self.THREAD - 1), self.THREAD - 86400)
        self.assertEqual(comments.server_vpos(Decimal("1730000000.137660")), 3080014)

    def test_丸めの範囲内は元のdateを残し範囲外は対象の時刻へ直す(self):
        origin = self.THREAD
        xml = ('<packet>'
               '<chat thread="lv1" no="7" vpos="3080014" date="1730000000" date_usec="137660" '
               'user_id="abc" mail="184" anonymity="1">あ&amp;い</chat>'
               '<chat vpos="0" date="1" date_usec="0" score="0">先頭</chat>'
               '<chat vpos="bad">捨てる</chat>'
               '</packet>')
        # vpos 3080014 は origin から 30800.14秒。元の時刻との差は0.00234秒なので残す。
        text = comments.format_server_comments(xml, origin)
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<packet>\n'))
        self.assertTrue(text.endswith("</packet>\n"))
        chats = ET.fromstring(text).findall("chat")
        self.assertEqual(len(chats), 2)
        self.assertEqual(chats[0].get("date"), "1730000000")
        self.assertEqual(chats[0].get("date_usec"), "137660")
        self.assertEqual(chats[0].get("vpos"), "3080014")
        self.assertEqual(chats[0].get("thread"), "lv1")
        self.assertEqual(chats[0].get("mail"), "184")
        self.assertEqual(chats[0].text, "あ&い")
        self.assertEqual(chats[1].get("date"), str(origin))
        self.assertEqual(chats[1].get("date_usec"), "0")
        self.assertEqual(chats[1].get("vpos"), "0")
        self.assertEqual(chats[1].get("score"), "0")
        before = comments.format_server_comments(
            '<packet><chat vpos="0" date="1">深夜</chat></packet>', self.THREAD - 3600)
        chat = ET.fromstring(before).find("chat")
        self.assertEqual(chat.get("date"), str(self.THREAD - 3600))
        self.assertEqual(chat.get("vpos"), "8280000")

    def test_統合結果を過去ログのXMLで書きASSは作らない(self):
        reference = self.write('<packet><chat thread="lv9" no="7" date="5005" date_usec="0" '
                               'mail="184" user_id="ref">基準&amp;</chat></packet>', "ref.xml")
        target = (f'<packet><chat thread="lv1" no="3" date="{self.THREAD + 2}" date_usec="250000" '
                  'mail="184" user_id="tgt">対象</chat></packet>')
        path = self.directory / "merged.xml"
        report = self.directory / "report.json"
        # 基準の5秒を、対象の先頭から30805秒(12:33:25 JST)へ写す。
        code, converted, api, log = self.run_make(
            ["make", "--start", str(self.THREAD), "--end", str(self.THREAD + 40000),
             "--ref-start", "5000", "--ref-end", "5020", "--ref-comments-file", str(reference),
             "--timemap", str(self.write('[[0, 20, 30800, 30820]]', "timemap.json")),
             "--mode", "merge", "--channel", "jk1", "--comments-output", str(path),
             "--report", str(report)],
            fetch=lambda *a: target)
        self.assertEqual(code, 0, log)
        self.assertEqual(converted, [])
        api.assert_called_once()
        chats = ET.fromstring(path.read_text(encoding="utf-8")).findall("chat")
        self.assertEqual([chat.text for chat in chats], ["対象", "基準&"])
        self.assertEqual(chats[0].get("date"), str(self.THREAD + 2))
        self.assertEqual(chats[0].get("date_usec"), "250000")
        self.assertEqual(chats[0].get("vpos"), "225")
        self.assertEqual(chats[1].get("date"), "1730000005")
        self.assertEqual(chats[1].get("date_usec"), "0")
        self.assertEqual(chats[1].get("vpos"), "3080500")
        self.assertEqual(chats[1].get("thread"), "lv9")
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(data["comments_output"], str(path))
        self.assertEqual(data["outputs"], [])
        self.assertEqual(data["comments"], 2)
        self.assertTrue(data["merged"])

    def test_取得範囲をずらしたファイルは表示される絶対時刻で書く(self):
        source = self.write('<packet><chat date="1000" date_usec="0" mail="184">本文</chat></packet>')
        path = self.directory / "out.xml"
        code, converted, api, log = self.run_make(
            ["make", "--comments-file", str(source), "--start", "1000", "--end", "1100",
             "--comments-start", "1000", "--comments-offset", "10",
             "--comments-output", str(path)],
            fetch=AssertionError("API呼び出し禁止"))
        self.assertEqual(code, 0, log)
        self.assertEqual(converted, [])
        api.assert_not_called()
        chat = ET.fromstring(path.read_text(encoding="utf-8")).find("chat")
        self.assertEqual(chat.get("date"), "1010")
        self.assertEqual(chat.get("date_usec"), "0")

    def test_XMLとASSを同時に書ける(self):
        source = self.write('<packet><chat date="101" date_usec="0">本文</chat></packet>')
        xml_path = self.directory / "out.xml"
        code, converted, _, log = self.run_make(
            ["make", "--comments-file", str(source), "--start", "100", "--end", "110",
             "--comments-start", "100", "--comments-output", str(xml_path)] + self.output(),
            fetch=AssertionError("API呼び出し禁止"))
        self.assertEqual(code, 0, log)
        self.assertEqual(len(converted), 1)
        chat = ET.fromstring(xml_path.read_text(encoding="utf-8")).find("chat")
        self.assertEqual(chat.get("date"), "101")
        self.assertEqual(chat.get("date_usec"), "0")


class CommandLineTests(TempDirTestCase):
    def test_versionを表示する(self):
        result = subprocess.run([sys.executable, str(ENTRY), "--version"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("nicojk_ass", result.stdout)

    def test_パッケージとしても実行できる(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        result = subprocess.run([sys.executable, "-m", "nicojk_ass", "--version"],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0)

    def test_不正な引数は終了コード2(self):
        for args in (["make", "--start", "100", "--end", "110"],  # --outputなし
                     ["make", "--start", "110", "--end", "100", "--output", "1280x720=x"],
                     ["make", "--start", "100", "--end", "110", "--output", "bad"],
                     ["make", "--start", "100", "--end", "110", "--output", "1280x720=x", "--timemap", "t.json"],
                     ["make", "--start", "100", "--end", "110", "--output", "1280x720=x", "--comments-file-auto"],
                     ["make", "--start", "100", "--end", "110", "--output", "1280x720=x", "--channel", "abc"]):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(ENTRY)] + args, capture_output=True, text=True)
                self.assertEqual(result.returncode, 2, result.stderr)


if __name__ == "__main__":
    unittest.main()
