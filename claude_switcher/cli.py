"""claude-switcher: command line for setting up and driving the switcher."""

import argparse
import getpass
import html
import logging
import re
import subprocess
import sys
import time

from . import claude_files, config, oauth, store
from .daemon import Switcher
from .telegram import Bot, TelegramError


def _plain(text):
    """Telegram HTML to terminal text."""
    text = re.sub(r'<a href="([^"]+)">([^<]*)</a>', r"\2 (\1)", text or "")
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def cmd_setup_telegram(args):
    cfg = config.load_config()
    token = args.token or getpass.getpass("Bot token from @BotFather (input hidden): ").strip()
    bot = Bot(token)
    try:
        me = bot.call("getMe")
    except TelegramError as e:
        print(f"That token did not work: {e}")
        return 1
    print(f"Bot @{me['username']} found. Open https://t.me/{me['username']} and send it /start")
    print("Waiting for your message…")
    offset = 0
    deadline = time.time() + 300
    while time.time() < deadline:
        for update in bot.updates(offset, timeout=30):
            offset = update["update_id"] + 1
            chat = (update.get("message") or {}).get("chat") or {}
            if chat.get("type") == "private":
                bot.chat_id = chat["id"]
                break
        if bot.chat_id:
            break
    if not bot.chat_id:
        print("No message arrived within five minutes; run this again.")
        return 1
    bot.updates(offset, timeout=0)  # acknowledge, so the daemon starts after it
    cfg["telegram"] = {"bot_token": token, "chat_id": bot.chat_id}
    config.save_config(cfg)
    bot.send("✅ claude-switcher will report here. Only this chat can control it.")
    print(f"Saved. Messages go to chat {bot.chat_id}; the bot ignores every other chat.")
    # The daemon reads its settings at start.
    subprocess.run(["systemctl", "--user", "try-restart", "claude-switcher.service"], check=False)
    return 0


def cmd_import_current(args):
    label = store.check_label(args.label)
    live = claude_files.read_live_oauth()
    if not live:
        print("Claude Code is not logged in on this machine.")
        return 1
    profile = oauth.fetch_profile(live["accessToken"])
    with store.app_lock():
        accounts = store.load_accounts()
        state = store.load_state()
        uuid = profile["account"]["uuid"]
        existing = store.find_by_uuid(accounts, uuid)
        if existing and existing != label:
            print(f"This login is already enrolled as '{existing}'.")
            return 1
        acct = accounts.get(label) or {"label": label, "added_at": oauth.now_ms()}
        acct.update(
            email=profile["account"].get("email"),
            account_uuid=uuid,
            org_uuid=profile["organization"]["uuid"],
            oauth=live,
            oauth_account=oauth.oauth_account_from_profile(profile, claude_files.read_oauth_account()),
        )
        store.save_account(acct)
        store.account_state(state, label)["auth"] = "ok"
        state["active"] = label
        store.save_state(state)
    print(f"Enrolled '{label}' ({acct['email']}), the account Claude Code runs on now.")
    return 0


def cmd_add(args):
    label = store.check_label(args.label)
    sw = Switcher()
    with store.app_lock():
        state = store.load_state()
        url = sw.start_auth(label, "add", state, args.email)
    print("1. Open this link in any browser and sign in to the account to add"
          + (f" ({args.email})" if args.email else "") + ":\n")
    print(url + "\n")
    print("2. The page then shows a code. Paste it here (or send it to the Telegram bot).")
    pasted = input("Code: ").strip()
    with store.app_lock():
        reply = sw.complete_auth(pasted, store.load_accounts(), store.load_state())
    print(_plain(reply or "That was not a code."))
    return 0 if reply and reply.startswith("✅") else 1


def cmd_status(args):
    sw = Switcher()
    with store.app_lock():
        accounts = store.load_accounts()
        state = store.load_state()
        for label in accounts:
            sw._poll(label, accounts, state)
        store.save_state(state)
        print(_plain(sw.status_text(accounts, state)))
    return 0


def cmd_sessions(args):
    print(_plain(Switcher().sessions_text()))
    return 0


def cmd_switch(args):
    sw = Switcher()
    with store.app_lock():
        accounts = store.load_accounts()
        state = store.load_state()
        if args.label not in accounts:
            print(f"Unknown account '{args.label}'. Enrolled: {', '.join(accounts) or 'none'}")
            return 1
        if args.label == state["active"]:
            print(f"Already on '{args.label}'.")
            return 0
        sw._sync_live(accounts, state)
        sw.switch_to(args.label, accounts, state, "a manual switch from the command line")
        waiting = len(state["rc_wanted"])
    print(f"Switched to '{args.label}'.")
    if waiting:
        print(f"{waiting} session(s) had Remote Control on; the running daemon brings it back.")
    return 0


def cmd_remove(args):
    with store.app_lock():
        state = store.load_state()
        if args.label == state["active"]:
            print("That is the account Claude Code runs on now; switch away first.")
            return 1
        store.delete_account(args.label)
        state["accounts"].pop(args.label, None)
        store.save_state(state)
    print(f"Removed '{args.label}'.")
    return 0


def cmd_auto(args):
    with store.app_lock():
        state = store.load_state()
        state["auto"] = args.command == "resume"
        store.save_state(state)
    print("Automatic switching " + ("resumed." if state["auto"] else "paused."))
    return 0


def cmd_test_telegram(args):
    sw = Switcher()
    if not sw.bot.configured:
        print("Telegram is not set up: run `claude-switcher setup-telegram`.")
        return 1
    return 0 if sw.notify("👋 Test message from claude-switcher.") else 1


def cmd_daemon(args):
    Switcher().run()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="claude-switcher", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup-telegram", help="connect the Telegram bot and your chat")
    p.add_argument("--token", help="bot token (asked for, hidden, when left out)")
    p.set_defaults(func=cmd_setup_telegram)

    p = sub.add_parser("import-current", help="enroll the account Claude Code is logged into now")
    p.add_argument("label")
    p.set_defaults(func=cmd_import_current)

    p = sub.add_parser("add", help="enroll another account through a sign-in link")
    p.add_argument("label")
    p.add_argument("--email", help="pre-fill this address on the sign-in page")
    p.set_defaults(func=cmd_add)

    sub.add_parser("status", help="every account's usage, fetched now").set_defaults(func=cmd_status)
    sub.add_parser("sessions", help="running sessions and Remote Control").set_defaults(func=cmd_sessions)

    p = sub.add_parser("switch", help="move every session to an account now")
    p.add_argument("label")
    p.set_defaults(func=cmd_switch)

    p = sub.add_parser("remove", help="forget an account")
    p.add_argument("label")
    p.set_defaults(func=cmd_remove)

    sub.add_parser("pause", help="stop automatic switching").set_defaults(func=cmd_auto)
    sub.add_parser("resume", help="restart automatic switching").set_defaults(func=cmd_auto)
    sub.add_parser("test-telegram", help="send a test message").set_defaults(func=cmd_test_telegram)
    sub.add_parser("daemon", help="run the switcher (systemd runs this)").set_defaults(func=cmd_daemon)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.command == "daemon" else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args) or 0
    except (oauth.HttpError, claude_files.LockTimeout, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
