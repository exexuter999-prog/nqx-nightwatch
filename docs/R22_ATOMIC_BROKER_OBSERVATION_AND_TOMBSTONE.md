# R22 atomic broker observation and tombstone contract

Machine-readable source: `execution_contract.json` (`R22-EXECUTION-CONTRACT-1`).

R22 changes only execution safety state. It does not enable live ordering.

## Atomic broker truth

Every recovery observation contains overall, position, and orders timestamps;
broker snapshot ID/cursor or a stable position-orders-position double-read;
platform, account, symbol, current intent hash; and a canonical SHA-256 snapshot
hash. Position/orders component skew may not exceed two seconds.

`observedAt` is strictly increasing. Equal timestamps are accepted only when
the complete canonical bytes are identical, as an idempotent no-op. Older,
future, cross-account, cross-symbol, cross-intent, reused-snapshot-ID, and
one-bit-different equal-time observations are rejected even with a higher
stream revision.

ENTRY and MANAGEMENT recovery include `brokerSnapshotHash`, `brokerSnapshotId`,
and cursor in the recovery command. The Durable Object compares that CAS tuple
with its latest fresh journal before releasing a global lock. A query/commit
interleaving that changes broker position therefore remains locked.

## Permanent ENTRY tombstone

Successful ENTRY recovery burns the authoritative tuple
`scenarioId + fingerprint + evidenceHash + marketCycleId` permanently. The
same tuple cannot be claimed again after nonce changes, Bot/AUTO mode changes,
process restart, or local-ledger loss. A different valid cycle may claim only
after the preceding lock has exact terminal broker proof and the current
position is verified flat.

## Ownership and market fills

Position generation is a local monotonic wrapper around a fresh
`broker_status.position_identity(raw_position)`; a supplied generation is
never authoritative. Continued management rechecks raw identity plus
orderId, receipt, and filledAt. MARKET ownership uses actual broker fills,
requires tick alignment and at most two points from the frozen quote, and
recalculates risk from the actual fills against the $240 cap.

Unrelated historical terminal order rows do not invalidate ownership. A
duplicate expected ID, receipt collision, or simultaneous active economic
conflict does. One exact accepted/filled PARTIAL leg may manage only that
confirmed leg; no missing runner is inferred.
