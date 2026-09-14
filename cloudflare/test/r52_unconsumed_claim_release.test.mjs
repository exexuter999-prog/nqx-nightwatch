// R52: 一度も CONSUME されなかった ENTRY claim は、claim.maxAgeSec(180 秒)を過ぎたら
// 不在証明(新鮮な観測・建玉 0・未終端注文なし)で即解放する。
//
// 背景: 2026-09-05 01:54、engine の dry-run が argparse で落ち(claim token が `-` 始まり)、
// CLAIM だけ立って CONSUME に届かなかった。180 秒後には CONSUME 自体が
// ENTRY_CLAIM_EXPIRED で拒否されるので注文は生めないのに、CLAIM 側は
// staleReleaseSec(900 秒)まで ALREADY_HELD を返し、A+ 候補が 15 分止まった。
// CONSUME 済み(送ったかもしれない)claim の 900 秒規則は変えない。
import { test } from "node:test";
import assert from "node:assert/strict";
import { applyEvent, emptyState, strategyEvidenceHash, entryKeyForTuple,
  brokerObservationHash } from "../src/state_machine.js";
import executionContract from "../../execution_contract.json" with { type: "json" };

const T0 = Date.parse("2026-09-05T00:00:00Z");
const CONSUME_MS = Number(executionContract.claim.maxAgeSec) * 1000;       // 180 s
const STALE_MS = Number(executionContract.claim.staleReleaseSec) * 1000;   // 900 s
const iso = (ms = T0) => new Date(ms).toISOString();

function frozenEvidence() {
  const raw = { version: "R14-STRATEGY-EVIDENCE-1", asOf: iso(), sessionId: "NY-20260905",
    source: "fixture", provenance: "test", models: { ifvg: { valid: false } } };
  return { ...raw, evidenceHash: strategyEvidenceHash(raw) };
}

function marketAt(nowMs, cycleId, evidence) {
  return { at: iso(nowMs), observedAt: iso(nowMs), verified: true, source: "fixture",
    sourceSymbol: "CME_MINI:MNQU6", resolution: "3", barResolution: "3", price: 20000,
    cvdAt: iso(nowMs), cycleId, dayguard: { at: iso(nowMs), available: true, blocked: false, codes: [] },
    bars: [{ t: 1700000000, o: 19999, h: 20000, l: 19998, c: 19999 },
      { t: 1700000180, o: 19999, h: 20001, l: 19999, c: 20000 }],
    levels: [], strategyEvidence: evidence };
}

function sealedState(cycleId = "cy-r52") {
  const evidence = frozenEvidence();
  const scenario = { scenarioId: `sc-${cycleId}`, fingerprint: `fp-${cycleId}`, state: "ARMED",
    symbol: "MNQU6", side: "BUY", qty: 2, entry: 20000, stop: 19940, target: 20060,
    targets: [20060, 20120], planVersion: "R17-SPLIT-1", grade: "A",
    legs: [{ id: "TP1", qty: 1, target: 20060 }, { id: "RUNNER", qty: 1, target: 20120 }],
    issuedAt: iso(), observedAt: iso(), expiresAt: iso(T0 + 3_600_000), marketCycleId: cycleId,
    setupVersion: "R14-SETUP", catalogVersion: "R14-CATALOG", detectorVersion: "R14-DETECTOR",
    executionContractVersion: "R22-EXECUTION-CONTRACT-1", evidenceHash: evidence.evidenceHash,
    executionContract: { version: "R22-EXECUTION-CONTRACT-1", riskCapDollars: 240,
      riskCapSource: "fixture", accountScope: ["APEX0001"] } };
  let r = applyEvent(emptyState("acct", "MNQU6"),
    { stream: "cycle", revision: 1, payload: { cycleId, market: marketAt(T0, cycleId, evidence), scenario } }, T0);
  assert.equal(r.accepted, true);
  r = applyEvent(r.state, { stream: "position", revision: 1,
    payload: { position: { verified: true, source: "broker", symbol: "MNQU6", qty: 0, observedAt: iso() } } }, T0);
  assert.equal(r.accepted, true);
  return { state: r.state, scenario: r.state.scenario };
}

function heartbeat(state, nowMs, revision) {
  const evidence = frozenEvidence();
  const cycleId = state.market?.cycleId || "cy-r52";
  const scenario = { ...state.scenario, issuedAt: iso(nowMs), observedAt: iso(nowMs),
    expiresAt: iso(nowMs + 600_000), evidenceHash: evidence.evidenceHash };
  const r = applyEvent(state, { stream: "cycle", revision,
    payload: { cycleId, market: marketAt(nowMs, cycleId, evidence), scenario } }, nowMs);
  assert.equal(r.accepted, true, r.reason || "heartbeat rejected");
  return r.state;
}

const tuple = (s) => Object.fromEntries(
  ["scenarioId", "fingerprint", "evidenceHash", "marketCycleId"].map((k) => [k, s[k]]));

/** CLAIM だけ立てて CONSUME に届かなかった状態(dry-run 失敗の再現)。 */
function unconsumedClaim() {
  const sealed = sealedState();
  const entryKey = entryKeyForTuple(tuple(sealed.scenario));
  const payload = { action: "CLAIM", entryKey, tuple: tuple(sealed.scenario),
    claimTokenHash: "a".repeat(64), orderType: "LIMIT" };
  const claimed = applyEvent(sealed.state, { stream: "entry_claim", revision: 1, payload }, T0);
  assert.equal(claimed.accepted, true);
  assert.equal(claimed.state.entryClaim.state, "CLAIMED");
  return { state: claimed.state, entryKey, payload,
    intentHash: claimed.state.entryClaim.executionIntentHash };
}

