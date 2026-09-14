// R58: 市場レーダー(radar.js)。market を読むだけで、判定も推測もしない。
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  buildRadar, buildLadder, buildBias, buildFlow, buildClock, buildSetup,
  collectLevels, mergeLevels, cvdSeries, sparkline, humanize, renderRadar, CVD_SENTINEL,
} from "../radar.js";

function market() {
  return {
    price: 29532.25, vwap: 29567.81, regime: "MX",
    levels: [
      { label: "C: POC", price: 29506.29 }, { label: "C: VAH", price: 29566.7 }, { label: "C: VAL", price: 29468.25 },
      { label: "P: POC", price: 29526.43 }, { label: "Weekly open", price: 29535 }, { label: "6pm open", price: 29500 },
      { label: "Asia Low", price: 29610.5 }, { label: "WIN HIGH", price: 29700 },
    ],
    indicators: {
      vwapHi: 29624.01, vwapLo: 29511.61,
      cvd: { value: -19025, fast: -26574, slow: -34031, bias: "BULLISH", history: [-21883, 59999, -17188, -19025] },
      po3: "MANIPULATION_UP", smt: { peer: "MES1!", bias: "BULLISH", agree: true },
      ct: { trend: -1, atr: 37.58, fib618: 29599.54, trail: 29680.69, ltfTrend: 1, hardStop: 29688.2 },
      event: "NONE",
    },
    strategyEvidence: { models: {
      htf: { frames: { "1d": { structure: "MIXED", ema20: 29408.57, emaSlope5: -28.84 },
        "1h": { structure: "BULLISH", ema20: 29552.49, emaSlope5: 8.53 } } },
      vwapReversion: { ema: 29539.34, atr: 14.12 },
      liquidity: { pools: [{ side: "BSL", price: 29587, kind: "SWING_HIGH" }, { side: "SSL", price: 29529.5, kind: "SWING_LOW" },
        { side: "SSL", price: 29506.29, kind: "NAMED_LEVEL", label: "C: POC" }], dol: { BUY: 29535, SELL: 29529.5 } },
      ict: { fvg: { BULL: [{ lo: 29494, hi: 29507, ageBars: 27 }], BEAR: [] } },
      ifvg: { status: "CONFIRMED", direction: "BUY", zones: [{ lo: 29520, hi: 29522.5, direction: "BUY", active: true }] },
      crt: { rangeHigh: 29544, rangeLow: 29536.75 },
      quarterly: { stage: "D", quarter: 3 },
      mmxm: { mss: "BEARISH" },
      gann: { angles: [{ label: "Gann 1x1", price: 29565.62, primary: true }] },
    } },
    evaluation: {
      msnr: { label: "C: VAH", price: 29566.7, chainType: "SWEEP", chainState: "MSS_CONFIRMED", barsLeft: 8, allowed: false,
        blockers: ["RETEST_NOT_HELD", "FLIP_RETEST_NOT_HELD"] },
      cvdGate: { status: "FRESH" },
      sessionGate: { window: "OFF_HOURS", et: "16:24", tradeable: false },
      advisory: { vwap: { side: "BUY", state: "VWAP_BREAK" } },
      volGate: { ratio: 0.24, noise: 14.25, ruling: "通常" },
      rotation: { signals: 4, negations: 0, verdict: "OK" },
      dataGate: { freshCount: 8, requiredCount: 8, requiredFresh: true },
      decision: { model: "FLAT", side: "FLAT", state: "WATCH", hardBlockers: ["NO_A_OR_A_PLUS_MODEL"] },
    },
  };
}

const names = (rows) => rows.map((r) => r.names.join(" · "));
const find = (rows, name) => rows.find((r) => r.names.includes(name));
const near = (a, b, eps = 1e-6) => assert.ok(Math.abs(a - b) < eps, `${a} ≈ ${b}`);

