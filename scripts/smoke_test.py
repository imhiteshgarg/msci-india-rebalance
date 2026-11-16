"""Fast end-to-end check on a handful of symbols.

Runs the whole pipeline against the five names MSCI actually moved in November 2026
plus a few obvious incumbents, with the cut-off pinned so the result does not depend
on having the full universe loaded. Catches wiring errors in seconds instead of
after a half-hour universe fetch.

The defaults reproduce the November 2026 review, which is the reference outcome the
rest of the repo is calibrated against. Every one of them can be overridden, so the
check can be pointed at a later review without editing this file:

    .venv/bin/python scripts/smoke_test.py
    .venv/bin/python scripts/smoke_test.py --review 2027-02 --cutoff-usd-mn 5600 \
        --symbols RELIANCE,TCS,HDFCBANK --explain RELIANCE

The cut-off mode is deliberately not exposed. A fifteen-symbol universe cannot
derive a size-segment cut-off from free-float coverage, so it has to be pinned.
"""

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from msci_predict import report
from msci_predict.backtest import load_actuals
from msci_predict.calendar_rules import build_review_calendar, parse_review_label
from msci_predict.data import DataStore
from msci_predict.pipeline import Predictor, load_parameters

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_REVIEW = "2026-11"
DEFAULT_CUTOFF_USD_MN = 5200.0

SYMBOLS = [
    # The November 2026 Standard additions and the one deletion.
    "COFORGE", "LICI", "ADANIGREEN", "MEESHO", "PIIND",
    # Names the sell side flagged but MSCI left alone.
    "SWIGGY", "GLENMARK", "SONACOMS", "MAXHEALTH",
    # Unambiguous incumbents, to check the deletion screen does not fire on them.
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "BHARTIARTL",
]

CONSTITUENTS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "BHARTIARTL", "PIIND",
]

EXPLAIN = ["LICI", "MEESHO", "PIIND"]


def _csv_list(raw: str, fallback: list[str]) -> list[str]:
    values = [s.strip().upper() for s in raw.split(",") if s.strip()]
    return values or list(fallback)


def _review_dates(actuals: dict, label: str) -> tuple[dt.date | None, dt.date | None]:
    """Effective and announcement dates for a review, from config/actual_reviews.yaml.

    The effective date is not cosmetic: it drives the preponement footnote to GIMI
    3.1.9, so hardcoding it here would silently diverge from the ground-truth file
    the back-test scores against.
    """
    entry = (actuals.get("reviews") or {}).get(label) or {}

    def _as_date(value) -> dt.date | None:
        if isinstance(value, dt.datetime):
            return value.date()
        if isinstance(value, dt.date):
            return value
        if isinstance(value, str):
            return dt.datetime.fromisoformat(value).date()
        return None

    return _as_date(entry.get("effective_date")), _as_date(entry.get("announcement_date"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--review", default=DEFAULT_REVIEW,
                    help=f"Review to screen, e.g. 2026-11 or Nov2026 (default: {DEFAULT_REVIEW})")
    ap.add_argument("--cutoff-usd-mn", type=float, default=DEFAULT_CUTOFF_USD_MN,
                    help="Pinned Standard size-segment cut-off in USD millions "
                         f"(default: {DEFAULT_CUTOFF_USD_MN:,.0f})")
    ap.add_argument("--symbols", default="",
                    help="Comma-separated universe. Defaults to the November 2026 fixture.")
    ap.add_argument("--constituents", default="",
                    help="Comma-separated names to treat as existing index members")
    ap.add_argument("--explain", default="",
                    help="Comma-separated symbols to trace test by test")
    ap.add_argument("--params", type=Path, default=ROOT / "config" / "parameters.yaml")
    ap.add_argument("--actuals", type=Path, default=ROOT / "config" / "actual_reviews.yaml")
    ap.add_argument("--cache-dir", type=Path, default=ROOT / "cache_smoke")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    symbols = _csv_list(args.symbols, SYMBOLS)
    constituents = _csv_list(args.constituents, CONSTITUENTS)
    explain = _csv_list(args.explain, EXPLAIN)

    params = load_parameters(args.params)
    year, month = parse_review_label(args.review)
    actuals = load_actuals(args.actuals) if args.actuals.exists() else {}
    effective, announced = _review_dates(actuals, f"{year}-{month:02d}")
    cal = build_review_calendar(
        year, month, effective_date=effective, announcement_date=announced
    )

    store = DataStore(args.cache_dir)
    predictor = Predictor(
        params=params,
        calendar=cal,
        store=store,
        cutoff_mode="fixed",
        fixed_cutoff_usd_mn=args.cutoff_usd_mn,
        constituents=set(constituents),
        universe_symbols=symbols,
    )
    predictor.prepare()
    prediction = predictor.run()
    print(report.summary(prediction))
    for sym in explain:
        print(report.explain(prediction, sym))
    return 0


if __name__ == "__main__":
    sys.exit(main())
