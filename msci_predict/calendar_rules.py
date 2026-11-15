"""Index Review date arithmetic.

Implements GIMI section 3.1.9 "Date of Data Used for Index Reviews", which fixes
three different cut-off dates per review. Getting these right matters: liquidity is
frozen a month earlier than prices, and the price cut-off is deliberately one of ten
possible dates so that the market cannot front-run a single known snapshot.

For the November review:
  Equity Universe Cutoff : last business day of August
  Liquidity Cutoff       : last business day of September
  Price Cutoff           : any one of the last 10 business days of October
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta

# GIMI 3.1: reviews happen in February, May, August and November.
REVIEW_MONTHS = (2, 5, 8, 11)

# Offsets, in months before the review month, for each cut-off (GIMI 3.1.9).
_EQUITY_UNIVERSE_OFFSET_MONTHS = 3
_LIQUIDITY_OFFSET_MONTHS = 2
_PRICE_OFFSET_MONTHS = 1

PRICE_CUTOFF_WINDOW_DAYS = 10


def _shift_month(year: int, month: int, back: int) -> tuple[int, int]:
    total = (year * 12 + (month - 1)) - back
    return total // 12, total % 12 + 1


def _business_days_in_month(year: int, month: int) -> list[date]:
    """Mon-Fri days in a month.

    GIMI defines a business day as a weekday on which markets making up more than
    80% of ACWI free-float market cap are expected to open, so this is a global
    calendar, not the NSE calendar. A plain Mon-Fri list is the right starting
    point; feed `extra_holidays` for the handful of global closures MSCI names
    (Thanksgiving, Good Friday, Easter Monday).
    """
    last = calendar.monthrange(year, month)[1]
    return [
        d
        for day in range(1, last + 1)
        if (d := date(year, month, day)).weekday() < 5
    ]


def last_business_day(year: int, month: int) -> date:
    return _business_days_in_month(year, month)[-1]


@dataclass(frozen=True)
class ReviewCalendar:
    """All dates that govern one Index Review."""

    review_year: int
    review_month: int
    equity_universe_cutoff: date
    liquidity_cutoff: date
    price_cutoff_candidates: tuple[date, ...]
    effective_date: date | None = None
    announcement_date: date | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.review_year}-{self.review_month:02d}"

    @property
    def review_type(self) -> str:
        # May and November are the Semi-Annual Index Reviews, which rebalance the
        # full size-segment structure. February and August are Quarterly Reviews.
        return "SAIR" if self.review_month in (5, 11) else "QIR"

    @property
    def price_cutoff_window(self) -> tuple[date, date]:
        return self.price_cutoff_candidates[0], self.price_cutoff_candidates[-1]

    def liquidity_months(self, count: int) -> list[tuple[int, int]]:
        """The `count` complete months ending at the liquidity cut-off, oldest first.

        ATVR is computed from whole calendar months, so the 3-month window for a
        November review is July/August/September and the 12-month window is the
        preceding October through September.
        """
        y, m = self.liquidity_cutoff.year, self.liquidity_cutoff.month
        months = []
        for back in range(count - 1, -1, -1):
            months.append(_shift_month(y, m, back))
        return months


def build_review_calendar(
    review_year: int,
    review_month: int,
    effective_date: date | None = None,
    announcement_date: date | None = None,
    extra_holidays: frozenset[date] = frozenset(),
) -> ReviewCalendar:
    """Derive the three cut-off dates for a review.

    `effective_date` matters because of the footnote to GIMI 3.1.9: when the review
    takes effect inside the announcement month rather than at the start of the
    following month, the price cut-off window is brought forward by the same number
    of business days. MSCI's own worked example is the November 2024 review, which
    took effect on 26 November and therefore used 15-28 October instead of the last
    ten business days of the month.
    """
    if review_month not in REVIEW_MONTHS:
        raise ValueError(
            f"{review_month} is not an MSCI review month; expected one of {REVIEW_MONTHS}"
        )

    notes: list[str] = []

    eu_y, eu_m = _shift_month(review_year, review_month, _EQUITY_UNIVERSE_OFFSET_MONTHS)
    liq_y, liq_m = _shift_month(review_year, review_month, _LIQUIDITY_OFFSET_MONTHS)
    px_y, px_m = _shift_month(review_year, review_month, _PRICE_OFFSET_MONTHS)

    price_month_days = [
        d for d in _business_days_in_month(px_y, px_m) if d not in extra_holidays
    ]

    prepone = 0
    if effective_date is not None:
        month_business_days = [
            d
            for d in _business_days_in_month(review_year, review_month)
            if d not in extra_holidays
        ]
        if effective_date in month_business_days:
            # Count business days between the effective date and month end.
            prepone = len(month_business_days) - 1 - month_business_days.index(effective_date)
        if prepone:
            notes.append(
                f"Effective {effective_date} is {prepone} business day(s) before the end of "
                f"the review month, so the price cut-off window is preponed by {prepone} day(s) "
                "per the footnote to GIMI 3.1.9."
            )

    end = len(price_month_days) - prepone
    start = end - PRICE_CUTOFF_WINDOW_DAYS
    if start < 0:
        raise ValueError("Price cut-off window runs off the start of the month.")
    candidates = tuple(price_month_days[start:end])

    return ReviewCalendar(
        review_year=review_year,
        review_month=review_month,
        equity_universe_cutoff=last_business_day(eu_y, eu_m),
        liquidity_cutoff=last_business_day(liq_y, liq_m),
        price_cutoff_candidates=candidates,
        effective_date=effective_date,
        announcement_date=announcement_date,
        notes=notes,
    )


def parse_review_label(label: str) -> tuple[int, int]:
    """Accept '2026-11' or 'Nov2026' or 'nov-2026'."""
    label = label.strip()

    def _validate(year: int, month: int) -> tuple[int, int]:
        if month not in REVIEW_MONTHS:
            raise ValueError(
                f"{label!r} resolves to month {month}, which is not an MSCI Index Review "
                f"month. Reviews happen in {REVIEW_MONTHS}."
            )
        return year, month

    if "-" in label and label.split("-")[0].isdigit():
        y, m = label.split("-")[:2]
        return _validate(int(y), int(m))
    month_names = {
        "feb": 2, "february": 2,
        "may": 5,
        "aug": 8, "august": 8,
        "nov": 11, "november": 11,
    }
    cleaned = label.lower().replace("-", "").replace("_", "").replace(" ", "")
    for name, num in month_names.items():
        if cleaned.startswith(name):
            rest = cleaned[len(name):]
            if rest.isdigit() and len(rest) == 4:
                return _validate(int(rest), num)
        if cleaned.endswith(name):
            rest = cleaned[: -len(name)]
            if rest.isdigit() and len(rest) == 4:
                return _validate(int(rest), num)
    raise ValueError(f"Could not parse review label {label!r}; try '2026-11' or 'Nov2026'.")


def history_start_date(cal: ReviewCalendar, months_of_history: int = 18) -> date:
    """Earliest date price history is needed for.

    Needs to cover the 12-month ATVR window plus the longest Extreme Price Increase
    look-back (250 weekday days), with slack for month-boundary effects.
    """
    y, m = _shift_month(cal.liquidity_cutoff.year, cal.liquidity_cutoff.month, months_of_history)
    return date(y, m, 1) - timedelta(days=5)
