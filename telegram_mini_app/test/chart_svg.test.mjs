import assert from "node:assert/strict";
import { test } from "node:test";
import { initTapeChart } from "../chart.js";

// A deliberately tiny DOM harness keeps the SVG assertion hermetic.  It uses
// the same element/attribute tree the browser renderer receives, rather than
// only testing the chart's pure normalization helpers.
class Element {
  constructor(tag) {
    this.tagName = tag;
    this.attributes = {};
    this.children = [];
    this.parentNode = null;
    this.style = {};
    this.hidden = false;
    this.textContent = "";
    this.clientWidth = 0;
    this.clientHeight = 0;
    const classes = new Set();
    this.classList = {
      add: (...items) => items.forEach((item) => classes.add(item)),
      remove: (...items) => items.forEach((item) => classes.delete(item)),
      toggle: (item, force) => {
        const enabled = force === undefined ? !classes.has(item) : Boolean(force);
        if (enabled) classes.add(item); else classes.delete(item);
        return enabled;
      },
      contains: (item) => classes.has(item),
    };
    Object.defineProperty(this, "className", {
      get: () => [...classes].join(" "),
      set: (value) => { classes.clear(); String(value || "").split(/\s+/).filter(Boolean).forEach((item) => classes.add(item)); },
    });
  }
  get firstChild() { return this.children[0] || null; }
  append(...nodes) { nodes.forEach((node) => this.appendChild(node)); }
  appendChild(node) { node.parentNode = this; this.children.push(node); return node; }
  removeChild(node) { this.children.splice(this.children.indexOf(node), 1); node.parentNode = null; return node; }
  remove() { this.parentNode?.removeChild(this); }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "class") this.className = value;
  }
  getAttribute(name) { return this.attributes[name] ?? null; }
}

function descendants(node) {
  return node.children.flatMap((child) => [child, ...descendants(child)]);
}

test("actual SVG keeps ICT overlays clipped and candles legible", () => {
  const oldDocument = globalThis.document;
  const oldWindow = globalThis.window;
  const events = new Map();
  globalThis.document = {
    visibilityState: "hidden",
    createElement: (tag) => new Element(tag),
    createElementNS: (_ns, tag) => new Element(tag),
    addEventListener: (kind, fn) => events.set(kind, fn),
    removeEventListener: (kind) => events.delete(kind),
  };
  globalThis.window = { setInterval: () => 1, clearInterval() {}, addEventListener() {}, removeEventListener() {} };
  try {
    const mount = new Element("div");
    mount.clientWidth = 420;
    mount.clientHeight = 240;
    const chart = initTapeChart(mount, { autoPoll: false });
    const now = Math.floor(Date.now() / 1000);
    const bars = Array.from({ length: 8 }, (_, index) => {
      const o = 20000 + index * 0.25;
      return { t: now - (9 - index) * 180, o, h: o + 1, l: o - 1, c: o + 0.5 };
    });
    chart.update({
      at: new Date(now * 1000).toISOString(), observedAt: new Date(now * 1000).toISOString(),
      resolution: "3", barResolution: "3", price: bars.at(-1).c, bars,
      strategyEvidence: { version: "R14-STRATEGY-EVIDENCE-1", models: {
        _matrix: { activeModels: ["ifvg", "blocks"] },
        ifvg: { zones: [{ lo: 19999, hi: 20000, active: true }, { lo: 20001, hi: 20002, active: true }] },
        blocks: { zones: [{ lo: 19998, hi: 19999, kind: "OB", active: true }, { lo: 20002, hi: 20003, kind: "BRK", active: true }] },
      } },
    });
    chart.arm({ side: "BUY", entry: 20001, stop: 19999, target: 20003 });

    const svg = mount.children.find((node) => node.tagName === "svg");
    assert.ok(svg, "renderer created an SVG");
    const nodes = descendants(svg);
    const overlays = nodes.filter((node) => /strategy-(ifvg|block)/.test(node.className));
    assert.equal(overlays.length, 4, "canonical IFVG/block overlays render once each");
    for (const overlay of overlays) {
      const y = Number(overlay.getAttribute("y"));
      const height = Number(overlay.getAttribute("height"));
      assert.ok(y >= 0 && y <= 240 && height >= 1 && y + height <= 240,
        `overlay remains clipped to chart bounds: y=${y} height=${height}`);
    }
    const candleBodies = nodes.filter((node) => node.className === "chart-body");
    assert.ok(candleBodies.length >= 6, "confirmed candles draw as SVG bodies");
    assert.ok(candleBodies.every((node) => Number(node.getAttribute("height")) >= 1),
      "candle bodies retain at least one visible pixel");
    chart.destroy();
  } finally {
    globalThis.document = oldDocument;
    globalThis.window = oldWindow;
  }
});
