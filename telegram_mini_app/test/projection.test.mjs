/**
 * R66: 期待損益(表示専用)。DOM もネットワークも使わない。
 * ここは「判定しない」ことと「0 で埋めない」ことの検証でもある。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  direction, normalizeLegs, positionProjectionHtml, projectPosition, projectScenario,
  scenarioProjectionHtml, signedDollars,
} from "../projection.js";

const SELL = {
  side: "SELL", qty: 2, entry: 30126.0, stop: 30147.0, target: 30076.0,
  targets: [30105.0, 30076.0],
  legs: [{ id: "TP1", qty: 1, target: 30105.0 }, { id: "RUNNER", qty: 1, target: 30076.0 }],
};

test("符号付きドルは丸めて桁区切り、欠損は —", () => {
  assert.equal(signedDollars(1234.4), "+$1,234");
  assert.equal(signedDollars(-42), "−$42");
  assert.equal(signedDollars(null), "—");
  assert.equal(signedDollars("abc"), "—");
});

test("方向は BUY/LONG=+1, SELL/SHORT=−1, それ以外 null", () => {
  assert.equal(direction("BUY"), 1); assert.equal(direction("long"), 1);
  assert.equal(direction("SELL"), -1); assert.equal(direction("SHORT"), -1);
  assert.equal(direction("FLAT"), null); assert.equal(direction(undefined), null);
});

test("シナリオ: 脚ごと(TP1 21pt×1 / RUNNER 50pt×1)と SL(21pt×2)を $2/pt で出す", () => {
  const p = projectScenario(SELL, 1);
  assert.equal(p.perLeg.length, 2);
  assert.equal(p.perLeg[0].usd, 42);      // (30126−30105)×2×1
  assert.equal(p.perLeg[1].usd, 100);     // (30126−30076)×2×1
  assert.equal(p.profit, 142);
  assert.equal(p.loss, 84);               // 21×2×2
  assert.equal(p.accounts, 1);
  assert.equal(p.profitTotal, 142); assert.equal(p.lossTotal, 84);
});

test("シナリオ: 口座数を掛ける。0/欠損は 1 口座として扱う", () => {
  assert.equal(projectScenario(SELL, 3).profitTotal, 426);
  assert.equal(projectScenario(SELL, 3).lossTotal, 252);
  assert.equal(projectScenario(SELL, 0).accounts, 1);
  assert.equal(projectScenario(SELL, null).accounts, 1);
});

test("シナリオ: 価格や枚数が欠けていれば null(0 で埋めない)", () => {
  assert.equal(projectScenario({ ...SELL, entry: null }), null);
  assert.equal(projectScenario({ ...SELL, qty: 0 }), null);
  assert.equal(projectScenario({ ...SELL, side: "FLAT" }), null);
  assert.equal(projectScenario({ side: "BUY", qty: 2, entry: 100, stop: 90 }), null); // 目標が無い
});

test("脚の正規化: legs 無しなら targets から 1枚ずつ、単一 target は全量", () => {
  assert.deepEqual(normalizeLegs({ qty: 2, targets: [30105, 30076] }),
    [{ id: "TP1", qty: 1, target: 30105 }, { id: "RUNNER", qty: 1, target: 30076 }]);
  assert.deepEqual(normalizeLegs({ qty: 2, target: 30076 }), [{ id: "TP", qty: 2, target: 30076 }]);
  assert.deepEqual(normalizeLegs({ qty: 18, targets: [30105, 30076] }),
    [{ id: "TP1", qty: 9, target: 30105 }, { id: "RUNNER", qty: 9, target: 30076 }]);
});

test("保有中: 含みは unrealizedPnl を優先し、無ければ現値から再計算する", () => {
  const legs = SELL.legs;
  const withPnl = projectPosition({ side: "SHORT", qty: 2, avgEntry: 30126, stop: 30147, unrealizedPnl: 67.5 }, legs, 30100);
  assert.equal(withPnl.unrealized, 67.5);
  const derived = projectPosition({ side: "SHORT", qty: 2, avgEntry: 30126, stop: 30147 }, legs, 30100);
  assert.equal(derived.unrealized, 104);  // 26pt×2×2
  const none = projectPosition({ side: "SHORT", qty: 2, avgEntry: 30126, stop: 30147 }, legs, null);
  assert.equal(none.unrealized, null);
});

test("保有中: 現値 0 / null / 空文字は欠損。(建値 − 0) × 枚数 の含みを作らない(2026-09-09 実測 +$354,621)", () => {
  const legs = SELL.legs;
  const position = { side: "SHORT", qty: 6, avgEntry: 29551.75, stop: 29579.25 };
  for (const bad of [0, "0", null, undefined, "", NaN, false]) {
    const p = projectPosition(position, legs, bad);
    assert.equal(p.unrealized, null, `last=${String(bad)}`);
    assert.equal(p.last, null, `last=${String(bad)}`);
  }
  const html = positionProjectionHtml(position, legs, 0);
  assert.match(html, /N\/A/);
  assert.doesNotMatch(html, /354,621/);
  assert.doesNotMatch(html, /last 0\.00/);
  assert.equal(projectPosition(position, legs, 29560).unrealized, -99);  // (29560−29551.75)×(−1)×2×6
});

test("保有中: 全量なら両脚、TP1 後(qty<initialQty)は残る脚だけ。SL は残り枚数で", () => {
  const full = projectPosition({ side: "SHORT", qty: 2, initialQty: 2, avgEntry: 30126, stop: 30147 }, SELL.legs, 30100);
  assert.equal(full.atTargets.length, 2);
  assert.equal(full.atTargetTotal, 142);
  assert.equal(full.atStop, -84);
  const runner = projectPosition({ side: "SHORT", qty: 1, initialQty: 2, avgEntry: 30126, stop: 30122 }, SELL.legs, 30092.25);
  assert.equal(runner.atTargets.length, 1);
  assert.equal(runner.atTargets[0].id, "RUNNER");
  assert.equal(runner.atTargetTotal, 100);
  assert.equal(runner.atStop, 8);          // 建値以下の SL = 確保額 (30126−30122)×2×1
});

test("保有中: 脚が無ければ position.target を全量で使う。SL 無しは null", () => {
  const p = projectPosition({ side: "LONG", qty: 2, avgEntry: 100, target: 110 }, [], 105);
  assert.equal(p.atTargetTotal, 40);
  assert.equal(p.atStop, null);
  assert.equal(p.unrealized, 20);
});

test("HTML: シナリオは IF TARGETS HIT / IF STOPPED と脚の内訳、口座倍率", () => {
  const html = scenarioProjectionHtml(SELL, 2);
  assert.match(html, /IF TARGETS HIT/);
  assert.match(html, /\+\$284/);          // 142×2
  assert.match(html, /IF STOPPED/);
  assert.match(html, /−\$168/);           // 84×2
  assert.match(html, /\+\$142 each × 2 accts/);
  assert.match(html, /TP1 \+\$42 · RUNNER \+\$100/);
  assert.match(html, /R:R <b>1:1\.69<\/b>/);
  assert.equal(scenarioProjectionHtml({ side: "FLAT" }), "");
});

test("HTML: 保有中は OPEN P&L を中央に、SL が建値以上なら SL LOCKS", () => {
  const html = positionProjectionHtml(
    { side: "SHORT", qty: 1, initialQty: 2, avgEntry: 30126, stop: 30122, unrealizedPnl: 67.5 },
    SELL.legs, 30092.25);
  assert.match(html, /OPEN P&amp;L/);
  assert.match(html, /\+\$68/);
  assert.match(html, /last 30,092\.25/);
  assert.match(html, /SL LOCKS/);
  assert.match(html, /pnl-cell locked/);
  const losing = positionProjectionHtml({ side: "LONG", qty: 2, avgEntry: 100, stop: 90 }, [], 95);
  assert.match(losing, /IF STOPPED/);
  assert.match(losing, /pnl-cell pain/);
  assert.equal(positionProjectionHtml({ side: "LONG", qty: 0 }), "");
});
