"""Console and file output."""

from __future__ import annotations

import csv
from pathlib import Path

from .backtest import BacktestResult
from .pipeline import Prediction

_LINE = "=" * 88
_THIN = "-" * 88


def _fmt_usd_mn(v: float) -> str:
    return f"{v:,.0f}"


def summary(prediction: Prediction, top_n: int = 25) -> str:
    cal = prediction.calendar
    lo, hi = prediction.cutoff_range
    out: list[str] = []
    a = out.append

    a(_LINE)
    a(f"MSCI India Standard Index - {cal.label} {cal.review_type} prediction")
    a(_LINE)
    a(f"Equity Universe cut-off : {cal.equity_universe_cutoff}")
    a(f"Liquidity cut-off       : {cal.liquidity_cutoff}  (ATVR / Frequency of Trading frozen here)")
    total = prediction.diagnostics.get("candidate_dates_total", prediction.n_dates)
    missing = prediction.diagnostics.get("candidate_dates_missing", 0)
    a(f"Price cut-off candidates: {cal.price_cutoff_candidates[0]} .. {cal.price_cutoff_candidates[-1]}"
      f"  ({prediction.n_dates} of {total} screened)")
    if missing:
        a(f"  *** PARTIAL WINDOW: {missing} date(s) have no prices yet (latest "
          f"{prediction.diagnostics.get('latest_price_date')}). Confidence below is out of "
          f"{prediction.n_dates}, not {total}, so it is NOT comparable with a later run. ***")
    if cal.announcement_date:
        a(f"Announced               : {cal.announcement_date}")
    if cal.effective_date:
        a(f"Effective               : {cal.effective_date}")
    for note in cal.notes:
        a(f"  note: {note}")
    a("")
    a(f"Universe screened       : {prediction.diagnostics.get('universe_size')} securities")
    a(f"Investable universe     : {prediction.date_results[-1].investable_count} companies")
    a(f"Standard cut-off        : USD {_fmt_usd_mn(lo)} - {_fmt_usd_mn(hi)} mn "
      f"(mean {_fmt_usd_mn(prediction.cutoff_mean)}), mode={prediction.diagnostics.get('cutoff_mode')}")
    rng = prediction.diagnostics.get("em_size_range_usd_mn")
    if rng:
        a(f"EM Global Min Size Range: USD {_fmt_usd_mn(rng[0])} - {_fmt_usd_mn(rng[1])} mn")
    last = prediction.date_results[-1].cutoff
    a(f"  raw 85%-coverage point: USD {_fmt_usd_mn(last.raw_coverage_cutoff)} mn"
      + (f", clamped to {last.clamped} bound" if last.clamped else ", inside the range"))
    a(f"  min FFMC for Standard : USD {_fmt_usd_mn(last.min_ffmc_for_standard)} mn "
      f"(50% of cut-off); low-FIF names need 1.8x that")
    a(f"  add buffer (1.5x)     : USD {_fmt_usd_mn(last.cutoff * 1.5)} mn full market cap")
    a(f"  delete buffer (0.67x) : USD {_fmt_usd_mn(last.cutoff * 2 / 3)} mn full market cap")
    a(f"USD/INR at cut-off      : {prediction.date_results[-1].usdinr:.2f}")
    a(f"Constituent set         : {len(prediction.constituents)} names, {prediction.constituent_source}")
    hist = prediction.diagnostics.get("constituent_history") or []
    if hist:
        a("  membership replay     : " + "  ".join(hist))
        a("  (MSCI's India Index factsheet reports around 165 constituents, "
          "so the final count is a sanity check)")
    overrides = prediction.diagnostics.get("fif_overrides") or []
    if overrides:
        a(f"Free-float overrides    : {', '.join(overrides)}")
    a("")

    a(_THIN)
    a("PREDICTED ADDITIONS to the India Standard Index")
    a(_THIN)
    adds = prediction.ranked_additions()
    if not adds:
        a("  none")
    else:
        a(f"  {'Symbol':<14}{'Dates':>6}{'Conf':>7}  {'Mcap/cut':>9}{'FFMC/req':>9}{'FIF':>7}  "
          f"{'Path':<17}Name")
        for sym, count, mult in adds[:top_n]:
            v = next(
                (r.all_verdicts[sym] for r in prediction.date_results if sym in r.all_verdicts), None
            )
            conf = count / prediction.n_dates
            a(f"  {sym:<14}{count:>3}/{prediction.n_dates:<2}{conf:>6.0%}  "
              f"{mult:>9.2f}{(v.ffmc_multiple if v else 0):>9.2f}{(v.fif if v else 0):>7.3f}  "
              f"{(v.path if v else ''):<17}{prediction.names.get(sym, '')[:30]}")

    a("")
    a(_THIN)
    a("PREDICTED DELETIONS from the India Standard Index")
    a(_THIN)
    dels = prediction.ranked_deletions()
    if not dels:
        a("  none")
    else:
        a(f"  {'Symbol':<14}{'Dates':>6}{'Conf':>7}  {'Mcap/cut':>9}{'FFMC/req':>9}  Reason")
        for sym, count, mult in dels[:top_n]:
            v = next(
                (r.all_verdicts[sym] for r in prediction.date_results if sym in r.all_verdicts), None
            )
            conf = count / prediction.n_dates
            reasons = ", ".join(v.blocking_reasons) if v else ""
            a(f"  {sym:<14}{count:>3}/{prediction.n_dates:<2}{conf:>6.0%}  "
              f"{mult:>9.2f}{(v.ffmc_multiple if v else 0):>9.2f}  {reasons[:34]}")

    a("")
    a(_THIN)
    a("FLAGGED - passed every MSCI rule, but the free-float estimate is not trustworthy")
    a(_THIN)
    if not prediction.flagged_frequency:
        a("  none")
    else:
        a("  These cleared the published screens and were withheld from the prediction")
        a("  above because the float input driving them is unreliable. Historically this")
        a("  is where the false positives live, so treat them as watchlist, not signal.")
        a("")
        a(f"  {'Symbol':<14}{'Dates':>6}  {'Mcap/cut':>9}{'FIF':>7}  Why the float is doubtful")
        last_res = prediction.date_results[-1]
        for sym, count in sorted(
            prediction.flagged_frequency.items(), key=lambda kv: -kv[1]
        ):
            v = last_res.all_verdicts.get(sym)
            if v is None:
                continue
            a(f"  {sym:<14}{count:>3}/{prediction.n_dates:<2}  "
              f"{v.size_multiple:>9.2f}{v.fif:>7.3f}  "
              f"{(v.float_warnings[0] if v.float_warnings else v.float_confidence)[:44]}")
    a("")
    a(_THIN)
    a("NEAR MISSES - cleared every screen except size, ranked by how close")
    a(_THIN)
    near = near_misses(prediction, limit=12)
    if not near:
        a("  none")
    else:
        a(f"  {'Symbol':<14}{'Mcap/cut':>9}{'Gap to 1.5x':>13}  {'FIF':>6}  Blocking")
        for sym, mult, gap, fif, blocking in near:
            a(f"  {sym:<14}{mult:>9.2f}{gap:>12.1f}%  {fif:>6.3f}  {', '.join(blocking)[:32]}")
    a("")
    return "\n".join(out)


