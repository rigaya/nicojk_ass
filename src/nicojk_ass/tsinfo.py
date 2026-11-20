"""TSの見出しの読み込み。TsInfo.cppと同じ読み方を、標準ライブラリだけで行う。

ファイルの真ん中から4MBずつ最大100MB読み、取れなければ先頭寄り(size/30)から読み直す。
PAT・PMT・SDT・EIT(現在の番組のsection 0)・TDT/TOTを見る。ARIBの文字列(番組名)は読まない。
188バイトと192バイト(m2ts)のパケットに対応する。
外部向けの--tsでは、先頭付近のTOT・PCR・映像のPTSから先頭の時刻を、末尾付近のPTSから末尾を求める。
"""

import os

BUFSIZE = 4 * 1024 * 1024
MAX_BYTES = 100 * 1024 * 1024
SYNC_CHECK_PACKETS = 8

_VIDEO_STREAM_TYPES = (0x02, 0x1B, 0x24)
_JST_OFFSET = 9 * 3600

_CRC_TABLE = []
for _i in range(256):
    _c = _i << 24
    for _ in range(8):
        _c = ((_c << 1) ^ 0x04C11DB7) if _c & 0x80000000 else (_c << 1)
    _CRC_TABLE.append(_c & 0xFFFFFFFF)


class TsInfoError(Exception):
    pass


def _crc_ok(data):
    """MPEG-2のCRC32。セクション全体(末尾のCRCを含む)を通した結果が0なら正しい。"""
    crc = 0xFFFFFFFF
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[(crc >> 24) ^ byte]
    return crc == 0


def jst_to_unix(value):
    """EIT・TOTの40bitの時刻(MJDの16bitとBCDの時分秒)をUNIX秒にする。未定義(全ビット1)と0はNone。"""
    if value == 0 or value == (1 << 40) - 1:
        return None
    mjd = value >> 24
    if mjd < 51544:  # TsInfo.cppのMJDtoYMDと同じく、2000年より前なら上位1ビットを足す
        mjd += 65536
    bcd = value & 0xFFFFFF
    hour = ((bcd >> 20) & 0xF) * 10 + ((bcd >> 16) & 0xF)
    minute = ((bcd >> 12) & 0xF) * 10 + ((bcd >> 8) & 0xF)
    second = ((bcd >> 4) & 0xF) * 10 + (bcd & 0xF)
    return (mjd - 40587) * 86400 + hour * 3600 + minute * 60 + second - _JST_OFFSET


class Program:
    """PATにあるサービス1つ分。genresは(level1, level2, user1, user2)の並び。"""

    def __init__(self, sid, has_video=False, video_pid=-1, eit_start=None, eit_duration=None, genres=None):
        self.sid = sid
        self.has_video = has_video
        self.video_pid = video_pid
        self.eit_start = eit_start
        self.eit_duration = eit_duration
        self.genres = genres or []
        # 以下は読み込み中の状態
        self.pmt_pid = -1
        self.video_ok = False
        self.scrambled = False
        self.event_ok = False

    def to_dict(self):
        return {"sid": self.sid, "has_video": self.has_video, "video_pid": self.video_pid,
                "eit_start": self.eit_start, "eit_duration": self.eit_duration,
                "genres": [list(g) for g in self.genres]}

    @classmethod
    def from_dict(cls, data):
        return cls(data["sid"], data["has_video"], data["video_pid"], data["eit_start"],
                   data["eit_duration"], [tuple(g) for g in data["genres"]])


class TsInfo:
    """読み込んだ結果。

    programs: PATのサービス(PATの順)。
    eit_start: 最後に読んだ放送中の番組の開始時刻(TsInfoのGetEITStartTimeと同じ値。UNIX秒)。
    tot: TDT/TOTの時刻(UNIX秒)。
    """

    def __init__(self, programs=None, eit_start=None, tot=None):
        self.programs = programs or []
        self.eit_start = eit_start
        self.tot = tot

    @property
    def service_ids(self):
        """映像のあるサービスのServiceId。"""
        return [p.sid for p in self.programs if p.has_video]

    def start_of(self, sid):
        """サービスの放送中の番組の開始時刻。そのサービスのEITがなければ、最後に読んだEITの値。"""
        for program in self.programs:
            if program.sid == sid and program.eit_start is not None:
                return program.eit_start
        return self.eit_start

    def to_dict(self):
        return {"programs": [p.to_dict() for p in self.programs],
                "eit_start": self.eit_start, "tot": self.tot}

    @classmethod
    def from_dict(cls, data):
        return cls([Program.from_dict(p) for p in data["programs"]], data["eit_start"], data["tot"])


