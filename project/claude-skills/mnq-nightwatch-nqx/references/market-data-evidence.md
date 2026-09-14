# Market Data Evidence Contract

## Contents

1. Evidence labels
2. Screenshot contract
3. OHLC extraction mode contract
4. Freshness and provenance

## 1. Evidence labels

Classify every claim you make into exactly one of these labels. Never promote
`INDICATOR_CLAIM` to `OBSERVED` — an indicator's displayed score, probability, or
state is never a direct market fact, no matter how confident it looks.

| Label | Meaning |
|---|---|
| `OBSERVED` | Confirmed directly from an image or numeric data — a price label, an axis tick, a candle whose OHLC is directly readable. |
| `INDICATOR_CLAIM` | A state, probability, score, or label an indicator overlay displays (SwingArm pressure score, RSI state, a "Bounce Probability" readout, etc.). |
| `INFERRED` | An interpretation derived from combining multiple observations. |
| `ASSUMED` | An explicit assumption made to continue the analysis; must be stated as such. |
| `UNREADABLE` | Something is visibly present but cannot be read precisely (a blurred label, an occluded candle). |
| `MISSING` | The evidence itself does not exist in what was supplied. |

Carry these labels into narrative text (e.g. "OBSERVED: ...", "INFERRED: ...") so a
reader can audit which class each claim belongs to, the same discipline
`C.pm` (participant map) already requires in the NQX/1 contract.

## 2. Screenshot contract

- For every image, confirm: symbol, timeframe, displayed time, price, and whether the
  right-most bar is a closed bar or still forming. State whichever of these is not
  determinable rather than guessing.
- Multiple timeframes almost never show the exact same instant. Keep each frame's
  price as its own asynchronous snapshot (`M.ps` — per-frame snapshot prices).
  **Never average cross-timeframe prices into a single "current price."**
- Never complete an unreadable digit. If a price is partially legible, treat it as
  `UNREADABLE`, not as a best guess.
- Screenshot-only analysis uses `qm=H` (HEURISTIC) throughout the resulting NQX
  packet — this is a Nightwatch/NQX-wide quality-mode constraint, not a UI cosmetic.
- Never place an unmarked estimate into a numeric NQX field that requires an exact
  price. Outside of the projection-shape display extension (which permits and
  requires `~`), use `MISSING`, omission, or a `~`-prefixed value in analysis prose —
  never a bare guessed number.

## 3. OHLC extraction mode contract

This is the contract for the `OHLC_EXTRACT` request mode (a single chart, a single
timeframe, producing exactly one `O|c=` line) — and is also the safety discipline
that the Nightwatch "OHLC / 15M CANDLE EXTRACT ONLY" uplink mode
(`GPT_OHLC_EXTRACT_MODE_DIRECTIVE.md`) uses for its own prompt.

- Target one chart image, one timeframe, per extraction.
- Emit up to the most recent 12 fully closed bars, oldest first.
- **Exclude the forming/incomplete rightmost bar** — never guess its close.
- If you cannot confidently read at least 3 bars, output `O|c=MISSING`. Do not pad
  with invented bars to reach a round number.
- Never reconstruct OHLC from a band, cloud, projection shape, or volume profile —
  those are not candle geometry.
- Never derive OHLC by guessing, interpolating between two visible candles, or
  averaging.
- A bar qualifies when open/high/low/close are directly readable from candle
  geometry against either an explicit price label or the chart's own axis/grid
  lines — never estimated from a colored fill, a covered candle, an indicator
  overlay, or a cloud/band.
- **When a price is read against a grid line rather than an explicit label** (e.g.
  a 1H chart with 20pt gridlines and no per-bar price labels), prefix that specific
  price with `~` to mark reduced precision, e.g.
  `07:00,~28720,~28742,~28690,~28700`. This is still an `OBSERVED` bar — it came
  from the chart — the `~` only flags that the reading is grid-aligned rather than
  an exact label. Do not silently present a grid-line reading as an exact number,
  and do not use this `~` to smuggle in a value you are actually guessing at
  (candle silhouette alone, without any grid/axis correspondence, is still
  `UNREADABLE`, not a `~` estimate).
- Verify per bar before emitting: `HIGH >= max(OPEN,CLOSE)`, `LOW <= min(OPEN,CLOSE)`,
  `HIGH >= LOW`.
- Output is exactly one line, `O|c=...` or `O|c=MISSING`. Never let this mode
  overwrite an entire NQX packet — it produces or updates the `O` record only. When
  merging into an existing packet, place `O` between `E` and `G` per the NQX/1
  record order (fall back to right after `Z`, then `M`, then the `!NQX/1` header,
  whichever is present, if `E` is absent).

Canonical transport form for the line you emit (see
[source-precedence.md](source-precedence.md) §2 for the comma/pipe distinction):

```text
O|c=TIME,OPEN,HIGH,LOW,CLOSE[,VOLUME];TIME,OPEN,HIGH,LOW,CLOSE[,VOLUME]
```

Escape a literal comma inside a value as `\,` and a literal pipe as `\|`. Semicolons
separate bars; never use a semicolon inside one bar's fields.

Final self-check before returning `OHLC_EXTRACT` output: exactly one line starting
with `O|c=`; every bar has 5 or 6 comma-separated fields; geometry checks above all
hold; bars are in chronological order; nothing was invented or interpolated.

## 4. Freshness and provenance

For every piece of external data (VIX, event times, gamma, options context, any
reference feed), retain as much of the following as is knowable:

- `source`
- `observed_at` / `as_of`
- `timezone`
- `delayed` / `realtime` / `unknown`
- `freshness`
- `transformation` (any conversion or derivation applied)

You do not have access to a realtime MNQ feed inside this skill. Never act as though
you do. Treat only user-supplied images or explicitly stated, dated data as
evidence. When freshness is unknown, say `UNKNOWN` rather than assuming `LIVE`.
