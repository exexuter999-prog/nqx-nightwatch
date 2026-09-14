/**
 * R56: ブローカー約定から組んだ記録(exitSource=broker)は、凍結プランの無い
 * 手動建玉だと stop を持たない。R を出さないだけで損益は事実なので受け付ける。
 * 手入力(manual)は従来どおり stop 必須。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { validateResult } from "../src/state_machine.js";

const T0 = Date.parse("2026-09-04T19:34:04Z");

function raw(overrides = {}) {
  return {
    resultId: "rs_0123456789abcdef0123",
    side: "SHORT", symbol: "MNQU6", qty: 28,
    entry: 29529.5, exit: 29538.75, stop: 29545.0,
    pointValue: 2,
    openedAt: new Date(T0 - 7 * 60_000).toISOString(),
    closedAt: new Date(T0).toISOString(),
    path: [29529.5, 29538.75],
    pathSource: "endpoints-only",
    exitSource: "broker",
    mode: "LIVE",
    ...overrides,
  };
}

test("broker 由来の記録は stop 無しでも受け付け、stop は null で保存される", () => {
  for (const stop of [null, undefined, ""]) {
    const checked = validateResult(raw({ stop }), { symbol: "MNQU6" });
    assert.equal(checked.ok, true, `stop=${String(stop)}: ${checked.reason}`);
    assert.equal(checked.result.stop, null);
    assert.equal(checked.result.entry, 29529.5);
  }
});

test("manual(手入力)は stop が無ければ拒む", () => {
  const checked = validateResult(raw({ stop: null, exitSource: "manual" }), { symbol: "MNQU6" });
  assert.equal(checked.ok, false);
  assert.match(checked.reason, /stop is required/);
});

test("stop があるときの検査は従来どおり(entry と同値は拒む・tick へ丸める)", () => {
  const same = validateResult(raw({ stop: 29529.5 }), { symbol: "MNQU6" });
  assert.equal(same.ok, false);
  assert.match(same.reason, /equals entry/);
  const rounded = validateResult(raw({ stop: 29545.1 }), { symbol: "MNQU6" });
  assert.equal(rounded.ok, true);
  assert.equal(rounded.result.stop, 29545.0);
  const bad = validateResult(raw({ stop: "abc" }), { symbol: "MNQU6" });
  assert.equal(bad.ok, false);
  assert.match(bad.reason, /stop is invalid/);
});

test("stop 無しでも分割脚(legs)はそのまま検査される", () => {
  const checked = validateResult(raw({
    stop: null,
    legs: [
      { id: "STOP", qty: 24, exit: 29538.75, kind: "manual" },
      { id: "STOP", qty: 4, exit: 29539.0, kind: "manual" },
    ],
    exit: 29538.75,
  }), { symbol: "MNQU6" });
  assert.equal(checked.ok, true, checked.reason);
  assert.equal(checked.result.legs.length, 2);
});
