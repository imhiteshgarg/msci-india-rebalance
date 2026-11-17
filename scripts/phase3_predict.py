#!/usr/bin/env python
"""Phase 3 — Authoritative prediction. Run after the close on 29 January.

Runs the headline prediction and the cut-off band robustness sweep, then
prints a combined guide to reading the output.

This is the only point at which all ten candidate price cut-off dates exist.
MSCI does not announce until ~9 February, so the forecast has about a week
to spare. The --refresh flag fetches fresh share counts (20–30 min); skip it
only if you ran it very recently.

    .venv/bin/python scripts/phase3_predict.py
    .venv/bin/python scripts/phase3_predict.py --review 2027-02 --no-refresh
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(args: list[str], label: str) -> None:
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}\n")
    result = subprocess.run([sys.executable] + args)
    if result.returncode != 0:
        raise SystemExit(f"\n✗ '{label}' failed (exit {result.returncode}). "
                         "Fix the issue above and re-run.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--review", default="2027-02")
    ap.add_argument("--cutoff-usd-mn", type=float, default=None,
                    help="Size cut-off. Reads from out/<review>/phase1_state.json if not set.")
    ap.add_argument("--constituents-file", type=Path,
                    default=ROOT / "config" / "msci_india_constituents.csv")
    ap.add_argument("--min-float-confidence",
                    choices=["LOW", "MEDIUM", "HIGH"], default="MEDIUM")
    ap.add_argument("--min-dates", type=int, default=6,
                    help="Minimum of 10 candidate dates a name must clear (default: 6)")
    ap.add_argument("--no-refresh", action="store_true",
                    help="Skip --refresh (use cached share counts; faster but less current)")
    args = ap.parse_args()

    refresh = [] if args.no_refresh else ["--refresh"]

    # Read calibrated cut-off from Phase 1 state file if not explicitly set.
    cutoff = args.cutoff_usd_mn
    if cutoff is None:
        import json as _json
        state_path = ROOT / "out" / args.review.replace("/", "-") / "phase1_state.json"
        if state_path.exists():
            state = _json.loads(state_path.read_text())
            cutoff = float(state.get("cutoff_usd_mn", 5600.0))
            print(f"\nUsing cut-off USD {cutoff:,.0f} mn from Phase 1 state ({state_path.name})")
        else:
            cutoff = 5600.0
            print(f"\nPhase 1 state not found; using default cut-off USD {cutoff:,.0f} mn")

    print(f"\nPhase 3 — authoritative prediction for {args.review}")

    # --- Run 1: headline prediction ---
    _run(
        ["run_prediction.py",
         "--review", args.review,
         "--cutoff-usd-mn", str(cutoff),
         "--constituents-file", str(args.constituents_file),
         "--min-float-confidence", args.min_float_confidence,
         "--min-dates", str(args.min_dates),
         ] + refresh,
        f"Headline prediction ({args.review})",
    )

    # --- Run 2: cut-off band robustness ---
    _run(
        ["scripts/calibrate_cutoff.py",
         "--review", args.review,
         "--cutoff-usd-mn", str(cutoff),
         "--constituents-file", str(args.constituents_file),
         "--min-float-confidence", args.min_float_confidence,
         "--min-dates", str(args.min_dates),
         "--auto-band", "--robustness"],
        "Cut-off band robustness",
    )

    out_dir = ROOT / "out" / args.review
    print(f"\n{'='*70}")
    print("  Phase 3 complete.")
    print(f"{'='*70}")
    print(f"\nResults written to {out_dir}/")
    print("  summary.txt       — the prediction table")
    print("  candidates.csv    — every screened name with per-test outcomes")
    print("  cutoff_by_date.csv — how the cut-off moved across the ten dates")
    print()
    print("How to read the output:")
    print("  Act on names that clear BOTH conditions:")
    print(f"    1. ≥{args.min_dates} of 10 candidate price cut-off dates (from Run 1)")
    print(f"    2. ≥40% of the cut-off band (from Run 2: 'Full recall retained' line)")
    print()
    print("  FLAGGED  — passed all MSCI rules but float estimate untrustworthy.")
    print("             Check manually; could be a corporate spinoff or recent IPO.")
    print("  NEAR MISSES — only size-limited; listed with how much price move is needed.")
    print()
    print("Next step (after MSCI announces ~10 Feb):")
    print(f"  .venv/bin/python scripts/phase4_score.py --review {args.review}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
