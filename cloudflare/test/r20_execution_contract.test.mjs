import { test } from "node:test";
import assert from "node:assert/strict";
import { applyEvent, emptyState, strategyEvidenceHash, entryKeyForTuple,
  managementIntentHash, managementKeyForIntent, brokerObservationHash } from "../src/state_machine.js";
import executionContract from "../../execution_contract.json" with { type: "json" };

// R41: 境界は契約から引く。5 秒を直書きしていたため、実パイプラインで満たせない
// 予算だと判明して契約を直したときにテストだけが取り残された。ここで守るのは
// 「鮮度と乖離を独立に見ている」という **挙動** であって、特定の秒数ではない。
const MAX_PRICE_AGE_MS = Number(executionContract.marketOrder.maxPriceAgeSec) * 1000;

const T0 = Date.parse("2026-08-23T12:00:00Z");
const iso = (ms = T0) => new Date(ms).toISOString();

function sealObservation(raw, id = "bs-r22") {
  const at = raw.observedAt;
  const position = { ...raw.position,
    rawPositionIdentity: Number(raw.position?.qty || 0) > 0
      ? (raw.position.rawPositionIdentity || "POS:" + "1".repeat(64)) : null };
  const observation = { ...raw, position, positionObservedAt: at, ordersObservedAt: at,
    snapshotId: id, cursor: null, snapshotMode: "STABLE_DOUBLE_READ",
    stableBeforeHash: "1".repeat(64), stableAfterHash: "1".repeat(64), platform: "STUB" };
  return { ...observation, snapshotHash: brokerObservationHash(observation) };
}

function entryFixture({ marketOrder = false } = {}) {
  const rawEvidence = { version: "R14-STRATEGY-EVIDENCE-1", asOf: iso(),
    sessionId: "NY-R20", source: "fixture", provenance: "test",
    models: { ifvg: { valid: false } } };
  const evidence = { ...rawEvidence, evidenceHash: strategyEvidenceHash(rawEvidence) };
  const cycleId = "cy-r20";
  const market = { at: iso(), observedAt: iso(), verified: true, source: "fixture",
    sourceSymbol: "CME_MINI:MNQU6", resolution: "3", barResolution: "3", price: 20000,
    cvdAt: iso(), cycleId, strategyEvidence: evidence,
    dayguard: { at: iso(), available: true, blocked: false, codes: [] },
    bars: [{ t: 1700000000, o: 20000, h: 20001, l: 19999, c: 20000 },
      { t: 1700000180, o: 20000, h: 20002, l: 20000, c: 20001 }] };
  const scenario = { scenarioId: "sc-r20", fingerprint: "fp-r20", state: "ARMED",
    symbol: "MNQU6", side: "BUY", qty: 2, entry: 20000, stop: 19940,
    target: 20060, targets: [20060, 20120],
    legs: [{ id: "TP1", qty: 1, target: 20060 }, { id: "RUNNER", qty: 1, target: 20120 }],
    planVersion: "R20-PLAN-1", grade: "A+", issuedAt: iso(), observedAt: iso(),
    expiresAt: iso(T0 + 300_000), sessionEndAt: iso(T0 + 240_000), marketCycleId: cycleId,
    setupVersion: "R20-S", catalogVersion: "R20-C", detectorVersion: "R20-D",
    executionContractVersion: "R22-EXECUTION-CONTRACT-1", evidenceHash: evidence.evidenceHash,
    executionContract: { version: "R22-EXECUTION-CONTRACT-1", riskCapDollars: 240,
      riskCapSource: "fixture", accountScope: ["APEX0001"] } };
  let state = applyEvent(emptyState("acct", "MNQU6"), { stream: "cycle", revision: 1,
    payload: { cycleId, market, scenario } }, T0).state;
  state = applyEvent(state, { stream: "position", revision: 1, payload: { position: {
    verified: true, source: "broker", symbol: "MNQU6", qty: 0, observedAt: iso(),
  } } }, T0).state;
  const tuple = Object.fromEntries(["scenarioId", "fingerprint", "evidenceHash", "marketCycleId"]
    .map((field) => [field, state.scenario[field]]));
  const claimPayload = { action: "CLAIM", entryKey: entryKeyForTuple(tuple), tuple,
    claimTokenHash: "a".repeat(64), orderType: marketOrder ? "MARKET" : "LIMIT" };
  const claimed = applyEvent(state, { stream: "entry_claim", revision: 1,
    payload: claimPayload }, T0);
  assert.equal(claimed.accepted, true);
  const consumePayload = { action: "CONSUME", entryKey: claimPayload.entryKey,
    claimTokenHash: claimPayload.claimTokenHash,
    executionIntent: claimed.state.entryClaim.executionIntent,
    executionIntentHash: claimed.state.entryClaim.executionIntentHash };
  return { state: claimed.state, claimPayload, consumePayload };
}

