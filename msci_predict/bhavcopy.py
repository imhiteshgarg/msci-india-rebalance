"""NSE bhavcopy price source.

An alternative to Yahoo Finance for the daily price and volume panel, and on the
merits the better primary source:

  * It is the exchange's own end-of-day file, not a redistributor's copy.
  * One request covers every listed security for a trading day. Three years of
    history is about 740 requests instead of 1,300 per-ticker requests, and a symbol
    can never be "missing" from a day that downloaded successfully.
  * It carries actual turnover alongside close and volume.
  * No rate limiting observed, whereas Yahoo answers heavy use with HTTP 429 and an
    empty frame, which previously let a run proceed on a fraction of the universe.

File: https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv
Columns: SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE,
LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES,
DELIV_QTY, DELIV_PER.

ONE THING BHAVCOPY DOES NOT GIVE YOU
------------------------------------
A split-adjusted price series. Closes are raw. The model needs an adjusted series to
carry a current share count backwards without a bonus issue making the company look
twice its real size.

That is recoverable from the file itself. NSE's PREV_CLOSE is adjusted for corporate
actions, so when PREV_CLOSE on day t differs from CLOSE on day t-1 the ratio is the
adjustment factor for whatever happened overnight. Accumulating those ratios rebuilds
an adjusted series without any second data source. Only discontinuities above a
threshold are treated as capital changes, so ordinary dividends are ignored: a
dividend changes the price without changing the share count, which is exactly what the
adjustment must not absorb.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
)
NSE_HOME = "https://www.nseindia.com/"

# Equity series worth keeping. EQ is the ordinary rolling-settlement series; BE is
# trade-for-trade, used for securities under surveillance, which still trade and can
# still be index constituents. Everything else (debt, ETFs, rights) is excluded.
EQUITY_SERIES = {"EQ", "BE", "BZ"}

# A single-day price discontinuity above this is treated as a capital change rather
# than a dividend. Indian splits and bonuses are rarely smaller; dividends rarely
# larger, outside occasional specials.
CAPITAL_CHANGE_THRESHOLD = 0.12


@dataclass
class DayData:
    trade_date: date
    closes: dict[str, float]
    volumes: dict[str, float]
    prev_closes: dict[str, float]
    turnovers: dict[str, float]


class BhavcopySource:
    """Downloads and caches NSE daily bhavcopy files."""

    def __init__(self, cache_dir: Path, offline: bool = False):
        self.cache_dir = Path(cache_dir) / "bhavcopy"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.offline = offline
        self._session = None
        # Days NSE has no file for are holidays. Remembering them stops a rebuild
        # re-requesting ~15 holidays a year on every run.
        self._holiday_marker = self.cache_dir / "_holidays.txt"
        self._holidays: set[str] = set()
        if self._holiday_marker.exists():
            self._holidays = {
                line.strip() for line in self._holiday_marker.read_text().splitlines() if line.strip()
            }

    def _get_session(self):
        if self._session is None:
            from curl_cffi import requests as cr

            self._session = cr.Session(impersonate="chrome")
            try:
                self._session.get(NSE_HOME, timeout=30)
            except Exception as exc:  # noqa: BLE001
                log.debug("NSE landing page fetch failed: %s", exc)
        return self._session

    def _record_holiday(self, day: date) -> None:
        """Remember that NSE has no file for a date, so a rebuild stops re-asking.

        Only dates strictly in the past are recorded, and that restriction is the
        whole point. NSE answers a date that has not happened yet with exactly the
        same 404 it gives a genuine exchange holiday - verified against both - so
        there is no way to tell them apart from the response alone. Recording the
        future therefore blacklists it permanently: a forward-looking run that
        reached for the next review's price window wrote every weekday of it in
        here, and because `fetch_day` short-circuits on this set before issuing any
        request, not even --refresh could recover those dates.

        Today is excluded as well as the future, because NSE publishes the day's
        bhavcopy after the close. A run earlier in the session must not conclude the
        market was shut.
        """
        if day >= date.today():
            log.debug("Not recording %s as a holiday: it is not in the past yet", day)
            return
        self._holidays.add(day.isoformat())
        self._holiday_marker.write_text("\n".join(sorted(self._holidays)))

    def _path(self, day: date) -> Path:
        return self.cache_dir / f"{day.isoformat()}.csv"

    def fetch_day(self, day: date, retries: int = 3) -> DayData | None:
        """One trading day, or None when the market was closed."""
        if day > date.today():
            # No file can exist for a date that has not happened. Skip without
            # recording anything, so the date is tried again once it arrives.
            return None
        if day.isoformat() in self._holidays:
            return None
        path = self._path(day)
        text: str | None = None
        if path.exists():
            text = path.read_text()
        elif self.offline:
            return None
        else:
            url = BHAVCOPY_URL.format(ddmmyyyy=day.strftime("%d%m%Y"))
            session = self._get_session()
            for attempt in range(1, retries + 1):
                try:
                    resp = session.get(url, timeout=90, headers={"Referer": NSE_HOME})
                except Exception as exc:  # noqa: BLE001
                    log.debug("%s attempt %d failed: %s", day, attempt, exc)
                    time.sleep(2 * attempt)
                    continue
                if resp.status_code == 404 or resp.content[:4] != b"SYMB":
                    # A 404 is how NSE reports an exchange holiday. _record_holiday
                    # decides whether the date is old enough for that to be a
                    # permanent fact.
                    self._record_holiday(day)
                    return None
                text = resp.text
                path.write_text(text)
                break
            if text is None:
                log.warning("Could not fetch bhavcopy for %s", day)
                return None

        closes, volumes, prev, turnover = {}, {}, {}, {}
        for row in csv.DictReader(io.StringIO(text)):
            clean = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
            if clean.get("SERIES") not in EQUITY_SERIES:
                continue
            sym = clean.get("SYMBOL", "")
            if not sym:
                continue

            def num(key: str) -> float | None:
                raw = clean.get(key, "")
                if raw in ("", "-", None):
                    return None
                try:
                    return float(raw)
                except ValueError:
                    return None

            close = num("CLOSE_PRICE")
            if close is None or close <= 0:
                continue
            closes[sym] = close
            volumes[sym] = num("TTL_TRD_QNTY") or 0.0
            pc = num("PREV_CLOSE")
            if pc is not None and pc > 0:
                prev[sym] = pc
            # TURNOVER_LACS is in hundred-thousands of rupees.
            tv = num("TURNOVER_LACS")
            if tv is not None:
                turnover[sym] = tv * 1e5
        return DayData(day, closes, volumes, prev, turnover)

    def build_panels(
        self,
        start: date,
        end: date,
        symbols: list[str] | None = None,
        progress_every: int = 50,
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """Close, volume and adjusted-close panels over a date range.

        The adjusted panel is reconstructed from PREV_CLOSE discontinuities, as
        described in the module docstring.
        """
        wanted = set(symbols) if symbols else None
        days: list[DayData] = []
        cursor = start
        requested = 0
        while cursor <= end:
            if cursor.weekday() < 5:
                requested += 1
                data = self.fetch_day(cursor)
                if data is not None:
                    days.append(data)
                if progress_every and requested % progress_every == 0:
                    log.info("  bhavcopy %s (%d trading days collected)", cursor, len(days))
            cursor += timedelta(days=1)

        if not days:
            raise RuntimeError(f"No bhavcopy data between {start} and {end}.")
        log.info("Bhavcopy: %d trading days from %s to %s",
                 len(days), days[0].trade_date, days[-1].trade_date)

        all_syms = sorted({s for d in days for s in d.closes} & wanted) if wanted else \
            sorted({s for d in days for s in d.closes})
        index = pd.DatetimeIndex([pd.Timestamp(d.trade_date) for d in days])

        px = pd.DataFrame(
            [[d.closes.get(s, np.nan) for s in all_syms] for d in days],
            index=index, columns=all_syms,
        )
        vol = pd.DataFrame(
            [[d.volumes.get(s, np.nan) for s in all_syms] for d in days],
            index=index, columns=all_syms,
        )
        prev = pd.DataFrame(
            [[d.prev_closes.get(s, np.nan) for s in all_syms] for d in days],
            index=index, columns=all_syms,
        )
        adj = _adjusted_from_prev_close(px, prev)
        return px, vol, adj


def _adjusted_from_prev_close(px: pd.DataFrame, prev: pd.DataFrame) -> pd.DataFrame:
    """Rebuild a split and bonus adjusted series from PREV_CLOSE discontinuities.

    NSE restates PREV_CLOSE for corporate actions, so

        ratio(t) = PREV_CLOSE(t) / CLOSE(t-1)

    is 1.0 on an ordinary day and equals the capital-change factor across a split or
    bonus: after a 1:1 bonus, PREV_CLOSE is half the previous close, giving 0.5.

    Working backwards from the latest date and multiplying by each factor produces a
    series on today's share basis. Only factors beyond
    `CAPITAL_CHANGE_THRESHOLD` are applied, so dividends are left alone - they move
    the price without changing the share count, and absorbing them would distort the
    market caps this series exists to compute.
    """
    close_prev_day = px.shift(1)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = prev / close_prev_day
    ratio = ratio.replace([np.inf, -np.inf], np.nan)

    # Keep only material discontinuities; everything else is treated as unchanged.
    is_capital_change = (ratio - 1.0).abs() > CAPITAL_CHANGE_THRESHOLD
    factors = ratio.where(is_capital_change, 1.0).fillna(1.0)

    # Cumulative factor to restate older prices onto the current share basis.
    # Reverse cumulative product of the factors strictly after each date.
    reversed_factors = factors.iloc[::-1]
    cum_forward = reversed_factors.cumprod().iloc[::-1]
    # cum_forward at date t includes factor(t); divide it out so the factor applies
    # only to dates before the change.
    cumulative = cum_forward / factors
    adj = px * cumulative

    changes = int(is_capital_change.sum().sum())
    if changes:
        log.info("Reconstructed adjusted prices across %d capital change(s)", changes)
    return adj
