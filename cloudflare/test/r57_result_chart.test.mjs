/**
 * R57: result.chart(根拠チャート)。表示専用の任意フィールドで、形が壊れていれば
 * result ごと拒否する。上限は送り側(result_context.py)と同じ値。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { validateResult, validateResultChart, RESULT_CHART_MAX_BARS } from "../src/state_machine.js";

const T0 = Date.parse("2026-09-04T19:55:00Z");

function raw(overrides = {}) {
  return {
    resultId: "rs_a3e99c8084d70a458b11",
    side: "SHORT", symbol: "MNQU6", qty: 20,
    entry: 29570.5, exit: 29582.75, stop: 29580.25,
    pointValue: 2,
    openedAt: new Date(T0 - 4 * 60_000).toISOString(),
    closedAt: new Date(T0).toISOString(),
    path: [29570.5, 29582.75],
    pathSource: "endpoints-only",
    exitSource: "broker",
    mode: "LIVE",
    model: "VP80_REVERSION", grade: "A+",
    ...overrides,
  };
}

function chart(overrides = {}) {
  const t0 = Math.floor(T0 / 1000) - 45 * 60;
  const bars = Array.from({ length: 20 }, (_, i) => {
    const o = 29560 + i * 0.5;
    return [t0 + i * 180, o, o + 4, o - 3, o + 1];
  });
  return {
    version: "NQX-RESULT-CHART/1", tf: 180, bars,
    levels: [{ label: "C: VAH", price: 29566.7 }, { label: "P: VAH", price: 29585 }],
    tp1: 29535, tp2: 29350.75,
    evidence: ["CVD_ALIGNED", "VP_ACCEPTED"], penalties: ["HTF_CONFLICT"],
    htf: { "1h": "UP", "4h": "MIXED" }, volRatio: 0.18, noise: 11, session: "NY PM", source: "raw",
    ...overrides,
  };
}

test("chart が result に載る(bars / levels / targets / tags / htf)", () => {
  const checked = validateResult(raw({ chart: chart() }), { symbol: "MNQU6" });
  assert.equal(checked.ok, true, checked.reason);
  const c = checked.result.chart;
  assert.equal(c.bars.length, 20);
  assert.deepEqual(c.levels[1], { label: "P: VAH", price: 29585 });
  assert.equal(c.tp1, 29535);
  assert.equal(c.tp2, 29350.75);
  assert.deepEqual(c.evidence, ["CVD_ALIGNED", "VP_ACCEPTED"]);
  assert.deepEqual(c.penalties, ["HTF_CONFLICT"]);
  assert.equal(c.htf["1h"], "UP");
  assert.equal(c.session, "NY PM");
});

test("chart が無ければ null(旧 result 互換)", () => {
  const checked = validateResult(raw(), { symbol: "MNQU6" });
  assert.equal(checked.ok, true);
  assert.equal(checked.result.chart, null);
});

test("壊れた chart は result ごと拒否する", () => {
  const cases = [
    [{ version: "X" }, /version/],
    [{ bars: "nope" }, /bars/],
    [{ bars: [[1, 2, 3]] }, /row/],
    [{ bars: [[10, 100, 101, 99, 100], [5, 100, 101, 99, 100]] }, /increasing/],
    [{ bars: [[10, 100, 99, 99, 100]] }, /OHLC/],
    [{ bars: Array.from({ length: RESULT_CHART_MAX_BARS + 1 }, (_, i) => [i, 1, 2, 0, 1]) }, /limit/],
    [{ levels: [{ label: "", price: 1 }] }, /levels/],
    [{ tp1: "abc" }, /tp1/],
    [{ evidence: ["bad tag!"] }, /evidence/],
    [{ evidence: Array.from({ length: 17 }, () => "A") }, /evidence/],
    [{ htf: [1] }, /htf/],
  ];
  for (const [overrides, pattern] of cases) {
    const checked = validateResult(raw({ chart: chart(overrides) }), { symbol: "MNQU6" });
    assert.equal(checked.ok, false, JSON.stringify(overrides));
    assert.match(checked.reason, pattern);
  }
});

test("chart は判定に影響しない(禁止フィールドの拒否・必須項目は従来どおり)", () => {
  assert.equal(validateResult(raw({ chart: chart(), pnl: 1 }), { symbol: "MNQU6" }).ok, false);
  const chk = validateResultChart(chart({ tp1: null, tp2: undefined, evidence: undefined, htf: undefined }));
  assert.equal(chk.ok, true);
  assert.equal(chk.chart.tp1, null);
  assert.deepEqual(chk.chart.evidence, []);
  assert.deepEqual(chk.chart.htf, {});
});
