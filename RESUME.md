# State, verified 15 November 2026

Everything below was measured twice and reproduced identically. That check is not
ceremony — it is what caught the bugs described at the end.

## Verified results

Four reviews, each with its own MSCI parameters, membership rolled back from INDA
through MSCI's published change lists, dated free-float series, MEDIUM confidence bar,
6-of-10 price cut-off dates. Every figure reproduced on a second run.

**Re-measured after the phantom trading day fix** (see below). The single-cut-off
precision figures in the table below are higher than this file previously recorded,
and that is not the fix: every number was reproduced on the pre-fix panel and came out
identical. The old row understated precision for May, August and November.

### Single assumed cut-off vs a cut-off band

The size cut-off cannot be pinned down and cannot be carried forward. Across four
reviews the optimal value ranged from **0.635x to 0.774x** the EM Standard reference -
inside MSCI's own 0.5x-1.15x range, but a 22% spread, and a 13% error cost 17 points
of precision on May 2026.

So rather than guess one value, run the screen across the plausible band
(`--robustness`) and keep names that clear a minimum share of it.

| | Feb 2026 | May 2026 | Aug 2026 | Nov 2026 | mean |
|---|---|---|---|---|---|
| EM GMSR (USD mn) | 7,599 | 7,874 | 8,138 | 8,686 | |
| Optimal cut-off | 5,400 | 5,000 | 6,300 | 5,600 | |
| as a multiple of GMSR | 0.711 | 0.635 | 0.774 | 0.645 | |
| **Recall, one assumed cut-off** | **50%** | 100% | 100% | 100% | |
| **Precision, one assumed cut-off** | 20% | 57% | 67% | 80% | 56% |
| **Precision, band at 40% share** | **20%** | **50%** | **50%** | **80%** | **50%** |
| Recall, band at 40% share | 100% | 100% | 100% | 100% | |

**Recall is not 100% at a single assumed cut-off.** February 2026 comes out at 50%,
because L&T Finance - which MSCI did add - clears only 5 of the 10 cut-offs in the
band and none of the 10 price cut-off dates at USD 5,400 mn. The 100% recall this
project reports is a property of the *band* method, not of a single cut-off, and the
two were conflated in earlier drafts of this file. Quote the band row when claiming
full recall.

The threshold has to be **40% of the band, not half or more.** An earlier draft
recommended 5-of-9 on the strength of three reviews, and February 2026 refuted it.
Measured across all four, 40% is the highest threshold that keeps full recall
everywhere: May 2026 needs 4-of-10 and loses recall at 5-of-10.

Demanding survival of the *entire* band is worse still. It reaches 100% precision on
November 2026 and collapses to 25% recall on May 2026.

### February 2026 is the weakest review, and consistently so

| | Feb 2026 | Nov 2026 |
|---|---|---|
| Months between the cut-off date and the float snapshot | ~10 | ~1 |
| Precision at 40% band share | 20% | 80% |

Precision falls monotonically with that gap across all four reviews. It is the same
model reading progressively staler capital structure onto older prices, which is why
the live case is the best case and historical back-tests understate live accuracy.

February also shows the failure mode most clearly: four false positives cleared the
entire cut-off band, ranking *above* a genuine addition. Two of the four were added by
MSCI at a later review.

| Feb 2026 false positive | Outcome |
|---|---|
| Adani Energy Solutions | added at the August 2026 review |
| Adani Green Energy | added at the November 2026 review |
| Angel One | not added through November 2026 |
| Swiggy | not added through November 2026 |

So on half of them the model was **early rather than wrong**: it had the company
clearing MSCI's size and liquidity arithmetic one to three reviews before MSCI acted.
That is a consistent bias, not noise, and it points at the same cause as everywhere
else - the free-float estimate sits above the factor MSCI actually applies, so a
company looks eligible sooner than it is.

### Deletions

| Review | Delete recall | Delete precision |
|---|---|---|
| Feb 2026 | **100%** (1 of 1) | 20% (1 of 5) |
| May 2026 | 25% (1 of 4) | 33% (1 of 3) |
| Aug 2026 | **100%** (3 of 3) | **75% (3 of 4)** |
| Nov 2026 | **100%** (1 of 1) | 50% (1 of 2) |

NMDC was removed from the deletion predictions in Feb and Aug by adding a dated FIF
anchor at Sep 2021 (NSE confirms 39.21% public holding stable from Sep 2021 through
Sep 2026, with the transition from the government's earlier 80% stake happening
between Dec 2015 and Sep 2021). The May result is unchanged because NMDC was not
nominated at the USD 5,000mn cut-off.

