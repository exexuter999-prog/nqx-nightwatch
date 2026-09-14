import { test } from "node:test";
import assert from "node:assert/strict";
import {
  barStepSec,
  buildScale,
  computeVwapSeries,
  confirmedBars,
  levelAnchorIndex,
  normalizeMarketSnapshot,
  sessionStartSec,
  slotIndices,
} from "../chart.js";

const NOW = Date.parse("2026-08-14T00:10:00Z");

test("TradingView long-form OHLC keys are rendered as 3m bars", () => {
  const snapshot = normalizeMarketSnapshot({
    observedAt: "2026-08-14T00:09:30Z",
    bars: [
      { time: 1786665960, open: 30200, high: 30204.25, low: 30198.75, close: 30203.5, volume: 42 },
      { time: 1786666140, open: 30203.5, high: 30206, low: 30202, close: 30205.25, volume: 51 },
    ],
  }, NOW);
  assert.ok(snapshot);
  assert.deepEqual(snapshot.bars[1], {
    t: 1786666140, o: 30203.5, h: 30206, l: 30202, c: 30205.25, v: 51, valid: true,
  });
  assert.equal(snapshot.ageMs, 30_000);
});

test("short-form OHLC remains backward compatible", () => {
  const snapshot = normalizeMarketSnapshot({
    at: "2026-08-14T00:09:30Z",
    bars: [{ t: 1, o: 10, h: 11, l: 9, c: 10.5 }],
  }, NOW);
  assert.equal(snapshot.bars[0].valid, true);
});

test("canonical evidence reconstructs Repo2 overlays exactly once", () => {
  const snapshot = normalizeMarketSnapshot({
    at: "2026-08-14T00:09:30Z", bars: [{ t: 1, o: 10, h: 11, l: 9, c: 10.5 }],
    strategyEvidence: { version: "R14-STRATEGY-EVIDENCE-1", models: {
      _matrix: { alignment: { BUY: 2 }, activeModels: ["ifvg", "blocks"] },
      ifvg: { zones: [{ lo: 10, hi: 11 }] }, blocks: { zones: [{ lo: 9, hi: 10 }] },
      sessions: { session: "NY" }, fibSd: { levels: [10] },
    } },
  }, NOW);
  assert.deepEqual(snapshot.strategyMatrix.overlays.ifvg, snapshot.strategyMatrix.models.ifvg);
  assert.deepEqual(snapshot.strategyMatrix.overlays.blocks, snapshot.strategyMatrix.models.blocks);
  assert.equal(snapshot.strategyMatrix.overlays.sessions.session, "NY");
  assert.equal(snapshot.strategyMatrix.overlays.fibSd.levels[0], 10);
});

test("impossible OHLC is not accepted as a drawable feed", () => {
  const snapshot = normalizeMarketSnapshot({
    at: "2026-08-14T00:09:30Z",
    bars: [{ time: 1, open: 10, high: 9, low: 8, close: 10 }],
  }, NOW);
  assert.equal(snapshot, null);
});

// ── 2026-08-16 刷新分: ローソク足・時間スロット・VWAP 遡及・レベル起点 ──

const bar = (t, o, h, l, c, v) => ({ t, o, h, l, c, v, valid: true });

test("形成中の最終バーは落とす(確定足のみ)", () => {
  const atMs = 1_786_666_320 * 1000;                    // 最終バーの開始直後に観測
  const bars = [bar(1_786_666_140, 1, 2, 0, 1, 5), bar(1_786_666_320, 1, 2, 0, 1, 5)];
  const out = confirmedBars(bars, 180, atMs);
  assert.equal(out.length, 1);
  assert.equal(out[0].t, 1_786_666_140);
  // close 時刻を過ぎた観測なら確定扱いで残る
  const closed = confirmedBars(bars, 180, (1_786_666_320 + 180) * 1000);
  assert.equal(closed.length, 2);
});

test("欠測期間は時間軸を削らずスロットの飛びとして残る", () => {
  const bars = [bar(0, 1, 2, 0, 1), bar(180, 1, 2, 0, 1), bar(900, 1, 2, 0, 1)];
  const { slots, count } = slotIndices(bars, 180);
  assert.deepEqual(slots, [0, 1, 5]);                   // 2〜4 が空白スロット
  assert.equal(count, 6);
});

test("barStepSec は barResolution 優先・無ければ実測中央値", () => {
  assert.equal(barStepSec([], "3"), 180);
  assert.equal(barStepSec([bar(0,1,2,0,1), bar(60,1,2,0,1), bar(120,1,2,0,1)], null), 60);
});

