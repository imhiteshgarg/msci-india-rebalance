"""Build a dated free-float series from NSE quarterly shareholding patterns.

This fixes the model's worst input problem: without it, a single current float
estimate gets applied to every historical date, so a back-test of May 2026 reads
November 2026's capital structure onto April 2026's prices.

HOW IT WORKS, AND WHY IT IS NOT A STRAIGHT COPY
-----------------------------------------------
NSE publishes promoter and public shareholding per quarter. It is tempting to use
"public shareholding" as the Foreign Inclusion Factor. Do not: SEBI's public category
counts strategic non-promoter holders that MSCI excludes, and the two numbers are
nowhere near each other.

    symbol        NSE public%   estimated FIF%   gap
    ADANIGREEN          37.57             20.7   16.8pp   (TotalEnergies et al.)
    MEESHO              83.66             35.9   47.8pp   (Peak XV, SoftBank et al.)
    LENSKART            82.06             43.5   38.5pp
    LICI                10.00             11.2   -1.2pp   (government is the promoter)

So the quarterly series is used for its **changes**, not its level:

    FIF(t) = FIF_now − (public_now − public(t))

The current FIF carries MSCI-like strategic exclusions; the NSE delta carries the
timing of real ownership changes. Combining them gives a dated series that is right
about *when* float moved without inheriting SEBI's definition of float.

For LIC this recovers the fact that matters: public holding went 3.5% to 10.0% between
June and September 2026, so its April 2026 FIF was about 0.047, not 0.112. The model
missed LIC entirely until this was corrected by hand; now it is derived.

WHAT THIS STILL CANNOT SEE
--------------------------
Reclassification of a strategic holder, and IPO lock-ins. Shareholding patterns report
a pre-IPO investor as public from day one even while its shares are locked. That is
why `rules.py` carries a separate lock-in reliability gate rather than pretending the
float number is trustworthy for a recently listed company.

USAGE
-----
    .venv/bin/python scripts/fetch_shareholding_history.py --symbols LICI,ADANIGREEN
    .venv/bin/python scripts/fetch_shareholding_history.py --from-universe --limit 400

Writes config/free_float_history.csv (dated FIF rows) and
config/strategic_gap.csv (NSE public% vs estimated FIF, for the confidence flag).
Raw API responses are cached under cache/shareholding/.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from datetime import datetime
from urllib.parse import quote
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
NSE_LANDING = "https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern"
NSE_API = "https://www.nseindia.com/api/corporate-share-holdings-master?index=equities&symbol={sym}"

log = logging.getLogger("shareholding")

# Only emit a dated row when the implied float differs from the current estimate by
# at least this much. Keeps the file reviewable and avoids encoding rounding noise as
# if it were a corporate action.
MATERIAL_PP = 1.0

# When SEBI's public holding sits within this many points of the estimated float,
# the two definitions effectively agree for that company, so the reported level is
# used directly instead of only its change.
LEVEL_TRUSTWORTHY_GAP_PP = 2.0


def _session():
    from curl_cffi import requests as cr

    s = cr.Session(impersonate="chrome")
    s.get(NSE_LANDING, timeout=60)
    return s


def fetch_symbol(session, symbol: str, cache_dir: Path, refresh: bool = False) -> list[dict]:
    path = cache_dir / f"{symbol}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    # Symbols such as M&M and J&KBANK contain an ampersand, which silently
    # truncates the query string unless it is percent-encoded.
    resp = session.get(
        NSE_API.format(sym=quote(symbol, safe="")),
        timeout=60,
        headers={"Referer": NSE_LANDING},
    )
    if resp.status_code != 200 or not resp.text.strip().startswith("["):
        log.debug("%s: no data (status %s)", symbol, resp.status_code)
        return []
    data = resp.json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return data


def parse_quarters(records: list[dict]) -> list[tuple[datetime, float]]:
    """(quarter end, public holding %) sorted oldest first, de-duplicated."""
    out: dict[datetime, float] = {}
    for rec in records:
        raw_date, raw_public = rec.get("date"), rec.get("public_val")
        if not raw_date or raw_public in (None, "", "-"):
            continue
        try:
            when = datetime.strptime(raw_date.strip(), "%d-%b-%Y")
            public = float(str(raw_public).strip())
        except ValueError:
            continue
        if not 0.0 <= public <= 100.0:
            continue
        # Several filings can share a quarter end after a revision; keep the latest.
        out[when] = public
    return sorted(out.items())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default="", help="Comma-separated NSE symbols")
    ap.add_argument("--from-universe", action="store_true",
                    help="Use every symbol with share-count data in the cache")
    ap.add_argument("--limit", type=int, default=0, help="Cap the symbol count (largest first)")
    ap.add_argument("--out", type=Path, default=ROOT / "config" / "free_float_history.csv")
    ap.add_argument("--gap-out", type=Path, default=ROOT / "config" / "strategic_gap.csv")
    ap.add_argument("--cache-dir", type=Path, default=ROOT / "cache" / "shareholding")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.35, help="Pause between requests")
    ap.add_argument("--merge", action="store_true",
                    help="Keep existing hand-written rows in the output file")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    info_path = ROOT / "cache" / "security_info.json"
    if not info_path.exists():
        raise SystemExit("cache/security_info.json missing. Run run_prediction.py once first.")
    info = json.loads(info_path.read_text())

    def current_fif(sym: str) -> float | None:
        rec = info.get(sym) or {}
        sh, fl = rec.get("shares_outstanding"), rec.get("free_float_shares")
        if sh and fl and sh > 0:
            return min(1.0, fl / sh)
        insiders = rec.get("held_pct_insiders")
        return max(0.0, 1.0 - insiders) if insiders is not None else None

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.from_universe:
        candidates = [
            (s, (info[s] or {}).get("market_cap_inr") or 0)
            for s in info
            if current_fif(s) is not None
        ]
        candidates.sort(key=lambda t: -t[1])
        symbols = [s for s, _ in candidates]
        if args.limit:
            symbols = symbols[: args.limit]
    else:
        raise SystemExit("Pass --symbols or --from-universe")

    log.info("Fetching shareholding history for %d symbols", len(symbols))
    session = _session()

    history_rows: list[dict] = []
    gap_rows: list[dict] = []
    no_data: list[str] = []

    for i, sym in enumerate(symbols, start=1):
        fif_now = current_fif(sym)
        if fif_now is None:
            continue
        try:
            quarters = parse_quarters(fetch_symbol(session, sym, args.cache_dir, args.refresh))
        except Exception as exc:  # noqa: BLE001
            log.debug("%s failed: %s", sym, exc)
            quarters = []
        if not quarters:
            no_data.append(sym)
            if args.sleep:
                time.sleep(args.sleep)
            continue

        public_now = quarters[-1][1]
        gap_rows.append({
            "symbol": sym,
            "nse_public_pct": round(public_now, 2),
            "estimated_fif_pct": round(fif_now * 100, 2),
            "strategic_gap_pp": round(public_now - fif_now * 100, 2),
            "latest_quarter": quarters[-1][0].date().isoformat(),
            "quarters_available": len(quarters),
        })

        # Where SEBI's public holding already agrees with the estimated float, the
        # *level* is trustworthy and can be used directly. Where it does not, only
        # the *change* is usable and it gets applied to the current estimate.
        #
        # This distinction decides real cases. LIC's gap is -1.25pp, so public
        # holding is its float. Carrying that offset through the delta method put its
        # April 2026 FIF at 0.0475 instead of 0.035, and since its free-float market
        # cap sat within 1% of the threshold, the difference was the whole
        # prediction: 0.0475 wrongly cleared the bar, 0.035 correctly fails it.
        gap_pp = public_now - fif_now * 100
        use_level = abs(gap_pp) <= LEVEL_TRUSTWORTHY_GAP_PP

        for when, public in quarters:
            if use_level:
                implied = public / 100.0
                basis = f"level: NSE public {public:.2f}%, gap to estimate {gap_pp:+.2f}pp"
            else:
                implied = fif_now - (public_now - public) / 100.0
                basis = (
                    f"delta: NSE public {public:.2f}% vs {public_now:.2f}% latest, "
                    f"current FIF {fif_now:.3f}, strategic gap {gap_pp:+.1f}pp"
                )
            implied = min(1.0, max(0.0, implied))
            if abs(implied - fif_now) * 100 >= MATERIAL_PP:
                history_rows.append({
                    "symbol": sym,
                    "effective_date": when.date().isoformat(),
                    "fif": round(implied, 4),
                    "note": basis,
                })

        if i % 25 == 0:
            log.info("  %d/%d", i, len(symbols))
        if args.sleep:
            time.sleep(args.sleep)

    # Every symbol with derived rows also needs its current value pinned, otherwise
    # the last derived row would stay in force for all later dates.
    # Pin a value at the latest filed quarter, otherwise the last derived row would
    # stay in force for every later date. It must use the same basis as the rows
    # above it: mixing a level-derived series with a snapshot-pinned endpoint would
    # make the float appear to jump on the final quarter for no reason.
    derived_symbols = {r["symbol"] for r in history_rows}
    for sym in sorted(derived_symbols):
        latest = max(
            (r for r in gap_rows if r["symbol"] == sym), key=lambda r: r["latest_quarter"]
        )
        used_level = abs(latest["strategic_gap_pp"]) <= LEVEL_TRUSTWORTHY_GAP_PP
        value = (
            latest["nse_public_pct"] / 100.0 if used_level else current_fif(sym)
        )
        history_rows.append({
            "symbol": sym,
            "effective_date": latest["latest_quarter"],
            "fif": round(value, 4),
            "note": (
                f"in force from the latest filed quarter "
                f"({'NSE level' if used_level else 'current estimate'})"
            ),
        })

    existing: list[dict] = []
    if args.merge and args.out.exists():
        with open(args.out, newline="") as fh:
            for row in csv.DictReader(fh):
                sym = (row.get("symbol") or "").strip().upper()
                if sym and not sym.startswith("#") and sym not in derived_symbols:
                    existing.append(row)
        log.info("Keeping %d existing hand-written row(s)", len(existing))

    history_rows.sort(key=lambda r: (r["symbol"], r["effective_date"]))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["symbol", "effective_date", "fif", "note"])
        w.writeheader()
        w.writerows(existing + history_rows)

    # Merge the gap file the same way as the history file. Overwriting it wholesale
    # on a partial run silently destroys coverage for every symbol not in that run,
    # and the gap values feed the float-confidence gate, so losing them quietly
    # degrades predictions with no visible error.
    gap_fields = [
        "symbol", "nse_public_pct", "estimated_fif_pct", "strategic_gap_pp",
        "latest_quarter", "quarters_available",
    ]
    fetched = {r["symbol"] for r in gap_rows}
    kept: list[dict] = []
    if args.gap_out.exists():
        with open(args.gap_out, newline="") as fh:
            for row in csv.DictReader(fh):
                sym = (row.get("symbol") or "").strip().upper()
                if sym and not sym.startswith("#") and sym not in fetched:
                    kept.append({k: row.get(k, "") for k in gap_fields})
    if kept:
        log.info("Kept %d existing strategic-gap row(s) for symbols not in this run", len(kept))

    merged = kept + gap_rows
    merged.sort(key=lambda r: -float(r["strategic_gap_pp"] or 0))
    with open(args.gap_out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=gap_fields)
        w.writeheader()
        w.writerows(merged)
    gap_rows = merged

    print()
    print(f"Wrote {len(history_rows)} dated float rows for {len(derived_symbols)} symbols "
          f"to {args.out}")
    print(f"Wrote {len(gap_rows)} strategic-gap rows to {args.gap_out}")
    if no_data:
        print(f"{len(no_data)} symbol(s) returned no shareholding data"
              + (f": {', '.join(no_data[:12])}..." if len(no_data) > 12 else
                 f": {', '.join(no_data)}" if no_data else ""))

    if derived_symbols:
        print("\nSymbols whose float actually moved (these are the ones that matter):")
        by_sym: dict[str, list[dict]] = {}
        for r in history_rows:
            by_sym.setdefault(r["symbol"], []).append(r)
        ranked = sorted(
            by_sym.items(),
            key=lambda kv: -(max(float(r["fif"]) for r in kv[1])
                             - min(float(r["fif"]) for r in kv[1])),
        )
        for sym, rows in ranked[:15]:
            lo = min(float(r["fif"]) for r in rows)
            hi = max(float(r["fif"]) for r in rows)
            print(f"  {sym:<14}FIF ranged {lo:.3f} to {hi:.3f} across "
                  f"{len(rows)} dated point(s)")

    print("\nLargest strategic gaps - where MSCI's holder classification dominates and")
    print("this model is least reliable:")
    for r in sorted(gap_rows, key=lambda r: -r["strategic_gap_pp"])[:12]:
        print(f"  {r['symbol']:<14}NSE public {r['nse_public_pct']:>6.2f}%  "
              f"est FIF {r['estimated_fif_pct']:>6.2f}%  gap {r['strategic_gap_pp']:>6.2f}pp")
    return 0


if __name__ == "__main__":
    sys.exit(main())