test("R20 MARKET keeps frozen last and checks quote freshness/deviation independently", () => {
  for (const [offsetMs, move, accepted, reason] of [
    [MAX_PRICE_AGE_MS, 2.0, true, null],
    [MAX_PRICE_AGE_MS + 1, 0, false, /PRICE_STALE/],
    [1000, 2.25, false, /PRICE_DEVIATION/],
    [-1, 0, false, /PRICE_STALE/],
  ]) {
    const fixture = entryFixture({ marketOrder: true });
    const changed = structuredClone(fixture.state);
    changed.market.price += move;
    changed.market.observedAt = iso(T0 + (offsetMs < 0 ? 1 : 0));
    const now = T0 + Math.max(offsetMs, 0);
    const result = applyEvent(changed, { stream: "entry_claim", revision: 2,
      payload: fixture.consumePayload }, now);
    assert.equal(result.accepted, accepted, `${offsetMs}/${move}: ${result.reason}`);
    if (reason) assert.match(result.reason, reason);
    if (accepted) assert.equal(result.state.entryClaim.executionIntent.last, "20000.00");
  }
});

function managementFixture() {
  let state = emptyState("acct", "MNQU6");
  state = applyEvent(state, { stream: "position", revision: 1, payload: { position: {
    verified: true, source: "broker", symbol: "MNQU6", side: "LONG", qty: 1,
    accountId: "APEX0001", filledAt: iso(), positionGeneration: "POS:" + "1".repeat(64),
    observedAt: iso(), avgEntry: 20000,
  } } }, T0).state;
  const intent = { version: "R22-MANAGEMENT-INTENT-1", accountId: "APEX0001",
    symbol: "MNQU6", positionGeneration: "POS:" + "1".repeat(64), action: "MODIFY",
    side: "BUY", qty: 1, stop: "20000.00", target: "20120.00",
    executionContractVersion: "R22-EXECUTION-CONTRACT-1" };
  const payload = { action: "CLAIM", managementKey: managementKeyForIntent(intent),
    claimTokenHash: "b".repeat(64), managementIntent: intent,
    managementIntentHash: managementIntentHash(intent) };
  const claimed = applyEvent(state, { stream: "management_claim", revision: 1, payload }, T0);
  assert.equal(claimed.accepted, true);
  return { state: claimed.state, intent, payload };
}

