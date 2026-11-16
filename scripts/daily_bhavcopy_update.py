#!/usr/bin/env python
"""Keep the committed bhavcopy day-file store current, one trading day at a time.

Designed to run from a daily GitHub Action (see .github/workflows/daily-bhavcopy.yml),
but it is an ordinary script and runs locally the same way.

WHAT IT DOES
------------
Fetches every trading day from the last one already in the cache up to yesterday
(IST), writing each new day as cache/bhavcopy/YYYY-MM-DD.csv. This "since the last
cached day" range is deliberate: it is self-healing. A skipped run, a GitHub outage
or an NSE bad day leaves a gap that the next run simply fills, rather than fetching
only a rigid "yesterday" and leaving the hole forever.

Each date is classified and appended as one row to the log
(logs/daily_bhavcopy_update.csv), so a `git pull` gives the full history of what the
job saw:

  updated         a new trading-day file was written
  already-present the day was already cached (idempotent re-run)
  holiday         NSE had no file for a weekday (a 404, or the previous session's
                  file served under this date - both mean the market was shut)
  error           the fetch failed after retries; this is the only outcome that
                  fails the job, so GitHub's built-in failure email is sent

Weekends are skipped silently.

WHY IT ONLY TOUCHES DAY FILES
-----------------------------
The compiled panels (prices.csv etc.) are NOT rebuilt here. They are large and churn
on every append, so committing them daily would bloat history. The day files are the
durable store; rebuild the panels on demand with scripts/hydrate_panels.py.

    .venv/bin/python scripts/daily_bhavcopy_update.py
    .venv/bin/python scripts/daily_bhavcopy_update.py --since 2026-11-09   # backfill/test
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
IST = timezone(timedelta(hours=5, minutes=30))
LOG_FIELDS = ["run_utc", "date", "weekday", "outcome", "detail"]
# When the store is empty and no --since is given, seed this many days back so a
# first run does something useful instead of erroring.
EMPTY_SEED_DAYS = 10


def _ist_yesterday() -> date:
    """Yesterday in IST.

    Computed in IST explicitly rather than from the runner's clock: GitHub runners
    are on UTC, and "yesterday" at the wrong end of the day would be off by one.
    Yesterday's end-of-day file is always published by the time any morning-IST run
    fires, so it is the newest date that is guaranteed to exist.
    """
    return datetime.now(IST).date() - timedelta(days=1)


def _last_cached_day(day_dir: Path) -> date | None:
    days = sorted(
        p.stem for p in day_dir.glob("*.csv") if not p.stem.startswith("_")
    )
    return date.fromisoformat(days[-1]) if days else None


def _daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--cache-dir", type=Path, default=ROOT / "cache")
    ap.add_argument("--log", type=Path, default=ROOT / "logs" / "daily_bhavcopy_update.csv")
    ap.add_argument("--since", type=date.fromisoformat, default=None,
                    help="Override the start date (YYYY-MM-DD). Default: day after the "
                         "last cached file. Useful for backfilling a gap or testing.")
    ap.add_argument("--until", type=date.fromisoformat, default=None,
                    help="Override the end date (YYYY-MM-DD). Default: yesterday (IST).")
    args = ap.parse_args()

    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    log = logging.getLogger("daily_update")

    from msci_predict.bhavcopy import BhavcopySource

    day_dir = args.cache_dir / "bhavcopy"
    day_dir.mkdir(parents=True, exist_ok=True)

    end = args.until or _ist_yesterday()
    if args.since is not None:
        start = args.since
    else:
        last = _last_cached_day(day_dir)
        if last is not None:
            start = last + timedelta(days=1)
        else:
            start = end - timedelta(days=EMPTY_SEED_DAYS)
            log.warning("No day files found; seeding from %s. Run a full backfill if "
                        "you want the whole history.", start)

    if start > end:
        print(f"Nothing to do: last cached day is current (next needed {start}, "
              f"newest available {end}).")
        _append_log(args.log, [(_now(), end.isoformat(),
                                end.strftime("%a"), "up-to-date", "no new trading days")])
        return 0

    print(f"Fetching {start} .. {end} (newest available: {end})")
    source = BhavcopySource(args.cache_dir)

    rows: list[tuple[str, str, str, str, str]] = []
    counts = {"updated": 0, "already-present": 0, "holiday": 0, "error": 0, "weekend": 0}
    now = _now()
    for d in _daterange(start, end):
        weekday = d.strftime("%a")
        if d.weekday() >= 5:
            counts["weekend"] += 1
            continue

        had_file = (day_dir / f"{d.isoformat()}.csv").exists()
        data = source.fetch_day(d)

        if data is not None:
            outcome = "already-present" if had_file else "updated"
            detail = f"{len(data.closes)} equity symbols"
        elif d.isoformat() in source._holidays:
            outcome = "holiday"
            detail = "no file served (exchange holiday)"
        else:
            outcome = "error"
            detail = "fetch failed after retries"

        counts[outcome] += 1
        rows.append((now, d.isoformat(), weekday, outcome, detail))
        print(f"  {d.isoformat()} ({weekday})  {outcome:<15} {detail}")

    if not rows:
        # Every run leaves at least one row, even one that found only weekends, so a
        # `git pull` always shows what the latest run did rather than silently
        # nothing. (The start > end branch above logs its own up-to-date row.)
        rows.append((now, end.isoformat(), end.strftime("%a"), "up-to-date",
                     f"range {start}..{end}: {counts['weekend']} weekend, no trading day"))

    _append_log(args.log, rows)

    print()
    print(f"Summary {start}..{end}: "
          f"{counts['updated']} updated, {counts['already-present']} already present, "
          f"{counts['holiday']} holiday, {counts['weekend']} weekend, "
          f"{counts['error']} error")
    if counts["error"]:
        print(f"FAILED: {counts['error']} day(s) could not be fetched. The cache is "
              "left as far as it got; the next run retries the gap.")
        return 1
    if counts["updated"]:
        print(f"OK: wrote {counts['updated']} new day file(s).")
    else:
        print("OK: nothing new to write.")
    return 0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_log(path: Path, rows: list[tuple[str, str, str, str, str]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with open(path, "a", newline="") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(LOG_FIELDS)
        w.writerows(rows)


if __name__ == "__main__":
    sys.exit(main())
