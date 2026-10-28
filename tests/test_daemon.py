import json
import os
import unittest
from unittest import mock

from claude_switcher import config, oauth, store
from claude_switcher import remote_control as rc
from claude_switcher.daemon import HELP, Switcher
from helpers import TempHome, oauth_record, profile, usage_payload


def label_of(token):
    return token.split("-")[1]


class SwitcherTest(TempHome):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.sw = Switcher(config.load_config())
        self.sw.bot = mock.Mock()
        self.sw.bot.send.side_effect = lambda html, button=None, quiet=False: self.sent.append((html, button)) or 1
        self.sw.follower = mock.Mock()
        for label in "abc":
            self.enroll(label)
        self.write_live(oauth_record("a"))
        self.write_global_config({"accountUuid": "uuid-a"})
        state = store.load_state()
        state["active"] = "a"
        store.save_state(state)
        self.usage = {"a": usage_payload(50, 30), "b": usage_payload(10, 50), "c": usage_payload(30, 20)}
        for name, fake in {
            "fetch_usage": lambda token: self.usage[label_of(token)],
            "fetch_profile": lambda token: profile(label_of(token)),
            "refresh": mock.Mock(side_effect=AssertionError("unexpected refresh")),
        }.items():
            patcher = mock.patch.object(oauth, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def state(self):
        return store.load_state()

    def texts(self):
        return "\n".join(html for html, _ in self.sent)


class AutomaticSwitch(SwitcherTest):
    def test_stays_put_below_the_threshold(self):
        self.sw.tick()
        self.assertEqual(self.state()["active"], "a")
        self.assertEqual(self.live()["claudeAiOauth"]["accessToken"], "access-a")
        self.assertEqual(self.state()["accounts"]["b"]["usage"]["session"], 10)

    def test_at_the_threshold_every_session_moves_to_the_roomiest_account(self):
        self.usage["a"] = usage_payload(96, 30)
        # A session rotated a's tokens since the last look; they must not be lost.
        rotated = {**oauth_record("a"), "accessToken": "access-a-2", "refreshToken": "refresh-a-2"}
        self.write_live(rotated, extra={"mcpOAuth": {"k": "v"}})
        self.sw.tick()

        state = self.state()
        self.assertEqual(state["active"], "c")  # c: 70% headroom, b: 50%
        self.assertEqual(self.live()["claudeAiOauth"]["accessToken"], "access-c")
        self.assertEqual(self.live()["mcpOAuth"], {"k": "v"})
        self.assertEqual(self.global_config()["oauthAccount"]["accountUuid"], "uuid-c")
        self.assertEqual(store.load_accounts()["a"]["oauth"]["refreshToken"], "refresh-a-2")
        self.assertIn("Switched to c", self.texts())
        self.assertIn("session usage at 96%", self.texts())

    def test_tokens_a_session_rotates_mid_cycle_are_handed_back(self):
        self.usage["a"] = usage_payload(96, 30)
        polled = oauth.fetch_usage

        def rotate_while_polling(token):
            # A session refreshes a's login after this cycle's first look.
            if label_of(token) == "c":
                self.write_live({**oauth_record("a"), "accessToken": "access-a-9", "refreshToken": "refresh-a-9"})
            return polled(token)

        with mock.patch.object(oauth, "fetch_usage", rotate_while_polling):
            self.sw.tick()
        self.assertEqual(self.state()["active"], "c")
        self.assertEqual(store.load_accounts()["a"]["oauth"]["refreshToken"], "refresh-a-9")

    def test_weekly_usage_triggers_a_switch_too(self):
        self.usage["a"] = usage_payload(10, 97)
        self.sw.tick()
        self.assertEqual(self.state()["active"], "c")

    def test_no_room_anywhere_alerts_once_and_stays(self):
        self.usage = {"a": usage_payload(96, 30), "b": usage_payload(99, 50), "c": usage_payload(10, 100)}
        self.sw.tick()
        self.sw._next_poll.clear()
        self.sw.tick()
        self.assertEqual(self.state()["active"], "a")
        self.assertEqual(self.texts().count("no other account has room"), 1)

    def test_paused_means_no_switch(self):
        state = self.state()
        state["auto"] = False
        store.save_state(state)
        self.usage["a"] = usage_payload(99, 30)
        self.sw.tick()
        self.assertEqual(self.state()["active"], "a")

    def test_a_target_whose_login_died_is_skipped_for_the_next(self):
        # Good enough to poll with, too short-lived to hand to sessions: the
        # switch refreshes it, the refresh is refused, b is next in line.
        acct = store.load_accounts()["c"]
        acct["oauth"]["expiresAt"] = oauth.now_ms() + 20 * 60 * 1000
        store.save_account(acct)
        oauth.refresh = mock.Mock(side_effect=oauth.LoginExpired("invalid_grant"))
        self.usage["a"] = usage_payload(96, 30)
        self.sw.tick()
        state = self.state()
        self.assertEqual(state["active"], "b")
        self.assertEqual(state["accounts"]["c"]["auth"], "expired")
        self.assertEqual(self.live()["claudeAiOauth"]["accessToken"], "access-b")
        oauth.refresh.assert_called_once_with("refresh-c")

    def test_sessions_with_remote_control_are_handed_to_rc_keeper(self):
        me = os.getpid()
        (config.SESSIONS_DIR / f"{me}.json").write_text(json.dumps({
            "pid": me, "sessionId": "sid-1", "procStart": rc._proc_start(me), "cwd": "/w",
            "bridgeSessionId": "session_01old", "name": "proj",
        }))
        store.ensure_dirs()
        (config.RC_MOD_DIR / "sid-1.json").write_text(json.dumps({"at": oauth.now_ms(), "v": rc.MOD_VERSION,
                                                                  "handled": 0}))
        self.usage["a"] = usage_payload(96, 30)
        self.sw.tick()
        state = self.state()
        wanted = state["rc_wanted"]
        self.assertEqual(wanted["sid-1"]["owner"], "mod")
        self.assertEqual(wanted["sid-1"]["bridge"], "session_01old")
        signal = json.loads(config.RC_SIGNAL.read_text())
        self.assertEqual(signal["sessions"], ["sid-1"])
        self.assertEqual(signal["epoch"], state["last_switch_at"])
        self.assertEqual(signal["restore"], "default")
        self.sw.follower.ensure.assert_called()


class Tokens(SwitcherTest):
    def test_an_inactive_account_is_refreshed_by_us_alone(self):
        acct = store.load_accounts()["b"]
        acct["oauth"]["expiresAt"] = 0
        store.save_account(acct)
        oauth.refresh = mock.Mock(return_value={"access_token": "access-b-2", "refresh_token": "refresh-b-2",
                                                "expires_in": 28800})
        self.sw.tick()
        self.assertEqual(store.load_accounts()["b"]["oauth"]["refreshToken"], "refresh-b-2")
        self.assertEqual(self.live()["claudeAiOauth"]["accessToken"], "access-a")

    def test_the_live_account_is_refreshed_in_place_when_no_session_did(self):
        self.write_live({**oauth_record("a"), "expiresAt": 0})
        acct = store.load_accounts()["a"]
        acct["oauth"]["expiresAt"] = 0
        store.save_account(acct)
        oauth.refresh = mock.Mock(return_value={"access_token": "access-a-2", "refresh_token": "refresh-a-2",
                                                "expires_in": 28800})
        self.sw.tick()
        self.assertEqual(self.live()["claudeAiOauth"]["refreshToken"], "refresh-a-2")
        self.assertEqual(store.load_accounts()["a"]["oauth"]["refreshToken"], "refresh-a-2")

    def test_a_dead_login_gets_a_sign_in_link(self):
        acct = store.load_accounts()["b"]
        acct["oauth"]["expiresAt"] = 0
        store.save_account(acct)
        oauth.refresh = mock.Mock(side_effect=oauth.LoginExpired("invalid_grant"))
        self.sw.tick()
        state = self.state()
        self.assertEqual(state["accounts"]["b"]["auth"], "expired")
        html, button = next((h, b) for h, b in self.sent if b)
        self.assertIn("login for <b>b</b> has expired", html)
        self.assertTrue(button[1].startswith(oauth.AUTHORIZE_URL))
        self.assertIn("login_hint=b%40example.com", button[1])
        self.assertEqual([f["label"] for f in state["pending_auth"].values()], ["b"])

    def test_an_expiring_login_is_renewed_ahead_of_time(self):
        acct = store.load_accounts()["c"]
        acct["oauth"]["refreshTokenExpiresAt"] = oauth.now_ms() + 3600_000
        store.save_account(acct)
        self.sw.tick()
        self.assertIn("login for <b>c</b> expires", self.texts())

    def test_the_active_login_dying_moves_everyone_off_it(self):
        self.write_live({**oauth_record("a"), "expiresAt": 0})
        acct = store.load_accounts()["a"]
        acct["oauth"]["expiresAt"] = 0
        store.save_account(acct)
        oauth.refresh = mock.Mock(side_effect=oauth.LoginExpired("invalid_grant"))
        self.sw.tick()
        self.assertEqual(self.state()["active"], "c")
        self.assertIn("its login expired", self.texts())


class SignIn(SwitcherTest):
    def paste_for(self, label, signed_in_as):
        state = self.state()
        self.sw.start_auth(label, "reauth", state)
        flow_state = next(iter(self.state()["pending_auth"]))
        oauth.exchange_code = mock.Mock(return_value={
            "access_token": f"access-{signed_in_as}-new", "refresh_token": f"refresh-{signed_in_as}-new",
            "expires_in": 28800, "refresh_token_expires_in": 30 * 86400,
        })
        self.addCleanup(setattr, oauth, "exchange_code", oauth.exchange_code)
        return self.sw.handle_message(f"some-long-authorization-code#{flow_state}", message_id=5)

    def test_the_pasted_code_finishes_the_sign_in(self):
        reply = self.paste_for("b", "b")
        self.assertTrue(reply.startswith("✅ <b>b</b>"))
        acct = store.load_accounts()["b"]
        self.assertEqual(acct["oauth"]["refreshToken"], "refresh-b-new")
        self.assertIn("refreshTokenExpiresAt", acct["oauth"])
        self.assertEqual(self.state()["pending_auth"], {})
        self.sw.bot.delete.assert_called_once_with(5)

    def test_signing_in_to_the_wrong_account_saves_nothing(self):
        reply = self.paste_for("b", "stranger")
        self.assertIn("this link was for <b>b</b>", reply)
        self.assertEqual(store.load_accounts()["b"]["oauth"]["refreshToken"], "refresh-b")

    def test_renewing_the_live_account_updates_the_live_login(self):
        self.paste_for("a", "a")
        self.assertEqual(self.live()["claudeAiOauth"]["refreshToken"], "refresh-a-new")

    def test_chatter_is_not_mistaken_for_a_code(self):
        self.assertEqual(self.sw.handle_message("hello there"), HELP)
        oauth.exchange_code = mock.Mock()
        self.addCleanup(setattr, oauth, "exchange_code", oauth.exchange_code)
        self.sw.start_auth("b", "reauth", self.state())
        self.assertEqual(self.sw.handle_message("thanks!"), HELP)
        oauth.exchange_code.assert_not_called()


class ManualLogin(SwitcherTest):
    def test_a_manual_login_to_another_enrolled_account_is_followed(self):
        self.write_live({**oauth_record("b"), "accessToken": "access-b-manual", "refreshToken": "refresh-b-manual"})
        self.sw.tick()
        self.assertEqual(self.state()["active"], "b")
        self.assertEqual(store.load_accounts()["b"]["oauth"]["refreshToken"], "refresh-b-manual")
        self.assertIn("logged in by hand as <b>b</b>", self.texts())

    def test_an_unknown_manual_login_is_enrolled(self):
        self.write_live({**oauth_record("zed"), "accessToken": "access-zed"})
        self.usage["zed"] = usage_payload(1, 1)
        self.sw.tick()
        self.assertEqual(self.state()["active"], "zed")
        self.assertIn("zed", store.load_accounts())


class Commands(SwitcherTest):
    def test_pause_resume_status_and_switch(self):
        self.assertIn("paused", self.sw.handle_message("/pause"))
        self.assertFalse(self.state()["auto"])
        self.assertIn("resumed", self.sw.handle_message("/resume@my_bot"))
        self.sw.tick()
        status = self.sw.handle_message("/status")
        self.assertIn("▶️ <b>a</b>", status)
        self.assertIn("session 10%", status)
        self.assertIsNone(self.sw.handle_message("/switch b"))
        self.assertEqual(self.state()["active"], "b")
        self.assertIn("Usage: /switch", self.sw.handle_message("/switch nobody"))


if __name__ == "__main__":
    unittest.main()
