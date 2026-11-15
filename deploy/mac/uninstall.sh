#!/bin/bash
# Stop and remove the launchd agent. The app directory (venv, .env, cookies, log) is kept
# unless PURGE=1 is given.
#
#   ./deploy/mac/uninstall.sh            # remove the launchd job only
#   PURGE=1 ./deploy/mac/uninstall.sh    # also delete the app directory (incl. cookies.txt)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
LABEL="com.achichi.notifier"
PLIST="${PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
APP_DIR="${APP_DIR:-$HOME/Library/Application Support/achichi-notifier}"

if [ "${PURGE:-0}" = "1" ]; then
  case "${APP_DIR%/}" in
    "" | "/" | "$HOME" | "$HOME/Library" | "$HOME/Library/Application Support" | "$REPO")
      echo "APP_DIR=$APP_DIR は削除しません（安全のため）" >&2; exit 1 ;;
  esac
fi

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
echo "✅ launchd から削除しました"

if [ "${PURGE:-0}" = "1" ]; then
  rm -rf "$APP_DIR"
  echo "✅ アプリ本体を削除しました（Cookie のコピーを含む）: $APP_DIR"
elif [ -d "$APP_DIR" ]; then
  echo "   アプリ本体（venv・.env・Cookie・ログ）は残しています: $APP_DIR"
  if [ -f "$APP_DIR/cookies.txt" ]; then
    echo "   Cookie のコピーだけ消す:   rm \"$APP_DIR/cookies.txt\""
  fi
  echo "   アプリ本体ごと消す:        PURGE=1 $0"
fi

if [ -f "$REPO/cookies.txt" ]; then
  echo "   リポジトリにも Cookie が残っています: rm \"$REPO/cookies.txt\""
fi
echo "ℹ️  Cookie が漏れたかもしれない場合は、Google アカウント > セキュリティ > お使いのデバイス"
echo "   で該当するログイン（セッション）をログアウトさせてください。ファイルを消すだけでは無効になりません。"
