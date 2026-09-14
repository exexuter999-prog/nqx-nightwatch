# Research Basis and Transfer Limits

## Contents

1. Verified primary sources
2. Falsification ledger discipline
3. Policy status

## 1. Verified primary sources

### MNQ OHLCV falsification

Mathias Mesfin, *Structural Limits of OHLCV-Based Intraday Signals in MNQ Futures: A
Systematic Falsification Study*, arXiv:2605.04004v2, revised 2026-07-13.

- Tests 14 signal families on 947 MNQ trading days from 2021-2025.
- Uses OOS walk-forward validation, `T >= 2.0`, `N >= 30`, fixed two-point
  round-trip friction, and multi-year consistency.
- Reports that none of the tested signal families passes every criterion; the
  reported gross edge range is about 0.07-1.50 points per trade.
- Provides two separate positive controls, showing the framework can detect a
  strong result.

Primary source: https://arxiv.org/abs/2605.04004

Operational implication: do not treat a common single-bar OHLCV trigger as
deployable merely because it looks plausible. Regime context, multi-bar outcomes,
costs, and falsification are mandatory.

### Dealer gamma and intraday behavior

Chukwuma Dim, Bjorn Eraker, and Grigory Vilkov, *0DTEs: Trading, Gamma Risk and
Volatility Propagation*, SSRN 4692190, revised 2025-06-06.

- Reports average positive market-maker inventory gamma and a negative relation
  with future intraday volatility.
- Reports evidence consistent with positive gamma strengthening reversal and
  negative gamma strengthening momentum.

Primary source: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4692190

Diego Amaya, Pedro A. Garcia-Ares, Neil D. Pearson, and Aurelio Vasquez, *0DTE
Index Options and Market Volatility: How Large is Their Impact?*, 2025-01-25.

- Uses proprietary SPX/SPXW option trades to estimate aggregate option-market-maker
  gamma.
- Finds the typical estimated gamma impact reduces volatility; the largest
  estimated 30-minute annualized-volatility increase is 6.4 percentage points, an
  outlier with a 99th percentile of 2.0 points.

Primary source:
https://cdn.cboe.com/resources/education/research_publications/gammasqueezes.pdf

Transfer limit: these are SPX/S&P 500 findings, not direct MNQ validation. Use them
only as labeled cross-market context. They do not justify inferring gamma sign
from NQ candles, VIX, or a strike map.

### Order-flow imbalance

Rama Cont, Arseniy Kukanov, and Sasha Stoikov, *The Price Impact of Order Book
Events*, Journal of Financial Econometrics 12(1), 2014.

- Finds a short-horizon linear relation between price changes and best-quote
  order-flow imbalance, with slope inversely related to market depth, in NYSE TAQ
  equity data.

Primary source: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1712822

Transfer limit: this does not validate an MNQ OFI trading setup. Add OFI to NQX
only after MNQ tick data, preregistration, walk-forward testing, costs, `T >= 2.0`,
`N >= 30`, and year stability.

## 2. Falsification ledger discipline

The evidence-based strategy upgrade blueprint
(`indicator/NQX_EVIDENCE_BASED_STRATEGY_UPGRADE_BLUEPRINT.md`) sets a standing
constitution for what can be called a "good" strategy: (1) an E1/E2-graded source
reporting PF with rules, period, market, and cost handling stated; (2) independent
reproduction with real MNQ costs, PF ≥ 1.5, N ≥ 200, OOS PF ≥ 1.3; (3) a 95%
bootstrap PF confidence interval whose lower bound exceeds 1.2, surviving a
multiple-testing discount. Do not cite standalone ORB, gap fill, volume spike,
expansion-bar chase, or post-event sixth-bar drift as `Evidence For` on their own —
this project's falsification ledger keeps single-signal forms of those rejected
unless a materially new preregistered hypothesis passes out-of-sample gates. A
rejected hypothesis is a permanent, valuable record, not something to quietly retry
with cosmetic parameter changes.

## 3. Policy status

`RG-H1` thresholds (see [regime-gamma-policy.md](regime-gamma-policy.md)) are
explicit operating heuristics chosen for reproducible labeling. They are not
reported estimates from the papers above and must not be described as
scientifically optimized. Any future GMM/HMM or OFI module remains separate until
validated on the project's own data.

This material supports research and educational decision processes, not
investment advice.
