#!/bin/bash
# Install the notifier on this Mac as a launchd agent (fallback runner by default).
#
# The app is installed into ~/Library/Application Support/achichi-notifier because
# launchd jobs cannot read ~/Documents without a Full Disk Access grant (macOS TCC).
# Re-run this script after changing the code or .env.
#
# role=fallback (default): a full check only while the primary (Windows) has not succeeded for
# FALLBACK_AFTER_MINUTES; with member cookies it also runs the members-only post check
# every MEMBER_CHECK_MINUTES while the primary's cookies are not working.
#
# Member cookies, two ways:
#   FIREFOX_PROFILE=1  recommended: a dedicated "bot" Firefox profile you log into once; the bot
#                      reads its cookies on every run and a daily launchd job opens YouTube in it
#                      headlessly so the browser keeps the session fresh. Stays on for later runs.
#   cookies.txt        an exported file in the repository folder (or COOKIES=<path>).
#
#   ./deploy/mac/install.sh                          # fallback runner (default)
#   FIREFOX_PROFILE=1 ./deploy/mac/install.sh        # use (and create) the bot Firefox profile
#   NO_FIREFOX_PROFILE=1 ./deploy/mac/install.sh     # stop using it (the folder is kept)
#   REFRESH_AT=05:30 ./deploy/mac/install.sh         # time of the daily headless visit (HH:MM)
#   ROLE=primary ./deploy/mac/install.sh             # Mac-only operation
#   INTERVAL=120 ./deploy/mac/install.sh             # check interval in seconds (default 180)
#   COOKIES=~/Downloads/x.txt ./deploy/mac/install.sh  # cookie source (default: <repo>/cookies.txt)
#   APP_DIR=... PLIST=... BROWSER_PLIST=... LOGIN_APP=...  # install locations (for testing)
set -euo pipefail
# Note for edits: bash 3.2 in a UTF-8 locale reads a multibyte character right after a bare
# $NAME as part of the name (then `set -u` aborts); write ${NAME} before Japanese text.

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
APP_DIR="${APP_DIR:-$HOME/Library/Application Support/achichi-notifier}"
LABEL="com.achichi.notifier"
PLIST="${PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
BROWSER_LABEL="com.achichi.notifier.browser"
BROWSER_PLIST="${BROWSER_PLIST:-$HOME/Library/LaunchAgents/$BROWSER_LABEL.plist}"
LOGIN_APP="${LOGIN_APP:-$HOME/Applications/achichi-notifier YouTube login.app}"
INTERVAL="${INTERVAL:-180}"
ROLE="${ROLE:-fallback}"
REFRESH_AT="${REFRESH_AT:-05:30}"
APP_COOKIES="$APP_DIR/cookies.txt"
FF_PROFILE="$APP_DIR/firefox-profile"
FF_MARKER="$FF_PROFILE/achichi-notifier-profile.txt"

command -v uv >/dev/null || { echo "uv が見つかりません: https://docs.astral.sh/uv/" >&2; exit 1; }
[ -f "$REPO/.env" ] || { echo "$REPO/.env がありません（.env.example をコピーして作成）" >&2; exit 1; }

# ---------------------------------------------------------------- bot Firefox profile (opt-in, sticky)
USE_FF=0
if [ "${NO_FIREFOX_PROFILE:-0}" = "1" ]; then
  USE_FF=0
elif [ "${FIREFOX_PROFILE:-0}" = "1" ] || [ -f "$FF_MARKER" ]; then
  USE_FF=1
