# machie-health-check

Ubuntu のマシンメトリクスを 30分間隔で収集して Google スプレッドシートへ記録し、
ダッシュボードで可視化する。

## 準備

リポジトリ直下に `.env` を置く（Git 管理しない）。

```bash
GOOGLE_SCRIPT_URL=https://script.google.com/macros/s/xxxxx/exec
METRICS_TOKEN=xxxxx
```

`METRICS_TOKEN` は Apps Script のスクリプトプロパティに設定した値と同じにする。

Apps Script を再デプロイすると `GOOGLE_SCRIPT_URL` が変わることがあるので、
デプロイのたびに `.env` を更新する。

```bash
uv sync
```

## メトリクスの送信

### 1回だけ送る（動作確認）

```bash
cd /home/hatanotakuma/works/machine-health-check
uv run python -m machine_health_check.main
```

収集した内容が JSON で表示され、最後に `POST result: {'ok': True, ...}` が出れば成功。
スプレッドシートの `metrics` シート末尾に1行増えていることでも確認できる。

失敗したときの見分け方:

| 症状 | 原因 |
| --- | --- |
| `Apps Script returned an error: ...` | `METRICS_TOKEN` が Apps Script 側と不一致 |
| `JSONDecodeError`（HTML が返っている） | デプロイのアクセス権が「全員」になっていない、または URL が古い |
| `KeyError: 'GOOGLE_SCRIPT_URL'` | `.env` が無い、またはリポジトリ直下以外で実行している |

### 送信の再試行

Apps Script はコールドスタート時に応答が遅れる。journal の実測では、送信が成功した回でも
最大23秒かかっていた（中央値4秒）。そのため送信は次の設定で行う。

| 項目 | 既定値 | 環境変数 |
| --- | --- | --- |
| タイムアウト | 45秒 | `SEND_TIMEOUT_SECONDS` |
| リトライ回数 | 3回（5秒 → 10秒待ち） | `SEND_RETRIES` |

タイムアウト・接続失敗・5xx はリトライする。一方、**4xx とトークン不一致はリトライしない**。
どちらも設定を直さないと解決しないため、終了コード `2` で即座に終わる
（`systemd` 側もこの終了コードでは再試行しない）。

`404` が返るのは、たいてい再デプロイで `/exec` の URL が変わったまま `.env` が古いとき。

### 送信に失敗した回の扱い

`state.json`（通信量の差分計算に使う）は、**送信が成功した回だけ**更新する。
失敗した回で更新してしまうと、その回の通信量がどこにも記録されないまま失われるため。

失敗した次の回は、欠測していた期間ぶんを合計したデルタになる。ダッシュボードの
転送量グラフは、欠測直後の点を描画から除外している。

### 定期送信の仕組み（systemd timer）

30分間隔の実行は systemd のシステムタイマーで行っている。ユーザー単位（`--user`）ではなく
システム単位なので、`systemctl` に `--user` は付けない。

ユニットファイルはリポジトリの `systemd/` にある。

| ファイル | 役割 |
| --- | --- |
| `systemd/machine-health-check.timer` | 30分間隔の起動 |
| `systemd/machine-health-check.service` | 収集と送信の実行 |
| `systemd/machine-health-check-failure.service` | 送信が失敗した回を Discord へ通知 |

インストール・更新は次のとおり。

```bash
sudo cp systemd/machine-health-check.service \
        systemd/machine-health-check.timer \
        systemd/machine-health-check-failure.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now machine-health-check.timer
```

`WorkingDirectory` は必ずリポジトリ直下にする。`.env` の読み込みと、ネットワーク転送量の
差分計算に使う `state.json` の読み書きが、どちらもカレントディレクトリ基準のため。

`Persistent=true` にしてあるので、スリープや電源断で実行を逃した場合は、
復帰後に1回だけまとめて実行される。

`.service` 自体は `disabled` で正しい。起動するのはタイマーであり、
`.service` を enable すると起動のたびに余計な1回が走る。

#### 失敗したときの再試行と通知

アプリ側のリトライ（45秒×3回）で送れなかった場合の保険として、`.service` に次を入れている。

