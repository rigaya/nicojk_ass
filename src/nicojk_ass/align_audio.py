"""音声照合(方式A)。Amatsukazeのこれまでのネイティブ実装(NicoJKAlign.cpp)をnumpyに移したもの。

時刻は対象・基準それぞれの録画先頭からの秒。deltaは基準-対象。
"""

import math
import time

import numpy as np

SAMPLE_RATE = 8000
WINDOW, HOP, BANDS = 512, 80, 24
PROBE, STEP = 800, 2000
CORRELATION_FFT = 65536
CORRELATION_STEP = CORRELATION_FFT - PROBE + 1
MIN_RATIO, SAME_DELTA = 1.5, 0.05
FEATURE_CHUNK = 4096


def _round(value):
    """C++のstd::roundと同じ四捨五入(0.5は絶対値の大きいほうへ)。"""
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


def get_features(audio):
    """24帯域の対数エネルギーの立ち上がり。形は(BANDS, フレーム数)のfloat32。"""
    if len(audio) <= WINDOW:
        return np.zeros((BANDS, 0), dtype=np.float32)
    frames = (len(audio) - WINDOW) // HOP
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(WINDOW) / (WINDOW - 1))
    hz = np.arange(WINDOW // 2 + 1) * (SAMPLE_RATE / WINDOW)
    edges = [100 * 38.0 ** (band / BANDS) for band in range(BANDS + 1)]
    band_index = np.full(len(hz), -1)
    for band in range(BANDS):
        band_index[(hz >= edges[band]) & (hz < edges[band + 1])] = band
    # 帯域ごとのエネルギーの合計を行列の積で求める。
    selection = np.zeros((len(hz), BANDS))
    for index, band in enumerate(band_index):
        if band >= 0:
            selection[index, band] = 1
    audio = np.asarray(audio, dtype=np.float32)
    level = np.empty((frames, BANDS))
    for first in range(0, frames, FEATURE_CHUNK):
        last = min(frames, first + FEATURE_CHUNK)
        block = np.lib.stride_tricks.sliding_window_view(audio[first * HOP:(last - 1) * HOP + WINDOW], WINDOW)[::HOP]
        spectrum = np.fft.rfft((block * window).astype(np.float32), axis=1)
        energy = (spectrum.real.astype(np.float64) ** 2 + spectrum.imag.astype(np.float64) ** 2) @ selection
        level[first:last] = np.log(energy + 1e-9)
    onset = np.zeros((frames, BANDS))
    onset[1:] = np.maximum(level[1:] - level[:-1], 0)
    onset = (onset - onset.mean(axis=0)) / (onset.std(axis=0) + 1e-9)
    return np.ascontiguousarray(onset.T.astype(np.float32))


def _local_score(target, reference, center, delta):
    """中心の前後1秒の窓を、遅れを±5フレーム動かして照合したときの最大の相関。"""
    center_frame, lag = _round(center * 100), _round(delta * 100)
    best = -1.0
    for adjustment in range(-5, 6):
        shift = lag + adjustment
        first = max(center_frame - 50, 0, -shift)
        last = min(center_frame + 50, target.shape[1], reference.shape[1] - shift)
        if last - first < 20:
            continue
        a = target[:, first:last].astype(np.float64)
        b = reference[:, first + shift:last + shift].astype(np.float64)
        left, right = float((a * a).sum()), float((b * b).sum())
        if left > 0 and right > 0:
            best = max(best, float((a * b).sum()) / math.sqrt(left * right))
    return best


def _refine_edge(target, reference, good, outside, delta, limit):
    """区間の端を、一致する点と一致しない点の間で0.1秒程度まで詰める。"""
    good_score = _local_score(target, reference, good, delta)
    outside_score = _local_score(target, reference, outside, delta)
    direction = 1 if outside >= good else -1
    threshold = good_score * 0.5
    # 外側の点も一致する間は1秒ずつ歩き、隣の一致点または録画端まで調べる。
    while outside_score >= threshold:
        if outside == limit:
            return limit
        good = outside
        outside = min(outside + 1, limit) if direction > 0 else max(outside - 1, limit)
        outside_score = _local_score(target, reference, outside, delta)
    while abs(good - outside) > 0.1:
        middle = (good + outside) / 2
        if _local_score(target, reference, middle, delta) > threshold:
            good = middle
        else:
            outside = middle
    return (good + outside) / 2


def _reference_blocks(reference, starts):
    """基準をPROBE-1点重なるブロックへ分け、各帯域のFFTを最初に1回だけ求める。"""
    blocks = []
    for start in range(0, starts, CORRELATION_STEP):
        padded = np.zeros((BANDS, CORRELATION_FFT), dtype=np.float32)
        part = reference[:, start:start + CORRELATION_FFT]
        padded[:, :part.shape[1]] = part
        blocks.append(np.fft.rfft(padded, axis=1))
    return blocks


def _correlate(target, frame, blocks, starts):
    """切り出し(PROBEフレーム)と基準の全位置の相関(帯域の合計)。"""
    padded = np.zeros((BANDS, CORRELATION_FFT), dtype=np.float32)
    padded[:, :PROBE] = target[:, frame:frame + PROBE][:, ::-1]
    probe = np.fft.rfft(padded, axis=1)
    result = np.empty(starts)
    for number, block in enumerate(blocks):
        output = np.fft.irfft((block * probe).sum(axis=0), CORRELATION_FFT)
        start = number * CORRELATION_STEP
        count = min(CORRELATION_STEP, starts - start)
        result[start:start + count] = output[PROBE - 1:PROBE - 1 + count]
    return result


def align_audio(target8k, reference8k):
    """音声照合。{"success", "reason", "segments", "seconds"}を返す。segmentsは
    {"start", "end", "delta", "probes", "ratio"}の並び。"""
    begin = time.perf_counter()
    result = {"success": False, "reason": "", "segments": [], "seconds": 0.0}
    target_duration = len(target8k) / SAMPLE_RATE
    target, reference = get_features(target8k), get_features(reference8k)
    if target.shape[1] < PROBE + STEP or reference.shape[1] < PROBE:
        result["reason"] = "照合に必要な音声が短すぎます"
        return result

    starts = reference.shape[1] - PROBE + 1
    blocks = _reference_blocks(reference, starts)
    probes = []  # (フレーム, ずれ, 相関比, 一致したか)
    for frame in range(100, target.shape[1] - PROBE + 1, STEP):
        correlation = _correlate(target, frame, blocks, starts)
        location = int(np.argmax(correlation))
        best = float(correlation[location])
        outside = np.concatenate((correlation[:max(0, location - 100)], correlation[location + 100:]))
        second = max(0.0, float(outside.max())) if len(outside) else 0.0
        ratio = best / max(second, 1e-12) if best > 0 else 0.0
        probes.append((frame, (location - frame) / 100.0, ratio, ratio > MIN_RATIO))

    first = 0
    while first < len(probes):
        if not probes[first][3]:
            first += 1
            continue
        last = first
        while (last + 1 < len(probes) and probes[last + 1][3]
               and abs(probes[last + 1][1] - probes[last][1]) <= SAME_DELTA + 1e-9):
            last += 1
        if last > first:
            offsets = sorted(probe[1] for probe in probes[first:last + 1])
            ratio = sum(probe[2] for probe in probes[first:last + 1])
            delta = offsets[len(offsets) // 2]
            first_center, last_center = probes[first][0] / 100.0 + 4, probes[last][0] / 100.0 + 4
            left = probes[first - 1][0] / 100.0 + 4 if first else 0.0
            right = probes[last + 1][0] / 100.0 + 4 if last + 1 < len(probes) else target_duration
            left_limit, right_limit = 0.0, target_duration
            for i in range(first - 1, -1, -1):
                if probes[i][3]:
                    left_limit = probes[i][0] / 100.0 + 4
                    break
            for i in range(last + 1, len(probes)):
                if probes[i][3]:
                    right_limit = probes[i][0] / 100.0 + 4
                    break
            result["segments"].append({
                "start": _refine_edge(target, reference, first_center, left, delta, left_limit),
                "end": _refine_edge(target, reference, last_center, right, delta, right_limit),
                "delta": delta, "probes": last - first + 1, "ratio": ratio / len(offsets)})
        first = last + 1

    matched, covered_end = 0.0, 0.0
    # 境界の推定が重なっても、同じ対象時間を二重に数えない。
    for segment in result["segments"]:
        matched += max(0.0, segment["end"] - max(segment["start"], covered_end))
        covered_end = max(covered_end, segment["end"])
    result["success"] = matched >= target_duration * 0.5 and bool(result["segments"])
    if not result["success"]:
        result["reason"] = "一致した区間が対象の長さの5割未満です"
    result["seconds"] = time.perf_counter() - begin
    return result


def make_timemap(segments, target_duration, reference_duration):
    """区間から、基準の時刻→対象の時刻の区分線形の対応表(4つ組の並び)を作る。"""
    if not segments or target_duration <= 0 or reference_duration <= 0:
        return []

    # 1. 一致区間、CMの間、前後の外挿を対象時刻順に並べる。
    first, last = segments[0], segments[-1]
    candidates = [(first["delta"], first["start"] + first["delta"], 0.0, first["start"])]
    for i, part in enumerate(segments):
        candidates.append((part["start"] + part["delta"], part["end"] + part["delta"], part["start"], part["end"]))
        if i + 1 < len(segments):
            following = segments[i + 1]
            candidates.append((part["end"] + part["delta"], following["start"] + following["delta"],
                               part["end"], following["start"]))
    candidates.append((last["end"] + last["delta"], target_duration + last["delta"], last["end"], target_duration))

    # 2. 基準または対象の録画範囲から外れる分を、傾きを保って切り詰める。
    clipped = []
    for reference_start, reference_end, target_start, target_end in candidates:
        if reference_end <= reference_start or target_end <= target_start:
            continue
        slope = (target_end - target_start) / (reference_end - reference_start)
        begin = max(reference_start, 0.0, reference_start - target_start / slope)
        end = min(reference_end, reference_duration, reference_start + (target_duration - target_start) / slope)
        if end > begin:
            clipped.append([begin, end, target_start + (begin - reference_start) * slope,
                            target_start + (end - reference_start) * slope])

    # 3. 直前の要素と重なる分を削り、二重配置と時刻の逆転を防ぐ。
    timemap = []
    for entry in clipped:
        if timemap:
            slope = (entry[3] - entry[2]) / (entry[1] - entry[0])
            begin = max(entry[0], timemap[-1][1], entry[0] + (timemap[-1][3] - entry[2]) / slope)
            entry[2] += (begin - entry[0]) * slope
            entry[0] = begin
        if entry[1] > entry[0]:
            timemap.append(tuple(entry))
    return timemap


def align_result_json(result, target_duration, reference_duration):
    """alignサブコマンドの出力(これまでのNicoJKAlignResultJSONと同じ形に、reasonを足したもの)。"""
    return {"success": result["success"], "reason": result["reason"], "seconds": result["seconds"],
            "segments": result["segments"],
            "timemap": [list(entry) for entry in
                        make_timemap(result["segments"], target_duration, reference_duration)]}
