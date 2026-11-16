"""Pull the current MSCI size thresholds out of the GIMI methodology book.

Two sets of numbers in config/parameters.yaml are re-published by MSCI at every Index
Review and drift with global equity markets:

  * Equity Universe Minimum Size Requirement  (GIMI 2.2.3)
  * Global Minimum Size References for Large Cap / Standard / IMI  (GIMI 2.3.2.1)

Reading them out of a 192-page PDF by hand each quarter is how they end up stale, so
this does it instead. It downloads the book, extracts both, and shows a diff against
the current config. Nothing is written unless you pass --write.

    .venv/bin/python scripts/update_msci_parameters.py --review Feb2027
    .venv/bin/python scripts/update_msci_parameters.py --review Feb2027 --write

Source: https://www.msci.com/index-methodology
The per-review PDF follows the pattern
https://www.msci.com/eqb/methodology/meth_docs/MSCI_GIMIMethodology_<Mon><Year>.pdf
so --review Feb2027 resolves to MSCI_GIMIMethodology_Feb2027.pdf. If MSCI changes the
pattern, download the book yourself and pass --pdf.

Everything else in parameters.yaml is structural - the 15% EM liquidity thresholds,
the 0.15 FIF floor, the 1.5x and 2/3 buffers, the 50% free-float fraction, the
70/85/99 coverage targets. Those change only when MSCI amends the methodology after a
public consultation, which is rare and well telegraphed. Check them when the book's
"Summary of Changes" mentions them; otherwise leave them alone.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
URL_TEMPLATE = "https://www.msci.com/eqb/methodology/meth_docs/MSCI_GIMIMethodology_{tag}.pdf"


def _fetch(url: str, attempts: int = 4) -> bytes:
    """GET with retries. MSCI's CDN truncates a large PDF often enough to matter."""
    import time

    last = None
    for attempt in range(1, attempts + 1):
        try:
            try:
                from curl_cffi import requests as cr  # type: ignore

                resp = cr.get(url, impersonate="chrome", timeout=240)
            except ImportError:
                import requests

                resp = requests.get(
                    url, timeout=240,
                    headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
                )
            resp.raise_for_status()
            if resp.content.startswith(b"%PDF"):
                return resp.content
            last = f"not a PDF ({len(resp.content)} bytes)"
        except Exception as exc:  # noqa: BLE001
            last = str(exc).split("\n")[0]
        if attempt < attempts:
            print(f"  attempt {attempt} failed ({last}); retrying")
            time.sleep(2 * attempt)
    raise SystemExit(
        f"Could not download {url} after {attempts} attempts. Last error: {last}\n"
        "Download the methodology book by hand from https://www.msci.com/index-methodology "
        "and pass --pdf."
    )


