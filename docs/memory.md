# Memory design

Phase 2 で user、project、conversation、work state、temporary、self memory を区別します。人間が読める長期記憶は Obsidian Markdown、会話・タスク・実行履歴とメタデータは SQLite、意味検索は比較検証後に選ぶ Vector DB に置きます。

記憶には出典、時刻、重要度、confidence、タグ、project を保持できるモデルを用意します。推測を確定事実として保存せず、矛盾する記憶は無条件に上書きしません。検索時には出典と更新時刻を確認します。Phase 0 では記憶機能は未実装です。
