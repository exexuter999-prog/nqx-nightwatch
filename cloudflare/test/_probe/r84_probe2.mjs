import { applyEvent, emptyState, projectState, strategyEvidenceHash, entryKeyForTuple } from "../../src/state_machine.js";
const T0 = Date.parse("2026-09-12T02:12:00Z");
const ACC = "LFF05062316710006";
const iso = (ms) => new Date(ms).toISOString();
function frozenEvidence(now) {
  const raw = { version: "R14-STRATEGY-EVIDENCE-1", asOf: iso(now), sessionId: "NY-R84",
    source: "fixture", provenance: "test", models: { ifvg: { valid: false } } };
  return { ...raw, evidenceHash: strategyEvidenceHash(raw) };
}
function build({ now, cycleId, qty = 8, baseQty = 4, avgEntry = 29484.25, grade = "A+", side = "SELL" }) {
  const evidence = frozenEvidence(now);
  const market = { at: iso(now), observedAt: iso(now), verified: true, source: "fixture",
    sourceSymbol: "CME_MINI:MNQU6", resolution: "3", barResolution: "3", price: 29448.5,
    cvdAt: iso(now), cycleId, dayguard: { at: iso(now), available: true, blocked: false, codes: [] },
    bars: [{ t: 1700000000, o: 29450, h: 29452, l: 29447, c: 29449 },
      { t: 1700000180, o: 29449, h: 29450, l: 29446, c: 29448.5 }],
    levels: [], strategyEvidence: evidence };
  const scenario = { scenarioId: `sc-${cycleId}`, fingerprint: `fp-${cycleId}`, state: "ARMED",
    symbol: "MNQU6", side, qty, entry: 29448.5, stop: 29507.5, target: 29400,
    targets: [29400, 29300], planVersion: "R19-ICT-SPLIT-1", grade,
    legs: [{ id: "TP1", qty: Math.floor(qty / 2), target: 29400 },
      { id: "RUNNER", qty: qty - Math.floor(qty / 2), target: 29300 }],
    issuedAt: iso(now), observedAt: iso(now), expiresAt: iso(now + 300_000), marketCycleId: cycleId,
    setupVersion: "R14-SETUP", catalogVersion: "R14-CATALOG", detectorVersion: "R14-DETECTOR",
    executionContractVersion: "R22-EXECUTION-CONTRACT-1", evidenceHash: evidence.evidenceHash,
    executionContract: { version: "R22-EXECUTION-CONTRACT-1", riskCapDollars: 3100,
      riskCapSource: "ACCOUNT_DRAWDOWN_BUFFER", accountScope: [ACC] } };
  let r = applyEvent(emptyState("acct", "MNQU6"), { stream: "cycle", revision: 1, payload: { cycleId, market, scenario } }, now);
  if (!r.accepted) throw new Error("cycle " + r.reason);
  r = applyEvent(r.state, { stream: "position", revision: 1, payload: { position: { verified: true, source: "broker",
      symbol: "MNQU6", qty: baseQty, side: side === "BUY" ? "LONG" : "SHORT", avgEntry,
      state: baseQty ? "OPEN" : "CLOSED", observedAt: iso(now) } } }, now);
  if (!r.accepted) throw new Error("position " + r.reason);
  return { state: r.state, scenario: r.state.scenario };
}
const tuple = (s) => Object.fromEntries(["scenarioId","fingerprint","evidenceHash","marketCycleId"].map(f=>[f,s[f]]));
const PY = { baseQty: 4, baseAvgEntry: 29484.25, addQty: 4, addsDone: 0,
  positionGeneration: "PG:1", preSendOrderIds: ["O-1","O-2","O-3","O-4"] };

// 1) pyramid CLAIM granted while 4 contracts are held
const held = build({ now: T0, cycleId: "cy-stuck" });
const payload = { action: "CLAIM", entryKey: entryKeyForTuple(tuple(held.scenario)),
  tuple: tuple(held.scenario), claimTokenHash: "a".repeat(64), orderType: "MARKET", pyramid: PY };
