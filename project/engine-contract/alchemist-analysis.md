# Alchemist Analysis Module — 分析規則の正本

Loaded by `mnq-nightwatch-nqx` at steps 2 and 5. This file owns **analysis only**.
Transport grammar lives in `nqx1-spec.md`; do not duplicate it here.

Write all explanations in clear Japanese. Keep parser-facing key names and controlled
enum tokens in English. Produce conditional decision support, not predictions. Prefer
`NO TRADE` over a weak setup. Never manufacture a scenario merely to fill a template.

## GOD VIEW protocol

Treat `GOD VIEW` as maximum situational awareness, never omniscience. Observe the market simultaneously through multiple independent lenses, expose contradictions, and state what would prove the analysis wrong.

### Truth hierarchy

Resolve conflicts using this order:

1. Timestamped price and OHLC
2. Executed volume and auction behavior
3. External and internal market structure
4. Volume Profile and session references
5. Liquidity and SNR-zone behavior
6. Derived indicators and oscillators
7. News narratives and social sentiment

Never allow a lower-ranked signal to overrule contradictory higher-ranked evidence without an explicit explanation.

### Seven simultaneous lenses

Analyze every chart through:

1. `STRUCTURE`: external trend, internal shift, impulse, retracement
2. `AUCTION`: value, acceptance, rejection, balance, price discovery
3. `LIQUIDITY`: resting stops, sweeps, trapped breakout participants
4. `POSITIONING`: who is trapped, who is profitable, who may be forced to act next
5. `TIME`: session phase, opening behavior, scenario decay, event proximity
6. `INTERMARKET`: yields, DXY, VIX, ES, SOX, mega-cap leadership, and oil when verified
7. `EXECUTION`: location, trigger quality, slippage risk, R:R, account constraints

If an intermarket input is unavailable, mark it `UNVERIFIED`; do not infer it from NQ price alone.

### Participant map

Infer participant pressure cautiously from observable behavior:

- Trapped longs above a failed breakout
- Trapped shorts below a reclaimed breakdown
- Responsive buyers/sellers at value extremes
- Initiative participants driving price discovery
- Late chasers vulnerable to a return into value
- Forced participants near stops, margin pressure, or event repricing

Split every participant statement into two explicit classes and never blur them:

- `OBSERVED`: directly visible on the supplied chart (a sweep printed, a body close
  beyond a level, a failed breakout that is actually on the tape).
- `INFERRED`: a conclusion about who is trapped, profitable, or forced. This is a
  reading of behaviour, not a fact.

If order-flow data (delta, CVD, absorption, DOM) was not supplied, it does not exist
for this analysis. Never fabricate it and never present an `INFERRED` pressure claim
as if it were order-flow evidence.

### Three-case tribunal

Before selecting scenarios, independently build:

- `Bull Case`: strongest evidence, required trigger, and failure point
- `Bear Case`: strongest evidence, required trigger, and failure point
- `Balance Case`: conditions for chop, absorption, or no-trade

Red-team each case with the best opposing evidence. Select a dominant hypothesis only after this adversarial pass. If no case dominates, remain neutral.

### Evidence ledger

For every major conclusion, record:

- Evidence For
- Evidence Against
- Missing Evidence
- Next Observable Confirmation

Avoid double-counting correlated signals. Structure, candle pattern, and a price-derived oscillator may all reflect the same underlying price move and must not receive three independent confidence boosts.

### Causal chain

Express the expected path as a sequence, not a destination:

`Liquidity target → sweep or acceptance → displacement → retest → execution → management → opposing liquidity`

Label each link `OBSERVED`, `INFERRED`, or `ASSUMED`. If any link is missing, keep the scenario at `WATCH` or reject it. If any required link is `ASSUMED`, cap confidence at 64 and do not promote the scenario to `PRIMARY` until observable confirmation replaces the assumption.

### Bayesian update discipline

Update confidence only when new observable evidence arrives. Do not increase confidence because time passed or because the narrative sounds persuasive.

- Sweep without displacement: no upgrade
- Displacement without retest: remain `TRIGGERED`
- Retest hold/rejection: upgrade to `CONFIRMED`
- Close through invalidation: immediately downgrade to zero and cancel
- Material event surprise: discard pre-event confidence and rebuild

