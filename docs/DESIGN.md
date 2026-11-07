# Sounding MVP 設計提案

状態：方針は承認済み（2026-11-07：まずは狭く始めて将来広げる／Python／MIT）。細部は各段の spec で詰める。

## 0. 一言で

**利用者が指定した WG・draft・キーワードについて、IETF メーリングリストのスレッドを「議論の現在地」形式で要約し、RSS の関連記事と合わせて日次ダイジェストとして届ける、単一利用者向けのセルフホストアプリ。**

将来は IETF 以外の一次情報（ほかの ML、GitHub、標準化団体、OSS のガバナンス）にも広げる。そのため、情報源は最初から差し替え可能な形（1.4）で作る。

---

## 1. MVP の範囲

方針：**まずは狭く**。情報源は RSS と IETF メーリングリストの 2 種類だけで始め、「議論の現在地」の質を固めてから広げる。

### 1.1 MVP に入れるもの

| 区分 | MVP に入れる | 理由 |
|---|---|---|
| 情報源 | **RSS/Atom 全般** | 実装が軽く、先行事例で必要性が確認済み |
| 情報源 | **IETF メーリングリスト（利用者が選んだリストのみ）** | プロダクトの核。全リスト（千以上）は扱わない |
| 選別 | 興味プロファイル（YAML）によるルール選別 ＋ 境界例だけ LLM 判定 | 2 章 |
| 要約 | ML スレッド：「議論の現在地」形式／RSS：項目要約 | 3 章 |
| 配信 | Web UI（閲覧）、メール、Webhook（Discord/Slack 互換）、Atom フィード出力 | |
| 利用者 | **単一利用者**（認証は Web UI のパスワード 1 つ、または Tailscale/Cloudflare Access 前提） | マルチユーザーは MVP の複雑さに見合わない |
| LLM | Anthropic / OpenAI（BYO キー） | 要件。抽象化層を最初から作る |

### 1.2 MVP の範囲についての判断

- **RSS は「現在地」形式の対象にしない。** ブログ記事やニュースは単発でスレッドを持たないので、通常の項目要約にする。「現在地」は ML のスレッド単位でだけ作る。
- **Datatracker の文書状態は MVP の直後（最初の拡張）に回す。** ML の議論だけだと「合意がどこまで進んだか」を LLM の読み取りに頼ることになる。Datatracker には draft の改版・WG 採択・WG Last Call・IESG 評価といった**客観的な手続き上の位置**が JSON であるので、これを「現在地」の骨格にすると正確さが大きく上がる。MVP では出力スキーマに `process_state` の枠だけ用意して空にしておき、拡張で埋める。

### 1.4 将来の拡張と、そのための作り

将来の情報源の候補（優先度の高い順の目安。順番は `specs/roadmap.md` で管理）：

| 段 | 情報源 | 「現在地」の単位 |
|---|---|---|
| 拡張 1 | IETF Datatracker（文書状態） | draft |
| 拡張 2 | GitHub Issues / PR / Discussions（多くの WG が draft の議論に使う） | issue・PR |
| 拡張 3 | ほかの ML（NANOG・RIPE・JANOG・各 RIR の政策 ML など） | スレッド |
| 拡張 4 | ほかの標準化団体・OSS のガバナンス（W3C、Linux カーネルの ML、各 OSS の RFC プロセス） | スレッド・提案 |
| 拡張 5 | 会合の議事録・スライド・録画の文字起こし | 会合 |

これに備えて、MVP から次の境界を守る。

- **情報源アダプタ**：`SourceAdapter` を共通の型にし、RSS と IETF ML はその実装の 1 つとして作る。アダプタが返すのは「項目（item）」と、あれば「会話のまとまり（conversation：スレッド・issue など）」と「手続き上の状態（process state）」の 3 種類だけ。
  ```python
  class SourceAdapter(Protocol):
      kind: str                                    # "rss" | "ietf-mail" | 将来 "github" ...
      def fetch(self, since: Cursor) -> FetchResult: ...       # 新着の item と次のカーソル
      def conversation_key(self, item: Item) -> str | None: ...  # スレッド化のキー（なければ単発）
      def process_state(self, ref: str) -> ProcessState | None: ...  # 手続き上の位置（なければ None）
  ```
