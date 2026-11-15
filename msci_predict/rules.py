"""The MSCI GIMI maths, as pure functions.

Nothing in this module touches the network or the filesystem. Everything takes
numbers in and gives numbers out, which is what makes the model testable and what
lets the back-test replay a review without re-downloading anything.

Section numbers in docstrings refer to the MSCI Global Investable Market Indexes
Methodology, August 2026 edition, and to the MSCI Liquidity Data Methodology
(May 2025) for the liquidity ratios.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import median

# ---------------------------------------------------------------------------
# Liquidity: Annual Traded Value Ratio and Frequency of Trading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MonthlyLiquidity:
    """One month of liquidity inputs for a single security."""

    year: int
    month: int
    daily_traded_values: tuple[float, ...]   # price x volume, in any one currency
    days_traded: int                          # days with non-zero volume
    possible_trading_days: int                # days the exchange was open
    ffmc_month_end: float                     # free-float market cap, same currency
    days_suspended: int = 0

    @property
    def median_daily_traded_value(self) -> float:
        """Median of daily traded values, excluding zero-volume days.

        Liquidity Data Methodology 1.1 step 2. The median, rather than the mean, is
        what makes ATVR robust to a single block deal or a delivery-day spike.
        """
        traded = [v for v in self.daily_traded_values if v > 0]
        return median(traded) if traded else 0.0

    @property
    def monthly_median_traded_value(self) -> float:
        """Median daily traded value scaled up by the number of days traded.

        Liquidity Data Methodology 1.1 step 3, plus the suspension adjustment in
        section 3.1: suspended days are added back to the traded-day count so a
        halt does not read as illiquidity.
        """
        return self.median_daily_traded_value * (self.days_traded + self.days_suspended)

    def traded_value_ratio(self, min_trading_days: int = 5) -> float | None:
        """Monthly median traded value as a fraction of month-end FFMC.

        Returns None when the month has too few trading days to be representative
        (Liquidity Data Methodology section 2 requires at least five) or when FFMC
        is zero, which happens when FIF is zero.
        """
        if self.ffmc_month_end <= 0:
            return 0.0
        if (self.days_traded + self.days_suspended) < min_trading_days:
            return None
        return self.monthly_median_traded_value / self.ffmc_month_end


def atvr(
    months: list[MonthlyLiquidity],
    window: int,
    min_trading_days: int = 5,
    allow_partial: bool = True,
) -> float | None:
    """Annual Traded Value Ratio over the last `window` months, in percent.

    Liquidity Data Methodology 1.1 step 4: average the monthly traded-value ratios
    across the window, then annualise by multiplying by 12.

    `allow_partial` implements GIMI 2.2.5, which is easy to miss and changes
    outcomes. When twelve months of data do not exist, MSCI computes the 12-month
    ATVR from however many months are available rather than declining to compute
    it, and likewise falls back to one month for the 3-month ratio. Without this, a
    company that listed seven months ago has no 12-month ATVR and gets rejected,
    when in reality it is screened on its seven months of history.

    Returns None only when not a single month in the window is usable.
    """
    if not months:
        return None
    selected = months[-window:] if len(months) >= window else list(months)
    ratios = [
        r for m in selected if (r := m.traded_value_ratio(min_trading_days)) is not None
    ]
    if not ratios:
        return None
    if not allow_partial and len(ratios) < window:
        return None
    return (sum(ratios) / len(ratios)) * 12 * 100


def atvr_months_used(
    months: list[MonthlyLiquidity], window: int, min_trading_days: int = 5
) -> int:
    """How many months actually fed an ATVR figure, for transparency in reports."""
    selected = months[-window:] if len(months) >= window else list(months)
    return sum(1 for m in selected if m.traded_value_ratio(min_trading_days) is not None)


def frequency_of_trading(
    months: list[MonthlyLiquidity], window: int, allow_partial: bool = True
) -> float | None:
    """Share of possible trading days on which the security actually traded, in percent.

    Liquidity Data Methodology 1.2. Suspended days are removed from the denominator
    rather than counted as missed trading days (section 3.1). As with ATVR, GIMI
    2.2.5 permits a shorter window when three months of history do not exist.
    """
    if not months:
        return None
    if len(months) < window and not allow_partial:
        return None
    selected = months[-window:] if len(months) >= window else list(months)
    traded = sum(m.days_traded for m in selected)
    possible = sum(m.possible_trading_days - m.days_suspended for m in selected)
    if possible <= 0:
        return None
    return traded / possible * 100


# ---------------------------------------------------------------------------
# Size-segment cutoffs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SizeRange:
    """A Global Minimum Size Range: 0.5x to 1.15x a Global Minimum Size Reference."""

    reference: float
    lower: float
    upper: float

    @classmethod
    def from_reference(
        cls, reference: float, lower_multiple: float = 0.50, upper_multiple: float = 1.15
    ) -> "SizeRange":
        return cls(reference, reference * lower_multiple, reference * upper_multiple)

    def clamp(self, value: float) -> float:
        return min(max(value, self.lower), self.upper)

    def contains(self, value: float) -> bool:
        return self.lower <= value <= self.upper


@dataclass(frozen=True)
class CutoffResult:
    """Outcome of deriving a Market Size-Segment Cutoff for one market."""

    cutoff: float                 # full market cap, USD mn
    raw_coverage_cutoff: float    # the pre-clamp 85%-coverage company's full mcap
    segment_number_of_companies: int
    coverage_achieved: float      # cumulative FFMC coverage at the cutoff
    size_range: SizeRange
    clamped: str                  # "", "lower" or "upper"
    universe_ffmc_total: float

    @property
    def min_ffmc_for_standard(self) -> float:
        """GIMI 2.3.6.1: FFMC must be at least 50% of the cutoff."""
        return self.cutoff * 0.50


def derive_size_segment_cutoff(
    companies: list[tuple[str, float, float]],
    size_range: SizeRange,
    coverage_target: float = 0.85,
) -> CutoffResult:
    """Derive the Market Size-Segment Cutoff for a market (GIMI 2.3.3).

    `companies` is (identifier, full market cap, free-float market cap) for every
    company in the Market Investable Equity Universe, in any consistent currency.

    The procedure: sort by full market cap descending, walk down accumulating
    free-float market cap, and stop at the company where cumulative free-float
    coverage first reaches the target. That company's *full* market cap is the
    cutoff. Then, because MSCI puts global size integrity ahead of per-market
    coverage, the cutoff is pulled back inside the Global Minimum Size Range.

    This is the single most important number in the model. Every add/delete
    decision is a multiple of it, so a 10% error here moves marginal names in and
    out of the prediction.
    """
    if not companies:
        raise ValueError("Cannot derive a cutoff from an empty investable universe.")

    ranked = sorted(companies, key=lambda c: c[1], reverse=True)
    total_ffmc = sum(c[2] for c in ranked)
    if total_ffmc <= 0:
        raise ValueError("Investable universe has zero aggregate free-float market cap.")

    cumulative = 0.0
    raw_cutoff = ranked[-1][1]
    count = len(ranked)
    coverage_at_cut = 1.0
    for i, (_, full_mcap, ffmc) in enumerate(ranked, start=1):
        cumulative += ffmc
        if cumulative / total_ffmc >= coverage_target:
            raw_cutoff = full_mcap
            count = i
            coverage_at_cut = cumulative / total_ffmc
            break

    cutoff = size_range.clamp(raw_cutoff)
    clamped = ""
    if cutoff > raw_cutoff:
        clamped = "lower"
    elif cutoff < raw_cutoff:
        clamped = "upper"

    if clamped:
        # The cutoff moved, so the company count implied by it moves too
        # (GIMI 3.1.4.1).
        count = sum(1 for c in ranked if c[1] >= cutoff)

    return CutoffResult(
        cutoff=cutoff,
        raw_coverage_cutoff=raw_cutoff,
        segment_number_of_companies=count,
        coverage_achieved=coverage_at_cut,
        size_range=size_range,
        clamped=clamped,
        universe_ffmc_total=total_ffmc,
    )


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""
    binding: bool = True   # False for informational checks that do not gate inclusion

    def __str__(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        return f"[{mark}] {self.name}: {self.detail}" if self.detail else f"[{mark}] {self.name}"


@dataclass
class Security:
    """Point-in-time inputs for one security at a given price cut-off date."""

    symbol: str
    name: str
    full_mcap_usd_mn: float
    fif: float                             # free float as a fraction of shares outstanding
    atvr_3m_pct: float | None
    atvr_12m_pct: float | None
    fot_3m_pct: float | None
    months_since_listing: float
    price_usd: float
    atvr_12m_months_used: int = 12
    # Provenance of the free-float estimate. Both feed the reliability gate below:
    # how old the number is, and how much of the company's "public" holding is
    # actually strategic and therefore a matter of MSCI's judgement.
    fif_as_of_months_stale: float | None = None
    strategic_gap_pp: float | None = None
    sector: str = ""
    is_standard_constituent: bool = False
    is_imi_constituent: bool = False
    epi_excess_returns_pct: dict[int, float] = field(default_factory=dict)
    foreign_room_pct: float | None = None   # None means no foreign ownership limit binds

    @property
    def ffmc_usd_mn(self) -> float:
        return self.full_mcap_usd_mn * self.fif


CONFIDENCE_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


@dataclass
class FloatReliability:
    """How much to trust a security's free-float estimate at a given cut-off date."""

    level: str              # HIGH | MEDIUM | LOW
    reasons: list[str]

    @property
    def rank(self) -> int:
        return CONFIDENCE_ORDER[self.level]

    def __str__(self) -> str:
        return f"{self.level}" + (f" ({'; '.join(self.reasons)})" if self.reasons else "")


