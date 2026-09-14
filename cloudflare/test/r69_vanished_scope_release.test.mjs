// R69: claim の accountScope の口座がブローカーから消えたら、名簿を不在の証明として解放する。
//
// 2026-09-08 17:54: Lucid Flex 50K の評価通過で LFE00000000000024 が CrossTrade から消え、
// funded 口座 LFF00000000000006 に入れ替わった。旧口座 scope の ENTRY claim(15:09、CONSUMED/SENT)は
// 「scope 内口座の観測で建玉 0」を永久に証明できず、新口座の ARMED 候補が
// ENTRY_CLAIM_ALREADY_HELD で全部止まった(2026-08-31 の 3 日ロックと同型)。
//
// ここで固定する線引き:
//   * 名簿(accounts.sync)が verified・新鮮で、scope の全口座がブローカーに居ない → 解放
//   * scope の口座が 1 つでも名簿に居る(configured で missing でない / broker-only) → 従来どおり観測を要求
//   * 名簿が古い / 未検証 / 無い → 解放しない
//   * claim が staleReleaseSec 未満 → 名簿がどうであれ解放しない
import { test } from "node:test";
import assert from "node:assert/strict";
import { applyEvent, emptyState, strategyEvidenceHash, entryKeyForTuple,
  claimScopeVanishedFromBroker } from "../src/state_machine.js";

const T0 = Date.parse("2026-09-08T06:09:00Z");
const STALE_MS = 900_000;            // execution_contract.claim.staleReleaseSec
const iso = (ms = T0) => new Date(ms).toISOString();
const OLD_ACCOUNT = "LFE00000000000024";
const NEW_ACCOUNT = "LFF00000000000006";

function frozenEvidence() {
  const raw = { version: "R14-STRATEGY-EVIDENCE-1", asOf: iso(), sessionId: "NY-20260908",
    source: "fixture", provenance: "test", models: { ifvg: { valid: false } } };
  return { ...raw, evidenceHash: strategyEvidenceHash(raw) };
}

function marketPayload(nowMs, cycleId) {
  const evidence = frozenEvidence();
  return { evidence, market: { at: iso(nowMs), observedAt: iso(nowMs), verified: true, source: "fixture",
    sourceSymbol: "CME_MINI:MNQU6", resolution: "3", barResolution: "3", price: 29600,
    cvdAt: iso(nowMs), cycleId, dayguard: { at: iso(nowMs), available: true, blocked: false, codes: [] },
    bars: [{ t: 1700000000, o: 29599, h: 29600, l: 29598, c: 29599 },
      { t: 1700000180, o: 29599, h: 29601, l: 29599, c: 29600 }],
    levels: [], strategyEvidence: evidence } };
}

function sealedState(cycleId = "cy-r69", revision = 1, scope = [OLD_ACCOUNT]) {
  const { evidence, market } = marketPayload(T0, cycleId);
  const scenario = { scenarioId: `sc-${cycleId}`, fingerprint: `fp-${cycleId}`, state: "ARMED",
    symbol: "MNQU6", side: "SELL", qty: 2, entry: 29622.5, stop: 29644.75, target: 29588.5,
    targets: [29588.5, 29521.75], planVersion: "R17-SPLIT-1", grade: "B",
    legs: [{ id: "TP1", qty: 1, target: 29588.5 }, { id: "RUNNER", qty: 1, target: 29521.75 }],
    issuedAt: iso(), observedAt: iso(), expiresAt: iso(T0 + 3_600_000), marketCycleId: cycleId,
    setupVersion: "R14-SETUP", catalogVersion: "R14-CATALOG", detectorVersion: "R14-DETECTOR",
    executionContractVersion: "R22-EXECUTION-CONTRACT-1", evidenceHash: evidence.evidenceHash,
    executionContract: { version: "R22-EXECUTION-CONTRACT-1", riskCapDollars: 200,
      riskCapSource: "fixture", accountScope: scope } };
  let r = applyEvent(emptyState("acct", "MNQU6"),
    { stream: "cycle", revision, payload: { cycleId, market, scenario } }, T0);
  assert.equal(r.accepted, true, r.reason || "cycle rejected");
  r = applyEvent(r.state, { stream: "position", revision: 1,
    payload: { position: { verified: true, source: "broker", symbol: "MNQU6", qty: 0,
      observedAt: iso() } } }, T0);
  assert.equal(r.accepted, true);
  return { state: r.state, scenario: r.state.scenario };
}

/** 同じ cycle を新しい時刻で打ち直して lease を保つ(claim は据え置き)。 */
function heartbeat(state, nowMs, revision) {
  const cycleId = state.market?.cycleId || "cy-r69";
  const { evidence, market } = marketPayload(nowMs, cycleId);
  const scenario = { ...state.scenario, issuedAt: iso(nowMs), observedAt: iso(nowMs),
    expiresAt: iso(nowMs + 600_000), evidenceHash: evidence.evidenceHash };
  const r = applyEvent(state, { stream: "cycle", revision, payload: { cycleId, market, scenario } }, nowMs);
  assert.equal(r.accepted, true, r.reason || "heartbeat rejected");
  return r.state;
}

const tuple = (s) => Object.fromEntries(
  ["scenarioId", "fingerprint", "evidenceHash", "marketCycleId"].map((k) => [k, s[k]]));

