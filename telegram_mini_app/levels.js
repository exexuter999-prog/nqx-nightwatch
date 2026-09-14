/**
 * レベル一覧フィード(R31)。
 *
 * 本体アプリとは **別エントリ**。three.js も app.js も読まないので、
 * 発注画面の起動を一切重くしない。ここは「今どの水準を意識しているか」を
 * ごちゃごちゃでいいから全部出す専用の面。
 *
 * 表示するもの:
 *   価格レベル(VP / セッション高安 / 前日 / VWAP / ピボット …)
 *   ICT ゾーン(FVG / OTE / レンジ / オーダーブロック / IFVG)
 *   流動性プール(BSL / SSL)、CRT レンジ
 *   シナリオ(ENTRY / SL / TP1 / runner)
 *   Quarterly Theory の位相と true open
 *
 * 本体チャートと違い**間引かない**。近い水準は積み上げて全部ラベルを出す。
 */

import { consumeLaunchParams, createStateClient } from "./state_client.js";

const NS = "http://www.w3.org/2000/svg";
// 右レーンはラベル文字列("29,533.75  New York High")が収まる幅が要る。
// 実データのラベルは最長 20 文字程度で、9px 等幅なら約 170px。
const PAD = { top: 20, right: 178, bottom: 24, left: 8 };
const ROW = 13;              // ラベル行の高さ
const NAME_MAX = 18;         // 右レーンからはみ出す名前は詰める
const POLL_MS = 30_000;

const el = (id) => document.getElementById(id);
const svgEl = (tag, attrs = {}) => {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
};
const num = (v) => {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};
const money = (v) => Number(v).toLocaleString("en-US", {
  minimumFractionDigits: 2, maximumFractionDigits: 2,
});

/**
 * 水準を種類で色分けする。本体の levelStyle と役割は同じだが、こちらは
 * **間引かない**ので種類ごとの識別が本体より重要になる。
 */
export function classify(label = "", source = "") {
  const n = `${label} ${source}`.toUpperCase();
  if (/VWAP/.test(n)) return { kind: "VWAP", color: "#e8c15a", rank: 2 };
  if (/\bP:|PREV|前日|PDH|PDL/.test(n)) return { kind: "PREV", color: "#ab47bc", rank: 1 };
  if (/VAH|VAL|POC/.test(n)) return { kind: "VP", color: "#2962ff", rank: 1 };
  if (/HIGH|LOW|高|安/.test(n)) return { kind: "SESSION", color: "#c9c5ba", rank: 1 };
  if (/OPEN/.test(n)) return { kind: "OPEN", color: "#f59e0b", rank: 2 };
  if (/PP|CPR|PIVOT/.test(n)) return { kind: "PIVOT", color: "#8f8b81", rank: 3 };
  if (/FIB|TRAIL|SWINGARM|CT /.test(n)) return { kind: "FIB", color: "#63b3ed", rank: 3 };
  return { kind: "OTHER", color: "#8f8b81", rank: 4 };
}

/**
 * 全部の水準を1本のリストに畳む。**捨てない** —— 近すぎるものも残し、
 * 描画側が縦に押し広げる。
 */