Do not change confidence by more than 10 points without a new candle close, sweep, displacement, retest, profile migration, or verified event result.

## Quantitative protocol

### Quant mandate

Add quantitative rigor without manufacturing precision. Separate chart interpretation from empirical inference, account for transaction costs and model uncertainty, and keep `FLAT` as a valid decision.

When OHLCV history, event data, order-flow data, or trade records are supplied, use the quantitative rules below. If required reference data is unavailable, remain `HEURISTIC` and do not fabricate statistics.

Only output a calibrated probability when the exact frozen setup has adequate out-of-sample observations under a comparable regime. Otherwise write `Probability: N/A — uncalibrated`.

### Mode gate

Assign exactly one mode before using statistics:

- `HEURISTIC`: screenshots, isolated observations, or no historical sample
- `EMPIRICAL`: historical data exists, but the setup is exploratory or not validated out of sample
- `CALIBRATED`: definitions are frozen and performance is validated on unseen chronological data in comparable regimes

In `HEURISTIC` mode:

- Allow setup-quality confidence only.
- Set probability, confidence interval, expected value, Sharpe, Kelly, and risk of ruin to `N/A`.
- Do not translate `Confidence: 72/100` into a 72% win probability.

In `EMPIRICAL` mode, label all estimates exploratory. In `CALIBRATED` mode, report the test period, sample size, uncertainty interval, and cost assumptions.

### Data contract

Require definitions before calculation:

- Instrument, contract month, roll convention, tick size, and point value
- Timestamped OHLCV with timezone and missing-bar policy
- RTH/ETH and Asia/London/New York session flags
- Bid/ask, spread, slippage, commissions, and fees when available
- Frozen setup label, trigger, entry, stop, targets, and expiry
- Economic-event timestamps and actual/consensus/previous values
- Profile levels computed without future bars
- Intermarket series aligned without forward-filling across unavailable periods
- Order-flow fields such as delta or CVD only when actually supplied

Reject calibration when contract rolls, timezone conversions, missing data, or event timestamps are unresolved.

### Sample sufficiency

Treat occurrence counts as guidelines, not guarantees:

- Fewer than 30 completed occurrences: insufficient for probability claims
- 30–99: exploratory only
- 100–299: provisional, with wide uncertainty and regime sensitivity
- 300 or more: potentially useful, still requiring out-of-sample stability

Count independent setup occurrences, not bars. Clustered signals from the same market episode are not independent observations.

### Feature set

Calculate only features available at the decision timestamp.

#### Returns and volatility

- Log return: `r_t = ln(P_t / P_(t-1))`
- Realized volatility: derive from squared intraday returns and state the annualization convention
- Normalized ATR: `ATR_n / Price`
- Gap and event move: measure relative to pre-event volatility

#### Trend and balance

- Trend efficiency: `abs(P_t - P_(t-n)) / sum(abs(ΔP))`
- Range position: `(Price - Range Low) / (Range High - Range Low)`
- VWAP deviation z-score using a trailing, non-forward-looking window
- Value migration: direction and magnitude of POC/VAH/VAL change
- Rotation factor and time spent inside versus outside value

#### Participation and liquidity

- Relative volume z-score by matching time-of-day bucket
- Spread and slippage relative to rolling median
- Sweep distance normalized by ATR
- Displacement size normalized by ATR and recent bar distribution
- Retest depth and time-to-retest
- Delta, CVD, imbalance, or DOM features only when sourced directly

Do not treat several transformations of the same price series as independent evidence.

### Regime engine

Classify one regime using rolling distributions rather than permanent universal thresholds:

- `TREND`: directional efficiency and value migration are elevated
- `BALANCE`: rotation and time-in-value dominate
- `TRANSITION`: structure and value migration disagree or change sign
- `EXPANSION`: realized volatility and range expansion are elevated
- `EVENT COMPRESSION`: volatility is suppressed immediately before scheduled information
- `EVENT REPRICING`: post-release price discovery invalidates pre-event distributions

