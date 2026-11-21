"""コメント本文の一致で、2局の実況コメントの時刻ずれを推定する(方式D)。

tools/nicojk_comment_align_eval/estimator.pyから移したもの。推定の中身は評価で決めた数字を
再現するため変えない。標準ライブラリだけで動く。

時刻はすべて絶対時刻(UNIX秒)。ずれ(delta)は「基準のコメントの時刻 − 対象のコメントの時刻」。
"""
import bisect
import collections
import math
import re
import unicodedata
import xml.etree.ElementTree as ET

# 初期値に、評価で決めた norm / fill / boundary と、gap_ties / edge / min_* / extend_ratio を反映したもの。
# 初版の条件は --set norm=l2 --set fill=interpolate --set boundary=midpoint --set same_delta_seconds=-1
# --set gap_ties=first --set edge=window --set min_segment_ratio=0 --set min_support=0 --set extend_ratio=0。
DEFAULT_PARAMS = {
    "search_seconds": 300,      # 探索の中心の前後に探す範囲
    "bin_seconds": 0.5,         # 投票の箱の幅
    "smooth_seconds": 10,       # 平滑化の三角窓の半幅
    "refine_smooth_seconds": 0, # 正なら、山の位置だけをこの半幅の平滑化で取り直す(0なら取り直さない)
    "gram_sizes": (1, 2),       # 特徴にする文字n-gramの長さ
    "use_idf": True,            # 特徴の重みにIDFを使うか
    # コメントごとの重みのそろえ方。l2=ベクトルの長さを1に(初版)。count=特徴の数の平方根で割る。
    # l2では「w」だけのコメントも長さ1に戻り、IDFで軽くした効果が消える。countはIDFの大小を残す。
    "norm": "count",
    "exclude_seconds": 20,      # 2番目の山を探すときに最大の山の前後から除く範囲
    "min_ratio": 2.5,           # 信頼度(最大の山÷2番目の山)の下限
    "window_seconds": 180,      # 対象側の窓の長さ
    "step_seconds": 30,         # 対象側の窓の刻み
    "min_window_comments": 5,   # 窓の中にこれより少ないコメントしかなければ推定しない
    "merge_seconds": 3,         # 同じ区間とみなす推定値の差
    "same_delta_seconds": 4,    # 区間を推定し直したあと、隣どうしを1つにまとめるずれの差
    "min_windows": 2,           # 区間にするのに必要な、続けて信頼できた窓の数
    # 区間の前後に続く窓を区間に足すときの山の比の下限(0なら足さない。第0回は0)。山の比は常に1以上なので、1.0は
    # 「推定値が区間の端の窓とmerge_seconds以内なら足す」の意味。dev/validationとも1.5や2.0より良かった。
    "extend_ratio": 1.0,
    # 対応表の区間の間と両端: interpolate=方式Aと同じ、inner=区間の間だけ補い両端は延ばさない、strict=区間の中だけ
    "fill": "strict",
    # 区間の境目: midpoint=窓の中心の中間(初版)、refine=コメント1件ずつの一致で1点に詰める、
    # gap=前の区間を最後に一致したコメントで終え、後ろの区間を最初に一致したコメントから始める(間は写さない)
    "boundary": "gap",
    "support_cosine": 0.5,      # gapで「本文が一致した」とみなすコサイン類似度の下限(2文字以上の特徴の共有も要る)
    "support_idf": math.log(100),  # gapで共有を求める特徴のIDFの下限(コメント100件に1件以下にしか出ない特徴)
    "gap_margin_seconds": 3,    # gapで、一致したコメントから区間を延ばす幅
    # 境目と外側の端を決めるとき、前後この秒数以内に同じずれの一致がもう1件ない一致は数えない(0なら全部数える)。
    # 第1回で試し、10秒超の誤は減るが正を多く失うので使わない。
    "support_cluster_seconds": 0,
    "support_cluster_edge": True,   # support_cluster_secondsを外側の端にも使うか(Falseなら区間の間の境目だけ)
    # gapで、前から足した値の最大が離れて複数あるとき: first=最初の最大の続く範囲だけ(第0回)、span=最初から最後の最大まで
    "gap_ties": "span",
    # 最初の区間の始まりと最後の区間の終わり: window=窓の端まで(第0回)、
    # support=本文が一致した最初・最後のコメントからedge_margin_secondsだけ延ばしたところまで
    "edge": "support",
    "edge_margin_seconds": 20,  # edge=supportで、外側の端を一致したコメントから延ばす幅
    "min_segment_ratio": 2.5,   # 推定し直したあとの山の比がこれより小さい区間は捨てる(0なら捨てない。第0回は0)
    # 信頼できた窓がこれ以上続いた区間は、min_segment_ratioで捨てない。長い区間をまとめて推定し直すと、
    # 反応だけのコメントが増えて山の比が下がることがあるが、窓ごとの推定がそろっていれば正しい(第1回)。
    "ratio_exempt_windows": 10,
    "min_support": 3,           # 区間の中で本文が一致したコメントがこれより少ない区間は捨てる(0なら捨てない。第0回は0)
    # 対象(録画の範囲)と基準(取得した範囲)のコメントの少ないほうがこれより少なければ、推定しない(第1回で決めた)。
    # 20〜29件の層では写したコメントの35%が誤り、30件以上の層では誤りが2%前後以下だった。
    "min_comments": 30,
    # 以下は、対象のCM解析の結果(本編の区間)を使うとき(find_segments_cm)だけの条件。
    # 本文の一致がcm_span_support件以上ある本編の区間は、山の比がcm_span_ratio以上なら信頼する
    # (長い区間をまとめて推定すると2番目の山が育ち、一致が多くても山の比がmin_ratioを少し割ることがある)。
    "cm_span_ratio": 2.0,
    "cm_span_support": 15,
    # 一致が少ない区間を、信頼できた区間のずれで写すときに許す推定値の差。
    "cm_near_seconds": 5,
    # 信頼できなかった本編の区間を、信頼できた区間のずれ(または全体のずれ)で写すのに求める一致件数と、
    # そのとき一致を数える平滑化の半幅(5秒違いの候補を区別するため狭くする)。2件以下では10秒超の誤が急に増えた。
    "cm_fill_support": 3,
    "cm_fill_smooth_seconds": 3,
    # 写す範囲を、本編の区間の端からCM側へ延ばす秒数(CM解析が本編の端を切ることがあるため)。
    # 最後の本編の区間の後ろは予告など、ずれの違う内容が続くことが多いので短くする。
    # 前後の区間のずれが違うときは、間のCMの長さが局で違い、どこまでがどちらのずれか決まらないので延ばさない
    # (延ばすと、CMの中の番宣などの一致を別のずれで写して10秒超の誤が増えた)。
    "cm_edge_seconds": 45,
    "cm_tail_seconds": 10,
    "cm_edge_change_seconds": 0,
    # 山の位置の取り直し(refine_smooth_seconds)。窓ごとの推定では悪化するが、本編の区間ごとの推定では
    # 10秒以内の誤が減った(5〜7秒で同じ程度、4秒以下と8秒以上で悪化)。
    "cm_refine_smooth_seconds": 5,
}