- **要約は情報源に依存させない**：「議論の現在地」の生成は conversation と process state だけを入力にし、IETF 固有の知識（WG、Last Call など）はアダプタ側と、プロンプトに差し込む「その情報源の手続きの説明」に閉じ込める。
- **興味プロファイルも情報源ごとの節に分ける**（`ietf:`・`feeds:`、将来 `github:` …）。共通の `keywords:` は全情報源にかかる。
- **DB は情報源の種類を列で持つ**（8 章の `sources.kind`）。情報源を足してもテーブルは増やさない。

**MVP から外す**：上の拡張すべて、マルチユーザー、Web UI でのプロファイル編集、通知のリアルタイム化。

### 1.3 IETF ML の取得手段（要検証）

実装前に小さなスパイクで確かめる必要がある。候補：

| 手段 | 長所 | 懸念 |
|---|---|---|
| IMAP（RFC 7017 の要件に基づく匿名アクセス） | リスト単位・メッセージ単位で差分取得しやすい。Message-ID / In-Reply-To でスレッド化できる | 実際のサーバー名・認証方式・レート制限を確認する |
| mailarchive.ietf.org の Web / エクスポート / フィード | HTTP だけで済む | 提供形式と利用条件を確認する |
| 旧 FTP の月次 mbox（`ietf-mail-archive/<list>/YYYY-MM.mail`） | 単純 | 2022 年で更新が止まっているリストがあり、現行の手段ではない |

どの手段でも、取得は**低頻度（例：1 時間ごと）・差分のみ・丁寧な User-Agent** を守る。

---

## 2. 興味プロファイル

### 2.1 持ち方

MVP では **`config/profile.yaml`**（ファイル）で持つ。Web UI の編集は第二弾。ファイルにしておくと git で管理でき、OSS 利用者も例をコピーして始められる。

```yaml
# config/profile.yaml の例
profile:
  language: ja            # 要約の出力言語
  digest:
    schedule: "0 7 * * *" # 配信時刻（cron、TZ は .env）
    max_threads: 10       # 1 回のダイジェストに載せる ML スレッドの上限
    max_items: 15         # RSS 項目の上限

ietf:
  working_groups:         # WG 名を書くと ML と Datatracker の両方を追う
    - sidrops
    - idr
    - grow
  drafts:                 # WG をまたいで特定の draft を追う（前方一致）
    - draft-ietf-sidrops-aspa-
    - draft-ietf-idr-bgp-sendholdtimer
  extra_lists:            # WG 以外のリスト
    - ietf                # 例：全体リスト（量が多いので keywords で強く絞る）

feeds:
  - url: https://blog.apnic.net/feed/
    weight: 1.0
  - url: https://datatracker.ietf.org/feed/rfc/
    weight: 0.5

keywords:
  include: [RPKI, ASPA, "route leak", BGPsec, "hold timer"]
  exclude: [crypto, NFT]

people: []                # 将来：特定の人の発言を重視（MVP では使わない）
```

### 2.2 選別のしかた（安い順に段階的に）

1. **ルールで確定**：追跡中の WG のリスト、追跡中の draft 名を件名・本文に含むスレッド → 必ず対象。
2. **ルールで除外**：`exclude` に一致、自動送信メール（Datatracker 通知・議題配布など、ヘッダーで判定）→ 除外または「手続き更新」として別枠。
3. **スコアリング**：キーワード一致数、スレッドの活発さ（新着件数・参加者数）、Datatracker の状態変化（WGLC 開始などは加点）。
4. **境界例だけ LLM 判定**：スコアが閾値付近のものだけ、安価なモデルに「この利用者の関心に合うか」を Yes/No＋理由で聞く。

選別理由（どのルール・スコアで入ったか）は記録して UI に出す。利用者が「なぜこれが来たのか」を分かるようにし、プロファイル調整の手がかりにする。

---

## 3. 要約の形式：「議論の現在地」

### 3.1 単位

- **スレッド**：Message-ID / In-Reply-To / References でまとめる。件名の `Re:` や改題（`[sidrops] WGLC: ...` → `Re: ...`）も補助的に使う。
- **文書（draft）**：同じ draft に言及する複数スレッド＋Datatracker の状態を束ねた上位の単位。ダイジェストでは文書単位の見出しの下にスレッドを並べる。

### 3.2 出力スキーマ（JSON Schema で強制する）

（以下の値はすべて形を示すための架空の例）

