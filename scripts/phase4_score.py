#!/usr/bin/env python
"""Phase 4 — Score the prediction. Run after MSCI announces (~10 February).

Steps:
  1. Extract the official additions/deletions from MSCI's published PDF.
  2. Pause so you can paste the printed YAML into config/actual_reviews.yaml.
  3. Score the prediction against the announced outcome.
  4. Derive the constituent snapshot for the next review's membership.

Recording the outcome is not bookkeeping. It feeds: the next review's cut-off
calibration, the known_members set so added names are not nominated again, and
an additional data point for the band/threshold settings.

    .venv/bin/python scripts/phase4_score.py --review 2027-02
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


def _run(args: list[str], label: str) -> None:
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}\n")
    result = subprocess.run([sys.executable] + args)
    if result.returncode != 0:
        raise SystemExit(f"\n✗ '{label}' failed (exit {result.returncode}).")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--review", default="2027-02")
    ap.add_argument("--cutoff-usd-mn", type=float, default=5600.0)
    ap.add_argument("--constituents-file", type=Path,
                    default=ROOT / "config" / "msci_india_constituents.csv")
    ap.add_argument("--min-float-confidence",
                    choices=["LOW", "MEDIUM", "HIGH"], default="MEDIUM")
    ap.add_argument("--min-dates", type=int, default=6)
    ap.add_argument("--skip-import", action="store_true",
                    help="Skip the import step (you already merged the YAML by hand)")
    args = ap.parse_args()

    # Derive the short tag MSCI uses in its URLs: 2027-02 → Feb27
    from msci_predict.calendar_rules import parse_review_label
    year, month = parse_review_label(args.review)
    _MONTH_ABBR = {2: "Feb", 5: "May", 8: "Aug", 11: "Nov"}
    msci_tag = f"{_MONTH_ABBR[month]}{str(year)[2:]}"  # "Feb27"

    print(f"\nPhase 4 — score the {args.review} review")

    # --- Step 1: Extract official result ---
    if not args.skip_import:
        _run(
            ["scripts/import_msci_changes.py", "--reviews", msci_tag],
            f"Extract official MSCI {msci_tag} additions/deletions",
        )
        print()
        print("  ↑ The YAML above needs to be merged into config/actual_reviews.yaml.")
        print("  Open the file and paste the block under the 'reviews:' key,")
        print("  then come back here and press Enter to continue.")
        input("  Press Enter when you have saved actual_reviews.yaml → ")

    # --- Step 2: Score ---
    _run(
        ["run_prediction.py",
         "--review", args.review,
         "--cutoff-usd-mn", str(args.cutoff_usd_mn),
         "--constituents-file", str(args.constituents_file),
         "--min-float-confidence", args.min_float_confidence,
         "--min-dates", str(args.min_dates),
         "--backtest", "--sweep",
         "--offline"],
        "Score prediction vs announced outcome",
    )

    # --- Step 3: Derive constituent snapshot for next review ---
    _run(
        ["scripts/derive_historical_membership.py",
         "--review", args.review,
         "--current", str(args.constituents_file)],
        f"Derive {args.review} constituent snapshot for future back-tests",
    )

    print(f"\n{'='*70}")
    print("  Phase 4 complete.")
    print(f"{'='*70}")
    print(f"\nWhat to do next:")
    print(f"  1. Download a fresh INDA file (membership changed on the effective date).")
    print(f"     .venv/bin/python scripts/import_inda_holdings.py ~/Downloads/iShares-MSCI-India-ETF_fund.xls")
    print(f"  2. When ready for the next review, run Phase 1 again:")
    print(f"     .venv/bin/python scripts/phase1_setup.py --review <next-review>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