class _SectionBuffer:
    """PIDごとのPSIセクションの組み立て(PsiParser相当)。"""

    def __init__(self):
        self.data = bytearray()

    def feed(self, pusi, payload):
        """完成したセクションをyieldする。"""
        if pusi:
            if not payload:
                return
            start = payload[0] + 1  # pointer field
            if start >= len(payload):
                return
            if start > 1:
                self.data += payload[1:start]
                yield from self._take()
            self.data = bytearray(payload[start:])
        else:
            self.data += payload
        yield from self._take()

    def _take(self):
        if len(self.data) >= 3:
            length = (((self.data[1] & 0x0F) << 8) | self.data[2]) + 3
            if len(self.data) >= length:
                section = bytes(self.data[:length])
                del self.data[:length]
                yield section


class _Parser:
    def __init__(self):
        self.pat_ok = False
        self.service_ok = False
        self.time_ok = False
        self.programs = []          # 最後のPATのサービス
        self.by_sid = {}
        self.pmt_pids = {}          # PMTのPID -> ServiceId
        self.start_raw = 0          # 最後に読んだEITの開始時刻(40bit)
        self.time = None
        self.buffers = {0x0000: _SectionBuffer(), 0x0011: _SectionBuffer(), 0x0012: _SectionBuffer(),
                        0x0026: _SectionBuffer(), 0x0027: _SectionBuffer(), 0x0014: _SectionBuffer()}
        self.video_pids = {}        # 映像PID -> そのPIDを使うサービス
        self.video_seen = {}        # 映像PID -> 暗号化されていないPES先頭が来たか
        self.video_scrambled = set()

    # ---- 状態の判定 ----
    def has_service_info(self):
        return self.service_ok and self.time_ok

    def is_program_ok(self):
        if not self.pat_ok:
            return False
        return any(p.has_video and p.video_ok for p in self.programs)

    def is_scrambled(self):
        ng = any(p.has_video and not p.video_ok and p.scrambled for p in self.programs)
        ok = any(p.has_video and p.video_ok for p in self.programs)
        return not ok and ng

    def is_ok(self):
        if not (self.pat_ok and self.service_ok and self.time_ok):
            return False
        for p in self.programs:
            if not p.video_ok or (p.has_video and not p.event_ok):
                return False
        return True

    # ---- パケット ----
    def packet(self, data, pos):
        """TSパケット(data[pos:pos+188])を1つ処理する。"""
        b1, b2, b3 = data[pos + 1], data[pos + 2], data[pos + 3]
        pid = ((b1 & 0x1F) << 8) | b2
        if 2 <= pid <= 0x0F:
            return
        scrambling = (b3 >> 6) & 3
        afc = (b3 >> 4) & 3
        if scrambling == 1 or afc == 0:
            return
        has_payload = bool(afc & 1)
        offset = 4
        if has_payload:
            if afc & 2:
                offset = 5 + data[pos + 4]
            if offset >= 188:
                return
        buf = self.buffers.get(pid)
        if buf is None:
            if pid in self.pmt_pids:
                buf = self.buffers[pid] = _SectionBuffer()
            elif pid in self.video_pids:
                self._video_packet(pid, scrambling, (b1 & 0x40) != 0)
                return
            else:
                return
        if not has_payload:
            return
        pusi = (b1 & 0x40) != 0
        payload = data[pos + offset:pos + 188]
        for section in buf.feed(pusi, payload):
            self._section(pid, section)

    def _video_packet(self, pid, scrambling, pusi):
        if scrambling:
            self.video_scrambled.add(pid)
            for p in self.programs:
                if p.video_pid == pid:
                    p.scrambled = True
            return
        if pusi and not self.video_seen.get(pid):
            # TsInfo.cppは映像のフォーマットを読めたらprogramOKにする。ここでは暗号化されていない
            # PESの先頭が来たことで代える。
            self.video_seen[pid] = True
            for p in self.programs:
                if p.video_pid == pid:
                    p.video_ok = True

    # ---- セクション ----
    def _section(self, pid, section):
        table_id = section[0]
        ssi = section[1] & 0x80
        if table_id == 0x00 and pid == 0x0000:
            if ssi and _crc_ok(section):
                self._pat(section)
        elif table_id == 0x02:
            if ssi and _crc_ok(section):
                self._pmt(pid, section)
        elif table_id == 0x42 and pid == 0x0011:
            if ssi and _crc_ok(section):
                self.service_ok = True
        elif table_id == 0x4E and pid in (0x0012, 0x0026, 0x0027):
            if ssi and len(section) > 8 and section[6] == 0 and section[5] & 1 and _crc_ok(section):
                self._eit(section)
        elif table_id == 0x70 and pid == 0x0014:
            if len(section) >= 8:
                self._time(section)
        elif table_id == 0x73 and pid == 0x0014:
            if len(section) >= 8 and not ssi and _crc_ok(section):
                self._time(section)

    def _time(self, section):
        self.time = int.from_bytes(section[3:8], "big")
        self.time_ok = True

    def _pat(self, section):
        payload = section[8:-4]
        if len(payload) % 4:
            return
        order = []
        for i in range(0, len(payload), 4):
            number = (payload[i] << 8) | payload[i + 1]
            if number == 0:
                continue
            pid = ((payload[i + 2] & 0x1F) << 8) | payload[i + 3]
            program = self.by_sid.get(number)
            if program is None:
                program = self.by_sid[number] = Program(number)
            program.pmt_pid = pid
            self.pmt_pids[pid] = number
            order.append(program)
        self.programs = order
        self.pat_ok = True

    def _pmt(self, pid, section):
        if not section[5] & 1:
            return
        sid = (section[3] << 8) | section[4]
        program = self.by_sid.get(sid)
        if program is None or program not in self.programs or program.pmt_pid != pid:
            return
        payload = section[8:-4]
        if len(payload) < 4:
            return
        pos = 4 + (((payload[2] & 0x0F) << 8) | payload[3])
        program.has_video = False
        while pos + 5 <= len(payload):
            stream_type = payload[pos]
            es_pid = ((payload[pos + 1] & 0x1F) << 8) | payload[pos + 2]
            es_len = ((payload[pos + 3] & 0x0F) << 8) | payload[pos + 4]
            pos += 5 + es_len
            if stream_type in _VIDEO_STREAM_TYPES:
                program.has_video = True
                program.video_pid = es_pid
                self.video_pids[es_pid] = program
                # 同じ映像PIDの別サービスが先に読めていれば、その結果を引き継ぐ
                if self.video_seen.get(es_pid):
                    program.video_ok = True
                break
        if not program.has_video:
            program.video_ok = True

    def _eit(self, section):
        sid = (section[3] << 8) | section[4]
        program = self.by_sid.get(sid)
        if program is None or program not in self.programs:
            return
        payload = section[8:-4]
        # EITの本体: tsid(2) onid(2) segment_last(1) last_table_id(1)、続いて番組
        if len(payload) < 6:
            return
        pos = 6
        first = None
        while pos < len(payload):
            if len(payload) - pos < 12:
                return
            length = ((payload[pos + 10] & 0x0F) << 8) | payload[pos + 11]
            if pos + 12 + length > len(payload):
                return
            if first is None:
                first = (pos, length)
            pos += 12 + length
        if first is None:
            return
        pos, length = first
        start_raw = int.from_bytes(payload[pos + 2:pos + 7], "big")
        duration_bcd = int.from_bytes(payload[pos + 7:pos + 10], "big")
        genres = []
        desc = payload[pos + 12:pos + 12 + length]
        i = 0
        while i + 2 <= len(desc):
            tag, size = desc[i], desc[i + 1]
            body = desc[i + 2:i + 2 + size]
            if tag == 0x54 and len(body) == size and size % 2 == 0:
                for j in range(0, size, 2):
                    genres.append((body[j] >> 4, body[j] & 0x0F, body[j + 1] >> 4, body[j + 1] & 0x0F))
            i += 2 + size
        self.start_raw = start_raw
        start = jst_to_unix(start_raw)
        duration = ((duration_bcd >> 20) & 0xF) * 36000 + ((duration_bcd >> 16) & 0xF) * 3600 + \
            ((duration_bcd >> 12) & 0xF) * 600 + ((duration_bcd >> 8) & 0xF) * 60 + \
            ((duration_bcd >> 4) & 0xF) * 10 + (duration_bcd & 0xF)
        # 同じ映像PIDのサービスは、番組の情報をすべて上書きする(TsInfo.cppと同じ)
        for p in self.programs:
            if p.video_pid == program.video_pid:
                p.genres = genres
                p.event_ok = True
        program.eit_start = start
        program.eit_duration = duration


