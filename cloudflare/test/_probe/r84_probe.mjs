import { applyEvent, emptyState, projectState, strategyEvidenceHash, entryKeyForTuple } from "../../src/state_machine.js";
const T0 = Date.parse("2026-09-12T02:12:00Z");
const iso = (ms = T0) => new Date(ms).toISOString();
const ACC = "LFF00000000000006";
function frozenEvidence() {
  const raw = { version: "R14-STRATEGY-EVIDENCE-1", asOf: iso(), sessionId: "NY-R84",
    source: "fixture", provenance: "test", models: { ifvg: { valid: false } } };
  return { ...raw, evidenceHash: strategyEvidenceHash(raw) };
}
function heldState({ cycleId = "cy-r84", qty = 8, baseQty = 4, avgEntry = 29484.25, grade = "A+", side = "SELL" } = {}) {
  const evidence = frozenEvidence();
  const market = { at: iso(), observedAt: iso(), verified: true, source: "fixture",
    sourceSymbol: "CME_MINI:MNQU6", resolution: "3", barResolution: "3", price: 29448.5,
    cvdAt: iso(), cycleId, dayguard: { at: iso(), available: true, blocked: false, codes: [] },
    bars: [{ t: 1700000000, o: 29450, h: 29452, l: 29447, c: 29449 },
      { t: 1700000180, o: 29449, h: 29450, l: 29446, c: 29448.5 }],
    levels: [], strategyEvidence: evidence };
  const scenario = { scenarioId: `sc-${cycleId}`, fingerprint: `fp-${cycleId}`, state: "ARMED",
    symbol: "MNQU6", side, qty, entry: 29448.5, stop: 29507.5, target: 29400,
    targets: [29400, 29300], planVersion: "R19-ICT-SPLIT-1", grade,
    legs: [{ id: "TP1", qty: Math.floor(qty / 2), target: 29400 },
      { id: "RUNNER", qty: qty - Math.floor(qty / 2), target: 29300 }],
    issuedAt: iso(), observedAt: iso(), expiresAt: iso(T0 + 300_000), marketCycleId: cycleId,
    setupVersion: "R14-SETUP", catalogVersion: "R14-CATALOG", detectorVersion: "R14-DETECTOR",
    executionContractVersion: "R22-EXECUTION-CONTRACT-1", evidenceHash: evidence.evidenceHash,
    executionContract: { version: "R22-EXECUTION-CONTRACT-1", riskCapDollars: 3100,
      riskCapSource: "ACCOUNT_DRAWDOWN_BUFFER", accountScope: [ACC] } };
  let r = applyEvent(emptyState("acct", "MNQU6"), { stream: "cycle", revision: 1, payload: { cycleId, market, scenario } }, T0);
  r = applyEvent(r.state, { stream: "position", revision: 1, payload: { position: { verified: true, source: "broker",
      symbol: "MNQU6", qty: baseQty, side: side === "BUY" ? "LONG" : "SHORT", avgEntry, state: "OPEN", observedAt: iso() } } }, T0);
  return { state: r.state, scenario: r.state.scenario };
}
const tuple = (s) => Object.fromEntries(["scenarioId","fingerprint","evidenceHash","marketCycleId"].map(f=>[f,s[f]]));
const PY = { baseQty: 4, baseAvgEntry: 29484.25, addQty: 4, addsDone: 0,
  positionGeneration: "PG:1", preSendOrderIds: ["O-1","O-2","O-3","O-4"] };

const held = heldState({ cycleId: "cy-stuck" });
const payload = { action: "CLAIM", entryKey: entryKeyForTuple(tuple(held.scenario)),
  tuple: tuple(held.scenario), claimTokenHash: "a".repeat(64), orderType: "MARKET", pyramid: PY };
const claimed = applyEvent(held.state, { stream: "entry_claim", revision: 2, payload }, T0);
console.log("pyramid CLAIM accepted:", claimed.accepted, claimed.reason || "");
console.log("claim state:", claimed.state.entryClaim.state, "routeState:", claimed.state.entryClaim.routeState);

// ---- order.py dry-run rejects -> engine writes PYRAMID_SKIPPED, never CONSUMEs.
// Much later the BASE position is stopped out and the account is FLAT.
const LATE = T0 + 6 * 3600_000;
const flat = JSON.parse(JSON.stringify(claimed.state));
flat.position = { verified: true, source: "broker", symbol: "MNQU6", qty: 0, state: "CLOSED", observedAt: iso(LATE) };
flat.positionCheck = { verified: true, observedAt: iso(LATE) };
flat.brokerObservation = {
  observedAt: iso(LATE - 5_000), positionObservedAt: iso(LATE - 5_000), ordersObservedAt: iso(LATE - 5_000),
  snapshotId: "snap-1", cursor: "c1", snapshotMode: "STABLE", stableBeforeHash: "h", stableAfterHash: "h",
  platform: "CROSSTRADE", accountId: ACC, symbol: "MNQU6",
  currentIntentHash: claimed.state.entryClaim.executionIntentHash,
  position: { qty: 0, side: null, positionGeneration: "PG:1" },
  orders: [],                       // broker is completely empty
};
console.log("broker is FLAT + zero live orders. staleReleasable =",
  projectState(flat, LATE).entryClaim.staleReleasable);

// A brand new (non-pyramid) A+ signal arrives.
const next = heldState({ cycleId: "cy-next" });
const nextState = JSON.parse(JSON.stringify(next.state));
nextState.entryClaim = flat.entryClaim;
nextState.brokerObservation = flat.brokerObservation;
nextState.position = flat.position; nextState.positionCheck = flat.positionCheck;
const newClaim = applyEvent(nextState, { stream: "entry_claim", revision: 9,
  payload: { action: "CLAIM", entryKey: entryKeyForTuple(tuple(next.scenario)),
    tuple: tuple(next.scenario), claimTokenHash: "d".repeat(64), orderType: "LIMIT" } }, LATE);
console.log("new normal CLAIM accepted:", newClaim.accepted, "reason:", newClaim.reason);

// And RECOVER cannot clear it either (state is CLAIMED, routeState null).
const rec = applyEvent(flat, { stream: "entry_claim", revision: 8,
  payload: { action: "RECOVER", entryKey: payload.entryKey, claimTokenHash: payload.claimTokenHash,
    brokerSnapshotHash: flat.brokerObservation.snapshotHash,
    brokerSnapshotId: "snap-1", brokerCursor: "c1" } }, LATE);
console.log("RECOVER accepted:", rec.accepted, "reason:", rec.reason);
