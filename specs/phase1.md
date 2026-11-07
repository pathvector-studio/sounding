# 段 1：`SourceAdapter` と取得（RSS・IETF ML）、DB、`job_runs`

> 状態: **承認待ち**（2026-11-07）。`docs/DESIGN.md` 1.4・4 章・8 章・9 章の段 1 を具体化する。前提は段 0 の結果（`docs/spike-phase0.md`）。

## 目的
利用者が選んだ IETF メーリングリストと RSS/Atom フィードを、**毎時・差分だけ・冪等に**取り込み、PostgreSQL にメッセージ（item）とスレッドとして蓄える。各ジョブの成否と件数を `job_runs` に残す。要約（段 2）と選別・配信（段 3）が乗る土台を作る。

完了の目安（`docs/DESIGN.md` 9 章）：`docker compose up -d` で worker が動き、毎時の取得が冪等に回る（同じ取得を 2 回しても重複しない）。

## スコープ
**段 1（この spec）**：`backend/`（Python パッケージ `sounding`、uv、Dockerfile）、設定の読み込み、DB モデルと Alembic の最初の migration、`SourceAdapter` と 2 つの実装（IETF ML の IMAP、RSS/Atom）、スレッド化、自動送信メールの分類、worker のスケジューラ、本文の保持期間による削除、CLI、テスト、CI の backend ジョブと依存の監査、compose の更新、README の起動手順。スパイクの `spike/` を削除する。

**スコープ外**：LLM（要約・選別の判定）は段 2・3。前処理（引用・署名の除去）は LLM 入力を作る段 2 で書き直す（段 1 は本文をそのまま保存する）。Web UI・配信は段 3。Datatracker は拡張 1。rsync による取得（予備の手段）は実装しない（必要になったら追加）。

## 確認済みの事実
- **取得手段**（段 0、`docs/spike-phase0.md`）：匿名 IMAP `imap.ietf.org:993`（TLS）、ユーザー `anonymous`、パスワード `FETCH_CONTACT`。メールボックスは `Shared Folders/<list>`。ログイン後に `UIDVALIDITY`・`UIDNEXT`・`UID SEARCH SINCE`・`UID FETCH` が使える。`ID` コマンドは非対応。sidrops 30 日分（146 通）で制限は見えなかった。
- **出典 URL**：IETF の ML のメールには `Archived-At: <https://mailarchive.ietf.org/arch/msg/<list>/<id>>` ヘッダーが付いている（sidrops 30 日分で 146/146）。これをそのまま出典リンクにする。mailarchive に問い合わせる必要はない。
- **スレッド化**：JWZ ＋ RFC 5256 の base subject（任意の `[...]` タグと各言語の返信接頭辞を外す）でサーバーと 100% 一致。38/146 通は References も In-Reply-To もなく、件名の補助が必須。
- **自動送信メール**：16/146 通。すべて `Auto-Submitted` ヘッダーで判定できた（Datatracker の通知）。段 3 で「手続きの更新」として出したいので捨てない。
- **雛形**：`docker-compose.yml` は `DATABASE_URL=postgresql+psycopg://…`、worker は `python -m sounding.worker`、web は `python -m sounding.web`、ビルド文脈は `./backend`、`read_only: true`、内部ネットワーク `sounding-internal` と外向き `sounding-egress` を分けている。`.env.example` に `FETCH_INTERVAL_MINUTES`・`FETCH_CONTACT`・`EXTERNAL_TIMEOUT_SECONDS`・`CONTENT_RETENTION_DAYS`・`IETF_*` がある。
- **biblioflow の型**：`backend/` に `pyproject.toml`（`requires-python = ">=3.12,<3.13"`、`[tool.uv] package = false`）、`uv.lock`、`alembic/`、`app/`、`tests/`。Dockerfile は `python:3.12-slim` の 2 段ビルドで uv を固定版でコピー。CI は Postgres のサービスコンテナで `uv sync --frozen` → `pytest`（skip があれば失敗）。依存のライセンス検査 `scripts/check-licenses.py` と週次の監査ワークフロー。

## 決定（案）

