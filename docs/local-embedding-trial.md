# ローカル日本語 embedding 試験（Draft）

人工データだけで実encoderからindex構築・言い換え検索・現在の承認済み正本解決まで試す。
配備モデルの採用、アプリ既定lexicalの変更、外部embedding API呼出しは行わない。
このPRは未着地の評価#37とindex/search stackをfeature baseに持つ。mainへまとめてpushしない。

## 候補と固定条件

試験環境はApple M2 / メモリ8 GiB / 取得前空き57 GiB。CPU float32、batch最大8。
モデルweightsは約471 MB、tokenizer等を含む取得は約493 MB。依存・cacheにも別途容量が必要。
GPU不要だがOS/他アプリのメモリ余裕を見て逐次試験する。

| 候補 | 日本語・条件 | 資源と前処理 | 判断 |
| --- | --- | --- | --- |
| [intfloat/multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small/tree/614241f622f53c4eeff9890bdc4f31cfecc418b3) | 100言語、日本語を含む。MIT。revision `614241f622f53c4eeff9890bdc4f31cfecc418b3` | 約118M parameters、384次元、512token。attention-mask mean pooling、L2、query `query: ` / document `passage: `（日本語でも英語prefix） | まず実行するretrieval候補。配備未採用 |
| [paraphrase-multilingual-MiniLM-L12-v2](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2/tree/e8f8c211226b894fcb81acc59f3b34ba3efd5f42) | 50言語、日本語を含む。Apache-2.0。revision `e8f8c211226b894fcb81acc59f3b34ba3efd5f42` | 同程度118M/384次元、公式SentenceTransformer設定128token、mean pooling。対称的sentence similarity向け、role prefixなし | 予備候補。公式カード・設定比較のみで、実測比較済みではない |

[E5公式実装例](https://huggingface.co/intfloat/multilingual-e5-small/blob/614241f622f53c4eeff9890bdc4f31cfecc418b3/README.md)
に合わせ、標準Transformers AutoTokenizer/AutoModelのみを利用する。
`trust_remote_code=False`、`use_safetensors=True`、`token=False`。モデル付属の任意コードを実行しない。
前処理、model/revision、dimension、pooling/L2、token limit、CPU float32、固定実装版を
immutable contractへ含め、hash付きEmbeddingSpaceを使用する。どれか変えると別spaceになる。
torch2.9.1/transformers4.57.6以外の実装版はこの契約で推論しない。
長文は512tokenでtruncateされ、品質への影響は未評価。

## 再現手順

このDraft branchの専用venvで実行する。実DB/vaultや.envは不要。
downloadは明示的な一度の準備で、以後はローカルcacheだけを読む。

```sh
python3.11 -m venv /tmp/jarvis-local-e5-env
/tmp/jarvis-local-e5-env/bin/python -m pip install -e '.[dev,vector,local-embedding]'
/tmp/jarvis-local-e5-env/bin/python scripts/download_local_e5.py \
  --cache-dir /tmp/jarvis-local-e5-cache \
  --manifest /tmp/jarvis-e5-download-new.json
export JARVIS_LOCAL_MODEL_CACHE=/tmp/jarvis-local-e5-cache
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
export OMP_NUM_THREADS=2 MKL_NUM_THREADS=2
/tmp/jarvis-local-e5-env/bin/python scripts/evaluate_memory.py \
  --provider-factory backend.memory.local_embedding:make_provider \
  --evidence-kind model --limit 3 --trials 2 \
  --output /tmp/jarvis-e5-ja-report-new.json
```

出力は新規pathを指定する。cache/weightsはGit外へ置き、commitしない。
既存#37 runner/fixture/goldをそのまま使う。毎試行一時SQLite/vault/Chromaで人工noteを再作成し、
explicit fixture review後に実document encoder→index構築、query encoder→近傍候補→正本解決を実行する。
訂正・退役・pending/conflictの古いIDsを意図的にcacheへ残し、除外を確認する。
編集後の結果は現在noteであり、cached本文をfactとして使わない。
データ/goldを書き換えて指標を上げない。旧EmbeddingProviderはembed経路を維持し、
optional `embed_query` がある場合だけquery用前処理へ分岐する。両roleのvector契約検証は共通。

reportにquery・期待するrelevant/supporting IDs・実上位結果/本文/provenance/confidence/revision・
モデル/契約・指標・開始終了時刻・失敗を記録する。根拠の有無はsupporting goldと照合する。
回答生成/正しさ/UI/abstentionは試験しない。scoreは確率やモデル間比較値ではない。
有限2試行は観測したANN変動を診断するだけで、一般的安定性の証明にしない。
`quality_assessment=not_established`、quality thresholds=nullを維持する。
モデルが動いたこと、実行成功・除外0、十分な品質は別に記録する。

## CIと保護

通常CIはdev/vectorだけを導入し、download/inferenceを必須処理にしない。
軽量fake encoderでquery/document役割・契約space分離・invalid出力・安全な失敗/retry・closeを検証する。
実モデル試験証跡は別に保存する。モデル導入はこのoptional factoryの明示操作だけで、既定設定を変えない。
closeは取消済みCPU threadの終了を待ってmodelを解放する。
既存approved-only/current canonical/provenance/confidence/context上限・lexical・失敗時保護を維持する。
OpenAI実APIpending、#31 Draft。localhost:8765の既存Gemini環境を操作しない。

## 実測記録

実測のquery別結果・指標・時間・限界は、この試験のreportとJAR-25/JAR-88へ記録する。
fakeとmodelはevidence_kindで区別し、配備encoderと品質基準は引き続き判断待ち。
