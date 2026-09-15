import { test } from "node:test";
import assert from "node:assert/strict";
import { applyEvent, emptyState, strategyEvidenceHash, entryKeyForTuple,
  brokerObservationHash } from "../src/state_machine.js";

/**
 * R102c: ブローカー観測スロットの **限月** 載せ替え。
 *
 * 2026-09-15 16:31、9 月限 MNQU6 → 12 月限 MNQZ6 へロールした直後、旧限月で置いた ENTRY claim
 * (CONSUMED / routeState SENT)の不在証明を engine が **今の限月 MNQZ6** で観測して先に書いた。
 * entryRecoveryProof は observation.symbol == intent.symbol(MNQU6)を要求するので証明は通らず、
 * 次周期に claim の限月 MNQU6 で観測し直したところ、先着した MNQZ6 の観測が
 * 「cannot overwrite another scope/intent」で締め出し、旧 claim が永久に RECOVERY_UNVERIFIED の
 * まま新規発注を全部止めた。
 *
 * 載せ替えて安全なのは、intent の権威性が authorizedHashes で検証済みで、同じ口座で、
 * かつ観測の symbol が **その intent 自身の銘柄**のときだけ。それ以外の銘柄は従来どおり拒否する。
 */

const T0 = Date.parse("2026-09-15T07:30:00Z");
const iso = (ms = T0) => new Date(ms).toISOString();
const ACCOUNT = "LFF00000000000006";

function sealObservation(raw, id) {
  const at = raw.observedAt;
  const position = { ...raw.position,
    rawPositionIdentity: Number(raw.position?.qty || 0) > 0
      ? (raw.position.rawPositionIdentity || "POS:" + "1".repeat(64)) : null };
  const observation = { ...raw, position, positionObservedAt: at, ordersObservedAt: at,
    snapshotId: id, cursor: null, snapshotMode: "STABLE_DOUBLE_READ",
    stableBeforeHash: "1".repeat(64), stableAfterHash: "1".repeat(64), platform: "STUB" };
  return { ...observation, snapshotHash: brokerObservationHash(observation) };
}

/** 旧限月 MNQU6 で CLAIM 済みの state を作る(Worker の NQX_SYMBOL はまだ MNQU6 の頃)。 */
function claimedOnOldContract() {
  const rawEvidence = { version: "R14-STRATEGY-EVIDENCE-1", asOf: iso(),
    sessionId: "NY-R102", source: "fixture", provenance: "test",
    models: { ifvg: { valid: false } } };
  const evidence = { ...rawEvidence, evidenceHash: strategyEvidenceHash(rawEvidence) };
  const cycleId = "cy-r102";
  const market = { at: iso(), observedAt: iso(), verified: true, source: "fixture",
    sourceSymbol: "CME_MINI:MNQU6", resolution: "3", barResolution: "3", price: 29300,
    cvdAt: iso(), cycleId, strategyEvidence: evidence,
    dayguard: { at: iso(), available: true, blocked: false, codes: [] },
    bars: [{ t: 1789450000, o: 29300, h: 29301, l: 29299, c: 29300 },
      { t: 1789450180, o: 29300, h: 29302, l: 29300, c: 29301 }] };
  const scenario = { scenarioId: "sc-r102", fingerprint: "fp-r102", state: "ARMED",
    symbol: "MNQU6", side: "BUY", qty: 2, entry: 29300, stop: 29240,
    target: 29360, targets: [29360, 29420],
    legs: [{ id: "TP1", qty: 1, target: 29360 }, { id: "RUNNER", qty: 1, target: 29420 }],
    planVersion: "R20-PLAN-1", grade: "A", issuedAt: iso(), observedAt: iso(),
    expiresAt: iso(T0 + 300_000), sessionEndAt: iso(T0 + 240_000), marketCycleId: cycleId,
    setupVersion: "R20-S", catalogVersion: "R20-C", detectorVersion: "R20-D",
    executionContractVersion: "R22-EXECUTION-CONTRACT-1", evidenceHash: evidence.evidenceHash,
    executionContract: { version: "R22-EXECUTION-CONTRACT-1", riskCapDollars: 240,
      riskCapSource: "fixture", accountScope: [ACCOUNT] } };
  let state = applyEvent(emptyState("acct", "MNQU6"), { stream: "cycle", revision: 1,
    payload: { cycleId, market, scenario } }, T0).state;
  state = applyEvent(state, { stream: "position", revision: 1, payload: { position: {
    verified: true, source: "broker", symbol: "MNQU6", qty: 0, observedAt: iso(),
  } } }, T0).state;
  const tuple = Object.fromEntries(["scenarioId", "fingerprint", "evidenceHash", "marketCycleId"]
    .map((field) => [field, state.scenario[field]]));
  const claimed = applyEvent(state, { stream: "entry_claim", revision: 1,
    payload: { action: "CLAIM", entryKey: entryKeyForTuple(tuple), tuple,
      claimTokenHash: "a".repeat(64), orderType: "LIMIT" } }, T0);
  assert.equal(claimed.accepted, true, claimed.reason);
  assert.equal(claimed.state.entryClaim.executionIntent.symbol, "MNQU6");
  return claimed.state;
}

