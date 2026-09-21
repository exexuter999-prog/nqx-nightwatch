/**
 * R112: **消費できなくなった** ENTRY claim を、不在の証明が届いたその場で捨てる。
 *
 * CONSUME は claim.maxAgeSec を過ぎると必ず ENTRY_CLAIM_EXPIRED で拒否される。
 * つまり CLAIMED かつ試行 0 のまま maxAgeSec を過ぎた claim は、注文を生む経路が
 * 構造的に存在しない枠の占有でしかない。ところが解放は CLAIM 到達時の stale release と
 * RECOVER しか無く、RECOVER は一度も送っていない claim では原理的に通らない
 * (2026-09-18 22:40 の claim が 2 時間 45 分残り、台帳に ENTRY_RECOVERED が 168 行積んだ)。
 *
 * ここで固定するのは「捨ててよい形」と「捨ててはいけない形」の境界。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { applyEvent, brokerObservationHash, emptyState } from "../src/state_machine.js";
import executionContract from "../../execution_contract.json" with { type: "json" };

const NOW = Date.parse("2026-09-19T00:00:00Z");
const MAX_AGE_SEC = Number(executionContract.claim.maxAgeSec);
const OBS_MAX_AGE_SEC = Number(executionContract.brokerObservation.maxAgeSec);
const ACCOUNT = "LFE00000000000026";
const OTHER = "MFFUEVREOD000000002";
const SYMBOL = "MNQZ6";
const INTENT_HASH = `xi_${"b".repeat(64)}`;

function iso(ms) {
  return new Date(ms).toISOString();
}

function stateWithClaim({
  claimState = "CLAIMED", totalAttempts = 0, ageSec = MAX_AGE_SEC + 60,
  scope = [ACCOUNT, OTHER],
} = {}) {
  const state = emptyState("acct", SYMBOL);
  state.entryClaim = {
    entryKey: "ENTRY:cafebabe",
    tuple: { scenarioId: "s", fingerprint: "f", evidenceHash: "e", marketCycleId: "c" },
    state: claimState,
    routeState: totalAttempts ? "SENT" : null,
    acceptedCount: 0,
    totalAttempts,
    claimedAt: iso(NOW - ageSec * 1000),
    executionIntent: { symbol: SYMBOL, side: "SELL", qty: 2, accountScope: [...scope] },
    executionIntentHash: INTENT_HASH,
  };
  return state;
}

function observationEvent({
  qty = 0, orders = [], account = ACCOUNT, symbol = SYMBOL,
  intentHash = INTENT_HASH, observedSecondsAgo = 1,
} = {}) {
  const at = iso(NOW - observedSecondsAgo * 1000);
  // snapshotHash は観測そのものから計算する(手で置くと「invalid」で受理されない)。
  // STABLE_DOUBLE_READ は「建玉を二度読んで同じだった」証明を要求する。
  const observation = {
    observedAt: at, positionObservedAt: at, ordersObservedAt: at,
    snapshotId: "bs_" + "1".repeat(32),
    cursor: null, snapshotMode: "STABLE_DOUBLE_READ",
    stableBeforeHash: "3".repeat(64), stableAfterHash: "3".repeat(64),
    platform: "STUB", accountId: account, symbol,
    currentIntentHash: intentHash,
    position: { verified: true, qty, side: qty ? "SHORT" : "FLAT",
      positionGeneration: null,
      rawPositionIdentity: qty ? "POS:" + "1".repeat(64) : null },
    orders,
  };
  return {
    stream: "broker_observation",
    revision: 7,
    payload: {
      observation: { ...observation, snapshotHash: brokerObservationHash(observation) },
    },
  };
}

function publish(state, event) {
  return applyEvent(state, event, NOW);
}

test("R112: 消費できない claim は不在の証明が届いた時点で捨てられる", () => {
  const out = publish(stateWithClaim(), observationEvent());
  assert.equal(out.accepted, true, out.reason || "");
  assert.equal(out.state.entryClaim, null, "claim が残っている");
  assert.equal(out.state.entryStaleRelease.entryKey, "ENTRY:cafebabe");
  assert.equal(out.state.entryStaleRelease.from, "CLAIMED");
  assert.equal(out.state.entryStaleRelease.reason, "UNCONSUMABLE_CLAIM_BROKER_EMPTY");
  assert.ok(String(out.reason).includes("ENTRY_CLAIM_STALE_RELEASED"), String(out.reason));
  assert.ok(out.transitions.some((t) => t.kind === "entry_claim" && t.to === "STALE_RELEASED"));
  // 観測そのものは通常どおり載る。
  assert.equal(out.state.brokerObservation.accountId, ACCOUNT);
});

test("R112: まだ CONSUME できる claim(maxAgeSec 以内)は捨てない", () => {
  const out = publish(stateWithClaim({ ageSec: MAX_AGE_SEC - 30 }), observationEvent());
  assert.ok(out.state.entryClaim, "まだ生きている claim が消えた");
  assert.equal(out.state.entryStaleRelease, undefined);
});

test("R112: 一度でも送った claim(totalAttempts > 0)は捨てない", () => {
  const out = publish(stateWithClaim({ totalAttempts: 1 }), observationEvent());
  assert.ok(out.state.entryClaim, "送信済みの claim が消えた");
});

test("R112: CONSUMED は捨てない(RECOVER と CLAIM 時の解放が扱う)", () => {
  const out = publish(
    stateWithClaim({ claimState: "CONSUMED", totalAttempts: 2 }), observationEvent());
  assert.ok(out.state.entryClaim, "CONSUMED が消えた");
  assert.equal(out.state.entryClaim.state, "CONSUMED");
});

test("R112: 建玉が残っていれば捨てない", () => {
  const out = publish(stateWithClaim(), observationEvent({ qty: 2 }));
  assert.ok(out.state.entryClaim, "建玉があるのに claim が消えた");
});

test("R112: 未終端の注文が 1 本でもあれば捨てない", () => {
  const out = publish(stateWithClaim(), observationEvent({
    orders: [{ orderId: "1", receipt: "r", accountId: ACCOUNT, symbol: SYMBOL, status: "WORKING" }],
  }));
  assert.ok(out.state.entryClaim, "生きた注文があるのに claim が消えた");
});

test("R112: 終端した注文だけなら捨てる", () => {
  const out = publish(stateWithClaim(), observationEvent({
    orders: [{ orderId: "1", receipt: "r", accountId: ACCOUNT, symbol: SYMBOL, status: "CANCELED" }],
  }));
  assert.equal(out.state.entryClaim, null, "終端した注文しか無いのに残った");
});

test("R112: 観測の口座が claim の scope 外なら捨てない", () => {
  // scope 外の口座の観測はそもそも受理されない(権威性の検査)。claim は残る。
  const out = publish(stateWithClaim({ scope: [ACCOUNT] }), observationEvent({ account: "ACC-XX" }));
  assert.ok(out.state.entryClaim, "scope 外の観測で claim が消えた");
});

test("R112: 観測の銘柄が claim と違えば捨てない", () => {
  const out = publish(stateWithClaim(), observationEvent({ symbol: "MNQH7" }));
  assert.ok(out.state.entryClaim, "別銘柄の観測で claim が消えた");
});

test("R112: 観測が古ければ(brokerObservation.maxAgeSec 超)捨てない", () => {
  const out = publish(stateWithClaim(),
    observationEvent({ observedSecondsAgo: OBS_MAX_AGE_SEC + 5 }));
  assert.ok(out.state.entryClaim, "古い観測で claim が消えた");
});

test("R112: claim が無い周期は普通に観測だけ載る", () => {
  const state = emptyState("acct", SYMBOL);
  state.entryClaim = {
    entryKey: "ENTRY:x", tuple: {}, state: "CLAIMED", totalAttempts: 0,
    claimedAt: iso(NOW), executionIntent: { symbol: SYMBOL, accountScope: [ACCOUNT] },
    executionIntentHash: INTENT_HASH,
  };
  const out = publish(state, observationEvent());
  assert.equal(out.accepted, true);
  assert.equal(out.reason, null, "解放していないのに reason が付いた");
  assert.ok(out.state.brokerObservation);
});
