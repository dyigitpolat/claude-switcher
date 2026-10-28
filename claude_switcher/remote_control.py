"""Bringing Remote Control back after a switch.

After an account change a session's Remote Control bridge either drops (its
own revive only handles a refreshed token of the same account) or lingers,
still bound to the old account. So every session that had it on is cycled,
off and then on, by the rc-keeper mod inside it, through Claude Code's plugin
API, once the session is idle. Nothing is typed into any terminal.

rc-keeper writes a heartbeat per session, so we know where it is loaded; the
sessions it should cycle are listed in its signal file, with the person's
"Remote Control for all sessions" setting, which its "off" step borrows and
puts back. A session without it (started before the mod was installed) is
reported over Telegram, and picks it up with `/reload-plugins`.

A session is done once its registry entry shows a bridge other than the one
it had at the switch: a new Remote Control session, under the new account.
"""

import os
import threading
import time
from dataclasses import dataclass
from html import escape

from . import config, store

MOD_HEARTBEAT_MAX_AGE_S = 90
# rc-keeper's heartbeat `v`. Before 2 it only reconnected a dropped bridge, never
# cycled one; it still gets the signal, but the person is told to reload it.
MOD_VERSION = 2
SUMMARY_AFTER_S = 75
HANDLED_BY_MOD = ("mod", "outdated")


@dataclass
class Session:
    pid: int
    session_id: str
    name: str
    cwd: str
    bridge: str | None
    status: str | None
    version: str | None

    @property
    def url(self):
        return f"https://claude.ai/code/{self.bridge}" if self.bridge else None

    @property
    def title(self):
        return self.name or os.path.basename(self.cwd) or self.session_id[:8]