def assess_float_reliability(
    sec: "Security",
    lockin_months: float = 6.0,
    stale_months: float = 4.0,
    gap_low_pp: float = 25.0,
    gap_medium_pp: float = 15.0,
) -> FloatReliability:
    """Flag securities whose free-float estimate cannot be trusted at this date.

    This is a reliability gate, not another MSCI rule. It exists because the model's
    free-float input is an estimate, and the back-test showed its errors are not
    random: they concentrate in three identifiable situations, and every false
    positive in the May 2026 review sat in at least one of them.

    **Recent listings.** SEBI lock-ins run roughly 30 and 90 days for anchor
    investors and six months for other pre-IPO shareholders. Shareholding patterns
    report those holders as public from the first day of listing, but MSCI classifies
    locked shares as non-free float. So for a company listed inside the lock-in
    horizon, every free data source overstates the float that MSCI would have used.
    Meesho and Lenskart were both inside this window at the May 2026 review, both
    looked comfortably eligible on current float, and MSCI added neither.

    **Large strategic gap.** Where SEBI's "public" holding greatly exceeds the
    estimated float, the difference is strategic holders whose treatment is MSCI's
    judgement rather than arithmetic. Adani Green is the case in point: a 17pp gap
    from TotalEnergies and similar holders, and it sat above every size threshold for
    years without being added.

    **Stale data.** A float figure measured months after the cut-off date may have
    moved in between.
    """
    reasons: list[str] = []
    level = "HIGH"

    if sec.months_since_listing < lockin_months:
        reasons.append(
            f"listed {sec.months_since_listing:.1f} months ago, inside the ~{lockin_months:.0f} "
            "month IPO lock-in horizon, so reported float overstates MSCI's"
        )
        level = "LOW"

    # The strategic gap is reported but never changes the confidence level, because
    # measurement showed it does not discriminate. At the May 2026 review Federal
    # Bank, which MSCI added, carried a 22pp gap while SAIL, which it did not, carried
    # 0.1pp.
    #
    # The reason is that the quantity is mis-specified for this purpose. gap = SEBI
    # public holding minus the estimated float, so a large gap means the estimate is
    # already well *below* public holding, which is conservative rather than
    # suspect. What would actually matter is where the estimate sits above MSCI's
    # own float, and that is only knowable for current constituents (see
    # scripts/derive_msci_fif.py).
    #
    # Leaving it in the level calculation did active harm: it marked almost every
    # name MEDIUM, which made the HIGH bar useless.
    if sec.strategic_gap_pp is not None and sec.strategic_gap_pp >= gap_medium_pp:
        reasons.append(
            f"{sec.strategic_gap_pp:.0f}pp between SEBI public holding and the float "
            "estimate (reported only, does not affect confidence)"
        )

    if sec.fif_as_of_months_stale is not None and sec.fif_as_of_months_stale > stale_months:
        reasons.append(
            f"float estimate is {sec.fif_as_of_months_stale:.0f} months newer than the "
            "price cut-off date"
        )
        if level == "HIGH":
            level = "MEDIUM"

    return FloatReliability(level, reasons)


