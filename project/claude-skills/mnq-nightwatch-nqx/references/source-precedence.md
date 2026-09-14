# Source Precedence and Known Conflicts

## Contents

1. Authority order
2. Known conflict: O-record delimiter (comma vs pipe)
3. Known conflict: conditional projection shape wording
4. Known conflict: old Codex skill is missing current fields
5. Known conflict: SL structural-distance formula evolved across three addenda
6. How to record a new conflict

## 1. Authority order

When project documents disagree, never silently pick one side. Resolve using this
order, and record the conflict below (or add a new entry per §6) instead of quietly
overriding a stated rule.

1. What the current `scripts/validate_nqx.py` actually accepts or rejects.
2. What the current Nightwatch HTML (`app/nq-nightwatch-nqx-final.html`) actually
   receives, validates, renders, and safety-gates.
3. The current `engine-contract/nqx1-spec.md` transport spec.
4. Directive addenda, later date or higher addendum number wins over an earlier one.
5. Older design documents, the old Codex skill, and explanatory material.

This skill's `references/nqx1-current.md` is built from rung 1-3 above, not copied
from the old Codex skill's `nqx1.md`. Where the old skill and the current contract
differ, the current contract wins and the difference is logged here.

## 2. Known conflict: O-record delimiter (comma vs pipe)

- `nqx1-spec.md` originally described one `O.c` bar as
  `TIME,OPEN,HIGH,LOW,CLOSE[,VOLUME]` (comma-delimited fields, semicolon-delimited
  bars).
- The receiver's `parseCandleRow` (app/nq-nightwatch-nqx-final.html) actually
  `raw.split('|')`s each bar — pipe-delimited fields, semicolon-delimited bars.
- Resolved by `GPT_SL_GATE_UPGRADE_DIRECTIVE_ADDENDUM3.md` §0: the implementation is
  authoritative (rung 2 above); the spec text was corrected to describe pipe-delimited
  fields. `validate_nqx.py`'s `parse_observed_candles` mirrors this exactly: it
  converts each comma-delimited **transport** row to the internal pipe form via
  `split_escaped(row, ",")` joined with `|`, then splits strictly on `|`.
- **Authoritative canonical NQX transport form** (what you should actually emit):
  `TIME,OPEN,HIGH,LOW,CLOSE[,VOLUME]` per bar, comma-delimited, semicolons between
  bars — e.g. `O|c=20:48,30018,30038,30008,30031;20:51,30031,30047,30022,30041`. The
  receiver converts this transport form to its strict internal pipe form before
  parsing. Do not emit raw pipe-delimited bars directly into `O.c` — escape any
  literal pipe inside a value as `\|` since an unescaped pipe is the record-field
  delimiter at the outer NQX layer.

## 3. Known conflict: conditional projection shape wording

- Some older design language did not clearly separate "observed candle" from
  "conditional projection shape" and left ambiguity about whether both could share a
  data class.
