# Sounding

> 一次情報の「議論の現在地」を測る、技術者向けのセルフホスト型ダイジェスト。

## 名前について

**Sounding**（サウンディング）は二つの意味を重ねた名前です。

- **測深（sounding）**：航海で水深を測り、海図と照らして自船の位置を確かめる作業。押し寄せる一次情報の中で「いま自分はどこにいるのか」を確かめる、というこのツールの役割そのものです。PathVector の「経路」の比喩ともつながります。
- **sound out（意見を探る）**：IETF では合意の度合いをハミング（hum）で測ります（RFC 7282）。メーリングリストの議論から「どこまで合意が進み、何が未決着か」を読み取ることは、場の温度を測る行為です。

リポジトリ名・サブドメイン（`sounding.pathvector.dev`）・CLI 名・コンテナ名の接頭辞にそのまま使えます。

候補比較と選定理由は [docs/DESIGN.md の「付録 A：命名」](docs/DESIGN.md#付録-a命名) を参照してください。

## コンセプト

追うべき一次情報が多すぎる専門職のための、**全部は見せない**ニュース集約です。

1. RSS フィードと **IETF メーリングリスト**（＋ Datatracker の文書状態）を集める
2. 利用者の **興味プロファイル**（WG 名・draft 名・キーワード・情報源）で選別する
3. LLM でスレッドごとに **「議論の現在地」** を要約する
   - 何が争点か
   - 合意はどこまで進んだか
   - 何が未決着か／次に何が起きるか
4. 要約＋出典リンクのダイジェストとして届ける（Web・メール・Webhook・Atom フィード）

一般向けのニュース要約アプリとは競合しません。速報性や網羅性ではなく、「専門家が自分で追うと何時間もかかる議論の状態を、数分で把握できること」に価値を置きます。

## 想定読者

- まずはネットワークエンジニア（IETF の routing / ops / security 系 WG、RIR・NOG の動向を追う人）
- 将来的には、一次情報の議論を追う必要がある他の専門職（標準化・OSS のガバナンス等）

## 設計上の前提

- **セルフホスト / OSS**：Docker Compose で動く。データは自分の手元に置く
- **まずは狭く、将来は広く**：最初は RSS と IETF メーリングリストだけ。情報源はアダプタで足していく（GitHub、ほかの ML、ほかの標準化団体など）
- **LLM は BYO キー**：Anthropic / OpenAI を切り替えられる抽象化。キーは利用者が `.env` に設定する
- **全文は再配布しない**：要約と出典リンクのみを出す（[docs/DESIGN.md](docs/DESIGN.md#7-著作権への配慮)）
- **差分だけ要約する**：スレッドの前回の状態＋新着メッセージだけを LLM に渡し、コストを抑える

## 開発状況

**段 3（選別・ダイジェスト・Web UI・配信）まで実装済み**。IETF メーリングリスト（匿名 IMAP）と RSS/Atom を PostgreSQL に取り込み、IETF の会話（スレッド）ごとに「何が争点か・合意はどこまでか・何が未決着か・前回から何が動いたか」を LLM で要約して版ごとに保存します（新着があった会話だけを前回の状態からの差分で更新）。毎日、興味プロファイルで会話と RSS 記事を選んでダイジェストにまとめ、Web UI・Atom フィード・メール・webhook（Discord / Slack）で届けます。1 コマンドのインストール、公開モードの手順、バックアップは段 4 です。

| ドキュメント | 内容 |
|---|---|
| [docs/DESIGN.md](docs/DESIGN.md) | MVP の設計（範囲・興味プロファイル・要約形式・アーキテクチャ・コスト・著作権） |
| [docs/SURVEY.md](docs/SURVEY.md) | 先行事例（個人用ニュースまとめ）の調査 |
| [specs/roadmap.md](specs/roadmap.md) | MVP と、その後の情報源の拡張 |
| [STATUS.md](STATUS.md) | 進行中・承認待ち・予定・詰まり |

## 起動手順

Docker Compose が必要です。

1. `.env.example` を `.env` にコピーし、`POSTGRES_PASSWORD`、`FETCH_CONTACT`（連絡先のメールアドレス）、`IDENTITY_SECRET`（`openssl rand -hex 32` で生成。最初の起動のあとは変えない）を設定する
2. `cp config/profile.example.yaml config/profile.yaml` し、追いたい WG・リスト・フィードを書く
3. `docker compose up -d --build`
4. 動作を確認する
   ```bash
   docker compose exec sounding-worker python -m sounding status
   docker compose exec sounding-worker python -m sounding sources
   ```

worker は起動直後に 1 回取得し、その後は `FETCH_INTERVAL_MINUTES` ごとに取得します。`CONTENT_RETENTION_DAYS` を過ぎた本文は毎日 03:30 に消します（メタデータは残す）。`FETCH_CONTACT` と `IDENTITY_SECRET` のどちらかが未設定のあいだ、IETF のリストは取得されません（RSS は動きます）。`sounding-web`（Web UI）も `docker compose up` で一緒に起動します（下の「Web UI（段 3）」）。

## LLM による要約（段 2）

既定は `LLM_PROVIDER=none` で、キーを入れるまで何も外に送りません（要約ジョブは `skipped`（`llm_disabled`）を残して終わります）。有効にするには `.env` に次を設定し、worker を再作成します。

- `LLM_PROVIDER=anthropic` または `openai`、`LLM_API_KEY`（利用者自身のキー）
- `LLM_MODEL_MAIN`（「現在地」の生成）と `LLM_MODEL_CHEAP`（長い会話の分割要約）。anthropic は空か `change-me` のままなら既定（main `claude-opus-5-5`、cheap `claude-haiku-5-5`）。openai は既定がないので両方を明示する
- `LLM_DAILY_BUDGET_USD`（既定 1.00。0 は無制限）

要約は毎日 06:00（`TZ`）に worker が実行します。手動では次のとおりです。

```bash
docker compose exec sounding-worker python -m sounding summarize --once [--conversation ID]
docker compose exec sounding-worker python -m sounding show <会話 ID> [--language ja]
```

- **外に送るもの**：件名、前処理した本文（引用・署名・定型フッターを除き、メールアドレスは伏せたもの）、送信者の表示名。選んだプロバイダへ送られます。キーは worker だけが受け取り、ログにもデータベースにも出しません。
- **費用の上限**：1 日（`TZ` の暦日）の推定費用が `LLM_DAILY_BUDGET_USD` に達するとその日の残りの要約を止め、止めた会話の ID を `job_runs`（`python -m sounding status` で見える）に「予算超過のため未要約」として残します。同じ入力の再実行は記録済みの結果を使うので課金されません。`status` で当日の費用を確認できます。
- **作り直せない場合**：取得した本文は `CONTENT_RETENTION_DAYS`（既定 90 日）を過ぎると消えるため、それより古い会話は状態を作り直せません（保存済みの状態は残ります）。
- 表示言語は `config/profile.yaml` の `profile.language`（既定 `en`）です。

## ダイジェスト（段 3）

worker は `config/profile.yaml` の `profile.digest.schedule`（cron 形式、`TZ` で解釈。既定 `0 7 * * *`）にダイジェストを 1 つ作り、本文（要約とリンクだけ。メッセージや記事の本文は入らない）をデータベースに残します。期間は「直前の実行時刻から今回の実行時刻まで」（最初の回は 24 時間）です。スケジュールは worker の起動時に読むので、変えたら worker を再起動してください（そのほかのプロファイルは実行のたびに読み直します）。

```bash
docker compose exec sounding-worker python -m sounding digest --once [--no-deliver]   # 今の期間のダイジェストを作って表示
docker compose exec sounding-worker python -m sounding show-digest [ID]                # 保存したダイジェストを表示（既定は最新）
```

- 動きがない日も「動きなし」のダイジェストを作ります（届かないのが故障か静かな日かを区別するため）。LLM が無効、ジョブの失敗、予算超過で要約しなかった会話があるときは、その旨が健康状態の節に出ます。
- 同じ期間は**その場で作り直します**（ID は同じ、選別と本文を再計算、配信の記録には触れない）。このため、その日の定時より前に手動で `digest --once` を実行すると、前日のダイジェストを作り直します。
- リンクは `PUBLIC_BASE_URL` を基にします（Web UI は下の「Web UI（段 3）」）。作ったダイジェストの送り方は下の「配信（段 3）」です。`--no-deliver` を付けると作るだけで何も送りません。

- `FETCH_CONTACT` は、IETF の匿名 IMAP のパスワードと、RSS 取得時の User-Agent に入ります（IETF の案内どおり）。
- フィードの URL は指定されたとおりに取得します。記事のページはたどらないので、robots.txt は参照しません。

## 配信（段 3）

`.env` の `DELIVERY_CHANNELS`（カンマ区切り、既定 `web,atom`）で、ダイジェストを作った直後に worker が送るチャネルを選びます。`web`（Web UI）と `atom`（フィード）は DB に入っていれば読めるので送る処理はありません。`email` と `webhook` を足し、下の値を `.env` に書いて `docker compose up -d` で worker を作り直します。

**送るもの**：要約（件名、「現在地」の 1 行、前回からの変化）、手続きの更新、関連記事の要約、出典リンク、Web UI のダイジェストへのリンクだけです。メッセージや記事の本文は送りません。動きがない日の「動きなし」のダイジェストも送ります。

- **webhook（Discord / Slack）**：チャネルの設定で Incoming Webhook を作り、その URL を `WEBHOOK_URL` に書きます（Discord は「連携サービス」→「ウェブフック」、Slack は Incoming Webhooks アプリ）。URL 自体が資格情報なので、誰にも見せず、`.env` の外（git）に置かないでください。ホストが `discord.com` なら `content`、`hooks.slack.com` なら `text`、それ以外（互換サーバー）は両方を含む JSON を送ります。Discord の上限（2000 文字）に収まるよう、見出し・健康状態・会話ごとの件名と「現在地」の 1 行とリンク・件数・ダイジェストへのリンクの順にまとめ、入りきらない会話は「... +n / m」に省きます（リンクは必ず最後に残る）。リダイレクトは辿らず、2xx 以外は失敗です。
- **email（SMTP）**：`SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASSWORD`、`SMTP_FROM`、`DIGEST_TO`（カンマ区切りで複数可）を書きます。ポート **587** は STARTTLS、**465** は最初から TLS です。サーバーが STARTTLS を出さないときは `starttls_unavailable` で失敗し、無暗号には戻りません（サーバー証明書は検証します）。`SMTP_USER` を空にするとログインしません。本文はプレーンテキストと HTML の両方です。
- 有効にしたのに値が空や `change-me` のままのチャネルは、そのチャネルだけ `not_configured` で失敗として記録されます（ダイジェストの作成は止まりません）。
- **記録と再送**：チャネルごとに成否、試行回数、最後の試行、短いエラー（`http_404`、`timeout`、`connect_error`、`smtp_auth_failed` など。URL・アドレス・サーバーの応答は入れない）を `deliveries` に残します。状態ページと `python -m sounding status` で見えます。送れたチャネルには二度送りません（同じ期間の作り直しでも）。失敗したチャネルは**次のダイジェストの実行で 1 回だけ**再送します（最初と合わせて 2 回まで。再送の対象は 3 日以内の期間のダイジェストだけ）。同じ日に手動で `digest --once` を続けて実行すると、その分も試行に数えます。
- **手動の再送**：`deliver` は 1 つのダイジェストを送り直します。試行回数の上限は無視しますが、送れたチャネルは `--force` を付けない限り飛ばします。

```bash
docker compose exec sounding-worker python -m sounding deliver --digest <ID> [--channel email|webhook] [--force]
```

- 秘密（webhook の URL、SMTP のユーザー・パスワード、宛先のアドレス）はログ・`job_runs`・`deliveries`・CLI の出力に出しません。web のコンテナには渡しません。

## Web UI（段 3）

`docker compose up -d` で `sounding-web` も起動します。ブラウザで `http://localhost:${SOUNDING_PORT}`（既定 `http://localhost:8080`、127.0.0.1 にだけ公開）を開くと、最新のダイジェスト、ダイジェスト一覧、会話ごとの「議論の現在地」（版の履歴・選ばれた理由つき）、状態ページ（ジョブ・情報源・配信・当日の LLM 費用・Atom フィードの URL）が読めます。読み取り専用で、データベースには何も書きません。画面の言語は `profile.language`（ja / en）に従います。メッセージや記事の本文は表示せず、要約と出典リンクだけです。

**パスワード**：Web UI のログインは `.env` の `WEB_PASSWORD_HASH` で決まります。

```bash
docker compose exec sounding-worker python -m sounding hash-password     # パスワードを 2 回聞かれ、ハッシュだけを表示する
# ホストで直接: cd backend && uv run python -m sounding hash-password   （スクリプトからは --stdin で 1 行読む）
```

表示された `scrypt$...` を `.env` に書き、`docker compose up -d` で web を作り直します。**`$` が compose に解釈されないよう、値全体を単一引用符で囲みます**（`$$` に置き換えても同じです）。

```
WEB_PASSWORD_HASH='scrypt$32768$8$1$<salt>$<hash>'
```

- 空のままなら**ログインなし**です。起動時に警告をログに出し、すべてのページの上部に帯を出します。Tailscale や Cloudflare Access のような外側の認証の内側でだけ使ってください（公開の方法は段 4 で選べるようにします）。
- ログインすると 30 日有効なセッション Cookie（`HttpOnly`、`SameSite=Lax`、`PUBLIC_BASE_URL` が https のとき `Secure`）が発行されます。失敗が 5 回続くと、そのクライアントアドレスからの試行は 429 で待たされます（待ち時間は 2 秒から倍々、最大 15 分。web を再起動すると忘れます）。
- `SESSION_SECRET` は 32 バイト以上で、`change-me` のままだと web は起動を拒否します（`openssl rand -hex 32`）。
- `GET /healthz`（`ok` だけ）、`/static/`、Atom フィードはログイン不要で、そのほかのページと `/api/` はログインが必要です。API は読み取り専用の JSON です（`/api/digests`、`/api/digests/latest`、`/api/digests/{id}`、`/api/conversations/{id}`、`/api/status`）。
- **Atom フィード**：状態ページに URL（`PUBLIC_BASE_URL/feed/<トークン>.atom`）が出ます。トークンは `SESSION_SECRET` から作るので、知っている人は誰でも読めます。公開しないでください。`SESSION_SECRET` を変えると URL も変わります。最新 20 件のダイジェストを要約テキストで載せます。

## 開発に参加する

- 最初に [AGENTS.md](AGENTS.md)（方針・確定済みの判断・ルール）と [LOCAL_DEV.md](LOCAL_DEV.md)（秘密の置き場所）を読む
- clone したら `git config core.hooksPath .githooks`（個人情報・秘密の形をした値をコミット前に検査する）
- 進め方は [specs/agent-workflow.md](specs/agent-workflow.md)、リリースは [RELEASING.md](RELEASING.md)、変更履歴は [CHANGELOG.md](CHANGELOG.md)
- 紹介サイト（`sounding.pathvector.dev`）はこのリポジトリの `site/` に置く予定

## ライセンス

[MIT](LICENSE)
