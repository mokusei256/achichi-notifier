"""CLI entry point.

    python -m achichi_notifier run [--role primary|fallback] [--dry-run]
    python -m achichi_notifier preview [--send]
    python -m achichi_notifier check
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config, ConfigError, load_dotenv
from .discord_api import DiscordError
from .youtube import YouTubeError


class _GitHubFormatter(logging.Formatter):
    """Surface warnings/errors as GitHub Actions annotations."""

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        if record.levelno >= logging.ERROR:
            return "::error::" + line
        if record.levelno >= logging.WARNING:
            return "::warning::" + line
        return line


def _setup_logging(verbose: bool, log_file: str | None = None) -> None:
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    formatter = (_GitHubFormatter(fmt) if os.environ.get("GITHUB_ACTIONS") == "true"
                 else logging.Formatter(fmt))
    handlers: list[logging.Handler] = []
    if log_file:
        # Windows Task Scheduler runs pythonw.exe without a console, so logs go to a file.
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    if sys.stderr is not None:  # None under pythonw.exe
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers, force=True)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.ERROR)


def _safe_console() -> None:
    """Never crash on characters the console encoding cannot show (e.g. cp932 + emoji)."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="backslashreplace")
            except (ValueError, OSError):
                pass


def _check(cfg: Config) -> int:
    """Log in through the gateway once (Discord requires this before a bot can post)
    and verify channel permissions, then check YouTube access."""
    import asyncio

    import discord

    from .youtube import YouTubeClient

    cfg.require_discord()
    problems: list[str] = []

    async def gateway() -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready() -> None:
            try:
                print(f"✅ [OK] Bot としてログイン: {client.user} (id={client.user.id})")
                targets = {f"通知({k})": cid for k, cid in cfg.channel_ids.items()}
                if cfg.state_backend == "discord":
                    targets["状態保存"] = cfg.state_channel_id
                for label, cid in targets.items():
                    channel = client.get_channel(int(cid))
                    if channel is None:
                        problems.append(f"{label}: チャンネル {cid} が見えません（Bot 未招待 or ID 誤り）")
                        continue
                    perms = channel.permissions_for(channel.guild.me)
                    need = {"view_channel": "チャンネルを見る", "send_messages": "メッセージを送信",
                            "embed_links": "埋め込みリンク", "read_message_history": "メッセージ履歴を読む"}
                    if label == "状態保存":
                        need["attach_files"] = "ファイルを添付"
                    missing = [name for attr, name in need.items() if not getattr(perms, attr)]
                    if missing:
                        problems.append(f"{label} #{channel.name}: 権限不足 → " + ", ".join(missing))
                    else:
                        print(f"✅ [OK] {label}: #{channel.name} ({channel.guild.name})")
            finally:
                await client.close()

        async with client:
            await client.start(cfg.discord_token)

    try:
        asyncio.run(gateway())
    except discord.LoginFailure:
        problems.append("Discord: トークンが無効です（DISCORD_BOT_TOKEN を確認）")
    except discord.HTTPException as exc:
        problems.append(f"Discord: 接続エラー ({exc.status})")

    from .notifier import classify_member_posts, comparable_public_page, member_page_problem

    yt = YouTubeClient.from_config(cfg)
    public_page = None
    try:
        refs, title = yt.fetch_rss()
        print(f"✅ [OK] YouTube RSS: {title} / {len(refs)} 件")
        public_page = yt.fetch_posts_page(member=False)
        print(f"✅ [OK] YouTube 投稿タブ（ログアウト表示）: {len(public_page.posts)} 件")
    except YouTubeError as exc:
        problems.append(f"YouTube: {exc}")
    if not yt.cookies_configured:
        print("ℹ️  [--] Cookie 未設定: メン限投稿は検知されません")
    elif yt.cookie_error or not yt.has_cookies:
        problems.append(f"Cookie: {yt.cookie_error}")
    else:
        try:
            member_page = yt.fetch_posts_page(member=True)
        except YouTubeError as exc:
            problems.append(f"Cookie 付きの投稿タブを取得できません: {exc}")
        else:
            label = {"member": "メンバー", "not_member": "メンバーではない", "unknown": "判定できず"}
            comparable = comparable_public_page(public_page, member_page)
            problem = member_page_problem(member_page, comparable) if member_page.logged_in else None
            badge, by_comparison = (classify_member_posts(member_page, comparable)
                                    if member_page.logged_in and not problem else ([], []))
            ok = member_page.logged_in and member_page.membership != "not_member" and not problem
            print(f"{'✅ [OK]' if ok else '❌ [NG]'} Cookie: ログイン状態={member_page.logged_in}"
                  f" / 加入状態={label.get(member_page.membership, member_page.membership)}"
                  f" / メン限投稿 {len(badge) + len(by_comparison)} 件（内容は表示しません）")
            if not member_page.logged_in:
                problems.append("Cookie でログイン状態になりません（失効の可能性）")
            elif member_page.membership == "not_member":
                problems.append("Cookie のアカウントがチャンネルのメンバーではありません")
            elif problem:
                problems.append(f"Cookie 付きの投稿タブが想定外の形です（{problem}）")

    for problem in problems:
        print(f"❌ [NG] {problem}")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="achichi_notifier")
    parser.add_argument("--env-file", default=".env", help="path to .env (default: ./.env)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--log-file", help="also write logs to this file (rotated at 1MB)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="check YouTube once and post new items")
    p_run.add_argument("--role", choices=("primary", "fallback"), default="primary",
                       help="fallback = run only when the primary runner looks down")
    p_run.add_argument("--dry-run", action="store_true",
                       help="print what would be posted; do not post or save state")
    p_run.add_argument("--state-file", help="use a local state file instead of Discord")
    p_run.add_argument("--max-age-hours", type=float, help="override MAX_AGE_HOURS")

    p_prev = sub.add_parser("preview", help="render the latest item of each type")
    p_prev.add_argument("--send", action="store_true", help="actually post the previews")

    sub.add_parser("check", help="gateway login + permission and YouTube checks")

    args = parser.parse_args(argv)
    _safe_console()
    _setup_logging(args.verbose, args.log_file)
    if sys.stderr is None:
        sys.excepthook = lambda kind, value, tb: logging.getLogger("achichi_notifier").critical(
            "uncaught error", exc_info=(kind, value, tb))
    load_dotenv(Path(args.env_file))
    if getattr(args, "state_file", None):
        os.environ["STATE_BACKEND"] = "file"
        os.environ["STATE_FILE"] = args.state_file
    if getattr(args, "max_age_hours", None) is not None:
        os.environ["MAX_AGE_HOURS"] = str(args.max_age_hours)

    try:
        cfg = Config.from_env()
        if args.command == "run":
            from .notifier import run
            return run(cfg, role=args.role, dry_run=args.dry_run)
        if args.command == "preview":
            from .notifier import preview
            return preview(cfg, send=args.send)
        if args.command == "check":
            return _check(cfg)
    except ConfigError as exc:
        logging.getLogger("achichi_notifier").error("config error: %s", exc)
        return 2
    except (DiscordError, YouTubeError) as exc:
        logging.getLogger("achichi_notifier").error("%s", exc)
        return 1
    except Exception:  # pythonw.exe has no console: make sure the traceback reaches the log
        logging.getLogger("achichi_notifier").exception("unexpected error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