### パッケージと構成
```
backend/
  pyproject.toml  uv.lock  Dockerfile  alembic.ini  alembic/versions/0001_initial.py
  sounding/
    config.py          # 設定（pydantic-settings、.env の値）
    profile.py         # config/profile.yaml の読み込みと検証
    db.py  models.py   # SQLAlchemy 2.0（同期、psycopg 3）
    sources/
      base.py          # SourceAdapter・Item・Cursor・FetchResult
      ietf_mail.py     # IMAP の実装
      rss.py           # RSS/Atom の実装
    threading.py       # JWZ ＋ base subject（spike/thread.py から移す）
    classify.py        # 自動送信メールの判定
    jobs.py            # fetch / retention の各ジョブ（job_runs を書く）
    worker.py          # `python -m sounding.worker`：スケジューラ
    cli.py             # `python -m sounding …`：migrate / fetch --once / sources / status
  tests/
```
- DB アクセスは**同期**の SQLAlchemy 2.0 ＋ psycopg 3（compose の URL のまま）。worker は逐次処理（同時実行 1）で、非同期にする利点がない。段 3 の FastAPI も同期のセッションで足りる。
- 起動時に `alembic upgrade head` を worker が実行する（biblioflow と同じく、migration は前進のみ）。

### 設定
- `.env`（既存の項目）を `pydantic-settings` で読む。`FETCH_CONTACT` が `change-me` のまま・空のときは **IETF の取得だけを止め**、`job_runs` に理由（`fetch_contact_not_set`）を残す（RSS は動く）。値はログに出さない。
- 追跡対象は `config/profile.yaml`（`docs/DESIGN.md` 2.1）の `ietf.working_groups`・`ietf.extra_lists`・`feeds` から作る。WG 名はそのままリスト名とみなす（`Shared Folders/<wg>` が `LIST` に無ければ、そのソースを `last_status=not_found` にしてほかは続ける）。`ietf.drafts`・`keywords` は段 3 の選別で使う（段 1 では読み込みと検証だけ）。
- `config/profile.example.yaml` をコミットし、`config/profile.yaml`（gitignore 済み）が無ければ例を使わずに「追跡対象なし」で起動する（勝手に取得しない）。
- 取得の対象は起動のたびに profile と `sources` テーブルを突き合わせる。profile から消えたソースは `enabled=false`（データは消さない）。

### `SourceAdapter`
`docs/DESIGN.md` 1.4 の形を具体化する。
```python
@dataclass(frozen=True)
class Item:
    external_id: str            # IMAP: Message-ID（無ければ "uid:<uidvalidity>:<uid>"）、RSS: guid（無ければ link）
    url: str | None             # IMAP: Archived-At、RSS: link
    title: str
    author: str | None          # 表示名（メールアドレスは保存しない。下記）
    published_at: datetime
    body_text: str              # 本文のテキスト（前処理前）
    conversation_hints: ConversationHints | None   # message_id / in_reply_to / references / base_subject
    is_auto: bool               # 自動送信（Auto-Submitted ほか）

class SourceAdapter(Protocol):
    kind: str
    def fetch(self, source: Source, cursor: dict | None) -> FetchResult: ...   # items と次の cursor
```
- `conversation_key` は DESIGN の案から変え、**アダプタはスレッド化のヒント（ヘッダー）を返すだけ**にし、スレッドへの割り当ては情報源に依存しない `threading.py` が DB を見て行う（増分で割り当てるため、既存スレッドを知っている側に置く）。
- `process_state` は拡張 1（Datatracker）まで作らない（YAGNI。型の枠も作らない）。