export function collectLevels(view) {
  const market = view?.market || {};
  const scenario = view?.scenario || null;
  const out = [];
  const push = (price, label, group, color, extra = {}) => {
    const p = num(price);
    if (p === null) return;
    out.push({ price: p, label, group, color, ...extra });
  };

  for (const lv of market.levels || []) {
    const c = classify(lv?.label, lv?.source);
    push(lv?.price, String(lv?.label ?? ""), c.kind, c.color, { rank: c.rank });
  }
  if (market.vwap != null) push(market.vwap, "VWAP", "VWAP", "#e8c15a", { rank: 2 });

  const models = view?.market?.strategyEvidence?.models || {};
  const ict = models.ict || {};
  const anchor = ict.rangeAnchor || {};
  if (anchor.valid) {
    push(anchor.high, "Range High", "RANGE", "#2962ff", { rank: 1 });
    push(anchor.low, "Range Low", "RANGE", "#2962ff", { rank: 1 });
    push(anchor.eq, "Range EQ", "RANGE", "#2962ff", { rank: 2 });
  }
  for (const side of ["BUY", "SELL"]) {
    const dol = ict.dol?.[side];
    if (dol) push(dol.target, `DOL ${side} (${dol.run ?? ""})`, "DOL", "#ff5566", { rank: 1 });
  }
  for (const pool of models.liquidity?.pools || []) {
    push(pool?.price, `${pool?.side ?? "POOL"} ${pool?.label ?? ""}`.trim(),
      "LIQUIDITY", pool?.side === "BSL" ? "#ef5350" : "#26a69a", { rank: 2 });
  }
  const crt = models.crt || {};
  push(crt.rangeHigh, "CRT High", "CRT", "#ab47bc", { rank: 2 });
  push(crt.rangeLow, "CRT Low", "CRT", "#ab47bc", { rank: 2 });

  // Gann は **助言専用**。他の水準と見分けが付くよう色と表記を分ける。
  const gann = models.gann || {};
  if (gann.status === "COMPUTED") {
    for (const row of gann.angles || []) {
      push(row?.price, row?.label, "GANN", "#7f8fa6", { rank: row?.primary ? 3 : 4 });
    }
    for (const row of gann.circles || []) {
      push(row?.price, row?.label, "GANN", "#5f6b7a", { rank: 4 });
    }
  }

  if (scenario) {
    push(scenario.entry, "ENTRY", "SCENARIO", "#ff3b4f", { rank: 0, emphasis: true });
    push(scenario.stop, "SL", "SCENARIO", "#ef5350", { rank: 0, emphasis: true });
    const targets = scenario.targets || [];
    if (targets[0] != null) push(targets[0], "TP1", "SCENARIO", "#26a69a", { rank: 0, emphasis: true });
    if (targets[1] != null) push(targets[1], "RUNNER", "SCENARIO", "#26a69a", { rank: 0, emphasis: true });
  }
  if (market.price != null) push(market.price, "LAST", "PRICE", "#ffffff", { rank: 0, emphasis: true });

  out.sort((a, b) => b.price - a.price);
  return out;
}

/** ゾーン(帯)。線ではなく面で出すもの。 */
export function collectZones(view) {
  const models = view?.market?.strategyEvidence?.models || {};
  const ict = models.ict || {};
  const zones = [];
  const add = (lo, hi, label, color, opacity) => {
    const a = num(lo), b = num(hi);
    if (a === null || b === null) return;
    zones.push({ lo: Math.min(a, b), hi: Math.max(a, b), label, color, opacity });
  };
  for (const side of ["BULL", "BEAR"]) {
    for (const gap of ict.fvg?.[side] || []) {
      add(gap?.lo, gap?.hi, `FVG ${side}${gap?.eligible ? " ✓" : ""}`,
        side === "BULL" ? "#26a69a" : "#ef5350", gap?.eligible ? 0.2 : 0.08);
    }
  }
  const anchor = ict.rangeAnchor || {};
  if (Array.isArray(anchor.oteBuy)) add(anchor.oteBuy[0], anchor.oteBuy[1], "OTE BUY", "#26a69a", 0.14);
  if (Array.isArray(anchor.oteSell)) add(anchor.oteSell[0], anchor.oteSell[1], "OTE SELL", "#ef5350", 0.14);
  for (const z of models.blocks?.zones || []) {
    add(z?.lo, z?.hi, `${z?.kind ?? "OB"}${z?.active ? " ✓" : ""}`, "#2962ff", z?.active ? 0.16 : 0.06);
  }
  for (const z of models.ifvg?.zones || []) {
    add(z?.lo, z?.hi, `IFVG ${z?.direction ?? ""}`.trim(),
      z?.direction === "SELL" ? "#ef5350" : "#26a69a", z?.active ? 0.18 : 0.07);
  }
  return zones;
}