- Current authoritative rule (nqx1-spec.md `S.pc` section + Required safety invariant
  9-10, mirrored in `validate_nqx.py`'s `validate_projection`): observed and projected
  candles are drawn on the same price scale but are a fully separate data class,
  separated by a white `NOW` boundary; every `S.pc` price carries `~`; recommended
  length is 3-6 consecutive bars; `S.pc` is display-only and never feeds current
  price, levels, trigger, invalidation, confidence, R:R, risk, or validator decisions;
  omit `S.pc` when no anchor price is readable rather than inventing one from the
  chart's visual look. See [projection-shape.md](projection-shape.md) for the full
  contract.

## 4. Known conflict: old Codex skill is missing current fields

The old skill at `C:\Users\exexu\.codex\skills\mnq-nightwatch-nqx\references\nqx1.md`
predates several safety upgrades. Differences, all resolved in favor of the current
contract:

- No `M.vx / vxc / vxa / vxs / vxf / vxr` (VIX provenance fields) — added later.
  `vxf` values are `LIVE`, `DELAYED`, `STALE`, or `UNVERIFIED`; if `vx` is present,
  `vxa`, `vxs`, and `vxf` are all required.
- `E` record lacked `at` (official event/research time) and `ref` (primary official
  reference). Current contract: `E.src=VERIFIED` requires both `at` and `ref`;
  without either, the receiver downgrades the event to `UNVERIFIED` and the
  validator warns (`EVENT_REFERENCE`).
- No `SL_INVALIDATION_TOO_CLOSE` or `SL_STRUCTURAL_DISTANCE` validator checks — both
  added by the SL-gate upgrade directive and its three addenda (§5 below).
- No `window.NIGHTWATCH_CONTEXT_FEED` external-context resolution order (packet
  fields → operator/licensed feed object → bundled dated reference snapshot).

Do not port the old skill's `nqx1.md` verbatim. Use it only as historical background
for regime/gamma/evidence-ledger material that the current contract has not
superseded (regime-gamma-policy.md, evidence-ledger.md, research-basis.md carry
those forward with updates where the current HTML/validator added detail, e.g. VIX
provenance).

## 5. Known conflict: SL structural-distance formula evolved across three addenda

The `SL_STRUCTURAL_DISTANCE` validator check went through three revisions before
reaching its current form (see `GPT_SL_GATE_UPGRADE_DIRECTIVE_ADDENDUM.md`,
`_ADDENDUM2.md`, `_ADDENDUM3.md`). Only the final form, as implemented in the current
`validate_nqx.py`, is authoritative:

1. Addendum 1 tried `proxy_unit = |anchor - invalidation|`. Rejected: degenerates to
   zero whenever invalidation coincides with the anchor level, a common
   scenario-design pattern, silencing the check exactly when it matters most.
2. Addendum 2 replaced it with `level_spacing_unit` — the median distance from a
   pivot price (`M.px`, falling back to the Z-record decision-zone midpoint) to
   every declared Z-record level, scaled by `0.42`, floored at `2`. This exactly
   reproduces the receiver's `rangeModel()` `MAPPED LEVEL SPACING` fallback and is
   correct whenever `O.c=MISSING`.
3. Addendum 3 added `supplied_ohlc_unit` — when at least three valid `O.c` bars
   exist, use the trailing-12-bar simple mean of True Range instead (mirrors the
   receiver's `SUPPLIED OHLC RANGE` branch exactly). This is preferred over
   `MAPPED LEVEL SPACING` whenever it is available.

Current authoritative order, as implemented: try `supplied_ohlc_unit` (needs ≥3
accepted `O.c` bars) first; fall back to `level_spacing_unit` when it returns `None`.
See [nqx1-current.md](nqx1-current.md) §SL structural distance for the full formula.

## 6. Deliberate spec change: `O.c` prices may carry `~` (2026-07-24)

This is not a conflict between existing documents — it is a deliberate,
user-directed change made after this skill's initial creation, applied to all
three authority rungs together so they stay consistent:

- `engine-contract/validate_nqx.py`: `parse_observed_candles` now flags a bar
  `approx: true` when any of its four OHLC fields carries a leading `~`; the
  numeric extraction itself was already `~`-tolerant (the regex simply skips
  non-digit characters), so no parsing behavior changed, only the visibility of
  the approximation. A new `OBSERVED_CANDLE_APPROX` warning reports how many
  bars in `O.c` are approximate. Approx bars still enter `supplied_ohlc_unit`'s
  True Range mean — an approximate observed range is preferred over falling back
  to `MAPPED LEVEL SPACING` when actual bars are visible on the chart.
- `app/nq-nightwatch-nqx-final.html`: `parseCandleRow` mirrors the same
  `approx` flag; `rangeModel()` labels its `SUPPLIED OHLC RANGE` source as
  `SUPPLIED OHLC RANGE (~N/M BARS APPROX)` when the trailing-12 window contains
  any approximate bar, so the reduced precision is visible in the UI rather than
  silent.
- `engine-contract/nqx1-spec.md`: the `O` record section and Required safety
  invariant 9 were updated to state that any `O.c` price may carry `~` to mark a
  grid-line/axis estimate of a still-`OBSERVED` bar — explicitly distinct from
  `~` on `S.pc`, which always marks a display-only projection and never an
  observed value. Do not conflate the two meanings of `~` across `O` and `S.pc`.

Before this change, `O.c` was "exact only" with `MISSING` as the sole fallback for
an unreadable bar. The `~` extension adds a third option — a grid-line-estimated
bar that is still treated as observed, still contributes to range calculations,
but is flagged as lower precision — for charts (e.g. plain gridline axes with no
per-bar price labels) where reading a bar against the axis is possible but not
exact. This does not loosen any existing safety gate: geometry checks, mutual
exclusion, invalidation ordering, and both `SL_INVALIDATION_TOO_CLOSE` /
`SL_STRUCTURAL_DISTANCE` checks apply identically to approx and exact bars.

Regression fixture: `evals/fixtures/test-ohlc-approx.nqx` (added alongside this
change) exercises a 5-bar, all-`~` `O.c` tape and confirms `validate_nqx.py` and
the HTML's `rangeModel()` compute the identical `unit` value (77.4) from it.

## 7. How to record a new conflict

If you discover a new discrepancy between project documents while using this skill,
do not silently resolve it in either direction. Add a numbered entry here stating:
the two conflicting statements, which rung of §1 each comes from, which one you
followed, and why. Never delete a resolved entry — it is the audit trail for why the
skill behaves the way it does.
