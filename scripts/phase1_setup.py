#!/usr/bin/env python
"""Phase 1 — Review setup. Run once, any day 11–17 January before the window opens.

Runs in this order (order matters for steps 3 and 4):
  1. Check the INDA holdings file is present and recent.
  2. Download and apply the February MSCI size thresholds.
  3. Refresh the dated free-float series from NSE quarterly filings.
  4. Derive MSCI's own float factors from the INDA weights (must follow step 3).
  5. Save a parameters snapshot for back-testing this review later.
  6. Re-calibrate the size cut-off against the most recent scored review.

Takes about 10–30 minutes depending on how many symbols need a shareholding refresh.

    .venv/bin/python scripts/phase1_setup.py
    .venv/bin/python scripts/phase1_setup.py --review 2027-02
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


def _latest_scored_review(actuals_path: Path, before: str) -> str | None:
    """The most recent review whose outcome is recorded in actual_reviews.yaml."""
    import yaml

    data = yaml.safe_load(actuals_path.read_text()) or {}
    reviews = data.get("reviews") or {}
    scored = sorted(
        k for k, v in reviews.items()
        if k < before and (v or {}).get("standard", {}).get("additions")
    )
    return scored[-1] if scored else None


def _run(args: list[str], label: str) -> None:
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")
    result = subprocess.run([sys.executable] + args)
    if result.returncode != 0:
        raise SystemExit(f"\n✗ '{label}' failed (exit {result.returncode}). "
                         "Fix the issue above and re-run.")
    print(f"\n✓ {label} done.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--review", default="2027-02",
                    help="Review to set up, e.g. 2027-02 or Feb2027 (default: 2027-02)")
    ap.add_argument("--constituents-file", type=Path,
                    default=ROOT / "config" / "msci_india_constituents.csv",
                    help="INDA-derived holdings file (default: config/msci_india_constituents.csv)")
    ap.add_argument("--params", type=Path, default=ROOT / "config" / "parameters.yaml")
    ap.add_argument("--actuals", type=Path, default=ROOT / "config" / "actual_reviews.yaml")
    ap.add_argument("--float-limit", type=int, default=500,
                    help="Max symbols to refresh for shareholding history (default: 500)")
    ap.add_argument("--skip-calibrate", action="store_true",
                    help="Skip the cut-off calibration step (fastest option if you already know the level)")
    args = ap.parse_args()

    print(f"\nPhase 1 setup for the {args.review} review")
    print(f"{'='*70}")

    # --- Step 1: Check INDA holdings file ---
    cons = args.constituents_file
    stale_days = 14
    if not cons.exists():
        print(f"\n⚠  INDA holdings file not found: {cons}")
        print("   Download it manually from:")
        print("   https://www.ishares.com/us/products/239659/ishares-msci-india-etf")
        print("   Then run:")
        print(f"   .venv/bin/python scripts/import_inda_holdings.py ~/Downloads/iShares-MSCI-India-ETF_fund.xls")
        raise SystemExit("Cannot continue without the INDA holdings file. "
                         "Re-run after importing it.")
    age = (date.today() - date.fromtimestamp(cons.stat().st_mtime)).days
    if age > stale_days:
        print(f"\n⚠  INDA holdings file is {age} days old (last modified {cons.stat().st_mtime:.0f}).")
        print("   For best accuracy, download a fresh copy before continuing:")
        print("   https://www.ishares.com/us/products/239659/ishares-msci-india-etf")
        print("   .venv/bin/python scripts/import_inda_holdings.py ~/Downloads/iShares-MSCI-India-ETF_fund.xls")
        answer = input("   Continue with the existing file? [y/N] ").strip().lower()
        if answer != "y":
            raise SystemExit("Aborted. Re-run after refreshing the INDA file.")
    else:
        print(f"\n✓ INDA holdings file is {age} day(s) old — recent enough.")

    # --- Step 2: Refresh MSCI size thresholds ---
    _run(
        ["scripts/update_msci_parameters.py", "--review", args.review,
         "--params", str(args.params), "--write"],
        f"Refresh MSCI size thresholds for {args.review}",
    )

    # --- Step 2b: Save a parameters snapshot for back-testing ---
    # e.g. config/parameters_2027-02.yaml
    label = args.review.replace("/", "-")
    snapshot = ROOT / "config" / f"parameters_{label}.yaml"
    if not snapshot.exists():
        shutil.copy(args.params, snapshot)
        print(f"\n✓ Saved parameters snapshot: {snapshot.name}")
    else:
        print(f"\n  Parameters snapshot {snapshot.name} already exists; not overwriting.")

    # --- Step 3: Refresh dated free-float series ---
    # Order matters: steps 3 and 4 must run in this sequence.
    # Step 3 rewrites free_float_history.csv (preserving MSCI-implied rows now);
    # step 4 appends fresh MSCI-implied rows on top.
    _run(
        ["scripts/fetch_shareholding_history.py",
         "--from-universe", "--limit", str(args.float_limit), "--merge"],
        "Refresh dated free-float series from NSE quarterly filings",
    )

    # --- Step 4: Derive MSCI-implied FIF from INDA weights ---
    _run(
        ["scripts/derive_msci_fif.py", "--write",
         "--constituents", str(cons)],
        "Derive MSCI-implied float factors from INDA weights",
    )

    # --- Step 5: Re-calibrate the size cut-off ---
    if not args.skip_calibrate:
        prev = _latest_scored_review(args.actuals, args.review)
        if prev:
            # Prefer a dated snapshot; fall back to the live files for the most
            # recent review which hasn't been archived under its own name yet.
            prev_params = ROOT / "config" / f"parameters_{prev}.yaml"
            prev_cons   = ROOT / "config" / f"msci_india_constituents_{prev}.csv"
            if not prev_params.exists():
                prev_params = args.params
            if not prev_cons.exists():
                prev_cons = args.constituents_file
            if prev_params.exists() and prev_cons.exists():
                _run(
                    ["scripts/calibrate_cutoff.py",
                     "--review", prev,
                     "--params", str(prev_params),
                     "--constituents-file", str(prev_cons),
                     "--min-float-confidence", "MEDIUM",
                     "--min-dates", "6",
                     "--auto-band", "--robustness"],
                    f"Re-calibrate size cut-off against {prev} (most recent scored review)",
                )
            else:
                print(f"\n  Skipping calibration: snapshot files for {prev} not found.")
        else:
            print("\n  Skipping calibration: no prior scored review found.")
    else:
        print("\n  --skip-calibrate: skipping cut-off calibration.")

    print(f"\n{'='*70}")
    print("  Phase 1 complete.")
    print(f"{'='*70}")
    print("\nNext steps:")
    print(f"  • Starting 18 January, run daily:")
    print(f"    .venv/bin/python scripts/phase2_daily.py --review {args.review}")
    print(f"  • After close on 29 January, run the authoritative prediction:")
    print(f"    .venv/bin/python scripts/phase3_predict.py --review {args.review}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
