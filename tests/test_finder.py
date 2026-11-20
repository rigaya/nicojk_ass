"""NicoJKReferenceFinderTests.csのケースをPythonへ移したもの(通信とTSの読み込みは差し替える)。"""

import os
import threading
import unittest

from finder_fixture import (EARLIER, TARGET, FinderTestCase, change_programs, jst, make_ts, program,
                            programs, title_response)
from nicojk_ass import finder


class FindTests(FinderTestCase):
    def test_実応答から同じ回の別局録画を見つける(self):
        f = self.f
        reference = f.add_recording("#ゾンビさがしてます_02_テレビ朝日.ts", 1064, EARLIER)
        self.assertEqual(f.find(), reference)
        self.assertEqual(len(f.requests), 3)
        self.assertEqual(len(f.reads), 1)
        self.assertIn("Range=20261011_003000-20261011_013000", f.requests[0])

    def test_最初の放送なら基準を選ばない(self):
        f = self.f
        f.range = f.episodes
        self.assertIsNone(f.find(1064, EARLIER))
        self.assertEqual(f.reads, [])
        self.assertTrue(1 <= len(f.requests) <= 3)

    def test_名前と拡張子で候補を絞る(self):
        for name, found in (("#ゾンビさがしてます ０２.m2ts", True), ("別作品_02.ts", False),
                            ("#ゾンビさがしてます_02.mp4", False)):
            with self.subTest(name=name):
                self.setUp()
                f = self.f
                reference = f.add_recording(name, 1064, EARLIER)
                self.assertEqual(f.find(), reference if found else None)
                self.assertEqual(len(f.reads), 1 if found else 0)

    def test_名前は幅大小文字空白記号をそろえる(self):
        self.assertEqual(finder.normalize_name("ＦＸ 戦士・くるみちゃん＃０２！"), "fx戦士くるみちゃん02")

    def test_TSの局と開始時刻で確定する(self):
        for sid, difference, found in ((1064, 300, True), (1064, 301, False), (1048, 0, False)):
            with self.subTest(sid=sid, difference=difference):
                self.setUp()
                f = self.f
                reference = f.add_recording("ゾンビさがしてます.ts", sid, EARLIER + difference)
                self.assertEqual(f.find(), reference if found else None)

    def test_対象放送が十分近くなければ何も選ばない(self):
        f = self.f
        f.add_recording("ゾンビさがしてます.ts", 1064, EARLIER)
        self.assertIsNone(f.find(151, TARGET + 11 * 60))
        self.assertEqual(f.reads, [])

    def test_基準ではない系列局は候補にしない(self):
        f = self.f
        f.channels.append((67, 3088, "メ～テレ", False))
        f.services[3088] = 5
        f.episodes = programs(program(8028, 67, "2", "2026-10-10 23:30:00"))
        f.add_recording("ゾンビさがしてます.ts", 3088, EARLIER)
        self.assertIsNone(f.find())
        self.assertEqual(f.reads, [])

    def test_削除された放送は基準にしない(self):
        f = self.f
        f.episodes = change_programs(f.episodes, lambda x: setattr(x.find("Deleted"), "text", "1"))
        f.add_recording("ゾンビさがしてます.ts", 1064, EARLIER)
        self.assertIsNone(f.find())
        self.assertEqual(f.reads, [])

    def test_移動先のサブフォルダから基準録画を見つける(self):
        for directory in ("succeeded", "failed"):
            with self.subTest(directory=directory):
                self.setUp()
                f = self.f
                reference = f.add_recording(os.path.join(directory, "ゾンビさがしてます.ts"), 1064, EARLIER)
                self.assertEqual(f.find(), reference)
                self.assertEqual(f.reads, [reference])

    def test_移動先フォルダがなくても同じフォルダの基準録画を見つける(self):
        f = self.f
        self.assertFalse(os.path.exists(os.path.join(f.root, "succeeded")))
        self.assertFalse(os.path.exists(os.path.join(f.root, "failed")))
        reference = f.add_recording("ゾンビさがしてます.ts", 1064, EARLIER)
        self.assertEqual(f.find(), reference)
        self.assertEqual(f.reads, [reference])

    def test_ほかのサブフォルダと入力自身は読まない(self):
        f = self.f
        f.add_recording(os.path.join("other", "ゾンビさがしてます.ts"), 1064, EARLIER)
        self.assertIsNone(f.find())
        self.assertEqual(f.reads, [])

    def test_短い作品名でも照合する(self):
        f = self.f
        f.set_title("#ゾンビさがしてます", "ゾンます")
        reference = f.add_recording("ゾンます_02.ts", 1064, EARLIER)
        self.assertEqual(f.find(), reference)

    def test_非ゼロのStOffsetを二重加算しない(self):
        f = self.f
        # TID8019#3の実例: 23:45の放送が15分遅れ、StTimeは変更後の00:00、StOffsetは変更量900秒。
        f.range = programs(program(8019, 6, "3", "2026-10-22 00:00:00", 900))
        f.episodes = programs(program(8019, 20, "3", "2026-10-21 22:00:00", 900))
        f.title = title_response(8019, "#ゾンビさがしてます")
        reference = f.add_recording("ゾンビさがしてます.ts", 333, jst(2026, 10, 21, 22))
        self.assertEqual(f.find(1064, jst(2026, 10, 22, 0)), reference)

    def test_通信や応答の失敗は警告して戻る(self):
        for malformed in (False, True):
            with self.subTest(malformed=malformed):
                self.setUp()
                f = self.f
                if malformed:
                    f.range = "壊れたXML"
                else:
                    f.fetch_failure = OSError("通信失敗")
                plan = f.plan()
                self.assertIsNone(plan["reference"])
                self.assertTrue(plan["error"])
                self.assertTrue(f.logs)

    def test_TSを読めなくてもジョブを失敗させない(self):
        f = self.f
        f.add_recording("ゾンビさがしてます.ts", 1064, EARLIER)
        f.read_failure = True
        plan = f.plan()
        self.assertIsNone(plan["reference"])
        self.assertEqual(plan["probed"][0]["result"], "read_error")
        self.assertTrue(f.logs)

    def test_TS読み込み数は78までに収める(self):
        f = self.f
        for i in range(85):
            f.add_recording(f"ゾンビさがしてます_{i:02}.ts", 1048, EARLIER)
        self.assertIsNone(f.find())
        self.assertEqual(len(f.reads), 78)
        self.assertEqual(len(set(f.reads)), 78)

    def test_早い候補局の録画がなく百本あっても次の局を少数の読み込みで見つける(self):
        f = self.f
        f.episodes = programs(program(8028, 20, "2", "2026-10-10 23:00:00"),
                              program(8028, 6, "2", "2026-10-10 23:30:00"))
        for i in range(99):
            other = f.add_recording(f"ゾンビさがしてます_{i:02}.ts", 1048, EARLIER - 86400)
            os.utime(other, (EARLIER + i, EARLIER + i))
        reference = f.add_recording("z_ゾンビさがしてます.ts", 1064, EARLIER)
        os.utime(reference, (EARLIER + 1800, EARLIER + 1800))
        self.assertEqual(f.find(), reference)
        self.assertEqual(len(f.reads), 4)
        self.assertEqual(f.reads[-1], reference)
        self.assertEqual(len(set(f.reads)), 4)

    def test_応答とTS情報は次回探索で再利用する(self):
        f = self.f
        reference = f.add_recording("ゾンビさがしてます.ts", 1064, EARLIER)
        self.assertEqual(f.find(), reference)
        self.assertEqual(f.find(), reference)
        self.assertEqual(len(f.requests), 3)
        self.assertEqual(len(f.reads), 1)
        self.assertEqual(len(os.listdir(os.path.join(f.cache_dir, "syoboi"))), 3)

    def test_別のプロセスでもTS情報はキャッシュのフォルダから再利用する(self):
        f = self.f
        reference = f.add_recording("ゾンビさがしてます.ts", 1064, EARLIER)
        self.assertEqual(f.find(), reference)
        f.finder = f.new_finder()  # メモリのキャッシュがない状態
        self.assertEqual(f.find(), reference)
        self.assertEqual(len(f.reads), 1)
        self.assertEqual(len(f.requests), 3)

    def test_ファイルが更新されたらTS情報を読み直す(self):
        f = self.f
        reference = f.add_recording("ゾンビさがしてます.ts", 1064, EARLIER)
        f.find()
        with open(reference, "a", encoding="utf-8") as file:
            file.write("追記")
        f.find()
        self.assertEqual(len(f.reads), 2)

    def test_同時探索でも応答キャッシュを共有して通信を三回に収める(self):
        f = self.f
        reference = f.add_recording("ゾンビさがしてます.ts", 1064, EARLIER)
        finders = [f.new_finder(), f.new_finder()]
        results = []

        def run(instance):
            results.append(instance.find(151, TARGET, f.search_dirs(), [f.source])["reference"]["path"])

        threads = [threading.Thread(target=run, args=(i,)) for i in finders]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, [reference, reference])
        self.assertEqual(len(f.requests), 3)
        self.assertEqual(len(os.listdir(os.path.join(f.cache_dir, "syoboi"))), 3)

    def test_追加通信の開始間隔を十秒空けキャッシュ利用では待たない(self):
        f = self.f
        reference = f.add_recording("ゾンビさがしてます.ts", 1064, EARLIER)
        self.assertEqual(f.find(), reference)
        self.assertEqual(len(f.request_times), 3)
        self.assertEqual(f.request_times[1] - f.request_times[0], 10)
        self.assertEqual(f.request_times[2] - f.request_times[1], 10)
        self.assertEqual(f.delays, [10, 10])
        self.assertEqual(f.find(), reference)
        self.assertEqual(len(f.request_times), 3)
        self.assertEqual(len(f.delays), 2)
        self.assertTrue(any("waiting" in line for line in f.logs))

    def test_通信で経過した時間を待機時間から引く(self):
        f = self.f
        f.fetch_duration = 4
        f.add_recording("ゾンビさがしてます.ts", 1064, EARLIER)
        self.assertIsNotNone(f.find())
        self.assertEqual(f.delays, [6, 6])

    def test_通信に失敗しても同じ探索では再試行しない(self):
        f = self.f
        f.fetch_failure = OSError("通信失敗")
        self.assertIsNone(f.find())
        self.assertEqual(len(f.request_times), 1)
        self.assertEqual(f.delays, [])

    def test_Season表記と第何期表記が違っても作品名で候補にする(self):
        f = self.f
        f.set_title("ゾンビさがしてます Season2")
        reference = f.add_recording("ゾンビさがしてます 第2期_02.ts", 1064, EARLIER)
        self.assertEqual(f.find(), reference)

    def test_長い副題を省いたファイル名でも先頭五文字で候補にする(self):
        f = self.f
        f.set_title("転生したら剣でした ～異世界の旅を仲間と歩む物語～")
        reference = f.add_recording("転生したら_02.ts", 1064, EARLIER)
        self.assertEqual(f.find(), reference)

    def test_同じ作品が五十本あっても終了時刻に近い録画を最初に読む(self):
        f = self.f
        f.set_title("作品名 長い副題")
        end = EARLIER + 1800
        for i in range(49):
            other = f.add_recording(f"{i:02}_作品名_長い副題.ts", 1064, EARLIER - 86400 * (i + 1))
            os.utime(other, (end - 86400 * (i + 1),) * 2)
        reference = f.add_recording("z_作品名.ts", 1064, EARLIER)
        os.utime(reference, (end, end))
        self.assertEqual(f.find(), reference)
        self.assertEqual(f.reads, [reference])

    def test_更新時刻がすべて同じでも上限内なら見つかる(self):
        f = self.f
        first = f.add_recording("a_ゾンビさがしてます.ts", 1064, EARLIER - 86400)
        reference = f.add_recording("m_ゾンビさがしてます.ts", 1064, EARLIER)
        last = f.add_recording("z_ゾンビさがしてます.ts", 1064, EARLIER + 86400)
        for path in (first, reference, last):
            os.utime(path, (EARLIER, EARLIER))
        self.assertEqual(f.find(), reference)
        self.assertEqual(f.reads, [first, reference])

    def test_数字のみの区間と一文字の区間からは候補を作らない(self):
        for title, filename in (("2026 １２３ Ａ", "2026_123_A.ts"), ("作品名 2026 Ａ", "2026_A.ts")):
            with self.subTest(title=title):
                self.setUp()
                f = self.f
                f.set_title(title)
                f.add_recording(filename, 1064, EARLIER)
                self.assertIsNone(f.find())
                self.assertEqual(f.reads, [])

    def test_複数の探すフォルダと除外ファイルと拡張子の大文字(self):
        f = self.f
        reference = f.add_recording("ゾンビさがしてます.TS", 1064, EARLIER)
        excluded = f.add_recording("ゾンビさがしてます_除外.ts", 1064, EARLIER)
        plan = f.finder.find(151, TARGET, [f.root, f.root], [f.source, excluded])
        self.assertEqual(plan["reference"]["path"], reference)
        self.assertEqual(f.reads, [reference])


