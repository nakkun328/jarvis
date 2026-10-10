# 実ローカルencoderによる日本語検索試験 — 2026-10-04

全て架空note/query、外部embedding/chat API0。E5 CPU float32を既存#37 runnerへ注入した。
模型的fake vectorの成功と区別し、`evidence_kind=model`。実モデルが動作した証拠であり配備採用/品質合格ではない。

実験source `dbb85298cd3cb8c5e92f5ead0ac337333e7fc560`（clean）。base `d18aefc3ed0b2c26c588f6ff1a5738c194f8c095`。実行9.55秒（process起動から終了、download除く）。モデル取得34.70秒、492794646bytes。CPU/メモリ/空き容量は[手順](../local-embedding-trial.md)。

モデル `intfloat/multilingual-e5-small`、revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`、dimension384。契約 `intfloat/multilingual-e5-small@614241f622f53c4eeff9890bdc4f31cfecc418b3-d5a845578979b21cdef5260843c0bf1f8a7f00abc1d7a3eb995ce4ab9164d1f5:d384`。query/document prefix、pooling、normalization、最大tokenと固定runtime版をhashでspaceへ束縛する。dataset `jarvis-synthetic-ja-v1` / hash `915366e1a160900992a4ee579dc3bae0c22d4530f0c5086020965549e87be230`、note11/query12、k=3。独立gold不変。

2回それぞれfresh temporary SQLite/vault/Chromaを再作成し、実document encoder→index構築→実query encoder→候補→現在approved正本を実行。completed2/failed0、除外違反0。superseded/retired/pending/conflict IDsを故意にindexに残したchallengeでもそれらをfactとして返さない。旧正本ファイルは全保持。訂正後ESP32、編集後水曜19時の現在本文を取得し、provenance/confidenceを保持した。

## query別の代表結果（trial1）

期待は元のrelevant gold、回答の根拠は元のsupporting goldが実上位結果にあるか。回答生成の正しさを試していない。

| query | 期待する記憶ID | 実上位3 ID（順序） | 回答を支える根拠 |
| --- | --- | --- | --- |
| あの星を観測する試作機、制御用のチップを何に替えたっけ？ | robot-current | robot-current, design-paper, lens-storage | あり（robot-current） |
| 工作の設計を書き留めるとき、どんな紙と筆記具を使う？ | design-paper | design-paper, data-format, lens-storage | あり（design-paper） |
| 星を見る架空の集まりは何曜日の何時から？ | observing-time | observing-time, garden-water, design-paper | あり（observing-time） |
| 菜園に自動で水をやるのは朝の何時から、どれくらい？ | garden-water | garden-water, observing-time, design-paper | あり（garden-water） |
| 光学実験の交換用レンズを探している。どこにしまった？ | lens-storage | lens-storage, design-paper, robot-current | あり（lens-storage） |
| 温度測定の結果はどのファイル形式で、何を記録する？ | data-format | data-format, design-paper, lens-storage | あり（data-format） |
| 観測試作機は今もRP2040で動かすことになっている？ | robot-current | robot-current, design-paper, garden-water | あり（robot-current） |
| 架空王国の鉄道の始発時刻は何時？ | なし | observing-time, garden-water, data-format | なし |
| 観測試作機ミズキの電池は何時間使える？ | robot-current | robot-current, garden-water, design-paper | なし |
| 設計メモに使う方眼紙の購入価格はいくら？ | design-paper | design-paper, robot-current, lens-storage | なし |
| 廃止した工作会の会場はどこだった？ | なし | design-paper, lens-storage, observing-time | なし |
| 採用保留の青い塗料にはどんな成分が入っている？ | なし | robot-current, design-paper, garden-water | なし |

制御チップの言い換え質問で最上位は `robot-current`:
「訂正：架空の観測試作機ミズキの制御基板はESP32へ変更済みで、RP2040ではない。」
集まりの曜日/時刻で最上位は `observing-time`:
「架空の星見会は水曜日の19時に始まる。」（編集後の正本）

## 指標と限界

両試行でpositive goldのP@3=1/3、Recall@3=1、MRR=1、support goldのsupport recall=1。
各positive queryのrelevant goldが1件でtop3を返すため、P@3は1/3になる。関連性と根拠の分母を混同しない。
今回2回の候補/rankは同じだったが、有限観測であり一般的なANN安定性や運用品質を証明しない。
実行失敗0。人工ケースは少数で、独立goldのレビュー・未見例・長文・分布差・実運用の品質は未確立。

negative3問は両試行ともcontextが非空、根拠不足2問も両試行とも関連contextを返す。
電池の持続時間や方眼紙の価格をその記憶から回答できるわけではない。top-kは回答可能性判定ではなく、
現policyを勝手にabstention/thresholdへ変更しない。quality thresholds=null、quality_assessment=not_established。
推奨はE5を次の未見人工gold比較用候補に残すこと。配備モデル採用・品質基準・根拠不足回答方針はJAR-88で別判断。
MiniLMは公式カード・資源・前処理比較だけで実行しておらず、実測の優劣を主張しない。

[機械可読の全2試行report](e5-ja-v1-model-2trials.json)に全query/gold/上位本文/provenance/confidence/revision/scoreと指標分布を保存。
[取得manifest](e5-download-manifest.json)に固定取得元/revisionと全fileサイズ/SHA256を保存。weights/cacheはcommitしない。
後続のdocs/tests-only commitでは、このexperiment sourceのruntime/gold/deps/config/runnerをblob照合してcomponent証跡として再利用する。
新headの全検証/CI成功とは区別する。以前のfake報告は任意SHA256 vectorによるrunner/安全性試験であり、モデル品質比較値にしない。