`IDEA` remains a spurious deletion in Feb, May and Aug 2026. The delta method
produces FIF = 0.126 for those periods because of its 48.7pp strategic gap — the
government holds a ~33% stake that SEBI classifies as "public" but MSCI would treat
as strategic, so changes in SEBI public holding don't map to changes in MSCI free
float. MSCI's actual FIF is ~0.246 (backed out from the INDA weight). The MSCI-implied
FIF has been added to `free_float_history.csv` with an effective date of Nov 2026, so
IDEA is correctly held in for the **live Feb 2027 prediction** and all future reviews,
but cannot be repaired for the historical back-tests without data that doesn't exist.

The May result shows the remaining structural gap: the
Segment Number of Companies mechanism (GIMI 3.1.4.2, Appendix X) is not implemented,
so deletions driven by company-count management rather than the buffer are invisible.
A predicted deletion means something; an absence of them means nothing.

### What remains wrong, and why

The surviving false positives are Adani Green and Meesho in August, Hyundai in
November, Adani Green, SAIL and Swiggy in May. All need MSCI's own free-float factor,
and none was an index constituent at the time, so `scripts/derive_msci_fif.py` cannot
price them - it only works where a fund weight exists. Adani Green and Meesho were
both added by MSCI at a later review, so the model was early rather than wrong.

A uniform float haircut was tested and rejected: it removes SAIL but also kills Indian
Bank, trading 25% recall for 7% precision. Recall is the model's strength and is not
worth trading for that.

## What was fixed in this round

**Price source replaced.** Yahoo Finance rate-limited heavily (HTTP 429) and is no
longer the default. Prices now come from NSE's own bhavcopy end-of-day files: one
request per trading day, every security included, no throttling observed, and the full
3.5-year panel rebuilds in about 2.5 minutes. Yahoo is still used for share counts and
float, which have no free alternative, and remains selectable with
`--price-source yfinance`.

Bhavcopy does not publish a split-adjusted series. It is reconstructed from the file
itself: NSE restates `PREV_CLOSE` for corporate actions, so `PREV_CLOSE(t) /
CLOSE(t-1)` is 1.0 on an ordinary day and equals the capital-change factor across a
split or bonus. **76** such changes exist across the panel. Only discontinuities above
12% count, so dividends are left alone — they move the price without changing the
share count.

This file previously recorded 484. That number was an artefact: 408 of those 484 were
manufactured by the phantom trading days described below, and vanished once the panel
was rebuilt without them.

**Dated free-float series, derived rather than hand-written.** NSE's shareholding
pattern API gives quarterly promoter and public holding. 4,773 dated rows now cover
316 symbols.

The subtlety that matters: SEBI's "public holding" is *not* MSCI's float, and the gap
is large (Meesho 48pp, Adani Green 17pp) because SEBI counts strategic non-promoter
holders that MSCI excludes. So the series is used two ways. Where the gap is within
2pp the reported *level* is used directly; otherwise only the *change* is used,
applied to the current estimate.

That distinction decided a real case. LIC's gap is −1.25pp, so public holding is its
float. Using the delta method put its April 2026 FIF at 0.0475 instead of 0.035, and
because its free-float market cap sat within 1% of the threshold, that 1.25pp was the
entire prediction. LIC is now correctly excluded from May 2026 and included in
November.

**Lock-in gate.** SEBI lock-ins run up to six months for pre-IPO holders, and
shareholding patterns report those holders as public from day one while MSCI treats
locked shares as non-free float. Any company listed inside that window is marked LOW
confidence and withheld into a separate FLAGGED section. It caught Meesho, Lenskart
and Groww at the May 2026 review.

**Strategic gap removed from the confidence calculation.** It went in as a gate, was
demoted to a cap, and is now reporting only, because measurement said it does not
discriminate: Federal Bank, which MSCI added in May 2026, carries a 22pp gap while
SAIL, which it did not, carries 0.1pp.

The quantity was mis-specified. gap = SEBI public holding minus the float estimate, so
a large gap means the estimate already sits well *below* public holding, which is
conservative rather than suspect. What would discriminate is where the estimate sits
*above* MSCI's own float, and that is only knowable for current constituents, via
`scripts/derive_msci_fif.py`. Leaving it in also did harm: it marked nearly every name
MEDIUM, which made the HIGH bar useless.

## Four bugs worth remembering

