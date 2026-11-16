"""Data acquisition for the Indian equity universe.

Three things are needed per security: a price and volume history, a share count, and
a free-float estimate. The first is easy and reliable; the second is reliable but
only available as a current snapshot; the third is the weak link and is where a paid
provider would earn its fee.

Caching is aggressive and on by default. Re-running a prediction should not refetch
anything, both to stay polite to the sources and because a stable cache is what
makes a back-test reproducible.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

NSE_ARCHIVES = "https://nsearchives.nseindia.com/content"
NSE_TOTAL_MARKET = f"{NSE_ARCHIVES}/indices/ind_niftytotalmarket_list.csv"
NSE_NIFTY_500 = f"{NSE_ARCHIVES}/indices/ind_nifty500list.csv"
NSE_EQUITY_LIST = f"{NSE_ARCHIVES}/equities/EQUITY_L.csv"

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _http_get_text(url: str, timeout: int = 60) -> str:
    """GET a URL as text, impersonating a browser TLS fingerprint when possible.

    NSE sits behind a CDN that rejects plain-Python TLS handshakes, so curl_cffi's
    Chrome impersonation is used when it is installed, with requests as fallback.
    """
    try:
        from curl_cffi import requests as cr  # type: ignore

        resp = cr.get(url, impersonate="chrome", timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except ImportError:
        import requests

        resp = requests.get(url, headers={"User-Agent": _BROWSER_UA}, timeout=timeout)
        resp.raise_for_status()
        return resp.text


# ---------------------------------------------------------------------------
# Static per-security attributes
# ---------------------------------------------------------------------------


@dataclass
class SecurityStatic:
    """Slow-moving attributes of a security.

    `shares_outstanding` and `free_float_shares` are current snapshots from the
    price source. They are applied to historical dates unchanged, which is a known
    approximation discussed in the README: fine over a two-week price cut-off
    window, increasingly wrong the further back a back-test reaches.
    """

    symbol: str
    name: str = ""
    sector: str = ""
    shares_outstanding: float | None = None
    free_float_shares: float | None = None
    held_pct_insiders: float | None = None
    market_cap_inr: float | None = None
    listing_date: date | None = None
    fif_source: str = ""

    @property
    def fif(self) -> float | None:
        """Foreign Inclusion Factor, approximated by the free-float share fraction.

        MSCI builds this from a shareholder-by-shareholder classification into
        strategic and non-strategic holders (GIMI Appendix VI). The approximation
        here is float shares over total shares, falling back to one minus insider
        holding. Both track MSCI's number for ordinary companies and drift from it
        for names with cross-holdings, government stakes or lock-ins, which is
        exactly the population where index decisions are marginal.
        """
        if self.shares_outstanding and self.free_float_shares:
            if self.shares_outstanding > 0:
                return min(1.0, self.free_float_shares / self.shares_outstanding)
        if self.held_pct_insiders is not None:
            return max(0.0, 1.0 - self.held_pct_insiders)
        return None


# ---------------------------------------------------------------------------


def _recorded_request_end(end: date, prev_end: str | None, px: pd.DataFrame) -> str:
    """The `requested_end` value to persist in the panel metadata.

    Cache freshness is judged against the range previously *requested* rather than
    against what came back, and for the start of the range that is deliberate: a
    stock listed in 2024 will never have 2022 rows, so comparing the needed start
    against the earliest row present marks the cache permanently incomplete and
    refetches the world on every run.

    The end of the range is a different case, because no symbol can have rows after
    the last trading day - that is a global bound, not a per-symbol one. Recording a
    requested end beyond the data lets the cache claim coverage it does not have,
    which is how one reach for a future review window left the panel asserting it
    held prices months past its final row: every later run read that, judged itself
    fresh, returned the stale panel, and the screen then found no candidate price
    cut-off date with any prices on it. So the recorded end is capped at the last
    row actually held, which also repairs a metadata file already overstating it.
    """
    recorded = max(end.isoformat(), prev_end) if prev_end else end.isoformat()
    if not px.empty:
        recorded = min(recorded, px.index.max().date().isoformat())
    return recorded


class DataStore:
    """Fetches and caches everything the pipeline needs."""

    def __init__(self, cache_dir: Path, offline: bool = False, price_source: str = "bhavcopy"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.offline = offline
        # "bhavcopy" is the default because it is the exchange's own end-of-day file:
        # one request per trading day covers every security, nothing rate-limits it,
        # and a symbol cannot go missing from a day that downloaded. "yfinance" is
        # kept as an alternative and for cross-checking.
        self.price_source = price_source

    # -- paths ------------------------------------------------------------
    @property
    def _info_path(self) -> Path:
        return self.cache_dir / "security_info.json"

    @property
    def _prices_path(self) -> Path:
        return self.cache_dir / "prices.csv"

    @property
    def _adj_prices_path(self) -> Path:
        return self.cache_dir / "adj_prices.csv"

    @property
    def _volumes_path(self) -> Path:
        return self.cache_dir / "volumes.csv"

    @property
    def _fx_path(self) -> Path:
        return self.cache_dir / "fx_usdinr.csv"

    @property
    def _listings_path(self) -> Path:
        return self.cache_dir / "nse_equity_list.csv"

    @property
    def _panel_meta_path(self) -> Path:
        return self.cache_dir / "price_panel_meta.json"

    def _read_panel_meta(self) -> dict:
        if self._panel_meta_path.exists():
            try:
                return json.loads(self._panel_meta_path.read_text())
            except json.JSONDecodeError:
                return {}
        return {}

    # -- universe ---------------------------------------------------------
    def nse_listings(self, refresh: bool = False) -> pd.DataFrame:
        """Full NSE equity master list: symbol, name, ISIN and listing date."""
        if self._listings_path.exists() and not refresh:
            df = pd.read_csv(self._listings_path)
        else:
            if self.offline:
                raise FileNotFoundError(
                    f"{self._listings_path} missing and running offline. "
                    "Run once without --offline to populate the cache."
                )
            text = _http_get_text(NSE_EQUITY_LIST)
            df = pd.read_csv(io.StringIO(text))
            df.columns = [c.strip() for c in df.columns]
            df.to_csv(self._listings_path, index=False)
        df.columns = [c.strip() for c in df.columns]
        df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip()
        df["DATE OF LISTING"] = pd.to_datetime(
            df["DATE OF LISTING"], format="%d-%b-%Y", errors="coerce"
        )
        return df

    def _index_members(self, url: str, cache_name: str, refresh: bool = False) -> list[str]:
        path = self.cache_dir / cache_name
        if path.exists() and not refresh:
            text = path.read_text()
        else:
            if self.offline:
                raise FileNotFoundError(f"{path} missing and running offline.")
            text = _http_get_text(url)
            path.write_text(text)
        rows = list(csv.DictReader(io.StringIO(text)))
        return [r["Symbol"].strip() for r in rows if r.get("Symbol")]

    def build_universe(
        self,
        ipo_lookback_months: int = 36,
        extra_symbols: list[str] | None = None,
        refresh: bool = False,
    ) -> list[str]:
        """Candidate symbol list for the India investable universe.

        Nifty Total Market (750 names) covers essentially all of India's free-float
        market cap, which is what the size-segment cutoff calculation needs. Recent
        listings are added separately because a company that IPO'd in the last few
        months is not yet in any NSE index yet can still enter the MSCI Standard
        Index through the large-IPO waiver. Meesho in November 2026 is the case in
        point, and a universe built from index membership alone would have missed it.

        `refresh` has to reach these NSE lists, not just the price data. They are
        the only place a brand-new listing can enter the universe, so a stale copy
        silently excludes every IPO since it was cached - which is precisely the
        population most likely to be added at the next review.
        """
        symbols: set[str] = set()
        try:
            symbols.update(
                self._index_members(
                    NSE_TOTAL_MARKET, "ind_niftytotalmarket_list.csv", refresh=refresh
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not load Nifty Total Market list: %s", exc)
        try:
            symbols.update(
                self._index_members(NSE_NIFTY_500, "ind_nifty500list.csv", refresh=refresh)
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not load Nifty 500 list: %s", exc)

        try:
            listings = self.nse_listings(refresh=refresh)
            cutoff = pd.Timestamp.today() - pd.DateOffset(months=ipo_lookback_months)
            recent = listings[
                (listings["DATE OF LISTING"] >= cutoff)
                & (listings["SERIES"].astype(str).str.strip() == "EQ")
            ]
            symbols.update(recent["SYMBOL"].tolist())
            log.info("Added %d listings from the last %d months", len(recent), ipo_lookback_months)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not load NSE equity master list: %s", exc)

        if extra_symbols:
            symbols.update(s.strip().upper() for s in extra_symbols if s.strip())

        if not symbols:
            raise RuntimeError("Universe is empty; no NSE source could be reached.")
        return sorted(symbols)

    # -- per-security info ------------------------------------------------
    def fetch_security_info(
        self, symbols: list[str], refresh: bool = False, sleep: float = 0.0
    ) -> dict[str, SecurityStatic]:
        """Share counts, float shares and sector per symbol, cached to disk.

        This is the slow step: the price source exposes these only one ticker at a
        time. Expect roughly a second per name on a cold cache.
        """
        cached: dict[str, dict] = {}
        if self._info_path.exists() and not refresh:
            cached = json.loads(self._info_path.read_text())

        missing = [s for s in symbols if s not in cached]
        if missing and self.offline:
            log.warning("%d symbols missing from info cache while offline; skipping them", len(missing))
            missing = []

        if missing:
            import yfinance as yf

            log.info("Fetching company info for %d symbols (cached: %d)", len(missing), len(cached))
            for i, sym in enumerate(missing, start=1):
                try:
                    info = yf.Ticker(f"{sym}.NS").info or {}
                    cached[sym] = {
                        "name": info.get("longName") or info.get("shortName") or sym,
                        "sector": info.get("sector") or "",
                        "shares_outstanding": info.get("sharesOutstanding"),
                        "free_float_shares": info.get("floatShares"),
                        "held_pct_insiders": info.get("heldPercentInsiders"),
                        "market_cap_inr": info.get("marketCap"),
                        "currency": info.get("currency"),
                    }
                except Exception as exc:  # noqa: BLE001
                    log.debug("info failed for %s: %s", sym, exc)
                    cached[sym] = {"name": sym, "error": str(exc)}
                if i % 50 == 0:
                    log.info("  ... %d/%d", i, len(missing))
                    self._info_path.write_text(json.dumps(cached, indent=0))
                if sleep:
                    time.sleep(sleep)
            self._info_path.write_text(json.dumps(cached, indent=0))

        listing_dates: dict[str, date] = {}
        try:
            listings = self.nse_listings()
            for _, row in listings.iterrows():
                if pd.notna(row["DATE OF LISTING"]):
                    listing_dates[row["SYMBOL"]] = row["DATE OF LISTING"].date()
        except Exception:  # noqa: BLE001
            pass

        out: dict[str, SecurityStatic] = {}
        for sym in symbols:
            raw = cached.get(sym, {})
            if raw.get("currency") not in (None, "INR"):
                # A non-INR quote means the ticker resolved to the wrong listing.
                log.debug("Skipping %s: quote currency %s", sym, raw.get("currency"))
                continue
            static = SecurityStatic(
                symbol=sym,
                name=raw.get("name") or sym,
                sector=raw.get("sector") or "",
                shares_outstanding=raw.get("shares_outstanding"),
                free_float_shares=raw.get("free_float_shares"),
                held_pct_insiders=raw.get("held_pct_insiders"),
                market_cap_inr=raw.get("market_cap_inr"),
                listing_date=listing_dates.get(sym),
            )
            if static.shares_outstanding and static.free_float_shares:
                static.fif_source = "float_shares"
            elif static.held_pct_insiders is not None:
                static.fif_source = "1_minus_insiders"
            out[sym] = static
        return out

    # -- prices -----------------------------------------------------------
    def fetch_price_panel(
        self,
        symbols: list[str],
        start: date,
        end: date,
        refresh: bool = False,
        batch_size: int = 100,
        min_coverage: float = 0.70,
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """Daily close, volume and adjusted-close panels, one column per symbol.

        The adjusted series is not a nicety. Raw closes combined with a current
        share count give nonsense market caps before any split or bonus issue: a
        company that did a 1:1 bonus looks twice as large as it was. The adjusted
        series is used to carry the share count backwards (see
        `Predictor._full_mcap_usd_mn`), while raw closes stay in use for traded
        value, where the actual transacted price is what matters.
        """
        paths = (self._prices_path, self._volumes_path, self._adj_prices_path)
        cached: tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame] | None = None
        if all(p.exists() for p in paths):
            cached = (
                pd.read_csv(self._prices_path, index_col=0, parse_dates=True),
                pd.read_csv(self._volumes_path, index_col=0, parse_dates=True),
                pd.read_csv(self._adj_prices_path, index_col=0, parse_dates=True),
            )

        if cached is not None and not refresh:
            px, vol, adj = cached
            meta = self._read_panel_meta()
            prev_start = meta.get("requested_start")
            prev_end = meta.get("requested_end")
            missing = [s for s in symbols if s not in set(px.columns)]

            # Judge coverage by what was previously *requested*, not by what came
            # back. A stock listed in 2024 will never have 2022 data, so comparing
            # the needed start against the earliest row present marks the cache
            # permanently incomplete and triggers a full refetch on every run. That
            # is how identical runs started producing different answers: each
            # refetch loses a different random handful of tickers to transient
            # upstream failures, which shifts the universe and the derived cut-off.
            range_covered = bool(
                prev_start and prev_end
                and date.fromisoformat(prev_start) <= start
                and date.fromisoformat(prev_end) >= end - timedelta(days=7)
            )
            if not missing and range_covered:
                log.info("Using cached price panel: %s dates x %s symbols", *px.shape)
                return px, vol, adj
            reasons = []
            if missing:
                reasons.append(f"{len(missing)} symbol(s) not cached")
            if not range_covered:
                reasons.append(
                    f"cached request covered {prev_start}..{prev_end}, need {start}..{end}"
                )
            log.info("Extending price cache: %s", "; ".join(reasons))

        if self.offline:
            raise FileNotFoundError("Price cache incomplete and running offline.")

        if self.price_source == "bhavcopy":
            return self._fetch_panel_bhavcopy(symbols, start, end, cached, min_coverage)

        # Always fetch at least the union of this request and every previous one, so
        # the cache only ever grows. Fetching a narrower window and writing the
        # result back is what corrupted the cache: a back-test of May 2026 asks for
        # data ending 30 April, every symbol listed after that date has nothing in
        # the window and gets dropped, and the panel shrinks a little more each run.
        meta = self._read_panel_meta()
        if meta.get("requested_start"):
            start = min(start, date.fromisoformat(meta["requested_start"]))
        if meta.get("requested_end"):
            end = max(end, date.fromisoformat(meta["requested_end"]))
        if cached is not None and not cached[0].empty:
            start = min(start, cached[0].index.min().date())
            end = max(end, cached[0].index.max().date())
        log.info("Fetching prices for the widened range %s .. %s", start, end)

        import yfinance as yf

        closes, volumes, adjusted = [], [], []
        tickers = [f"{s}.NS" for s in symbols]
        end_str = (pd.Timestamp(end) + pd.Timedelta(days=1)).date().isoformat()
        failed_batches = 0
        for i in range(0, len(tickers), batch_size):
            batch = tickers[i : i + batch_size]
            log.info("Downloading prices %d-%d of %d", i + 1, min(i + batch_size, len(tickers)), len(tickers))

            # Yahoo rate-limits heavy use with HTTP 429 and simply returns nothing.
            # Without backoff the run continues on a fraction of the universe, which
            # silently moves the size cut-off and changes the predictions. Retrying
            # is much cheaper than a confidently wrong answer.
            raw = None
            for attempt in range(1, 5):
                try:
                    raw = yf.download(
                        batch,
                        start=start.isoformat(),
                        end=end_str,
                        progress=False,
                        auto_adjust=False,
                        group_by="column",
                        threads=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.debug("batch error: %s", exc)
                    raw = None
                if raw is not None and not raw.empty:
                    break
                if attempt < 4:
                    wait = 5 * (2 ** (attempt - 1))
                    log.warning(
                        "Empty response for %d tickers (likely rate limited); "
                        "retrying in %ds", len(batch), wait,
                    )
                    time.sleep(wait)
            if raw is None or raw.empty:
                failed_batches += 1
                log.error("Gave up on a batch of %d tickers after 4 attempts", len(batch))
                continue
            adj_key = "Adj Close" if "Adj Close" in raw.columns.get_level_values(0) else "Close"
            if isinstance(raw.columns, pd.MultiIndex):
                closes.append(raw["Close"])
                volumes.append(raw["Volume"])
                adjusted.append(raw[adj_key])
            else:
                # Single-ticker responses come back flat.
                name = batch[0]
                closes.append(raw[["Close"]].rename(columns={"Close": name}))
                volumes.append(raw[["Volume"]].rename(columns={"Volume": name}))
                adjusted.append(raw[[adj_key]].rename(columns={adj_key: name}))

        if not closes:
            raise RuntimeError(
                "No price data returned for any symbol. Yahoo Finance rate-limits "
                "heavy use; wait a few minutes and re-run. The cache is left untouched."
            )

        def _tidy(frames: list[pd.DataFrame]) -> pd.DataFrame:
            df = pd.concat(frames, axis=1)
            df.columns = [c.replace(".NS", "") for c in df.columns]
            df = df.loc[:, ~df.columns.duplicated()].sort_index()
            df.index = pd.to_datetime(df.index).tz_localize(None)
            return df

        px, vol, adj = _tidy(closes), _tidy(volumes), _tidy(adjusted)
        # Drop names with no usable history at all.
        keep = [c for c in px.columns if px[c].notna().sum() > 0]
        px, vol = px[keep], vol[[c for c in keep if c in vol.columns]]
        adj = adj[[c for c in keep if c in adj.columns]]

        if cached is not None:
            # Merge rather than replace. A fetch that loses a few tickers to a
            # transient upstream error must not delete data that was already good,
            # otherwise the universe silently changes between runs and so do the
            # predictions. New values win on overlap; old values survive elsewhere.
            def _merge(fresh: pd.DataFrame, old: pd.DataFrame) -> pd.DataFrame:
                combined = fresh.combine_first(old)
                return combined.sort_index()

            before = set(cached[0].columns)
            px = _merge(px, cached[0])
            vol = _merge(vol, cached[1])
            adj = _merge(adj, cached[2])
            recovered = before - set(_tidy(closes).columns)
            if recovered:
                log.info(
                    "Kept %d symbol(s) from cache that this fetch did not return: %s",
                    len(recovered), ", ".join(sorted(recovered)[:8]),
                )

        # Refuse to proceed on a badly incomplete universe. The size-segment cut-off
        # is derived from cumulative free-float coverage of the whole investable
        # universe, so a missing third of the market shifts it by more than the
        # buffers that decide membership. An error here is far better than a
        # plausible-looking prediction built on a fraction of the market.
        coverage = len(px.columns) / max(1, len(symbols))
        if coverage < min_coverage:
            raise RuntimeError(
                f"Only {len(px.columns)} of {len(symbols)} requested symbols have price "
                f"data ({coverage:.0%}, below the {min_coverage:.0%} floor).\n"
                f"  {failed_batches} batch(es) returned nothing, which usually means Yahoo "
                "Finance is rate limiting.\n"
                "  The cache has been left unchanged. Wait several minutes and re-run, or "
                "pass a lower --min-price-coverage if you knowingly want a partial universe."
            )
        if failed_batches:
            log.warning(
                "%d batch(es) failed but coverage is %.0f%%, above the floor; proceeding",
                failed_batches, coverage * 100,
            )

        px.to_csv(self._prices_path)
        vol.to_csv(self._volumes_path)
        adj.to_csv(self._adj_prices_path)

        meta = self._read_panel_meta()
        prev_start = meta.get("requested_start")
        prev_end = meta.get("requested_end")
        self._panel_meta_path.write_text(json.dumps({
            "requested_start": min(start.isoformat(), prev_start) if prev_start else start.isoformat(),
            "requested_end": _recorded_request_end(end, prev_end, px),
            "symbols": sorted(set(px.columns)),
            "rows": int(px.shape[0]),
        }, indent=0))
        log.info("Price panel: %d dates x %d symbols", *px.shape)
        return px, vol, adj

    # -- FX ---------------------------------------------------------------
    def fetch_fx(self, start: date, end: date, ticker: str = "INR=X", refresh: bool = False) -> pd.Series:
        """USD/INR daily close, forward-filled onto the NSE calendar by the caller.

        Every MSCI threshold is in US dollars, so the rupee rate is not a detail:
        a 3% currency move shifts every stock's dollar market cap by 3% relative to
        a cutoff that is itself denominated in dollars.
        """
        if self._fx_path.exists() and not refresh:
            s = pd.read_csv(self._fx_path, index_col=0, parse_dates=True).iloc[:, 0]
            if not s.empty and s.index.min().date() <= start:
                return s
        if self.offline:
            raise FileNotFoundError("FX cache missing and running offline.")

        s = None
        try:
            import yfinance as yf

            raw = yf.download(
                ticker,
                start=start.isoformat(),
                end=(pd.Timestamp(end) + pd.Timedelta(days=5)).date().isoformat(),
                progress=False,
                auto_adjust=False,
            )
            if raw is not None and not raw.empty:
                s = raw["Close"]
                if isinstance(s, pd.DataFrame):
                    s = s.iloc[:, 0]
        except Exception as exc:  # noqa: BLE001
            log.warning("Yahoo FX fetch failed (%s); trying the fallback", exc)

        if s is None or s.empty:
            log.warning("No FX from Yahoo; falling back to Frankfurter")
            s = fetch_fx_frankfurter(start, end)

        s.index = pd.to_datetime(s.index).tz_localize(None)
        s.name = "usdinr"
        s.to_frame().to_csv(self._fx_path)
        return s


# ---------------------------------------------------------------------------
# Free-float overrides
# ---------------------------------------------------------------------------


def load_fif_overrides(path: Path | None) -> dict[str, tuple[float, str]]:
    """Read manual FIF overrides from CSV: symbol, fif, note.

    The automated float estimate is the model's biggest weakness. This hook exists
    so a known corporate action can be entered by hand. LIC's August 2026 Offer For
    Sale is the canonical example: the float roughly doubled, and that single input
    is the whole reason it qualified in November.
    """
    if not path or not Path(path).exists():
        return {}
    out: dict[str, tuple[float, str]] = {}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            sym = (row.get("symbol") or "").strip().upper()
            raw = (row.get("fif") or "").strip()
            if not sym or sym.startswith("#") or not raw:
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            if value > 1:          # tolerate percentages
                value /= 100.0
            out[sym] = (value, (row.get("note") or "").strip())
    return out


def load_fif_history(path: Path | None) -> dict[str, list[tuple[date, float, str]]]:
    """Read dated free-float changes from CSV: symbol, effective_date, fif, note.

    This is the input that most separates a free model from a paid one. Without it
    the current float estimate gets applied to every historical date, which does
    not merely add noise, it inverts conclusions. Replaying the index's history with
    LIC's post-Offer-For-Sale float makes LIC look eligible since 2022, so the
    simulation adds it years early and then reports its actual November 2026
    inclusion as "already a member".

    Entries are sorted by date; the applicable float for a date is the latest entry
    on or before it, falling back to the automated estimate when none applies.
    """
    if not path or not Path(path).exists():
        return {}
    out: dict[str, list[tuple[date, float, str]]] = {}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            sym = (row.get("symbol") or "").strip().upper()
            raw_date = (row.get("effective_date") or "").strip()
            raw_fif = (row.get("fif") or "").strip()
            if not sym or sym.startswith("#") or not raw_date or not raw_fif:
                continue
            try:
                eff = datetime.fromisoformat(raw_date).date()
                value = float(raw_fif)
            except ValueError:
                log.warning("Skipping malformed free-float history row: %s", row)
                continue
            if value > 1:
                value /= 100.0
            out.setdefault(sym, []).append((eff, value, (row.get("note") or "").strip()))
    for sym in out:
        out[sym].sort(key=lambda t: t[0])
    return out


# ---------------------------------------------------------------------------
# Derived series
# ---------------------------------------------------------------------------


def trading_calendar(px: pd.DataFrame, fraction_of_typical: float = 0.5) -> pd.DatetimeIndex:
    """Dates on which the exchange was open, inferred from the price panel.

    This is the denominator for Frequency of Trading, so it has to reflect the
    exchange calendar rather than any single stock's history. A date counts as a
    trading day when at least `fraction_of_typical` of the usual number of names
    report a price, which filters out the stray rows yfinance emits for holidays
    without assuming any particular universe size.
    """
    counts = px.notna().sum(axis=1)
    if counts.empty:
        return pd.DatetimeIndex([])
    threshold = max(1.0, float(counts.median()) * fraction_of_typical)
    return px.index[counts >= threshold]


def excess_returns(
    px: pd.DataFrame,
    symbol: str,
    as_of: pd.Timestamp,
    peers: list[str],
    windows: list[int],
) -> dict[int, float]:
    """Excess return of `symbol` over its peer group for each look-back window.

    GIMI 2.3.6.3 compares a security's return to the average return of IMI
    constituents in the same country-sector. Windows are counted in Mon-Fri days
    before the price cut-off date, which is how MSCI specifies them.
    """
    if symbol not in px.columns:
        return {}
    series = px[symbol].dropna()
    series = series[series.index <= as_of]
    if series.empty:
        return {}
    peer_cols = [p for p in peers if p in px.columns and p != symbol]
    out: dict[int, float] = {}
    for w in windows:
        ref = as_of - pd.tseries.offsets.BDay(w)
        past = series[series.index <= ref]
        if past.empty:
            continue
        own = (series.iloc[-1] / past.iloc[-1] - 1) * 100
        peer_rets = []
        for p in peer_cols:
            ps = px[p].dropna()
            ps = ps[ps.index <= as_of]
            pp = ps[ps.index <= ref]
            if ps.empty or pp.empty:
                continue
            peer_rets.append((ps.iloc[-1] / pp.iloc[-1] - 1) * 100)
        peer_avg = float(np.mean(peer_rets)) if peer_rets else 0.0
        out[w] = float(own - peer_avg)
    return out


def load_known_non_constituents(path: Path | None, as_of: date | None = None) -> set[str]:
    """Read symbols known not to be index members, honouring the `as_of` column.

    The date filter is what keeps a back-test honest. A note published in September
    tells you nothing you could have acted on at a July price cut-off, so rows dated
    after `as_of` are dropped. Rows without a date are always applied, on the
    assumption they describe a standing fact rather than a point-in-time read.
    """
    if not path or not Path(path).exists():
        return set()
    out: set[str] = set()
    dropped = 0
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            sym = (row.get("symbol") or "").strip().upper()
            if not sym or sym.startswith("#"):
                continue
            raw = (row.get("as_of") or "").strip()
            if raw and as_of:
                try:
                    if datetime.fromisoformat(raw).date() > as_of:
                        dropped += 1
                        continue
                except ValueError:
                    pass
            out.add(sym)
    if dropped:
        log.info(
            "Ignored %d known-non-constituent rows dated after %s to avoid hindsight",
            dropped, as_of,
        )
    return out


def _merge_panels(fresh: pd.DataFrame, old: pd.DataFrame) -> pd.DataFrame:
    """Union of two panels, new values winning on overlap.

    Merging rather than replacing is what keeps a partial fetch from deleting data
    that was already good. Replacing is how the cache previously shrank a little on
    every back-test run until the universe was a fifth of the market.
    """
    return fresh.combine_first(old).sort_index()


def _store_panels(
    store: "DataStore",
    px: pd.DataFrame,
    vol: pd.DataFrame,
    adj: pd.DataFrame,
    start: date,
    end: date,
) -> None:
    px.to_csv(store._prices_path)
    vol.to_csv(store._volumes_path)
    adj.to_csv(store._adj_prices_path)
    meta = store._read_panel_meta()
    prev_start, prev_end = meta.get("requested_start"), meta.get("requested_end")
    store._panel_meta_path.write_text(json.dumps({
        "requested_start": min(start.isoformat(), prev_start) if prev_start else start.isoformat(),
        "requested_end": _recorded_request_end(end, prev_end, px),
        "symbols": sorted(set(px.columns)),
        "rows": int(px.shape[0]),
        "source": store.price_source,
    }, indent=0))


def _fetch_panel_bhavcopy(
    self: "DataStore",
    symbols: list[str],
    start: date,
    end: date,
    cached: tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame] | None,
    min_coverage: float,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Build the price panel from NSE bhavcopy files."""
    from .bhavcopy import BhavcopySource

    meta = self._read_panel_meta()
    if meta.get("requested_start"):
        start = min(start, date.fromisoformat(meta["requested_start"]))
    if meta.get("requested_end"):
        end = max(end, date.fromisoformat(meta["requested_end"]))
    if cached is not None and not cached[0].empty:
        start = min(start, cached[0].index.min().date())
        end = max(end, cached[0].index.max().date())

    log.info("Building price panel from NSE bhavcopy, %s .. %s", start, end)
    source = BhavcopySource(self.cache_dir, offline=self.offline)
    px, vol, adj = source.build_panels(start, end, symbols=None)

    # Keep every symbol bhavcopy returned. The extra names cost nothing, and a
    # company only enters the screen once a share count exists for it, so a wider
    # panel can only improve the universe later without changing it now.
    if cached is not None:
        px, vol, adj = (
            _merge_panels(px, cached[0]),
            _merge_panels(vol, cached[1]),
            _merge_panels(adj, cached[2]),
        )

    requested_present = [s for s in symbols if s in px.columns]
    coverage = len(requested_present) / max(1, len(symbols))
    if coverage < min_coverage:
        raise RuntimeError(
            f"Only {len(requested_present)} of {len(symbols)} requested symbols appear in "
            f"the bhavcopy panel ({coverage:.0%}, below the {min_coverage:.0%} floor).\n"
            "  The cache has been left unchanged. Check that NSE archives are reachable."
        )

    _store_panels(self, px, vol, adj, start, end)
    log.info(
        "Price panel from bhavcopy: %d dates x %d symbols (%d of %d requested present)",
        px.shape[0], px.shape[1], len(requested_present), len(symbols),
    )
    return px, vol, adj


