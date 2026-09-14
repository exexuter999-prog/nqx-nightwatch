import { test } from "node:test";
import assert from "node:assert/strict";
import { applyEvent, emptyState, strategyEvidenceHash, entryKeyForTuple,
  managementIntentHash, managementKeyForIntent } from "../src/state_machine.js";
import executionContract from "../../execution_contract.json" with { type: "json" };

// R41: 期限は契約から引く。秒数を直書きすると、実パイプラインで満たせない予算を
// 直したときにテストだけが古い数字を守り続ける。
const CLAIM_MAX_AGE_MS = Number(executionContract.claim.maxAgeSec) * 1000;
const MAX_PRICE_AGE_MS = Number(executionContract.marketOrder.maxPriceAgeSec) * 1000;

const T0 = Date.parse("2026-08-23T12:00:00Z");
const iso = (ms = T0) => new Date(ms).toISOString();

function entryState({ marketOrder = false } = {}) {
  const rawEvidence = { version: "R14-STRATEGY-EVIDENCE-1", asOf: iso(),
    sessionId: "NY-R19", source: "fixture", provenance: "test",
    models: { ifvg: { valid: false } } };
  const evidence = { ...rawEvidence, evidenceHash: strategyEvidenceHash(rawEvidence) };
  const cycleId = "cy-r19";
  const market = { at: iso(), observedAt: iso(), verified: true, source: "fixture",
    sourceSymbol: "CME_MINI:MNQU6", resolution: "3", barResolution: "3", price: 20000,
    cvdAt: iso(), cycleId, strategyEvidence: evidence,
    dayguard: { at: iso(), available: true, blocked: false, codes: [] },
    bars: [{ t: 1700000000, o: 20000, h: 20001, l: 19999, c: 20000 },
      { t: 1700000180, o: 20000, h: 20002, l: 20000, c: 20001 }] };
  const scenario = { scenarioId: "sc-r19", fingerprint: "fp-r19", state: "ARMED",
    symbol: "MNQU6", side: "BUY", qty: 2, entry: 20000, stop: 19940,
    target: 20060, targets: [20060, 20120],
    legs: [{ id: "TP1", qty: 1, target: 20060 }, { id: "RUNNER", qty: 1, target: 20120 }],
    planVersion: "R19-PLAN-77", grade: "A+", issuedAt: iso(), observedAt: iso(),
    expiresAt: iso(T0 + 300_000), sessionEndAt: iso(T0 + 240_000), marketCycleId: cycleId,
    setupVersion: "R19-S", catalogVersion: "R19-C", detectorVersion: "R19-D",
    executionContractVersion: "R22-EXECUTION-CONTRACT-1", evidenceHash: evidence.evidenceHash,
    executionContract: { version: "R22-EXECUTION-CONTRACT-1", riskCapDollars: 240,
      riskCapSource: "fixture", accountScope: ["APEX0001"] } };
  let result = applyEvent(emptyState("acct", "MNQU6"), { stream: "cycle", revision: 1,
    payload: { cycleId, market, scenario } }, T0);
  assert.equal(result.accepted, true, result.reason);
  assert.ok(result.state.scenario, result.reason);
  result = applyEvent(result.state, { stream: "position", revision: 1,
    payload: { position: { verified: true, source: "broker", symbol: "MNQU6", qty: 0,
      observedAt: iso() } } }, T0);
  const tuple = Object.fromEntries(["scenarioId", "fingerprint", "evidenceHash", "marketCycleId"]
    .map((field) => [field, result.state.scenario[field]]));
  const payload = { action: "CLAIM", entryKey: entryKeyForTuple(tuple), tuple,
    claimTokenHash: "a".repeat(64), orderType: marketOrder ? "MARKET" : "LIMIT" };
  const claim = applyEvent(result.state, { stream: "entry_claim", revision: 1, payload }, T0);
  assert.equal(claim.accepted, true);
  const consume = (state, now = T0) => applyEvent(state, { stream: "entry_claim", revision: 2,
    payload: { action: "CONSUME", entryKey: payload.entryKey,
      claimTokenHash: payload.claimTokenHash,
      executionIntent: claim.state.entryClaim.executionIntent,
      executionIntentHash: claim.state.entryClaim.executionIntentHash } }, now);
  return { state: claim.state, consume };
}

