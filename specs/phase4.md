# 段 4：インストール・公開モード・バックアップ・紹介サイト

> 状態: **案**（2026-11-11）。`docs/DESIGN.md` 4.3・4.4・9 章の段 4 を具体化する。前提は段 1〜3（`specs/phase1.md`〜`specs/phase3.md`）。

## 目的
段 3 までで「自分の日次ダイジェストが届く」ところまでできた。段 4 では、**ほかの人が別のマシンに 1 コマンドで入れて、安全に外から読めて、データを失わない**状態にする。あわせて、名前の由来と入れ方を載せた紹介サイトを置く。

完了の目安（`docs/DESIGN.md` 9 章）：**別のマシンに 1 コマンドで入る**。

## 要件
- [ ] R1: **install.sh**（リポジトリ直下）。`curl -sSL https://sounding.pathvector.dev/install.sh | bash` で次を行う。
  - Docker・Compose v2・git があり、Docker のデーモンに届くかを確かめ、足りなければ直し方を出して止まる。
  - 最新のリリースタグ（`vX.Y.Z`、プレリリースは除く。なければ `main`）を clone する。既存の checkout があれば、手元の変更がない場合だけ更新する。
  - `.env` がなければ `.env.example` から作り、`POSTGRES_PASSWORD`・`SESSION_SECRET`・`IDENTITY_SECRET` を生成値で置き換え、権限を 600 にする。`.env` があれば触らない。
  - `config/profile.yaml` がなければ `config/profile.example.yaml` からコピーする。
  - 端末があれば（`curl | bash` でも `/dev/tty` から）次を聞く。端末がなければ聞かずに既定値で進む。
    - 取得時の連絡先（`FETCH_CONTACT`）。空なら IETF の取得は止まったまま（RSS は動く）で、そのことを表示する。
    - LLM のプロバイダ（`none` / `anthropic` / `openai`、既定 `none`）とキー。キーは画面に表示しない入力で読み、`.env` にだけ書く。
  - Linux では `APP_UID`/`APP_GID` をインストールした利用者に合わせる（1000 未満なら変えない）。
  - 同じ compose プロジェクトがほかの場所で動いている、またはポートが使用中なら、直し方を出して止まる。
  - `docker compose up -d --build --wait` で起動し、Web UI の URL と次の一歩（パスワードの設定、プロファイルの編集、公開モード）を出す。
  - 環境変数 `SOUNDING_DIR`・`SOUNDING_REPO`・`SOUNDING_REF`・`SOUNDING_PORT`・`SOUNDING_NO_START` で上書きできる。`SOUNDING_LLM_PROVIDER` のように、質問の答えも環境変数で渡せる（キーを除く）。
  - スクリプト全体を関数で包み、途中までしか落ちてこなかったときに半端に実行されないようにする。
- [ ] R2: **公開モード 3 種**を README に書き、動く形で用意する。
  - A. **LAN**：`BIND_ADDRESS`（既定 `127.0.0.1`）を足し、`0.0.0.0` にすると LAN から開ける。ログインのパスワードを設定していないと起動しない（LAN に開くのにログインなしは不可）。
  - B. **Tailscale**：tailnet の中だけに HTTPS で出す。
  - C. **Cloudflare Tunnel＋Access**：外から開けるが、Access の内側に置く手順を必須として書く。
  - どのモードでも `PUBLIC_BASE_URL` が https ならクッキーに `Secure` を付ける（段 3 の実装のまま）。
- [ ] R3: **バックアップと復元**。
  - `scripts/backup.sh`：`pg_dump`（カスタム形式）と、行数の `MANIFEST` を `BACKUP_DIR`（既定 `./backups`）の時刻つきのディレクトリに書く。新しい `BACKUP_KEEP`（既定 14）個だけ残す。`.env` は含めない。
  - `scripts/restore.sh --check <snapshot>`：使い捨ての DB に復元して行数を `MANIFEST` と比べる。本番のデータには触らない。
  - `scripts/restore.sh --yes <snapshot>`：今の状態をバックアップしてから、worker と web を止めて本番の DB を置き換え、また起動する。
  - README に cron での日次バックアップと週次の `--check` の例を書く。