```jsonc
{
  "thread_id": "…",
  "title": "draft-ietf-sidrops-aspa-verification の WGLC",
  "status_line": "WGLC 中（〆切 11/20）。主要論点 2 件のうち 1 件は合意、1 件は未決着",
  "process_state": {                // Datatracker 由来（LLM ではなく取得値）。MVP では null、拡張 1 で埋める
    "doc": "draft-ietf-sidrops-aspa-verification-19",
    "state": "In WG Last Call",
    "since": "2026-11-03"
  },
  "issues": [
    {
      "issue": "上流方向の検証で AS_SET を含む経路をどう扱うか",
      "positions": [
        { "stance": "Invalid とすべき", "by": ["A. Author", "B. Reviewer"], "refs": ["msg-id-1"] },
        { "stance": "Unknown に留めるべき", "by": ["C. Operator"], "refs": ["msg-id-2"] }
      ],
      "status": "open",            // agreed | leaning | open | parked
      "evidence": "議長が 11/05 に「現行テキストのままで rough consensus」と判断",
      "refs": ["msg-id-3"]
    }
  ],
  "agreed": ["…（何が合意済みか、出典つき）"],
  "open_questions": ["…"],
  "next": ["11/20 WGLC 〆切", "IETF 126 で議論予定"],
  "changed_since_last": "前回から：論点 1 が open → agreed（議長判断）",
  "sources": [{ "msg_id": "…", "url": "https://mailarchive.ietf.org/…", "date": "…", "from": "…" }]
}
```

設計上の規則：

- **合意・未決着の判断には必ず根拠メッセージ（refs）をつける**。根拠がなければ `open` とし、推測で `agreed` にしない。
- 手続き上の状態（`process_state`）は **Datatracker の取得値だけ**を使い、LLM に書かせない。
- 「誰が何を主張したか」は名前を出す（ML は公開記録で、誰の意見かは判断材料として重要）。ただし発言の長い引用はしない（7 章）。
- **`changed_since_last`** がダイジェストの主役。毎日読む人に必要なのは「前回から何が動いたか」。

### 3.3 生成手順（差分要約）

```
初回:       全メッセージ（長ければ時系列に分割して要約→統合）→ 状態 S1
新着あり:   S(n) ＋ 新着メッセージだけ → S(n+1) と changed_since_last
新着なし:   LLM を呼ばない。前回の状態をそのまま使う（ダイジェストには載せない）
```

- メッセージ本文は前処理で**引用部分（`>` 行）・署名・定型フッター（Note Well 等）を除去**してから渡す。ML は引用の重複が非常に多いので、これだけで入力が大きく減る。
- 状態 S は JSON として DB に保存する（要約の履歴が残り、「いつ合意が動いたか」を後から辿れる）。
- 状態が長くなりすぎたら（例：論点 10 件超）、`parked` の論点を畳む。

### 3.4 ダイジェストの構成

```
📍 Sounding — 2026-11-08
■ 動きがあった文書（現在地の変化が大きい順）
  draft-ietf-sidrops-aspa-verification  [WGLC 中 → 〆切 11/20]
    変化：論点 1 が合意（議長判断）／論点 2 は未決着
    → スレッド 2 件・新着 14 通
■ 手続きの更新（Datatracker）
  draft-ietf-idr-…  WG 採択 / RFC Editor Queue 入り …
■ 関連記事（RSS）
  APNIC Blog: … — 1〜2 文の要約
```

---

## 4. アーキテクチャ

### 4.1 構成（Docker Compose）

```
            ┌──────────────┐   cron (APScheduler)
            │ sounding-     │──► ingest: RSS / IETF ML / Datatracker
            │ worker        │──► select → summarize(diff) → digest → deliver
            └──────┬───────┘
                   │ SQL
┌──────────────┐   ▼          ┌──────────────┐
│ sounding-web │──► sounding-db (PostgreSQL 16)
│ (API + UI)   │               └──────────────┘
└──────┬───────┘
       ▼ 127.0.0.1:${SOUNDING_PORT}  → LAN / Tailscale / Cloudflare Tunnel（biblioflow と同じ 3 モード）
```

| コンテナ | 役割 | 備考 |
|---|---|---|
| `sounding-db` | PostgreSQL 16 | スレッド・メッセージ・状態・ダイジェスト・LLM 呼び出しログ |
| `sounding-worker` | 取得・選別・要約・配信のスケジュール実行 | 同時実行 1。ジョブは冪等 |
| `sounding-web` | 閲覧 UI と最小 API、Atom フィード出力 | MVP はサーバーサイドレンダリングで十分 |

