# achichi-notifier

YouTube チャンネル [@AchichiMela](https://www.youtube.com/@AchichiMela) の更新を Discord に通知する Bot です。

| 検知するもの | 通知の見出し | 補足 |
|---|---|---|
| 動画 | 🎬 新しい動画が公開されました | プレミア公開は予定作成時に通知 |
| ショート | 📱 新しいショートが公開されました | |
| 配信枠の作成 | 📅 配信枠が作成されました | 開始予定時刻を Discord のローカル時刻表示で添付 |
| 枠なしで即配信 | 🔴 配信が始まりました | 枠作成と同時に配信開始していた場合 |
| 投稿 | 📝 新しい投稿がありました | 本文・画像・アンケート選択肢を表示 |
| メン限投稿 | 🔒 メンバー限定の投稿がありました | **内容は一切載せずリンクのみ**。自宅の PC（Windows／Mac）に Cookie を入れた場合だけ、その PC が起動している間に検知（[6.](#6-メン限投稿用の-cookie自宅の-pc-にだけ置く) 参照） |

メン限の動画・配信は、タイトルが YouTube 上で公開されているため、タイトル付きで「🔒 メンバー限定」と表示します。

## 仕組み

```
Windows PC（主系・タスクスケジューラで 5 分ごと）
  └ 動画・ショート・配信・投稿（Cookie があればメン限投稿も）を確認 → Discord に通知

Mac launchd（予備系・3 分ごとに起動）
  ├ Windows が 15 分以上成功していないとき → 上と同じ確認を代行
  └ Mac に Cookie があり、Windows 側の Cookie が動いていないとき → 10 分ごとにメン限投稿だけを確認

GitHub Actions：テスト専用（push のたびに自動テスト。Bot の実行には使いません）

既読状態は Discord の非公開チャンネル（#管理）に JSON 添付で保存し、全員で共有
（どれが動いても二重通知になりません）
```

> **GitHub Actions で Bot を動かさない理由**: GitHub の規約では、GitHub のサーバーで動く Actions は「そのリポジトリのソフトの開発・テスト・公開」に使うものとされ、常駐 Bot の実行はグレーです（違反と判断されるとアカウント停止もありえます）。また 5 分ごとの定期実行は遅れたり発火しなかったりします。そのため Bot は自宅の PC で動かし、GitHub は自動テストだけに使います。

- 情報源（API キー不要）
  - RSS フィード: 動画・ショート
  - チャンネルの「動画・ショート・ライブ」タブ: RSS より更新が早く、配信予定の枠やメン限動画も載る
  - 各動画の視聴ページ: 種類（配信予定 / 配信中 / プレミア公開 / アーカイブ）と開始予定時刻の判定
  - 投稿タブ: ログアウト状態で公開投稿を取得。Cookie があればログイン状態でも取得し、メン限投稿を見分ける
- **メン限投稿の見分け方**: 投稿タブを「ログインあり」と「ログインなし」の両方で開き、**ログインしたときだけ見える投稿**、または「メンバー限定」バッジが付いた投稿をメン限とみなします。判断がつかないものは安全側（メン限扱い＝リンクのみ）に倒します。ログインなしのページが取れなかった回は、バッジ付きの投稿だけを通知し、残りは次回に回します。
- **Mac は予備系**です。主系（Windows）の最終成功から `FALLBACK_AFTER_MINUTES`（既定 15 分）が過ぎたときだけ全体の確認を代行します。それ以外の回は、Mac に Cookie があり主系の Cookie が動いていなければメン限投稿の確認（`MEMBER_CHECK_MINUTES`、既定 10 分ごと）だけを行い、公開投稿は主系に任せます。
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

### 4. Windows PC（主系）

電源を入れっぱなしにできる Windows PC を主系にします（あとでミニ PC を買ったら、同じ手順で移せます）。以下は Windows の **PowerShell** で実行します（スタートメニューで「PowerShell」と検索）。

1. 必要なツールを入れる（入れたら PowerShell を**いったん閉じて開き直す**）

   ```powershell
   winget install --id=Git.Git -e
   winget install --id=astral-sh.uv -e
   ```

   Windows 10 では winget が「ソースを更新できませんでした」で失敗することがあります。その場合 uv は、[公式の配布ページ](https://github.com/astral-sh/uv/releases)の `uv-x86_64-pc-windows-msvc.zip` を展開して `uv.exe` を `%USERPROFILE%\.local\bin` に置き、そのフォルダをユーザーの PATH に追加すれば使えます（チェックサム `.sha256` で照合してください）。

2. コードを取ってくる。**「ドキュメント」には置かない**でください（OneDrive と同期されていることが多く、トークン入りの `.env` や Cookie がクラウドに上がります）

   ```powershell
   cd $HOME
   git clone https://github.com/mokusei256/achichi-notifier
   cd achichi-notifier
   ```

3. 設定ファイル `.env` を作る。中身は Mac の `~/Documents/DiscordBot_achichi/.env` と同じ値（Bot トークンと 2 つのチャンネル ID）にします。**トークンはチャットやクラウドに貼らず**、USB メモリなどで移すか、メモ帳に直接入力してください。

   ```powershell
   Copy-Item .env.example .env
   notepad .env
   ```

4. （任意）メン限投稿も Windows で検知する場合は、Cookie を `cookies.txt` としてこのフォルダに置く（[6.](#6-メン限投稿用の-cookie自宅の-pc-にだけ置く) 参照）

5. インストールする（タスクスケジューラに 5 分ごとの実行が登録され、すぐに 1 回動きます）

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\install.ps1
   ```

6. 確認する（❌ が出なければ OK。コマンドはインストールの最後に表示されます）

   ```powershell
   & "$env:LOCALAPPDATA\achichi-notifier\venv\Scripts\python.exe" -m achichi_notifier --env-file "$env:LOCALAPPDATA\achichi-notifier\.env" check
   ```

7. **スリープしない設定にする**: 設定 → システム → 電源 → 「電源接続時に、次の時間が経過した後にデバイスをスリープ状態にする」を「なし」。スリープ中は動きません。

補足:

- アプリは `%LOCALAPPDATA%\achichi-notifier` にコピーして動かします（`.env` と Cookie は本人だけが読める権限）。画面は出ず、ログは `notifier.log` に書かれます（1MB ごとに切り替え）。
- タスクは**サインインしている間**に動きます（画面がロックされていても動きます）。手動で再起動したらサインインしてください。
- Windows Update の自動再起動のあとも動き続けるように、**設定 → アカウント → サインイン オプション →「更新後にサインイン情報を使用して、デバイスのセットアップを自動的に完了する」を オン**にしておいてください（オフだと、誰かがサインインするまで止まります。そのあいだは Mac が代わりに確認し、#管理 に「主系の PC が止まっています」と届きます）。
- コードを更新するとき: `cd $HOME\achichi-notifier` → `git pull` → `install.ps1` をもう一度実行。`.env` や Cookie を変えたときも `install.ps1` を再実行します（実行中の確認が終わるのを待ってから入れ替えます）。
- 主系を Windows に移したら、Mac 側はそのまま予備系として動きます（設定変更は不要）。

ログを見る／今すぐ 1 回動かす／止める:

```powershell
Get-Content -Wait -Tail 20 "$env:LOCALAPPDATA\achichi-notifier\notifier.log"
Start-ScheduledTask -TaskName achichi-notifier
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\uninstall.ps1          # タスクだけ削除
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\uninstall.ps1 -Purge   # アプリと Cookie のコピーも削除
```

#### ミニ PC に移すとき

新しい PC で 1〜7 をやり直し、動いたのを確認してから古い Windows で次を実行します（タスクとアプリ・Cookie のコピーを削除し、クローンに残った `.env` も消します）。

```powershell
cd $HOME\achichi-notifier
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\uninstall.ps1 -Purge
Remove-Item .\.env
```
ミニ PC が Linux の場合は、同じ内容を systemd タイマーで動かします（必要になったら手順を追加します）。

#### GitHub について

GitHub は自動テスト（`.github/workflows/tests.yml`）だけに使います。以前 Bot 用に登録した Secret・Variable（`DISCORD_BOT_TOKEN` など）は不要なので、削除して構いません。

### 5. Mac（予備系）

launchd に登録し、3 分ごとに起動します。

```bash
cd ~/Documents/DiscordBot_achichi
./deploy/mac/install.sh
```

起動のたびに、次のどちらかだけを行います（どちらも不要なら状態を 1 回読んで終了）。

- 主系（Windows）が `FALLBACK_AFTER_MINUTES`（15 分）以上成功していない → 全体の確認を代行
- Mac に Cookie が入っている → `MEMBER_CHECK_MINUTES`（10 分）ごとにメン限投稿を確認（主系が正常でも行う。主系にも動く Cookie がある場合は省略）

補足:

- アプリは `~/Library/Application Support/achichi-notifier/` にコピーして動かします。`~/Documents` 配下は macOS のプライバシー保護（TCC）により、launchd から読めないためです。
- **コード・`.env`・Cookie を変更したら、もう一度 `install.sh` を実行**してください。
- コードを更新するときは、**Windows と Mac の両方を同じ版に**してください（Windows: `git pull` → `install.ps1`、Mac: `install.sh`）。版が違うと、古い版が新しい版の記録（保留中のメン限投稿など）を消してしまうことがあります。
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

Windows を使わず Mac だけで運用する場合（Mac を主系にする）:

```bash
ROLE=primary ./deploy/mac/install.sh
```

### 6. メン限投稿用の Cookie（自宅の PC にだけ置く）

ログアウト状態の投稿タブにはメン限投稿が表示されません。検知するには、**メンバー加入済みアカウント**の Cookie（ログイン情報）を Netscape 形式の `cookies.txt` で書き出し、**自宅の PC にだけ**入れます。

- **おすすめは主系の Windows に入れる**ことです。常時動いているので、24 時間メン限投稿を検知できます。
- Mac にも入れておくと、Windows が止まったときや Windows の Cookie が失効したときに Mac が代わりに確認します。Mac の Cookie が使えるかは、Mac が 1 日 1 回だけ自分で点検します。
- **Windows 用と Mac 用は、それぞれ別のシークレットウィンドウで書き出してください**（別のセッションになり、片方が失効してももう片方が残ります。同じファイルを両方に入れると、失効も同時に起きます）。
- GitHub などのクラウドには置かないでください。

> ⚠️ **Cookie はアカウントへのログイン権限そのもの**です。中身をチャット・Issue・Discord・AI への質問・スクリーンショットに**絶対に貼らないでください**。ファイルの共有・コミットもしないでください。

#### 書き出し方（Chrome）

1. Chrome ウェブストアで **「Get cookies.txt LOCALLY」** を追加する
   - ID が `cclelndahbckbenkjhflpdbgdldlbecc`、ソースが `github.com/kairi003` のものです。
   - 名前が **「Get cookies.txt」だけ**（LOCALLY が付かない）の拡張機能が入っていたら削除してください（過去に問題が報告された別物です）。
2. アドレスバーに `chrome://extensions/?id=cclelndahbckbenkjhflpdbgdldlbecc` と入れて開き、**「シークレット モードでの実行を許可」** を ON にする
3. 開いているシークレットウィンドウを**すべて閉じ**、新しいシークレットウィンドウを開く（Mac: ⌘⇧N、Windows: Ctrl+Shift+N）
4. YouTube に **メンバー加入済みのアカウント**でログインする
5. **同じタブ**で <https://www.youtube.com/robots.txt> を開く（YouTube の画面を開いたままだと書き出し中に Cookie が更新されるため）
6. 拡張機能のアイコンをクリックし、形式が **Netscape** になっていることを確認して **「Export」** を押す（「Export All Cookies」は全サイトのログイン情報が入るので使わない）。ダウンロードフォルダに `www.youtube.com_cookies.txt` として保存されます。
7. **ログアウトせずに**ウィンドウを閉じる。そのセッションは二度と開かない（ログアウトしたり使い続けたりすると、書き出した Cookie が無効になります）

Chrome で書き出した Cookie が数時間で失効してしまう場合は、Firefox で試してください。アドオン **「cookies.txt」（作者: Lennon Hill）** を入れ、アドオンの管理で **「プライベートウィンドウでの実行」を許可**し、プライベートウィンドウ（⌘⇧P）で同じ手順を行います（書き出すのは今開いているサイトの Cookie だけ。全サイト分は書き出さない）。

#### Windows に入れる

書き出したファイルを、[4.](#4-windows-pc主系) で `git clone` したフォルダに `cookies.txt` という名前で置き、`install.ps1` を再実行します（PowerShell）。

```powershell
cd $HOME\achichi-notifier
Move-Item $HOME\Downloads\www.youtube.com_cookies.txt .\cookies.txt
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\install.ps1
Remove-Item .\cookies.txt    # アプリ側にコピーされたので元は削除
```

書き出しは Windows の Chrome で行うのがおすすめです（上の「書き出し方」と同じ手順。シークレットウィンドウは Ctrl+Shift+N）。ファイルを PC 間で移す場合は、チャット・メール・クラウドストレージは経由しないでください。

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

1. 上の「書き出し方」と「Windows に入れる」「Mac に入れる」をもう一度行う（`install.ps1`／`install.sh` の再実行で、新しい Cookie に上書きされます）
2. [Google アカウント](https://myaccount.google.com/device-activity) **> セキュリティ > お使いのデバイス** で、**古いほうのセッション**をログアウトさせる（ファイルを消すだけでは無効になりません）

Google アカウントのパスワードを変更すると、すべてのセッションがログアウトされます（Bot の Cookie も無効になるので、書き出し直してください）。

#### #管理 に届く Cookie の警告

Cookie に問題があっても、**動画・公開投稿の通知は止まりません**。メン限投稿の確認だけが止まり、状態保存チャンネル（#管理）に「⚠️ メン限投稿のチェックが止まっています（主系の PC / Mac（予備系））」という警告が届きます（実行場所ごとに最大 1 日 1 回）。括弧内が直す対象です。

| 警告の内容 | 原因 | 対処 |
|---|---|---|
| Cookie を読み込めません | ファイルがない・壊れている・YouTube のログイン情報が入っていない | 書き出し直して、その PC で `install.ps1`／`install.sh` を再実行 |
| ログイン状態になりません | Cookie の失効（ログアウト・パスワード変更・時間切れなど） | 書き出し直して `install.ps1`／`install.sh` を再実行 |
| ログインできていますが、メンバーではありません | 書き出したアカウントのメンバーシップが切れた（チャンネルのヘッダーが「メンバー特典」ではなく「メンバーになる」になっている）、または別アカウントで書き出した | メンバーシップを確認し、加入中のアカウントで書き出し直す |

---

## 別のサーバーにも通知する（送り先の追加）

`.env` のチャンネルは「main」という送り先です。ほかのサーバーへは、**ターミナルからコマンドで送り先を追加**します。送り先の一覧は #管理 の共有状態に保存されるので、Mac で追加すれば Windows の主系も次の確認（5 分以内）から送ります。ファイルの編集や `install` のやり直しは不要です。

1. そのサーバーに Bot を招待する（「1. Discord Bot を作る」と同じ招待 URL。招待には、そのサーバーの「サーバー管理」権限が必要です）。非公開チャンネルなら、Bot に「見る・送信・埋め込みリンク・メッセージ履歴を読む」を付けます。
2. 通知を流したいチャンネルの ID をコピーする（開発者モードで右クリック → ID をコピー）
3. Mac のターミナルで追加する（Bot がそのチャンネルを見られることを確認してから登録します）

```bash
cd ~/Documents/DiscordBot_achichi
uv run python -m achichi_notifier dest add bigserver --channel 123456789012345678
```

よく使うオプション:

| オプション | 意味 |
|---|---|
| `--live-channel ID` など | 種類ごとに別のチャンネルへ（`--video-channel` `--short-channel` `--live-channel` `--post-channel`） |
| `--kinds video,live` | 送る種類を絞る（`video` `short` `live` `post` `member_post`。省略時はすべて） |
| `--mention-live '<@&ロールID>'` | 種類ごとのメンション（`--mention-video` `--mention-short` `--mention-live` `--mention-post` `--mention-member-post`） |

同じ名前でもう一度 `add` すると上書きされます。そのほかの操作:

```bash
uv run python -m achichi_notifier dest list                       # 一覧
uv run python -m achichi_notifier dest check bigserver --send-test # 権限の確認とテスト送信
uv run python -m achichi_notifier dest disable bigserver          # 一時停止（enable で再開）
uv run python -m achichi_notifier dest remove bigserver           # 削除
uv run python -m achichi_notifier preview --send --dest bigserver # 見た目の確認（【プレビュー】付き・メンションなし）
```

- 1 つの送り先に送れなくても、ほかの送り先には届きます（#管理 に「送り先「〇〇」に通知を送れませんでした」と届きます）。すべての送り先に送れなかった通知は、次の確認でもう一度送ります。
- 大きなサーバーでメン限投稿のリンクを流したくない場合は、`--kinds` から `member_post` を外してください。

## コマンド一覧

| コマンド | 内容 |
|---|---|
| `run --role primary` | 1 回確認して新着を投稿（主系の PC 用） |
| `run --role fallback` | 主系が `FALLBACK_AFTER_MINUTES` 以上止まっているときは全体を確認。Cookie があれば `MEMBER_CHECK_MINUTES` ごとにメン限投稿を確認（Mac 用） |
| `run --dry-run --state-file /tmp/achichi-test-state.json` | 投稿せずに表示するだけ。状態はリポジトリの外の一時ファイルに保存し、Discord の状態には触れない |
| `preview [--send]` | 種類ごとの最新 1 件を通知の見た目で表示／送信 |
| `check` | ゲートウェイログイン・権限（追加した送り先も含む）・YouTube・Cookie を確認 |
| `dest list/add/remove/enable/disable/check` | 通知の送り先を管理（[上記](#別のサーバーにも通知する送り先の追加)） |

すべて `uv run python -m achichi_notifier <コマンド>` の形で実行します。

## 設定（環境変数）

`.env.example` を参照してください。主なもの:

- `DISCORD_CHANNEL_ID_{VIDEO,SHORT,LIVE,POST}`: 種類ごとに通知先を分ける
- `MENTION_{VIDEO,SHORT,LIVE,POST,MEMBER_POST}`: 種類ごとのメンション（例: `<@&ロールID>`）
- `YOUTUBE_COOKIES_FILE`: Cookie ファイルの場所（`install.ps1`／`install.sh` が自動で設定）
- `YOUTUBE_COOKIES_B64`: （非推奨）Cookie の中身を環境変数で渡す場合。base64 でも cookies.txt のテキストのままでも可。`YOUTUBE_COOKIES_FILE` があればそちらが優先
- `FALLBACK_AFTER_MINUTES`（既定 15）: 予備系（Mac）が全体の確認を代行し始めるまでの猶予。`0` にすると常に両方が動く
- `MEMBER_CHECK_MINUTES`（既定 10）: Mac がメン限投稿を確認する間隔（Cookie があるときだけ。実際は launchd の起動間隔 3 分単位）
- `MAX_AGE_HOURS`（既定 72）: これより古いものは初めて見つけても通知しない

## 既知の制約

- YouTube の公開ページ（HTML 内の JSON）を解析しているため、YouTube 側の仕様変更で動かなくなる可能性があります。テスト（`uv run pytest`）は構造の想定を固定したものです。
- 視聴ページで YouTube にボット確認を求められた場合は、タブ一覧の情報（「配信予定」「ライブ」バッジ）で種類を判定するため通知は届きますが、開始予定時刻が付かないことがあります（自宅回線ではまれです）。
- 投稿の正確な投稿時刻は取得できないため、「5 時間前」などの表示をそのまま添えています。
- メン限投稿は、Cookie を入れた PC が起動している間しか検知できません。
- メン限投稿の判定は安全側に倒しているため、ログイン時だけ一時的に見えた公開投稿が「メン限（リンクのみ）」として通知されることがあります。実際のメン限投稿が届くまで、本番での動作は未検証です。

## 開発

```bash
uv run pytest
```

GitHub に push すると、同じテストが GitHub Actions でも自動で走ります（Bot は動きません）。
