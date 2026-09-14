# Immutable Evidence Ledger

## Contents

1. Purpose and invariants
2. Event schema
3. Event lifecycle
4. Hash chain
5. State and counting rules
6. Frozen setup identity
7. Promotion gate

## 1. Purpose and invariants

Use one append-only event ledger. Append a `DECISION` before its outcome is known,
then append exactly one `RESOLUTION` or `CANCEL`; never edit the decision row.
`AMENDMENT` preserves a correction without deleting history and blocks automatic
promotion until manual review.

The ledger measures a frozen setup under a frozen regime label. Screenshot-only
decisions may be logged for process review but cannot support `qm=E/C` unless
executable prices, fills, costs, packet hashes, and regime metrics are
independently exact.

## 2. Event schema

CSV columns, in this exact order:

```text
event_id,event_type,recorded_at_jst,decision_id,parent_event_id,decision_at_jst,data_vintage,symbol,session,source_frames,setup_version,setup_family,direction,regime_code,regime_policy_version,regime_mode,regime_metrics,gamma_state,gamma_source,gamma_asof,event_state,decision_price,trigger,entry_price,structural_invalidation,hard_stop,targets,valid_until_jst,filled,fill_at_jst,status,outcome,outcome_definition,exit_price,exit_at_jst,gross_points,round_trip_cost_points,net_points,r_multiple,mfe_points,mae_points,cancel_reason,sample_role,year,nqx_sha256,prev_event_sha256,event_sha256,notes
```

Key fields:

- `event_id`: unique immutable ID.
- `event_type`: `DECISION`, `RESOLUTION`, `CANCEL`, or `AMENDMENT`.
- `decision_id`: stable ID shared by the decision and its terminal event.
- `data_vintage`: source timestamp or file/hash identifying what was known.
- `setup_version`: frozen strategy identity, e.g. `BC_ACCEPT_RETEST_v1`. Any
  parameter or outcome-definition change creates a new version.
- `regime_code`, `regime_policy_version`, `regime_mode`, `regime_metrics`: label and
  exact facts known at decision time.
- `gamma_state/source/asof`: use `U/MISSING/MISSING` when unavailable.
- `trigger`, `structural_invalidation`, `hard_stop`, `targets`, `valid_until_jst`:
  freeze before entry.
- `outcome_definition`: one immutable event, normally TP1 before hard stop within
  validity.
- `sample_role`: `IS` or `OOS`, assigned before the outcome.
- `nqx_sha256`: SHA-256 of the exact validated decision packet — a missing hash
  blocks promotion.

## 3. Event lifecycle

**`DECISION`** — Append before the result is observable. Status must be `WATCH` or
`ARMED`; `filled` is blank/false; all outcome fields stay blank.

**`RESOLUTION`** — Append after a filled decision is terminal. Reference the prior
decision event, set `filled=true`, use status `CLOSED` or `INVALIDATED`, record
exact fill/exit, gross points, round-trip cost, and net points. Require
`net_points = gross_points - round_trip_cost_points`.

**`CANCEL`** — Append for an unfilled invalidation or expiry. Reference the
decision, `filled=false`, status `CANCELED` or `EXPIRED`, state the reason.
Retained but excluded from N and expectancy.

**`AMENDMENT`** — A note referencing the incorrect event, never a delete/rewrite of
the original. Any ledger containing an amendment is ineligible for automatic
promotion until an independent manual audit creates a clean derived dataset with
full provenance.

Only one terminal `RESOLUTION` or `CANCEL` is allowed per decision.

## 4. Hash chain

Each event stores the previous event's hash. `event_sha256` is SHA-256 of the
canonical UTF-8 JSON representation of every column except `event_sha256`,
including `prev_event_sha256`. The first event points to 64 zeroes.

This detects edits, deletions, insertions, and reordering inside the ledger. It
does not prevent someone from replacing the whole file and is not a digital
signature — preserve periodic read-only copies or repository commits for external
anchoring.

## 5. State and counting rules

- An unfilled invalidation is `CANCEL`; it does not count as a trade.
- A filled position stopped or structurally invalidated is `RESOLUTION` and counts
  with its realized net result.
- `CANCEL`, `EXPIRED`, and every unfilled decision are excluded from N,
  t-statistics, and expectancy.
- Multiple signals from one unresolved auction episode are not independent — use
  one `decision_id`; do not duplicate decisions to inflate N.
- Opposite directions require separate decisions and remain mutually exclusive
  under the NQX packet.
- Use the same signed-point convention for every outcome.

## 6. Frozen setup identity

Group evidence by:

```text
setup_version × regime_code × session × direction
```

Do not pool opposite directions, sessions, event classes, or regime codes merely to
reach N. If a group changes entry mode, stop logic, target hierarchy, expiry,
event rule, gamma gate, outcome definition, or regime policy, increment
`setup_version` before observing results.

Assign `year` from the decision timestamp. Assign `IS/OOS` chronologically before
opening OOS. Opening OOS more than once for the same trial invalidates confirmatory
use.

## 7. Promotion gate

The same frozen OOS group must satisfy all:

1. At least 30 independent, filled occurrences after excluding `CANCEL` and
   `EXPIRED`.
2. One-sample t-statistic of net points at least 2.0.
3. Positive mean and cumulative net points after at least 2.0 points round-trip
   friction per counted occurrence.
4. At least two OOS calendar years, each with positive mean net points.
5. One outcome definition, one regime-policy version, metric-mode regime evidence,
   exact NQX packet hashes, and no post-hoc mutation or unresolved amendment.

Passing supports at most `qm=E`. It does not create a calibrated probability.
`qm=C` additionally requires a frozen probability event, unseen calibration sample,
calibration diagnostics, and uncertainty reporting.

Failure is permanent evidence. Record the failed condition and do not retry the
same hypothesis with cosmetic parameter changes. A materially different hypothesis
requires a new `setup_version` and preregistration.