- 言語は **Python（FastAPI ＋ SQLAlchemy、uv ＋ Alembic）**（2026-11-07 決定）。biblioflow の backend と同じ型にする。`feedparser`、標準ライブラリの `imaplib`/`email` が使える。
- Redis は MVP では使わない（ジョブキューが不要な規模。必要になったら biblioflow と同じく Valkey を足す）。
- 埋め込み（pgvector）も MVP では使わない。関連スレッドの束ね直しなどで必要になったら追加する。

### 4.2 LLM 抽象化

```python
class LLMProvider(Protocol):
    def complete_json(self, *, system: str, user: str, schema: dict,
                      tier: Literal["cheap", "main"], max_tokens: int) -> LLMResult: ...
```

- 実装：`AnthropicProvider` / `OpenAIProvider`（将来 `OpenAICompatibleProvider` で Ollama 等）。
- 呼び出し側は**用途の段（cheap / main）だけを指定**し、具体的なモデル名は `.env` で決める。
  - cheap：選別の境界判定、RSS 項目要約
  - main：「議論の現在地」の生成
- すべての呼び出しを `llm_calls` テーブルに記録（プロバイダ・モデル・入出力トークン・推定コスト・用途・キャッシュ命中）。予算管理と「どのモデルで作ったか」の表示に使う（先行事例で見えた表示名の不一致を防ぐ）。

### 4.3 運用の型（biblioflow と揃える）

| 項目 | 方針 |
|---|---|
| `.env` | `.env.example` を追跡、`.env` は追跡しない。必須値は compose 側で `${VAR:?}` で検査。シークレットは `change-me-*` をインストーラが生成値で置き換える |
| `STATUS.md` | 進行中／承認待ち／予定／詰まり／直近の完了（biblioflow と同形式） |
| コンテナ | `read_only: true`、`no-new-privileges`、ログのローテーション、`restart: unless-stopped`、内部ネットワークと外向けネットワークを分離。LLM キーは worker だけに渡す |
| 公開モード | A. LAN（http）／ B. Tailscale／ C. Cloudflare Tunnel（＋Access）の 3 つを README に書く |
| install（将来像） | `curl -sSL https://sounding.pathvector.dev/install.sh \| bash`。clone → `.env` 生成（シークレット生成、LLM プロバイダとキーを対話で聞く、キー未設定でも起動はできる）→ `docker compose up -d`。環境変数 `SOUNDING_DIR` / `SOUNDING_REF` / `SOUNDING_PORT` / `SOUNDING_NO_START` で上書き（biblioflow の install.sh と同じ形） |
| バックアップ | `pg_dump` の日次スナップショット（biblioflow と同じスクリプトの型を流用） |
| 自己検査 | 先行事例の発想を取り込み、各ジョブの成否・取得件数・LLM 失敗を `job_runs` に記録。失敗時は配信チャネルに通知。Web UI に「最後に成功した時刻」を表示 |
| 紹介サイト | **同じリポジトリの `site/`** に静的サイトとして置く（2026-11-07 決定、4.4） |

### 4.4 紹介サイト（`sounding.pathvector.dev`）

biblioflow はアプリ（biblioflow）とサイト（biblioflow-web）を別リポジトリに分けているが、Sounding では**分けない**。

- **分けない理由**：サイトは小さな静的ページ（紹介・インストール手順・`install.sh` の配信）で、ビルド工程も独自の依存もない。同じリポジトリなら、README・install.sh・リリースとサイトの説明が 1 つの PR でずれずに更新でき、親セッションがリポジトリをまたぐ受け渡しも不要になる。
- **置き場所**：`site/`（HTML/CSS のみ、ビルドなし）。`install.sh` はリポジトリ直下を正とし、デプロイ時に `site/` へコピーする（サイト側を直接編集しない。biblioflow と同じ）。
- **デプロイ**：Cloudflare Pages。出力ディレクトリ `site/`、**ビルドの監視パスを `site/**` と `install.sh` に限定**して、アプリの変更でサイトが再デプロイされないようにする。
- **公開の承認**：biblioflow-web と同じく、**`site/` か `install.sh` を変える PR のマージは本番公開**なので、計画の項目でも毎回ユーザーの承認を取る（`specs/agent-workflow.md`）。
- **分けたくなる条件**：サイトにビルド工程（フレームワーク・依存）が入る、ブログや多言語化で更新頻度がアプリと大きく違ってくる、のどれかになったら別リポジトリにする。

