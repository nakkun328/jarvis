# Development plan

## 現状分析（2026-09-29）

開始時の worktree `Orca/jarvis-v0.1` は `README.md` と `.gitignore` のみで、作業ツリーに未コミット変更はありませんでした。Python 3.11.2、Git 2.39.5、SQLite 3.39.4 を確認しました。既存の Python 仮想環境と依存パッケージはありませんでした。既存の feature worktree で開発し、main は変更しません。

## Phase 0 implementation plan

1. **P0-01 Repository / environment**: Python パッケージ、依存関係、`.env.example`、設定の入力検証、gitignore、セットアップ手順。設定の優先順位と不正値をテストする。
2. **P0-02 API / logging / SQLite**: FastAPI アプリケーション factory、ヘルスチェック、ログ、SQLite 接続、初期スキーマ版管理。起動と障害時の応答をテストする。
3. **P0-03 LLM provider layer**: 共通インターフェース、クラウドプロバイダーのアダプター、鍵の安全な扱い、テスト用 fake。プロバイダーの交換と失敗時の処理をテストする。
4. **P0-04 Quality gate / docs**: tests、構文・型・lint、secret scan、diff review、設計・運用ドキュメント。小さな変更単位でコミットし、PR 作成前にゲートを再実行する。

P0-01、P0-02、P0-03 の最初の実装はこの worktree にあります。各タスクは実装・テスト・エラー処理・秘密情報・既存機能・ドキュメントを確認してから完了とします。

## Phase sequence

| Phase | Outcome |
| --- | --- |
| 0 Foundation | API、設定、SQLite、プロバイダー抽象化、品質ゲート |
| 1 Basic JARVIS | 会話 API、人格、文脈、streaming、基本 UI |
| 2 Memory | 会話と種類別記憶、Obsidian、意味検索、統合 |
| 3 Research | 検索計画、ソース評価、照合、引用、深い調査 |
| 4 Task / Tools | Tool registry、権限、キュー、実行検証 |
| 5 Productivity | カレンダー、課題、プロジェクト、GitHub、ファイル |
| 6 Multi Device | 端末登録、同期、リモート実行 |
| 7 Voice / Home | 音声と家庭内連携 |

各 Phase を複数の小さな PR に分けます。PR 前には tests、構文・型、lint、secret scan、diff review を行い、main への merge は明示的な許可を待ちます。
