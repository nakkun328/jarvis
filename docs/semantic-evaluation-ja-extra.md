# 追加の人工日本語評価 v1

`tests/fixtures/semantic-evaluation-ja-extra-v1.json` は既存 ja-v1 の12問とは別の人工corpus/goldで、16 note・25 queryを含む。既存fixture・runner・アプリ設定は変更しない。結果を得る前に本文とgoldを固定し、統括が人工本文と照合してから推論する。固定記録は [gold lock](evidence/ja-extra-v1-gold-lock.json)。この固定時点ではfake/実モデルとも検索結果を見ていない。

Canonical SHA256: `015533847686661625ddd52bbf7375a4a70fa58a28dcc0296af16d7fd1fb15b5`

Fixture file SHA256: `5ee1f477bd9ce2b22f488eabfe5293a86e2a1f0cb18ad9ed29778693e08fb06a`

固定commitはfixtureを追加したcommitで確認できる。goldを結果に合わせて書き換えない。事実・ラベルの誤りが判明した場合はv1を保持し、理由と修正版のversion/digestを別に記録する。結果reportはこの固定hashとの一致を確認する。

## goldの意味

`relevant_ids` は現在承認済みのnoteのうち、質問の対象や話題に関係するもの。似た名前の別機体・別の会・別測定のnoteを、単に共通語があるという理由で含めない。`supporting_ids` はその中で、問われた項目を直接裏づけるnote。各queryの `rationale` は現在本文から期待値を説明する。

対象はミズキ／ミツキ、星見会／星見工作会、レンズ／ミラー、温度／照度で区別する。ミズキは訂正後ESP32、ミツキはSTM32。星見会はindex作成後の編集で水曜19時、星見工作会は土曜14時。レンズは棚の右端の箱、ミラーは左端の袋。温度はCSVと摂氏温度、照度はTSVとルクスである。

2種類を同時に問う3問では各noteが自分の項目を支持し、両noteが揃って初めて全項目の根拠が揃う。runnerのsupport recallは取得したsupporting gold数の割合であり、回答生成や両項目を正しく結び付けたことは評価しない。

電池持続時間・設計用紙の価格・レンズ洗浄液・雨天時の中止・温度測定間隔の5問は関連noteを持つがsupportingは空。本文は各項目を未記録と明記する。質問は欠けた値や判断を尋ねているため、「未記録」という記述はその値・判断の支持goldではない。電池2時間という未承認候補も根拠にはしない。

無関係2問、退役会場1問、未承認だけに存在する塗料とハルカ2問はrelevant/supportingとも空。歴史を問われても、このrunnerの現在approved-only検索に退役noteを復活させない。退役noteの保持や履歴を別の入口で読むことの可否は変更しない。

## 固定した構成

| 観点 | query数 | 確認内容 |
| --- | ---: | --- |
| paraphrase | 12 | 言い換え、似た名前の区別、複数noteを必要とする3問 |
| revision | 1 | index作成後に編集した水曜19時の正本 |
| correction | 2 | 旧RP2040と未採択の木曜20時を現在factに戻さない |
| insufficient_evidence | 5 | relevantがあってもsupportingがない |
| unrelated | 2 | 鉄道・調理はcorpusにない |
| retirement | 1 | 港の倉庫の退役noteを現在contextへ戻さない |
| unapproved | 2 | 塗料・別機体ハルカのpending candidateをfactにしない |

現在approved noteは10件で、既定k=3より多い。除外対象はsuperseded1、retired1、pending3、conflict1。runnerはこれらのIDsを意図的にindexへ残す／挿入するため、現在正本への解決と除外を検索品質とは別に確認できる。

## 実行と記録

推論の所有者は統括のみ。既存の固定revision E5・専用環境・cacheを使用し、install/downloadを追加せず、CLI実モデル試験と同時実行しない。レビュー後に既存runnerの `--fixture` だけで追加セットを選ぶ。

```sh
export JARVIS_LOCAL_MODEL_CACHE=/private/tmp/jarvis-p2-local-encoder-20261004/model-cache
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
export OMP_NUM_THREADS=2 MKL_NUM_THREADS=2
/private/tmp/jarvis-p2-local-encoder-20261004/venv/bin/python scripts/evaluate_memory.py \
  --fixture tests/fixtures/semantic-evaluation-ja-extra-v1.json \
  --provider-factory backend.memory.local_embedding:make_provider \
  --evidence-kind model --limit 3 --trials 2 \
  --output /tmp/jarvis-e5-ja-extra-v1-report-new.json
```

既存reportを上書きしない。モデルrevision・契約・現在の実行source/depsはrunner reportと既存 [ローカルembedding手順](local-embedding-trial.md) に従って照合する。再現にはこのstackのコードと既存optional依存が必要。上のpathは今回の専用環境であり、別環境では明示的に対応するpathを指定する。

2試行の全25問について候補ID・順位・現在本文・revision・source/confidenceとgoldを保存し、除外違反、時間、失敗、関連性と支持根拠の取得を報告する。既存ja-v1の指標と混ぜて平均しない。検索候補が非空であることは回答可能性の保証ではない。無関係・根拠不足のcontextは診断として記録し、threshold・abstention・配備方針を新設しない。`quality_assessment=not_established` と `quality_thresholds=null` を維持する。少数人工25問と有限2試行は、一般品質やANNの一般的安定性の合格証拠にならない。