/** CLAIM → CONSUME まで進んだ(送ったかもしれない)状態。 */
function consumedClaim() {
  const base = unconsumedClaim();
  const consumed = applyEvent(base.state, { stream: "entry_claim", revision: 2,
    payload: { action: "CONSUME", entryKey: base.entryKey, claimTokenHash: base.payload.claimTokenHash,
      executionIntent: base.state.entryClaim.executionIntent,
      executionIntentHash: base.intentHash } }, T0);
  assert.equal(consumed.accepted, true);
  return { ...base, state: consumed.state };
}

function observe(state, nowMs, intentHash, orders, revision, qty = 0) {
  const at = iso(nowMs);
  const observation = {
    observedAt: at, positionObservedAt: at, ordersObservedAt: at,
    snapshotId: `snap-${nowMs}`, cursor: `cur-${nowMs}`, snapshotMode: "BROKER_CURSOR",
    stableBeforeHash: null, stableAfterHash: null,
    platform: "STUB", accountId: "APEX0001", symbol: "MNQU6",
    currentIntentHash: intentHash,
    position: { verified: true, qty, side: qty > 0 ? "LONG" : "FLAT",
      positionGeneration: qty > 0 ? "gen-1" : null, rawPositionIdentity: qty > 0 ? "pos-1" : null },
    orders,
  };
  const canonical = {
    ...observation,
    orders: [...orders].map((row) => ({
      orderId: String(row.orderId), receipt: String(row.receipt),
      accountId: String(row.accountId), symbol: String(row.symbol),
      status: String(row.status).toUpperCase(),
    })).sort((l, r) => `${l.orderId} ${l.receipt}`.localeCompare(`${r.orderId} ${r.receipt}`)),
  };
  canonical.snapshotHash = brokerObservationHash(canonical);
  return applyEvent(state, { stream: "broker_observation", revision, payload: { observation: canonical } }, nowMs);
}

function reclaim(state, payload, nowMs, revision) {
  return applyEvent(state, { stream: "entry_claim", revision,
    payload: { ...payload, claimTokenHash: "b".repeat(64) } }, nowMs);
}

test("未消費の claim は maxAgeSec を過ぎ、空を証明できれば解放する", () => {
  const { state, payload, intentHash, entryKey } = unconsumedClaim();
  const later = T0 + CONSUME_MS + 20_000;            // 180 秒超、900 秒未満
  assert.ok(later - T0 < STALE_MS);
  const obs = observe(heartbeat(state, later, 3), later, intentHash, [], 4);
  assert.equal(obs.accepted, true, obs.reason || "observation rejected");
  const retry = reclaim(obs.state, payload, later, 5);
  assert.equal(retry.accepted, true, retry.reason || "解放されるべき");
  assert.match(retry.reason || "", /STALE_RELEASED/);
  assert.equal(retry.state.entryClaim.state, "CLAIMED", "新しい claim が立つ");
  assert.equal(retry.state.entryStaleRelease?.entryKey, entryKey);
});

test("未消費でも maxAgeSec 以内(まだ CONSUME できる)は解放しない", () => {
  const { state, payload, intentHash } = unconsumedClaim();
  const soon = T0 + CONSUME_MS - 30_000;
  const obs = observe(heartbeat(state, soon, 3), soon, intentHash, [], 4);
  assert.equal(obs.accepted, true);
  const retry = reclaim(obs.state, payload, soon, 5);
  assert.equal(retry.accepted, false);
  assert.match(retry.reason, /ALREADY_HELD/);
});

test("未消費でも空の証明が無ければ解放しない", () => {
  const { state, payload } = unconsumedClaim();
  const later = T0 + CONSUME_MS + 20_000;
  const retry = reclaim(heartbeat(state, later, 3), payload, later, 4);
  assert.equal(retry.accepted, false);
  assert.match(retry.reason, /ALREADY_HELD/);
});

test("未消費でも未終端の注文が板にあれば解放しない", () => {
  const { state, payload, intentHash } = unconsumedClaim();
  const later = T0 + CONSUME_MS + 20_000;
  const obs = observe(heartbeat(state, later, 3), later, intentHash,
    [{ orderId: "O-1", receipt: "R-1", accountId: "APEX0001", symbol: "MNQU6", status: "WORKING" }], 4);
  assert.equal(obs.accepted, true);
  const retry = reclaim(obs.state, payload, later, 5);
  assert.equal(retry.accepted, false);
  assert.match(retry.reason, /ALREADY_HELD/);
});

test("CONSUME 済み(送ったかもしれない)claim は従来どおり 900 秒まで解放しない", () => {
  const { state, payload, intentHash } = consumedClaim();
  const later = T0 + CONSUME_MS + 20_000;            // 180 秒超、900 秒未満
  const obs = observe(heartbeat(state, later, 3), later, intentHash, [], 4);
  assert.equal(obs.accepted, true);
  const retry = reclaim(obs.state, payload, later, 5);
  assert.equal(retry.accepted, false, "CONSUMED は 900 秒規則のまま");
  assert.match(retry.reason, /ALREADY_HELD/);
});
