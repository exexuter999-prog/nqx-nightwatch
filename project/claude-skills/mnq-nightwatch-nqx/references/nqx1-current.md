# NQX/1 — Current Generation, Validation, and Repair Contract

Built from the current `engine-contract/nqx1-spec.md` transport spec, the current
`scripts/validate_nqx.py` validator, and the current Nightwatch HTML receiver
functions. Where an older document disagrees, see
[source-precedence.md](source-precedence.md). Do not copy the old Codex skill's
`nqx1.md` verbatim — several safety fields and checks postdate it.

## Contents

1. Transport basics
2. Record order and required records
3. Mandatory behavior
4. Mutual exclusion
5. Invalidation and SL invariants
6. SL structural distance (the two-path formula)
7. R:R
8. Quality and confidence
9. Output modes

## 1. Transport basics

```ebnf
packet = "!NQX/1", newline, { line } ;
line   = tag, { "|", key, "=", value }, newline ;
tag    = "D" | "M" | "C" | "Z" | "E" | "O" | "G" | "S1" | "S2" | "S3" ;
```

- Escape `|`, `\`, and a literal newline as `\|`, `\\`, `\n`.
- Range: `29560..29645`. Axis-estimated price or range: prefix `~`.
- Commas separate lists; semicolons separate independent clauses.
- Use `MISSING`, `N/A`, `U` rather than inventing data — never convert one of these
  into a number or a fact.
- `D` defines packet-local aliases referenced as `$NAME`; an alias that is
  referenced but never defined, or that resolves into itself directly or through a
  chain, is an error.
- The header must be exactly `!NQX/1`. An unknown version is rejected outright, never
  guessed at.

## 2. Record order and required records

Emit `D` (optional) then `M C Z E O G` then supported `S1`-`S3`. `M` and `G` are
required; a packet missing either is invalid. `G.n` must equal the number of `S`
records present exactly — a mismatch is an error.

Record field summaries (see `engine-contract/nqx1-spec.md` for full detail on every
key):

- **M** — market, metadata, quant gate. Key codes: `dq` (H/M/L), `qm` (H/E/C — see
  §8), `ds` (I/E/P/A), `rg` (TR/BA/TX/EX/EC/ER/MX). VIX fields `vx/vxc/vxa/vxs/vxf/vxr`
  — if `vx` is present, `vxa`, `vxs`, `vxf` are all required; `vxf` must be one of
  `LIVE`, `DELAYED`, `STALE`, `UNVERIFIED`. VIX is volatility context only — never
  use it to infer gamma sign, gamma-flip levels, or order-flow direction.
- **C** — adversarial context (auction, Alchemist/MSNR, liquidity map, participant
  map with OBSERVED vs INFERRED split, bull/bear/balance cases, dominant hypothesis,
  disconfirming evidence, commander intent).
- **Z** — price map (`dz, poc, vah, val, s, r, rbs, sbr, qml, ocl, a, v, la, lb, tm`).
  Omit an unsupported level; never write a fabricated value into it.
- **E** — event intelligence. `src=VERIFIED` requires both `at` (official event/
  research time) and `ref` (primary official reference); without both, the receiver
  downgrades to `UNVERIFIED` and the validator warns.
- **O** — exact, or axis/grid-line-estimated, observed OHLC only. `O|c=MISSING`
  when unavailable. Any of a bar's four prices may individually carry a leading `~`
  to mark a grid-line estimate rather than an exact label reading — the bar is
  still `OBSERVED` (it came from the chart), just lower precision on that value; a
  `~`-marked bar still participates in `SUPPLIED OHLC RANGE` True Range
  calculations. This `~` means something different from `S.pc`'s `~`, which marks
  a display-only projection and is never an observed value — do not conflate the
  two. See [market-data-evidence.md](market-data-evidence.md) §3 for the
  extraction contract and [source-precedence.md](source-precedence.md) §2 for the
  comma/pipe transport detail. Model output, reconstructed candles, interpolated
  prices, and scenario bars remain forbidden here — those belong only in `S.pc`.
- **P** — forecast cone (optional, display-only). A market-dynamics
  *distribution*, not a price forecast: it states the band the path would
  occupy given the volatility already printed, generated from `O.c` and `Z`
  alone. Because it reads no scenario, it is valid at `G.n=0` — the state
  where `S.pc` cannot exist. Fields: `h` (1..24 bar horizon), `u` (True Range
  unit, bare decimal), `q50/q80/q95` (`~low..~high`), `med` (`~price`), `b`
  (generation basis), `un` (limitations). Every band bound and `med` carries
  `~` — same meaning as on `S.pc` (simulated), *not* the `O.c` meaning
  (observed but grid-read). Validator enforces: quantile nesting, `u` agreeing
  with the packet's own `O.c` trailing True Range mean within 2%/0.25pt, and
  `med` within 25% of the 95% band width from the latest close (an off-centre
  median is a direction forecast, not a spread). Declaring a drift term in `b`
  is an error. The cone never feeds price, levels, trigger, invalidation, SL,
  TP, R:R, confidence, grade, or any validator decision. See §10.
- **G** — global decision gate (`n, ip, mx, io, nt, nr, rn`). `nt` is required when
  `n=0`. `ip` is required when `n>0`.
- **S1..S3** — scenarios. See §3-8 below for the safety invariants; see
  `engine-contract/nqx1-spec.md` for the full field list and controlled-code tables
  (`d, st, su, ha, zq, fr, em`).

## 3. Mandatory behavior

- Every scenario requires `vu`, `eh`, `fm`, `sd`, and `od` (Outcome Definition).
  `od` states the success test in Japanese, e.g.
  `od=Fill後、Valid UntilまでにSLより先にTP1到達=SUCCESS`.
- Unknown proprietary indicator labels stay literal or `MISSING` — never expand an
  undefined abbreviation.
- A confidence score (`cf`) is never a probability. `cf=75` does not mean 75%.
- `ASSUMED` anywhere in `cc` (causal chain) caps `cf` at 64 and forbids `do=PRIMARY`.
- **Always run the validator after generating or repairing a packet:**

  ```powershell
  python "scripts/validate_nqx.py" "<packet-path>"
  ```

  Exit code `0` is the only "done" result. Never report a packet complete while it
  fails validation, and never invent a value solely to make the validator pass —
  reduce scenario count instead, including to zero.
- A repair preserves unknown fields and the packet's existing meaning wherever
  possible. If the needed change would alter meaning, treat it as a new analysis,
  not a repair.
- An invalid packet must never overwrite the last valid Nightwatch analysis state.

## 4. Mutual exclusion

If LONG and SHORT entry bands overlap, do not return them as an ambiguous
both-sides candidate. `G.mx` must order the triggers and name which scenario becomes
`STAND DOWN`; the validator's `EXCLUSION_MISSING` / `EXCLUSION_ACTION` checks enforce
that the rule names both scenario IDs, states a stand-down action, and is
price-ordered (contains a recognizable precedence word plus a number).

## 5. Invalidation and SL invariants

```text
LONG:  SL < Invalidation < Entry Low
SHORT: Entry High < Invalidation < SL
```

Equality between invalidation and SL is permitted only when explicitly declared
(e.g. "SL = Invalidation") — the validator accepts this via an explicit-equality
phrase match, not silently.

The SL is never merely "a price backed out from an acceptable loss." It sits outside
the structural level that would prove the trade's thesis wrong, with a buffer sized
to the market's own observed range or declared level spacing (§6).

Two independent validator gates apply on top of the ordering check:

- **`SL_INVALIDATION_TOO_CLOSE`**: fires when `|invalidation - SL|` is under 15% of
  the entry-midpoint-to-SL risk distance. A close-based invalidation this near the
  hard stop may not confirm before the stop is physically touched.
- **`SL_STRUCTURAL_DISTANCE`**: fires when the SL does not clear the nearest declared
  structural level by a reasonable buffer, using the two-path unit calculation in §6.

Both are independent of the ordering check above and independent of each other —
passing one does not imply the other passes.

## 6. SL structural distance (the two-path formula)

This mirrors the receiver's `structuralStopPlan()` / `rangeModel()` exactly (see
[source-precedence.md](source-precedence.md) §5 for how this formula was reached
across three addenda). Do not use a different formula, and do not skip straight to
"looks far enough" — compute it.

**Step 1 — find the anchor.** Among protective Z-record levels (`s, r, rbs, sbr,
qml, ocl`), find the nearest one on the losing side of the trade:

- LONG: the highest level whose high is below the entry band's low.
- SHORT: the lowest level whose low is above the entry band's high.

If none exists, fall back to the invalidation price itself as the anchor.

**Step 2 — find the range unit**, trying in this order:

1. **`SUPPLIED OHLC RANGE`** — if at least 3 valid `O.c` bars exist: take the
   trailing 12 bars (fewer if unavailable), compute True Range per bar (`prev==null
   ? high-low : max(high-low, |high-prevClose|, |low-prevClose|)`), keep only
   finite, positive values, and if at least 3 remain, `unit` = their simple mean.
   Bars with a `~`-marked price still count here — an approximate observed range is
   preferred over falling back to `MAPPED LEVEL SPACING` when actual bars are
   visible. The receiver labels the source `SUPPLIED OHLC RANGE (~N/M BARS
   APPROX)` when any bar in the window is approximate, so the reduced precision
   stays visible rather than silent.
2. **`MAPPED LEVEL SPACING`** — if step 1 yields nothing: take the pivot price
   (`M.px`, or failing that the Z-record decision-zone `dz` midpoint), compute the
   absolute distance from the pivot to every declared level's low and high, take the
   median of those distances, and `unit = max(median * 0.42, 2)`.

**Step 3 — compute buffer, recommended stop, and minimum risk:**

```text
buffer      = max(unit * 0.32, 2)
recommended = anchor - buffer   (LONG)   or   anchor + buffer   (SHORT)
risk_to_sl  = |entry_midpoint - SL|
inv_dist    = |entry_midpoint - invalidation| * 1.12   (0 if invalidation unreadable)
minimum     = max(unit * 0.72, inv_dist, 4)
```

**Step 4 — flag the violation:**

```text
beyond = SL <= recommended   (LONG)   or   SL >= recommended   (SHORT)
violates SL_STRUCTURAL_DISTANCE  if  NOT beyond  OR  risk_to_sl < minimum
```

Do not substitute `|anchor - invalidation|` as a shortcut for the unit — this
degenerates to zero whenever invalidation coincides with the anchor level (a common
scenario-design pattern), which silences the check exactly when it should fire
hardest. Always compute the two-path unit above.

### 6.1 `WG-H1` wide-geometry override

When `G.rn` declares `POLICY=WG-H1`, do not use the legacy fallback above.
`WG-H1` is a preregistered heuristic, not evidence of edge, and is directional only
when `M.rg=TR|EX` and `S.su=BC|PC`.

- Require `M.rm` to declare `policy=RG-H1`. `mode=METRIC` needs `cmp>=20`,
  `rvr/rar/gar/rvp/rap/dep/ovp`, matched `session/clock/prior`, `bias`,
  `structure`, and `stable>=2`, then the frozen TR/EX thresholds. The
  `mode=SCREENSHOT` fallback is TR-only, requires 3M/15M/45M source frames,
  aligned/confirmed/stable structure, and `S.ha=A` for every emitted scenario.
  Missing evidence emits `G.n=0`; screenshot evidence never assigns EX.
- Require 15 exact accepted chronological `O.c` bars; any `~`-estimated OHLC
  blocks WG-H1. Timestamps must use strict `HH:MM` on the `M.at` date or full
  `YYYY-MM-DD HH:MM JST`, be unique and strictly increasing at exactly `M.tf`
  cadence, and end on the latest bar already closed at `M.at`. Duplicate,
  reversed, off-cadence, stale, or unclosed bars block it. `S.en/sl/tp/iv` and
  every mapped `Z`/`TM` price used by WG-H1 must be canonical bare decimal
  tokens (or `low..high` where ranges are allowed); never strip `~`, `≈`,
  labels, prefixes, suffixes, or prose into an executable price. Let `U` be
  the simple mean of the trailing 14 True Ranges. Missing data blocks the
  scenario; `MAPPED LEVEL
  SPACING` cannot authorize it.
- Require `M.px`, executable prices, and every used `Z/TM` boundary to remain
  within `0.5x..1.5x` of the latest exact `O.c` close. This is a defensive
  decimal/unit guard, not an edge parameter.
- Require one fresh primary-calendar `E` scan checked no more than 30 minutes
  before `M.at`: `E.rt` is the nearest material release, `E.at` is the check
  time, and `E.ref` is the primary source. A release inside the inclusive
  `M.at ± 30 minutes` window blocks the scenario. A verified empty window uses
  exactly `E.ev=NONE_WITHIN_30M|rt=N/A`.
- If `S.vu` crosses an actual `E.rt`, require `S.eh` to name
  `FLAT/CLOSE/REDUCE` and an exact JST deadline no later than `E.rt`.
- Anchor only to the nearest losing-side `S/R/RBS/SBR/QML/OCL` outer edge.
- Set the hard-stop threshold beyond that edge by `max(0.50U, 2pt)`, and require
  entry-midpoint risk of at least `1.50U`.
- Require TP1/TP2/TP3 to be successive declared `Z`/`TM` roadblocks at no less
  than `1.80R`, `2.80R`, and `4.00R`.
- Align every executable price to the NQ/MNQ `0.25pt` tick.
- `G.rn` must state fixed-dollar-risk size-down/MNQ discipline, rejection when one
  MNQ exceeds the cap, no stop widening after fill, and no mechanical breakeven
  before `+1.25R` (then trail only after a confirmed structural swing).
- The receiver collects the cash cap as an operator-only session setting rather
  than fabricating numeric `M.sz` under `qm=H`; it sizes MNQ with `R + 8pt` and
  blocks local `EXECUTE` if the cap is absent or below one-contract stress risk.
  Lock the cap to symbol/date/session. `EXECUTE` is permitted only from
  `S.st=C`, after whole-packet revalidation and a live receiver check. Activation
  freezes side, entry/SL/TP/invalidation, MNQ size, stress risk, and packet
  fingerprint; geometry is immutable after activation.
- Require `G.ip` to state current numeric location, action now, next-30-minute
  observable trigger/retest/acceptance/rejection, and the reason.

Changing any of those constants requires a new `setup_version`; never pool the
result with `WG-H1`.

## 7. R:R

- Compute from the entry-band midpoint, never from the entry edge.
- Do not flip the sign convention between LONG and SHORT — reward is always
  `(target - entry)` for LONG, `(entry - target)` for SHORT, divided by risk.
- TP1 below `1.00R` requires an explicit downgrade note in `no` (or rejection of the
  scenario) — the validator's `RR_TP1_LOW` check looks for a downgrade/reject
  keyword.
- Risk distance of zero, negative, or with reversed order (SL on the wrong side of
  entry) is rejected, not silently clamped.

## 8. Quality and confidence

- Screenshot-only analysis is `qm=H` (HEURISTIC). Every quantitative field
  (`pp, br, ge, ne, ce, sz, bp`) must decode to `N/A` or be declared in `M.na` — a
  numeric value under `qm=H` is a validator error (`HEURISTIC_NUMERIC`), because a
  screenshot cannot calibrate a number.
- Never fabricate a precise success probability, expected value, or size from a
  screenshot alone.
- `ASSUMED` anywhere in a scenario's causal chain caps `cf` at 64 and forbids
  `do=PRIMARY` — this is enforced by the validator, not just a style preference.
- Never drop `vu`, `eh`, `fm`, `sd`, or `od` — all five are validator-required on
  every scenario.

## 9. Output modes

- **Paste-ready / "NQXだけ"**: no prose, no code fence, NQX body only.
- **Normal analysis**: lead with the conclusion, then key levels, then what would
  change the posture, then the validated NQX packet.

## 10. Forecast cone (`P` record)

Added 2026-07-27. A `P` record carries a **distribution**, never a prediction of
price. Emit one only when a forecast cone is actually wanted, and only when the
packet's own `O.c` tape can support it.

```text
P|h=12|u=30.32|q50=~28621.6..~28719.61|q80=~28573.85..~28760.39|q95=~28513.14..~28795.1|med=~28671.68|b=観測TRブートストラップ;レベル摩擦;no drift|un=表示専用;価格予測ではない
```

Generation contract:

- Inputs are `O.c` and `Z` **only**. Never read a scenario, trigger, or
  direction into the cone — that is what keeps it valid at `G.n=0`.
- **Centre the step sample before simulating.** A raw close-to-close sample
  carries its own mean; bootstrapping it uncentred compounds into a large
  directional push and silently converts a spread into a "price will rise"
  claim. This is the single easiest way to produce a dishonest cone.
- `u` must be the trailing True Range mean of the same tape the packet carries.
  A cone scaled to a different tape is not auditable, and the validator rejects
  it (`CONE_UNIT_MISMATCH`).
- Declared levels act as friction — obstacles that slow and damp the path, never
  targets that attract it.

Validator error codes: `CONE_HORIZON`, `CONE_BAND_MISSING`, `CONE_BAND_INVALID`,
`CONE_MEDIAN_INVALID`, `CONE_QUANTILE_ORDER`, `CONE_UNIT_MISSING`,
`CONE_UNIT_INVALID`, `CONE_UNIT_MISMATCH`, `CONE_DRIFT`, `CONE_DRIFT_DECLARED`,
`CONE_BASIS_MISSING`, `CONE_LIMITS_MISSING`.

Receiver behaviour: a supplied `P` record **wins over local simulation**, so the
packet and the drawing can never disagree; the chart labels the provenance
`FROM P RECORD` versus `N PATHS`. The cone lane and the `S.pc` shape lane are
mutually exclusive — when a scenario shape is showing, the cone stands down.
Regression fixtures: `evals/fixtures/test-cone-*.nqx`.
