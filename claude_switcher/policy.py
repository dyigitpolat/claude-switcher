"""When to leave the active account, and which account to move to.

An account is only as usable as its tighter limit: 10% session use means
little when the weekly limit is at 92%. So candidates are ranked by headroom,
`100 - max(session %, weekly %)`; ties go to the account whose weekly window
resets first, since its unused allowance expires soonest.
"""

from datetime import datetime


def headroom(usage):
    return 100.0 - max(usage["session"], usage["weekly"])


def leave_reason(usage, cfg):
    """Why the active account should be left now, or None."""
    limit = cfg["switch_at_percent"]
    if usage["session"] >= limit:
        return f"session usage at {usage['session']:.0f}%"
    if usage["weekly"] >= limit:
        return f"weekly usage at {usage['weekly']:.0f}%"
    return None


def _reset_key(iso):
    if not iso:
        return float("inf")
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return float("inf")


def choose_target(view, active, cfg, max_age_ms, now_ms):
    """Pick the next account: (label or None, one line per account on why).

    `view` maps label -> {"auth": "ok"|..., "usage": summary or None}.
    """
    switch_at = cfg["switch_at_percent"]
    roomy, tight, notes = [], [], {}
    for label, entry in view.items():
        if label == active:
            continue
        usage = entry.get("usage")
        if entry.get("auth") != "ok":
            notes[label] = "needs sign-in"
            continue
        if not usage or now_ms - usage.get("fetched_at", 0) > max_age_ms:
            notes[label] = "usage unknown"
            continue
        figures = f"session {usage['session']:.0f}%, weekly {usage['weekly']:.0f}%"
        if usage["session"] >= switch_at or usage["weekly"] >= switch_at:
            notes[label] = f"{figures}: at its limit"
            continue
        key = (-headroom(usage), _reset_key(usage.get("weekly_resets")), label)
        if usage["session"] <= cfg["candidate_max_session"] and usage["weekly"] <= cfg["candidate_max_weekly"]:
            roomy.append((key, label))
        else:
            tight.append((key, label))
        notes[label] = f"{figures}: {headroom(usage):.0f}% headroom"
    ranked = sorted(roomy) or sorted(tight)
    return (ranked[0][1] if ranked else None), notes
