"""Domain tests for mid-shift handoff re-estimation (stdlib unittest only)."""

import random
import unittest

from src.domain.handoff import COMPLETION, PROGRESS, estimate_handoff
from src.domain.survival import Observation, conditional_quantile, kaplan_meier


def independent_survival(observations, elapsed, percentile):
    """Closed-form Kaplan-Meier re-derived without importing production code.

    Builds the full product-limit curve in one pass, then takes the quantile
    on the *conditional* curve S(t) / S(elapsed) of the surviving risk set.
    """
    distinct = sorted({observation.time for observation in observations})
    at_risk = len(observations)
    survive = 1.0
    curve = {}
    for t in distinct:
        events = sum(1 for observation in observations
                     if not observation.censored and observation.time == t)
        if events:
            survive *= 1 - events / at_risk
        curve[t] = survive
        at_risk -= sum(1 for observation in observations if observation.time == t)
    # S(elapsed): survival after every event at or before the bound.
    base_survive = 1.0
    for t in distinct:
        if t <= elapsed:
            base_survive = curve[t]
        else:
            break
    # Conditional CDF = 1 - S(t)/S(elapsed); require it to reach alpha.
    target_survive = base_survive * (1 - percentile / 100)
    for t in distinct:
        if t >= elapsed and curve[t] <= target_survive + 1e-12:
            return t
    return None



def shot(shot_id, split, actual=None, due=None):
    row = {"id": shot_id, "scene_id": f"S-{shot_id}", "split": split,
           "frames": 50, "occlusion_pct": 20, "overlapping_characters": 1,
           "rig_marks": 2, "due_minutes": due, "actual_minutes": actual}
    return row


def train_rows(durations):
    return [shot(f"H-{index:02d}", "train", actual=duration) for index, duration in enumerate(durations, 1)]


def pending_rows(count):
    return [shot(f"P-{index:02d}", "pending", due=240) for index in range(1, count + 1)]


def event(event_id, shot_id, kind, minutes, received_at):
    return {"id": event_id, "shot_id": shot_id, "kind": kind,
            "minutes": minutes, "received_at": received_at}


class KaplanMeierTests(unittest.TestCase):
    def test_all_uncensored_matches_empirical_quantile(self):
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        curve = kaplan_meier([Observation(value, False) for value in values])
        # ceil(n * 0.5) = 3rd ordered sample; ceil(n * 0.9) = 5th.
        self.assertEqual(conditional_quantile(curve, 0.0, 50), 30.0)
        self.assertEqual(conditional_quantile(curve, 0.0, 90), 50.0)

    def test_censored_observations_are_not_treated_as_finish_events(self):
        # One censored observation at 25 cannot create an event step there.
        observations = [Observation(10, False), Observation(25, True),
                        Observation(30, False), Observation(40, False)]
        curve = kaplan_meier(observations)
        self.assertNotIn(25.0, curve.event_times)
        # S(10) = 0.75; the censored point at 25 leaves S unchanged.
        self.assertEqual(curve.cdf_at(24.9), 0.25)
        self.assertEqual(curve.cdf_at(25.0), 0.25)
        # S(30) = 0.75 * 2/3 = 0.5, so median total duration is 30.
        self.assertEqual(conditional_quantile(curve, 0.0, 50), 30.0)

    def test_conditional_quantile_uses_survivor_branch(self):
        values = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
        curve = kaplan_meier([Observation(value, False) for value in values])
        # Given T >= 55, survivors are 60..100; median survivor is 80.
        self.assertEqual(conditional_quantile(curve, 55.0, 50), 80.0)
        # Given T >= 50, six values survive; conditional P90 step is at 100.
        self.assertEqual(conditional_quantile(curve, 50.0, 90), 100.0)

    def test_tier_blank_when_surviving_sample_cannot_reach_percentile(self):
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        curve = kaplan_meier([Observation(value, False) for value in values])
        # Only 50 survives past 45: max conditional CDF there is 1.0 at 50,
        # P90 of a single survivor is still reachable at that point...
        self.assertEqual(conditional_quantile(curve, 45.0, 90), 50.0)
        # ...but with the largest duration itself censored, nothing reaches 0.9.
        censored_curve = kaplan_meier([Observation(v, False) for v in values[:-1]]
                                      + [Observation(50.0, True)])
        self.assertIsNone(conditional_quantile(censored_curve, 45.0, 90))
        self.assertIsNone(conditional_quantile(censored_curve, 45.0, 50))

    def test_empty_pool_has_no_quantiles(self):
        curve = kaplan_meier([])
        self.assertIsNone(conditional_quantile(curve, 0.0, 50))