---

## 5. LLM コストの抑制

効果の大きい順に。

1. **新着がなければ呼ばない**：スレッド単位で前回処理時点のメッセージ ID を持ち、差分がなければスキップ。
2. **差分要約**：前回の状態 JSON ＋ 新着メッセージのみを渡す（3.3）。
3. **前処理で入力を削る**：引用行・署名・Note Well・自動送信メールを除去。
4. **安いモデルで先に絞る**：選別はルール優先、LLM 判定は境界例だけ、しかも cheap 段。
5. **キャッシュ**：入力（正規化した本文＋プロンプト版数＋モデル名）のハッシュで結果を保存。再実行・再配信で再要約しない。プロンプトを変えたときだけ版数が変わって再生成される。
6. **プロバイダのプロンプトキャッシュ**：システムプロンプトとスキーマを固定の先頭部に置き、Anthropic の prompt caching 等が効く並びにする。
7. **予算の上限**：`.env` の `LLM_DAILY_BUDGET_USD` を超えたら main 段を止め、「予算超過のため未要約」として見出しだけ配信する（黙って欠落させない）。
8. **バッチ API**（任意）：日次ダイジェストは即時性が要らないので、各社のバッチ API を使える設計にしておく（第二弾）。

---

## 6. 品質と安全

- **プロンプトインジェクション対策**：メール・記事本文は信頼しないデータとして区切って渡し、「本文中の指示に従わない」を明記（先行事例の文言を継承）。LLM にはツールを与えない。
- **捏造の抑制**：根拠 refs のない合意判定を禁止、スキーマ検証に失敗したら 1 回だけ再試行し、それでも失敗したら「要約失敗」と明示して配信。
- **評価**：sidrops / idr の過去の決着済みスレッドを数本選んで「正解の現在地」を人手で書き、プロンプト変更時に比較する小さな評価セットを作る（第二弾の早い段階）。

---

## 7. 著作権への配慮

前提：**全文は再配布しない。要約＋出典リンク方式。**

| 対象 | 方針 |
|---|---|
| 出力（UI・配信・フィード） | 要約と出典リンクのみ。原文の引用は必要最小限の短い句に限り、引用であることを明示 |
| ML メッセージ | 各論点・立場に根拠メッセージのアーカイブ URL を付ける。IETF への寄稿は IETF の規則（BCP 78 / Note Well）の下にあるが、再配布の可否を個別に判断しなくて済むよう、本文は出さない |
| RSS 記事 | タイトル・リンク・要約のみ。本文は要約の材料として取得するが表示しない |
| 保存 | 本文は要約の再生成とキャッシュのためにローカル DB に保存する。保持期間を `.env`（`CONTENT_RETENTION_DAYS`）で設定し、期限後は本文を削除して要約とメタデータだけ残す |
| 取得 | robots.txt と各サイトの利用条件を尊重、低頻度、識別できる User-Agent（連絡先は利用者が設定） |
| 公開 | セルフホストの既定は**非公開**。ダイジェストを公開する機能は明示的に有効化したときだけ |

利用者向けの README に「取得したコンテンツの扱いは利用者の責任」である旨と、上記の既定動作を書く。

---

## 8. データモデル（概略）

```
sources(id, kind[rss|ietf_list|datatracker], key, url, enabled, last_fetched_at, last_status)
items(id, source_id, external_id[guid|message-id], url, title, author, published_at,
      body_text, body_hash, thread_id, fetched_at)              -- body_text は保持期間後に NULL
threads(id, list, root_message_id, subject, doc_name, last_message_at, last_summarized_item_id)
thread_states(id, thread_id, version, state_json, changed_since_last, prompt_version, model, created_at)
documents(name, wg, rev, state, state_since, updated_at)        -- Datatracker 由来
selections(item_or_thread_id, score, reasons_json, decided_by[rule|llm], created_at)
digests(id, period_start, period_end, body_json, delivered_at, channels)
llm_calls(id, purpose, provider, model, tier, input_tokens, output_tokens, cost_usd, cache_hit, created_at)
job_runs(id, job, started_at, finished_at, status, counts_json, error)
```

---

## 9. 段階計画（提案）