def _download(url: str, dest: Path) -> Path:
    print(f"Downloading {url}")
    body = _fetch(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    print(f"Saved {dest} ({len(body) / 1e6:.1f} MB)")
    return dest


def _extract_text(pdf: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise SystemExit("pypdf is required: .venv/bin/pip install pypdf")
    reader = PdfReader(str(pdf))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _num(raw: str) -> float:
    return float(raw.replace(",", "").strip())


def extract_equity_universe_min_size(text: str) -> tuple[float | None, str]:
    """Find the Equity Universe Minimum Size Requirement for DM and EM (GIMI 2.2.3).

    The book states it in prose, e.g. "the Equity Universe Minimum Size Requirement
    was USD 585 million for the Developed and Emerging Markets". Frontier Markets has
    a separate, much smaller value in the same sentence, so the pattern is anchored on
    the Developed/Emerging wording to avoid picking that up.
    """
    patterns = [
        r"Equity\s+Universe\s+Minimum\s+Size\s+Requirement\s+was\s+USD\s+([\d,]+)\s*million"
        r"[^.]{0,80}?Developed\s+and\s+Emerging",
        r"Equity\s+Universe\s+Minimum\s+Size\s+Requirement\s+was\s+USD\s+([\d,]+)\s*million",
    ]
    flat = re.sub(r"\s+", " ", text)
    for i, pat in enumerate(patterns):
        m = re.search(pat, flat, re.IGNORECASE)
        if m:
            return _num(m.group(1)), "anchored on Developed and Emerging" if i == 0 else "first match"
    return None, "not found"


def extract_gmsr(text: str) -> tuple[dict[str, float] | None, str]:
    """Find the Developed Markets Global Minimum Size References (GIMI 2.3.2.1).

    The values live in a table whose text extraction collapses to a run of numbers
    under the labels Large Cap / Mid Cap / Small Cap, with the DM figure first, the EM
    figure (exactly half) second and Frontier third. Rather than trust column order,
    each row is matched on the label and the DM value is taken as the figure whose
    successor is half of it - that halving relationship is specified by the
    methodology, so it doubles as a validity check.
    """
    flat = re.sub(r"[ \t]+", " ", text)
    out: dict[str, float] = {}
    labels = {"large_cap": "Large Cap", "standard": "Mid Cap", "imi": "Small Cap"}
    for key, label in labels.items():
        for m in re.finditer(rf"\b{label}\b[^\n]*\n?((?:[\d,]+\s+){{2,4}})", flat):
            nums = [_num(n) for n in re.findall(r"[\d,]+", m.group(1)) if _num(n) > 0]
            for a, b in zip(nums, nums[1:]):
                if abs(b * 2 - a) / a < 0.02:        # b is half of a
                    out[key] = a
                    break
            if key in out:
                break
    if len(out) != 3:
        return None, f"found {sorted(out)} of large_cap/standard/imi"
    # Sanity: Large Cap > Standard > IMI by construction.
    if not out["large_cap"] > out["standard"] > out["imi"]:
        return None, f"values are not ordered as expected: {out}"
    return out, "matched on the DM/EM halving relationship"


_REVIEW_MONTH_ABBR = {2: "Feb", 5: "May", 8: "Aug", 11: "Nov"}


def _provenance_stamps(review: str) -> dict[str, str]:
    """The gmsr.as_of and gmsr.published_at_review values implied by --review.

    Neither stamp is read by the model, but leaving them untouched while patching
    the four numbers beneath them is worse than carrying no stamp at all: it is how
    config/parameters_2026-02.yaml and its siblings all ended up holding their own
    review's size values while claiming to come from the November 2026 book.

    Both are derivable. `as_of` is the first candidate price cut-off date for the
    review, which is the convention the existing value already follows - the
    November 2026 review's window opens on 19 October 2026, and that is exactly
    what sits in the file today.

    Returns an empty dict when --review was not given, since --pdf mode has no
    review to derive from and guessing would be worse than leaving the stamps alone.
    """
    if not review:
        print("  No --review given, so gmsr.as_of and gmsr.published_at_review are "
              "left unchanged. Set them by hand.")
        return {}
    from msci_predict.calendar_rules import build_review_calendar, parse_review_label

    try:
        year, month = parse_review_label(review)
    except ValueError as exc:
        print(f"  Could not read a review from {review!r} ({exc}), so the provenance "
              "stamps are left unchanged.")
        return {}
    cal = build_review_calendar(year, month)
    return {
        "as_of": cal.price_cutoff_candidates[0].isoformat(),
        "published_at_review": f"{_REVIEW_MONTH_ABBR[month]} {year}",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--review", default="",
                    help="Methodology edition, e.g. Feb2027. Resolves the MSCI URL.")
    ap.add_argument("--pdf", type=Path, default=None,
                    help="Use a methodology PDF you already have instead of downloading")
    ap.add_argument("--write", action="store_true",
                    help="Apply the values to config/parameters.yaml")
    ap.add_argument("--params", type=Path, default=ROOT / "config" / "parameters.yaml")
    args = ap.parse_args()

    if args.pdf:
        pdf = args.pdf
        if not pdf.exists():
            raise SystemExit(f"Not found: {pdf}")
    elif args.review:
        pdf = _download(
            URL_TEMPLATE.format(tag=args.review), ROOT / "research" / f"gimi_{args.review}.pdf"
        )
    else:
        raise SystemExit("Pass --review Feb2027 or --pdf /path/to/book.pdf")

    print("Extracting text ...")
    text = _extract_text(pdf)

    eumsr, eumsr_how = extract_equity_universe_min_size(text)
    gmsr, gmsr_how = extract_gmsr(text)

    import yaml

    current = yaml.safe_load(args.params.read_text())
    cur_eumsr = float(current["equity_universe"]["min_size_usd_mn"])
    cur_gmsr = {k: float(v) for k, v in current["gmsr"]["dm"].items()}

    print()
    print("=" * 78)
    print("MSCI size parameters, in USD millions")
    print("=" * 78)
    print(f"  {'parameter':<34}{'current':>12}{'in book':>12}{'change':>12}")
    print("-" * 78)

    rows = [("equity_universe.min_size_usd_mn", cur_eumsr, eumsr)]
    for key in ("large_cap", "standard", "imi"):
        rows.append((f"gmsr.dm.{key}", cur_gmsr.get(key), (gmsr or {}).get(key)))

    changed = False
    for name, old, new in rows:
        if new is None:
            print(f"  {name:<34}{old:>12,.0f}{'NOT FOUND':>12}{'-':>12}")
            continue
        delta = f"{(new - old) / old:+.1%}" if old else "n/a"
        print(f"  {name:<34}{old:>12,.0f}{new:>12,.0f}{delta:>12}")
        if abs(new - old) > 0.5:
            changed = True
    print("-" * 78)
    print(f"  Equity Universe Minimum Size: {eumsr_how}")
    print(f"  Global Minimum Size References: {gmsr_how}")

    if gmsr:
        em_std = gmsr["standard"] * 0.5
        print()
        print(f"  Implied EM Standard reference : USD {em_std:,.0f} mn (half of DM)")
        print(f"  Implied EM Standard size range: USD {em_std * 0.5:,.0f} - "
              f"{em_std * 1.15:,.0f} mn  (0.5x to 1.15x)")
        print("  India's Standard cut-off must fall inside that range, whatever the")
        print("  coverage calculation says. Sanity-check your calibrated cut-off against it.")

    if eumsr is None or gmsr is None:
        print()
        print("Extraction was incomplete. Read the values out of the book directly:")
        print("  Equity Universe Minimum Size Requirement -> section 2.2.3")
        print("  Global Minimum Size References table     -> section 2.3.2.1")
        return 1

    if not args.write:
        print()
        print("Nothing written. Re-run with --write to apply."
              if changed else "Config already matches the book. Nothing to do.")
        return 0

    # Patch in place with a line-level edit so comments and layout survive, which
    # matters here: every value in this file carries its GIMI section reference.
    lines = args.params.read_text().splitlines(keepends=True)
    targets = {
        "min_size_usd_mn": eumsr,
        "large_cap": gmsr["large_cap"],
        "standard": gmsr["standard"],
        "imi": gmsr["imi"],
    }
    stamps = _provenance_stamps(args.review)
    applied: set[str] = set()
    applied_stamps: set[str] = set()
    in_dm = False
    in_gmsr = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("dm:"):
            in_dm = True
        elif stripped and not line.startswith((" ", "\t")):
            in_dm = False
            in_gmsr = stripped.startswith("gmsr:")
        for key, value in targets.items():
            if key in applied:
                continue
            if key in ("large_cap", "standard", "imi") and not in_dm:
                continue
            m = re.match(rf"^(\s*{re.escape(key)}:\s*)([\d.]+)(.*)$", line)
            if m:
                lines[i] = f"{m.group(1)}{value:.1f}{m.group(3)}\n"
                applied.add(key)
                break
        if not in_gmsr:
            continue
        for key, value in stamps.items():
            if key in applied_stamps:
                continue
            m = re.match(rf'^(\s*{re.escape(key)}:\s*)"[^"]*"(.*)$', line)
            if m:
                lines[i] = f'{m.group(1)}"{value}"{m.group(2)}\n'
                applied_stamps.add(key)
                break
    missing = set(targets) - applied
    if missing:
        raise SystemExit(f"Could not locate these keys in {args.params}: {sorted(missing)}")
    for key in sorted(set(stamps) - applied_stamps):
        print(f"  Warning: could not locate gmsr.{key} to stamp; set it by hand.")

    args.params.write_text("".join(lines))
    print()
    print(f"Updated {args.params}")
    for key in sorted(applied_stamps):
        print(f"  gmsr.{key} stamped as {stamps[key]!r}")
    print("Now re-calibrate the cut-off, since the size range has moved:")
    print("  .venv/bin/python scripts/calibrate_cutoff.py --review <last known review>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