_KATA_TO_HIRA = {c: c - 0x60 for c in range(0x30A1, 0x30F7)}
_NON_WORD = re.compile(r"[^\w]")
_REPEAT = re.compile(r"(.)\1+")


def normalize(text):
    """本文の正規化。NFKC、小文字、カタカナ→ひらがな、記号と空白の除去、同じ文字の連続を1文字に。"""
    text = unicodedata.normalize("NFKC", text).casefold().translate(_KATA_TO_HIRA)
    # \wは「_」を含むので、記号として除く。
    text = _NON_WORD.sub("", text).replace("_", "")
    return _REPEAT.sub(r"\1", text)


def grams(text, sizes):
    """正規化済みの本文の文字n-gram。同じ特徴は1回だけ数える。
    重みの足し算の順が実行ごとに変わらないよう(文字列のハッシュは実行ごとに変わる)、並べて返す。"""
    return sorted({text[i:i + n] for n in sizes for i in range(len(text) - n + 1)})


def comment_time(chat):
    """コメントの絶対時刻。vposは端末が決めた値で投稿者ごとにずれるので使わない。"""
    return int(chat.get("date")) + int(chat.get("date_usec", "0")) / 1e6


def read_comments(path, begin=None, end=None):
    """過去ログのXMLファイルから(絶対時刻, 本文)を時刻順に返す。begin/endで範囲を絞る(半開区間)。"""
    return _rows(ET.parse(path).getroot(), begin, end)


def comments_from_xml(xml_str, begin=None, end=None):
    """過去ログのXML文字列から(絶対時刻, 本文)を時刻順に返す。begin/endで範囲を絞る(半開区間)。"""
    return _rows(ET.fromstring(xml_str), begin, end)


def _rows(root, begin, end):
    rows = []
    for chat in root.findall("chat"):
        if not chat.text or chat.get("date") is None:
            continue
        stamp = comment_time(chat)
        if (begin is None or stamp >= begin) and (end is None or stamp < end):
            rows.append((stamp, chat.text))
    rows.sort(key=lambda row: row[0])
    return rows


def build_idf(rows_list, params):
    """両局のコメントを合わせた文書頻度からIDFを作る。use_idfがFalseなら全部1。"""
    df = collections.Counter()
    total = 0
    for rows in rows_list:
        for _, text in rows:
            df.update(grams(normalize(text), params["gram_sizes"]))
            total += 1
    if not params["use_idf"]:
        return {key: 1.0 for key in df}
    return {key: math.log(total / count) for key, count in df.items()}


