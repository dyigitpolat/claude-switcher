"""This tool's own records: one file per account, plus a small state file.

The account files hold OAuth tokens, so everything here is written 0600 under
a 0700 directory. The daemon and the CLI both mutate these files; `app_lock()`
serialises them (an flock across processes, a re-entrant lock across threads).
"""

import fcntl
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager

from . import config

LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def write_json_atomic(path, obj, mode=0o600):
    path = os.fspath(path)
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def ensure_dirs():
    for d in (config.DATA_DIR, config.ACCOUNTS_DIR, config.RC_MOD_DIR):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(config.DATA_DIR, 0o700)
    os.chmod(config.ACCOUNTS_DIR, 0o700)
    # The mod writes heartbeats here from inside Claude Code.
    os.chmod(config.RC_MOD_DIR, 0o700)


class _AppLock:
    def __init__(self):
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._fd = None

    @contextmanager
    def hold(self):
        with self._thread_lock:
            if self._depth == 0:
                ensure_dirs()
                if self._fd is None:
                    self._fd = os.open(config.APP_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(self._fd, fcntl.LOCK_EX)
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)


_lock = _AppLock()


def app_lock():
    return _lock.hold()


def check_label(label):
    if not LABEL_RE.match(label or ""):
        raise ValueError(
            "a label is 1-32 characters: lowercase letters, digits, '-' or '_'"
        )
    return label


def account_path(label):
    return config.ACCOUNTS_DIR / f"{check_label(label)}.json"


def load_accounts():
    """Every enrolled account, keyed by label, in label order."""
    ensure_dirs()
    out = {}
    for path in sorted(config.ACCOUNTS_DIR.glob("*.json")):
        acct = read_json(path)
        if acct and acct.get("label") == path.stem:
            out[path.stem] = acct
    return out


def save_account(acct):
    ensure_dirs()
    write_json_atomic(account_path(acct["label"]), acct)


def delete_account(label):
    account_path(label).unlink(missing_ok=True)


def find_by_uuid(accounts, account_uuid):
    for label, acct in accounts.items():
        if acct.get("account_uuid") == account_uuid:
            return label
    return None


def load_state():
    ensure_dirs()
    state = read_json(config.STATE_FILE, {}) or {}
    state.setdefault("active", None)
    state.setdefault("auto", True)
    state.setdefault("last_switch_at", 0)
    state.setdefault("accounts", {})
    state.setdefault("pending_auth", {})
    state.setdefault("alerts", {})
    state.setdefault("rc_wanted", {})
    state.setdefault("tg_offset", 0)
    return state


def save_state(state):
    ensure_dirs()
    write_json_atomic(config.STATE_FILE, state)


def account_state(state, label):
    return state["accounts"].setdefault(label, {"auth": "ok"})