@dataclass
class Thresholds:
    """Everything the screens need, pulled from config/parameters.yaml."""

    eu_min_size_usd_mn: float
    eu_min_ffmc_fraction: float
    atvr_3m_min: float
    atvr_12m_min: float
    fot_3m_min: float
    existing_atvr_12m_min: float
    existing_atvr_3m_min: float
    existing_fot_3m_min: float
    max_price_usd: float
    min_fif: float
    low_fif_ffmc_multiple: float
    epi_exempt_fif: float
    standard_min_ffmc_fraction: float
    existing_constituent_relief: float
    add_multiple: float
    delete_multiple: float
    min_listing_months: float
    waiver_full_mcap_multiple: float
    waiver_ffmc_multiple: float
    epi_thresholds_pct: dict[int, float]
    epi_enabled: bool = True
    min_foreign_room_pct: float = 15.0


@dataclass
class Verdict:
    symbol: str
    name: str
    decision: str                 # ADD | DELETE | HOLD_IN | HOLD_OUT
    checks: list[Check]
    cutoff_usd_mn: float
    full_mcap_usd_mn: float
    ffmc_usd_mn: float
    fif: float
    size_multiple: float          # full mcap / cutoff
    ffmc_multiple: float          # FFMC / required minimum FFMC
    blocking_reasons: list[str] = field(default_factory=list)
    path: str = "standard"        # "standard", "low_fif" or "large_ipo_waiver"
    float_confidence: str = "HIGH"
    float_warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.blocking_reasons