**NSE serves the previous session's file for a holiday URL.** Asking the archive for
25 December 2025 returns HTTP 200 with 3,090 rows stamped `24-Dec-2025`. The loader
accepted it, so the holiday entered the panel as a trading day carrying a duplicate of
the day before. 51 of 1,152 cached files were like this, every one a known NSE holiday.

The duplicated close was the harmless part. The date also landed in the Frequency of
Trading denominator and double counted a session in the ATVR series, and worse, it
corrupted the adjusted price series: `ratio(t) = PREV_CLOSE(t)/CLOSE(t-1)` is measured
against the previous *row*, so a phantom row yields roughly the inverse of the prior
day's return. Any symbol that moved more than the 12% threshold the day before a
holiday acquired a spurious capital-change factor, which then propagated to every
earlier date for that symbol. 445 of 3,557 symbols had distorted history, 32 of them
by more than 50%, including Kotak Bank at 4.07x.

`fetch_day` now checks the file's own `DATE1` against the date requested and drops a
mismatch. The check runs after parsing rather than at download, so files already in
the cache are validated too. Note that a normal run cannot repair this on its own:
`_merge_panels` uses `combine_first`, so phantom rows survive any rebuild and the
panel had to be replaced explicitly.

Measured effect on accuracy: **none.** Liquidity figures move 4-5% (12-month ATVR for
MCX 144.3% to 136.9%), the trading calendar drops from 1,152 days to 1,101, and no
back-test decision on any of the four reviews changes. It matters for names near a
threshold and for the membership replay, which reads historical market caps.

**A reach into the future permanently blacklisted it.** NSE answers a date that has not
happened with the same 404 it gives a genuine holiday, and `_record_holiday` wrote both
into `_holidays.txt` as permanent facts. One forward-looking run recorded all 55
weekdays from 2026-11-16 to 2027-01-29, including every candidate price cut-off date
for the February 2027 review. Because `fetch_day` short-circuits on that set before
issuing any request, `--refresh` could not recover them either. Holidays are now only
recorded for dates strictly in the past, and today is excluded as well, since the
bhavcopy is published after the close.

Alongside it, `price_panel_meta.json` was left claiming a `requested_end` of
2027-01-29 against a panel ending 2026-11-13, so every later run judged itself fresh
and returned a stale panel. The recorded end is now capped at the last row held.

**The price cache was replaced rather than merged.** A back-test fetching data ending
30 April drops every symbol listed later; writing that over the full panel shrank the
cache on every run, eventually to a fifth of the market. Now the requested window is
widened to the union of all previous requests, and fresh data is merged rather than
substituted.

**A throttled fetch produced a confidently wrong answer.** Yahoo returns an empty
frame on 429. The old code warned and carried on, so the universe silently shrank, the
derived cut-off moved with it, and identical commands gave different predictions. Now
there is per-batch backoff and a hard floor: below `--min-price-coverage` (default 70%)
the run aborts and leaves the cache untouched.

The throttled-fetch one is the one to remember. A tool that refuses to answer is fine.
One that answers plausibly from a third of the market is not, because the cut-off is
derived from cumulative coverage of the whole universe and a missing third moves it by
more than the buffers that decide membership.

The common thread across all four is that each cached a *guess* as a fact — a holiday
that was only an unpublished future, a substituted file taken for the day requested, a
requested range taken for a delivered one. The lesson is to validate what came back
against what was asked for, and the corollary is that the numbers in this file are
themselves only as good as their last re-measurement. Three of them were wrong until
re-run.

## Open items

- **Deletion precision still improvable.** `IDEA` remains a spurious deletion in three
  historical reviews (Feb/May/Aug 2026) because its 48.7pp strategic gap means NSE
  public holding changes don't track MSCI free float changes. The delta method is
  fundamentally misspecified for IDEA in this period. No fix without MSCI's actual
  dated FIF, which is not public for non-current-constituents.
- **Deletions remain structurally under-predicted** because the Segment Number of
  Companies mechanism (GIMI 3.1.4.2, Appendix X) is not implemented. A predicted
  deletion means something; an absence of predicted deletions means nothing.
- **Four recorded reviews** is a small sample for fitting the cut-off band and the
  40% band-share threshold. Those two settings remain the least well-established
  part of the model.

## Next commands

```bash
cd ~/msci-india-rebalance

# Live prediction for a review (no Yahoo dependency for prices)
.venv/bin/python run_prediction.py --review 2027-02 \
    --constituents-file config/msci_india_constituents.csv \
    --min-float-confidence MEDIUM --backtest --min-dates 6

# Keep the float series current - run after each quarterly filing season
.venv/bin/python scripts/fetch_shareholding_history.py --from-universe --limit 500 --merge
```
