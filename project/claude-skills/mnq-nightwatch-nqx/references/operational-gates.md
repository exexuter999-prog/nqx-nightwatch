# Operational gates — initial SL, CVD backing, state promotion, bundle transfer

> **R12 current override (2026-08-22).** The original R10/R8 sections below
> are retained as historical analysis, but do not define an actionable
> Nightwatch/NQX decision where they conflict with this block or
> `docs/R12_ICT_EXECUTION_CONTRACT.md`.
>
> - ICT is a decision input, not an advisory display. OTE requires the saved
>   `rangeAnchor` fields `rangeTf/rangeStart/rangeEnd/anchorType/freshness/high/low`.
>   A rolling 3M range must never create an OTE entry.
> - FVG records timeframe, age, displacement, and `preArrivalStructure`; SMT
>   requires identical timestamps, an identical `sessionId`, fresh peers, and
>   a valid SMT window. Missing evidence is neutral, never invented.
> - CVD missing or stalled means **fetch the study once again first** and save
>   `cvdMeta`. If it remains non-fresh, omit CVD directional score and cap an
>   otherwise A+ setup at **A**. It does not force WATCH when the structural
>   model is valid.
> - An executable scenario has fixed qty=2, structure-first SL no wider than
>   60pt/$240, two distinct targets, and the post-event/post-volatility/
>   post-dayguard published state. High-event blackout demotes to WATCH;
>   session flatten remains opt-in and never blocks exit.
> - Entry mode comes from the selected R11/R12 model (`AGGRESSIVE_ACCEPT_CLOSE`
>   or `STRUCTURE_RETEST`), not the retired universal `E within +/-2pt of S`
>   rule. It expires on the next 15M structure boundary, invalidation, event,
>   or 30 minutes maximum—never a fixed 10-minute TTL.
> - Use true two-contract management: independent 1-contract TP1 and runner
>   OCO brackets. Never replace both targets with a common bracket. Partial
>   response, broker mismatch, and unknown state are HALT with no retry.
>
> Evaluate empirical value only through the chronological OOS ablation order
> `MSNR -> PD -> DOL -> FVG -> SMT -> Killzone -> CVD`, holding recorded entry,
> stop, exit specification, and cost fixed. See `oos_ablation.py`.

Added 2026-08-16 after the analysis-logic audit (HANDOFF.md §11 ⑤ in the
`nq-nightwatch-claude-code-handoff` working directory). These gates close the
gap between packet-level validation and the operating rules in the project's
CLAUDE.md / TRADING_CONTEXT.md. Apply them whenever this skill proposes an
actionable scenario, or its output is transferred into the monitor bundle
consumed by `monitor_publish.py`.

A validator PASS (exit 0) is necessary but **never sufficient** for any gate
in this file.

## 1. Initial SL — structure first, then recompute against real bar range

Order (never skip a step, never reverse the order):

```
1. Structural invalidation price (where the trade idea is wrong)
2. + buffer = one full recent closed-bar range on the execution timeframe
3. = SL distance
4. contracts = risk budget ÷ (SL distance × $2.00 per MNQ point)
5. does not fit with 1 contract → no trade (do not tighten the SL to fit)
```

- **Buffer sizing**: use the typical range (average or median high−low) of
  the last ~12 closed bars on the execution timeframe (3M unless the user
  states otherwise), treated as ≈1.0 bar of noise. The validator's legacy
  floors (buffer 0.32×U, minimum risk 0.72×U) are lower bounds for packet
  consistency only — a stop can clear them and still sit inside ordinary
  noise.
- **Recompute before presenting**: compare the final SL distance against the
  recent bar range. If the distance from entry to stop is smaller than one
  typical closed bar, the stop is harvested by a "normal" bar — widen to
  structure+buffer or drop the scenario. (2026-08-15 failure: SL 13pt vs a
  15pt average 3M range passed the validator and was caught by the operator.)
- **Never derive the SL distance from a dollar amount.** The risk cap is a
  budget the structural distance must fit into, not a target to shrink
  toward. Shrinking the stop to make the dollars smaller raises the
  probability of being stopped, it does not reduce risk.

## 2. CVD backing and indicator health

- An actionable scenario (st=`A`/`X`, or ARMED/ACTIVE downstream) requires
  CVD backing from the supplied evidence: direction consistent with the
  route, or a fresh EMA cross on the CVD study (GC for LONG, DC for SHORT).
- **Indicator stall**: the same CVD value on 3+ consecutive closed bars
  while price moved means the study is frozen. While frozen, emit no new
  actionable scenarios; WATCH-level records and `watching` text remain
  allowed. Do not use the frozen value as evidence in either direction.
