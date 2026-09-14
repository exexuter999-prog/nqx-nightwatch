# NQX/1 — NQ eXecution Exchange Language

Transport module. NQX carries a **completed** analysis in fewer tokens. It is not an
analysis method and must never change a conclusion. If a field was decided, it is
serialized; compression is achieved by removing repeated labels and prose, never by
removing information.

## Design guarantees

- Lossless at the semantic-field level: every market, quant, audit, and scenario field
  has a code.
- No prediction compression: `Confidence` stays setup quality. Probability and EV stay
  `N/A` unless calibrated data exists.
- Versioned: packets start with `!NQX/1`. A receiver that does not know the version
  **rejects the packet**; it never guesses.
- Auditable: scenario count, mutual exclusion, invalidation order, R:R, expiry, event
  handling, and outcome definition remain explicit.
- Forward-compatible: unknown keys are retained and reported, never silently dropped.
- Backward-compatible: the receiver still accepts the verbose `Key: Value` packet.

## Lexical rules

```ebnf
packet   = "!NQX/1", newline, { line } ;
line     = tag, { "|", key, "=", value }, newline ;
tag      = "D" | "M" | "C" | "Z" | "E" | "O" | "G" | "S1" | "S2" | "S3" ;
key      = letter, { letter | digit | "_" } ;
value    = { any-char-except-unescaped-newline-or-pipe } ;
```