/**
 * 縦に重なるラベルを押し広げる。捨てずに全部残す。
 *
 * 上から順に「前の行 + gap」以上へ押し下げるだけ。下端補正は入れない ——
 * 一度入れていたが、全体を上へ寄せた後の `Math.max(top, y)` が上端の行を
 * 再び潰してしまい、実際に最上部 3 行が重なった。
 *
 * 下端をはみ出さないのは呼び出し側の責任: キャンバス高さを
 * `件数 * gap + 余白` で確保しておけば、この前向き 1 パスで必ず収まる。
 */
export function spread(items, top, bottom, gap = ROW) {
  const rows = items.map((it) => ({ ...it, y: Math.max(top, it.y0) }));
  rows.sort((a, b) => a.y - b.y);
  for (let i = 1; i < rows.length; i += 1) {
    if (rows[i].y - rows[i - 1].y < gap) rows[i].y = rows[i - 1].y + gap;
  }
  return rows;
}

function render(view) {
  const host = el("levels");
  const status = el("status");
  const meta = el("meta");
  if (!host) return;
  host.textContent = "";

  const levels = collectLevels(view);
  const zones = collectZones(view);
  if (!levels.length) {
    status.textContent = "NO FEED";
    return;
  }
  const market = view?.market || {};
  const prices = levels.map((l) => l.price);
  const zoneEdges = zones.flatMap((z) => [z.lo, z.hi]);
  const all = prices.concat(zoneEdges);
  const min = Math.min(...all), max = Math.max(...all);
  const span = Math.max(1, max - min);

  const width = host.clientWidth || 360;
  // 価格を写す高さ。ラベルはここから押し広げられるので、最終的な高さは
  // 押し広げた**後**に決める(先に決めると最下行がはみ出して切れる)。
  const plotHeight = Math.max(300, levels.length * ROW);
  const plotBottom = PAD.top + plotHeight;
  const y = (p) => plotBottom - ((p - min) / span) * plotHeight;
  const plotRight = width - PAD.right;

  const laid = spread(levels.map((l) => ({ ...l, y0: y(l.price) })), PAD.top, plotBottom);
  const lastY = laid.length ? laid[laid.length - 1].y : plotBottom;
  const height = Math.max(plotBottom, lastY) + PAD.bottom;

  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width, height,
    role: "img", "aria-label": "全レベル一覧" });

  // ゾーンを先に敷く
  for (const z of zones) {
    const top = y(z.hi), bot = y(z.lo);
    svg.append(svgEl("rect", { x: PAD.left, y: Math.min(top, bot), width: plotRight - PAD.left,
      height: Math.max(1, Math.abs(bot - top)), fill: z.color, opacity: z.opacity }));
    const t = svgEl("text", { x: PAD.left + 3, y: Math.min(top, bot) + 9,
      fill: z.color, "font-size": 8, "font-family": "var(--mono)", opacity: 0.85 });
    t.textContent = z.label;
    svg.append(t);
  }

  // 線 + 右側のラベル。間引かない。
  for (const item of laid) {
    const py = y(item.price);
    svg.append(svgEl("line", { x1: PAD.left, x2: plotRight, y1: py, y2: py,
      stroke: item.color, "stroke-width": item.emphasis ? 1.4 : 0.7,
      "stroke-dasharray": item.emphasis ? "" : "3 3",
      opacity: item.emphasis ? 0.95 : 0.5 }));
    // 引き出し線(押し広げたぶんのズレを見せる)
    if (Math.abs(item.y - py) > 1) {
      svg.append(svgEl("line", { x1: plotRight, x2: plotRight + 6, y1: py, y2: item.y,
        stroke: item.color, "stroke-width": 0.5, opacity: 0.4 }));
    }
    const label = svgEl("text", { x: plotRight + 8, y: item.y + 3, fill: item.color,
      "font-size": 9, "font-family": "var(--mono)",
      "font-weight": item.emphasis ? 600 : 400 });
    // 名前が長いと右端で切れる。切ったことが分かるよう末尾を … にする。
    const name = item.label.length > NAME_MAX
      ? `${item.label.slice(0, NAME_MAX - 1)}…` : item.label;
    label.textContent = `${money(item.price)}  ${name}`;
    svg.append(label);
  }
  host.append(svg);

  // 見出し
  const qt = view?.market?.strategyEvidence?.models?.quarterly || {};
  const bits = [
    market.sourceSymbol || "MNQ",
    market.price != null ? money(market.price) : "—",
    qt.session ? `QT ${qt.session}/${qt.stage}` : null,
    `${levels.length} levels · ${zones.length} zones`,
  ].filter(Boolean);
  meta.textContent = bits.join("  ·  ");
  status.textContent = market.stale ? "STALE" : "LIVE";
  status.className = market.stale ? "is-stale" : "";
}