```ini
Restart=on-failure
RestartSec=60
RestartPreventExitStatus=2   # 設定の問題(4xx など)では再試行しない
StartLimitIntervalSec=600
StartLimitBurst=3            # 壊れている状態で再試行を繰り返さない
```

`Restart=on-failure` は `Type=oneshot` でも使える（`always` と `on-success` だけが拒否される）。

さらに `OnFailure=machine-health-check-failure.service` で、失敗した回を Discord に通知する。
しきい値（60分）に届かない単発の失敗は journal にしか残らず観測されないため、
1回の失敗でも気づけるようにしている。通知には journal の直近の例外行を含める。

通知先は `.env` の `DISCORD_WEBHOOK_URL` を使う。未設定なら何も送らずに正常終了する。

```bash
# 通知の文面を確認する（Webhook が設定されていれば実際に飛ぶ）
uv run python -m machine_health_check.notify_failure machine-health-check.service
```

### 状態とログを見る

```bash
# 次回・前回の実行時刻
systemctl list-timers machine-health-check.timer

# 直近の実行結果
systemctl status machine-health-check.service

# 送信ログ（POST result の行が出る）
journalctl -u machine-health-check.service -n 50

# 送信のたびに流し見する
journalctl -u machine-health-check.service -f
```

`POST result: {'ok': True}` で終わっていれば送信できている。

タイマーを待たずに1回実行して確かめたいときは次のようにする。

```bash
sudo systemctl start machine-health-check.service
```

### 開始・停止する

```bash
# 開始（再起動後も有効）
sudo systemctl enable --now machine-health-check.timer

# 一時的に止める（再起動すると復活する）
sudo systemctl stop machine-health-check.timer

# 完全に止める（再起動しても動かない）
sudo systemctl disable --now machine-health-check.timer
```

ユニットファイルを編集したときは `sudo systemctl daemon-reload` を実行する。

止めている間はスプレッドシートに記録が入らないため、60分を超えると
Discord へ欠測通知が飛び、以後15分ごとに鳴り続ける。長時間止めるときは監視も
無効にしておく（GitHub の Actions タブ → Monitor Ubuntu Metrics → `Disable workflow`）。

`.env` の `GOOGLE_SCRIPT_URL` を更新したときは、タイマー側に再読み込みの操作は要らない。
実行のたびに `.env` を読み直すため、次回の実行から新しい URL が使われる。

## ダッシュボード

<https://gghatano.github.io/machine-health-check/>

- `docs/` を GitHub Pages（main ブランチ）から配信している静的ページ。ビルド不要、依存ライブラリなし
- スプレッドシートの `metrics` シートを gviz の CSV 出力として直接読む（サーバも認証も無し）
- 表示期間は 24時間 / 1週間 / 1ヶ月 を切り替え。`?range=24h|7d|30d` を付ければその期間で開ける
- 欠測（記録が途切れた期間）は `0` で補完せず、線を途切れさせて網掛けで表示する
- CPU / Load Average / メモリ / Swap / ディスク / ネットワーク（速度・転送量）を時系列表示

前提: スプレッドシートの共有設定が「リンクを知っている全員が閲覧可」であること。
非公開にすると、ブラウザからは読めなくなる。

### ローカルで確認する

```bash
python3 -m http.server 8000 --directory docs
# http://localhost:8000/ を開く
```

ES modules を使っているため、`file://` で直接開かず簡易サーバ経由で開く。

## テスト

単体テストは外部通信をせず、`requests` と `psutil` を差し替えて実行する。
`.env` が無くても通る。

```bash
# 初回のみ（開発用の依存を入れる）
uv sync --group dev

# 全部走らせる
uv run pytest

# 失敗の詳細を見る
uv run pytest -v

# ファイル単位・テスト単位で走らせる
uv run pytest tests/test_sender.py
uv run pytest tests/test_metrics.py -k network
```

| ファイル | 対象 |
| --- | --- |
| `tests/test_metrics.py` | メトリクス収集（`metrics.py`） |
| `tests/test_sender.py` | スプレッドシートへの POST（`sender.py`） |
| `tests/test_main.py` | 収集から送信までの流れ（`main.py`） |

`monitor.py`（欠測の監視）にはテストが無い。動作確認は
「欠測の監視 → 手元で試す」で行う。