| 段 | 内容 | 完了の目安 |
|---|---|---|
| 0 | **スパイク**：IETF ML の取得手段の確認（1.3）、sidrops の 1 か月分でスレッド化と前処理の削減率を測る | 取得手段の決定、入力トークンの見積もり |
| 1 | `SourceAdapter` と取得（RSS・IETF ML）、DB、`job_runs` | 毎時の取得が冪等に回る |
| 2 | LLM 抽象化（Anthropic/OpenAI）、「現在地」生成、差分要約、キャッシュ、予算 | 1 スレッドの状態履歴が差分で更新される |
| 3 | 選別とダイジェスト生成、Web UI、配信（メール・Webhook・Atom） | 自分の日次ダイジェストが届く |
| 4 | install.sh、README の 3 公開モード、バックアップ | 別マシンに 1 コマンドで入る |

MVP の後は 1.4 の拡張を順に（`specs/roadmap.md`）。各段は着手前に `specs/<name>.md` を書いて承認をもらう（`specs/agent-workflow.md`）。

---

## 付録 A：命名

### 評価した候補

| 候補 | 由来 | 短さ・響き | repo / サブドメイン | 既存との紛らわしさ | コンセプトとのつながり | 判定 |
|---|---|---|---|---|---|---|
| **sounding** | 測深（位置を確かめる）＋ sound out（意見・合意を探る） | ○ 8 文字、英語として自然 | ○ | ○ 同名の著名な技術製品は見当たらない（一般語なので検索性はやや弱い） | ◎ 「現在地を測る」と「合意の温度を探る」の両方 | **採用** |
| hum | IETF のハミングによる合意確認（RFC 7282） | ◎ 3 文字 | ○ | × 日本の App Store に「Hum - AI Radio」（AI によるニュース系音声アプリ）があり、領域が近い。商標も複数 | ◎ IETF 文化に直結 | 不採用 |
| roughcon | "rough consensus and running code" | △ 造語で読みにくい | ○ | ○ | ○ 合意の度合い | 不採用 |
| lastcall | IETF の Last Call | ○ | ○ | × IETF 内の用語・リスト名（last-call ML）そのもので、ドメインの中でかえって紛らわしい。手続きの一段階だけを指す | △ | 不採用 |
| nexthop | 次に見るべきところ（ルーティング用語） | ◎ | ○ | × Nexthop AI（AI データセンター向けネットワーク企業、2025 年創業・大型調達）と同名で同業界 | ○ | 不採用 |

### 選定理由

`sounding` は、プロダクトの核である「一次情報の海の中で議論の現在地を確かめる」を比喩として一語で表し、さらに IETF の合意形成（意見を探る＝sound out）にも掛かる。ネットワーク・ルーティングの直接の用語ではないが、PathVector（経路）の世界観の航海の比喩と相性が良い。同業界の著名製品との衝突が見当たらず、`pathvector-studio/sounding`・`sounding.pathvector.dev`・`sounding-worker` のようにそのまま使える。

弱点は一般名詞ゆえの検索性で、これは「Sounding — where the discussion stands」のような説明的な副題を常に添えて補う。

### 追加の候補（2026-11-07）

| 候補 | 由来 | 評価 |
|---|---|---|
| roomsense | IETF の "sense of the room"（議長が場の合意を読むこと） | 衝突は見当たらない。ただし IETF 色が強く、将来 IETF 以外に広げると名前が狭くなる |
| tideline | 潮が寄せた跡の線＝議論がどこまで来たか | 衝突は小さい。意味の伝わりやすさは sounding に劣る |
| plumbline | 下げ振り・測深索。基準線を測る | 衝突は小さい。やや長く、"plumb" の連想（配管）がぶれる |
| statepath | 状態（state）＋経路（PathVector との連続性） | 衝突は見当たらない。説明的だが印象に残りにくい |
| leadline | 測深に使う錘付きの索（sounding そのものの道具） | 英語ではリード獲得（営業）系のアプリ名と重なる |
| fathom | 水深の単位・「理解する」 | Fathom（Web 解析、AI 議事録）が有名で不可 |
| quorum | 定足数 | ブロックチェーン基盤など既存製品が多く不可 |

**将来 IETF 以外に広げる**方針（1.4）を踏まえると、IETF の用語に寄った名前（hum、lastcall、roomsense）より、一般的な比喩である sounding のほうが長く使える。