def _min_ffmc_required(sec: Security, cutoff: float, th: Thresholds) -> tuple[float, str]:
    """Minimum free-float market cap this security needs for the Standard Index.

    GIMI 2.3.6.1. The base requirement is 50% of the cutoff. A security whose FIF is
    below 0.15 must instead clear 1.8 times that, which is the rule that governs
    state-controlled and promoter-heavy names: LIC entered the index in November 2026
    on exactly this path, not by passing the ordinary 0.15 FIF test.
    """
    base = cutoff * th.standard_min_ffmc_fraction
    if sec.fif < th.min_fif:
        return base * th.low_fif_ffmc_multiple, "low_fif"
    return base, "standard"


def _liquidity_checks(sec: Security, th: Thresholds, existing: bool) -> list[Check]:
    """GIMI 2.2.5 for new securities, GIMI 3.1.2.4 for existing constituents."""
    if existing:
        a12, a3, fot = th.existing_atvr_12m_min, th.existing_atvr_3m_min, th.existing_fot_3m_min
        label = "existing-constituent"
    else:
        a12, a3, fot = th.atvr_12m_min, th.atvr_3m_min, th.fot_3m_min
        label = "new-constituent"

    checks = []
    if sec.atvr_12m_pct is None:
        checks.append(
            Check(
                "12m ATVR",
                False,
                f"not computable (needs 12 complete months); {label} threshold {a12:.1f}%",
            )
        )
    else:
        short = (
            f", from {sec.atvr_12m_months_used} months only"
            if sec.atvr_12m_months_used < 12
            else ""
        )
        checks.append(
            Check(
                "12m ATVR",
                sec.atvr_12m_pct >= a12,
                f"{sec.atvr_12m_pct:.1f}% vs {a12:.1f}% ({label}){short}",
            )
        )

    if sec.atvr_3m_pct is None:
        checks.append(Check("3m ATVR", False, f"not computable; {label} threshold {a3:.1f}%"))
    else:
        checks.append(
            Check(
                "3m ATVR",
                sec.atvr_3m_pct >= a3,
                f"{sec.atvr_3m_pct:.1f}% vs {a3:.1f}% ({label})",
            )
        )

    if sec.fot_3m_pct is None:
        checks.append(Check("3m Frequency of Trading", False, f"not computable; threshold {fot:.0f}%"))
    else:
        checks.append(
            Check(
                "3m Frequency of Trading",
                sec.fot_3m_pct >= fot,
                f"{sec.fot_3m_pct:.1f}% vs {fot:.0f}% ({label})",
            )
        )
    return checks


