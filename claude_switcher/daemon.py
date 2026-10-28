"""The switcher: polls every account, swaps the live login, talks to Telegram.

Who owns a login's tokens: the account Claude Code is logged into is owned by
the live `~/.claude/.credentials.json` (sessions rotate its refresh token and
write it there); every other account is owned by its file in our store, and
only this tool refreshes it. A switch hands the outgoing account's latest
tokens back to the store and the incoming account's to the live file, inside
Claude Code's own refresh lock.
"""

import logging
import os
import socket
import threading
import time
from datetime import datetime
from html import escape

from . import claude_files, config, oauth, policy, store
from . import remote_control as rc
from .telegram import Bot, TelegramError

log = logging.getLogger("claude_switcher")

TICK_S = 5
REFRESH_MARGIN_MS = 10 * 60 * 1000
TARGET_MIN_VALIDITY_MS = 30 * 60 * 1000
CANDIDATE_MAX_AGE_MS = 2 * 60 * 1000
AUTH_FLOW_TTL_MS = 24 * 3600 * 1000
ERROR_RETRY_S = 60
# One poll is a 50 s long poll plus the commands it brings; well past that,
# the bot is stuck.
TELEGRAM_STALL_S = 600


def sd_notify(message):
    """Tell systemd how the service is (Type=notify); nothing outside systemd."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())
    except OSError as e:
        log.warning("sd_notify failed: %s", e)


def fmt_time(value):
    """A reset time or an epoch-ms expiry, in this machine's time zone."""
    if not value:
        return "?"
    try:
        if isinstance(value, (int, float)):
            moment = datetime.fromtimestamp(value / 1000)
        else:
            moment = datetime.fromisoformat(value)
        return moment.astimezone().strftime("%a %d %b %H:%M %Z")
    except (ValueError, OSError):
        return str(value)


def usage_line(usage):
    if not usage:
        return "usage unknown"
    line = (
        f"session {usage['session']:.0f}% (resets {fmt_time(usage.get('session_resets'))}) · "
        f"weekly {usage['weekly']:.0f}% (resets {fmt_time(usage.get('weekly_resets'))})"
    )
    for scoped in usage.get("scoped") or []:
        if scoped.get("percent"):
            line += f" · {escape(str(scoped['name']))} weekly {scoped['percent']:.0f}%"
    return line


HELP = """<b>claude-switcher</b>
/status: every account's usage
/sessions: running sessions and Remote Control
/switch &lt;label&gt;: move every session to that account now
/auth &lt;label&gt;: a new sign-in link for an account
/add &lt;label&gt; [email]: enroll another account
/pause, /resume: stop or restart automatic switching

After a sign-in, paste the code the page shows here."""


