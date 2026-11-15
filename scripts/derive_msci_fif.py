"""Back MSCI's own free-float factors out of the INDA holdings file.

The model's largest error source is its free-float estimate. For index constituents
that estimate can be replaced with MSCI's actual number, because an index fund's
weight is proportional to the free-float market cap MSCI assigns:

    weight_i  proportional to  FFMC_i  =  full market cap_i  x  FIF_i

so

    FIF_i = k x weight_i / full market cap_i

for a single constant k shared by every holding. Fix k and you have MSCI's FIF for all
165 constituents.

CALIBRATING k
-------------
The obvious anchor is a company NSE reports as 100% public, assuming its FIF is 1.0.
That is wrong, and wrong in an instructive way. Anchoring on HDFC Bank implied a FIF
above NSE public holding for 77 of 165 constituents, which is arithmetically
impossible, all by a suspiciously uniform 1.3x. The anchor was the problem: HDFC Bank
has no promoter but sits near the 74% banking foreign ownership limit, so MSCI applies
the low-foreign-room adjustment in GIMI 2.3.6.2 and its FIF is nowhere near 1.0.

So k is calibrated against a bound that always holds instead of an assumption that
sometimes does: MSCI's FIF can never exceed SEBI public holding, since public holding
is the most that could possibly be free float. Taking the 5th percentile of
`public_i x mcap_i / weight_i` rather than the strict minimum keeps one bad share
count from depressing every estimate.

WHAT THIS REVEALS
-----------------
MSCI's FIF runs at a median of about 0.83 times NSE public holding, with a tenth
percentile near 0.53. The discount is largest for banks and financials, which is the
signature of the foreign-room adjustment rather than of strategic holdings.

TWO BY-PRODUCTS
---------------
Names whose implied FIF still exceeds public holding after calibration almost
certainly have a stale share count, since that is the other term in the ratio. They
are reported separately.

This only works for current constituents. A non-constituent has no weight, so the
names most likely to be predicted wrongly are exactly the ones this cannot price. Use
the constituent values directly, and the fitted relationship as a prior elsewhere.

    .venv/bin/python scripts/derive_msci_fif.py
    .venv/bin/python scripts/derive_msci_fif.py --write
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics as st
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_BOUND_TOLERANCE = 1.02
ANCHOR_PERCENTILE = 0.05


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--constituents", type=Path,
                    default=ROOT / "config" / "msci_india_constituents.csv")
    ap.add_argument("--gap-file", type=Path, default=ROOT / "config" / "strategic_gap.csv")
    ap.add_argument("--out", type=Path, default=ROOT / "config" / "msci_implied_fif.csv")
    ap.add_argument("--as-of", default="",
                    help="Date to stamp the derived values with. Defaults to the latest "
                         "price date, which is when the holdings weights were measured.")
    ap.add_argument("--write", action="store_true",
                    help="Append the derived values to config/free_float_history.csv")
    args = ap.parse_args()

    import pandas as pd

    weights = {
        r["symbol"].strip().upper(): float(r["weight_pct"])
        for r in csv.DictReader(open(args.constituents))
        if r.get("weight_pct", "").strip() and not r["symbol"].lstrip().startswith("#")
    }
    info = json.loads((ROOT / "cache" / "security_info.json").read_text())
    px = pd.read_csv(ROOT / "cache" / "prices.csv", index_col=0, parse_dates=True)
    last = px.ffill().iloc[-1]
    as_of = args.as_of or px.index.max().date().isoformat()

    public = {
        r["symbol"].strip().upper(): float(r["nse_public_pct"]) / 100.0
        for r in csv.DictReader(open(args.gap_file))
        if r.get("nse_public_pct", "").strip()
    }

    rec: dict[str, dict] = {}
    for sym, weight in weights.items():
        meta = info.get(sym) or {}
        shares = meta.get("shares_outstanding")
        if not shares or sym not in last.index or pd.isna(last[sym]):
            continue
        rec[sym] = {"weight": weight, "mcap": shares * float(last[sym])}

    bounded = {s: v for s, v in rec.items() if s in public and public[s] > 0}
    if len(bounded) < 20:
        raise SystemExit(
            f"Only {len(bounded)} constituents have both a weight and NSE public holding; "
            "run scripts/fetch_shareholding_history.py first."
        )

    candidates = sorted(public[s] * v["mcap"] / v["weight"] for s, v in bounded.items())
    k = candidates[max(0, int(ANCHOR_PERCENTILE * len(candidates)))]
    implied = {s: (k * v["weight"]) / v["mcap"] for s, v in rec.items()}

    impossible, usable = [], []
    for sym, value in implied.items():
        bound = public.get(sym)
        if bound and value > bound * PUBLIC_BOUND_TOLERANCE:
            impossible.append((sym, value, bound))
        else:
            usable.append((sym, min(1.0, value)))

    ratios = [implied[s] / public[s] for s in implied if public.get(s)]
    print(f"Calibrated against {len(bounded)} constituents, k anchored at the "
          f"{ANCHOR_PERCENTILE:.0%} percentile of the public-holding bound")
    print(f"Derived MSCI implied FIF for {len(usable)} constituents, as at {as_of}")
    print(f"implied / NSE public holding: median {st.median(ratios):.2f}, "
          f"p10 {st.quantiles(ratios, n=10)[0]:.2f}, p90 {st.quantiles(ratios, n=10)[8]:.2f}")
    print("A median well below 1.0 is the foreign-room adjustment (GIMI 2.3.6.2) and "
          "strategic\nholdings working together; the model implements neither.")

    print(f"\n{'symbol':<14}{'MSCI implied':>13}{'own estimate':>14}{'NSE public':>11}{'ratio':>8}")
    def own(sym: str) -> float | None:
        meta = info.get(sym) or {}
        sh, fl = meta.get("shares_outstanding"), meta.get("free_float_shares")
        return fl / sh if sh and fl else None

    biggest = sorted(
        ((s, v) for s, v in usable if own(s)),
        key=lambda t: -abs(t[1] - (own(t[0]) or 0)),
    )
    for sym, value in biggest[:15]:
        mine = own(sym) or float("nan")
        print(f"{sym:<14}{value:>13.3f}{mine:>14.3f}{public.get(sym, float('nan')):>11.3f}"
              f"{value / mine:>8.2f}")

    if impossible:
        print(f"\n{len(impossible)} constituent(s) still imply a FIF above public holding. "
              "The other\nterm in the ratio is market cap, so these share counts are "
              "probably stale:")
        for sym, value, bound in sorted(impossible, key=lambda t: -t[1] / t[2])[:12]:
            meta = info.get(sym) or {}
            print(f"  {sym:<14}implied {value:.3f} vs public bound {bound:.3f} "
                  f"({value / bound:.2f}x)  shares used {meta.get('shares_outstanding', 0):,.0f}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "msci_implied_fif", "own_estimate", "nse_public", "status"])
        for sym, value in sorted(usable):
            w.writerow([sym, round(value, 4), round(own(sym), 4) if own(sym) else "",
                        round(public.get(sym, 0), 4) or "", "derived"])
        for sym, value, bound in sorted(impossible):
            w.writerow([sym, round(value, 4), round(own(sym), 4) if own(sym) else "",
                        round(bound, 4), "REJECTED - implies stale share count"])
    print(f"\nWrote {args.out}")

    if args.write:
        history = ROOT / "config" / "free_float_history.csv"
        existing = list(csv.DictReader(open(history))) if history.exists() else []
        derived_syms = {s for s, _ in usable}
        kept = [
            r for r in existing
            if (r.get("symbol") or "").strip().upper() not in derived_syms
            or "msci implied" not in (r.get("note") or "")
        ]
        rows = kept + [
            {"symbol": s, "effective_date": as_of, "fif": round(v, 4),
             "note": "msci implied from INDA weight"}
            for s, v in sorted(usable)
        ]
        with open(history, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["symbol", "effective_date", "fif", "note"])
            w.writeheader()
            w.writerows(rows)
        print(f"Appended {len(usable)} MSCI-implied values to {history}")
        print("These are dated, so they apply from the holdings date onward and leave "
              "earlier\nreviews on the NSE-derived series. Re-run the back-tests.")
    else:
        print("\nNothing written to the float history. Re-run with --write to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
