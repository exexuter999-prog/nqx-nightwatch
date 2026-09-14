// R57: 根拠チャートの純関数(座標系・MAE/MFE・決済後・決済種別・目盛り)。
import { test } from "node:test";
import assert from "node:assert/strict";
import { buildChartModel, classifyExit, scales, metricRows } from "../resultchart.js";

const OPEN = Date.parse("2026-09-04T19:51:27Z") / 1000;
const CLOSE = Date.parse("2026-09-04T19:55:00Z") / 1000;

function trade(overrides = {}) {
  const t0 = OPEN - 45 * 60;
  const bars = [];
  for (let i = 0; i < 24; i += 1) {
    const t = t0 + i * 180;
    // 保有中(04:51〜04:55)の 2 本は高値 29,583(MAE)と安値 29,559.5(MFE)を作る
    const inHold = t + 180 > OPEN && t < CLOSE;
    const after = t >= CLOSE;
    const base = 29560 + i * 0.5;
    if (inHold) bars.push([t, 29572, 29583, 29559.5, 29580]);
    else if (after) bars.push([t, 29580 - (i - 16) * 8, 29584 - (i - 16) * 8, 29530 - (i - 16) * 8, 29535 - (i - 16) * 8]);
    else bars.push([t, base, base + 4, base - 3, base + 1]);
  }
  return {
    side: "SHORT", symbol: "MNQU6", qty: 20, entry: 29570.5, exit: 29582.75, stop: 29580.25, pointValue: 2,
    openedAt: new Date(OPEN * 1000).toISOString(), closedAt: new Date(CLOSE * 1000).toISOString(),
    path: [29570.5, 29582.75], model: "VP80_REVERSION", grade: "A+",
    chart: {
      version: "NQX-RESULT-CHART/1", tf: 180, bars,
      levels: [{ label: "C: VAH", price: 29566.7 }, { label: "P: VAH", price: 29585 }, { label: "P: VAL", price: 29350.7 }],
      tp1: 29535, tp2: 29350.75, evidence: ["CVD_ALIGNED", "VP_ACCEPTED"], penalties: ["HTF_CONFLICT"],
      htf: { "1h": "UP" }, volRatio: 0.18, noise: 11, session: "NY PM", source: "raw",
    },
    ...overrides,
  };
}

test("足が無い(旧 result)なら null → 従来の path 描画に落ちる", () => {
  assert.equal(buildChartModel({ side: "SHORT", entry: 1, exit: 2 }), null);
  assert.equal(buildChartModel(trade({ chart: { bars: [[1, 1, 1, 1, 1]] } })), null);
});

test("保有中の MAE / MFE は実在する高安から(SHORT)", () => {
  const m = buildChartModel(trade());
  assert.equal(m.mae.pt, 12.5);          // 29583 − 29570.5
  assert.equal(m.mae.price, 29583);
  assert.equal(m.mfe.pt, 11);            // 29570.5 − 29559.5
  assert.equal(m.maeR, 1.28);            // SL 9.75pt
  assert.equal(m.held, 2);
  assert.equal(m.holdMin, 3.6);
});

test("決済後: 有利側の伸びと TP1 到達までの分数(損切りの検証)", () => {
  const m = buildChartModel(trade());
  assert.equal(m.exitKind, "SL");
  assert.equal(m.slip, 2.5);
  assert.ok(m.post.favPt > 40, String(m.post.favPt));
  assert.ok(m.post.tp1AfterMin !== null && m.post.tp1AfterMin > 0, String(m.post));
});

test("価格レンジは足+ENTRY/SL/TP1 を含み、遠い TP2 は枠外扱い", () => {
  const m = buildChartModel(trade());
  assert.ok(m.lo < 29535 && m.hi > 29583);
  assert.equal(m.tp2Visible, false);
  const levels = m.levels.map((l) => l.label);
  assert.deepEqual(levels, ["C: VAH", "P: VAH"]);   // P: VAL 29350.7 は枠外
  assert.equal(m.levels[0].kind, "C");
});

test("座標変換は帯の内側に収まる", () => {
  const m = buildChartModel(trade());
  const { xOf, yOf } = scales(m, { x: 10, y: 20, w: 400, h: 200 });
  assert.equal(Math.round(xOf(m.t0)), 10);
  assert.equal(Math.round(xOf(m.t1)), 410);
  assert.equal(Math.round(yOf(m.hi)), 20);
  assert.equal(Math.round(yOf(m.lo)), 220);
});

test("目盛り: 時間は 15 分ごと、価格は 7 本以下", () => {
  const m = buildChartModel(trade());
  assert.ok(m.timeTicks.length >= 3);
  assert.ok(m.timeTicks.every((t) => t.t % 900 === 0));
  assert.ok(m.priceTicks.length <= 8);
});

test("決済の種類", () => {
  assert.deepEqual(classifyExit("SHORT", 29570.5, 29582.75, 29580.25, 29535, null), { kind: "SL", slip: 2.5 });
  assert.deepEqual(classifyExit("SHORT", 29570.5, 29535.25, 29580.25, 29535, null), { kind: "TP1", slip: null });
  assert.deepEqual(classifyExit("SHORT", 29570.5, 29552, 29580.25, 29535, null), { kind: "PARTIAL", slip: null });
  assert.deepEqual(classifyExit("LONG", 20000, 19989.5, 19990, 20030, null), { kind: "SL", slip: 0.5 });
  assert.deepEqual(classifyExit("SHORT", 29570.5, 29574, 29599.5, null, null), { kind: "MANUAL", slip: null });
});

test("計測行は数字がある項目だけ", () => {
  const rows = metricRows(buildChartModel(trade()));
  const keys = rows.map(([k]) => k);
  assert.ok(keys.includes("MAE") && keys.includes("MFE") && keys.includes("EXIT BY") && keys.includes("SESSION"));
  const noHold = buildChartModel(trade({ openedAt: undefined }));
  assert.equal(noHold.mae, null);
  assert.ok(!metricRows(noHold).some(([k]) => k === "MAE"));
});