Report the features causing the classification. If metrics conflict, output `REGIME: MIXED` and widen uncertainty.

### Base rates and Bayesian updates

Estimate the prior from the same frozen setup, session, regime, direction, and event class. Avoid a global win rate when the current conditional state differs.

When likelihood estimates are validated, update odds:

`Posterior Odds = Prior Odds × Likelihood Ratio`

Never invent a likelihood ratio. If conditional samples are sparse, use a hierarchical or shrinkage estimate and disclose the prior. If this cannot be done, remain in `EMPIRICAL` mode.

Upgrade a posterior only after new evidence unavailable at the prior decision point. Preserve the full timestamped update trail.

### Probability calibration

Report a probability only when it predicts one frozen event, such as `TP1 before SL within validity window`.

Include:

- Event definition
- Out-of-sample sample size
- Base rate
- Model probability
- Confidence or credible interval
- Brier score and reliability assessment when enough predictions exist

Prefer probability ranges over a single point estimate. If the interval crosses the break-even probability materially, default to `FLAT` unless another utility term justifies action.

### Net expected value

Use realized or conservatively estimated costs.

`EV_net = p_win × AvgWin_R - (1 - p_win) × AvgLoss_R - Cost_R`

For a single binary target and stop:

`p_break_even = (Loss_R + Cost_R) / (Win_R + Loss_R)`

Report:

- Gross EV
- Fees, spread, slippage, and missed-fill assumptions
- Net EV
- Conservative EV under adverse costs
- Uncertainty interval

If probability is uncalibrated, set EV to `N/A`; R:R alone is not expectancy.

### Decision rule

Compare `LONG`, `SHORT`, and `FLAT` under the same constraints.

Promote a scenario to `PRIMARY` only when:

- Its trigger is confirmed.
- Net EV remains positive under conservative costs.
- The uncertainty range does not make the conclusion fragile.
- The result survives nearby parameter choices and regime partitions.
- It respects account and event constraints.

In non-calibrated modes, rank by evidence quality and execution clarity, not fake EV.

### Position sizing

When account inputs are supplied, calculate:

`Dollar Risk per Contract = Stop Points × Point Value + Expected Costs`

`Contracts = floor(Allowed Dollar Risk / Dollar Risk per Contract)`

Return zero contracts when the minimum contract exceeds allowed risk. Include daily loss limit, trailing drawdown, correlated exposure, and scheduled-event gap risk.

Do not use Kelly sizing without a calibrated probability and payoff distribution. When explicitly requested and statistically defensible, show fractional Kelly with a severe uncertainty haircut and compare it with the user's stricter fixed-risk cap.

### Backtest integrity

Freeze the complete setup definition before testing. Prevent:

- Look-ahead bias
- Future-derived profile levels
- Survivorship and selection bias
- Timezone leakage
- Contract-roll distortion
- Duplicate/overlapping trades counted as independent
- Using revised macro data instead of the release-time vintage
- Optimizing on the test period

Use chronological train/validation/test splits. For overlapping labels or horizons, use purging and an embargo. Prefer walk-forward evaluation to random cross-validation.

### Multiple testing and overfitting

Track every tested variant, not just the winner. Penalize repeated searching across entries, stops, targets, timeframes, and filters.

Require:

- Parameter-sensitivity maps
- Performance across adjacent parameter values
- Regime and session stability
- Bootstrap or block-bootstrap uncertainty
- Out-of-sample degradation report
- A simpler benchmark comparison

Reject a model whose edge exists only at one exact parameter or disappears after realistic costs.

### Stress testing

Recalculate under:

- 1×, 2×, and 3× normal slippage
- Delayed entry and missed best fill
- Volatility expansion
- Event gaps through the stop
- Consecutive-loss clusters
- Reduced liquidity
- Correlated losses across NQ, ES, SOX, and mega-cap exposure
- Removal of the best five trades

Report the earliest condition that turns net EV non-positive.

### Outcome logging

Store one immutable record per decision:

- Decision timestamp and data vintage
- Quant Mode and regime
- Observable features
- Scenario state and confidence
- Entry, stop, targets, expiry, and costs
- Whether the trigger, confirmation, and fill occurred
- Outcome in R
- Maximum favorable and adverse excursion
- Reason for cancellation or stand-down

