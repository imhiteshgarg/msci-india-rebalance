"""Scoring a prediction against what MSCI actually announced."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .pipeline import Prediction


def load_actuals(path: Path) -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


@dataclass
class Score:
    label: str
    predicted: list[str]
    actual: list[str]
    hits: list[str] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)
    false_positives: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        p, a = set(self.predicted), set(self.actual)
        self.hits = sorted(p & a)
        self.misses = sorted(a - p)
        self.false_positives = sorted(p - a)

    @property
    def recall(self) -> float | None:
        return len(self.hits) / len(self.actual) if self.actual else None

    @property
    def precision(self) -> float | None:
        return len(self.hits) / len(self.predicted) if self.predicted else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if not p or not r:
            return 0.0 if (p is not None and r is not None) else None
        return 2 * p * r / (p + r)


@dataclass
class BacktestResult:
    review_label: str
    additions: Score
    deletions: Score
    threshold_dates: int
    n_dates: int
    announced_but_reversed: list[str] = field(default_factory=list)
    detail: dict[str, dict] = field(default_factory=dict)


def score_prediction(
    prediction: Prediction,
    actuals: dict,
    min_dates: int = 1,
) -> BacktestResult | None:
    """Compare the prediction with the recorded outcome for the same review.

    `min_dates` is the confidence cut: a stock counts as predicted only if it
    cleared the screen on at least this many of the candidate price cut-off dates.
    Sweeping it is the honest way to report accuracy, because setting it to 1
    maximises recall and setting it to 10 maximises precision.
    """
    entry = (actuals.get("reviews") or {}).get(prediction.calendar.label)
    if not entry:
        return None

    std = entry.get("standard") or {}
    actual_adds = [s.upper() for s in (std.get("additions") or [])]
    actual_dels = [s.upper() for s in (std.get("deletions") or [])]

    # Names MSCI put on its published addition list and then withdrew before
    # implementation. Scoring these as model errors misattributes the cause: the
    # model's job is to reproduce MSCI's size and liquidity arithmetic, and on that it
    # agreed with MSCI. Adani Energy Solutions in May 2026 was pulled because the
    # stock entered India's Additional Surveillance Measure, a regulatory overlay no
    # size rule predicts. They are excluded from both the hit and the miss counts
    # rather than silently credited.
    reversed_names = {s.upper() for s in (entry.get("announced_but_reversed") or [])}

    predicted_adds = [
        s for s, c, _ in prediction.ranked_additions()
        if c >= min_dates and s not in reversed_names
    ]
    predicted_dels = [s for s, c, _ in prediction.ranked_deletions() if c >= min_dates]
    withheld = sorted(
        s for s, c, _ in prediction.ranked_additions()
        if c >= min_dates and s in reversed_names
    )

    detail: dict[str, dict] = {}
    for sym in set(predicted_adds) | set(actual_adds) | set(predicted_dels) | set(actual_dels):
        dates_passed = prediction.add_frequency.get(sym, 0) or prediction.delete_frequency.get(sym, 0)
        verdicts = [r.all_verdicts[sym] for r in prediction.date_results if sym in r.all_verdicts]
        detail[sym] = {
            "name": prediction.names.get(sym, sym),
            "dates_passed": dates_passed,
            "in_universe": bool(verdicts),
            "mean_size_multiple": (
                sum(v.size_multiple for v in verdicts) / len(verdicts) if verdicts else None
            ),
            "mean_ffmc_multiple": (
                sum(v.ffmc_multiple for v in verdicts) / len(verdicts) if verdicts else None
            ),
            "blocking_reasons": sorted({r for v in verdicts for r in v.blocking_reasons}),
            "is_constituent": sym in prediction.constituents,
        }

    return BacktestResult(
        review_label=prediction.calendar.label,
        additions=Score("Standard additions", predicted_adds, actual_adds),
        deletions=Score("Standard deletions", predicted_dels, actual_dels),
        announced_but_reversed=withheld,
        threshold_dates=min_dates,
        n_dates=prediction.n_dates,
        detail=detail,
    )


def sweep_confidence(prediction: Prediction, actuals: dict) -> list[BacktestResult]:
    """Score at every confidence threshold from 1 to the number of candidate dates."""
    results = []
    for k in range(1, prediction.n_dates + 1):
        r = score_prediction(prediction, actuals, min_dates=k)
        if r:
            results.append(r)
    return results


def membership_from_announcements(actuals: dict, before_label: str) -> tuple[set[str], set[str]]:
    """Replay recorded MSCI announcements to get known members and known non-members.

    Returns (known_members, known_departures) using only reviews strictly earlier
    than `before_label`, so there is no hindsight.

    This exists because a "not an index member" fact has a shelf life. The pre-review
    note that correctly identified Adani Energy Solutions, Groww, Laurus Labs and
    Lenskart as non-members in July 2026 stopped being true on 28 August, when MSCI
    added all four. Carrying that stale fact into the November review reported them
    as additions a second time. Announced history is the cheapest correction
    available: every recorded addition is a member from its effective date onward.
    """
    members: set[str] = set()
    departures: set[str] = set()
    reviews = actuals.get("reviews") or {}
    for label in sorted(reviews):
        if label >= before_label:
            continue
        std = (reviews[label] or {}).get("standard") or {}
        for sym in std.get("additions") or []:
            s = sym.upper()
            members.add(s)
            departures.discard(s)
        for sym in std.get("deletions") or []:
            s = sym.upper()
            members.discard(s)
            departures.add(s)
    return members, departures
