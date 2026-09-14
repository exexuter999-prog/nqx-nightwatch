# R23 broker route identity acquisition

Machine-readable source: `execution_contract.json` (`routeIdentity`).

R23 changes only how a route's broker identity is obtained. It does not enable
live ordering, relax any gate, or add a new execution path.

## Why

CrossTrade answers both `PLACE` and `cancelandbracket` with a bare
`{"success": true}`. It returns no order id and no receipt. R19..R22 require
both before a leg may be called ACCEPTED, so on real hardware:

- every live ENTRY leg resolved UNKNOWN with `acceptedCount = 0`;
- `entryRecoveryProof` has no frozen ACCEPTED row to prove terminal, so the
  Durable Object entry claim could never be recovered — a permanent lock;
- `ownership_binder.bind()` returned "UNKNOWN accepted=0 has no ownership", so
  no position could ever be managed or plan-flattened;
- `extract_replacement_receipt` failed for MODIFY, making every bracket
  replacement UNKNOWN.

The suite did not catch this because its stubs answer with an identity-bearing
body that the real broker never sends.

## Identity comes from broker truth, not the response echo

`route_identity` brackets **one single attempt** between two order-view
snapshots and binds it to the one row that appeared in that window.

- TP1 and RUNNER share account, side, quantity, order type and entry price, so
  a per-leg snapshot is mandatory: a window holding two matching parents is
  ambiguous and rejected.
- A new row is a candidate only when account, symbol, action, quantity, order
  type and (for LIMIT) the exact entry price all match the frozen attempt.
  Nothing is copied from the request; values are only compared.
- Any non-allowlisted status in the window, any row without an order id, either
  snapshot unverified, or a candidate count other than one returns `None` and
  the attempt stays classified by its HTTP result.

Broker truth outranks the response echo in **both** directions. A bare success
body becomes ACCEPTED once its row exists, and an attempt that returned an
error or no status at all is still ACCEPTED when it demonstrably created an
order — an owned order is always safer than an orphan.

## Derived receipts

CrossTrade issues neither `receipt` nor `requestId` on webhook responses or
REST order rows. `broker_status.derived_receipt()` therefore supplies
`PLATFORM:ACCOUNT:ORDER_ID` for rows that carry none, and records
`receiptSource` as `broker` or `derived`.

The value is derived only from broker-returned row identity, is unique per
order row, and reproduces byte-identically on every later re-query, so route
snapshots, ownership binding and R22 observations all compare the same string.

**It is deliberately not independent evidence.** For a derived row the receipt
adds no collision detection beyond the broker order id itself. What protects
the route is the single-candidate window binding, order id uniqueness, and the
unchanged economic verification in `ownership_binder` and
`verify_protective_orders`. A broker that does issue a receipt keeps it, and
`receiptSource` makes the difference auditable.

## Replacement brackets

MODIFY acquires its replacement identity the same way: exactly one new active
STOP at the frozen stop price and one new active LIMIT at the frozen target,
same account/symbol, opposite action, matching quantity, sharing one non-empty
broker OCO parent. Requiring the pair to be **new** is stricter than the
response-body route it replaces — an unchanged bracket that merely survived the
request can no longer be mistaken for a successful replacement.

An acquired route receipt freezes one receipt per replacement order
(`stopReceipt`, `targetReceipt`). `verify_protective_orders`,
`nqx_state.recover_management_from_broker` and the Durable Object's
`managementRecoveryProof` all bind each order to its own receipt when those
fields are present, and fall back to the single shared request receipt for the
legacy response-body route.

## Machine envelope hygiene

`route_envelope.parse` treats `ERROR|FATAL|EXCEPTION|FAILED|INTERRUPTED|
TRACEBACK` anywhere in the route stdout as a poisoned envelope. `order.py`
printed `ROUTE PARTIAL/FAILED` on that same stdout, both from the post helpers
and once more just before the envelope lines, so every PARTIAL route was
downgraded to UNKNOWN and the frozen-leg path in `autotrade_engine` was
unreachable.

The producer now prints `ROUTE NOT_ALL_ACCEPTED`. The marker rule itself is
unchanged: a genuine traceback still poisons the envelope. A test asserts that
no `print(` in `order.py` contains a marker word.

## Not covered

A MARKET parent that fills instantly may be invisible in a Tradovate
collection-route snapshot, which has no frozen id to merge yet. That attempt
binds nothing and stays UNKNOWN — fail-closed, as before. LIMIT split entry,
the production path, is unaffected.
