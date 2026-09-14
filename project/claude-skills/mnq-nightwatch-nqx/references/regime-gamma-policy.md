# Regime and Gamma Policy RG-H1

## Contents

1. Scope and status
2. Input contract
3. Metric-mode classifier
4. Screenshot fallback
5. Stability and conflict rules
6. Gamma provenance and setup gates
7. NQX encoding

## 1. Scope and status

`RG-H1` is a deterministic labeling policy, not a proven trading edge. Always report
it as `qm=H`. Its purpose is to make discretionary regime labels reproducible enough
to collect comparable observations.

The regime code describes current market behavior. It does not authorize an entry.
Gamma context changes the evidence required for a setup but does not override
measured structure, levels, event gates, or risk geometry.

## 2. Input contract

Use only information available at the frozen decision timestamp. Compute
percentiles against the same session and clock-time bucket from at least 20
completed comparable sessions. Retain both the raw ratios and their percentile
ranks so a later audit can reproduce the label.

| Input | Meaning | Valid values |
|---|---|---|
| `mode` | Evidence mode | `METRIC` or `SCREENSHOT` |
| `session` | Session bucket | `ASIA`, `LONDON`, `NY_AM`, `NY_PM`, `OTHER` |
| `clock_bucket` | Frozen within-session comparison bucket | e.g. `NY_AM+00:30` |
| `minutes_from_session_open` | Minutes since the named session opened | Non-negative or missing |
| `realized_vol_ratio` | Trailing 30-minute realized vol ÷ same-clock median | Non-negative or missing |
| `rv_percentile` | Trailing 30-minute realized-vol percentile | 0-100 or missing |
| `range_atr_ratio` | Current session high-low ÷ completed daily ATR(20) | Non-negative or missing |
| `range_atr_percentile` | Current session range percentile by clock bucket | 0-100 or missing |
| `gap_atr_ratio` | \|open - prior settlement\| ÷ completed daily ATR(20) | Non-negative or missing |
| `directional_efficiency_percentile` | `abs(Ct-Ct-30m) / sum(abs(bar returns))`, percentile | 0-100 or missing |
| `overlap_percentile` | Median adjacent-bar overlap ratio, percentile | 0-100 or missing |
| `prior_day_structure` | Completed prior-day auction state | `BALANCE`, `TREND_UP`, `TREND_DOWN`, `EXPANSION_UP`, `EXPANSION_DOWN`, `MIXED`, `UNKNOWN` |
| `directional_bias` | Current confirmed direction | `UP`, `DOWN`, `NEUTRAL`, `UNKNOWN` |
| `structure` | Confirmed auction structure | `RANGE`, `TREND`, `BREAK_HOLD`, `BREAK_RECLAIM`, `TRANSITION`, `CONFLICT`, `UNKNOWN` |
| `htf_alignment` | Supplied structure/context frames | `ALIGNED`, `MIXED`, `CONFLICT`, `UNKNOWN` |
| `event.state` | Scheduled-information state | `NONE`, `UPCOMING`, `POST` |
| `event.verified` | Authoritative event time verified | Boolean |
| `event.minutes` | Minutes before/after event | Non-negative or missing |
| `gamma.state` | Dealer gamma sign | `P`, `N`, `U` |
| `gamma.source` | Provenance class | `DIRECT_VENDOR`, `CHAIN_CALC`, `USER_CLAIM`, `VIX_PROXY`, `MISSING` |
| `gamma.as_of` | Source timestamp | Timestamp or `MISSING` |

For screenshot mode also provide `frames_supplied`, `confirmed_rotations`,
`accepted_boundary`, `visible_compression`, and `visible_repricing` when
observable. Do not manufacture absent values to satisfy a rule.

## 3. Metric-mode classifier

Apply the following rules in priority order. Stop at the first passing rule.

### Data gate