test("R21 expired MANAGEMENT CLAIMED is replaceable and RECOVER uses durable broker truth", () => {
  let fixture = managementFixture();
  const boundary = applyEvent(fixture.state, { stream: "management_claim", revision: 2,
    payload: { ...fixture.payload, claimTokenHash: "c".repeat(64) } }, T0 + 30_000);
  assert.equal(boundary.accepted, false);
  assert.match(boundary.reason, /ALREADY_HELD/);
  const replaced = applyEvent(fixture.state, { stream: "management_claim", revision: 2,
    payload: { ...fixture.payload, claimTokenHash: "c".repeat(64) } }, T0 + 30_001);
  assert.equal(replaced.accepted, true);
  const losingProducer = applyEvent(replaced.state, { stream: "management_claim", revision: 3,
    payload: { ...fixture.payload, claimTokenHash: "f".repeat(64) } }, T0 + 30_001);
  assert.equal(losingProducer.accepted, false);
  assert.match(losingProducer.reason, /ALREADY_HELD/);
  const oldReplay = applyEvent(replaced.state, { stream: "management_claim", revision: 3,
    payload: { ...fixture.payload, action: "CONSUME" } }, T0 + 30_002);
  assert.equal(oldReplay.accepted, false);
  assert.match(oldReplay.reason, /TOKEN_INVALID/);

  fixture = managementFixture();
  const future = structuredClone(fixture.state);
  future.managementClaim.claimedAt = iso(T0 + 1);
  const futureClaim = applyEvent(future, { stream: "management_claim", revision: 2,
    payload: { ...fixture.payload, claimTokenHash: "d".repeat(64) } }, T0);
  assert.equal(futureClaim.accepted, false);
  assert.match(futureClaim.reason, /TIME_INVALID/);

  const consumed = applyEvent(fixture.state, { stream: "management_claim", revision: 2,
    payload: { ...fixture.payload, action: "CONSUME" } }, T0);
  assert.equal(consumed.accepted, true);
  const replaceConsumed = applyEvent(consumed.state, { stream: "management_claim", revision: 3,
    payload: { ...fixture.payload, claimTokenHash: "e".repeat(64) } }, T0 + 31_000);
  assert.equal(replaceConsumed.accepted, false);
  const weakRecover = applyEvent(consumed.state, { stream: "management_claim", revision: 3,
    payload: { action: "RECOVER", managementKey: fixture.payload.managementKey,
      claimTokenHash: fixture.payload.claimTokenHash, brokerPositionVerified: true,
      brokerOrderVerifiedTerminal: false } }, T0 + 31_000);
  assert.equal(weakRecover.accepted, false);
  const recovered = applyEvent(consumed.state, { stream: "management_claim", revision: 3,
    payload: { action: "RECOVER", managementKey: fixture.payload.managementKey,
      claimTokenHash: fixture.payload.claimTokenHash, brokerPositionVerified: true,
      brokerOrderVerifiedTerminal: true } }, T0 + 31_000);
  assert.equal(recovered.accepted, false);

  const receipt = { accounts: [{ accountId: "APEX0001", stopOrderId: "SL-R21",
    targetOrderId: "TP-R21", ocoGroupId: "OCO-R21", receipt: "REC-R21" }] };
  const resolved = applyEvent(consumed.state, { stream: "management_claim", revision: 3,
    payload: { action: "RESOLVE", managementKey: fixture.payload.managementKey,
      claimTokenHash: fixture.payload.claimTokenHash, routeState: "UNKNOWN",
      routeReceipt: receipt } }, T0 + 31_000);
  assert.equal(resolved.accepted, true);
  const managementObservation = sealObservation({ observedAt: iso(T0 + 31_000), accountId: "APEX0001",
      symbol: "MNQU6", currentIntentHash: fixture.payload.managementIntentHash,
      position: { verified: true, qty: 1, side: "LONG",
        positionGeneration: fixture.intent.positionGeneration },
      orders: ["SL-R21", "TP-R21"].map((orderId) => ({ orderId, receipt: "REC-R21",
        accountId: "APEX0001", symbol: "MNQU6", status: "CANCELED" })) });
  const observed = applyEvent(resolved.state, { stream: "broker_observation", revision: 1,
    payload: { observation: managementObservation } }, T0 + 31_000);
  assert.equal(observed.accepted, true);
  const exactRecover = applyEvent(observed.state, { stream: "management_claim", revision: 4,
    payload: { action: "RECOVER", managementKey: fixture.payload.managementKey,
      claimTokenHash: fixture.payload.claimTokenHash,
      brokerSnapshotHash: managementObservation.snapshotHash,
      brokerSnapshotId: managementObservation.snapshotId, brokerCursor: "" } }, T0 + 31_001);
  assert.equal(exactRecover.accepted, true);
  assert.equal(exactRecover.state.managementClaim.state, "RECOVERED");
});