def vectorize(rows, idf, params):
    """コメントごとの(時刻, {特徴: 重み})。normに従って重みをそろえる(l2ならコサイン類似度の分解)。
    正規化で空になったコメントと、重みが全部0のコメントは使わない。"""
    result = []
    for stamp, text in rows:
        weights = {key: idf.get(key, 0.0) for key in grams(normalize(text), params["gram_sizes"])}
        if params["norm"] == "count":
            norm = math.sqrt(len(weights))
        else:
            norm = math.sqrt(sum(value * value for value in weights.values()))
        if norm > 0:
            result.append((stamp, {key: value / norm for key, value in weights.items() if value > 0}))
    return result


class ReferenceIndex:
    """基準のコメントの、特徴ごとの(時刻, 重み)の索引。時刻順に並べ、探索範囲だけを二分探索で取り出す。"""

    def __init__(self, vectors, idf=None):
        entries = collections.defaultdict(list)
        self.idf = idf or {}
        # コメントごとの重みのベクトルの長さ(境目を詰めるときのコサイン類似度に使う)
        self.lengths = [math.sqrt(sum(w * w for w in weights.values())) for _, weights in vectors]
        for number, (stamp, weights) in enumerate(vectors):
            for key, weight in weights.items():
                entries[key].append((stamp, weight, number))
        self.times, self.weights, self.numbers = {}, {}, {}
        for key, items in entries.items():
            items.sort()
            self.times[key] = [stamp for stamp, _, _ in items]
            self.weights[key] = [weight for _, weight, _ in items]
            self.numbers[key] = [number for _, _, number in items]


def vote(target, index, center, params):
    """投票。時刻差(基準−対象−center)を箱に分け、共有する特徴の重みの積を足す。"""
    search, width = params["search_seconds"], params["bin_seconds"]
    size = int(round(2 * search / width)) + 1
    votes = [0.0] * size
    for stamp, weights in target:
        low, high = stamp + center - search, stamp + center + search
        for key, weight in weights.items():
            times = index.times.get(key)
            if times is None:
                continue
            others = index.weights[key]
            for i in range(bisect.bisect_left(times, low), bisect.bisect_right(times, high)):
                position = int(round((times[i] - low) / width))
                if 0 <= position < size:
                    votes[position] += weight * others[i]
    return votes


def smooth(votes, params):
    """三角窓で平滑化する。"""
    half = int(round(params["smooth_seconds"] / params["bin_seconds"]))
    kernel = [1 - abs(i) / (half + 1) for i in range(-half, half + 1)]
    size = len(votes)
    out = [0.0] * size
    for i, value in enumerate(votes):
        if value == 0.0:
            continue
        for j, k in enumerate(kernel):
            p = i + j - half
            if 0 <= p < size:
                out[p] += value * k
    return out


def find_peak(scores, center, params):
    """最大の山の位置(絶対のずれ)と信頼度(最大の山÷前後exclude_secondsの外の2番目の山)。"""
    best = max(range(len(scores)), key=scores.__getitem__)
    if scores[best] <= 0:
        return None
    exclude = int(round(params["exclude_seconds"] / params["bin_seconds"]))
    outside = scores[:max(0, best - exclude)] + scores[best + exclude + 1:]
    second = max(outside) if outside else 0.0
    return {"delta": center - params["search_seconds"] + best * params["bin_seconds"],
            "ratio": scores[best] / second if second > 0 else math.inf, "score": scores[best]}


def estimate(target, index, center, params):
    """対象のコメントの集まり1つについて、ずれを推定する。山がなければNone。

    refine_smooth_secondsが正なら、山の有無と信頼度はsmooth_secondsの平滑化で決め、
    位置だけをその山の前後smooth_seconds以内で、より狭い平滑化の最大に取り直す。
    """
    if not target:
        return None
    votes = vote(target, index, center, params)
    peak = find_peak(smooth(votes, params), center, params)
    fine_seconds = params["refine_smooth_seconds"]
    if peak is None or fine_seconds <= 0:
        return peak
    fine = smooth(votes, dict(params, smooth_seconds=fine_seconds))
    width, origin = params["bin_seconds"], center - params["search_seconds"]
    best = int(round((peak["delta"] - origin) / width))
    half = int(round(params["smooth_seconds"] / width))
    low, high = max(0, best - half), min(len(fine), best + half + 1)
    position = max(range(low, high), key=fine.__getitem__)
    if fine[position] > 0:
        peak["delta"] = origin + position * width
    return peak


def comment_fit(stamp, weights, index, delta, params):
    """対象のコメント1件が、ずれdeltaのとき基準のコメントとどれだけ一致するか(平滑化と同じ三角窓で重み付け)。"""
    half = params["smooth_seconds"]
    total = 0.0
    for key, weight in weights.items():
        times = index.times.get(key)
        if times is None:
            continue
        others = index.weights[key]
        expected = stamp + delta
        for i in range(bisect.bisect_left(times, expected - half), bisect.bisect_right(times, expected + half)):
            total += weight * others[i] * (1 - abs(times[i] - expected) / (half + params["bin_seconds"]))
    return total