def _packet_size(data, begin):
    """data[begin:]から、同期バイトが8パケット続く最初の位置とパケットの大きさ(188か192)を探す。"""
    limit = len(data) - SYNC_CHECK_PACKETS * 192
    pos = begin
    while pos < limit:
        pos = data.find(b"\x47", pos, limit)
        if pos < 0:
            return None
        for size in (188, 192):
            if all(data[pos + size * k] == 0x47 for k in range(SYNC_CHECK_PACKETS)):
                return pos, size
        pos += 1
    return None


def _scan(parser, file):
    """現在位置から読み、0(取得できた)、1(情報なし)、2(スクランブル)、3(EITなしでサービスのみ)を返す。"""
    total = 0
    carry = b""
    size = None
    while True:
        chunk = file.read(BUFSIZE)
        data = carry + chunk if carry else chunk
        pos = 0
        if size is None and len(data) >= SYNC_CHECK_PACKETS * 192:
            found = _packet_size(data, 0)
            if found:
                pos, size = found
        if size is not None:
            end = len(data) - size + 1
            while pos < end:
                if data[pos] != 0x47:
                    # 同期が外れた。次の同期を探す
                    found = _packet_size(data, pos + 1)
                    if not found:
                        # 次の読み込みで続きから探す
                        pos = max(pos + 1, len(data) - SYNC_CHECK_PACKETS * 192)
                        break
                    pos, size = found
                    continue
                parser.packet(data, pos)
                pos += size
            carry = data[pos:] if pos < len(data) else b""
        else:
            carry = data[-SYNC_CHECK_PACKETS * 192:] if len(data) > SYNC_CHECK_PACKETS * 192 else data
        if parser.is_ok():
            return 0
        if parser.has_service_info() and parser.start_raw != 0:
            return 0
        total += len(chunk)
        if len(chunk) < BUFSIZE or total >= MAX_BYTES:
            break
    if parser.is_program_ok():
        return 3 if parser.start_raw == 0 else 0
    if parser.is_scrambled():
        return 2
    return 1


