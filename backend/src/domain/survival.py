"""Kaplan-Meier survival analysis for right-censored handoff observations.

A finished shot contributes an event observation at its total actual minutes.
A shot still in progress contributes a right-censored observation: its total
duration is only known to be at least the minutes already spent.  The
Kaplan-Meier product-limit estimator turns these mixed observations into an
empirical distribution function without treating censored work as completed.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Observation:
    """One duration fact. ``censored=True`` means only ``time`` is a lower bound."""

    time: float
    censored: bool


@dataclass(frozen=True)
class SurvivalCurve:
    """Stepwise survival function S(t) = P(T > t) stored at event times."""

    event_times: tuple[float, ...]
    survival: tuple[float, ...]

    def cdf_at(self, time: float) -> float:
        """Largest empirical CDF reached at ``time`` (right-continuous steps)."""
        reached = 0.0
        for event_time, survive in zip(self.event_times, self.survival):
            if event_time <= time:
                reached = 1.0 - survive
            else:
                break
        return reached


def kaplan_meier(observations: Sequence[Observation]) -> SurvivalCurve:
    """Estimate the product-limit survival curve from mixed observations."""
    if not observations:
        return SurvivalCurve((), ())
    events_at = Counter(observation.time for observation in observations if not observation.censored)
    censored_at = Counter(observation.time for observation in observations if observation.censored)
    event_times: list[float] = []
    survival: list[float] = []
    at_risk = len(observations)
    survive = 1.0
    for current in sorted(events_at.keys() | censored_at.keys()):
        events = events_at.get(current, 0)
        if events:
            survive *= 1 - events / at_risk
            event_times.append(current)
            survival.append(survive)
        at_risk -= events + censored_at.get(current, 0)
    return SurvivalCurve(tuple(event_times), tuple(survival))


def conditional_quantile(curve: SurvivalCurve, elapsed: float, percentile: int) -> float | None:
    """Smallest total duration t >= elapsed with P(T <= t | T >= elapsed) >= alpha.

    The conditional distribution is the Kaplan-Meier curve restricted to the
    risk set, so its CDF at ``t`` is ``(F(t) - F0) / (1 - F0)`` where
    ``F0 = F(elapsed)`` already includes an event occurring exactly at the
    bound.  Returns ``None`` when the observations still at risk cannot reach
    the requested percentile, which is the "sample does not support this tier"
    case.  Quantile convention mirrors the boosting module: the smallest
    ordered sample whose empirical CDF reaches ``percentile``.
    """
    if not curve.event_times:
        return None
    base = curve.cdf_at(elapsed)
    target = base + percentile / 100 * (1 - base)
    for event_time, survive in zip(curve.event_times, curve.survival):
        if event_time >= elapsed and 1 - survive >= target - 1e-12:
            return event_time
    return None
