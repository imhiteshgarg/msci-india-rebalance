"""Run the screen daily and track how persistently each name qualifies.

There are two different kinds of confidence here and conflating them will mislead you.

  Within-run confidence (the x/10 column) counts how many of MSCI's ten candidate
  price cut-off dates a stock clears. MSCI picks one of those ten at random and never
  says which, so this measures that specific uncertainty. Early in the window it is
  out of three or four rather than ten, because the later dates have not happened yet.

  Across-run persistence (what this script adds) counts how many separate days a name
  has shown up as you re-run. It measures something else entirely: whether the call is
  stable as prices move and the window fills, or whether it is a name hovering on the
  boundary that flickers in and out.

A name you would act on scores highly on both. One that appears on nine run-days but
only ever at 2/10 within-run is clearing a couple of early dates and failing the rest,
which is not the same thing at all.

Usage, once per day from mid-January:

    .venv/bin/python scripts/daily_watch.py --review 2027-02 --refresh-prices

History accumulates in out/<review>/daily_watch.csv, so the tally survives restarts.
Report alone, without re-running the screen:

    .venv/bin/python scripts/daily_watch.py --review 2027-02 --report-only
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from msci_predict.backtest import load_actuals, membership_from_announcements
from msci_predict.calendar_rules import build_review_calendar, parse_review_label
from msci_predict.data import (
    DataStore,
    load_fif_history,
    load_fif_overrides,
    load_known_non_constituents,
)
from msci_predict.pipeline import Predictor, load_parameters

ROOT = Path(__file__).resolve().parents[1]

# Centre of the USD 5,200-6,000 mn plateau over which the November 2026 review is
# reproduced in full. Re-fit each quarter: the MSCI size references moved 5-7%
# between the August and November 2026 methodology editions.
DEFAULT_CUTOFF_USD_MN = 5600.0

FIELDS = [
    "run_date", "review", "side", "symbol", "name", "dates_passed", "dates_screened",
    "cutoff_usd_mn", "full_mcap_usd_mn", "ffmc_usd_mn", "fif",
    "size_multiple", "ffmc_multiple", "path",
]


def _load_constituents(path: Path | None) -> set[str] | None:
    if not path or not path.exists():
        return None
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    return {
        r["symbol"].strip().upper()
        for r in rows
        if r.get("symbol", "").strip() and not r["symbol"].lstrip().startswith("#")
    } or None


def append_history(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)


def read_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def report(history: list[dict], side: str, min_days: int) -> str:
    """Tally appearances across run-days for one side of the review."""
    rows = [r for r in history if r["side"] == side]
    if not rows:
        return f"  no {side} recorded yet"

    all_days = sorted({r["run_date"] for r in history})
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_symbol[r["symbol"]].append(r)

    out = [
        f"  {len(all_days)} run-day(s) recorded: {all_days[0]} .. {all_days[-1]}",
        "",
        f"  {'Symbol':<14}{'Days':>6}{'Latest':>9}{'Best':>7}  "
        f"{'Mcap/cut':>9}{'FIF':>7}  Name",
    ]
    ranked = []
    for sym, entries in by_symbol.items():
        entries.sort(key=lambda r: r["run_date"])
        last = entries[-1]
        latest = f"{last['dates_passed']}/{last['dates_screened']}"
        best = max(int(r["dates_passed"]) for r in entries)
        ranked.append((len(entries), best, sym, latest, last))
    ranked.sort(key=lambda t: (-t[0], -t[1]))

    for days, best, sym, latest, last in ranked:
        flag = " <-- meets threshold" if days >= min_days else ""
        out.append(
            f"  {sym:<14}{days:>6}{latest:>9}{best:>7}  "
            f"{float(last['size_multiple']):>9.2f}{float(last['fif']):>7.3f}  "
            f"{last['name'][:26]}{flag}"
        )

    qualifying = [sym for days, _, sym, _, _ in ranked if days >= min_days]
    out.append("")
    out.append(
        f"  Appearing on at least {min_days} run-day(s): "
        + (", ".join(qualifying) if qualifying else "none")
    )
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--review", default="2027-02")
    ap.add_argument("--min-days", type=int, default=5,
                    help="Run-days a name must appear on to be highlighted (default 5)")
    ap.add_argument("--cutoff-usd-mn", type=float, default=None,
                    help="Overrides the default in run_prediction.py; re-calibrate each quarter")
    ap.add_argument("--constituents-file", type=Path,
                    default=ROOT / "config" / "msci_india_constituents.csv")
    ap.add_argument("--refresh-prices", action="store_true",
                    help="Refetch prices and share counts. Needed daily during the window, "
                         "since yesterday's close is new data.")
    ap.add_argument("--report-only", action="store_true",
                    help="Print the accumulated tally without re-running the screen")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "out")
    ap.add_argument("--cache-dir", type=Path, default=ROOT / "cache")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    year, month = parse_review_label(args.review)
    label = f"{year}-{month:02d}"
    history_path = args.out_dir / label / "daily_watch.csv"

    if not args.report_only:
        params = load_parameters(ROOT / "config" / "parameters.yaml")
        actuals = load_actuals(ROOT / "config" / "actual_reviews.yaml")
        entry = (actuals.get("reviews") or {}).get(label) or {}

        def _as_date(v):
            if isinstance(v, date):
                return v
            return datetime.fromisoformat(v).date() if isinstance(v, str) else None

        cal = build_review_calendar(
            year, month,
            effective_date=_as_date(entry.get("effective_date")),
            announcement_date=_as_date(entry.get("announcement_date")),
        )
        store = DataStore(args.cache_dir)
        known_members, _ = membership_from_announcements(actuals, label)

        predictor = Predictor(
            params=params,
            calendar=cal,
            store=store,
            fif_overrides=load_fif_overrides(ROOT / "config" / "free_float_overrides.csv"),
            fif_history=load_fif_history(ROOT / "config" / "free_float_history.csv"),
            constituents=_load_constituents(args.constituents_file),
            non_constituents=load_known_non_constituents(
                ROOT / "config" / "known_non_constituents.csv",
                as_of=cal.price_cutoff_candidates[-1],
            ),
            known_members=known_members,
            cutoff_mode="calibrated",
            # Keep this in step with run_prediction.py's default and re-calibrate
            # every quarter with scripts/calibrate_cutoff.py.
            fixed_cutoff_usd_mn=args.cutoff_usd_mn or DEFAULT_CUTOFF_USD_MN,
            universe_symbols=store.build_universe(refresh=args.refresh_prices),
        )
        predictor.prepare(refresh=args.refresh_prices)
        prediction = predictor.run()

        run_date = date.today().isoformat()
        existing = {r["run_date"] for r in read_history(history_path)}
        if run_date in existing:
            logging.warning(
                "A run for %s is already recorded; skipping the append so one day "
                "cannot be counted twice. Delete the row to redo it.", run_date
            )
        else:
            rows = []
            for side, ranked in (
                ("addition", prediction.ranked_additions()),
                ("deletion", prediction.ranked_deletions()),
            ):
                for sym, count, _ in ranked:
                    v = next(
                        (r.all_verdicts[sym] for r in prediction.date_results
                         if sym in r.all_verdicts), None
                    )
                    if v is None:
                        continue
                    rows.append({
                        "run_date": run_date,
                        "review": label,
                        "side": side,
                        "symbol": sym,
                        "name": prediction.names.get(sym, ""),
                        "dates_passed": count,
                        "dates_screened": prediction.n_dates,
                        "cutoff_usd_mn": round(prediction.cutoff_mean, 1),
                        "full_mcap_usd_mn": round(v.full_mcap_usd_mn, 1),
                        "ffmc_usd_mn": round(v.ffmc_usd_mn, 1),
                        "fif": round(v.fif, 4),
                        "size_multiple": round(v.size_multiple, 4),
                        "ffmc_multiple": round(v.ffmc_multiple, 4),
                        "path": v.path,
                    })
            append_history(history_path, rows)
            logging.info("Recorded %d rows for %s in %s", len(rows), run_date, history_path)

    history = read_history(history_path)
    if not history:
        print("No history yet. Run without --report-only first.")
        return 0

    print()
    print("=" * 88)
    print(f"DAILY WATCH - {label}")
    print("=" * 88)
    print("Days   = separate run-days this name appeared on (persistence)")
    print("Latest = within-run confidence on the most recent run, out of the candidate")
    print("         price cut-off dates that existed that day")
    print("Best   = highest within-run count seen on any run-day")
    print()
    print("A name worth acting on scores on both. High Days with a low Latest means it")
    print("clears only the early dates in the window and fails the rest.")
    for side in ("addition", "deletion"):
        print()
        print("-" * 88)
        print(f"{side.upper()}S")
        print("-" * 88)
        print(report(history, side, args.min_days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