test("R20 ENTRY RESOLVE requires the exact account x TP1/RUNNER product and unique identities", () => {
  const fixture = entryFixture();
  const consumed = applyEvent(fixture.state, { stream: "entry_claim", revision: 2,
    payload: fixture.consumePayload }, T0);
  assert.equal(consumed.accepted, true);
  const base = { action: "RESOLVE", entryKey: fixture.claimPayload.entryKey,
    claimTokenHash: fixture.claimPayload.claimTokenHash,
    routeState: "SENT", acceptedCount: 2, explicitRejectCount: 0, totalAttempts: 2 };
  const valid = [
    { accountId: "APEX0001", legId: "TP1", state: "ACCEPTED", orderId: "O1", receipt: "R1" },
    { accountId: "APEX0001", legId: "RUNNER", state: "ACCEPTED", orderId: "O2", receipt: "R2" },
  ];
  for (const rows of [
    valid.slice(0, 1),
    [valid[0], { ...valid[1], accountId: "OTHER" }],
    [valid[0], { ...valid[1], legId: "TP1" }],
    [valid[0], { ...valid[1], orderId: "O1" }],
    [valid[0], { ...valid[1], receipt: "R1" }],
    [...valid, { ...valid[1], orderId: "O3", receipt: "R3" }],
  ]) {
    const result = applyEvent(consumed.state, { stream: "entry_claim", revision: 3,
      payload: { ...base, totalAttempts: rows.length, acceptedCount: rows.length,
        routeSnapshot: rows } }, T0);
    assert.equal(result.accepted, false, JSON.stringify(rows));
  }
  const accepted = applyEvent(consumed.state, { stream: "entry_claim", revision: 3,
    payload: { ...base, routeSnapshot: valid } }, T0);
  assert.equal(accepted.accepted, true, accepted.reason);
});

test("R21 ENTRY RECOVER rejects boolean self-report and requires exact fresh broker journal", () => {
  const fixture = entryFixture();
  const consumed = applyEvent(fixture.state, { stream: "entry_claim", revision: 2,
    payload: fixture.consumePayload }, T0);
  assert.equal(consumed.accepted, true);
  const routeSnapshot = [
    { accountId: "APEX0001", legId: "TP1", state: "ACCEPTED", orderId: "E1", receipt: "ER1" },
    { accountId: "APEX0001", legId: "RUNNER", state: "ACCEPTED", orderId: "E2", receipt: "ER2" },
  ];
  const resolved = applyEvent(consumed.state, { stream: "entry_claim", revision: 3,
    payload: { action: "RESOLVE", entryKey: fixture.claimPayload.entryKey,
      claimTokenHash: fixture.claimPayload.claimTokenHash, routeState: "SENT",
      acceptedCount: 2, explicitRejectCount: 0, totalAttempts: 2, routeSnapshot } }, T0);
  assert.equal(resolved.accepted, true);
  const weak = applyEvent(resolved.state, { stream: "entry_claim", revision: 4,
    payload: { action: "RECOVER", entryKey: fixture.claimPayload.entryKey,
      claimTokenHash: fixture.claimPayload.claimTokenHash,
      brokerPositionVerifiedFlat: true, brokerOrderVerifiedTerminal: true } }, T0 + 1);
  assert.equal(weak.accepted, false);
  const observation = sealObservation({ observedAt: iso(T0 + 1), accountId: "APEX0001", symbol: "MNQU6",
    currentIntentHash: resolved.state.entryClaim.executionIntentHash,
    position: { verified: true, qty: 0, side: "FLAT", positionGeneration: null },
    orders: routeSnapshot.map((row) => ({ orderId: row.orderId, receipt: row.receipt,
      accountId: row.accountId, symbol: "MNQU6", status: "CANCELED" })) });
  const observed = applyEvent(resolved.state, { stream: "broker_observation", revision: 1,
    payload: { observation } }, T0 + 1);
  assert.equal(observed.accepted, true);
  const recovered = applyEvent(observed.state, { stream: "entry_claim", revision: 4,
    payload: { action: "RECOVER", entryKey: fixture.claimPayload.entryKey,
      claimTokenHash: fixture.claimPayload.claimTokenHash,
      brokerSnapshotHash: observation.snapshotHash,
      brokerSnapshotId: observation.snapshotId, brokerCursor: "" } }, T0 + 2);
  assert.equal(recovered.accepted, true);
  assert.equal(recovered.state.entryClaim.state, "RECOVERED");

  const stale = applyEvent(observed.state, { stream: "entry_claim", revision: 4,
    payload: { action: "RECOVER", entryKey: fixture.claimPayload.entryKey,
      claimTokenHash: fixture.claimPayload.claimTokenHash,
      brokerSnapshotHash: observation.snapshotHash,
      brokerSnapshotId: observation.snapshotId, brokerCursor: "" } }, T0 + 30_002);
  assert.equal(stale.accepted, false);
  const tampered = structuredClone(observed.state);
  tampered.brokerObservation.orders[0].receipt = "WRONG";
  const wrong = applyEvent(tampered, { stream: "entry_claim", revision: 4,
    payload: { action: "RECOVER", entryKey: fixture.claimPayload.entryKey,
      claimTokenHash: fixture.claimPayload.claimTokenHash,
      brokerSnapshotHash: observation.snapshotHash,
      brokerSnapshotId: observation.snapshotId, brokerCursor: "" } }, T0 + 2);
  assert.equal(wrong.accepted, false);
});

