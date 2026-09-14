// R64: カード背景の鉄粉(cardfield.js)。純関数と配線だけを検査する(canvas は node に無い)。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  layoutGrid, restAngle, hash2, influence, fieldAngle, lerpAngle, angleDelta, rippleGlow, magnetStrength,
  attachCardField, attachCardFields, SPACING, SIGMA, RIPPLE_MS, RELAX_MS, BASE_ALPHA, PEAK_ALPHA,
} from "../cardfield.js";

test("磁力の強さ: 触れている間は 1、離すと RELAX_MS かけて直線で 0 へ(離した瞬間に消えず、止まった後に急に消えない)", () => {
  assert.equal(magnetStrength(true, 99999), 1, "居る間はずっと 1");
  assert.equal(magnetStrength(false, 0), 1, "離した瞬間はまだ 1");
  assert.ok(Math.abs(magnetStrength(false, RELAX_MS / 2) - 0.5) < 1e-9, "半分の時間で半分");
  assert.equal(magnetStrength(false, RELAX_MS), 0, "RELAX_MS で 0");
  assert.equal(magnetStrength(false, RELAX_MS * 3), 0);
  assert.equal(magnetStrength(false, -Infinity), 0, "一度も触れていなければ 0");
});

test("格子: 面を等間隔に切り、一行おきに半格子ずらす。端に貼り付かない", () => {
  const pts = layoutGrid(140, 70, SPACING);
  assert.ok(pts.length > 30 && pts.length < 60, `${pts.length} 粒`);
  assert.ok(pts.every((p) => p.x >= 0 && p.x <= 140 && p.y >= 0 && p.y <= 70));
  const row0 = pts.filter((p) => p.iy === 0).map((p) => p.x);
  const row1 = pts.filter((p) => p.iy === 1).map((p) => p.x);
  assert.ok(Math.abs((row1[0] - row0[0]) - SPACING / 2) < 1e-9, "半格子ずれ");
  assert.deepEqual(layoutGrid(5, 5).length, 1, "小さくても 1 粒はある");
});

test("眠りの向き: 決定的で、ほぼ水平(±28°)に散る", () => {
  assert.equal(restAngle(3, 4, 7), restAngle(3, 4, 7));
  assert.notEqual(restAngle(3, 4, 7), restAngle(4, 3, 7));
  for (let i = 0; i < 200; i += 1) {
    const a = restAngle(i % 17, Math.floor(i / 17), 7);
    assert.ok(Math.abs(a) <= 28 * Math.PI / 180 + 1e-9);
  }
  const h = hash2(1, 2, 3);
  assert.ok(h >= 0 && h <= 1);
});

test("磁力: 近いほど強く、遠くは 0 に落ちる。鉄粉は磁極へ放射状に並ぶ(極性なし)", () => {
  assert.ok(influence(0, 0) > 0.999);
  assert.ok(influence(SIGMA, 0) < influence(0, 0) && influence(SIGMA, 0) > 0.5);
  assert.ok(influence(SIGMA * 4, 0) < 0.001);
  // 磁極が右にある鉄粉は水平(0)へ、上にある鉄粉は垂直(±π/2)へ
  assert.ok(Math.abs(fieldAngle(0.3, 100, 0, 1)) < 1e-9);
  assert.ok(Math.abs(Math.abs(fieldAngle(0.1, 0, -100, 1)) - Math.PI / 2) < 1e-9);
  // 極性なし: 左の磁極でも同じ線に並ぶ(π 回した角は同じ向き)
  const left = fieldAngle(0.3, -100, 0, 1);
  assert.ok(Math.abs(Math.sin(left)) < 1e-9, "左でも水平");
  assert.equal(fieldAngle(0.3, 100, 0, 0), 0.3, "磁力 0 なら眠りのまま");
  assert.ok(Math.abs(fieldAngle(0.3, 100, 0, 0.5) - 0.15) < 1e-9, "半分の磁力で半分だけ向く");
});

test("角の追従は最短側(極性なし)で目標へ寄り、差は [-π, π] に畳まれる", () => {
  assert.ok(Math.abs(angleDelta(0.1, 0.1 + Math.PI * 2 + 0.2) - 0.2) < 1e-9);
  const a = lerpAngle(0, 3.0, 1);           // 3.0 ≈ π − 0.14 → 極性なしでは −0.14 側が近い
  assert.ok(Math.abs(a - (3.0 - Math.PI)) < 1e-9);
  let cur = 0;
  for (let i = 0; i < 60; i += 1) cur = lerpAngle(cur, 1.0, 0.15);
  assert.ok(Math.abs(cur - 1.0) < 1e-3, "収束する");
});

