"""TSの見出しの読み込み。TsInfo.cppと同じ読み方を、標準ライブラリだけで行う。

ファイルの真ん中から4MBずつ最大100MB読み、取れなければ先頭寄り(size/30)から読み直す。
PAT・PMT・SDT・EIT(現在の番組のsection 0)・TDT/TOTを見る。ARIBの文字列(番組名)は読まない。
188バイトと192バイト(m2ts)のパケットに対応する。
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