def read_ts_info(path):
    """TSの見出しを読んでTsInfoを返す。読めなければTsInfoErrorを送出する。"""
    parser = _Parser()
    with open(path, "rb") as file:
        size = os.fstat(file.fileno()).st_size
        file.seek(size // 2)
        ret = _scan(parser, file)
        if ret != 0:
            scrambled = ret == 2
            file.seek(size // 30)
            ret = _scan(parser, file)
            if ret not in (0, 3):
                if scrambled:
                    raise TsInfoError("すべてのプログラムがスクランブルされています")
                raise TsInfoError("TSファイルに情報がありません")
    return TsInfo(list(parser.programs), jst_to_unix(parser.start_raw),
                  jst_to_unix(parser.time) if parser.time is not None else None)


# ------------------------------------------------------------------ #
# 先頭と末尾の時刻(外部向けの--ts)
# ------------------------------------------------------------------ #

HEAD_BYTES = 8 * 1024 * 1024
HEAD_MAX_BYTES = 64 * 1024 * 1024
TAIL_BYTES = 8 * 1024 * 1024
_PTS_MOD = 1 << 33
_AUDIO_STREAM_TYPE = 0x0F


def pts_difference(pts, origin):
    """33bitのPTSの差(秒ではなく90kHzの目盛り)。std::remainderと同じ半分の範囲に収める。"""
    import math
    return math.remainder(float(pts) - float(origin), float(_PTS_MOD))


def _int32(value):
    """C++のint32_tへの変換。下位32bitを符号付きにする。"""
    value = int(value) & 0xFFFFFFFF
    return value - 0x100000000 if value >= 0x80000000 else value


def _c_round(value):
    """std::round(半分は0から遠い側)。"""
    import math
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def _pts_of(payload):
    """PESの先頭からPTSを読む。PTSがなければNone。"""
    if len(payload) < 14 or payload[0:3] != b"\x00\x00\x01":
        return None
    if not payload[7] & 0x80 or payload[8] < 5:
        return None
    b = payload[9:14]
    return ((((b[0] >> 1) & 7) << 30) | (b[1] << 22) | ((b[2] & 0xFE) << 14) | (b[3] << 7) | (b[4] >> 1))


def _pcr_base(packet):
    """適応フィールドのPCRの90kHz部分。なければNone。"""
    afc = (packet[3] >> 4) & 3
    if not afc & 2:
        return None
    length = packet[4]
    if length < 7 or not packet[5] & 0x10:
        return None
    b = packet[6:12]
    return ((b[0] << 25) | (b[1] << 17) | (b[2] << 9) | (b[3] << 1) | (b[4] >> 7)) & 0x1FFFFFFFF


def _payload(packet):
    """(PESの先頭か, ペイロード)。適応フィールドだけならNone。"""
    afc = (packet[3] >> 4) & 3
    if not afc & 1:
        return False, b""
    offset = 4
    if afc & 2:
        offset = 5 + packet[4]
    if offset >= 188:
        return False, b""
    return bool(packet[1] & 0x40), packet[offset:]


def _find_sync(data, begin):
    """data[begin:]で、同期バイトが続く最初の位置とパケットの大きさ(188か192)を返す。

    見出しの走査と同じく8パケット分を見る。データがそれより短いときは、収まる数(最低2)で見る。
    """
    if len(data) - begin < 188 * 2:
        return None
    checks = SYNC_CHECK_PACKETS
    while checks >= 2 and begin + checks * 192 > len(data):
        checks -= 1
    limit = len(data) - (checks - 1) * 192
    pos = begin
    while pos < limit:
        pos = data.find(b"\x47", pos, limit)
        if pos < 0:
            return None
        for size in (188, 192):
            if pos + (checks - 1) * size + 188 <= len(data) and all(
                    data[pos + size * k] == 0x47 for k in range(checks)):
                return pos, size
        pos += 1
    return None


def _iter_packets(data):
    """同期の取れた188バイトのパケットを(先頭からの番号, パケット)で返す。"""
    found = _find_sync(data, 0)
    if not found:
        return
    pos, size = found
    index = 0
    while pos + 188 <= len(data):
        if data[pos] != 0x47:
            found = _find_sync(data, pos + 1)
            if not found:
                return
            pos, size = found
            continue
        yield index, data[pos:pos + 188]
        index += 1
        pos += size


class _Timeline:
    """先頭付近のPAT・PMT・TOT・PCR・映像と音声のPTS。"""

    def __init__(self):
        self.pmt_pid = {}       # ServiceId -> PMTのPID
        self.video_pid = {}
        self.video_type = {}
        self.audio_pid = {}
        self.pcr_pid = {}
        self.buffers = {0x0000: _SectionBuffer(), 0x0014: _SectionBuffer()}
        self.tot = None
        self.tot_index = None
        self.pcr = []           # (パケット番号, PCRの90kHz)

    def feed_psi(self, index, packet):
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        if pid in self.pmt_pid.values() and pid not in self.buffers:
            self.buffers[pid] = _SectionBuffer()
        buf = self.buffers.get(pid)
        if buf is None:
            return
        pusi, payload = _payload(packet)
        if not payload:
            return
        for section in buf.feed(pusi, payload):
            self._section(pid, index, section)

    def _section(self, pid, index, section):
        table_id = section[0]
        ssi = section[1] & 0x80
        if table_id == 0x00 and pid == 0x0000 and ssi and _crc_ok(section):
            self._pat(section)
        elif table_id == 0x02 and ssi and _crc_ok(section):
            self._pmt(pid, section)
        elif table_id == 0x73 and pid == 0x0014 and len(section) >= 8 and not ssi and _crc_ok(section):
            if self.tot is None:
                self.tot = jst_to_unix(int.from_bytes(section[3:8], "big"))
                self.tot_index = index
        elif table_id == 0x70 and pid == 0x0014 and len(section) >= 8 and self.tot is None:
            self.tot = jst_to_unix(int.from_bytes(section[3:8], "big"))
            self.tot_index = index

    def _pat(self, section):
        payload = section[8:-4]
        if len(payload) % 4:
            return
        for i in range(0, len(payload), 4):
            number = (payload[i] << 8) | payload[i + 1]
            if number == 0:
                continue
            self.pmt_pid[number] = ((payload[i + 2] & 0x1F) << 8) | payload[i + 3]

    def _pmt(self, pid, section):
        if not section[5] & 1:
            return
        sid = (section[3] << 8) | section[4]
        if self.pmt_pid.get(sid) != pid:
            return
        payload = section[8:-4]
        if len(payload) < 4:
            return
        self.pcr_pid[sid] = ((payload[0] & 0x1F) << 8) | payload[1]
        pos = 4 + (((payload[2] & 0x0F) << 8) | payload[3])
        video = audio = None
        while pos + 5 <= len(payload):
            stream_type = payload[pos]
            es_pid = ((payload[pos + 1] & 0x1F) << 8) | payload[pos + 2]
            es_len = ((payload[pos + 3] & 0x0F) << 8) | payload[pos + 4]
            pos += 5 + es_len
            if video is None and stream_type in _VIDEO_STREAM_TYPES:
                video = (es_pid, stream_type)
            elif audio is None and stream_type == _AUDIO_STREAM_TYPE:
                audio = es_pid
        if video is not None:
            self.video_pid[sid] = video[0]
            self.video_type[sid] = video[1]
        if audio is not None:
            self.audio_pid[sid] = audio

    def video_services(self):
        return [sid for sid in self.pmt_pid if sid in self.video_pid]


def _choose_service(timeline, sid):
    services = timeline.video_services()
    if sid is not None:
        if sid not in timeline.video_pid:
            known = ", ".join(str(item) for item in services) or "なし"
            raise TsInfoError(f"ServiceId {sid} の映像がありません(ファイルにある映像: {known})")
        return sid
    if len(services) == 1:
        return services[0]
    if not services:
        raise TsInfoError("映像のあるサービスがありません")
    raise TsInfoError("サービスが複数あります。--sidで選んでください: " + ", ".join(str(item) for item in services))


def _mpeg2_picture_type(payload):
    """PESから最初のMPEG-2ピクチャの種類(1=I, 2=P, 3=B)を返す。見つからなければNone。"""
    if len(payload) > 9 and payload[0:3] == b"\x00\x00\x01":
        payload = payload[9 + payload[8]:]
    start = payload.find(b"\x00\x00\x01\x00")
    if start < 0 or start + 6 > len(payload):
        return None
    word = (payload[start + 4] << 8) | payload[start + 5]
    kind = (word >> 3) & 7
    return kind if kind in (1, 2, 3) else None


def video_origin(frames, stream_type):
    """映像の先頭のPTS。

    framesはファイル順の(PTS, ピクチャの種類)。MPEG-2では、最初のIまたはPより前の
    Bピクチャ(開いたGOPの先頭で表示されないもの)を除いたあとの最小のPTSを使う。
    Amatsukazeがデコードできない先頭フレームを原点にしないため。
    """
    if not frames:
        return None
    if stream_type == 0x02:
        for index, (_, kind) in enumerate(frames):
            if kind in (1, 2):
                frames = frames[index:]
                break
    return min(pts for pts, _ in frames)


def _media_pts(data, video_pid, audio_pid, pcr_pid, stream_type=None):
    """映像の(PTS, 種類)の並び、最初の音声のPTS、PCRの(パケット番号, 値)の並び。

    MPEG-2のときだけ、先頭の数十枚についてピクチャの種類を読む。PESが複数パケットに
    分かれていても、ピクチャヘッダは先頭パケットに入る。
    """
    video, audio, pcr = [], None, []
    classify = stream_type == 0x02
    current = None

    def finish():
        nonlocal current
        if current is None or current["pts"] is None:
            current = None
            return
        kind = _mpeg2_picture_type(current["data"]) if classify and len(video) < 120 else None
        video.append((current["pts"], kind))
        current = None

    for index, packet in _iter_packets(data):
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        if pid == pcr_pid:
            base = _pcr_base(packet)
            if base is not None:
                pcr.append((index, base))
        if (packet[3] >> 6) & 3:
            continue
        pusi, payload = _payload(packet)
        if pid == video_pid:
            if pusi:
                finish()
                pts = _pts_of(payload)
                if classify and len(video) < 120:
                    current = {"pts": pts, "data": bytearray(payload)}
                elif pts is not None:
                    video.append((pts, None))
            elif current is not None and len(current["data"]) < 8192:
                current["data"] += payload
        elif pusi and pid == audio_pid and audio is None:
            audio = _pts_of(payload)
    finish()
    return video, audio, pcr


def _pcr_at(samples, index):
    """TOTのパケット位置のPCRを、前後のPCRから直線で補間する。"""
    if not samples:
        return None
    before = after = None
    for sample_index, base in samples:
        if sample_index <= index:
            before = (sample_index, base)
        elif after is None:
            after = (sample_index, base)
            break
    if before and after and after[0] != before[0]:
        span = pts_difference(after[1], before[1])
        return before[1] + span * (index - before[0]) / (after[0] - before[0])
    return (before or after)[1]


def _times_from(video_pts, audio_pts, pcr, tot, tot_index, origin=None):
    """映像の先頭のPTSを原点にした時刻。TOTとPCRがなければhas_timeはFalseでstartは0。"""
    if origin is None:
        origin = min(video_pts)
    end_pts = max(video_pts, key=lambda pts: pts_difference(pts, origin))
    duration = pts_difference(end_pts, origin) / 90000.0
    audio_shift = None if audio_pts is None else pts_difference(audio_pts, origin) / 90000.0
    if tot is None or tot_index is None:
        return 0.0, 0, duration, False, audio_shift
    pcr_value = _pcr_at(pcr, tot_index)
    if pcr_value is None:
        return 0.0, 0, duration, False, audio_shift
    # StreamReformInfoのfirstFrameTime_と同じく、TOTから映像の先頭へ戻す。丸めない値を正とする。
    difference = _int32(pcr_value - origin) / 90000.0
    start = tot - difference
    return start, tot - _c_round(difference), duration, True, audio_shift


def _parse_head(data, sid):
    """先頭のバイト列から、選んだサービスと、映像・音声のPTS、PCRを返す。"""
    timeline = _Timeline()
    for index, packet in _iter_packets(data):
        timeline.feed_psi(index, packet)
    chosen = _choose_service(timeline, sid)
    video_pid = timeline.video_pid[chosen]
    audio_pid = timeline.audio_pid.get(chosen)
    pcr_pid = timeline.pcr_pid.get(chosen, video_pid)
    video, audio, pcr = _media_pts(data, video_pid, audio_pid, pcr_pid, timeline.video_type.get(chosen))
    ready = bool(video) and audio is not None and timeline.tot is not None and bool(pcr)
    return timeline, chosen, video_pid, audio_pid, video, audio, pcr, ready


def read_timeline(path, sid=None):
    """対象の録画の先頭と末尾の時刻を返す。

    先頭付近の最初のTOTと、その位置のPCR、映像のPTSの最小から先頭を求める。
    末尾は、末尾付近の映像のPTSから求める。AmatsukazeのfirstFrameTimeExactと同じ戻し方。
    戻り値はsid, start, start_rounded, end, duration, has_time, video_pid, audio_pid, audio_shift。
    audio_shiftは、音声の先頭が映像の先頭より何秒遅いか(負なら音声が前)。
    """
    with open(path, "rb") as file:
        size = os.fstat(file.fileno()).st_size
        head = file.read(min(size, HEAD_BYTES))
        try:
            timeline, chosen, video_pid, audio_pid, video, audio, pcr, ready = _parse_head(head, sid)
        except TsInfoError:
            timeline = None
            ready = False
        if not ready and size > len(head):
            head += file.read(min(size - len(head), HEAD_MAX_BYTES - len(head)))
            timeline, chosen, video_pid, audio_pid, video, audio, pcr, ready = _parse_head(head, sid)
        elif timeline is None:
            _parse_head(head, sid)
        if not video:
            raise TsInfoError("映像の先頭が見つかりません")
        origin = video_origin(video, timeline.video_type.get(chosen))
        if size > len(head):
            file.seek(max(0, size - TAIL_BYTES))
            tail_video, _, _ = _media_pts(file.read(TAIL_BYTES), video_pid, None, -1)
            if not tail_video:
                raise TsInfoError("映像の末尾が見つかりません")
            # 先頭側は原点だけを残し、末尾はファイルの終わりのPTSにする。
            video = [(origin, None)] + tail_video
        video_pts = [pts for pts, _ in video]
    start, start_rounded, duration, has_time, audio_shift = _times_from(
        video_pts, audio, pcr, timeline.tot, timeline.tot_index, origin)
    if duration <= 0:
        raise TsInfoError("映像の長さを求められません")
    return {"sid": chosen, "start": start, "start_rounded": start_rounded,
            "end": start + duration, "duration": duration, "has_time": has_time,
            "video_pid": video_pid, "audio_pid": audio_pid, "audio_shift": audio_shift}