- [ ] R4: **web の DB ロールを読み取り専用にする**（段 3 の「決めたこと」で段 4 に回した件）。web は SELECT しかできないロールで接続し、書き込みの SQL を DB が拒む。ロールの作成とパスワードは migration と compose で自動にし、利用者の手順は増やさない。
- [ ] R5: **紹介サイト**（`site/`、HTML/CSS のみ、ビルドなし）。
  - トップページに、何をするものか（議論の現在地を届ける）、**名前の由来**（測深＝位置を確かめる、sound out＝意見・合意を探る）、インストールの 1 行、送られるもの（要約と出典リンクだけ、LLM は自分のキー、既定は何も外に送らない）、GitHub へのリンク。
  - `install.sh` はリポジトリ直下を正とし、デプロイのときに `site/` にコピーする（`site/` 側を直接編集しない）。
  - 日本語と英語の 2 ページ。
- [ ] R6: **README の整理**。セットアップを install.sh 起点に書き直し、手動の手順は「手で入れる」に下げる。更新（`install.sh` の再実行）、公開モード、バックアップ、2 つ目のインスタンスの節を足す。

## スコープ外
- **Cloudflare Pages の設定そのもの**（プロジェクトの作成、`sounding.pathvector.dev` の DNS、監視パスの設定）。利用者が Cloudflare のダッシュボードで行う。この段では手順を `RELEASING.md` に書くだけ。
- `site/` と `install.sh` の**本番公開**。マージは毎回利用者の承認（`AGENTS.md` 4.2）。
- Cloudflare Tunnel・Access を CLI から作るスクリプト（biblioflow の `cloudflare-tunnel.sh` 相当）。手順書だけにする。必要になったら別の PR。
- `scripts/deploy.sh`（本番用 clone の更新とロールバック）。更新は install.sh の再実行で行う。
- バックアップの暗号化・外部ストレージへの送信。`BACKUP_DIR` を別のディスクに置くよう README に書く。
- Web UI でのプロファイル編集、マルチユーザー、Datatracker（MVP の後）。
- Windows のネイティブ対応（WSL2 の Linux として扱う）。

## 受け入れ基準
1. `uv run pytest`（実 DB）がすべて通る。web のロールで INSERT・UPDATE・DELETE が拒まれるテストがある。
2. 空のディレクトリで `SOUNDING_DIR=<tmp> SOUNDING_REF=<このブランチ> bash install.sh` を端末なしで実行すると、秘密が生成された `.env`（`change-me` が残るのは LLM・配信・SMTP の任意の値だけ）と `profile.yaml` ができ、3 つのコンテナが healthy になり、Web UI が 200 を返す。もう一度実行しても `.env` は変わらず、コンテナはそのまま動く。
3. `SOUNDING_NO_START=1` ではコンテナを起動しない。ポートが使用中なら、何も起動せずに直し方を出して止まる。
4. `BIND_ADDRESS=0.0.0.0` でパスワードが空なら web は起動しない。パスワードを設定すれば LAN の別の端末から開ける。
5. Tailscale のモードを README の手順どおりに設定すると、tailnet の端末から https で開け、tailnet の外からは開けない（利用者の tailnet で確認）。
6. `scripts/backup.sh` のあと `scripts/restore.sh --check` が行数の一致を報告する。`--yes` で復元すると、バックアップ後に足したダイジェストが消え、バックアップ時点の件数に戻る。
7. `site/` をローカルで開くと、名前の由来・インストールの 1 行・送られるものの説明が日英で読め、スマートフォンの幅で横にはみ出さない。
8. `scripts/check-private-info.py` が通る（install.sh・README・site に手元の情報がない）。

