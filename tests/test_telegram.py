import http.client
import io
import json
import os
import socket
import time
import unittest
from unittest import mock

from claude_switcher import config, daemon, store
from claude_switcher.daemon import Switcher
from claude_switcher.telegram import Bot, TelegramError
from helpers import TempHome


class Stop(BaseException):
    """Ends an endless loop under test; not an Exception, so no handler eats it."""


def answer(body):
    resp = mock.MagicMock()
    resp.__enter__.return_value = io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())
    return resp


class Client(unittest.TestCase):
    def test_every_way_a_call_can_go_wrong_is_a_retryable_telegram_error(self):
        bot = Bot("123:abc", 42)
        cut_off = mock.MagicMock()
        cut_off.__enter__.side_effect = http.client.IncompleteRead(b"{")
        for outcome in [
            answer(b"<html>proxy error</html>"),
            answer({"ok": True}),  # no result
            answer([1, 2]),
            cut_off,
            ConnectionResetError(),
            TimeoutError(),
        ]:
            kind = mock.patch("urllib.request.urlopen", side_effect=[outcome]) if isinstance(outcome, BaseException) \
                else mock.patch("urllib.request.urlopen", return_value=outcome)
            with kind, self.assertRaises(TelegramError) as raised:
                bot.call("getUpdates")
            self.assertNotIn("123:abc", str(raised.exception))

    def test_send_never_raises(self):
        bot = Bot("123:abc", 42)
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertIsNone(bot.send("hi"))


class Loop(TempHome):
    def setUp(self):
        super().setUp()
        self.sw = Switcher(config.load_config())
        self.sw.bot = mock.Mock(chat_id=42, configured=True)
        self.sw.handle_message = mock.Mock(return_value="pong")

    def test_one_poll_answers_its_own_chat_alone_and_remembers_where_it_got(self):
        self.sw.bot.updates.return_value = [
            {"update_id": 7, "message": {"chat": {"id": 999}, "text": "/status", "message_id": 1}},
            {"update_id": 8, "message": {"chat": {"id": 42}, "text": "/status", "message_id": 2}},
        ]
        self.assertEqual(self.sw._telegram_poll(None), 9)
        self.sw.handle_message.assert_called_once_with("/status", 2)
        self.sw.bot.send.assert_called_once_with("pong")
        self.assertEqual(store.load_state()["tg_offset"], 9)

    def test_the_loop_outlives_any_error(self):
        polls = mock.Mock(side_effect=[RuntimeError("disk full"), 5, Stop()])
        with mock.patch.object(self.sw, "_telegram_poll", polls), mock.patch.object(daemon.time, "sleep"):
            with self.assertRaises(Stop):
                self.sw._telegram_loop()
        self.assertEqual(polls.call_count, 3)
        self.assertEqual(polls.call_args_list[2], mock.call(5))

    def test_a_dead_or_stalled_bot_makes_the_service_exit_for_a_restart(self):
        self.sw._telegram_thread = mock.Mock(is_alive=mock.Mock(return_value=True))
        self.sw.telegram_polled_at = time.monotonic()
        self.sw.check_health()  # healthy: nothing happens
        self.sw.telegram_polled_at = time.monotonic() - daemon.TELEGRAM_STALL_S - 1
        with self.assertRaises(SystemExit):
            self.sw.check_health()
        self.sw.telegram_polled_at = time.monotonic()
        self.sw._telegram_thread.is_alive.return_value = False
        with self.assertRaises(SystemExit):
            self.sw.check_health()

    def test_without_telegram_there_is_nothing_to_watch(self):
        self.sw.bot.configured = False
        self.sw._telegram_thread = None
        self.sw.check_health()


class SystemdNotify(TempHome):
    def test_ready_and_watchdog_reach_systemds_socket(self):
        path = str(self.root / "notify.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as listener:
            listener.bind(path)
            with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": path}):
                daemon.sd_notify("READY=1")
                daemon.sd_notify("WATCHDOG=1")
            self.assertEqual(listener.recv(64), b"READY=1")
            self.assertEqual(listener.recv(64), b"WATCHDOG=1")

    def test_outside_systemd_it_does_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            daemon.sd_notify("READY=1")


if __name__ == "__main__":
    unittest.main()