def near_misses(prediction: Prediction, limit: int = 12) -> list[tuple[str, float, float, float, list[str]]]:
    """Non-constituents failing only on size or float, closest first.

    These are the names worth watching: a few percent of price movement before the
    cut-off date flips them. Sell-side notes quote exactly this number when they say
    a stock "needs to trade at X to come into contention".

    The reported gap is the price move needed to clear whichever test is binding.
    A name blocked on free-float market cap rather than full market cap needs a
    larger move, because only its float counts, so the two cases are not
    interchangeable and the gap is computed per test.
    """
    last = prediction.date_results[-1]
    size_test = "Size: +50% add buffer"
    ffmc_test = "Minimum FFMC for Standard"
    rows = []
    for sym, v in last.all_verdicts.items():
        if sym in prediction.constituents or v.decision == "ADD":
            continue
        blocking = set(v.blocking_reasons)
        if not blocking or not blocking.issubset({size_test, ffmc_test}):
            continue
        gaps = []
        if size_test in blocking and v.size_multiple > 0:
            gaps.append(1.5 / v.size_multiple - 1)
        if ffmc_test in blocking and v.ffmc_multiple > 0:
            gaps.append(1.0 / v.ffmc_multiple - 1)
        if not gaps:
            continue
        gap = max(gaps) * 100
        if gap > 100:          # beyond a doubling it is not a near miss
            continue
        rows.append((sym, v.size_multiple, gap, v.fif, sorted(blocking)))
    rows.sort(key=lambda r: r[2])
    return rows[:limit]


def explain(prediction: Prediction, symbol: str) -> str:
    """Full check-by-check trace for one symbol on each candidate date."""
    symbol = symbol.upper()
    out = [_LINE, f"Screen trace: {symbol} - {prediction.names.get(symbol, 'unknown')}", _LINE]
    present = False
    for r in prediction.date_results:
        v = r.all_verdicts.get(symbol)
        if not v:
            continue
        present = True
        out.append(
            f"\n{r.price_cutoff}  cut-off USD {_fmt_usd_mn(r.cutoff.cutoff)}mn  "
            f"USD/INR {r.usdinr:.2f}  ->  {v.decision}  (path: {v.path})"
        )
        out.append(
            f"  full mcap USD {_fmt_usd_mn(v.full_mcap_usd_mn)}mn = {v.size_multiple:.2f}x cut-off | "
            f"FFMC USD {_fmt_usd_mn(v.ffmc_usd_mn)}mn = {v.ffmc_multiple:.2f}x required | FIF {v.fif:.3f}"
        )
        for c in v.checks:
            out.append(f"    {c}")
    if not present:
        out.append(
            f"  {symbol} is not in the screened universe. It may be below the Equity Universe "
            "minimum size, missing share-count data, or absent from the symbol list."
        )
    return "\n".join(out)