Require: `comparable_sessions >= 20`; exact `realized_vol_ratio`,
`range_atr_ratio`, `gap_atr_ratio`; all four percentile metrics; an explicit
`session`, `clock_bucket`, completed `prior_day_structure`, and current
`directional_bias`; a confirmed `structure` value other than `UNKNOWN`; confirmed
bars only.

If the gate fails, do not run metric mode. Use the screenshot fallback (§4) when its
facts are available; otherwise assign `MX`.

### Priority 1 — EVENT REPRICING (`ER`)

All of: event verified and `POST` within 60 minutes; `rv_percentile >= 80` or
`range_atr_percentile >= 80`; structure is `BREAK_HOLD`. Do not carry pre-event
profile/value assumptions into the new distribution without revalidation.

### Priority 2 — EVENT COMPRESSION (`EC`)

All of: event verified and `UPCOMING` within 30 minutes; `rv_percentile <= 40`;
`range_atr_percentile <= 40`. An upcoming event without measurable compression is
not `EC`; continue through the classifier.

### Priority 3 — EXPANSION (`EX`)

All of: no active verified event rule qualifies for `EC`/`ER`; `rv_percentile >=
80`; `range_atr_percentile >= 80`; `directional_efficiency_percentile >= 60`;
structure is `BREAK_HOLD`.

### Priority 3.5 — unresolved opening context (`TX`)

Before considering `TR`/`BA`, assign provisional `TX` when either: during the
first 60 minutes of `NY_AM`, `gap_atr_ratio >= 0.50` and structure has not reached
`BREAK_HOLD`; or current `directional_bias` opposes a prior-day `TREND_*`/
`EXPANSION_*` direction and structure has not reached `BREAK_HOLD`. The `0.50 ATR`
cutoff is an explicit sampling heuristic, not an empirical edge estimate.

### Priority 4 — TREND (`TR`)

All of: `directional_efficiency_percentile >= 70`; `overlap_percentile <= 40`;
structure is `TREND` or `BREAK_HOLD`; HTF alignment is `ALIGNED`;
`directional_bias` is explicitly `UP` or `DOWN`; did not qualify as `EX`. Publish
`TR` only after the same candidate persists for two consecutive confirmed
execution-timeframe bars — publish `TX` before that.

### Priority 5 — BALANCE (`BA`)

All of: `directional_efficiency_percentile <= 35`; `overlap_percentile >= 65`;
`range_atr_percentile <= 70`; structure is `RANGE`; no verified event rule
qualifies for `EC`/`ER`. Publish `BA` only after two consecutive confirmed bars —
publish `TX` before that.

### Priority 6 — TRANSITION (`TX`)

Assign `TX` when any is true and no earlier rule passed: structure is
`BREAK_RECLAIM` or `TRANSITION`; a previously stable `TR`/`BA` loses a required
condition; structure and HTF alignment disagree but the conflict is newly
developing; a boundary has broken but acceptance/rejection is not yet confirmed.

### Default — MIXED (`MX`)

Assign `MX` when inputs conflict, core observations are missing, multiple stable
hypotheses remain, or no rule passes. `MX` is an honest output, not a classifier
failure.

## 4. Screenshot fallback

Screenshot mode is always heuristic and should usually remain `MX` when current
3M/15M/45M (or whichever frames are supplied) evidence is incomplete.

1. `ER`: only when an authoritative event time is verified, the screenshot is
   post-event within 60 minutes, visible repricing is explicit, and a named
   boundary has accepted.
2. `EC`: only when an authoritative event time is verified, the screenshot is
   within 30 minutes before it, and visible compression is explicit.
3. `TR`: requires current multi-frame evidence, aligned confirmed HH/HL or LL/LH
   structure, and confirmed acceptance on the same side of a named boundary.
4. `BA`: requires named upper/lower edges, at least two visible rotations between
   them, and no confirmed acceptance outside either edge.
5. `TX`: a visible break, reclaim, failed acceptance, or newly conflicting frame
   structure.
