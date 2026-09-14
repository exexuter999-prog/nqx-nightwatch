# Event and VIX Contract

## Contents

1. Event research
2. VIX

For gamma-specific rules (provenance gate, setup gate matrix, large-OI level
handling), see [regime-gamma-policy.md](regime-gamma-policy.md) §6 — gamma and VIX
are governed together but VIX itself is never a gamma proxy (see §2 below).

## 1. Event research

When time-sensitive event information matters to the analysis, research it at
runtime if you can. Prefer primary sources:

- Federal Reserve
- BLS (Bureau of Labor Statistics)
- BEA (Bureau of Economic Analysis)
- U.S. Treasury
- CME Group
- Cboe
- The target company's own official investor-relations page

Attach to every event:

- event name
- exact time
- timezone
- `VERIFIED` / `UNVERIFIED`
- primary source URL
- `checked_at`
- expected impact window

**Never mark an event `VERIFIED` without both a primary URL and an exact time** —
this mirrors the NQX/1 `E.src=VERIFIED` requirement (`E.at` + `E.ref` both present;
see [nqx1-current.md](nqx1-current.md) §2). If you cannot reach the web, do not
pretend you checked — mark the event `UNVERIFIED` or `MISSING` honestly. A
multi-day event window with an unverified exact release time (e.g. "FOMC meeting,
decision time unverified") may carry `windowStart`/`windowEnd` ordering bounds, but
that never upgrades it to `VERIFIED` and must never be displayed as an exact event
time.

## 2. VIX

- Record VIX value, change, as-of time, source, and freshness as separate fields —
  never collapse them into a single unlabeled number.
- Explicitly label a delayed value as delayed; never present a stale reading as
  live.
- **Never decide MNQ direction from VIX alone.** VIX is stress/volatility context,
  not a directional signal.
- **Never infer gamma from VIX.** VIX cannot substitute for a gamma regime
  determination — see [regime-gamma-policy.md](regime-gamma-policy.md) §6 for what
  actually qualifies as gamma evidence.
- VIX's proper role is limited to: position-sizing context, event-risk context, and
  general volatility-regime context. It does not by itself change a scenario's
  trigger, invalidation, or grade.

In NQX/1 terms, this maps to `M.vx/vxc/vxa/vxs/vxf/vxr` — all of `vxa`, `vxs`, `vxf`
are required whenever `vx` is present, and `vxf` must be one of `LIVE`, `DELAYED`,
`STALE`, `UNVERIFIED` (see [nqx1-current.md](nqx1-current.md) §2).
