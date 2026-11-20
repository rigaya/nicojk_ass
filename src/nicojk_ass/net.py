"""HTTPSの証明書の設定。"""

import ssl
import sys


def ssl_context():
    """exe(PyInstaller)のときは同梱のcertifiの証明書を使う。ソースのままのときはNone(標準の設定)。

    Linuxのexeは同梱のOpenSSLがビルドした環境の証明書の場所を探すため、certifiのものを明示する。
    """
    if not getattr(sys, "frozen", False):
        return None
    import certifi
    return ssl.create_default_context(cafile=certifi.where())
