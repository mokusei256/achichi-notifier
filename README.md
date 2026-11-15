# achichi-notifier

YouTube チャンネル [@AchichiMela](https://www.youtube.com/@AchichiMela) の更新を Discord に通知する Bot です。

| 検知するもの | 通知の見出し | 補足 |
|---|---|---|
| 動画 | 🎬 新しい動画が公開されました | プレミア公開は予定作成時に通知 |
| ショート | 📱 新しいショートが公開されました | |
| 配信枠の作成 | 📅 配信枠が作成されました | 開始予定時刻を Discord のローカル時刻表示で添付 |
| 枠なしで即配信 | 🔴 配信が始まりました | 枠作成と同時に配信開始していた場合 |
| 投稿 | 📝 新しい投稿がありました | 本文・画像・アンケート選択肢を表示 |
| メン限投稿 | 🔒 メンバー限定の投稿がありました | **内容は一切載せずリンクのみ**。Mac に Cookie を入れた場合だけ、Mac が起動している間に検知（[6.](#6-メン限投稿用の-cookiemac-だけに置く) 参照） |

メン限の動画・配信は、タイトルが YouTube 上で公開されているため、タイトル付きで「🔒 メンバー限定」と表示します。

## 仕組み

```
GitHub Actions（主系・5 分ごと）
  └ 動画・ショート・配信・公開投稿を確認 → Discord に通知

Mac launchd（予備系・3 分ごとに起動）
  ├ GitHub が 15 分以上成功していないとき → 上と同じ確認を代行（Cookie があればメン限投稿も）
  └ Cookie があれば 10 分ごと → メン限投稿だけを確認 → Discord に通知（リンクのみ）

既読状態は Discord の非公開チャンネル（#管理）に JSON 添付で保存し、両者で共有
（どちらが動いても二重通知になりません）
```

- 情報源（API キー不要）
  - RSS フィード: 動画・ショート
  - チャンネルの「動画・ショート・ライブ」タブ: RSS より更新が早く、配信予定の枠やメン限動画も載る
  - 各動画の視聴ページ: 種類（配信予定 / 配信中 / プレミア公開 / アーカイブ）と開始予定時刻の判定
  - 投稿タブ: ログアウト状態で公開投稿を取得。Cookie があればログイン状態でも取得し、メン限投稿を見分ける
- **メン限投稿の見分け方**: 投稿タブを「ログインあり」と「ログインなし」の両方で開き、**ログインしたときだけ見える投稿**、または「メンバー限定」バッジが付いた投稿をメン限とみなします。判断がつかないものは安全側（メン限扱い＝リンクのみ）に倒します。ログインなしのページが取れなかった回は、バッジ付きの投稿だけを通知し、残りは次回に回します。
- **Mac は予備系**です。GitHub Actions の最終成功から `FALLBACK_AFTER_MINUTES`（既定 15 分）が過ぎたときだけ全体の確認を代行します。それ以外の回は、Cookie があればメン限投稿の確認（`MEMBER_CHECK_MINUTES`、既定 10 分ごと）だけを行い、公開投稿は GitHub に任せます。
- 初回実行時は、その時点で見えている動画・投稿をすべて既読扱いにし、通知は送りません（Cookie を入れて最初のメン限確認でも、既存のメン限投稿は既読扱いになります）。
- 取りこぼしや状態の消失があったときに大量通知しないよう、`MAX_AGE_HOURS`（既定 72 時間）より古いものは通知しません。

---

## セットアップ

### 1. Discord Bot を作る

1. <https://discord.com/developers/applications> で **New Application**
2. **Bot** タブで **Reset Token** を押してトークンを控える（他人に見せない）。Privileged Gateway Intents は全部 OFF のままで OK。
3. **OAuth2 → URL Generator** で次を選び、生成された URL からサーバーに招待する
   - Scopes: `bot`
   - Bot Permissions: `View Channels`, `Send Messages`, `Embed Links`, `Attach Files`, `Read Message History`
   - メンション不可設定のロールを `MENTION_*` で通知する場合は `Mention Everyone` も選ぶ
4. Discord の **設定 → 詳細設定 → 開発者モード** を ON にする。チャンネルを右クリック → **ID をコピー** で ID を取得できるようになる。

### 2. チャンネルを用意

- **通知用チャンネル**: 通知が流れる場所（`DISCORD_CHANNEL_ID`）
- **状態保存用チャンネル**（例: `#管理`）: 非公開にして Bot だけが読み書きできるようにする（`DISCORD_STATE_CHANNEL_ID`）。状態ファイルのメッセージと、Cookie の警告（[下記](#管理-に届く-cookie-の警告)）がここに投稿されます。**状態メッセージは削除しないでください。**

### 3. ローカルで動作確認

```bash
cd ~/Documents/DiscordBot_achichi
cp .env.example .env    # 値を埋める
uv sync
```

Bot として一度ゲートウェイにログインし、権限と YouTube へのアクセスを確認します。Discord の仕様上、Bot は一度ゲートウェイに接続しないとメッセージを送れないため、**最初に必ず実行してください**。

```bash
uv run python -m achichi_notifier check
```

最新の動画・ショート・配信・投稿を 1 件ずつ、通知と同じ見た目で出力します（`--send` を付けると実際にチャンネルへ投稿します）。

```bash
uv run python -m achichi_notifier preview
```

```bash
uv run python -m achichi_notifier preview --send
```

### 4. GitHub Actions（主系）

1. GitHub に **public リポジトリ**を作って push する
   - private リポジトリは Actions の無料枠が月 2,000 分です。5 分間隔だと 1 回 1 分 × 288 回/日 ≒ 8,600 分/月になり、無料枠に収まりません。public なら無料・無制限です。
   - コードに秘密情報は含まれていません（`.env` や、リポジトリ直下の `cookies.txt`・`*cookies*.json`・`*state*.json` などは `.gitignore` 済み）。ただし **public リポジトリの Actions ログは誰でも閲覧できます**。そのためログには投稿本文を出さないようにしています。

```bash
cd ~/Documents/DiscordBot_achichi
git init && git add . && git commit -m "Initial commit"
gh repo create achichi-notifier --public --source . --push
```

2. **Settings → Secrets and variables → Actions** で次を登録する

   | 種類 | 名前 | 値 |
   |---|---|---|
   | Secret | `DISCORD_BOT_TOKEN` | Bot トークン |
   | Variable | `DISCORD_CHANNEL_ID` | 通知チャンネル ID |
   | Variable | `DISCORD_STATE_CHANNEL_ID` | 状態保存チャンネル ID |
   | Variable | `MENTION_LIVE` など | （任意）`.env.example` 参照 |

   Cookie（`YOUTUBE_COOKIES_B64`）は **GitHub には登録しないのがおすすめ**です（[理由](#github-でも-cookie-を使う場合非推奨)）。

3. **Actions → notify → Run workflow** で 1 回手動実行する。初回は既存分を既読にするだけで、通知は送られません。以降は 5 分ごとに自動で実行されます。

GitHub の定期実行は混雑時に数分〜十数分遅れることがあります。その間は Mac が補完します。

public リポジトリでは、60 日間活動がないと定期実行が自動で停止されます。これを防ぐため、`keepalive` ジョブが週 1 回確認し、最終コミットから 45 日を過ぎていれば空コミットを push します。

### 5. Mac（予備系＋メン限投稿の確認）

launchd に登録し、3 分ごとに起動します。

```bash
cd ~/Documents/DiscordBot_achichi
./deploy/mac/install.sh
```

起動のたびに、次のどちらかだけを行います（どちらも不要なら状態を 1 回読んで終了）。

- GitHub Actions が `FALLBACK_AFTER_MINUTES`（15 分）以上成功していない → 全体の確認を代行
- Cookie が入っている → `MEMBER_CHECK_MINUTES`（10 分）ごとにメン限投稿を確認（GitHub が正常でも行う。GitHub 側にも動く Cookie がある場合は省略）

補足:

- アプリは `~/Library/Application Support/achichi-notifier/` にコピーして動かします。`~/Documents` 配下は macOS のプライバシー保護（TCC）により、launchd から読めないためです。
- **コード・`.env`・Cookie を変更したら、もう一度 `install.sh` を実行**してください。
- コードを更新するときは、**先に GitHub へ push してから** Mac で `install.sh` を実行してください。GitHub と Mac で版が違うと、古い版が新しい版の記録（保留中のメン限投稿など）を消してしまうことがあります。
- **スリープ中は止まります。** メン限投稿はスリープ中には検知されず、復帰後に見つけたものを通知します（投稿から `MAX_AGE_HOURS`＝72 時間以内のもの）。

ログの確認:

```bash
tail -f ~/Library/Application\ Support/achichi-notifier/notifier.log
```

今すぐ 1 回実行する:

```bash
launchctl kickstart gui/$(id -u)/com.achichi.notifier
```

停止・削除（`PURGE=1` を付けるとアプリ本体と Cookie のコピーも削除）:

```bash
./deploy/mac/uninstall.sh
PURGE=1 ./deploy/mac/uninstall.sh
```

GitHub Actions を使わず Mac だけで運用する場合:

```bash
ROLE=primary ./deploy/mac/install.sh
```

### 6. メン限投稿用の Cookie（Mac だけに置く）

ログアウト状態の投稿タブにはメン限投稿が表示されません。検知するには、**メンバー加入済みアカウント**の Cookie（ログイン情報）を Netscape 形式の `cookies.txt` で書き出し、**Mac にだけ**入れます。

> ⚠️ **Cookie はアカウントへのログイン権限そのもの**です。中身をチャット・Issue・Discord・AI への質問・スクリーンショットに**絶対に貼らないでください**。ファイルの共有・コミットもしないでください。

#### 書き出し方（Chrome）

1. Chrome ウェブストアで **「Get cookies.txt LOCALLY」** を追加する
   - ID が `cclelndahbckbenkjhflpdbgdldlbecc`、ソースが `github.com/kairi003` のものです。
   - 名前が **「Get cookies.txt」だけ**（LOCALLY が付かない）の拡張機能が入っていたら削除してください（過去に問題が報告された別物です）。
2. アドレスバーに `chrome://extensions/?id=cclelndahbckbenkjhflpdbgdldlbecc` と入れて開き、**「シークレット モードでの実行を許可」** を ON にする
3. 開いているシークレットウィンドウを**すべて閉じ**、新しいシークレットウィンドウを開く（⌘⇧N）
4. YouTube に **メンバー加入済みのアカウント**でログインする
5. **同じタブ**で <https://www.youtube.com/robots.txt> を開く（YouTube の画面を開いたままだと書き出し中に Cookie が更新されるため）
6. 拡張機能のアイコンをクリックし、形式が **Netscape** になっていることを確認して **「Export」** を押す（「Export All Cookies」は全サイトのログイン情報が入るので使わない）。`~/Downloads/www.youtube.com_cookies.txt` に保存されます。
7. **ログアウトせずに**ウィンドウを閉じる。そのセッションは二度と開かない（ログアウトしたり使い続けたりすると、書き出した Cookie が無効になります）

Chrome で書き出した Cookie が数時間で失効してしまう場合は、Firefox で試してください。アドオン **「cookies.txt」（作者: Lennon Hill）** を入れ、アドオンの管理で **「プライベートウィンドウでの実行」を許可**し、プライベートウィンドウ（⌘⇧P）で同じ手順を行います（書き出すのは今開いているサイトの Cookie だけ。全サイト分は書き出さない）。

#### Mac に入れる

保存されたファイル名が `www.youtube.com_cookies.txt` と違う場合（Firefox や、同名ファイルがあって `(1)` が付いた場合など）は、コマンドのファイル名を読み替えてください。

```bash
install -m 600 ~/Downloads/www.youtube.com_cookies.txt ~/Documents/DiscordBot_achichi/cookies.txt && rm ~/Downloads/www.youtube.com_cookies.txt
cd ~/Documents/DiscordBot_achichi
./deploy/mac/install.sh
```

- `install.sh` が Cookie を `~/Library/Application Support/achichi-notifier/cookies.txt`（本人だけ読める権限）にコピーします。**コピー後は `~/Documents/DiscordBot_achichi/cookies.txt` を削除して構いません**（iCloud の「デスクトップと書類」同期が ON だと、`~/Documents` は iCloud にアップロードされます）。
  ```bash
  rm ~/Documents/DiscordBot_achichi/cookies.txt
  ```
- `~/Documents` を経由したくない場合は、ダウンロードしたファイルを直接指定できます: `COOKIES=~/Downloads/www.youtube.com_cookies.txt ./deploy/mac/install.sh`（終わったら元ファイルを削除）
- 次回以降、リポジトリに `cookies.txt` がなくても `install.sh` はコピー済みの Cookie を使い続けます。Cookie をやめるときは `rm ~/Library/Application\ Support/achichi-notifier/cookies.txt` してから `install.sh` を再実行します。
- リポジトリ直下に別名の Cookie ファイル（`www.youtube.com_cookies.txt` など）が残っていると、`install.sh` が警告します。
- 書き出したファイルのうち `youtube.com` 以外のドメインの行は無視されます。有効期限が `0` の行はセッション Cookie として扱います。
- 以前の手順で `.env` に `YOUTUBE_COOKIES_FILE=./cookies.txt` を書いていた場合は、その行を削除してください（Mac 用の設定は `install.sh` が自動で書き込みます）。

確認（❌ が出なければ OK）:

```bash
cd ~/Library/Application\ Support/achichi-notifier && ./venv/bin/python -m achichi_notifier check
```

#### 作り直す（更新・漏れたかもしれないとき）

1. 上の「書き出し方」と「Mac に入れる」をもう一度行う（`install.sh` の再実行で、新しい Cookie に上書きされます）
2. [Google アカウント](https://myaccount.google.com/device-activity) **> セキュリティ > お使いのデバイス** で、**古いほうのセッション**をログアウトさせる（ファイルを消すだけでは無効になりません）

Google アカウントのパスワードを変更すると、すべてのセッションがログアウトされます（Bot の Cookie も無効になるので、書き出し直してください）。

#### #管理 に届く Cookie の警告

Cookie に問題があっても、**動画・公開投稿の通知は止まりません**。メン限投稿の確認だけが止まり、状態保存チャンネル（#管理）に「⚠️ メン限投稿のチェックが止まっています（GitHub Actions / Mac（予備系））」という警告が届きます（実行場所ごとに最大 1 日 1 回）。括弧内が直す対象です。

| 警告の内容 | 原因 | 対処 |
|---|---|---|
| Cookie を読み込めません | ファイルがない・壊れている・YouTube のログイン情報が入っていない（GitHub なら Secret の値が不正） | Mac: 書き出し直して `install.sh` を再実行。GitHub: Secret を登録し直すか削除 |
| ログイン状態になりません | Cookie の失効（ログアウト・パスワード変更・時間切れなど） | 書き出し直して `install.sh` を再実行 |
| ログインできていますが、メンバーではありません | 書き出したアカウントのメンバーシップが切れた（チャンネルのヘッダーが「メンバー特典」ではなく「メンバーになる」になっている）、または別アカウントで書き出した | メンバーシップを確認し、加入中のアカウントで書き出し直す |

#### GitHub でも Cookie を使う場合（非推奨）

Mac がスリープしていてもメン限投稿を検知したい場合だけ検討してください。次のリスクがあります。

- public リポジトリの Actions ログは誰でも閲覧できる（Secret の値は伏せ字になりますが、置き場所が増えるぶん漏れる可能性が増える）
- 同じセッションが米国のデータセンターと日本の自宅回線から同時に使われると、無効化されたり不審なログインとして扱われたりすることがある（使うなら Mac 用とは別に書き出した Cookie にする）
- Actions は YouTube が更新した Cookie を保存できないため、失効しやすい

登録は、GitHub 用に別のシークレットウィンドウで書き出したファイルで行います（クリップボードを経由しません。`base64` にせずテキストのまま登録しても動きます）:

```bash
cd ~/Documents/DiscordBot_achichi
base64 -i ~/Downloads/www.youtube.com_cookies.txt | gh secret set YOUTUBE_COOKIES_B64
rm ~/Downloads/www.youtube.com_cookies.txt
```

GitHub 側の Cookie が正常に動いている間、Mac のメン限確認は省略されます。やめるときは:

```bash
gh secret delete YOUTUBE_COOKIES_B64
```

---

## コマンド一覧

| コマンド | 内容 |
|---|---|
| `run --role primary` | 1 回確認して新着を投稿（GitHub Actions 用） |
| `run --role fallback` | 主系が `FALLBACK_AFTER_MINUTES` 以上止まっているときは全体を確認。Cookie があれば `MEMBER_CHECK_MINUTES` ごとにメン限投稿を確認（Mac 用） |
| `run --dry-run --state-file /tmp/achichi-test-state.json` | 投稿せずに表示するだけ。状態はリポジトリの外の一時ファイルに保存し、Discord の状態には触れない |
| `preview [--send]` | 種類ごとの最新 1 件を通知の見た目で表示／送信 |
| `check` | ゲートウェイログイン・権限・YouTube・Cookie を確認 |

すべて `uv run python -m achichi_notifier <コマンド>` の形で実行します。

## 設定（環境変数）

`.env.example` を参照してください。主なもの:

- `DISCORD_CHANNEL_ID_{VIDEO,SHORT,LIVE,POST}`: 種類ごとに通知先を分ける
- `MENTION_{VIDEO,SHORT,LIVE,POST,MEMBER_POST}`: 種類ごとのメンション（例: `<@&ロールID>`）
- `YOUTUBE_COOKIES_FILE`: Cookie ファイルの場所（Mac では `install.sh` が自動で設定）
- `YOUTUBE_COOKIES_B64`: （GitHub 用・非推奨）Cookie の中身。base64 でも cookies.txt のテキストのままでも可。`YOUTUBE_COOKIES_FILE` があればそちらが優先
- `FALLBACK_AFTER_MINUTES`（既定 15）: Mac が全体の確認を代行し始めるまでの猶予。`0` にすると常に両方が動く
- `MEMBER_CHECK_MINUTES`（既定 10）: Mac がメン限投稿を確認する間隔（Cookie があるときだけ。実際は launchd の起動間隔 3 分単位）
- `MAX_AGE_HOURS`（既定 72）: これより古いものは初めて見つけても通知しない

## 既知の制約

- YouTube の公開ページ（HTML 内の JSON）を解析しているため、YouTube 側の仕様変更で動かなくなる可能性があります。テスト（`uv run pytest`）は構造の想定を固定したものです。
- GitHub Actions のサーバーから視聴ページを開くと、YouTube にボット確認を求められることがあります。その場合はタブ一覧の情報（「配信予定」「ライブ」バッジ）で種類を判定するため通知は届きますが、開始予定時刻が付かないことがあります。
- 投稿の正確な投稿時刻は取得できないため、「5 時間前」などの表示をそのまま添えています。
- メン限投稿は Mac が起動している間しか検知できません（GitHub に Cookie を入れない場合）。
- メン限投稿の判定は安全側に倒しているため、ログイン時だけ一時的に見えた公開投稿が「メン限（リンクのみ）」として通知されることがあります。実際のメン限投稿が届くまで、本番での動作は未検証です。

## 開発

```bash
uv run pytest
```
