# JARVIS

目的レベルの依頼を受け、調査・実行・検証・記憶まで扱うパーソナル AI アシスタントです。現在は Phase 0 の基盤を開発中です。会話機能はまだありません。

## Setup

Python 3.11 以上を使用します。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install '.[dev]'
```

この Mac の Python 3.11.2 では `pip` と通常の `venv` 作成が強制終了したため、実際の検証には `uv` で仮想環境を作成して `uv pip install --python .venv/bin/python '.[dev]'` を使用しました。

## Environment variables

`.env.example` を `.env` にコピーして設定します。環境変数は `.env` より優先されます。`.env` と `data/` は Git の追跡対象外です。

| Variable | Default | Purpose |
| --- | --- | --- |
| `JARVIS_ENVIRONMENT` | `development` | `development`, `test`, `production` のいずれか |
| `JARVIS_LOG_LEVEL` | `INFO` | Python の標準ログレベル |
| `JARVIS_DATA_DIR` | `./data` | ローカルデータの保存先 |
| `JARVIS_DATABASE_PATH` | `<data dir>/jarvis.sqlite3` | SQLite ファイル |
| `JARVIS_LLM_PROVIDER` | `openai` | LLM アダプターの選択 |
| `JARVIS_LLM_MODEL` | `gpt-5.5` | 使用するモデル名 |
| `OPENAI_API_KEY` | 未設定 | OpenAI アダプターを使用する場合に必要 |

相対パスはサーバーを起動した作業ディレクトリを基準に解決します。リポジトリのルートから起動する例を以下に示します。

## Run

```sh
.venv/bin/uvicorn backend.api.app:app --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/health
```

開発中の API はローカルホストにだけ公開します。外部端末からのアクセスには認証と安全な通信の実装が必要です。

## Test

```sh
sh scripts/test.sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m compileall -q backend tests scripts
```

この作業環境では pytest と FastAPI を同じプロセスで読み込むと Python プロセスが強制終了する場合があります。`scripts/test.sh` はユニットテストをファイルごとに実行し、API は別プロセスでスモークチェックします。CI では全テストの一括実行も確認します。

LLM アダプターは [OpenAI の公式ドキュメント](https://developers.openai.com/api/docs/guides/text) に従って Responses HTTP API を使用します。送信した response の API 保存は `store=False` で無効にします。API キーがなくてもヘルスチェックとテストは実行できます。

開発順序と最初のタスクは [docs/development.md](docs/development.md) を参照してください。
