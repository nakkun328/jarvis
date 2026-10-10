# 追加人工日本語 v1：固定E5・実モデル2試行

追加25問では全relevant/supporting goldをtop3内で取得したが、3問で似た対象の別noteが正対象より上位になった。無関係・退役・未承認だけの5問と、関連はあるが支持根拠がない5問は、両試行とも全て非空contextだった。実行完了と除外違反0は確認したが、一般品質は未確立であり合格・不合格の品質判定は行わない。

この結果は既存ja-v1の12問と別corpus/goldの実測で、平均を混ぜない。[固定gold・判定方針](../semantic-evaluation-ja-extra.md)、[推論前lock](ja-extra-v1-gold-lock.json)、[全2試行の機械可読結果](e5-ja-extra-v1-model-2trials.json)を参照。JSONは全50 queryのscore、現在本文、出典、origin/confidence、revision、編集状態を保持する。

## 条件と固定証跡

- gold固定commit: `6221fe3cc5e22e67c5b3389333427c93d2b496e7`。統括は2026-10-04 07:01:42 UTC、結果未見の状態で16 note・25 queryを現在本文と照合し、推論前承認。実装agentも同snapshotを独立に本文照合し必須修正なし。
- 実行source: `cf9e96a7d59651b24ae83a937cb986f45c7c4e15`、runner dirty=false。統合後のCLI準備helperを再利用した既存runner、`--fixture`で追加セットを指定。
- Canonical SHA256: `015533847686661625ddd52bbf7375a4a70fa58a28dcc0296af16d7fd1fb15b5`。fixture file SHA256: `5ee1f477bd9ce2b22f488eabfe5293a86e2a1f0cb18ad9ed29778693e08fb06a`。両値はgold-lock、承認記録、実行reportと一致し、goldは結果取得後も不変。
- Report file SHA256: `fe1104811618b204227c0bb6babb2b6cb32906b508870041366e8d515b2d665a`。
- Encoder: `intfloat/multilingual-e5-small` revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`、384次元。契約hash `d5a845578979b21cdef5260843c0bf1f8a7f00abc1d7a3eb995ce4ab9164d1f5`。固定CPU float32・query/passage前処理等は[ローカルembedding手順](../local-embedding-trial.md)に記載。
- Darwin / Python 3.11.11 / Chroma 1.5.9、k=3。統括のみが既存専用venv/cacheでoffline推論を実施し、download・外部embedding/chat API・LLM回答生成なし。
- 同じprovider instanceと契約でfresh temporary SQLite/vault/Chromaを2回逐次作成。実DB/vaultを使わず、superseded/retired/pending/conflictの6 IDを意図的に残すchallenge。承認済みは10 note。

## 時間・失敗・指標

| 試行 | 開始UTC | 終了UTC | report区間秒 | 完了query | 失敗 | 除外違反 |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| 1 | 2026-10-04T07:05:19.938883+00:00 | 2026-10-04T07:05:43.590139+00:00 | 23.651256 | 25 | 0 | 0 |
| 2 | 2026-10-04T07:05:43.590831+00:00 | 2026-10-04T07:05:46.604005+00:00 | 3.013174 | 25 | 0 | 0 |

2試行全体のreport区間は 26.671215 秒、統括のprocess計測はreal 29.40秒（user 10.10秒、sys 4.93秒）。初回はlazy model loadを含み、2回目は同instanceを再利用するため、この時間差を独立した同条件のレイテンシ比較にしない。queryごとの時間はrunnerが記録していない。失敗試行・部分結果・除外された試行はいずれも0。

| 指標 | 試行1 | 試行2 | 分母・範囲 |
| --- | ---: | ---: | --- |
| Mean P@3 | 0.3833333333 | 0.3833333333 | relevantあり20問。hit数 / configured k=3 |
| Mean Recall@3 | 1 | 1 | relevantあり20問。hit数 / relevant gold数 |
| Mean MRR | 0.925 | 0.925 | relevantあり20問。最初の関連rankの逆数 |
| Mean support recall@3 | 1 | 1 | supportingあり15問。hit数 / supporting gold数 |
| relevantなしで非空context | 5/5 | 5/5 | 無関係2・退役1・未承認2 |
| 根拠不足で非空context | 5/5 | 5/5 | relevantあり・supporting空の5問 |
| 除外違反 | 0 | 0 | 全25問のinactive/unreviewed ID返却数 |

negativeはP/Recall/MRR平均から除外、supporting空はsupport recall平均から除外される。従ってRecall=1とsupport recall=1は根拠のない10問を回答可能と評価した値ではない。両試行とも全25問の候補集合・順位が一致し、集計指標の観測population stdevは0。ただし有限2試行でANNの一般的安定性は証明しない。

## 全25問の固定goldと本文根拠

Rはrelevant、Sはsupporting。∅は現在承認済みgoldなし。以下のquery文・期待ID・説明は推論前fixtureをそのまま表示し、結果でラベルを調整していない。

| Query ID | 質問 | R | S | 人工現在本文からの根拠 |
| --- | --- | --- | --- | --- |
| `mizuki-controller-paraphrase` | ミズキという星を観測する試作機、頭脳にあたる制御基板を何に替えた？ | `mizuki-current` | `mizuki-current` | ミズキの現在本文はESP32への変更を明記する。ミツキのSTM32は名前が似ても別機体であり、この質問の根拠にはならない。 |
| `mitsuki-controller-disambiguation` | ミズキではなくミツキの方は、どの制御基板を使っている？ | `mitsuki-controller` | `mitsuki-controller` | 対象はミツキ。承認本文のSTM32が直接の根拠であり、ミズキのESP32や旧RP2040は回答対象が異なる。 |
| `paper-tools-paraphrase` | 工作室で設計の覚え書きを残すとき、紙の種類と書く道具は何？ | `design-paper` | `design-paper` | 設計メモという用途と覚え書きの言い換え。承認本文に厚手の方眼紙と鉛筆の両方がある。価格情報は不要。 |
| `observing-current-revision` | 工作をする方ではない星見会は、今は何曜日の何時に始まる？ | `observing-time` | `observing-time` | 編集後の承認正本は水曜日19時。index作成時の火曜19時、矛盾候補の木曜20時、別会の土曜14時は現在の星見会を裏づけない。 |
| `craft-time-disambiguation` | 星見会と名前が似た星見工作会の開始日時を教えて。 | `craft-time` | `craft-time` | 対象は星見工作会であり、その承認本文の土曜日14時が根拠。星見会は明示的に別の集まりなのでrelevantにも含めない。 |
| `garden-duration-paraphrase` | 菜園へ自動で水が出るのは朝の何時から何分間？ | `garden-water` | `garden-water` | 給水タイマーを自動の水やりと表現。承認本文の毎朝7時・10分間が両項目を支持する。 |
| `lens-location-disambiguation` | 交換用のレンズを探している。銀色の棚のどちら側の何に入っている？ | `lens-storage` | `lens-storage` | 予備レンズの承認本文は右端の箱。左端の袋は予備ミラー専用で、この質問の対象とは違う。 |
| `mirror-location-disambiguation` | レンズではなく光学実験の予備ミラーを取り出す場所は？ | `mirror-storage` | `mirror-storage` | 対象は予備ミラー。承認本文の銀色の棚の左端の袋が根拠で、予備レンズの右端の箱とは区別する。 |
| `temperature-format-paraphrase` | 温度を測ったログは、どんな形式でどの項目を書き出す？ | `temperature-format` | `temperature-format` | 温度実験の承認本文にCSV、測定時刻、摂氏温度がある。照度実験のTSVとルクスは別の対象。 |
| `brightness-format-disambiguation` | 温度のログではなく、明るさを測る照度実験では保存形式と測定値の単位は何？ | `brightness-format` | `brightness-format` | 質問が求めるのは照度実験側の形式と単位だけ。承認本文のTSVとルクスが直接支持する。温度側の形式や単位を問うgoldではない。 |
| `mizuki-old-fact-correction` | 観測試作機ミズキは今もRP2040という理解で合っている？ | `mizuki-current` | `mizuki-current` | 現在の承認本文がRP2040ではなくESP32へ変更済みと明記しており、旧理解の否定を支持する。supersededのmizuki-oldをfactとして戻さない。 |
| `two-controllers-disambiguation` | ミズキとミツキ、それぞれの現在の制御基板を対応づけたい。 | `mizuki-current`, `mitsuki-controller` | `mizuki-current`, `mitsuki-controller` | ミズキ=ESP32、ミツキ=STM32をそれぞれ別の承認本文が支持する。一方だけでは両機体の対応表の全項目を満たさない。旧ミズキは除外。 |
| `two-meeting-times-disambiguation` | 星見会と星見工作会の開始曜日と時刻を、会の名前ごとに並べて確認したい。 | `observing-time`, `craft-time` | `observing-time`, `craft-time` | 星見会=編集後の水曜19時、星見工作会=土曜14時。各承認本文が自分の会の項目を支持する。両方の根拠が必要で、木曜候補は不可。 |
| `two-log-formats-disambiguation` | 温度実験と照度実験のファイル形式と記録列を、それぞれ教えて。 | `temperature-format`, `brightness-format` | `temperature-format`, `brightness-format` | 温度側のCSV・時刻・摂氏温度と照度側のTSV・時刻・ルクス照度を各承認本文が支持する。関連する両noteを揃えても、測定間隔の情報は増えない。 |
| `unsupported-mizuki-runtime` | ミズキの電池を満充電にしたら何時間観測できる？ | `mizuki-current` | ∅ | ミズキの承認本文は機体に関連するが電池持続時間未記録と明記。2時間というpending-runtimeは未承認であり数値の支持根拠にはできない。別機体のミツキも含めない。 |
| `unsupported-paper-price` | 工作室の設計メモ用の厚手の方眼紙は、いくらで買った？ | `design-paper` | ∅ | 設計用紙の承認本文は対象と用途に関連するが購入価格は未記録。材質と筆記具の記録は金額を裏づけない。 |
| `unsupported-lens-cleaning` | 予備レンズを使う前に洗うとき、どの洗浄液を使えばいい？ | `lens-storage` | ∅ | 予備レンズの承認本文には収納位置だけがあり、洗浄方法は未記録。場所の支持と洗浄液の支持を区別し、ミラーの収納noteも根拠にしない。 |
| `unsupported-observing-rain` | 水曜日の星見会は雨が降ったら中止になる？ | `observing-time` | ∅ | 編集後の星見会本文は水曜19時の予定に関連するが、雨天時の開催判断は未記録。開始日時から中止条件を推測しない。別の星見工作会も回答根拠ではない。 |
| `unsupported-temperature-interval` | 温度実験では何秒おきにサンプルを取ることになっている？ | `temperature-format` | ∅ | 温度ログの本文は同じ実験に関連するが測定時間間隔は未記録。測定時刻という列の存在は何秒おきかの数値を支持しない。 |
| `unrelated-train` | 架空王国の東西鉄道の最終列車は何時に出る？ | ∅ | ∅ | 人工corpusに鉄道や最終列車の情報はない。集まりや給水の時刻は話題が違い、単に時刻を持つという理由ではrelevantにしない。 |
| `unrelated-cooking` | 架空食堂のカレーには何グラムの塩を入れる？ | ∅ | ∅ | 人工corpusに食堂・カレー・調理分量の情報はない。工作用塗料の成分候補は調理の根拠でも承認factでもない。 |
| `retired-venue-history` | 廃止された工作会の昔の会場はどこだった？ | ∅ | ∅ | 港の倉庫という本文はretired-venueにのみある。現在approved-only検索では歴史の質問でも退役noteをfactとして返さない。現役の星見工作会は別会であり会場も記録していない。 |
| `unapproved-paint-composition` | まだ採用を保留している青い塗料に含まれる成分を確認したい。 | ∅ | ∅ | ユメライトという成分はpending-paintにだけあり、承認済みnoteには塗料の情報がない。未承認candidateを現在の承認済み回答根拠に含めない。 |
| `unapproved-solar-equipment` | 風測定試作機ハルカに太陽電池を積む予定は採択済み？ | ∅ | ∅ | ハルカと太陽電池の情報はpending-solarだけで採択保留と書かれている。別の観測機ミズキ/ミツキからハルカの採択状況を裏づけることもできない。 |
| `conflict-time-correction` | 星見会は木曜20時という案を、現在の開始予定として使ってよい？ | `observing-time` | `observing-time` | 現在の承認正本は編集後の水曜19時なので、木曜20時を現在の予定として使う理解を否定できる。conflict-timeは未採択であり、結果に戻してよい支持goldではない。 |

## 全25問・2試行の実順位

取得R/Sは固定goldのうち実際に取得したID。結果の順序はtop1→top3で、試行2も省略せず記載する。除外列は試行1/試行2の違反数。

| Query ID | 試行1 top3 | 試行2 top3 | 取得R（両試行） | 取得S（両試行） | 除外1/2 |
| --- | --- | --- | --- | --- | --- |
| `mizuki-controller-paraphrase` | 1. `mitsuki-controller`<br>2. `mizuki-current`<br>3. `design-paper` | 1. `mitsuki-controller`<br>2. `mizuki-current`<br>3. `design-paper` | `mizuki-current` | `mizuki-current` | 0/0 |
| `mitsuki-controller-disambiguation` | 1. `mitsuki-controller`<br>2. `mizuki-current`<br>3. `design-paper` | 1. `mitsuki-controller`<br>2. `mizuki-current`<br>3. `design-paper` | `mitsuki-controller` | `mitsuki-controller` | 0/0 |
| `paper-tools-paraphrase` | 1. `design-paper`<br>2. `brightness-format`<br>3. `temperature-format` | 1. `design-paper`<br>2. `brightness-format`<br>3. `temperature-format` | `design-paper` | `design-paper` | 0/0 |
| `observing-current-revision` | 1. `craft-time`<br>2. `observing-time`<br>3. `garden-water` | 1. `craft-time`<br>2. `observing-time`<br>3. `garden-water` | `observing-time` | `observing-time` | 0/0 |
| `craft-time-disambiguation` | 1. `craft-time`<br>2. `observing-time`<br>3. `garden-water` | 1. `craft-time`<br>2. `observing-time`<br>3. `garden-water` | `craft-time` | `craft-time` | 0/0 |
| `garden-duration-paraphrase` | 1. `garden-water`<br>2. `craft-time`<br>3. `mizuki-current` | 1. `garden-water`<br>2. `craft-time`<br>3. `mizuki-current` | `garden-water` | `garden-water` | 0/0 |
| `lens-location-disambiguation` | 1. `lens-storage`<br>2. `mirror-storage`<br>3. `design-paper` | 1. `lens-storage`<br>2. `mirror-storage`<br>3. `design-paper` | `lens-storage` | `lens-storage` | 0/0 |
| `mirror-location-disambiguation` | 1. `mirror-storage`<br>2. `lens-storage`<br>3. `design-paper` | 1. `mirror-storage`<br>2. `lens-storage`<br>3. `design-paper` | `mirror-storage` | `mirror-storage` | 0/0 |
| `temperature-format-paraphrase` | 1. `brightness-format`<br>2. `temperature-format`<br>3. `lens-storage` | 1. `brightness-format`<br>2. `temperature-format`<br>3. `lens-storage` | `temperature-format` | `temperature-format` | 0/0 |
| `brightness-format-disambiguation` | 1. `brightness-format`<br>2. `temperature-format`<br>3. `lens-storage` | 1. `brightness-format`<br>2. `temperature-format`<br>3. `lens-storage` | `brightness-format` | `brightness-format` | 0/0 |
| `mizuki-old-fact-correction` | 1. `mizuki-current`<br>2. `mitsuki-controller`<br>3. `design-paper` | 1. `mizuki-current`<br>2. `mitsuki-controller`<br>3. `design-paper` | `mizuki-current` | `mizuki-current` | 0/0 |
| `two-controllers-disambiguation` | 1. `mitsuki-controller`<br>2. `mizuki-current`<br>3. `design-paper` | 1. `mitsuki-controller`<br>2. `mizuki-current`<br>3. `design-paper` | `mizuki-current`, `mitsuki-controller` | `mizuki-current`, `mitsuki-controller` | 0/0 |
| `two-meeting-times-disambiguation` | 1. `craft-time`<br>2. `observing-time`<br>3. `brightness-format` | 1. `craft-time`<br>2. `observing-time`<br>3. `brightness-format` | `observing-time`, `craft-time` | `observing-time`, `craft-time` | 0/0 |
| `two-log-formats-disambiguation` | 1. `brightness-format`<br>2. `temperature-format`<br>3. `lens-storage` | 1. `brightness-format`<br>2. `temperature-format`<br>3. `lens-storage` | `temperature-format`, `brightness-format` | `temperature-format`, `brightness-format` | 0/0 |
| `unsupported-mizuki-runtime` | 1. `mizuki-current`<br>2. `mitsuki-controller`<br>3. `temperature-format` | 1. `mizuki-current`<br>2. `mitsuki-controller`<br>3. `temperature-format` | `mizuki-current` | ∅ | 0/0 |
| `unsupported-paper-price` | 1. `design-paper`<br>2. `lens-storage`<br>3. `mirror-storage` | 1. `design-paper`<br>2. `lens-storage`<br>3. `mirror-storage` | `design-paper` | ∅ | 0/0 |
| `unsupported-lens-cleaning` | 1. `lens-storage`<br>2. `mirror-storage`<br>3. `design-paper` | 1. `lens-storage`<br>2. `mirror-storage`<br>3. `design-paper` | `lens-storage` | ∅ | 0/0 |
| `unsupported-observing-rain` | 1. `observing-time`<br>2. `craft-time`<br>3. `garden-water` | 1. `observing-time`<br>2. `craft-time`<br>3. `garden-water` | `observing-time` | ∅ | 0/0 |
| `unsupported-temperature-interval` | 1. `temperature-format`<br>2. `brightness-format`<br>3. `lens-storage` | 1. `temperature-format`<br>2. `brightness-format`<br>3. `lens-storage` | `temperature-format` | ∅ | 0/0 |
| `unrelated-train` | 1. `craft-time`<br>2. `observing-time`<br>3. `garden-water` | 1. `craft-time`<br>2. `observing-time`<br>3. `garden-water` | ∅ | ∅ | 0/0 |
| `unrelated-cooking` | 1. `design-paper`<br>2. `mirror-storage`<br>3. `lens-storage` | 1. `design-paper`<br>2. `mirror-storage`<br>3. `lens-storage` | ∅ | ∅ | 0/0 |
| `retired-venue-history` | 1. `craft-time`<br>2. `observing-time`<br>3. `lens-storage` | 1. `craft-time`<br>2. `observing-time`<br>3. `lens-storage` | ∅ | ∅ | 0/0 |
| `unapproved-paint-composition` | 1. `mizuki-current`<br>2. `design-paper`<br>3. `lens-storage` | 1. `mizuki-current`<br>2. `design-paper`<br>3. `lens-storage` | ∅ | ∅ | 0/0 |
| `unapproved-solar-equipment` | 1. `mizuki-current`<br>2. `design-paper`<br>3. `mitsuki-controller` | 1. `mizuki-current`<br>2. `design-paper`<br>3. `mitsuki-controller` | ∅ | ∅ | 0/0 |
| `conflict-time-correction` | 1. `observing-time`<br>2. `craft-time`<br>3. `mizuki-current` | 1. `observing-time`<br>2. `craft-time`<br>3. `mizuki-current` | `observing-time` | `observing-time` | 0/0 |

## 観測した限界

`mizuki-controller-paraphrase` は両試行ともミツキのSTM32が1位、問われたミズキのESP32が2位だった。`observing-current-revision` は星見工作会の土曜14時が1位、星見会の編集後水曜19時が2位。`temperature-format-paraphrase` は照度のTSVが1位、温度のCSVが2位。この3問だけでMRRが1から下がる。正しいgoldはtop3にあるが、最上位候補を回答として採用すると対象を取り違える。似た名前を含む単語の近さと回答対象の一致は別である。

一方、旧RP2040を問う訂正queryは訂正後ミズキが1位、木曜20時を問うqueryは現在星見会が1位だった。全150取得候補を確認し、現在approved本文がfixtureの `edit_to` または `content` と一致、sourceが追加dataset/IDを指し、origin=user_explicit、confidence=0.8、importance=0.7、revisionが記録されていることを照合した。星見会の取得本文は全て水曜19時で `edited_since_approval=true`、他はfalse。indexのstale IDは星見会のみで、旧・退役正本の保持も両試行でtrue。除外の成功を、意味的な対象識別の成功とは混同しない。

根拠不足5問ではそれぞれ関連noteを1位で取得したが、電池何時間・価格・洗浄液・中止判断・何秒間隔の支持goldは空である。2時間の未承認候補は返されず、承認本文は持続時間未記録のまま。無関係な鉄道には会や給水の時刻、調理分量には設計紙や光学部品の保管位置が返った。退役会場・塗料成分・ハルカの採択にも別のapproved contextが返り、問われた事実は支持しない。除外違反0でも、非空contextが回答根拠になる保証はない。

複数noteを問う3問は両方のsupportingを取得したが、回答生成・名前と値の結合・回答の正しさ・abstentionは測っていない。`quality_assessment=not_established`、`quality_thresholds=null`のまま、配備モデル・品質threshold・根拠不足時の回答方針は採用しない。既存セットと追加セットの観測はそれぞれ有限の人工例であり、一般の日本語検索品質・実ユーザー分布・より大きいcorpusを保証しない。次の品質・回答方針判断はJAR-88で扱う。