def _epi_check(sec: Security, th: Thresholds) -> Check:
    """Extreme Price Increase screen (GIMI 2.3.6.3).

    A would-be addition whose excess return over its country-sector peers breaches
    any monitoring window is held out of the Standard Index for one review, even
    when it passes every size and liquidity test. Securities with FIF of 0.75 or
    more are exempt, on the reasoning that a widely held float is harder to squeeze.
    """
    if not th.epi_enabled:
        return Check("Extreme Price Increase", True, "screen disabled", binding=False)
    if sec.fif >= th.epi_exempt_fif:
        return Check(
            "Extreme Price Increase",
            True,
            f"exempt, FIF {sec.fif:.2f} >= {th.epi_exempt_fif:.2f}",
        )
    if not sec.epi_excess_returns_pct:
        return Check(
            "Extreme Price Increase",
            True,
            "no return history supplied; screen not evaluated",
            binding=False,
        )
    breaches = [
        (window, excess, th.epi_thresholds_pct[window])
        for window, excess in sorted(sec.epi_excess_returns_pct.items())
        if window in th.epi_thresholds_pct and excess > th.epi_thresholds_pct[window]
    ]
    if breaches:
        worst = max(breaches, key=lambda b: b[1] - b[2])
        return Check(
            "Extreme Price Increase",
            False,
            f"{worst[0]}D excess return {worst[1]:.0f}% exceeds {worst[2]:.0f}% "
            f"({len(breaches)} window(s) breached)",
        )
        
    peak = max(sec.epi_excess_returns_pct.items(), key=lambda kv: kv[1])
    return Check(
        "Extreme Price Increase",
        True,
        f"worst window {peak[0]}D at {peak[1]:.0f}% excess, under threshold",
    )


