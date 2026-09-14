import { test } from "node:test";
import assert from "node:assert/strict";
import { applyEvent, emptyState, projectState, strategyEvidenceHash } from "../src/state_machine.js";

const T0 = Date.parse("2026-08-22T12:00:00Z");
const iso = (ms = T0) => new Date(ms).toISOString();

function evidence() {
  const value = { version: "R14-STRATEGY-EVIDENCE-1", asOf: iso(), sessionId: "NY-20260822",
    source: "fixture", provenance: "test", models: { ifvg: { valid: false } } };
  return { ...value, evidenceHash: strategyEvidenceHash(value) };
}

function market(cycleId, price = 20000) {
  return { at: iso(), observedAt: iso(), verified: true, source: "fixture", sourceSymbol: "CME_MINI:MNQU6",
    resolution: "3", barResolution: "3", price, cvdAt: iso(), cycleId,
    bars: [{ t: 1700000000, o: price - 1, h: price, l: price - 2, c: price - 1 },
           { t: 1700000180, o: price - 1, h: price + 1, l: price - 1, c: price }],
    levels: [], strategyEvidence: evidence() };
}

function scenario(cycleId, overrides = {}) {
  return { scenarioId: `sc-${cycleId}`, fingerprint: `fp-${cycleId}`, state: "ARMED", symbol: "MNQU6",
    side: "BUY", qty: 2, entry: 20000, stop: 19940, target: 20100, targets: [20100, 20200],
    planVersion: "R19-CYCLE-SPLIT-1",
    issuedAt: iso(), observedAt: iso(), expiresAt: iso(T0 + 300000), grade: "A", marketCycleId: cycleId,
    setupVersion: "R14-SETUP", catalogVersion: "R14-CATALOG", detectorVersion: "R14-DETECTOR",
    executionContractVersion: "R22-EXECUTION-CONTRACT-1",
    executionContract: { version: "R22-EXECUTION-CONTRACT-1", riskCapDollars: 240,
      riskCapSource: "fixture", accountScope: ["APEX0001"] },
    evidenceHash: evidence().evidenceHash, ...overrides };
}

test("R15 cycle commit is atomic and mismatched/reversed pairs are not orderable", () => {
  let state = emptyState("acct", "MNQU6");
  const good = { stream: "cycle", revision: 1, payload: { cycleId: "cy-1", market: market("cy-1"), scenario: scenario("cy-1") } };
  let result = applyEvent(state, good, T0);
  assert.equal(result.accepted, true);
  state = result.state;
  assert.equal(projectState(state, T0).display.cyclePaired, true);
  const committed = projectState(state, T0);
  assert.ok(committed.market.strategyEvidence.models.ifvg);
  assert.equal(Object.hasOwn(committed.scenario, "strategyEvidence"), false, "scenario stores only the evidence hash reference");
  assert.equal(committed.market.strategyEvidence.evidenceHash, committed.scenario.evidenceHash);

  const badScenario = applyEvent(state, { stream: "cycle", revision: 2,
    payload: { cycleId: "cy-2", market: market("cy-2", 20001), scenario: scenario("cy-wrong") } }, T0);
  assert.equal(badScenario.accepted, true);
  assert.equal(badScenario.state.market.cycleId, "cy-2", "new verified market is retained display-only");
  assert.equal(badScenario.state.market.cycleCommitted, false);
  assert.equal(badScenario.state.scenario, null, "invalid new scenario tombstones old arm");

  const marketFailure = applyEvent(state, { stream: "cycle", revision: 3,
    payload: { cycleId: "cy-market-invalid", market: null, scenario: null } }, T0);
  assert.equal(marketFailure.accepted, true, "market failure atomically disarms both halves");
  assert.equal(marketFailure.state.market, null);
  assert.equal(marketFailure.state.scenario, null);
  assert.equal(projectState(marketFailure.state, T0).display.orderable, false);
  const invalidDisarm = applyEvent(state, { stream: "cycle", revision: 4,
    payload: { cycleId: "cy-invalid-disarm", market: null, scenario: scenario("cy-invalid-disarm") } }, T0);
  assert.equal(invalidDisarm.accepted, true, "market-null with a scenario tombstones the old pair");
  assert.equal(invalidDisarm.state.scenario, null);

  const disarm = applyEvent(marketFailure.state, { stream: "cycle", revision: 4,
    payload: { cycleId: "cy-3", market: market("cy-3", 20002), scenario: null } }, T0);
  assert.equal(disarm.accepted, true);
  assert.equal(projectState(disarm.state, T0).display.orderable, false);

  const reversed = applyEvent(disarm.state, good, T0);
  assert.equal(reversed.accepted, true, "signed different-cycle rollback disarms instead of preserving an arm");
  assert.equal(reversed.state.scenario, null);
  assert.equal(reversed.state.market.cycleId, "cy-3", "rollback payload market is never adopted");
  assert.equal(reversed.state.market.cycleCommitted, false);
});

