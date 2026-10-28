"""Reading and swapping the login Claude Code runs on, the way Claude Code does.

Claude Code coordinates its own processes with proper-lockfile locks (a
directory created with mkdir, stale once its mtime is older than the holder's
window): `~/.claude/.oauth_refresh.lock` plus the legacy `~/.claude.lock`
around every token refresh, and `~/.claude.json.lock` around every write of
the global config. Taking the same locks means a session refreshing its token
can never write the old account back over a switch.
"""

import json
import os
import random
import tempfile
import time
from contextlib import contextmanager

from . import config

OAUTH_LOCK_STALE_S = 60  # Claude Code's `stale` for the refresh lock
CONFIG_LOCK_STALE_S = 10  # proper-lockfile's default, which the config lock uses


class LockTimeout(Exception):
    pass


@contextmanager
def dir_lock(path, stale_s, timeout_s=20):
    path = os.fspath(path)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            os.mkdir(path)
            break
        except FileExistsError:
            try:
                age = time.time() - os.stat(path).st_mtime
            except FileNotFoundError:
                continue
            if age > stale_s:
                # The holder stopped refreshing its mtime: proper-lockfile
                # treats that as abandoned and removes it.
                try:
                    os.rmdir(path)
                except OSError:
                    pass
                continue
            if time.monotonic() > deadline:
                raise LockTimeout(f"{path} is held by another process")
            time.sleep(0.05 + random.random() * 0.15)
    try:
        yield
    finally:
        try:
            os.rmdir(path)
        except FileNotFoundError:
            pass


@contextmanager
def oauth_locks():
    with dir_lock(config.OAUTH_LOCK, OAUTH_LOCK_STALE_S):
        with dir_lock(config.OAUTH_LEGACY_LOCK, OAUTH_LOCK_STALE_S):
            yield


def _write_atomic(path, text, mode):
    path = os.fspath(path)
    fd, tmp = tempfile.mkstemp(prefix=".switcher-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
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


def read_live_oauth():
    """The `claudeAiOauth` record every session on this machine runs on."""
    try:
        with open(config.CREDENTIALS) as f:
            return (json.load(f) or {}).get("claudeAiOauth")
    except (FileNotFoundError, ValueError):
        return None


def write_live_oauth(oauth):
    """Replace the live login. Call inside `oauth_locks()`.

    Other keys in the file (MCP servers' OAuth tokens) are kept as they are.
    """
    try:
        with open(config.CREDENTIALS) as f:
            data = json.load(f) or {}
    except (FileNotFoundError, ValueError):
        data = {}
    data["claudeAiOauth"] = oauth
    config.CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    _write_atomic(config.CREDENTIALS, json.dumps(data), 0o600)


def read_oauth_account():
    try:
        with open(config.GLOBAL_CONFIG) as f:
            return (json.load(f) or {}).get("oauthAccount")
    except (FileNotFoundError, ValueError):
        return None


def write_oauth_account(oauth_account):
    """Point ~/.claude.json's `oauthAccount` at the account now logged in.

    Remote Control takes its organization from here; the sessions' own
    bootstrap rewrites it from the API later, so only the identity has to be
    right at the moment of the switch.
    """
    target = os.path.realpath(config.GLOBAL_CONFIG)
    with dir_lock(config.GLOBAL_CONFIG_LOCK, CONFIG_LOCK_STALE_S):
        with open(target) as f:
            data = json.load(f)
        data["oauthAccount"] = oauth_account
        mode = os.stat(target).st_mode & 0o777
        _write_atomic(target, json.dumps(data, indent=2, ensure_ascii=False), mode)