test("PRICE MAP: 価格を持つものが全部 1 本の梯子になり、上下とも価格の降順", () => {
  const ladder = buildLadder(market());
  assert.equal(ladder.last, 29532.25);
  const prices = [...ladder.above, ...ladder.below].map((r) => r.price);
  assert.deepEqual(prices, [...prices].sort((a, b) => b - a), "上端が最高値、下端が最安値");
  assert.ok(ladder.above.every((r) => r.price >= ladder.last) && ladder.below.every((r) => r.price < ladder.last));
  // 種別をまたいで集まる: VP・開始値・VWAP 帯・EMA・HTF EMA20・BSL/SSL・FVG・IFVG・CRT・GANN・15分・HARD SL
  for (const name of ["VAH", "POC", "P.POC", "6PM OPEN", "VWAP", "VWAP HI", "VWAP LO", "EMA", "EMA20 1D", "EMA20 1H",
    "BSL", "SSL", "FVG ▲ 27b", "IFVG BUY", "CRT", "GANN 1x1", "15M 61.8", "15M TRAIL", "HARD SL", "ASIA LOW"]) {
    assert.ok(find([...ladder.above, ...ladder.below], name), `${name} が梯子にある`);
  }
  assert.ok(!find([...ladder.above, ...ladder.below], "WIN HIGH"), "チャート内部の窓高安は出さない");
});

test("PRICE MAP: 距離は現在値との差、帯は HTF EMA20 を除いた最大距離で正規化", () => {
  const m = market();
  m.strategyEvidence.models.htf.frames["1d"].ema20 = 28800;   // 日足 EMA20 は 700pt 以上下
  const ladder = buildLadder(m);
  const all = [...ladder.above, ...ladder.below];
  near(find(all, "VAH").dist, 29566.7 - 29532.25);
  near(find(all, "SSL").dist, 29529.5 - 29532.25);
  assert.ok(all.every((r) => r.bar >= 0 && r.bar <= 1));
  const farthestNonHtf = all.filter((r) => r.kind !== "htf").sort((a, b) => Math.abs(b.dist) - Math.abs(a.dist))[0];
  near(farthestNonHtf.bar, 1, 1e-9);
  assert.equal(find(all, "EMA20 1D").bar, 1, "数百 pt 先の日足 EMA20 は帯 100% で止まり、尺度を潰さない");
  assert.ok(find(all, "VAH").bar > 0.1, "近い水準の帯が読める太さで残る");
});

test("PRICE MAP: VWAP 系は水準リストに同名があればそちらを正とし、指標側を重ねない", () => {
  const m = market();
  m.levels.push({ label: "VWAP HI", price: 29630 });      // チャートの水準(indicators.vwapHi 29,624.01 と違う値)
  const all = [...buildLadder(m).above, ...buildLadder(m).below];
  const rows = all.filter((r) => r.names.includes("VWAP HI"));
  assert.equal(rows.length, 1);
  assert.equal(rows[0].price, 29630);
  assert.equal(all.filter((r) => r.names.includes("VWAP LO")).length, 1, "無い名前は indicators から補う");
});

test("PRICE MAP: 同じ tick の点水準は 1 行に合流し、タグが付く(DOL / MSNR)", () => {
  const ladder = buildLadder(market());
  const all = [...ladder.above, ...ladder.below];
  const wk = find(all, "WK OPEN");
  assert.deepEqual(wk.names, ["WK OPEN", "DOL ▲"], "DOL BUY 29,535 は同値の WK OPEN へ合流");
  const ssl = find(all, "SSL");
  assert.deepEqual(ssl.tags, ["DOL"], "DOL SELL は SSL プールと同値なのでタグに");
  const vah = find(all, "VAH");
  assert.deepEqual(vah.tags, ["MSNR"], "MSNR の水準は VAH に合流してタグに");
  assert.equal(all.filter((r) => r.names.includes("VAH")).length, 1, "二重に出ない");
  assert.equal(vah.kind, "vp");
});