/** CLAIM → CONSUME まで進めて RESOLVE せずに放置(送信後に落ちた状況)。 */
function strandedClaim() {
  const sealed = sealedState();
  const entryKey = entryKeyForTuple(tuple(sealed.scenario));
  const payload = { action: "CLAIM", entryKey, tuple: tuple(sealed.scenario),
    claimTokenHash: "a".repeat(64), orderType: "LIMIT" };
  const claimed = applyEvent(sealed.state, { stream: "entry_claim", revision: 1, payload }, T0);
  assert.equal(claimed.accepted, true, claimed.reason || "claim rejected");
  const consumed = applyEvent(claimed.state, { stream: "entry_claim", revision: 2,
    payload: { action: "CONSUME", entryKey, claimTokenHash: payload.claimTokenHash,
      executionIntent: claimed.state.entryClaim.executionIntent,
      executionIntentHash: claimed.state.entryClaim.executionIntentHash } }, T0);
  assert.equal(consumed.accepted, true, consumed.reason || "consume rejected");
  return { state: consumed.state, entryKey, payload };
}

/** 口座名簿を publish する(monitor 側の accounts.sync と同じ形)。 */
function roster(state, nowMs, revision, { list = [NEW_ACCOUNT], unknown = [], missing = [],
  verified = true, observedAt = iso(nowMs) } = {}) {
  const r = applyEvent(state, { stream: "account", revision, payload: { accounts: {
    observedAt, source: "crosstrade-env",
    list: list.map((id) => ({ id, cap: 200, buffer: 2000 })),
    sync: { verified, observedAt, missing, unknown, dead: [] },
  } } }, nowMs);
  assert.equal(r.accepted, true, r.reason || "roster rejected");
  return r.state;
}

function retryClaim(state, payload, nowMs, revision) {
  return applyEvent(state, { stream: "entry_claim", revision,
    payload: { ...payload, claimTokenHash: "b".repeat(64) } }, nowMs);
}

test("scope の口座が名簿から消えていれば、古い claim は観測なしで解放される", () => {
  const { state, payload, entryKey } = strandedClaim();
  const later = T0 + STALE_MS + 60_000;
  const withRoster = roster(heartbeat(state, later, 3), later, 4);
  assert.equal(claimScopeVanishedFromBroker(withRoster, withRoster.entryClaim, later), true);
  const retry = retryClaim(withRoster, payload, later, 5);
  assert.equal(retry.accepted, true, retry.reason || "解放されるべき");
  assert.match(retry.reason || "", /STALE_RELEASED/);
  assert.equal(retry.state.entryClaim.state, "CLAIMED", "新しい claim が立つ");
  assert.equal(retry.state.entryStaleRelease?.entryKey, entryKey, "解放が記録される");
});

test("scope の口座が名簿に居る(broker-only)なら従来どおり観測を要求する", () => {
  const { state, payload } = strandedClaim();
  const later = T0 + STALE_MS + 60_000;
  const withRoster = roster(heartbeat(state, later, 3), later, 4, { unknown: [OLD_ACCOUNT] });
  assert.equal(claimScopeVanishedFromBroker(withRoster, withRoster.entryClaim, later), false);
  const retry = retryClaim(withRoster, payload, later, 5);
  assert.equal(retry.accepted, false);
  assert.match(retry.reason, /ALREADY_HELD/);
});

test("scope の口座が configured で missing でなければ名簿は「居る」= 解放しない", () => {
  const { state, payload } = strandedClaim();
  const later = T0 + STALE_MS + 60_000;
  const withRoster = roster(heartbeat(state, later, 3), later, 4, { list: [OLD_ACCOUNT, NEW_ACCOUNT] });
  assert.equal(claimScopeVanishedFromBroker(withRoster, withRoster.entryClaim, later), false);
  assert.match(retryClaim(withRoster, payload, later, 5).reason, /ALREADY_HELD/);
});

test("configured でも missing(ブローカーに無い)なら消えた扱いで解放する", () => {
  const { state, payload } = strandedClaim();
  const later = T0 + STALE_MS + 60_000;
  const withRoster = roster(heartbeat(state, later, 3), later, 4,
    { list: [OLD_ACCOUNT], missing: [OLD_ACCOUNT], unknown: [NEW_ACCOUNT] });
  assert.equal(claimScopeVanishedFromBroker(withRoster, withRoster.entryClaim, later), true);
  assert.equal(retryClaim(withRoster, payload, later, 5).accepted, true);
});

test("名簿が古い / 未検証 / 無い なら解放しない", () => {
  const { state, payload } = strandedClaim();
  const later = T0 + STALE_MS + 60_000;
  const beat = heartbeat(state, later, 3);
  assert.equal(claimScopeVanishedFromBroker(beat, beat.entryClaim, later), false, "名簿なし");
  assert.match(retryClaim(beat, payload, later, 4).reason, /ALREADY_HELD/);
  const stale = roster(beat, later, 4, { observedAt: iso(later - 901_000) });
  assert.equal(claimScopeVanishedFromBroker(stale, stale.entryClaim, later), false, "名簿が古い");
  assert.match(retryClaim(stale, payload, later, 5).reason, /ALREADY_HELD/);
  const unverified = roster(beat, later, 4, { verified: false });
  assert.equal(claimScopeVanishedFromBroker(unverified, unverified.entryClaim, later), false, "未検証");
  assert.match(retryClaim(unverified, payload, later, 5).reason, /ALREADY_HELD/);
});

test("claim が staleReleaseSec 未満なら、名簿が消えていても解放しない", () => {
  const { state, payload } = strandedClaim();
  const soon = T0 + 60_000;
  const withRoster = roster(heartbeat(state, soon, 3), soon, 4);
  assert.equal(claimScopeVanishedFromBroker(withRoster, withRoster.entryClaim, soon), true);
  const retry = retryClaim(withRoster, payload, soon, 5);
  assert.equal(retry.accepted, false, "通信の一時断と区別する");
  assert.match(retry.reason, /ALREADY_HELD/);
});
