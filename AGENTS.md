# Project Specification: Sounding — Self-Hosted Digest of Where Technical Discussions Stand

エージェント（と人）が作業を始める前に読む文書。設計の詳細は `docs/DESIGN.md`、進め方は `specs/agent-workflow.md`、進捗は `STATUS.md`。

## 1. プロジェクト概要

Sounding は、追うべき一次情報が多すぎる専門職（まずネットワークエンジニア）のための、セルフホスト型のダイジェストである。RSS と IETF メーリングリストを集め、利用者の興味プロファイルで選別し、LLM でスレッドごとに「議論の現在地」（何が争点か・合意はどこまで進んだか・何が未決着か）を要約して届ける。全部は見せない。一般向けのニュース要約とは競合させない。

- セルフホスト / OSS（MIT）。Docker Compose で動く
- LLM は利用者のキー持ち込み（Anthropic / OpenAI を切り替えられる）
- 全文は再配布しない。要約＋出典リンク
- まずは狭く（RSS ＋ IETF ML）、将来は情報源を広げる（`docs/DESIGN.md` 1.4）

## 2. 技術スタック

- Python 3.12、FastAPI、SQLAlchemy、Alembic、uv
- PostgreSQL 16
- Docker Compose（`sounding-db` / `sounding-worker` / `sounding-web`）
- 紹介サイト：`site/`（静的 HTML、Cloudflare Pages）

## 3. 実装の進め方

`docs/DESIGN.md` 9 章の段階計画に従う。各段は着手前に `specs/<name>.md` を書いてユーザーの承認をもらう。モックアップ（`TODO` コメントだけの実装）で済ませない。

## 4. 進捗ステータス & 再開手順（エージェントはまずここを読むこと）

> 最終更新: 2026-11-07

### 4.1 段の進捗
| 段 | 状態 | 備考 |
|---|---|---|
| 立ち上げ（調査・命名・設計・雛形・開発の仕組み） | ✅ 完了 | `docs/SURVEY.md`、`docs/DESIGN.md` |
| 0. スパイク（IETF ML の取得手段、削減率の測定） | ✅ 完了 | `docs/spike-phase0.md`。取得は匿名 IMAP が主 |
| 1. `SourceAdapter`・取得・DB | 実装中（5/5） | `specs/phase1.md` |
| 2〜4 | 未着手 | `docs/DESIGN.md` 9 章 |

### 4.2 確定済みの設計判断（再検討しない）
- 名前は **Sounding**（2026-11-07）。リポジトリ `pathvector-studio/sounding`、サイト `sounding.pathvector.dev`、コンテナ接頭辞 `sounding-`。
- **MVP は狭く**：情報源は RSS と IETF メーリングリスト（利用者が選んだリストのみ）だけ。Datatracker は MVP 直後の最初の拡張（2026-11-07）。
- **情報源はアダプタで差し替え可能にする**（`SourceAdapter`）。IETF 固有の知識はアダプタとプロンプトの差し込み部分に閉じ込め、「現在地」の生成は情報源に依存させない。
- 実装言語は Python（biblioflow の backend と同じ型、2026-11-07）。
- ライセンスは **MIT**（2026-11-07）。本番依存に GPL / AGPL / SSPL / BUSL / Elastic / Commons Clause / 非商用のライセンスを入れない。
- 紹介サイトは**同じリポジトリの `site/`**（2026-11-07）。`site/` か `install.sh` を変える PR のマージは本番公開なので毎回ユーザーの承認を取る。紹介サイトには**名前の由来を必ず載せる**（`docs/DESIGN.md` 4.4）。
- **取り込みは共有の層、利用者のものは別の層**（2026-11-08、`specs/phase1.md` の原則）。取得・会話（`conversations`、スレッドの一般化）・イベント（`events`）・「現在地」は利用者の列を持たない。送信者は表示名＋情報源ごとの識別子（メールは HMAC）で、アドレスは保存しない。
- RSS は「現在地」形式の対象外（通常の項目要約）。
- 出力は要約＋出典リンクのみ。取得した本文は `CONTENT_RETENTION_DAYS` を過ぎたら削除する。
- LLM 既定は `none`（キーを入れるまで何も外に送らない）。

### 4.3 ルール
- **秘密**：実値はコミット・PR・issue・ドキュメント・メモリ・会話の要約・scratchpad に書かない。`.env` の中身は表示しない（キー名と「設定済み・空・`change-me` のまま」だけを出す）。新しい秘密は `.env.example` にダミー値で足す。実体はリポジトリの外の `~/.config/sounding/` に置く（`LOCAL_DEV.md`）。
- **公開リポジトリに書かないもの**（ファイル・コミットメッセージ・PR 本文・issue のすべて）：LAN の IP、`*.local` のホスト名、ホームのパス、個人のメールアドレス、tailnet 名、開発用ホスト名、同じマシンのほかのプロジェクトやサービスの名前・運用の詳細。例には `192.168.1.10`、`digest.example.com`、`you@example.com`、`<host>.<tailnet>.ts.net` を使う。手元の情報は `AGENTS.local.md`（git に入らない）に書く。`scripts/check-private-info.py` が git フックと CI で検査する。
- **clone したら最初に** `git config core.hooksPath .githooks`。
- コミットのメールは GitHub の noreply アドレスを使う。
- ブランチの更新は rebase + force push ではなく `git merge origin/main`。
- PR ごとに `CHANGELOG.md` の `[Unreleased]` に利用者向けの 1〜3 行を足す（CI・テストだけの PR は `skip-changelog` ラベル）。
- バージョンは `VERSION` だけで決める（`RELEASING.md`）。タグの push と GitHub Release の作成はユーザーが行う。
- 権限と承認の範囲は `specs/agent-workflow.md`（`.claude/settings.json` と `.claude/hooks/guard.py` が補助する）。

### 4.4 環境の再開手順
```bash
cd <checkout>
git config core.hooksPath .githooks     # 初回だけ
git fetch && git status
cat STATUS.md                            # 進行中・承認待ち・詰まり
cat AGENTS.local.md                      # ローカル固有のこと（なければユーザーに聞く）
```

起動（環境ファイルと `config/profile.yaml` を用意したあと。手順の詳細は `README.md`）：
```bash
docker compose up -d --build
docker compose exec sounding-worker python -m sounding status
docker compose exec sounding-worker python -m sounding fetch --once    # 手動で 1 回取得
```
テスト（実 PostgreSQL。スキップは不可）：
```bash
docker run -d --rm --name sounding-test-db -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=sounding_test \
  -p 127.0.0.1:55432:5432 postgres:16
cd backend && TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55432/sounding_test \
  uv run pytest -q -rs -p no:cacheprovider
docker stop sounding-test-db
```
ホストで直接動かすときは `cd backend && uv run python -m sounding fetch --once`（`DATABASE_URL` などを環境に出しておく）。
イメージは名前付きコンテキストでビルドする：`docker build --build-context repo=. backend`（`VERSION` を入れるため）。
