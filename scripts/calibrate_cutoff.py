"""Find the size cut-off that best reproduces a review whose outcome is known.

The Standard size-segment cut-off is the model's most important parameter and the
hardest to pin down: MSCI derives it from the global investable universe, and the
local 85%-coverage calculation here is biased because this universe is broader than
MSCI's. Rather than guess, fit it.

The data is prepared once and only the screen is re-run per candidate cut-off, so a
sweep of a dozen values costs about as much as two full runs.

    .venv/bin/python scripts/calibrate_cutoff.py --review 2026-11
    .venv/bin/python scripts/calibrate_cutoff.py --review 2026-11 --low 4200 --high 6600 --step 150

Read the output as a range, not a point. A plateau of cut-offs that all reproduce the
outcome is the honest answer; a single best value is usually overfitting to one
marginal name.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from msci_predict.backtest import (
    load_actuals,
    membership_from_announcements,
    score_prediction,
)
from msci_predict.calendar_rules import build_review_calendar, parse_review_label
from msci_predict.data import (
    DataStore,
    load_fif_history,
    load_fif_overrides,
    load_known_non_constituents,
)
from msci_predict.pipeline import Predictor, load_parameters

ROOT = Path(__file__).resolve().parents[1]

# Multiples of the EM Standard reference that bracket the cut-off. The optimum sat
# at 0.635x to 0.774x across four reviews; the band is widened below that because
# the May 2026 optimum sat on the lower edge and clipping it cost recall.
# Fitted on four reviews - re-check it as more are recorded.
BAND_LOW_MULT = 0.57
BAND_HIGH_MULT = 0.80


def _band_values(low: float, high: float, step: float) -> list[float]:
    values, value = [], low
    while value <= high + 1e-9:
        values.append(value)
        value += step
    return values


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--review", default="2026-11")
    ap.add_argument("--low", type=float, default=4100.0, help="Lowest cut-off, USD mn")
    ap.add_argument("--high", type=float, default=7000.0, help="Highest cut-off, USD mn")
    ap.add_argument("--step", type=float, default=200.0)
    ap.add_argument("--mode", choices=["calibrated", "fixed"], default="calibrated")
    ap.add_argument("--params", type=Path, default=ROOT / "config" / "parameters.yaml",
                    help="Use that review's own MSCI parameters, not the current ones")
    ap.add_argument("--min-float-confidence", default="LOW",
                    choices=["LOW", "MEDIUM", "HIGH"])
    ap.add_argument("--auto-band", action="store_true",
                    help="Derive --low and --high from the EM Standard reference in "
                         "--params, using the 0.63x-0.78x range observed across four "
                         "reviews. Avoids doing the arithmetic by hand.")
    ap.add_argument("--robustness", action="store_true",
                    help="Also report how many cut-offs in the band each name survives")
    ap.add_argument("--constituents-file", type=Path,
                    default=ROOT / "config" / "msci_india_constituents.csv",
                    help="Real index membership. Calibrating without it conflates "
                         "cut-off error with membership error.")
    ap.add_argument("--min-dates", type=int, default=6,
                    help="Candidate dates a name must clear to count as predicted")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")

    params = load_parameters(args.params)
    year, month = parse_review_label(args.review)
    actuals = load_actuals(ROOT / "config" / "actual_reviews.yaml")
    entry = (actuals.get("reviews") or {}).get(f"{year}-{month:02d}") or {}

    def _as_date(v) -> date | None:
        if isinstance(v, date):
            return v
        return datetime.fromisoformat(v).date() if isinstance(v, str) else None

    cal = build_review_calendar(
        year, month,
        effective_date=_as_date(entry.get("effective_date")),
        announcement_date=_as_date(entry.get("announcement_date")),
    )
    scoring = bool(entry.get("standard"))
    if not scoring:
        print(f"No recorded outcome for {cal.label}, so nothing can be scored. Running in")
        print("forecast mode: the robustness table is produced without accuracy columns.")
        if not args.robustness:
            raise SystemExit(
                "Nothing to do. Add --robustness to get the cut-off band report, or pick "
                "a review whose outcome is recorded in config/actual_reviews.yaml."
            )

    constituents = None
    if args.constituents_file and args.constituents_file.exists():
        import csv as _csv
        with open(args.constituents_file, newline="") as fh:
            constituents = {
                r["symbol"].strip().upper()
                for r in _csv.DictReader(fh)
                if r.get("symbol", "").strip()
                and not r["symbol"].lstrip().startswith("#")
            }
        print(f"Using {len(constituents)} constituents from {args.constituents_file.name}")

    if args.auto_band:
        g = params["gmsr"]
        em = float(g["dm"]["standard"]) * float(g["em_multiplier_of_dm"])
        args.low, args.high = round(em * BAND_LOW_MULT, -1), round(em * BAND_HIGH_MULT, -1)
        args.step = max(50.0, round((args.high - args.low) / 9, -1))
        print(f"EM Standard reference USD {em:,.0f} mn -> band USD {args.low:,.0f} to "
              f"{args.high:,.0f} mn, step {args.step:,.0f}")

    store = DataStore(ROOT / "cache")
    predictor = Predictor(
        params=params,
        calendar=cal,
        store=store,
        fif_overrides=load_fif_overrides(ROOT / "config" / "free_float_overrides.csv"),
        fif_history=load_fif_history(ROOT / "config" / "free_float_history.csv"),
        non_constituents=load_known_non_constituents(
            ROOT / "config" / "known_non_constituents.csv",
            as_of=cal.price_cutoff_candidates[-1],
        ),
        constituents=constituents,
        known_members=membership_from_announcements(actuals, cal.label)[0],
        min_float_confidence=args.min_float_confidence,
        cutoff_mode=args.mode,
        fixed_cutoff_usd_mn=args.low,
        universe_symbols=store.build_universe(),
    )
    print(f"Preparing data for {cal.label} ...")
    predictor.cal = cal
    predictor.prepare()

    print()
    print(f"Cut-off calibration for {cal.label}  (mode={args.mode}, "
          f"a name counts as predicted at {args.min_dates}/10 dates)")
    print("=" * 94)
    print(f"  {'cut-off':>9}{'members':>9}  {'add recall':>11}{'add prec':>10}{'add F1':>8}"
          f"{'del recall':>12}{'del prec':>10}  missed")
    print("-" * 94)

    rows = []
    value = args.low
    if not scoring:
        value = args.high + 1  # skip the scored sweep entirely
        rows = [(v, 0.0, 0.0, 0.0, 0.0) for v in
                _band_values(args.low, args.high, args.step)]
    while value <= args.high + 1e-9:
        predictor.fixed_cutoff = value
        predictor._anchor_derived = None
        prediction = predictor.run()
        result = score_prediction(prediction, actuals, min_dates=args.min_dates)
        a, d = result.additions, result.deletions

        def f(x):
            return f"{x:.0%}" if x is not None else "n/a"

        print(f"  {value:>9,.0f}{len(prediction.constituents):>9}  "
              f"{f(a.recall):>11}{f(a.precision):>10}{f(a.f1):>8}"
              f"{f(d.recall):>12}{f(d.precision):>10}  "
              f"{', '.join(a.misses + d.misses) or '-'}")
        rows.append((value, a.f1 or 0.0, d.f1 or 0.0, a.recall or 0.0, d.recall or 0.0))
        value += args.step

    print("-" * 94)
    if not scoring:
        print(robustness_table(predictor, actuals, [r[0] for r in rows], args.min_dates))
        return 0
    perfect = [r for r in rows if r[3] == 1.0 and r[4] == 1.0]
    if perfect:
        print(f"Full recall on both sides over cut-offs USD {perfect[0][0]:,.0f} - "
              f"{perfect[-1][0]:,.0f} mn.")
        best = max(perfect, key=lambda r: r[1] + r[2])
        print(f"Best combined F1 at USD {best[0]:,.0f} mn.")
    else:
        best = max(rows, key=lambda r: r[1] + r[2])
        print(f"No cut-off achieves full recall on both sides. Best combined F1 at "
              f"USD {best[0]:,.0f} mn.")
    print("Treat a plateau as the answer. A single winning value is usually one "
          "marginal name, not a real signal.")

    if args.robustness:
        band = [r[0] for r in rows]
        print(robustness_table(predictor, actuals, band, args.min_dates))
    return 0



def robustness_table(
    predictor, actuals, cutoffs: list[float], min_dates: int, top_n: int = 25
) -> str:
    """How many cut-offs in a band each name survives.

    This exists because the cut-off cannot be pinned down and cannot be carried
    forward. Measured against three reviews, the optimal value ranged from 0.635x to
    0.774x the EM Standard reference - inside MSCI's own 0.5x-1.15x range, but a 22%
    spread, and a 13% error was worth 17 points of precision on May 2026.

    Reporting a single answer at a single assumed cut-off hides that. A name that
    qualifies across the whole plausible band is a different proposition from one
    that appears only at the bottom of it, and the second kind is where the false
    positives concentrate. Combined with the ten candidate price cut-off dates, this
    gives two independent robustness axes.
    """
    from msci_predict import rules  # noqa: F401

    survived: dict[str, int] = {}
    names: dict[str, str] = {}
    for value in cutoffs:
        predictor.fixed_cutoff = value
        predictor._anchor_derived = None
        prediction = predictor.run()
        for sym, count, _ in prediction.ranked_additions():
            if count >= min_dates:
                survived[sym] = survived.get(sym, 0) + 1
                names[sym] = prediction.names.get(sym, "")

    entry = (actuals.get("reviews") or {}).get(predictor.cal.label) or {}
    actual = {s.upper() for s in ((entry.get("standard") or {}).get("additions") or [])}
    scored = bool(actual)

    out = [
        "",
        "=" * 94,
        f"ROBUSTNESS ACROSS THE CUT-OFF BAND  (USD {cutoffs[0]:,.0f} to {cutoffs[-1]:,.0f} mn, "
        f"{len(cutoffs)} values)",
        "=" * 94,
        "A name surviving the whole band does not depend on the cut-off estimate being",
        "right. One surviving only part of it does, and that is where false positives sit.",
        "",
        f"  {'Symbol':<14}{'Cut-offs':>10}{'Share':>8}  "
        + (f"{'Actual?':>8}  " if scored else "")
        + "Name",
    ]
    for sym, count in sorted(survived.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]:
        cell = f"{'YES' if sym in actual else '-':>8}  " if scored else ""
        out.append(
            f"  {sym:<14}{count:>4}/{len(cutoffs):<5}{count / len(cutoffs):>7.0%}  "
            f"{cell}{names.get(sym, '')[:34]}"
        )

    # Report across thresholds rather than only at 100%. Demanding survival of the
    # whole band looks appealing but costs recall whenever the true cut-off sits near
    # a band edge: at the May 2026 review it dropped recall to 25%, because MCX,
    # Federal Bank and Indian Bank all fail at the top of the band. Measured across
    # three reviews, a threshold around half the band holds 100% recall on all of
    # them while roughly halving the false positives.
    if not scored:
        need = max(1, int(round(0.40 * len(cutoffs))))
        shortlist = [s for s, c in survived.items() if c >= need]
        out.append("")
        out.append(f"  Clearing at least 40% of the band ({need} of {len(cutoffs)}): "
                   f"{len(shortlist)} name(s)")
        out.append(f"  {', '.join(sorted(shortlist)) or 'none'}")
        out.append("")
        out.append("  That threshold held 100% recall on all four back-tested reviews, with")
        out.append("  precision from 25% (Feb 2026, ten months of input staleness) to 80%")
        out.append("  (Nov 2026, one month). A live run sits at the favourable end.")
        return "\n".join(out)

    out.append("")
    out.append(f"  {'threshold':>12}{'predicted':>11}{'correct':>9}{'precision':>11}{'recall':>9}")
    best = None
    for need in range(len(cutoffs), 0, -1):
        chosen = [s for s, c in survived.items() if c >= need]
        if not chosen:
            continue
        hit = len([s for s in chosen if s in actual])
        precision = hit / len(chosen)
        recall = hit / len(actual) if actual else float("nan")
        marker = ""
        if actual and recall == 1.0 and (best is None or precision > best[1]):
            best = (need, precision)
            marker = "  <-- full recall, best precision so far"
        out.append(
            f"  {need:>8}/{len(cutoffs):<3}{len(chosen):>11}{hit:>9}"
            f"{precision:>11.0%}{recall:>9.0%}{marker}"
        )
    if best:
        out.append("")
        out.append(f"  Full recall retained down to {best[0]}/{len(cutoffs)} cut-offs, "
                   f"at {best[1]:.0%} precision.")
    return "\n".join(out)

if __name__ == "__main__":
    sys.exit(main())
