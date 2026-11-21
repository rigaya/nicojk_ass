"""tsinfo.pyを、テストの中で組み立てた小さな合成TSで確かめる。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nicojk_ass import tsinfo  # noqa: E402

PAT_PID, PMT_PID, VIDEO_PID, SDT_PID, EIT_PID, TOT_PID = 0x0000, 0x0100, 0x0101, 0x0011, 0x0012, 0x0014
JST_EPOCH_OFFSET = 9 * 3600


def crc32(data):
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def jst_fields(unix):
    """UNIX秒を、EIT・TOTの40bit(MJDとBCDの時分秒)にする。"""
    local = unix + JST_EPOCH_OFFSET
    mjd = local // 86400 + 40587
    rest = local % 86400
    h, m, s = rest // 3600, rest // 60 % 60, rest % 60
    bcd = lambda v: ((v // 10) << 4) | (v % 10)
    return mjd.to_bytes(2, "big") + bytes([bcd(h), bcd(m), bcd(s)])


def long_section(table_id, ident, payload, section_number=0):
    body = ident.to_bytes(2, "big") + bytes([0xC3, section_number, 0]) + payload
    length = len(body) + 4
    head = bytes([table_id, 0xB0 | (length >> 8), length & 0xFF])
    data = head + body
    return data + crc32(data).to_bytes(4, "big")


def tot_section(unix):
    body = jst_fields(unix) + bytes([0xF0, 0x00])
    length = len(body) + 4
    data = bytes([0x73, 0x70 | (length >> 8), length & 0xFF]) + body
    return data + crc32(data).to_bytes(4, "big")


def pat(services):
    return long_section(0x00, 1, b"".join(sid.to_bytes(2, "big") + (0xE000 | pmt).to_bytes(2, "big")
                                          for sid, pmt in services))


def pmt(sid, video_pid, with_video=True):
    es = b""
    if with_video:
        es += bytes([0x1B]) + (0xE000 | video_pid).to_bytes(2, "big") + b"\xf0\x00"
    es += bytes([0x0F]) + (0xE000 | 0x0110).to_bytes(2, "big") + b"\xf0\x00"
    return long_section(0x02, sid, (0xE000 | video_pid).to_bytes(2, "big") + b"\xf0\x00" + es)


def sdt(sids):
    body = (1).to_bytes(2, "big") + b"\xff" + b"".join(
        sid.to_bytes(2, "big") + b"\xfc" + (0x8000).to_bytes(2, "big") for sid in sids)
    return long_section(0x42, 1, body)


def eit(sid, start, duration=1800, genres=((7, 0, 15, 15),), section_number=0):
    h, m, s = duration // 3600, duration // 60 % 60, duration % 60
    bcd = lambda v: ((v // 10) << 4) | (v % 10)
    descriptor = b""
    if genres:
        content = b"".join(bytes([(a << 4) | b, (c << 4) | d]) for a, b, c, d in genres)
        descriptor = bytes([0x54, len(content)]) + content
    event = (1).to_bytes(2, "big") + jst_fields(start) + bytes([bcd(h), bcd(m), bcd(s)]) \
        + (0x8000 | len(descriptor)).to_bytes(2, "big") + descriptor
    return long_section(0x4E, sid, (1).to_bytes(2, "big") * 2 + bytes([0, 0x4E]) + event, section_number)


def packets(pid, section, size=188, scrambling=0, start_counter=0):
    """セクションをTSパケットに分ける(先頭にpointer_fieldを付け、余りは0xFFで埋める)。"""
    payload = b"\x00" + section
    out = []
    for i in range(0, len(payload), 184):
        part = payload[i:i + 184].ljust(184, b"\xff")
        header = bytes([0x47, ((0x40 if i == 0 else 0) | (pid >> 8)), pid & 0xFF,
                        (scrambling << 6) | 0x10 | ((start_counter + i // 184) & 0xF)])
        out.append(header + part)
    return [(b"\x00\x00\x00\x00" + p) if size == 192 else p for p in out]


def video_packet(size=188, scrambling=0):
    header = bytes([0x47, 0x40 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, (scrambling << 6) | 0x10])
    packet = header + b"\x00\x00\x01\xe0" + b"\x00" * 180
    return (b"\x00\x00\x00\x00" + packet) if size == 192 else packet


def null_packet(size=188):
    packet = bytes([0x47, 0x1F, 0xFF, 0x10]) + b"\xff" * 184
    return (b"\x00\x00\x00\x00" + packet) if size == 192 else packet


def build(services, start, size=188, scrambling=0, with_eit=True, with_tot=True, tot=None,
          repeat=40, padding=300, head_padding=0, genres=((7, 0, 15, 15),), eit_starts=None):
    """services: [(sid, has_video)]。PSIの一式をrepeat回、nullパケットを挟んで並べたTSのバイト列。"""
    eit_starts = eit_starts or {}
    block = []
    block += packets(PAT_PID, pat([(sid, PMT_PID + i) for i, (sid, _) in enumerate(services)]), size)
    for i, (sid, has_video) in enumerate(services):
        block += packets(PMT_PID + i, pmt(sid, VIDEO_PID, has_video), size)
    block += packets(SDT_PID, sdt([sid for sid, _ in services]), size)
    if with_tot:
        block += packets(TOT_PID, tot_section(tot or start + 600), size)
    block.append(video_packet(size, scrambling))
    if with_eit:
        for sid, _ in services:
            block += packets(EIT_PID, eit(sid, eit_starts.get(sid, start), genres=genres), size)
    out = [null_packet(size)] * head_padding
    for _ in range(repeat):
        out += block + [null_packet(size)] * padding
    return b"".join(out)


class TsInfoTests(unittest.TestCase):
    START = 1760106600  # 2025-10-10 23:30:00 JST

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def read(self, data):
        path = os.path.join(self.tmp.name, "a.ts")
        with open(path, "wb") as f:
            f.write(data)
        return tsinfo.read_ts_info(path)

    def test_時刻の変換(self):
        self.assertEqual(tsinfo.jst_to_unix(int.from_bytes(jst_fields(self.START), "big")), self.START)
        self.assertIsNone(tsinfo.jst_to_unix((1 << 40) - 1))
        self.assertIsNone(tsinfo.jst_to_unix(0))

    def test_188バイトのTSから見出しを読む(self):
        info = self.read(build([(1064, True)], self.START))
        self.assertEqual(info.service_ids, [1064])
        self.assertEqual(info.eit_start, self.START)
        self.assertEqual(info.start_of(1064), self.START)
        self.assertEqual(info.programs[0].eit_duration, 1800)
        self.assertEqual(info.programs[0].genres, [(7, 0, 15, 15)])
        self.assertEqual(info.tot, self.START + 600)

    def test_192バイトのm2tsから見出しを読む(self):
        info = self.read(build([(1064, True)], self.START, size=192))
        self.assertEqual(info.service_ids, [1064])
        self.assertEqual(info.eit_start, self.START)

    def test_映像のないサービスはServiceIdの一覧に入れない(self):
        info = self.read(build([(1064, True), (1065, False)], self.START))
        self.assertEqual(info.service_ids, [1064])
        self.assertEqual([p.sid for p in info.programs], [1064, 1065])

    def test_サービスごとの開始時刻と最後に読んだ開始時刻(self):
        later = self.START + 3600
        info = self.read(build([(1064, True), (1072, True)], self.START, eit_starts={1072: later}))
        self.assertEqual(info.start_of(1064), self.START)
        self.assertEqual(info.start_of(1072), later)
        self.assertEqual(info.eit_start, later)  # TsInfoのGetEITStartTimeと同じく最後のEIT

    def test_ジャンルなしのEIT(self):
        info = self.read(build([(1064, True)], self.START, genres=()))
        self.assertEqual(info.programs[0].genres, [])

    def test_すべてスクランブルされていてEITがなければエラー(self):
        with self.assertRaisesRegex(tsinfo.TsInfoError, "scrambled"):
            self.read(build([(1064, True)], self.START, scrambling=2, with_eit=False, with_tot=False))

    def test_映像がスクランブルされていてもEITとサービス情報があれば読める(self):
        info = self.read(build([(1064, True)], self.START, scrambling=2))
        self.assertEqual(info.start_of(1064), self.START)

    def test_EITがなくても映像のあるサービスが読めれば開始時刻なしで返す(self):
        info = self.read(build([(1064, True)], self.START, with_eit=False))
        self.assertEqual(info.service_ids, [1064])
        self.assertIsNone(info.eit_start)

    def test_情報のないファイルはエラー(self):
        with self.assertRaises(tsinfo.TsInfoError):
            self.read(os.urandom(100000))
        with self.assertRaises(tsinfo.TsInfoError):
            self.read(b"")

    def test_真ん中に情報がなければ先頭寄りから読み直す(self):
        # 情報は先頭の1割にだけある。残りはnullパケット。
        head = build([(1064, True)], self.START, repeat=10, padding=10)
        data = head + null_packet() * (len(head) // 188 * 15)
        info = self.read(data)
        self.assertEqual(info.start_of(1064), self.START)

    def test_途中で壊れたパケットがあっても同期を取り直す(self):
        data = build([(1064, True)], self.START)
        info = self.read(data[:188 * 4000] + b"\x00" * 100 + data[188 * 4000:])
        self.assertEqual(info.start_of(1064), self.START)

    def test_壊れたCRCのEITは使わない(self):
        data = bytearray(build([(1064, True)], self.START, repeat=3, padding=10))
        # EITのパケットのCRCを壊す。EITは各ブロックの最後のpsiパケット
        for i in range(0, len(data), 188):
            if ((data[i + 1] & 0x1F) << 8 | data[i + 2]) == EIT_PID:
                data[i + 30] ^= 0xFF
        info = self.read(bytes(data))
        self.assertIsNone(info.eit_start)

    def test_スクランブルのパケットのPSIも読み取る(self):
        # TsInfo.cppと同じく、PSIのPIDのスクランブル表示は見ない(映像のPIDだけ見る)
        info = self.read(build([(1064, True)], self.START, scrambling=3))
        self.assertEqual(info.service_ids, [1064])

    def test_結果をJSONの辞書にして戻せる(self):
        info = self.read(build([(1064, True)], self.START))
        again = tsinfo.TsInfo.from_dict(info.to_dict())
        self.assertEqual(again.to_dict(), info.to_dict())


def pes(pid, pts, stream_id, size=188):
    """PTS付きのPESを1パケットにする。"""
    pts &= (1 << 33) - 1
    body = bytes([
        0x20 | (((pts >> 30) & 7) << 1) | 1,
        (pts >> 22) & 0xFF,
        (((pts >> 15) & 0x7F) << 1) | 1,
        (pts >> 7) & 0xFF,
        ((pts & 0x7F) << 1) | 1,
    ])
    pes_data = b"\x00\x00\x01" + bytes([stream_id, 0x00, 0x00, 0x80, 0x80, 5]) + body
    packet = bytearray(188)
    packet[0:4] = bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10])
    packet[4:4 + len(pes_data)] = pes_data
    raw = bytes(packet)
    return (b"\x00" * 4 + raw) if size == 192 else raw


def pcr(pid, base, size=188):
    """PCRだけのパケット。baseは90kHz。"""
    base &= 0x1FFFFFFFF
    packet = bytearray(188)
    packet[0:4] = bytes([0x47, (pid >> 8) & 0x1F, pid & 0xFF, 0x20])
    packet[4] = 7
    packet[5] = 0x10
    packet[6:11] = bytes([(base >> 25) & 0xFF, (base >> 17) & 0xFF, (base >> 9) & 0xFF,
                          (base >> 1) & 0xFF, ((base & 1) << 7) | 0x7E])
    raw = bytes(packet)
    return (b"\x00" * 4 + raw) if size == 192 else raw


class TimelineTests(unittest.TestCase):
    START = 1760106600

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, data):
        path = os.path.join(self.tmp.name, "a.ts")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def file(self, *parts, size=188):
        head = b"".join(packets(PAT_PID, pat([(1064, PMT_PID)]), size))
        head += b"".join(packets(PMT_PID, pmt(1064, VIDEO_PID), size))
        return self.write(head + b"".join(parts))

    def test_TOTとPCRとPTSから先頭と末尾を求める(self):
        origin = 1_000_000
        path = self.file(pcr(VIDEO_PID, origin + 9000),
                         b"".join(packets(TOT_PID, tot_section(self.START))),
                         pes(VIDEO_PID, origin + 3000, 0xE0),  # 表示順では次のフレームが先
                         pes(VIDEO_PID, origin, 0xE0),
                         pes(0x0110, origin + 3600, 0xC0),
                         pes(VIDEO_PID, origin + 90000 * 5, 0xE0))
        info = tsinfo.read_timeline(path)
        self.assertEqual(info["sid"], 1064)
        self.assertEqual(info["audio_pid"], 0x0110)
        self.assertAlmostEqual(info["start"], self.START - 0.1, places=6)
        self.assertEqual(info["start_rounded"], self.START)
        self.assertAlmostEqual(info["duration"], 5.0, places=6)
        self.assertAlmostEqual(info["audio_shift"], 0.04, places=6)
        self.assertTrue(info["has_time"])

    def test_192バイトでも同じ時刻になる(self):
        origin = 500_000
        path = self.file(pcr(VIDEO_PID, origin, 192),
                         b"".join(packets(TOT_PID, tot_section(self.START), 192)),
                         pes(VIDEO_PID, origin, 0xE0, 192),
                         pes(0x0110, origin, 0xC0, 192),
                         pes(VIDEO_PID, origin + 90000, 0xE0, 192), size=192)
        info = tsinfo.read_timeline(path)
        self.assertAlmostEqual(info["start"], self.START, places=6)
        self.assertAlmostEqual(info["duration"], 1.0, places=6)
        self.assertAlmostEqual(info["audio_shift"], 0.0, places=6)

    def test_サービスが複数ならsidが要る(self):
        data = b"".join(packets(PAT_PID, pat([(1064, PMT_PID), (1072, PMT_PID + 1)])))
        data += b"".join(packets(PMT_PID, pmt(1064, VIDEO_PID)))
        data += b"".join(packets(PMT_PID + 1, pmt(1072, VIDEO_PID + 1)))
        path = self.write(data + pes(VIDEO_PID, 1000, 0xE0) + pes(VIDEO_PID, 1000 + 90000, 0xE0)
                          + pes(VIDEO_PID + 1, 1000, 0xE0) + pes(VIDEO_PID + 1, 1000 + 90000, 0xE0))
        with self.assertRaisesRegex(tsinfo.TsInfoError, "--sid"):
            tsinfo.read_timeline(path)
        info = tsinfo.read_timeline(path, 1072)
        self.assertEqual(info["sid"], 1072)
        self.assertEqual(info["video_pid"], VIDEO_PID + 1)
        self.assertFalse(info["has_time"])

    def test_開いたGOPの先頭のBピクチャは原点にしない(self):
        frames = [(1000, 3), (1000 + 12012, 1), (1000 + 6006, 3)]
        self.assertEqual(tsinfo.video_origin(frames, 0x02), 1000 + 6006)
        self.assertEqual(tsinfo.video_origin(frames, 0x1B), 1000)
        pes_b = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + b"\x00" * 5 + b"\x00\x00\x01\x00\x00\x18"
        # temporal_reference 0、coding_type 3(B)。16bitの上位10bitが番号、続く3bitが種類。
        self.assertEqual(tsinfo._mpeg2_picture_type(pes_b), 3)

    def test_PTSの差は33bitの折り返しを戻す(self):
        self.assertAlmostEqual(tsinfo.pts_difference(100, (1 << 33) - 9000), 9100.0, places=3)
        start, rounded, duration, has_time, shift = tsinfo._times_from(
            [((1 << 33) - 9000)], None, [(0, 100)], self.START, 0)
        self.assertTrue(has_time)
        self.assertAlmostEqual(start, self.START - 9100 / 90000, places=6)
        self.assertAlmostEqual(duration, 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