実際にスプレッドシートへ届くかどうかは単体テストでは確認できないので、
「メトリクスの送信 → 1回だけ送る」で確かめる。
送信せず収集結果だけ見たいときは次のようにする。

```bash
uv run python -c "import json; from machine_health_check.metrics import collect_metrics; print(json.dumps(collect_metrics(), indent=2, ensure_ascii=False))"
```

## 欠測の監視（Discord通知）

記録が一定時間途切れたら Discord へ通知する。監視対象の Ubuntu 自身で動かすと、
Ubuntu が停止したときに監視も止まってしまうため、**GitHub Actions から**
Apps Script に最新レコードを問い合わせる方式にしている
（`.github/workflows/monitor-metrics.yml` / `src/machine_health_check/monitor.py`）。

### 準備

1. Discord のチャンネル設定 → 連携サービス → ウェブフック からウェブフックを作成し、URL をコピーする
2. リポジトリの Settings → Secrets and variables → Actions に、
   `DISCORD_WEBHOOK_URL` と `GOOGLE_SCRIPT_URL` を登録する（どちらも Git 管理しない）
3. Actions タブの「Monitor Ubuntu Metrics」から `Run workflow` で動作確認する

Apps Script を再デプロイして URL が変わったときは、`.env` だけでなく
**Actions の `GOOGLE_SCRIPT_URL` シークレットも更新する**。ここが古いままだと
監視だけが静かに失敗する。

### 動作

- 15分間隔で実行し、最新データが **60分**以上前なら Discord へ通知する
- 同じ障害で鳴り続けないよう、通知後 **6時間**は再通知しない
- 記録が再開したら復旧を通知する
- 通知済みかどうかは Actions のキャッシュに保存する（失われた場合は通知が1回重複しうる）
- 通知を実際に送れなかったとき（`DRY_RUN` / Webhook未設定）は状態を進めない

しきい値と間隔はワークフローの env で変更する。

| 項目 | 既定値 | 環境変数 |
| --- | --- | --- |
| 通知するまでの猶予 | 60分 | `THRESHOLD_MINUTES` |
| 同一障害の再通知間隔 | 6時間 | `REPEAT_ALERT_HOURS` |
| APIのタイムアウト | 45秒 | `REQUEST_TIMEOUT_SECONDS` |
| APIのリトライ回数 | 3回 | `REQUEST_RETRIES` |
| 取得失敗が続いたら通知する回数 | 3回 | `FETCH_FAILURE_ALERTS` |

監視は `GOOGLE_SCRIPT_URL` への GET で行う。Apps Script は次の形を返す。

```json
{"ok": true, "record": {"timestamp": "2026-08-15T23:00:18+09:00", "hostname": "hatanotakuma-M6-Ultra"}}
```

Apps Script はコールドスタートで十数秒かかることがあり、短いタイムアウトだと
散発的にタイムアウトで落ちる。45秒待ってから最大3回リトライする。

### Actions が赤くなるとき

**赤い run ＝「監視が実行できなかった」**を意味する。欠測を検知したときは
通知を送ったうえで正常終了するので、run は緑のままになる（通知が信号であり、
run の成否と混ぜない）。

リトライしても取得できなかった場合は run が失敗する。それが **3回続いた**時点で、
「監視自体が失敗している」ことを Discord にも通知する（サーバ停止と紛らわしいため、
1回のタイムアウトでは通知しない）。

### 手元で試す

```bash
# 判定だけ表示する（通知は送らない）
uv run python -m machine_health_check.monitor --dry-run

# しきい値を短くして、通知の文面を確認する
THRESHOLD_MINUTES=1 uv run python -m machine_health_check.monitor --dry-run
```

`elapsed_minutes` と `status: healthy` / `status: stale` が表示される。
通知を飛ばさずに最新レコードだけ見たいときは次のようにする。

```bash
uv run python -c "
import os, requests
from dotenv import load_dotenv
load_dotenv()
print(requests.get(os.environ['GOOGLE_SCRIPT_URL'], timeout=15).json())
"
```

注意: GitHub の schedule 実行は数分〜十数分遅れることがある。また、リポジトリが
60日間更新されないと schedule は自動停止する。