### IETF ML（IMAP）
- カーソル：`{"uidvalidity": int, "last_uid": int}`。初回は `UID SEARCH SINCE <今日 − IETF_BACKFILL_DAYS>`（既定 30 日）。2 回目以降は `UID FETCH <last_uid+1>:*`。`UIDVALIDITY` が変わったら初回と同じ扱いで取り直す（Message-ID で重複は排除される）。
- 1 回の実行：接続 1 本、リストを順に `EXAMINE`（読み取り専用）、50 通ずつ `UID FETCH (BODY.PEEK[])`、バッチ間 1 秒。1 リストあたりの上限 `IETF_MAX_MESSAGES_PER_RUN`（既定 500）。超えた分は次回。タイムアウトは `EXTERNAL_TIMEOUT_SECONDS`。
- 接続・ログインの失敗は、そのジョブを `failed` にして次の定時まで待つ（その場での再試行は 1 回だけ、30 秒後）。連続失敗の回数を `sources` に持ち、3 回連続で `job_runs` に警告を残す（通知は段 3）。
- 本文：`text/plain` を優先、無ければ `text/html` を標準ライブラリの `html.parser` でテキスト化。添付は保存しない。文字コードは `email` の `policy.default` に任せ、失敗したら `errors="replace"`。
- **個人情報の最小化**：保存するのは From の表示名だけ（表示名が無ければアドレスのローカル部を `a…@` のように伏せる）。メールアドレスは DB に入れない。本文中のアドレスは本文として保存される（保持期間で消える）。
- 自動送信：`Auto-Submitted`（`no` 以外）、`Precedence: bulk|junk`、送信元が Datatracker・`internet-drafts@`・`noreply` 系なら `is_auto=true`（判定理由を `items.auto_reason` に残す）。

### RSS/Atom
- `feedparser` で解析する。取得は `httpx`（`User-Agent: Sounding/<VERSION> (+https://github.com/pathvector-studio/sounding; <FETCH_CONTACT>)`、`FETCH_CONTACT` 未設定なら連絡先なし）。
- カーソル：`{"etag", "last_modified"}` で条件付き GET（304 なら何もしない）。新着の判定は DB の一意制約（`source_id, external_id`）に任せる。
- 本文はフィードの `content` か `summary` を HTML からテキスト化したもの（記事ページは取りに行かない。段 0 の方針どおり要約の材料はフィードの中身だけ）。
- リダイレクトは 5 回まで、応答は 5 MiB まで、`http`/`https` 以外の URL は拒否。
- robots.txt：フィード URL は利用者が明示したものなので確認しない（記事本文を巡回しないため）。README に書く。

### スレッド化（`threading.py`）
- `spike/thread.py` の `build_threads`・`base_subject` を移し、増分で使えるように書き直す：新しい item ごとに
  1. In-Reply-To・References のどれかの Message-ID が既存の item にあれば、そのスレッドに入れる。複数のスレッドに当たったら、古いスレッドに統合する（`items.thread_id` を付け替え、新しい方の `threads` 行は `merged_into` を指す）。
  2. 無ければ、同じソースで base subject が一致し、最後のメッセージが 30 日以内のスレッドに入れる（件名の補助）。
  3. どれにも当たらなければ新しいスレッド。
- 後から親が届いた場合（子が先に取り込まれた）にも、上の 1 で統合される。
- IETF 固有の定数（リストのタグの形、返信接頭辞）は引数で渡す。
- RSS の item はスレッドを作らない（`thread_id` は NULL。単発、`docs/DESIGN.md` 1.2）。
- 検証：段 0 と同じく、テスト用の合成データで「ヘッダーだけ」「件名の補助」「後から親」「統合」を確かめる。実データとの突き合わせは手動で一度（sidrops の 30 日分をサーバーの `THREAD=REFERENCES` と比べ、結果を PR に書く）。

### データモデル（migration `0001_initial`）
`docs/DESIGN.md` 8 章から、段 1 で使うものだけ。
```
sources(id, kind[ietf_mail|rss], key[リスト名|フィード URL], url, enabled, cursor jsonb,
        last_fetched_at, last_status, last_error, consecutive_failures, created_at)
        unique(kind, key)
items(id, source_id → sources, external_id, url, title, author, published_at,
      body_text NULL可, body_hash, message_id, in_reply_to, references text[],
      base_subject, is_auto, auto_reason, thread_id → threads NULL可, fetched_at)
      unique(source_id, external_id)、index(message_id)、index(thread_id, published_at)
threads(id, source_id, root_item_id, subject, base_subject, first_message_at, last_message_at,
        message_count, merged_into → threads NULL可, updated_at)
        index(source_id, base_subject, last_message_at)
job_runs(id, job, source_id NULL可, started_at, finished_at, status[ok|failed|skipped],
         counts jsonb, error)
```
- `threads.last_summarized_item_id`・`thread_states`・`selections`・`digests`・`llm_calls`・`documents` はそれぞれの段で足す。
- エラー文は外部の応答をそのまま入れない（例外の型名と、こちらで決めた短い説明だけ。秘密やアドレスが混ざらないように）。

