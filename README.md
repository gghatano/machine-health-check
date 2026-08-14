# machie-health-check

Ubuntu のマシンメトリクスを 30分間隔で収集して Google スプレッドシートへ記録し、
ダッシュボードで可視化する。

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

```bash
uv sync --group dev
uv run pytest
```

## 欠測の監視（Discord通知）

記録が一定時間途切れたら Discord へ通知する。監視対象の Ubuntu 自身で動かすと、
Ubuntu が停止したときに監視も止まってしまうため、**GitHub Actions から**
スプレッドシートの最新 timestamp だけを見る方式にしている
（`.github/workflows/metrics-watchdog.yml`）。

### 準備

1. Discord のチャンネル設定 → 連携サービス → ウェブフック からウェブフックを作成し、URL をコピーする
2. リポジトリの Settings → Secrets and variables → Actions に
   `DISCORD_WEBHOOK_URL` という名前で登録する（URL は Git 管理しない）
3. Actions タブの「metrics watchdog」から `Run workflow` で動作確認する
   （`dry_run` を on にすると、通知を送らず判定結果だけ表示する）

### 動作

- 30分間隔で実行し、最新データが **90分**以上前なら通知する（＝2回連続で記録が欠けたら通知）
- 同じ障害で鳴り続けないよう、通知後は **6時間**経つまで再通知しない
- 記録が再開したら復旧を通知する
- 通知済みかどうかは Actions のキャッシュに保存する。キャッシュが失われた場合は
  通知が1回だけ重複することがある

しきい値はワークフローの `MISSING_AFTER_MINUTES` / `REPEAT_ALERT_HOURS` で変更できる。

### 手元で試す

```bash
# 通知は送らず、判定だけ表示する
uv run python -m machine_health_check.watchdog --dry-run

# しきい値を短くして、通知内容を確認する
MISSING_AFTER_MINUTES=1 uv run python -m machine_health_check.watchdog --dry-run
```

注意: GitHub の schedule 実行は数分〜十数分遅れることがある。また、リポジトリが
60日間更新されないと schedule は自動停止する。
