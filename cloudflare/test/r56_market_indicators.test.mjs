/**
 * R56: market.indicators(チャート用の指標束)。表示専用の透過フィールドで、
 * 数値は有限のものだけ・文字列は短く・履歴は 24 件まで。無ければ null。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { validateMarket } from "../src/state_machine.js";

const NOW = Date.parse("2026-09-04T20:24:30Z");

function market(overrides = {}) {
  const t0 = Math.floor(NOW / 1000) - 600;
  return {
    verified: true, observedAt: new Date(NOW - 5000).toISOString(),
    source: "tv", sourceSymbol: "CME_MINI:MNQU2026", barResolution: "3", resolution: "3",
    price: 29532.25,
    bars: [
      { t: t0, o: 29530, h: 29535, l: 29528, c: 29533, v: 100 },
      { t: t0 + 180, o: 29533, h: 29538.25, l: 29531.25, c: 29532.25, v: 120 },
    ],
    ...overrides,
  };
}

test("indicators は数値・短い文字列・履歴 24 件までを通す", () => {
  const checked = validateMarket(market({ indicators: {
    vwapHi: 29624.01, vwapLo: 29511.61, vwapAnchorT: 1788472800,
    cvd: { value: -19025, fast: -26574, slow: -34031, bias: "BULLISH", status: "FRESH",
      history: Array.from({ length: 30 }, (_, i) => i) },
    po3: "MANIPULATION_UP", smt: { peer: "MES1!", bias: "BULLISH", agree: true, position: 0.019 },
    ct: { trend: -1, atr: 37.58, fib618: 29599.54, trail: 29680.69, bogus: "x" },
    event: "NONE",
  } }), NOW);
  assert.equal(checked.ok, true, checked.reason);
  const ind = checked.market.indicators;
  assert.equal(ind.vwapHi, 29624.01);
  assert.equal(ind.cvd.bias, "BULLISH");
  assert.equal(ind.cvd.history.length, 24, "履歴は末尾 24 件");
  assert.equal(ind.cvd.history[0], 6);
  assert.equal(ind.smt.agree, true);
  assert.equal(ind.ct.trend, -1);
  assert.equal("bogus" in ind.ct, false, "未知の CT キーは落ちる");
  assert.equal(ind.po3, "MANIPULATION_UP");
  assert.equal(ind.event, "NONE");
});

test("indicators が無い・壊れているときは null で、market 自体は通る", () => {
  assert.equal(validateMarket(market(), NOW).market.indicators, null);
  assert.equal(validateMarket(market({ indicators: "junk" }), NOW).market.indicators, null);
  assert.equal(validateMarket(market({ indicators: { cvd: { value: "NaN" } } }), NOW).market.indicators, null);
});

test("cvd は数値だけを受ける(辞書のままだと null)", () => {
  assert.equal(validateMarket(market({ cvd: -19025 }), NOW).market.cvd, -19025);
  assert.equal(validateMarket(market({ cvd: { value: -19025 } }), NOW).market.cvd, null);
});