class AffiliateTests(FinderTestCase):
    """遅れネット・同時ネットの判定(target.comments)。"""

    def setUp(self):
        super().setUp()
        self.f.set_affiliate()

    def affiliate(self, sid=3088):
        return self.f.plan(sid)

    def test_系列局とキー局が五分以内なら今までどおりにする(self):
        for difference in (-300, 0, 300):
            with self.subTest(difference=difference):
                self.setUp()
                f = self.f
                from finder_fixture import jst_text
                f.episodes = programs(program(8028, 6, "2", jst_text(TARGET + difference)))
                plan = self.affiliate()
                self.assertEqual(plan["target"]["comments"], "ok")
                self.assertIsNone(plan["reference"])
                self.assertTrue(any("同時" in line for line in f.logs))

    def test_遅れネットの録画があれば基準にし対象は無関係と判定する(self):
        f = self.f
        reference = f.add_recording("ゾンビさがしてます.ts", 1064, EARLIER)
        plan = self.affiliate()
        self.assertEqual(plan["reference"]["path"], reference)
        self.assertEqual(plan["target"]["comments"], "unrelated")

    def test_キー局と五分を超えて違い録画もなければ実況を付けない(self):
        from finder_fixture import jst_text
        for difference in (301, 5400):
            with self.subTest(difference=difference):
                self.setUp()
                f = self.f
                f.set_affiliate()
                f.episodes = programs(program(8028, 6, "2", jst_text(TARGET - difference)))
                plan = self.affiliate()
                self.assertEqual(plan["target"]["comments"], "unrelated")
                self.assertIsNone(plan["reference"])

    def test_キー局の同じ回の放送がなければ実況を付けない(self):
        f = self.f
        f.episodes = programs(program(8028, 67, "2", "2026-10-11 01:00:00"))
        plan = self.affiliate()
        self.assertEqual(plan["target"]["comments"], "unrelated")
        self.assertEqual(f.reads, [])

    def test_系列局の録画した放送が特定できなければ今までどおりにする(self):
        f = self.f
        f.range = programs()
        plan = self.affiliate()
        self.assertEqual(plan["target"]["comments"], "unknown")
        self.assertEqual(len(f.requests), 1)

    def test_系列局の話数がなければ今までどおりにする(self):
        f = self.f
        f.range = change_programs(f.range, lambda x: setattr(x.find("Count"), "text", ""))
        plan = self.affiliate()
        self.assertEqual(plan["target"]["comments"], "unknown")
        self.assertEqual(len(f.requests), 1)

    def test_同じChIDの複数ServiceIdをどちらも対象として判定する(self):
        for sid in (3088, 4000):
            with self.subTest(sid=sid):
                self.setUp()
                f = self.f
                f.set_affiliate()
                f.channels.append((67, 4000, "メ～テレ支局", False))
                f.services[4000] = 5
                plan = self.affiliate(sid)
                self.assertEqual(plan["target"]["comments"], "unrelated")
                self.assertIn("ChID=67", f.requests[0])

    def test_応答の失敗でも遅れネットの判定は残る(self):
        f = self.f
        f.title = "壊れたXML"
        plan = self.affiliate()
        self.assertEqual(plan["target"]["comments"], "unrelated")
        self.assertTrue(plan["error"])

    def test_同時刻でjk番号が同じ局の録画は使わない(self):
        f = self.f
        f.episodes = programs(program(8028, 6, "2", "2026-10-11 01:00:00"))
        f.add_recording("ゾンビさがしてます_同時刻.ts", 1064, TARGET)
        plan = self.affiliate()
        self.assertIsNone(plan["reference"])
        self.assertEqual(plan["target"]["comments"], "ok")
        self.assertEqual(f.reads, [])

    def test_五分を超えて早い同じjk番号の局の録画は使える(self):
        from finder_fixture import jst_text
        f = self.f
        start = TARGET - 301
        f.episodes = programs(program(8028, 6, "2", jst_text(start)))
        reference = f.add_recording("ゾンビさがしてます_先行.ts", 1064, start)
        plan = self.affiliate()
        self.assertEqual(plan["reference"]["path"], reference)
        self.assertEqual(plan["target"]["comments"], "unrelated")
        self.assertEqual(f.reads, [reference])


