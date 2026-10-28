import base64
import hashlib
import unittest
import urllib.parse
from unittest import mock

from claude_switcher import oauth


class Login(unittest.TestCase):
    def test_the_link_is_claude_codes_manual_flow_with_pkce(self):
        url, verifier, state = oauth.new_login("b@example.com")
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.assertTrue(url.startswith(oauth.AUTHORIZE_URL + "?"))
        self.assertEqual(query["client_id"], oauth.CLIENT_ID)
        self.assertEqual(query["redirect_uri"], oauth.MANUAL_REDIRECT_URL)
        self.assertEqual(query["scope"].split(), oauth.LOGIN_SCOPES)
        self.assertEqual(query["state"], state)
        self.assertEqual(query["login_hint"], "b@example.com")
        digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(query["code_challenge"], digest)
        self.assertEqual(query["code_challenge_method"], "S256")

    def test_pasted_codes(self):
        self.assertEqual(oauth.parse_code("abc123#st4te"), ("abc123", "st4te"))
        self.assertEqual(oauth.parse_code("  abc\n123#st 4te \n"), ("abc123", "st4te"))
        self.assertEqual(oauth.parse_code("abc123"), ("abc123", None))
        self.assertEqual(
            oauth.parse_code(f"{oauth.MANUAL_REDIRECT_URL}?code=abc&state=xyz"), ("abc", "xyz")
        )
        self.assertEqual(oauth.parse_code("   "), (None, None))


class RefreshErrors(unittest.TestCase):
    # The bodies platform.claude.com returns for each case.
    DEAD = {"error": "invalid_grant", "error_description": "Refresh token not found or invalid"}
    MALFORMED = {"type": "error", "error": {"type": "invalid_request_error", "message": "Invalid request format"}}

    def refused(self, status, body):
        return mock.patch.object(oauth, "_request", side_effect=oauth.HttpError(status, body))

    def test_only_a_dead_token_asks_for_a_new_sign_in(self):
        with self.refused(400, self.DEAD), self.assertRaises(oauth.LoginExpired):
            oauth.refresh("r")
        for status, body in [(400, self.MALFORMED), (429, {}), (500, "upstream error")]:
            with self.refused(status, body), self.assertRaises(oauth.HttpError):
                oauth.refresh("r")


class Tokens(unittest.TestCase):
    def test_a_refresh_keeps_what_the_response_leaves_out(self):
        previous = {
            "accessToken": "old", "refreshToken": "r1", "expiresAt": 0, "refreshTokenExpiresAt": 123,
            "scopes": ["user:inference"], "subscriptionType": "max", "rateLimitTier": "tier",
        }
        rec = oauth.oauth_from_tokens({"access_token": "new", "expires_in": 3600}, previous=previous)
        self.assertEqual(rec["refreshToken"], "r1")
        self.assertEqual(rec["refreshTokenExpiresAt"], 123)
        self.assertEqual(rec["scopes"], ["user:inference"])
        self.assertEqual(rec["subscriptionType"], "max")
        self.assertGreater(rec["expiresAt"], oauth.now_ms() + 3500_000)

    def test_a_rotated_refresh_token_does_not_inherit_the_old_expiry(self):
        previous = {"refreshToken": "r1", "refreshTokenExpiresAt": 123}
        rec = oauth.oauth_from_tokens({"access_token": "a", "refresh_token": "r2", "expires_in": 60,
                                       "scope": "user:profile user:inference"}, previous=previous)
        self.assertEqual(rec["refreshToken"], "r2")
        self.assertNotIn("refreshTokenExpiresAt", rec)
        self.assertEqual(rec["scopes"], ["user:profile", "user:inference"])

    def test_a_login_takes_its_plan_from_the_profile(self):
        prof = {"organization": {"organization_type": "claude_pro", "rate_limit_tier": "default_claude_pro"}}
        rec = oauth.oauth_from_tokens({"access_token": "a", "refresh_token": "r", "expires_in": 60,
                                       "refresh_token_expires_in": 86400}, profile=prof)
        self.assertEqual(rec["subscriptionType"], "pro")
        self.assertEqual(rec["rateLimitTier"], "default_claude_pro")
        self.assertIn("refreshTokenExpiresAt", rec)


class Usage(unittest.TestCase):
    # Trimmed from a real /api/oauth/usage response.
    RAW = {
        "five_hour": {"utilization": 47.0, "resets_at": "2026-10-28T12:59:59.653398+00:00", "locked_reason": None},
        "seven_day": {"utilization": 44.0, "resets_at": "2026-11-02T23:59:59.653419+00:00", "locked_reason": None},
        "seven_day_opus": None,
        "limits": [
            {"kind": "session", "group": "session", "percent": 47},
            {"kind": "weekly_all", "group": "weekly", "percent": 44},
            {"kind": "weekly_scoped", "group": "weekly", "percent": 0,
             "resets_at": "2026-11-03T00:00:00+00:00",
             "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
        ],
    }

    def test_session_weekly_and_scoped(self):
        u = oauth.summarize_usage(self.RAW)
        self.assertEqual((u["session"], u["weekly"]), (47.0, 44.0))
        self.assertEqual(u["session_resets"], "2026-10-28T12:59:59.653398+00:00")
        self.assertEqual(u["scoped"], [{"name": "Fable", "percent": 0, "resets_at": "2026-11-03T00:00:00+00:00"}])

    def test_a_locked_window_counts_as_spent_and_a_missing_one_as_unused(self):
        u = oauth.summarize_usage({"five_hour": {"utilization": 12, "locked_reason": "limit"}, "seven_day": None})
        self.assertEqual((u["session"], u["weekly"]), (100.0, 0.0))


if __name__ == "__main__":
    unittest.main()