### ジョブとスケジュール
- `fetch`：`FETCH_INTERVAL_MINUTES`（既定 60）ごと。IETF のリスト群 → フィード群の順に逐次。1 ソースの失敗はほかを止めない。ソースごとに `job_runs` を 1 行。
- `retention`：日次（03:30 TZ）。`fetched_at` が `CONTENT_RETENTION_DAYS` を過ぎた item の `body_text` を NULL にする（メタデータと `body_hash` は残す）。0 なら削除しない。
- スケジューラは **APScheduler 3.x**（`BlockingScheduler`、`max_instances=1`、`coalesce=True`）。起動直後に 1 回 `fetch` を走らせる。同時実行の防止として、ジョブ開始時に PostgreSQL の advisory lock を取る（2 つ目の worker が起動しても二重に取得しない）。
- 終了シグナルで実行中のジョブの区切り（1 ソースの処理後）まで待って止まる。

### CLI
`python -m sounding <command>`（argparse、依存を増やさない）：
- `migrate`：`alembic upgrade head`
- `fetch --once [--source KEY]`：1 回だけ取得（手動確認用）
- `sources`：ソースの一覧と状態（最終取得時刻・状態・連続失敗）
- `status`：直近の `job_runs`、ソースごとの item 数とスレッド数

### compose と Docker
- `backend/Dockerfile`：biblioflow と同じ形（`python:3.12-slim`、uv `0.12.3` を固定でコピー、非 root、`read_only` で動く）。
- compose：`TODO` を外す。worker は `python -m sounding.worker`。**web は段 3 まで compose の `profiles: ["web"]` に入れて既定では起動しない**（→ 要判断 3）。
- `config/` を作り、`config/profile.example.yaml` を置く。worker は `./config:/app/config:ro` を使う（雛形どおり）。

### 依存（追加は承認事項。すべて MIT/BSD/Apache 系）
| パッケージ | 用途 | ライセンス |
|---|---|---|
| `sqlalchemy` 2.0 | ORM | MIT |
| `psycopg[binary]` 3 | PostgreSQL ドライバ | LGPL-3.0（動的リンクで無改変利用。biblioflow の方針どおり LGPL は可、`THIRD_PARTY_NOTICES.md` に記載） |
| `alembic` | migration | MIT |
| `pydantic-settings`（`pydantic`） | 設定 | MIT |
| `pyyaml` | profile.yaml | MIT |
| `feedparser` | RSS/Atom の解析 | BSD-2-Clause |
| `httpx` | HTTP 取得 | BSD-3-Clause |
| `apscheduler` 3.x | スケジューラ | MIT |
| dev：`pytest` | テスト | MIT |

IMAP・メールの解析・HTML のテキスト化は標準ライブラリ（`imaplib`・`email`・`html.parser`）。FastAPI は段 3 で足す。

### CI と監査
- `ci.yml` に `backend` ジョブ：Postgres 16 のサービスコンテナ、`astral-sh/setup-uv`（uv 0.12.3）、`uv sync --frozen`、`pytest`（skip があれば失敗。biblioflow と同じ）、`alembic upgrade head` → `downgrade` は作らない（前進のみ）代わりに「モデルと migration の差分が無い」検査。
- `audit.yml`（biblioflow と同じ形、週次＋PR）：`pip-audit` 相当の既知脆弱性検査と、`scripts/check-licenses.py python`（biblioflow から持ってきて Sounding 用に直す）。
- `dependabot.yml` に `uv`（`/backend`）と `docker`（`/backend`）を足す。
- compose ジョブは `docker compose config -q` に加えて、イメージのビルドが通ることを確かめる。

