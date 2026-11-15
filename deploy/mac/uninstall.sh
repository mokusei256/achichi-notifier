#!/bin/bash
# Stop and remove the launchd agent. The app directory (venv, .env, cookies, log) is kept.
set -euo pipefail
LABEL="com.achichi.notifier"
PLIST="${PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
APP_DIR="${APP_DIR:-$HOME/Library/Application Support/achichi-notifier}"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
echo "✅ launchd から削除しました"
echo "   アプリ本体・Cookie も消す場合: rm -rf \"$APP_DIR\""