const claimed = applyEvent(held.state, { stream: "entry_claim", revision: 2, payload }, T0);
console.log("pyramid CLAIM:", claimed.accepted, "| state:", claimed.state.entryClaim.state,
            "| routeState:", claimed.state.entryClaim.routeState);

// 2) order.py dry-run rejects -> no CONSUME.  Hours later the base position is stopped out.
const LATE = T0 + 6 * 3600_000;
const nextCycle = build({ now: LATE, cycleId: "cy-next", baseQty: 0 });
const stuck = JSON.parse(JSON.stringify(nextCycle.state));
stuck.entryClaim = JSON.parse(JSON.stringify(claimed.state.entryClaim));
stuck.positionCheck = { verified: true, observedAt: iso(LATE) };
stuck.brokerObservation = {
  observedAt: iso(LATE - 5_000), positionObservedAt: iso(LATE - 5_000), ordersObservedAt: iso(LATE - 5_000),
  snapshotId: "snap-1", cursor: "c1", snapshotMode: "STABLE", stableBeforeHash: "h", stableAfterHash: "h",
  platform: "CROSSTRADE", accountId: ACC, symbol: "MNQU6",
  currentIntentHash: stuck.entryClaim.executionIntentHash,
  position: { qty: 0, side: null, positionGeneration: "PG:1" },
  orders: [],                        // broker completely empty: no position, no live orders
};
const proj = projectState(stuck, LATE);
console.log("broker FLAT + no live orders -> staleReleasable:", proj.entryClaim.staleReleasable,
            "| display.orderable:", proj.display.orderable, "|", proj.display.blockReason);

// 3) a brand new, ordinary A+ entry claim
const fresh = applyEvent(stuck, { stream: "entry_claim", revision: 9,
  payload: { action: "CLAIM", entryKey: entryKeyForTuple(tuple(nextCycle.scenario)),
    tuple: tuple(nextCycle.scenario), claimTokenHash: "d".repeat(64), orderType: "LIMIT" } }, LATE);
console.log("new ORDINARY CLAIM:", fresh.accepted, "| reason:", fresh.reason);

// 4) can RECOVER clear it?
const rec = applyEvent(stuck, { stream: "entry_claim", revision: 8,
  payload: { action: "RECOVER", entryKey: payload.entryKey, claimTokenHash: payload.claimTokenHash,
    brokerSnapshotHash: stuck.brokerObservation.snapshotHash, brokerSnapshotId: "snap-1", brokerCursor: "c1" } }, LATE);
console.log("RECOVER:", rec.accepted, "| reason:", rec.reason);

// 5) control: identical situation with an ORDINARY stuck claim releases fine
const ctlHeld = build({ now: T0, cycleId: "cy-ctl", baseQty: 0 });
const ctlPayload = { action: "CLAIM", entryKey: entryKeyForTuple(tuple(ctlHeld.scenario)),
  tuple: tuple(ctlHeld.scenario), claimTokenHash: "e".repeat(64), orderType: "LIMIT" };
const ctlClaimed = applyEvent(ctlHeld.state, { stream: "entry_claim", revision: 2, payload: ctlPayload }, T0);
const ctlStuck = JSON.parse(JSON.stringify(nextCycle.state));
ctlStuck.entryClaim = JSON.parse(JSON.stringify(ctlClaimed.state.entryClaim));
ctlStuck.positionCheck = { verified: true, observedAt: iso(LATE) };
ctlStuck.brokerObservation = { ...stuck.brokerObservation,
  currentIntentHash: ctlStuck.entryClaim.executionIntentHash };
console.log("control (non-pyramid stuck claim) staleReleasable:",
  projectState(ctlStuck, LATE).entryClaim.staleReleasable);
const ctlFresh = applyEvent(ctlStuck, { stream: "entry_claim", revision: 9,
  payload: { action: "CLAIM", entryKey: entryKeyForTuple(tuple(nextCycle.scenario)),
    tuple: tuple(nextCycle.scenario), claimTokenHash: "f".repeat(64), orderType: "LIMIT" } }, LATE);
console.log("control new ORDINARY CLAIM:", ctlFresh.accepted, "| reason:", ctlFresh.reason);
