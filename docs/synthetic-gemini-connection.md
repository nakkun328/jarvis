# 人工 E5 → Gemini 隔離接続試験（Draft）

通常アプリの provider / lexical 既定は変更しない。#38 の固定 E5 と人工 corpus 準備、#36 の現在正本再確認と bounded context、#31 の既存 Gemini factory / REST adapter、既存 ChatService / SQLiteConversationStore を接続する明示 CLI。任意 DB / vault / fixture / 質問 / provider は引数に受け付けない。実ユーザー資料、OpenAI API、モデル download、ブラウザへのキー配布はない。

4 問は結果前の commit `d9cf0d4` の [固定質問](../tests/fixtures/synthetic-gemini-questions-v1.json) にある。既存 extra-v1 の人工資料・goldを変更せず使う。訂正後ミズキの ESP32、別機体ミツキの STM32、電池持続時間の根拠不足、無関係な架空鉄道を問う。ミズキ／ミツキの順位は現状観測として記録し、結果に合わせて質問や gold を変更しない。

軽量契約試験（fake vector と fake HTTP。日本語検索品質・回答品質・実 API の証拠ではない）：

```sh
/private/tmp/jarvis-p2-local-encoder-20261004/venv/bin/python \
  scripts/verify_synthetic_gemini.py --contract-only \
  --output /tmp/jarvis-synthetic-gemini-contract-new.json
```

実試験は統括のみが、他の実 E5 process がない状態で一度実行する。既存の server-side `GEMINI_API_KEY` が利用可能なときだけ API を呼ぶ。`.env` を読み込まず、資格情報の探索や入力要求は行わない。キーなしならモデル起動せず pending / request_attempts=0 の JSON を残す。

```sh
export JARVIS_LOCAL_MODEL_CACHE=/private/tmp/jarvis-p2-local-encoder-20261004/model-cache
export JARVIS_GEMINI_MODEL=gemini-2.5-flash
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
export OMP_NUM_THREADS=2 MKL_NUM_THREADS=2
/private/tmp/jarvis-p2-local-encoder-20261004/venv/bin/python \
  scripts/verify_synthetic_gemini.py --live --max-output-tokens 4096 \
  --output /tmp/jarvis-synthetic-gemini-live-new.json
```

各 question は新会話、最大 4 attempt、API の自動再試行なし。失敗を直すための再起動も追加 live request なので、今回の最大 4 リクエスト許可を超えて再試験しない。新規 report path を専用 mode 0600 で作り、既存 evidence / symlink を上書きしない。正常 exit0、質問・準備・cleanup 失敗 exit1、引数・cache・出力 path の問題 exit2、取消 exit130。close 後に worker 終了を待ち、利用可能な Chroma client.close を呼んで一時 SQLite/vault/index を除去する。

`--max-output-tokens` は 1〜8192 の明示 cap（既定1024）。REST の `generationConfig.maxOutputTokens` を毎 request に設定し、通常 provider の指定なし動作は保持する。[公式 generateContent](https://ai.google.dev/api/generate-content) と [公式 thinking](https://ai.google.dev/gemini-api/docs/generate-content/thinking) を 2026-10-04 に確認した。上限は思考 token も含み、2.5 Flash の既定 dynamic thinking は変更しない。上限で空／途中出力や MAX_TOKENS があり得る。既存 adapter は STOP 以外を失敗にする。試験側 observer は安全な応答 text / finish reason / 数値 usage だけを失敗と併記し、headers、URL、上流 error payload、thought text は記録しない。

記録には実 source HEAD / dirty、fixture / plan digest、embedding space、送信 bounded JSON 実文、各 excerpt と現在正本全文、ID / revision / 出典 / origin / confidence、候補順位・score、回答・安全な failure・履歴保存数がある。既存 context 上限（最大3件、本文500字、出典200字、JSON2400字）を再利用する。score は confidence や回答可能性ではない。現在正本の再確認失敗なら provider を呼ばず、provider 失敗ならその会話履歴を保存しない。

既存 response contract は使用 source の構造化 attribution を返さない。`sources_provided` は渡した出典、`explicit_source_mentions` は回答に文字通り現れた出典だけであり、実利用の証明ではない。`used_sources=null` と `manual_fact_to_source_review_required` を残す。統括は raw 回答の各事実を `input_context[].canonical.current_content` と固定 gold に対応づけ、使用根拠と支持不足・別対象混同・無関係への断定・provider failure を別の照合記録に残す。実回答・途中回答の原記録を変更しない。

既存 SYSTEM_PROMPT、回答方針、abstention をそのまま試す。検索候補が返っても回答の支持根拠があるとは限らない。新 threshold、品質合格基準、独自 abstention は導入しない。`quality_assessment=not_established` と `quality_thresholds=null` を維持する。4 問は接続と人工事実の限定検証であり、一般回答品質や配備採用の証明ではない。
