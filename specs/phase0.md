# 段 0：スパイク（IETF ML の取得手段、スレッド化と前処理の削減率）

> 状態: **承認待ち**（2026-11-07）。`docs/DESIGN.md` 1.3・3.3・5 章・9 章の段 0 を具体化する。

## 目的
段 1 の `SourceAdapter`（IETF ML）を作る前に、次の 3 つを実測で決める。

1. **IETF ML の取得手段**（IMAP / rsync の月次 mbox / mailarchive の HTTP）を 1 つに決める。到達性・認証・レート制限・利用条件・差分取得のしかたを一次情報と実機で確かめる。
2. **スレッド化と前処理が効くか**：1 つの WG（sidrops）の直近 1 か月分で、Message-ID / In-Reply-To / References によるスレッド化の精度と、引用行・署名・Note Well などを除いたときの入力量の削減率を測る。
3. **LLM 入力トークンの見積もり**：測定値から、利用者 1 人・追跡リスト数本での 1 日あたりの入力トークンと費用の目安を出す。

あわせて、スパイクのコードを捨てるか段 1 に育てるかの判断材料を残す。

## スコープ
**段 0（この spec）**：`spike/` 以下の使い捨てスクリプト（Python 3.12、標準ライブラリのみ）、測定、結果の文書 `docs/spike-phase0.md`、`docs/DESIGN.md` 1.3 の更新（取得手段の決定）、`.env.example` の IETF 項目の更新。

**スコープ外**：DB、`SourceAdapter` の正式な実装、LLM の呼び出し（トークン数は推定式で出す。実際の要約はしない）、RSS、Datatracker、Docker イメージ、CI のテストジョブ。

## 確認済みの事実（2026-11-07）
一次情報（IETF の公式ページ）と、このマシンからの実機確認。

### IMAP（`imap.ietf.org:993`）
- https://www.ietf.org/how/lists/ に記載：IMAP サーバーは `imap.ietf.org:993`。匿名アクセスは username `anonymous`、パスワードに自分のメールアドレス。認証つきアクセスは Datatracker のログイン。
- RFC 7017（Informational、2013）の要件：リストごとに 1 つのメールボックス、特別なユーザー名での LOGIN、閲覧のみ（削除・追加・移動は不可）。レート制限は「実装者が考慮すべき」とだけあり、具体値の記載はない。
- 実機：TLS で接続でき、ログイン前の CAPABILITY に `IMAP4rev1`・`THREAD=REFERENCES`・`CONDSTORE`・`QRESYNC`・`ESEARCH`・`WITHIN`・`COMPRESS=DEFLATE` が出る（Isode M-Box）。**匿名ログインそのものは未確認**（パスワードにメールアドレスが要るため。スパイクで `FETCH_CONTACT` を使って確かめる）。
- 差分取得：`UIDVALIDITY` ＋ `UID` の上限で新着だけ取れる見込み。`CONDSTORE`/`QRESYNC` があるので変更検知も可能。サーバー側の `THREAD=REFERENCES` で結果を突き合わせられる。

### rsync（`rsync.ietf.org::mailman-archive/<list>/`）
- https://www.ietf.org/about/open-records/ に記載：`rsync.ietf.org::mailman-archive`、リスト名を後ろに付けると個別のリスト。認証の記載はない。同じページに、記録の提供方法は見直しで将来やめることがある旨の注意がある。
- 実機：匿名で一覧が取れる。`sidrops/` に月次の `YYYY-MM.mail`（mbox）があり、`2026-11.mail` は 2026-11-07 に更新されていた（**現行で更新されている**）。2026 年の月次ファイルは 0.5〜2.7 MB。
- 差分取得：当月ファイルだけを rsync すれば差分転送で安い。ただし更新の頻度（実機ではおおむね 1 時間ごとに見える）と、過去月のファイルが後から書き換わること（`2026-06.mail` の更新日が 10 月）は要確認。

