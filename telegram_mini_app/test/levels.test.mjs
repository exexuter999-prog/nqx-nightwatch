// レベル一覧フィード。本体と違い **間引かない** ことを固定する。
import { test } from "node:test";
import assert from "node:assert/strict";
import { classify, collectLevels, collectZones, spread } from "../levels.js";

const VIEW = {
  market: {
    price: 29370,
    vwap: 29372.98,
    sourceSymbol: "CME_MINI:MNQ1!",
    levels: [
      { label: "C: VAH", price: 29486.65 },
      { label: "C: VAL", price: 29318.92 },
      { label: "P: POC", price: 29367.61 },
      { label: "New York High", price: 29533.75 },
      { label: "6pm open", price: 29331 },
      { label: "PP-S1", price: 29280 },
    ],
    strategyEvidence: {
      models: {
        ict: {
          rangeAnchor: { valid: true, high: 29533.75, low: 29220.25, eq: 29377,
            oteBuy: [29320, 29360], oteSell: [29400, 29440] },
          dol: { SELL: { target: 29220.25, run: "LRLR" } },
          fvg: { BULL: [{ lo: 29300, hi: 29320, eligible: true }], BEAR: [] },
        },
        liquidity: { pools: [{ side: "BSL", price: 29540, label: "eq highs" }] },
        crt: { rangeHigh: 29500, rangeLow: 29250 },
        blocks: { zones: [{ lo: 29350, hi: 29365, kind: "OB", active: true }] },
        quarterly: { session: "NewYork", stage: "D" },
      },
    },
  },
  scenario: { entry: 29400, stop: 29430, targets: [29350, 29250] },
};

test("水準を種類で色分けする", () => {
  assert.equal(classify("C: VAH").kind, "VP");
  assert.equal(classify("VWAP").kind, "VWAP");
  assert.equal(classify("P: POC").kind, "PREV");
  assert.equal(classify("New York High").kind, "SESSION");
  assert.equal(classify("6pm open").kind, "OPEN");
  assert.equal(classify("PP-S1").kind, "PIVOT");
  assert.equal(classify("なにか").kind, "OTHER");
});

test("全部集める — シナリオも VWAP も現在値も落とさない", () => {
  const out = collectLevels(VIEW);
  const labels = out.map((r) => r.label);
  for (const want of ["ENTRY", "SL", "TP1", "RUNNER", "LAST", "VWAP",
                      "C: VAH", "New York High", "Range High", "Range EQ",
                      "CRT High", "CRT Low"]) {
    assert.ok(labels.includes(want), `${want} が無い: ${labels.join(",")}`);
  }
  assert.ok(labels.some((l) => l.startsWith("DOL SELL")), labels.join(","));
  assert.ok(labels.some((l) => l.startsWith("BSL")), labels.join(","));
  // 高い順
  for (let i = 1; i < out.length; i += 1) {
    assert.ok(out[i - 1].price >= out[i].price, "価格降順でない");
  }
});

test("本体の 3 件上限のような間引きをしない", () => {
  const out = collectLevels(VIEW);
  // 入力の 6 レベル + VWAP + range 3 + DOL + pool + CRT 2 + シナリオ 4 + LAST
  assert.ok(out.length >= 18, `間引かれている: ${out.length}`);
  const emphasised = out.filter((r) => r.emphasis).length;
  assert.equal(emphasised, 5, "ENTRY/SL/TP1/RUNNER/LAST が強調される");
});

test("ゾーンは帯として集める", () => {
  const zones = collectZones(VIEW);
  const labels = zones.map((z) => z.label);
  assert.ok(labels.some((l) => l.startsWith("FVG BULL")), labels.join(","));
  assert.ok(labels.includes("OTE BUY") && labels.includes("OTE SELL"), labels.join(","));
  assert.ok(labels.some((l) => l.startsWith("OB")), labels.join(","));
  for (const z of zones) assert.ok(z.hi >= z.lo, "帯の上下が逆");
});

test("重なるラベルは押し広げる。捨てない", () => {
  const items = [{ y0: 100 }, { y0: 101 }, { y0: 102 }, { y0: 103 }];
  const rows = spread(items, 0, 400, 13);
  assert.equal(rows.length, items.length, "本数が減っている");
  for (let i = 1; i < rows.length; i += 1) {
    assert.ok(rows[i].y - rows[i - 1].y >= 12.99, `重なっている: ${rows.map((r) => r.y)}`);
  }
});

test("上端に固まっていても潰さない", () => {
  // 実データで踏んだ不具合: 最上部の 3 行が重なっていた。下端補正で全体を
  // 上へ寄せた後の clamp が、上端の行を再び同じ y へ潰していた。
  const items = [{ y0: 20 }, { y0: 20.5 }, { y0: 21 }, { y0: 22 }];
  const rows = spread(items, 20, 400, 13);
  for (let i = 1; i < rows.length; i += 1) {
    assert.ok(rows[i].y - rows[i - 1].y >= 12.99,
      `上端で重なっている: ${rows.map((r) => r.y)}`);
  }
  assert.ok(rows[0].y >= 20, "上へ突き抜けている");
});

test("キャンバス高さを件数から取れば下端に収まる", () => {
  const n = 24, gap = 13, top = 20;
  const items = Array.from({ length: n }, () => ({ y0: top }));   // 全部同じ位置
  const rows = spread(items, top, top + n * gap, gap);
  assert.ok(rows[rows.length - 1].y <= top + n * gap,
    `はみ出している: ${rows[rows.length - 1].y}`);
});

test("空の入力で落ちない", () => {
  assert.deepEqual(collectLevels({}), []);
  assert.deepEqual(collectZones({}), []);
  assert.deepEqual(collectLevels(null), []);
  assert.deepEqual(spread([], 0, 100), []);
});
