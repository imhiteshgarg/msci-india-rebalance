#!/usr/bin/env python
"""Phase 2 — Daily watch. Run every weekday from 18 to 29 January.

A thin wrapper around daily_watch.py that adds the standard flags and prints
a reminder of what the two columns mean. The underlying script appends to
out/<review>/daily_watch.csv and refuses to double-count a day, so running
it multiple times on the same day is safe.

The window opens on 18 January: running before that date stops with an
explanation. Run after the close each evening.

    .venv/bin/python scripts/phase2_daily.py
    .venv/bin/python scripts/phase2_daily.py --review 2027-02
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--review", default="2027-02")
    ap.add_argument("--min-days", type=int, default=5,
                    help="Minimum run-days a name must appear on to be listed (default: 5)")
    ap.add_argument("--constituents-file", type=Path,
                    default=ROOT / "config" / "msci_india_constituents.csv")
    args = ap.parse_args()

    print(f"\nPhase 2 — daily watch for {args.review}")
    print("Reading two columns together:")
    print("  Days   = how many separate run-days the name appeared (persistence across prices)")
    print("  Latest = how many of the 10 candidate dates it cleared in today's run")
    print("           ↑ denominator grows through the window; nothing exceeds 3/10 on 20 Jan")
    print()

    result = subprocess.run([
        sys.executable, "scripts/daily_watch.py",
        "--review", args.review,
        "--refresh-prices",
        "--min-days", str(args.min_days),
        "--constituents-file", str(args.constituents_file),
    ])
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
