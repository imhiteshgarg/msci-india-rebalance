"""Orchestration: turn raw market data into an MSCI India Standard Index prediction.

The central design choice is that the model does not produce one answer. MSCI picks
its price cut-off at random from the last ten business days of the month before the
review, and nobody outside MSCI knows which one. So the pipeline runs the full
screen on every candidate date and reports, per stock, on how many of those dates it
would have qualified. A name that clears on ten out of ten is a different
proposition from one that clears on three, and collapsing both to "predicted
addition" throws away the most useful thing the model knows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from . import rules
from .calendar_rules import ReviewCalendar, history_start_date
from .data import DataStore, SecurityStatic, load_fif_overrides, trading_calendar

log = logging.getLogger(__name__)

_MONTHS_IN_YEAR = 12
_DAYS_PER_MONTH = 30.4375


# ---------------------------------------------------------------------------


def load_parameters(path: Path) -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh)


def thresholds_from_params(params: dict) -> rules.Thresholds:
    eu = params["equity_universe"]
    liq = params["liquidity"]
    fif = params["fif"]
    fin = params["final_investability"]
    buf = params["buffers"]
    mlt = params["min_length_of_trading"]
    epi = params["extreme_price_increase"]
    return rules.Thresholds(
        eu_min_size_usd_mn=float(eu["min_size_usd_mn"]),
        eu_min_ffmc_fraction=float(eu["min_ffmc_fraction_of_min_size"]),
        atvr_3m_min=float(liq["new_constituent"]["atvr_3m_min_pct"]),
        atvr_12m_min=float(liq["new_constituent"]["atvr_12m_min_pct"]),
        fot_3m_min=float(liq["new_constituent"]["fot_3m_min_pct"]),
        existing_atvr_12m_min=float(liq["existing_constituent"]["atvr_12m_min_pct"]),
        existing_atvr_3m_min=float(liq["existing_constituent"]["atvr_3m_min_pct"]),
        existing_fot_3m_min=float(liq["existing_constituent"]["fot_3m_min_pct"]),
        max_price_usd=float(liq["max_price_usd"]),
        min_fif=float(fif["min_fif"]),
        low_fif_ffmc_multiple=float(fif["low_fif_ffmc_multiple"]),
        epi_exempt_fif=float(fif["epi_exempt_fif"]),
        standard_min_ffmc_fraction=float(fin["standard_min_ffmc_fraction_of_cutoff"]),
        existing_constituent_relief=float(fin["existing_constituent_relief"]),
        add_multiple=float(buf["add_multiple"]),
        delete_multiple=float(buf["delete_multiple"]),
        min_listing_months=float(mlt["months"]),
        waiver_full_mcap_multiple=float(mlt["waiver_full_mcap_multiple"]),
        waiver_ffmc_multiple=float(mlt["waiver_ffmc_multiple"]),
        epi_thresholds_pct={int(k): float(v) for k, v in epi["thresholds_pct"].items()},
        epi_enabled=bool(epi.get("enabled", True)),
    )


def em_standard_size_range(params: dict) -> rules.SizeRange:
    """The Emerging Markets Standard Global Minimum Size Range, in USD millions.

    EM references are exactly half the Developed Markets ones (GIMI 2.3.2.1), and
    the range spans 0.5x to 1.15x the reference. On August 2026 data that puts
    India's Standard cutoff somewhere between USD 4.07bn and USD 9.36bn whatever
    the local coverage calculation says, which is a useful sanity bound on the
    single most important parameter in the model.
    """
    g = params["gmsr"]
    dm_standard = float(g["dm"]["standard"])
    em_reference = dm_standard * float(g["em_multiplier_of_dm"])
    return rules.SizeRange.from_reference(
        em_reference,
        lower_multiple=float(g["range_lower_multiple"]),
        upper_multiple=float(g["range_upper_multiple"]),
    )


# ---------------------------------------------------------------------------


@dataclass
class DateResult:
    """Screen output for one candidate price cut-off date."""

    price_cutoff: date
    cutoff: rules.CutoffResult
    usdinr: float
    investable_count: int
    additions: list[rules.Verdict]
    deletions: list[rules.Verdict]
    all_verdicts: dict[str, rules.Verdict]


@dataclass
class Prediction:
    """Aggregated prediction across all candidate price cut-off dates."""

    calendar: ReviewCalendar
    date_results: list[DateResult]
    add_frequency: dict[str, int]
    delete_frequency: dict[str, int]
    names: dict[str, str]
    cutoff_range: tuple[float, float]
    cutoff_mean: float
    constituent_source: str
    constituents: set[str]
    # Names that passed every published rule but whose free-float estimate is below
    # the confidence bar. Reported, not counted as predictions.
    flagged_frequency: dict[str, int] = field(default_factory=dict)
    diagnostics: dict[str, object] = field(default_factory=dict)

    @property
    def n_dates(self) -> int:
        return len(self.date_results)

    def ranked_additions(self) -> list[tuple[str, int, float]]:
        """(symbol, dates passed, mean full-mcap multiple of cutoff), best first."""
        out = []
        for sym, count in self.add_frequency.items():
            mults = [
                r.all_verdicts[sym].size_multiple
                for r in self.date_results
                if sym in r.all_verdicts
            ]
            out.append((sym, count, float(np.mean(mults)) if mults else float("nan")))
        return sorted(out, key=lambda t: (-t[1], -t[2]))

    def ranked_deletions(self) -> list[tuple[str, int, float]]:
        out = []
        for sym, count in self.delete_frequency.items():
            mults = [
                r.all_verdicts[sym].size_multiple
                for r in self.date_results
                if sym in r.all_verdicts
            ]
            out.append((sym, count, float(np.mean(mults)) if mults else float("nan")))
        return sorted(out, key=lambda t: (-t[1], t[2]))


# ---------------------------------------------------------------------------


class Predictor:
    def __init__(
        self,
        params: dict,
        calendar: ReviewCalendar,
        store: DataStore,
        fif_overrides: dict[str, tuple[float, str]] | None = None,
        fif_history: dict[str, list[tuple[date, float, str]]] | None = None,
        constituents: set[str] | None = None,
        non_constituents: set[str] | None = None,
        known_members: set[str] | None = None,
        strategic_gap: dict[str, float] | None = None,
        min_float_confidence: str = "LOW",
        cutoff_mode: str = "derive",
        fixed_cutoff_usd_mn: float | None = None,
        universe_symbols: list[str] | None = None,
    ):
        self.params = params
        self.cal = calendar
        self.store = store
        self.th = thresholds_from_params(params)
        self.size_range = em_standard_size_range(params)
        self.fif_overrides = fif_overrides or {}
        self.fif_history = fif_history or {}
        self.explicit_constituents = constituents
        self.known_non_constituents = (non_constituents or set()) - (known_members or set())
        # Names MSCI has announced as additions in an earlier review. Forced into
        # the membership set so the replay cannot nominate them a second time.
        self.known_members = known_members or set()
        # SEBI public holding minus the estimated float, in percentage points. A
        # large gap means MSCI's strategic-holder judgement drives the number.
        self.strategic_gap = strategic_gap or {}
        self.min_float_confidence = min_float_confidence
        self.min_price_coverage = 0.70
        self.cutoff_mode = cutoff_mode
        self.fixed_cutoff = fixed_cutoff_usd_mn
        self.universe_symbols = universe_symbols

        self.static: dict[str, SecurityStatic] = {}
        self.px: pd.DataFrame = pd.DataFrame()
        self.vol: pd.DataFrame = pd.DataFrame()
        self.adj: pd.DataFrame = pd.DataFrame()
        self.anchor: pd.Timestamp | None = None
        self.fx: pd.Series = pd.Series(dtype=float)
        self.calendar_days: pd.DatetimeIndex = pd.DatetimeIndex([])
        self.liquidity: dict[str, list[rules.MonthlyLiquidity]] = {}
        self.atvr_cache: dict[str, tuple[float | None, float | None, float | None, int]] = {}
        self.sectors: dict[str, str] = {}
        self._epi_cache: dict[pd.Timestamp, dict[str, dict[int, float]]] = {}
        self._constituent_history: list[str] = []
        self._anchor_derived: float | None = None

    # -- setup ------------------------------------------------------------
    def prepare(self, refresh: bool = False) -> None:
        symbols = self.universe_symbols or self.store.build_universe()
        log.info("Universe: %d symbols", len(symbols))

        self.static = self.store.fetch_security_info(symbols, refresh=refresh)
        symbols = sorted(self.static)

        # Long enough to replay a dozen prior Index Reviews when reconstructing the
        # constituent list, and to cover the 250-day Extreme Price Increase window.
        start = history_start_date(self.cal, months_of_history=42)
        end = max(self.cal.price_cutoff_candidates[-1], self.cal.liquidity_cutoff)
        self.px, self.vol, self.adj = self.store.fetch_price_panel(
            symbols, start, end, refresh=refresh, min_coverage=self.min_price_coverage
        )

        usable = [s for s in symbols if s in self.px.columns]
        dropped = len(symbols) - len(usable)
        if dropped:
            log.info("Dropped %d symbols with no price history", dropped)
        self.static = {s: self.static[s] for s in usable}

        self.fx = self.store.fetch_fx(start, end, ticker=self.params["market"]["fx_ticker"], refresh=refresh)
        self.calendar_days = trading_calendar(self.px)
        log.info("Trading calendar: %d days from %s to %s",
                 len(self.calendar_days), self.calendar_days.min().date(), self.calendar_days.max().date())

        self.sectors = {s: (self.static[s].sector or "UNKNOWN") for s in self.static}
        # The share count and float are a snapshot taken now, so market caps are
        # carried backwards relative to the most recent candidate price cut-off date.
        # Anchoring there rather than at "today" makes the review-window figures
        # exact and confines the adjustment drift to the historical replay.
        candidates = [
            pd.Timestamp(d) for d in self.cal.price_cutoff_candidates
            if pd.Timestamp(d) <= self.px.index.max()
        ]
        self.anchor = candidates[-1] if candidates else self.px.index.max()
        self._apply_fif_overrides()
        self._build_liquidity()

    def _apply_fif_overrides(self) -> None:
        for sym, (value, note) in self.fif_overrides.items():
            if sym in self.static:
                st = self.static[sym]
                if st.shares_outstanding:
                    st.free_float_shares = st.shares_outstanding * value
                    st.held_pct_insiders = 1.0 - value
                else:
                    st.held_pct_insiders = 1.0 - value
                st.fif_source = f"override:{note}" if note else "override"
                log.info("FIF override %s -> %.3f %s", sym, value, f"({note})" if note else "")

    def fif(self, sym: str) -> float | None:
        return self.static[sym].fif if sym in self.static else None

    def fif_at(self, sym: str, as_of: pd.Timestamp) -> float | None:
        """Foreign Inclusion Factor applicable on a date.

        Uses the dated history when one is supplied for the symbol, otherwise the
        current estimate. See `data.load_fif_history` for why the dated version
        matters.
        """
        return self.fif_at_with_staleness(sym, as_of)[0]

    def fif_at_with_staleness(
        self, sym: str, as_of: pd.Timestamp
    ) -> tuple[float | None, float | None]:
        """Applicable FIF and how many months newer its source is than `as_of`.

        Staleness of zero means a dated observation on or before the cut-off is in
        force. A positive number means no dated observation existed and the current
        snapshot is being projected backwards, which is the condition that produced
        the historical false positives.
        """
        entries = self.fif_history.get(sym)
        if entries:
            applicable = [e for e in entries if e[0] <= as_of.date()]
            if applicable:
                return applicable[-1][1], 0.0
        current = self.fif(sym)
        if current is None:
            return None, None
        newest = self.px.index.max().date()
        months = max(0.0, (newest - as_of.date()).days / _DAYS_PER_MONTH)
        return current, months

    def _full_mcap_inr(self, sym: str, as_of: pd.Timestamp) -> float | None:
        """Full market cap in rupees, with the share count carried back correctly.

        The share count is a current snapshot, so applying it to an old raw close
        overstates market cap by the factor of any split or bonus issue since. The
        adjusted-close ratio undoes that:

            mcap(t) = shares_now x close(anchor) x adj(t) / adj(anchor)

        At the anchor date this reduces to shares x price, so the review-window
        numbers are unaffected. Going back, it is exact for splits and bonuses and
        drifts slowly with dividends, which is an acceptable trade for the
        historical replay and is the reason Coforge no longer looks twice its real
        size before its bonus issue.
        """
        st = self.static.get(sym)
        if st is None or not st.shares_outstanding:
            return None
        anchor_close = self._close_on(sym, self.anchor)
        if anchor_close is None:
            return None
        if as_of >= self.anchor or sym not in self.adj.columns:
            close = self._close_on(sym, as_of)
            return close * st.shares_outstanding if close is not None else None

        adj = self.adj[sym].dropna()
        adj_t = adj[adj.index <= as_of]
        adj_anchor = adj[adj.index <= self.anchor]
        if adj_t.empty or adj_anchor.empty or adj_anchor.iloc[-1] <= 0:
            close = self._close_on(sym, as_of)
            return close * st.shares_outstanding if close is not None else None
        ratio = float(adj_t.iloc[-1]) / float(adj_anchor.iloc[-1])
        return anchor_close * st.shares_outstanding * ratio

    # -- liquidity --------------------------------------------------------
    def _build_liquidity(self) -> None:
        """Build the monthly liquidity series that feed ATVR and Frequency of Trading.

        Note the window: liquidity is frozen at the *liquidity* cut-off (end of
        September for a November review), one month before the price cut-off. Using
        October volumes here would be a look-ahead error and would flatter any stock
        that rallied on index-inclusion speculation in that month, which is exactly
        the population being screened.
        """
        months = self.cal.liquidity_months(_MONTHS_IN_YEAR)
        min_days = int(self.params["liquidity"]["min_trading_days_for_monthly_atvr"])

        month_days: dict[tuple[int, int], pd.DatetimeIndex] = {}
        for y, m in months:
            mask = (self.calendar_days.year == y) & (self.calendar_days.month == m)
            month_days[(y, m)] = self.calendar_days[mask]

        for sym, st in self.static.items():
            shares = st.shares_outstanding
            fif = st.fif
            series: list[rules.MonthlyLiquidity] = []
            for (y, m) in months:
                days = month_days[(y, m)]
                if len(days) == 0:
                    continue
                closes = self.px[sym].reindex(days)
                vols = self.vol[sym].reindex(days) if sym in self.vol.columns else pd.Series(index=days, dtype=float)
                traded_value = (closes * vols).dropna()
                traded_value = traded_value[traded_value > 0]
                last_close = closes.dropna()
                ffmc_end = 0.0
                if shares and fif and not last_close.empty:
                    ffmc_end = float(last_close.iloc[-1]) * shares * fif
                series.append(
                    rules.MonthlyLiquidity(
                        year=y,
                        month=m,
                        daily_traded_values=tuple(float(v) for v in traded_value.values),
                        days_traded=int(len(traded_value)),
                        possible_trading_days=int(len(days)),
                        ffmc_month_end=ffmc_end,
                    )
                )
            self.liquidity[sym] = series
            self.atvr_cache[sym] = (
                rules.atvr(series, 3, min_days),
                rules.atvr(series, _MONTHS_IN_YEAR, min_days),
                rules.frequency_of_trading(series, 3),
                rules.atvr_months_used(series, _MONTHS_IN_YEAR, min_days),
            )

    # -- point-in-time snapshot -------------------------------------------
    def _usdinr(self, as_of: pd.Timestamp) -> float:
        s = self.fx[self.fx.index <= as_of].dropna()
        if s.empty:
            raise RuntimeError(f"No USD/INR rate available on or before {as_of.date()}")
        return float(s.iloc[-1])

    def _close_on(self, sym: str, as_of: pd.Timestamp) -> float | None:
        s = self.px[sym]
        s = s[s.index <= as_of].dropna()
        return float(s.iloc[-1]) if not s.empty else None

    def _months_listed(self, sym: str, as_of: pd.Timestamp) -> float:
        st = self.static[sym]
        first = st.listing_date
        if first is None:
            series = self.px[sym].dropna()
            if series.empty:
                return 0.0
            # The panel starts 18 months back, so anything present on day one has
            # been listed at least that long; treat it as comfortably seasoned.
            if series.index[0] <= self.px.index[0] + pd.Timedelta(days=3):
                return 999.0
            first = series.index[0].date()
        return max(0.0, (as_of.date() - first).days / _DAYS_PER_MONTH)

    def _epi_excess(self, as_of: pd.Timestamp) -> dict[str, dict[int, float]]:
        """Excess returns per symbol per EPI window, computed for the whole panel at once."""
        if as_of in self._epi_cache:
            return self._epi_cache[as_of]
        windows = sorted(self.th.epi_thresholds_pct)
        panel = self.px[self.px.index <= as_of]
        if panel.empty:
            return {}
        latest = panel.ffill().iloc[-1]
        sector_series = pd.Series(self.sectors)
        out: dict[str, dict[int, float]] = {s: {} for s in panel.columns}
        for w in windows:
            ref_date = as_of - pd.tseries.offsets.BDay(w)
            past_panel = panel[panel.index <= ref_date]
            if past_panel.empty:
                continue
            past = past_panel.ffill().iloc[-1]
            ret = (latest / past - 1.0) * 100.0
            ret = ret.replace([np.inf, -np.inf], np.nan)
            sectors = sector_series.reindex(ret.index).fillna("UNKNOWN")
            sector_mean = ret.groupby(sectors).transform("mean")
            excess = ret - sector_mean
            for sym, value in excess.items():
                if pd.notna(value):
                    out[sym][w] = float(value)
        self._epi_cache[as_of] = out
        return out

    def _securities_at(self, as_of: pd.Timestamp, constituents: set[str]) -> dict[str, rules.Security]:
        usdinr = self._usdinr(as_of)
        epi = self._epi_excess(as_of)
        out: dict[str, rules.Security] = {}
        for sym, st in self.static.items():
            close = self._close_on(sym, as_of)
            mcap_inr = self._full_mcap_inr(sym, as_of)
            if close is None or mcap_inr is None:
                continue
            fif, staleness = self.fif_at_with_staleness(sym, as_of)
            if fif is None:
                continue
            full_mcap_usd_mn = mcap_inr / usdinr / 1e6
            a3, a12, fot, used = self.atvr_cache.get(sym, (None, None, None, 0))
            out[sym] = rules.Security(
                symbol=sym,
                name=st.name,
                full_mcap_usd_mn=full_mcap_usd_mn,
                fif=fif,
                atvr_3m_pct=a3,
                atvr_12m_pct=a12,
                fot_3m_pct=fot,
                atvr_12m_months_used=used,
                fif_as_of_months_stale=staleness,
                strategic_gap_pp=self.strategic_gap.get(sym),
                months_since_listing=self._months_listed(sym, as_of),
                price_usd=close / usdinr,
                sector=self.sectors.get(sym, "UNKNOWN"),
                is_standard_constituent=sym in constituents,
                is_imi_constituent=sym in constituents,
                epi_excess_returns_pct=epi.get(sym, {}),
            )
        return out

    # -- cutoff -----------------------------------------------------------
    def _derive_cutoff(self, investable: list[rules.Security]) -> rules.CutoffResult:
        target = float(self.params["coverage_targets"]["standard"])
        return rules.derive_size_segment_cutoff(
            [(s.symbol, s.full_mcap_usd_mn, s.ffmc_usd_mn) for s in investable],
            self.size_range,
            coverage_target=target,
        )

    def _anchor_derived_cutoff(self) -> float:
        """The derived cut-off at the anchor date, used to rebase calibrated mode."""
        if self._anchor_derived is None:
            securities = self._securities_at(self.anchor, set())
            investable = [
                s for s in securities.values() if rules.passes_equity_universe(s, self.th)
            ]
            self._anchor_derived = self._derive_cutoff(investable).cutoff
        return self._anchor_derived

    def _as_result(
        self, cutoff: float, investable: list[rules.Security], raw: float
    ) -> rules.CutoffResult:
        total_ffmc = sum(s.ffmc_usd_mn for s in investable)
        above = [s for s in investable if s.full_mcap_usd_mn >= cutoff]
        return rules.CutoffResult(
            cutoff=cutoff,
            raw_coverage_cutoff=raw,
            segment_number_of_companies=len(above),
            coverage_achieved=(
                sum(s.ffmc_usd_mn for s in above) / total_ffmc if total_ffmc else 0.0
            ),
            size_range=self.size_range,
            clamped="",
            universe_ffmc_total=total_ffmc,
        )

    def _cutoff_at(self, securities: dict[str, rules.Security]) -> tuple[rules.CutoffResult, int]:
        investable = [s for s in securities.values() if rules.passes_equity_universe(s, self.th)]

        if self.cutoff_mode == "derive":
            return self._derive_cutoff(investable), len(investable)

        if not self.fixed_cutoff:
            raise ValueError(f"cutoff_mode={self.cutoff_mode!r} requires fixed_cutoff_usd_mn")

        if self.cutoff_mode == "fixed":
            # One constant level for every date. Simple, and wrong for the
            # historical replay: holding the cut-off at its late-2026 level across
            # 2023-2024 made Coforge cross the +50% buffer two years early. Use
            # 'calibrated' unless you specifically want a flat cut-off.
            return self._as_result(self.fixed_cutoff, investable, self.fixed_cutoff), len(investable)

        if self.cutoff_mode == "calibrated":
            # Take the *level* from the calibrated value and the *shape over time*
            # from the derived series. The derived cut-off is biased - the local
            # investable universe here is broader than MSCI's - but the bias is
            # roughly constant, so the ratio between two dates carries real
            # information even when the absolute level does not.
            derived = self._derive_cutoff(investable)
            anchor = self._anchor_derived_cutoff()
            scaled = self.fixed_cutoff * (derived.cutoff / anchor) if anchor else self.fixed_cutoff
            return self._as_result(scaled, investable, derived.raw_coverage_cutoff), len(investable)

        raise ValueError(f"Unknown cutoff_mode {self.cutoff_mode!r}")

    # -- constituents -----------------------------------------------------
    def _past_reviews(self) -> list[ReviewCalendar]:
        """Every review between the start of price history and the target review."""
        from .calendar_rules import build_review_calendar

        earliest = self.px.index.min()
        out: list[ReviewCalendar] = []
        year = earliest.year
        while True:
            for month in (2, 5, 8, 11):
                if (year, month) >= (self.cal.review_year, self.cal.review_month):
                    return out
                cal = build_review_calendar(year, month)
                if pd.Timestamp(cal.price_cutoff_candidates[0]) >= earliest:
                    out.append(cal)
            year += 1
            if year > self.cal.review_year + 1:
                return out

    def resolve_constituents(self) -> tuple[set[str], str]:
        """Decide which names to treat as current index members.

        An explicit list is always better, and `--constituents-file` takes
        precedence. Without one, the membership has to be reconstructed, and the
        naive reconstruction is wrong in a way that matters: taking everyone above
        the cutoff as a member would have flagged Coforge as an incumbent in
        November 2026 and so suppressed the single largest predicted addition. The
        buffers mean membership is path-dependent - a company can sit between 1.0x
        and 1.5x the cutoff indefinitely without ever being added.

        So the index is simulated forward instead. Seed with everyone above the
        cutoff at the oldest review covered by the price history, where the buffer
        history has washed out, then replay each subsequent review applying the
        +50% add and -33% delete buffers. The constituent count is a free sanity
        check: MSCI's own India Index factsheet reports roughly 165 constituents, so
        a simulation landing far from that is telling you something is wrong.

        One approximation: liquidity is taken from the single current snapshot for
        every historical review. Liquidity practically never binds for a company
        sitting at 1.5x the cutoff, so this is immaterial for membership, but it is
        a look-ahead and is flagged here rather than buried.
        """
        if self.explicit_constituents:
            # An explicit list is authoritative and is NOT filtered by the
            # known-non-constituents file. That file is a heuristic for the case
            # where membership has to be guessed, and the two sources genuinely
            # conflict: a note correctly calling a name a non-member in July says
            # nothing about October if MSCI added it in August. Letting the weaker
            # source veto the stronger one re-nominates names that are already in.
            members = set(self.explicit_constituents) | self.known_members
            conflicts = members & self.known_non_constituents
            if conflicts:
                log.info(
                    "Explicit constituent list overrides %d entr%s in the "
                    "known-non-constituents file: %s",
                    len(conflicts), "y" if len(conflicts) == 1 else "ies",
                    ", ".join(sorted(conflicts)),
                )
            return members, f"explicit list ({len(members)} names)"

        past = self._past_reviews()
        if not past:
            log.warning("Price history covers no prior review; cannot reconstruct membership")
            return set(), "unavailable"

        seed_cal = past[0]
        seed_ref = pd.Timestamp(seed_cal.price_cutoff_candidates[-1])
        securities = self._securities_at(seed_ref, set())
        cutoff, _ = self._cutoff_at(securities)

        # Seed with the full addition screen but no migration buffer. Size alone is
        # not enough: Hindustan Zinc sits at five times the cutoff on full market
        # cap and is nowhere near the index because its float is tiny. Applying
        # every investability test at the seed keeps those names out instead of
        # seeding them as members and then reporting them as spurious deletions.
        seed_th = replace(self.th, add_multiple=1.0)
        members = {
            sym
            for sym, sec in securities.items()
            if rules.screen_for_addition(sec, cutoff, seed_th).decision == "ADD"
        } - self.known_non_constituents
        members |= self.known_members & set(securities)
        log.info(
            "Seeded %d constituents at the %s review (cutoff USD %.0fmn, %s)",
            len(members), seed_cal.label, cutoff.cutoff, seed_ref.date(),
        )

        history: list[str] = [f"seed {seed_cal.label}: {len(members)}"]
        for cal in past[1:]:
            ref = pd.Timestamp(cal.price_cutoff_candidates[-1])
            if ref > self.px.index.max():
                break
            securities = self._securities_at(ref, members)
            cut, _ = self._cutoff_at(securities)
            adds, drops = set(), set()
            for sym, sec in securities.items():
                if sec.is_standard_constituent:
                    if rules.screen_for_deletion(sec, cut, self.th).decision == "DELETE":
                        drops.add(sym)
                elif rules.screen_for_addition(sec, cut, self.th).decision == "ADD":
                    adds.add(sym)
            members = ((members | adds) - drops) - self.known_non_constituents
            members |= {
                s for s in self.known_members
                if s in securities and s not in drops
            }
            history.append(f"{cal.label}: +{len(adds)} -{len(drops)} = {len(members)}")
            log.info(
                "Replayed %s review: +%d -%d -> %d members (cutoff USD %.0fmn)",
                cal.label, len(adds), len(drops), len(members), cut.cutoff,
            )

        self._constituent_history = history
        return members, (
            f"simulated forward from {seed_cal.label} through {past[-1].label} "
            f"({len(past)} reviews)"
        )

    def membership_trace(self, symbols: list[str]) -> str:
        """Replay the membership simulation, reporting one symbol's path review by review.

        This is the diagnostic that tells you *which input* is wrong when a known
        addition comes out flagged as an existing member. A name shown entering the
        simulated index two years before MSCI actually added it is almost always a
        float or share-count history problem, not a rules problem.
        """
        wanted = [s.upper() for s in symbols]
        past = self._past_reviews()
        if not past:
            return "No prior review is covered by the price history."

        lines = [
            "=" * 88,
            "MEMBERSHIP SIMULATION TRACE",
            "=" * 88,
            "Mcap/cut is full market cap over the cut-off. A non-member joins above 1.50 "
            "and an existing member leaves below 0.67.",
            "",
            f"  {'Review':<9}{'Date':<12}{'Cutoff':>8}  "
            + "".join(f"{s[:11]:>13}" for s in wanted),
        ]

        members: set[str] = set()
        seed_th = replace(self.th, add_multiple=1.0)
        for i, cal in enumerate(past):
            ref = pd.Timestamp(cal.price_cutoff_candidates[-1])
            if ref > self.px.index.max():
                break
            securities = self._securities_at(ref, members)
            cut, _ = self._cutoff_at(securities)
            if i == 0:
                members = {
                    sym
                    for sym, sec in securities.items()
                    if rules.screen_for_addition(sec, cut, seed_th).decision == "ADD"
                } - self.known_non_constituents
                members |= self.known_members & set(securities)
            else:
                adds, drops = set(), set()
                for sym, sec in securities.items():
                    if sec.is_standard_constituent:
                        if rules.screen_for_deletion(sec, cut, self.th).decision == "DELETE":
                            drops.add(sym)
                    elif rules.screen_for_addition(sec, cut, self.th).decision == "ADD":
                        adds.add(sym)
                members = ((members | adds) - drops) - self.known_non_constituents
                members |= {
                    s for s in self.known_members if s in securities and s not in drops
                }

            cells = []
            for sym in wanted:
                sec = securities.get(sym)
                if sec is None:
                    cells.append(f"{'-':>13}")
                    continue
                flag = "IN " if sym in members else "out"
                cells.append(f"{flag}{sec.full_mcap_usd_mn / cut.cutoff:>9.2f}".rjust(13))
            label = f"{cal.label}{' (seed)' if i == 0 else ''}"
            lines.append(
                f"  {label:<9}{str(ref.date()):<12}{cut.cutoff:>8.0f}  " + "".join(cells)
            )

        lines.append("")
        for sym in wanted:
            st = self.static.get(sym)
            if not st:
                lines.append(f"  {sym}: not in the screened universe")
                continue
            hist = self.fif_history.get(sym)
            fif_note = (
                "dated history: " + ", ".join(f"{d} -> {f:.3f}" for d, f, _ in hist)
                if hist
                else f"single current estimate {st.fif:.3f} applied to every date "
                f"(source: {st.fif_source or 'unknown'})"
            )
            lines.append(f"  {sym:<12} listed {st.listing_date}, float {fif_note}")
        return "\n".join(lines)

    # -- run --------------------------------------------------------------
    def run(self) -> Prediction:
        constituents, source = self.resolve_constituents()

        date_results: list[DateResult] = []
        add_freq: dict[str, int] = {}
        del_freq: dict[str, int] = {}
        flagged: dict[str, int] = {}
        names: dict[str, str] = {}

        latest_price_date = self.px.index.max()
        skipped_future = [
            d for d in self.cal.price_cutoff_candidates if pd.Timestamp(d) > latest_price_date
        ]
        if skipped_future and len(skipped_future) < len(self.cal.price_cutoff_candidates):
            log.warning(
                "PARTIAL WINDOW: %d of %d candidate price cut-off dates have no prices yet "
                "(latest price %s). Confidence is out of %d, not %d, so it is not comparable "
                "with a later run.",
                len(skipped_future), len(self.cal.price_cutoff_candidates),
                latest_price_date.date(),
                len(self.cal.price_cutoff_candidates) - len(skipped_future),
                len(self.cal.price_cutoff_candidates),
            )

        for d in self.cal.price_cutoff_candidates:
            ts = pd.Timestamp(d)
            if ts > latest_price_date:
                continue
            securities = self._securities_at(ts, constituents)
            cutoff, investable_count = self._cutoff_at(securities)

            verdicts: dict[str, rules.Verdict] = {}
            adds, dels = [], []
            bar = rules.CONFIDENCE_ORDER[self.min_float_confidence]
            for sym, sec in securities.items():
                names[sym] = sec.name
                if sec.is_standard_constituent:
                    v = rules.screen_for_deletion(sec, cutoff, self.th)
                    qualifies = v.decision == "DELETE"
                    bucket, freq = dels, del_freq
                else:
                    v = rules.screen_for_addition(sec, cutoff, self.th)
                    qualifies = v.decision == "ADD"
                    bucket, freq = adds, add_freq
                if qualifies:
                    # A name whose float estimate is below the confidence bar still
                    # passed every published rule, so it is recorded separately
                    # rather than discarded. Suppressing it silently would hide the
                    # reason it is doubtful, which is the only useful thing to know.
                    if rules.CONFIDENCE_ORDER[v.float_confidence] >= bar:
                        bucket.append(v)
                        freq[sym] = freq.get(sym, 0) + 1
                    else:
                        flagged[sym] = flagged.get(sym, 0) + 1
                verdicts[sym] = v

            adds.sort(key=lambda v: -v.size_multiple)
            dels.sort(key=lambda v: v.size_multiple)

            # NOTE: GIMI 3.1.4.2 Segment Number of Companies management is implemented
            # in rules.segment_count_deletions() but NOT called here. The trigger
            # condition (coverage > 90%) cannot be computed reliably from our data:
            # our investable universe is broader than MSCI's (~566 vs ~430 for India),
            # making our FFMC denominator larger and our coverage estimates lower than
            # MSCI's. The function fires for the wrong reviews and misses the right
            # ones. Leave the implementation in place for when better calibration data
            # is available. See rules.segment_count_deletions() for details.
            date_results.append(
                DateResult(
                    price_cutoff=d,
                    cutoff=cutoff,
                    usdinr=self._usdinr(ts),
                    investable_count=investable_count,
                    additions=adds,
                    deletions=dels,
                    all_verdicts=verdicts,
                )
            )
            log.info(
                "%s  cutoff USD %.0f mn (%s)  investable %d  adds %d  deletes %d",
                d, cutoff.cutoff, cutoff.clamped or "unclamped", investable_count, len(adds), len(dels),
            )

        if not date_results:
            first, last = self.cal.price_cutoff_window
            raise RuntimeError(
                f"None of the {self.cal.label} candidate price cut-off dates has prices yet.\n"
                f"  The window runs {first} to {last}; the latest price available is "
                f"{latest_price_date.date()}.\n"
                f"  Nothing can be screened until {first}, and all ten dates only exist "
                f"after the close on {last}.\n"
                f"  If you expected data to be there, re-run with --refresh."
            )

        cutoffs = [r.cutoff.cutoff for r in date_results]
        return Prediction(
            calendar=self.cal,
            date_results=date_results,
            add_frequency=add_freq,
            delete_frequency=del_freq,
            names=names,
            cutoff_range=(min(cutoffs), max(cutoffs)),
            cutoff_mean=float(np.mean(cutoffs)),
            flagged_frequency=flagged,
            constituent_source=source,
            constituents=constituents,
            diagnostics={
                "universe_size": len(self.static),
                "cutoff_mode": self.cutoff_mode,
                "em_size_range_usd_mn": (self.size_range.lower, self.size_range.upper),
                "liquidity_cutoff": str(self.cal.liquidity_cutoff),
                "fif_overrides": sorted(self.fif_overrides),
                "fif_history": sorted(self.fif_history),
                "known_non_constituents": len(self.known_non_constituents),
                "known_members_from_announcements": len(self.known_members),
                "strategic_gap_symbols": len(self.strategic_gap),
                "min_float_confidence": self.min_float_confidence,
                "constituent_history": list(self._constituent_history),
                "candidate_dates_total": len(self.cal.price_cutoff_candidates),
                "candidate_dates_missing": len(skipped_future),
                "latest_price_date": str(latest_price_date.date()),
            },
        )
