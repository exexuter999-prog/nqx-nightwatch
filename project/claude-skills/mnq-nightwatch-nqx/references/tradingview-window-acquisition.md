# Nightwatch live TradingView window acquisition

Use this contract only for the repository at
`C:\Users\exexu\Downloads\nq-nightwatch-claude-code-handoff` and the operator's
fixed MNQ TradingView layout.

## Window invariant

The visual layout has two chart panes and three evidence regions:

- pane 0, left: `CME_MINI:MNQ1!`, 15 minutes, candles and context studies;
- pane 1, right top: the same MNQ symbol, 3 minutes, execution studies;
- pane 1, right bottom: `CVD Unified`, an indicator subpane, not chart pane 2.

Start with `pane_list`. Do not change the symbol. Repair only a timeframe
mismatch. A cycle with a missing pane, wrong symbol, wrong resolution, or no
visible CVD Unified study is blocked as an acquisition defect.

**`chart_get_state` does not prove which pane `data_get_ohlcv` reads.** Measured
2026-08-25: consecutive `chart_get_state` calls returned resolution `"3"`, then
`"15"`, then `"3"`, then `"45"` while `data_get_ohlcv` kept returning 900-second
bars throughout. `pane_list` was stale in the same window. Treat both as hints.

The only trustworthy resolution check is **the bar timestamps themselves**: the
dominant difference between consecutive `time` values must equal the timeframe
you believe you are on (180s for 3m, 900s for 15m). Verify that before writing
`bars3m.json`/`bars15m.json`, and if it does not match, `chart_set_timeframe`
and refetch rather than saving the file. `tv_snapshot.normalize_bars` enforces
the same rule and blocks the cycle, but by then the raw file is already wrong.

## Fixed order

**R51 (2026-09-01): `tv_fetch.py` performs this order; `nqx_cycle.py` calls it at
the start of every cycle. The agent does NOT run these steps by hand** — its whole
job is `python nqx_cycle.py`. Hand transcription of MCP output broke the 240-second
raw freshness window (a full cycle took ~20 minutes; it now takes 36 seconds) and
was the same step that drove `rangeAnchor`/`peers`/`po3`/`cvdMeta` capture to 0%
across 403 cycles. This section remains as the contract definition.

Acquire context before triggers. This prevents choosing a 3-minute direction and
then fitting 15-minute levels to it.

1. **WINDOW_LAYOUT** — save `pane_list` verbatim as `pane_layout.json`.
2. **CONTEXT_15M** — focus pane 0; save state as `chart_state_15m.json`, 60 OHLCV
   bars as `bars15m.json`, and all study values as `study_15m.json`. **The Pine
   reads belong here too**, because `data_get_pine_*` only sees studies on the
   focused pane and pane 1 carries nothing but `CVD Unified`: session labels as
   `pine_labels.json` (`study_filter="Sessions"`, `max_labels=100`), SMT lines as
   `smt_lines.json` (`study_filter="SMT"`, `verbose=true`), SMT labels as
   `smt_labels.json` (`study_filter="SMT"`, `max_labels=4`). Reading them while
   pane 1 is focused returns `study_count: 0`, which empties the required
   `pine_labels.json` and blocks the whole cycle on zero levels (measured
   2026-09-01). Zero results are a focus bug first, `SMT_SOURCE_MISSING` second.
3. **BARS_HTF_DUE** — run `python htf_context.py --raw-dir .secrets/tv_raw` and
   fetch only due 45m/1h/4h/1D frames directly from pane 0. After the last due
   frame, restore pane 0 to 15m, verify it, and overwrite `chart_state_15m.json`.
4. **RANGE_ANCHOR** — resolve a named session or refreshed directional HTF leg with
   `rangeTf/rangeStart/rangeEnd/anchorType/freshness/high/low`. Never use a
   rolling 3-minute high/low as an OTE anchor.
5. **EXECUTION_3M** — focus pane 1; save state as `chart_state.json`, 240 OHLCV
   bars as `bars3m.json`, study values as `study_3m.json`, and quote as
   `quote.json`. Full bars are required here; a statistical summary cannot feed
   closed-bar chains. Session labels and lines are **not** read here — see step 2.
6. **CVD_INITIAL** — save the CVD table as `cvd_table.json`. Direction exists only
   when the study EMA relation and table verdict agree. If absent/stale/stalled,
   reacquire CVD fields once; a second unhealthy result caps grade at A, not FLAT.
7. **SMT_PEERS** — the raw lines and labels were saved in step 2 (pane 0). Require
   same timestamp, same sessionId, fresh peer/observation, and lifecycle not
   BROKEN. If the study is genuinely absent from pane 0, save an empty tool result
   and report `SMT_SOURCE_MISSING`; do not infer SMT. An empty result read from
   pane 1 is a focus bug, not a missing source.
8. **EVENT_CONTEXT** — retain event state, source, and checked-at time without
   rewriting market fields.
9. Restore focus to pane 0 and verify MNQ/15m.
10. Run only `python nqx_cycle.py`. Do not hand-write a bundle or publish the raw
    input directly.

Save MCP response JSON verbatim, UTF-8 without semantic rewriting. The project
receipt freezes file hash, modification time, freshness, and window identity.

## Evidence-to-decision wiring

| Evidence region | Required decision use |
|---|---|
| 15m SwingArm/structure | CT direction, CT ATR/noise scale, trail/rails, range anchor, structural invalidation |
| Direct 45m/1h/4h/1D bars | `htfContext`, alignment/conflict, fresh HTF range; never synthesize from 3m |
| 3m closed OHLCV | MSNR sweep/flip chain, FVG/IFVG lifecycle, rotation, noise floor, entry reach and stop geometry |
| VP/session labels | VAH/VAL/POC, DOL, named range, VP80 candidate and structural targets |
| CVD study + table | directional confirmation, health/retry state, A+ cap |
| SMT lines/labels/alert | fresh divergence confirmation or penalty; never a timeless background note |
| Other indicator labels | `INDICATOR_CLAIM`; score only when the engine has an explicit mapping and independent price evidence |

Every acquired item must appear in `strategyEvidence`/`evaluation`, or be logged
as missing, stale, contradictory, not applicable, or evaluated-with-zero-effect.
An unconsumed observation must not be cited as a reason for the selected model.

## Output boundary

Use the same evidence set to compare LONG, SHORT, and FLAT, then let the project
select one primary model. This skill does not arm AUTO, call `order.py --confirm`,
or bypass the project's acquisition, broker, event, risk, or session gates.