### mailarchive.ietf.org（HTTP）
- 一覧 `https://mailarchive.ietf.org/arch/browse/<list>/`（ログイン不要、`?page=` で 1 ページ約 40 件、`?qdr=d|w|m|y` で期間指定）。個々のメッセージは `https://mailarchive.ietf.org/arch/msg/<list>/<id>/` の恒久 URL。**ダイジェストの出典リンクはこの URL を使う**。
- エクスポート（mbox / maildir）は**ログインが必要**で、`robots.txt` が `/arch/export/`・`/arch/search/`・`/arch/advsearch/` を禁止している → 無人の取得には使わない。
- リストごとの Atom フィードは見つからなかった（`/feed/<list>/` は 404）。
- 旧 `https://www.ietf.org/mail-archive/text/<list>/` は 404（現行の手段ではない）。

### 利用条件
- IETF ML への投稿は Note Well の下の IETF への寄稿で、WG のリストのアーカイブは公開（https://www.ietf.org/how/lists/）。Note Well は参加者の規範で、自動取得を禁じる記述はない。レート制限の明記も見当たらない。
- アーカイブのライセンスは IETF Trust の規定を参照とされている。Sounding は本文を再配布しない（要約＋出典リンク、`docs/DESIGN.md` 7 章）ので、スパイクでは Trust の規定を読んで矛盾がないかだけ確かめ、結果の文書に書く。

## 決定（案）

### 取得手段の候補と、スパイクで比べること
| 観点 | IMAP（匿名） | rsync の月次 mbox | mailarchive HTTP |
|---|---|---|---|
| 認証 | `anonymous` ＋ メールアドレス | なし | なし |
| 差分 | UID で新着のみ（最も細かい） | 当月ファイルの差分転送 | 一覧を既知の URL まで遡る（HTML 解析） |
| スレッド化のヘッダー | 完全（RFC 822 そのまま） | mbox だが加工の有無を確認 | HTML から取り出す必要 |
| 出典 URL との対応 | Message-ID から mailarchive URL を引く方法が要る | 同左 | そのまま |
| 依存 | 標準ライブラリ `imaplib` | `rsync` コマンド（イメージに追加が必要） | HTML パーサ |
| 懸念 | 匿名ログインの可否、接続数の制限 | 更新間隔、将来の廃止の可能性 | 負荷が大きく脆い。主手段にはしない |

推奨の仮説：**主手段は IMAP、予備は rsync**。mailarchive の HTTP は「Message-ID → 恒久 URL」の対応づけにだけ使う。スパイクでこの仮説を確かめ、崩れたら rsync を主にする。

### 取得のマナー（どの手段でも）
- User-Agent / IMAP の ID は `sounding-spike/0 (+https://github.com/pathvector-studio/sounding)`。連絡先は `FETCH_CONTACT`（利用者の設定。公開リポジトリには書かない）。
- 同時接続 1、リクエスト間に 1 秒以上、1 回の実行で 1 リスト 1 か月分まで。スパイクの全期間で取得は数回に限る。取得したデータは `spike/data/`（gitignore）に置き、コミットしない。

### スレッド化
- 自前の JWZ アルゴリズム（Message-ID / In-Reply-To / References、件名の `Re:` と `[sidrops]` を正規化した補助）でスレッドを組む。
- 正解の代わりに IMAP の `UID THREAD REFERENCES` の結果と突き合わせ、一致率と食い違いの例を記録する。食い違いは 10 件を目視で分類する（ヘッダー欠落・改題・クロスポストなど）。

### 前処理と削減率
本文（text/plain、なければ HTML をテキスト化）に対して、段階ごとに文字数と推定トークン数を記録する。
1. 生のメール（ヘッダー込み）
2. 本文だけ（MIME 展開、添付・署名の S/MIME 部分を除く）
3. 引用行（`>` 行、「On ... wrote:」の導入行、Outlook 形式の `From: ... Sent: ...` 以下）を除く
4. 署名（`-- ` 以降、末尾の定型ブロック）を除く
5. ML の定型フッター（`_______________` 以下の mailman フッター、Note Well の定型文）を除く
6. 自動送信メール（Datatracker 通知・議題配布など。ヘッダー `Auto-Submitted`・送信元で判定）を除く

トークン数は「文字数 ÷ 4」の推定と、標準ライブラリだけで数えられない点を明記する（正確な数は段 2 でプロバイダのトークン計数を使う）。除去しすぎ（本文の主張まで消える）を見るため、20 通を無作為に選んで前後を目視で比べる。

