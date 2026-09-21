/**
 * R110: 名簿(accounts.sync)が「claim の scope はブローカーに居ない」と証明した周期で、
 * その場で古い ENTRY claim を捨てる。
 *
 * R69 の stale release は CLAIM 到達時にしか走らないので、口座入替後の claim は
 * 「次の新規 ENTRY」まで残り、Mini App が STALE SLOT を出し続けていた。
 * ここで固定するのは「捨ててよい形」と「捨ててはいけない形」の境界。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { applyEvent, emptyState } from "../src/state_machine.js";
import executionContract from "../../execution_contract.json" with { type: "json" };

const NOW = Date.parse("2026-09-18T11:30:00Z");
const STALE_SEC = Number(executionContract.claim.staleReleaseSec);
const GONE = "LFF00000000000006";
const LIVE = "LFE00000000000026";
const BROKER_ONLY = "LTATANOBA1000000000001";

function claimedAt(secondsAgo) {
  return new Date(NOW - secondsAgo * 1000).toISOString();
}

function stateWithClaim({ scope = [GONE], claimState = "CONSUMED", ageSec = STALE_SEC + 60 } = {}) {
  const state = emptyState("acct", "MNQZ6");
  state.entryClaim = {
    entryKey: "ENTRY:deadbeef",
    tuple: { scenarioId: "s", fingerprint: "f", evidenceHash: "e", marketCycleId: "c" },
    state: claimState,
    routeState: "SENT",
    acceptedCount: 2,
    totalAttempts: 2,
    claimedAt: claimedAt(ageSec),
    executionIntent: { symbol: "MNQZ6", side: "SELL", qty: 6, accountScope: [...scope] },
    executionIntentHash: "ih_test",
  };
  return state;
}

function rosterEvent({ configured = [LIVE], missing = [], unknown = [BROKER_ONLY],
                       verified = true, observedSecondsAgo = 5 } = {}) {
  const observedAt = new Date(NOW - observedSecondsAgo * 1000).toISOString();
  return {
    stream: "account",
    revision: 1,
    payload: {
      accounts: {
        list: configured.map((id) => ({ id, cap: 200, buffer: 2000, equity: 50000 })),
        totalBuffer: 2000 * configured.length,
        observedAt,
        source: "crosstrade-rest",
        sync: { verified, observedAt, missing: [...missing], unknown: [...unknown], dead: [] },
      },
    },
  };
}

function publishRoster(state, event) {
  return applyEvent(state, event, NOW);
}

test("R110: 消えた口座の claim は名簿 publish で捨てられる", () => {
  const out = publishRoster(stateWithClaim(), rosterEvent());
  assert.equal(out.accepted, true, out.reason || "");
  assert.equal(out.state.entryClaim, null);
  assert.equal(out.state.entryStaleRelease.entryKey, "ENTRY:deadbeef");
  assert.equal(out.state.entryStaleRelease.from, "CONSUMED");
  assert.equal(out.state.entryStaleRelease.reason, "SCOPE_VANISHED_FROM_BROKER");
  assert.ok(String(out.reason).includes("ENTRY_CLAIM_STALE_RELEASED"), String(out.reason));
  assert.ok(out.transitions.some((t) => t.kind === "entry_claim" && t.to === "STALE_RELEASED"));
  // 名簿そのものは通常どおり載る。
  assert.equal(out.state.accounts.list.length, 1);
});

test("R110: CLAIMED(未消費)の claim も同じ条件で捨てられる", () => {
  const out = publishRoster(stateWithClaim({ claimState: "CLAIMED" }), rosterEvent());
  assert.equal(out.state.entryClaim, null);
  assert.equal(out.state.entryStaleRelease.from, "CLAIMED");
});

test("R110: 生きている口座の claim は捨てない", () => {
  const out = publishRoster(stateWithClaim({ scope: [LIVE] }), rosterEvent());
  assert.ok(out.state.entryClaim, "生存口座の claim が消えた");
  assert.equal(out.state.entryClaim.entryKey, "ENTRY:deadbeef");
  assert.equal(out.state.entryStaleRelease, undefined);
});

test("R110: 1 口座でも生きていれば捨てない(混在 scope)", () => {
  const out = publishRoster(stateWithClaim({ scope: [GONE, LIVE] }), rosterEvent());
  assert.ok(out.state.entryClaim, "混在 scope の claim が消えた");
});

test("R110: 設定外でもブローカーに居る口座(unknown)は『消えた』ではない", () => {
  const out = publishRoster(stateWithClaim({ scope: [BROKER_ONLY] }), rosterEvent());
  assert.ok(out.state.entryClaim, "broker-only の claim が消えた");
});

test("R110: 新しい claim(staleReleaseSec 未満)は捨てない", () => {
  const out = publishRoster(
    stateWithClaim({ ageSec: STALE_SEC - 60 }), rosterEvent());
  assert.ok(out.state.entryClaim, "まだ新しい claim が消えた");
});

test("R110: 名簿が未検証なら捨てない", () => {
  const out = publishRoster(stateWithClaim(), rosterEvent({ verified: false }));
  assert.ok(out.state.entryClaim, "未検証の名簿で claim が消えた");
});

test("R110: 名簿が古ければ(900 秒超)捨てない", () => {
  const out = publishRoster(stateWithClaim(), rosterEvent({ observedSecondsAgo: 901 }));
  assert.ok(out.state.entryClaim, "古い名簿で claim が消えた");
});

test("R110: RECOVERED は終端なので触らない", () => {
  const state = stateWithClaim();
  state.entryClaim.state = "RECOVERED";
  const out = publishRoster(state, rosterEvent());
  assert.ok(out.state.entryClaim, "RECOVERED の claim が消えた");
  assert.equal(out.state.entryClaim.state, "RECOVERED");
});

test("R110: claim が無い周期は普通に名簿だけ載る", () => {
  const out = publishRoster(emptyState("acct", "MNQZ6"), rosterEvent());
  assert.equal(out.accepted, true);
  assert.equal(out.reason, null);
  assert.equal(out.state.entryClaim, null);
  assert.equal(out.state.accounts.list.length, 1);
});

test("R110: configured だが missing の口座も不在として扱う", () => {
  const out = publishRoster(
    stateWithClaim({ scope: [LIVE] }),
    rosterEvent({ configured: [LIVE], missing: [LIVE] }));
  assert.equal(out.state.entryClaim, null, "missing の口座が解放されない");
});