/** ?demo=1 用の見本。実データが無い環境でも面の作りを確認できるようにする。 */
export const DEMO_VIEW = {
  market: {
    price: 29370, vwap: 29372.98, sourceSymbol: "CME_MINI:MNQ1! (demo)",
    levels: [
      { label: "New York High", price: 29533.75 }, { label: "C: VAH", price: 29486.65 },
      { label: "P: VAH", price: 29425.63 }, { label: "C: POC", price: 29402.78 },
      { label: "P: POC", price: 29367.61 }, { label: "6pm open", price: 29331 },
      { label: "C: VAL", price: 29318.92 }, { label: "P: VAL", price: 29309.6 },
      { label: "PP-S1", price: 29280 }, { label: "New York Low", price: 29220.25 },
    ],
    strategyEvidence: {
      models: {
        ict: {
          rangeAnchor: { valid: true, high: 29533.75, low: 29220.25, eq: 29377,
            oteBuy: [29317, 29414], oteSell: [29414, 29511] },
          dol: { SELL: { target: 29220.25, run: "LRLR" } },
          fvg: { BULL: [{ lo: 29300, hi: 29322, eligible: true }],
                 BEAR: [{ lo: 29455, hi: 29470, eligible: false }] },
        },
        liquidity: { pools: [{ side: "BSL", price: 29540, label: "eq highs" },
                             { side: "SSL", price: 29210, label: "eq lows" }] },
        crt: { rangeHigh: 29500, rangeLow: 29250 },
        blocks: { zones: [{ lo: 29350, hi: 29366, kind: "OB", active: true }] },
        quarterly: { session: "NewYork", stage: "D" },
        gann: { status: "COMPUTED", advisory: true,
          angles: [{ label: "Gann 1x1", price: 29390, primary: true },
                   { label: "Gann 2x1", price: 29455, primary: false },
                   { label: "Gann 1x2", price: 29357, primary: false }],
          circles: [{ label: "Gann circle 1 上", price: 29457, ring: 1 },
                    { label: "Gann circle 1 下", price: 29297, ring: 1 }] },
      },
    },
  },
  scenario: { entry: 29400, stop: 29430, targets: [29350, 29250] },
};

function boot() {
  const launch = consumeLaunchParams();
  if (new URLSearchParams(location.search).has("demo")) {
    render(DEMO_VIEW);
    return;
  }
  if (launch.apiBase) {
    createStateClient({
      onView: render,
      onError: () => { el("status").textContent = "OFFLINE"; },
    });
    return;
  }
  // デモ/ブラウザ: 同梱の market.json を読む
  const load = () => fetch("./market.json", { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : null))
    .then((m) => { if (m) render({ market: m, scenario: null }); })
    .catch(() => { el("status").textContent = "OFFLINE"; });
  load();
  setInterval(load, POLL_MS);
}

if (typeof document !== "undefined" && document.getElementById("levels")) boot();
