# 開発用メモ

## 注意点

- [ニコニコ実況 過去ログ API](https://jikkyo.tsukumijima.net)やしょぼいカレンダーなどからの取得時、負荷をかけないよう、短い間隔で繰り返し呼ばないこと。

## 版の番号とリリース

- 版の番号は`0.01`、`0.02`、…、`0.99`、`1.00`、…(整数部.小数2桁)。`src/nicojk_ass/__init__.py`の`__version__`だけに書く。`nicojk_ass --version`と`--help`に出る。`pyproject.toml`はここから読む(PEP 440では`0.01`が`0.1`に正規化される)。
- gitのタグは版の番号そのもの(`0.01`。`v`を付けない)。
- リリースの手順:
    1. `__version__`を、既存の最大のタグより大きい値に上げてコミットし、pushする。
    2. `python3 packaging/check_version.py`で、版の形、タグがまだないこと、既存の最大のタグより大きいことを確かめる(手動の起動のワークフローも最初に同じ確認をする)。
    3. GitHubのActionsで「Release」を手動で起動する。Windows x64、Linux x64、Linux arm64のexeを作り、すべて成功したらタグを付けて、draft releaseを作る(zipを付ける)。
    4. draft releaseの説明(exeはGPL-3、ソースの場所、danmaku2assのコミット)を確かめて公開する。
- push・pull requestでは、テストとexeのビルドだけを行い、exeはartifactに残す。

## exeをローカルで作る

Python 3.11以上が要る。作業用のvenvを`build/`の下に作ってPyInstallerなどを入れ、単一のexeを`dist/`に作る(`dist/nicojk_ass(.exe)`)。できたexeで`--version`が版と合うことも確かめる。CIも同じスクリプトを呼ぶ。

```sh
python3 packaging/build.py                      # Windowsでは python packaging\build.py
python3 packaging/build.py --work /tmp/work --dist /tmp/dist
```

- 依存は`packaging/requirements.txt`に書いてある。版は固定せず、そのときのPythonに入る最新版を使う。
- zipへの梱包はCI(`.github/workflows/build-exe.yml`の「zipにする」)が行う。`src/nicojk_ass/_vendor/README.md`に記録したdanmaku2assをgit cloneし、`danmaku2ass.py`が`_vendor/`のものと一致することを確かめてから、`COPYING`を`LICENSE`・`README.md`と一緒にzipへ入れる。
- Linuxで古いglibcのexeを作るには、`quay.io/pypa/manylinux_2_28_x86_64`などのコンテナの中で、`dnf install -y python3.12`したうえで`/usr/bin/python3.12 packaging/build.py`を実行する(`/opt/python`のPythonは共有ライブラリがなく、PyInstallerが使えない)。


## テスト

```sh
python3 -m unittest discover -s tests -v
```

## メモ

- HTTPSの証明書は、exeに同梱のcertifiのものを使う。
