// タイピングアニメーション(typewriter.js)。
//
// 固定するのは「情報を壊さない」こと:
//   - 打ち終わった文字列は元と完全に一致する
//   - 同じ文字列の再描画では打ち直さない(3秒ごとの render でチラつかない)
//   - 打っている途中に新しい文字列が来たら古い打鍵を捨てる
//   - DOM 構造と属性は触らない(テキストノードだけ)
//   - 価格など data-type の無い要素は対象外
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// ---- 最小 DOM スタブ。typewriter が使う API だけを持つ。
class TextNode {
  constructor(value) { this.nodeType = 3; this.nodeValue = value; }
}
class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.classes = new Set(); this.dataset = {}; this.parent = null;
    this.classList = {
      add: (...cs) => cs.forEach((c) => this.classes.add(c)),
      remove: (...cs) => cs.forEach((c) => this.classes.delete(c)),
      contains: (c) => this.classes.has(c),
    };
  }
  /** ".a.b" のクラス連鎖だけを解く最小の closest(assignCarets の検査用) */
  matches(sel) { return sel.split(".").filter(Boolean).every((c) => this.classes.has(c)); }
  closest(sel) { let n = this; while (n) { if (n.matches(sel)) return n; n = n.parent; } return null; }
  append(...nodes) { nodes.forEach((n) => { if (n instanceof Element) n.parent = this; }); this.children.push(...nodes); }
  get textContent() { return this.children.map((c) => c.nodeValue ?? c.textContent).join(""); }
  set textContent(v) { this.children = [new TextNode(String(v))]; }
  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => { if (n instanceof Element) { if (sel === "[data-type]" && "data-type" in n.attrs) out.push(n); n.children.forEach(walk); } };
    this.children.forEach(walk);
    return out;
  }
}
const texts = (root) => {
  const out = [];
  const walk = (n) => { if (n instanceof TextNode) { if (n.nodeValue.trim()) out.push(n); } else n.children.forEach(walk); };
  walk(root); return out;
};

let now = 0;
const frames = [];
const timers = [];
globalThis.window = {
  matchMedia: () => ({ matches: false }),
  requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
  cancelAnimationFrame: (id) => { frames[id - 1] = null; },
  setTimeout: (fn, ms) => { timers.push({ fn, at: now + ms }); return timers.length; },
};
globalThis.performance = { now: () => now };
globalThis.document = {
  createTreeWalker(root) {
    const list = texts(root); let i = -1;
    return { nextNode: () => list[++i] ?? null };
  },
};
globalThis.NodeFilter = { SHOW_TEXT: 4, FILTER_ACCEPT: 1, FILTER_SKIP: 3 };

const { typeInto, typeAll, typeText, replayAll, applyCaretVariant, assignCarets, CARET_VARIANTS, CARET_MODES, AFTERGLOW_MS } = await import("../typewriter.js");

function runFrames(ms) {
  // 時間を進めながら、待っているフレームと期限の来たタイマーを全部消化する
  now += ms;
  let guard = 0;
  while (frames.some(Boolean) && guard++ < 500) {
    const batch = frames.splice(0).filter(Boolean);
    batch.forEach((fn) => fn(now));
  }
  timers.splice(0).forEach((tm) => { if (tm.at <= now) tm.fn(); else timers.push(tm); });
}

test("打ち終わった文字列は元と完全に一致し、途中は前方一致", () => {
  const el = new Element("p"); el.attrs["data-type"] = "";
  el.append(new TextNode("SHORT — READY"));
  assert.equal(typeInto(el), true);
  assert.equal(el.textContent, "", "開始時は空");
  assert.ok(el.classes.has("is-typing"));
  now += 34 * 5; frames.splice(0).forEach((fn) => fn(now));
  assert.equal(el.textContent, "SHORT", "5文字ぶん進む");
  runFrames(600);
  assert.equal(el.textContent, "SHORT — READY");
  assert.ok(el.classes.has("is-typed"), "打ち終わり直後はカーソルが余韻(目)で残る");
  runFrames(1300);
  assert.ok(el.classes.has("is-typed"), "目の振り付け(2.4 秒)の間は残る");
  runFrames(AFTERGLOW_MS);
  assert.ok(!el.classes.has("is-typing") && !el.classes.has("is-typed"), "余韻のあとカーソルを消す");
});