test("波紋: 輪が広がって薄れ、時間外は 0", () => {
  assert.equal(rippleGlow(10, -1), 0);
  assert.equal(rippleGlow(10, RIPPLE_MS + 1), 0);
  const early = rippleGlow(0, 0);
  assert.ok(early > 0.9, "始まりは中心が明るい");
  const t = 300;
  const r = t * 0.55;
  assert.ok(rippleGlow(r, t) > rippleGlow(r + 80, t), "輪の上が明るい");
  assert.ok(rippleGlow(r, t) > rippleGlow(r, RIPPLE_MS - 10), "時間で薄れる");
  assert.ok(BASE_ALPHA < 0.1 && PEAK_ALPHA <= 0.6, "眠りは薄く、芯でも本文を隠さない");
});

test("attach: canvas を 1 枚 prepend し、二重には付けない。getContext が無ければ何もしない", () => {
  const makeHost = (ctx) => {
    const children = [];
    const listeners = {};
    return {
      children, listeners,
      ownerDocument: { createElement: () => ({
        className: "", style: {}, width: 0, height: 0,
        setAttribute() {}, getContext: () => ctx, remove() { children.pop(); },
      }) },
      prepend(node) { children.unshift(node); },
      getBoundingClientRect: () => ({ left: 0, top: 0, width: 200, height: 120 }),
      addEventListener: (ev, fn) => { listeners[ev] = fn; },
    };
  };
  const ctx = {
    calls: 0,
    setTransform() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() { this.calls += 1; }, fillRect() {},
    createRadialGradient: () => ({ addColorStop() {} }),
  };
  const win = { devicePixelRatio: 2, matchMedia: () => ({ matches: false }), requestAnimationFrame: () => 1, cancelAnimationFrame() {} };
  globalThis.performance ??= { now: () => 0 };
  const host = makeHost(ctx);
  const handle = attachCardField(host, { win });
  assert.ok(handle, "付いた");
  assert.equal(host.children.length, 1, "canvas 1 枚");
  assert.equal(host.children[0].className, "card-field");
  assert.ok(ctx.calls > 30, "初回に静止画を描く");
  assert.ok(host.listeners.pointermove && host.listeners.pointerdown, "ポインタはカード本体が受ける");
  assert.ok(host.listeners.pointerleave && host.listeners.pointercancel && host.listeners.pointerup, "離す経路(leave / cancel / 指の up)がある");
  assert.equal(attachCardField(host, { win }), null, "二重には付けない");
  const dead = makeHost(null);
  assert.equal(attachCardField(dead, { win }), null);
  assert.equal(dead.children.length, 0, "描けなければ canvas を残さない");
  // reduced-motion: 反応の listener を付けない(静止画だけ)
  const still = makeHost({ ...ctx, calls: 0 });
  attachCardField(still, { win: { ...win, matchMedia: () => ({ matches: true }) } });
  assert.equal(Object.keys(still.listeners).length, 0);
  // DOM から消えたカードの鉄粉は、次の attachCardFields で片付ける(canvas と bitmap を手放す)
  host.isConnected = false;
  const container = { querySelectorAll: () => [] };
  attachCardFields(container, ".state-card", { win });
  assert.equal(host.children.length, 0, "消えたカードの canvas は remove される");
  const fresh = makeHost({ ...ctx, calls: 0 });
  fresh.isConnected = true;
  assert.ok(attachCardField(fresh, { win }), "生きているカードには付く");
  attachCardFields({ querySelectorAll: () => [] }, ".state-card", { win });
  assert.equal(fresh.children.length, 1, "生きているカードの canvas は残る");
  assert.ok(!/new\s+(win\.)?ResizeObserver\(/.test(readFileSync(new URL("../cardfield.js", import.meta.url), "utf8")), "ResizeObserver は使わない(消えたカードを掴む)");
});

test("配線: app.js は state stack を描いた直後に鉄粉を付け、CSS は本文の下に敷く。鉄粉は表示専用", () => {
  const APP = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  const CSS = readFileSync(new URL("../styles.css", import.meta.url), "utf8");
  // R67: 起動の見張り(boot_guard.js)が safe と判定した起動では鉄粉を敷かない(`if (boot.particles)`)。
  assert.match(APP, /stateStack\.innerHTML = cards\.join\(""\);\n\s*mountStandbyEye\(\);\n\s*if \(boot\.particles\) attachCardFields\(stateStack\);/);
  assert.match(CSS, /\.state-card:not\(\.empty\) \{ isolation: isolate; \}/);
  assert.match(CSS, /\.state-card > canvas\.card-field \{[\s\S]*?z-index: -1;[\s\S]*?pointer-events: none;/);
  const source = readFileSync(new URL("../cardfield.js", import.meta.url), "utf8");
  for (const forbidden of ["fetch(", "sendData", "localStorage", "/api/", "WebSocket", "currentView", "order", "scenario"]) {
    assert.ok(!source.includes(forbidden), `${forbidden} を含まない`);
  }
  assert.equal(typeof attachCardFields, "function");
});
