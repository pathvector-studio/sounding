# ローカル開発

biblioflow と同じ型：**秘密とローカル固有の情報はチェックアウトの外**に置き、チェックアウトには symlink だけを置く。

## 初回

```bash
git config core.hooksPath .githooks      # コミット前の検査（個人情報・秘密の形をした値）
mkdir -p ~/.config/sounding && chmod 700 ~/.config/sounding
cp .env.example ~/.config/sounding/dev.env && chmod 600 ~/.config/sounding/dev.env
ln -s ~/.config/sounding/dev.env .env
touch ~/.config/sounding/AGENTS.local.md && chmod 600 ~/.config/sounding/AGENTS.local.md
ln -s ~/.config/sounding/AGENTS.local.md AGENTS.local.md
# dev.env の change-me-* を置き換える（openssl rand -hex 32）
```

アプリの実装が入ったら、これを `scripts/dev-setup.sh`（冪等、既存の `.env` は移すだけで上書きしない）にまとめる。

## 設定の分類

`.env.example` に全項目がダミー値で載っている。実値は `~/.config/sounding/` にだけ置く。

| 分類 | 項目 | 置き場 |
|---|---|---|
| A. 公開してよい設定 | `SOUNDING_INSTANCE` `SOUNDING_PORT` `TZ` `APP_UID` `APP_GID` `POSTGRES_USER` `POSTGRES_DB` `LLM_PROVIDER` `LLM_MODEL_*` `LLM_DAILY_BUDGET_USD` `LLM_MAX_OUTPUT_TOKENS` `FETCH_*`（連絡先を除く） `EXTERNAL_TIMEOUT_SECONDS` `CONTENT_RETENTION_DAYS` `IETF_*` `DELIVERY_CHANNELS` | 既定値は `.env.example` とコード。変えるなら `dev.env` |
| B. 秘密 | `POSTGRES_PASSWORD` `SESSION_SECRET`（マシンごとに生成する） | `dev.env` |
| | `LLM_API_KEY` `WEBHOOK_URL` `SMTP_PASSWORD` `WEB_PASSWORD_HASH` | `dev.env`（1Password に置き `op://` 参照で書ける形にする予定） |
| C. 個人・ローカル固有 | `PUBLIC_BASE_URL` `FETCH_CONTACT` `SMTP_*` `DIGEST_TO`、`config/profile.yaml` | `dev.env` / チェックアウトの `config/`（git 管理外） |
| | ほかのサービスとのポートの衝突、開発用ホスト名 | `AGENTS.local.md` |

`DATABASE_URL` は compose が B から組み立てるので、どこにも書かない。

## 秘密情報の扱いのルール

人もコーディングエージェントも、次を守る（`AGENTS.md` 4.3 と同じ）。

- 秘密の実値は、コミット・PR・issue・ドキュメント・ログ・エージェントのメモリや会話の要約に書かない。参照するときは変数名だけを使う。
- `.env`（`dev.env`）の中身は表示しない。調べるときは、キー名と「設定済み・空・`change-me` のまま」だけを出す。
- 値を渡すときは compose か `op run` を使う。コマンドラインの引数や `echo` に値を書かない。
- 個人・ローカル固有の情報は `AGENTS.local.md` に書く。公開する文章では例の値（`192.168.1.10`、`digest.example.com`、`you@example.com`、`<host>.<tailnet>.ts.net`）を使う。
- 新しい秘密を足すときは、`.env.example` にダミー値（`change-me-*` か空）で載せる。
- コミットの前に `scripts/check-private-info.py` が動く。LAN の IP・ホーム以下のパス・個人のメール・トークンの形をした値を拒否する。gitleaks が入っていれば staged の変更も検査する。