Evaluate expectancy, profit factor, drawdown, tail loss, calibration, and stability. Do not optimize for hit rate alone.

### Required quantitative output

Add these fields to the main packet when data supports them:

```text
Quant Mode: HEURISTIC/EMPIRICAL/CALIBRATED
Data Sufficiency: INSUFFICIENT/EXPLORATORY/PROVISIONAL/ADEQUATE
Regime Metrics: <features available at decision time>
Sample Size: <independent in-sample and out-of-sample occurrences>
Probability Event: <frozen outcome definition>
Base Rate: <n>% or N/A
Posterior Probability: <range or N/A>
Uncertainty Interval: <interval and method or N/A>
Break-even Probability: <n>% or N/A
Gross EV: <n>R or N/A
Cost Model: <fees + spread + slippage assumptions>
Net EV: <n>R or N/A
Conservative EV: <n>R or N/A
Sizing: <contracts and dollar risk or N/A>
Robustness: PASS/FRAGILE/FAIL/NOT TESTED
Model Risk: <largest statistical or data weakness>
```

### Quant audit

Before showing numbers, verify:

- The setup and outcome were frozen before testing.
- Every feature existed at the decision timestamp.
- Samples represent comparable regimes.
- Costs and failed fills are included.
- Probability includes uncertainty.
- EV is not inferred from R:R alone.
- Confidence score is not presented as probability.
- Position size cannot violate the user's hard loss limits.
- Results survive reasonable parameter and cost stress.
- `FLAT` was evaluated as an action.

If any required condition fails, downgrade the Quant Mode and replace unsupported statistics with `N/A`.

## Non-negotiable rules

- Read visible numbers back before analyzing them.
- Never infer an unreadable price. Mark it `判読不能` or `約`.
- Treat screenshots from different timeframes as separate snapshots unless timestamps match.
- If snapshot prices differ, report the full observed range and do not pretend they are synchronous.
- Determine candle direction from OHLC or chart behavior, not candle color; users may use inverted colors.
- Do not call a lower pane `volume` unless the indicator is identified as volume. Use `oscillator` when unknown.
- Separate confirmed facts, consensus estimates, chart inference, and uncertainty.
- Never present a confidence score as historical win probability.
- Never issue an unconditional market order from a screenshot.
- Do not average down, widen a stop, or move invalidation beyond the stop.
- If inputs are insufficient, output `NO TRADE` and state what is missing.

## Required inputs and data quality

Prefer three synchronized screenshots:

- 45M: external structure and HTF storyline
- 15M: decision zone, supply/demand, and intermediate confirmation
- 3M: liquidity sweep, displacement, trigger, and execution

Useful optional inputs:

- Current price and capture time with timezone
- RTH Volume Profile and composite profile
- Previous day/week high, low, midpoint, and open
- Session highs/lows for Asia, London, and New York
- Economic calendar and current event context
- Account drawdown limit and maximum risk per trade

Assign one data-quality grade:

- `HIGH`: synchronized charts, precise axes, current price and timezone supplied
- `MEDIUM`: readable screenshots but some levels or timestamps are approximate
- `LOW`: cropped, blurred, conflicting, stale, or missing key timeframes

When quality is `MEDIUM` or `LOW`, prefix every axis-estimated level with `約` or `≈`.

## Time-sensitive research protocol

When web access is available, verify same-day market events before finalizing scenarios.

Prioritize:

1. BLS, BEA, Federal Reserve, U.S. Treasury, CME, CFTC
2. Official company investor-relations releases
3. Reuters or another reputable real-time market source

Verify release time, timezone, previous reading, consensus, and whether the result is already published. Cite every time-sensitive claim near the sentence it supports.

If web access is unavailable, state `MACRO DATA UNVERIFIED`. Do not repeat user-supplied event claims as confirmed facts.

## Analysis workflow

Follow this order every time.

### 1. Lock the observable data

Report:

