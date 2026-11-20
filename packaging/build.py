#!/usr/bin/env python3
"""PyInstallerでexeを作る。ローカルでもCIでも同じものを使う。

使い方: python packaging/build.py [--work 作業フォルダ] [--dist 出力先]

作業用のvenvを作り、固定した依存を入れて、単一のexe(--onefile)を <dist>/nicojk_ass(.exe) に作る。
できたexeで--versionが版と合うことも確かめる。
zipへの梱包(LICENSE、README.md、danmaku2assのCOPYING)はCIのワークフローで行う。
"""

import argparse
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IS_WINDOWS = sys.platform == "win32"
EXE_NAME = "nicojk_ass.exe" if IS_WINDOWS else "nicojk_ass"

# danmaku2ass.pyはデータとして同梱し、runpyで実行する。PyInstallerはその中のimportを辿れないので明示する。
DANMAKU2ASS_IMPORTS = ["argparse", "calendar", "gettext", "io", "json", "logging", "math",
                       "random", "re", "time", "xml.dom.minidom"]


def run(cmd, **kwargs):
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kwargs)


def read_version():
    with open(os.path.join(ROOT, "src", "nicojk_ass", "__init__.py"), encoding="utf-8") as f:
        return re.search(r'^__version__\s*=\s*"([^"]+)"', f.read(), re.MULTILINE).group(1)


def make_venv(work):
    """作業用のvenvを作って依存を入れ、そのpythonのパスを返す。"""
    venv = os.path.join(work, "venv")
    run([sys.executable, "-m", "venv", venv])
    python = os.path.join(venv, "Scripts" if IS_WINDOWS else "bin", "python.exe" if IS_WINDOWS else "python")
    run([python, "-m", "pip", "install", "--quiet", "-r", os.path.join(ROOT, "packaging", "requirements.txt")])
    return python


def run_pyinstaller(python, work):
    """単一のexeを作り、そのパスを返す。"""
    cmd = [python, "-m", "PyInstaller", "--noconfirm", "--clean", "--console", "--onefile",
           "--name", "nicojk_ass",
           "--paths", os.path.join(ROOT, "src"),
           "--distpath", os.path.join(work, "dist_onefile"),
           "--workpath", os.path.join(work, "pyi_onefile"),
           "--specpath", os.path.join(work, "pyi_onefile"),
           "--add-data", os.path.join(ROOT, "src", "nicojk_ass", "data") + os.pathsep + "nicojk_ass/data",
           "--add-data", os.path.join(ROOT, "src", "nicojk_ass", "_vendor", "danmaku2ass.py") + os.pathsep + "nicojk_ass/_vendor",
           "--hidden-import", "certifi"]
    for name in DANMAKU2ASS_IMPORTS:
        cmd += ["--hidden-import", name]
    cmd.append(os.path.join(ROOT, "packaging", "entry.py"))
    run(cmd)
    return os.path.join(work, "dist_onefile", EXE_NAME)


def verify(exe, version):
    """できたexeの--versionが版と合うことを確かめる。"""
    out = subprocess.run([exe, "--version"], capture_output=True, text=True, check=True).stdout.strip()
    if out != f"nicojk_ass {version}":
        raise SystemExit(f"error: {exe} --version is '{out}' (expected: nicojk_ass {version})")
    print(f"verified: {exe}: {out}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Build the executable")
    parser.add_argument("--work", default=os.path.join(ROOT, "build"), help="work directory (recreated)")
    parser.add_argument("--dist", default=os.path.join(ROOT, "dist"), help="directory for the executable")
    args = parser.parse_args()

    version = read_version()
    work = os.path.abspath(args.work)
    dist = os.path.abspath(args.dist)
    # distはマウントされたフォルダのこともあるので、フォルダ自体は消さない
    if os.path.isdir(work):
        shutil.rmtree(work)
    os.makedirs(work)
    os.makedirs(dist, exist_ok=True)

    python = make_venv(work)
    built = run_pyinstaller(python, work)
    exe = os.path.join(dist, EXE_NAME)
    shutil.copy2(built, exe)
    verify(exe, version)


if __name__ == "__main__":
    main()