def _proc_start(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
    except OSError:
        return None
    # Field 22 (start time); counting resumes at field 3 after the comm's ')'.
    return stat.rsplit(")", 1)[1].split()[19]


def live_sessions():
    """This user's running Claude Code sessions, from Claude Code's registry."""
    out = []
    uid = os.getuid()
    for path in config.SESSIONS_DIR.glob("*.json"):
        try:
            rec = store.read_json(path) or {}
        except ValueError:
            continue  # caught mid-write
        pid = rec.get("pid")
        if not isinstance(pid, int) or not rec.get("sessionId"):
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
        except OSError:
            continue
        if str(rec.get("procStart")) != _proc_start(pid):
            continue  # the pid now belongs to another process
        out.append(
            Session(
                pid=pid,
                session_id=rec["sessionId"],
                name=rec.get("name") or "",
                cwd=rec.get("cwd") or "",
                bridge=rec.get("bridgeSessionId"),
                status=rec.get("status"),
                version=rec.get("version"),
            )
        )
    return sorted(out, key=lambda s: s.pid)


def mod_beat(session_id):
    try:
        return store.read_json(config.RC_MOD_DIR / f"{session_id}.json")
    except ValueError:
        return None


def mod_alive(session_id):
    beat = mod_beat(session_id)
    if not beat:
        return False
    return time.time() * 1000 - beat.get("at", 0) < MOD_HEARTBEAT_MAX_AGE_S * 1000


def owner_of(session_id):
    if not mod_alive(session_id):
        return "manual"
    return "mod" if mod_beat(session_id).get("v", 1) >= MOD_VERSION else "outdated"


def setting_value():
    """The person's "Remote Control for all sessions" row: 'true', 'false' or 'default'.

    Claude Code keeps it in user settings, or in ~/.claude.json from older versions.
    """
    for path in (config.CLAUDE_DIR / "settings.json", config.GLOBAL_CONFIG):
        try:
            value = (store.read_json(path) or {}).get("remoteControlAtStartup")
        except ValueError:
            continue
        if isinstance(value, bool):
            return "true" if value else "false"
    return "default"


def restore_value(now_ms):
    """The value rc-keeper puts back after borrowing the row.

    Read at the switch; a session still mid-cycle from the switch before may
    hold the row at 'false' for a moment, so that switch's value wins then.
    """
    value = setting_value()
    try:
        previous = store.read_json(config.RC_SIGNAL) or {}
    except ValueError:
        previous = {}
    if value == "false" and previous.get("restore") == "true" and now_ms < previous.get("expiresAt", 0):
        return "true"
    return value


def write_signal(wanted, ttl_s):
    """List the sessions rc-keeper should cycle, under the latest switch."""
    epoch = max((e["since"] for e in wanted.values()), default=0)
    restore = next((e["restore"] for e in wanted.values() if e.get("restore")), "default")
    store.write_json_atomic(
        config.RC_SIGNAL,
        {
            "epoch": epoch,
            "sessions": sorted(sid for sid, e in wanted.items() if e.get("owner") in HANDLED_BY_MOD),
            "expiresAt": epoch + ttl_s * 1000,
            "restore": restore,
        },
    )


class Follower:
    """Watches the sessions recorded in `state["rc_wanted"]` until each is back.

    One per process; `ensure()` (re)starts its thread when there is work.
    """

    def __init__(self, notify, cfg):
        self._notify = notify
        self._cfg = cfg["remote_control"]
        self._thread = None
        self._status = {}
        self._summarized = set()

    def ensure(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="rc-follower", daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            with store.app_lock():
                state = store.load_state()
                wanted = state["rc_wanted"]
                if not wanted:
                    return
                live = {s.session_id: s for s in live_sessions()}
                before = {sid: dict(e) for sid, e in wanted.items()}
                finished = {}
                for sid, entry in wanted.items():
                    outcome = self.step(entry, live.get(sid))
                    if outcome:
                        finished[sid] = outcome
                for sid in finished:
                    del wanted[sid]
                if wanted != before or finished:
                    write_signal(wanted, self._cfg["watch_minutes"] * 60)
                    store.save_state(state)
                snapshot = {**before, **wanted}
            self._report(snapshot, live, finished)
            time.sleep(5)

    def step(self, entry, session):
        """Advance one session (mutating its entry); its final outcome, once it has one."""
        sid = entry["session_id"]
        age = time.time() - entry["since"] / 1000
        if session is None:
            return "gone"
        if session.bridge and session.bridge != entry.get("bridge"):
            return "back"
        if age > self._cfg["watch_minutes"] * 60:
            return "expired"
        if entry["owner"] != "mod":
            entry["owner"] = owner_of(sid)  # rc-keeper loaded or updated since (/reload-plugins)
        if entry["owner"] == "mod":
            beat = mod_beat(sid) or {}
            if beat.get("handled") == entry["since"] and beat.get("result") in ("off-timeout", "off-denied"):
                return "failed"
        self._status[sid] = entry["owner"]
        return None

    def _report(self, wanted, live, finished):
        now = time.time()
        batches = {}
        for sid, entry in wanted.items():
            batches.setdefault(entry["since"], []).append(sid)
        for since, sids in batches.items():
            if since in self._summarized:
                for sid in sids:
                    if sid in finished:
                        self._notify(self._late_line(wanted[sid], live.get(sid), finished[sid]))
                continue
            if now - since / 1000 < SUMMARY_AFTER_S and not all(sid in finished for sid in sids):
                continue
            self._summarized.add(since)
            account = wanted[sids[0]].get("account", "?")
            lines = [f"📡 <b>Remote Control</b> after the switch to <b>{escape(account)}</b>:"]
            for sid in sids:
                lines.append(self._line(wanted[sid], live.get(sid), finished.get(sid), self._status.get(sid)))
            self._notify("\n".join(lines))

    @staticmethod
    def _name(entry, session):
        return escape(session.title if session else entry.get("name") or "session")

    BY_HAND = "turn Remote Control off and on in it by hand (/remote-control → Disconnect, then /remote-control)"

    def _line(self, entry, session, outcome, status):
        name = self._name(entry, session)
        if outcome in ("back", "gone", "expired", "failed"):
            return self._late_line(entry, session, outcome).removeprefix("📡 ")
        if status == "mod":
            return f"⏳ {name}: rc-keeper turns it off and on once the session is idle"
        if status == "outdated":
            return (f"⚠️ {name}: an older rc-keeper is loaded there, which reconnects a dropped bridge but "
                    "does not cycle one still up. Run /reload-plugins in it to update")
        return (f"✋ {name}: rc-keeper is not loaded there. Run /reload-plugins in it "
                f"(it then cycles Remote Control by itself), or {self.BY_HAND}")

    def _late_line(self, entry, session, outcome):
        name = self._name(entry, session)
        if outcome == "back":
            return f"📡 ✅ {name}: Remote Control reconnected, <a href=\"{session.url}\">open</a>"
        if outcome == "failed":
            return f"📡 ✋ {name}: rc-keeper could not turn Remote Control off; {self.BY_HAND}"
        if outcome == "expired":
            return f"📡 ✋ {name}: Remote Control was not cycled in time; {self.BY_HAND}"
        return f"📡 ➖ {name}: the session has exited"
