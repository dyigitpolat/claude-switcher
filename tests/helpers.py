"""A throwaway home for each test: every path the package uses points into it."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from claude_switcher import config, store


def oauth_record(name, expires_in_s=8 * 3600, refresh_expires_in_s=None):
    now = int(time.time() * 1000)
    rec = {
        "accessToken": f"access-{name}",
        "refreshToken": f"refresh-{name}",
        "expiresAt": now + expires_in_s * 1000,
        "scopes": ["user:inference", "user:profile"],
        "subscriptionType": "max",
        "rateLimitTier": "default_claude_max_20x",
    }
    if refresh_expires_in_s is not None:
        rec["refreshTokenExpiresAt"] = now + refresh_expires_in_s * 1000
    return rec


def profile(label):
    return {
        "account": {"uuid": f"uuid-{label}", "email": f"{label}@example.com", "display_name": label},
        "organization": {"uuid": f"org-{label}", "name": f"{label} org", "organization_type": "claude_max",
                         "rate_limit_tier": "default_claude_max_20x"},
    }


def usage_payload(session, weekly, session_resets="2026-10-28T12:59:59+00:00",
                  weekly_resets="2026-11-02T23:59:59+00:00"):
    return {
        "five_hour": {"utilization": session, "resets_at": session_resets, "locked_reason": None},
        "seven_day": {"utilization": weekly, "resets_at": weekly_resets, "locked_reason": None},
        "limits": [],
    }


class TempHome(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = self.root = Path(tmp.name)
        claude = root / ".claude"
        data = root / ".local/share/claude-switcher"
        paths = {
            "CLAUDE_DIR": claude,
            "CREDENTIALS": claude / ".credentials.json",
            "GLOBAL_CONFIG": root / ".claude.json",
            "SESSIONS_DIR": claude / "sessions",
            "OAUTH_LOCK": claude / ".oauth_refresh.lock",
            "OAUTH_LEGACY_LOCK": root / ".claude.lock",
            "GLOBAL_CONFIG_LOCK": root / ".claude.json.lock",
            "CONFIG_DIR": root / ".config/claude-switcher",
            "CONFIG_FILE": root / ".config/claude-switcher/config.json",
            "DATA_DIR": data,
            "ACCOUNTS_DIR": data / "accounts",
            "STATE_FILE": data / "state.json",
            "APP_LOCK": data / ".lock",
            "RC_SIGNAL": data / "rc-signal.json",
            "RC_MOD_DIR": data / "rc-mod",
        }
        for name, value in paths.items():
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(store, "_lock", store._AppLock())
        patcher.start()
        self.addCleanup(patcher.stop)
        (claude / "sessions").mkdir(parents=True)

    def write_live(self, oauth, extra=None):
        data = {"claudeAiOauth": oauth, **(extra or {})}
        config.CREDENTIALS.write_text(json.dumps(data))
        os.chmod(config.CREDENTIALS, 0o600)

    def live(self):
        return json.loads(config.CREDENTIALS.read_text())

    def write_global_config(self, oauth_account, extra=None):
        data = {"numStartups": 7, "projects": {"/x": {"allowedTools": []}}, "oauthAccount": oauth_account,
                **(extra or {})}
        config.GLOBAL_CONFIG.write_text(json.dumps(data, indent=2))
        os.chmod(config.GLOBAL_CONFIG, 0o600)

    def global_config(self):
        return json.loads(config.GLOBAL_CONFIG.read_text())

    def enroll(self, label, oauth=None):
        prof = profile(label)
        acct = {
            "label": label,
            "email": prof["account"]["email"],
            "account_uuid": prof["account"]["uuid"],
            "org_uuid": prof["organization"]["uuid"],
            "oauth": oauth or oauth_record(label),
            "oauth_account": {"accountUuid": prof["account"]["uuid"],
                              "emailAddress": prof["account"]["email"],
                              "organizationUuid": prof["organization"]["uuid"]},
        }
        store.save_account(acct)
        return acct