def supports(stamp, weights, index, delta, params):
    """対象のコメント1件に、ずれdeltaの前後smooth_seconds以内で本文が一致する基準のコメントがあるか。

    「w」「草」のような1文字だけの一致や、「きた」のような定型の反応の一致では足りないとし、
    IDFがsupport_idf以上の2文字以上の特徴の共有と、コサイン類似度support_cosine以上を求める。
    """
    half = params["smooth_seconds"]
    expected = stamp + delta
    dots, long_shared = collections.Counter(), set()
    for key, weight in weights.items():
        times = index.times.get(key)
        if times is None:
            continue
        others, numbers = index.weights[key], index.numbers[key]
        for i in range(bisect.bisect_left(times, expected - half), bisect.bisect_right(times, expected + half)):
            dots[numbers[i]] += weight * others[i]
            if len(key) >= 2 and index.idf.get(key, 0.0) >= params["support_idf"]:
                long_shared.add(numbers[i])
    length = math.sqrt(sum(w * w for w in weights.values()))
    return any(dots[number] / (length * index.lengths[number]) >= params["support_cosine"] for number in long_shared)


def supported_flags(target, times, index, low, high, delta, params):
    """[low, high)の対象のコメントごとに、ずれdeltaで本文が一致するか(supports)を返す。

    support_cluster_secondsが正なら、前後その秒数以内に同じずれで一致したコメントがもう1件ないものは数えない。
    偶然の一致1件で、境目や端がCMの向こうまで動くのを防ぐ(一致を探すため、範囲の外もその秒数だけ見る)。
    """
    cluster = params["support_cluster_seconds"]
    first, last = bisect.bisect_left(times, low), bisect.bisect_left(times, high)
    if cluster <= 0:
        return [supports(target[k][0], target[k][1], index, delta, params) for k in range(first, last)]
    outer_first, outer_last = bisect.bisect_left(times, low - cluster), bisect.bisect_left(times, high + cluster)
    raw = {k: supports(target[k][0], target[k][1], index, delta, params) for k in range(outer_first, outer_last)}
    hits = [times[k] for k in range(outer_first, outer_last) if raw[k]]
    # 自分自身も hits に入っているので、前後cluster秒以内に2件以上あれば、他にも一致がある。
    return [raw[k] and bisect.bisect_right(hits, times[k] + cluster) - bisect.bisect_left(hits, times[k] - cluster) >= 2
            for k in range(first, last)]


def gap_boundary(target, times, index, low, high, before, after, params):
    """[low, high)の中で、前の区間の終わりと後ろの区間の始まりを決める(boundary="gap")。

    本文が前のずれでだけ一致したコメントを+1、後ろのずれでだけ一致したものを−1とし、
    前から足した値が最大になる範囲を「どちらとも決まらない範囲」(CMの中など)として返す。
    """
    first, last = bisect.bisect_left(times, low), bisect.bisect_left(times, high)
    gain = [0]
    flags_before = supported_flags(target, times, index, low, high, before, params)
    flags_after = supported_flags(target, times, index, low, high, after, params)
    for hit_before, hit_after in zip(flags_before, flags_after):
        gain.append(gain[-1] + int(hit_before) - int(hit_after))
    best = max(gain)
    edges = [low] + times[first:last] + [high]
    run_begin = gain.index(best)
    if params["gap_ties"] == "span":
        # 最大が離れた場所に複数あれば(OPのように両方のずれで一致するコメントが続く場合)、どこで切るか決まらないので、
        # 最初の最大から最後の最大までをまとめて「どちらとも決まらない範囲」にする。
        run_end = len(gain) - 1 - gain[::-1].index(best)
    else:
        run_end = run_begin
        while run_end + 1 < len(gain) and gain[run_end + 1] == best:
            run_end += 1
    # 前の区間は、最後に前のずれで一致したコメントの少し後で終える。後ろの区間は、最初に後ろのずれで
    # 一致したコメントの少し前から始める。一致したコメントの前後の書き込みも拾うため。
    end, start = edges[run_begin], edges[run_end + 1]
    margin = min(params["gap_margin_seconds"], (start - end) / 2)
    return (end + margin if run_begin > 0 else end), (start - margin if run_end + 1 < len(gain) else start)


def refine_boundary(target, times, index, low, high, before, after, params):
    """[low, high)の中で、前の区間のずれbeforeと後ろの区間のずれafterの境目を決める。

    境目より前のコメントはbeforeで、後ろはafterで一致度を数え、その合計が最大になる位置で切る。
    最大が続く範囲(CMの中など、どちらとも一致しないコメントしかない範囲)では、その範囲の中央に置く。
    """
    first, last = bisect.bisect_left(times, low), bisect.bisect_left(times, high)
    # gain[k]: k件目より前で切ったときの、すべてafterで数えた場合からの増分
    gain, best = [0.0], 0.0
    for stamp, weights in target[first:last]:
        gain.append(gain[-1] + comment_fit(stamp, weights, index, before, params)
                    - comment_fit(stamp, weights, index, after, params))
        best = max(best, gain[-1])
    # k件目より前で切るとき、境目は edges[k](直前のコメント、またはlow)〜edges[k + 1](k件目、またはhigh)のどこでもよい。
    edges = [low] + times[first:last] + [high]
    tolerance = max(abs(best), 1.0) * 1e-9
    run_begin = next(k for k, value in enumerate(gain) if value >= best - tolerance)
    run_end = run_begin
    while run_end + 1 < len(gain) and gain[run_end + 1] >= best - tolerance:
        run_end += 1
    return (edges[run_begin] + edges[run_end + 1]) / 2


