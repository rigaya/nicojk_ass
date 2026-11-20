"""align_comment.pyの単体テスト。合成したコメントで、正規化・投票・区間のまとめ方・対応表を確かめる。

    python3 -m unittest discover -s tests
"""
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nicojk_ass import align_comment  # noqa: E402

TARGET_START = 1_000_000.0
OFFSET = 5400.0              # 基準の放送が対象より何秒後か(録画の先頭どうし)
CM_START, CM_TARGET, CM_REFERENCE = 600.0, 90.0, 60.0   # 対象のCMは基準より30秒長い
DURATION = 1500.0
PHRASE_LENGTH = 8
KANA = "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん"
KANJI = "猫犬鳥魚空海山川森花雨雪風火水木金土日月星光影夢愛剣城王姫竜鬼神"


def katakana(text):
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in text)


def synthesize(seed=1, scene_seconds=4.0, noise_per_minute=20):
    """場面ごとに固有の言葉を両局に書かせ、局ごとに表記ゆれと時刻のぶれを入れる。CMの長さが局で違う。"""
    rng = random.Random(seed)
    target, reference = [], []

    def reference_time(position):
        # 対象の録画の時刻→基準の絶対時刻。CMより後は、CMの長さの差だけずれが小さくなる。
        delta = OFFSET if position < CM_START + CM_TARGET else OFFSET - (CM_TARGET - CM_REFERENCE)
        return TARGET_START + position + delta

    position = 0.0
    while position < DURATION:
        if not CM_START <= position < CM_START + CM_TARGET:
            phrase = "".join(rng.choice(KANA + KANJI) for _ in range(PHRASE_LENGTH))
            for _ in range(2):
                target.append((TARGET_START + position + rng.uniform(0, 4), phrase + "ｗｗｗ"))
                reference.append((reference_time(position) + rng.uniform(0, 4), katakana(phrase) + "！！"))
            # 片方の局にしかないコメント
            target.append((TARGET_START + position + rng.uniform(0, 4), "".join(rng.choice(KANJI) for _ in range(3))))
        position += scene_seconds
    # どの時刻にも出る反応(重みが軽くなるはず)
    for stamp_list, origin in ((target, TARGET_START), (reference, TARGET_START + OFFSET - 300)):
        for _ in range(int(noise_per_minute * (DURATION + 600) / 60)):
            stamp_list.append((origin + rng.uniform(0, DURATION + 600), rng.choice(["w", "草", "きたー", "8888"])))
    return sorted(target), sorted(reference)