def screen_for_addition(sec: Security, cutoff: CutoffResult, th: Thresholds) -> Verdict:
    """Decide whether a non-constituent should join the India Standard Index.

    Order follows the methodology: first the Equity Universe screens (GIMI 2.2),
    then the size-segment assignment with its +50% migration buffer (GIMI 3.1.5.1),
    then the Final Size-Segment Investability Requirements (GIMI 2.3.6).
    """
    checks: list[Check] = []
    c = cutoff.cutoff
    min_ffmc, path = _min_ffmc_required(sec, c, th)

    # --- Equity Universe screens (GIMI 2.2.3, 2.2.4) ---
    checks.append(
        Check(
            "Equity Universe minimum size",
            sec.full_mcap_usd_mn >= th.eu_min_size_usd_mn,
            f"full mcap USD {sec.full_mcap_usd_mn:,.0f}mn vs USD {th.eu_min_size_usd_mn:,.0f}mn",
        )
    )
    eu_min_ffmc = th.eu_min_size_usd_mn * th.eu_min_ffmc_fraction
    checks.append(
        Check(
            "Equity Universe minimum FFMC",
            sec.ffmc_usd_mn >= eu_min_ffmc,
            f"FFMC USD {sec.ffmc_usd_mn:,.0f}mn vs USD {eu_min_ffmc:,.0f}mn",
        )
    )

    # --- Minimum length of trading (GIMI 2.2.7, 3.1.2.7) ---
    young = sec.months_since_listing < th.min_listing_months
    if young:
        waiver_ok = (
            sec.full_mcap_usd_mn >= c * th.waiver_full_mcap_multiple
            and sec.ffmc_usd_mn >= c * th.standard_min_ffmc_fraction * th.waiver_ffmc_multiple
        )
        checks.append(
            Check(
                "Minimum length of trading",
                waiver_ok,
                f"listed {sec.months_since_listing:.1f} months (< {th.min_listing_months:.0f}); "
                f"large-IPO waiver needs full mcap >= USD {c * th.waiver_full_mcap_multiple:,.0f}mn "
                f"and FFMC >= USD {c * th.standard_min_ffmc_fraction * th.waiver_ffmc_multiple:,.0f}mn"
                + ("" if waiver_ok else " - not met"),
            )
        )
        if waiver_ok:
            path = "large_ipo_waiver"
    else:
        checks.append(
            Check(
                "Minimum length of trading",
                True,
                f"listed {sec.months_since_listing:.1f} months",
            )
        )

    # --- Liquidity (GIMI 2.2.5) ---
    # A security coming in under the large-IPO waiver cannot have 12 months of
    # history, so GIMI 3.2.7 exempts it from the ordinary liquidity window.
    if path == "large_ipo_waiver":
        checks.append(
            Check(
                "Liquidity",
                True,
                "waived: large IPO / primary offering under GIMI 3.2.7",
                binding=False,
            )
        )
    else:
        checks.extend(_liquidity_checks(sec, th, existing=False))

    checks.append(
        Check(
            "Maximum share price",
            sec.price_usd <= th.max_price_usd,
            f"USD {sec.price_usd:,.0f} vs USD {th.max_price_usd:,.0f} limit",
        )
    )

    # --- Foreign room (GIMI 2.2.8) ---
    if sec.foreign_room_pct is not None:
        checks.append(
            Check(
                "Minimum foreign room",
                sec.foreign_room_pct >= th.min_foreign_room_pct,
                f"{sec.foreign_room_pct:.1f}% vs {th.min_foreign_room_pct:.0f}%",
            )
        )

    # --- Foreign Inclusion Factor (GIMI 2.2.6, 3.1.2.5) ---
    if sec.fif >= th.min_fif:
        checks.append(Check("Minimum FIF", True, f"FIF {sec.fif:.3f} >= {th.min_fif:.2f}"))
    else:
        # Below 0.15 the security is not disqualified outright; it has to be large
        # enough in float terms to justify the exception.
        ok = sec.ffmc_usd_mn >= min_ffmc
        checks.append(
            Check(
                "Minimum FIF",
                ok,
                f"FIF {sec.fif:.3f} < {th.min_fif:.2f}, so FFMC must reach "
                f"{th.low_fif_ffmc_multiple:.1f}x the normal minimum: "
                f"USD {sec.ffmc_usd_mn:,.0f}mn vs USD {min_ffmc:,.0f}mn",
            )
        )

    # --- Size-segment migration buffer (GIMI 3.1.5.1) ---
    add_threshold = c * th.add_multiple
    checks.append(
        Check(
            f"Size: +{(th.add_multiple - 1) * 100:.0f}% add buffer",
            sec.full_mcap_usd_mn >= add_threshold,
            f"full mcap USD {sec.full_mcap_usd_mn:,.0f}mn vs {th.add_multiple:.2f}x cutoff "
            f"= USD {add_threshold:,.0f}mn",
        )
    )

    # --- Final Size-Segment Investability Requirement (GIMI 2.3.6.1) ---
    checks.append(
        Check(
            "Minimum FFMC for Standard",
            sec.ffmc_usd_mn >= min_ffmc,
            f"FFMC USD {sec.ffmc_usd_mn:,.0f}mn vs USD {min_ffmc:,.0f}mn"
            + (f" ({th.low_fif_ffmc_multiple:.1f}x, low FIF)" if sec.fif < th.min_fif else ""),
        )
    )

    checks.append(_epi_check(sec, th))

    # Reliability of the free-float input. Deliberately non-binding: it must not
    # silently drop a name, because the gate is a heuristic about data quality while
    # everything above is a published rule. Surfaced so the caller can decide.
    reliability = assess_float_reliability(sec)
    checks.append(
        Check(
            "Free-float estimate reliability",
            reliability.level == "HIGH",
            str(reliability),
            binding=False,
        )
    )

    blocking = [c_.name for c_ in checks if c_.binding and not c_.passed]
    return Verdict(
        symbol=sec.symbol,
        name=sec.name,
        decision="ADD" if not blocking else "HOLD_OUT",
        checks=checks,
        cutoff_usd_mn=c,
        full_mcap_usd_mn=sec.full_mcap_usd_mn,
        ffmc_usd_mn=sec.ffmc_usd_mn,
        fif=sec.fif,
        size_multiple=sec.full_mcap_usd_mn / c if c else math.nan,
        ffmc_multiple=sec.ffmc_usd_mn / min_ffmc if min_ffmc else math.nan,
        blocking_reasons=blocking,
        path=path,
        float_confidence=reliability.level,
        float_warnings=reliability.reasons,
    )