test("PRICE MAP: ゾーンは近い縁までの距離、現在値を含めば INSIDE(0)", () => {
  const ladder = buildLadder(market());
  const all = [...ladder.above, ...ladder.below];
  const crt = find(all, "CRT");
  assert.equal(crt.side, "above");
  near(crt.dist, 29536.75 - 29532.25, 1e-6);
  const fvg = find(all, "FVG ▲ 27b");
  assert.equal(fvg.side, "below");
  near(fvg.dist, 29507 - 29532.25);
  const inside = buildLadder({ ...market(), price: 29540 });
  const crtIn = find([...inside.above, ...inside.below], "CRT");
  assert.equal(crtIn.inside, true);
  assert.equal(crtIn.dist, 0);
});

test("PRICE MAP: 計画(ENTRY/SL/TP)は ARMED/ACTIVE のときだけ載る", () => {
  const watch = buildLadder(market());
  assert.ok(!find([...watch.above, ...watch.below], "ENTRY"));
  const m = market();
  m.evaluation.decision = { model: "VP80_REVERSION", side: "SELL", state: "ARMED", grade: "A", entry: 29560, stop: 29590, targets: [29520, 29480] };
  const armed = buildLadder(m);
  const all = [...armed.above, ...armed.below];
  assert.equal(find(all, "ENTRY").kind, "plan");
  assert.ok(find(all, "SL") && find(all, "TP1") && find(all, "TP2"));
});

test("PRICE MAP: 価格が無ければ空、上下は各 24 本まで(近い順に残す)", () => {
  assert.deepEqual(buildLadder({ levels: [{ label: "C: POC", price: 1 }] }), { last: null, above: [], below: [], total: 0 });
  const many = { price: 30000, levels: Array.from({ length: 40 }, (_, i) => ({ label: `L${i}`, price: 30001 + i })) };
  const ladder = buildLadder(many);
  assert.equal(ladder.above.length, 24);
  assert.equal(ladder.above[ladder.above.length - 1].price, 30001, "一番近いものは残る");
  assert.equal(ladder.total, 40);
});

test("BIAS: 向きを持つものだけがチップになり、色は向きから", () => {
  const chips = buildBias(market());
  const labels = chips.map((c) => c.label);
  assert.deepEqual(labels, ["1D", "1H", "VWAP", "CVD", "SMT", "MSS", "IFVG", "PO3", "15M CT", "LTF", "REGIME"]);
  const byLabel = Object.fromEntries(chips.map((c) => [c.label, c]));
  assert.deepEqual([byLabel["1D"].mark, byLabel["1D"].word, byLabel["1D"].tone], ["▼", "MIX", ""]);
  assert.deepEqual([byLabel["1H"].mark, byLabel["1H"].word, byLabel["1H"].tone], ["▲", "BULL", "is-up"]);
  assert.deepEqual([byLabel.VWAP.mark, byLabel.VWAP.word, byLabel.VWAP.tone], ["▲", "BREAK", "is-up"]);
  assert.deepEqual([byLabel.SMT.word, byLabel.SMT.title], ["BULL ✓", "MES1!"]);
  assert.deepEqual([byLabel.MSS.mark, byLabel.MSS.tone], ["▼", "is-down"]);
  assert.deepEqual([byLabel.IFVG.mark, byLabel.IFVG.word, byLabel.IFVG.title], ["▲", "CONF", "BUY CONFIRMED"], "向きは矢印、語は状態だけ");
  assert.deepEqual([byLabel.PO3.mark, byLabel.PO3.word, byLabel.PO3.title], ["▲", "MANIP", "MANIPULATION UP"]);
  assert.deepEqual([byLabel["15M CT"].mark, byLabel["15M CT"].tone, byLabel.LTF.mark], ["▼", "is-down", "▲"]);
  assert.deepEqual([byLabel.REGIME.word, byLabel.REGIME.tone], ["MX", ""]);
});

