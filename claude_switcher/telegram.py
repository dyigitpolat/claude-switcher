"""A minimal Telegram Bot API client: send, delete, long-poll for messages."""

import http.client
import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


class TelegramError(Exception):
    pass


class Bot:
    def __init__(self, token, chat_id=None):
        self.token = token
        self.chat_id = chat_id

    @property
    def configured(self):
        return bool(self.token and self.chat_id)

    def call(self, method, http_timeout=30, **params):
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as e:
            try:
                body = json.load(e)
            except (ValueError, OSError, http.client.HTTPException):
                body = {"description": f"HTTP {e.code}"}
        except (OSError, ValueError, http.client.HTTPException) as e:
            # A dropped connection, a cut-off or non-JSON answer (a proxy's
            # page). The token is part of the URL; keep it out of logs.
            raise TelegramError(f"{method}: {type(e).__name__}") from None
        if not isinstance(body, dict) or not body.get("ok") or "result" not in body:
            raise TelegramError(f"{method}: {body.get('description') if isinstance(body, dict) else body}")
        return body["result"]

    def send(self, html, button=None, quiet=False):
        """Send to the configured chat; never raises. Returns the message id."""
        if not self.configured:
            log.info("telegram (not configured): %s", html)
            return None
        params = {
            "chat_id": self.chat_id,
            "text": html[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": quiet,
        }
        if button:
            text, url = button
            params["reply_markup"] = {"inline_keyboard": [[{"text": text, "url": url}]]}
        try:
            return self.call("sendMessage", **params)["message_id"]
        except TelegramError as e:
            log.warning("telegram send failed: %s", e)
            return None

    def delete(self, message_id):
        try:
            self.call("deleteMessage", chat_id=self.chat_id, message_id=message_id)
        except TelegramError as e:
            log.info("telegram delete failed: %s", e)

    def updates(self, offset, timeout=50):
        return self.call(
            "getUpdates",
            http_timeout=timeout + 10,
            offset=offset,
            timeout=timeout,
            allowed_updates=["message"],
        )
