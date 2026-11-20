"""音声照合用のWAVの読み込み(numpyを使う)。"""

import math
import wave

import numpy as np

SAMPLE_RATE = 8000
# 阻止約60dBのKaiser。4kHzちょうどで止め、遷移は400Hz(通過の端は3.6kHz)。
_ATTEN_DB = 60.0
_STOP_HZ = SAMPLE_RATE / 2
_TRANSITION_HZ = 400.0
_MAX_TAPS = 8191


def read_wav(path):
    """PCMのWAVを、8kHzモノラルのfloat32にして返す。

    値の大きさはAmatsukazeのAddNicoJKAlignAudioFrameと同じ(フルスケールが±1。
    16bitならint16を32768で割る。複数チャンネルは平均)。標準ライブラリのwaveが読む
    PCM(8/16/24/32bit、任意のサンプリング周波数とチャンネル数)を受け、8kHzでなければ
    折り返しを落としてから8kHzにする。拡張子が.f32なら、8kHzモノラルのfloat32の
    生のPCMとして読む(開発用)。
    """
    if str(path).lower().endswith(".f32"):
        return np.fromfile(path, dtype=np.float32)
    try:
        with wave.open(str(path), "rb") as source:
            channels, width, rate = source.getnchannels(), source.getsampwidth(), source.getframerate()
            if source.getcomptype() != "NONE" or width not in (1, 2, 3, 4) or channels < 1 or rate <= 0:
                raise ValueError(f"PCMのWAVではありません: {path}")
            data = source.readframes(source.getnframes())
    except wave.Error as error:
        raise ValueError(f"WAVを読めません: {path}: {error}")
    return _resample(_decode_pcm(data, channels, width), rate)


def _decode_pcm(data, channels, width):
    """PCMのバイト列を、チャンネルを平均したfloat32の±1にする。"""
    frames = len(data) // (channels * width)
    if frames == 0:
        return np.zeros(0, dtype=np.float32)
    data = data[:frames * channels * width]
    if width == 1:
        values = (np.frombuffer(data, dtype=np.uint8).astype(np.float32) - np.float32(128)) / np.float32(128)
    elif width == 2:
        values = np.frombuffer(data, dtype="<i2").astype(np.float32) / np.float32(32768)
    elif width == 3:
        raw = np.frombuffer(data, dtype=np.uint8).reshape(frames, channels, 3)
        assembled = (raw[:, :, 0].astype(np.int32)
                     | (raw[:, :, 1].astype(np.int32) << 8)
                     | (raw[:, :, 2].astype(np.int32) << 16))
        values = np.where(assembled >= 0x800000, assembled - 0x1000000, assembled)
        values = values.astype(np.float32) / np.float32(8388608)
        if channels == 1:
            return np.ascontiguousarray(values.reshape(frames), dtype=np.float32)
        return np.ascontiguousarray(values.mean(axis=1), dtype=np.float32)
    else:
        values = (np.frombuffer(data, dtype="<i4").astype(np.float64) / 2147483648.0).astype(np.float32)
    if channels == 1:
        return np.ascontiguousarray(values, dtype=np.float32)
    return np.ascontiguousarray(values.reshape(frames, channels).mean(axis=1), dtype=np.float32)


def _resample(samples, rate):
    """8kHzのfloat32にする。すでに8kHzならフィルタを通さない。

    高いレートは、入力のレートで4kHzより上を落としてから間引く。8kHzで割り切れるときは
    その間引きだけである。割り切れないときは、折り返す成分がもう小さいので、遷移の広い
    ポリフェーズで出力の時刻へ載せる。低いレートは、元のナイキストでイメージを止める
    ポリフェーズで上げる。
    """
    if len(samples) == 0 or rate == SAMPLE_RATE:
        return np.ascontiguousarray(samples, dtype=np.float32)
    group = math.gcd(int(rate), SAMPLE_RATE)
    up, down = SAMPLE_RATE // group, int(rate) // group
    audio = np.asarray(samples, dtype=np.float64)
    if rate > SAMPLE_RATE:
        audio = _filter_zero_phase(audio, _lowpass_taps(rate, _STOP_HZ - _TRANSITION_HZ / 2, _TRANSITION_HZ))
        if up == 1:
            return np.ascontiguousarray(audio[::down], dtype=np.float32)
        stop = rate - _STOP_HZ
        transition = stop - (_STOP_HZ - _TRANSITION_HZ)
        center = (stop + _STOP_HZ - _TRANSITION_HZ) / 2
        taps = _lowpass_taps(rate * up, center, transition, minimum=8 * up + 1)
        return np.ascontiguousarray(_polyphase(audio, taps, up, down), dtype=np.float32)
    center = rate / 2 - min(_TRANSITION_HZ, rate / 8) / 2
    transition = min(_TRANSITION_HZ, rate / 8)
    taps = _lowpass_taps(rate * up, center, transition, minimum=8 * up + 1)
    return np.ascontiguousarray(_polyphase(audio, taps, up, down), dtype=np.float32)