class SimultaneousTests(FinderTestCase):
    def test_同時刻でjk番号が違う局の録画を基準にする(self):
        f = self.f
        f.episodes = programs(program(8028, 6, "2", "2026-10-11 01:00:00"))
        reference = f.add_recording("ゾンビさがしてます_同時刻.ts", 1064, TARGET)
        plan = f.plan()
        self.assertEqual(plan["reference"]["path"], reference)
        self.assertTrue(plan["candidates"][0]["same_time"])
        self.assertEqual(plan["target"]["comments"], "ok")
        self.assertEqual(f.reads, [reference])

    def test_応答が同時刻の放送から並んでいても早い放送を優先する(self):
        f = self.f
        f.episodes = programs(program(8028, 20, "2", "2026-10-11 01:00:00"),
                              program(8028, 6, "2", "2026-10-10 23:30:00"))
        simultaneous = f.add_recording("a_ゾンビさがしてます_同時刻.ts", 333, TARGET)
        earlier = f.add_recording("z_ゾンビさがしてます_先行.ts", 1064, EARLIER)
        os.utime(simultaneous, (TARGET + 1800,) * 2)
        os.utime(earlier, (EARLIER + 1800,) * 2)
        self.assertEqual(f.find(), earlier)
        self.assertEqual(f.reads, [earlier])

    def test_候補の前後五分の境界ではjk番号によって選別する(self):
        from finder_fixture import jst_text
        cases = [(False, -301, True), (False, -300, True), (False, -299, True), (False, 299, True),
                 (False, 300, True), (False, 301, False), (True, -301, True), (True, -300, False),
                 (True, -299, False), (True, 299, False), (True, 300, False), (True, 301, False)]
        for same_jk, difference, found in cases:
            with self.subTest(same_jk=same_jk, difference=difference):
                self.setUp()
                f = self.f
                if same_jk:
                    f.set_affiliate()
                start = TARGET + difference
                f.episodes = programs(program(8028, 6, "2", jst_text(start)))
                reference = f.add_recording("ゾンビさがしてます_境界.ts", 1064, start)
                self.assertEqual(f.find(3088 if same_jk else 151, TARGET), reference if found else None)
                self.assertEqual(len(f.reads), 1 if found else 0)


