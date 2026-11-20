"""局の表(ServiceId→ニコニコ実況のチャンネル番号)。"""

import os

_TABLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ch_sid.txt")


def lookup_jk(sid, path=_TABLE_PATH):
    """ServiceIdに対応する(jk番号, 局名)を返す。表になければNone。
    表は「jk番号 networkId serviceId (予約) 局名」のタブ区切りで、#で始まる行は無視する。"""
    with open(path, "r", encoding="utf-8-sig") as table:
        for line in table:
            fields = line.rstrip("\r\n").split("\t")
            if len(fields) < 5:
                continue
            try:
                jk = int(fields[0], 0)
                table_sid = int(fields[2], 0)
            except ValueError:
                continue
            if table_sid == sid:
                return jk, fields[4]
    return None


_SYOBOI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "syoboi_ch.txt")


def read_syoboi_channels(path=_SYOBOI_PATH):
    """しょぼいカレンダーの局の表を読む。(ChID, ServiceId, 局名, 基準にできるか)のリストを返す。
    形式は「ChID<TAB>ServiceId<TAB>局名<TAB>基準」で、#で始まる行は注釈。
    北海道の地域別ServiceIdは同じChIDに対応するので、ChIDを一意なキーにしない。"""
    channels = []
    with open(path, "r", encoding="utf-8-sig") as table:
        for line in table:
            line = line.rstrip("\r\n")
            if not line.strip() or line.startswith("#"):
                continue
            fields = line.split("\t")
            channels.append((int(fields[0]), int(fields[1]), fields[2], fields[3] == "1"))
    return channels


def read_comment_services(path=_TABLE_PATH):
    """ServiceId→jk番号の辞書を返す(jk番号が正のものだけ)。"""
    services = {}
    with open(path, "r", encoding="utf-8-sig") as table:
        for line in table:
            if not line.strip() or line.startswith("#"):
                continue
            fields = line.split()
            try:
                jk, sid = int(fields[0], 0), int(fields[2], 0)
            except (ValueError, IndexError):
                continue
            if jk > 0:
                services.setdefault(sid, jk)
    return services