6. `MX`: every remaining case.

Never assign `EX` from screenshots alone — exact volatility/range comparisons are
missing. Never infer exact percentile values from visual candle size.

## 5. Stability and conflict rules

- Use the execution timeframe for the two-confirmed-bar stability check.
- Never pool session buckets — a `TR` label in London does not establish `TR` at
  the NY open.
- Record every label at the decision timestamp; never relabel history after seeing
  the outcome.
- If a metric crosses a threshold back and forth, use `TX` until two consecutive
  observations agree.
- If directional efficiency says trend while structure remains a confirmed range,
  use `MX`, not an average score.
- If the event clock is unverified, ignore the event for `rg` and disclose it in
  `M.un`.
- If exact metrics exist but current HTF evidence is stale, `TR` cannot pass — use
  `TX` or `MX`.

## 6. Gamma provenance and setup gates

### Provenance gate

Accept `P` or `N` only from:

- `DIRECT_VENDOR`: an identified dealer-gamma source with a visible as-of timestamp
  and relevant expiry scope.
- `CHAIN_CALC`: a reproducible aggregation from an option chain, with model, sign
  convention, expiry scope, spot, and timestamp recorded.

Force gamma to `U` when the source is `USER_CLAIM`, `VIX_PROXY`, `MISSING`, stale,
or lacks a sign convention. VIX, prior-day range, candle shape, and a large
open-interest strike can describe volatility or liquidity context but cannot
identify aggregate dealer gamma sign by themselves.

SPX gamma applied to MNQ is cross-market context. Label the causal link
`INFERRED`; it cannot be the sole `ef` item or the sole reason to make a scenario
primary.

### Setup gate matrix

| Gamma | Range reversion (`RR`) | Breakout continuation (`BC`) |
|---|---|---|
| `P` | `SUPPORT`: still require a mapped edge, sweep/rejection, and close back inside | `ELEVATED_CONFIRMATION`: require two confirmed closes outside, or one close plus a confirmed retest hold; HTF must not conflict |
| `N` | `ELEVATED_CONFIRMATION`: require sweep, reclaim, micro structure shift, and a hold; one rejection candle is insufficient | `SUPPORT`: still require a named boundary and confirmed acceptance; do not chase an unconfirmed displacement |
| `U` | `BASELINE`: gamma contributes no evidence | `BASELINE`: gamma contributes no evidence |

Gamma never cancels event blackouts, structural invalidation, R:R checks, or
mutual exclusion. Do not convert these ordinal gates into a win probability.

### Large-open-interest level trial

When an exact large-OI strike is available, record its strike, expiry scope,
source, and as-of timestamp. It may be added to the `Z` liquidity map as an
`OBSERVED` options level. It does not establish dealer gamma sign, pinning,
support, or resistance by itself. If any provenance field is absent, omit the
price rather than estimating it.

## 7. NQX encoding

Preserve NQX/1 compatibility. Do not add a gamma protocol key until the receiver
and both validators are versioned together.

Encode policy detail in existing fields:

```text
M.rg=<TR/BA/TX/EX/EC/ER/MX>
M.rm=policy=RG-H1;mode=METRIC;rvr=1.42;rar=0.86;gar=0.18;rvp=84;rap=82;dep=71;ovp=24;session=NY_AM;clock=NY_AM+00:30;prior=BALANCE;bias=UP;structure=BREAK_HOLD;gamma=U;gamma_src=MISSING
M.un=gamma sign unavailable; current 45M stale
C.pm=INFERRED: SPX gamma context may affect MNQ index-futures flow; not a standalone trigger
```

When gamma is `U`, omit it from scenario evidence or state explicitly that it
contributes no weight. When metrics are unavailable, do not place invented numbers
in `M.rm`. For VIX-specific field requirements (which are separate from gamma),
see [event-volatility.md](event-volatility.md) §2 and
[nqx1-current.md](nqx1-current.md) §2.
