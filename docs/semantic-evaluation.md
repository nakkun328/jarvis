# 日本語の意味検索評価

この runner は人工データで検索結果と gold を比較する。実ユーザーの DB/vault は受け取らず、毎回一時 SQLite/vault/Chroma を作成する。承認・訂正・退役は fixture に対する明示的な操作で、アプリの自動承認機能を追加しない。

```sh
python scripts/evaluate_memory.py --output /tmp/jarvis-ja-report-new.json
```

既存 report は上書きしない。既定の SHA256 由来の arbitrary fake vector は日本語モデルではなく、runner の契約・正本への解決・除外安全性を検証する。exit 0 は実行完了と除外違反なしを表し、検索品質の合格ではない。report の `quality_assessment` は常に `not_established`、`quality_thresholds` は null。

`tests/fixtures/semantic-evaluation-ja-v1.json` は架空の日本語 note 11 件、query 12 件。言い換え・無関係・根拠不足・訂正・正本編集・退役・未承認を含む。承認済み 6 件に対して既定 k=3 のため、全候補が返るだけで recall が満点になる構成を避ける。古い・未承認 ID を index に残す challenge に対し、現在の承認済み正本だけを解決する。古い正本ファイルは削除しない。

`relevant_ids` は話題に関連する gold、`supporting_ids` は質問への回答を裏づける gold。例えば「制御基板は ESP32」という記憶はバッテリー持続時間の話題に関連しても、その数値の根拠にはならない。gold と rationale は fixture 側で独立にラベル付けし、検索結果から生成しない。人工ケースは実運用データ分布を代表する証拠ではなく、gold 自体もモデル比較前にレビューする。

report は dataset の canonical hash とファイル hash、runner commit/dirty/hash、EmbeddingSpace の name/version/dimension、query/gold/現在本文/source/origin/confidence/revision/score、指標と除外違反を記録する。score は既存 index の値で、provider をまたぐ比較値や確率として扱わない。P@k は configured k、Recall@k は関連 gold 数、MRR は最初の関連 rank、support recall は supporting gold 数を分母とする。関連 gold がない query は順位平均から除き、nonempty context を別診断にする。根拠不足は関連性と分けて数える。回答生成・正しさ・abstention の評価は含まない。

入力・gold・契約と実行環境を再現できるよう Python/platform/Chroma 版も記録する。Chroma の近似検索は同じ入力でも fresh index 間で候補・rank が完全一致するとは限らない。実 Linux CI でも候補一部の欠落が観測されたため、実際の rank/指標をそのまま残し、report の一致や recall=1 を実行成功条件にしない。品質を比較する際は複数試行と index 側の recall の切り分けが別途必要で、今回その評価方針を確定しない。

既存 `EmbeddingProvider` を返す同期 factory を明示的に差し替えられる。

```sh
python scripts/evaluate_memory.py --provider-factory local_adapter:make_provider \
  --evidence-kind model --limit 3 --output /tmp/jarvis-model-report-new.json
```

factory はローカルでレビューしたコードを指定する。runner は `.env` を読み込まず provider を自動選択しない。factory 内のモデル設定や接続は呼び出し側が管理する。CLI は取得した provider の optional async `aclose` を成功・失敗・取消時に呼ぶ。core の `evaluate` へ注入した provider は呼び出し側所有。契約出力の数・有限値・次元を既存経路で検証し、障害の本文・credential・path を report に転記しない。部分結果は failed 状態で残る。

今回は fake のみで、実 API 呼び出しはない。OpenAI 実 API は pending、必要な将来の標準は Gemini / gemini-2.5-flash。配備 provider/model、品質しきい値、無関係 query の abstention、重要度尺度、一般抽出・意味的統合の方針は未決定。現在の top-k が根拠のない質問にも context を返すことを診断として可視化するが、chat/search policy は変更しない。JAR-88 で選択肢と影響を判断し、未着地 index stack #18→#21→#24→#26→#29 を前提に Draft としてレビューする。
