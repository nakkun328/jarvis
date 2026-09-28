# Architecture

JARVIS は単一の論理的なアシスタントとして設計します。端末ごとに人格、記憶、タスクを複製しません。初期段階では一つの FastAPI サーバーと SQLite を使い、Core は OS 固有の操作から切り離します。

```text
Web UI / future device clients
             |
        FastAPI API
             |
      JARVIS Core
      |    |     |
   Memory Tasks Orchestrator
                 |      |
              Tools  LLM Provider
```

現在実装済み: 設定の読込と検証、ログ初期化、SQLite 接続とスキーマ版管理、API ヘルスチェック、LLM プロバイダーの共通インターフェースと OpenAI アダプター。機能モジュールは Phase ごとに追加します。FastAPI の起動時にデータベースを初期化し、起動不能なスキーマは明示的にエラーにします。

API は将来的に複数端末が同じサーバーへ接続する境界です。端末上の実行エージェントと Core の通信、認証、同期方式は Phase 6 で設計・検証します。
