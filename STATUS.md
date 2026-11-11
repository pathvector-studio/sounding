# STATUS

親セッションだけが更新する（体制は `specs/agent-workflow.md`）。最終更新: 2026-11-11

## 進行中
| タスク | リポジトリ / ブランチ | 担当 | 状態 |
|---|---|---|---|
| なし | | | |

## 承認待ち
- なし

## 予定
- 受け入れ基準 6 の残り：利用者が `~/.config/sounding/dev.env` に `WEBHOOK_URL` と `DELIVERY_CHANNELS`（`webhook` を含む）を入れたら、`digest --once` で実際に 1 回だけ届き、再実行で届かないことを確かめる
- 段 4 の実装（5 項目。install.sh と `site/` の PR のマージは毎回承認）
- Anthropic のキーを入れたら、実モデルで費用と品質を測って `LLM_DAILY_BUDGET_USD` の既定を見直す

## 詰まり
- なし

## 直近の完了
- 段 4 の spec を承認（2026-11-11、要判断はすべて (a)：Tailscale と cloudflared は compose のサイドカー（イメージ 2 つを追加）、install.sh ではプロファイルの中身を聞かない、紹介サイトもこの段で作る）
- **段 3 完了**（2026-11-11）。4/4：email と webhook の配信、配信の記録と再送（2 回・3 日以内）、`deliver` コマンド、状態のページに配信の表。手元の受け手で、失敗の記録 → 1 回だけ届く → 再実行で送らない、を確認。実チャネルへの配信確認は予定に残す
- 段 3 の実装 3/4（2026-11-10）：Web UI・JSON API・ログイン（scrypt、失敗 5 回で 429）・Atom フィード、web を compose で既定で起動（秘密を渡さない）
- 段 3 の実装 2/4（2026-11-10）：ダイジェストの期間と本文、テキスト表示、`digest --once`・`show-digest`、worker の digest ジョブ。実データで LLM なし・ローカル LLM の両方で作成し、同じ期間はその場で作り直す
- 段 3 の実装 1/4（2026-11-10）：会話と RSS 項目の選別（点数と理由を記録）、RSS の境界の LLM 判定、RSS 項目の要約、migration 0003
- 段 3 の spec を承認（2026-11-10、要判断はすべて (a)：RSS の境界は cheap 段の LLM で判定、パスワードが空ならログインなしで警告、実際に届くかは webhook で確認。fastapi・uvicorn・jinja2 の追加）
- **段 2 完了**（2026-11-08）。4/4：summarize ジョブ（日次 06:00・予算で停止・未要約を記録）、`summarize --once` と `show`。受け入れ基準 3〜6 はローカル LLM（Ollama の OpenAI 互換、7B）で確認：sidrops 36 会話に版 1（失敗 0、26 分）、差分更新でプロンプトが 18,347 字→1,906 字、同じ入力の再実行は $0、予算で 5 件後に停止し 31 件を記録
- 段 2 の実装 3/4（2026-11-08）：「現在地」の生成（差分更新・作り直し・長い会話の分割・根拠の検査で裏付けのない合意を open に落とす）。プロンプトは情報源に依存しない部分と IETF 固有の部分に分離
- 段 2 の実装 2/4（2026-11-08）：LLM のプロバイダ（Anthropic・OpenAI の公式 SDK）、構造化出力、呼び出しの記録、単価表、日次予算、結果の再利用、migration 0002。出力トークンの既定を 16000 に
- 段 2 の実装 1/4（2026-11-08）：LLM に渡す本文の前処理（引用・署名・フッターの除去、行間返信の文脈を残す）。実データの sidrops 138 通で本文の 36.4% に削減、返信本文の欠落なし
- 段 2 の spec を承認（2026-11-08、要判断はすべて (a)：公式 SDK を追加、要約は日次 06:00、既定の main は `claude-opus-5-5`）
- **段 1 完了**（2026-11-08）。5/5：worker（定時取得・advisory lock・終了シグナル）、retention、`IDENTITY_SECRET` 未設定時は IETF を止める、README の起動手順。`docker compose up` で初回取得のあと 3 回の定時取得が重複なく回り、停止も正常
- 段 1 の実装 4/5（2026-11-08）：RSS/Atom の取得（条件付き GET、リダイレクトとサイズの上限）。実フィード 2 本で取得と 304 を確認
- 段 1 の実装 3/5（2026-11-08）：IETF ML の IMAP 取得、祖先の補完、プロファイル、fetch ジョブ。sidrops 30 日分を手動取得し、会話の分け方がサーバーの THREAD と 144/144 一致、祖先の補完で 16 通増
- 段 1 の実装 2/5（2026-11-08）：会話への増分割り当て（統合・件名の補助）、自動送信の判定とイベント、送信者の識別子（HMAC）。`spike/` を削除
- 段 1 の実装 1/5（2026-11-08）：backend の雛形、共有の層のスキーマ（migration 0001）、CI の backend ジョブと依存の監査
- 段 1 の spec を承認（2026-11-08、組み合わせ B「将来に備える」：共有の層と利用者の層、conversations、events、祖先の補完、送信者は HMAC の識別子）
- 段 0 スパイクの測定（2026-11-07）：取得は匿名 IMAP が主・rsync が予備。スレッド化はサーバーと 100% 一致、前処理で本文の約 36% に削減、main 入力は 1 日 7 万トークン前後（`docs/spike-phase0.md`）
- 段 0 スパイクの spec を承認（2026-11-07、要判断はすべて (a)）
- GitHub のリポジトリ設定を biblioflow と揃えた（2026-11-07）：main のルールセット、secret scanning と push protection、Dependabot のセキュリティ更新、マージ後のブランチ削除、`skip-changelog` ラベル
- GitHub への最初の push（2026-11-07）。push 前の検査（private-info・全履歴・gitleaks）と CI（private-info・compose）はすべて通過
- 紹介サイトに名前の由来を必ず載せる、を `AGENTS.md` と `docs/DESIGN.md` 4.4 に追記
- 立ち上げ：先行事例の調査（`docs/SURVEY.md`）、命名（Sounding）、MVP 設計（`docs/DESIGN.md`）、雛形
- 決定（2026-11-07）：MVP は狭く・将来は広く／Python／MIT／紹介サイトは同じリポジトリの `site/`
- 開発の仕組みを biblioflow に揃えた：`AGENTS.md`、`specs/agent-workflow.md`、`specs/roadmap.md`、`CHANGELOG.md`、`RELEASING.md`、`VERSION`、`LOCAL_DEV.md`、`scripts/check-private-info.py` と git フック、`.claude/`、CI（private-info・compose・changelog）、Dependabot
