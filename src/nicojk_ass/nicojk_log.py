"""NicoJKの設定とローカルログを読み込む。"""

import re
import sys
import tomllib
import xml.etree.ElementTree as ET
import zipfile
from itertools import groupby
from pathlib import Path

from .comments import comment_time, log

_CHAT_DATE = re.compile(rb'\sdate="([0-9]{1,19})"(?=[\s/]|$)')


def load_dirs():
    """起動した実行ファイルまたはスクリプトの隣にあるログ設定を読む。"""
    entry = sys.executable if getattr(sys, "frozen", False) else sys.argv[0]
    path = Path(entry).absolute().with_name("nicojk_ass.toml")
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        log(f"warning: NicoJK log configuration not found: {path}")
        return []
    try:
        config = tomllib.loads(data.decode("utf-8-sig"))
    except tomllib.TOMLDecodeError as error:
        if "Unescaped '\\'" in str(error):
            raise ValueError(f"{error}。パスは'…'(シングルクォート)で囲んでください") from error
        raise
    section = config.get("nicojk_log", {})
    if not isinstance(section, dict):
        raise ValueError("[nicojk_log]にはテーブルを指定してください")
    dirs = section.get("dirs", [])
    if not isinstance(dirs, list) or not all(isinstance(directory, str) for directory in dirs):
        raise ValueError("nicojk_log.dirsには文字列のリストを指定してください")
    if not dirs:
        log("warning: nicojk_log.dirs is empty")
    result = []
    for directory in dirs:
        log_dir = path.parent / directory
        if not log_dir.is_dir():
            log(f"warning: NicoJK log directory not found: {log_dir}")
        else:
            result.append(log_dir)
    return result


def _select(entries, start, end):
    """開始以前で最も新しいログと、範囲内に始まるログを時刻順に選ぶ。"""
    prior = None
    selected = []
    for stamp, entry in sorted(entries.items()):
        if stamp <= start:
            prior = entry
        elif stamp <= end:
            selected.append(entry)
    return selected if prior is None else [prior, *selected]


class _LogReader:
    def __init__(self, start, end):
        self.start = start
        self.end = end
        self.packet = ET.Element("packet")
        self.files = []
        self.number = 0
        self.invalid = 0

    def read_lines(self, lines, name):
        self.files.append(str(name))
        for raw in lines:
            if not raw.startswith(b"<chat "):
                continue
            # 開始タグのdateで範囲外と分かる行は、XMLとして解釈しない。
            match = _CHAT_DATE.search(raw.partition(b">")[0])
            if match and not self.start <= int(match[1]) <= self.end:
                self.number += 1
                continue
            try:
                chat = ET.fromstring(raw.decode("utf-8"))
                comment_time(chat)
                date = int(chat.attrib["date"])
            except (UnicodeError, ET.ParseError, ValueError):
                self.invalid += 1
                continue
            self.number += 1
            if self.start <= date <= self.end:
                if chat.get("no") is None:
                    chat.set("no", str(self.number))
                self.packet.append(chat)


def _read_directory(directory, start, end):
    reader = _LogReader(start, end)
    try:
        paths = sorted(directory.iterdir())
    except FileNotFoundError:
        return reader
    except OSError as error:
        log(f"warning: cannot list NicoJK logs in {directory}: {error}")
        return reader

    entries = {}
    for path in paths:
        if re.fullmatch(r"[0-9]{10}\.zip", path.name) and int(path.stem) <= end:
            try:
                with zipfile.ZipFile(path) as archive:
                    for name in archive.namelist():
                        if re.fullmatch(r"[0-9]{10}\.txt", name):
                            entries[int(name[:10])] = (path, name)
            except (OSError, zipfile.BadZipFile) as error:
                log(f"warning: cannot list NicoJK logs in {path}: {error}")
    for path in paths:
        if re.fullmatch(r"[0-9]{10}\.txt", path.name) and path.is_file():
            entries[int(path.stem)] = (path, None)

    for path, logs in groupby(_select(entries, start, end), key=lambda entry: entry[0]):
        try:
            if path.suffix == ".txt":
                with path.open("rb") as stream:
                    reader.read_lines(stream, path)
            else:
                with zipfile.ZipFile(path) as archive:
                    for _, name in logs:
                        with archive.open(name) as stream:
                            reader.read_lines(stream, f"{path}:{name}")
        except (OSError, zipfile.BadZipFile, RuntimeError) as error:
            log(f"warning: cannot read NicoJK logs in {path}: {error}")
    return reader


class NicoJKLog:
    def __init__(self, dirs, attempts):
        self.dirs = dirs
        self.attempts = attempts

    def read(self, channel, start, end):
        """設定順にフォルダを探し、コメントが取れた最初のフォルダを使う。"""
        attempt = dict(channel=channel, start=start, end=end, used=False, files=[])
        self.attempts.append(attempt)
        for directory in self.dirs:
            reader = _read_directory(directory / channel, start, end)
            if reader.invalid:
                log(f"warning: NicoJK log {directory / channel}: dropped {reader.invalid} invalid rows")
            if len(reader.packet):
                attempt.update(used=True, files=reader.files)
                log(f"NicoJK log: {channel}: {len(reader.packet)} comments from {', '.join(reader.files)}")
                return ET.tostring(reader.packet, encoding="unicode")
        log(f"warning: NicoJK log: no comments for {channel} in {start} - {end}, using the API")
        return None
