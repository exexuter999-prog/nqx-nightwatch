// 価格予測の氷ローソク。計画のアンカーの外側に価格を発明しないことを固定する。
import { test } from "node:test";
import assert from "node:assert/strict";
import { projectionCandles, renderIceCandles, iceBlocks, renderProjectionPath, wickSegments } from "../icecandles.js";

test("ヒゲは実体の外側だけ 2 本に分かれ、実体の中を貫かない", () => {
  const seg = wickSegments({ cy: 100, h: 40, wickCy: 100, wickH: 70 });   // 実体 80..120、ヒゲ 65..135
  assert.deepEqual(seg, { up: { cy: 72.5, len: 15 }, down: { cy: 127.5, len: 15 } });
  assert.ok(seg.up.cy + seg.up.len / 2 <= 80 + 1e-9 && seg.down.cy - seg.down.len / 2 >= 120 - 1e-9, "実体 80..120 に重ならない");
  const none = wickSegments({ cy: 100, h: 40, wickCy: 100, wickH: 40 });
  assert.equal(none.up.len, 0);
  assert.equal(none.down.len, 0);
  const upOnly = wickSegments({ cy: 100, h: 40, wickCy: 95, wickH: 50 });   // ヒゲ 70..120
  assert.equal(upOnly.up.len, 10);
  assert.equal(upOnly.down.len, 0);
});

const LONG = { last: 100, entry: 96, stop: 92, target: 110, side: "LONG" };
const SHORT = { last: 100, entry: 104, stop: 108, target: 90, side: "SHORT" };

test("計画のアンカーの外側の価格を発明しない", () => {
  for (const plan of [LONG, SHORT]) {
    const candles = projectionCandles(plan, 8);
    const lo = Math.min(plan.last, plan.entry, plan.stop, plan.target);
    const hi = Math.max(plan.last, plan.entry, plan.stop, plan.target);
    for (const k of candles) {
      for (const v of [k.o, k.h, k.l, k.c]) {
        assert.ok(v >= lo - 1e-9 && v <= hi + 1e-9, `${v} が [${lo}, ${hi}] の外`);
      }
      assert.ok(k.h >= Math.max(k.o, k.c) && k.l <= Math.min(k.o, k.c));
    }
  }
});

test("approach は建値へ、run は目標へ向かう", () => {
  const candles = projectionCandles(LONG, 9);
  const approach = candles.filter((k) => k.phase === "approach");
  const run = candles.filter((k) => k.phase === "run");
  assert.ok(approach.length >= 1 && run.length >= 1);
  assert.equal(candles[0].o, LONG.last);
  assert.equal(candles[candles.length - 1].c, LONG.target);
  // approach は下降(買いは押し目を待つ)、run は上昇。
  assert.ok(approach[approach.length - 1].c < LONG.last);
  assert.ok(run[run.length - 1].c > run[0].o);
});

test("小さなヒゲはあるが SL には届かない(2026-09-06: SL までのヒゲは廃止、無しも寂しい)", () => {
  for (const plan of [LONG, SHORT]) {
    const candles = projectionCandles(plan, 9);
    const pivots = candles.filter((k) => k.phase === "pivot");
    assert.equal(pivots.length, 1, "pivot は 1 本だけ");
    let wicked = 0;
    for (const k of candles) {
      const body = Math.abs(k.c - k.o);
      const up = k.h - Math.max(k.o, k.c);
      const down = Math.min(k.o, k.c) - k.l;
      assert.ok(up >= 0 && down >= 0, "ヒゲは実体の外側");
      assert.ok(up <= body * 0.5 + 1e-9 || k.phase === "pivot", `上ヒゲが長すぎる ${up}/${body}`);
      assert.ok(down <= body * 0.5 + 1e-9 || k.phase === "pivot", `下ヒゲが長すぎる ${down}/${body}`);
      if (up > 0 || down > 0) wicked += 1;
    }
    assert.ok(wicked >= candles.length - 1, "ほぼ全ての足に小さなヒゲがある");
    // SL には触れない(建値の足も SL との距離の 45% で止まる)
    const long = plan.stop < plan.entry;
    const pivot = pivots[0];
    const reach = long ? pivot.l : pivot.h;
    assert.ok(Math.abs(reach - plan.stop) > Math.abs(plan.entry - plan.stop) * 0.5, "建値の足のヒゲは SL の手前で止まる");
    assert.ok(!candles.some((k) => k.l === plan.stop || k.h === plan.stop), "どの足も SL に触れない");
    // 決定的(乱数なし): 同じ計画は同じ絵
    assert.deepEqual(projectionCandles(plan, 9), candles);
  }
});

test("方向として破綻した計画は描かない", () => {
  assert.equal(projectionCandles({ ...LONG, stop: 99, entry: 96 }, 6), null); // SL が建値の上
  assert.equal(projectionCandles({ ...LONG, target: 90 }, 6), null);          // 目標が逆側
  assert.equal(projectionCandles({ last: 1, entry: 2 }, 6), null);            // 欠損
  assert.equal(projectionCandles(null, 6), null);
});