class Switcher:
    def __init__(self, cfg=None):
        self.cfg = cfg or config.load_config()
        tg = self.cfg["telegram"]
        self.bot = Bot(tg.get("bot_token"), tg.get("chat_id"))
        self.follower = rc.Follower(self.notify, self.cfg)
        self._next_poll = {}
        self._telegram_thread = None
        self.telegram_polled_at = time.monotonic()

    def notify(self, html, button=None, quiet=False):
        log.info("notify: %s", html.replace("\n", " | "))
        return self.bot.send(html, button=button, quiet=quiet)

    def _alert(self, state, key, html, every_s, button=None):
        """Notify at most once per `every_s` for the same condition."""
        sent = state["alerts"].get(key, 0)
        if oauth.now_ms() - sent < every_s * 1000:
            return
        state["alerts"][key] = oauth.now_ms()
        self.notify(html, button=button)

    # Tokens

    def _refresh(self, label, acct, state):
        try:
            resp = oauth.refresh(acct["oauth"]["refreshToken"])
        except oauth.LoginExpired:
            self._login_expired(label, acct, state)
            raise
        return oauth.oauth_from_tokens(resp, previous=acct["oauth"])

    def _fresh_oauth(self, label, accounts, state, min_validity_ms=REFRESH_MARGIN_MS):
        """A usable access token for `label`, refreshing it when close to expiry."""
        acct = accounts[label]
        if label == state["active"]:
            live = claude_files.read_live_oauth()
            if live:
                acct["oauth"] = live
        current = acct["oauth"]
        if current["expiresAt"] - oauth.now_ms() > min_validity_ms:
            return current
        if label != state["active"]:
            acct["oauth"] = self._refresh(label, acct, state)
            store.save_account(acct)
            return acct["oauth"]
        with claude_files.oauth_locks():
            # Re-read under the lock: a session may have rotated it meanwhile,
            # and refreshing a superseded refresh token would fail.
            acct["oauth"] = claude_files.read_live_oauth() or current
            if acct["oauth"]["expiresAt"] - oauth.now_ms() <= min_validity_ms:
                acct["oauth"] = self._refresh(label, acct, state)
                claude_files.write_live_oauth(acct["oauth"])
        store.save_account(acct)
        return acct["oauth"]

    def _login_expired(self, label, acct, state):
        st = store.account_state(state, label)
        st["auth"] = "expired"
        flow_age = oauth.now_ms() - st.get("reauth_sent_at", 0)
        if flow_age > self.cfg["reauth_reminder_hours"] * 3600 * 1000:
            self.send_auth_link(label, state, f"The login for <b>{escape(label)}</b> has expired.")

    # Knowing which account is live

    def _sync_live(self, accounts, state):
        """Follow token rotation by sessions, and notice a manual /login."""
        live = claude_files.read_live_oauth()
        active = state["active"]
        if not live:
            if active:
                self._alert(state, "logged-out", "⚠️ Claude Code is logged out on this machine. "
                            "/switch &lt;label&gt; logs it back in.", 6 * 3600)
            return
        state["alerts"].pop("logged-out", None)
        if active in accounts and live.get("refreshToken") == accounts[active]["oauth"].get("refreshToken"):
            if live != accounts[active]["oauth"]:
                accounts[active]["oauth"] = live
                store.save_account(accounts[active])
        else:
            self._identify_live(live, accounts, state)
        active = state["active"]
        if active in accounts:
            wanted = accounts[active].get("account_uuid")
            shown = (claude_files.read_oauth_account() or {}).get("accountUuid")
            if shown != wanted and accounts[active].get("oauth_account"):
                claude_files.write_oauth_account(accounts[active]["oauth_account"])

    def _identify_live(self, live, accounts, state):
        try:
            profile = oauth.fetch_profile(live["accessToken"])
        except oauth.HttpError as e:
            if e.status != 401:
                raise
            # Expired with no session around to refresh it: refresh it the way
            # a session would, then ask again.
            with claude_files.oauth_locks():
                live = claude_files.read_live_oauth()
                live = oauth.oauth_from_tokens(oauth.refresh(live["refreshToken"]), previous=live)
                claude_files.write_live_oauth(live)
            profile = oauth.fetch_profile(live["accessToken"])
        uuid = profile["account"]["uuid"]
        email = profile["account"].get("email")
        label = store.find_by_uuid(accounts, uuid)
        if label is None:
            label = self._unused_label(email, accounts)
            accounts[label] = {
                "label": label,
                "email": email,
                "account_uuid": uuid,
                "org_uuid": profile["organization"]["uuid"],
                "added_at": oauth.now_ms(),
                "oauth_account": oauth.oauth_account_from_profile(profile, claude_files.read_oauth_account()),
            }
            self.notify(f"➕ Claude Code was logged in by hand as {escape(email or uuid)}; "
                        f"enrolled it as <b>{label}</b>.")
        elif label != state["active"]:
            self.notify(f"👀 Claude Code was logged in by hand as <b>{label}</b> "
                        f"({escape(email or '')}); following it.")
        accounts[label]["oauth"] = live
        store.save_account(accounts[label])
        store.account_state(state, label)["auth"] = "ok"
        state["active"] = label

    @staticmethod
    def _unused_label(email, accounts):
        base = "".join(c for c in (email or "account").split("@")[0].lower() if c.isalnum() or c in "-_")[:24]
        base = base or "account"
        label, n = base, 2
        while label in accounts:
            label, n = f"{base}{n}", n + 1
        return label

    # Usage

    def _poll(self, label, accounts, state):
        st = store.account_state(state, label)
        try:
            token = self._fresh_oauth(label, accounts, state)["accessToken"]
            try:
                raw = oauth.fetch_usage(token)
            except oauth.HttpError as e:
                if e.status != 401:
                    raise
                accounts[label]["oauth"]["expiresAt"] = 0  # refused: refresh and retry once
                token = self._fresh_oauth(label, accounts, state)["accessToken"]
                raw = oauth.fetch_usage(token)
        except oauth.LoginExpired:
            self._next_poll[label] = time.monotonic() + self.cfg["poll_seconds"]
            return
        except (oauth.HttpError, OSError, ValueError, KeyError, claude_files.LockTimeout) as e:
            st["error"] = f"{type(e).__name__}: {e}"[:300]
            log.warning("usage poll for %s failed: %s", label, st["error"])
            self._next_poll[label] = time.monotonic() + ERROR_RETRY_S
            return
        st["usage"] = oauth.summarize_usage(raw)
        st["auth"] = "ok"
        st.pop("error", None)
        fast = label == state["active"] and max(st["usage"]["session"], st["usage"]["weekly"]) >= self.cfg[
            "fast_poll_above_percent"
        ]
        self._next_poll[label] = time.monotonic() + self.cfg["fast_poll_seconds" if fast else "poll_seconds"]

    def _check_logins(self, accounts, state):
        """Warn ahead of a refresh token's own expiry, with a link to renew it."""
        horizon = self.cfg["reauth_warn_hours"] * 3600 * 1000
        for label, acct in accounts.items():
            expires = acct["oauth"].get("refreshTokenExpiresAt")
            st = store.account_state(state, label)
            if not expires or st.get("auth") != "ok" or expires - oauth.now_ms() > horizon:
                continue
            if oauth.now_ms() - st.get("reauth_sent_at", 0) > self.cfg["reauth_reminder_hours"] * 3600 * 1000:
                self.send_auth_link(
                    label, state,
                    f"⏰ The login for <b>{escape(label)}</b> expires {fmt_time(expires)}. "
                    "Sign in again to renew it.",
                )

    # Switching

    def _view(self, accounts, state):
        return {label: store.account_state(state, label) for label in accounts}

    def _maybe_switch(self, accounts, state):
        active = state["active"]
        if not state["auto"] or active not in accounts:
            return
        st = store.account_state(state, active)
        if st.get("auth") == "expired":
            reason = "its login expired"
        else:
            reason = st.get("usage") and policy.leave_reason(st["usage"], self.cfg)
        if not reason:
            state["alerts"].pop("exhausted", None)
            return
        since_last = oauth.now_ms() - state["last_switch_at"]
        if st.get("auth") == "ok" and since_last < self.cfg["min_seconds_between_switches"] * 1000:
            return
        # Choose on current numbers, not the ones from up to five minutes ago.
        for label in accounts:
            usage = store.account_state(state, label).get("usage")
            if label != active and (not usage or oauth.now_ms() - usage["fetched_at"] > CANDIDATE_MAX_AGE_MS):
                self._poll(label, accounts, state)
        while True:
            target, notes = policy.choose_target(
                self._view(accounts, state), active, self.cfg, CANDIDATE_MAX_AGE_MS, oauth.now_ms()
            )
            if not target:
                break
            try:
                self.switch_to(target, accounts, state, f"{escape(active)}: {reason}", notes)
                return
            except oauth.LoginExpired:
                continue  # marked expired, with a sign-in link sent; try the next one
        others = "\n".join(f"• {escape(k)}: {escape(v)}" for k, v in notes.items()) or "• no other accounts"
        self._alert(
            state, "exhausted",
            f"⚠️ <b>{escape(active)}</b> is at its limit ({reason}) and no other account has room:\n{others}",
            1800,
        )

    def switch_to(self, target, accounts, state, reason, notes=None):
        """Move this machine, and every running session, to `target`. Hold app_lock."""
        source = state["active"]
        acct = accounts[target]
        incoming = self._fresh_oauth(target, accounts, state, TARGET_MIN_VALIDITY_MS)
        try:
            profile = oauth.fetch_profile(incoming["accessToken"])
            acct["oauth_account"] = oauth.oauth_account_from_profile(profile, acct.get("oauth_account"))
        except (oauth.HttpError, OSError, ValueError) as e:
            log.warning("profile for %s not refreshed: %s", target, e)
        sessions = rc.live_sessions()
        epoch = oauth.now_ms()
        with claude_files.oauth_locks():
            outgoing = claude_files.read_live_oauth()
            if source in accounts and outgoing:
                accounts[source]["oauth"] = outgoing
                store.save_account(accounts[source])
            claude_files.write_live_oauth(incoming)
        store.save_account(acct)
        claude_files.write_oauth_account(acct["oauth_account"])
        state["active"] = target
        state["last_switch_at"] = epoch
        self._next_poll[target] = time.monotonic() + self.cfg["fast_poll_seconds"]

        restore = rc.restore_value(epoch)
        for sid, entry in state["rc_wanted"].items():  # still waiting from an earlier switch
            entry.update(owner=rc.owner_of(sid), since=epoch, account=target, restore=restore)
        for s in sessions:
            if s.bridge:
                state["rc_wanted"][s.session_id] = {
                    "session_id": s.session_id, "bridge": s.bridge, "name": s.title,
                    "owner": rc.owner_of(s.session_id), "since": epoch, "account": target,
                    "restore": restore,
                }
        rc.write_signal(state["rc_wanted"], self.cfg["remote_control"]["watch_minutes"] * 60)
        store.save_state(state)

        usage = store.account_state(state, target).get("usage")
        lines = [
            f"🔄 <b>Switched to {escape(target)}</b> ({escape(acct.get('email') or '')})",
            f"From {reason}",
            f"{escape(target)}: {usage_line(usage)}",
        ]
        for label, note in (notes or {}).items():
            if label != target:
                lines.append(f"• {escape(label)}: {escape(note)}")
        lines.append(f"{len(sessions)} running session(s) move over on their next request.")
        if state["rc_wanted"]:
            lines.append(f"Bringing Remote Control back in {len(state['rc_wanted'])} of them…")
        self.notify("\n".join(lines))
        self.follower.ensure()

    # Sign-in

    def send_auth_link(self, label, state, why):
        acct = store.load_accounts().get(label, {})
        email = acct.get("email")
        url = self.start_auth(label, "reauth" if acct else "add", state, email)
        store.account_state(state, label)["reauth_sent_at"] = oauth.now_ms()
        who = f" as <b>{escape(email)}</b>" if email else ""
        self.notify(
            f"🔑 {why}\nTap the button, sign in{who}, then paste the code the page shows here.",
            button=(f"Sign in{(' as ' + email) if email else ''}", url),
        )

    def start_auth(self, label, purpose, state, email_hint=None):
        store.check_label(label)
        url, verifier, flow_state = oauth.new_login(email_hint)
        pending = {
            k: v for k, v in state["pending_auth"].items()
            if v["label"] != label and oauth.now_ms() - v["created"] < AUTH_FLOW_TTL_MS
        }
        pending[flow_state] = {
            "label": label, "verifier": verifier, "purpose": purpose,
            "email": email_hint, "created": oauth.now_ms(),
        }
        state["pending_auth"] = pending
        store.save_state(state)
        return url

    def complete_auth(self, pasted, accounts, state):
        """Finish a sign-in from the pasted code; returns the reply to show."""
        code, flow_state = oauth.parse_code(pasted)
        pending = state["pending_auth"]
        if not pending or not code or len(code) < 20:
            return None  # not a code, or nothing waiting for one
        if flow_state is None and len(pending) == 1:
            flow_state = next(iter(pending))
        flow = pending.get(flow_state)
        if not flow:
            return "No sign-in is waiting for that code. /auth &lt;label&gt; sends a new link."
        try:
            resp = oauth.exchange_code(code, flow["verifier"], flow_state)
            profile = oauth.fetch_profile(resp["access_token"])
        except oauth.HttpError as e:
            return f"That code was not accepted ({e.status}). Codes work once and expire within minutes; try the link again."
        uuid = profile["account"]["uuid"]
        email = profile["account"].get("email")
        label = flow["label"]
        enrolled_as = store.find_by_uuid(accounts, uuid)
        if label in accounts and accounts[label].get("account_uuid") != uuid:
            if not enrolled_as:
                return (f"You signed in as {escape(email)}, but this link was for <b>{escape(label)}</b> "
                        f"({escape(accounts[label].get('email') or '')}). Nothing saved; "
                        "/add &lt;label&gt; enrolls a new account.")
            label = enrolled_as
        elif enrolled_as and enrolled_as != label:
            label = enrolled_as
        acct = accounts.get(label) or {"label": label, "added_at": oauth.now_ms()}
        acct.update(
            email=email,
            account_uuid=uuid,
            org_uuid=profile["organization"]["uuid"],
            oauth=oauth.oauth_from_tokens(resp, profile, previous=acct.get("oauth")),
            oauth_account=oauth.oauth_account_from_profile(profile, acct.get("oauth_account")),
        )
        accounts[label] = acct
        store.save_account(acct)
        pending.pop(flow_state, None)
        st = store.account_state(state, label)
        st.update(auth="ok", reauth_sent_at=0)
        if state["active"] == label or (state["active"] is None and claude_files.read_live_oauth() is None):
            with claude_files.oauth_locks():
                claude_files.write_live_oauth(acct["oauth"])
            claude_files.write_oauth_account(acct["oauth_account"])
            state["active"] = label
        self._next_poll[label] = 0
        store.save_state(state)
        until = acct["oauth"].get("refreshTokenExpiresAt")
        return (f"✅ <b>{escape(label)}</b> ({escape(email)}) is signed in"
                + (f"; the login lasts until {fmt_time(until)}." if until else "."))

    # Reports

    def status_text(self, accounts, state):
        if not accounts:
            return "No accounts enrolled yet. /add &lt;label&gt; enrolls one."
        lines = [f"<b>Auto-switch:</b> {'on' if state['auto'] else 'paused'}"
                 f" · switches at {self.cfg['switch_at_percent']}%"]
        for label, acct in accounts.items():
            st = store.account_state(state, label)
            mark = "▶️" if label == state["active"] else "▫️"
            head = f"{mark} <b>{escape(label)}</b> {escape(acct.get('email') or '')}"
            if st.get("auth") != "ok":
                lines.append(f"{head}\n    🔑 needs sign-in (/auth {escape(label)})")
                continue
            body = usage_line(st.get("usage"))
            if st.get("error"):
                body += f"\n    ⚠️ {escape(st['error'][:120])}"
            lines.append(f"{head}\n    {body}")
        if state["last_switch_at"]:
            lines.append(f"Last switch: {fmt_time(state['last_switch_at'])}")
        return "\n".join(lines)

    def sessions_text(self):
        sessions = rc.live_sessions()
        if not sessions:
            return "No Claude Code sessions are running."
        lines = []
        hows = {"mod": "rc-keeper loaded", "outdated": "rc-keeper outdated (/reload-plugins)",
                "manual": "rc-keeper not loaded (/reload-plugins)"}
        for s in sessions:
            how = hows[rc.owner_of(s.session_id)]
            remote = f"<a href=\"{s.url}\">Remote Control on</a>" if s.bridge else "Remote Control off"
            lines.append(f"• <b>{escape(s.title)}</b> (pid {s.pid}, {escape(s.status or '?')}): {remote} · {how}")
        return "\n".join(lines)

    # Telegram commands

    def handle_message(self, text, message_id=None):
        text = (text or "").strip()
        command, _, rest = text.partition(" ")
        command = command.split("@")[0].lower()
        args = rest.split()
        with store.app_lock():
            accounts = store.load_accounts()
            state = store.load_state()
            if command in ("/start", "/help"):
                return HELP
            if command == "/status":
                return self.status_text(accounts, state)
            if command == "/sessions":
                return self.sessions_text()
            if command in ("/pause", "/resume"):
                state["auto"] = command == "/resume"
                store.save_state(state)
                return f"Automatic switching {'resumed' if state['auto'] else 'paused'}."
            if command == "/switch":
                if len(args) != 1 or args[0] not in accounts:
                    return f"Usage: /switch &lt;label&gt;, one of: {escape(', '.join(accounts))}"
                if args[0] == state["active"]:
                    return f"Already on {escape(args[0])}."
                self.switch_to(args[0], accounts, state, "a /switch from Telegram")
                return None
            if command == "/auth":
                if len(args) != 1 or args[0] not in accounts:
                    return f"Usage: /auth &lt;label&gt;, one of: {escape(', '.join(accounts))}"
                self.send_auth_link(args[0], state, f"Sign-in for <b>{escape(args[0])}</b>.")
                store.save_state(state)
                return None
            if command == "/add":
                if not args:
                    return "Usage: /add &lt;label&gt; [email]"
                label, email = args[0].lower(), (args[1] if len(args) > 1 else None)
                try:
                    url = self.start_auth(label, "add", state, email)
                except ValueError as e:
                    return escape(str(e))
                self.notify(
                    f"🔑 Enrolling <b>{escape(label)}</b>. Tap the button, sign in to that account, "
                    "then paste the code the page shows here.",
                    button=("Sign in", url),
                )
                return None
            if command.startswith("/"):
                return HELP
            reply = self.complete_auth(text, accounts, state)
            if reply and message_id:
                self.bot.delete(message_id)  # the code is single-use, but keep chats tidy
            return reply or HELP

    def _telegram_loop(self):
        offset = None
        while True:
            self.telegram_polled_at = time.monotonic()
            try:
                offset = self._telegram_poll(offset)
            except Exception:  # whatever happens, the bot keeps listening
                log.exception("telegram loop failed; retrying")
                time.sleep(15)

    def _telegram_poll(self, offset):
        """One long poll: answer what arrived; returns the next offset."""
        if offset is None:
            with store.app_lock():
                offset = store.load_state()["tg_offset"]
        try:
            updates = self.bot.updates(offset)
        except TelegramError as e:
            log.warning("telegram: %s", e)
            time.sleep(15)
            return offset
        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            if (message.get("chat") or {}).get("id") != self.bot.chat_id:
                log.warning("ignored a message from chat %s", (message.get("chat") or {}).get("id"))
                continue
            try:
                reply = self.handle_message(message.get("text"), message.get("message_id"))
            except Exception as e:  # keep the bot alive whatever one command does
                log.exception("command failed")
                reply = f"⚠️ {escape(type(e).__name__)}: {escape(str(e))[:300]}"
            if reply:
                self.bot.send(reply)
        if updates:
            with store.app_lock():
                state = store.load_state()
                state["tg_offset"] = offset
                store.save_state(state)
        return offset

    # Main loop

    def check_health(self):
        """Exit, for systemd to restart us, when the bot has stopped listening."""
        if not self.bot.configured:
            return
        thread = self._telegram_thread
        stalled = time.monotonic() - self.telegram_polled_at > TELEGRAM_STALL_S
        if thread is None or not thread.is_alive() or stalled:
            log.error("the Telegram bot stopped listening (%s); exiting to be restarted",
                      "stalled" if stalled else "thread gone")
            raise SystemExit(1)

    def tick(self):
        with store.app_lock():
            accounts = store.load_accounts()
            state = store.load_state()
            if state["rc_wanted"]:
                self.follower.ensure()
            if not accounts:
                return
            try:
                self._sync_live(accounts, state)
            except (oauth.HttpError, oauth.LoginExpired, OSError, ValueError, KeyError,
                    claude_files.LockTimeout) as e:
                log.warning("could not check the live login: %s", e)
            for label in accounts:
                if time.monotonic() >= self._next_poll.get(label, 0):
                    self._poll(label, accounts, state)
            self._check_logins(accounts, state)
            self._maybe_switch(accounts, state)
            store.save_state(state)

    def run(self):
        if self.bot.configured:
            self._telegram_thread = threading.Thread(target=self._telegram_loop, name="telegram", daemon=True)
            self._telegram_thread.start()
        else:
            log.warning("Telegram is not configured; running without notifications")
        with store.app_lock():
            accounts = store.load_accounts()
            state = store.load_state()
            active = state["active"]
        self.notify(f"🟢 claude-switcher is running: {len(accounts)} account(s), active "
                    f"<b>{escape(active or 'none')}</b>.", quiet=True)
        sd_notify("READY=1")
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
            self.check_health()
            # Missed for WatchdogSec (a hung tick), systemd restarts the service.
            sd_notify("WATCHDOG=1")
            time.sleep(TICK_S)
