"""Extract MSCI India Standard Index changes from MSCI's own published change lists.

MSCI publishes the authoritative additions and deletions for every Index Review at

    https://www.msci.com/eqb/gimi/stdindex/MSCI_<Mon><YY>_STPublicList.pdf

which is a far better source than press coverage: complete, unambiguous, and it
includes the deletions that news articles routinely omit. This downloads one or more
of those lists, pulls out the MSCI INDIA INDEX block, resolves MSCI's abbreviated
company names onto NSE symbols, and prints YAML ready for config/actual_reviews.yaml.

    .venv/bin/python scripts/import_msci_changes.py --reviews Feb26,May26,Aug26,Nov26

Why bother populating old reviews: every recorded review makes the model better in
two independent ways. It is another data point for back-testing and cut-off
calibration, and it lets `derive_historical_membership` roll the current INDA holdings
backwards to reconstruct exactly who was in the index at an earlier review - which is
otherwise unobtainable, because BlackRock only serves the current holdings file.

One caveat on the source. These lists are as-announced. MSCI occasionally reverses a
decision between announcement and implementation, and May 2026 is exactly that case:
the list names five Indian additions including Adani Energy Solutions, which MSCI then
decided against adding. Check the review's news coverage before trusting a count.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
URL = "https://www.msci.com/eqb/gimi/stdindex/MSCI_{tag}_STPublicList.pdf"

# MSCI truncates company names to fit a fixed-width column, so a plain string
# comparison against NSE's full legal names fails on exactly the names that matter.
ABBREVIATIONS = {
    "CMDTY": "COMMODITY", "EXCH": "EXCHANGE", "IND": "INDUSTRIES", "INDS": "INDUSTRIES",
    "FIN": "FINANCE", "FINL": "FINANCIAL", "SERV": "SERVICES", "SVCS": "SERVICES",
    "CORP": "CORPORATION", "CO": "COMPANY", "NATL": "NATIONAL", "INTL": "INTERNATIONAL",
    "PHARMA": "PHARMACEUTICALS", "PWR": "POWER", "ENRG": "ENERGY", "TECH": "TECHNOLOGIES",
    "MACH": "MACHINERY", "MTRS": "MOTORS", "MFG": "MANUFACTURING", "ALUMINIUM": "ALUMINIUM",
    "BK": "BANK", "INS": "INSURANCE", "LIFE": "LIFE", "HLDG": "HOLDINGS",
    "HLDGS": "HOLDINGS", "DEV": "DEVELOPMENT", "CONSTR": "CONSTRUCTION",
    "TELECOM": "TELECOMMUNICATIONS", "LTD": "LIMITED", "NIGAM": "NIGAM",
}
NOISE = {"LIMITED", "LTD", "THE", "OF", "AND", "CO", "COMPANY", "INDIA", "INDIAN"}


def _download(tag: str) -> Path:
    dest = ROOT / "research" / f"stdlist_{tag}.pdf"
    if dest.exists() and dest.read_bytes().startswith(b"%PDF"):
        return dest
    url = URL.format(tag=tag)
    try:
        from curl_cffi import requests as cr  # type: ignore

        resp = cr.get(url, impersonate="chrome", timeout=120)
    except ImportError:
        import requests

        resp = requests.get(url, timeout=120,
                            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
    if not resp.content.startswith(b"%PDF"):
        raise SystemExit(f"{url} did not return a PDF. Check the tag format, e.g. May26.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    return dest


def _text(pdf: Path) -> str:
    from pypdf import PdfReader

    return "\n".join((p.extract_text() or "") for p in PdfReader(str(pdf)).pages)


def parse_india_block(text: str) -> tuple[list[str], list[str], str]:
    """Pull the additions and deletions out of the MSCI INDIA INDEX section.

    The block is a two-column layout: additions on the left, deletions on the right,
    with either side able to run out first. Column membership is decided by where the
    line's first non-space character sits, because a deletion-only row is simply
    indented into the right-hand column.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*MSCI INDIA INDEX\s*$", line):
            start = i
            break
    if start is None:
        return [], [], "MSCI INDIA INDEX section not found"

    body: list[str] = []
    for line in lines[start + 1:]:
        if re.match(r"^\s*MSCI [A-Z][A-Z &.\-()]+ INDEX\s*$", line):
            break
        if re.match(r"^\s*Additions\s+Deletions\s*$", line):
            continue
        if line.strip():
            body.append(line.rstrip())

    # Infer the column boundary from the header row if possible, else use the
    # observed layout width.
    split_at = 40
    for line in lines[start:start + 3]:
        m = re.search(r"Additions(\s+)Deletions", line)
        if m:
            split_at = m.start() + len("Additions") + len(m.group(1))
            break

    additions, deletions = [], []
    for line in body:
        indent = len(line) - len(line.lstrip())
        if indent >= split_at - 4:
            name = line.strip()
            if name.upper() != "NONE":
                deletions.append(name)
            continue
        left = line[:split_at].strip()
        right = line[split_at:].strip() if len(line) > split_at else ""
        if not right:
            parts = re.split(r"\s{3,}", line.strip())
            left = parts[0].strip()
            right = parts[1].strip() if len(parts) > 1 else ""
        if left and left.upper() != "NONE":
            additions.append(left)
        if right and right.upper() != "NONE":
            deletions.append(right)
    return additions, deletions, "ok"