function resolvedEntry() {
  const fixture = entryFixture();
  const consumed = applyEvent(fixture.state, { stream: "entry_claim", revision: 2,
    payload: fixture.consumePayload }, T0);
  const routeSnapshot = [
    { accountId: "APEX0001", legId: "TP1", state: "ACCEPTED", orderId: "E1", receipt: "ER1" },
    { accountId: "APEX0001", legId: "RUNNER", state: "ACCEPTED", orderId: "E2", receipt: "ER2" },
  ];
  const resolved = applyEvent(consumed.state, { stream: "entry_claim", revision: 3,
    payload: { action: "RESOLVE", entryKey: fixture.claimPayload.entryKey,
      claimTokenHash: fixture.claimPayload.claimTokenHash, routeState: "SENT",
      acceptedCount: 2, explicitRejectCount: 0, totalAttempts: 2, routeSnapshot } }, T0);
  assert.equal(resolved.accepted, true);
  return { fixture, state: resolved.state, routeSnapshot };
}

test("R23 MANAGEMENT recovery accepts per-order replacement receipts and still binds each order", () => {
  // CrossTrade issues no request receipt, so a broker-acquired route receipt
  // freezes one distinct receipt per replacement order instead of one shared
  // value.  Each order must still match its own receipt exactly.
  const fixture = managementFixture();
  const consumed = applyEvent(fixture.state, { stream: "management_claim", revision: 2,
    payload: { ...fixture.payload, action: "CONSUME" } }, T0);
  assert.equal(consumed.accepted, true);
  const receipt = { accounts: [{ accountId: "APEX0001", stopOrderId: "SL-R23",
    targetOrderId: "TP-R23", ocoGroupId: "OCO-R23", receipt: "NT8:APEX0001:OCO-R23",
    stopReceipt: "NT8:APEX0001:SL-R23", targetReceipt: "NT8:APEX0001:TP-R23" }] };
  const resolved = applyEvent(consumed.state, { stream: "management_claim", revision: 3,
    payload: { action: "RESOLVE", managementKey: fixture.payload.managementKey,
      claimTokenHash: fixture.payload.claimTokenHash, routeState: "UNKNOWN",
      routeReceipt: receipt } }, T0 + 31_000);
  assert.equal(resolved.accepted, true);

  const observationFor = (rows) => sealObservation({ observedAt: iso(T0 + 31_000),
    accountId: "APEX0001", symbol: "MNQU6",
    currentIntentHash: fixture.payload.managementIntentHash,
    position: { verified: true, qty: 1, side: "LONG",
      positionGeneration: fixture.intent.positionGeneration },
    orders: rows }, "bs-r23");
  const exact = observationFor([
    { orderId: "SL-R23", receipt: "NT8:APEX0001:SL-R23", accountId: "APEX0001",
      symbol: "MNQU6", status: "CANCELED" },
    { orderId: "TP-R23", receipt: "NT8:APEX0001:TP-R23", accountId: "APEX0001",
      symbol: "MNQU6", status: "CANCELED" },
  ]);
  const observed = applyEvent(resolved.state, { stream: "broker_observation", revision: 1,
    payload: { observation: exact } }, T0 + 31_000);
  assert.equal(observed.accepted, true);
  const recovered = applyEvent(observed.state, { stream: "management_claim", revision: 4,
    payload: { action: "RECOVER", managementKey: fixture.payload.managementKey,
      claimTokenHash: fixture.payload.claimTokenHash,
      brokerSnapshotHash: exact.snapshotHash, brokerSnapshotId: exact.snapshotId,
      brokerCursor: "" } }, T0 + 31_001);
  assert.equal(recovered.accepted, true);
  assert.equal(recovered.state.managementClaim.state, "RECOVERED");

  // Swapping the two per-order receipts must not recover.
  const swapped = observationFor([
    { orderId: "SL-R23", receipt: "NT8:APEX0001:TP-R23", accountId: "APEX0001",
      symbol: "MNQU6", status: "CANCELED" },
    { orderId: "TP-R23", receipt: "NT8:APEX0001:SL-R23", accountId: "APEX0001",
      symbol: "MNQU6", status: "CANCELED" },
  ]);
  const swappedState = applyEvent(resolved.state, { stream: "broker_observation", revision: 1,
    payload: { observation: swapped } }, T0 + 31_000);
  assert.equal(swappedState.accepted, true);
  const rejected = applyEvent(swappedState.state, { stream: "management_claim", revision: 4,
    payload: { action: "RECOVER", managementKey: fixture.payload.managementKey,
      claimTokenHash: fixture.payload.claimTokenHash,
      brokerSnapshotHash: swapped.snapshotHash, brokerSnapshotId: swapped.snapshotId,
      brokerCursor: "" } }, T0 + 31_001);
  assert.equal(rejected.accepted, false);
  assert.match(rejected.reason, /RECOVERY_UNVERIFIED/);
});