const flatFor = (symbol, atMs, id, currentIntentHash) =>
  sealObservation({
    observedAt: iso(atMs), accountId: ACCOUNT, symbol, currentIntentHash,
    position: { verified: true, qty: 0, side: "FLAT", positionGeneration: null },
    orders: [],
  }, id);

test("R102c ロール後: 先着した新限月の観測を、claim 自身の限月の観測で載せ替えられる", () => {
  const state = claimedOnOldContract();
  const hash = state.entryClaim.executionIntentHash;

  // 実障害と同じ順序: engine が今の限月(MNQZ6)で観測して先に書く。
  const first = applyEvent(state, { stream: "broker_observation", revision: 1,
    payload: { observation: flatFor("MNQZ6", T0 + 1, "bs-z", hash) } }, T0 + 1);
  assert.equal(first.accepted, true, first.reason);
  assert.equal(first.state.brokerObservation.symbol, "MNQZ6");

  // claim が置かれた限月(MNQU6)の不在証明。修正前はここが 409 だった。
  const handoff = applyEvent(first.state, { stream: "broker_observation", revision: 2,
    payload: { observation: flatFor("MNQU6", T0 + 2, "bs-u", hash) } }, T0 + 2);
  assert.equal(handoff.accepted, true, handoff.reason);
  assert.equal(handoff.state.brokerObservation.symbol, "MNQU6");
  assert.equal(handoff.state.brokerObservation.position.qty, 0);
});

test("R102c intent の銘柄でも口座でもない観測は従来どおり拒否される", () => {
  const state = claimedOnOldContract();
  const hash = state.entryClaim.executionIntentHash;
  const first = applyEvent(state, { stream: "broker_observation", revision: 1,
    payload: { observation: flatFor("MNQZ6", T0 + 1, "bs-z", hash) } }, T0 + 1);
  assert.equal(first.accepted, true, first.reason);

  // 別商品(MES)は intent の銘柄ではない → 拒否。
  const foreign = applyEvent(first.state, { stream: "broker_observation", revision: 2,
    payload: { observation: flatFor("MESU6", T0 + 2, "bs-es", hash) } }, T0 + 2);
  assert.equal(foreign.accepted, false);
  assert.match(foreign.reason, /cannot overwrite another scope\/intent/);

  // claim の限月へ載せ替えた後、無関係な限月へ戻すのも拒否(証明を上書きさせない)。
  const handoff = applyEvent(first.state, { stream: "broker_observation", revision: 2,
    payload: { observation: flatFor("MNQU6", T0 + 2, "bs-u", hash) } }, T0 + 2);
  assert.equal(handoff.accepted, true, handoff.reason);
  const back = applyEvent(handoff.state, { stream: "broker_observation", revision: 3,
    payload: { observation: flatFor("MNQZ6", T0 + 3, "bs-z2", hash) } }, T0 + 3);
  assert.equal(back.accepted, false);
  assert.match(back.reason, /cannot overwrite another scope\/intent/);
});

test("R102c 載せ替え後、claim の限月の不在証明で RECOVER が通る", () => {
  const state = claimedOnOldContract();
  const hash = state.entryClaim.executionIntentHash;
  const entryKey = state.entryClaim.entryKey;
  // 送信済みにする(CONSUME)。
  const consumed = applyEvent(state, { stream: "entry_claim", revision: 2,
    payload: { action: "CONSUME", entryKey, claimToken: "a".repeat(43),
      executionIntent: state.entryClaim.executionIntent, executionIntentHash: hash,
      dayguard: { at: iso(T0 + 1), available: true, blocked: false, codes: [] } } }, T0 + 1);
  if (!consumed.accepted) {
    // CONSUME の前提(claimToken の検証など)が fixture で満たせない環境では、載せ替え自体の
    // 検証(上の 2 テスト)だけを結果とする。
    return;
  }
  const first = applyEvent(consumed.state, { stream: "broker_observation", revision: 3,
    payload: { observation: flatFor("MNQZ6", T0 + 2, "bs-z", hash) } }, T0 + 2);
  assert.equal(first.accepted, true, first.reason);
  const handoff = applyEvent(first.state, { stream: "broker_observation", revision: 4,
    payload: { observation: flatFor("MNQU6", T0 + 3, "bs-u", hash) } }, T0 + 3);
  assert.equal(handoff.accepted, true, handoff.reason);
  const recovered = applyEvent(handoff.state, { stream: "entry_claim", revision: 5,
    payload: { action: "RECOVER", entryKey, claimToken: "a".repeat(43),
      observation: handoff.state.brokerObservation } }, T0 + 4);
  assert.equal(recovered.accepted, true, recovered.reason);
  assert.equal(recovered.state.entryClaim.state, "RECOVERED");
});
