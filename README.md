# nicojk_ass

ニコニコ実況の過去ログ(コメント)を取得し、ASS字幕にするツール。[Amatsukaze](https://github.com/rigaya/Amatsukaze)から呼び出して使う。単体でも使える。

過去ログは [ニコニコ実況 過去ログ API](https://jikkyo.tsukumijima.net)(個人運営)から取得する。負荷をかけないよう、短い間隔で繰り返し呼ばないこと。

この版(第2段)でできること:

- 局(ServiceIdまたはjk番号)と時刻の範囲を指定して、コメントを取得してASSにする
- ローカルのコメントファイル(XML、NicoJKのjkl)をASSにする(取得元の時刻合わせを含む)
- 別の局(基準)のコメントを、時刻の対応表(`--timemap`)で対象の時刻へ写して使う、またはその局のコメントと統合する
- 対象と基準の音声(WAV)を照合して対応表を作り、基準のコメントを写す(`--wav`、`--ref-wav`)
- 音声照合だけを行う(`align`。開発用)
- 1回の実行で複数の解像度のASSを出力する

## 必要なもの

Python 3.10以上。音声照合(`--ref-wav`、`align`)にはnumpyが要る。それ以外は標準ライブラリだけで動く。

## 使い方

```sh
# 局のコメントをAPIから取得する
python3 nicojk_ass.py make --sid 211 \
    --start 2026-11-19T21:00:00 --end 2026-11-19T21:30:00 \
    --output 1920x1080=out1080.ass --output 1280x720=out720.ass

# コメントファイルを使う(番組開始の時刻を合わせる)
python3 nicojk_ass.py make --comments-file comments.xml \
    --start 20261119210000 --end 20261119213000 --comments-start 20261119210000 \
    --output 1920x1080=out.ass
```

時刻はUNIX秒(小数可)、ISO 8601(タイムゾーンを省くとJST)、14桁のJST(`YYYYMMDDhhmmss`)のどれでも指定できる。

### `make`のオプション

対象:

| オプション | 内容 |
|---|---|
| `--sid N` / `--channel jkN` | 対象の局。`--sid`はServiceIdから`ch_sid.txt`でjk番号を引く。両方あれば`--channel`を使う |
| `--start T` `--end T` | 対象の録画の先頭と末尾の時刻。ASSの時刻0は取得範囲の始まり |
| `--program-start T` | 録画した放送の開始時刻。省略時は`--start` |
| `--comments-file F` | コメントファイル(`.xml`、`.jkl`)。指定すればAPIは使わない |
| `--comments-start T` | コメントの時刻の基準(番組開始に当たる時刻)。省略時は`--program-start`。ファイルで省略すると、録画の範囲に半数以上のコメントが入っていれば番組開始、そうでなければ最初のコメントを基準にする(警告を出す) |
| `--comments-offset S` | 取得範囲を、ここで指定した秒数だけ前へずらす(取得範囲の始まりから引く) |
| `--comments-file-auto` | `--comments-file`は自動で見つけた同名ファイル。コメントが1件もなければ警告してファイルを使わず、`--sid`/`--channel`でAPIから取得する |

取得範囲の始まりは`--start + (--comments-start − --program-start) − --comments-offset`、長さは`--end − --start`。

基準(別の局のコメントを時刻の対応表で写して使う):

| オプション | 内容 |
|---|---|
| `--timemap F` | 基準の時刻から対象の時刻への対応表(JSON。`[基準の開始, 基準の終了, 対象の開始, 対象の終了]`の並び) |
| `--ref-sid N` / `--ref-channel jkN` | 基準の局 |
| `--ref-start T` `--ref-end T` | 基準の録画の先頭と末尾の時刻 |
| `--wav F` `--ref-wav F` | 対象と基準の音声(16bit・8kHzのWAV。モノラルかステレオで、ステレオは平均する)。`--ref-wav`を指定すると音声照合で対応表を作る(`--timemap`とは同時に指定できない。`--wav`、`--ref-start`、`--ref-end`と、`--ref-sid`・`--ref-channel`・`--ref-comments-file`のどれかが要る)。照合に失敗したら警告を出し、基準を使わずに対象の局の通常の取得へ進む。結果は`--report`の`"align"`に入る |
| `--ref-comments-file F` | 基準のコメントファイル(指定すればAPIは使わない) |
| `--mode replace\|merge` | `replace`は基準のコメントだけ、`merge`は対象の局のコメントと統合する(既定は`replace`) |

出力:

| オプション | 内容 |
|---|---|
| `--output WxH=PATH` | 解像度ごとのASSの出力先。繰り返し指定できる。取得は1回だけ |
| `--font` `--fontsize` `--display-seconds` `--opacity` | フォント、サイズ(省略時は解像度から決まる)、スクロールコメントの表示秒数、不透明度 |
| `--report F` | 取得元、範囲、件数、終了の理由などをJSONで書く(`"schema": 1`) |
| `--mark-reference-color COLOR` | 統合で基準の局のコメントにこの色を足す(調査用) |
| `--api-url URL` | 過去ログAPIのベースURL |

終了コード: 0は成功(コメントが0件でもASSを書く)、100は付けるコメントがない(局にjk番号がない、APIが400/404を返した)、1はエラー。

`nicojk_ass --version`で版を表示する。

### `align`(開発用)

```sh
python3 nicojk_ass.py align --wav 対象.wav --ref-wav 基準.wav [--report result.json]
```

音声照合だけを行い、区間・ずれ・相関比・対応表をJSONに書く(`--report`を省くと標準出力)。拡張子が`.f32`のファイルは、8kHzモノラルのfloat32の生のPCMとして読む。終了コードは、成功0、照合の失敗1、その他のエラー2。

## テスト

```sh
python3 -m unittest discover -s tests -v
```

## ライセンス

- このリポジトリのソース(`src/nicojk_ass/`のうち`_vendor/`以外、`nicojk_ass.py`、テストなど)はMIT License(`LICENSE`)。
- `src/nicojk_ass/_vendor/danmaku2ass.py`は[danmaku2ass](https://github.com/m13253/danmaku2ass)のコミット`ced881747670c2eb1c0dbd292c2a567f444b056a`を、手を加えずに同梱したもの。ライセンスはGPL-3.0(ファイルの先頭の表記と https://www.gnu.org/licenses/gpl-3.0.html を参照)。
- danmaku2assを含むため、配布するexe(今後の予定)はGPL-3.0になる。ソースはこのリポジトリで公開する。
- Amatsukazeからは別のプロセスとして呼ぶだけで、Amatsukazeのライセンスは変わらない。