class OracleCrossCheckTests(unittest.TestCase):
    def test_mixed_censored_samples_match_independent_kaplan_meier(self):
        rng = random.Random(20261126)
        for trial in range(200):
            observations = []
            for _ in range(rng.randint(4, 60)):
                duration = round(rng.uniform(1, 200), 2)
                censored = rng.random() < 0.35
                observations.append(Observation(duration, censored))
            curve = kaplan_meier(observations)
            elapsed = round(rng.uniform(0, 120), 2)
            for percentile in (50, 90):
                expected = independent_survival(observations, elapsed, percentile)
                actual = conditional_quantile(curve, elapsed, percentile)
                self.assertEqual(actual, expected,
                                 f"trial {trial} alpha={percentile} elapsed={elapsed}")


class HandoffTests(unittest.TestCase):
    def setUp(self):
        # Ten historical durations give clean ordered total-duration evidence.
        self.rows = train_rows([10.0 * i for i in range(1, 11)]) + pending_rows(3)

    def test_in_progress_shot_is_censored_at_elapsed_lower_bound(self):
        events = [event(1, "P-01", PROGRESS, 35.0, "2026-11-26T12:00:00+00:00")]
        result = estimate_handoff(self.rows, events, "2026-11-26T13:00:00+00:00", 240)
        entry = next(row for row in result["shot_table"]["rows"] if row["id"] == "P-01")
        self.assertEqual(entry["state"], "进行中")
        self.assertEqual(entry["elapsed_minutes"], 35.0)
        # Pool = 10 historical events + one censored observation at 35.
        self.assertEqual(result["profile_table"]["rows"][2]["value"], 11)
        self.assertEqual(result["profile_table"]["rows"][3]["value"], 1)
        # Survivors T >= 35 among historical: 40..100 (7) plus the censored
        # shot itself; P50 total duration conditional on surviving 35 is 70.
        self.assertEqual(entry["p50_total_minutes"], 70.0)
        self.assertEqual(entry["p50_remaining_minutes"], 35.0)
        self.assertEqual(entry["p50_risk_size"], 8)

    def test_late_completion_only_counts_from_receipt_and_skips_old_handoff(self):
        events = [
            event(1, "P-01", PROGRESS, 35.0, "2026-11-26T12:00:00+00:00"),
            # Completion arrives after the handoff instant: ignored at 13:00.
            event(2, "P-01", COMPLETION, 88.0, "2026-11-26T14:30:00+00:00"),
        ]
        at_handoff = estimate_handoff(self.rows, events, "2026-11-26T13:00:00+00:00", 240)
        entry = next(row for row in at_handoff["shot_table"]["rows"] if row["id"] == "P-01")
        self.assertEqual(entry["state"], "进行中")
        self.assertIsNone(entry["completed_minutes"])
        self.assertEqual(at_handoff["profile_table"]["rows"][5]["value"], 1)  # late events
        self.assertTrue(any("晚于交班时点" in warning for warning in at_handoff["warnings"]))

        # Re-running the old handoff after later receipt gives identical tiers.
        rerun = estimate_handoff(self.rows, events, "2026-11-26T13:00:00+00:00", 240)
        self.assertEqual(rerun["shot_table"]["rows"], at_handoff["shot_table"]["rows"])
        self.assertEqual(rerun["queue_table"]["rows"], at_handoff["queue_table"]["rows"])

        # A new handoff at 15:00 treats the shot as finished event evidence.
        later = estimate_handoff(self.rows, events, "2026-11-26T15:00:00+00:00", 240)
        entry_later = next(row for row in later["shot_table"]["rows"] if row["id"] == "P-01")
        self.assertEqual(entry_later["state"], "已完工")
        self.assertEqual(entry_later["completed_minutes"], 88.0)
        self.assertEqual(later["profile_table"]["rows"][5]["value"], 0)

    def test_missing_p90_leaves_blank_and_never_takes_station(self):
        # Only one historical shot; P-01 already past its duration so no event
        # time survives long enough to support either tier.
        rows = train_rows([10.0]) + pending_rows(2)
        events = [event(1, "P-01", PROGRESS, 55.0, "2026-11-26T12:00:00+00:00"),
                  event(2, "P-02", PROGRESS, 5.0, "2026-11-26T12:05:00+00:00")]
        result = estimate_handoff(rows, events, "2026-11-26T13:00:00+00:00", 240)
        by_id = {row["id"]: row for row in result["shot_table"]["rows"]}
        self.assertIsNone(by_id["P-01"]["p90_remaining_minutes"])
        self.assertFalse(by_id["P-01"]["p90_supported"])
        queue = {row["id"]: row for row in result["queue_table"]["rows"]}
        self.assertEqual(queue["P-01"]["action"], "顺延复核")
        self.assertIn("不占下一班工位", queue["P-01"]["reason"])
        self.assertIsNone(queue["P-01"]["reserve_minutes"])
        self.assertIsNone(queue["P-01"]["start_minute"])
        # P-02 can use the single event at 10: conditional median is 10.
        self.assertEqual(by_id["P-02"]["p50_total_minutes"], 10.0)
        self.assertEqual(by_id["P-02"]["p50_remaining_minutes"], 5.0)

    def test_queue_reserves_ceil_remaining_p90_and_schedules_greedy(self):
        rows = train_rows([10.0 * i for i in range(1, 11)]) + [
            shot("P-01", "pending", due=60), shot("P-02", "pending", due=240)]
        events = [event(1, "P-01", PROGRESS, 5.0, "2026-11-26T12:00:00+00:00"),
                  event(2, "P-02", PROGRESS, 5.0, "2026-11-26T12:00:00+00:00")]
        result = estimate_handoff(rows, events, "2026-11-26T13:00:00+00:00", 240)
        queue = {row["id"]: row for row in result["queue_table"]["rows"]}
        # Tiers: conditional P90 at elapsed 5 over 10..100 is 90 -> 85 left,
        # ceil 85; P-01 due 60 cannot fit, P-02 then takes the station.
        self.assertEqual(queue["P-01"]["reserve_minutes"], 85)
        self.assertEqual(queue["P-01"]["action"], "顺延复核")
        self.assertEqual(queue["P-02"]["action"], "下一班可排")
        self.assertEqual(queue["P-02"]["start_minute"], 0)
        self.assertEqual(queue["P-02"]["finish_minute"], 85)
        self.assertEqual(result["scheduled"], 1)
        self.assertEqual(result["reserved"], 85)

    def test_idle_pending_shots_are_flagged_and_not_queued(self):
        result = estimate_handoff(self.rows, [], "2026-11-26T13:00:00+00:00", 240)
        self.assertEqual(result["in_progress"], 0)
        self.assertTrue(any("尚无任何工时记录" in warning for warning in result["warnings"]))
        self.assertEqual(result["queue_table"]["rows"], [])

    def test_finished_pending_shot_enters_pool_as_event(self):
        events = [
            event(1, "P-01", PROGRESS, 30.0, "2026-11-26T11:00:00+00:00"),
            event(2, "P-01", COMPLETION, 72.5, "2026-11-26T12:30:00+00:00"),
            event(3, "P-02", PROGRESS, 20.0, "2026-11-26T12:40:00+00:00"),
        ]
        result = estimate_handoff(self.rows, events, "2026-11-26T13:00:00+00:00", 240)
        by_id = {row["id"]: row for row in result["shot_table"]["rows"]}
        self.assertEqual(by_id["P-01"]["state"], "已完工")
        self.assertEqual(by_id["P-02"]["state"], "进行中")
        # Pool: 10 historical + completion event at 72.5 + censored at 20.
        self.assertEqual(result["profile_table"]["rows"][2]["value"], 12)
        queued_ids = {row["id"] for row in result["queue_table"]["rows"]}
        self.assertNotIn("P-01", queued_ids)
        self.assertIn("P-02", queued_ids)

    def test_rerun_is_deterministic_and_ignores_events_arriving_after_cutoff(self):
        events = [
            event(1, "P-01", PROGRESS, 35.0, "2026-11-26T12:00:00+00:00"),
            event(2, "P-02", PROGRESS, 12.0, "2026-11-26T12:10:00+00:00"),
        ]
        cutoff = "2026-11-26T13:00:00+00:00"
        first = estimate_handoff(self.rows, events, cutoff, 240)
        # More facts arrive two hours later, including a completion.
        events.extend([
            event(3, "P-01", COMPLETION, 80.0, "2026-11-26T15:00:00+00:00"),
            event(4, "P-02", PROGRESS, 48.0, "2026-11-26T15:05:00+00:00"),
        ])
        rerun = estimate_handoff(self.rows, events, cutoff, 240)
        self.assertEqual(rerun["shot_table"], first["shot_table"])
        self.assertEqual(rerun["queue_table"], first["queue_table"])
        self.assertEqual(rerun["profile_table"]["rows"][5]["value"], 2)
        # A fresh handoff at 16:00 absorbs the new facts and shifts the tiers.
        new = estimate_handoff(self.rows, events, "2026-11-26T16:00:00+00:00", 240)
        self.assertNotEqual(new["shot_table"], first["shot_table"])
        entry = next(row for row in new["shot_table"]["rows"] if row["id"] == "P-01")
        self.assertEqual(entry["state"], "已完工")
        self.assertEqual(entry["completed_minutes"], 80.0)


if __name__ == "__main__":
    unittest.main()
