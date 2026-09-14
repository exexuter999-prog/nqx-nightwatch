# R21 execution ownership and recovery contract

Status: authoritative for new execution and management paths.

Machine-readable source: `execution_contract.json` (`R21-EXECUTION-CONTRACT-1`).

## Ownership

`ownership_binder.bind()` is the only normal-management ownership decision.
It joins the frozen split route with a verified broker position and the full
broker order view. It never copies a missing broker field from the request.

- RESTING/qty 0: every accepted route leg must exist as one active broker row.
- qty 1: TP1 or RUNNER must be exactly FILLED and the other accepted leg exactly
  active. A continued runner after prior full ownership may use the same frozen
  position generation when both entry legs are already FILLED.
- qty 2: TP1 and RUNNER must both be exactly FILLED; the aggregate position must
  match account, symbol, side, quantity, full generation, and one frozen fill
  identity (or the already-frozen generation).
- account, symbol, action, leg qty, order type, entry reference, order ID,
  receipt and allowlisted status are exact comparisons.
- UNKNOWN with accepted=0, unknown broker identity, duplicate/colliding rows,
  same-account manual positions and another-account positions are unowned.

Unowned positions cannot use plan MODIFY, SL/TP management or plan-driven
flatten. Explicit kill remains available and is broker-truth driven. Entry is
not resent while a SENT/PARTIAL/UNKNOWN lifecycle remains unresolved.

## Broker truth

CrossTrade position and order responses require `success === true`. Account
and symbol/contract identity must be present in the response and match the
configured account or explicit aliases. URL scope and requested symbol are not
identity evidence. Unknown or empty broker statuses make the whole view
UNVERIFIED. Protective orders must be active, not terminal.

MODIFY verification is one `position -> orders -> position` transaction. The
full position generation, account, side and quantity must remain identical
across both position reads.

## Durable recovery

`broker_observation` is a signed, revisioned Durable Object stream. It stores a
fresh observed time, account, symbol, current intent hash, verified full
position generation and the frozen broker order IDs/receipts/statuses.

ENTRY and MANAGEMENT `RECOVER` ignore caller booleans. Recovery is accepted
only when the Durable Object itself matches a fresh observation (maximum age
30 seconds) against the frozen claim journal. Stale, future, wrong-account,
wrong-generation, wrong-intent, wrong-order or wrong-receipt observations fail
closed. A consumed claim without a frozen route receipt cannot be recovered.

## Route and time boundaries

- Confirmed route stdout contains one SNAPSHOT and one final FINAL line.
- stderr is diagnostics only; any confirmed-route stderr makes the result
  UNKNOWN. Route stdout/diagnostics over 2 MiB are rejected.
- `ERROR`, `FATAL`, `EXCEPTION`, `FAILED`, `INTERRUPTED` or `TRACEBACK` mixed
  into the machine envelope make the result UNKNOWN.
- Dayguard age is inclusive at 600 seconds. 600.001 seconds and any future
  timestamp are invalid.

All implementation tests use stubs/dry-run. This contract does not enable live
orders, external publishing or secret access.
