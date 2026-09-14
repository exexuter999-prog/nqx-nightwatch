import { test } from "node:test";
import assert from "node:assert/strict";
import { applyEvent, emptyState, projectState, strategyEvidenceHash,
  entryKeyForTuple, executionIntentHash } from "../src/state_machine.js";

const T0 = Date.parse("2026-08-22T12:00:00Z");
const iso = (ms = T0) => new Date(ms).toISOString();

function frozenEvidence() {
  const raw = { version: "R14-STRATEGY-EVIDENCE-1", asOf: iso(), sessionId: "NY-20260822",
    source: "fixture", provenance: "test", models: { ifvg: { valid: false } } };
  return { ...raw, evidenceHash: strategyEvidenceHash(raw) };
}

function sealedState(cycleId = "cy-r17", revision = 1) {
  const evidence = frozenEvidence();
  const market = { at: iso(), observedAt: iso(), verified: true, source: "fixture",
    sourceSymbol: "CME_MINI:MNQU6", resolution: "3", barResolution: "3", price: 20000,
    cvdAt: iso(), cycleId, dayguard: { at: iso(), available: true, blocked: false, codes: [] }, bars: [
      { t: 1700000000, o: 19999, h: 20000, l: 19998, c: 19999 },
      { t: 1700000180, o: 19999, h: 20001, l: 19999, c: 20000 }],
    levels: [], strategyEvidence: evidence };
  const scenario = { scenarioId: `sc-${cycleId}`, fingerprint: `fp-${cycleId}`, state: "ARMED",
    symbol: "MNQU6", side: "BUY", qty: 2, entry: 20000, stop: 19940, target: 20060,
    targets: [20060, 20120], planVersion: "R17-SPLIT-1", grade: "A",
    legs: [{ id: "TP1", qty: 1, target: 20060 },
      { id: "RUNNER", qty: 1, target: 20120 }],
    issuedAt: iso(), observedAt: iso(), expiresAt: iso(T0 + 300_000), marketCycleId: cycleId,
    setupVersion: "R14-SETUP", catalogVersion: "R14-CATALOG", detectorVersion: "R14-DETECTOR",
    executionContractVersion: "R22-EXECUTION-CONTRACT-1", evidenceHash: evidence.evidenceHash,
    executionContract: { version: "R22-EXECUTION-CONTRACT-1", riskCapDollars: 240,
      riskCapSource: "fixture", accountScope: ["APEX0001"] } };
  let result = applyEvent(emptyState("acct", "MNQU6"), { stream: "cycle", revision,
    payload: { cycleId, market, scenario } }, T0);
  assert.equal(result.accepted, true);
  result = applyEvent(result.state, { stream: "position", revision: 1,
    payload: { position: { verified: true, source: "broker", symbol: "MNQU6", qty: 0,
      observedAt: iso() } } }, T0);
  assert.equal(result.accepted, true);
  return { state: result.state, scenario: result.state.scenario };
}

function tuple(scenario) {
  return Object.fromEntries(["scenarioId", "fingerprint", "evidenceHash", "marketCycleId"]
    .map((field) => [field, scenario[field]]));
}

