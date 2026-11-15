#!/bin/bash
# Install the notifier on this Mac as a launchd agent (fallback runner by default).
#
# The app is installed into ~/Library/Application Support/achichi-notifier because
# launchd jobs cannot read ~/Documents without a Full Disk Access grant (macOS TCC).
# Re-run this script after changing the code, .env or cookies.txt.
#
#   ./deploy/mac/install.sh                 # fallback: runs only while GitHub Actions is down
#   ROLE=primary ./deploy/mac/install.sh    # Mac-only operation (no GitHub Actions)
#   INTERVAL=120 ./deploy/mac/install.sh    # check interval in seconds (default 180)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
APP_DIR="${APP_DIR:-$HOME/Library/Application Support/achichi-notifier}"
LABEL="com.achichi.notifier"
PLIST="${PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
INTERVAL="${INTERVAL:-180}"
ROLE="${ROLE:-fallback}"

command -v uv >/dev/null || { echo "uv が見つかりません: https://docs.astral.sh/uv/" >&2; exit 1; }
[ -f "$REPO/.env" ] || { echo "$REPO/.env がありません（.env.example をコピーして作成）" >&2; exit 1; }

echo "→ インストール先: $APP_DIR"
mkdir -p "$APP_DIR"
chmod 700 "$APP_DIR"
uv venv --quiet --allow-existing --python 3.12 "$APP_DIR/venv"
uv pip install --quiet --python "$APP_DIR/venv/bin/python" \
  --reinstall-package achichi-notifier "$REPO"

# .env (without any YOUTUBE_COOKIES_FILE pointing into ~/Documents)
grep -v '^[[:space:]]*YOUTUBE_COOKIES_FILE=' "$REPO/.env" > "$APP_DIR/.env.tmp" || true
if [ -f "$REPO/cookies.txt" ]; then
  install -m 600 "$REPO/cookies.txt" "$APP_DIR/cookies.txt"
  echo "YOUTUBE_COOKIES_FILE=$APP_DIR/cookies.txt" >> "$APP_DIR/.env.tmp"
  echo "→ cookies.txt をコピーしました（メン限投稿の検知が有効）"
elif [ -f "$APP_DIR/cookies.txt" ]; then
  echo "YOUTUBE_COOKIES_FILE=$APP_DIR/cookies.txt" >> "$APP_DIR/.env.tmp"
fi
install -m 600 "$APP_DIR/.env.tmp" "$APP_DIR/.env"
rm -f "$APP_DIR/.env.tmp"

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