def backtest_report(result: BacktestResult, prediction: Prediction) -> str:
    out = [_LINE, f"BACK-TEST vs actual MSCI announcement - {result.review_label}", _LINE]
    out.append(f"Confidence threshold: a name counts as predicted if it cleared "
               f"{result.threshold_dates} or more of {result.n_dates} candidate price cut-off dates.")
    for score in (result.additions, result.deletions):
        out.append("")
        out.append(f"{score.label}")
        out.append(f"  actual    : {', '.join(score.actual) or '(none recorded)'}")
        out.append(f"  predicted : {', '.join(score.predicted) or '(none)'}")
        out.append(f"  correct   : {', '.join(score.hits) or '(none)'}")
        out.append(f"  missed    : {', '.join(score.misses) or '(none)'}")
        out.append(f"  spurious  : {', '.join(score.false_positives) or '(none)'}")
        if score.recall is not None:
            out.append(f"  recall    : {score.recall:.0%}   precision: "
                       f"{score.precision:.0%}" if score.precision is not None else "")
    if result.announced_but_reversed:
        out.append("")
        out.append(f"  Predicted and announced by MSCI, then withdrawn before taking effect: "
                   f"{', '.join(result.announced_but_reversed)}")
        out.append("  Excluded from the counts above. The size and liquidity call matched "
                   "MSCI's; the")
        out.append("  reversal came from a regulatory overlay the model does not model.")
    out.append("")
    out.append(_THIN)
    out.append("Per-name diagnosis")
    out.append(_THIN)
    out.append(f"  {'Symbol':<14}{'Dates':>6}  {'Mcap/cut':>9}{'FFMC/req':>9}  {'Member':>7}  Blocking")
    for sym, d in sorted(result.detail.items(), key=lambda kv: -(kv[1]["dates_passed"] or 0)):
        if not d["in_universe"]:
            out.append(f"  {sym:<14}{'-':>6}  {'n/a':>9}{'n/a':>9}  {'-':>7}  NOT IN SCREENED UNIVERSE")
            continue
        out.append(
            f"  {sym:<14}{d['dates_passed']:>3}/{result.n_dates:<2}  "
            f"{d['mean_size_multiple']:>9.2f}{d['mean_ffmc_multiple']:>9.2f}  "
            f"{'yes' if d['is_constituent'] else 'no':>7}  "
            f"{', '.join(d['blocking_reasons'])[:40] or 'none'}"
        )
    return "\n".join(out)


def write_candidates_csv(prediction: Prediction, path: Path) -> None:
    """Every screened security with its verdict on the final candidate date."""
    path.parent.mkdir(parents=True, exist_ok=True)
    last = prediction.date_results[-1]
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([
            "symbol", "name", "is_constituent", "decision", "dates_passed", "n_dates",
            "full_mcap_usd_mn", "ffmc_usd_mn", "fif", "size_multiple_of_cutoff",
            "ffmc_multiple_of_requirement", "path", "float_confidence",
            "float_warnings", "blocking_reasons",
        ])
        rows = []
        for sym, v in last.all_verdicts.items():
            passed = prediction.add_frequency.get(sym, 0) or prediction.delete_frequency.get(sym, 0)
            rows.append((
                sym, prediction.names.get(sym, ""), sym in prediction.constituents, v.decision,
                passed, prediction.n_dates, round(v.full_mcap_usd_mn, 1), round(v.ffmc_usd_mn, 1),
                round(v.fif, 4), round(v.size_multiple, 4), round(v.ffmc_multiple, 4), v.path,
                v.float_confidence, "; ".join(v.float_warnings),
                "; ".join(v.blocking_reasons),
            ))
        rows.sort(key=lambda r: -r[9])
        w.writerows(rows)


def write_cutoff_csv(prediction: Prediction, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([
            "price_cutoff_date", "cutoff_usd_mn", "raw_coverage_cutoff_usd_mn", "clamped",
            "segment_number_of_companies", "coverage_achieved", "investable_companies",
            "usdinr", "n_additions", "n_deletions",
        ])
        for r in prediction.date_results:
            w.writerow([
                r.price_cutoff, round(r.cutoff.cutoff, 1), round(r.cutoff.raw_coverage_cutoff, 1),
                r.cutoff.clamped or "none", r.cutoff.segment_number_of_companies,
                round(r.cutoff.coverage_achieved, 4), r.investable_count, round(r.usdinr, 4),
                len(r.additions), len(r.deletions),
            ])