test("R16 different-cycle revision rollback is disarm-only and rev11 alone can rearm", () => {
  let state = emptyState("acct", "MNQU6");
  const rev10 = applyEvent(state, { stream: "cycle", revision: 10, payload: {
    cycleId: "cycle-rev10", market: market("cycle-rev10", 20010), scenario: scenario("cycle-rev10"),
  } }, T0);
  assert.equal(rev10.accepted, true);
  state = rev10.state;
  assert.equal(projectState({ ...state, positionCheck: { verified: true } }, T0).display.cyclePaired, true);

  const rollback = applyEvent(state, { stream: "cycle", revision: 9, payload: {
    cycleId: "cycle-rollback", market: market("cycle-rollback", 19900), scenario: scenario("cycle-rollback"),
  } }, T0 + 1);
  assert.equal(rollback.accepted, true);
  assert.match(rollback.reason, /CYCLE_ROLLBACK_DISARMED/);
  assert.equal(rollback.state.revisions.cycle, 10, "rollback cannot lower authoritative revision");
  assert.equal(rollback.state.market.cycleId, "cycle-rev10", "rollback market is not adopted");
  assert.equal(rollback.state.market.price, 20010);
  assert.equal(rollback.state.market.cycleCommitted, false);
  assert.equal(rollback.state.scenario, null);
  assert.equal(projectState(rollback.state, T0 + 1).display.orderable, false);

  const replay = applyEvent(rollback.state, { stream: "cycle", revision: 9, payload: {
    cycleId: "cycle-rollback", market: market("cycle-rollback", 19800), scenario: scenario("cycle-rollback"),
  } }, T0 + 2);
  assert.equal(replay.accepted, false, "same rollback retry is inert and cannot revive");
  assert.equal(replay.state.scenario, null);
  assert.equal(replay.state.market.price, 20010);

  const rev11 = applyEvent(replay.state, { stream: "cycle", revision: 11, payload: {
    cycleId: "cycle-rev11", market: market("cycle-rev11", 20011), scenario: scenario("cycle-rev11"),
  } }, T0 + 3);
  assert.equal(rev11.accepted, true);
  assert.equal(rev11.state.market.cycleCommitted, true);
  assert.equal(rev11.state.scenario.cycleCommitted, true);
  assert.equal(projectState({ ...rev11.state, positionCheck: { verified: true } }, T0 + 3).display.cyclePaired, true);
});

test("R16 cycle lease expires without producer contact and higher-revision heartbeat renews within max TTL", () => {
  let state = emptyState("acct", "MNQU6");
  const longScenario = scenario("lease-cycle", { expiresAt: iso(T0 + 900_000) });
  state = applyEvent(state, { stream: "cycle", revision: 1, payload: {
    cycleId: "lease-cycle", market: market("lease-cycle"), scenario: longScenario,
    leaseExpiresAt: iso(T0 + 86_400_000),
  } }, T0).state;
  state = { ...state, positionCheck: { verified: true, observedAt: iso() } };
  assert.equal(state.cycleLeaseExpiresAt, iso(T0 + 600_000), "producer lease is capped by Worker TTL");
  assert.equal(projectState(state, T0 + 599_000).display.orderable, true);
  const expired = projectState(state, T0 + 600_000);
  assert.equal(expired.display.orderable, false);
  assert.equal(expired.display.cyclePaired, false);
  assert.equal(expired.display.cycleSealReason, "CYCLE_LEASE_EXPIRED");

  const heartbeatAt = T0 + 500_000;
  const heartbeatMarket = { ...market("lease-cycle"), at: iso(heartbeatAt), observedAt: iso(heartbeatAt),
    cvdAt: iso(heartbeatAt) };
  const heartbeatScenario = scenario("lease-cycle", { issuedAt: iso(heartbeatAt), observedAt: iso(heartbeatAt),
    expiresAt: iso(heartbeatAt + 900_000) });
  const heartbeat = applyEvent(state, { stream: "cycle", revision: 2, payload: {
    cycleId: "lease-cycle", market: heartbeatMarket, scenario: heartbeatScenario,
  } }, heartbeatAt);
  assert.equal(heartbeat.accepted, true);
  assert.equal(heartbeat.state.cycleLeaseExpiresAt, iso(heartbeatAt + 600_000));
  assert.equal(projectState(heartbeat.state, T0 + 1_000_000).display.cyclePaired, true,
    "fresh higher-revision heartbeat renews the lease");
});