test("R22 broker observation is monotonic, byte-idempotent, scoped, and recovery is CAS-bound", () => {
  const { fixture, state, routeSnapshot } = resolvedEntry();
  const base = { observedAt: iso(T0 + 1), accountId: "APEX0001", symbol: "MNQU6",
    currentIntentHash: state.entryClaim.executionIntentHash,
    position: { verified: true, qty: 0, side: "FLAT", positionGeneration: null },
    orders: routeSnapshot.map((row) => ({ orderId: row.orderId, receipt: row.receipt,
      accountId: row.accountId, symbol: "MNQU6", status: "CANCELED" })) };
  const flat = sealObservation(base, "bs-flat-t1");
  const first = applyEvent(state, { stream: "broker_observation", revision: 1,
    payload: { observation: flat } }, T0 + 1);
  assert.equal(first.accepted, true);
  const same = applyEvent(first.state, { stream: "broker_observation", revision: 2,
    payload: { observation: flat } }, T0 + 2);
  assert.equal(same.accepted, true);
  assert.match(same.reason, /IDEMPOTENT/);
  assert.equal(same.state.revisions.broker_observation, 1);
  const equalDifferent = sealObservation({ ...base,
    orders: [{ ...base.orders[0], status: "REJECTED" }, base.orders[1]] }, "bs-flat-other");
  const bitChanged = applyEvent(first.state, { stream: "broker_observation", revision: 2,
    payload: { observation: equalDifferent } }, T0 + 2);
  assert.equal(bitChanged.accepted, false);
  assert.match(bitChanged.reason, /equal-time/);

  const opened = sealObservation({ ...base, observedAt: iso(T0 + 10),
    position: { verified: true, qty: 2, side: "LONG",
      positionGeneration: "PG:2:POS:" + "2".repeat(64),
      rawPositionIdentity: "POS:" + "2".repeat(64) } }, "bs-open-t10");
  const newer = applyEvent(first.state, { stream: "broker_observation", revision: 2,
    payload: { observation: opened } }, T0 + 10);
  assert.equal(newer.accepted, true, newer.reason);
  const oldHigherRevision = applyEvent(newer.state, { stream: "broker_observation", revision: 3,
    payload: { observation: sealObservation(base, "bs-old-high-revision") } }, T0 + 10);
  assert.equal(oldHigherRevision.accepted, false);
  assert.match(oldHigherRevision.reason, /not monotonic/);
  const recoverOldCas = applyEvent(newer.state, { stream: "entry_claim", revision: 4,
    payload: { action: "RECOVER", entryKey: fixture.claimPayload.entryKey,
      claimTokenHash: fixture.claimPayload.claimTokenHash,
      brokerSnapshotHash: flat.snapshotHash, brokerSnapshotId: flat.snapshotId,
      brokerCursor: "" } }, T0 + 11);
  assert.equal(recoverOldCas.accepted, false);
});

