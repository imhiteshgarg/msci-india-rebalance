#!/usr/bin/env python
"""Rebuild the price panels from the committed daily bhavcopy files.

The durable store this project commits is the set of raw day files under
cache/bhavcopy/. The model, though, reads the compiled wide panels
(prices.csv, volumes.csv, adj_prices.csv) and their metadata, which are deliberately
NOT committed - they are large, and rebuilding them from the day files is exact.

So the flow after cloning or pulling is:

    git pull                                  # gets the latest day files
    .venv/bin/python scripts/hydrate_panels.py   # rebuilds the panels
    .venv/bin/python run_prediction.py ...    # reads the panels

This is the only step between "I have the day files" and "I can predict". It is also
what the daily GitHub Action does NOT do: the Action only appends day files, leaving
the (large, churny) panels to be rebuilt locally on demand, which keeps the committed
history small.

Phantom files - a holiday served the previous session's data - are rejected by
BhavcopySource.fetch_day on the DATE1 check, so they never enter the panels even if
one is still sitting on disk.

    .venv/bin/python scripts/hydrate_panels.py
    .venv/bin/python scripts/hydrate_panels.py --cache-dir cache_smoke
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--cache-dir", type=Path, default=ROOT / "cache",
                    help="Cache directory holding bhavcopy/ (default: cache)")
    args = ap.parse_args()

    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    from msci_predict.bhavcopy import BhavcopySource

    day_dir = args.cache_dir / "bhavcopy"
    files = sorted(day_dir.glob("*.csv"))
    if not files:
        raise SystemExit(
            f"No day files in {day_dir}. Pull the repo (which carries them) or run "
            "scripts/daily_bhavcopy_update.py to populate them first."
        )

    start = date.fromisoformat(files[0].stem)
    end = date.fromisoformat(files[-1].stem)
    print(f"Hydrating panels from {len(files)} day files, {start} .. {end}")

    # offline=True so this never reaches the network: it compiles only what is on
    # disk, which is the point - the day files are the source of truth here.
    source = BhavcopySource(args.cache_dir, offline=True)
    px, vol, adj = source.build_panels(start, end)

    px.to_csv(args.cache_dir / "prices.csv")
    vol.to_csv(args.cache_dir / "volumes.csv")
    adj.to_csv(args.cache_dir / "adj_prices.csv")

    meta_path = args.cache_dir / "price_panel_meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            meta = {}
    meta.update({
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "symbols": sorted(set(px.columns)),
        "rows": int(px.shape[0]),
        "source": "bhavcopy",
    })
    meta_path.write_text(json.dumps(meta, indent=0))

    print(f"Wrote panels: {px.shape[0]} dates x {px.shape[1]} symbols")
    print(f"  {args.cache_dir / 'prices.csv'}")
    print(f"  {args.cache_dir / 'volumes.csv'}")
    print(f"  {args.cache_dir / 'adj_prices.csv'}")
    print(f"  {meta_path}")
    print("\nFX and share counts are separate inputs this script does not touch. If "
          "cache/fx_usdinr.csv\nor cache/security_info.json are missing, a prediction "
          "run will fetch them (needs network).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
