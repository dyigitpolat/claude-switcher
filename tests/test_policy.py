import unittest

from claude_switcher import config, policy

NOW = 10_000_000
CFG = config.DEFAULTS


def entry(session, weekly, weekly_resets="2026-11-02T23:59:59+00:00", auth="ok", fetched_at=NOW):
    return {"auth": auth, "usage": {"session": session, "weekly": weekly, "weekly_resets": weekly_resets,
                                    "fetched_at": fetched_at}}


def choose(view, active="a"):
    return policy.choose_target(view, active, CFG, max_age_ms=120_000, now_ms=NOW)


class LeaveReason(unittest.TestCase):
    def test_session_or_weekly_at_threshold_leaves(self):
        self.assertIn("session", policy.leave_reason({"session": 95, "weekly": 10}, CFG))
        self.assertIn("weekly", policy.leave_reason({"session": 10, "weekly": 95.5}, CFG))
        self.assertIsNone(policy.leave_reason({"session": 94.9, "weekly": 94.9}, CFG))


class ChooseTarget(unittest.TestCase):
    def test_the_tighter_limit_decides(self):
        # b has the emptier session, but its weekly limit leaves only 30%.
        target, _ = choose({"a": entry(96, 10), "b": entry(5, 70), "c": entry(40, 20)})
        self.assertEqual(target, "c")

    def test_never_the_active_one_nor_unusable_ones(self):
        view = {
            "a": entry(96, 0),
            "b": entry(0, 0, auth="expired"),
            "c": entry(0, 0, fetched_at=NOW - 600_000),  # stale numbers
            "d": entry(95, 0),  # at its limit
        }
        target, notes = choose(view)
        self.assertIsNone(target)
        self.assertEqual(notes["b"], "needs sign-in")
        self.assertEqual(notes["c"], "usage unknown")
        self.assertIn("at its limit", notes["d"])

    def test_roomy_accounts_beat_ones_near_the_limit(self):
        # c has more headroom overall, but its session is past the candidate cap.
        target, _ = choose({"a": entry(99, 0), "b": entry(50, 80), "c": entry(88, 0)})
        self.assertEqual(target, "b")

    def test_a_tight_account_is_used_when_nothing_roomy_is_left(self):
        target, _ = choose({"a": entry(99, 0), "b": entry(92, 10), "c": entry(90, 93)})
        self.assertEqual(target, "b")

    def test_ties_go_to_the_weekly_window_that_resets_first(self):
        target, _ = choose({
            "a": entry(99, 0),
            "b": entry(20, 20, weekly_resets="2026-11-04T00:00:00+00:00"),
            "c": entry(20, 20, weekly_resets="2026-11-01T00:00:00+00:00"),
        })
        self.assertEqual(target, "c")


if __name__ == "__main__":
    unittest.main()