def find_segments(target, index, center, target_start, target_duration, params):
    """区間の決め方。区間を知らない状態で、対象の録画の時刻(先頭からの秒)で区間とずれを返す。

    返り値: (segments, windows)
      segments: [{"start", "end", "delta", "ratio", "comments", "windows"}] 対象の録画の時刻順
      windows: 窓ごとの推定(評価・調査用)
    """
    window, step = params["window_seconds"], params["step_seconds"]
    times = [stamp - target_start for stamp, _ in target]

    def part(begin, end):
        return target[bisect.bisect_left(times, begin):bisect.bisect_left(times, end)]

    # 1. 窓ごとに推定する。窓の終わりは録画の末尾を越えてよい(最後の窓が短くなるだけ)。
    windows = []
    begin = 0.0
    while begin < target_duration:
        comments = part(begin, begin + window)
        row = {"begin": begin, "end": begin + window, "comments": len(comments), "delta": None, "ratio": 0.0}
        if len(comments) >= params["min_window_comments"]:
            result = estimate(comments, index, center, params)
            if result is not None:
                row.update(delta=result["delta"], ratio=result["ratio"])
        windows.append(row)
        if begin + window >= target_duration:
            break
        begin += step

    # 2. 信頼できた窓を時刻順に並べ、推定値の差がmerge_seconds以内で続くものをまとめる。
    groups = []
    for row in windows:
        if row["delta"] is None or row["ratio"] < params["min_ratio"]:
            continue
        if groups and abs(row["delta"] - groups[-1][-1]["delta"]) <= params["merge_seconds"]:
            groups[-1].append(row)
        else:
            groups.append([row])
    groups = [group for group in groups if len(group) >= params["min_windows"]]
    # 短いまとまりを捨てたあとで、隣どうしが同じずれになったらつなぐ。
    merged = []
    for group in groups:
        if merged and abs(group[0]["delta"] - merged[-1][-1]["delta"]) <= params["merge_seconds"]:
            merged[-1].extend(group)
        else:
            merged.append(group)

    # 2b. 区間の前後に続く窓は、山の比がextend_ratio以上で、推定値が区間の端の窓とmerge_seconds以内なら区間に足す。
    #     番組の冒頭や末尾は反応だけのコメントが多く、推定は合っていても山の比が下がるため、区間を始めるには
    #     min_ratioを求め、延ばすだけなら弱い山でもよいとする。足した窓は区間の範囲を広げるのにだけ使い、
    #     ずれと山の比の推定(4., 5.)は足す前の範囲(芯)で行う(広げた範囲で推定すると山の比が薄まり、正しい区間を
    #     7.で捨ててしまう)。外側の端は8.で本文が一致したコメントに合わせて切り詰める。
    extended = [list(group) for group in merged]
    if params["extend_ratio"] > 0:
        position = {id(row): i for i, row in enumerate(windows)}
        taken = {id(row) for group in merged for row in group}

        def weak(row, reference):
            return (row["delta"] is not None and row["ratio"] >= params["extend_ratio"] and id(row) not in taken
                    and abs(row["delta"] - reference["delta"]) <= params["merge_seconds"])

        for group in extended:
            # 比べる相手は元の端の窓に固定する(足した窓と比べると、merge_secondsずつずれが流れていく)
            first, last = group[0], group[-1]
            i = position[id(first)] - 1
            while i >= 0 and weak(windows[i], first):
                group.insert(0, windows[i])
                taken.add(id(windows[i]))
                i -= 1
            i = position[id(last)] + 1
            while i < len(windows) and weak(windows[i], last):
                group.append(windows[i])
                taken.add(id(windows[i]))
                i += 1

    # 3. 境目を、前の区間の最後の窓と後ろの区間の最初の窓の中心の中間に置く。
    center_of = lambda row: (row["begin"] + min(row["end"], target_duration)) / 2

    def spans(groups):
        result = []
        for i, group in enumerate(groups):
            start = group[0]["begin"] if i == 0 else (center_of(groups[i - 1][-1]) + center_of(group[0])) / 2
            end = (min(group[-1]["end"], target_duration) if i + 1 == len(groups)
                   else (center_of(group[-1]) + center_of(groups[i + 1][0])) / 2)
            result.append((start, end))
        return result

    segments = [{"start": start, "end": end, "core_start": core_start, "core_end": core_end, "windows": len(group)}
                for (core_start, core_end), (start, end), group in zip(spans(merged), spans(extended), merged)]

    # 4. 区間ごとに、芯の範囲の対象のコメント全部で推定し直す。窓の推定値から離れたら窓の中央値を使う。
    for segment, group in zip(segments, merged):
        comments = part(segment["core_start"], segment["core_end"])
        deltas = sorted(row["delta"] for row in group)
        median = deltas[len(deltas) // 2]
        result = estimate(comments, index, center, params)
        if result is not None and abs(result["delta"] - median) <= params["merge_seconds"]:
            segment.update(delta=result["delta"], ratio=result["ratio"])
        else:
            segment.update(delta=median, ratio=min(row["ratio"] for row in group))
    merged = extended

    # 5. 推定し直したずれがほぼ同じ隣どうしは、CMの長さの違いではなく推定のぶれなので、1つにまとめて推定し直す。
    #    局ごとのCMの長さの差は5秒単位のことが多いので、それより小さい差はまとめる。
    i = 0
    while i + 1 < len(segments):
        if abs(segments[i]["delta"] - segments[i + 1]["delta"]) > params["same_delta_seconds"]:
            i += 1
            continue
        joined = {"start": segments[i]["start"], "end": segments[i + 1]["end"],
                  "core_start": segments[i]["core_start"], "core_end": segments[i + 1]["core_end"],
                  "windows": segments[i]["windows"] + segments[i + 1]["windows"]}
        result = estimate(part(joined["core_start"], joined["core_end"]), index, center, params)
        if result is not None:
            joined.update(delta=result["delta"], ratio=result["ratio"])
        else:
            joined.update(delta=segments[i]["delta"], ratio=segments[i]["ratio"])
        segments[i:i + 2] = [joined]
        merged[i:i + 2] = [merged[i] + merged[i + 1]]

    # 6. 境目を詰める。探す範囲は、前の区間の最後の窓の始まりから、後ろの区間の最初の窓の終わりまで。
    if params["boundary"] in ("refine", "gap"):
        for i in range(len(segments) - 1):
            low = max(segments[i]["start"], merged[i][-1]["begin"])
            high = min(segments[i + 1]["end"], merged[i + 1][0]["end"], target_duration)
            if high <= low:
                continue
            before, after = segments[i]["delta"], segments[i + 1]["delta"]
            if params["boundary"] == "gap":
                segments[i]["end"], segments[i + 1]["start"] = gap_boundary(target, times, index, low, high,
                                                                            before, after, params)
            else:
                boundary = refine_boundary(target, times, index, low, high, before, after, params)
                segments[i]["end"] = segments[i + 1]["start"] = boundary

    # 7. 区間ごとに本文が一致したコメントを数え、信頼できない区間を捨てる。
    #    一致件数は偶然の一致も含めて数え、外側の端を決める一致はsupport_cluster_secondsで孤立したものを除く。
    supported_times = []
    for segment in segments:
        first, last = bisect.bisect_left(times, segment["start"]), bisect.bisect_left(times, segment["end"])
        segment["support"] = sum(supports(target[k][0], target[k][1], index, segment["delta"], params)
                                 for k in range(first, last))
        edge_params = params if params["support_cluster_edge"] else dict(params, support_cluster_seconds=0)
        flags = supported_flags(target, times, index, segment["start"], segment["end"], segment["delta"], edge_params)
        supported_times.append([times[first + k] for k, flag in enumerate(flags) if flag])
    kept = [(s, t) for s, t in zip(segments, supported_times)
            if (s["ratio"] >= params["min_segment_ratio"] or s["windows"] >= params["ratio_exempt_windows"])
            and s["support"] >= params["min_support"]]

    # 8. 外側の端は、窓の端ではなく一致したコメントに合わせる
    #    (窓は180秒あるので、端の窓の中にずれの違う短い本編やCMがあると、そこまで写してしまう)。
    if params["edge"] == "support" and kept:
        margin = params["edge_margin_seconds"]
        if kept[0][1]:
            kept[0][0]["start"] = max(kept[0][0]["start"], kept[0][1][0] - margin)
        if kept[-1][1]:
            kept[-1][0]["end"] = min(kept[-1][0]["end"], kept[-1][1][-1] + margin)
    segments = [s for s, _ in kept if s["end"] > s["start"]]
    for segment in segments:
        segment["comments"] = len(part(segment["start"], segment["end"]))
    return segments, windows


# find_segments_cmの中で使う窓ごとの推定(評価の道具がfind_segmentsを差し替えても、こちらは変わらない)
_window_segments = find_segments


def find_segments_cm(target, index, center, target_start, target_duration, spans, params):
    """本編の区間ごとの決め方。対象のCM解析の結果の本編の区間spans([(開始, 終了)]、録画の先頭からの秒)ごとにずれを推定する。

    返り値はfind_segmentsと同じ形(windowsは空)。区間の範囲は推定せず、本編の区間(と、その端からCM側へ延ばした範囲)を写す。
    """
    params = dict(params, refine_smooth_seconds=params["cm_refine_smooth_seconds"])
    narrow = dict(params, smooth_seconds=params["cm_fill_smooth_seconds"])
    times = [stamp - target_start for stamp, _ in target]

    def part(begin, end):
        return target[bisect.bisect_left(times, begin):bisect.bisect_left(times, end)]

    def support(comments, delta, p=params):
        return sum(supports(stamp, weights, index, delta, p) for stamp, weights in comments)

    def distance(a, b):
        return max(a["start"] - b["end"], b["start"] - a["end"])

    # 1. 本編の区間ごとに、その中の対象のコメント全部で1回だけ推定する(窓には分けない)。
    rows = []
    for start, end in sorted(spans):
        start, end = max(start, 0.0), min(end, target_duration)
        if end <= start:
            continue
        comments = part(start, end)
        row = {"start": start, "end": end, "delta": None, "ratio": 0.0, "windows": 0, "support": 0, "ok": False}
        result = estimate(comments, index, center, params) if comments else None
        if result is not None:
            row.update(delta=result["delta"], ratio=result["ratio"], support=support(comments, result["delta"]))
            row["ok"] = ((row["ratio"] >= params["min_ratio"] and row["support"] >= params["min_support"])
                         or (row["ratio"] >= params["cm_span_ratio"] and row["support"] >= params["cm_span_support"]))
        rows.append(row)
    anchors = [row for row in rows if row["ok"]]

    # 2. ずれの近い信頼できた区間(隣どうしでなくてよい。本編のAパートとBパートなど)をまとめて推定し直し、同じずれにそろえる。
    if len(anchors) >= 2:
        ordered = sorted(anchors, key=lambda row: row["delta"])
        clusters = [[ordered[0]]]
        for row in ordered[1:]:
            if row["delta"] - clusters[-1][-1]["delta"] <= params["same_delta_seconds"]:
                clusters[-1].append(row)
            else:
                clusters.append([row])
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            comments = [c for row in sorted(cluster, key=lambda row: row["start"]) for c in part(row["start"], row["end"])]
            result = estimate(comments, index, center, params)
            if result is not None and (cluster[0]["delta"] - params["merge_seconds"] <= result["delta"]
                                       <= cluster[-1]["delta"] + params["merge_seconds"]):
                for row in cluster:
                    row["delta"] = result["delta"]

    # 3. 一致が少なくて信頼できなかった区間でも、単独の推定の山が十分に高く、信頼できた区間のずれと合っていれば、そのずれで写す。
    #    (コメントの少ない番組では、区間ごとの一致は1〜2件でも、推定は信頼できた区間と同じずれになる)
    for row in rows:
        if row["ok"] or row["delta"] is None or row["ratio"] < params["min_ratio"] or row["support"] < 1:
            continue
        near = [a for a in anchors if abs(a["delta"] - row["delta"]) <= params["cm_near_seconds"]]
        if near:
            row.update(delta=min(near, key=lambda a: abs(a["delta"] - row["delta"]))["delta"], ok=True, filled=True)

    # 4. 本編の区間すべてのコメントで1つのずれを推定する(候補の1つ。信頼できた区間より後ろに置く)。
    candidates = sorted(anchors, key=lambda a: a["start"])
    everything = [c for row in rows for c in part(row["start"], row["end"])]
    whole = estimate(everything, index, center, params) if everything else None
    if whole is not None and (whole["ratio"] < params["min_ratio"]
                              or support(everything, whole["delta"]) < params["min_support"]):
        whole = None

    # 5. 信頼できなかった区間は、信頼できた区間のずれと全体のずれのうち、本文の一致がcm_fill_support件以上で
    #    最も多いもので写す(区間自身の推定のほうが一致が多ければ写さない)。信頼できた区間が1つもなければ、全体のずれで写す。
    for row in rows:
        if row["ok"]:
            continue
        comments = part(row["start"], row["end"])
        deltas = [a["delta"] for a in sorted(candidates, key=lambda a: distance(a, row))]
        if whole is not None:
            deltas.append(whole["delta"])
        best, chosen = 0, None
        for delta in deltas:
            count = support(comments, delta, narrow) if comments else 0
            if count >= params["cm_fill_support"] and count > best:
                best, chosen = count, delta
        # 区間自身の推定(山の比が足りずに信頼しなかったもの)のほうが本文の一致が多ければ、候補のずれは偶然の一致とみて写さない。
        # (CMのたびにずれが変わる作品で、ずれの30秒違う候補に3件だけ一致して、区間ごと写し間違えることがあった)
        own = row["delta"]
        if chosen is not None and own is not None and abs(own - chosen) > params["merge_seconds"] \
                and support(comments, own, narrow) > best:
            chosen = None
        if chosen is None and not candidates and whole is not None:
            chosen = whole["delta"]
        if chosen is not None:
            row.update(delta=chosen, ok=True, filled=True, support=best)

    # 6. それでも推定できなかった区間は、区間の中だけで窓ごとの推定を行い、見つかった範囲だけ写す
    #    (基準の局にだけCMが入り、本編の区間の途中でずれが変わる場合など)。
    output = [row for row in rows if row["ok"]]
    for row in rows:
        comments = part(row["start"], row["end"])
        if row["ok"] or not comments:
            continue
        found, _ = _window_segments(comments, index, center, target_start + row["start"], row["end"] - row["start"], params)
        for segment in found:
            start, end = max(row["start"], segment["start"] + row["start"]), min(row["end"], segment["end"] + row["start"])
            if end > start:
                output.append(dict(segment, start=start, end=end, filled=True))
    output.sort(key=lambda segment: segment["start"])

    # 7. 写す範囲を、本編の区間の端からCM側へ延ばす(CM解析が本編の端を切ることがあるため)。
    #    隣の区間との間が延ばす幅の2倍より短ければ、間の中点で分ける。隣の区間とずれが違えばcm_edge_change_seconds、
    #    最後の本編の区間の後ろだけはcm_tail_secondsにする。
    if output:
        edge = params["cm_edge_seconds"]
        output[0]["start"] = max(0.0, output[0]["start"] - edge)
        later = any(row["start"] >= output[-1]["end"] for row in rows)
        output[-1]["end"] = min(target_duration, output[-1]["end"] + (edge if later else params["cm_tail_seconds"]))
        for before, after in zip(output, output[1:]):
            width = edge if abs(before["delta"] - after["delta"]) <= params["merge_seconds"] else params["cm_edge_change_seconds"]
            step = min(width, (after["start"] - before["end"]) / 2)
            before["end"] += step
            after["start"] -= step
    segments = []
    for segment in output:
        segment.pop("ok", None)
        segment["comments"] = len(part(segment["start"], segment["end"]))
        segments.append(segment)
    return segments, []


def enough_comments(target_rows, reference_rows, params):
    """両局のコメントが、推定するのに足りる件数あるか(min_comments)。"""
    return min(len(target_rows), len(reference_rows)) >= params["min_comments"]


def make_timemap(segments, target_duration, reference_duration, fill="interpolate"):
    """区間とずれから対応表[(基準の開始, 基準の終了, 対象の開始, 対象の終了)]を作る。

    方式AのMakeNicoJKTimeMap(NicoJKAlign.cpp)と同じ。時刻は、対象が録画の先頭から、
    基準が基準の原点からの秒。segmentsのdeltaも同じ原点どうしの差(基準 − 対象)で渡す。
    fill="inner"なら両端の外挿を作らない。fill="strict"なら区間の間も作らず、区間の中だけを写す。
    """
    if not segments or target_duration <= 0 or reference_duration <= 0:
        return []
    # 1. 一致区間、区間の間、前後の外挿を対象の時刻順に並べる。
    candidates = []
    first, last = segments[0], segments[-1]
    if fill == "interpolate":
        candidates.append((first["delta"], first["start"] + first["delta"], 0.0, first["start"]))
    for i, part in enumerate(segments):
        candidates.append((part["start"] + part["delta"], part["end"] + part["delta"], part["start"], part["end"]))
        if fill != "strict" and i + 1 < len(segments):
            following = segments[i + 1]
            candidates.append((part["end"] + part["delta"], following["start"] + following["delta"],
                               part["end"], following["start"]))
    if fill == "interpolate":
        candidates.append((last["end"] + last["delta"], target_duration + last["delta"], last["end"], target_duration))

    # 2. 基準または対象の範囲から外れる分を、傾きを保って切り詰める。
    clipped = []
    for ref_start, ref_end, tgt_start, tgt_end in candidates:
        if ref_end <= ref_start or tgt_end <= tgt_start:
            continue
        slope = (tgt_end - tgt_start) / (ref_end - ref_start)
        begin = max(ref_start, 0.0, ref_start - tgt_start / slope)
        end = min(ref_end, reference_duration, ref_start + (target_duration - tgt_start) / slope)
        if end > begin:
            clipped.append((begin, end, tgt_start + (begin - ref_start) * slope, tgt_start + (end - ref_start) * slope))

    # 3. 直前の要素と重なる分を削り、二重配置と時刻の逆転を防ぐ。
    timemap = []
    for ref_start, ref_end, tgt_start, tgt_end in clipped:
        if timemap:
            slope = (tgt_end - tgt_start) / (ref_end - ref_start)
            begin = max(ref_start, timemap[-1][1], ref_start + (timemap[-1][3] - tgt_start) / slope)
            tgt_start += (begin - ref_start) * slope
            ref_start = begin
        if ref_end > ref_start:
            timemap.append((ref_start, ref_end, tgt_start, tgt_end))
    return timemap


def map_time(timemap, position):
    """nicojk_ass.pyのapply_timemapと同じ半開区間で1点を写す。対応がなければNone。"""
    for ref_start, ref_end, tgt_start, tgt_end in timemap:
        if ref_start <= position < ref_end:
            return tgt_start + (position - ref_start) * (tgt_end - tgt_start) / (ref_end - ref_start)
    return None


def prepare(target_rows, reference_rows, params):
    """両局のコメントからIDFを作り、対象のベクトルと基準の索引を返す。"""
    idf = build_idf([target_rows, reference_rows], params)
    return vectorize(target_rows, idf, params), ReferenceIndex(vectorize(reference_rows, idf, params), idf)
