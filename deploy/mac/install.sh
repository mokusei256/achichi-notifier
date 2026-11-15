#!/bin/bash
# Install the notifier on this Mac as a launchd agent (fallback runner by default).
#
# The app is installed into ~/Library/Application Support/achichi-notifier because
# launchd jobs cannot read ~/Documents without a Full Disk Access grant (macOS TCC).
# Re-run this script after changing the code, .env or cookies.txt.
#
# role=fallback (default): a full check only while GitHub Actions has not succeeded for
# FALLBACK_AFTER_MINUTES; with cookies installed it also runs the members-only post check
# every MEMBER_CHECK_MINUTES (cookies are recommended on the Mac only).
#
#   ./deploy/mac/install.sh                          # fallback runner (default)
#   ROLE=primary ./deploy/mac/install.sh             # Mac-only operation (no GitHub Actions)
#   INTERVAL=120 ./deploy/mac/install.sh             # check interval in seconds (default 180)
#   COOKIES=~/Downloads/x.txt ./deploy/mac/install.sh  # cookie source (default: <repo>/cookies.txt)
#   APP_DIR=... PLIST=...                            # install locations (for testing)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
APP_DIR="${APP_DIR:-$HOME/Library/Application Support/achichi-notifier}"
LABEL="com.achichi.notifier"
PLIST="${PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
INTERVAL="${INTERVAL:-180}"
ROLE="${ROLE:-fallback}"
APP_COOKIES="$APP_DIR/cookies.txt"

command -v uv >/dev/null || { echo "uv が見つかりません: https://docs.astral.sh/uv/" >&2; exit 1; }
[ -f "$REPO/.env" ] || { echo "$REPO/.env がありません（.env.example をコピーして作成）" >&2; exit 1; }

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

# .env (without any YOUTUBE_COOKIES_FILE pointing into ~/Documents)
grep -v '^[[:space:]]*YOUTUBE_COOKIES_FILE=' "$REPO/.env" > "$APP_DIR/.env.tmp" || true

if [ -n "$COOKIES_SRC" ]; then
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
if [ -f "$APP_COOKIES" ]; then
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
