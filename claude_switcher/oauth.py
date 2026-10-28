"""The Anthropic endpoints Claude Code itself uses for login, refresh and usage.

Values are the ones Claude Code 2.1.312 ships with: the public client id, the
manual-redirect login flow (the callback page shows a code to paste, so the
sign-in works from any device), and the scope lists it asks for.
"""

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
MANUAL_REDIRECT_URL = "https://platform.claude.com/oauth/code/callback"
API_BASE = "https://api.anthropic.com"
OAUTH_BETA = "oauth-2025-04-20"

REFRESH_SCOPES = [
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
    "user:plugins",
]
LOGIN_SCOPES = ["org:create_api_key", *REFRESH_SCOPES]

USER_AGENT = f"claude-switcher/{__version__}"

SUBSCRIPTION_TYPES = {
    "claude_max": "max",
    "claude_pro": "pro",
    "claude_team": "team",
    "claude_enterprise": "enterprise",
}


class HttpError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {str(body)[:300]}")
        self.status = status
        self.body = body


class LoginExpired(Exception):
    """The refresh token was refused: only a new sign-in fixes this account."""


def now_ms():
    return int(time.time() * 1000)


def _request(method, url, *, token=None, body=None, beta=False, timeout=30):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if beta:
        headers["anthropic-beta"] = OAUTH_BETA
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = raw.decode(errors="replace")
        raise HttpError(e.code, parsed) from None
    return json.loads(raw) if raw else None


# Sign-in


def new_login(email_hint=None):
    """A fresh PKCE login: (url to open, verifier to keep, state to match)."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = _b64url(secrets.token_bytes(32))
    params = [
        ("code", "true"),
        ("client_id", CLIENT_ID),
        ("response_type", "code"),
        ("redirect_uri", MANUAL_REDIRECT_URL),
        ("scope", " ".join(LOGIN_SCOPES)),
        ("code_challenge", challenge),
        ("code_challenge_method", "S256"),
        ("state", state),
    ]
    if email_hint:
        params.append(("login_hint", email_hint))
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}", verifier, state


def parse_code(text):
    """(code, state) from what the callback page shows (`code#state`) or its URL."""
    text = (text or "").strip()
    if text.startswith("http"):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [None])[0]
        return (code, state) if code else (None, None)
    text = "".join(text.split())
    if not text:
        return None, None
    code, _, state = text.partition("#")
    return code, (state or None)


def exchange_code(code, verifier, state):
    return _request(
        "POST",
        TOKEN_URL,
        body={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": MANUAL_REDIRECT_URL,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
            "state": state,
        },
    )


def refresh(refresh_token, scopes=None):
    try:
        return _request(
            "POST",
            TOKEN_URL,
            body={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
                "scope": " ".join(scopes or REFRESH_SCOPES),
            },
        )
    except HttpError as e:
        # `invalid_grant` (expired, revoked, already rotated) means the
        # refresh token is dead. A malformed request is also a 400, but
        # `invalid_request_error`: that, and anything else, is retried later.
        error = e.body.get("error") if isinstance(e.body, dict) else None
        if e.status == 401 or error == "invalid_grant":
            raise LoginExpired(str(e)) from None
        raise


def oauth_from_tokens(resp, profile=None, previous=None):
    """The `claudeAiOauth` record Claude Code keeps, from a token response."""
    previous = previous or {}
    issued = now_ms()
    refresh_token = resp.get("refresh_token") or previous.get("refreshToken")
    out = {
        "accessToken": resp["access_token"],
        "refreshToken": refresh_token,
        "expiresAt": issued + int(resp.get("expires_in") or 3600) * 1000,
    }
    if resp.get("refresh_token_expires_in"):
        out["refreshTokenExpiresAt"] = issued + int(resp["refresh_token_expires_in"]) * 1000
    elif refresh_token == previous.get("refreshToken") and previous.get("refreshTokenExpiresAt"):
        out["refreshTokenExpiresAt"] = previous["refreshTokenExpiresAt"]
    scope = resp.get("scope")
    out["scopes"] = scope.split() if scope else previous.get("scopes") or list(REFRESH_SCOPES)
    org = (profile or {}).get("organization") or {}
    subscription = SUBSCRIPTION_TYPES.get(org.get("organization_type"))
    out["subscriptionType"] = subscription or previous.get("subscriptionType")
    out["rateLimitTier"] = org.get("rate_limit_tier") or previous.get("rateLimitTier")
    return out


# Account details


def fetch_profile(access_token):
    return _request("GET", f"{API_BASE}/api/oauth/profile", token=access_token, beta=True, timeout=15)


def oauth_account_from_profile(profile, previous=None):
    """The `oauthAccount` block of ~/.claude.json for this profile."""
    account = profile.get("account") or {}
    org = profile.get("organization") or {}
    out = dict(previous or {}) if (previous or {}).get("accountUuid") == account.get("uuid") else {}
    out.update(
        {
            "accountUuid": account.get("uuid"),
            "emailAddress": account.get("email"),
            "organizationUuid": org.get("uuid"),
            "hasExtraUsageEnabled": bool(org.get("has_extra_usage_enabled")),
            "billingType": org.get("billing_type"),
            "accountCreatedAt": account.get("created_at"),
            "subscriptionCreatedAt": org.get("subscription_created_at"),
            "ccOnboardingFlags": org.get("cc_onboarding_flags") or {},
            "claudeCodeTrialEndsAt": org.get("claude_code_trial_ends_at"),
            "claudeCodeTrialDurationDays": org.get("claude_code_trial_duration_days"),
            "seatTier": org.get("seat_tier"),
            "displayName": account.get("display_name"),
            "fullName": account.get("full_name"),
            "profileFetchedAt": now_ms(),
            "organizationName": org.get("name"),
            "organizationType": org.get("organization_type"),
            "organizationRateLimitTier": org.get("rate_limit_tier"),
        }
    )
    return out


def fetch_usage(access_token):
    return _request("GET", f"{API_BASE}/api/oauth/usage", token=access_token, beta=True, timeout=20)


def summarize_usage(raw):
    """The two limits that stop a session (5h session, 7-day), plus model-scoped ones."""
    five = raw.get("five_hour") or {}
    week = raw.get("seven_day") or {}
    scoped = []
    for limit in raw.get("limits") or []:
        if limit.get("kind") == "weekly_scoped":
            model = ((limit.get("scope") or {}).get("model") or {}).get("display_name")
            scoped.append(
                {
                    "name": model or "scoped",
                    "percent": limit.get("percent"),
                    "resets_at": limit.get("resets_at"),
                }
            )
    session = float(five.get("utilization") or 0)
    weekly = float(week.get("utilization") or 0)
    # A window the server has locked is spent, whatever its percentage says.
    if five.get("locked_reason"):
        session = max(session, 100.0)
    if week.get("locked_reason"):
        weekly = max(weekly, 100.0)
    return {
        "session": session,
        "session_resets": five.get("resets_at"),
        "weekly": weekly,
        "weekly_resets": week.get("resets_at"),
        "scoped": scoped,
        "fetched_at": now_ms(),
    }


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
