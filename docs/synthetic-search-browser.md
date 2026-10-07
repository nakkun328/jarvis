# 人工記憶をブラウザで検索する試験入口（Draft）

通常アプリとは別の入口です。[人工検索CLI](synthetic-memory-search.md)と同じ共有setup、
SemanticMemorySearcherを使います。画面は似た対象を含む固定人工fixture `ja-extra-v1` を
検索します（CLIの固定fixture `ja-v1` は変更しません）。実DB/vault・任意fixture・LLM providerを
引数/APIで受け付けず、通常アプリの設定も変更しません。外部API呼び出し・LLM回答生成なし。

既存の[固定E5環境/cache](local-embedding-trial.md)を再利用し、新たなdownloadはしません。
実E5を使うサーバー・CLI・評価は全体で同時に1プロセスだけにします。既存8765は保護し、
空いている別ポートを指定してください（既定8766）。起動は127.0.0.1・1worker固定です。

```sh
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
export OMP_NUM_THREADS=2 MKL_NUM_THREADS=2
/private/tmp/jarvis-p2-local-encoder-20261004/venv/bin/python scripts/serve_synthetic_search.py \
  --cache-dir /private/tmp/jarvis-p2-local-encoder-20261004/model-cache --port 8766 --limit 3
```

`http://127.0.0.1:8766/`を開いて質問を入力します。初回検索は人工記憶と一時indexの
準備を待ち、以降は同じ資源を使います。欠損cache・不完全snapshot・失敗時に自動取得や
fakeへの切替はしません。空入力と4000文字超は検索せず、失敗は生例外やpathを隠し、
「同じ質問で再試行」できます。準備失敗時は一時資源を解放して次回に準備し直します。
終了はサーバー端末でCtrl-C。進行中の検索を待ち、provider、利用可能なindex client.close、
一時データの順で解放します。ブラウザのタブを閉じるだけではサーバーは終了しません。

モデルなしの画面・契約確認には`python scripts/serve_synthetic_search.py --contract-only`を
使えます（vector依存が必要）。画面上部に黄色の「FAKE契約検証モード」帯、タブ題名の`[FAKE]`、
各候補の`[FAKE]`印を出し、順序は無意味で日本語semantic品質の証拠にはしません。
`--contract-only`は`--cache-dir`を受け付けません。
サーバーはHostが`127.0.0.1`/`localhost`以外のリクエストを400で拒否します（DNS rebinding対策）。
画面は「candidate != answer evidence」と、候補が現在の正本で承認済みの記憶だけであること
（旧版・未承認・却下は除外）を常時表示します。

現在approvedの本文、出典、origin、ID/revision、訂正元、承認後の編集、stale、記憶metadataの
confidence/importance、index scoreを表示します。scoreは確率・確信度・回答可能性ではありません。
候補が返ることと質問への支持根拠があることは別で、回答可能性は判定しません。

実モデルの画面確認には次の4質問を固定します（順位は保証しません）：

- 訂正：`ミズキという星を観測する試作機、頭脳にあたる制御基板を何に替えた？`（mizuki-currentはESP32）
- 編集：`星を見る架空の集まりは何曜日の何時から？`（observing-timeは水曜日19時、編集あり）
- 似た対象：`ミズキではなくミツキの方は、どの制御基板を使っている？`（mitsuki-controllerはSTM32）
- 根拠不足：`ミズキの電池を満充電にしたら何時間観測できる？`（支持根拠なし）

無関係の追加確認には`カナダの首都はどこ？`を使えます（人工正本に支持根拠なし）。

軽量確認：`python -m pytest -q tests/test_synthetic_search_browser.py`、
`node --test frontend/test/*.test.mjs`（Nodeによってはディレクトリ指定は不可）。
fake試験はHTTP入力境界、現在正本・訂正・編集・非承認除外、準備失敗後の再試行、
検索失敗の秘匿、終了時の解放順序を確認します。実E5での順位・ブラウザ操作は別途確認が必要です。
