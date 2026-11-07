# STATUS

親セッションだけが更新する（体制は `specs/agent-workflow.md`）。最終更新: 2026-11-07

## 進行中
| タスク | リポジトリ / ブランチ | 担当 | 状態 |
|---|---|---|---|
| 段 1 の実装 1/5（backend の雛形と DB） | `phase1/scaffold` | 親 + subagent | 着手 |

## 承認待ち
- なし

## 予定
- 段 1 の実装 2〜5（会話・イベント・識別子 → IETF ML の取得 → RSS → worker と運用）
- 段 1〜4（`specs/roadmap.md`）

## 詰まり
- なし

## 直近の完了
- 段 1 の spec を承認（2026-11-08、組み合わせ B「将来に備える」：共有の層と利用者の層、conversations、events、祖先の補完、送信者は HMAC の識別子）
- 段 0 スパイクの測定（2026-11-07）：取得は匿名 IMAP が主・rsync が予備。スレッド化はサーバーと 100% 一致、前処理で本文の約 36% に削減、main 入力は 1 日 7 万トークン前後（`docs/spike-phase0.md`）
- 段 0 スパイクの spec を承認（2026-11-07、要判断はすべて (a)）
- GitHub のリポジトリ設定を biblioflow と揃えた（2026-11-07）：main のルールセット、secret scanning と push protection、Dependabot のセキュリティ更新、マージ後のブランチ削除、`skip-changelog` ラベル
- GitHub への最初の push（2026-11-07）。push 前の検査（private-info・全履歴・gitleaks）と CI（private-info・compose）はすべて通過
- 紹介サイトに名前の由来を必ず載せる、を `AGENTS.md` と `docs/DESIGN.md` 4.4 に追記
- 立ち上げ：先行事例の調査（`docs/SURVEY.md`）、命名（Sounding）、MVP 設計（`docs/DESIGN.md`）、雛形
- 決定（2026-11-07）：MVP は狭く・将来は広く／Python／MIT／紹介サイトは同じリポジトリの `site/`
- 開発の仕組みを biblioflow に揃えた：`AGENTS.md`、`specs/agent-workflow.md`、`specs/roadmap.md`、`CHANGELOG.md`、`RELEASING.md`、`VERSION`、`LOCAL_DEV.md`、`scripts/check-private-info.py` と git フック、`.claude/`、CI（private-info・compose・changelog）、Dependabot
