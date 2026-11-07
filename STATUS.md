# STATUS

親セッションだけが更新する（体制は `specs/agent-workflow.md`）。最終更新: 2026-11-07

## 進行中
| タスク | リポジトリ / ブランチ | 担当 | 状態 |
|---|---|---|---|
| 段 0 スパイクの spec | `spec/phase0` | 親 | PR を作成、承認待ち |

## 承認待ち
- 段 0（スパイク）の spec：`specs/phase0.md`（PR）。要判断 4 問（主手段の仮説、匿名 IMAP のメールアドレス、取得範囲、スパイクのコードの置き場）
- GitHub のリポジトリ設定を biblioflow と揃える（main のルールセット、secret scanning と push protection、Dependabot のセキュリティ更新、マージ後のブランチ削除、`skip-changelog` ラベル）

## 予定
- 段 0 スパイク：IETF ML の取得手段（IMAP / HTTP）の確認、1 つの WG の 1 か月分でスレッド化と前処理の削減率を測る
- 段 1〜4（`specs/roadmap.md`）

## 詰まり
- なし

## 直近の完了
- GitHub への最初の push（2026-11-07）。push 前の検査（private-info・全履歴・gitleaks）と CI（private-info・compose）はすべて通過
- 紹介サイトに名前の由来を必ず載せる、を `AGENTS.md` と `docs/DESIGN.md` 4.4 に追記
- 立ち上げ：先行事例の調査（`docs/SURVEY.md`）、命名（Sounding）、MVP 設計（`docs/DESIGN.md`）、雛形
- 決定（2026-11-07）：MVP は狭く・将来は広く／Python／MIT／紹介サイトは同じリポジトリの `site/`
- 開発の仕組みを biblioflow に揃えた：`AGENTS.md`、`specs/agent-workflow.md`、`specs/roadmap.md`、`CHANGELOG.md`、`RELEASING.md`、`VERSION`、`LOCAL_DEV.md`、`scripts/check-private-info.py` と git フック、`.claude/`、CI（private-info・compose・changelog）、Dependabot
