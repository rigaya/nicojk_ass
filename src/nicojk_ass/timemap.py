"""対応表での時刻の写しと、2つの局のコメントの統合。"""

import json
import math
import xml.etree.ElementTree as ET

from .comments import log


def read_timemap(path):
    """対応表(基準の開始, 基準の終了, 対象の開始, 対象の終了の4つ組の並び)をfloatで読む。"""
    with open(path, "r", encoding="utf-8-sig") as source:
        return [(float(a), float(b), float(c), float(d)) for a, b, c, d in json.load(source)]


def apply_timemap(xml_str, timemap):
    """基準の相対vposを半開区間で対象へ写し、対応のないコメントを捨てる。"""
    root = ET.fromstring(xml_str)
    removed = 0
    for chat in list(root.findall("chat")):
        if chat.get("vpos") is None:
            root.remove(chat)
            removed += 1
            continue
        position = float(chat.get("vpos")) / 100
        if not math.isfinite(position):
            raise ValueError("comment vpos is not a finite number")
        for reference_start, reference_end, target_start, target_end in timemap:
            if reference_start <= position < reference_end:
                mapped = target_start + ((position - reference_start) * (target_end - target_start)
                                         / (reference_end - reference_start))
                chat.set("vpos", str(round(mapped * 100)))
                break
        else:
            root.remove(chat)
            removed += 1
    if removed:
        log(f"dropped {removed} comments outside the time map")
    return ET.tostring(root, encoding="unicode")


def merge_comments(reference_xml, target_xml, mark_reference_color=None):
    """両局のコメントを時刻順に混ぜ、重複を残したまま1回の配置へ渡す。
    mark_reference_colorがあれば、基準の局のコメントにその色を足す。"""
    reference = ET.fromstring(reference_xml)
    target = ET.fromstring(target_xml)
    if mark_reference_color:
        # danmaku2assは後に書いた色を使うので、末尾に足せば元の色より優先される。
        for chat in reference.findall("chat"):
            chat.set("mail", (chat.get("mail", "") + " " + mark_reference_color).strip())
    chats = reference.findall("chat") + target.findall("chat")
    root = ET.Element("packet")
    for chat in sorted(chats, key=lambda item: int(item.get("vpos"))):
        root.append(chat)
    return ET.tostring(root, encoding="unicode")