test("R19 ENTRY consume re-evaluates claim age and every current authoritative blocker", () => {
  let fixture = entryState();
  assert.match(fixture.consume(fixture.state, T0 + CLAIM_MAX_AGE_MS + 1_000).reason, /CLAIM_EXPIRED/);
  const cases = [
    ["stop revision", (s) => { s.scenario.stop = 19940.25; }, /CURRENT_INTENT_MISMATCH/],
    ["event", (s) => { s.market.eventBlackout = true; }, /SEAL_INVALID/],
    ["session", (s) => { s.scenario.sessionEndAt = iso(T0 - 1); }, /SEAL_INVALID/],
    ["CVD", (s) => { s.market.cvdAt = null; }, /SEAL_INVALID/],
    ["dayguard", (s) => { s.market.dayguard.blocked = true; }, /SEAL_INVALID/],
  ];
  for (const [label, mutate, reason] of cases) {
    fixture = entryState();
    const changed = structuredClone(fixture.state);
    mutate(changed);
    const result = fixture.consume(changed);
    assert.equal(result.accepted, false, label);
    assert.match(result.reason, reason, label);
  }
});

test("R19 MARKET consume rejects stale quote and configured deviation", () => {
  let fixture = entryState({ marketOrder: true });
  assert.match(fixture.consume(fixture.state, T0 + MAX_PRICE_AGE_MS + 1_000).reason, /MARKET_PRICE_STALE/);
  fixture = entryState({ marketOrder: true });
  const moved = structuredClone(fixture.state);
  moved.market.price = 20002.25;
  assert.match(fixture.consume(moved, T0 + 1_000).reason, /MARKET_PRICE_DEVIATION/);
});

function managementState() {
  const state = emptyState("acct", "MNQU6");
  return applyEvent(state, { stream: "position", revision: 1, payload: { position: {
    verified: true, source: "broker", symbol: "MNQU6", side: "LONG", qty: 1,
    accountId: "APEX0001", filledAt: iso(), positionGeneration: "POS:" + "1".repeat(64),
    observedAt: iso(), avgEntry: 20000,
  } } }, T0).state;
}

test("R19 MANAGEMENT CAS serializes producers and binds current position generation", () => {
  const state = managementState();
  const intent = { version: "R22-MANAGEMENT-INTENT-1", accountId: "APEX0001",
    symbol: "MNQU6", positionGeneration: "POS:" + "1".repeat(64), action: "MODIFY",
    side: "BUY", qty: 1, stop: "20000.00", target: "20120.00",
    executionContractVersion: "R22-EXECUTION-CONTRACT-1" };
  const payload = { action: "CLAIM", managementKey: managementKeyForIntent(intent),
    claimTokenHash: "b".repeat(64), managementIntent: intent,
    managementIntentHash: managementIntentHash(intent) };
  const first = applyEvent(state, { stream: "management_claim", revision: 1, payload }, T0);
  assert.equal(first.accepted, true);
  const competing = applyEvent(first.state, { stream: "management_claim", revision: 2,
    payload: { ...payload, claimTokenHash: "c".repeat(64) } }, T0);
  assert.equal(competing.accepted, false);
  assert.match(competing.reason, /ALREADY_HELD/);
  const changed = structuredClone(first.state);
  changed.position.positionGeneration = "POS:" + "2".repeat(64);
  const stale = applyEvent(changed, { stream: "management_claim", revision: 2,
    payload: { ...payload, action: "CONSUME" } }, T0);
  assert.equal(stale.accepted, false);
  assert.match(stale.reason, /CURRENT_POSITION_MISMATCH/);
  const consumed = applyEvent(first.state, { stream: "management_claim", revision: 2,
    payload: { ...payload, action: "CONSUME" } }, T0);
  assert.equal(consumed.accepted, true);
  const resolved = applyEvent(consumed.state, { stream: "management_claim", revision: 3,
    payload: { action: "RESOLVE", managementKey: payload.managementKey,
      claimTokenHash: payload.claimTokenHash, routeState: "SENT",
      routeReceipt: { accounts: [{ accountId: "APEX0001", stopOrderId: "SL1",
        targetOrderId: "TP1", ocoGroupId: "OCO1", receipt: "R1" }] } } }, T0);
  assert.equal(resolved.accepted, true);
  assert.equal(resolved.state.managementClaim.routeState, "SENT");
});
