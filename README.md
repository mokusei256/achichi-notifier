# achichi-notifier

YouTube チャンネル [@AchichiMela](https://www.youtube.com/@AchichiMela) の更新を Discord に通知する Bot です。

| 検知するもの | 通知の見出し | 補足 |
|---|---|---|
| 動画 | 🎬 新しい動画が公開されました | プレミア公開は予定作成時に通知 |
| ショート | 📱 新しいショートが公開されました | |
| 配信枠の作成 | 📅 配信枠が作成されました | 開始予定時刻を Discord のローカル時刻表示で添付 |
| 枠なしで即配信 | 🔴 配信が始まりました | 枠作成と同時に配信開始していた場合 |
| 投稿 | 📝 新しい投稿がありました | 本文・画像・アンケート選択肢を表示 |
| メン限投稿 | 🔒 メンバー限定の投稿がありました | **内容は一切載せずリンクのみ**（要 Cookie） |

メン限の動画・配信は、タイトルが YouTube 上で公開されているため、タイトル付きで「🔒 メンバー限定」と表示します。

## 仕組み

```
GitHub Actions (主系, 5分ごと) ─┐
                                 ├─▶ YouTube を確認 ─▶ 新着を Discord に投稿
Mac launchd (予備系, 3分ごと) ──┘         ▲
          │                                │
          └── 状態ファイル（既読ID・最終実行時刻）── Discord の非公開チャンネルに JSON 添付で保存
```

- 情報源（API キー不要）
  - RSS フィード: 動画・ショート
  - チャンネルの「動画・ショート・ライブ」タブ: RSS より更新が早く、配信予定の枠やメン限動画も載る
  - 各動画の視聴ページ: 種類（配信予定 / 配信中 / プレミア公開 / アーカイブ）と開始予定時刻の判定
  - 投稿タブ: Cookie を設定するとメン限投稿も取得
- 既読状態は **Discord の非公開チャンネル**に置いた 1 件のメッセージ（JSON 添付）で共有します。このため GitHub Actions と Mac のどちらが動いても二重通知になりません。
- **Mac は予備系**です。GitHub Actions の最終成功から `FALLBACK_AFTER_MINUTES`（既定 15 分）が過ぎているときだけ確認を代行します。GitHub が正常なあいだは、Discord から状態を 1 回読むだけで終了します。
- 初回実行時は、その時点で見えている動画・投稿をすべて既読扱いにし、通知は送りません。
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
- **状態保存用チャンネル**: 非公開にして Bot だけが読み書きできるようにする（`DISCORD_STATE_CHANNEL_ID`）。状態ファイルのメッセージと、Cookie 失効などの警告がここに投稿されます。**状態メッセージは削除しないでください。**

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

### 4. メン限投稿用の Cookie（任意）

ログアウト状態の投稿タブにはメン限投稿が表示されません。検知するには、**メンバー加入済みアカウント**の Cookie を Netscape 形式（`cookies.txt`）で書き出します。

1. Chrome などに Cookie 書き出し用の拡張機能を入れる（例: 「Get cookies.txt LOCALLY」など。信頼できるものを選ぶ）
2. **シークレットウィンドウ**で YouTube にログインし、`youtube.com` の Cookie を書き出す。書き出したら、**そのウィンドウはログアウトせずに閉じる**（ブラウザで使い続けると Cookie が更新されて早く失効しやすいため）
3. 書き出したファイルを `~/Documents/DiscordBot_achichi/cookies.txt` に置く（`.gitignore` 済み）。ローカルで試す場合は `.env` に `YOUTUBE_COOKIES_FILE=./cookies.txt` を追加する。
4. 確認すると `ログイン状態=True` と表示されるはずです。

```bash
uv run python -m achichi_notifier check
```

> ⚠️ **Cookie はアカウントへのログイン権限そのもの**です。ファイルの共有・コミットは絶対にしないでください。失効するとメン限投稿の検知が止まり、状態保存チャンネルに警告が投稿されます（最大 1 日 1 回）。そのときは書き出し直して、ローカルと GitHub Secret の両方を更新してください。

### 5. GitHub Actions（主系）

1. GitHub に **public リポジトリ**を作って push する
   - private リポジトリは Actions の無料枠が月 2,000 分です。5 分間隔だと 1 回 1 分 × 288 回/日 ≒ 8,600 分/月になり、無料枠に収まりません。public なら無料・無制限です。
   - コードに秘密情報は含まれていません（`.env` / `cookies.txt` は `.gitignore` 済み）。ただし **public リポジトリの Actions ログは誰でも閲覧できます**。そのためログには投稿本文を出さないようにしています。

```bash
cd ~/Documents/DiscordBot_achichi
git init && git add . && git commit -m "Initial commit"
gh repo create achichi-notifier --public --source . --push
```

2. **Settings → Secrets and variables → Actions** で次を登録する

   | 種類 | 名前 | 値 |
   |---|---|---|
   | Secret | `DISCORD_BOT_TOKEN` | Bot トークン |
   | Secret | `YOUTUBE_COOKIES_B64` | （任意）`base64 -i cookies.txt \| pbcopy` でコピーした値 |
   | Variable | `DISCORD_CHANNEL_ID` | 通知チャンネル ID |
   | Variable | `DISCORD_STATE_CHANNEL_ID` | 状態保存チャンネル ID |
   | Variable | `MENTION_LIVE` など | （任意）`.env.example` 参照 |

3. **Actions → notify → Run workflow** で 1 回手動実行する。初回は既存分を既読にするだけで、通知は送られません。以降は 5 分ごとに自動で実行されます。

GitHub の定期実行は混雑時に数分〜十数分遅れることがあります。その間は Mac が補完します。

public リポジトリでは、60 日間活動がないと定期実行が自動で停止されます。これを防ぐため、`keepalive` ジョブが週 1 回確認し、最終コミットから 45 日を過ぎていれば空コミットを push します。

### 6. Mac（予備系）

launchd に登録し、3 分ごとに予備系として実行します。

```bash
./deploy/mac/install.sh
```

- アプリは `~/Library/Application Support/achichi-notifier/` にコピーして動かします。`~/Documents` 配下は macOS のプライバシー保護（TCC）により、launchd から読めないためです。
- **コード・`.env`・`cookies.txt` を変更したら、もう一度 `install.sh` を実行**してください。
- スリープ中は止まり、復帰後に再開します。

ログの確認:

```bash
tail -f ~/Library/Application\ Support/achichi-notifier/notifier.log
```

今すぐ 1 回実行する:

```bash
launchctl kickstart gui/$(id -u)/com.achichi.notifier
```

停止・削除:

```bash
./deploy/mac/uninstall.sh
```

GitHub Actions を使わず Mac だけで運用する場合:

```bash
ROLE=primary ./deploy/mac/install.sh
```

---

## コマンド一覧

| コマンド | 内容 |
|---|---|
| `run --role primary` | 1 回確認して新着を投稿（GitHub Actions 用） |
| `run --role fallback` | 主系が `FALLBACK_AFTER_MINUTES` 以上止まっているときだけ実行（Mac 用） |
| `run --dry-run --state-file ./test-state.json` | 投稿せずに表示するだけ。ローカルの状態ファイルで試せる |
| `preview [--send]` | 種類ごとの最新 1 件を通知の見た目で表示／送信 |
| `check` | ゲートウェイログイン・権限・YouTube・Cookie を確認 |

すべて `uv run python -m achichi_notifier <コマンド>` の形で実行します。

## 設定（環境変数）

`.env.example` を参照してください。主なもの:

- `DISCORD_CHANNEL_ID_{VIDEO,SHORT,LIVE,POST}`: 種類ごとに通知先を分ける
- `MENTION_{VIDEO,SHORT,LIVE,POST,MEMBER_POST}`: 種類ごとのメンション（例: `<@&ロールID>`）
- `FALLBACK_AFTER_MINUTES`（既定 15）: Mac が代行を始めるまでの猶予。`0` にすると常に両方が動く
- `MAX_AGE_HOURS`（既定 72）: これより古いものは初めて見つけても通知しない

## 既知の制約

- YouTube の公開ページ（HTML 内の JSON）を解析しているため、YouTube 側の仕様変更で動かなくなる可能性があります。テスト（`uv run pytest`）は構造の想定を固定したものです。
- GitHub Actions のサーバーから視聴ページを開くと、YouTube にボット確認を求められることがあります。その場合はタブ一覧の情報（「配信予定」「ライブ」バッジ）で種類を判定するため通知は届きますが、開始予定時刻が付かないことがあります。
- 投稿の正確な投稿時刻は取得できないため、「5 時間前」などの表示をそのまま添えています。
- メン限投稿の検知は、Cookie 付きページの構造（`sponsorsOnlyBadge`）を前提にしています。メン限投稿が実際に届くまで本番での動作は未検証です。

## 開発

```bash
uv run pytest
```