- Escape `|`, `\`, and a literal newline as `\|`, `\\`, and `\n`.
- Range: `29560..29645`. Axis-estimated price or range: prefix `~` → `~29620..29650`.
- Commas separate lists; semicolons separate independent clauses.
- Use `MISSING`, `N/A`, and `U` rather than inventing data. Never convert any of these
  three into a number or a fact.

## Level aliases

`D` defines packet-local reusable values, referenced as `$NAME`.

```text
D|UH=~29620..29650|P=~29470..29500|NH=~29750
```

Aliases expand before parsing and may appear in narrative fields and scenarios. An
alias that is referenced but never defined is an **error**. An alias that resolves
into itself, directly or through a chain, is an **error**.

## Records

### M — market, metadata, and quant gate

```text
M|sy=<symbol>|tf=<frames>|at=<JST time>|ss=<session>|sf=<source frames>|dq=<H/M/L>|qr=<quality reason>|px=<price>|ps=<per-frame snapshot prices>|un=<uncertainty>|ht=<HTF storyline>|rg=<regime>|vo=<volatility>|mo=<momentum>|vx=<VIX>|vxc=<change>|vxa=<as-of>|vxs=<source>|vxf=<freshness>|vxr=<reference>|qm=<H/E/C>|ds=<I/E/P/A>|na=<N/A field list>|rm=<regime metrics>|sn=<sample>|pe=<probability event>|br=<base rate>|pp=<posterior>|ui=<interval>|bp=<break-even>|ge=<gross EV>|cm=<cost model>|ne=<net EV>|ce=<conservative EV>|sz=<sizing>|rb=<robustness>|mr=<model risk>
```

- `dq`: `H` HIGH, `M` MEDIUM, `L` LOW
- `qm`: `H` HEURISTIC, `E` EMPIRICAL, `C` CALIBRATED
- `ds`: `I` INSUFFICIENT, `E` EXPLORATORY, `P` PROVISIONAL, `A` ADEQUATE
- `rg`: `TR` TREND, `BA` BALANCE, `TX` TRANSITION, `EX` EXPANSION, `EC` EVENT
  COMPRESSION, `ER` EVENT REPRICING, `MX` MIXED
- `na`: comma-separated keys known to be unavailable, from
  `rm,sn,pe,br,pp,ui,bp,ge,cm,ne,ce,sz`
- `vxf`: `LIVE`, `DELAYED`, `STALE`, or `UNVERIFIED`. If `vx` is present, `vxa`,
  `vxs`, and `vxf` are required. `vxr` identifies the source page or feed.

VIX is volatility context only. It is not dealer positioning and must never be used
to infer gamma sign, gamma-flip levels, or order-flow direction.

Nightwatch resolves external context in this order: packet fields, an operator- or
licensed-feed object at `window.NIGHTWATCH_CONTEXT_FEED`, then the bundled dated
reference snapshot. It performs no automatic page scraping. A feed object uses:

```js
window.NIGHTWATCH_CONTEXT_FEED = {
  vix: { value: 18.77, change: 2.04, asOf: "2026-07-17T20:15:01Z",
         source: "LICENSED FEED", freshness: "DELAYED", ref: "feed-id" },
  events: [{ at: "2026-07-21T21:30:00+09:00", label: "BEA release",
             status: "VERIFIED", source: "BEA", ref: "official-url" },
           { at: "2026-07-28/29 ET",
             windowStart: "2026-07-28T00:00:00-04:00",
             windowEnd: "2026-07-29T23:59:59-04:00",
             label: "FOMC meeting · decision time unverified",
             status: "DATE VERIFIED / TIME UNVERIFIED",
             source: "FEDERAL RESERVE", ref: "official-url" }]
};
```

`windowStart` and `windowEnd` are optional ordering windows for a multi-day event
whose exact release time is not verified. They must not be displayed as an exact
event time and do not upgrade the event to `VERIFIED`.

Reference snapshots always retain their printed source time and may render as
`STALE`; they are context, not a live quote service.

When `qm=H`, omitted `ds`, `rb`, and quantitative statistics decode deterministically
as `INSUFFICIENT`, `NOT TESTED`, and `N/A`. This is a semantic default, not missing
information. **`qm=H` with a numeric `pp`, `br`, `ge`, `ne`, `ce`, or `sz` is an
error** — a screenshot cannot produce a calibrated number.

### C — adversarial context

```text
C|ac=<auction>|al=<Alchemist/MSNR>|lm=<liquidity map>|pm=<participant map; OBSERVED vs INFERRED>|bu=<bull case>|be=<bear case>|ba=<balance/FLAT case>|dh=<dominant hypothesis>|de=<disconfirming evidence>|ci=<commander intent>
```

### Z — price map

```text
Z|dz=<decision zone>|poc=|vah=|val=|s=|r=|rbs=|sbr=|qml=|ocl=|a=|v=|la=<liquidity above>|lb=<liquidity below>|tm=<tactical price map>
```

Omit an unsupported optional level. Never write a fabricated value into it.

### E — event intelligence

```text
E|ev=<nearest material event or NONE_WITHIN_30M>|src=<VERIFIED/UNVERIFIED>|rt=<release YYYY-MM-DD HH:MM JST or N/A for sentinel>|rule=<global handling>|at=<calendar verification YYYY-MM-DD HH:MM JST>|ref=<primary official reference>
```

Event times are converted to JST only when verified against a primary official
calendar. `rt` is the release timestamp; `at` is when that calendar was checked.
`src=VERIFIED` requires `at` and `ref`. An unverified time is never estimated.
Secondary calendars may be noted as context but cannot upgrade an event to
`VERIFIED`.

For directional `WG-H1`, the event scan is fail-closed: `at` must be no more than
30 minutes old and not future-dated, `ref` must name the primary source, and the
nearest material event must have an exact `rt`. A release inside the inclusive
`M.at ± 30 minutes` window blocks every scenario. When a fresh official-calendar
scan finds no material event in that window, encode exactly
`ev=NONE_WITHIN_30M|rt=N/A`; `MISSING`, an unverified scan, or a stale past event
cannot authorize a trade.

### O — exact or axis-estimated observed OHLC only

```text
O|c=20:48,30018,30038,30008,30031;20:51,30031,30047,30022,30041
```

The canonical NQX transport form for each bar is
`TIME,OPEN,HIGH,LOW,CLOSE[,VOLUME]`; semicolons delimit bars. The receiver converts
each comma-delimited transport row to its strict internal
`TIME|OPEN|HIGH|LOW|CLOSE[|VOLUME]` form before calling `parseCandleRow`. A literal
pipe inside an NQX value must therefore be escaped as `\|`; an unescaped pipe remains
the record-field delimiter. Canonical serializers must emit the comma form shown
above.

These are already-observed market bars with a real bar time. Model output,
reconstructed candles, interpolated prices, and scenario bars are forbidden in `O`.
Unavailable → `O|c=MISSING`.

Any of a bar's four prices (`OPEN`, `HIGH`, `LOW`, `CLOSE`) may individually carry a
leading `~` when it was read from an axis or grid line rather than an explicit price
label — for example `O|c=07:00,~28720,~28742,~28690,~28700`. This is still an
OBSERVED bar (it came from the chart, not from a model or interpolation); the `~`
only signals reduced precision on that specific value. A bar with any `~`-marked
price is reported to the operator as approximate, and its True Range still
participates in `SUPPLIED OHLC RANGE` range-unit calculations — an approximate
observed range remains preferable to falling back to `MAPPED LEVEL SPACING` when
actual bars are visible on the chart. `~` on an `O.c` price never means "estimated by
a model"; that is what `S.pc` (§ Optional display-only scenario tape) is for, and the
two remain separate data classes on the same price scale (see Required safety
invariant 9-10).

### G — global decision gate

```text
G|n=<0..3>|ip=<immediate posture>|mx=<mutual exclusion>|io=<invalidation-order summary>|nt=<NO TRADE reason, only when n=0>|nr=<next review>|rn=<risk note>
```

`n` must equal the number of `S` records present. A mismatch is an error.

### S1..S3 — scenarios

```text
S1|n=<Japanese name>|d=<L/S/N>|st=<state>|su=<setup>|ha=<alignment>|lt=<level type>|zq=<zone quality>|fr=<freshness>|lq=<liquidity event>|ms=<MSS>|sh=<structure shift>|em=<entry mode>|cf=<0..100>|gr=<verified grade>|sp=<calibrated probability only>|tr=<trigger>|co=<confirmation>|en=<entry range>|sl=<stop>|tp=<TP1,TP2,TP3>|rr=<R1,R2,R3>|iv=<invalidation>|od=<outcome definition>|vu=<valid until JST>|eh=<event handling>|fm=<failure mode>|sd=<stand down>|ef=<evidence for>|ea=<evidence against>|me=<missing evidence>|nc=<next confirmation>|cc=<labeled causal chain>|wp=<what proves wrong>|do=<dominance>|fc=<confidence factors>|no=<note>|pk=<SCENARIO/MODEL>|pb=<projection basis>|pc=<projection tape>|pu=<projection uncertainty>
```

Controlled codes:

- `d`: `L` LONG, `S` SHORT, `N` NEUTRAL
- `st`: `W` WATCH, `A` ARMED, `T` TRIGGERED, `C` CONFIRMED, `X` ACTIVE, `P` PARTIAL,
  `B` BE, `D` CLOSED, `I` INVALIDATED
- `su`: `BC` BREAKOUT CONTINUATION, `PC` PULLBACK CONTINUATION, `RR` RANGE REVERSION,
  `SR` SWEEP REVERSAL, `FA` FAILED AUCTION
- `ha`: `A` ALIGNED, `N` NEUTRAL, `M` MIXED, `C` CONFLICT
- `zq`: `S` STRONG, `M` MODERATE, `W` WEAK, `U` UNKNOWN
- `fr`: `F` FRESH, `WT` WICK TESTED, `BT` BODY TESTED, `M` MITIGATED, `C` CONSUMED,
  `B` BROKEN, `FL` FLIPPED, `R` RECLAIMED, `U` UNKNOWN
- `em`: `R` STANDARD RETEST, `C` CONSERVATIVE, `A` AGGRESSIVE, `N` NO TRADE
- Omitted `cf`, `gr`, `sp`, `zq`, `fr` decode as `AUTO`, `AUTO`, `UNVERIFIED`,
  `UNKNOWN`, `UNKNOWN`. Emit them only for a verified non-default value.

`od` (Outcome Definition, added in this build) states the success test in Japanese,
for example: `od=Fill後、Valid UntilまでにSLより先にTP1到達=SUCCESS`. `od` is required
on every scenario. Older receivers that do not know `od` retain it as an unknown key
rather than dropping it, so the extension stays backward-compatible.

### Optional display-only scenario tape

`pk`, `pb`, `pc`, and `pu` are an all-or-none display extension on an `S` record.
They do not alter the scenario decision. Example:

```text
S1|...|pk=SCENARIO|pb=観測レンジ内での条件付き反発経路|pc=+1,~28910,~28928,~28896,~28922;+2,~28922,~28945,~28912,~28938|pu=方向と値幅は未検証;イベントで無効化
```

- `pk` is `SCENARIO` or `MODEL`; `OBSERVED`, `LIVE`, and `ACTUAL` are forbidden.
- `pc` contains 1–12 sequential relative bars. Each is
  `+N,~OPEN,~HIGH,~LOW,~CLOSE`. `+N` is not clock time.
- Every projected price carries `~`, even if a model emitted more decimals.
- `pb` states the model/evidence basis and `pu` states limitations and invalidators.
- The receiver renders these bars in a physically separate `SCENARIO / SIMULATED`
  lane. They are **not market data**.
- Scenario bars are forbidden as inputs to current price, level discovery, trigger or
  confirmation state, confidence, grade, probability, R:R, stop placement, candidate
  detection, event logic, or any validator decision.
- If `pc` is absent, the receiver draws no future candles. It never auto-completes a
  path.

## Built-in semantic macros

The receiver expands these into full Japanese operator text:

```text
^WAIT(zone,upper,lower)  current zone, wait instruction, next-30-minute watch
^ACC(price,tf)           body-close acceptance above price
^REJ(price,tf)           body-close rejection below price
^RT(range,tf)            successful retest and hold
^SWP(range,BSL-or-SSL)   liquidity sweep
^FLAT(time)              close all positions by JST time
^NOENT(from,to)          no new entries during JST window
```

Macros are optional. Free Japanese remains valid whenever a macro would lose nuance.

## Required safety invariants

1. Entry bands must not overlap. If they do, `G.mx` must order the triggers and name
   the scenario that becomes `STAND DOWN`.
2. `LONG`: `SL < invalidation < entry low`. `SHORT`: `entry high < invalidation < SL`.
   Equality with SL is permitted only when explicitly declared.
2a. Invalidation should sit meaningfully inside the stop, not immediately adjacent to
    it. The validator flags `SL_INVALIDATION_TOO_CLOSE` when the invalidation-to-SL gap
    is under 15% of the entry-to-SL risk distance, since a close-based invalidation
    may not confirm before the hard stop is physically touched.
2b. The validator additionally flags `SL_STRUCTURAL_DISTANCE` when the hard stop
    does not clear the nearest declared S/R/RBS/SBR/QML/OCL level (on the
    losing side of the trade) by a reasonable buffer. With at least three valid
    `O.c` bars, it mirrors the receiver's `SUPPLIED OHLC RANGE`: the simple mean
    of finite positive True Range values from the trailing 12 accepted bars.
    Rejected rows generate `OBSERVED_CANDLE_REJECTED` warnings and do not enter
    that calculation. With fewer than three valid bars, it mirrors the receiver's
    `MAPPED LEVEL SPACING` fallback: median distance from `M.px` (or the
    decision-zone midpoint) to declared structural levels, scaled by `0.42`.
2c. When `G.rn` declares `POLICY=WG-H1`, the legacy two-path fallback is replaced
    by the versioned wide-geometry gate. A directional scenario is eligible only
    for `M.rg=TR|EX` with `S.su=BC|PC`; it requires 15 exact accepted `O.c`
    bars (no `~`-estimated OHLC). Their timestamps must use strict `HH:MM` on
    the `M.at` date or full `YYYY-MM-DD HH:MM JST`, be unique and strictly
    increasing at exactly `M.tf` cadence, and end on the latest bar already
    closed at `M.at`. Duplicate, reversed, off-cadence, stale, or unclosed bars
    cannot authorize WG-H1. `M.px`, `S.en/sl/tp/iv`, and every mapped `Z`/`TM` price used
    by the policy must also be canonical bare decimal tokens (or `low..high`
    ranges where ranges are allowed). `~`, `≈`, labels, prefixes, suffixes, and
    prose are never stripped into executable WG-H1 prices. Every such price must
    also remain within `0.5x..1.5x` of the latest exact `O.c` close; this is a
    defensive decimal/unit guard, not an edge parameter. The policy uses
    the simple mean of the trailing 14 True Ranges as `U`. The hard stop must clear
    the nearest losing-side `S/R/RBS/SBR/QML/OCL` outer edge by
    `max(0.50U, 2pt)`, while entry-midpoint risk is at least `1.50U`. No mapped
    spacing fallback is allowed.
2d. `WG-H1` requires three successive declared `Z`/`TM` roadblocks at no less than
    `1.80R`, `2.80R`, and `4.00R`; executable prices must align to the `0.25pt`
    NQ/MNQ tick. Its note must declare size-down/MNQ discipline, no stop widening
    after fill, and no mechanical breakeven before `+1.25R`. It also requires the
    fresh verified `E.rt/E.at/E.ref` event-clock contract above; a release inside
    `M.at ± 30 minutes` blocks WG-H1. These constants are a preregistered
    heuristic, not a validated edge.
2e. The receiver's operator-only `WG SESSION RISK CAP` is not encoded as a
    screenshot-derived `M.sz`. It sizes MNQ with the `R + 8pt` stress profile and
    blocks local `EXECUTE` when absent or when one MNQ exceeds the cap.
3. `rr` is computed from the entry midpoint. TP1 below `1.00R` requires a downgrade
   note in `no`.
4. Every scenario requires `vu`, `eh`, `fm`, `sd`, and `od`.
5. Screenshot-only packets use `qm=H`; unavailable quantitative fields decode to
   `N/A`. `na=` may declare the null set explicitly.
6. Unknown proprietary indicator labels stay literal or `MISSING`. Never expand an
   undefined abbreviation.
7. A confidence score is never a probability. `cf=75` does not mean 75%.
8. `ASSUMED` anywhere in `cc` caps `cf` at 64 and forbids `do=PRIMARY`.
9. `O.c` contains observed bars only. Scenario/model bars can appear only in `S.pc`.
   An `O.c` price may carry `~` to mark an axis/grid-line-estimated reading of a
   still-observed bar; this is a different meaning from `~` on `S.pc`, which marks a
   display-only projection and never an observed value.
10. Every `S.pc` price begins with `~`; projections are display-only and cannot alter
    a gate, audit, decision, or outcome.
11. VIX requires source, as-of time, and freshness and is never a gamma proxy.
12. `E.src=VERIFIED` requires a primary official `E.ref`, an explicit verification
    time in `E.at`, and either exact release time in `E.rt` or the canonical
    `NONE_WITHIN_30M` / `rt=N/A` sentinel.

## Minimal valid packet (NO TRADE)

```text
!NQX/1
M|sy=MNQ1!|tf=3M/15M/45M|at=2026-07-14 17:10 JST|ss=CME|sf=3M,15M,45M|dq=M|px=29574.75|qm=H|na=rm,sn,pe,br,pp,ui,bp,ge,cm,ne,ce,sz
Z|dz=~29560..29645|r=~29620..29650|s=~29470..29500
E|ev=CPI|src=VERIFIED|rt=2026-07-14 21:30 JST|rule=21:00以降新規禁止;21:15全決済|at=2026-07-14 20:55 JST|ref=https://www.bls.gov/schedule/
G|n=0|ip=^WAIT(~29560..29645,~29645,~29600)|mx=NOT REQUIRED|io=NOT REQUIRED|nt=排他トリガー未確定|nr=29645上の受容または29600下の拒否|rn=条件付き分析であり利益や勝率を保証しない
```

## Validation

Before any packet leaves this skill, run:

```bash
python validate_nqx.py packet.nqx
```

Exit code `0` = clean. Any error must be repaired and the packet re-validated. An
erroring packet is never registered in Nightwatch.
