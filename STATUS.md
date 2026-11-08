# STATUS

親セッションだけが更新する（体制は `specs/agent-workflow.md`）。最終更新: 2026-11-08

## 進行中
| タスク | リポジトリ / ブランチ | 担当 | 状態 |
|---|---|---|---|
| 段 2 の spec（LLM 抽象化・前処理・「現在地」の生成） | — | 親 | 未着手 |

## 承認待ち
- なし

## 予定
- 段 2〜4（`specs/roadmap.md`）

## 詰まり
- なし

## 直近の完了
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