test("R22 recovered ENTRY tuple is a permanent tombstone across producer restart", () => {
  const { fixture, state, routeSnapshot } = resolvedEntry();
  const flat = sealObservation({ observedAt: iso(T0 + 1), accountId: "APEX0001",
    symbol: "MNQU6", currentIntentHash: state.entryClaim.executionIntentHash,
    position: { verified: true, qty: 0, side: "FLAT", positionGeneration: null },
    orders: routeSnapshot.map((row) => ({ orderId: row.orderId, receipt: row.receipt,
      accountId: row.accountId, symbol: "MNQU6", status: "CANCELED" })) }, "bs-terminal");
  const observed = applyEvent(state, { stream: "broker_observation", revision: 1,
    payload: { observation: flat } }, T0 + 1);
  const recovered = applyEvent(observed.state, { stream: "entry_claim", revision: 4,
    payload: { action: "RECOVER", entryKey: fixture.claimPayload.entryKey,
      claimTokenHash: fixture.claimPayload.claimTokenHash,
      brokerSnapshotHash: flat.snapshotHash, brokerSnapshotId: flat.snapshotId,
      brokerCursor: "" } }, T0 + 2);
  assert.equal(recovered.accepted, true, recovered.reason);
  assert.equal(recovered.state.entryTombstones.length, 1);
  const restarted = structuredClone(recovered.state);
  restarted.entryClaim = null;
  const replay = applyEvent(restarted, { stream: "entry_claim", revision: 5,
    payload: fixture.claimPayload }, T0 + 3);
  assert.equal(replay.accepted, false);
  assert.match(replay.reason, /TOMBSTONED/);
  const rotated = structuredClone(recovered.state);
  rotated.scenario.scenarioId = "sc-r22-next";
  rotated.scenario.fingerprint = "fp-r22-next";
  rotated.scenario.marketCycleId = "cy-r22-next";
  rotated.market.cycleId = "cy-r22-next";
  const nextTuple = Object.fromEntries(
    ["scenarioId", "fingerprint", "evidenceHash", "marketCycleId"]
      .map((field) => [field, rotated.scenario[field]]));
  const nextClaim = applyEvent(rotated, { stream: "entry_claim", revision: 5,
    payload: { action: "CLAIM", entryKey: entryKeyForTuple(nextTuple), tuple: nextTuple,
      claimTokenHash: "f".repeat(64), orderType: "LIMIT" } }, T0 + 3);
  assert.equal(nextClaim.accepted, true, nextClaim.reason);
  assert.equal(nextClaim.state.entryRelease, null);
});
