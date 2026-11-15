"""Reconstruct index membership as it stood at an earlier review.

BlackRock serves only the current INDA holdings file. There is no archive, so for a
back-test of, say, the May 2026 review you cannot simply download April's copy.

You do not need to. Membership changes only at Index Reviews, and MSCI publishes every
change. So today's holdings can be rolled backwards exactly:

    membership(before review R) = current holdings
                                  - (everything added at R and later)
                                  + (everything deleted at R and later)

This is exact rather than approximate, provided the recorded change lists are
complete - which is why `scripts/import_msci_changes.py` pulls them from MSCI's own
published documents rather than from press coverage, where deletions are routinely
omitted.

    .venv/bin/python scripts/derive_historical_membership.py --review 2026-05

Writes config/msci_india_constituents_<review>.csv for use with --constituents-file.

A caveat that applies to the whole back-test rather than to this script: share counts
and free-float estimates in the cache are current snapshots. Rolling membership back
fixes membership, not those. The further back you go, the more the model is reading
today's capital structure onto yesterday's prices, so treat an old review as
indicative and weight recent ones more heavily when calibrating.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from msci_predict.backtest import load_actuals
from msci_predict.calendar_rules import parse_review_label

ROOT = Path(__file__).resolve().parents[1]


def read_symbols(path: Path) -> tuple[set[str], dict[str, str]]:
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    symbols, names = set(), {}
    for r in rows:
        sym = (r.get("symbol") or "").strip().upper()
        if not sym or sym.startswith("#"):
            continue
        symbols.add(sym)
        names[sym] = (r.get("name") or "").strip()
    return symbols, names


def roll_back(
    current: set[str], actuals: dict, target_label: str
) -> tuple[set[str], list[str]]:
    """Undo every review at or after `target_label`, newest first."""
    reviews = actuals.get("reviews") or {}
    applicable = sorted((l for l in reviews if l >= target_label), reverse=True)
    members = set(current)
    log: list[str] = []
    for label in applicable:
        std = (reviews[label] or {}).get("standard") or {}
        adds = {s.upper() for s in (std.get("additions") or [])}
        dels = {s.upper() for s in (std.get("deletions") or [])}

        # A holdings file downloaded between a review's announcement and its effective
        # date does not yet reflect that review, so there is nothing to undo. Detect
        # it by the additions being absent: undoing it anyway would wrongly restore
        # that review's deletions, which are still legitimately in the file.
        if adds and not (adds & members):
            log.append(
                f"skip {label}: none of its additions "
                f"({', '.join(sorted(adds))}) are in the holdings file, so the file "
                "predates that review taking effect. Nothing to undo."
            )
            continue

        before = len(members)
        partial = adds - members
        already_present = dels & members
        members = (members - adds) | dels
        log.append(
            f"undo {label}: removed {len(adds) - len(partial)}/{len(adds)} additions, "
            f"restored {len(dels) - len(already_present)}/{len(dels)} deletions, "
            f"{before} -> {len(members)}"
        )
        if partial:
            log.append(
                f"  warning: {label} additions missing from the file: "
                f"{', '.join(sorted(partial))}. Check the symbol spelling against "
                "the holdings file."
            )
        if already_present:
            log.append(
                f"  warning: {label} deletions were already present: "
                f"{', '.join(sorted(already_present))}. Check for a stale change list."
            )
    return members, log


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--review", required=True,
                    help="Review to reconstruct membership BEFORE, e.g. 2026-05")
    ap.add_argument("--current", type=Path,
                    default=ROOT / "config" / "msci_india_constituents.csv",
                    help="Current holdings, from scripts/import_inda_holdings.py")
    ap.add_argument("--actuals", type=Path, default=ROOT / "config" / "actual_reviews.yaml")
    ap.add_argument("-o", "--out", type=Path, default=None)
    args = ap.parse_args()

    year, month = parse_review_label(args.review)
    label = f"{year}-{month:02d}"
    if not args.current.exists():
        raise SystemExit(
            f"{args.current} not found. Build it first:\n"
            "  .venv/bin/python scripts/import_inda_holdings.py <downloaded INDA file>"
        )

    current, names = read_symbols(args.current)
    actuals = load_actuals(args.actuals)
    members, log = roll_back(current, actuals, label)

    print(f"Starting from {len(current)} current holdings in {args.current.name}")
    for line in log:
        print("  " + line)

    recorded = sorted(l for l in (actuals.get("reviews") or {}) if l >= label)
    if not recorded:
        print(f"\nNo recorded review at or after {label}, so nothing was rolled back. "
              "The output equals the current holdings.")
    else:
        print(f"\nRolled back through: {', '.join(recorded)}")

    earliest = min((actuals.get("reviews") or {}), default=None)
    if earliest and label < earliest:
        print(f"Note: {label} precedes the earliest recorded review ({earliest}), so any "
              "changes before that are not undone. Add more reviews with "
              "scripts/import_msci_changes.py.")

    out = args.out or ROOT / "config" / f"msci_india_constituents_{label}.csv"
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "name", "note"])
        for sym in sorted(members):
            w.writerow([sym, names.get(sym, ""), f"membership as at the {label} review"])

    print(f"\nWrote {len(members)} constituents to {out}")
    print("\nUse it with:")
    print(f"  .venv/bin/python run_prediction.py --review {label} --backtest \\")
    print(f"      --constituents-file {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
