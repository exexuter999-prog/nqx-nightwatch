# Chart Structure Analysis Contract

## Contents

1. Analysis order
2. Standard timeframe roles
3. Extraction targets
4. Level priority and confluence
5. Regime classification
6. LONG / SHORT / FLAT comparison

## 1. Analysis order

Follow this order every time. Building a route first and fitting levels to it
afterward produces confirmation-biased analysis; building the level map first and
letting the route fall out of it does not.

1. Evidence inventory (what images/data exist, and their exact provenance).
2. Current price and each timeframe's own snapshot timestamp.
3. Extract observed price levels (§3 below).
4. Resolve overlap, proximity, and priority into confluence groups (§4).
5. Value location and auction state (where is price relative to the level map).
6. Swing structure (§3, swing series).
7. Regime classification (§5).
8. Compare LONG, SHORT, and FLAT under the same evidence (§6).
9. Conditions, invalidation, risk, and target for whichever candidate(s) survive.
10. NQX encoding and validation (see [nqx1-current.md](nqx1-current.md)).

## 2. Standard timeframe roles

| Role | Typical frame | Purpose |
|---|---|---|
| LTF | 3M | Trigger, short-horizon acceptance/rejection, execution microstructure. |
| CTF | 15M | Primary auction and scenario design. |
| HTF | 45M | Structure, context, higher-timeframe bias. |

Use these roles only when the frames actually supplied are 3/15/45. **When the user
supplies different timeframes, use those actual timeframes in their equivalent
roles** — do not force a conversion to 3/15/45 that the evidence does not support.

## 3. Extraction targets

Pull whichever of the following are actually observable; never fabricate one that
is not present:

- Recent swing highs/lows and the swing series (HH/HL vs LL/LH).
- Breaks, acceptance, rejection, and failed breaks.
- Range top, range bottom, and range midpoint.
- Session high/low, prior-day high/low, weekly/monthly levels.
- POC, VAH, VAL, and value migration.
- Volume concentration and low-volume pass-through zones — **only when clearly
  readable from the image**, never inferred from candle shape alone.
- ATR/pressure-zone bands, clouds, and indicator scores — keep these filed as
  `INDICATOR_CLAIM`, managed separately from `OBSERVED` price levels.
- State changes around events (before/after).
- Agreement, alignment, or conflict across timeframes.

## 4. Level priority and confluence

When independent observed levels fall in the same price band, list them by name and
treat the band as **confluence** — do not fabricate a single precise price by
averaging merely-nearby levels.

Evaluate at least these factors when ranking priority:

- Higher timeframe origin.
- Repetition across multiple sessions or lookback periods.
- Distance from current price.
- Recent acceptance or rejection at the level.
- Agreement with the volume profile.
- Re-evaluation around an event.

## 5. Regime classification

Classify into at least one of:

- `TREND`
- `ROTATION`
- `TRANSITION`
- `EXPANSION`
- `COMPRESSION`
- `EVENT-RISK`
- `UNKNOWN`

A regime is a description of current market behavior, not a trade direction by
itself. Keep direction and regime state in separate fields/sentences — a `TREND`
regime does not by itself mean "go long."

For the NQX `M.rg` code specifically (`TR/BA/TX/EX/EC/ER/MX`) and its metric-mode
classifier, priority-ordered rules, and screenshot fallback, use
[regime-gamma-policy.md](regime-gamma-policy.md) — it is the authoritative,
falsifiable version of this classification for anything that will be encoded into a
packet.

## 6. LONG / SHORT / FLAT comparison

Compare all three candidates against the identical evidence set, using the same
fields for each:

- supporting evidence
- conflicting evidence
- trigger
- entry geometry
- invalidation
- structural stop
- target
- R:R
- event risk
- reason to stand down

**Score FLAT as an independent candidate, not a passive leftover.** If neither LONG
nor SHORT clears the bar, say so explicitly and explain what FLAT is waiting to see
— this is a first-class output, not an omission.
