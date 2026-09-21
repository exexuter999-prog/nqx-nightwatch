/**
 * R123: engine が全口座 FLAT を確かめた古い ENTRY claim を、次の CLAIM を待たずに捨てる。
 *
 * 多口座の CONSUMED claim は RECOVER を構造的に通せず、解放は次の ARMED の CLAIM 到達時
 * だけだった。トレードが終わるたびに枠が残り、Mini App は STALE SLOT を出し続けた。
 * RELEASE は CLAIM 時と同じ staleEntryClaimReleasable を DO が再検証する。条件は緩めない。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { applyEvent, brokerObservationHash, emptyState } from "../src/state_machine.js";
import executionContract from "../../execution_contract.json" with { type: "json" };

const NOW = Date.parse("2026-09-21T00:00:00Z");
const STALE_SEC = Number(executionContract.claim.staleReleaseSec);
const ACCOUNT = "LFE00000000000026";
const OTHER = "MFFUEVREOD000000002";
const SYMBOL = "MNQZ6";
const INTENT_HASH = `xi_${"c".repeat(64)}`;
const TOKEN_HASH = createHash("sha256").update("token").digest("hex");

const iso = (ms) => new Date(ms).toISOString();

function stateWithClaim({ claimState = "CONSUMED", ageSec = STALE_SEC + 60 } = {}) {
  const state = emptyState("acct", SYMBOL);
  state.entryClaim = {
    entryKey: "ENTRY:feedface",
    tuple: { scenarioId: "s", fingerprint: "f", evidenceHash: "e", marketCycleId: "c" },
    claimTokenHash: TOKEN_HASH,
    state: claimState, routeState: "SENT", acceptedCount: 4, totalAttempts: 4,
    claimedAt: iso(NOW - ageSec * 1000),
    executionIntent: { symbol: SYMBOL, side: "SELL", qty: 2, accountScope: [ACCOUNT, OTHER] },
    executionIntentHash: INTENT_HASH,
  };
  return state;
}

function observe(state, { qty = 0, orders = [] } = {}) {
  const at = iso(NOW - 1000);
  const observation = {
    observedAt: at, positionObservedAt: at, ordersObservedAt: at,
    snapshotId: "bs_" + "2".repeat(32), cursor: null, snapshotMode: "STABLE_DOUBLE_READ",
    stableBeforeHash: "4".repeat(64), stableAfterHash: "4".repeat(64),
    platform: "STUB", accountId: ACCOUNT, symbol: SYMBOL, currentIntentHash: INTENT_HASH,
    position: { verified: true, qty, side: qty ? "SHORT" : "FLAT", positionGeneration: qty ? "PG:1" : null,
      rawPositionIdentity: qty ? "POS:" + "1".repeat(64) : null },
    orders,
  };
  const out = applyEvent(state, { stream: "broker_observation", revision: 7,
    payload: { observation: { ...observation, snapshotHash: brokerObservationHash(observation) } } }, NOW);
  assert.equal(out.accepted, true, out.reason || "");
  return out.state;
}

function release(state, { tokenHash = TOKEN_HASH, entryKey = "ENTRY:feedface" } = {}) {
  return applyEvent(state, { stream: "entry_claim", revision: 8,
    payload: { action: "RELEASE", entryKey, claimTokenHash: tokenHash } }, NOW);
}

test("R123: 古い CONSUMED claim は FLAT の証明があれば RELEASE で捨てられる", () => {
  const out = release(observe(stateWithClaim()));
  assert.equal(out.accepted, true, out.reason || "");
  assert.equal(out.state.entryClaim, null);
  assert.equal(out.state.entryStaleRelease.from, "CONSUMED");
  assert.equal(out.state.entryStaleRelease.reason, "ENGINE_SCOPE_ALL_FLAT");
  assert.ok(String(out.reason).includes("ENTRY_CLAIM_STALE_RELEASED"));
  assert.ok(out.transitions.some((t) => t.to === "STALE_RELEASED"));
});

test("R123: token が違えば捨てない", () => {
  const out = release(observe(stateWithClaim()), { tokenHash: "0".repeat(64) });
  assert.equal(out.accepted, false);
  assert.equal(out.reason, "ENTRY_CLAIM_TOKEN_INVALID");
});

test("R123: staleReleaseSec 以内の claim は捨てない", () => {
  const out = release(observe(stateWithClaim({ ageSec: STALE_SEC - 60 })));
  assert.equal(out.accepted, false);
  assert.equal(out.reason, "ENTRY_CLAIM_RELEASE_UNVERIFIED");
});

test("R123: 建玉が残っていれば捨てない", () => {
  const out = release(observe(stateWithClaim(), { qty: 2 }));
  assert.equal(out.reason, "ENTRY_CLAIM_RELEASE_UNVERIFIED");
});

test("R123: 未終端の注文が残っていれば捨てない", () => {
  const out = release(observe(stateWithClaim(), {
    orders: [{ orderId: "9", receipt: "r", accountId: ACCOUNT, symbol: SYMBOL, status: "WORKING" }],
  }));
  assert.equal(out.reason, "ENTRY_CLAIM_RELEASE_UNVERIFIED");
});

test("R123: 観測が無ければ捨てない", () => {
  const out = release(stateWithClaim());
  assert.equal(out.reason, "ENTRY_CLAIM_RELEASE_UNVERIFIED");
});

test("R123: RECOVERED(終端済み)は対象外", () => {
  const out = release(observe(stateWithClaim({ claimState: "RECOVERED" })));
  assert.equal(out.reason, "ENTRY_CLAIM_NOT_ACTIVE");
});