def screen_for_deletion(sec: Security, cutoff: CutoffResult, th: Thresholds) -> Verdict:
    """Decide whether an existing Standard constituent should drop out.

    Incumbents are protected: they only leave once full market cap falls under
    two-thirds of the cutoff (GIMI 3.1.5.1) or free-float market cap falls under
    two-thirds of the minimum FFMC requirement (GIMI 3.1.6.2). This asymmetry is
    why deletions are rarer than additions and why a deletion call needs a genuine
    decline rather than a marginal one.
    """
    checks: list[Check] = []
    c = cutoff.cutoff
    base_min_ffmc, path = _min_ffmc_required(sec, c, th)
    relief = th.existing_constituent_relief

    delete_threshold = c * th.delete_multiple
    size_ok = sec.full_mcap_usd_mn >= delete_threshold
    checks.append(
        Check(
            f"Size: -{(1 - th.delete_multiple) * 100:.0f}% delete buffer",
            size_ok,
            f"full mcap USD {sec.full_mcap_usd_mn:,.0f}mn vs {th.delete_multiple:.3f}x cutoff "
            f"= USD {delete_threshold:,.0f}mn",
        )
    )

    ffmc_floor = base_min_ffmc * relief
    ffmc_ok = sec.ffmc_usd_mn >= ffmc_floor
    checks.append(
        Check(
            "Minimum FFMC (2/3 relief)",
            ffmc_ok,
            f"FFMC USD {sec.ffmc_usd_mn:,.0f}mn vs USD {ffmc_floor:,.0f}mn",
        )
    )

    checks.extend(_liquidity_checks(sec, th, existing=True))

    reliability = assess_float_reliability(sec)
    checks.append(
        Check(
            "Free-float estimate reliability",
            reliability.level == "HIGH",
            str(reliability),
            binding=False,
        )
    )

    failed = [c_.name for c_ in checks if c_.binding and not c_.passed]
    return Verdict(
        symbol=sec.symbol,
        name=sec.name,
        decision="DELETE" if failed else "HOLD_IN",
        checks=checks,
        cutoff_usd_mn=c,
        full_mcap_usd_mn=sec.full_mcap_usd_mn,
        ffmc_usd_mn=sec.ffmc_usd_mn,
        fif=sec.fif,
        size_multiple=sec.full_mcap_usd_mn / c if c else math.nan,
        ffmc_multiple=sec.ffmc_usd_mn / base_min_ffmc if base_min_ffmc else math.nan,
        blocking_reasons=failed,
        path=path,
        float_confidence=reliability.level,
        float_warnings=reliability.reasons,
    )


def passes_equity_universe(sec: Security, th: Thresholds) -> bool:
    """Whether a security belongs in the Market Investable Equity Universe (GIMI 2.2).

    Used to build the denominator for the size-segment cutoff. Deliberately
    permissive on liquidity for securities with incomplete history, because
    excluding a large recent IPO from the universe would understate aggregate
    free-float market cap and bias the cutoff downward.
    """
    if sec.full_mcap_usd_mn < th.eu_min_size_usd_mn:
        return False
    if sec.ffmc_usd_mn < th.eu_min_size_usd_mn * th.eu_min_ffmc_fraction:
        return False
    if sec.fif < th.min_fif and sec.ffmc_usd_mn < th.eu_min_size_usd_mn * th.low_fif_ffmc_multiple:
        return False
    if sec.atvr_12m_pct is not None and sec.atvr_12m_pct < th.atvr_12m_min:
        return False
    if sec.atvr_3m_pct is not None and sec.atvr_3m_pct < th.atvr_3m_min:
        return False
    if sec.fot_3m_pct is not None and sec.fot_3m_pct < th.fot_3m_min:
        return False
    return True