def _kaiser_beta(atten_db):
    """Kaiser窓のβ。阻止量からの近似(Oppenheim/Schafer)。"""
    if atten_db > 50:
        return 0.1102 * (atten_db - 8.7)
    if atten_db >= 21:
        return 0.5842 * (atten_db - 21) ** 0.4 + 0.07886 * (atten_db - 21)
    return 0.0


def _lowpass_taps(sample_rate, center_hz, transition_hz, minimum=3):
    """和が1の線形位相ローパス。center_hzは遷移の中央、遷移幅はHz。"""
    transition_hz = max(float(transition_hz), sample_rate / _MAX_TAPS)
    order = math.ceil((_ATTEN_DB - 8) / (2.285 * (2 * math.pi * transition_hz / sample_rate)))
    length = max(order + 1, minimum)
    if length % 2 == 0:
        length += 1
    if length > _MAX_TAPS:
        length = _MAX_TAPS if _MAX_TAPS % 2 else _MAX_TAPS - 1
    offset = np.arange(length) - (length - 1) / 2
    scale = 2 * center_hz / sample_rate
    taps = scale * np.sinc(scale * offset)
    taps *= np.kaiser(length, _kaiser_beta(_ATTEN_DB))
    total = float(taps.sum())
    if total != 0:
        taps /= total
    return taps


def _filter_zero_phase(samples, taps):
    """線形位相FIRを、遅れが残らないように畳み込む。両端は0として扱う。"""
    samples = np.asarray(samples, dtype=np.float64)
    taps = np.asarray(taps, dtype=np.float64)
    n_samples, n_taps = len(samples), len(taps)
    if n_samples == 0:
        return samples.copy()
    delay = (n_taps - 1) // 2
    fft_size = 256
    while fft_size < max(n_taps * 8, 1024):
        fft_size *= 2
    step = fft_size - n_taps + 1
    n_blocks = (n_samples + step - 1) // step
    padded = np.zeros(n_blocks * step, dtype=np.float64)
    padded[:n_samples] = samples
    spectrum = np.fft.rfft(taps, fft_size)
    output = np.zeros(n_blocks * step + fft_size, dtype=np.float64)
    columns = np.arange(fft_size)
    for start in range(0, n_blocks, 64):
        block = padded.reshape(n_blocks, step)[start:start + 64]
        response = np.fft.irfft(np.fft.rfft(block, fft_size, axis=1) * spectrum, fft_size, axis=1)
        offset = (start * step) + np.arange(len(response))[:, None] * step + columns
        np.add.at(output, offset, response)
    return output[delay:delay + n_samples]


def _polyphase(samples, taps, up, down):
    """和が1のFIR(アップサンプル後のレート)で、入力をup/down倍する。出力はゼロ位相。"""
    samples = np.asarray(samples, dtype=np.float64)
    fir = np.asarray(taps, dtype=np.float64) * up
    n_in = len(samples)
    if n_in == 0:
        return samples.copy()
    delay = (len(fir) - 1) // 2
    n_out = (n_in * up + down - 1) // down
    output = np.zeros(n_out, dtype=np.float64)
    inverse = pow(down % up, -1, up)
    for phase in range(up):
        first = ((phase - delay) % up * inverse) % up
        if first >= n_out:
            continue
        count = (n_out - first + up - 1) // up
        coeffs = fir[phase::up]
        if len(coeffs) == 0 or count <= 0:
            continue
        origin = (first * down + delay) // up
        output[first::up] = _dot_past(samples, coeffs, origin, down, count)
    return output


def _dot_past(samples, coeffs, origin, step, count):
    """出力iは、samples[origin + i*step]から過去へcoeffsを畳み込んだ値。範囲外は0。"""
    n_taps = len(coeffs)
    start = origin - (n_taps - 1)
    left = max(0, -start)
    end = start + (count - 1) * step + n_taps
    padded = np.pad(samples, (left, max(0, end - len(samples))))
    base = start + left
    reversed_coeffs = np.ascontiguousarray(coeffs[::-1])
    result = np.empty(count, dtype=np.float64)
    stride = padded.strides[0]
    for offset in range(0, count, 16384):
        rows = min(16384, count - offset)
        window = np.lib.stride_tricks.as_strided(
            padded[base + offset * step:], shape=(rows, n_taps), strides=(step * stride, stride), writeable=False)
        result[offset:offset + rows] = window @ reversed_coeffs
    return result