test("R17 atomic ENTRY claim permits one claimant and keeps PARTIAL locked", () => {
  const sealed = sealedState();
  assert.equal(projectState(sealed.state, T0).display.orderable, true);
  const payload = { action: "CLAIM", entryKey: entryKeyForTuple(tuple(sealed.scenario)),
    tuple: tuple(sealed.scenario), claimTokenHash: "a".repeat(64), orderType: "LIMIT" };
  const first = applyEvent(sealed.state, { stream: "entry_claim", revision: 1, payload }, T0);
  assert.equal(first.accepted, true);
  assert.equal(first.state.entryClaim.executionIntentHash,
    executionIntentHash(first.state.entryClaim.executionIntent));
  const competing = applyEvent(first.state, { stream: "entry_claim", revision: 2,
    payload: { ...payload, claimTokenHash: "b".repeat(64) } }, T0);
  assert.equal(competing.accepted, false);
  assert.match(competing.reason, /ALREADY_HELD/);

  const consumed = applyEvent(first.state, { stream: "entry_claim", revision: 2,
    payload: { action: "CONSUME", entryKey: payload.entryKey, claimTokenHash: payload.claimTokenHash,
      executionIntent: first.state.entryClaim.executionIntent,
      executionIntentHash: first.state.entryClaim.executionIntentHash } }, T0);
  assert.equal(consumed.accepted, true);
  const malformed = applyEvent(consumed.state, { stream: "entry_claim", revision: 3,
    payload: { action: "RESOLVE", entryKey: payload.entryKey, claimTokenHash: payload.claimTokenHash,
      routeState: "PARTIAL", acceptedCount: 1, explicitRejectCount: 1, totalAttempts: 2,
      routeSnapshot: [
        { accountId: "APEX0001", legId: "TP1", state: "ACCEPTED" },
        { accountId: "APEX0001", legId: "RUNNER", state: "REJECTED" },
      ] } }, T0);
  assert.equal(malformed.accepted, false);
  assert.match(malformed.reason, /ROUTE_SNAPSHOT_INVALID/);
  const partial = applyEvent(consumed.state, { stream: "entry_claim", revision: 3,
    payload: { action: "RESOLVE", entryKey: payload.entryKey, claimTokenHash: payload.claimTokenHash,
      routeState: "PARTIAL", acceptedCount: 1, explicitRejectCount: 1, totalAttempts: 2,
      routeSnapshot: [
        { accountId: "APEX0001", legId: "TP1", state: "ACCEPTED", orderId: "O-1", receipt: "R-1" },
        { accountId: "APEX0001", legId: "RUNNER", state: "REJECTED" },
      ] } }, T0);
  assert.equal(partial.accepted, true);
  assert.equal(projectState(partial.state, T0).entryClaim.routeState, "PARTIAL");
  const retry = applyEvent(partial.state, { stream: "entry_claim", revision: 4,
    payload: { ...payload, claimTokenHash: "c".repeat(64) } }, T0);
  assert.equal(retry.accepted, false);
  assert.match(retry.reason, /ALREADY_HELD/);
});

test("R17 only explicit zero-accepted rejection unlocks a claim", () => {
  const sealed = sealedState("cy-reject");
  const payload = { action: "CLAIM", entryKey: entryKeyForTuple(tuple(sealed.scenario)),
    tuple: tuple(sealed.scenario), claimTokenHash: "d".repeat(64), orderType: "LIMIT" };
  const first = applyEvent(sealed.state, { stream: "entry_claim", revision: 1, payload }, T0);
  const consumed = applyEvent(first.state, { stream: "entry_claim", revision: 2,
    payload: { action: "CONSUME", entryKey: payload.entryKey, claimTokenHash: payload.claimTokenHash,
      executionIntent: first.state.entryClaim.executionIntent,
      executionIntentHash: first.state.entryClaim.executionIntentHash } }, T0);
  const rejected = applyEvent(consumed.state, { stream: "entry_claim", revision: 3,
    payload: { action: "RESOLVE", entryKey: payload.entryKey, claimTokenHash: payload.claimTokenHash,
      routeState: "REJECTED", acceptedCount: 0, explicitRejectCount: 2, totalAttempts: 2,
      routeSnapshot: [
        { accountId: "APEX0001", legId: "TP1", state: "REJECTED" },
        { accountId: "APEX0001", legId: "RUNNER", state: "REJECTED" },
      ] } }, T0);
  assert.equal(rejected.accepted, true);
  assert.equal(rejected.state.entryClaim.state, "REJECTED_ZERO");
  const reclaimed = applyEvent(rejected.state, { stream: "entry_claim", revision: 4,
    payload: { ...payload, claimTokenHash: "e".repeat(64) } }, T0);
  assert.equal(reclaimed.accepted, true);
});

