/**
 * cycle_health(監視ループのビーコン)の検証。
 *
 * このストリームは表示専用の生存報告で、市場データ・シナリオ・建玉・注文・
 * 実行契約に一切影響しないことをここで固定する。ここが緩むと「ビーコンを
 * 装った書き込みで armed 状態を動かす」経路ができてしまう。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { applyEvent, emptyState, projectState } from "../src/state_machine.js";

const T0 = Date.parse("2026-08-28T14:00:00Z");

function baseState() {
  return emptyState("lucid-50k-daily", "MNQU6");
}

function beaconEvent(revision, beacon) {
  return { stream: "cycle_health", revision, payload: { beacon } };
}

test("BLOCKED ビーコンが受理され view に投影される", () => {
  const result = applyEvent(baseState(), beaconEvent(1, {
    status: "BLOCKED",
    at: new Date(T0).toISOString(),
    reason: "acquisition — chart_state.json stale",
  }), T0);
  assert.equal(result.accepted, true);
  const view = projectState(result.state, T0);
  assert.equal(view.cycleHealth.status, "BLOCKED");
  assert.equal(view.cycleHealth.reason, "acquisition — chart_state.json stale");
  assert.equal(view.cycleHealth.kill, false);
  assert.ok(view.cycleHealth.publishedAt);
});

test("HALT と PUBLISHED も受理される。未知の status は拒否", () => {
  for (const status of ["HALT", "PUBLISHED"]) {
    const result = applyEvent(baseState(), beaconEvent(1, {
      status, at: new Date(T0).toISOString(),
    }), T0);
    assert.equal(result.accepted, true, status);
  }
  const bad = applyEvent(baseState(), beaconEvent(1, {
    status: "EXPLODED", at: new Date(T0).toISOString(),
  }), T0);
  assert.equal(bad.accepted, false);
  assert.match(bad.reason, /beacon\.status/);
});

test("beacon が無い・時刻が壊れている payload は拒否", () => {
  const missing = applyEvent(baseState(), { stream: "cycle_health", revision: 1, payload: {} }, T0);
  assert.equal(missing.accepted, false);
  const badAt = applyEvent(baseState(), beaconEvent(1, { status: "BLOCKED", at: "garbage" }), T0);
  assert.equal(badAt.accepted, false);
});

test("ビーコンは他ストリームのデータを運べない", () => {
  for (const foreign of ["position", "scenario", "order", "result", "market", "accounts"]) {
    const result = applyEvent(baseState(), {
      stream: "cycle_health",
      revision: 1,
      payload: {
        beacon: { status: "BLOCKED", at: new Date(T0).toISOString() },
        [foreign]: { anything: true },
      },
    }, T0);
    assert.equal(result.accepted, false, foreign);
    assert.match(result.reason, /must not carry/);
  }
});

test("ビーコンは既存の armed 状態・市場・建玉を一切動かさない", () => {
  const before = baseState();
  before.market = { cycleId: "cy_x", cycleCommitted: true, verified: true };
  before.scenario = { scenarioId: "sc-1", state: "ACTIVE" };
  before.position = { qty: 2, side: "LONG" };
  const result = applyEvent(before, beaconEvent(1, {
    status: "HALT", at: new Date(T0).toISOString(), reason: "publish failed", kill: true,
  }), T0);
  assert.equal(result.accepted, true);
  assert.equal(result.state.market, before.market);
  assert.equal(result.state.scenario, before.scenario);
  assert.equal(result.state.position, before.position);
  assert.equal(result.state.cycleHealth.kill, true);
  assert.deepEqual(result.transitions, []);
});

test("reason は 500 文字に切り詰める", () => {
  const result = applyEvent(baseState(), beaconEvent(1, {
    status: "BLOCKED", at: new Date(T0).toISOString(), reason: "x".repeat(2000),
  }), T0);
  assert.equal(result.accepted, true);
  assert.equal(result.state.cycleHealth.reason.length, 500);
});

test("revision は他ストリームと同じ単調増加規則に従う", () => {
  const first = applyEvent(baseState(), beaconEvent(5, {
    status: "PUBLISHED", at: new Date(T0).toISOString(),
  }), T0);
  assert.equal(first.accepted, true);
  const replay = applyEvent(first.state, beaconEvent(5, {
    status: "HALT", at: new Date(T0).toISOString(),
  }), T0);
  assert.equal(replay.accepted, false);
  assert.match(replay.reason, /not newer/);
});

test("旧 state(cycleHealth 無し)でも view は null で壊れない", () => {
  const state = baseState();
  delete state.cycleHealth;
  delete state.revisions.cycle_health;
  const view = projectState(state, T0);
  assert.equal(view.cycleHealth, null);
});
