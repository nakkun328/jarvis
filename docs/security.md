# Security

`.env`、鍵、ローカルデータは Git に入れません。設定値や例外の全文をログに出さず、API キーをクライアントへ送らない設計にします。入力値は API 境界と Tool 境界で検証します。OpenAI アダプターは `store=False` を指定します。これは API の response 保存を無効にする設定であり、すべてのデータ保持を無効にする保証ではありません。詳細は [OpenAI の data controls](https://developers.openai.com/api/docs/guides/your-data) を参照してください。

将来の Tool 権限は green（読取）、yellow（条件付き確認）、red（破壊・外部送信など必ず確認）に分けます。各 Tool は必要な権限だけを持ち、実行内容と確認対象をユーザーに示します。現在 Tool 実行は未実装です。

現在の API に認証はありません。ローカルホストでの開発用途のみを想定し、ネットワーク公開前に認証、TLS、アクセス制御、脅威テストを導入します。
