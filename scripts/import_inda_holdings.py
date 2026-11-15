"""Convert an iShares MSCI India ETF (INDA) holdings download into a constituents file.

INDA tracks the MSCI India Index, so its equity holdings are the best public proxy for
the India Standard Index membership. Supplying that list removes the model's largest
remaining source of error: without it, membership has to be reconstructed by replaying
past reviews, and every false positive in the November 2026 back-test traced to a name
the replay failed to seat.

BlackRock blocks scripted downloads, so fetch the file by hand from
https://www.ishares.com/us/products/239659/ishares-msci-india-etf (the "Download
Holdings" link) and point this script at it.

    .venv/bin/python scripts/import_inda_holdings.py ~/Downloads/iShares-MSCI-India-ETF_fund.xls

Handles both formats BlackRock serves: the SpreadsheetML workbook that arrives named
.xls, and the plain CSV. Writes config/msci_india_constituents.csv.

One caveat worth keeping in mind: INDA holds the large and mid cap segment, which is
the Standard Index, but an ETF can hold a name slightly before or after an index
change takes effect and may carry small residual or cash-equivalent lines. Treat the
output as very good, not as MSCI's official list.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
NS = {"ss": "urn:schemas-microsoft-com:office:spreadsheet"}
SS = "{urn:schemas-microsoft-com:office:spreadsheet}"

# Lines that are not operating companies. INDA carries a few of these and they would
# otherwise be written out as index constituents.
NON_EQUITY_TICKERS = {"XTSLA", "INR", "USD", "MVRXXRP", "BRPMM"}


def _repair_xml(text: str) -> str:
    """Escape bare ampersands.

    BlackRock emits query strings like `type=iShares&style=All` straight into an HRef
    attribute without escaping, which makes the document invalid XML and stops any
    standards-compliant parser at the first one.
    """
    return re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)


def _rows_from_spreadsheetml(path: Path, sheet: str = "Holdings") -> list[list[str]]:
    root = ET.fromstring(_repair_xml(path.read_text(encoding="utf-8", errors="replace")))
    target = None
    for ws in root.findall("ss:Worksheet", NS):
        if (ws.get(f"{SS}Name") or "").strip().lower() == sheet.lower():
            target = ws
            break
    if target is None:
        names = [ws.get(f"{SS}Name") for ws in root.findall("ss:Worksheet", NS)]
        raise SystemExit(f"No {sheet!r} worksheet. Found: {names}")

    rows: list[list[str]] = []
    for row in target.findall(".//ss:Row", NS):
        cells: list[str] = []
        for cell in row.findall("ss:Cell", NS):
            # MergeAcross and Index mean cells can be sparse; the holdings table
            # itself is dense, so a simple read is enough once the preamble is skipped.
            data = cell.find("ss:Data", NS)
            cells.append((data.text or "").strip() if data is not None else "")
        rows.append(cells)
    return rows


def _rows_from_csv(path: Path) -> list[list[str]]:
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        return [row for row in csv.reader(fh)]


def _find_header(rows: list[list[str]]) -> int:
    """Locate the holdings header row, skipping BlackRock's disclaimer preamble."""
    for i, row in enumerate(rows):
        lowered = [c.strip().lower() for c in row]
        if "ticker" in lowered and any(c in lowered for c in ("name", "asset class")):
            return i
    raise SystemExit("Could not find a header row containing 'Ticker'.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("holdings", type=Path, help="Downloaded INDA holdings file (.xls or .csv)")
    ap.add_argument("-o", "--out", type=Path,
                    default=ROOT / "config" / "msci_india_constituents.csv")
    ap.add_argument("--sheet", default="Holdings")
    args = ap.parse_args()

    if not args.holdings.exists():
        raise SystemExit(f"Not found: {args.holdings}")

    head = args.holdings.read_bytes()[:200]
    if head.lstrip().startswith(b"<?xml") or b"ss:Workbook" in head:
        rows = _rows_from_spreadsheetml(args.holdings, args.sheet)
        source_format = "SpreadsheetML"
    else:
        rows = _rows_from_csv(args.holdings)
        source_format = "CSV"

    # BlackRock stamps the holdings date into the preamble. Surfacing it matters
    # because the file is published daily with a day or two of lag, and a copy taken
    # before an index change took effect describes the old membership. Downloading
    # one from before 30 November 2026 would still show PI Industries as a member and
    # none of that review's four additions.
    as_of = ""
    for i, row in enumerate(rows):
        for j, cell in enumerate(row):
            if cell.strip().lower().startswith("fund holdings as of"):
                rest = [c.strip() for c in row[j + 1:] if c.strip()]
                if rest:
                    as_of = rest[0]
                elif i + 1 < len(rows):
                    nxt = [c.strip() for c in rows[i + 1] if c.strip()]
                    as_of = nxt[0] if nxt else ""
                break
        if as_of:
            break
    if as_of:
        print(f"Fund holdings as of: {as_of}")
    else:
        print("Could not read the holdings date from the file; check it is recent by hand.")

    header_idx = _find_header(rows)
    header = [c.strip().lower() for c in rows[header_idx]]
    col = {name: i for i, name in enumerate(header)}
    t_i = col["ticker"]
    n_i = col.get("name")
    a_i = col.get("asset class")
    w_i = col.get("weight (%)")

    holdings: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    skipped: list[str] = []
    for row in rows[header_idx + 1:]:
        if len(row) <= t_i:
            continue
        ticker = row[t_i].strip().upper()
        if not ticker or ticker == "TICKER":
            continue
        asset = row[a_i].strip() if a_i is not None and len(row) > a_i else ""
        if asset and asset.lower() != "equity":
            skipped.append(f"{ticker} ({asset})")
            continue
        if ticker in NON_EQUITY_TICKERS:
            skipped.append(f"{ticker} (cash/derivative line)")
            continue
        if ticker in seen:
            continue
        seen.add(ticker)
        name = row[n_i].strip() if n_i is not None and len(row) > n_i else ""
        weight = row[w_i].strip() if w_i is not None and len(row) > w_i else ""
        holdings.append((ticker, name, weight))

    print(f"Parsed {args.holdings.name} as {source_format}: "
          f"{len(holdings)} equity holdings, {len(skipped)} non-equity lines skipped")
    if skipped:
        print("  skipped: " + ", ".join(skipped[:10]) + (" ..." if len(skipped) > 10 else ""))

    # Map every holding onto an NSE symbol. BlackRock does not use NSE conventions
    # throughout: it writes BAJAJ.AUTO with a dot, and falls back to numeric BSE
    # scrip codes for some names. An unresolved ticker is not a cosmetic problem -
    # the model would treat that company as a non-member and nominate it for
    # addition, which is exactly how MCX became a false positive.
    nse_path = ROOT / "cache" / "nse_equity_list.csv"
    resolved: list[tuple[str, str, str, str]] = []   # symbol, name, weight, how
    unresolved: list[tuple[str, str]] = []

    if nse_path.exists():
        import pandas as pd

        nse = pd.read_csv(nse_path)
        nse.columns = [c.strip() for c in nse.columns]
        nse["SYMBOL"] = nse["SYMBOL"].astype(str).str.strip().str.upper()
        nse["NAME"] = nse["NAME OF COMPANY"].astype(str).str.strip().str.upper()
        valid = set(nse["SYMBOL"])
        by_name = dict(zip(nse["NAME"], nse["SYMBOL"]))

        def _tokens(s: str) -> set[str]:
            drop = {"LTD", "LIMITED", "THE", "OF", "AND", "INDIA", "CO", "COMPANY", "CORPORATION"}
            return {w for w in re.findall(r"[A-Z0-9]+", s.upper()) if w not in drop}

        for ticker, name, weight in holdings:
            if ticker in valid:
                resolved.append((ticker, name, weight, "exact"))
                continue
            # BlackRock writes dots where NSE uses hyphens.
            swapped = ticker.replace(".", "-")
            if swapped in valid:
                resolved.append((swapped, name, weight, f"dot->hyphen from {ticker}"))
                continue
            # Numeric BSE scrip code, or any other mismatch: resolve on company name.
            want = _tokens(name)
            if want:
                exact_name = by_name.get(name.upper())
                if exact_name:
                    resolved.append((exact_name, name, weight, f"name match from {ticker}"))
                    continue
                scored = [
                    (len(want & _tokens(n)) / len(want | _tokens(n)), sym, n)
                    for n, sym in by_name.items()
                    if want & _tokens(n)
                ]
                scored.sort(reverse=True)
                if scored and scored[0][0] >= 0.6:
                    resolved.append((scored[0][1], name, weight, f"name match from {ticker}"))
                    continue
            unresolved.append((ticker, name))

        print(f"\nNSE symbol resolution: {len(resolved)}/{len(holdings)} "
              f"({len(resolved) / len(holdings):.0%})")
        inferred = [r for r in resolved if r[3] != "exact"]
        if inferred:
            print("  resolved by inference - please eyeball these:")
            for sym, name, _, how in inferred:
                print(f"    {sym:<14}{name[:42]:<44}{how}")
        if unresolved:
            print("  UNRESOLVED - add these by hand to the output file, or the model will "
                  "treat them as non-members:")
            for t, n in unresolved:
                print(f"    {t:<14}{n[:50]}")
    else:
        print("\nNSE master list not cached, so tickers cannot be verified. "
              "Run run_prediction.py once to populate it, then re-run this script.")
        resolved = [(t, n, w, "unverified") for t, n, w in holdings]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "name", "weight_pct", "resolution"])
        for sym, name, weight, how in sorted(resolved):
            w.writerow([sym, name, weight, how])
        for ticker, name in sorted(unresolved):
            w.writerow([ticker, name, "", "UNRESOLVED - verify"])
    holdings = [(s, n, w) for s, n, w, _ in resolved]

    print(f"\nWrote {len(holdings)} constituents to {args.out}")
    print("\nUse it with:")
    print(f"  .venv/bin/python run_prediction.py --review 2026-11 --backtest \\")
    print(f"      --constituents-file {args.out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
