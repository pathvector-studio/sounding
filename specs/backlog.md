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
| p4-install | 段 4 の 3/5：install.sh と README（PR #26） | `specs/phase4.md` | needs-approval | `install.sh` を変えるのでマージは毎回ユーザー |
| p4-site | 段 4 の 5/5：紹介サイト（PR #27） | `specs/phase4.md` | needs-approval | `site/` を変えるのでマージは毎回ユーザー |
| p3-webhook | 受け入れ基準 6 の残り：実 webhook に 1 回だけ届く | `specs/phase3.md` | needs-user | 利用者の `WEBHOOK_URL` が要る |
| llm-budget | 実モデルで費用と品質を測り `LLM_DAILY_BUDGET_USD` の既定を見直す | — | needs-user | Anthropic のキーが要る |
| eval-set | 評価セット（決着済みスレッドの「正解の現在地」）とプロンプトの回帰テスト | — | spec | ロードマップ目標 1。LLM なしで回る部分（データ形式・採点・CI）から |
| ext-datatracker | 拡張 1：IETF Datatracker | — | spec | `eval-set` のあと（目標 1「質が固まるまで広げない」） |
