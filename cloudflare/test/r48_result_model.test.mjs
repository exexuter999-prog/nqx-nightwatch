/**
 * R48: result の model / grade(モデル別スコアカードの表示・集計キー)。
 * 表示専用フィールドであり、判定へ影響しないこと・未知値は落ちることを固定する。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { validateResult } from "../src/state_machine.js";

const T0 = Date.parse("2026-08-28T14:00:00Z");

function raw(overrides = {}) {
  return {
    resultId: "rs_0123456789abcdef0123",
    side: "SHORT", symbol: "MNQU6", qty: 2,
    entry: 30126.0, exit: 30098.5, stop: 30147.0,
    pointValue: 2,
    openedAt: new Date(T0 - 30 * 60_000).toISOString(),
    closedAt: new Date(T0).toISOString(),
    path: [30126.0, 30098.5],
    pathSource: "endpoints-only",
    exitSource: "broker",
    mode: "LIVE",
    ...overrides,
  };
}

test("model と grade が result に載る", () => {
  const checked = validateResult(raw({ model: "TURTLE_SOUP_REVERSAL", grade: "A+" }), { symbol: "MNQU6" });
  assert.equal(checked.ok, true, checked.reason);
  assert.equal(checked.result.model, "TURTLE_SOUP_REVERSAL");
  assert.equal(checked.result.grade, "A+");
});

test("無い場合は null(旧 result 互換)", () => {
  const checked = validateResult(raw(), { symbol: "MNQU6" });
  assert.equal(checked.ok, true);
  assert.equal(checked.result.model, null);
  assert.equal(checked.result.grade, null);
});

test("未知の grade は null に倒す。model は32文字に切る", () => {
  const checked = validateResult(raw({ model: "X".repeat(64), grade: "S+" }), { symbol: "MNQU6" });
  assert.equal(checked.ok, true);
  assert.equal(checked.result.model.length, 32);
  assert.equal(checked.result.grade, null);
});

test("禁止フィールド(pnl 等)の拒否は変わらない", () => {
  const checked = validateResult(raw({ model: "VP80_REVERSION", pnl: 110 }), { symbol: "MNQU6" });
  assert.equal(checked.ok, false);
});
