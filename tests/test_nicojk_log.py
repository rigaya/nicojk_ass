import json
import random
import sys
import xml.etree.ElementTree as ET
import zipfile
from codecs import BOM_UTF8
from unittest import mock

from test_nicojk_ass import MakeRunner, comments
from nicojk_ass import nicojk_log

START = 1700000010
END = START + 10


def chat(date, text, extra=""):
    return f'<chat date="{date}" {extra}>{text}</chat>\r\n'.encode("utf-8")


class NicoJKLogTests(MakeRunner):
    def setUp(self):
        super().setUp()
        self.report = self.directory / "report.json"
        self.config = self.directory / "nicojk_ass.toml"
        self.args = ["make", "--channel", "jk9", "--start", str(START), "--end", str(END),
                     "--report", str(self.report)] + self.output()

    def config_with_dirs(self, *dirs):
        self.config.write_text(f"[nicojk_log]\ndirs = {list(dirs)!r}\n", encoding="utf-8-sig")

    def log_file(self, root, start, data, channel="jk9"):
        directory = self.directory / root / channel
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{start}.txt"
        path.write_bytes(data)
        return path

    def make(self, *extra, prefer=True):
        args = self.args + list(extra)
        if prefer:
            args.append("--prefer-nicojk-log")
        with mock.patch.object(sys, "argv", [str(self.directory / "nicojk_ass.py")]):
            code, converted, api, stderr = self.run_make(args, lambda *args: "<packet/>")
        report = json.loads(self.report.read_text(encoding="utf-8"))
        return code, converted[0][3] if converted else [], api, stderr, report

    def test_BOM付き設定と2つ目のフォルダのTXTを使う(self):
        self.config_with_dirs("first", "second")
        self.log_file("first", START, chat(START, "基準局"), channel="jk1")
        old = self.log_file("second", START - 15, chat(START, "古いファイル"))
        first = self.log_file("second", START - 5, chat(START - 5, "範囲外")
                              + chat(START, "開始", 'date_usec="0"')
                              + chat(START + 4, "境界直前", 'date_usec="0"'))
        second = self.log_file("second", START + 5, chat(START + 5, "境界", 'date_usec="0"')
                               + chat(END, "末尾", 'no="42" date_usec="0"') + chat(END + 1, "範囲外"))
        code, chats, api, stderr, report = self.make()
        self.assertEqual(code, 0, stderr)
        api.assert_not_called()
        self.assertEqual([(c.text, c.get("no"), c.get("vpos")) for c in chats],
                         [("開始", "2", "0"), ("境界直前", "3", "400"),
                          ("境界", "4", "500"), ("末尾", "42", "1000")])
        self.assertEqual(report["nicojk_log"], [dict(channel="jk9", start=START, end=END,
                                                     used=True, files=[str(first), str(second)])])
        self.assertNotIn(str(old), stderr)

    def test_BOMを外して非表示行を除きファイルをまたぐ重複を除く(self):
        self.config_with_dirs("logs")
        boundary = chat(START + 5, "境界", 'no=""')
        self.log_file("logs", START, BOM_UTF8 + chat(START, "先頭", 'no=""')
                      + chat(START - 1, "範囲外の注釈", 'abone="1"')
                      + BOM_UTF8 + chat(START + 2, "番組名", 'abone="1" no=""')
                      + boundary + boundary)
        self.log_file("logs", START + 5, BOM_UTF8 + b"  " + boundary.rstrip() + b" \n"
                      + chat(START + 5, "同じ秒の続き", 'abone="0"'))
        code, chats, api, stderr, _ = self.make()
        self.assertEqual(code, 0, stderr)
        api.assert_not_called()
        # 同じファイルにある同一行は残し、次のファイルとの重複だけを除く。
        self.assertEqual([(entry.text, entry.get("no")) for entry in chats],
                         [("先頭", "1"), ("境界", "2"), ("境界", "3"), ("同じ秒の続き", "4")])

    def test_足りないマイクロ秒だけを秒ごとの乱数で再現可能に振る(self):
        self.log_file("logs", START, chat(START, "一番目")
                      + chat(START, "元の値", 'date_usec="250001" no="70"')
                      + chat(START, "二番目") + chat(START, "三番目")
                      + chat(START + 1, "次の秒"))
        log = nicojk_log.NicoJKLog([self.directory / "logs"], [])
        first = log.read("jk9", START, END)
        self.assertEqual(log.read("jk9", START, END), first)
        packet = list(ET.fromstring(first))
        offsets = [int(packet[i].get("date_usec")) for i in (0, 2, 3)]
        rng = random.Random(START)
        self.assertEqual(offsets, sorted(rng.randrange(1000000) for _ in range(3)))
        self.assertLessEqual(0, offsets[0])
        self.assertLess(offsets[-1], 1000000)
        self.assertEqual(packet[1].get("date_usec"), "250001")
        self.assertEqual(packet[1].get("no"), "70")
        self.assertEqual(int(packet[4].get("date_usec")), random.Random(START + 1).randrange(1000000))

    def test_壊れた行だけを捨てる(self):
        self.config_with_dirs("logs")
        outside = f"""<chat date="{START - 1}" mail="don't">""".encode("ascii")
        self.log_file("logs", START, b"<!-- NicoJK logfile -->\r\n\r\n<x_past_chat_begin />\r\n"
                      + outside + b'\xff</chat>\r\n'
                      + chat(START, "正常", 'date_usec="250000"')
                      + chat(START, "マイクロ秒が範囲外", 'date_usec="1000000"')
                      + chat("bad", "時刻が不正")
                      + b'<chat date="1700000010">\xff</chat>\r\n'
                      + b'<chat date="1700000011">unfinished')
        code, chats, api, stderr, _ = self.make()
        self.assertEqual(code, 0, stderr)
        self.assertEqual([(c.text, c.get("vpos")) for c in chats], [("正常", "25")])
        self.assertIn("dropped 4 invalid rows", stderr)
        api.assert_not_called()

    def test_不正なTOMLはエラーにする(self):
        for contents, error in (
            ('[nicojk_log]\ndirs = ["C:\\DTV"]', "シングルクォート"),
            ("[nicojk_log]\ndirs = [42]", "文字列のリスト"),
            ('[nicojk_log]\ndirs = "logs"', "文字列のリスト"),
            ("[nicojk_log", "cannot read nicojk_ass.toml"),
        ):
            with self.subTest(contents=contents):
                self.config.write_text(contents, encoding="utf-8")
                code, _, api, stderr, report = self.make()
                self.assertEqual(code, comments.EXIT_ERROR)
                self.assertIn(error, stderr)
                self.assertEqual(report["nicojk_log"], [])
                api.assert_not_called()

    def test_設定がないかコメントがなければAPIへ戻る(self):
        for config, warning in (
            (None, "configuration not found"),
            ("[nicojk_log]\ndirs = []", "dirs is empty"),
            ("[nicojk_log]\ndirs = ['missing']", "directory not found"),
            ("[nicojk_log]\ndirs = ['logs']", "no comments"),
        ):
            with self.subTest(config=config):
                if config is not None:
                    self.config.write_text(config, encoding="utf-8")
                    self.log_file("logs", START - 1, chat(START - 1, "範囲外"))
                code, _, api, stderr, report = self.make()
                self.assertEqual(code, 0, stderr)
                self.assertIn(warning, stderr)
                api.assert_called_once_with("jk9", START, END, comments.DEFAULT_API_BASE)
                self.assertEqual(report["nicojk_log"], [dict(channel="jk9", start=START, end=END,
                                                             used=False, files=[])])

    def test_スイッチなしでは設定を読まない(self):
        self.config.write_text("invalid TOML", encoding="utf-8")
        with mock.patch.object(nicojk_log, "load_dirs") as load_dirs:
            code, _, api, stderr, report = self.make(prefer=False)
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("nicojk_log", report)
        load_dirs.assert_not_called()
        api.assert_called_once()

    def test_明示したコメントファイルを優先する(self):
        self.config_with_dirs("logs")
        self.log_file("logs", START, chat(START, "ログ"))
        source = self.write(f"<packet>{chat(START, '指定ファイル').decode()}</packet>")
        code, chats, api, stderr, report = self.make("--comments-file", str(source))
        self.assertEqual(code, 0, stderr)
        self.assertEqual([c.text for c in chats], ["指定ファイル"])
        self.assertEqual(report["nicojk_log"], [])
        api.assert_not_called()


    def test_ZIPとTXTを時刻順につなぎ同名ならTXTを使う(self):
        self.config_with_dirs("logs")
        first = START - 5
        second = START + 5
        txt = self.log_file("logs", second, chat(second, "TXT"))
        archive = txt.with_name(f"{first}.zip")
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr(f"{START - 8}.txt", chat(START, "古いログ"))
            zipped.writestr(f"{first}.txt", chat(START, "ZIP"))
            zipped.writestr(txt.name, chat(second, "ZIP内の同名ファイル"))
            zipped.writestr(f"{END + 1}.txt", chat(END, "範囲外に始まるファイル"))
        code, chats, api, stderr, report = self.make()
        self.assertEqual(code, 0, stderr)
        api.assert_not_called()
        self.assertEqual([c.text for c in chats], ["ZIP", "TXT"])
        self.assertEqual(report["nicojk_log"][0]["files"], [f"{archive}:{first}.txt", str(txt)])

    def test_ZIPだけのフォルダから複数のログを読む(self):
        self.config_with_dirs("logs")
        directory = self.directory / "logs" / "jk9"
        directory.mkdir(parents=True)
        with zipfile.ZipFile(directory / f"{START}.zip", "w") as archive:
            archive.writestr(f"{START}.txt", chat(START, "開始"))
            archive.writestr(f"{END}.txt", chat(END, "末尾"))
        code, chats, api, stderr, report = self.make()
        self.assertEqual(code, 0, stderr)
        api.assert_not_called()
        self.assertEqual([c.text for c in chats], ["開始", "末尾"])
        self.assertEqual(len(report["nicojk_log"][0]["files"]), 2)


    def test_ZIP内の同名ログは開始時刻が新しいZIPを使う(self):
        self.config_with_dirs("logs")
        directory = self.directory / "logs" / "jk9"
        directory.mkdir(parents=True)
        name = f"{START}.txt"
        old, new = (directory / f"{stamp}.zip" for stamp in (START - 10, START - 5))
        for path, text in ((old, "古いZIP"), (new, "新しいZIP")):
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(name, chat(START, text))
        code, chats, api, stderr, report = self.make()
        self.assertEqual(code, 0, stderr)
        api.assert_not_called()
        self.assertEqual([c.text for c in chats], ["新しいZIP"])
        self.assertEqual(report["nicojk_log"][0]["files"], [f"{new}:{name}"])

    def test_統合では対象と基準で別々にログを探す(self):
        self.config_with_dirs("logs")
        self.log_file("logs", START, chat(START + 1, "対象"))
        self.log_file("logs", START, chat(START + 2, "基準のログ"), channel="jk1")
        ref_file = self.write(f"<packet>{chat(START + 2, '基準の指定ファイル').decode()}</packet>")
        timemap = self.write("[[0, 10, 0, 10]]", "timemap.json")
        args = ["--mode", "merge", "--timemap", str(timemap),
                "--ref-start", str(START), "--ref-end", str(END)]
        for ref, text, channels in (
            (["--ref-channel", "jk1"], "基準のログ", ["jk1", "jk9"]),
            (["--ref-comments-file", str(ref_file)], "基準の指定ファイル", ["jk9"]),
        ):
            with self.subTest(ref=ref):
                code, chats, api, stderr, report = self.make(*args, *ref)
                self.assertEqual(code, 0, stderr)
                api.assert_not_called()
                self.assertEqual([chat.text for chat in chats], ["対象", text])
                self.assertEqual([entry["channel"] for entry in report["nicojk_log"]], channels)
                self.assertTrue(all(entry["used"] for entry in report["nicojk_log"]))
