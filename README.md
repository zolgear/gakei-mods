# GAKEI Mod のサンプル

[GAKEI](https://github.com/zolgear/gakei)(セルフホストの画像生成 Web ツール)の Mod 機能のサンプル集。Mod は、GAKEI が決まった時点(イベント)で、サーバーに置いた Python のコード(Python の Mod)や外部のシステム(Webhook)を呼ぶ機能。仕様は GAKEI の [docs/mods.md](https://github.com/zolgear/gakei/blob/dev/docs/mods.md)(設計は ADR-0051)。

English: [README.en.md](README.en.md)

## どれを使えば何ができるか

| やりたいこと | サンプル | 種類 | イベント |
|---|---|---|---|
| AI へ送る前に、禁止語を含むプロンプトを止める | [`python-mods/banned-words`](python-mods/banned-words) | Python の Mod | `ai.before_send` |
| AI へ送る前の内容と、取り込み・生成を記録する | [`python-mods/audit-log`](python-mods/audit-log) | Python の Mod | 3つすべて |
| 決まった画像(sha256 の一覧)を拒否する・印を付ける | [`python-mods/hash-blocklist`](python-mods/hash-blocklist) | Python の Mod | `asset.ingested`、`run.finished`、`ai.before_send` |
| Mod の連携を作るときに、届く中身をログで見て、許可・拒否・判定を思いどおりに返す(デバッグ用。本番では使わない) | [`python-mods/echo`](python-mods/echo) | Python の Mod | 3つすべて |
| 社内のシステムで検査・判定する(別のサーバーで受ける) | [`webhook-receivers/python`](webhook-receivers/python) | Webhook | 3つすべて |
| 判定を後から返す(審査に時間がかかる) | [`webhook-receivers/python/submit_verdict.py`](webhook-receivers/python/submit_verdict.py) | Webhook | 判定の受付の API |
| Node.js で受ける | [`webhook-receivers/node`](webhook-receivers/node) | Webhook | `ai.before_send` |
| 生成した画像を手元のフォルダに保存する | GAKEI に同梱の「ローカルに保存」(`local-export`) | Python の Mod | `run.finished`、`asset.ingested` |

手元への保存は、GAKEI に同梱の Mod「ローカルに保存」を使う(管理者設定の「Mod」で有効にするだけ。使い方は [docs/mods.md の11章](https://github.com/zolgear/gakei/blob/dev/docs/mods.md))。このリポジトリには、同じことをする別のサンプルは置かない。書き方の参考には、同梱の Mod のコード(GAKEI の `backend/app/mods/builtin/local_export/`)を読む。

Python の Mod と Webhook の選び方:

- **Python の Mod:** GAKEI のサーバーに置くだけで、別のサーバーが要らない。GAKEI のプロセスの中で、GAKEI と同じ権限で動く(後の「注意」)。
- **Webhook:** 別のサーバー(社内の検査のシステムなど)で受ける。言語を選ばない。GAKEI からの要求には署名が付く。

## 対応する GAKEI の版

- ADR-0051 の Mod が入った GAKEI。リリースでは次の版から。それまでは dev 版のイメージ `ghcr.io/zolgear/gakei:dev`(または `dev` ブランチ)で使う。
- 契約の版は `schema_version` 1、Python の Mod の `api_version` 1。
- マニフェストの `returns_verdicts`(判定を返さない Mod の印)は、それを読む GAKEI で効く。読まない版では無視され、エラーにはならない(その場合、判定を返さない Mod に「判定を待つ」を選ばないこと)。

## Python の Mod を置く

### 置き場所

GAKEI の `MODS_DIR`(既定は `DATA_DIR/mods`)の下に、Mod のフォルダをそのままコピーする。フォルダの中の `gakei-mod.toml`(マニフェスト)と `main.py` があればよい。

```bash
# run.sh / run.bat で動かしているとき(DATA_DIR の既定はリポジトリ直下の data/)
mkdir -p /path/to/gakei/data/mods
cp -r python-mods/banned-words /path/to/gakei/data/mods/
```

Docker では、`DATA_DIR`(`/data`)のボリュームの `mods/` に置く。ホストのフォルダを読み取り専用でマウントしてもよい。

```bash
docker run -d --name gakei -p 127.0.0.1:8000:8000 \
  -v gakei-data:/data \
  -v "$PWD/python-mods/banned-words:/data/mods/banned-words:ro" \
  ghcr.io/zolgear/gakei:dev
```

`audit-log` のように Mod がファイルを書くときは、書き出し先のフォルダもマウントし、コンテナの uid 10001 で書けるようにする。

```bash
  -v /srv/gakei-audit:/audit      # audit-log の「記録のフォルダ」に /audit と書く
```

**置いた・書き換えたら GAKEI を再起動する**(マニフェストは起動のときに読み、一度読み込んだ Mod は読み直さない)。

### 有効にする

1. 管理者として GAKEI を開き、管理者設定の「Mod」(`/settings/mods`)を開く。
2. 「Python の Mod」の一覧に、置いた Mod が出る。読み込みエラーなら、理由が出る。
3. Mod を開き、設定の項目(下の表)を入れて、有効にする。購読するイベント、`purpose`・`source`・プロバイダーの絞り込み、待つ時間、失敗のとき(止める・通す)も、ここで決める。
4. 「テスト送信」でダミーのイベントを送り、応答を確かめる(ダミーの文は `GAKEI test event`、画像は 8×8 の灰色の PNG で `asset_id` が null)。

既定では、Mod は1つも動かない。有効にしたものだけを呼ぶ。

## Python の Mod のサンプル

どれも `gakei_mod`(GAKEI の公開モジュール)と Python の標準ライブラリだけを使う。追加のパッケージは要らない。

### `banned-words` — 禁止語のチェック

外部の AI へ送る直前(`ai.before_send`)に、送る文に禁止語が含まれていれば拒否する。拒否すると、その処理は送らずに止まり、利用者には「禁止語のチェック が送信を止めました: 禁止語「…」を含みます(prompt)」と出る。文と禁止語は NFKC で正規化し、大文字・小文字を区別せずに比べる(全角の「ＣＯＮＦＩＤＥＮＴＩＡＬ」も一致する)。

| 設定 | 既定 | 内容 |
|---|---|---|
| `words` | `社外秘,confidential` | 禁止語(カンマ区切り) |
| `words_file` | 空 | 禁止語のファイル(1行に1語。`#` から行末は無視)。例: [`words.example.txt`](python-mods/banned-words/words.example.txt)。`words` と併用できる |
| `purposes` | 空(すべて) | 検査する目的(`image_run`、`annotation`、`assistant`、`embedding`) |
| `localities` | `cloud,unset` | 検査する送り先の区分(`local`、`cloud`、`unset`)。既定では、組織の外と、区分を決めていない送り先だけを検査する |
| `roles` | 空(すべて) | 検査する文の種類(`prompt`、`negative_prompt`、`system`、`user`、`tags` など) |

- 送り先の区分は、GAKEI の管理者設定で接続・ワークフローごとに決める(docs/mods.md の7章)。GAKEI は自動で判定しない。
- `words_file` が読めないときは例外になり、「失敗のとき」の扱いに従う(既定の「止める」なら、送信を止める)。
- 判定は返さない(`returns_verdicts = false`)。

### `audit-log` — イベントの記録

`ai.before_send`、`asset.ingested`、`run.finished` を、1イベント1行の JSON(JSON Lines)で `<記録のフォルダ>/audit-YYYY-MM-DD.jsonl` に追記する(日付はイベントの時刻の UTC)。行の中身はイベントの封筒(docs/mods.md の3章)に `logged_at` を足したもの。

| 設定 | 既定 | 内容 |
|---|---|---|
| `log_dir` | 空(必須) | 記録のフォルダ。GAKEI のサーバーの上の絶対パス。無ければ作る |
| `include_text` | オン | プロンプトなどの文とパラメーターを記録する。オフなら、文は長さと sha256 だけにし、`params`(プロンプトを含むことがある)を記録しない |
| `include_actor` | オン | 実行した人(id、メール、表示名)を記録する。オフなら id だけ |
| `max_text_chars` | 2000 | 文を記録する長さの上限 |

- `ai.before_send` には常に許可を返す。記録に失敗しても送信は止めない(サーバーのログに残す)。
- `asset.ingested` と `run.finished` は、書けなければ例外にする。GAKEI が配達の失敗として送り直す(30 秒〜240 秒の間隔で、5 回まで)。送り直しで同じイベントが2行になることがあるので、読む側は `event_id` で重複を除く。
- 判定は返さない(`returns_verdicts = false`)。「判定を待つ」は選べない。
- 「テスト送信」でも、ダミーのイベントを実際に記録する(書けるかどうかの確認に使える)。

### `hash-blocklist` — sha256 の一覧での判定

画像の sha256 を、拒否の一覧と印の一覧に照らして判定を返す。

- `asset.ingested`: 取り込んだ画像が拒否の一覧にあれば `deny`、印の一覧にあれば `flag`。
- `run.finished`: 出力の画像ごとに同じく判定し、一致があれば Run にもまとめの判定を付ける。
- `ai.before_send`: 送る画像(Edit の入力など)が拒否の一覧にあれば、送信を止める。

| 設定 | 既定 | 内容 |
|---|---|---|
| `deny_file` | 空 | 拒否する画像の一覧のファイル(1行に「sha256 ラベル」)。例: [`deny-list.example.txt`](python-mods/hash-blocklist/deny-list.example.txt) |
| `flag_file` | 空 | 印を付ける画像の一覧のファイル(形は同じ) |
| `deny_sha256` | 空 | 拒否する画像の sha256(カンマ区切り。ファイルと併用できる) |
| `flag_sha256` | 空 | 印を付ける画像の sha256(カンマ区切り) |
| `deny_label` / `flag_label` | `禁止の一覧に一致` / `要確認の一覧に一致` | 判定の名前(一覧の行にラベルが無いとき) |
| `report_ok` | オフ | 一致しなかった画像にも `ok` の判定を返す |
| `block_send` | オン | 送る画像が拒否の一覧に一致したら、`ai.before_send` で送信を止める |

使い方の勧め:

- **`asset.ingested` を「判定を待つ」で購読する。** 画像をストレージに書く前に呼ばれ、`deny` なら画像を一度も保存しない。生成の出力も、アップロードも、取り込みはすべてここを通る。
- 「判定を待つ」にしない(非同期の)購読では、判定を受けた時点で原本と派生を消す。それまでの間は画像が見え、ダウンロードもできる。
- **`deny` の判定は取り消せない。** 後で `ok` を返しても画像は戻らない。同じ内容(同じ sha256)のほかの画像にも及ぶ。一覧には、確かに拒否するものだけを入れる。迷うものは `flag`(印を付けて表示するだけ)の一覧に入れる。
- 一覧のファイルが読めないときは例外になる。非同期の購読は送り直し、「判定を待つ」購読と `ai.before_send` は「失敗のとき」の扱いに従う(「止める」なら、取り込んだ画像を残さない)。
- テスト送信のダミーの画像は `asset_id` が null なので、Asset への判定は作らない(`run.finished` では Run へのまとめの判定だけが返る)。

### `echo` — デバッグ用のエコー

**デバッグ用。本番では使わない。** Mod の連携(Webhook の受け口や、ほかの Mod)を作る人が、次を確かめるための Mod。

1. いつ、どのイベントが、どんな中身で呼ばれるか(GAKEI のサーバーのログに出す)。
2. 許可・拒否・判定を思いどおりに返したときの、GAKEI の振る舞い(拒否の表示、画像を残さない処理、失敗と送り直し、時間切れ)。

応答は、管理者設定の値か、プロンプトに書いたマーカー(`[[echo:deny]]` など)で決める。マーカーは設定より優先するので、生成の画面から1回ごとに応答を変えられる。

3つのイベントすべてを購読し、判定を返す(`returns_verdicts = true`)。

#### ログ

イベントごとに、GAKEI のサーバーのログ(ロガー `gakei.mods.echo`)に1行の要約を出す。

```text
echo: event=ai.before_send event_id=… purpose=image_run target=openai/gpt-image-1 locality=cloud(default) text=[prompt:23字] images=0[] run_id=… asset_id=- 応答=deny(出どころ: マーカー [[echo:deny]])
echo: event=asset.ingested event_id=… kind=generated source=generated asset_id=… sha256=6b8d2599d7ce size=1024x1024 mime=image/png run_id=… rating=- run_prompt=35字 応答=deny(出どころ: マーカー [[echo:ingested:deny]])
echo: event=run.finished event_id=… status=succeeded target=openai/gpt-image-1 operation=generate run_id=… prompt=35字 inputs=0[] outputs=2[6b8d2599d7ce, 543436859fb3] 応答=none(判定を返さない)(出どころ: 設定 run_finished)
```

- 要約には、文の長さだけを出す(中身は出さない)。画像は sha256 の先頭 12 文字。待つ秒数と、例外を投げるときはその出どころも出す。
- 設定 `log_payload` がオン(既定)なら、続けてイベントの封筒全体(docs/mods.md の3章)を整形した JSON で出す。**プロンプト、パラメーター、実行した人(id、メール、表示名)がそのままログに残る。** ログを人に渡すとき・ログを集めているときは注意する。
- Mod の設定の値(`ctx.settings`)はログに出さない。

#### 設定

| 設定 | 既定 | 内容 |
|---|---|---|
| `before_send` | `allow` | `ai.before_send` への応答(`allow` / `deny`) |
| `ingested` | `none` | `asset.ingested` の判定(`none` / `ok` / `flag` / `deny`。`none` は判定を返さない) |
| `run_finished` | `none` | `run.finished` の判定(同上)。出力の画像ごとと、Run に同じ判定を付ける |
| `delay_seconds` | 0 | 応答する前に待つ秒数(0〜300)。時間切れの確認用 |
| `fail_events` | 空 | 例外を投げるイベント(カンマ区切り。`ai.before_send`、`asset.ingested`、`run.finished`。`send`、`ingested`、`run` とも書ける) |
| `markers` | オン | プロンプトなどの文のマーカーを読む(下の表) |
| `log_payload` | オン | 封筒全体を整形した JSON でもログに出す |

不正な値(知らない選択肢、負の秒数、数でない値など)は、ログに警告を出して既定値として扱う。`delay_seconds` が 300 を超えるときは 300 にする。

#### マーカー

プロンプトなどの文に書くと、設定より優先する。大文字・小文字は区別しない。複数書ける(例: `[[echo:sleep=5]] [[echo:deny]]` は 5 秒待ってから拒否する)。

| マーカー | 内容 |
|---|---|
| `[[echo:allow]]` / `[[echo:deny]]` | `ai.before_send` で許可する・拒否する |
| `[[echo:ok]]` / `[[echo:flag]]` / `[[echo:deny]]` | `asset.ingested` と `run.finished` で、その判定を返す |
| `[[echo:error]]` | 例外を投げる(失敗の確認用) |
| `[[echo:sleep=N]]` | N 秒待ってから応答する(上限 60 秒。小数も書ける) |
| `[[echo:send:…]]`、`[[echo:ingested:…]]`、`[[echo:run:…]]` | イベントを前に付けると、そのイベントだけに効く(例: `[[echo:ingested:deny]]`、`[[echo:send:sleep=3]]`) |

- `[[echo:deny]]` はどのイベントにも効く。生成のプロンプトに書くと、まず `ai.before_send` で送信が止まり、画像はできない。取り込みや Run の判定だけを `deny` にしたいときは `[[echo:ingested:deny]]` や `[[echo:run:deny]]` と書く。
- `ai.before_send` は `allow` / `deny` だけを読み(`ok` と `flag` は無視する)、`asset.ingested` と `run.finished` は `ok` / `flag` / `deny` だけを読む(`allow` は無視する)。
- 応答のマーカーが複数あれば、強いほうを採る(`deny` > `flag` > `ok` > `allow`)。イベントを付けたマーカーがあれば、付けないものより優先する。
- 待つのは、マーカーの `sleep` があればそれ(複数なら長いほう)、無ければ設定の `delay_seconds`。待ってから例外を投げる・応答する(要約のログは待つ前に出す)。
- どこから読むか:
  - `ai.before_send`: 送る文のすべて(`subject.text` の各パート。プロンプト、ネガティブプロンプト、アシスタントへの発言など)。
  - `run.finished`: Run のプロンプト(`subject.prompt`)。
  - `asset.ingested`: 生成の出力なら、`ctx.get_run` でその Run のプロンプトを読む(「判定を待つ」で同期に呼ばれたときも読める)。アップロードなど Run の無い取り込みは、設定だけで決める。
- 拒否の理由と判定の文に、出どころを書く(「echo: マーカー [[echo:deny]] による拒否」「echo: 設定 ingested による判定」)。判定の `label` は `echo`、`reference` はイベントの `event_id`。
- **マーカーは、拒否しない限り、プロンプトの一部としてそのまま AI に送られる**(GAKEI は文を書き換えない)。生成の結果に影響することがある。
- **マーカーを書けば、誰でも拒否や `deny` の判定を出せる。** `deny` の判定を受けた画像は残らない(取り消せない)。確認用の環境だけで有効にする。

#### 確かめられることの例

- **送る前の拒否の表示:** プロンプトに `[[echo:deny]]` を書いて生成する。Run は失敗になり(`hook_denied`)、「エコー(デバッグ用) が送信を止めました: echo: マーカー [[echo:deny]] による拒否」と出る。
- **判定の表示:** `[[echo:flag]]` で生成する。出力の画像と Run に `flag` の判定(`echo`)が付く(既定の非同期の購読では、取り込みの後に届く)。
- **画像を残さない処理:** 管理者設定の「Mod」で echo を「判定を待つ」にして、`[[echo:ingested:deny]]` で生成する(または設定 `ingested` を `deny` にしてアップロードする)。画像は一度も保存されず、「画像なし(拒否)」になる(中身は 410)。非同期の購読のまま `deny` を返すと、取り込みの後で原本と派生が消える。
- **失敗と送り直し:** 設定 `fail_events` を `run` にする(「判定を待つ」はオフ)。`run.finished` の配達が失敗し、GAKEI が間隔を空けて送り直す(30 秒、60 秒、…)。管理者設定の「Mod」の直近の呼び出しに失敗が並ぶ。`send` にすると、「失敗のとき」が「止める」なら「送る前のチェックに失敗しました」で送信が止まり、「通す」なら送られる。
- **時間切れ:** echo の待つ時間を 1 秒にして、`[[echo:send:sleep=3]]` で生成する(または設定 `delay_seconds` を待つ時間より長くする)。呼び出しは時間切れとして記録され、「失敗のとき」の扱いに従う。
- **テスト送信:** 管理者設定の「テスト送信」でも同じく要約と封筒がログに出る。ダミーの画像は `asset_id` が null なので、Asset への判定は作らない(`run.finished` では Run への判定だけを返す)。ダミーの文 `GAKEI test event` にはマーカーが無いので、応答は設定で決まる。

## Webhook の受け口のサンプル

### `webhook-receivers/python`

Python の標準ライブラリ(`http.server`)だけで書いた受け口。Python 3.12 以上。

- 署名(`X-Gakei-Signature`、HMAC-SHA256)と時刻のずれ(前後 300 秒)を確かめる。本文は、読む前に加工せず、受け取ったバイト列そのものに対して確かめる。
- 本文は `application/json`(既定)と `multipart/form-data`(Webhook の「画像を送る」をオンにしたとき)の両方を受ける。multipart で受けた画像は、sha256 がイベントの値と合うかも確かめる。
- `ai.before_send`: 禁止語(送り先の区分で絞る)と、拒否する画像の sha256 で許可・拒否を返す。
- `asset.ingested` / `run.finished`: sha256 の一覧で `deny` / `flag` の判定を返す。

```bash
cd webhook-receivers/python
GAKEI_WEBHOOK_SECRET='<推測できない長い値>' python receiver.py
# http://127.0.0.1:8799/ で待ち受ける
```

| 環境変数 | 既定 | 内容 |
|---|---|---|
| `GAKEI_WEBHOOK_SECRET` | (必須) | Webhook に登録した署名の鍵 |
| `HOST` / `PORT` | `127.0.0.1` / `8799` | 待ち受けるアドレスとポート |
| `BANNED_WORDS` | `社外秘,confidential` | 禁止語(カンマ区切り) |
| `CHECK_LOCALITIES` | `cloud,unset` | 禁止語を検査する送り先の区分(空ならすべて) |
| `DENY_SHA256` / `FLAG_SHA256` | 空 | 拒否する・印を付ける画像の sha256(カンマ区切り) |
| `SAVE_DIR` | 空 | 指定すると、受けたイベントと画像を `<SAVE_DIR>/<event_id>/` に保存する |

GAKEI の管理者設定の「Mod」で Webhook を登録する(名前、URL、署名の鍵、購読するイベント、画像を送るか、など)。登録したら「テスト送信」で確かめる。GAKEI を Docker で動かしているときは、コンテナから受け口へ届く URL を登録する(同じホストなら、Docker の設定に応じて `host.docker.internal` など)。本番では TLS の終端(リバースプロキシなど)の後ろに置く。

#### 判定を後から送る(`submit_verdict.py`)

外部の審査に時間がかかるときは、配達にはすぐ空の応答(`[]`)を返し、結果が出てから判定の受付の API(`POST /api/mods/{Webhook の id}/verdicts`)へ送る。署名は、Webhook の鍵で、送られてくるときと同じ形式で付ける。

```bash
GAKEI_WEBHOOK_SECRET='<署名の鍵>' python submit_verdict.py \
  --gakei-url http://127.0.0.1:8000 --webhook-id <Webhook の id> \
  --asset <Asset の id> --verdict deny --label "侵害の疑い" --reference case-1234
# {"accepted": 1}
```

- Webhook の id は、管理者設定の「Mod」の Webhook の一覧で確かめる。
- 送れる対象は、その Webhook に届いた Asset と Run だけ(対象外が1つでもあれば 403)。署名が合わない・時刻が古いと 401、Webhook が無い・無効だと 404、形が合わないと 422。
- `deny` を送ると、GAKEI はその画像を残さない(取り消せない)。

### `webhook-receivers/node`

Node.js 24 の標準の機能(`node:http`、`node:crypto`、標準の `Request` での multipart の読み取り)だけで書いた最小の受け口。署名を確かめ、`ai.before_send` に禁止語で許可・拒否を返す。`asset.ingested` と `run.finished` には空の判定(`[]`)を返す。

```bash
cd webhook-receivers/node
GAKEI_WEBHOOK_SECRET='<署名の鍵>' node receiver.mjs   # HOST、PORT、BANNED_WORDS で変えられる
```

## 契約の JSON Schema

[`schema/mods-schema.json`](schema/mods-schema.json) は、イベント、応答、判定の受付の API の本文の JSON Schema(2020-12)。`$defs` に `AiBeforeSendEvent`、`AssetIngestedEvent`、`RunFinishedEvent`、`Decision`、`VerdictsResponse`、`VerdictSubmission` などが入る。Webhook の受け口を別の言語で書くときの型の生成や、検証に使う。

GAKEI の backend で書き出したもの。作り直すときは、GAKEI のリポジトリで次を実行する。

```bash
cd backend
uv run python -m app.tools.export_mod_schema - > /path/to/gakei-mods/schema/mods-schema.json
```

`schema_version` の互換性の規則: 項目を足すときは版を上げない(受ける側は、知らない項目を無視する)。項目を消す・意味を変えるときは上げる(受ける側は、知らない版を受けたら、何もしないか拒否する。このサンプルの受け口は 400 を返す)。

## テスト

```bash
# Webhook の受け口のテスト(Python)。Python の Mod のテストは GAKEI_BACKEND が無ければ飛ばす
uv run pytest

# Python の Mod のテストも回す(GAKEI のリポジトリの backend/ を指す)
GAKEI_BACKEND=/path/to/gakei/backend uv run pytest

# Webhook の受け口のテスト(Node.js)
cd webhook-receivers/node && npm test
```

- `gakei_mod` は GAKEI のプロセスの中にしか無い。テストは `GAKEI_BACKEND` の `app/mods/api.py`(`gakei_mod` の本体。標準ライブラリだけで書かれている)を `gakei_mod` の名前で読み込み、本物の `ModContext` に偽の画像の読み出しを渡して Mod の関数を呼ぶ。Mod は GAKEI と同じく、フォルダを1つのパッケージとして import する。
- GAKEI の backend の環境(依存のパッケージ)で回すと、マニフェストを GAKEI の検証(`app.mods.loader.parse_manifest`)でも確かめる。依存が無ければ、その試験だけ飛ばす。
- どのテストも外部に通信しない(受け口のテストは 127.0.0.1 の空きポートで立てる)。
- テストの依存(`pytest`、multipart の本文を GAKEI と同じ形で組み立てるための `httpx`、`ruff`)は `pyproject.toml` の dev グループ。サンプル自体は使わない。

## 注意

- **Python の Mod は、GAKEI のプロセスの中で、GAKEI と同じ権限で動く。** DB、画像、`secrets.json`(API キーを含む)を読み書きでき、外へ送ることもできる。サンドボックスは無い。中身を読んで信頼できるものだけを置く。`MODS_DIR` を、ほかの人が書き込めるフォルダにしない。
- 作りの悪い Mod は、GAKEI を遅くしたり落としたりできる。Mod の関数は GAKEI のイベントループの上で動くので、ファイルの読み書きなどの同期の処理は `await asyncio.to_thread(...)` でスレッドへ逃がす(このサンプルもそうしている)。
- **イベントには、実行した人の情報(id、メール、表示名)と、プロンプトや画像が入る。** Webhook や、外部へ送る Mod を有効にすると、それらが外部のシステムへ渡る。送ってよいかは組織の判断で、送り先の取り扱い(保存期間、アクセスできる人)を確かめてから有効にする。`audit-log` の記録のファイルにも同じ情報が入る。
- `ai.before_send` を「止める」で使うと、検査のシステム(Webhook の受け口など)が止まったときに、GAKEI の生成・自動タイトル・タグ・アシスタント・リモートの埋め込みも止まる。
- `deny` の判定は取り消せない(画像を残さない)。
- 署名の鍵は推測できない長い値にし、漏れたら GAKEI の画面で差し替える。
- これらはサンプルであり、そのままの運用は保証しない。組織の要件に合わせて直して使う。

## ライセンス

[Apache License 2.0](LICENSE)(GAKEI と同じ)。