test("SVG は前面・上面・側面の三面体を足ごとに出す", () => {
  const nodes = [];
  const make = (tag, attrs) => {
    const node = { tag, attrs, children: [], append(c) { this.children.push(c); }, set textContent(v) {} };
    nodes.push(node);
    return node;
  };
  const candles = projectionCandles(LONG, 6);
  const g = renderIceCandles({
    make, candles, y: (v) => 200 - v, x0: 100, spanW: 120, long: true, uid: "t",
  });
  assert.equal(g.tag, "g");
  const count = (cls) => nodes.filter((n) => String(n.attrs.class || "").startsWith(cls)).length;
  assert.equal(count("ice-face"), candles.length);
  assert.equal(count("ice-top"), candles.length);
  assert.equal(count("ice-side"), candles.length);
  // ヒゲは実体の上下に分かれるので足ごとに 0〜2 本(0.5px 未満は描かない)。長さ 0 の線は出さない
  const wickLines = nodes.filter((n) => String(n.attrs.class || "") === "ice-wick");
  assert.ok(wickLines.length >= 1 && wickLines.length <= candles.length * 2, `wick lines ${wickLines.length}`);
  wickLines.forEach((n) => assert.ok(Math.abs(n.attrs.y2 - n.attrs.y1) > 0.5, "長さのあるヒゲだけ"));
  // 透明であること: 面は不透明塗りではなく gradient 参照。
  const faces = nodes.filter((n) => String(n.attrs.class || "").startsWith("ice-face"));
  assert.ok(faces.every((f) => String(f.attrs.fill).startsWith("url(#")));
  // 面取りされている(矩形ではなく 8 頂点)。
  assert.ok(faces.every((f) => f.attrs.points.split(" ").length === 8));
});

test("ピクセル矩形は未来ゾーンの中に収まり、pivot が 1 本だけ立つ", () => {
  const candles = projectionCandles(LONG, 6);
  const blocks = iceBlocks({ candles, y: (v) => 200 - v, x0: 300, spanW: 120 });
  assert.equal(blocks.length, candles.length);
  for (const b of blocks) {
    assert.ok(b.cx >= 300 && b.cx <= 420, `cx ${b.cx} が未来ゾーン外`);
    assert.ok(b.w > 0 && b.h > 0 && b.wickH > 0);
    // ヒゲは実体を必ず含む
    assert.ok(b.wickH >= b.h - 1e-9, `${b.wickH} < ${b.h}`);
  }
  assert.equal(blocks.filter((b) => b.pivot).length, 1);
});

test("ローソクが無ければ矩形も無い(3D は何も描かない)", () => {
  assert.deepEqual(iceBlocks({ candles: [], y: (v) => v, x0: 0, spanW: 10 }), []);
  assert.deepEqual(iceBlocks({ candles: null, y: (v) => v, x0: 0, spanW: 10 }), []);
});

test("軌道は現値から始まり、各足の終値を結ぶ", () => {
  const nodes = [];
  const make = (tag, attrs) => {
    const n = { tag, attrs, children: [], append(c) { this.children.push(c); } };
    nodes.push(n);
    return n;
  };
  const candles = projectionCandles(LONG, 6);
  const blocks = iceBlocks({ candles, y: (v) => 200 - v, x0: 300, spanW: 120 });
  const g = renderProjectionPath({ make, blocks, fromX: 290, fromY: 100, long: true, uid: "p" });
  const core = nodes.find((n) => n.attrs.class === "ice-path-core");
  assert.ok(core, "芯の線がある");
  const pts = core.attrs.d.split(/[ML]/).filter(Boolean).map((p) => p.split(",").map(Number));
  assert.equal(pts.length, blocks.length + 1, "現値 + 各足の終値");
  assert.deepEqual(pts[0], [290, 100], "現値から始まる");
  assert.equal(pts[pts.length - 1][0], Number(blocks[blocks.length - 1].cx.toFixed(1)));
  // にじみは芯より太い(下敷きとして機能する)
  const glow = nodes.find((n) => n.attrs.class === "ice-path-glow");
  assert.ok(glow.attrs["stroke-width"] > core.attrs["stroke-width"]);
});

test("足が 2 本未満なら軌道を描かない", () => {
  const make = (tag, attrs) => ({ tag, attrs, append() {} });
  assert.equal(renderProjectionPath({ make, blocks: [], fromX: 0, fromY: 0, long: true }), null);
  assert.equal(renderProjectionPath({ make, blocks: null, fromX: 0, fromY: 0, long: true }), null);
});

// 2026-09-06 バグ狩り: 氷の 3D は「見えている間だけ回す」「main チャンクを抱えない」。
import { readFileSync as readSrc } from "node:fs";
test("icescene: 見えていない間は RAF を止め(IntersectionObserver)、destroy で外す。icecandles.js(main)から import しない(遅延予算の罠)", () => {
  const src = readSrc(new URL("../icescene.js", import.meta.url), "utf8");
  assert.ok(src.includes("new IntersectionObserver("), "見えている間だけ回す observer がある");
  assert.ok(/if \(disposed \|\| !frame \|\| !visible\) return;/.test(src), "tick は見えていなければ回さない");
  assert.ok(src.includes("observer?.disconnect();"), "destroy で observer を外す");
  assert.ok(!src.includes('from "./icecandles.js"'), "icecandles.js(main チャンク)から import しない");
  assert.ok(src.includes('from "./wicks.js"'), "ヒゲの分割は wicks.js から");
});
