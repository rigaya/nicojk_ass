# PyInstallerの入口。cli.main()を呼ぶだけ。
import sys

from nicojk_ass.cli import main

if __name__ == "__main__":
    # exeはPYTHONIOENCODINGなどの環境変数を読まない。
    # パイプに出すログとヘルプが、WindowsでANSI(cp932)にならないよう、UTF-8に固定する。
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    sys.exit(main())
