"""Fast end-to-end check on a handful of symbols.

Runs the whole pipeline against the five names MSCI actually moved in November 2026
plus a few obvious incumbents, with the cut-off pinned so the result does not depend
on having the full universe loaded. Catches wiring errors in seconds instead of
after a half-hour universe fetch.
"""

import datetime as dt
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from msci_predict import report
from msci_predict.calendar_rules import build_review_calendar
from msci_predict.data import DataStore
from msci_predict.pipeline import Predictor, load_parameters

ROOT = Path(__file__).resolve().parents[1]

SYMBOLS = [
    # The November 2026 Standard additions and the one deletion.
    "COFORGE", "LICI", "ADANIGREEN", "MEESHO", "PIIND",
    # Names the sell side flagged but MSCI left alone.
    "SWIGGY", "GLENMARK", "SONACOMS", "MAXHEALTH",
    # Unambiguous incumbents, to check the deletion screen does not fire on them.
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "BHARTIARTL",
]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    params = load_parameters(ROOT / "config" / "parameters.yaml")
    cal = build_review_calendar(
        2026, 11,
        effective_date=dt.date(2026, 11, 30),
        announcement_date=dt.date(2026, 11, 11),
    )
    store = DataStore(ROOT / "cache_smoke")
    predictor = Predictor(
        params=params,
        calendar=cal,
        store=store,
        cutoff_mode="fixed",
        fixed_cutoff_usd_mn=5200.0,
        constituents={"RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "BHARTIARTL", "PIIND"},
        universe_symbols=SYMBOLS,
    )
    predictor.prepare()
    prediction = predictor.run()
    print(report.summary(prediction))
    for sym in ("LICI", "MEESHO", "PIIND"):
        print(report.explain(prediction, sym))
    return 0


if __name__ == "__main__":
    sys.exit(main())