## 実装計画（PR 単位。承認後、親が 1 項目ずつ subagent に渡して検証する）
1. **backend の雛形と DB**：`pyproject.toml`・`uv.lock`（依存の追加）、`config.py`、`db.py`・`models.py`、migration `0001_initial`、`cli.py` の `migrate`/`status`、Dockerfile、CI の backend ジョブ、`check-licenses.py` と `audit.yml`、`THIRD_PARTY_NOTICES.md`、dependabot。
2. **スレッド化と分類**：`threading.py`（増分・統合・件名の補助）、`classify.py`、合成データのテスト。`spike/` を削除。
3. **IETF ML の取得**：`sources/base.py`・`ietf_mail.py`、`profile.py`、`config/profile.example.yaml`、`fetch` ジョブと `job_runs`、偽の IMAP サーバーによるテスト、`fetch --once`・`sources`。手動で sidrops を 1 回取得し、スレッド化をサーバーの結果と比べる。
4. **RSS/Atom の取得**：`rss.py`、条件付き GET、httpx の MockTransport によるテスト。
5. **worker と運用**：`worker.py`（APScheduler、advisory lock、終了シグナル）、`retention` ジョブ、compose の更新（web を profile に）、README の起動手順、`AGENTS.md` 4.4 に起動とテストの手順、`.env.example`（`IETF_BACKFILL_DAYS`・`IETF_MAX_MESSAGES_PER_RUN`）、`CHANGELOG.md`。`docker compose up -d` で 2 回の定時取得が重複なく回ることを確かめる。

## テスト計画
- **実 DB**（CI の Postgres、ローカルは compose の DB かテスト用 DB）：migration、一意制約による冪等性（同じ取得を 2 回 → 件数が増えない）、スレッドの統合、retention で本文だけが消える、advisory lock で二重実行しない。
- **IMAP**：本物には接続しない。`imaplib.IMAP4` を差し替えられるようにし、偽の実装（`UIDVALIDITY` の変更、`UID FETCH` の範囲、ログイン失敗、途中切断）でアダプタを試す。テスト用のメールはすべて合成（`example.com`、架空の名前）。実際の ML のメールはリポジトリに入れない。
- **RSS**：httpx の `MockTransport` で 200 / 304 / リダイレクト / 巨大応答 / 壊れた XML。
- **スレッド化**：ヘッダーあり、ヘッダーなしで件名の補助、`[Internet]` タグと各言語の返信接頭辞、後から親が届く、2 つのスレッドの統合、References のループ、Message-ID の重複。
- **個人情報**：From のアドレスが DB に入らないこと、ログに `FETCH_CONTACT` とメールアドレスが出ないこと（caplog）。
- CI では外部に接続しない。本物の IMAP・フィードの確認は手動（計画 3・4 の最後）。

## 要判断
1. **DB アクセスの方式**：(a) 同期の SQLAlchemy ＋ psycopg 3（compose の URL のまま） / (b) biblioflow と同じ非同期（asyncpg）。推奨 (a)（worker は逐次処理で、非同期の複雑さに見合う利点がない。psycopg は LGPL だが biblioflow の方針で可）。
2. **初回に遡る期間**：(a) 30 日（`IETF_BACKFILL_DAYS=30`） / (b) 7 日 / (c) 遡らない（起動後の新着だけ）。推奨 (a)（最初のダイジェストで「現在地」が組めるだけの文脈があり、sidrops なら 150 通程度で負荷も小さい。idr はおよそ 8 倍）。
3. **web コンテナの扱い**：(a) 段 3 まで compose の `profiles: ["web"]` で既定では起動しない / (b) 段 1 で最小の web（`/healthz` と取得状況の表示だけ。FastAPI を足す）を作る。推奨 (a)（段 1 の完了条件は worker だけで満たせ、画面は段 3 でまとめて作るほうが手戻りがない。状況は CLI の `status` で見られる）。
4. **送信者の扱い**：(a) 表示名だけを保存し、メールアドレスは保存しない / (b) アドレスも保存する（同姓同名の区別や、将来の「特定の人の発言を重視」に使える）。推奨 (a)（「誰が何を主張したか」は表示名で足りる。アドレスを持たなければ漏えいの影響と公開時の配慮が減る。必要になったらハッシュで持つ方法を検討する）。
