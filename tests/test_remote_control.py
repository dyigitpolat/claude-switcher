import json
import os
import time
import unittest

from claude_switcher import config, store
from claude_switcher import remote_control as rc
from helpers import TempHome


class Registry(TempHome):
    def register(self, pid, proc_start, bridge=None):
        rec = {"pid": pid, "sessionId": f"sid-{pid}", "procStart": proc_start, "cwd": "/w/proj",
               "name": f"name-{pid}", "status": "busy", "kind": "interactive",
               "bridgeSessionId": bridge}
        (config.SESSIONS_DIR / f"{pid}.json").write_text(json.dumps(rec))

    def test_only_live_sessions_whose_pid_was_not_reused(self):
        me = os.getpid()
        self.register(me, rc._proc_start(me), bridge="session_01abc")
        self.register(me + 1_000_000, "1")  # no such process
        (config.SESSIONS_DIR / "torn.json").write_text("{")
        sessions = rc.live_sessions()
        self.assertEqual([s.pid for s in sessions], [me])
        self.assertEqual(sessions[0].url, "https://claude.ai/code/session_01abc")
        self.register(me, "12345")  # same pid, another start time: a reused pid
        self.assertEqual(rc.live_sessions(), [])


def session(bridge=None):
    return rc.Session(pid=1, session_id="s1", name="proj", cwd="/w/proj", bridge=bridge,
                      status="busy", version="2.1.312")


class FollowerStep(TempHome):
    CFG = {"remote_control": {"watch_minutes": 120}}

    def setUp(self):
        super().setUp()
        store.ensure_dirs()
        self.follower = rc.Follower(lambda html: None, self.CFG)

    def entry(self, owner, age_s=10):
        return {"session_id": "s1", "bridge": "session_old", "name": "proj", "owner": owner,
                "since": int((time.time() - age_s) * 1000), "account": "b"}

    def beat(self, age_s=0, v=rc.MOD_VERSION):
        at = int((time.time() - age_s) * 1000)
        (config.RC_MOD_DIR / "s1.json").write_text(json.dumps({"at": at, "v": v, "handled": 0}))

    def test_a_new_bridge_means_it_is_back(self):
        self.assertEqual(self.follower.step(self.entry("mod"), session(bridge="session_new")), "back")

    def test_the_old_bridge_still_up_needs_nothing_yet(self):
        self.assertIsNone(self.follower.step(self.entry("mod"), session(bridge="session_old")))

    def test_rc_keeper_owns_sessions_where_it_is_loaded(self):
        self.assertEqual(rc.owner_of("s1"), "manual")
        self.beat(age_s=rc.MOD_HEARTBEAT_MAX_AGE_S + 5)  # a session that has since exited
        self.assertEqual(rc.owner_of("s1"), "manual")
        self.beat()
        self.assertEqual(rc.owner_of("s1"), "mod")
        self.beat(v=1)  # loaded before the off-and-on cycle existed
        self.assertEqual(rc.owner_of("s1"), "outdated")
        (config.RC_MOD_DIR / "s1.json").write_text(json.dumps({"at": int(time.time() * 1000), "handled": 0}))
        self.assertEqual(rc.owner_of("s1"), "outdated")

    def test_an_outdated_rc_keeper_is_still_signalled_and_upgraded_once_reloaded(self):
        self.beat(v=1)
        entry = self.entry("outdated")
        self.follower.step(entry, session())
        rc.write_signal({"s1": entry}, 600)
        self.assertEqual(json.loads(config.RC_SIGNAL.read_text())["sessions"], ["s1"])
        self.beat()
        self.follower.step(entry, session())
        self.assertEqual(entry["owner"], "mod")

    def test_a_session_that_loads_rc_keeper_later_is_handed_to_it(self):
        entry = self.entry("manual")
        self.follower.step(entry, session())
        self.assertEqual(entry["owner"], "manual")
        self.beat()  # /reload-plugins in that session
        self.follower.step(entry, session())
        self.assertEqual(entry["owner"], "mod")

    def test_endings(self):
        self.assertEqual(self.follower.step(self.entry("mod"), None), "gone")
        old = 121 * 60
        self.assertEqual(self.follower.step(self.entry("mod", age_s=old), session()), "expired")
        # Still on the bridge it had at the switch: never cycled.
        self.assertEqual(self.follower.step(self.entry("mod", age_s=old), session(bridge="session_old")), "expired")

    def test_rc_keeper_reporting_it_could_not_turn_off_ends_the_watch(self):
        entry = self.entry("mod")
        beat = {"at": int(time.time() * 1000), "handled": entry["since"], "result": "off-timeout"}
        (config.RC_MOD_DIR / "s1.json").write_text(json.dumps(beat))
        self.assertEqual(self.follower.step(entry, session(bridge="session_old")), "failed")
        beat["handled"] = entry["since"] - 1  # a result from an earlier switch
        (config.RC_MOD_DIR / "s1.json").write_text(json.dumps(beat))
        self.assertIsNone(self.follower.step(entry, session(bridge="session_old")))

    def test_the_signal_lists_only_what_rc_keeper_owns(self):
        now = int(time.time() * 1000)
        rc.write_signal({"s1": {"owner": "mod", "since": now, "restore": "true"},
                         "s2": {"owner": "manual", "since": now, "restore": "true"}}, 600)
        signal = json.loads(config.RC_SIGNAL.read_text())
        self.assertEqual(signal["sessions"], ["s1"])
        self.assertEqual(signal["epoch"], now)
        self.assertEqual(signal["expiresAt"], now + 600_000)
        self.assertEqual(signal["restore"], "true")

    def test_one_summary_then_a_line_per_late_reconnect(self):
        sent = []
        follower = rc.Follower(sent.append, self.CFG)
        entry = self.entry("mod", age_s=rc.SUMMARY_AFTER_S + 1)
        wanted = {"s1": entry}
        follower.step(entry, session())
        follower._report(wanted, {"s1": session()}, {})
        follower._report(wanted, {"s1": session()}, {})
        self.assertEqual(len(sent), 1)
        self.assertIn("rc-keeper turns it off and on once the session is idle", sent[0])
        back = session(bridge="session_new")
        follower._report(wanted, {"s1": back}, {"s1": "back"})
        self.assertEqual(len(sent), 2)
        self.assertIn("https://claude.ai/code/session_new", sent[1])


class Setting(TempHome):
    def test_user_settings_then_the_older_global_config_then_default(self):
        self.assertEqual(rc.setting_value(), "default")
        self.write_global_config({}, extra={"remoteControlAtStartup": True})
        self.assertEqual(rc.setting_value(), "true")
        (config.CLAUDE_DIR / "settings.json").write_text(json.dumps({"remoteControlAtStartup": False}))
        self.assertEqual(rc.setting_value(), "false")
        (config.CLAUDE_DIR / "settings.json").write_text(json.dumps({"remoteControlAtStartup": None}))
        self.assertEqual(rc.setting_value(), "true")

    def test_a_false_borrowed_mid_cycle_is_not_taken_for_the_persons_value(self):
        store.ensure_dirs()
        now = int(time.time() * 1000)
        (config.CLAUDE_DIR / "settings.json").write_text(json.dumps({"remoteControlAtStartup": False}))
        self.assertEqual(rc.restore_value(now), "false")
        config.RC_SIGNAL.write_text(json.dumps({"restore": "true", "expiresAt": now + 60_000}))
        self.assertEqual(rc.restore_value(now), "true")
        self.assertEqual(rc.restore_value(now + 120_000), "false")  # that cycle is long over


if __name__ == "__main__":
    unittest.main()
