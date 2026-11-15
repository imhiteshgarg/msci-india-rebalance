# State, verified 15 November 2026

Everything below was measured twice and reproduced identically. That check is not
ceremony — it is what caught the bugs described at the end.

## Verified results

Three reviews, each with its own MSCI parameters, membership rolled back from INDA
through MSCI's published change lists, dated free-float series, MEDIUM confidence bar,
6-of-10 price cut-off dates. Every figure reproduced on a second run.

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
| **Precision, one assumed cut-off** | 20% | 40% | 36% | 57% | 38% |
| **Precision, band at 40% share** | **25%** | **50%** | **57%** | **80%** | **53%** |
| Recall, band at 40% share | 100% | 100% | 100% | 100% | |

The threshold has to be **40% of the band, not half or more.** An earlier draft of this
file recommended 5-of-9 on the strength of three reviews. February 2026 refuted it: at
5-of-10 its recall is 50%, because L&T Finance - which MSCI did add - clears only 4 of
10 cut-offs. Three reviews were not enough to fix the parameter, which is the same
lesson as the cut-off itself.

Demanding survival of the *entire* band is worse still. It reaches 100% precision on
November 2026 and collapses to 25% recall on May 2026.

### February 2026 is the weakest review, and consistently so

| | Feb 2026 | Nov 2026 |
|---|---|---|
| Months between the cut-off date and the float snapshot | ~10 | ~1 |
| Precision at 40% band share | 25% | 80% |

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

| Review | Delete recall at the optimal cut-off |
|---|---|
| May 2026 | 25% (1 of 4) |
| Aug 2026 | **100%** (3 of 3) |
| Nov 2026 | 100% (1 of 1) |

Better than the earlier 0%, but still unreliable, and the May result shows why: the
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
split or bonus. 484 such changes were found across the panel. Only discontinuities
above 12% count, so dividends are left alone — they move the price without changing
the share count.

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

## Two bugs worth remembering

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

The second is the one to remember. A tool that refuses to answer is fine. One that
answers plausibly from a third of the market is not, because the cut-off is derived
from cumulative coverage of the whole universe and a missing third moves it by more
than the buffers that decide membership.

## Open items

- **LIC's share count is unverified.** Yahoo reports 12.65bn shares, exactly twice
  LIC's 6.32bn issued shares, and its reported market cap is internally consistent
  with the larger figure. The model's LIC calls match MSCI's actual decisions at the
  larger number, which is weak evidence for it, but worth confirming from a filing.
  36 of 1,319 symbols show share count × close disagreeing with reported market cap by
  more than 25%; none is currently an index-decision candidate.
- **Deletions remain under-predicted** because the Segment Number of Companies
  mechanism (GIMI 3.1.4.2, Appendix X) is not implemented. A predicted deletion means
  something; an absence of predicted deletions means nothing.
- **Only two reviews are verified.** Feb and Aug 2026 are recorded in
  `config/actual_reviews.yaml` and can be scored once historical membership is derived
  for them.

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