- If CVD evidence is absent from the supplied material, say so explicitly
  and keep st=`W`. Price action alone does not substitute for the missing
  confirmation.

## 3. State promotion (W → A/X)

- st=`W` (WATCH) is the default **and the ceiling** until ALL of the
  following hold **on a closed bar**: structural basis, CVD backing (§2),
  SL passing §1, **entry placement passing §6**, **HTF alignment passing
  §7**, and the risk fitting the operator's per-account session cap.
  A forming bar never promotes a scenario.
- Bundle mapping: st=`W` → `"state": "WATCH"`; st=`A` → `"ARMED"`;
  st=`X` → `"ACTIVE"`. ARMED and ACTIVE both render a live order control in
  the Mini App — treat promotion as arming a one-tap order, not as a
  display nuance.
- At DECISION logging time only WATCH or ARMED are valid statuses
  (evidence-ledger.md).

## 4. NQX → monitor-bundle transfer (field shape contract)

The bundle consumed by `monitor_publish.py` requires **single bare numbers**
for `entry` / `stop` / `target`, and `side` ∈ BUY/SELL (LONG/SHORT accepted).
NQX notations must be resolved, never copied verbatim:

| NQX value | Bundle rule |
|---|---|
| `en=low..high` (range) | Pick ONE executable price — the edge you would actually rest the order at. Never paste the range. |
| `tp=TP1,TP2,TP3` (list) | `target` = TP1 only. TP2/TP3 survive as the management plan (§10), not as bundle fields. |
| `~`-prefixed price (estimate) | Estimates never enter the bundle. Resolve to an observed price or drop the scenario. |
| `d=N` (neutral) | No scenario record at all — `side` is mandatory; neutral posture belongs in `watching` text. |
| st codes other than `W`/`A`/`X` | Do not transfer. T/C/P/B/D/I are packet lifecycle states, not bundle states. |

A value that cannot be resolved under these rules stays out of the bundle;
the scenario degrades to `watching` text instead. `monitor_publish.py`
rejects or demotes violations (unknown side → not published; unknown state →
WATCH), so a shortcut here produces a dead card, not an order.

## 5. Scheduled-event blackout (news releases and speeches)

- No scenario may be promoted to st=`A`/`X` inside the blackout window of a
  High-impact USD event — default 15 minutes before to 10 minutes after the
  scheduled time. The operator's session source is `events.py`
  (ForexFactory weekly feed cache + manual entries); the verification rules
  in event-volatility.md still govern how event *times* are treated as
  evidence (the feed is UNVERIFIED provenance).
- Inside the window, existing analyses stay at st=`W` and new packets should
  prefer FLAT / zero scenarios. A release can invalidate structure faster
  than any stop management can react.
- The pipeline enforces this downstream (`monitor_publish.py` demotes
  ARMED/ACTIVE to WATCH during blackout, and again at the state server),
  but do not rely on that alone — the packet itself must not claim an armed
  state it cannot hold through the release.
- Known upcoming events inside the session belong in the packet's event
  record (nqx1-current.md). Absence of calendar data is a disclosed
  unknown ("calendar unavailable"), never "no events".

## 6. Entry placement — the entry sits ON the structure, not near the market

Added 2026-08-18 after the 8/17 session. §1 gives the stop a procedure; the
entry had none, so entries were placed near current price instead of at the
level that justified the trade. Two consequences, both observed live:

- entry above/below the structure ⇒ the order **fills before the thesis is
  tested**, and the "air" between entry and structure is pure cost carrying
  no analytical content;
- entry far from current price ⇒ a resting order that **outlives the
  structure** and fills later under conditions nobody evaluated.

Order (mirrors §1, never skip a step):

```
1. S = the structural price that justifies the trade
      (confluence edge, defended shelf, reclaimed ceiling, SwingArm rail)
2. E is placed within ±2pt of S — never at "current price minus a little"
3. SL from §1, measured outward from S (not from E)
4. air        = |E − S|          → must be ≤ 20% of total risk (S..SL + air)
5. reach      = |last − E|       → must be ≤ 1.0R to promote; above that the
                                    scenario stays st=W until price closes in
```

- **air > 20%** ⇒ the entry floats off the structure. Move E onto S, or drop
  the scenario. Do not "split the difference" — a mid-air entry converts a
  structural trade into a directional bet.
- **reach > 1.0R** ⇒ this is a wait, not an arm. Publishing it as A/X arms a
  one-tap order that will sit through a regime the analysis never saw.
- The presented scenario must show **S, air, reach** as numbers next to
  entry/stop/target. A proposal missing them is incomplete, not merely terse.

Worked failure (2026-08-17, MNQ, both LONG):

