#!/usr/bin/env python3
"""リリースの版の番号を確かめる。ローカルでもCIでも同じものを使う。

確かめること:
  1. src/nicojk_ass/__init__.py の __version__ が「整数部.小数2桁」(0.01、1.00など)の形であること
  2. その版のタグがまだないこと
  3. 既存の版のタグの最大より大きいこと
成功したら版の番号を標準出力に出して終了コード0、誤りがあれば理由を標準エラーに出して1で終わる。
"""

import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_RE = re.compile(r"^(\d+)\.(\d{2})$")


def read_version(root):
    path = os.path.join(root, "src", "nicojk_ass", "__init__.py")
    with open(path, encoding="utf-8") as f:
        m = re.search(r'^__version__\s*=\s*"([^"]*)"', f.read(), re.MULTILINE)
    if not m:
        raise ValueError(f"{path} has no __version__")
    return m.group(1)


def version_key(text):
    m = VERSION_RE.match(text)
    return (int(m.group(1)), int(m.group(2))) if m else None


def list_tags(root):
    out = subprocess.run(["git", "-C", root, "tag", "--list"], capture_output=True, text=True, check=True)
    return out.stdout.split()


def check(version, tags):
    """誤りの理由(文字列)を返す。問題がなければNone。"""
    key = version_key(version)
    if key is None:
        return (f"version '{version}' has the wrong form "
                "(use major.minor with 2 digits, such as 0.01 or 1.00)")
    if version in tags:
        return f"tag '{version}' already exists"
    keys = [k for k in (version_key(t) for t in tags) if k is not None]
    if keys and key <= max(keys):
        latest = "%d.%02d" % max(keys)
        return f"version '{version}' is not greater than the latest tag '{latest}'"
    return None


def main():
    parser = argparse.ArgumentParser(description="Check the release version number")
    parser.add_argument("--root", default=ROOT, help="repository root (default: parent of this script)")
    args = parser.parse_args()
    try:
        version = read_version(args.root)
        error = check(version, list_tags(args.root))
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
