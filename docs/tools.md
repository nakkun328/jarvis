# Tools and skills design

Phase 4 で Core から外部操作を分離した Tool registry を実装します。各 Tool には name、description、input schema、permission level、execution environment を定義します。入力の検証、実行ログ、結果の確認を共通層で扱います。

Skill は複数 Tool を使う作業手順です。Research Skill は検索から出典確認・保存まで、Development Skill は repository 調査から編集・テスト・diff review・報告までを扱います。現在 Tool と Skill の実行は未実装です。
