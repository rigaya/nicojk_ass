"""対応表での時刻の写しと、複数の局のコメントの統合。"""

import json
import math
import xml.etree.ElementTree as ET

from .comments import log


def read_timemap(path):
    """対応表(基準の開始, 基準の終了, 対象の開始, 対象の終了の4つ組の並び)をfloatで読む。"""
    with open(path, "r", encoding="utf-8-sig") as source:
        return [(float(a), float(b), float(c), float(d)) for a, b, c, d in json.load(source)]


def compose_timemaps(first, second):
    """中間の時刻が重なる部分だけを残して、2つの区分線形の対応表をつなぐ。"""
    result = []
    for source_start, source_end, middle_start, middle_end in first:
        if middle_end <= middle_start:
            continue  # 長さのない区間は写す先がない(0で割らない)
        source_scale = (source_end - source_start) / (middle_end - middle_start)
        for ref_start, ref_end, target_start, target_end in second:
            begin, end = max(middle_start, ref_start), min(middle_end, ref_end)
            if begin >= end or ref_end <= ref_start:
                continue
            target_scale = (target_end - target_start) / (ref_end - ref_start)
            result.append((source_start + (begin - middle_start) * source_scale,
                           source_start + (end - middle_start) * source_scale,
                           target_start + (begin - ref_start) * target_scale,
                           target_start + (end - ref_start) * target_scale))
    return result


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


def merge_comments(reference_xml, target_xml, mark_reference_color=None, *additional_references):
    """複数局のコメントを時刻順に混ぜ、重複を残したまま1回の配置へ渡す。"""
    chats = []
    for xml in (reference_xml, *additional_references):
        reference_chats = ET.fromstring(xml).findall("chat")
        if mark_reference_color:
            # danmaku2assは後に書いた色を使うので、末尾に足せば元の色より優先される。
            for chat in reference_chats:
                chat.set("mail", (chat.get("mail", "") + " " + mark_reference_color).strip())
        chats.extend(reference_chats)
    if target_xml is not None:
        chats.extend(ET.fromstring(target_xml).findall("chat"))
    root = ET.Element("packet")
    for chat in sorted(chats, key=lambda item: int(item.get("vpos"))):
        root.append(chat)
    return ET.tostring(root, encoding="unicode")
