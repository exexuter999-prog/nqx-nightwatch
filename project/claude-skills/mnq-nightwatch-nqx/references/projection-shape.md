# Conditional Projection Shape Contract

## Contents

1. Purpose
2. Data separation
3. Shape representation
4. Scale
5. Display

## 1. Purpose

A projection shape is not a claim about the future price. It translates a scenario's
current conditions into a chart shape so acceptance, rejection, pullback, and
continuation are easier to read visually. It is a display aid layered on top of an
already-frozen analysis, never a forecast that stands on its own.

Only build one when the user actually asks for it, or when a scenario's form is
materially clearer as a shape than as prose.

## 2. Data separation

- Observed OHLC lives in `O.c`.
- The conditional projection shape lives in `S.pk / S.pb / S.pc / S.pu`.
- **Never put both in the same array, the same boolean flag, or the same evidence
  class.** They are different data classes sharing only a display price scale.
- Never reuse `S.pc` as observed OHLC, even retroactively once the projected path
  turns out to match reality.
- Never recompute current price, levels, trigger, invalidation, SL, TP, or R:R from
  a projection shape. The shape is downstream of those decisions, not an input to
  them.

## 3. Shape representation

Recommended length is 3-6 consecutive bars, maximum 12:

```text
+1,~OPEN,~HIGH,~LOW,~CLOSE;+2,~OPEN,~HIGH,~LOW,~CLOSE
```

Requirements:

- Sequential steps starting at `+1` — `+N` is a relative step, not a clock time.
- Every price carries `~`, even if the source model produced more decimals.
- `HIGH >= max(OPEN,CLOSE)`, `LOW <= min(OPEN,CLOSE)`, `HIGH >= LOW` for every bar.
- Do not mechanically repeat the same range and body across bars — vary the shape to
  actually represent the scenario's character.
- Classify the shape using this vocabulary in `S.pb` (projection basis) or
  surrounding prose:
  - `ADVANCE / DECLINE / ROTATION`
  - `DIRECT / PULLBACK / TWO-WAY`
  - `EXPANDING / COMPRESSING / STEADY`

`S.pk` must be `SCENARIO` or `MODEL` — `OBSERVED`, `LIVE`, and `ACTUAL` are forbidden
values here, since this tape is never market data. `S.pb` states the model/evidence
basis; `S.pu` states limitations and what would invalidate the shape. All three of
`pk/pb/pu` present without `pc` (or vice versa) is a validator error — the
projection extension is all-or-none.

## 4. Scale

When enough observed OHLC exists, the trailing-12-bar average range may inform the
projection shape's visual scale — purely for how big the drawn bars look.

**Never use this average range for:**

- structural stop
- risk
- R:R
- confidence
- success probability
- scenario grade
- validator pass/fail

If there is no accurate anchor price to start the shape from, omit `S.pc` entirely.
Do not generate an unmarked future price purely from how the chart looks visually.

## 5. Display

- Observed and projected bars share the same price plane.
- A white `NOW` boundary line marks where observed data ends and the projection
  begins.
- The projected high/low envelope and close path are drawn as display-only overlays.
- The conditional nature of the shape stays visible at all times — never let it read
  as confirmed data.
- When a scenario is invalidated, the projection shape ends/is voided visibly rather
  than continuing to display as if still live.
- The shape's meaning must survive with reduced motion, reduced transparency, or
  effects disabled — it cannot rely purely on animation or gloss to communicate
  "this is conditional."
