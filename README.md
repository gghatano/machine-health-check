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
