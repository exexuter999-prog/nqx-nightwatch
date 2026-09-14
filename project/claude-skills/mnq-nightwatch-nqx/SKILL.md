---
name: mnq-nightwatch-nqx
description: Analyze NQ/MNQ futures from screenshots, exact OHLCV, or the live Nightwatch TradingView window; acquire its fixed 15m/3m/CVD evidence in a verified order; classify structure and regime; compare LONG, SHORT, and FLAT; and generate, validate, audit, or repair Nightwatch NQX/1 packets. Use for Nightwatch/NQX market analysis, window acquisition, scenarios, levels, OHLC extraction, packet generation, validation, or repair.
---

# MNQ Nightwatch NQX

Treat every output as a falsifiable decision record. Preserve market truth, unknowns,
and state transitions before optimizing presentation. This skill only produces
analysis, evidence classification, and NQX/1 packets — it never changes Nightwatch
itself (`app/nq-nightwatch-nqx-final.html`), the NQX/1 spec, or the validator.

## 0. Absolute principles (no exceptions)

- Market Truth First. Levels Before Routes. No Synthetic Future. State-Driven
  Rendering. Risk Is Geometry.
- Never fabricate an unreadable price. Prefix every estimated price with `~`.
- Never mix observed OHLC with conditional projection shapes.
- Treat each timeframe's price as an asynchronous snapshot. Never average
  cross-timeframe prices into one "current price."
- An indicator's displayed probability, score, or state is an `INDICATOR_CLAIM`, not
  an `OBSERVED` fact.
- VIX is market-stress context. It is never a gamma proxy.
- Projection shapes are display-only. They never feed decision, current price,
  trigger, confidence, R:R, risk, or validation.
- Preserve NQX/1's version rejection, mutual exclusion, R:R, invalidation, event, and
  safety-gate rules exactly as the current spec and validator define them.
- An invalid packet may be returned to the user, but never overwrites the last valid
  analysis.
- Zero scenarios is a normal, often correct, outcome. When evidence is thin, say
  `NO TRADE` or `FLAT` explicitly rather than filling a weak route.

## 1. Classify the request mode first

1. **ANALYZE** — analyze market data and chart structure; produce an NQX packet if
   evidence supports one. Default mode when unspecified.
2. **NQX_ONLY** — return only a paste-ready NQX/1 packet.
3. **OHLC_EXTRACT** — convert one chart image's readable closed bars into a single
   `O|c=` line.
4. **AUDIT_REPAIR** — validate an existing NQX packet and apply the smallest fix that
   does not change its meaning.
5. **UPDATE** — connect new evidence to a prior analysis and decide whether it
   sustains, strengthens, weakens, or invalidates it.
6. **OUTCOME_LOG** — record the outcome of an already-frozen scenario without
   retroactively editing its conditions.
7. **WINDOW_ACQUIRE** — read the live fixed TradingView window, save its raw
   evidence in the project contract, and hand it to the deterministic Nightwatch
   cycle without manually rewriting values.

If the request is ambiguous, proceed as `ANALYZE` under an explicit, disclosed
assumption rather than stopping to ask.

## 2. Load references by mode

Always load first:

- [references/source-precedence.md](references/source-precedence.md) — authority
  order when project documents disagree.
- [references/market-data-evidence.md](references/market-data-evidence.md) — evidence
  labels, screenshot contract, OHLC extraction contract, freshness/provenance.

Load conditionally:

| Situation | Load |
|---|---|
| Any chart image or structural analysis | [references/chart-structure.md](references/chart-structure.md) |
| Generating, auditing, or repairing an NQX packet | [references/nqx1-current.md](references/nqx1-current.md) |
| Proposing any scenario with a stop, promoting st beyond `W`, or transferring NQX values into a monitor bundle | [references/operational-gates.md](references/operational-gates.md) |
| User asks for a conditional projection / forecast candle tape | [references/projection-shape.md](references/projection-shape.md) |
| Events, VIX, or gamma are in scope | [references/event-volatility.md](references/event-volatility.md) and [references/regime-gamma-policy.md](references/regime-gamma-policy.md) |
| Logging a decision or outcome | [references/evidence-ledger.md](references/evidence-ledger.md) |
| Explaining rationale or auditing design choices | [references/research-basis.md](references/research-basis.md) |
| Reading the live Nightwatch TradingView window | [references/tradingview-window-acquisition.md](references/tradingview-window-acquisition.md) |

