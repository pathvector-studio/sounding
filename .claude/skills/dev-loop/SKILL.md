---
name: dev-loop
description: Sounding の開発ループを 1 回まわす。specs/backlog.md から ready の項目を 1 つ取り、実装・テスト・自己レビュー・PR・CI・（ゲートに当たらなければ）マージまで進め、結果を記録する。`/loop /dev-loop` で繰り返す。ループ用 worktree（.claude/worktrees/loop）の中でだけ使う。
---

# 開発ループの 1 回（/dev-loop）

体制と権限は `specs/agent-workflow.md`（特に「ループ」）。ここはその手順だけ。**1 回で進めるのは 1 項目まで。** 迷ったら止めて `parked` にし、次の回に任せる。ユーザーに質問して待たない（見ている人はいない）。

## 0. 前提の確認（どれかが外れたら何もせず終わる）
- 作業ディレクトリが `.claude/worktrees/loop` の中であること（`pwd`）。違えば「ループ用 worktree で起動してください」とだけ言って終わる。
- `.loop/STOP` があれば、ループを止める（`/loop` なら ScheduleWakeup の `stop: true`）。
- `git status --porcelain` が空でなければ、前の回の残りなので `.loop/journal.md` に記録して止める（自分で捨てない）。

## 1. 同期と、前の回の PR の後始末
```bash
git fetch --prune origin
gh pr list --author "@me" --state open --json number,headRefName,statusCheckRollup,mergeable
```
- `loop/` で始まるブランチの自分の PR だけを見る。
  - CI が green → `gh pr merge <n> --squash --delete-branch`。ガードが deny したら（ゲートのパス・新しい spec）その項目を `needs-approval` にする。
  - CI が red → そのブランチで直す。同じ PR で 2 回直して通らなければ `parked`（理由と失敗の出力の要点を書く）。
  - CI が実行中 → この回はほかの項目に進まず終わる（同時に 2 つ抱えない）。
  - 競合 → `git merge origin/main` で解く（rebase・force push はしない）。
- 終わったら `git switch --detach origin/main`。

## 2. 項目を選ぶ
`specs/backlog.md` を上から読み、最初の `ready` か `spec` の項目を取る。どちらもなければ「キューが空」と記録して、ループを長い間隔（1 時間）にするか止める。

## 3a. `spec` の項目
- `git switch -c loop/spec-<ID> origin/main`。`spec` スキルの形（例は `specs/phase4.md`）で `specs/<name>.md` の案を書く。要判断は 1 問ずつ選択肢つきで、推奨を (a) に。
- バックログの状態を `needs-approval` にし、PR を出す（`skip-changelog` ラベル）。**マージしない**（新しい spec のマージはガードが止める。ユーザーのマージが承認）。

## 3b. `ready` の項目
- `git switch -c loop/<ID> origin/main`。バックログの状態を `in-progress` に。
- spec の実装計画の次の 1 項目だけを、Sonnet の subagent に「担当ファイル・完了条件・テストのコマンド」を付けて渡す（`specs/agent-workflow.md` の受け渡しフォーマット）。依存の追加が要るなら止めて `parked`。
- 報告を diff とテストで自分で確かめる：
  - `scripts/test.sh`（使い捨ての DB。スキップがあれば失敗）
  - `python3 scripts/check-private-info.py`（あれば `--all` 相当も）
  - `/code-review medium` 相当の自己レビュー。見つかった正しさの問題は直す
- `CHANGELOG.md` の `[Unreleased]` に利用者向けの 1〜3 行。spec の該当チェックボックスを埋める。
- コミットして `git push -u origin loop/<ID>`、`gh pr create`（本文に spec の項目・テスト結果・残り。末尾に既定の署名）。マージは次の回の 1. で CI を見てから。

## 4. 記録
- `.loop/journal.md`（git に入らない）に 1 回 3 行：日時・項目・結果（PR 番号 / parked の理由 / 空）。
- `STATUS.md` とバックログは、その回の PR の中で更新する（main に直接書かない）。
- 次の起床：CI 待ちなら 10 分前後、作業を続けるなら 1〜2 分、キューが空・全部 parked なら 1 時間。

## してはいけないこと（ガードが deny する。回避を試さない）
- force push、`main` への push、`--admin` マージ、タグ・リリース・デプロイ、`wrangler`、秘密の読み取り
- `site/`・`install.sh`・`.claude/`・`.githooks/`・`.github/`・`scripts/check-private-info.py`・`specs/agent-workflow.md` を変える PR のマージ、新しい spec のマージ
- 自分のガード（`.claude/settings*.json`・`.claude/hooks/`・`.githooks/` など）の編集
- 依存の追加、`docker compose up`（開発スタックはユーザーの環境とコンテナ名・ポートがぶつかる。テストは `scripts/test.sh` だけ）
- バックログの項目を自分で `ready` にすること、項目の追加・並べ替え