test("最後のカーソルは目になる: CSS の振り付けが余韻の中に収まり、DOM は増えない", () => {
  const CSS = readFileSync(new URL("../styles.css", import.meta.url), "utf8");
  assert.match(CSS, /\[data-type\]\.is-typed::after,[\s\S]*?animation: nw-caret-eye ([\d.]+)s/, "打ち終わりの ::after が目の振り付けを持つ");
  const seconds = Number(CSS.match(/animation: nw-caret-eye ([\d.]+)s/)[1]);
  assert.ok(seconds * 1000 <= AFTERGLOW_MS, "振り付けは AFTERGLOW_MS の中で終わる");
  assert.match(CSS, /@keyframes nw-caret-eye \{[\s\S]*?clip-path: ellipse\(50% 2% at 50% 50%\)/, "まばたきがある");
  assert.match(CSS, /@keyframes nw-caret-eye \{[\s\S]*?background-position: -\.36em 0/, "打った文字を振り返る");
  const source = readFileSync(new URL("../typewriter.js", import.meta.url), "utf8");
  assert.ok(!/createElement|append\(|insertBefore|innerHTML/.test(source), "typewriter は DOM 構造を増やさない(目は ::after)");
});

test("同じ文字列の再描画では打ち直さない", () => {
  const el = new Element("p"); el.attrs["data-type"] = "";
  el.append(new TextNode("ORDER SENT"));
  typeInto(el); runFrames(2000);
  assert.equal(typeInto(el), false, "同一内容は no-op");
  assert.equal(el.textContent, "ORDER SENT", "文字が消えない");
});

test("打っている途中に新しい文字列が来たら古い打鍵を捨てる", () => {
  const el = new Element("p"); el.attrs["data-type"] = "";
  el.append(new TextNode("FIRST MESSAGE"));
  typeInto(el);
  now += 34 * 3; frames.splice(0).forEach((fn) => fn(now));
  el.textContent = "SECOND";
  assert.equal(typeInto(el), true);
  runFrames(2000);
  assert.equal(el.textContent, "SECOND", "古い文字が混ざらない");
});

test("余韻の最中に同じ要素へ続けて打つと、余韻の印(is-typed)は打ち始めで外れ、打ち終わりで付き直る", () => {
  // トースト・AUTO の帯・設定の状態語は同じ要素へ続けて打つ。is-typed が残ったままだと
  // 打っている間に型の振り付けが出て、打ち終わりの付け直しでは CSS のアニメーションが再始動しない。
  const el = new Element("p"); el.attrs["data-type"] = "";
  el.append(new TextNode("FIRST"));
  typeInto(el);
  runFrames(400);
  assert.ok(el.classList.contains("is-typed"), "1 回目が打ち終わって余韻に入る");
  assert.equal(typeText(el, "SECOND"), true, "余韻の途中(< AFTERGLOW_MS)に打ち直す");
  assert.ok(el.classList.contains("is-typing"), "打っている");
  assert.ok(!el.classList.contains("is-typed"), "打っている間は余韻の印が無い");
  runFrames(400);
  assert.equal(el.textContent, "SECOND");
  assert.ok(el.classList.contains("is-typed"), "打ち終わりで付き直る");
  runFrames(AFTERGLOW_MS);
  assert.ok(!el.classList.contains("is-typed") && !el.classList.contains("is-typing"), "余韻が終われば両方消える");
});

test("複数のテキストノードを順に打ち、構造は変えない", () => {
  const el = new Element("p"); el.attrs["data-type"] = "";
  const b = new Element("b"); b.append(new TextNode("APEX-01"));
  el.append(new TextNode("managing "), b, new TextNode(" 30"));
  typeInto(el); runFrames(2000);
  assert.equal(el.children.length, 3, "子ノードの数が変わらない");
  assert.equal(el.children[1], b, "要素ノードは同一のまま");
  assert.equal(el.textContent, "managing APEX-01 30");
});

test("data-type の無い要素は typeAll の対象外", () => {
  const root = new Element("section");
  const price = new Element("p"); price.append(new TextNode("30,126.00"));
  const note = new Element("p"); note.attrs["data-type"] = ""; note.append(new TextNode("READY"));
  root.append(price, note);
  assert.equal(typeAll(root), 1);
  assert.equal(price.textContent, "30,126.00", "価格はそのまま");
  runFrames(2000);
  assert.equal(note.textContent, "READY");
});

test("typeText は差し替えて打つ", () => {
  const toast = new Element("div");
  assert.equal(typeText(toast, "DEMO ON"), true);
  runFrames(2000);
  assert.equal(toast.textContent, "DEMO ON");
  assert.equal(typeText(toast, "DEMO ON"), false, "同じ内容は打ち直さない");
});

test("長文でも上限時間内に打ち終える", () => {
  const el = new Element("p"); el.attrs["data-type"] = "";
  el.append(new TextNode("x".repeat(400)));
  typeInto(el);
  runFrames(1650);
  assert.equal(el.textContent.length, 400, "1.65 秒で 400 文字を打ち終える");
});

test("対象は情報テキストだけで、価格やボタンには付けていない", () => {
  const APP = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  assert.match(APP, /<span class="state" data-type>/, "状態行は対象");
  assert.match(APP, /<p class="card-note" data-type>/, "注記は対象");
  assert.match(APP, /<p class="card-warning" data-type>/, "警告は対象");
  assert.ok(!/class="val[^"]*" data-type/.test(APP), "価格の値は対象外");
  assert.ok(!/slide-label" data-type|hold-btn" data-type|ghost-btn" data-type/.test(APP), "ボタンは対象外");
  assert.ok(!/marketStatus[^\n]*typeText/.test(APP), "30秒ごとに変わる市況ステータスは対象外");
});

test("カーソルの型: 7 つの型が「要素自身」と「html で固定」の両方の規則を持ち、どれも余韻の中で終わる。属性が無ければ従来の点滅", () => {
  const CSS = readFileSync(new URL("../styles.css", import.meta.url), "utf8");
  assert.deepEqual(CARET_VARIANTS, ["eye", "diamond", "ember", "glitch", "candle", "reticle", "classic"]);
  assert.deepEqual(CARET_MODES, ["auto", ...CARET_VARIANTS]);
  for (const v of CARET_VARIANTS) {
    assert.ok(CSS.includes(`[data-type][data-caret="${v}"].is-typed::after`), `${v}: 要素自身の規則`);
    assert.ok(CSS.includes(`html[data-caret="${v}"] [data-type].is-typed::after`), `${v}: 固定の規則`);
    if (v === "classic") continue;
    const m = CSS.match(new RegExp(`animation: nw-caret-${v} ([\\d.]+)s`));
    assert.ok(m, `${v} の振り付け`);
    assert.ok(Number(m[1]) * 1000 <= AFTERGLOW_MS, `${v} は AFTERGLOW_MS の中で終わる`);
    assert.ok(CSS.includes(`@keyframes nw-caret-${v} {`), `${v} の keyframes`);
  }
  // 各規則は基準の姿から書く(別の型の宣言を引き継がない)
  const own = CSS.indexOf('[data-type][data-caret="glitch"].is-typed::after');
  assert.ok(CSS.slice(own, own + 900).includes("clip-path: none;"), "glitch の規則も RESET を持つ");
  assert.ok(CSS.includes("#settingsState.is-typed::after { animation-duration: .85s; }"), "属性が無いときは従来の点滅");
});

test("状況で割り振る: 武装 = 照準、監視 = 目、建玉 = ローソク足、注文 = グリッチ。html の固定はより強い", () => {
  const stack = new Element("section");
  const armed = new Element("article"); ["state-card", "scenario", "armed"].forEach((c) => armed.classes.add(c));
  const armedText = new Element("span"); armedText.attrs["data-type"] = ""; armedText.append(new TextNode("SHORT — READY"));
  armed.append(armedText);
  const watch = new Element("article"); ["state-card", "scenario"].forEach((c) => watch.classes.add(c));
  const watchText = new Element("span"); watchText.attrs["data-type"] = ""; watchText.append(new TextNode("SHORT — WATCH"));
  watch.append(watchText);
  const pos = new Element("article"); ["state-card", "position"].forEach((c) => pos.classes.add(c));
  const posText = new Element("p"); posText.attrs["data-type"] = ""; posText.append(new TextNode("managing"));
  pos.append(posText);
  const order = new Element("article"); ["state-card", "order"].forEach((c) => order.classes.add(c));
  const orderText = new Element("p"); orderText.attrs["data-type"] = ""; orderText.append(new TextNode("ORDER SENT"));
  order.append(orderText);
  const loose = new Element("p"); loose.attrs["data-type"] = ""; loose.append(new TextNode("x"));
  stack.append(armed, watch, pos, order, loose);
  const rules = [[".state-card.scenario.armed", "reticle"], [".state-card.scenario", "eye"], [".state-card.position", "candle"], [".state-card.order", "glitch"]];
  assert.equal(assignCarets(stack, rules, "eye"), 5);
  assert.equal(armedText.dataset.caret, "reticle");
  assert.equal(watchText.dataset.caret, "eye", "armed でないシナリオは目");
  assert.equal(posText.dataset.caret, "candle");
  assert.equal(orderText.dataset.caret, "glitch");
  assert.equal(loose.dataset.caret, "eye", "当たらなければ fallback");
  assert.equal(assignCarets(null, rules), 0);
  const APP = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  assert.match(APP, /assignCarets\(stateStack, CARD_CARETS, "eye"\);\n\s*typeAll\(stateStack\);/, "カードは打つ前に割り振る");
  assert.match(APP, /\[".state-card.scenario.armed", "reticle"\]/);
  assert.match(APP, /\[".route-summary.tone-up", "diamond"\]/, "SYSTEM が全部 UP なら ◆");
});

test("applyCaretVariant: auto は html の属性を外し、型は付ける。未知は auto。replayAll は同じ文字を打ち直す", () => {
  const doc = { documentElement: { dataset: {} } };
  assert.equal(applyCaretVariant("diamond", doc), "diamond");
  assert.equal(doc.documentElement.dataset.caret, "diamond");
  assert.equal(applyCaretVariant("auto", doc), "auto");
  assert.equal("caret" in doc.documentElement.dataset, false, "auto では属性を消す(個別の割り振りが効く)");
  assert.equal(applyCaretVariant("nonsense", doc), "auto");
  assert.equal(applyCaretVariant(null, null), "auto", "document が無くても落ちない");
  const root = new Element("section");
  const note = new Element("p"); note.attrs["data-type"] = ""; note.append(new TextNode("READY"));
  root.append(note);
  typeAll(root); runFrames(4000);
  assert.equal(typeAll(root), 0, "同じ内容は打ち直さない");
  assert.equal(replayAll(root), 1, "replayAll は打ち直す");
  assert.equal(note.textContent, "", "打ち直しの開始時は空");
  runFrames(4000);
  assert.equal(note.textContent, "READY", "打ち直しても文字は元どおり");
});
