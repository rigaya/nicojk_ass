"""danmaku2assの呼び出しと、NicoConvAssに合わせるASSの後処理。"""

import os
import re
import subprocess
import sys

from .comments import EXIT_ERROR, NicoJKExit, log

DEFAULT_FONT = "MS PGothic"
DEFAULT_FONTSIZE_BASE = 37
DEFAULT_DURATION = 6.0
DEFAULT_OPACITY = 1.0

DANMAKU2ASS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_vendor", "danmaku2ass.py")


def default_fontsize(height):
    return max(16, int(DEFAULT_FONTSIZE_BASE * height / 720))


def run_danmaku2ass(xml_path, output_path, width, height,
                    font, fontsize, opacity, duration):
    """danmaku2ass.py をサブプロセスで実行する。"""
    if not os.path.isfile(DANMAKU2ASS_PATH):
        log(f"danmaku2ass.py が見つかりません: {DANMAKU2ASS_PATH}")
        raise NicoJKExit(EXIT_ERROR)
    cmd = [
        sys.executable, DANMAKU2ASS_PATH,
        "-f", "Niconico",
        "-s", f"{width}x{height}",
        "-fn", font,
        "-fs", str(fontsize),
        "-a", str(opacity),
        "-dm", str(duration),
        "-ds", str(duration),
        "-o", output_path,
        xml_path,
    ]
    log(f"danmaku2ass: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout, end="", file=sys.stderr)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode != 0:
        log(f"danmaku2ass が失敗しました (exit={result.returncode})")
        raise NicoJKExit(EXIT_ERROR)


# ------------------------------------------------------------------ #
# ASS後処理（readASS() 互換化）
# ------------------------------------------------------------------ #

# NicoConvAssスタイル色定義（ASS形式: &HAABBGGRR）
_NICOCONVASS_COLORS = [
    ("white",  "&H00ffffff"),
    ("red",    "&H000000ff"),
    ("pink",   "&H009314ff"),
    ("orange", "&H00008cff"),
    ("yellow", "&H0000ffff"),
    ("green",  "&H00008000"),
    ("cyan",   "&H00ffff00"),
    ("blue",   "&H00ff0000"),
    ("purple", "&H00800080"),
    ("black",  "&H00000000"),
]

_MOVE_RE = re.compile(
    r"\\move\(\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\)")
_POS_RE = re.compile(r"\\pos\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)")
_COLOR_TAG_RE = re.compile(r"\\c&H([0-9A-Fa-f]{6})&")
_3COLOR_TAG_RE = re.compile(r"\\3c&H[0-9A-Fa-f]+&")


def _convert_color_bt709(rgb):
    """BT.601→BT.709色空間変換（danmaku2assのConvertColor相当）。BGR hex文字列を返す。"""
    if rgb == 0x000000:
        return "000000"
    if rgb == 0xffffff:
        return "FFFFFF"
    R = (rgb >> 16) & 0xff
    G = (rgb >> 8) & 0xff
    B = rgb & 0xff
    clip = lambda x: max(0, min(255, round(x)))
    return "%02X%02X%02X" % (
        clip(R * 0.00956384088080656 + G * 0.03217254540203729
             + B * 0.95826361371715607),
        clip(R * -0.10493933142075390 + G * 1.17231478191855154
             + B * -0.06737545049779757),
        clip(R * 0.91348912373987645 + G * 0.07858536372532510
             + B * 0.00792551253479842))


def _build_bgr_to_style_map():
    """danmaku2assのBT.709変換後BGR値→NicoConvAssスタイル名の逆引きテーブル"""
    nico_color_to_style = {
        0xff0000: "red", 0xff8080: "pink", 0xffcc00: "orange",
        0xffff00: "yellow", 0x00ff00: "green", 0x00ffff: "cyan",
        0x0000ff: "blue", 0xc000ff: "purple", 0x000000: "black",
        # 拡張色（niconicowhite, truered, passionorange 等）
        0xcccc99: "white", 0xcc0033: "red", 0xff6600: "orange",
        0x999900: "yellow", 0x00cc66: "green", 0x33ffcc: "cyan",
        0x6633cc: "purple",
    }
    result = {}
    for rgb, style in nico_color_to_style.items():
        bgr = _convert_color_bt709(rgb)
        result[bgr.upper()] = style
    return result


_BGR_TO_STYLE = _build_bgr_to_style_map()


def _fixup_dialogue(line, y_offset):
    """Dialogue行をNicoConvAss互換形式に変換する。"""
    if not line.startswith("Dialogue:"):
        return line
    after = line[len("Dialogue:"):].lstrip()
    parts = after.split(",", 9)
    if len(parts) < 10:
        return line

    text = parts[9]

    # 色タグからスタイル名を決定
    style_name = "white"
    cm = _COLOR_TAG_RE.search(text)
    if cm:
        bgr = cm.group(1).upper()
        style_name = _BGR_TO_STYLE.get(bgr, "white")

    # 色タグ除去（Style側で色指定）
    text = _COLOR_TAG_RE.sub("", text)
    text = _3COLOR_TAG_RE.sub("", text)

    # moveのY座標にオフセット追加（上端見切れ防止）
    text = _MOVE_RE.sub(
        lambda m: "\\move(%s,%d,%s,%d)" % (
            m.group(1), int(m.group(2)) + y_offset,
            m.group(3), int(m.group(4)) + y_offset),
        text)

    # posのY座標にもオフセット追加（ただし下揃え\an2は除く）
    if "\\an2" not in text:
        text = _POS_RE.sub(
            lambda m: "\\pos(%s,%d)" % (
                m.group(1), int(m.group(2)) + y_offset),
            text)

    parts[0] = "0"
    parts[3] = style_name
    parts[9] = text
    return "Dialogue: " + ",".join(parts)


def fixup_ass(ass_path, width, height, fontsize, font):
    """danmaku2ass出力をNicoConvAss互換形式に変換する。"""
    with open(ass_path, "r", encoding="utf-8-sig") as f:
        content = f.read()

    dialogues = []
    for line in content.splitlines():
        if line.startswith("Dialogue:"):
            dialogues.append(line)

    y_offset = max(1, round(height / 120))
    out = []

    # [Script Info]
    out.append("[Script Info]")
    out.append("ScriptType: v4.00+")
    out.append("Collisions: Normal")
    out.append("ScaledBorderAndShadow: Yes")
    out.append("PlayResX: %d" % width)
    out.append("PlayResY: %d" % height)
    out.append("Timer: 100.0000")
    out.append("WrapStyle: 0")
    out.append("")

    # [V4+ Styles]
    out.append("[V4+ Styles]")
    out.append("Format: Name, Fontname, Fontsize, PrimaryColour, "
               "SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
               "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
               "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
               "MarginV, Encoding")
    for name, color in _NICOCONVASS_COLORS:
        out.append(
            "Style: %s,%s,%d,%s,%s,&H00000000,&H00000000,"
            "-1,0,0,0,100,100,0,0.00,1,0,4,7,20,20,40,1"
            % (name, font, fontsize, color, color))
    out.append("")

    # [Events]
    out.append("[Events]")
    out.append("Format: Layer, Start, End, Style, Name, MarginL, "
               "MarginR, MarginV, Effect, Text")
    for d in dialogues:
        out.append(_fixup_dialogue(d, y_offset))

    with open(ass_path, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(out))
        if out:
            f.write("\r\n")
