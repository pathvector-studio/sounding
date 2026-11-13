# リリースの手順

リリースは `vX.Y.Z` のタグ。`install.sh` は最新のタグを入れるので、利用者に届くのはタグであり、`main` は明示的に頼んだ人（`SOUNDING_REF=main`）だけのもの。紹介サイト（`site/`、https://sounding.pathvector.dev）は最新のタグについて書き、サイトの `/install.sh` は最新の GitHub Release に添付した `install.sh` を指す。

役割：リリースを準備する人（人でもエージェントのセッションでもよい）がリリース PR を出す。タグの push と GitHub Release の公開はメンテナーが行う。

<a id="versioning"></a>
## 版の付け方

[Semantic Versioning](https://semver.org/) に従う。版を決めるのは [`VERSION`](VERSION) の 1 か所だけ。`scripts/version.py set X.Y.Z` がすべての写し（`backend/pyproject.toml`、`backend/uv.lock`）を書き換え、写しがずれているときと、push したタグが `v$(cat VERSION)` でないときに CI が失敗する。アプリは `VERSION` を自分で読む（イメージにコピーされる）。

公開インターフェース（ほかの人の環境が依存しているもの）：`.env` のキー、`config/profile.yaml`、HTTP API と Atom フィードの URL、データベース（migration を通して）、バックアップ、compose のサービスとボリューム、`install.sh` のオプション。

| 上げる桁 | いつ |
|---|---|
| **MAJOR** | 公開インターフェースを壊す変更（`.env` やプロファイルのキーを移行期間なしに改名・削除、手作業が要る migration、compose のサービスやボリュームの改名） |
| **MINOR** | 後方互換の追加（新しい情報源アダプタ、新しい配信チャネル、既定値のある任意のキー、自動で走る migration） |
| **PATCH** | 修正、ドキュメント、リファクタリング、出力のスキーマを変えないプロンプトの修正、依存の patch・minor 更新 |

**0.x の間**：壊す変更は MINOR を上げ、CHANGELOG に「Breaking」の行と Upgrade notes を必ず書く。0.x のリリースは「プレビュー」と呼ぶ。

**プレリリース**：`vX.Y.Z-rc.N` は migration かインストールの流れを変えるリリースにだけ使う。インストーラは rc のタグを飛ばすので、試す人は `SOUNDING_REF=vX.Y.Z-rc.N` で選ぶ。rc は GitHub の「Pre-release」として公開する。

## 1. 準備（リリース PR）

最新の `main` から切ったブランチ `release/vX.Y.Z` で：

```bash
scripts/version.py set X.Y.Z
```

`CHANGELOG.md` の `## [Unreleased]` を `## [X.Y.Z] - YYYY-MM-DD` に変え、その上に空の `## [Unreleased]` を足し、Upgrade notes に新しい `.env` のキーと手作業がすべて書いてあるか確かめる。サイトが版に触れているなら（`site/index.html`、`site/en/index.html`）同じ PR で直す。PR を出し、CI が通ったらマージする（`site/` を変えるのでマージはメンテナーの承認）。

## 2. タグの前の確認

マージしたリリースのコミットで：

- [ ] **秘密と個人の情報**：main への push で CI の `private-info` ジョブが通っている（全ファイル、全コミットのメールとメッセージ、履歴全体の gitleaks）。
- [ ] そのコミットで **CI がすべて通っている**：private-info、version、compose、backend、installer、audit-backend（本番依存に high・critical の勧告がない、ライセンス）。
- [ ] **新規インストール**：開発用のスタックとは別の名前で入れる（開発用には絶対に向けない）。`SOUNDING_DIR=<空のディレクトリ> SOUNDING_INSTANCE=sounding-rel SOUNDING_PORT=18080 SOUNDING_REF=<ブランチかタグ> bash install.sh`。3 つのコンテナが healthy、`/healthz` と `/` が 200、`docker compose exec sounding-worker cat /app/VERSION` が新しい版。終わったらそこで `docker compose down -v`。
- [ ] **更新**（2 回目のリリースから）：前のタグを同じように入れ、`scripts/backup.sh` を取り、新しい ref でインストーラをもう一度実行する。migration が走り、ダイジェストが残り、`scripts/restore.sh --check` で古いバックアップを復元できることを確かめる。
- [ ] CHANGELOG の節がリリースノートとして読める。

## 3. タグと公開

メンテナーが行う。**リリース PR が `main` にマージされてから**（マージ前にタグを打つと、古いコミットにタグが付く）。

```bash
git switch main && git pull && test "$(cat VERSION)" = X.Y.Z && git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z
```

`test` は `VERSION` が違えばそこで止める。タグの push で CI がもう一度走り、`version.py check --tag` も確かめる。通ったら：

```bash
awk '/^## \[X.Y.Z\]/{f=1;next} /^## \[/{f=0} f' CHANGELOG.md > /tmp/notes.md
gh release create vX.Y.Z --title "vX.Y.Z" --notes-file /tmp/notes.md --latest install.sh
```

`install.sh` を添付することで、`https://sounding.pathvector.dev/install.sh` がそれを配る（サイトは `releases/latest/download/install.sh` に転送する）。0.x も通常のリリースとして公開し（「Pre-release」にしない）、最新のものに Latest が付くようにする。rc は `--prerelease --latest=false`。

Release を公開したタグは動かさない・消さない。誤りは次の PATCH で直す（Release を作る前で、誰も取っていないタグなら、消して付け直してよい）。

## 4. 公開のあと

- [ ] `curl -sSLI https://sounding.pathvector.dev/install.sh` が新しいリリースの `install.sh` に転送され、`curl -sSL https://sounding.pathvector.dev/install.sh | SOUNDING_NO_START=1 bash` が `vX.Y.Z` を選ぶ。
- [ ] GitHub Release へのリンクを付けて告知する。

## 戻すとき

利用者は `SOUNDING_REF=vX.Y.(Z-1)` でインストーラを実行して戻る。コードを戻しても migration は戻らない。新しい版がデータベースを migration していたら、更新の前に取ったバックアップを復元する（`scripts/restore.sh --yes <snapshot>`）。そのうえで問題を直し、PATCH を出す。

## 紹介サイト（Cloudflare Workers）

紹介サイトは静的な `site/`（日本語が `/`、英語が `/en/`、ビルドなし）で、ほかの pathvector のサイトと同じく Cloudflare Workers の静的アセットとして配る（[`wrangler.toml`](wrangler.toml)）。`main` に push すると Workers Builds がデプロイする。

- `install.sh` はサイトに置かない。`site/_redirects` が `/install.sh` を最新の GitHub Release に添付した `install.sh` へ 302 で転送するので、利用者に届くのはリリース済みのインストーラだけで、`main` の未リリースのものは届かない。最初の Release ができるまで `/install.sh` は 404。
- `site/_headers` がセキュリティヘッダーを付ける。
- 手元での確認：`npx wrangler dev`（http://localhost:8787）。

最初の設定（メンテナーが 1 回だけ行う）：

1. **手元から最初のデプロイ**（コマンドだけでできる。Worker・独自ドメインの DNS レコード・証明書ができる。`pathvector.dev` のゾーンが同じ Cloudflare アカウントにあること）：
   ```bash
   git switch main && git pull
   npx wrangler login     # ブラウザで Cloudflare にログイン（初回だけ。済んでいれば npx wrangler whoami で確認）
   npx wrangler deploy
   ```
2. **Workers Builds の接続**（GitHub との連携。ダッシュボードで行う。これで以後は `main` への push で自動デプロイ）：Workers & Pages → `sounding` の Worker → Settings → Builds → Connect → `pathvector-studio/sounding`。
   - ブランチ `main`、ルートディレクトリ `/`
   - ビルドコマンド：`python3 scripts/check-private-info.py`（検査に落ちればデプロイしない）。デプロイコマンド：`npx wrangler deploy`
   - ビルドの監視パス：`site/*` と `wrangler.toml` だけを含める（アプリの変更でサイトを作り直さない）
3. 確かめる：`curl -sSI https://sounding.pathvector.dev/` が 200、`curl -sSI https://sounding.pathvector.dev/install.sh` が Release の URL へ 302。

`site/`・`wrangler.toml`・`install.sh` を変える PR のマージは本番公開。毎回メンテナーの承認を取る。

## 秘密や個人の情報を公開してしまったら

1. まず秘密を失効・更新する（LLM のキー、webhook の URL、SMTP のパスワード、データベースとセッションの秘密）。履歴を書き換えても漏れは取り消せない。
2. 通常の PR でファイルから消す。
3. メンテナーの承認があるときだけ：履歴を書き換える。または受け入れて、そのコミットを理由とともに `scripts/check-private-info.py` に記録する。

<a id="changelog"></a>
## CHANGELOG

[`CHANGELOG.md`](CHANGELOG.md) は Keep a Changelog の形に、**Upgrade notes** の節を足したもの。

- 変更を入れる PR が、`[Unreleased]` に利用者向けの 1〜3 行を足す。行を足さずにマージしたエージェントのセッションは、セッションの終わりまでに足す。
- `CHANGELOG.md` に触れない PR は CI が失敗させる。ただし `skip-changelog` ラベル（CI・テストだけ、誤字の修正）か Dependabot の PR は除く。
- リリースのときに `[Unreleased]` がその版の節になり、その節がリリースノートになる。