class PlanJsonTests(FinderTestCase):
    def test_planに特定した放送と候補と確かめた録画が入る(self):
        f = self.f
        f.add_recording("ゾンビさがしてます_00_別局.ts", 1048, EARLIER)
        reference = f.add_recording("ゾンビさがしてます_02.ts", 1064, EARLIER)
        plan = f.plan()
        self.assertEqual(plan["schema"], 1)
        self.assertEqual(plan["program"]["tid"], 8028)
        self.assertEqual(plan["program"]["count"], "2")
        self.assertEqual(plan["program"]["title"], "#ゾンビさがしてます")
        self.assertEqual(plan["target"], {"sid": 151, "jk": 151, "name": "BS朝日", "comments": "ok"})
        self.assertEqual(len(plan["candidates"]), 1)
        self.assertEqual(plan["candidates"][0]["sid"], 1064)
        self.assertEqual(plan["candidates"][0]["jk"], 5)
        self.assertEqual(plan["candidates"][0]["recording"], reference)
        self.assertEqual({p["path"]: p["result"] for p in plan["probed"]},
                         {os.path.join(f.root, "ゾンビさがしてます_00_別局.ts"): "sid_mismatch", reference: "match"})
        self.assertEqual(plan["reference"], {"path": reference, "sid": 1064, "candidate": 0})

    def test_開始時刻が合わない録画の理由を残す(self):
        f = self.f
        f.add_recording("ゾンビさがしてます.ts", 1064, EARLIER + 301)
        plan = f.plan()
        self.assertEqual(plan["probed"][0]["result"], "time_mismatch")

    def test_複数サービスのTSは候補の局の開始時刻を使う(self):
        f = self.f
        path = f.add_recording("ゾンビさがしてます.ts", 1064, EARLIER)
        info = make_ts(1064, EARLIER)
        info.programs.append(info.programs[0].__class__(1072, True, 257, EARLIER + 3600, 1800))
        info.eit_start = EARLIER + 3600  # 最後に読んだEITは別のサービスのもの
        f.recordings[path] = info
        self.assertEqual(f.find(), path)


if __name__ == "__main__":
    unittest.main()
