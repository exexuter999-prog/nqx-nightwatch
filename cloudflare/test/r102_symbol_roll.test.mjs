import { test } from "node:test";
import assert from "node:assert/strict";
import { adoptSymbol, emptyState } from "../src/state_machine.js";

/**
 * R102d: DO の state.symbol は作成時に固定される。Worker の NQX_SYMBOL を切り替えて deploy しても
 * 文書側は旧限月のままで、新限月の scenario / position / result が「symbol does not match」で
 * 全部拒否された(2026-09-15 16:44、ロール後の初回 ARMED で HALT)。安全なときだけ載せ替える。
 */

const T0 = Date.parse("2026-09-15T07:44:00Z");

function oldContractState(overrides = {}) {
  const state = emptyState("acct", "MNQU6");
  return { ...state,
    position: { verified: true, symbol: "MNQU6", qty: 0, observedAt: new Date(T0).toISOString() },
    scenario: { scenarioId: "sc-old", symbol: "MNQU6", state: "WATCH" },
    market: { at: new Date(T0).toISOString(), sourceSymbol: "CME_MINI:MNQU6", price: 29300 },
    brokerObservation: { symbol: "MNQU6", observedAt: new Date(T0).toISOString() },
    entryClaim: { entryKey: "ENTRY:" + "a".repeat(64), state: "RECOVERED", routeState: "SENT" },
    ...overrides };
}

test("R102d FLAT・claim 終端なら新限月へ載せ替え、旧限月の scenario/market/観測は捨てる", () => {
  const out = adoptSymbol(oldContractState(), "MNQZ6", T0);
  assert.equal(out.adopted, true, out.reason);
  assert.equal(out.state.symbol, "MNQZ6");
  assert.equal(out.state.scenario, null);
  assert.equal(out.state.market, null);
  assert.equal(out.state.brokerObservation, null);
  assert.equal(out.state.position, null);
  assert.equal(out.state.positionCheck, null);
  assert.deepEqual(out.state.symbolRoll, { from: "MNQU6", to: "MNQZ6", at: new Date(T0).toISOString() });
  // 終端した claim と tombstone は残す(同じ tuple を二度送らせない)。
  assert.equal(out.state.entryClaim.state, "RECOVERED");
});

test("R102d 同じ限月なら何もしない", () => {
  const state = oldContractState();
  const out = adoptSymbol(state, "MNQU6", T0);
  assert.equal(out.adopted, false);
  assert.equal(out.state, state);
});

test("R102d 建玉がある・claim が CLAIMED/CONSUMED・blocking 注文があるときは載せ替えない", () => {
  const withPosition = adoptSymbol(oldContractState({
    position: { verified: true, symbol: "MNQU6", qty: 2, side: "LONG" } }), "MNQZ6", T0);
  assert.equal(withPosition.adopted, false);
  assert.match(withPosition.reason, /position qty 2/);
  assert.equal(withPosition.state.symbol, "MNQU6");

  for (const claimState of ["CLAIMED", "CONSUMED"]) {
    const out = adoptSymbol(oldContractState({
      entryClaim: { entryKey: "ENTRY:" + "b".repeat(64), state: claimState } }), "MNQZ6", T0);
    assert.equal(out.adopted, false, claimState);
    assert.match(out.reason, new RegExp(`entry claim ${claimState}`));
  }
  const mgmt = adoptSymbol(oldContractState({
    managementClaim: { managementKey: "M", state: "CLAIMED" } }), "MNQZ6", T0);
  assert.equal(mgmt.adopted, false);
  assert.match(mgmt.reason, /management claim CLAIMED/);

  const blocking = adoptSymbol(oldContractState({
    order: { state: "PENDING", idempotencyKey: "x" } }), "MNQZ6", T0);
  assert.equal(blocking.adopted, false);
  assert.match(blocking.reason, /order PENDING/);
});
