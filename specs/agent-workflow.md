# エージェントの開発体制（親セッション + subagent）

biblioflow の体制（2026-11-07 にユーザーが承認したもの）を、単一リポジトリの Sounding に合わせたもの。

## 役割
- **親**（このリポジトリで動くセッション 1 つ）：作業を分解し、subagent に「担当ファイル・完了条件」を渡す。報告を diff とテスト結果で確かめる。`STATUS.md` を書くのは親だけ。ユーザーには判断が要ることだけを上げる。
- **subagent**：実装・調査・レビュー・検証の既定。担当範囲だけを実装・テストし、ローカルにコミットする。push・PR・マージ・タグ・デプロイはしない。詰まったら親に返す。
- **子セッション**（デスクトップアプリの子セッション、worktree・作業ブランチ）：数時間かかる仕事や、ユーザーが直接触りたい仕事だけ。

実装は Sonnet の subagent に委譲し、計画（`specs/<name>.md`）と検証は親が持つ。

## 受け渡しフォーマット
subagent・子は作業ブランチの `.handoff/<task>.md`（gitignore 対象）に書き、親への最後の報告の先頭にも同じ要約を付ける。

```yaml
task: <親が付けた ID>
branch: <作業ブランチ>
status: done | blocked | needs-decision
did: [やったこと]
files: [変更したファイル]
tests: "<実行したコマンド>: <結果>"   # 省略しない。落ちたら出力を付ける
commits: [<sha>]                     # ローカルのみ、未 push
next: [次に必要なこと（誰が）]
decision: null                       # 要判断なら 1 問 + 選択肢
```

## 権限
| 自動で可 | ユーザーの承認が必須 |
|---|---|
| ファイル編集、build / lint / typecheck / テスト、開発用の `docker compose` | 承認済みの計画にない変更の push・PR・マージ |
| 作業ブランチ・worktree の作成、ローカルコミット、`git merge origin/main` | タグ・リリース・デプロイ（`wrangler`、Cloudflare Pages の設定） |
| `gh` の閲覧（pr view / checks / run view） | 秘密（`.env`・`~/.config/sounding/`・`op`）、大量削除、依存の追加 |
| subagent・子セッションの起動 | 課金がありうる操作（`gh workflow run` など）、外部への送信（メール・他人のリポジトリへの PR や issue） |
| **承認済みの計画の項目**：push → PR → CI green → squash マージ（1 項目ずつ） | **`site/` か `install.sh` を変える PR のマージ**（本番サイトへの公開。計画の項目でも毎回） |

**承認は計画単位**：ユーザーが spec か `STATUS.md` の計画を承認したら、その項目は親が push からマージまで進める。計画の外に出る変更が必要になったら、その時点で止めて聞く。force push は常に禁止。

仕組み：`.claude/settings.json` の `ask` / `deny` と、PreToolUse フック `.claude/hooks/guard.py`（ルールが前方一致で見落とす書き方を拾う）。どちらも安全の境界ではない。最終的な歯止めは main のルールセット・GitHub の権限・ここに書いた運用。

## 報告
親はターンの終わりに「進捗 / 要判断 / 詰まり」の 3 行と判断 1 問だけを出す。`STATUS.md` は節目（作業の完了・判断待ち・PR のマージ）で更新する。公開リポジトリなので、ローカル固有の情報は書かない。

## ループ（無人で回す開発、2026-11-12）
対話のセッションとは別に、1 つのセッションが `specs/backlog.md` を上から 1 項目ずつ進める。手順は `.claude/skills/dev-loop/SKILL.md`。

- **隔離**：ループは専用の worktree `.claude/worktrees/loop`（`main` を detached で）で動き、項目ごとに `loop/<ID>` ブランチを切る。利用者のチェックアウト・ブランチ・開発用の compose には触れない。テストは `scripts/test.sh`（使い捨ての DB を空いたポートで立てる）だけ。
- **何を進めるか**：バックログの `ready`（`main` にある承認済み spec の項目）と `spec`（spec 案を書いて PR を出すところまで）。項目の追加・並べ替え・`ready` にするのはユーザー。
- **ゲート**：上の権限表のまま。ループの中ではガードの「ask」がすべて「deny」になり、ループは待たずに項目を `parked` / `needs-approval` にして次へ進む。ガード（`.claude/hooks/guard.py`）は加えて、ゲートのパス（`site/`・`install.sh`・`.claude/`・`.githooks/`・`.github/`・`scripts/check-private-info.py`・この文書）を変える PR と新しい spec の PR の `gh pr merge` を止め（ループ外では ask）、ループの中ではガード自身の編集と `main` への push を止める。安全の境界ではない点は変わらない。
- **承認の受け取り方**：spec はユーザーが spec PR をマージすれば承認（決定事項は PR のコメントか会話で受け、親が spec に書き写す）。ゲートの PR はユーザーがマージする。
- **止め方**：ループの worktree に `.loop/STOP` を置くか、そのセッションを止める。CI が 2 回直して通らない項目は `parked`。
- **記録**：`.loop/journal.md`（git に入らない）に 1 回 3 行。`STATUS.md` とバックログはその回の PR の中で更新する。

起動（初回だけ worktree を作る）：
```bash
git worktree add --detach .claude/worktrees/loop origin/main
```
そのフォルダでセッションを開き、`/loop dev-loop スキルを 1 回まわす`。スラッシュコマンドは 1 つの指示に 1 つしか効かないので、`/loop /dev-loop` とは書かず、`/loop` の中身は文で頼む。