fi
FF_APP=""
if [ "$USE_FF" = "1" ]; then
  if [ -n "${FIREFOX_APP:-}" ]; then
    FF_APP="$FIREFOX_APP"
  else
    for cand in /Applications/Firefox.app "$HOME/Applications/Firefox.app"; do
      if [ -x "$cand/Contents/MacOS/firefox" ]; then FF_APP="$cand"; break; fi
    done
  fi
  if [ -z "$FF_APP" ] || [ ! -x "$FF_APP/Contents/MacOS/firefox" ]; then
    echo "Firefox が見つかりません。先に入れてください: brew install --cask firefox（何も変更していません）" >&2
    exit 1
  fi
  if ! [[ "$REFRESH_AT" =~ ^([01]?[0-9]|2[0-3]):([0-5][0-9])$ ]]; then
    echo "REFRESH_AT=$REFRESH_AT は HH:MM の形で指定してください（何も変更していません）" >&2
    exit 1
  fi
  REFRESH_HOUR=$((10#${BASH_REMATCH[1]}))
  REFRESH_MINUTE=$((10#${BASH_REMATCH[2]}))
fi

# ---------------------------------------------------------------- cookies.txt (only without the profile)
# Cookie source: COOKIES=<path> if given, else <repo>/cookies.txt (optional).
# The cookie file is never printed; only its location.
COOKIES_SRC=""
if [ -n "${COOKIES:-}" ]; then
  [ -f "$COOKIES" ] || { echo "COOKIES=$COOKIES が見つかりません" >&2; exit 1; }
  COOKIES_SRC="$(cd "$(dirname "$COOKIES")" && pwd)/$(basename "$COOKIES")"
elif [ -f "$REPO/cookies.txt" ]; then
  COOKIES_SRC="$REPO/cookies.txt"
fi
if [ -n "$COOKIES_SRC" ] && [ "$COOKIES_SRC" -ef "$APP_COOKIES" ]; then
  COOKIES_SRC=""   # already the installed copy
fi
UNUSED_COOKIES=""
if [ "$USE_FF" = "1" ] && [ -n "$COOKIES_SRC" ]; then
  UNUSED_COOKIES="$COOKIES_SRC"   # the profile wins: never copy an export next to it
  COOKIES_SRC=""
fi
if [ -n "$COOKIES_SRC" ] && ! grep -q 'youtube\.com' "$COOKIES_SRC"; then
  echo "$COOKIES_SRC に youtube.com の Cookie がありません（空、または Netscape 形式でない）。" >&2
  echo "書き出し直すか、ファイルを削除してから再実行してください。" >&2
  exit 1
fi

echo "→ インストール先: $APP_DIR"
mkdir -p "$APP_DIR"
chmod 700 "$APP_DIR"
uv venv --quiet --allow-existing --python 3.12 "$APP_DIR/venv"
uv pip install --quiet --python "$APP_DIR/venv/bin/python" \
  --reinstall-package achichi-notifier "$REPO"

if [ "$USE_FF" = "1" ]; then
  mkdir -p "$FF_PROFILE" "$APP_DIR/tmp"
  chmod 700 "$FF_PROFILE" "$APP_DIR/tmp"
  printf '%s\n' "achichi-notifier bot profile: used only to keep the member account logged in to YouTube. Do not browse with it." > "$FF_MARKER"
  cat > "$FF_PROFILE/user.js" <<'PREFS'
user_pref("signon.rememberSignons", false);
user_pref("browser.privatebrowsing.autostart", false);
user_pref("privacy.sanitize.sanitizeOnShutdown", false);
user_pref("privacy.purge_trackers.enabled", false);
user_pref("privacy.bounceTrackingProtection.mode", 0);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("startup.homepage_welcome_url", "");
user_pref("startup.homepage_welcome_url.additional", "");
user_pref("startup.homepage_override_url", "");
user_pref("browser.startup.page", 0);
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("toolkit.startup.max_resumed_crashes", -1);
user_pref("browser.tabs.crashReporting.sendReport", false);
user_pref("media.autoplay.default", 5);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("datareporting.healthreport.uploadEnabled", false);
user_pref("toolkit.telemetry.unified", false);
user_pref("app.normandy.enabled", false);
user_pref("app.shield.optoutstudies.enabled", false);
user_pref("extensions.update.enabled", false);
user_pref("places.history.enabled", false);
user_pref("browser.formfill.enable", false);
PREFS
  # A small app (Spotlight: "achichi") that opens the bot profile for logging in.
  as_string() { printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')"; }
  mkdir -p "$(dirname "$LOGIN_APP")"
  rm -rf "$LOGIN_APP"
  osacompile -o "$LOGIN_APP" \
    -e "set ffApp to $(as_string "$FF_APP")" \
    -e "set prof to $(as_string "$FF_PROFILE")" \
    -e 'do shell script "open -n -a " & quoted form of ffApp & " --args --profile " & quoted form of prof & " https://www.youtube.com/"'
  echo "→ Bot 用 Firefox プロファイル: ${FF_PROFILE}（ログイン用アプリ: ${LOGIN_APP}）"
elif [ "${NO_FIREFOX_PROFILE:-0}" = "1" ]; then
  rm -f "$FF_MARKER"
  rm -rf "$LOGIN_APP"
  if [ -d "$FF_PROFILE" ]; then
    echo "→ Bot 用 Firefox プロファイルは使わなくなりました（フォルダは残しています: ${FF_PROFILE}）"
    echo "   消す前に、そのログインを Google アカウント > セキュリティ > お使いのデバイス からログアウトしてください。"
  fi
fi

# .env (without cookie settings pointing elsewhere; the app's own ones are added below)
grep -v -E '^[[:space:]]*(export[[:space:]]+)?(YOUTUBE_COOKIES_FILE|YOUTUBE_FIREFOX_PROFILE|FIREFOX_EXE)[[:space:]]*=' \
  "$REPO/.env" > "$APP_DIR/.env.tmp" || true

if [ "$USE_FF" = "1" ]; then
  echo "YOUTUBE_FIREFOX_PROFILE=$FF_PROFILE" >> "$APP_DIR/.env.tmp"
  echo "FIREFOX_EXE=$FF_APP/Contents/MacOS/firefox" >> "$APP_DIR/.env.tmp"
  for f in "$APP_COOKIES" "$UNUSED_COOKIES"; do
    if [ -n "$f" ] && [ -f "$f" ]; then
      echo "⚠️  $f はもう使われません（Firefox プロファイルが優先）。不要なら削除してください: rm \"$f\"" >&2
    fi
  done
elif [ -n "$COOKIES_SRC" ]; then
  install -m 600 "$COOKIES_SRC" "$APP_COOKIES"
  echo "→ Cookie を $APP_COOKIES にコピーしました（メン限投稿の検知が有効）"
  echo "   コピー元はもう不要なので削除して構いません: rm \"$COOKIES_SRC\""
  case "$COOKIES_SRC" in
    "$HOME/Documents/"* | "$HOME/Desktop/"*)
      echo "   （iCloud の「デスクトップと書類」同期が ON だと、この場所のファイルは iCloud にアップロードされます）" ;;
  esac
elif [ -f "$APP_COOKIES" ]; then
  chmod 600 "$APP_COOKIES"
  echo "→ 既存の Cookie を引き続き使用中: $APP_COOKIES"
  echo "   止めるには rm \"$APP_COOKIES\" して再実行してください"
elif grep -q '^[[:space:]]*YOUTUBE_COOKIES_B64=[[:space:]]*[^[:space:]]' "$APP_DIR/.env.tmp"; then
  echo "⚠️  cookies.txt はありませんが、.env の YOUTUBE_COOKIES_B64 の Cookie を使います（不要なら .env から削除して再実行）" >&2
else
  echo "→ Cookie なし（メン限投稿は検知しません）"
fi
if [ "$USE_FF" != "1" ] && [ -f "$APP_COOKIES" ]; then
  echo "YOUTUBE_COOKIES_FILE=$APP_COOKIES" >> "$APP_DIR/.env.tmp"
fi
install -m 600 "$APP_DIR/.env.tmp" "$APP_DIR/.env"
rm -f "$APP_DIR/.env.tmp"

# Leftover cookie exports in the repository root (git-ignored, but still on disk / iCloud).
shopt -s nullglob
for f in "$REPO"/*cookies*.txt "$REPO"/*cookies*.json; do
  [ "$f" = "$REPO/cookies.txt" ] && continue
  if [ -n "$COOKIES_SRC" ] && [ "$f" -ef "$COOKIES_SRC" ]; then continue; fi
  echo "⚠️  リポジトリ直下に Cookie らしきファイルがあります（使われません）: $f" >&2
  echo "    不要なら削除してください: rm \"$f\"" >&2
done
shopt -u nullglob

mkdir -p "$(dirname "$PLIST")"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP_DIR/venv/bin/python</string>
    <string>-m</string>
    <string>achichi_notifier</string>
    <string>run</string>
    <string>--role</string>
    <string>$ROLE</string>
  </array>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$APP_DIR/notifier.log</string>
  <key>StandardErrorPath</key><string>$APP_DIR/notifier.log</string>
</dict>
</plist>
PLIST
plutil -lint "$PLIST" >/dev/null

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "✅ launchd に登録しました（role=$ROLE, ${INTERVAL}秒ごと）"
echo "   ログ: tail -f \"$APP_DIR/notifier.log\""

launchctl bootout "gui/$(id -u)/$BROWSER_LABEL" 2>/dev/null || true
if [ "$USE_FF" = "1" ]; then
  cat > "$BROWSER_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$BROWSER_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP_DIR/venv/bin/python</string>
    <string>-m</string>
    <string>achichi_notifier</string>
    <string>refresh-browser</string>
  </array>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>$REFRESH_HOUR</integer>
    <key>Minute</key><integer>$REFRESH_MINUTE</integer>
  </dict>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$APP_DIR/browser-refresh.log</string>
  <key>StandardErrorPath</key><string>$APP_DIR/browser-refresh.log</string>
</dict>
</plist>
PLIST
  plutil -lint "$BROWSER_PLIST" >/dev/null
  launchctl bootstrap "gui/$(id -u)" "$BROWSER_PLIST"
  printf '✅ 毎日 %02d:%02d に Bot 用 Firefox で YouTube を開いてログインを保ちます（スリープ中なら起きたとき）\n' \
    "$REFRESH_HOUR" "$REFRESH_MINUTE"
  echo "   手動で 1 回: launchctl kickstart gui/\$(id -u)/$BROWSER_LABEL"
  echo "   結果: tail \"$APP_DIR/browser-refresh.log\""
  if [ ! -f "$FF_PROFILE/cookies.sqlite" ]; then
    echo ""
    echo "次: Bot 用 Firefox で一度だけログインしてください（ログイン用アプリ: ${LOGIN_APP}）。"
    echo "   メンバー加入済みのアカウントで YouTube にログインし、パスワードは保存せず、"
    echo "   ログアウトせずにメニューから Firefox を終了します。"
    if [ -z "${SSH_CONNECTION:-}${SSH_CLIENT:-}" ]; then
      open "$LOGIN_APP" || true
    fi
  fi
else
  rm -f "$BROWSER_PLIST"
fi
