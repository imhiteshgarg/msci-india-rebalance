# Runbook — February 2027 Index Review

Dates the model derives from GIMI 3.1.9 for this review:

| | Date |
|---|---|
| Equity Universe cut-off | 30 November 2026 |
| Liquidity cut-off | 31 December 2026 (ATVR from Oct, Nov, Dec) |
| **Price cut-off candidates** | **Mon 18 – Fri 29 January 2027** |
| MSCI announcement | around 9–10 February 2027 |
| Effective | close of 26 February 2027 |

Everything runs from the repo root:

```bash
cd ~/msci-india-rebalance
```

---

## Phase 1 — Setup. Any day from 11 to 17 January. About 15 minutes.

Do this before the price window opens, because step 1 needs a manual download and
step 2 depends on MSCI having published the February methodology book.

**1. Download the INDA holdings file by hand.** BlackRock blocks scripted access.
Go to the [iShares MSCI India ETF page](https://www.ishares.com/us/products/239659/ishares-msci-india-etf),
use "Download Holdings", then:

```bash
.venv/bin/python scripts/import_inda_holdings.py ~/Downloads/iShares-MSCI-India-ETF_fund.xls
```

Check three things in its output: the holdings date is recent, it resolves **165
equity holdings** (matching MSCI's published constituent count), and every ticker
resolves to an NSE symbol. It should now contain Coforge, LIC, Adani Green and Meesho,
and no longer contain PI Industries.

**2. Refresh the MSCI size thresholds.** These move 5–18% between editions, so this is
not optional.

```bash
.venv/bin/python scripts/update_msci_parameters.py --review Feb2027 --write
```

If MSCI has changed its URL pattern, download the methodology book from
[msci.com/index-methodology](https://www.msci.com/index-methodology) and pass `--pdf`.

**3. Refresh the free-float series** from NSE quarterly shareholding patterns. The
December quarter filings land through January, so run this late in the window.

```bash
.venv/bin/python scripts/fetch_shareholding_history.py --from-universe --limit 500 --merge
```

**4. Pull MSCI's own float factors for current constituents**, backed out of the INDA
weights. This is MSCI's actual number rather than an estimate, and it mainly improves
the deletion side.

```bash
.venv/bin/python scripts/derive_msci_fif.py --write
```

**5. Record the November 2026 result** — already in `config/actual_reviews.yaml`, so
Coforge, LIC, Adani Green and Meesho are treated as members and PI Industries as a
departure. Nothing to do unless MSCI amended the review after the fact.

---

## Phase 2 — Daily watch. Every weekday from Monday 18 to Friday 29 January.

```bash
.venv/bin/python scripts/daily_watch.py \
    --review 2027-02 \
    --refresh-prices \
    --min-days 5
```

Same command each day. It appends to `out/2027-02/daily_watch.csv`, refuses to
double-count a day, and reprints the running tally.

**Do not start before 18 January.** The first candidate price cut-off date is 18
January; an earlier run has nothing to screen and will stop with an explanation.

Read two columns together, not just one:

- **Days** — how many separate run-days the name appeared on. Persistence.
- **Latest** — how many of the candidate cut-off dates it cleared on the most recent
  run. This denominator **grows through the window**: a run on 20 January screens
  three dates, so nothing can exceed 3/10 until late in the month.

A name on nine run-days that never exceeds 2/10 is clearing only the early dates. That
is weaker than a name on four run-days at 10/10.

---

## Phase 3 — The authoritative run. After the close on Friday 29 January.

This is the only point at which all ten candidate dates exist, and MSCI does not
announce until about 9 February, so it is a genuine forecast with roughly a week to
spare.

**Run both commands.** The first gives the headline prediction, the second tells you
which names survive the cut-off uncertainty.

```bash
# 1. Headline prediction
.venv/bin/python run_prediction.py \
    --review 2027-02 \
    --refresh \
    --constituents-file config/msci_india_constituents.csv \
    --min-float-confidence MEDIUM \
    --min-dates 6

# 2. Robustness across the cut-off band
.venv/bin/python scripts/calibrate_cutoff.py \
    --review 2027-02 \
    --constituents-file config/msci_india_constituents.csv \
    --min-float-confidence MEDIUM \
    --auto-band --robustness
```

`--refresh` takes 20–30 minutes, almost all of it fetching share counts one ticker at a
time. It also re-pulls the NSE symbol lists, which is the only route by which a January
2027 IPO enters the universe at all. Prices come from NSE bhavcopy and take about two
minutes.

### Sanity checks before trusting the output

| Line in the log | Expect |
|---|---|
| `Price panel from bhavcopy` | ~950+ dates, 3,000+ symbols |
| `Investable universe` | roughly 550–600 companies |
| `Price cut-off candidates` | `10 of 10 screened`, no PARTIAL WINDOW warning |
| `Constituent set` | ~165 names, "explicit list" |

If the run aborts on the coverage floor, that is the guard working. Re-run rather than
lowering `--min-price-coverage`.

### Which names to act on

**Take the intersection of the two runs.** Act on names that clear
**6 of 10 price cut-off dates** *and* **at least 40% of the cut-off band**.

Those are two independent uncertainties. The dates cover MSCI's random choice of price
cut-off; the band covers the fact that the size cut-off cannot be pinned down. Across
four back-tested 2026 reviews that combination held 100% recall every time.

Also read the two secondary tables:

- **FLAGGED** — passed every MSCI rule but the free-float input is untrustworthy,
  usually a recent listing still inside its IPO lock-in. Watchlist, not signal.
- **NEAR MISSES** — failed only on size, with the percentage move needed. These flip on
  a few days of price action before the cut-off.

### What to expect

Precision tracks how fresh the inputs are, and it tracks it tightly:

Additions, at a 40% band share:

| Back-tested review | Months from cut-off to float snapshot | Precision | Recall |
|---|---|---|---|
| Feb 2026 | ~10 | 20% | 100% |
| May 2026 | ~7 | 50% | 100% |
| Aug 2026 | ~3 | 50% | 100% |
| Nov 2026 | ~1 | 80% | 100% |

A live run in late January sits at the **~1 month** end, so expect the favourable end
of that range. The older back-tests measure how much the inputs rot, not how the model
performs live.

Recall is 100% on all four only via the band. At a single assumed cut-off February
2026 drops to 50%, because L&T Finance clears none of the ten price cut-off dates
there. That is the reason step 2 of this phase is not optional.

**Deletions: good recall, poor precision.** Measured recall is 100% on Feb, Aug and
Nov 2026 and 25% on May, but precision runs 17% / 33% / 60% / 50% — so expect to sift
two to six names to find one real departure, and treat `IDEA` and `NMDC` with
suspicion: they are nominated in three and two reviews respectively, and were
deleted in none.

The Segment Number of Companies mechanism (GIMI
3.1.4.2, Appendix X) is not implemented, so deletions driven by company-count
management rather than the buffer are invisible. A predicted deletion means something;
an absence of predicted deletions means nothing.

---

## Phase 4 — Score it. After MSCI announces, around 10 February.

```bash
# Pull MSCI's own change list and merge the printed YAML into config/actual_reviews.yaml
.venv/bin/python scripts/import_msci_changes.py --reviews Feb27

# Then score
.venv/bin/python run_prediction.py --review 2027-02 --backtest --sweep \
    --constituents-file config/msci_india_constituents.csv \
    --min-float-confidence MEDIUM --min-dates 6
```

Recording the outcome is not bookkeeping. It feeds three things: the next review's
cut-off calibration, the `known_members` set so those names are not nominated again,
and a fifth data point for the band and threshold settings, which are currently fitted
on four reviews and are the least well-pinned part of the model.

---

## Quick reference

| When | Command |
|---|---|
| 11–17 Jan | `import_inda_holdings.py`, `update_msci_parameters.py --review Feb2027 --write`, `fetch_shareholding_history.py`, `derive_msci_fif.py --write` |
| Daily, 18–29 Jan | `daily_watch.py --review 2027-02 --refresh-prices --min-days 5` |
| After close 29 Jan | `run_prediction.py --review 2027-02 --refresh ...` and `calibrate_cutoff.py --review 2027-02 --auto-band --robustness` |
| After ~10 Feb | `import_msci_changes.py --reviews Feb27`, then `run_prediction.py ... --backtest` |

Run anything twice and confirm the output is identical before acting on it. That check
is what caught the two worst bugs in this repo.