test("FLOW / CLOCK: 価格でない計測はタイル、CVD はスパークライン付き(センチネル除外)", () => {
  const flow = buildFlow(market());
  assert.deepEqual(flow.map((t) => t.label), ["CVD", "CVD FAST", "CVD SLOW", "Δ VWAP", "ATR 3M", "ATR 15M", "NOISE"]);
  const cvd = flow[0];
  assert.deepEqual([cvd.value, cvd.tone, cvd.sub, cvd.spark], ["−19,025", "is-down", "FRESH", [-21883, -17188, -19025]]);
  assert.deepEqual([flow[3].value, flow[3].tone], ["−35.6pt", "is-down"]);
  assert.deepEqual([flow[6].value, flow[6].sub], ["14.3pt", "×0.24"]);
  const clock = buildClock(market());
  assert.deepEqual(clock.map((t) => [t.label, t.value, t.sub]), [["KILLZONE", "OFF HOURS", "16:24 ET"], ["QUARTER", "D", "q3"]]);
  assert.deepEqual(cvdSeries([-21883, 59999, -17188, "x", null, -19025]), [-21883, -17188, -19025]);
  assert.equal(CVD_SENTINEL, 59999);
  assert.match(sparkline([-21883, -17188, -19025]), /<polyline points="/);
  assert.equal(sparkline([1]), "");
});

test("SETUP: MSNR とモデルの結論。識別子は区切りを空けるだけで意味は変えない", () => {
  const rows = buildSetup(market());
  assert.deepEqual(rows.map((r) => r.key), ["MSNR", "MODEL"]);
  assert.equal(rows[0].text, "C: VAH · SWEEP · MSS CONFIRMED · 8b");
  assert.deepEqual([rows[0].tone, rows[0].blockers], ["is-warn", ["RETEST NOT HELD", "FLIP RETEST NOT HELD"]]);
  assert.equal(rows[1].text, "FLAT · WATCH");
  assert.deepEqual(rows[1].blockers, ["NO A OR A PLUS MODEL"]);
  assert.equal(humanize("TARGET_HEADROOM_INSUFFICIENT"), "TARGET HEADROOM INSUFFICIENT");
});

test("mergeLevels / collectLevels は入力の形に寛容(壊れた水準は落とす)", () => {
  const levels = collectLevels({ levels: [{ label: "", price: 1 }, { label: "X", price: "abc" }, { label: "Y", price: 2 }] });
  assert.deepEqual(levels.map((l) => l.name), ["Y"]);
  const merged = mergeLevels([{ name: "A", kind: "named", price: 10, tags: [] }, { name: "B", kind: "vp", price: 10.1, tags: ["T"] },
    { name: "Z", kind: "zone", lo: 9, hi: 11, price: 10, zone: true, tags: [] }]);
  assert.equal(merged.length, 2);
  assert.deepEqual([merged[0].names, merged[0].kind, merged[0].tags], [["A", "B"], "vp", ["T"]]);
});

test("buildRadar: market が無ければ null。renderRadar は無ければ未確認の一枚", () => {
  assert.equal(buildRadar(null), null);
  const host = { dataset: {}, classList: { toggle() {} }, innerHTML: "" };
  renderRadar(host, null);
  assert.match(host.innerHTML, /NO VERIFIED MARKET/);
  renderRadar(host, buildRadar(market()));
  for (const text of ["PRICE MAP", "LAST", "29,532.25", "BIAS", "FLOW", "CLOCK", "SETUP", "rd-spark", "MSS CONFIRMED", "−2\\.8"]) {
    assert.match(host.innerHTML, new RegExp(text), `${text} を描く`);
  }
  assert.ok(!host.innerHTML.includes("<script"), "生の HTML を差し込まない");
});

test("レーダーは market を読むだけ(発注・保存・ネットワークに触れない)", async () => {
  const { readFileSync } = await import("node:fs");
  const source = readFileSync(new URL("../radar.js", import.meta.url), "utf8");
  for (const forbidden of ["fetch(", "sendData", "localStorage", "/api/", "WebSocket"]) {
    assert.ok(!source.includes(forbidden), `${forbidden} を含まない`);
  }
});