test("buildScale keeps bars and armed plan as the domain, not distant overlays", () => {
  const bars = [bar(0, 100, 102, 99, 101), bar(180, 101, 103, 100, 102)];
  const scale = buildScale(bars, { strategyMatrix: { overlays: { levels: [{ price: 10_000 }] } } },
    { entry: 102, stop: 98, target: 110 });
  assert.ok(scale.min < 98 && scale.max > 110);
  assert.ok(scale.max < 200, "far overlay must not flatten the execution domain");
});

test("VWAP は hlc3×v の累積で系列復元される", () => {
  const bars = [bar(0, 10, 12, 8, 10, 10), bar(180, 10, 14, 10, 12, 30)];
  const series = computeVwapSeries(bars, 180);
  assert.ok(series);
  assert.equal(series.points.length, 2);
  assert.equal(series.points[0].value, 10);             // hlc3 = (12+8+10)/3
  assert.equal(series.points[1].value, (10 * 10 + 12 * 30) / 40);
  // volume が無いフィードでは復元しない(推測で埋めない)
  assert.equal(computeVwapSeries([bar(0,10,12,8,10), bar(180,10,14,10,12)], 180), null);
});

test("セッション境界(22:00 UTC = 18:00 EDT)が窓内なら anchored", () => {
  const boundary = sessionStartSec(Date.parse("2026-08-14T23:00:00Z") / 1000);
  assert.equal(boundary, Date.parse("2026-08-14T22:00:00Z") / 1000);
  const mk = (t) => bar(t, 10, 12, 8, 10, 5);
  const inWindow = computeVwapSeries(
    [mk(boundary - 180), mk(boundary + 180), mk(boundary + 360)], 180);
  assert.equal(inWindow.anchored, true);
  assert.equal(inWindow.points[0].index, 1);            // 境界以降から累積
  const outWindow = computeVwapSeries([mk(boundary + 180), mk(boundary + 360)], 180);
  assert.equal(outWindow.anchored, false);              // 窓先頭からの近似
});

test("レベルの起点は最初に触れた足の top/bottom 側で判定", () => {
  const bars = [
    bar(0,   100, 104, 98, 102),
    bar(180, 102, 110, 101, 108),                       // 110 を最初に付けた足
    bar(360, 108, 110, 105, 106),
  ];
  assert.deepEqual(levelAnchorIndex(110, bars), { index: 1, side: "top" });
  assert.deepEqual(levelAnchorIndex(98.5, bars), { index: 0, side: "bottom" });
  assert.equal(levelAnchorIndex(140, bars), null);      // 窓内に接触なし → 全幅描画
});

// ── TradingView 流儀の軸(2026-08-23)────────────────────────────────────
import { niceStep } from "../chart.js";
import { readFileSync as readChartSource } from "node:fs";

test("価格グリッドの刻みは 1/2/2.5/5/10/25/50 系の丸い数", () => {
  // 120pt のレンジ・高さ 230px → 行間 28px 以上で最小の丸い刻み
  assert.equal(niceStep(120, 230), 20);
  assert.equal(niceStep(60, 230), 10);
  assert.equal(niceStep(700, 230), 100);
  assert.equal(niceStep(8, 230), 1);
  // 刻みは候補表のどれかで、レンジ/最大本数を下回らない
  for (const range of [5, 40, 150, 900, 4000]) {
    const step = niceStep(range, 230);
    assert.ok([0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000].includes(step));
    assert.ok(step >= range / Math.floor(230 / 28), `${range}: ${step}`);
  }
  assert.equal(niceStep(0, 230), 1, "レンジ 0 でも壊れない");
});

test("ポーリング失敗は既にあるデータを消さない", () => {
  const source = readChartSource(new URL("../chart.js", import.meta.url), "utf8");
  const fetchBody = source.slice(source.indexOf("const fetchMarket = async"),
    source.indexOf("const arm = "));
  assert.match(fetchBody, /catch \{[\s\S]*?if \(!snapshot\) update\(null\);/,
    "catch では snapshot が無いときだけ NO FEED に落とす");
});

test("軸レーンとタグの寸法が定義されている", () => {
  const source = readChartSource(new URL("../chart.js", import.meta.url), "utf8");
  assert.match(source, /const PRICE_LANE = 58;/);
  assert.match(source, /const TIME_LANE = 18;/);
  assert.match(source, /const TAG_H = 14;/);
  // 名前ラベルと引出線はチャートを汚すので描かない
  assert.ok(!/class: "chart-leader"/.test(source), "引出線を描かない");
  assert.ok(!/class: "chart-level-label"/.test(source), "名前ラベルを描かない");
  assert.ok(!/class: "chart-level-stem"/.test(source), "ステムを描かない");
});
