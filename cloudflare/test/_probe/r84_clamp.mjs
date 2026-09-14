import { applyEvent, emptyState, strategyEvidenceHash, entryKeyForTuple } from "../../src/state_machine.js";
const T0 = Date.parse("2026-09-12T02:12:00Z");
const ACC = "LFF05062316710006";
const iso = (ms=T0) => new Date(ms).toISOString();
const ev = (() => { const raw = { version:"R14-STRATEGY-EVIDENCE-1", asOf:iso(), sessionId:"s",
  source:"f", provenance:"t", models:{ifvg:{valid:false}} }; return {...raw, evidenceHash: strategyEvidenceHash(raw)}; })();
const market = { at: iso(), observedAt: iso(), verified: true, source: "fixture",
  sourceSymbol: "CME_MINI:MNQU6", resolution:"3", barResolution:"3", price: 29448.5, cvdAt: iso(),
  cycleId: "cy", dayguard: { at: iso(), available: true, blocked: false, codes: [] },
  bars: [{t:1,o:29450,h:29452,l:29447,c:29449},{t:2,o:29449,h:29450,l:29446,c:29448.5}],
  levels: [], strategyEvidence: ev };
const scenario = { scenarioId:"sc", fingerprint:"fp", state:"ARMED", symbol:"MNQU6", side:"SELL",
  qty: 8, entry: 29448.5, stop: 29507.5, target: 29400, targets:[29400,29300],
  planVersion:"R19-ICT-SPLIT-1", grade:"A+", legs:[{id:"TP1",qty:4,target:29400},{id:"RUNNER",qty:4,target:29300}],
  issuedAt: iso(), observedAt: iso(), expiresAt: iso(T0+300000), marketCycleId:"cy",
  setupVersion:"R14-SETUP", catalogVersion:"R14-CATALOG", detectorVersion:"R14-DETECTOR",
  executionContractVersion:"R22-EXECUTION-CONTRACT-1", evidenceHash: ev.evidenceHash,
  executionContract:{ version:"R22-EXECUTION-CONTRACT-1", riskCapDollars: 500,
    riskCapSource:"ACCOUNT_DRAWDOWN_BUFFER", accountScope:[ACC] } };
let r = applyEvent(emptyState("acct","MNQU6"), {stream:"cycle",revision:1,payload:{cycleId:"cy",market,scenario}}, T0);
r = applyEvent(r.state, {stream:"position",revision:1,payload:{position:{verified:true,source:"broker",
  symbol:"MNQU6",qty:4,side:"SHORT",avgEntry:29484.25,state:"OPEN",observedAt:iso()}}}, T0);
const s = r.state.scenario;
const tup = Object.fromEntries(["scenarioId","fingerprint","evidenceHash","marketCycleId"].map(f=>[f,s[f]]));
for (const addQty of [2, 4]) {
  const out = applyEvent(r.state, { stream:"entry_claim", revision:2, payload:{ action:"CLAIM",
    entryKey: entryKeyForTuple(tup), tuple: tup, claimTokenHash:"a".repeat(64), orderType:"MARKET",
    pyramid: { baseQty:4, baseAvgEntry:29484.25, addQty, addsDone:0, positionGeneration:"PG:1",
      preSendOrderIds:["O-1","O-2"] } } }, T0);
  console.log(`addQty=${addQty} (python _fit_to_cap picked 2) -> accepted=${out.accepted} reason=${out.reason}`);
}