Do not load a reference the current request does not need — it only adds
irrelevant context.

## 3. Core workflow (ANALYZE)

Follow this order; do not fit levels to a route decided in advance.

1. Evidence inventory — list every image/data source with symbol, timeframe,
   displayed time, price, and whether the right-most bar is closed or forming.
2. Current price and per-frame timestamps, kept asynchronous.
3. Extract observed price levels (see chart-structure.md §extraction targets).
4. Resolve overlap/proximity/priority among levels into confluence groups.
5. Value location and auction state.
6. Swing structure (HH/HL, LL/LH, breaks, acceptance, rejection, failed breaks).
7. Regime classification (chart-structure.md §regime).
8. Compare LONG, SHORT, and FLAT on identical evidence (chart-structure.md
   §LONG/SHORT/FLAT comparison).
9. Conditions, invalidation, risk, and target for any scenario that survives —
   then apply the initial-SL, CVD, and promotion gates in
   [references/operational-gates.md](references/operational-gates.md).
10. Encode as NQX/1 and validate.

Standard timeframe roles — 3M execution/trigger, 15M primary auction/scenario design,
45M structure/context/bias — apply only when the user's frames are actually 3/15/45.
Use the timeframes actually supplied; do not force a conversion.

## 3a. Live window acquisition

For `WINDOW_ACQUIRE`, read
[references/tradingview-window-acquisition.md](references/tradingview-window-acquisition.md)
fully and follow its context-first order. Treat the right-bottom CVD panel as an
independent evidence region inside the 3-minute pane, not as a third chart pane.
The workflow is complete only after the left pane is restored and verified as
MNQ/15m and the raw bundle passes the project's acquisition receipt.

## 4. NQX/1 generation and validation

Read [references/nqx1-current.md](references/nqx1-current.md) fully before emitting,
auditing, or repairing a packet. In summary:

- Emit records in order: optional `D`, then `M C Z E O G`, then `S1`-`S3`.
- After generating or repairing a packet, always run:

  ```powershell
  python "scripts/validate_nqx.py" "<packet-path>"
  ```

  or pipe the packet to `-`. Exit code `0` is the only passing result.
- Never report a packet as finished while the validator's exit code is non-zero.
- Never invent a value to make the validator pass. If evidence is insufficient,
  reduce the scenario count — including to zero — rather than force a pass.
- A validator PASS alone never arms a scenario. Promotion of `S.st` beyond `W`,
  initial-SL sizing, and CVD backing are governed by
  [references/operational-gates.md](references/operational-gates.md).
- A repair preserves unknown fields and the packet's existing meaning. A change that
  would alter meaning is a new analysis, not a repair.
- An invalid packet may be shown to the user as a diagnostic, but must never replace
  the last valid Nightwatch state.

When the user asks for a paste-ready packet only ("NQXだけ", "貼り付け用"): return the
raw NQX text with no prose and no code fence. Otherwise lead with the conclusion,
then key levels, then what would change the posture, then the validated NQX.

## 5. Conditional projection shapes

Only build a projection tape when the user actually asks for one, or a scenario's
form is materially clearer as a shape. Read
[references/projection-shape.md](references/projection-shape.md) first. In summary:
observed OHLC (`O.c`) and projection tape (`S.pc`) are different data classes on the
same price scale, separated by a white `NOW` boundary; every projected price carries
`~`; the shape never feeds back into current price, levels, trigger, invalidation,
stop, target, R:R, or confidence. Omit `S.pc` rather than inventing an anchor price.

## 6. Events, VIX, and gamma

Only when the request involves timing-sensitive events, VIX, or gamma, read
[references/event-volatility.md](references/event-volatility.md) and
[references/regime-gamma-policy.md](references/regime-gamma-policy.md) first. In
summary: verify event times against a primary source before marking `VERIFIED`;
never infer gamma sign from VIX, price action, or a single large-OI strike; keep VIX
and gamma provenance, as-of time, and freshness explicit and separate.

## 7. Evidence logging

Only when logging a decision or its outcome, read
[references/evidence-ledger.md](references/evidence-ledger.md) first and use its
append-only, hash-chained event schema. Never edit a decision row after the fact.

## Commands

```powershell
python "scripts/validate_nqx.py" packet.nqx
```

Exit codes: `0` clean, `1` one or more validation errors, `2` usage/IO error.

This skill is for research and educational decision support, not investment advice.