test("R18 claim recomputes tuple key and binds every LIMIT execution field", () => {
  const sealed = sealedState("cy-intent-limit");
  const base = { action: "CLAIM", entryKey: entryKeyForTuple(tuple(sealed.scenario)),
    tuple: tuple(sealed.scenario), claimTokenHash: "1".repeat(64), orderType: "LIMIT" };
  const wrongKey = applyEvent(sealed.state, { stream: "entry_claim", revision: 1,
    payload: { ...base, entryKey: `${base.entryKey.slice(0, -1)}0` } }, T0);
  assert.equal(wrongKey.accepted, false);
  assert.match(wrongKey.reason, /KEY_MISMATCH/);

  const claim = applyEvent(sealed.state, { stream: "entry_claim", revision: 1, payload: base }, T0);
  assert.equal(claim.accepted, true);
  const frozen = claim.state.entryClaim.executionIntent;
  assert.deepEqual(frozen.accountScope, ["APEX0001"]);
  assert.equal(frozen.entry, "20000.00");
  assert.equal(frozen.last, null);

  const mutations = [
    { ...frozen, symbol: "MNQZ6" },
    { ...frozen, side: "SELL" },
    { ...frozen, entry: "20000.25" },
    { ...frozen, stop: "19939.75" },
    { ...frozen, targets: ["20060.25", frozen.targets[1]],
      legs: [{ ...frozen.legs[0], target: "20060.25" }, frozen.legs[1]] },
    { ...frozen, orderType: "MARKET", entry: null, last: "20000.00" },
    { ...frozen, accountScope: ["APEX0002"] },
    { ...frozen, accountScope: ["APEX0001", "APEX0001"] },
    { ...frozen, planVersion: "R17-SPLIT-2" },
  ];
  for (const [index, mutation] of mutations.entries()) {
    const result = applyEvent(claim.state, { stream: "entry_claim", revision: 2,
      payload: { action: "CONSUME", entryKey: base.entryKey,
        claimTokenHash: base.claimTokenHash, executionIntent: mutation,
        executionIntentHash: executionIntentHash(mutation) } }, T0);
    assert.equal(result.accepted, false, `mutation ${index} must not consume`);
    assert.match(result.reason, /INTENT_MISMATCH/);
  }
  const wrongHash = applyEvent(claim.state, { stream: "entry_claim", revision: 2,
    payload: { action: "CONSUME", entryKey: base.entryKey,
      claimTokenHash: base.claimTokenHash, executionIntent: frozen,
      executionIntentHash: `${claim.state.entryClaim.executionIntentHash.slice(0, -1)}0` } }, T0);
  assert.equal(wrongHash.accepted, false);
});

test("R18 MARKET claim freezes the verified current market, not scenario entry", () => {
  const sealed = sealedState("cy-intent-market");
  sealed.state.market = { ...sealed.state.market, price: 20000.25 };
  const payload = { action: "CLAIM", entryKey: entryKeyForTuple(tuple(sealed.scenario)),
    tuple: tuple(sealed.scenario), claimTokenHash: "2".repeat(64), orderType: "MARKET" };
  const claim = applyEvent(sealed.state, { stream: "entry_claim", revision: 1, payload }, T0);
  assert.equal(claim.accepted, true);
  assert.equal(claim.state.entryClaim.executionIntent.entry, null);
  assert.equal(claim.state.entryClaim.executionIntent.last, "20000.25");

  const tampered = { ...claim.state.entryClaim.executionIntent, last: "20000.50" };
  const consume = applyEvent(claim.state, { stream: "entry_claim", revision: 2,
    payload: { action: "CONSUME", entryKey: payload.entryKey,
      claimTokenHash: payload.claimTokenHash, executionIntent: tampered,
      executionIntentHash: executionIntentHash(tampered) } }, T0);
  assert.equal(consume.accepted, false);
  assert.match(consume.reason, /INTENT_MISMATCH/);

  const unverified = sealedState("cy-intent-unverified");
  unverified.state.market = { ...unverified.state.market, verified: false };
  const refused = applyEvent(unverified.state, { stream: "entry_claim", revision: 1,
    payload: { ...payload, entryKey: entryKeyForTuple(tuple(unverified.scenario)),
      tuple: tuple(unverified.scenario), claimTokenHash: "3".repeat(64) } }, T0);
  assert.equal(refused.accepted, false);
});