### 1 日あたりの入力トークンの見積もり
- sidrops 1 か月の「前処理後のトークン数 ÷ 日数」を基準に、`docs/DESIGN.md` 2.1 の例（sidrops・idr・grow ＋ `ietf` を keywords で絞る）に当てはめる。idr・grow は rsync の月次ファイルのサイズ比で概算する（取得はしない）。
- 差分要約（3.3）の前提で、1 日分 = 新着メッセージの前処理後トークン ＋ 状態 JSON（スレッドあたり 1〜2k トークンと仮定）× 動いたスレッド数。
- 費用は main 段・cheap 段のモデル単価を仮置きした表にする（具体的なモデル名と単価は段 2 で決める）。

### スパイクのコードの扱い
- 既定は**使い捨て**：`spike/` に置き、段 1 の PR で削除する。
- ただし、スレッド化（JWZ）と前処理（引用・署名・フッターの除去）は、情報源に依存しない部品として段 1 に移す候補にする。移すかどうかは、結果の文書で「そのまま使える / 書き直す」を部品ごとに判定する。判定の観点：テストが書けるか、IETF 固有の知識（Note Well など）が設定として分離できているか、例外ケースの扱い。

## 実装計画（承認後、親が 1 項目ずつ。spike の PR は 1 本）
1. `spike/fetch_imap.py`：匿名ログイン、`LIST` でリスト名の確認、`sidrops` を `SELECT`、`UIDVALIDITY`・メッセージ数、直近 1 か月を `UID SEARCH SINCE` → `UID FETCH (RFC822)` で `spike/data/imap/` に保存。`UID THREAD REFERENCES` の結果も保存。所要時間・エラー・制限らしき挙動を記録。
2. `spike/fetch_rsync.sh`：`rsync.ietf.org::mailman-archive/sidrops/` から当月と前月の mbox を取得。2 回目の実行で差分転送の量を記録。IMAP と件数・Message-ID を突き合わせる。
3. `spike/thread.py`：JWZ でスレッド化し、IMAP の THREAD と比較。
4. `spike/preprocess.py`：上の 6 段階の削減率を出し、CSV と要約を出力。
5. `spike/estimate.py`：1 日あたりのトークンと費用の表。
6. `docs/spike-phase0.md`：結果、取得手段の決定、部品ごとの「育てる / 捨てる」判定。`docs/DESIGN.md` 1.3 と `.env.example`（`IETF_MAIL_FETCH`・`IETF_IMAP_HOST` の既定値）を更新。`CHANGELOG.md` に 1 行、`STATUS.md` を更新。

## テスト計画
- スパイクなので自動テストは付けない（段 1 で移す部品にはそのとき付ける）。
- 再現性：スクリプトは `spike/data/` の保存済みデータだけで 3〜5 を再実行できる（ネットワークに再アクセスしない）。
- 確認：`python3 scripts/check-private-info.py` が通ること、`spike/data/` がコミットされていないこと、結果の文書にメール本文の長い引用や個人のメールアドレスが入っていないこと（例示は数値と分類だけ）。

## 要判断
1. **主手段の仮説**：(a) IMAP を主・rsync を予備とし、スパイクで確かめる / (b) 最初から rsync を主にする（認証不要で単純。差分は月単位で粗く、`rsync` コマンドへの依存が増える）。推奨 (a)（UID による細かい差分とサーバー側のスレッド化があり、`imaplib` は標準ライブラリ）。
2. **匿名 IMAP のパスワードに使うメールアドレス**：(a) 利用者が `FETCH_CONTACT` に設定したアドレスを使う（スパイクではあなたの連絡用アドレスを `dev.env` に入れてもらう） / (b) スパイクでは IMAP を試さず rsync だけで測る。推奨 (a)（IETF の案内どおりの使い方で、匿名ログインの可否が段 1 の設計を左右する）。
3. **スパイクで取得する範囲**：(a) sidrops の直近 1 か月だけ（idr・grow はファイルサイズから概算） / (b) sidrops・idr・grow の 1 か月ずつを実取得。推奨 (a)（取得を最小にし、比率で十分見積もれる）。
4. **スパイクのコードの置き場**：(a) `spike/` にコミットして PR で残し、段 1 で消す / (b) コミットせず、結果の文書だけを PR にする。推奨 (a)（測定を再現でき、段 1 で部品を移すときに差分で追える）。