test("legacy market/scenario stream writes remain display-only and never arm", () => {
  let state = emptyState("acct", "MNQU6");
  const directMarket = applyEvent(state, { stream: "market", revision: 1,
    payload: { market: market("legacy-1") } }, T0);
  assert.equal(directMarket.accepted, true);
  state = directMarket.state;
  const directScenario = applyEvent(state, { stream: "scenario", revision: 1,
    payload: { scenario: { ...scenario("legacy-1"), strategyEvidence: evidence() } } }, T0);
  assert.equal(directScenario.accepted, true);
  const view = projectState(directScenario.state, T0);
  assert.equal(view.display.cyclePaired, false);
  assert.equal(view.display.orderable, false);
  assert.match(view.display.blockReason, /CYCLE_MISMATCH/);

  const directHashOnly = applyEvent(emptyState("acct", "MNQU6"), { stream: "scenario", revision: 1,
    payload: { scenario: scenario("legacy-hash-only") } }, T0);
  assert.equal(directHashOnly.accepted, false, "hash-only scenarios cannot arm through the direct stream");
  assert.match(directHashOnly.reason, /paired market/);

  const mismatchEvidence = applyEvent(emptyState("acct", "MNQU6"), { stream: "cycle", revision: 1,
    payload: { cycleId: "cy-evidence", market: market("cy-evidence"),
      scenario: scenario("cy-evidence", { evidenceHash: "se_000000000000000000000000" }) } }, T0);
  assert.equal(mismatchEvidence.accepted, true);
  assert.equal(mismatchEvidence.state.scenario, null);
  assert.match(mismatchEvidence.reason, /paired market/);

  const differentValue = { version: "R14-STRATEGY-EVIDENCE-1", asOf: iso(), sessionId: "NY-20260822",
    source: "fixture", provenance: "test", models: { ifvg: { valid: true, direction: "SELL" } } };
  const differentEvidence = { ...differentValue, evidenceHash: strategyEvidenceHash(differentValue) };
  const splitEvidence = applyEvent(emptyState("acct", "MNQU6"), { stream: "cycle", revision: 1,
    payload: { cycleId: "cy-split-evidence", market: market("cy-split-evidence"),
      scenario: scenario("cy-split-evidence", { strategyEvidence: differentEvidence,
        evidenceHash: differentEvidence.evidenceHash }) } }, T0);
  assert.equal(splitEvidence.accepted, true);
  assert.equal(splitEvidence.state.scenario, null);
  assert.match(splitEvidence.reason, /only evidenceHash/);
});

test("R16 cycle seal rechecks canonical evidence and tombstones every newer invalid pair", () => {
  let state = emptyState("acct", "MNQU6");
  const good = applyEvent(state, { stream: "cycle", revision: 1,
    payload: { cycleId: "seal-1", market: market("seal-1"), scenario: scenario("seal-1") } }, T0);
  assert.equal(good.accepted, true);
  state = { ...good.state, positionCheck: { verified: true, observedAt: iso() } };
  const live = projectState(state, T0);
  assert.equal(live.display.cyclePaired, true);
  assert.equal(live.display.orderable, true, "only a fully committed valid pair can arm");
  assert.equal((JSON.stringify(live).match(/"strategyEvidence":/g) || []).length, 1,
    "full evidence has exactly one state/output path");

  const oneBit = structuredClone(state);
  const originalHash = oneBit.market.strategyEvidence.evidenceHash;
  oneBit.market.strategyEvidence.evidenceHash = `${originalHash.slice(0, -1)}${originalHash.endsWith("0") ? "1" : "0"}`;
  const tampered = projectState(oneBit, T0);
  assert.equal(tampered.display.cyclePaired, false);
  assert.equal(tampered.display.orderable, false);
  assert.match(tampered.display.blockReason, /CYCLE_MISMATCH/);
  for (const partial of [
    { ...state, market: { ...state.market, cycleCommitted: false } },
    { ...state, scenario: { ...state.scenario, cycleCommitted: false } },
    { ...state, market: { ...state.market, strategyEvidence: null } },
  ]) {
    const view = projectState(partial, T0);
    assert.equal(view.display.orderable, false);
    assert.equal(view.display.cyclePaired, false);
  }

  const invalidScenario = applyEvent(state, { stream: "cycle", revision: 2, payload: {
    cycleId: "seal-2", market: market("seal-2", 20001), scenario: scenario("wrong-seal"),
  } }, T0);
  assert.equal(invalidScenario.accepted, true);
  assert.equal(invalidScenario.state.market.price, 20001, "verified newer market remains displayable");
  assert.equal(invalidScenario.state.market.cycleCommitted, false);
  assert.equal(invalidScenario.state.scenario, null, "old armed scenario is never retained");
  assert.equal(projectState(invalidScenario.state, T0).display.orderable, false);

  const invalidMarket = applyEvent(state, { stream: "cycle", revision: 3, payload: {
    cycleId: "seal-3", market: market("seal-3", 20000.13), scenario: scenario("seal-3"),
  } }, T0);
  assert.equal(invalidMarket.accepted, true);
  assert.equal(invalidMarket.state.market, null, "invalid market produces a full tombstone");
  assert.equal(invalidMarket.state.scenario, null);

  const malformedEvidence = applyEvent(state, { stream: "cycle", revision: 4, payload: {
    cycleId: "seal-4", market: { ...market("seal-4", 20002), strategyEvidence: { ...evidence(), extra: true } },
    scenario: scenario("seal-4"),
  } }, T0);
  assert.equal(malformedEvidence.accepted, true);
  assert.equal(malformedEvidence.state.market.price, 20002, "market price survives evidence-only failure");
  assert.equal(malformedEvidence.state.market.strategyEvidence, null);
  assert.equal(malformedEvidence.state.scenario, null);
  assert.equal(projectState(malformedEvidence.state, T0).display.orderable, false);
});