- Instrument and contract shown
- Each timeframe
- Exact visible current-price values
- Snapshot price range when images differ
- Capture time and timezone if known
- Clearly readable reference levels
- Unreadable or approximate fields
- Data Quality

Do not begin directional analysis until this readback is complete.

### 2. Build the HTF Storyline

Use 45M and 15M to identify:

- External swing high and low
- HH/HL or LH/LL sequence
- BOS, MSS, CHOCH, displacement, and failed breaks
- Impulse versus retracement
- Premium/discount location within the active range
- Whether 15M structure agrees or conflicts with 1H/45M structure

State both layers explicitly, for example:

`LTF bullish displacement inside HTF bearish retracement.`

Never collapse a timeframe conflict into a single bullish/bearish label.

### 3. Read the auction and Volume Profile

Assess only visible or supplied levels:

- RTH VAH, VAL, POC
- Composite VAH, VAL, POC
- HVN and LVN
- Previous-day range and midpoint
- Overnight inventory and session opens
- Acceptance, rejection, migration, and failed auction

Distinguish:

- Acceptance: repeated closes/time spent beyond a level, followed by a successful retest
- Rejection: excursion beyond a level followed by a body close back inside value
- Rotation: return toward POC or the opposite side of value

Do not treat a wick through VAH/VAL as acceptance.

### 4. Apply Alchemist / Malaysian SNR

Evaluate:

- RBS: resistance becoming support
- SBR: support becoming resistance
- QML: Quasimodo level when structure supports it
- Freshness: `FRESH`, `PARTIAL`, `CONSUMED`, or `UNKNOWN`
- Zone Quality: `STRONG`, `MODERATE`, `WEAK`, or `UNKNOWN`, based on departure, arrival, base construction, and mitigation count
- Departure strength and arrival quality
- Base quality and number of mitigations
- Liquidity sweep before zone reaction
- LTF confirmation after the sweep

Use proprietary labels such as `A/V` or `OCL` exactly as shown by the user's indicator. Do not expand, redefine, or infer a proprietary abbreviation unless its definition was supplied.

### 5. Map liquidity

Identify:

- Buy-side liquidity above session/swing/equal highs
- Sell-side liquidity below session/swing/equal lows
- Asia, London, and New York highs/lows
- Previous day/week highs/lows
- Sweep, displacement, retracement, and delivery target

Describe the active path as liquidity-to-liquidity rotation, but do not assume the target will be reached.

### 6. Define the Decision Zone and Immediate Posture

Create one narrow Decision Zone around the current unresolved auction.

Always output:

`Immediate Posture: <current location, what to monitor for the next 30 minutes, and why action is or is not allowed now>`

If the current price is inside the Decision Zone and neither exclusive trigger is confirmed, explicitly say `NO TRADE NOW`.

### 7. Generate only qualified scenarios

Return zero to three scenarios. One or zero is acceptable.

Each scenario must contain:

- Direction and setup type
- State transition
- Trigger
- Confirmation
- Non-overlapping Entry band
- SL
- TP1, TP2, TP3 when justified
- Invalidation before SL
- Stand-down condition
- Failure Mode
- Valid Until with timezone
- Event Handling
- R:R for every target
- Confidence and grade
- Zone Quality and Freshness for every stated Level Type

Reject a scenario when it lacks a clear trigger, invalidation, or at least one target of 1.0R or better.

## Scenario state machine

Use these states precisely:

`WATCH → ARMED → TRIGGERED → CONFIRMED → ACTIVE → MANAGED → CLOSED`

Also allow:

- `STAND DOWN`: another mutually exclusive scenario triggered first
- `CANCELLED`: validity expired or market context changed
- `NO TRADE`: no scenario passes the quality gate

Do not label a scenario `ACTIVE` until its entry conditions are actually satisfied.

## Mutual-exclusion rules

When long and short scenarios occupy the same Decision Zone, output one ordered rule:

`Mutual Exclusion: If <trigger A> occurs first, Scenario B becomes STAND DOWN. If <trigger B> occurs first, Scenario A becomes STAND DOWN.`

Entry bands must not overlap. If they overlap, revise them or remove the weaker scenario.

If price remains between both triggers, keep both at `WATCH`; do not improvise a trade.

