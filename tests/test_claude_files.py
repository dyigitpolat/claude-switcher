import os
import stat
import threading
import time
import unittest

from claude_switcher import claude_files, config
from helpers import TempHome, oauth_record


class DirLock(TempHome):
    def test_held_until_released_then_removed(self):
        path = self.root / "x.lock"
        with claude_files.dir_lock(path, stale_s=60):
            self.assertTrue(path.is_dir())
            with self.assertRaises(claude_files.LockTimeout):
                with claude_files.dir_lock(path, stale_s=60, timeout_s=0.3):
                    pass
        self.assertFalse(path.exists())

    def test_waits_for_a_live_holder(self):
        path = self.root / "x.lock"
        os.mkdir(path)
        threading.Timer(0.3, os.rmdir, [path]).start()
        started = time.monotonic()
        with claude_files.dir_lock(path, stale_s=60, timeout_s=5):
            self.assertGreater(time.monotonic() - started, 0.25)

    def test_takes_over_a_stale_lock(self):
        path = self.root / "x.lock"
        os.mkdir(path)
        old = time.time() - 120
        os.utime(path, (old, old))
        with claude_files.dir_lock(path, stale_s=60, timeout_s=1):
            self.assertTrue(path.is_dir())


class LiveLogin(TempHome):
    def test_swapping_keeps_other_credentials_and_stays_private(self):
        config.CLAUDE_DIR.mkdir(exist_ok=True)
        self.write_live(oauth_record("a"), extra={"mcpOAuth": {"server": {"accessToken": "keep-me"}}})
        before = os.stat(config.CREDENTIALS).st_ino
        with claude_files.oauth_locks():
            claude_files.write_live_oauth(oauth_record("b"))
        data = self.live()
        self.assertEqual(data["claudeAiOauth"]["accessToken"], "access-b")
        self.assertEqual(data["mcpOAuth"], {"server": {"accessToken": "keep-me"}})
        info = os.stat(config.CREDENTIALS)
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertNotEqual(info.st_ino, before)  # replaced whole, never half-written
        self.assertFalse(config.OAUTH_LOCK.exists())
        self.assertFalse(config.OAUTH_LEGACY_LOCK.exists())

    def test_the_global_config_changes_only_its_account(self):
        self.write_global_config({"accountUuid": "uuid-a"}, extra={"name": "Zoë Ångström"})
        claude_files.write_oauth_account({"accountUuid": "uuid-b", "organizationUuid": "org-b"})
        data = self.global_config()
        self.assertEqual(data["oauthAccount"], {"accountUuid": "uuid-b", "organizationUuid": "org-b"})
        self.assertEqual(data["projects"], {"/x": {"allowedTools": []}})
        self.assertEqual(data["name"], "Zoë Ångström")
        self.assertIn("Zoë Ångström", config.GLOBAL_CONFIG.read_text())
        self.assertEqual(stat.S_IMODE(os.stat(config.GLOBAL_CONFIG).st_mode), 0o600)
        self.assertFalse(config.GLOBAL_CONFIG_LOCK.exists())

    def test_the_global_config_waits_for_claude_codes_lock(self):
        self.write_global_config({"accountUuid": "uuid-a"})
        os.mkdir(config.GLOBAL_CONFIG_LOCK)
        threading.Timer(0.3, os.rmdir, [config.GLOBAL_CONFIG_LOCK]).start()
        claude_files.write_oauth_account({"accountUuid": "uuid-b"})
        self.assertEqual(self.global_config()["oauthAccount"]["accountUuid"], "uuid-b")


if __name__ == "__main__":
    unittest.main()
