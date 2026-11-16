#!/usr/bin/env python
"""Probe whether NSE's bhavcopy archive is reachable from this machine.

Purpose-built to answer one question before we build the daily cache updater: NSE
geo-fences and blocks datacentre IPs, and a GitHub Actions runner is a US-based
datacentre IP. If the runner cannot reach the archive, the whole daily workflow is
pointless, so this is worth confirming first and cheaply.

It mirrors the exact fetch path msci_predict/bhavcopy.py uses - curl_cffi Chrome
impersonation, the nseindia.com cookie warm-up, the Referer header - and probes
several recent weekdays, reporting per date whether the CDN answered with data,
answered 404 (reachable, just a holiday or a missing day), or refused.

The key distinction: a 404 still proves NSE is reachable. Only a 403, a connection
error or a timeout means this host is blocked.

    .venv/bin/python scripts/nse_reachability_check.py
    .venv/bin/python scripts/nse_reachability_check.py --days 15
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

# Kept deliberately in step with msci_predict/bhavcopy.py. If those constants ever
# change, this probe should change with them, since its whole value is fidelity to
# the real fetch path.
BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
)
NSE_HOME = "https://www.nseindia.com/"


def _recent_weekdays(n: int) -> list[date]:
    """The `n` most recent Mon-Fri dates, newest first, starting from yesterday.

    Yesterday rather than today because today's end-of-day file may not be published
    yet, and a spurious 404 on today would muddy a reachability read.
    """
    out: list[date] = []
    day = date.today() - timedelta(days=1)
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day -= timedelta(days=1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--days", type=int, default=12,
                    help="How many recent weekdays to probe (default: 12)")
    args = ap.parse_args()

    try:
        from curl_cffi import requests as cr
    except ImportError:
        print("curl_cffi is not installed. `pip install curl_cffi` "
              "(it is already in requirements.txt).")
        return 2

    session = cr.Session(impersonate="chrome")
    print(f"Warming {NSE_HOME} for cookies ...")
    try:
        warm = session.get(NSE_HOME, timeout=30)
        print(f"  landing page: HTTP {warm.status_code}")
    except Exception as exc:  # noqa: BLE001
        print(f"  landing page FAILED: {type(exc).__name__}: {exc}")
        print("\nVERDICT: BLOCKED - could not even reach nseindia.com from this host.")
        return 1

    print(f"\nProbing {args.days} recent weekdays of bhavcopy:\n")
    print(f"  {'date':<12}{'status':<8}{'first4':<12}{'bytes':<10}verdict")
    print("  " + "-" * 70)

    data_days = reachable = blocked = 0
    for day in _recent_weekdays(args.days):
        url = BHAVCOPY_URL.format(ddmmyyyy=day.strftime("%d%m%Y"))
        try:
            r = session.get(url, timeout=90, headers={"Referer": NSE_HOME})
        except Exception as exc:  # noqa: BLE001
            print(f"  {day.isoformat():<12}{'ERR':<8}{'':<12}{'':<10}"
                  f"{type(exc).__name__}: {exc}")
            blocked += 1
            continue

        head = bytes(r.content[:4])
        size = len(r.content)
        if r.status_code == 200 and head == b"SYMB":
            verdict = "DATA (trading day)"
            data_days += 1
            reachable += 1
        elif r.status_code == 200 and head[:2] == b"PK":
            verdict = "200 zip (reachable; unexpected form)"
            reachable += 1
        elif r.status_code == 404:
            verdict = "404 (reachable; holiday or missing)"
            reachable += 1
        elif r.status_code == 403:
            verdict = "403 BLOCKED"
            blocked += 1
        else:
            verdict = f"HTTP {r.status_code} (unexpected)"
        print(f"  {day.isoformat():<12}{r.status_code:<8}{repr(head):<12}{size:<10}{verdict}")

    print()
    if data_days:
        print(f"VERDICT: WORKING - {data_days} day(s) returned real bhavcopy data. "
              "This host\n  can reach NSE, so the daily workflow is viable here.")
        return 0
    if reachable:
        print("VERDICT: REACHABLE BUT NO DATA - every probe was a 404. NSE answered, so\n"
              "  this host is not blocked, but none of the probed days had a file. That is\n"
              "  unlikely across this many weekdays; re-run with a larger --days.")
        return 1
    print("VERDICT: BLOCKED - nothing reached NSE (403s or connection errors). This\n"
          "  host's IPs are blocked. On a GitHub runner this means you need a proxy or\n"
          "  an India-based self-hosted runner.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