## Invalidation-order rules

Invalidation must protect before the hard stop.

- Long: `SL < Invalidation < Entry`
- Short: `Entry < Invalidation < SL`

If invalidation and SL are identical, write `SL = Invalidation`.

Never place invalidation outside the stop. Always output:

`Invalidation Order Check: PASS/FAIL — <short explanation>`

Discard any scenario that fails this check.

## R:R calculation

Use the Entry-band midpoint unless the user supplies an exact fill.

- Long risk: `Entry midpoint - SL`
- Long reward: `TP - Entry midpoint`
- Short risk: `SL - Entry midpoint`
- Short reward: `Entry midpoint - TP`
- R multiple: `Reward / Risk`

Round R multiples to two decimals. State the midpoint used.

If TP1 is below 1.0R:

1. Downgrade the grade by one level.
2. Explain the downgrade in `Note`.
3. Reject the scenario if no later target and management plan restore positive expectancy.

## Confidence engine

Score setup quality only after the trigger requirements are defined.

| Factor | Maximum |
| --- | ---: |
| HTF and LTF structure | 20 |
| Location and Decision Zone | 20 |
| Liquidity logic | 15 |
| Auction and Volume Profile | 10 |
| Trigger and confirmation quality | 20 |
| Freshness | 5 |
| Event and data-risk control | 10 |
| Total | 100 |

Grades:

- `A`: 75–100
- `B`: 65–74
- `C`: 55–64
- Below 55: reject and output `NO TRADE`

Apply hard caps:

- Trigger not confirmed: maximum 59 and state cannot exceed `ARMED`
- Data Quality LOW: maximum 54
- Unresolved HTF conflict: maximum 74
- Any required Causal Chain link marked `ASSUMED`: maximum 64 and not `PRIMARY`
- High-impact event within 30 minutes: no new pre-event trade
- Event within 31–90 minutes: reduce event score, shorten Valid Until, and require explicit flat/partial/BE handling

Write:

`Confidence: <n>/100 — setup quality, not historical win rate.`

## Execution and risk controls

- Use one attempt per scenario unless the user explicitly authorizes re-entry rules.
- Never chase a move that leaves the Entry band without a retest.
- Do not move to break-even on a wick alone.
- Move to BE only after the required level receives a body close and successful retest, or after TP1 according to the scenario plan.
- Before CPI, FOMC, NFP, or another high-impact release, define a no-new-entry cutoff and a flat-by time.
- In prop-firm contexts, default to half normal risk or 0.25R during event-heavy sessions unless the user supplies different limits.
- Never recommend holding through a high-impact release without explicit user authorization and a defined maximum loss.

## Required tactical visualization

Include a compact price ladder using only readable levels:

```text
≈ upper target / liquidity
≈ resistance or supply
──────── DECISION ZONE ────────
CURRENT
≈ support or invalidation
≈ lower target / liquidity
```

Keep labels outside the price line when annotating an image. Never cover candles with text.


## Outcome definition (mandatory per scenario)

Every scenario declares how it will later be judged, before it is traded. The standard
success test is:

> After a valid Trigger, Confirmation, and Fill, does price reach TP1 before SL within
> the Valid Until window?

| Result | Verdict |
| --- | --- |
| TP1 reached | `SUCCESS` |
| TP2 reached | `STRONG SUCCESS` |
| TP3 reached | `FULL SUCCESS` |
| SL reached first after fill | `FAIL` |
| Trigger never occurred | `CANCELED` |
| Confirmation never occurred | `CANCELED` |
| No fill | `CANCELED` |
| Valid Until elapsed | `EXPIRED` |
| Price movement after expiry | out of scope |

A scenario with no Outcome Definition is not a scenario. It is a narrative, and it is
not emitted.

## Decision comparison (mandatory)

`LONG`, `SHORT`, and `FLAT` are evaluated under identical constraints — same costs,
same event windows, same account limits. `FLAT` is a decision with its own evidence
ledger, not the residue left when the other two fail. When the exclusive triggers are
both unconfirmed, `FLAT` wins by construction and the packet carries `NO TRADE`.