class SymbolResolver:
    """Resolve an MSCI company name onto an NSE symbol."""

    def __init__(self, nse_csv: Path):
        import pandas as pd

        df = pd.read_csv(nse_csv)
        df.columns = [c.strip() for c in df.columns]
        df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip().str.upper()
        df["NAME"] = df["NAME OF COMPANY"].astype(str).str.strip().str.upper()
        self.by_name = dict(zip(df["NAME"], df["SYMBOL"]))
        self.norm_to_symbol = {self._normalise(n): s for n, s in self.by_name.items()}
        self.names = list(self.norm_to_symbol)

    @staticmethod
    def _normalise(name: str) -> str:
        tokens = re.findall(r"[A-Z0-9]+", name.upper())
        expanded = [ABBREVIATIONS.get(t, t) for t in tokens]
        return " ".join(t for t in expanded if t not in NOISE)

    def resolve(self, msci_name: str) -> tuple[str | None, float, str]:
        target = self._normalise(msci_name)
        if not target:
            return None, 0.0, "empty after normalisation"
        if target in self.norm_to_symbol:
            return self.norm_to_symbol[target], 1.0, "exact"
        best = difflib.get_close_matches(target, self.names, n=3, cutoff=0.55)
        if not best:
            return None, 0.0, "no candidate above 0.55 similarity"
        score = difflib.SequenceMatcher(None, target, best[0]).ratio()
        # A truncated MSCI name is a prefix of the full one more often than not, so
        # treat that as strong evidence even when the overall ratio is middling.
        if best[0].startswith(target) or target.startswith(best[0]):
            score = max(score, 0.9)
        return self.norm_to_symbol[best[0]], score, f"fuzzy -> {best[0]}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reviews", default="Feb26,May26,Aug26,Nov26",
                    help="Comma-separated MSCI tags, e.g. Feb26,May26,Aug26,Nov26")
    ap.add_argument("--confident", type=float, default=0.85,
                    help="Similarity at or above which a match is accepted silently")
    args = ap.parse_args()

    nse_csv = ROOT / "cache" / "nse_equity_list.csv"
    if not nse_csv.exists():
        raise SystemExit("cache/nse_equity_list.csv missing. Run run_prediction.py once first.")
    resolver = SymbolResolver(nse_csv)

    month_num = {"FEB": "02", "MAY": "05", "AUG": "08", "NOV": "11"}
    yaml_blocks: list[str] = []
    needs_review: list[str] = []

    for tag in [t.strip() for t in args.reviews.split(",") if t.strip()]:
        m = re.match(r"^([A-Za-z]{3})(\d{2})$", tag)
        if not m:
            print(f"Skipping {tag!r}: expected a form like May26")
            continue
        mon, yy = m.group(1).upper(), m.group(2)
        label = f"20{yy}-{month_num[mon]}"

        pdf = _download(tag)
        adds, dels, status = parse_india_block(_text(pdf))
        print(f"\n{'=' * 72}\n{tag}  ->  review {label}   ({status})\n{'=' * 72}")

        resolved: dict[str, list[str]] = {"additions": [], "deletions": []}
        for side, names in (("additions", adds), ("deletions", dels)):
            print(f"  {side.capitalize()} ({len(names)}):")
            for name in names:
                sym, score, how = resolver.resolve(name)
                flag = ""
                if sym is None:
                    flag = "  <-- UNRESOLVED, fill in by hand"
                    needs_review.append(f"{label} {side}: {name}")
                elif score < args.confident:
                    flag = f"  <-- CHECK (similarity {score:.2f})"
                    needs_review.append(f"{label} {side}: {name} -> {sym}?")
                print(f"    {name:<34}{sym or '?':<16}{how}{flag}")
                resolved[side].append(sym or f"UNKNOWN_{re.sub(r'[^A-Z0-9]', '', name.upper())}")

        block = [f"  {label}:",
                 f"    type: {'SAIR' if mon in ('MAY', 'NOV') else 'QIR'}",
                 "    standard:",
                 "      additions:"]
        block += [f"        - {s}" for s in resolved["additions"]] or ["        []"]
        block.append("      deletions:")
        block += [f"        - {s}" for s in resolved["deletions"]] or ["        []"]
        block.append(f"    sources:\n      - {URL.format(tag=tag)}")
        yaml_blocks.append("\n".join(block))

    print(f"\n{'=' * 72}")
    print("YAML for config/actual_reviews.yaml (merge under the existing `reviews:` key)")
    print("=" * 72)
    print("\n".join(yaml_blocks))

    if needs_review:
        print(f"\n{'=' * 72}")
        print(f"{len(needs_review)} name(s) need a human look before you trust them:")
        for item in needs_review:
            print(f"  {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
