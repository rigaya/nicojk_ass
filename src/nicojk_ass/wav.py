"""音声照合用のWAVの読み込み(numpyを使う)。"""

import wave

import numpy as np

SAMPLE_RATE = 8000


def read_wav(path):
    """8kHz・16bitのPCMのWAV(モノラルかステレオ)をfloat32の配列にして返す。

    値の大きさはAmatsukazeのAddNicoJKAlignAudioFrameと同じ(int16を32768で割った±1の目盛り。
    ステレオはL+Rの平均)。拡張子が.f32なら、8kHzモノラルのfloat32の生のPCMとして読む(開発用)。
    """
    if str(path).lower().endswith(".f32"):
        return np.fromfile(path, dtype=np.float32)
    try:
        with wave.open(str(path), "rb") as source:
            channels, width, rate = source.getnchannels(), source.getsampwidth(), source.getframerate()
            if source.getcomptype() != "NONE" or width != 2 or rate != SAMPLE_RATE or channels not in (1, 2):
                raise ValueError(f"WAVは16bitのPCM、8kHz、モノラルかステレオにしてください: {path}")
            data = source.readframes(source.getnframes())
    except wave.Error as error:
        raise ValueError(f"WAVを読めません: {path}: {error}")
    pcm = np.frombuffer(data, dtype="<i2").astype(np.float32)
    if channels == 2:
        pcm = pcm[: len(pcm) // 2 * 2].reshape(-1, 2).sum(axis=1)
        return pcm / np.float32(65536)
    return pcm / np.float32(32768)