| | Trade 1 | Trade 2 |
|---|---|---|
| S | 30,232 (reclaimed ceiling) | 30,255 (shelf defended 3×) |
| E | 30,238 | **30,268** |
| SL | 30,213 | 30,245 |
| air | 6pt (24%) | **13pt (57%)** |
| reach at arm | **30pt = 1.2R** | 8.5pt (0.4R) |
| outcome | filled 38 min later, after the ceiling had already broken | filled on a shallow dip; the shelf was never tested before entry, then broke |

Rebuilt under this gate, Trade 2 is E=30,257 / SL=30,245, risk 12pt instead
of 23pt — and it only fills if price actually returns to the shelf.

### 6b. Resting-order lifetime is bound to the scenario

A scenario and its unfilled order share one lifetime. The scenario TTL
(10 min ≈ 3× 3M bars in this operator's setup) applies to the order too.

- Unfilled past TTL ⇒ withdraw the scenario **and** the order.
- Invalidation price touched ⇒ withdraw the order even though it never
  filled. A resting limit whose thesis is dead is an unrelated trade.
- **Every withdrawal statement must name what happens to the order** in the
  same sentence. Reporting "scenario invalidated" alone leaves a live order.
- Count outstanding unfilled orders each cycle; **two or more is a warning
  condition**, because independent scenarios can stack size the operator
  never sized for.

2026-08-17: a scenario withdrawn at 00:07 left its 30,238 limit working; it
filled at 00:42 alongside a second, unrelated scenario.

## 7. HTF alignment — the 15M engine is read, and then used

The execution timeframe (3M) cannot see the structure it is trading inside.
The SwingArm study on the 15M pane exports `NQX_DATA_*` regardless of what
is visible, so there is no cost excuse for skipping it.

**Cadence** — a fresh 15M read is mandatory at session start, on each 15M
close, and **without exception immediately before any W → A/X promotion**.
An un-refreshed HTF read is a blocked promotion, not a soft warning.

Apply the values to three checks and state each result in the output:

1. **Direction** — `NQX_DATA_CT_TREND` opposing the proposed side blocks
   promotion. A long taken against a bearish CT arm is frequently a
   retracement rally being bought at the level where sellers re-engage.
2. **Rail proximity** — compare E against `CT_FIB618` / `CT_FIB786` /
   `CT_TRAIL`. Do not place an entry immediately beneath a counter-trend
   rail (or above one, for shorts).
3. **Stop scale** — the §1 buffer must come from **the timeframe the
   invalidation belongs to**. If the scenario dies when a 15M-scale range,
   rail, or session level is negated, the buffer is `NQX_DATA_CT_ATR`, and
   a stop narrower than that sits inside one HTF bar of noise. If the
   scenario is a 3M *reaction* at a 15M level (pullback-sell, bounce-buy —
   invalidated by the reaction failing, not by the whole level breaking),
   the buffer is the 3M noise floor (§8) — the direction check above still
   applies in full. Collapsing these two cases in either direction is
   fatal: 8/17 first traded a 15M-scale structure with a 3M-sized stop
   (both stopped), then spent the rest of the night refusing every short
   as "CT_ATR exceeds budget" when the 3M-reaction form was available.

2026-08-17 failure: the 15M pane was read once at session start and not
again for seven hours of monitoring, because the operator's loop text
described only the 3M pass. Over that window `CT_TREND` flipped +1 → −1,
`CT_ATR` widened 24.5 → 34.6pt, and the 00:28 long entry (30,268) sat just
above `CT_FIB618` (30,260) with a 23pt stop — 0.67× CT ATR. None of it was
visible because none of it was fetched. **A gate that lives only in the
procedure document does not run; it has to be in the text that drives each
cycle.**

## 8. Volatility budget gate — session tradability is arithmetic, not a clock

Added 2026-08-18. Small-budget accounts (per-trade caps of $50–$60 on MNQ)
do not fail "in the NY session"; they fail whenever the execution-timeframe
noise floor consumes the stop budget. Decide per cycle, with numbers:

```
SL_max (pt)   = per-account cap ÷ $2.00   (25pt across $60/$60/$50;
                                           30pt if dropping the $50 account)
noise (pt)    = median high−low of the last 12 CLOSED execution-TF bars
                (this is also the §1 minimum buffer, by construction)
ratio         = noise ÷ SL_max
```

| ratio | ruling |
|---|---|
| ≤ 0.40 | normal — structure + buffer fits the budget |
| 0.40–0.60 | A+ only — propose only if structure width ≤ SL_max − noise |
| > 0.60 | stand down — the buffer alone exhausts the budget; no st=A/X |

Every monitoring cycle publishes one line: noise, ratio, ruling. When the
ruling blocks everything, the `watching` text must state the **unlock
condition** ("tradable if noise settles below ~15pt", "next 15M close may
flip CT") — a standing "no" with no exit condition is indistinguishable
from a blanket ban, which this gate exists to replace.

Validation against 2026-08-17 (measured, not estimated): the two losing
arms happened at noise 19.25pt (ratio 0.77) and 20.50pt (ratio 0.82) —
both deep in the stand-down band; this gate alone blocks both. The
2026-08-12 winning trades used 7–19pt stops, viable only because that
session's noise floor sat in the normal band. Hourly medians on 8/17:
22:00 JST = 30pt (stand down), 23:00 = 25pt (stand down), 01:00+ = 14–17pt
(A+ zone). Same session label, opposite rulings — which is the point.

## 9. MSNR confirmation chain — reaction setups arm only on a completed chain

Added 2026-08-18 (revised the same day after the R1 audit). The MSNR
(Alchemist / Malaysian SNR) doctrine requires a confirmation chain and states
that a break alone is a SWEEP CANDIDATE, not a setup. **Both 2026-08-17
losses were armed in exactly that candidate state** — a closed-bar break
followed by a retest, with no sweep and no market-structure shift. A
completed chain is what confers arming eligibility for a reaction setup; an
incomplete one is a reason to keep st=`W`, not a reason to size smaller.

Two chains exist, because a level plays two roles over its life:

- **SWEEP chain** — the level *holds*: its liquidity is swept and reclaimed,
  structure shifts, and the level is retested from the original side.
- **FLIP chain (RBS/SBR)** — the level *fails and inverts*: broken with
  acceptance, revisited from the other side, and held. This is the
  2026-08-12 winning form (selling the retest of a broken shelf), and an
  implementation without it can never approve that trade.

The judgement is mechanical and runs outside this skill: `msnr_gate.py` in
the operator's working directory consumes the same monitor bundle and emits
per-level, per-side rulings. This section is the specification it implements
— when the two disagree, the numbers below are what must be reconciled, not
re-interpreted case by case.

### 9.1 Composition (the conclusion)

```
promotion[side].allowed =
      (  SWEEP.state == RETEST_HELD and barsSinceRetest <= COMPLETE_TTL
      or FLIP.state  == FLIP_HELD   and barsSinceHold   <= COMPLETE_TTL )
  AND anchorOk[side]
  AND rotation.verdict == "OK"
  AND NOT dynamic
```

This gate **does not replace** the volatility budget gate (§8), HTF
alignment (§7), entry placement (§6), CVD backing (§2) or the event blackout
(§5). It is an **additional AND condition** on top of them.

Blocker codes (nothing outside this enumeration is emitted):
`NO_CHAIN` / `SWEEP_ONLY` / `NO_DISPLACEMENT` / `MSS_NOT_CONFIRMED` /
`RETEST_NOT_HELD` / `ACCEPTANCE_NOT_CONFIRMED` / `FLIP_RETEST_NOT_HELD` /
`CHAIN_EXPIRED` / `ANCHOR_BROKEN` / `ANCHOR_CONSUMED` / `ROTATION_REGIME` /
`INSUFFICIENT_BARS` / `DYNAMIC_LEVEL`

Three more codes exist for the **ADVISORY** paths (§9.13). They appear only on
`vwapPath.blockers` and **never** on a `promotion`:
`VWAP_DRIFT` / `VWAP_ACCEPTANCE_NOT_CONFIRMED` / `VWAP_RETEST_NOT_HELD`

### 9.2 Parameters (defaults are normative; env vars override)

| Parameter | Default | Env var |
|---|---|---|
| TICK | 0.25 | (fixed) |
| TOUCH_TOL (zone half-width) | `max(2.0, 0.10 x noiseFloor)` pt | `NQX_MSNR_TOUCH_PT` (replaces the 2.0 floor) |
| DISPLACEMENT_MULT | 1.0 | `NQX_MSNR_DISP_MULT` |
| MSS_LOOKBACK | 5 bars | `NQX_MSNR_MSS_LOOKBACK` |
| MSS_WINDOW | within 6 bars after the sweep | `NQX_MSNR_MSS_WINDOW` |
| RETEST_WINDOW | within 10 bars (shared by both chains) | `NQX_MSNR_RETEST_WINDOW` |
| FLIP_ACCEPT_BARS | within 3 bars after the break | `NQX_MSNR_FLIP_ACCEPT_BARS` |
| CHAIN_TTL | 15 bars from the sweep / break | `NQX_MSNR_CHAIN_TTL` |
| COMPLETE_TTL | 10 bars from the holding close | `NQX_MSNR_COMPLETE_TTL` |
| CONSUMED_TOUCHES | 3 body-touch **episodes** | `NQX_MSNR_CONSUMED` |
| ROTATION_WINDOW / threshold | 10 bars / ratio 0.5 (when signals >= 4) | `NQX_MSNR_ROT_WINDOW` |
| APLUS_DISP_MULT (R4 §9.12) | 1.3 | `NQX_MSNR_APLUS_DISP` |
| VWAP_DRIFT_MAX (R4 §9.13) | 3.0 pt | `NQX_MSNR_VWAP_DRIFT_MAX` |
| VWAP_PRIOR_BARS (R4 §9.13) | 2 closed bars on the far side | `NQX_MSNR_VWAP_PRIOR_BARS` |
| VP_OUTSIDE_BARS (R4 §9.14) | 2 closed bars outside the value area | `NQX_MSNR_VP_OUTSIDE_BARS` |

Everything is evaluated on **closed bars only**. A bar is closed when
`t + 180 <= epoch(priceAt)` (falling back to `at`); a forming final bar is
dropped before any of the scans below.

### 9.3 noiseFloor and zones

- `noiseFloor` = median `(h - l)` of the last 12 closed bars — the same
  definition as §8, by construction.
- Level zone = `[P - TOUCH_TOL, P + TOUCH_TOL]`.
- **Dynamic levels**: any label matching `/vwap/i`. No freshness, no chain
  scan, promotion is always `{allowed: false, blockers: ["DYNAMIC_LEVEL"]}`
  — a VWAP moves every bar, so a chain against a static snapshot price is
  undefined.
- Levels within **2.0pt** of each other merge into one. **The representative
  is the higher tier (lower number), not the first occurrence** (R4 §9.11);
  ties keep the first. Absorbed labels — including a displaced representative —
  are listed as `mergedWith`, and `confluence` = `1 + len(mergedWith)`.

### 9.4 freshness and side-specific anchoring

Support example below; resistance is the vertical mirror.

| Event | Definition | Transition |
|---|---|---|
| wick test | `l < P` and `c > P` | FRESH -> `WICK_TESTED` |
| body touch | `c` inside the zone | -> `BODY_TESTED`, one **episode** counted |
| break | `c < P - TOUCH_TOL` | -> `BROKEN` |
| reclaim | after BROKEN, `c > P + TOUCH_TOL` within 2 bars of the break | -> `RECLAIMED` |
| flip | a FLIP chain reaching FLIP_HELD on a BROKEN level | -> `FLIPPED` |
| consumed | episodes >= CONSUMED_TOUCHES | -> `CONSUMED` (terminal) |

**Body touches are counted as episodes, not bars.** An episode begins when a
close enters the zone from outside it; consecutive in-zone closes are the
same episode. Three bars resting on a level is one test, not three.

`anchorOk` is **per side**, because a broken level is not dead — it is an
anchor for the other direction (RBS/SBR):

| freshness | original-role side (SUPPORT->BUY / RESISTANCE->SELL) | flip side |
|---|---|---|
| FRESH / WICK_TESTED / BODY_TESTED / RECLAIMED | true | true |
| BROKEN | **false** | **true** |
| FLIPPED | false | true |
| CONSUMED | false | false |

### 9.5 SWEEP chain (BUY = support sweep; SELL mirrors it)

`NONE -> SWEEP_CANDIDATE -> SWEEP_CONFIRMED -> MSS_CONFIRMED -> RETEST_HELD`
(terminal PASS); `EXPIRED` if a TTL runs out.

| Stage | Mechanical definition |
|---|---|
| **sweep (1-bar)** | closed bar i: `l <= P - 1 tick` and `c > P`, with the level acting as support immediately before (the most recent close outside the zone was above it). sweepBar = i |
| **sweep (2-bar)** | bar i: `c < P - TOUCH_TOL` (break of a level that was support), then within **2 bars** a bar j: `c > P` (reclaim). sweepBar = j. Later than 2 bars means no chain, and freshness stays BROKEN |
| **displacement** | the reclaim bar's body `abs(c - o) >= DISPLACEMENT_MULT x noiseFloor` and directional (`c > o` for BUY). Satisfied gives SWEEP_CONFIRMED; otherwise it stops at SWEEP_CANDIDATE (`NO_DISPLACEMENT`) |
| **MSS** | within MSS_WINDOW bars after the sweep, a closed bar with `c > max(h)` of the MSS_LOOKBACK bars **preceding** the sweep (fewer than 5 available: 3 is enough; fewer than 3 is undecidable, `MSS_NOT_CONFIRMED`) |
| **held retest** | within RETEST_WINDOW bars after MSS, a closed bar j whose extreme **reaches the level itself** (`l <= P`) and closes back above it (`c > P`) gives RETEST_HELD |
| **chain reset** | a close `c < P - TOUCH_TOL` after the MSS destroys the chain; freshness becomes BROKEN |

### 9.6 FLIP chain (SELL = flipped support; BUY mirrors it)

`NONE -> FLIP_BREAK -> FLIP_ACCEPTED -> FLIP_HELD` (terminal PASS).

| Stage | Mechanical definition |
|---|---|
| **break** | closed bar b: `c < P - TOUCH_TOL` and the level was support immediately before (most recent close outside the zone was above) |
| **acceptance** | within FLIP_ACCEPT_BARS bars after b, one more close `c < P - TOUCH_TOL` — two closes outside the level is the doctrine's acceptance outside |
| **revisit & hold** | within RETEST_WINDOW bars after acceptance, a closed bar j whose extreme **reaches the level itself** (`h >= P`) and closes back below it (`c < P`) gives FLIP_HELD |
| **reset** | any close `c > P + TOUCH_TOL` after the break kills the chain — the flip failed and the level was reclaimed |

**Reach is measured against the level, never against the zone (2026-08-18,
settled).** The revisit bar's extreme must print the level price itself. An
earlier revision accepted `l <= P + TOUCH_TOL`, which at a wide noise floor
passed a bar that stopped short of the level: measured on 2026-08-18 00:31
(noise floor 20.5pt, TOUCH_TOL 2.05), a low of 30,255.00 against a level of
30,253.00 qualified by 0.05pt — less than one tick — and the chain read as
complete while the level had never been tested.

**Principle: the zone (P ± TOUCH_TOL) classifies — touches, breaks,
acceptance. It never decides reach.** Reach is `l <= P` / `h >= P`, which is
independent of TOUCH_TOL; the zone's remaining uses should keep scaling with
noise, and they do.

The rationale is the operator's own account of the 2026-08-17 losses: *the
pullback was never waited for*. This rule is that sentence made numeric — if
the retracement did not reach the level, the thesis has not been tested yet.
The 00:28 "hold" stopped 2pt above 30,253; when price finally did test the
level, at 00:42, the structure was already dead.

### 9.7 Both chains expire — including completed ones

- Incomplete chains expire CHAIN_TTL bars after the sweep/break
  (`CHAIN_EXPIRED`); `barsLeft` is always reported.
- **A completed chain also expires**: RETEST_HELD / FLIP_HELD go to EXPIRED
  once `barsSinceRetest` / `barsSinceHold` exceeds COMPLETE_TTL. A hold that
  closed 90 minutes ago is history, not a live setup.

Both BUY and SELL, and both chain types, are scanned for every level — never
pre-filtered by whether the level currently sits above or below price,
because after a sweep or a flip that relationship is inverted. Per level and
side, the most advanced SWEEP chain and the most advanced FLIP chain are
reported; either one completing satisfies the composition in §9.1.

### 9.8 rotation (the 2026-08-17 lesson, made numeric)

Over the last ROTATION_WINDOW+1 closed bars:

- **signal bar**: body `abs(c - o) >= 0.5 x noiseFloor`
- **negation**: the next bar closes beyond the signal bar's *open* in the
  opposite direction (a full body retrace)
- `signals >= 4` and `negations / signals >= 0.5` gives verdict `ROTATION`,
  which puts `ROTATION_REGIME` on every level's promotion. Otherwise `OK`.

### 9.9 Insufficient data is a refusal, not a pass

Fewer than 12 closed bars, no levels, or every level dynamic means every
promotion is false with `INSUFFICIENT_BARS`, and the summary line says so.
This is the **opposite** of the volatility gate's fail-open: a chain is
permitted once proven, so unprovable means not permitted.

### 9.10 Operating procedure

1. Write the cycle bundle, strip the BOM, then run `msnr_gate.py --summary`
   against it.
2. Put that one line **both** in the cycle report **and** at the end of the
   bundle's `watching` array before publishing — otherwise it never reaches
   the Telegram banner detail or the Mini App. Keep the structural narrative
   first in `watching`; the banner's second line shows only the first entry.
3. Before any W -> A/X promotion, run `--level <structure price S> --side
   buy|sell` and require `allowed: true` for that side. On false, write the
   blocker into `watching` and stay at W.
4. Then run `--card` and put its JSON into the bundle's `evaluation` key
   before publishing — that field is what feeds the Mini App's gate
   checklist and GATES board. Add `htf` / `entry` / `tp` by hand only when
   presenting a scenario, and **re-measure the serialized size afterwards**:
   over 4096 bytes the Worker drops the whole `evaluation` to null.
5. Also copy the promotion's `grade` onto the scenario payload. The app
   shows it on the order button and refuses to arm when the volatility band
   demands A+ and the grade is lower.

### 9.11 Level tier (R4)

Tier comes from the label, matched top-down; the first hit wins (= the
smallest tier when several would match). It drives both the dedupe
representative (§9.3) and the A+ grade (§9.12).

| tier | Covers | Pattern |
|---|---|---|
| 1 | Monthly/Weekly High-Low, All Time High | `monthly (high\|low)`, `weekly (high\|low)`, `all time` |
| 2 | Previous Day High/Low/Mid | `previous day`, `prev day` |
| 3 | Session highs/lows, VAH/VAL/POC, Weekly/Monthly Mid | `asia`, `london`, `new york`, `vah`, `val`, `poc`, `mid` |
| 4 | Pivots, CPR | `pp`, `r1/r2`, `s1/s2`, `cpr` |
| 5 | Clock opens, everything unmatched | `\d{2}:\d{2}`, `market open`, default |

Short tokens carry word boundaries so `pp` does not fire inside "Supply".
`mid` (not `mid range`) is deliberate: live labels read "Weekly Mid".

### 9.12 grade — A+ / A (R4, ENFORCED)

Every `allowed: true` promotion carries a grade; a blocked one carries
`null`. **A+ requires all three**:

1. the completed chain's `dispBody` >= `APLUS_DISP_MULT` x noiseFloor
   (SWEEP: the sweep bar's body; FLIP: the larger body of the break and
   acceptance bars),
2. `tier <= 2` **or** `len(mergedWith) >= 1` (confluence),
3. `freshness` in `{FRESH, WICK_TESTED, FLIPPED}`.

Anything else that passes is `A`. The volatility band 0.40–0.60 admits **A+
only**; `<= 0.40` admits any grade; `> 0.60` still admits nothing.

`FLIPPED` joined the set in R5. Every completed FLIP chain necessarily reads
`FLIPPED` (a flip requires the level to break first), so excluding it barred
the entire 8/12 winning pattern from A+ by construction — a proven defect,
not a judgement call. Repeated testing is still caught by `CONSUMED`, which
kills `allowed` outright.

### 9.13 vwapPath — VWAP reaction (R4, **ADVISORY ONLY**)

Never feeds `promotion`. Reported so the pattern can be measured before
anyone decides to arm on it.

- The window VWAP is recomputed from `bars3m`:
  `vwap_i = sum(hlc3_k * v_k) / sum(v_k)` for `k <= i`, anchored at the
  oldest supplied bar. Any bar missing volume disables the path.
- `drift = |vwap_last - bundle.vwap|`. Above `VWAP_DRIFT_MAX`, or when
  `bundle.vwap` is absent, the path is disabled with `VWAP_DRIFT`
  (fail-closed: an approximation that cannot be checked is not used).
- Chain (BUY = RECLAIM; SELL mirrors), all compared against **that bar's**
  `vwap_i`, with `tolv` = TOUCH_TOL:
  at least `VWAP_PRIOR_BARS` earlier closes on the far side, then
  `c > vwap + tolv` (`VWAP_BREAK`), another such close within
  FLIP_ACCEPT_BARS (`VWAP_ACCEPTED`), then within RETEST_WINDOW a bar with
  `l <= vwap` and `c > vwap` (`VWAP_HELD`). A close back across after
  acceptance kills it; CHAIN_TTL and COMPLETE_TTL apply as usual.

**Anchor (R5).** The series is computed over **every supplied closed bar**
(capped at 240), while every other scan — chains, freshness, rotation,
noiseFloor — still uses only the trailing 60. The monitor now supplies 140
bars (`data_get_ohlcv count=140`) purely to move the anchor closer to the
session open; the published feed is still trimmed to 60 by `NQX_FEED_BARS`,
so nothing downstream changes.

**Why.** With a 60-bar (3-hour) anchor the drift median across 108 evaluable
bundles was 11.09pt and only 20% landed at or under 3pt, so the path sat
disabled on four cycles in five. Raising the threshold would have hidden the
mismatch instead of fixing it. The 140-bar anchor is the fix; the 3pt
threshold is unchanged and still refuses an approximation it cannot verify.
**The new drift distribution is unmeasured** — the archive has only 60-bar
bundles. Record it on the first live session.

### 9.14 vpPath — value-area acceptance (R4, **ADVISORY ONLY**)

The 80% rule adapted to 3M. Requires both VAH and VAL in `snapshot.levels`
(searched across `label` and `mergedWith`); otherwise it is not evaluated.

At least `VP_OUTSIDE_BARS` closes outside an edge, then a close back inside
(`VP_RE_ENTRY`), then another inside close within FLIP_ACCEPT_BARS
(`VP_ACCEPTED`), whose `target` is the opposite edge. A close back outside
kills it; CHAIN_TTL applies.

**Measured caveat (2026-08-18).** Zero firings in 165 bundles: the only
cycles carrying VAH/VAL had 11 closed bars, below the 12-bar minimum. The
monitoring loop must publish VAH/VAL/POC into `snapshot.levels` (pass A step
5, `data_get_pine_lines study_filter="VP"`) or this path can never be
observed.

### 9.15 Known limitations

`MITIGATED` is not implemented. `FLIPPED` exists only as observed by the FLIP
chain above — a level therefore reads BROKEN (and blocks its original side)
for several bars before its flip completes. That lag is deliberate: the flip
is credited on the holding close, never on the break.

QML / OCL / A / V level generation and HVN/LVN classification remain out of
scope for the same reason as before: the current data source does not carry
them, and inventing them would violate the no-fabrication rule. `VP node`
style labels fall to tier 5 because the tier table does not name them.

## 10. Management plan — high-RR exits are declared at entry, then executed

Added 2026-08-19 (STRATEGY_EVOLUTION_R8.md). Closes the exit-side gap: the
doctrine requires TP1/TP2/TP3 ≥ 1.80R / 2.80R / 4.00R (nqx1-current.md
231-232), but §4's bundle transfer collapses `tp=TP1,TP2,TP3` to `target` =
TP1 only. TP2/TP3 are expressed here, as a declared management plan, not as
additional bundle fields.

### 10.1 Definition

```
High-RR trade = a trade whose entry presentation is accompanied, at the same
                time, by a management plan containing a segment with planned
                RR ≥ 2.8R (the doctrine's TP2 level).
```

Entry-side conditions (msnr_gate / volatility budget / CT_TREND / dayguard /
event blackout) are unchanged from a normal trade. Only the exit design
changes.

### 10.2 The two types

| Type | Structure | Applies when |
|---|---|---|
| **Split** | 2 contracts. 1st exits at TP1 (~1.8R structure); 2nd is a runner (no TP, shares the SL → move to breakeven once TP1 is hit → trail). | SL ≤ 26pt (2 × 26pt = $104 ≤ $105 per-account cap) |
| **Single-runner** | 1 contract, submitted with no TP (`order.py` already accepts a new order with TP omitted — verified). Move to breakeven when price *passes* the TP1-equivalent price → trail. | SL 26-52pt structures. Partial profit-taking is physically impossible with 1 contract, so the whole position runs. |

### 10.3 Mandatory 5-item management plan

An entry presentation for a High-RR trade that is missing any one of these
five items is invalid — the same rank as an incomplete CLAUDE.md §2
presentation (entry/SL/TP without the structure price and the two checks):

```
1. TP1 price (split = the 1st contract's actual TP / single-runner = the
   breakeven-move trigger price)
2. Breakeven-move condition ("on TP1 hit" is the default; state a reason if
   using anything else)
3. Trail method: follow NQX_DATA_CT_TRAIL, or an "amount to protect"
   (TRADING_CONTEXT §6 principle 1)
4. Runner final target: an HTF structure (tier ≤2 level / CT rail), given as
   a price. "Let it run as far as it goes" is not acceptable.
5. Time-out: if still open at session end (JST 04:00), close at market.
```

### 10.4 The plan is part of the entry, not a later amendment

The 5 items above are written **at the same time** as the entry
presentation. They cannot be added after the position is open — that would
be the post-hoc change CLAUDE.md §4 exists to prevent.

**Executing a plan declared in advance is not "moving the SL" or "extending
the TP."** It was approved the moment the plan was declared, and is
therefore **out of scope of CLAUDE.md §4's defaults** ("do not move the SL",
"oppose TP extension first"). Concretely:

- A pre-declared breakeven-move or trail step is not a stop-loss change.
- A pre-declared runner final target is not a TP extension — it was the
  target price from the start, not a stretch invented after the position
  showed profit.

Any operation **not** named in the plan, wanted while the position is open,
falls back into CLAUDE.md §4 (holding-position conduct) and §5 (reaction to
a missed move) territory — it needs the same scrutiny as any other
in-flight change.

### 10.5 TP reachability — runner time basis

CLAUDE.md §2's TP-reachability check (`|TP−Entry| ÷ CT_ATR ≤ 4.0 bars`) is
built for a single TP. When the runner final target (item 4 above) exceeds
4.0 bars, the presentation must also carry a numeric time justification:

```
remaining session time ≥ target distance ÷ CT_ATR × 15 minutes
```

A final target that does not clear this bar gets pulled back to the nearer
structure (tier ≤2) instead.