## 決めたこと
- **install.sh は biblioflow の install.sh と同じ形**にする（環境変数の名前、`set_env` の書き方、`/dev/tty` からの質問、関数で包む）。利用者が両方を使うときに迷わないように。
- **LLM のキーを聞くのはインストール時だけ**。後から変えるときは `.env` を編集して `docker compose up -d`。キーは `read -s` で読み、画面・ログ・シェルの履歴に出さない。
- **既定は何も外に送らない**ままにする。端末がない実行では LLM は `none`、`FETCH_CONTACT` は空のまま（IETF の取得は止まる）。RSS の取得だけは既定で動く（`profile.example.yaml` のフィード）。
- **バックアップは DB だけ**。Sounding にはファイルの置き場（biblioflow の PDF のような）がないので、`rsync` も要らない。`config/profile.yaml` は小さいので snapshot に一緒にコピーする。
- **web の読み取り専用ロール**：migration でロールを作り、パスワードは `POSTGRES_PASSWORD` から導く（HMAC）。新しい秘密を利用者に増やさないため。既存の DB にも migration で入る。
- **LAN でログインなしは不可**：`BIND_ADDRESS` が `127.0.0.1` 以外で `WEB_PASSWORD_HASH` が空なら、web は起動しない。段 3 の決定事項 2 (a)（空ならログインなし）は、127.0.0.1 と Tailscale・Access の内側だけで成り立つ前提だったため。
- **ログインの試行回数の制限**は、Tailscale や Cloudflare の経由ではクライアントのアドレスが見えず全員で 1 つの数になる（段 3 のまま。安全側）。転送ヘッダーは信頼しない。
- **サイトは 2 ページ（日・英）の静的 HTML**。フォントは system-ui、外部のスクリプトなし、アクセス解析なし。

## 決定事項（要判断。承認時に選ぶ）
1. **Tailscale と Cloudflare Tunnel の置き方**
   - (a) compose の profile にサイドカーを足す（`tailscale/tailscale`（BSD-3-Clause）と `cloudflare/cloudflared`（Apache-2.0）のイメージ。biblioflow と同じ）。`docker compose --profile tailscale up -d` で済み、ホストに何も入れなくてよい。**イメージ 2 つの追加になるので、依存の追加として承認がいる**。
   - (b) ホストに入れた `tailscale serve` / `cloudflared` から `127.0.0.1:${SOUNDING_PORT}` に向ける手順を README に書くだけ。追加はないが、ホストでの作業が要り、手順が OS ごとに分かれる。
2. **install.sh でプロファイルの中身（追うリスト）を聞くか**
   - (a) 聞かない。`profile.example.yaml`（例のリストとフィード）をそのままコピーし、最後に「`config/profile.yaml` を編集して `docker compose restart sounding-worker`」と出す。
   - (b) 追う IETF のリスト名をカンマ区切りで聞き、`profile.yaml` に書く。最初から自分のダイジェストになるが、YAML をスクリプトで書き換える処理が増える。
3. **紹介サイトをこの段で作るか**
   - (a) 作る（`specs/roadmap.md` の段 4 のとおり）。公開（マージと Cloudflare Pages の設定）は利用者の承認と作業のあと。
   - (b) 段 4 から外し、install.sh を GitHub の raw URL で配る。サイトは後で別に作る。

## 実装計画（PR 単位。承認後、親が 1 項目ずつ subagent に渡して検証する）
1. **web の読み取り専用ロールと LAN の起動条件**：migration、compose、`BIND_ADDRESS`、テスト（R4、R2 の A）。受け入れ基準 1・4。
2. **バックアップと復元**：`scripts/backup.sh`・`scripts/restore.sh`、README の節（R3）。受け入れ基準 6。
3. **install.sh と README の整理**：R1・R6、決定事項 2 の結果。受け入れ基準 2・3・8。**`install.sh` を変えるのでマージは利用者の承認**。
4. **公開モード B・C**：決定事項 1 の結果に従い compose の profile か手順書（R2）。受け入れ基準 5 は利用者の tailnet で確かめる。
5. **紹介サイト**：`site/`、`RELEASING.md` に Cloudflare Pages の手順（R5）。受け入れ基準 7。**`site/` を変えるのでマージは利用者の承認**。
