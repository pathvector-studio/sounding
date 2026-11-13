# バックログ（ループの作業キュー）

ループ（`specs/agent-workflow.md`「ループ」）は、上から順に `ready` の項目を 1 つずつ取る。項目を足す・順を変える・`ready` にするのはユーザー（または、ユーザーが会話で承認したことを親が書き写す）。ループが自分で変えてよいのは状態を `in-progress` / `parked` / `done` に進めることと、`spec` 項目の spec PR を出したあとに `needs-approval` にすることだけ。

状態：
- `ready`：承認済みの spec（`main` にある）の項目。ループが実装して、ゲートに当たらなければマージまで進める
- `spec`：spec を書く段階。ループは `specs/<name>.md` の案を書いて spec PR を出し、`needs-approval` にして止める（マージはしない）
- `needs-approval`：ユーザーの判断待ち。ループは触らない
- `needs-user`：ユーザーの手（手元の秘密・実チャネル・本番）が要る。ループは触らない
- `in-progress` / `parked`（ゲートに当たった・2 回直して CI が通らない。理由を書く）/ `done`

| ID | 内容 | spec | 状態 | メモ |
|---|---|---|---|---|
| release-v0.1.0 | v0.1.0 のリリース PR（#30。#26 install.sh・#27 紹介サイトを含む） | `RELEASING.md` | needs-approval | `site/`・`install.sh`・`.github/` を変えるのでマージはユーザー。マージ後 #26・#27 は閉じる |
| launch | タグ `v0.1.0` と GitHub Release（`install.sh` を添付）、Workers Builds の接続、本番 URL での確認 | `RELEASING.md` | needs-user | タグ・リリース・Cloudflare はユーザー。済んだら親が本番の `/install.sh` と両ページを確かめる |
| p4-tailscale | 段 4 の受け入れ基準 5：tailnet の中からだけ https で開ける | `specs/phase4.md` | needs-user | 利用者の tailnet と認証キーが要る |
| p3-webhook | 受け入れ基準 6 の残り：実 webhook に 1 回だけ届く | `specs/phase3.md` | needs-user | 利用者の `WEBHOOK_URL` が要る |
| llm-budget | 実モデルで費用と品質を測り `LLM_DAILY_BUDGET_USD` の既定を見直す | — | needs-user | Anthropic のキーが要る |
| eval-set | 評価セット（決着済みスレッドの「正解の現在地」）とプロンプトの回帰テスト | — | spec | ロードマップ目標 1。LLM なしで回る部分（データ形式・採点・CI）から |
| ext-datatracker | 拡張 1：IETF Datatracker | — | spec | `eval-set` のあと（目標 1「質が固まるまで広げない」） |
| compose-rebuild | `docker compose up --build` のたびに worker と web が作り直される（同じタグを 2 サービスがビルドし、attestation で ID が変わる）。compose 側で直す | — | needs-approval | 計画の外の小さな修正。ユーザーが `ready` にすればループが拾う。ループは `docker compose up` を使えないので、確認は親か CI で |
