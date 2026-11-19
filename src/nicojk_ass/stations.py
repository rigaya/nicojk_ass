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
