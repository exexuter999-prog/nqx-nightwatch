import { test } from "node:test";
import assert from "node:assert/strict";
import { applyEvent, emptyState, strategyEvidenceHash, entryKeyForTuple,
  brokerObservationHash } from "../src/state_machine.js";

/**
 * R43: ブローカー観測スロットの口座載せ替え。
 *
 * accountScope が複数口座のとき観測は口座ごとに別々に届くが、保存スロットは
 * 1つしか無い。先に入った口座で固定すると、解放証明が **名指しで**要求する
 * accountScope[0] の観測を二度と書けなくなる。スロットが空くのは CLAIM 成功時
 * だけで、その CLAIM は解放証明を待っている —— 円環になって新規発注が恒久停止する。
 *
 * 2026-08-26 02:36 に実際に落ちた。口座B(=accountScope[1])の観測が 17:34:28Z に
 * 先着し、以後すべての口座A観測が 409 で弾かれ、両口座が FLAT・未終端注文ゼロに
 * なっても ENTRY claim が解放されなかった。
 */

const T0 = Date.parse("2026-08-23T12:00:00Z");
const iso = (ms = T0) => new Date(ms).toISOString();

const ACCOUNT_A = "APEX0001";
const ACCOUNT_B = "APEX0002";
const OUT_OF_SCOPE = "APEX9999";

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

/** 2口座スコープで CLAIM 済みの state を作る。 */
function twoAccountFixture() {
  const rawEvidence = { version: "R14-STRATEGY-EVIDENCE-1", asOf: iso(),
    sessionId: "NY-R43", source: "fixture", provenance: "test",
    models: { ifvg: { valid: false } } };
  const evidence = { ...rawEvidence, evidenceHash: strategyEvidenceHash(rawEvidence) };
  const cycleId = "cy-r43";
  const market = { at: iso(), observedAt: iso(), verified: true, source: "fixture",
    sourceSymbol: "CME_MINI:MNQU6", resolution: "3", barResolution: "3", price: 20000,
    cvdAt: iso(), cycleId, strategyEvidence: evidence,
    dayguard: { at: iso(), available: true, blocked: false, codes: [] },
    bars: [{ t: 1700000000, o: 20000, h: 20001, l: 19999, c: 20000 },
      { t: 1700000180, o: 20000, h: 20002, l: 20000, c: 20001 }] };
  const scenario = { scenarioId: "sc-r43", fingerprint: "fp-r43", state: "ARMED",
    symbol: "MNQU6", side: "BUY", qty: 2, entry: 20000, stop: 19940,
    target: 20060, targets: [20060, 20120],
    legs: [{ id: "TP1", qty: 1, target: 20060 }, { id: "RUNNER", qty: 1, target: 20120 }],
    planVersion: "R20-PLAN-1", grade: "A+", issuedAt: iso(), observedAt: iso(),
    expiresAt: iso(T0 + 300_000), sessionEndAt: iso(T0 + 240_000), marketCycleId: cycleId,
    setupVersion: "R20-S", catalogVersion: "R20-C", detectorVersion: "R20-D",
    executionContractVersion: "R22-EXECUTION-CONTRACT-1", evidenceHash: evidence.evidenceHash,
    executionContract: { version: "R22-EXECUTION-CONTRACT-1", riskCapDollars: 240,
      riskCapSource: "fixture", accountScope: [ACCOUNT_A, ACCOUNT_B] } };
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
  assert.deepEqual(claimed.state.entryClaim.executionIntent.accountScope, [ACCOUNT_A, ACCOUNT_B]);
  return claimed.state;
}

// currentIntentHash は封印前に載せる。あとから足すと snapshotHash が合わない。
const flatFor = (accountId, atMs, id, currentIntentHash, symbol = "MNQU6") =>
  sealObservation({
    observedAt: iso(atMs), accountId, symbol, currentIntentHash,
    position: { verified: true, qty: 0, side: "FLAT", positionGeneration: null },
    orders: [],
  }, id);