class NormalizeTest(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(align_comment.normalize("ワロタｗｗｗｗ"), "わろたw")
        self.assertEqual(align_comment.normalize("８８８８８"), "8")
        self.assertEqual(align_comment.normalize("キタ━━━━(ﾟ∀ﾟ)━━━━!!"), "きた")
        self.assertEqual(align_comment.normalize("ｷﾀｰｰｰ！"), "きたー")
        self.assertEqual(align_comment.normalize("ABC_def　ＧＨＩ"), "abcdefghi")
        self.assertEqual(align_comment.normalize("！？…"), "")

    def test_grams(self):
        self.assertEqual(align_comment.grams("ねこ", (1, 2)), ["こ", "ね", "ねこ"])
        self.assertEqual(align_comment.grams("ねねね", (1,)), ["ね"])


class EstimateTest(unittest.TestCase):
    def setUp(self):
        self.params = dict(align_comment.DEFAULT_PARAMS)
        self.target, self.reference = synthesize()
        self.vectors, self.index = align_comment.prepare(self.target, self.reference, self.params)

    def test_single_segment(self):
        part = [(s, w) for s, w in self.vectors if s - TARGET_START < CM_START]
        result = align_comment.estimate(part, self.index, OFFSET + 40, self.params)
        self.assertAlmostEqual(result["delta"], OFFSET, delta=1.0)
        self.assertGreater(result["ratio"], self.params["min_ratio"])

    def test_noise_is_light(self):
        # IDFがあると、どこにでも出る反応の重みは場面の言葉より軽い。
        idf = align_comment.build_idf([self.target, self.reference], self.params)
        self.assertLess(idf["w"], min(v for k, v in idf.items() if len(k) == 2 and k[0] in KANJI and k[1] in KANJI))

    def test_norm_keeps_idf(self):
        # l2では「w」だけのコメントの重みが1に戻る。countならIDFの小さいまま残る。
        idf = align_comment.build_idf([self.target, self.reference], self.params)
        l2 = align_comment.vectorize([(0.0, "ｗｗｗ")], idf, dict(self.params, norm="l2"))[0][1]
        count = align_comment.vectorize([(0.0, "ｗｗｗ")], idf, dict(self.params, norm="count"))[0][1]
        self.assertAlmostEqual(l2["w"], 1.0)
        self.assertAlmostEqual(count["w"], idf["w"])

    def test_find_segments(self):
        params = dict(self.params, norm="count", boundary="midpoint")
        vectors, index = align_comment.prepare(self.target, self.reference, params)
        segments, _ = align_comment.find_segments(vectors, index, OFFSET, TARGET_START, DURATION, params)
        self.assertEqual(len(segments), 2)
        self.assertAlmostEqual(segments[0]["delta"], OFFSET, delta=1.0)
        self.assertAlmostEqual(segments[1]["delta"], OFFSET - (CM_TARGET - CM_REFERENCE), delta=1.0)
        # 境目は窓の刻みの精度で、対象のCMの付近にある。
        boundary = segments[0]["end"]
        self.assertEqual(boundary, segments[1]["start"])
        self.assertGreater(boundary, CM_START - params["step_seconds"])
        self.assertLess(boundary, CM_START + CM_TARGET + params["step_seconds"])

    def test_refine_boundary(self):
        # 境目を詰めると、対象のCMの中か、CMの直前の場面のコメントのすぐ後に来る(場面のコメントは4秒以内にぶれる)。
        params = dict(self.params, norm="count", boundary="refine")
        vectors, index = align_comment.prepare(self.target, self.reference, params)
        segments, _ = align_comment.find_segments(vectors, index, OFFSET, TARGET_START, DURATION, params)
        self.assertEqual(len(segments), 2)
        self.assertGreaterEqual(segments[0]["end"], CM_START - 4)
        self.assertLessEqual(segments[0]["end"], CM_START + CM_TARGET)

    def test_gap_boundary(self):
        # 前の区間は対象のCMの始まり付近で終わり、後ろの区間はCMの終わり付近から始まる。間は写さない。
        params = dict(self.params, norm="count", boundary="gap", fill="strict")
        vectors, index = align_comment.prepare(self.target, self.reference, params)
        segments, _ = align_comment.find_segments(vectors, index, OFFSET, TARGET_START, DURATION, params)
        self.assertEqual(len(segments), 2)
        margin = params["gap_margin_seconds"] + 4
        self.assertAlmostEqual(segments[0]["end"], CM_START, delta=margin)
        self.assertAlmostEqual(segments[1]["start"], CM_START + CM_TARGET, delta=margin)

    def test_edge_support(self):
        # 対象の録画の末尾200秒に基準と一致する場面がないとき、窓の端までではなく、最後に一致したコメントの
        # edge_margin_seconds後で終える。
        last_scene = DURATION - 200
        limit = TARGET_START + last_scene + OFFSET - (CM_TARGET - CM_REFERENCE)
        reference = [(s, text) for s, text in self.reference if s < limit or len(align_comment.normalize(text)) <= 3]
        for edge, check in (("window", self.assertGreater), ("support", self.assertLess)):
            params = dict(self.params, edge=edge)
            vectors, index = align_comment.prepare(self.target, reference, params)
            segments, _ = align_comment.find_segments(vectors, index, OFFSET, TARGET_START, DURATION, params)
            check(segments[-1]["end"], last_scene + 4 + params["edge_margin_seconds"])
        self.assertGreater(segments[-1]["end"], last_scene - 10)
        self.assertGreater(segments[-1]["support"], 0)

    def test_min_support(self):
        # 一致したコメントの下限を区間の一致件数より上げると、区間を捨てる。
        params = dict(self.params, min_support=10 ** 6)
        vectors, index = align_comment.prepare(self.target, self.reference, params)
        segments, _ = align_comment.find_segments(vectors, index, OFFSET, TARGET_START, DURATION, params)
        self.assertEqual(segments, [])

    def test_gap_ties(self):
        # 前のずれ(0秒)と後ろのずれ(50秒)の一致が交互に来ると、前から足した値の最大が2か所にできる。
        # firstは最初の最大で切り、spanは最初から最後の最大までを「どちらとも決まらない範囲」にする。
        rng = random.Random(3)
        phrases = ["".join(rng.choice(KANJI) for _ in range(6)) for _ in range(4)]
        target = [(TARGET_START + 10.0 * (i + 1), phrase) for i, phrase in enumerate(phrases)]
        reference = [(TARGET_START + 10.0 * (i + 1) + (50.0 if i % 2 else 0.0), phrase) for i, phrase in enumerate(phrases)]
        # IDFが十分大きくなるよう、どこにでも出る反応を足す(録画の範囲の外に置く)。
        filler = [(TARGET_START + 1000.0 + i, "w") for i in range(300)]
        params = dict(self.params, smooth_seconds=5)
        vectors, index = align_comment.prepare(sorted(target + filler), sorted(reference + filler), params)
        # find_segmentsと同じく、コメントは絶対時刻、timesと範囲は録画の先頭からの秒で渡す。
        times = [stamp - TARGET_START for stamp, _ in vectors]
        margin = params["gap_margin_seconds"]
        first = align_comment.gap_boundary(vectors, times, index, 0.0, 50.0, 0.0, 50.0, dict(params, gap_ties="first"))
        span = align_comment.gap_boundary(vectors, times, index, 0.0, 50.0, 0.0, 50.0, dict(params, gap_ties="span"))
        self.assertEqual(first, (10.0 + margin, 20.0 - margin))
        self.assertEqual(span, (10.0 + margin, 40.0 - margin))

    def test_extend_ratio(self):
        # 窓の半分ほどが山の比の下限を割るようにしても、延ばせば区間の範囲は広がり、ずれと山の比は芯で推定するので変わらない。
        params = dict(self.params, edge="window", min_segment_ratio=0)
        vectors, index = align_comment.prepare(self.target, self.reference, params)
        _, windows = align_comment.find_segments(vectors, index, OFFSET, TARGET_START, DURATION, params)
        ratios = sorted(row["ratio"] for row in windows if row["delta"] is not None)
        params["min_ratio"] = ratios[len(ratios) // 2]
        base, _ = align_comment.find_segments(vectors, index, OFFSET, TARGET_START, DURATION, dict(params, extend_ratio=0))
        extended, _ = align_comment.find_segments(vectors, index, OFFSET, TARGET_START, DURATION, dict(params, extend_ratio=1.0))
        self.assertEqual([(s["delta"], s["ratio"]) for s in base], [(s["delta"], s["ratio"]) for s in extended])
        covered = lambda segments: sum(s["end"] - s["start"] for s in segments)
        self.assertGreater(covered(extended), covered(base))

    def test_find_segments_cm(self):
        # 本編の区間ごとにずれを推定し、ずれの同じ隣との間へcm_edge_seconds、ずれの違う隣との間へcm_edge_change_seconds、
        # 最後の区間の後ろへcm_tail_secondsだけ延ばす。
        params = dict(self.params, cm_edge_seconds=15, cm_edge_change_seconds=0, cm_tail_seconds=10)
        spans = [(0.0, 280.0), (320.0, CM_START), (CM_START + CM_TARGET, DURATION - 100)]
        segments, windows = align_comment.find_segments_cm(self.vectors, self.index, OFFSET, TARGET_START, DURATION, spans, params)
        self.assertEqual(windows, [])
        self.assertEqual(len(segments), 3)
        self.assertAlmostEqual(segments[0]["delta"], OFFSET, delta=1.0)
        self.assertAlmostEqual(segments[1]["delta"], OFFSET, delta=1.0)
        self.assertAlmostEqual(segments[2]["delta"], OFFSET - (CM_TARGET - CM_REFERENCE), delta=1.0)
        self.assertEqual([(s["start"], s["end"]) for s in segments],
                         [(0.0, 295.0), (305.0, CM_START), (CM_START + CM_TARGET, DURATION - 90)])

    def test_find_segments_cm_split_gap(self):
        # 本編の区間の間が延ばす幅の2倍より短ければ、間の中点で分ける。
        params = dict(self.params, cm_edge_seconds=60, cm_edge_change_seconds=60)
        spans = [(0.0, CM_START), (CM_START + CM_TARGET, DURATION)]
        segments, _ = align_comment.find_segments_cm(self.vectors, self.index, OFFSET, TARGET_START, DURATION, spans, params)
        self.assertEqual(segments[0]["end"], CM_START + CM_TARGET / 2)
        self.assertEqual(segments[1]["start"], CM_START + CM_TARGET / 2)

    def test_find_segments_cm_fill(self):
        # 単独では信頼できない本編の区間(山の比の下限を上げて落とす)も、信頼できた区間のずれで本文が一致すれば写す。
        # ずれの違う区間は、信頼できた区間のずれでは一致しないので写さない。
        spans = [(0.0, 300.0), (300.0, CM_START), (CM_START + CM_TARGET, DURATION)]
        base, _ = align_comment.find_segments_cm(self.vectors, self.index, OFFSET, TARGET_START, DURATION, spans, self.params)
        self.assertEqual(len(base), 3)
        ratio = min(base[0]["ratio"], base[1]["ratio"])
        params = dict(self.params, min_ratio=ratio + 0.01, cm_span_ratio=ratio + 0.01)
        segments, _ = align_comment.find_segments_cm(self.vectors, self.index, OFFSET, TARGET_START, DURATION, spans, params)
        same = [s for s in segments if s["start"] < CM_START]
        self.assertEqual(len(same), 2)
        self.assertEqual([s.get("filled", False) for s in same].count(True), 1)
        for segment in same:
            self.assertAlmostEqual(segment["delta"], OFFSET, delta=1.0)
        for segment in segments:
            if segment["start"] >= CM_START:
                self.assertAlmostEqual(segment["delta"], OFFSET - (CM_TARGET - CM_REFERENCE), delta=1.0)

    def test_enough_comments(self):
        params = dict(self.params, min_comments=30)
        self.assertTrue(align_comment.enough_comments([(0.0, "a")] * 30, [(0.0, "a")] * 30, params))
        self.assertFalse(align_comment.enough_comments([(0.0, "a")] * 29, [(0.0, "a")] * 1000, params))

    def test_no_match(self):
        # 本文に共通点がなければ推定しない(信頼度が下限を通らないか、山がない)。
        rng = random.Random(2)
        other = [(TARGET_START + OFFSET + rng.uniform(0, DURATION), "".join(rng.choice(KANA) for _ in range(5)))
                 for _ in range(300)]
        vectors, index = align_comment.prepare(self.target, other, self.params)
        segments, _ = align_comment.find_segments(vectors, index, OFFSET, TARGET_START, DURATION, self.params)
        self.assertEqual(segments, [])


class TimemapTest(unittest.TestCase):
    SEGMENTS = [{"start": 10.0, "end": 100.0, "delta": 5.0}, {"start": 160.0, "end": 300.0, "delta": -15.0}]

    def test_interpolate(self):
        timemap = align_comment.make_timemap(self.SEGMENTS, 320.0, 400.0)
        self.assertEqual(timemap[0], (5.0, 15.0, 0.0, 10.0))
        self.assertEqual(timemap[1], (15.0, 105.0, 10.0, 100.0))
        # 区間の間は線形に伸び縮みする(基準40秒→対象60秒)。
        self.assertEqual(timemap[2], (105.0, 145.0, 100.0, 160.0))
        self.assertEqual(timemap[3], (145.0, 285.0, 160.0, 300.0))
        self.assertEqual(timemap[4], (285.0, 305.0, 300.0, 320.0))
        self.assertAlmostEqual(align_comment.map_time(timemap, 125.0), 130.0)

    def test_inner(self):
        timemap = align_comment.make_timemap(self.SEGMENTS, 320.0, 400.0, fill="inner")
        self.assertEqual(timemap, [(15.0, 105.0, 10.0, 100.0), (105.0, 145.0, 100.0, 160.0), (145.0, 285.0, 160.0, 300.0)])

    def test_strict(self):
        timemap = align_comment.make_timemap(self.SEGMENTS, 320.0, 400.0, fill="strict")
        self.assertEqual(timemap, [(15.0, 105.0, 10.0, 100.0), (145.0, 285.0, 160.0, 300.0)])
        self.assertIsNone(align_comment.map_time(timemap, 125.0))
        # 半開区間: 終わりちょうどは写さない。
        self.assertIsNone(align_comment.map_time(timemap, 105.0))
        self.assertEqual(align_comment.map_time(timemap, 15.0), 10.0)

    def test_overlap_is_trimmed(self):
        # ずれが区間の間の長さより大きく減ると、基準の時刻が重なる。後ろの要素を削って二重に写さない。
        segments = [{"start": 0.0, "end": 100.0, "delta": 0.0}, {"start": 110.0, "end": 200.0, "delta": -30.0}]
        timemap = align_comment.make_timemap(segments, 200.0, 300.0)
        for previous, current in zip(timemap, timemap[1:]):
            self.assertLessEqual(previous[1], current[0])
            self.assertLessEqual(previous[3], current[2])

    def test_clip_to_reference(self):
        timemap = align_comment.make_timemap([{"start": 0.0, "end": 100.0, "delta": -20.0}], 100.0, 1000.0)
        self.assertEqual(timemap, [(0.0, 80.0, 20.0, 100.0)])


if __name__ == "__main__":
    unittest.main()
