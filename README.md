# MSCI India Standard Index rebalance predictor

A from-the-methodology model of which Indian stocks MSCI will add to and drop from
the **MSCI India Standard Index** (the large + mid cap segment that `INDA` and the
EM index funds track) at each Quarterly Index Review.

The premise is correct: MSCI's index construction rules are fully published and
entirely deterministic. There is no proprietary formula to reverse-engineer. What
firms like Nuvama, Centrum and IIFL sell is not a secret model but **better inputs** —
point-in-time share counts, a careful shareholder-by-shareholder free-float
classification, and clean traded-value history. This repo implements the published
rules faithfully and is explicit about exactly where its free inputs are weaker than
theirs.

---

## 1. How MSCI actually decides

Everything below traces to the
[MSCI Global Investable Market Indexes (GIMI) Methodology, November 2026](https://www.msci.com/eqb/methodology/meth_docs/MSCI_GIMIMethodology_Nov2026.pdf)
and the [MSCI Liquidity Data Methodology, May 2025](https://www.msci.com/documents/10199/995265d3-adce-42ce-dad7-a1696b3f800a).
Section numbers are cited in the code so each rule can be re-verified when MSCI
revises the book. *Content from MSCI documents has been paraphrased for compliance
with licensing restrictions.*

### 1.1 The three cut-off dates (GIMI 3.1.9)

A review does not use one snapshot of data. It uses three, taken a month apart:

| Cut-off | November review | What it fixes |
|---|---|---|
| Equity Universe | last business day of **August** | the eligible security list, Equity Universe Minimum Size |
| Liquidity | last business day of **September** | ATVR and Frequency of Trading |
| Price | **any one of the last 10 business days of October** | prices, market caps, share counts, Foreign Inclusion Factors |

The price cut-off is the interesting one. MSCI deliberately does not say which of the
ten dates it used, so no single date can be front-run. That is why this model screens
**all ten** and reports a confidence out of ten rather than a yes/no — see §3.

For the November 2026 review the window was **19–30 October 2026**. A footnote to
3.1.9 prepones the window when the review takes effect mid-month; since 30 November
2026 was the last business day of the month, no preponement applied.

### 1.2 Equity Universe screens (GIMI 2.2)

A company must clear all of these to be in India's Market Investable Equity Universe:

| Screen | Threshold (India, an Emerging Market) | Section |
|---|---|---|
| Equity Universe Minimum Size | full market cap ≥ **USD 585 mn** | 2.2.3 |
| Minimum free-float market cap | FFMC ≥ 50% of the above (**USD 292.5 mn**) | 2.2.4 |
| 3-month ATVR | ≥ **15%** | 2.2.5 |
| 12-month ATVR | ≥ **15%** | 2.2.5 |
| 3-month Frequency of Trading | ≥ **80%** | 2.2.5 |
| Foreign Inclusion Factor | ≥ **0.15** | 2.2.6 |
| Minimum length of trading | ≥ **3 months** since listing | 2.2.7 |
| Minimum foreign room | ≥ **15%** where a foreign ownership limit applies | 2.2.8 |

Note the EM thresholds are 15%/15%/80%, not the 20%/20%/90% that applies to
Developed Markets. Getting this wrong tightens the screen materially.

`USD 585 mn` is the figure MSCI published for the August 2026 review; it is the full
market cap of the company sitting at 99% cumulative free-float coverage of the
Developed Markets Equity Universe, and it is revised each review.

**The figures quoted through §1 are the August 2026 ones, to match the methodology
edition cited above.** `config/parameters.yaml` has since moved on to the November
2026 book: Equity Universe Minimum Size **USD 621 mn**, DM Standard reference
**USD 17,373 mn**, so the EM Standard range is **USD 4,343 – 9,989 mn**. The live
config is always the authority; refresh it with
`scripts/update_msci_parameters.py` rather than reading numbers out of this page.

### 1.3 Liquidity: the ATVR formula

The Annualised Traded Value Ratio is built in four steps, per Liquidity Data
Methodology 1.1:

```
daily traded value        = shares traded that day x closing price
median daily traded value = median of those values within the month,
                            excluding zero-volume days
monthly median traded value = median daily traded value x days traded in the month
monthly traded value ratio  = monthly median traded value / free-float market cap
                              at month end

12-month ATVR = mean(monthly traded value ratio over 12 months) x 12 x 100%
 3-month ATVR = mean(monthly traded value ratio over  3 months) x 12 x 100%
```

The **median** is the whole point: it stops one block deal or one delivery-day spike
from making an illiquid stock look tradeable. A month needs at least five trading
days to count. Suspended days are added back to the traded-day count rather than
penalising the stock.

```
Frequency of Trading = days traded / exchange trading days in the period
```

One subtlety that is easy to get wrong and changes outcomes: GIMI 2.2.5 says that
when twelve months of data do not exist, the ratio is computed from however many
months *are* available (six, three or one). A company that listed seven months ago is
screened on seven months of history, not rejected for lacking a 12-month figure.

### 1.4 The size cut-off (GIMI 2.3)

This is the single most important number in the model — every add/delete test is a
multiple of it — and it is produced by two constraints intersecting.

**Local coverage.** Sort India's investable universe by full market cap descending,
accumulate free-float market cap, and stop where cumulative free-float coverage
reaches **85%**. That company's *full* market cap is the raw Standard cut-off.
(70% gives the Large Cap boundary, 99% the IMI boundary.)

**Global size integrity.** The result is then forced inside the **Global Minimum Size
Range**, because MSCI will not let one market's mid caps be another market's small
caps. The Global Minimum Size Reference for Developed Markets is the full market cap
at 85% coverage of the *DM* investable universe; **the EM reference is exactly half of
it**; and the range runs from 0.5x to 1.15x the reference.

On the August 2026 figures MSCI published:

```
DM Standard GMSR                 USD 16,276 mn
EM Standard GMSR  (= 0.5 x DM)   USD  8,138 mn
EM Standard range (0.5x .. 1.15x) USD 4,069 mn .. USD 9,359 mn
```

So India's Standard cut-off has to land between roughly **USD 4.1 bn and USD 9.4 bn**
in full market cap whatever the local coverage calculation says. Centrum's published
estimate for the November 2026 review — a minimum free-float market cap boundary of
about INR 23,197 crore, which under GIMI 2.3.6.1 is half the cut-off — implies a
full-market-cap cut-off near **USD 5.0–5.3 bn**, comfortably inside that range and a
useful independent check on the derived value.

### 1.5 The buffers — why additions and deletions are asymmetric (GIMI 3.1.5.1)

Index turnover is expensive, so MSCI puts a wide dead zone around the cut-off:

```
                    0.67x cut-off          cut-off          1.5x cut-off
  ────────────────────────┼───────────────────┼───────────────────┼──────────────
   incumbent is deleted   │   nothing changes: membership depends │  outsider is
                          │   only on whether you were already in │  added
```

- A **non-constituent** needs full market cap ≥ **1.5x** the cut-off to be added.
- A **constituent** is only deleted once full market cap falls below **2/3x** the cut-off.

Between those two lines nothing moves. This is why a stock can sit above the cut-off
for a year without entering the index, and why deletions require a genuine decline
rather than a marginal one. It is also why knowing the *current constituent list*
matters — see §4.3.

### 1.6 Final investability requirements (GIMI 2.3.6)

Clearing the size buffer is not enough:

- **Free-float market cap ≥ 50% of the cut-off.** Existing constituents get relief and
  only need 2/3 of that.
- **A security with FIF below 0.15 needs 1.8x that**, i.e. 0.9x the cut-off in
  free-float terms. This is the rule that governs state-controlled and
  promoter-heavy companies, and it is exactly how **LIC entered in November 2026** —
  not by passing the 0.15 FIF test, which it still fails, but by being large enough
  in float terms to earn the exception.
- **Extreme Price Increase screen.** A would-be addition whose excess return over its
  country-sector peers breaches any monitoring window (100% over 5–20 days, rising to
  2500% over 250 days) is held out for one review even if it passes everything else.
  Securities with FIF ≥ 0.75 are exempt.

### 1.7 The large-IPO waiver (GIMI 2.2.7, 3.1.2.7, 3.2.7)

A company listed for under three months can still enter, but needs
**full market cap ≥ 1.8x the cut-off** *and* **free-float market cap ≥ 1.8x the
minimum**. Both legs must hold; the second is what catches anchor-heavy new listings
that are large on paper but thin on float. This is the path that matters for India's
recent IPO wave, and a universe built only from NSE index membership would miss these
names entirely because they are not in any index yet.

---

## 2. Measured results

### November 2026 review

Screened on the ten candidate price cut-off dates of 19–30 October 2026 and compared
with what MSCI announced on 11 November. With the real constituent list supplied from
the INDA holdings file (see §4.3):

```
Standard additions
  actual    : COFORGE, LICI, ADANIGREEN, MEESHO
  predicted : LICI, ADANIGREEN, HYUNDAI, MEESHO, COFORGE
  spurious  : HYUNDAI
  recall    : 100%   precision: 80%

Standard deletions
  actual    : PIIND
  predicted : PIIND, ICICILIFE
  spurious  : ICICILIFE
  recall    : 100%   precision: 50%
```

Run at a cut-off of USD 5,600 mn with `--min-float-confidence MEDIUM` and
`--min-dates 6`. Reproduce it with the command in §4.2.

Hyundai is the one false positive, and it is a close one: FIF 0.165 sits a hair above
the 0.15 threshold, which is enough to leave it needing only the ordinary free-float
test rather than the 1.8x one. It clears the size buffer at 3.12x. Getting it right
needs MSCI's own float factor, not an estimate.

All four additions came out at 10/10 confidence, and each entered through a different
part of the rulebook — which is what makes this a real test rather than a size sort:

| Name | Mcap/cut-off | FIF | Qualifying path |
|---|---|---|---|
| LIC | 9.92x | 0.112 | **FIF below 0.15**: enters on the 1.8x free-float test (GIMI 2.3.6.1) after the August 2026 government Offer For Sale lifted its float |
| Adani Green | 4.22x | 0.207 | ordinary, FIF just above the 0.15 line |
| Meesho | 2.08x | 0.359 | recent listing, large enough on float-adjusted size |
| Coforge | 1.75x | 0.785 | ordinary: full float, clears the +50% buffer |
| PI Industries | 0.64x | 0.534 | falls under the −33% delete buffer |

`tests/test_rules.py` pins each path as a regression, because they fail silently. If a
refactor breaks the low-FIF branch, LIC drops out of the prediction and nothing else
complains.

### Across five reviews

Each run uses that review's own MSCI parameters and a constituent list reconstructed
by rolling the current INDA holdings backwards through the published change lists
(§4.4). Confidence threshold 6 of 10.

| Review | Months of input look-ahead | Add recall | Add precision | Delete recall | Delete precision |
|---|---|---|---|---|---|
| Nov 2026 | ~1 | **100%** (4/4) | 80% (4/5) | 100% (1/1) | 50% (1/2) |
| Aug 2026 | ~3 | **100%** (4/4) | 67% (4/6) | **100%** (3/3) | **75% (3/4)** |
| May 2026 | ~7 | **100%** (4/4) | 57% (4/7) | 25% (1/4) | 33% (1/3) |
| Feb 2026 | ~10 | **50%** (1/2) | 20% (1/5) | 100% (1/1) | 20% (1/5) |
| Nov 2025 | ~13 | 75% (3/4) | 25% (3/12) | 0% (0/2) | 0% (0/2) |

Three things to read off this, and none is flattering by accident.

**Recall on additions is 100% on three of the five reviews, not all five.** Feb 2026
comes out at 50% (LTF clears none of the ten price cut-off dates) and Nov 2025 at 75%
(ENRIN withheld by the lock-in gate). Full recall on all four of the 2026 reviews is a
property of the *cut-off band* method, not of a single assumed cut-off, and conflating
the two overstates the result. The rules engine does find the names when the cut-off is
close
to right, which is the part straightforwardly computable from the published
methodology.

**Precision decays with how far back the review is**, from 80% to 20–25%, though not
strictly monotonically: Nov 2025 (75%, 13 months) scores higher than Feb 2026 (50%,
10 months) because the Feb miss is a data problem (LTF's stale capital structure),
while the Nov 2025 miss is the lock-in gate correctly withholding ENRIN (listed 4
months before the window, a corporate spinoff). Both trace to the same cause: the
model reads today's capital structure onto older prices. The false positives are mostly
names that genuinely qualified on *today's* float but not on the float they actually
had (Meesho and Lenskart were inside their IPO lock-ins, Adani Green's float had not
yet risen), or names the membership replay failed to seat.

Five of the nine spurious Nov 2025 names (MCX, ABCAPITAL, ADANIENSOL, ADANIGREEN,
COFORGE) were added by MSCI at later 2026 reviews — early rather than wrong.

**Deletion recall is complete on three of five, but precision never exceeds 75%.** The
model nominates between two and six names to find one real departure. `IDEA` turns up
as a spurious deletion in four of the five reviews. `NMDC` appeared in two before its
FIF history was corrected; it is no longer predicted for deletion. The deletion side
is worth reading, but it needs a human filter.

The practical implication is the useful bit: **the live use case is the best case.**
Predicting next quarter's review from last week's prices involves almost no
look-ahead, which is the regime where the model scored 100% recall at 80% precision.
Historical back-tests understate live accuracy, so treat older reviews as a check on
recall rather than a precision estimate, and weight recent reviews when calibrating.

### Deletions are systematically under-predicted

This is a genuine gap, not a data problem. The model implements the buffer tests from
GIMI 3.1.5.1 and 3.1.6.2: an incumbent leaves when full market cap falls under ⅔ of
the cut-off or free-float market cap under ⅔ of its requirement. It does **not**
implement the Segment Number of Companies management in GIMI 3.1.4.2 and Appendix X,
under which MSCI shrinks a segment's company count when free-float coverage runs above
the target range, forcing out the smallest members regardless of where they sit
relative to the buffer.

May 2026 shows the consequence plainly: only Rail Vikas Nigam is caught, and the other
three sit comfortably inside both buffers, so no rule the model implements can remove
them. MSCI added five and deleted four that review — it was managing the company count,
not applying the buffer. The other three reviews fare better, with complete deletion
recall, because their departures did fall through the buffer.

So: a predicted deletion is meaningful, but an *absence* of predicted deletions means
nothing. Read the bottom of `candidates.csv` by `ffmc_multiple_of_requirement` for the
names most exposed to a count reduction.

The separate problem is precision, which tops out at 60% and falls to 17% on
February 2026. `IDEA` is nominated in four of the five reviews and was never deleted.

### August 2026 review

```
Standard additions
  actual    : ADANIENSOL, GROWW, LAURUSLABS, LENSKART
  predicted : ADANIGREEN, ADANIENSOL, GROWW, LENSKART, LAURUSLABS, MEESHO
  spurious  : ADANIGREEN, MEESHO
  recall    : 100%   precision: 67%

Standard deletions
  actual    : ASTRAL, BALKRISIND, SBICARD
  predicted : ASTRAL, BALKRISIND, NMDC, IDEA, SBICARD
  spurious  : IDEA, NMDC
  recall    : 100%   precision: 60%
```

Recall is complete on both sides. Both spurious additions — Adani Green and Meesho —
were added by MSCI at the following review, so the model was early rather than wrong.
That is the characteristic error: the free-float estimate sits above the factor MSCI
actually applies, so a company looks eligible a review or two before it is.

An earlier version of this section reported 44% precision with Federal Bank, Nykaa and
Paytm as spurious. Those were existing index members the membership simulation failed
to seat, and supplying the rolled-back constituent list (§4.4) removed them.

### Every false positive, diagnosed

This matters more than the headline numbers, because the causes are specific and
fixable rather than diffuse:

With the real constituent list, November 2026 has one spurious addition and one
spurious deletion. Across all five reviews the same handful of names recurs:

| Name | Spurious in | Why it is wrong |
|---|---|---|
| Hyundai Motor India | Nov 2026 (add) | FIF of 0.165 sits a hair above the 0.15 threshold, so it is screened on the ordinary free-float test rather than the 1.8x one, and clears size at 3.12x. Centrum separately noted the tracking ETF does not hold it. Turns entirely on float precision. Also survives 8 of 9 cut-offs in the August 2026 band. |
| Adani Green | Feb, May, Aug 2026 (add) | A ~17pp strategic gap from TotalEnergies and similar holders. MSCI added it in November 2026, so every earlier appearance was **early rather than wrong**. |
| Adani Energy Solutions | Feb 2026 (add) | Added at the August 2026 review; early rather than wrong. |
| Meesho | Aug 2026 (add) | Inside its IPO lock-in, so reported float overstates MSCI's. Added at the following review. |
| Swiggy | Feb, May 2026 (add) | Float estimate above MSCI's factor. Not added through November 2026. |
| Angel One | Feb 2026 (add) | Same cause. Not added through November 2026. |
| SAIL | May 2026 (add) | Same cause, and notably carries a strategic gap of only 0.1pp, which is why that quantity was dropped from the confidence gate. |
| ICICI Prudential Life | Nov 2026 (delete) | Free-float estimate puts free-float market cap at 0.61x the requirement, under the 2/3 relief line. MSCI kept it because its float factor was left unchanged. **Nuvama called this one for deletion too, and was also wrong** — a fair calibration of what the free-float input is worth. |
| Vodafone Idea | Feb, May, Aug 2026 (delete) | Nominated in three reviews, deleted in none. The single largest source of deletion-side false positives and the cheapest to investigate. |
| NMDC | Feb, Aug 2026 (delete) | Stale FIF history (Dec 2015 value of 0.200 applied through 2026). NSE data confirms 39.21% from Sep 2021; a dated anchor was added, and NMDC no longer appears as a false deletion. |

Every addition-side error needs MSCI's own free-float factor, and none of these names
was a constituent at the time, so `scripts/derive_msci_fif.py` cannot price them — it
only works where a fund weight exists. That is the ceiling on precision with free data.

Two structural gaps do not show up in these numbers because they are outside what the
model screens: **early inclusion of large IPOs** (GIMI 3.2.7), which is how Jio
Platforms would be handled after its 28 October 2026 listing, and **Segment Number of
Companies management** (GIMI 3.1.4.2, Appendix X), which is why three of May 2026's
four deletions are unreachable.

### Calibrating the cut-off

`scripts/calibrate_cutoff.py` sweeps the cut-off against a known outcome. For
November 2026:

```
    cut-off  members   add recall  add prec   del recall  del prec  missed
      4,950      165         100%       57%           0%        0%  PIIND
      5,170      165         100%       57%           0%        0%  PIIND
      5,390      165         100%       80%         100%       50%  -
      5,610      165         100%       80%         100%       50%  -
      5,830      165         100%       80%         100%       33%  -
      6,050      165         100%       80%         100%       33%  -
      6,270      165          75%      100%         100%       33%  LICI
      6,490      165          50%      100%         100%       17%  COFORGE, LICI
      6,930      165          50%      100%         100%       12%  COFORGE, LICI
```

Full recall on both sides holds over **USD 5,390–6,050 mn**. Below the band PI
Industries does not breach the delete buffer; above it LIC and then Coforge stop
clearing the add buffer. Note the trade-off at the top: additions reach 100% precision
from USD 6,270 mn, but only by dropping to 75% and then 50% recall.

That brackets MSCI's actual India Standard cut-off to about a 12% band, consistent with
the USD 5.0–5.3 bn implied by Centrum's published INR 23,197 crore free-float boundary,
and well inside the EM Global Minimum Size Range. The default is set to USD 5,600 mn,
inside the plateau. **Re-calibrate each review** — the global references move.

Note what the plateau says about effort allocation: the cut-off is already pinned
tightly enough that it is no longer the binding constraint. Free float and the
membership list are.

### The three real weaknesses

**1. Free float is estimated, not classified.** The model uses float shares ÷ total
shares from the price source. MSCI builds its FIF from a holder-by-holder
classification into strategic and non-strategic (GIMI Appendix VI). The two agree for
ordinary companies and diverge precisely where index decisions are marginal:
government stakes, promoter cross-holdings, pre-IPO lock-ins. **This is the single
largest source of error** and the main thing a paid data feed buys you.

It is not confined to edge cases. The source reports Bharti Airtel's float as 99.9%
when its promoters hold roughly half the company, so `candidates.csv` should be read
with the `fif` column in view rather than taken at face value. Use
`config/free_float_history.csv` for dated changes and
`config/free_float_overrides.csv` for undated corrections; both are read before any
screen runs.

**2. Share counts and float are current snapshots, not point-in-time.** Applying
today's share count to a price two weeks ago is harmless. Applying it to a back-test
two years ago is not — it bakes in look-ahead bias wherever there has since been an
equity raise, buyback or OFS. Treat deep back-tests as indicative only.

**3. The cut-off is derived from a broader universe than MSCI's.** The 85%-coverage
calculation needs exactly MSCI's investable universe. This one screens 1,309 symbols
down to about 566 investable companies, against roughly 430 in MSCI's India IMI. A
broader universe means a larger free-float denominator, so the 85% point lands further
down the list: the derived cut-off comes out near USD 4,550 mn against a calibrated
USD 5,200–6,000 mn, about 15% low. That is why `calibrated` is the default mode — it
takes the level from the fitted value and only the variation over time from the derived
series.

Why 15% matters so much: with a +50% add buffer, a 15% cut-off error moves the
addition threshold by 15%, and marginal names sit within a few percent of it. In the
first run against November 2026 this single error was enough to put Coforge into the
simulated index two years early, which turned a correct prediction into a reported
miss.

**4. Rules the model does not implement.** In order of how much they cost:

- **Segment Number of Companies management** (GIMI 3.1.4.2, Appendix X). The reason
  deletions are systematically under-predicted; see above. The largest known gap.
- **Early inclusion of large IPOs outside a review** (GIMI 3.2.7). Why Jio Platforms
  shows up as a false positive in November 2026 after listing on 28 October.
- **Regulatory and discretionary overlays.** MSCI withdrew Adani Energy Solutions from
  the May 2026 addition list after announcing it, because the stock entered India's
  Additional Surveillance Measure; under Appendix I, MSCI does not implement IMI
  additions for securities entering ASM at a review. Nothing in the size and liquidity
  rules predicts that.
- Company-level aggregation of multiple listed share lines, which is immaterial in
  India.

---

## 3. Output: confidence, not a verdict

Because MSCI picks its price cut-off at random from ten dates, the model screens all
ten and reports how many a stock cleared:

```
  Symbol         Dates   Conf   Mcap/cut FFMC/req    FIF  Path
  LICI           10/10  100%       9.96     1.26  0.112  low_fif
  COFORGE        10/10  100%       1.76     2.71  0.785  standard
  SOMENAME        3/10   30%       1.52     1.88  0.610  standard
```

10/10 is a different proposition from 3/10, and collapsing both to "predicted
addition" discards the most useful thing the model knows. `--sweep` reports precision
and recall at every threshold so the trade-off is explicit rather than assumed.

The **near-miss table** is the other practically useful output: non-constituents whose
only failing test is size, with the percentage move needed to reach 1.5x the cut-off.
This is the same number sell-side notes quote when they say a stock "needs to trade at
X to come into contention".

---

## 4. Usage

### 4.1 Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 4.2 Run

```bash
# Predict the November 2026 review and score it against the actual announcement.
# First run populates the cache and takes 20-30 minutes; later runs take about a minute.
.venv/bin/python run_prediction.py --review 2026-11 --backtest --sweep

# Trace every screen for one name, test by test, on all ten dates.
.venv/bin/python run_prediction.py --review 2026-11 --explain LICI,MEESHO,PIIND

# Diagnose a known addition that comes out flagged as an existing member.
.venv/bin/python run_prediction.py --review 2026-11 --trace-membership COFORGE,LICI

# Import the real constituent list from an INDA holdings download (big accuracy win).
.venv/bin/python scripts/import_inda_holdings.py ~/Downloads/iShares-MSCI-India-ETF_fund.xls
.venv/bin/python run_prediction.py --review 2026-11 --backtest \
    --constituents-file config/msci_india_constituents.csv

# Refresh the MSCI size thresholds, which move every review.
.venv/bin/python scripts/update_msci_parameters.py --review Feb2027 --write

# Fit the size cut-off against a review whose outcome is known.
.venv/bin/python scripts/calibrate_cutoff.py --review 2026-11

# Predict a future review (no --backtest, since there is nothing to score against).
.venv/bin/python run_prediction.py --review 2027-02
```

Cut-off modes: `calibrated` (default — level from `--cutoff-usd-mn`, variation over
time from the data), `derive` (pure 85%-coverage calculation, biased about 15% low for
the reason in §2), `fixed` (one constant level, useful for isolating effects but wrong
for the historical replay).

Results land in `out/<review>/`: `summary.txt`, `candidates.csv` (every screened name
with each test's outcome), `cutoff_by_date.csv` (how the cut-off moved across the ten
dates), and `backtest.txt`.

### 4.3 How membership is determined, and why it is the main accuracy lever

Recall on additions is 100% on three of the five reviews tested, and 100% on four of
the five once the cut-off band is swept. Almost every remaining error is a false
positive, and historically most came from one thing: the
model not knowing who is already in the index.

This is structural, not sloppiness. The +50% add buffer means membership is
**path-dependent** — a company can sit between 1.0x and 1.5x the cut-off for years
without ever being added, so you cannot infer membership from current size. So the
model simulates the index forward: seed with everyone above the cut-off at the oldest
review the price history covers, then replay each subsequent review applying the
buffers. The constituent count is a free check on whether that is working, since
MSCI's India Index factsheet reports roughly 165 constituents.

Three inputs sharpen it, in order of payoff:

1. **The real constituent list**, via `--constituents-file`. The practical source is
   the iShares MSCI India ETF (`INDA`) holdings file, which tracks the MSCI India Index
   and is what Centrum anchors its published work to. BlackRock blocks scripted
   downloads, so fetch it by hand from the
   [fund page](https://www.ishares.com/us/products/239659/ishares-msci-india-etf) and
   convert it:

   ```bash
   .venv/bin/python scripts/import_inda_holdings.py ~/Downloads/iShares-MSCI-India-ETF_fund.xls
   .venv/bin/python run_prediction.py --review 2026-11 --backtest \
       --constituents-file config/msci_india_constituents.csv
   ```

   The importer handles both formats BlackRock serves (the SpreadsheetML workbook that
   arrives named `.xls`, and plain CSV), strips cash and futures lines, and resolves
   tickers onto NSE symbols — BlackRock writes `BAJAJ.AUTO` with a dot and falls back
   to numeric BSE scrip codes for some names, and an unresolved ticker would be read as
   a non-member and nominated for addition. Anything it cannot resolve is flagged
   rather than dropped silently.

   Two useful sanity checks on the download: it should contain **165 equity holdings**,
   matching the constituent count on MSCI's India Index factsheet, and it should be
   point-in-time. The file pulled on 15 November 2026 still held PI Industries and
   held none of Coforge, LIC, Adani Green or Meesho, which is correct for a review
   taking effect on 30 November.

   An explicit list is treated as authoritative and is not filtered by
   `known_non_constituents.csv`; conflicts are logged. The weaker source must not veto
   the stronger one, or names already in the index get nominated again.
2. **`config/known_non_constituents.csv`** — the cheaper half of the same fix. Names
   known *not* to be members, subtracted from the simulated set. The rows shipped here
   come from Centrum's pre-review notes, published before the announcements, and each
   carries an `as_of` date so a back-test cannot use a note that did not exist yet.
   Supplying this took November 2026 additions from 25% recall to 100%.
3. **Announced history**, which is automatic. Every addition recorded in
   `config/actual_reviews.yaml` from an earlier review is forced into the membership
   set. This also stops a stale non-member fact being reused: the note that correctly
   called Adani Energy Solutions, Groww, Laurus Labs and Lenskart as non-members in
   July 2026 stopped being true on 28 August, when MSCI added all four.

### 4.4 Back-testing a past review

Three things differ from a live run, and all three have scripts.

**Membership.** BlackRock serves only today's INDA holdings; there is no archive. You
do not need one, because membership changes only at reviews and MSCI publishes every
change, so today's file can be rolled backwards exactly:

```bash
# Record the published changes for past reviews (authoritative, from MSCI's own lists)
.venv/bin/python scripts/import_msci_changes.py --reviews Feb26,May26,Aug26,Nov26
# ... merge the printed YAML into config/actual_reviews.yaml, then:
.venv/bin/python scripts/derive_historical_membership.py --review 2026-05
```

`import_msci_changes.py` reads
`https://www.msci.com/eqb/gimi/stdindex/MSCI_<Mon><YY>_STPublicList.pdf`, which is
better than press coverage in two ways: it is complete, and it includes the deletions
that news articles routinely leave out. It resolves MSCI's truncated company names
onto NSE symbols and flags anything it is unsure of.

`derive_historical_membership.py` then computes

```
membership(before review R) = current holdings
                              − everything added at R and later
                              + everything deleted at R and later
```

It skips a review whose additions are absent from the file, which is how it handles a
holdings copy taken between announcement and effective date. Verified against May
2026: the output correctly excludes all four May additions and includes all four May
deletions.

**Parameters.** Each review used different MSCI size references, and they move a lot —
the May 2026 book has a DM Standard reference of USD 15,748 mn against November's
USD 17,373 mn, a 9% difference. Keep a snapshot per review rather than overwriting
the live file:

```bash
.venv/bin/python scripts/update_msci_parameters.py --review May2026 --write
cp config/parameters.yaml config/parameters_2026-05.yaml
# restore your live file afterwards, then pass the snapshot explicitly:
.venv/bin/python run_prediction.py --review 2026-05 --params config/parameters_2026-05.yaml ...
```

**Cut-off.** Calibrate against the review *before* the one you are testing, never
against a later one — that is look-ahead. For May 2026 that means `--review 2026-02`.

Full worked example:

```bash
.venv/bin/python run_prediction.py \
    --review 2026-05 \
    --params config/parameters_2026-05.yaml \
    --cutoff-usd-mn 4500 \
    --constituents-file config/msci_india_constituents_2026-05.csv \
    --backtest --min-dates 6
```

### 4.5 Improving accuracy further

1. **Enter known float changes** in `config/free_float_history.csv`. One OFS or
   promoter sale moves a name more than any amount of parameter tuning. LIC is the
   proof: without its dated float the model missed it entirely, and with it LIC came
   out at 10/10. Lock-in expiries after an IPO are the other common case.
2. **Re-calibrate the cut-off each review** with `scripts/calibrate_cutoff.py`.
3. **Update `config/parameters.yaml` every review.** The Equity Universe Minimum Size
   and the Global Minimum Size References are republished each time and drift with
   global markets.
4. **Add more reviews to `config/actual_reviews.yaml`.** Five are now recorded (Nov
   2025, Feb/May/Aug/Nov 2026). Six quarters would tell you whether the band method's
   100% addition recall holds, and would also pin the cut-off band and the band-share
   threshold, which are currently fitted on the four 2026 reviews and are the least
   well-established settings in the model.
   would also pin the cut-off band and the band-share threshold, which are currently
   fitted on those four and are the least well-established settings in the model.
5. **Use `--trace-membership SYMBOL`** when a known addition is reported as an
   existing member. It prints the simulation review by review and localises which
   input is wrong, which is how the Coforge and LIC problems were found.

### 4.6 Quarterly checklist for a live review

Worked for the February 2027 review. Substitute dates for later ones; the review
months are always February, May, August and November.

**Dates the model derives** (GIMI 3.1.9):

| | February 2027 review |
|---|---|
| Equity Universe cut-off | 30 November 2026 |
| Liquidity cut-off | 31 December 2026 (ATVR window: Oct, Nov, Dec 2026) |
| Price cut-off candidates | **18–29 January 2027** |
| MSCI announcement | around 9–10 February 2027 |

**When to run.** After the close on Friday 29 January, which is the first moment all
ten candidate dates have prices. That still leaves about a week before MSCI announces,
so it is a genuine forecast. Running earlier works and is useful as a watchlist, but
the model can only screen the dates that exist, so a run on 20 January sees three of
ten and the confidence column is not comparable.

**Before running:**

1. **Re-download the INDA holdings file.** Membership changed on 30 November 2026, so
   the current copy is stale. This is the highest-value step.
2. **Update `config/parameters.yaml`** — automated:

   ```bash
   .venv/bin/python scripts/update_msci_parameters.py --review Feb2027          # preview
   .venv/bin/python scripts/update_msci_parameters.py --review Feb2027 --write  # apply
   ```

   This downloads the GIMI book, extracts the Equity Universe Minimum Size (2.2.3)
   and the three Global Minimum Size References (2.3.2.1), and diffs them against the
   config. It is not a formality: between the August and November 2026 editions these
   moved 5–7%, which widened the EM Standard size range from USD 4,069–9,359 mn to
   USD 4,343–9,989 mn.

   Nothing else in `parameters.yaml` needs touching. The 15% EM liquidity thresholds,
   the 0.15 FIF floor, the 1.5x and 2/3 buffers, the 50% free-float fraction and the
   70/85/99 coverage targets are structural — MSCI changes those only after a public
   consultation. Check them when the book's Summary of Changes mentions them.
3. **Record the November 2026 result** — already in `config/actual_reviews.yaml`, so
   Coforge, LIC, Adani Green and Meesho are automatically treated as members and
   PI Industries as a departure.
4. **Add any float-moving corporate action** to `config/free_float_history.csv` —
   see §4.6 for where the numbers come from.
5. **Re-calibrate the cut-off** against the most recent review whose outcome you have
   recorded. For a January 2027 run that is `--review 2026-11`; you cannot calibrate
   against the review you are predicting. Re-do it every quarter, because the
   references above move.

```bash
cd ~/msci-india-rebalance

# 1. Fresh membership
.venv/bin/python scripts/import_inda_holdings.py ~/Downloads/iShares-MSCI-India-ETF_fund.xls

# 2. Re-calibrate against the last known outcome
.venv/bin/python scripts/calibrate_cutoff.py --review 2026-11

# 3. Predict, with --refresh to pull new prices, share counts and any new listings
.venv/bin/python run_prediction.py \
    --review 2027-02 \
    --refresh \
    --cutoff-usd-mn 5600 \
    --constituents-file config/msci_india_constituents.csv \
    --min-dates 6
```

`--refresh` takes 20–30 minutes because share counts and float are fetched one ticker
at a time. It is also what re-pulls the NSE symbol lists, which is the only route by
which a January 2027 IPO enters the universe at all.

**Running daily through the window.** Use `scripts/daily_watch.py`, which appends
each run to `out/<review>/daily_watch.csv` and tallies appearances across run-days:

```bash
.venv/bin/python scripts/daily_watch.py --review 2027-02 --refresh-prices --min-days 5
```

Two different confidences are involved and conflating them will mislead you:

| | What it measures |
|---|---|
| **Within-run** (`x/10`) | How many of MSCI's ten candidate price cut-off dates the stock clears. This is the randomness in MSCI's date choice. |
| **Across-run** (`Days`) | How many separate run-days the name appeared on. This is stability as prices move and the window fills. |

The within-run denominator **grows through the window**, because the later candidate
dates have not happened yet. A run on 20 January screens three dates, so nothing can
score above 3/10; only after the close on 29 January is anything out of ten. The
report prints a `PARTIAL WINDOW` warning whenever this applies, and a run before
18 January fails outright with an explanation rather than returning an empty answer.

So read the two columns together. A name on nine run-days that never exceeds 2/10
within-run is clearing the first couple of dates and failing the rest, which is a
weaker signal than a name on four run-days at 10/10.

**After MSCI announces**, add the outcome to `config/actual_reviews.yaml` and score it:

```bash
.venv/bin/python run_prediction.py --review 2027-02 --backtest --sweep \
    --constituents-file config/msci_india_constituents.csv
```

A note on holidays: 26 January 2027 is Republic Day and the NSE is shut, but MSCI
defines a business day globally, so it stays in the candidate window. The model uses
the last available close for a closed market, which is what MSCI does too.

### 4.7 Where free-float numbers come from

MSCI's Foreign Inclusion Factor is the share of a company available to international
investors. It is built from a holder-by-holder classification into strategic and
non-strategic (GIMI Appendix VI), so it cannot be read off a single field anywhere.
The workable approximation:

```
FIF ≈ 1 − (promoter + promoter group
           + government holdings
           + strategic corporate and cross-holdings
           + shares still under lock-in)
```

Promoter holding alone gives you a **ceiling** on FIF, not FIF. The gap is where the
model's errors live: a strategic non-promoter holder such as TotalEnergies in Adani
Green, or an anchor investor still inside an IPO lock-in, counts as non-free-float to
MSCI but as public holding on a shareholding-pattern filing.

**Primary sources, all free:**

| Source | What you get |
|---|---|
| [NSE shareholding patterns](https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern) | Quarterly promoter / public / institutional split per company. The authoritative filing. |
| [BSE shareholding pattern](https://www.bseindia.com/corporates/Shrholdng.aspx) | Same data, often easier to query |
| [NSE corporate announcements](https://www.nseindia.com/companies-listing/corporate-filings-announcements) | Offers for sale, promoter stake sales, QIPs, lock-in expiries as they happen |
| NSE index factsheets | NSE's own Investable Weight Factor, roughly 1 − promoter holding. A good cross-check, but it does **not** strip non-promoter strategic holders, so it reads higher than MSCI's FIF |
| Screener.in, Trendlyne | Convenient quarter-by-quarter promoter holding history for building a dated series |

**What to watch for each quarter**, in order of how much they move an index decision:

1. **Government offers for sale** in PSUs and state-controlled insurers. LIC is the
   worked example: the August 2026 OFS lifted its float from roughly 5% to low double
   digits, and that single input decided its inclusion. Several PSUs still have minimum
   public shareholding deadlines, so more of these are coming.
2. **IPO lock-in expiries**, six months after listing for anchor investors. A company
   that looked thin on float at one review can clear the bar at the next without its
   price moving at all.
3. **Promoter stake sales and block deals**, which show up in corporate announcements
   before the quarterly filing catches up.
4. **Strategic stake changes** — one corporate selling out of another converts
   non-free-float into float.

Add each as a dated row so the historical replay stays honest:

```csv
symbol,effective_date,fif,note
LICI,2022-05-17,0.035,IPO - government holds ~96.5%
LICI,2026-08-15,0.112,August 2026 government Offer For Sale
```

Use `--trace-membership SYMBOL` to check the effect: it prints the float applied at
each past review, so you can see whether a single current estimate is being wrongly
projected backwards.

### 4.8 Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

38 tests covering the ATVR and Frequency of Trading formulas, cut-off derivation and
clamping, the add/delete buffers, the low-FIF and large-IPO paths, the EPI screen, the
free-float reliability gate, and the review-date arithmetic including MSCI's own worked
preponement example.

---

## 5. Layout

```
config/parameters.yaml              every MSCI threshold, with section references
config/actual_reviews.yaml          ground truth for back-testing
config/free_float_history.csv       dated float changes - the highest-value input
config/free_float_overrides.csv     undated float corrections
config/known_non_constituents.csv   names known not to be members, with as_of dates
config/msci_india_constituents.csv  real membership, built by the INDA importer
config/strategic_gap.csv            SEBI public holding vs the float estimate, in pp
config/msci_implied_fif.csv         MSCI's own float factors, backed out of INDA weights
msci_predict/rules.py               the maths: pure functions, no I/O, fully tested
msci_predict/calendar_rules.py      the three cut-off dates per GIMI 3.1.9
msci_predict/data.py                NSE universe, prices, share counts, FX, caching
msci_predict/pipeline.py            orchestration, ten-date screen, membership replay
msci_predict/backtest.py            scoring against actual announcements
msci_predict/report.py              console and CSV output
run_prediction.py                   CLI
scripts/daily_watch.py              run daily through the window, track persistence
scripts/import_msci_changes.py      pull official add/delete lists from MSCI
scripts/derive_historical_membership.py  roll INDA holdings back to a past review
scripts/update_msci_parameters.py   pull current MSCI size thresholds from the GIMI book
scripts/import_inda_holdings.py     convert an INDA holdings download to a symbol list
scripts/calibrate_cutoff.py         fit the size cut-off to a known outcome
scripts/fetch_shareholding_history.py    dated float series from NSE quarterly filings
scripts/derive_msci_fif.py          back MSCI's float factors out of the INDA weights
scripts/smoke_test.py               15-symbol end-to-end check, runs in seconds
research/                           the source PDFs and extracted text
```

---

## 6. Sources

Primary (public domain, cited inline in the code):

- [MSCI GIMI Methodology, November 2026](https://www.msci.com/eqb/methodology/meth_docs/MSCI_GIMIMethodology_Nov2026.pdf)
- [MSCI Liquidity Data Methodology, May 2025](https://www.msci.com/documents/10199/995265d3-adce-42ce-dad7-a1696b3f800a)
- [MSCI Index Calculation Methodology](https://www.msci.com/eqb/methodology/meth_docs/MSCI_Index_Calculation_Methodology_20240301.pdf)

November 2026 review outcome and sell-side context:

- [Business Standard — four companies enter the MSCI Standard Index](https://www.business-standard.com/markets/news/four-companies-enter-msci-standard-index-2-bn-passive-inflows-expected-126111201772_1.html)
- [Moneycontrol — the November 2026 rejig](https://www.moneycontrol.com/news/business/msci-november-rejig-lic-adani-green-coforge-and-meesho-added-pi-industries-excluded-14077462.html)
- [Moneycontrol — Centrum's pre-review watchlist, including the buffer mechanics and its cut-off estimate](https://www.moneycontrol.com/news/business/markets/lic-meesho-coforge-swiggy-top-centrum-s-high-conviction-list-for-msci-india-november-review-14036314.html)
- [Economic Times — Nuvama's flow estimates](https://economictimes.com/markets/stocks/news/msci-rejig-lic-meesho-coforge-set-to-gain-from-index-inclusion-how-much-inflows-could-they-attract/articleshow/135753338.cms)

Market data:

| Need | Source | Notes |
|---|---|---|
| Daily close, volume | [NSE bhavcopy](https://nsearchives.nseindia.com/products/content/) `sec_bhavdata_full_DDMMYYYY.csv` | Default. Exchange end-of-day file, one request per trading day, ~3,000 securities per file |
| Share count, float shares, sector | Yahoo Finance via `yfinance` | The only free source for these; a current snapshot only |
| Quarterly promoter/public holding | [NSE shareholding patterns](https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern) API | Feeds the dated float series |
| Index membership | iShares INDA holdings, downloaded manually | Proxy for MSCI India Index membership |
| Add/delete history | [MSCI Standard Index change lists](https://www.msci.com/eqb/gimi/stdindex/) | Authoritative |
| USD/INR | `yfinance` (`INR=X`), falling back to [Frankfurter](https://api.frankfurter.app) | Frankfurter is ECB reference, ~1% off spot |

### Why bhavcopy rather than Yahoo for prices

Yahoo was the original source and remains available via `--price-source yfinance`, but
the exchange file is better on every axis that matters here:

- **No rate limiting.** Yahoo answers heavy use with HTTP 429 and an *empty frame*.
  The original code logged a warning and carried on with whatever returned, so the
  investable universe silently shrank, the derived size cut-off moved with it, and
  identical commands produced different predictions. That is the worst failure mode
  this tool has: a plausible answer computed from a fraction of the market.
- **A symbol cannot go missing.** One request covers every security for a day, so
  coverage is all-or-nothing per day rather than per ticker.
- **Wider universe.** About 3,000 securities against 1,300 resolvable Yahoo tickers.
- **Authoritative, with real turnover** alongside close and volume.

It does not supply a split-adjusted series, which the model needs to carry a current
share count backwards without a bonus issue making a company look twice its size. That
is recovered from the file itself: NSE restates `PREV_CLOSE` for corporate actions, so
`PREV_CLOSE(t) / CLOSE(t-1)` is 1.0 on an ordinary day and equals the capital-change
factor across a split or bonus. Accumulating those factors rebuilds the adjusted
series with no second source. Only discontinuities above 12% are treated as capital
changes, so dividends are ignored on purpose: a dividend moves the price without
changing the share count, and absorbing it would distort the market caps the series
exists to compute.

Two safeguards were added alongside, both learned the hard way:

- The panel cache is **merged, never replaced**. A back-test fetching data that ends
  in April drops every symbol listed later, and writing that over the full panel
  shrank the cache on every run until the universe was a fifth of the market.
- A fetch returning less than `--min-price-coverage` (default 70%) of the requested
  symbols **aborts** and leaves the cache untouched, rather than producing an answer.

*Content from the news sources above was rephrased for compliance with licensing
restrictions.*

---

## 7. Caveat

This is a research tool for understanding a published index methodology. It is not
investment advice. Index-inclusion trades are crowded, the flow is often priced in
before the announcement, and a prediction that is right about the stock can still lose
money on the entry. The model's own free-float estimates are its weakest link, and
that weakness concentrates in exactly the marginal names where a trade would be most
tempting.