DataStore._fetch_panel_bhavcopy = _fetch_panel_bhavcopy


def fetch_fx_frankfurter(start: date, end: date) -> pd.Series:
    """USD/INR daily from Frankfurter, as a keyless fallback when Yahoo is throttled.

    Frankfurter publishes ECB reference rates, which sit close to but not exactly on
    spot: late October 2026 reads about 1% away from the Yahoo series. That matters,
    because every MSCI threshold is in dollars and a 1% currency shift moves every
    market cap 1% against a cut-off calibrated in dollars. Use it to get unblocked,
    then re-calibrate, and do not mix the two sources inside one back-test.
    """
    from curl_cffi import requests as cr

    url = (
        f"https://api.frankfurter.app/{start.isoformat()}..{end.isoformat()}"
        "?from=USD&to=INR"
    )
    resp = cr.get(url, impersonate="chrome", timeout=90)
    resp.raise_for_status()
    rates = (resp.json() or {}).get("rates") or {}
    if not rates:
        raise RuntimeError("Frankfurter returned no USD/INR rates")
    series = pd.Series(
        {pd.Timestamp(day): float(vals["INR"]) for day, vals in rates.items() if "INR" in vals}
    ).sort_index()
    series.name = "usdinr"
    log.warning(
        "Using Frankfurter ECB reference rates for USD/INR, not spot. Expect roughly "
        "1%% difference from the Yahoo series; re-calibrate the cut-off if you switch."
    )
    return series
