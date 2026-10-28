"""Paths of the files this tool and Claude Code share, and the user's settings."""

import copy
import json
import os
from pathlib import Path

HOME = Path.home()

# Claude Code's own files. Every running session watches CREDENTIALS' mtime and
# drops its cached token when it changes; GLOBAL_CONFIG holds `oauthAccount`,
# which Remote Control reads for the organization it registers under.
CLAUDE_DIR = HOME / ".claude"
CREDENTIALS = CLAUDE_DIR / ".credentials.json"
GLOBAL_CONFIG = HOME / ".claude.json"
SESSIONS_DIR = CLAUDE_DIR / "sessions"

# Locks Claude Code takes (proper-lockfile: a directory, stale once its mtime
# is older than the holder's `stale` window).
OAUTH_LOCK = CLAUDE_DIR / ".oauth_refresh.lock"
OAUTH_LEGACY_LOCK = Path(str(CLAUDE_DIR.resolve()) + ".lock")
GLOBAL_CONFIG_LOCK = Path(str(GLOBAL_CONFIG) + ".lock")

# This tool's files. The rc-keeper mod reads DATA_DIR by this same spelling.
CONFIG_DIR = HOME / ".config" / "claude-switcher"
CONFIG_FILE = CONFIG_DIR / "config.json"
DATA_DIR = HOME / ".local" / "share" / "claude-switcher"
ACCOUNTS_DIR = DATA_DIR / "accounts"
STATE_FILE = DATA_DIR / "state.json"
APP_LOCK = DATA_DIR / ".lock"
RC_SIGNAL = DATA_DIR / "rc-signal.json"
RC_MOD_DIR = DATA_DIR / "rc-mod"

DEFAULTS = {
    "telegram": {"bot_token": "", "chat_id": None},
    # Switch away when the active account's session (5h) or weekly usage
    # reaches this percentage.
    "switch_at_percent": 95,
    # A target must be at or below both of these, so a switch buys real time
    # instead of bouncing again minutes later. When no account qualifies, the
    # best one below switch_at_percent is used.
    "candidate_max_session": 85,
    "candidate_max_weekly": 90,
    "poll_seconds": 300,
    # Poll the active account faster once it is close to the threshold.
    "fast_poll_above_percent": 80,
    "fast_poll_seconds": 60,
    "min_seconds_between_switches": 120,
    # Send a fresh sign-in link this long before a login would expire.
    "reauth_warn_hours": 48,
    "reauth_reminder_hours": 12,
    "remote_control": {
        # How long to keep watching sessions whose reconnect waits for their
        # current turn to end.
        "watch_minutes": 120,
    },
}


def _merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            user = json.load(f)
    except FileNotFoundError:
        user = {}
    return _merge(DEFAULTS, user)


def save_config(cfg):
    from .store import write_json_atomic

    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(CONFIG_DIR, 0o700)
    write_json_atomic(CONFIG_FILE, cfg)
