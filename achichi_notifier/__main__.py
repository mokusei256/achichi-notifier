"""CLI entry point.

    python -m achichi_notifier run [--role primary|fallback] [--dry-run]
    python -m achichi_notifier preview [--send]
    python -m achichi_notifier check
    python -m achichi_notifier stop | resume | status
    python -m achichi_notifier delete --since 1h [--yes]
    python -m achichi_notifier watch <url> | --list | --remove <url>
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config, ConfigError, load_dotenv
from .discord_api import DiscordClient, DiscordError
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


def _refresh_browser(cfg: Config, dwell: float | None, url: str) -> int:
    log = logging.getLogger("achichi_notifier")
    if sys.platform not in ("win32", "darwin"):
        log.info("refresh-browser only runs on Windows and macOS; nothing to do here")
        return 0
    if not cfg.firefox_profile:
        log.error("YOUTUBE_FIREFOX_PROFILE is not set")
        return 2
    from .browser_refresh import GOOD, NOT_COUNTED, find_firefox, refresh
    if sys.platform != "win32":
        # launchd stops the job with SIGTERM (install.sh re-run, uninstall.sh). Turn that into
        # SystemExit so refresh()'s cleanup still stops the headless Firefox, which runs in its
        # own process group and would otherwise keep holding the bot profile.
        import signal

        def _stop(signum, _frame):
            raise SystemExit(128 + signum)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, _stop)
    firefox = find_firefox(str(cfg.firefox_exe) if cfg.firefox_exe else None)
    result = refresh(cfg.firefox_profile, firefox, dwell=dwell or cfg.refresh_seconds, url=url)
    return 0 if result in GOOD + NOT_COUNTED else 1


TAB_LABELS = {"videos": "動画", "shorts": "ショート", "streams": "配信"}


def _check_sources(yt, problems: list[str]):
    """The channel tabs, the RSS feed and the logged-out posts tab, judged the way a run judges
    them: an RSS failure is only a problem when the tabs do not cover it. Returns the posts page."""
    from .youtube import VIDEO_TABS

    counts, tab_problems = [], []
    for tab in VIDEO_TABS:
        try:
            refs, _ = yt.fetch_tab(tab)
        except YouTubeError as exc:
            tab_problems.append(f"{tab}: {exc}")
            continue
        counts.append(f"{TAB_LABELS.get(tab, tab)} {len(refs)} 件")
        if not refs:
            tab_problems.append(f"{tab}: 一覧が 0 件（YouTube の表示形式が変わった可能性があります）")
    if tab_problems:
        problems.append("YouTube タブ: " + " / ".join(tab_problems))
    else:
        print("✅ [OK] YouTube タブ: " + " / ".join(counts))
    try:
        refs, title = yt.fetch_rss()
        print(f"✅ [OK] YouTube RSS: {title} / {len(refs)} 件")
    except YouTubeError as exc:
        if tab_problems:
            problems.append(f"YouTube RSS: {exc}")
        else:
            print(f"ℹ️  [--] YouTube RSS: {exc}（ときどき起きます。タブで足りているので通知には影響しません）")
    try:
        public_page = yt.fetch_posts_page(member=False)
    except YouTubeError as exc:
        problems.append(f"YouTube 投稿タブ（ログアウト表示）: {exc}")
        return None
    print(f"✅ [OK] YouTube 投稿タブ（ログアウト表示）: {len(public_page.posts)} 件")
    return public_page


def _check(cfg: Config) -> int:
    """Log in through the gateway once (Discord requires this before a bot can post)
    and verify channel permissions, then check YouTube access."""
    import asyncio

    import discord

    from .youtube import YouTubeClient

    cfg.require_discord()
    problems: list[str] = []
    from . import destinations as dests
    from .notifier import make_store
    try:
        rest = DiscordClient(cfg.discord_token)
        state = make_store(cfg, rest).load()
        extra_destinations = [d for d in dests.resolve(cfg, state) if d.name != dests.MAIN and d.enabled]
        from .control import RESUME_HINT, pause_reason
        stopped = pause_reason(cfg, rest)
        if stopped:
            print(f"⏸ [!!] Bot を止めています（{stopped}）。Discord には何も送りません。{RESUME_HINT}。")
    except DiscordError as exc:
        problems.append(f"共有の状態を読めません: {exc}")
        extra_destinations = []

    async def gateway() -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready() -> None:
            try:
                print(f"✅ [OK] Bot としてログイン: {client.user} (id={client.user.id})")
                targets = {f"通知({k})": cid for k, cid in cfg.channel_ids.items()}
                for dest in extra_destinations:
                    for group, cid in sorted(dest.channels.items()):
                        targets[f"送り先 {dest.name}({group})"] = cid
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
    public_page = _check_sources(yt, problems)
    if cfg.firefox_profile:
        from .browser_refresh import read_status
        from .firefox_cookies import profile_in_use
        print(f"ℹ️  [--] Cookie の取得元: Bot 用 Firefox プロファイル（{yt.cookie_info or '読み取りできず'}）")
        in_use = profile_in_use(cfg.firefox_profile)
        if in_use:
            print("ℹ️  [--] Bot 用 Firefox が開いています（ログインが済んだらメニューから終了してください）")
        status = read_status(cfg.firefox_profile)
        if status:
            print(f"ℹ️  [--] 最後の自動更新: {status.get('last_attempt', '?')} 結果={status.get('last_result', '?')}"
                  f" 連続失敗={status.get('consecutive_failures', 0)} PSIDTS rotated={status.get('psidts_rotated')}")
        if cfg.cookies_file or cfg.cookies_secret:
            print("ℹ️  [--] YOUTUBE_COOKIES_FILE / B64 は使われていません（Firefox プロファイルを優先）")
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

    p_prev = sub.add_parser("preview", help="render the latest item of each type (streams: open slots "
                                            "or live only, EXCLUDE_LIVE_TITLES skipped)")
    p_prev.add_argument("--send", action="store_true", help="actually post the previews")
    p_prev.add_argument("--dest", default="main", help="destination to preview for (default: main)")
    p_prev.add_argument("--force", action="store_true",
                        help="send even while a runner is on an older version (it may then skip the real notification)")

    sub.add_parser("check", help="gateway login + permission and YouTube checks")

    p_ref = sub.add_parser("refresh-browser",
                           help="open YouTube headlessly in the bot's Firefox profile (daily task, Windows/macOS)")
    p_ref.add_argument("--dwell", type=float, help="seconds to keep the page open (default FIREFOX_REFRESH_SECONDS)")
    p_ref.add_argument("--url", default="https://www.youtube.com/")

    p_stop = sub.add_parser("stop", help="emergency stop: posts a marker in #管理; every runner then "
                                          "sends nothing from its next run")
    p_stop.add_argument("--reason", help="a note written into the marker")
    sub.add_parser("resume", help="delete the stop marker (a 🛑 reaction in #管理 must be removed by hand)")
    sub.add_parser("status", help="show whether the bot is stopped and what it held back")
    p_del = sub.add_parser("delete", help="delete the bot's own messages in the destination channels "
                                          "(lists them unless --yes)")
    p_del.add_argument("--since", help="messages from this long ago, e.g. 30m, 2h, 1d")
    p_del.add_argument("--last", type=int, help="the newest N matching bot messages (across the channels)")
    p_del.add_argument("--url", help="messages about this YouTube video/post (URL or id)")
    p_del.add_argument("--dest", action="append", help="only this destination (repeatable; default: all)")
    p_del.add_argument("--channel", action="append",
                       help="a channel id, e.g. of a removed destination (repeatable; #管理 is never touched)")
    p_del.add_argument("--ids", help="only these message ids, comma-separated (the list prints the command)")
    p_del.add_argument("--yes", action="store_true", help="really delete (otherwise only list)")

    p_watch = sub.add_parser("watch", help="announce when a stream on another channel (e.g. a collab) "
                                            "goes on air")
    p_watch.add_argument("url", nargs="?", help="the stream's URL or video id")
    p_watch.add_argument("--list", action="store_true", help="show the registered streams")
    p_watch.add_argument("--remove", metavar="URL_OR_ID", help="stop watching this stream")

    p_dest = sub.add_parser("dest", help="manage notification destinations (other Discord servers)")
    dsub = p_dest.add_subparsers(dest="dest_command", required=True)
    dsub.add_parser("list", help="show all destinations")
    d_add = dsub.add_parser("add", help="add or update a destination")
    d_add.add_argument("name", help="short name, e.g. bigserver")
    d_add.add_argument("--channel", required=True, help="channel id for everything")
    for group in ("video", "short", "live", "post"):
        d_add.add_argument(f"--{group}-channel", help=f"separate channel id for {group} notifications")
    d_add.add_argument("--kinds", help="comma list of video,short,live,post,member_post (default: all)")
    for key in ("video", "short", "live", "post", "member_post"):
        d_add.add_argument(f"--mention-{key.replace('_', '-')}", dest=f"mention_{key}",
                           help=f"mention for {key}, e.g. '<@&ROLE_ID>' or @everyone")
    d_add.add_argument("--clear-mentions", action="store_true",
                       help="drop the mentions saved earlier (then apply only the --mention-* given now)")
    d_rm = dsub.add_parser("remove", help="delete a destination")
    d_rm.add_argument("name")
    for verb in ("enable", "disable"):
        d_en = dsub.add_parser(verb, help=f"{verb} a destination")
        d_en.add_argument("name")
    d_chk = dsub.add_parser("check", help="check the bot can see/post to a destination's channels")
    d_chk.add_argument("name")
    d_chk.add_argument("--send-test", action="store_true", help="also post a short test message")

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
            return preview(cfg, send=args.send, dest_name=args.dest, force=args.force)
        if args.command == "refresh-browser":
            return _refresh_browser(cfg, args.dwell, args.url)
        if args.command == "dest":
            from . import dest_cli
            if args.dest_command == "list":
                return dest_cli.cmd_list(cfg)
            if args.dest_command == "add":
                return dest_cli.cmd_add(cfg, args)
            if args.dest_command == "remove":
                return dest_cli.cmd_remove(cfg, args.name)
            if args.dest_command in ("enable", "disable"):
                return dest_cli.cmd_enable(cfg, args.name, args.dest_command == "enable")
            if args.dest_command == "check":
                return dest_cli.cmd_check(cfg, args.name, args.send_test)
        if args.command == "check":
            return _check(cfg)
        if args.command == "watch":
            from . import watch_cli
            if args.remove:
                return watch_cli.cmd_remove(cfg, args.remove)
            if args.list or not args.url:
                return watch_cli.cmd_list(cfg)
            return watch_cli.cmd_add(cfg, args.url)
        if args.command in ("stop", "resume", "status", "delete"):
            from . import control_cli
            if args.command == "stop":
                return control_cli.cmd_stop(cfg, args.reason)
            if args.command == "resume":
                return control_cli.cmd_resume(cfg)
            if args.command == "status":
                return control_cli.cmd_status(cfg)
            return control_cli.cmd_delete(cfg, since=args.since, last=args.last, url=args.url,
                                          dest_names=args.dest, yes=args.yes,
                                          channel_ids=args.channel, ids=args.ids)
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
