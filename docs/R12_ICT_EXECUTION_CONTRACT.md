# R12 — ICT evidence, execution, and OOS contract

This is the current operational contract for `msnr_gate.py`, `monitor_publish.py`,
`autotrade_engine.py`, and `order.py`. It supersedes conflicting R10 advisory
text. The active decision remains one primary A/A+ model; this document does
not authorize a live order by itself.

## 1. One decision contract

Every cycle carries the same immutable facts from evaluation to execution:

| Area | Contract |
|---|---|
| CVD | Fetch once, then re-fetch once when missing or stalled. Emit `cvdMeta.attempts`, `status`, source, and recent history. Until fresh data exists, do not score CVD direction and cap an otherwise A+ decision at A. A remains allowed if structural requirements pass. |
| Grade | A/A+ requires completed model, structural SL, two distinct targets, fixed qty=2, valid price/symbol, and no hard blocker. A+ additionally requires fresh CVD; it does not require CVD directional agreement. |
| Event | High-impact blackout is a hard downstream demotion to WATCH. Calendar unavailability is disclosed; it is never fabricated as “no event”. |
| Risk | MNQU6, two contracts fixed, structure-first SL, maximum 60 pt / $240 total risk. The same scenario is demoted if this arithmetic, volatility rule, or day guard fails. |
| Session end | New entry is not created outside the monitoring window. An open position is flattened by clock only when `NQX_AUTOTRADE_SESSION_FLATTEN=1`; stop/TP/kill/forceFlatten remain executable regardless of day guard. |

`evaluation.decision`, the published scenario, and the frozen ledger plan must
retain `decisionId`, `entry`, `stop`, `targets`, `targetR`, `grade`,
`cvdHealth`, `rangeAnchor`, FVG evidence, SMT evidence, event outcome, and
the final post-gate state. No component may recreate these values from a
different snapshot.

## 2. ICT anchors and evidence

### Range anchor / OTE

`rangeAnchor` is mandatory for `OTE_FVG_PULLBACK` and is stored as:

```json
{
  "rangeTf": "15m",
  "rangeStart": "2026-08-21T20:00:00+09:00",
  "rangeEnd": "2026-08-21T21:00:00+09:00",
  "anchorType": "HTF_DIRECTIONAL_LEG",
  "freshness": "FRESH",
  "high": 0.0,
  "low": 0.0
}
```

Only a fresh HTF or named-session range is usable. A rolling 3-minute high/low
is display-only and is rejected for OTE construction. OTE is therefore a
location defined before entry, not a retrospective zone fit to the last bars.

### FVG

Each FVG record has `timeframe`, `createdAt`, `ageBars`, `ageMinutes`,
`displacementBody`, `displacementR`, `arrivalState`, and
`preArrivalStructure`. An OTE/FVG candidate requires an `eligible=true`,
unfilled FVG with `preArrivalStructure=INTACT`, age no more than 20 execution
bars, and displacement of at least 1.30× the noise floor.

### SMT

SMT is usable only with primary and peer bars at the identical timestamp,
identical `sessionId`, a fresh peer last bar, and an AM/PM SMT window. Missing,
stale, cross-session, or out-of-window SMT contributes zero; it never becomes
a synthetic trigger.

## 3. CVD re-fetch protocol

1. Fetch the visible CVD study with the initial market/study pass.
2. If absent, or the last three recorded CVD values are unchanged while price
   moved, perform exactly one second `data_get_study_values` request before
   calling `monitor_publish.py`.
3. Save both the value and `cvdMeta = {provider, attempts, status, history}`.
4. If the second read is fresh, evaluate normally. If it is still unavailable,
   `msnr_gate` emits `cvdRefresh`/`CVD_UNAVAILABLE_A_CAP`; the decision can be
   A but cannot be A+. Do not invent a directional CVD value or retry an order.

The retry is a market-data action, not an execution retry. Any order response
that is partial, failed, timed out, or unknown remains HALT.

## 4. Real two-contract management

An executable plan requires two distinct targets. `order.py --split-tp TP1,TP2`
creates two independent one-contract OCO brackets with the same structural SL:

- TP1 contract exits at `targets[0]`.
- Runner contract exits at `targets[-1]` unless its protective stop is improved.
- While broker quantity is 2, no blanket modify is sent, so TP1 cannot be
  accidentally erased.
- Once broker quantity is 1, only that runner receives a breakeven/one-way
  trail modification. A residual position at final target/stop/kill/session
  cutoff is flattened.

A partial split submission or post-send quantity other than 0/2 is HALT and
requires broker reconciliation. It is never compensated by an automatic
second submission.

## 5. OOS ablation, without performance fabrication

`oos_ablation.py` consumes audit JSONL rows containing the original fixed
`entry`, `stop`, `exitSpec`, an actual `outcome`, and seven boolean features.
It applies only incremental filters in this exact order:

```text
MSNR -> +PD -> +DOL -> +FVG -> +SMT -> +Killzone -> +CVD
```

Run it only on the chronological OOS partition:

```powershell
Get-Content -Raw -Encoding UTF8 .secrets/monitor_cycle_completed.json | python ict_observe.py --oos-jsonl .secrets/ict_trade_outcomes.jsonl
python oos_ablation.py --input .secrets/ict_trade_outcomes.jsonl --oos-start 2026-09-01T00:00:00+09:00 --output artifacts/ict_oos_ablation.json
```

For every stage it reports signal/fill counts, fill rate, realized R, win rate,
MFE R, MAE R, total net R, and post-cost expectancy per fill and per signal.
`outcome.realizedR`, `costR`, `mfeR`, and `maeR` are mandatory for filled
trades. The tool rejects missing outcomes rather than simulating fills,
costs, or results.
