# 人工記憶の手入力検索（Draft）

一度の起動で既存の人工日本語評価fixtureを一時SQLite/vault/Chromaへ準備し、複数の質問を手入力できます。実DB/vault、任意fixture、外部providerの引数は受け付けません。アプリ設定や既定lexical検索を変更せず、LLM回答生成・外部API呼び出しは行いません。

[ローカルE5の事前準備](local-embedding-trial.md)で作成した専用環境と固定revision cacheを指定します。このCLIはdownloadしません。専用環境/cacheは一人が管理し、実モデル推論は評価runnerを含め一度に1プロセスとしてください。

```sh
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
export OMP_NUM_THREADS=2 MKL_NUM_THREADS=2
/tmp/jarvis-local-e5-env/bin/python scripts/search_synthetic_memory.py \
  --cache-dir /tmp/jarvis-local-e5-cache --limit 3
```

`--cache-dir`の代わりに`JARVIS_LOCAL_MODEL_CACHE`を使えます。欠損cacheや不完全なsnapshotは安全に失敗し、自動取得やfakeへの自動切替はありません。モデルなしの操作確認だけなら`python scripts/search_synthetic_memory.py --contract-only`（vector依存が必要）。これは任意fake vectorによる契約確認で、日本語semantic品質の証拠にはなりません。

次の質問を続けて入力して、返った候補の本文を確認してください。

- 訂正: `あの星を観測する試作機、制御用のチップを何に替えたっけ？`（人工正本robot-currentはESP32、旧RP2040は承認済み候補に出さない）
- 編集: `星を見る架空の集まりは何曜日の何時から？`（人工正本observing-timeは水曜日19時、承認後の編集=true）
- 無関係: `カナダの首都はどこ？`
- 根拠不足: `星を観測する試作機のバッテリーは何時間持つ？`（人工本文に持続時間の根拠はない）

候補が返ることは回答可能性の保証ではありません。無関係・根拠不足でもtop-kが非空になり得ます。関連性と質問への支持根拠を別々に確認してください。scoreは確率・confidenceではなく、配備モデル・品質threshold・回答方針は未採用です。上記は試すための質問とfixtureの事実であり、特定の順位を保証する説明ではありません。

各候補に現在approved本文、出典、origin、revision、confidence、importance、score、承認後の編集、stale、人工fixtureの訂正元名を表示します。検索は既存SemanticMemorySearcherを使い、index IDを現在の承認済み正本へ解決します。既存評価runnerと同じ訂正・退役・未承認・編集challengeの準備処理を再利用し、元fixture/goldを変更しません。旧正本は一時corpus内で保持します。

`/quit`または`/exit`で終了します。空入力は検索せず再入力、4000文字超も拒否します。EOFは通常終了、Ctrl-Cは取消（exit130）。正常終了はexit0、準備・検索・closeの失敗はexit1、欠損cache/不正引数はexit2です。推論失敗の生例外やpathは表示せず、次の質問または終了を選べます。終了・失敗・取消でproviderのcloseを呼び、一時データを削除します。stdin待機workerを残さず、CPUモデルのcloseは進行中の推論threadの終了を待ちます。worker終了を待ってから利用可能なChroma client.closeを呼び、一時ファイルを削除します。Chromaのプロセス内資源はCLIプロセス終了時にも解放されます。
