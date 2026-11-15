"""Unit tests for the MSCI GIMI maths.

These check the formulas against hand-worked numbers and against the four
November 2026 India additions, each of which entered through a different part of
the rulebook. That last set is the useful regression: if a refactor breaks the
low-FIF path, LIC silently drops out of the prediction and nothing else complains.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from msci_predict import rules
from msci_predict.calendar_rules import build_review_calendar, parse_review_label


# ---------------------------------------------------------------------------
# Liquidity
# ---------------------------------------------------------------------------


def _month(median_value: float, days_traded: int, ffmc: float, possible: int = 21, year=2026, month=9):
    """A month whose daily traded values have exactly `median_value` as their median."""
    values = tuple([median_value] * days_traded)
    return rules.MonthlyLiquidity(
        year=year, month=month, daily_traded_values=values,
        days_traded=days_traded, possible_trading_days=possible, ffmc_month_end=ffmc,
    )


def test_monthly_median_traded_value_uses_median_not_mean():
    # One huge block trade should not lift the ratio; that is the whole point of
    # using a median (Liquidity Data Methodology 1.1).
    spiky = rules.MonthlyLiquidity(
        year=2026, month=9,
        daily_traded_values=(10, 10, 10, 10, 10_000),
        days_traded=5, possible_trading_days=21, ffmc_month_end=1000,
    )
    assert spiky.median_daily_traded_value == 10
    assert spiky.monthly_median_traded_value == 50


def test_atvr_is_mean_monthly_ratio_annualised_by_twelve():
    # median 100 x 20 days traded = 2000 monthly median traded value.
    # 2000 / 24000 FFMC = 1/12 per month. Annualised: 1/12 x 12 = 100%.
    months = [_month(100, 20, 24_000) for _ in range(12)]
    assert rules.atvr(months, 3) == pytest.approx(100.0)
    assert rules.atvr(months, 12) == pytest.approx(100.0)


def test_atvr_at_the_em_fifteen_percent_boundary():
    # Ratio of 0.0125 per month annualises to exactly the 15% EM threshold.
    months = [_month(125, 20, 200_000) for _ in range(12)]   # 2500 / 200000 = 0.0125
    assert rules.atvr(months, 12) == pytest.approx(15.0)


def test_short_history_falls_back_to_the_months_available():
    """GIMI 2.2.5 explicitly allows this, and missing it changes outcomes.

    A company listed five months ago is screened on five months of history, not
    rejected for having no 12-month figure.
    """
    months = [_month(100, 20, 24_000) for _ in range(5)]
    assert rules.atvr(months, 12) == pytest.approx(100.0)
    assert rules.atvr_months_used(months, 12) == 5
    assert rules.atvr(months, 12, allow_partial=False) is None


def test_months_with_too_few_trading_days_are_skipped_not_fatal():
    months = [_month(100, 20, 24_000) for _ in range(11)]
    months.append(_month(100, 2, 24_000))          # only two trading days
    assert rules.atvr(months, 12) == pytest.approx(100.0)
    assert rules.atvr_months_used(months, 12) == 11


def test_atvr_is_none_only_when_nothing_is_usable():
    assert rules.atvr([], 12) is None
    assert rules.atvr([_month(100, 1, 24_000)], 12) is None   # below the 5-day minimum


def test_suspended_days_are_added_back_for_atvr():
    quiet = rules.MonthlyLiquidity(
        year=2026, month=9, daily_traded_values=(100,) * 10,
        days_traded=10, possible_trading_days=21, ffmc_month_end=24_000, days_suspended=10,
    )
    assert quiet.monthly_median_traded_value == 100 * 20


def test_frequency_of_trading_excludes_suspended_days_from_denominator():
    months = [
        rules.MonthlyLiquidity(2026, m, (100,) * 15, 15, 20, 1000, days_suspended=5)
        for m in (7, 8, 9)
    ]
    # 45 traded out of (20-5)*3 = 45 possible.
    assert rules.frequency_of_trading(months, 3) == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Size-segment cutoff
# ---------------------------------------------------------------------------


def _ladder():
    """Ten companies at 10000 down to 1000, each 50% free float."""
    return [(f"C{i}", m, m * 0.5) for i, m in enumerate(range(10_000, 0, -1_000), start=1)]


def test_cutoff_is_the_full_mcap_at_eighty_five_percent_float_coverage():
    # Cumulative free float reaches 85% of 27,500 at the seventh company,
    # whose full market cap is 4,000.
    size_range = rules.SizeRange.from_reference(4_000)        # 2,000 .. 4,600
    result = rules.derive_size_segment_cutoff(_ladder(), size_range, coverage_target=0.85)
    assert result.cutoff == pytest.approx(4_000)
    assert result.raw_coverage_cutoff == pytest.approx(4_000)
    assert result.segment_number_of_companies == 7
    assert result.clamped == ""
    assert result.coverage_achieved >= 0.85


def test_cutoff_is_clamped_up_to_the_global_minimum_size_range():
    # GIMI 2.3.3 gives global size integrity priority over local coverage, so a
    # cut-off below the range is pulled up to 0.5x the Global Minimum Size Reference.
    em_range = rules.SizeRange.from_reference(8_138)          # 4,069 .. 9,359
    result = rules.derive_size_segment_cutoff(_ladder(), em_range, coverage_target=0.85)
    assert result.raw_coverage_cutoff == pytest.approx(4_000)
    assert result.cutoff == pytest.approx(4_069)
    assert result.clamped == "lower"
    assert result.segment_number_of_companies == 6   # recounted at the clamped cut-off


def test_min_ffmc_for_standard_is_half_the_cutoff():
    result = rules.derive_size_segment_cutoff(
        _ladder(), rules.SizeRange.from_reference(4_000), coverage_target=0.85
    )
    assert result.min_ffmc_for_standard == pytest.approx(2_000)


def test_em_size_range_matches_msci_published_figures():
    # MSCI's own worked example in GIMI 2.3.2.1 for the August 2026 review:
    # DM Standard reference USD 16.28bn, EM range USD 4.07bn to USD 9.36bn.
    em = rules.SizeRange.from_reference(16_276 * 0.5)
    assert em.lower == pytest.approx(4_069, abs=5)
    assert em.upper == pytest.approx(9_359, abs=10)


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

CUTOFF_USD_MN = 5_200.0


def _thresholds(**overrides) -> rules.Thresholds:
    base = dict(
        eu_min_size_usd_mn=585.0, eu_min_ffmc_fraction=0.50,
        atvr_3m_min=15.0, atvr_12m_min=15.0, fot_3m_min=80.0,
        existing_atvr_12m_min=10.0, existing_atvr_3m_min=5.0, existing_fot_3m_min=70.0,
        max_price_usd=10_000.0, min_fif=0.15, low_fif_ffmc_multiple=1.8, epi_exempt_fif=0.75,
        standard_min_ffmc_fraction=0.50, existing_constituent_relief=2 / 3,
        add_multiple=1.50, delete_multiple=2 / 3,
        min_listing_months=3.0, waiver_full_mcap_multiple=1.8, waiver_ffmc_multiple=1.8,
        epi_thresholds_pct={5: 100, 30: 200, 250: 2500}, epi_enabled=True,
    )
    base.update(overrides)
    return rules.Thresholds(**base)


def _cutoff(value: float = CUTOFF_USD_MN) -> rules.CutoffResult:
    return rules.CutoffResult(
        cutoff=value, raw_coverage_cutoff=value, segment_number_of_companies=160,
        coverage_achieved=0.85, size_range=rules.SizeRange.from_reference(8_138),
        clamped="", universe_ffmc_total=1_000_000,
    )


def _security(**kw) -> rules.Security:
    base = dict(
        symbol="TEST", name="Test Ltd", full_mcap_usd_mn=10_000.0, fif=0.60,
        atvr_3m_pct=40.0, atvr_12m_pct=40.0, fot_3m_pct=100.0,
        months_since_listing=60.0, price_usd=50.0,
    )
    base.update(kw)
    return rules.Security(**base)


def test_ordinary_addition_needs_one_and_a_half_times_the_cutoff():
    th, cut = _thresholds(), _cutoff()
    just_over = rules.screen_for_addition(_security(full_mcap_usd_mn=CUTOFF_USD_MN * 1.51), cut, th)
    just_under = rules.screen_for_addition(_security(full_mcap_usd_mn=CUTOFF_USD_MN * 1.49), cut, th)
    assert just_over.decision == "ADD"
    assert just_under.decision == "HOLD_OUT"
    assert just_under.blocking_reasons == ["Size: +50% add buffer"]


def test_low_fif_name_enters_on_the_one_point_eight_times_float_test():
    """The LIC path (GIMI 2.3.6.1).

    FIF under 0.15 does not disqualify a company. It raises the free-float bar to
    1.8 x 50% = 0.9 times the cut-off. LIC joined the index in November 2026 on
    exactly this route, with a float in the low teens.
    """
    th, cut = _thresholds(), _cutoff()
    # Full mcap 53bn at FIF 0.112 -> FFMC 5.94bn, against a required 0.9 x 5.2 = 4.68bn.
    lic_like = _security(symbol="LICI", full_mcap_usd_mn=53_100.0, fif=0.112)
    v = rules.screen_for_addition(lic_like, cut, th)
    assert v.path == "low_fif"
    assert v.decision == "ADD", v.blocking_reasons
    # Same FIF, a quarter of the size: now the float test bites.
    too_small = _security(symbol="LICI", full_mcap_usd_mn=13_000.0, fif=0.112)
    v2 = rules.screen_for_addition(too_small, cut, th)
    assert v2.decision == "HOLD_OUT"
    assert "Minimum FFMC for Standard" in v2.blocking_reasons


def test_low_fif_threshold_is_exactly_one_point_eight_times_the_base():
    th, cut = _thresholds(), _cutoff()
    required = CUTOFF_USD_MN * 0.5 * 1.8          # 4,680
    full = required / 0.10                         # FIF 0.10 gives FFMC == required
    v = rules.screen_for_addition(_security(full_mcap_usd_mn=full, fif=0.10), cut, th)
    assert v.decision == "ADD", v.blocking_reasons
    v2 = rules.screen_for_addition(_security(full_mcap_usd_mn=full * 0.99, fif=0.10), cut, th)
    assert "Minimum FFMC for Standard" in v2.blocking_reasons


def test_recent_ipo_enters_through_the_large_offering_waiver():
    """The Meesho path (GIMI 2.2.7 and 3.1.2.7).

    A company listed for less than three months cannot satisfy the ordinary
    liquidity window, so it needs full market cap at 1.8x the cut-off and free
    float at 1.8x the minimum.
    """
    th, cut = _thresholds(), _cutoff()
    big_ipo = _security(
        symbol="MEESHO", full_mcap_usd_mn=CUTOFF_USD_MN * 3.0, fif=0.36,
        months_since_listing=1.0, atvr_12m_pct=None, fot_3m_pct=None, atvr_3m_pct=None,
    )
    v = rules.screen_for_addition(big_ipo, cut, th)
    assert v.path == "large_ipo_waiver"
    assert v.decision == "ADD", v.blocking_reasons

    # Too small outright.
    small_ipo = _security(
        symbol="TINY", full_mcap_usd_mn=CUTOFF_USD_MN * 1.7, fif=0.36,
        months_since_listing=1.0, atvr_12m_pct=None, fot_3m_pct=None, atvr_3m_pct=None,
    )
    v2 = rules.screen_for_addition(small_ipo, cut, th)
    assert v2.decision == "HOLD_OUT"
    assert "Minimum length of trading" in v2.blocking_reasons

    # Large enough on full market cap but too thin on float: the waiver has two
    # legs and this is the one that catches anchor-heavy new listings.
    thin_ipo = _security(
        symbol="THIN", full_mcap_usd_mn=CUTOFF_USD_MN * 2.0, fif=0.20,
        months_since_listing=1.0, atvr_12m_pct=None, fot_3m_pct=None, atvr_3m_pct=None,
    )
    v3 = rules.screen_for_addition(thin_ipo, cut, th)
    assert v3.decision == "HOLD_OUT"
    assert "Minimum length of trading" in v3.blocking_reasons


def test_seasoned_name_without_liquidity_history_is_rejected():
    th, cut = _thresholds(), _cutoff()
    v = rules.screen_for_addition(
        _security(full_mcap_usd_mn=CUTOFF_USD_MN * 2, atvr_12m_pct=None), cut, th
    )
    assert "12m ATVR" in v.blocking_reasons


def test_em_liquidity_thresholds_are_fifteen_percent_not_twenty():
    """India is an Emerging Market, so 15% applies, not the 20% DM level."""
    th, cut = _thresholds(), _cutoff()
    sec = _security(full_mcap_usd_mn=CUTOFF_USD_MN * 2, atvr_3m_pct=16.0, atvr_12m_pct=16.0)
    assert rules.screen_for_addition(sec, cut, th).decision == "ADD"
    sec_fail = _security(full_mcap_usd_mn=CUTOFF_USD_MN * 2, atvr_3m_pct=14.0, atvr_12m_pct=14.0)
    v = rules.screen_for_addition(sec_fail, cut, th)
    assert set(v.blocking_reasons) >= {"3m ATVR", "12m ATVR"}


def test_extreme_price_increase_blocks_a_qualifying_addition():
    th, cut = _thresholds(), _cutoff()
    sec = _security(
        full_mcap_usd_mn=CUTOFF_USD_MN * 2, fif=0.40,
        epi_excess_returns_pct={5: 150.0, 30: 50.0, 250: 300.0},
    )
    v = rules.screen_for_addition(sec, cut, th)
    assert v.decision == "HOLD_OUT"
    assert "Extreme Price Increase" in v.blocking_reasons


def test_extreme_price_increase_exempts_high_float_names():
    th, cut = _thresholds(), _cutoff()
    sec = _security(
        full_mcap_usd_mn=CUTOFF_USD_MN * 2, fif=0.80,
        epi_excess_returns_pct={5: 900.0},
    )
    assert rules.screen_for_addition(sec, cut, th).decision == "ADD"


def test_incumbent_survives_until_two_thirds_of_the_cutoff():
    """The PI Industries path, in reverse.

    An incumbent at 0.70x the cut-off stays; at 0.60x it goes. PI Industries was
    deleted in November 2026 from roughly 0.52x.
    """
    th, cut = _thresholds(), _cutoff()
    safe = _security(full_mcap_usd_mn=CUTOFF_USD_MN * 0.70, is_standard_constituent=True)
    gone = _security(full_mcap_usd_mn=CUTOFF_USD_MN * 0.60, is_standard_constituent=True)
    assert rules.screen_for_deletion(safe, cut, th).decision == "HOLD_IN"
    v = rules.screen_for_deletion(gone, cut, th)
    assert v.decision == "DELETE"
    assert "Size: -33% delete buffer" in v.blocking_reasons


def test_incumbent_gets_relaxed_liquidity_thresholds():
    th, cut = _thresholds(), _cutoff()
    # 12% ATVR fails the 15% new-entry bar but clears the 10% incumbent bar.
    sec = _security(
        full_mcap_usd_mn=CUTOFF_USD_MN, is_standard_constituent=True,
        atvr_12m_pct=12.0, atvr_3m_pct=8.0, fot_3m_pct=75.0,
    )
    assert rules.screen_for_deletion(sec, cut, th).decision == "HOLD_IN"
    assert rules.screen_for_addition(sec, cut, th).decision == "HOLD_OUT"


def test_buffers_make_addition_and_deletion_asymmetric():
    """Between 0.67x and 1.5x the cut-off, membership depends only on history."""
    th, cut = _thresholds(), _cutoff()
    mid = CUTOFF_USD_MN * 1.0
    outsider = _security(full_mcap_usd_mn=mid)
    insider = _security(full_mcap_usd_mn=mid, is_standard_constituent=True)
    assert rules.screen_for_addition(outsider, cut, th).decision == "HOLD_OUT"
    assert rules.screen_for_deletion(insider, cut, th).decision == "HOLD_IN"


def test_equity_universe_minimum_size_excludes_micro_caps():
    th = _thresholds()
    assert not rules.passes_equity_universe(_security(full_mcap_usd_mn=400.0), th)
    assert rules.passes_equity_universe(_security(full_mcap_usd_mn=2_000.0), th)


# ---------------------------------------------------------------------------
# Review calendar
# ---------------------------------------------------------------------------


def test_november_2026_price_cutoff_window_is_late_october():
    cal = build_review_calendar(2026, 11, effective_date=date(2026, 11, 30))
    assert cal.review_type == "SAIR"
    assert cal.equity_universe_cutoff == date(2026, 8, 31)
    assert cal.liquidity_cutoff == date(2026, 9, 30)
    assert len(cal.price_cutoff_candidates) == 10
    assert cal.price_cutoff_candidates[0] == date(2026, 10, 19)
    assert cal.price_cutoff_candidates[-1] == date(2026, 10, 30)
    assert all(d.month == 10 for d in cal.price_cutoff_candidates)


def test_liquidity_window_is_the_twelve_months_to_september():
    cal = build_review_calendar(2026, 11)
    months = cal.liquidity_months(12)
    assert months[-1] == (2026, 9)
    assert months[-3:] == [(2026, 7), (2026, 8), (2026, 9)]
    assert months[0] == (2025, 10)


def test_mid_month_effective_date_prepones_the_price_window():
    """MSCI's own example: the November 2024 review took effect on 26 November and
    used 15-28 October rather than the last ten business days of the month."""
    cal = build_review_calendar(2024, 11, effective_date=date(2024, 11, 26))
    assert cal.price_cutoff_candidates[0] == date(2024, 10, 15)
    assert cal.price_cutoff_candidates[-1] == date(2024, 10, 28)
    assert cal.notes


def test_review_label_parsing():
    assert parse_review_label("2026-11") == (2026, 11)
    assert parse_review_label("Nov2026") == (2026, 11)
    assert parse_review_label("aug-2026") == (2026, 8)
    with pytest.raises(ValueError):
        parse_review_label("2026-07")        # July is not a review month


# ---------------------------------------------------------------------------
# Free-float reliability gate
# ---------------------------------------------------------------------------


def test_seasoned_name_with_clean_float_is_high_confidence():
    sec = _security(months_since_listing=60.0, strategic_gap_pp=1.0, fif_as_of_months_stale=0.0)
    assert rules.assess_float_reliability(sec).level == "HIGH"


def test_recent_listing_is_low_confidence_because_of_lock_ins():
    """The Meesho and Lenskart case at the May 2026 review.

    Both listed within six months of that price cut-off, both looked comfortably
    eligible on their current float, and MSCI added neither. Shareholding patterns
    report pre-IPO investors as public from day one while their shares are locked,
    so any free float estimate overstates what MSCI would use.
    """
    sec = _security(months_since_listing=4.5)
    r = rules.assess_float_reliability(sec)
    assert r.level == "LOW"
    assert any("lock-in" in reason for reason in r.reasons)


def test_lock_in_window_boundary():
    assert rules.assess_float_reliability(_security(months_since_listing=5.9)).level == "LOW"
    assert rules.assess_float_reliability(
        _security(months_since_listing=6.1, strategic_gap_pp=0.0, fif_as_of_months_stale=0.0)
    ).level == "HIGH"


def test_strategic_gap_is_reported_but_does_not_change_confidence():
    """Measured and found not to discriminate, so it informs only.

    At the May 2026 review Federal Bank, which MSCI added, carried a 22pp gap while
    SAIL, which it did not, carried 0.1pp. The quantity is SEBI public holding minus
    the estimate, so a large gap means the estimate is already conservative. Leaving
    it in the level calculation marked nearly everything MEDIUM and made the HIGH bar
    useless.
    """
    sec = _security(months_since_listing=90.0, strategic_gap_pp=48.0, fif_as_of_months_stale=0.0)
    r = rules.assess_float_reliability(sec)
    assert r.level == "HIGH"
    assert any("SEBI public holding" in reason for reason in r.reasons)


def test_only_the_lock_in_check_can_force_low_confidence():
    """The lock-in test is the one genuinely point-in-time signal, so it alone gates.

    Same company, two dates: inside the lock-in it is LOW, afterwards HIGH. That is
    what lets a MEDIUM bar reject Meesho at the May 2026 review and accept it in
    November, and it is worth 17 points of precision on May 2026 at no cost to recall.
    """
    inside = _security(months_since_listing=4.6, strategic_gap_pp=48.0, fif_as_of_months_stale=0.0)
    after = _security(months_since_listing=10.7, strategic_gap_pp=48.0, fif_as_of_months_stale=0.0)
    assert rules.assess_float_reliability(inside).level == "LOW"
    assert rules.assess_float_reliability(after).level == "HIGH"


def test_small_strategic_gap_does_not_reduce_confidence():
    sec = _security(months_since_listing=90.0, strategic_gap_pp=5.0, fif_as_of_months_stale=0.0)
    assert rules.assess_float_reliability(sec).level == "HIGH"


def test_stale_float_estimate_is_medium_confidence():
    sec = _security(months_since_listing=90.0, strategic_gap_pp=0.0, fif_as_of_months_stale=7.0)
    r = rules.assess_float_reliability(sec)
    assert r.level == "MEDIUM"
    assert any("newer than the" in reason for reason in r.reasons)


def test_dated_float_observation_is_not_stale():
    """Staleness of zero means a dated observation on or before the cut-off applied."""
    sec = _security(months_since_listing=90.0, strategic_gap_pp=0.0, fif_as_of_months_stale=0.0)
    assert rules.assess_float_reliability(sec).level == "HIGH"


def test_reliability_is_reported_but_never_blocks():
    """The gate is a data-quality heuristic, not an MSCI rule.

    It must not change a verdict, because doing so would mix a guess about input
    quality into a chain of published rules. The caller decides what to do with it.
    """
    th, cut = _thresholds(), _cutoff()
    sec = _security(
        full_mcap_usd_mn=CUTOFF_USD_MN * 2, months_since_listing=1.0,
        strategic_gap_pp=40.0, atvr_12m_pct=None, atvr_3m_pct=None, fot_3m_pct=None,
        fif=0.36,
    )
    v = rules.screen_for_addition(sec, cut, th)
    assert v.float_confidence == "LOW"
    assert v.float_warnings
    assert "Free-float estimate reliability" not in v.blocking_reasons


def test_confidence_ordering_is_usable_as_a_threshold():
    assert rules.CONFIDENCE_ORDER["LOW"] < rules.CONFIDENCE_ORDER["MEDIUM"]
    assert rules.CONFIDENCE_ORDER["MEDIUM"] < rules.CONFIDENCE_ORDER["HIGH"]
