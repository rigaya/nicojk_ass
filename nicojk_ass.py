#!/usr/bin/env python3
# ソースのまま動かすときの入口。パッケージを読み込んでcli.main()を呼ぶだけ。
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from nicojk_ass import cli  # noqa: E402

if __name__ == "__main__":
    sys.exit(cli.main())
