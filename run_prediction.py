#!/usr/bin/env python
"""Predict MSCI India Standard Index additions and deletions for an Index Review.

Examples
--------
Predict the November 2026 review from the last ten business days of October 2026
and score the result against what MSCI actually announced on 11 November:

    .venv/bin/python run_prediction.py --review 2026-11 --backtest

Calibrate the size cut-off to a sell-side estimate instead of deriving it:

    .venv/bin/python run_prediction.py --review 2026-11 --cutoff-mode fixed \
        --cutoff-usd-mn 5200 --backtest

Show every screen for one name:

    .venv/bin/python run_prediction.py --review 2026-11 --explain LICI
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

from msci_predict import report
from msci_predict.backtest import (
    load_actuals,
    membership_from_announcements,
    score_prediction,
    sweep_confidence,
)
from msci_predict.calendar_rules import build_review_calendar, parse_review_label
from msci_predict.data import (
    DataStore,
    load_fif_history,
    load_fif_overrides,
    load_known_non_constituents,
)
from msci_predict.pipeline import Predictor, load_parameters

ROOT = Path(__file__).resolve().parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="MSCI India Standard Index rebalance predictor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--review", default="2026-11",
                   help="Review to predict, e.g. 2026-11 or Nov2026 (default: 2026-11)")
    p.add_argument("--params", type=Path, default=ROOT / "config" / "parameters.yaml")
    p.add_argument("--actuals", type=Path, default=ROOT / "config" / "actual_reviews.yaml")
    p.add_argument("--cache-dir", type=Path, default=ROOT / "cache")
    p.add_argument("--out-dir", type=Path, default=ROOT / "out")

    p.add_argument("--cutoff-mode", choices=["derive", "fixed", "calibrated"],
                   default="calibrated",
                   help="derive: compute the 85%% free-float coverage cut-off from the "
                        "universe each date. fixed: hold --cutoff-usd-mn constant. "
                        "calibrated: take the level from --cutoff-usd-mn and the "
                        "variation over time from the derived series (default).")
    p.add_argument("--cutoff-usd-mn", type=float, default=5600.0,
                   help="Standard size-segment cut-off in USD millions, for fixed and "
                        "calibrated modes. The default is the centre of the USD 5,200-6,000 mn "
                        "plateau over which scripts/calibrate_cutoff.py reproduces the November "
                        "2026 review in full; it is also consistent with Centrum's published "
                        "INR 23,197 crore free-float boundary. Re-calibrate each review.")

    p.add_argument("--constituents-file", type=Path, default=None,
                   help="CSV with a 'symbol' column listing current India Standard members. "
                        "Without this the incumbent set is reconstructed from the prior review.")
    p.add_argument("--non-constituents-file", type=Path,
                   default=ROOT / "config" / "known_non_constituents.csv",
                   help="CSV with a 'symbol' column of names known NOT to be index members. "
                        "Subtracted from the reconstructed set. Cheaper to maintain than a "
                        "full constituent list and fixes the error that matters most.")
    p.add_argument("--fif-overrides", type=Path, default=ROOT / "config" / "free_float_overrides.csv",
                   help="CSV of manual free-float corrections (symbol, fif, note)")
    p.add_argument("--fif-history", type=Path, default=ROOT / "config" / "free_float_history.csv",
                   help="CSV of dated free-float changes (symbol, effective_date, fif, note). "
                        "Needed so the historical replay does not apply today's float to "
                        "past reviews.")
    p.add_argument("--extra-symbols", default="",
                   help="Comma-separated NSE symbols to force into the universe")

    p.add_argument("--backtest", action="store_true",
                   help="Score against config/actual_reviews.yaml")
    p.add_argument("--sweep", action="store_true",
                   help="Report accuracy at every confidence threshold")
    p.add_argument("--min-dates", type=int, default=1,
                   help="Candidate dates a name must clear to count as predicted (default: 1)")
    p.add_argument("--explain", default="", help="Comma-separated symbols to trace in full")
    p.add_argument("--trace-membership", default="",
                   help="Comma-separated symbols: show the membership simulation review by "
                        "review. Use this when a known addition is reported as an existing "
                        "member - it localises which input is wrong.")

    p.add_argument("--refresh", action="store_true", help="Ignore caches and refetch")
    p.add_argument("--offline", action="store_true", help="Fail rather than hit the network")
    p.add_argument("--min-float-confidence", choices=["LOW", "MEDIUM", "HIGH"],
                   default="LOW",
                   help="Withhold predictions whose free-float estimate falls below this "
                        "confidence and list them separately. LOW (default) reports "
                        "everything; MEDIUM drops recent IPOs still inside their lock-in "
                        "and names where strategic holders dominate the float.")
    p.add_argument("--strategic-gap-file", type=Path,
                   default=ROOT / "config" / "strategic_gap.csv",
                   help="SEBI public holding vs estimated float, from "
                        "scripts/fetch_shareholding_history.py")
    p.add_argument("--price-source", choices=["bhavcopy", "yfinance"], default="bhavcopy",
                   help="bhavcopy (default): NSE end-of-day files, one request per "
                        "trading day, no rate limiting, every security included. "
                        "yfinance: per-ticker, convenient but throttles under load.")
    p.add_argument("--min-price-coverage", type=float, default=0.70,
                   help="Abort if fewer than this fraction of universe symbols have "
                        "price data. Guards against a rate-limited fetch silently "
                        "shrinking the universe and shifting the size cut-off.")
    p.add_argument("--no-epi", action="store_true",
                   help="Disable the Extreme Price Increase screen")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def _load_constituents(path: Path | None) -> set[str] | None:
    if not path:
        return None
    if not path.exists():
        raise SystemExit(f"Constituents file not found: {path}")
    import csv

    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    if rows and "symbol" in rows[0]:
        return {
            r["symbol"].strip().upper()
            for r in rows
            if r.get("symbol", "").strip() and not r["symbol"].lstrip().startswith("#")
        }
    # Tolerate a plain one-symbol-per-line file.
    return {
        line.strip().upper()
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }


def _load_strategic_gap(path: Path | None) -> dict[str, float]:
    """SEBI public holding minus the estimated float, in percentage points.

    A large value means most of what SEBI calls public holding is strategic, so the
    float MSCI would use is a matter of its own classification rather than
    arithmetic. That is where this model is least reliable.
    """
    if not path or not path.exists():
        return {}
    import csv

    out: dict[str, float] = {}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            sym = (row.get("symbol") or "").strip().upper()
            raw = (row.get("strategic_gap_pp") or "").strip()
            if not sym or sym.startswith("#") or not raw:
                continue
            try:
                out[sym] = float(raw)
            except ValueError:
                continue
    return out


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    params = load_parameters(args.params)
    if args.no_epi:
        params["extreme_price_increase"]["enabled"] = False

    year, month = parse_review_label(args.review)
    actuals = load_actuals(args.actuals) if args.actuals.exists() else {}
    entry = (actuals.get("reviews") or {}).get(f"{year}-{month:02d}") or {}

    def _as_date(v) -> date | None:
        if isinstance(v, date):
            return v
        if isinstance(v, str):
            return datetime.fromisoformat(v).date()
        return None

    cal = build_review_calendar(
        year, month,
        effective_date=_as_date(entry.get("effective_date")),
        announcement_date=_as_date(entry.get("announcement_date")),
    )
    logging.info("Review %s (%s): price cut-off candidates %s .. %s",
                 cal.label, cal.review_type,
                 cal.price_cutoff_candidates[0], cal.price_cutoff_candidates[-1])

    # Members MSCI announced in earlier reviews. Uses only announcements that
    # predate this review, so it adds information without adding hindsight.
    known_members, _ = membership_from_announcements(actuals, cal.label)
    if known_members:
        logging.info(
            "Treating %d names as members from earlier announcements: %s",
            len(known_members), ", ".join(sorted(known_members)),
        )

    store = DataStore(args.cache_dir, offline=args.offline, price_source=args.price_source)
    extra = [s for s in args.extra_symbols.split(",") if s.strip()]
    universe = store.build_universe(extra_symbols=extra, refresh=args.refresh)

    predictor = Predictor(
        params=params,
        calendar=cal,
        store=store,
        fif_overrides=load_fif_overrides(args.fif_overrides),
        fif_history=load_fif_history(args.fif_history),
        constituents=_load_constituents(args.constituents_file),
        non_constituents=load_known_non_constituents(
            args.non_constituents_file, as_of=cal.price_cutoff_candidates[-1]
        ),
        known_members=known_members,
        strategic_gap=_load_strategic_gap(args.strategic_gap_file),
        min_float_confidence=args.min_float_confidence,
        cutoff_mode=args.cutoff_mode,
        fixed_cutoff_usd_mn=args.cutoff_usd_mn,
        universe_symbols=universe,
    )
    predictor.min_price_coverage = args.min_price_coverage
    predictor.prepare(refresh=args.refresh)
    prediction = predictor.run()

    print()
    print(report.summary(prediction))

    out_dir = args.out_dir / cal.label
    out_dir.mkdir(parents=True, exist_ok=True)
    report.write_candidates_csv(prediction, out_dir / "candidates.csv")
    report.write_cutoff_csv(prediction, out_dir / "cutoff_by_date.csv")
    (out_dir / "summary.txt").write_text(report.summary(prediction))
    logging.info("Wrote results to %s", out_dir)

    if args.trace_membership:
        print()
        print(predictor.membership_trace(
            [s.strip() for s in args.trace_membership.split(",") if s.strip()]))

    for sym in (s.strip() for s in args.explain.split(",") if s.strip()):
        print()
        print(report.explain(prediction, sym))

    if args.backtest:
        result = score_prediction(prediction, actuals, min_dates=args.min_dates)
        if not result:
            print(f"\nNo recorded outcome for {cal.label} in {args.actuals}; nothing to score.")
        else:
            text = report.backtest_report(result, prediction)
            print()
            print(text)
            (out_dir / "backtest.txt").write_text(text)

    if args.sweep:
        print()
        print("=" * 88)
        print("ACCURACY vs CONFIDENCE THRESHOLD")
        print("=" * 88)
        print(f"  {'min dates':>10}{'add recall':>12}{'add prec':>10}{'add F1':>9}"
              f"{'del recall':>12}{'del prec':>10}")
        for r in sweep_confidence(prediction, actuals):
            a, d = r.additions, r.deletions
            def f(x):
                return f"{x:.0%}" if x is not None else "n/a"
            print(f"  {r.threshold_dates:>7}/{r.n_dates:<2}{f(a.recall):>12}{f(a.precision):>10}"
                  f"{f(a.f1):>9}{f(d.recall):>12}{f(d.precision):>10}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