test("R43 accountScope 内なら口座を載せ替えて観測を書ける(先着口座が締め出さない)", () => {
  const state = twoAccountFixture();
  const hash = state.entryClaim.executionIntentHash;

  // 口座B(=accountScope[1])の観測が先着する。実障害と同じ順序。
  const first = applyEvent(state, { stream: "broker_observation", revision: 1,
    payload: { observation: flatFor(ACCOUNT_B, T0 + 1, "bs-b", hash) } },
    T0 + 1);
  assert.equal(first.accepted, true, first.reason);
  assert.equal(first.state.brokerObservation.accountId, ACCOUNT_B);

  // 解放証明が名指しする accountScope[0] の観測。修正前はここが 409 だった。
  const handoff = applyEvent(first.state, { stream: "broker_observation", revision: 2,
    payload: { observation: flatFor(ACCOUNT_A, T0 + 2, "bs-a", hash) } },
    T0 + 2);
  assert.equal(handoff.accepted, true, handoff.reason);
  assert.equal(handoff.state.brokerObservation.accountId, ACCOUNT_A);
  assert.equal(handoff.state.brokerObservation.position.qty, 0);
  assert.deepEqual(handoff.state.brokerObservation.orders, []);

  // 戻れることも確認する。片方向だけ通ると次は逆側が締め出される。
  const back = applyEvent(handoff.state, { stream: "broker_observation", revision: 3,
    payload: { observation: flatFor(ACCOUNT_B, T0 + 3, "bs-b2", hash) } },
    T0 + 3);
  assert.equal(back.accepted, true, back.reason);
  assert.equal(back.state.brokerObservation.accountId, ACCOUNT_B);
});

test("R43 accountScope 外の口座は従来どおり拒否される", () => {
  const state = twoAccountFixture();
  const hash = state.entryClaim.executionIntentHash;
  const first = applyEvent(state, { stream: "broker_observation", revision: 1,
    payload: { observation: flatFor(ACCOUNT_A, T0 + 1, "bs-a", hash) } },
    T0 + 1);
  assert.equal(first.accepted, true, first.reason);

  const foreign = applyEvent(first.state, { stream: "broker_observation", revision: 2,
    payload: { observation: flatFor(OUT_OF_SCOPE, T0 + 2, "bs-x", hash) } },
    T0 + 2);
  assert.equal(foreign.accepted, false);
  assert.match(foreign.reason, /cannot overwrite another scope\/intent/);
  assert.equal(first.state.brokerObservation.accountId, ACCOUNT_A);

  // 銘柄違いも載せ替え対象にしない。
  const otherSymbol = flatFor(ACCOUNT_B, T0 + 2, "bs-sym", hash, "MESU6");
  const symbolSwap = applyEvent(first.state, { stream: "broker_observation", revision: 2,
    payload: { observation: otherSymbol } }, T0 + 2);
  assert.equal(symbolSwap.accepted, false);
  assert.match(symbolSwap.reason, /cannot overwrite another scope\/intent/);
});

test("R43 同一口座の単調性とスナップショット整合は緩めていない", () => {
  const state = twoAccountFixture();
  const hash = state.entryClaim.executionIntentHash;
  const first = applyEvent(state, { stream: "broker_observation", revision: 1,
    payload: { observation: flatFor(ACCOUNT_A, T0 + 10, "bs-a", hash) } },
    T0 + 10);
  assert.equal(first.accepted, true, first.reason);

  const rewind = applyEvent(first.state, { stream: "broker_observation", revision: 2,
    payload: { observation: flatFor(ACCOUNT_A, T0 + 9, "bs-a2", hash) } },
    T0 + 10);
  assert.equal(rewind.accepted, false);
  assert.match(rewind.reason, /not monotonic/);

  // 同じ snapshotId を別の中身で使い回す経路は塞がったまま。
  const reused = applyEvent(first.state, { stream: "broker_observation", revision: 2,
    payload: { observation: flatFor(ACCOUNT_A, T0 + 11, "bs-a", hash) } },
    T0 + 11);
  assert.equal(reused.accepted, false);
  assert.match(reused.reason, /snapshot id was reused/);

  // 同時刻・同一内容は従来どおり冪等。
  const idempotent = applyEvent(first.state, { stream: "broker_observation", revision: 2,
    payload: { observation: flatFor(ACCOUNT_A, T0 + 10, "bs-a", hash) } },
    T0 + 10);
  assert.equal(idempotent.accepted, true);
  assert.equal(idempotent.reason, "BROKER_OBSERVATION_IDEMPOTENT");
});

test("R43 権威でない intent は載せ替え経路でも通らない", () => {
  const state = twoAccountFixture();
  const hash = state.entryClaim.executionIntentHash;
  const first = applyEvent(state, { stream: "broker_observation", revision: 1,
    payload: { observation: flatFor(ACCOUNT_B, T0 + 1, "bs-b", hash) } },
    T0 + 1);
  assert.equal(first.accepted, true, first.reason);

  const forged = applyEvent(first.state, { stream: "broker_observation", revision: 2,
    payload: { observation: flatFor(ACCOUNT_A, T0 + 2, "bs-a", "xi_" + "0".repeat(64)) } }, T0 + 2);
  assert.equal(forged.accepted, false);
  assert.match(forged.reason, /intent is not authoritative/);
});
