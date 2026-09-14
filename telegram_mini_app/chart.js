import { projectionCandles, renderIceCandles, iceBlocks, renderProjectionPath } from "./icecandles.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const POLL_MS = 30_000;

// TradingView 流儀の寸法。価格軸は右の専用レーン、時間軸は下の専用レーン。
// プロット領域はその内側だけ。値札は軸レーンに塗りつぶしタグで置く。
const PRICE_LANE = 58;      // 価格軸の幅(5桁 + 小数2桁が等幅で収まる)
const TIME_LANE = 18;       // 時間軸の高さ
// R56: 上端に OHLC の HUD(HTML)が乗るので、その分だけプロットを下げる。
const PLOT_PAD_TOP = 22;
const PLOT_PAD_BOTTOM = 6;
const LABEL_SIZE = 10;
const NAME_SIZE = 9;
// 値札の最小行間。タグの高さ(14px)+ 3px。
const LABEL_GAP = 17;
const TAG_H = 14;
// 出来高の帯。プロット下端からこの比率まで(ローソクの下に敷く)。
const VOLUME_RATIO = 0.16;

// VWAP は金(sigil・碑文と同じ血統)。acid は「武装」の色なので
// 常時出ているローソクや指標には使わない(§10 の色規律)。
const GOLD = "#e8c15a";

const FULLWIDTH = /[　-鿿＀-￯]/;
const charWidth = (ch) => (FULLWIDTH.test(ch) ? NAME_SIZE : NAME_SIZE * 0.6);

/** 指定幅に収まるまで詰める。切ったことが分かるように末尾を … にする。 */
function fitText(value, maxPx) {
  const source = String(value ?? "");
  let out = "";
  let used = 0;
  for (const ch of source) {
    const next = used + charWidth(ch);
    if (next > maxPx) return out ? `${out.slice(0, -1)}…` : "";
    out += ch;
    used = next;
  }
  return out;
}

/** 文字列の推定幅(px)。名前ピルの背景幅に使う。 */
function textWidth(value) {
  let used = 0;
  for (const ch of String(value ?? "")) used += charWidth(ch);
  return used;
}

/**
 * 値札用に名前を短くする。括弧書きと「/」以降は補足なので、
 * 機械的に末尾を削るより先に落とした方が意味が残る。
 */
function shortName(value) {
  return String(value ?? "")
    .replace(/[（(][^）)]*[）)]/g, "")
    .split(/\s*[/|]\s*/)[0]
    .replace(/\s+/g, " ")
    .trim();
}

const priceLabel = (value) =>
  Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/**
 * レベルの見た目と優先度を決める。
 *
 * 全部を同じ破線で描くと、どれが VWAP でどれが単なる参照値なのか読めない。
 * 種類ごとに線種と濃さを変え、間引くときの優先度もここで持たせる。
 */
function levelStyle(level) {
  const name = `${level?.label ?? level?.name ?? ""}`;
  const source = `${level?.source ?? ""}`;
  if (/VWAP/i.test(name) || /VWAP/i.test(source)) {
    return /上限|下限|band|hi\b|lo\b/i.test(name)
      ? { stroke: GOLD, dash: "1 5", opacity: 0.4, weight: 3 }
      : { stroke: GOLD, dash: "", opacity: 0.7, weight: 4 };
  }
  if (/high|low|高値|安値/i.test(name)) {
    return { stroke: "var(--bone)", dash: "5 4", opacity: 0.5, weight: 3 };
  }
  return { stroke: "var(--muted)", dash: "2 6", opacity: 0.34, weight: 1 };
}

/**
 * 値札を縦に押し広げて重なりを解消する(前方走査 → 後方走査の2パス)。
 *
 * 元の実装は各レベルの y にそのまま文字を置いていたため、価格が近いレベル同士が
 * 完全に重なって判読不能になっていた。
 */
function spreadLabels(items, height, gap = LABEL_GAP) {
  const sorted = [...items].sort((a, b) => a.y - b.y);
  let cursor = gap * 0.9;
  sorted.forEach((item) => {
    item.labelY = Math.max(item.y, cursor);
    cursor = item.labelY + gap;
  });
  cursor = height - gap * 0.9;
  for (let i = sorted.length - 1; i >= 0; i -= 1) {
    sorted[i].labelY = Math.min(sorted[i].labelY, cursor);
    cursor = sorted[i].labelY - gap;
  }
  return sorted;
}

// Number(null) と Number("") は 0 になる。素の Number.isFinite だと欠損値が
// 「価格 0」として通り、STALE でサーバーが price/vwap/cvd を null にした瞬間に
// 値札へ 0.00 が出る(実在しない価格を表示してしまう)。欠損は必ず弾く。
const finite = (value) =>
  value !== null && value !== undefined && value !== "" &&
  typeof value !== "boolean" && Number.isFinite(Number(value));
const number = (value) => (finite(value) ? Number(value) : null);
const text = (value, fallback = "") => (value == null ? fallback : String(value));
const priceFromScenario = (value) => {
  const parsed = Number(String(value ?? "").replace(/,/g, ""));
  return Number.isFinite(parsed) ? parsed : null;
};

function svgElement(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
  return node;
}

function parseAt(value) {
  if (!value) return NaN;
  const raw = String(value).trim();
  const normalized = raw.includes(" ") && !raw.includes("T")
    ? raw.replace(" ", "T")
    : raw;
  const parsed = Date.parse(normalized);
  return Number.isFinite(parsed) ? parsed : NaN;
}

export function normalizeMarketSnapshot(data, nowMs = Date.now()) {
  if (!data || !Array.isArray(data.bars)) return null;
  const bars = data.bars.map((bar) => {
    const o = number(bar?.o ?? bar?.open);
    const h = number(bar?.h ?? bar?.high);
    const l = number(bar?.l ?? bar?.low);
    const c = number(bar?.c ?? bar?.close);
    return {
      t: number(bar?.t ?? bar?.time),
      o,
      h,
      l,
      c,
      v: number(bar?.v ?? bar?.volume),
      valid: [o, h, l, c].every((value) => value !== null) &&
        h >= l && h >= Math.max(o, c) && l <= Math.min(o, c)
    };
  });
  if (!bars.some((bar) => bar.valid)) return null;
  const at = parseAt(data.observedAt || data.at);
  if (!Number.isFinite(at)) return null;
  const ageMs = Math.max(0, nowMs - at);
  // Canonical R14 evidence owns the matrix; legacy data remains read-only
  // fallback so an older frozen snapshot is still drawable.
  const evidence = data.strategyEvidence || null;
  const meta = evidence?.models?._matrix || {};
  // R14 transports one canonical strategyEvidence envelope.  Build the old
  // chart-friendly facade locally; do not ask the producer to duplicate it.
  const canonicalModels = evidence ? Object.fromEntries(Object.entries(evidence.models || {})
    .filter(([name]) => name !== "_matrix")) : null;
  // Overlay geometry is reconstructed locally from the one canonical model
  // transport.  It is deliberately not duplicated under market/snapshot.
  const strategyMatrix = evidence ? {
    version: meta.catalogVersion || evidence.version,
    alignment: meta.alignment || {},
    activeModels: Array.isArray(meta.activeModels) ? meta.activeModels : [],
    models: canonicalModels,
    overlays: {
      ifvg: canonicalModels.ifvg || {}, blocks: canonicalModels.blocks || {},
      quarterly: canonicalModels.quarterly || {}, sessions: canonicalModels.sessions || {},
      fibSd: canonicalModels.fibSd || {}, fibCrt: canonicalModels.fibCrt || {},
      fvg: canonicalModels.ict?.fvg || {}, rangeAnchor: canonicalModels.ict?.rangeAnchor || {},
      // crt と liquidity は描画側が読んでいるのに facade が作っておらず、
      // R14 経路では BSL/SSL プールと CRT レンジが一度も描かれていなかった。
      crt: canonicalModels.crt || {}, liquidity: canonicalModels.liquidity || {},
    },
  } : (data.strategyMatrix || data.evaluation?.strategyMatrix || null);
  return { ...data, at: data.observedAt || data.at, bars, ageMs, strategyMatrix };
}

function freshness(ageMs) {
  const mins = Math.max(0, Math.floor(ageMs / 60_000));
  return {
    mins,
    stale: ageMs >= 10 * 60_000,
    delayed: ageMs >= 2 * 60_000,
    label: ageMs >= 10 * 60_000 ? `STALE ${mins}m` : ageMs >= 2 * 60_000 ? `${mins}m ago` : ""
  };
}

/* ═══ 時間軸とローソク足(2026-08-16 刷新)═══════════════════════
   - 描くのは確定足のみ。形成中バーは confirmedBars で落とす
   - x は「インデックス」ではなく「時間スロット」。欠測期間は時間軸を
     削らず空白のまま残る(データが無い時間を詰めて見せない)
   ═══════════════════════════════════════════════════════════ */

/** バー間隔(秒)。barResolution("3" = 3分)を優先し、無ければ実測の中央値。 */
export function barStepSec(bars, barResolution) {
  const parsed = Number(barResolution);
  if (Number.isFinite(parsed) && parsed > 0) return parsed * 60;
  const diffs = [];
  for (let i = 1; i < bars.length; i += 1) {
    const a = bars[i - 1]?.t;
    const b = bars[i]?.t;
    if (finite(a) && finite(b) && b > a) diffs.push(b - a);
  }
  if (!diffs.length) return 180;
  diffs.sort((x, y) => x - y);
  return diffs[Math.floor(diffs.length / 2)];
}

/** 形成中の最終バーを落とす(確定足のみ描く)。closeTime > 観測時刻なら未確定。 */
export function confirmedBars(bars, stepSec, atMs) {
  if (!Array.isArray(bars) || !bars.length) return [];
  const atSec = Number.isFinite(atMs) ? atMs / 1000 : Infinity;
  const out = [...bars];
  const last = out[out.length - 1];
  if (last && finite(last.t) && last.t + stepSec > atSec + 2) out.pop();
  return out;
}

/** 各バーの時間スロット(欠測はスロット番号の飛びとして残る)。 */
export function slotIndices(bars, stepSec) {
  const first = bars.find((bar) => bar.valid && finite(bar.t));
  if (!first) return { slots: bars.map(() => null), count: 0 };
  const t0 = first.t;
  const slots = bars.map((bar) =>
    bar.valid && finite(bar.t) ? Math.max(0, Math.round((bar.t - t0) / stepSec)) : null);
  const count = slots.reduce((max, slot) => (slot !== null && slot > max ? slot : max), 0) + 1;
  return { slots, count };
}

/* ═══ セッション VWAP の遡及計算 ══════════════════════════════
   publish される vwap はスカラー1点なので、そのまま引くと平行線になる。
   バーに volume が載っているので、hlc3×v の累積から系列を復元する。

   アンカーは CME セッション開始(18:00 ET)。order.py と同じく EDT 固定の
   近似(22:00 UTC)。開始が窓内にあれば正確なセッション VWAP、窓外なら
   窓先頭からの近似になり、値札を「VWAP≈」にして区別する。
   volume が無いフィードでは復元せず、従来どおり公表値の水平線に落ちる
   (無いものを推測で埋めない)。 ═══════════════════════════════ */

export function sessionStartSec(lastSec) {
  const date = new Date(lastSec * 1000);
  let start = Date.UTC(
    date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate(), 22, 0, 0) / 1000;
  if (start > lastSec) start -= 86_400;
  return start;
}

export function computeVwapSeries(bars, stepSec) {
  const valid = bars
    .map((bar, index) => ({ ...bar, index }))
    .filter((bar) => bar.valid && finite(bar.t) && finite(bar.v) && bar.v > 0);
  if (valid.length < 2) return null;
  const boundary = sessionStartSec(valid[valid.length - 1].t);
  const anchored = valid[0].t <= boundary;
  const startIdx = anchored
    ? valid.findIndex((bar) => bar.t >= boundary)
    : 0;
  if (startIdx < 0) return null;
  let pv = 0;
  let vol = 0;
  const points = [];
  for (let i = startIdx; i < valid.length; i += 1) {
    const bar = valid[i];
    pv += ((bar.h + bar.l + bar.c) / 3) * bar.v;
    vol += bar.v;
    points.push({ index: bar.index, value: pv / vol });
  }
  if (points.length < 2) return null;
  return { points, anchored };
}

/* ═══ レベルの起点推定 ═══════════════════════════════════════
   levels には label と price しか無く、「どの足で生まれたか」は載っていない。
   窓内で最初にそのレベル価格に触れた足(高値側 or 安値側)を起点とみなし、
   その足の top / bottom からレイを描く。窓内に接触が無い(前日高値など
   窓より古いレベル)は従来どおり全幅の淡い線で描き、見た目で区別する。 */

export function levelAnchorIndex(price, bars, tolerance = 0.75) {
  for (let i = 0; i < bars.length; i += 1) {
    const bar = bars[i];
    if (!bar.valid) continue;
    if (price <= bar.h + tolerance && price >= bar.l - tolerance) {
      return {
        index: i,
        side: Math.abs(bar.h - price) <= Math.abs(price - bar.l) ? "top" : "bottom"
      };
    }
  }
  return null;
}

/**
 * 価格グリッドの刻み。TradingView と同じく 1/2/5/10/25/50… の丸い数で、
 * 高さに対して行間が 28px を下回らない最小の刻みを選ぶ。
 */
export function niceStep(range, plotHeight, minPx = 28) {
  if (!(range > 0) || !(plotHeight > 0)) return 1;
  const maxLines = Math.max(2, Math.floor(plotHeight / minPx));
  const rough = range / maxLines;
  const candidates = [0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000];
  for (const step of candidates) if (step >= rough) return step;
  return candidates[candidates.length - 1];
}

/** 時間軸ラベル。JST で HH:MM。 */
function timeLabel(sec) {
  if (!Number.isFinite(sec)) return "";
  return new Date(sec * 1000).toLocaleTimeString("ja-JP",
    { timeZone: "Asia/Tokyo", hour12: false, hour: "2-digit", minute: "2-digit" });
}

/**
 * R56: 時間軸の目盛りを「丸い時刻」に置く。
 *
 * 以前は N 本ごとのスロットに機械的にラベルを置いていたので、01:24 / 01:45 /
 * 02:06 のような半端な時刻が並び、読んでも位置が掴めなかった。JST の 15/30/60/
 * 120/240 分のうち、ラベル間隔が minPx 以上になる最小の刻みを選ぶ。
 * 戻り値は ``{ stepMin, marks: [epochSec...] }``(窓の先頭から末尾まで)。
 */
export function timeTicks(t0Sec, tEndSec, pxPerSec, minPx = 56) {
  if (!Number.isFinite(t0Sec) || !Number.isFinite(tEndSec) || !(pxPerSec > 0)) {
    return { stepMin: 60, marks: [] };
  }
  const stepMin = [15, 30, 60, 120, 240].find((m) => m * 60 * pxPerSec >= minPx) || 240;
  const stepSec = stepMin * 60;
  const offset = 9 * 3600;   // JST の丸い時刻に揃える
  const marks = [];
  let mark = Math.ceil((t0Sec + offset) / stepSec) * stepSec - offset;
  for (; mark <= tEndSec; mark += stepSec) marks.push(mark);
  return { stepMin, marks };
}

/**
 * セッションの節目(EDT 固定の近似、order.py と同じ扱い)。
 *   NY OPEN   09:30 ET = 13:30 UTC   TRUE OPEN 00:00 ET = 04:00 UTC   GLOBEX 18:00 ET = 22:00 UTC
 * 窓内に入るものだけ返す。
 */
export function sessionMarks(t0Sec, tEndSec) {
  if (!Number.isFinite(t0Sec) || !Number.isFinite(tEndSec)) return [];
  const anchors = [
    { label: "NY OPEN", utcSec: 13.5 * 3600 },
    { label: "TRUE OPEN", utcSec: 4 * 3600 },
    { label: "GLOBEX", utcSec: 22 * 3600 },
  ];
  const dayStart = Math.floor(t0Sec / 86_400) * 86_400;
  const out = [];
  for (let day = -1; day <= 2; day += 1) {
    anchors.forEach((anchor) => {
      const at = dayStart + day * 86_400 + anchor.utcSec;
      if (at >= t0Sec && at <= tEndSec) out.push({ label: anchor.label, at });
    });
  }
  return out.sort((a, b) => a.at - b.at);
}

export function buildScale(bars, snapshot, armed) {
  const values = [];
  bars.forEach((bar) => {
    if (!bar.valid) return;
    values.push(bar.h, bar.l);
  });
  // Keep candle action plus an actually armed plan as the price domain.
  // ICT overlays remain visible, but distant levels are clipped to the edge
  // instead of flattening the execution-relevant candle geometry.
  if (armed) {
    [armed.entry, armed.stop, armed.target].forEach((value) => {
      if (value !== null) values.push(value);
    });
  }
  if (!values.length) return null;
  let min = Math.min(...values);
  let max = Math.max(...values);
  const spread = Math.max(max - min, 1);
  const padding = spread * 0.06;
  min -= padding;
  max += padding;
  return { min, max };
}

/** HUD の変化量表示。前足の終値との差(pt)と率。 */
function changeText(bar, previous) {
  if (!bar || !previous || !finite(previous.c)) return null;
  const diff = bar.c - previous.c;
  const pct = previous.c ? (diff / previous.c) * 100 : null;
  const sign = diff >= 0 ? "+" : "−";
  return {
    up: diff >= 0,
    label: `${sign}${Math.abs(diff).toFixed(2)}${pct === null ? "" : ` (${sign}${Math.abs(pct).toFixed(2)}%)`}`,
  };
}

// `autoPoll` を false にすると market.json の定期取得を止める。
// 検証済みサーバー状態を受け取っているときに、ビルド同梱の静的 JSON が
// あとから上書きしてしまうのを防ぐ(古い値が新しい値に勝ってはいけない)。
export function initTapeChart(mountEl, { autoPoll = true, webgl = true } = {}) {
  if (!mountEl) {
    return { update() {}, arm() {}, disarm() {}, destroy() {} };
  }

  mountEl.setAttribute("aria-live", "polite");
  const status = document.createElement("span");
  status.className = "chart-status";
  status.setAttribute("role", "status");
  mountEl.append(status);

  // R56: OHLC の読み出し(HUD)。SVG の文字より HTML の方がにじまない。
  // 通常は最後の確定足、十字カーソル中はその足を出す。
  const hud = document.createElement("div");
  hud.className = "chart-hud";
  hud.setAttribute("aria-hidden", "true");
  hud.hidden = true;
  mountEl.append(hud);

  const svg = svgElement("svg", {
    role: "img",
    "aria-label": "Three-minute market tape",
    focusable: "false"
  });
  mountEl.append(svg);

  // 氷の 3D レイヤ。SVG の上に重ねる(pointer-events は CSS で殺してある)。
  // three.js は遅延読み込みする — 3D 依存で発注ゲートを遅らせない
  // (app.js:56-66 と同じ規律)。読めるまで、あるいは WebGL が無い環境では
  // SVG の氷が出る。
  let ice3d = null;
  const iceCanvas = document.createElement("canvas");
  iceCanvas.className = "chart-ice3d";
  iceCanvas.setAttribute("aria-hidden", "true");
  mountEl.append(iceCanvas);
  // R61: 3D が届くまでの間、SVG の氷(旧版の代替)を一瞬でも見せない(2026-09-06 ユーザー報告
  // 「一瞬表示される古い氷チャートを削除して」)。WebGL が使える端末では 3D 待ちの間は何も描かず、
  // 3D が来なければ(読み込み失敗・WebGL 不可)そのとき初めて SVG に落ちる。
  // R67: 起動の見張り(boot_guard.js)が WebGL を止めた起動では、氷の 3D を読みにも行かず
  // 最初から SVG の氷を描く(Telegram Desktop で WebView が落ちていた)。
  let ice3dPending = (() => {
    if (!webgl) return false;
    try {
      const probe = document.createElement("canvas");
      return Boolean(window.WebGLRenderingContext && (probe.getContext("webgl2") || probe.getContext("webgl")));
    } catch {
      return false;
    }
  })();
  if (webgl && typeof document !== "undefined" && typeof requestAnimationFrame === "function") {
    import("./icescene.js").then(({ initIceScene }) => {
      if (destroyed) return;
      ice3d = initIceScene(iceCanvas);
      if (!ice3d.ok) iceCanvas.classList.add("is-unavailable");
      ice3dPending = false;
      render();
    }).catch(() => {
      iceCanvas.classList.add("is-unavailable");
      ice3dPending = false;
      if (!destroyed) render();
    });
  } else {
    iceCanvas.classList.add("is-unavailable");
    ice3dPending = false;
  }

  let snapshot = null;
  let armed = null;
  let destroyed = false;
  let resizeObserver = null;
  // 直近の描画の幾何。十字カーソルが再描画なしに座標 → 足 / 価格を引くために持つ。
  let layout = null;
  const crosshair = svgElement("g", { class: "chart-crosshair" });

  const setStatus = (message, kind = "") => {
    status.textContent = message;
    status.hidden = !message;
    mountEl.classList.toggle("is-stale", kind === "stale");
    mountEl.classList.toggle("is-no-feed", kind === "no-feed");
  };

  const setHud = (bar, previous, { pinned = false } = {}) => {
    if (!bar) {
      hud.hidden = true;
      return;
    }
    const change = changeText(bar, previous);
    const cell = (label, value) => `<span><i>${label}</i><b>${priceLabel(value)}</b></span>`;
    hud.innerHTML = `<span class="hud-time">${timeLabel(bar.t)}</span>`
      + cell("O", bar.o) + cell("H", bar.h) + cell("L", bar.l) + cell("C", bar.c)
      + (change ? `<span class="hud-change ${change.up ? "is-up" : "is-down"}">${change.label}</span>` : "")
      + (finite(bar.v) && bar.v > 0
        ? `<span><i>V</i><b>${Math.round(bar.v).toLocaleString("en-US")}</b></span>` : "");
    hud.classList.toggle("is-pinned", pinned);
    hud.hidden = false;
  };

  const clearCrosshair = () => {
    while (crosshair.firstChild) crosshair.removeChild(crosshair.firstChild);
    mountEl.classList.remove("has-crosshair");
    if (layout) setHud(layout.lastBar, layout.prevBar);
  };

  /** 十字カーソル。ポインタ位置の足へ吸着し、価格は軸に、時刻は時間レーンに出す。 */
  const drawCrosshair = (px, py) => {
    if (!layout) return;
    const { bars, slots, xSlot, y, plotTop, plotBottom, plotWidth, slotW, scale, height } = layout;
    while (crosshair.firstChild) crosshair.removeChild(crosshair.firstChild);
    if (px < 0 || px > plotWidth || py < plotTop || py > plotBottom) {
      mountEl.classList.remove("has-crosshair");
      setHud(layout.lastBar, layout.prevBar);
      return;
    }
    let nearest = -1;
    let best = Infinity;
    bars.forEach((bar, index) => {
      if (!bar.valid || slots[index] === null) return;
      const distance = Math.abs(xSlot(slots[index]) - px);
      if (distance < best) { best = distance; nearest = index; }
    });
    const price = scale.max - ((py - plotTop) / Math.max(1, plotBottom - plotTop)) * (scale.max - scale.min);
    const cx = nearest >= 0 && best <= slotW * 2 ? xSlot(slots[nearest]) : px;
    crosshair.append(svgElement("line", { x1: cx, x2: cx, y1: plotTop, y2: plotBottom }));
    crosshair.append(svgElement("line", { x1: 0, x2: plotWidth, y1: py, y2: py }));
    // 価格タグ(軸レーン)
    const tagY = Math.max(plotTop, Math.min(plotBottom - TAG_H, py - TAG_H / 2));
    crosshair.append(svgElement("rect", { x: plotWidth + 2, y: tagY, width: PRICE_LANE - 4, height: TAG_H, rx: 2 }));
    const priceText = svgElement("text", {
      x: plotWidth + PRICE_LANE / 2, y: tagY + TAG_H / 2 + 3.5,
      "text-anchor": "middle", "font-size": LABEL_SIZE, "font-family": "var(--mono)",
    });
    priceText.textContent = priceLabel(Math.round(price / 0.25) * 0.25);
    crosshair.append(priceText);
    // 時刻タグ(時間レーン)
    if (nearest >= 0 && best <= slotW * 2) {
      const label = timeLabel(bars[nearest].t);
      const w = 40;
      const tx = Math.max(0, Math.min(plotWidth - w, cx - w / 2));
      crosshair.append(svgElement("rect", { x: tx, y: height - TIME_LANE + 2, width: w, height: TIME_LANE - 4, rx: 2 }));
      const timeText = svgElement("text", {
        x: tx + w / 2, y: height - 5, "text-anchor": "middle",
        "font-size": 9, "font-family": "var(--mono)",
      });
      timeText.textContent = label;
      crosshair.append(timeText);
      let previous = null;
      for (let i = nearest - 1; i >= 0; i -= 1) {
        if (bars[i].valid) { previous = bars[i]; break; }
      }
      setHud(bars[nearest], previous, { pinned: true });
    }
    mountEl.classList.add("has-crosshair");
  };

  // 氷の 3D 層を空にする。SVG は render のたびに消しているが、WebGL の canvas は update(null) を
  // 呼ばない限り前の絵が残る —— NO FEED に落ちた後も氷の足が浮かんだままだった(2026-09-06 バグ狩り)。
  const clearIce = () => { if (ice3d && ice3d.ok) ice3d.update(null); };

  const render = () => {
    if (destroyed) return;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    const width = Math.max(1, Math.round(mountEl.clientWidth || 0));
    const height = Math.max(1, Math.round(mountEl.clientHeight || 0));
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("width", width);
    svg.setAttribute("height", height);
    layout = null;

    if (!snapshot || width <= 1 || height <= 1) {
      mountEl.classList.remove("has-armed");
      setHud(null);
      // 寸法 0 は一時的(差し戻しの最中)なので氷は保つ。市況が無いときだけ消す。
      if (!snapshot) clearIce();
      return;
    }

    const freshnessState = freshness(snapshot.ageMs);
    const normalizedArmed = armed ? {
      side: String(armed.accent || armed.side || "").toUpperCase(),
      entry: priceFromScenario(armed.entry),
      stop: priceFromScenario(armed.stop),
      target: priceFromScenario(armed.target)
    } : null;

    // 確定足のみ。形成中バーは価格が動き続けるので描かない。
    const stepSec = barStepSec(snapshot.bars, snapshot.barResolution || snapshot.resolution);
    const bars = confirmedBars(snapshot.bars, stepSec, parseAt(snapshot.at));
    const scale = buildScale(bars, snapshot, normalizedArmed);
    if (!scale) {
      setStatus("NO FEED", "no-feed");
      mountEl.classList.remove("has-armed");
      setHud(null);
      clearIce();
      return;
    }

    // プロット領域 = 価格軸(右)と時間軸(下)の内側。
    const plotTop = PLOT_PAD_TOP;
    const plotBottom = height - TIME_LANE - PLOT_PAD_BOTTOM;
    const plotHeight = Math.max(1, plotBottom - plotTop);
    const y = (value) => {
      const ratio = (value - scale.min) / (scale.max - scale.min);
      return plotBottom - (Math.max(0, Math.min(1, ratio)) * plotHeight);
    };
    const gutter = PRICE_LANE;
    const plotWidth = Math.max(1, width - gutter);

    // 時間スロット。武装中は右端に SL/TP ボックス用の未来ゾーンを確保する。
    const { slots, count } = slotIndices(bars, stepSec);
    const hasArmedBox = Boolean(normalizedArmed &&
      [normalizedArmed.entry, normalizedArmed.stop, normalizedArmed.target]
        .every((value) => value !== null));
    // 右側の未来ゾーン。武装中はポジションボックスぶんだけ空け、非武装は
    // TradingView の既定と同じく僅かな余白にとどめる。
    const futureSlots = hasArmedBox ? Math.max(12, Math.round(count * 0.3)) : 2;
    const slotW = plotWidth / Math.max(1, count + futureSlots);
    const xSlot = (slot) => slot * slotW + slotW / 2;
    const firstValid = bars.find((bar) => bar.valid && finite(bar.t));
    const t0 = firstValid ? firstValid.t : NaN;
    const tEnd = Number.isFinite(t0) ? t0 + (count + futureSlots) * stepSec : NaN;

    const chartRoot = svgElement("g", { class: "chart-root" });
    const matrix = snapshot.strategyMatrix || {};
    const strategyOverlays = matrix.overlays || {};

    // TradingView 流儀のグリッド。価格は丸い刻み(25/50/100…)で、
    // 軸ラベルはその刻みの値だけ。時間は下の軸レーンに HH:MM。
    const tvGrid = svgElement("g", { class: "tv-grid" });
    const step = niceStep(scale.max - scale.min, plotHeight);
    const firstTick = Math.ceil(scale.min / step) * step;
    const axisLabels = [];
    for (let value = firstTick; value <= scale.max + 1e-9; value += step) {
      const gy = y(value);
      tvGrid.append(svgElement("line", { x1: 0, x2: plotWidth, y1: gy, y2: gy,
        stroke: "var(--chart-grid)", "stroke-width": 1 }));
      const label = svgElement("text", { x: plotWidth + 6, y: gy + 3.5,
        fill: "var(--chart-axis)", "font-size": LABEL_SIZE, "font-family": "var(--mono)" });
      label.textContent = step >= 1 ? Math.round(value).toLocaleString("en-US") : priceLabel(value);
      tvGrid.append(label);
      axisLabels.push({ y: gy, node: label });
    }
    // 時間軸: 丸い時刻(JST 15/30/60/120 分)に目盛りとラベル。
    const xAtTime = (sec) => xSlot((sec - t0) / stepSec);
    if (Number.isFinite(t0)) {
      const ticks = timeTicks(t0, tEnd, slotW / stepSec);
      ticks.marks.forEach((mark) => {
        const gx = xAtTime(mark);
        if (gx < 0 || gx > plotWidth) return;
        tvGrid.append(svgElement("line", { x1: gx, x2: gx, y1: plotTop, y2: plotBottom,
          stroke: "var(--chart-grid)", "stroke-width": 1 }));
        // ラベル幅(≈30px)がプロットの外へ出る位置は描かない。
        if (gx < 16 || gx > plotWidth - 16) return;
        const label = svgElement("text", { x: gx, y: height - 5, fill: "var(--chart-axis)",
          "text-anchor": "middle", "font-size": 9, "font-family": "var(--mono)" });
        label.textContent = timeLabel(mark);
        tvGrid.append(label);
      });
      // セッションの節目: 強めの破線と小さな名札。
      sessionMarks(t0, tEnd).forEach((mark) => {
        const gx = xAtTime(mark.at);
        if (gx < 0 || gx > plotWidth) return;
        tvGrid.append(svgElement("line", { class: "chart-session", x1: gx, x2: gx, y1: plotTop, y2: plotBottom,
          stroke: "var(--chart-axis)", "stroke-width": 1, "stroke-dasharray": "2 4", opacity: ".55" }));
        const label = svgElement("text", { class: "chart-session-label", x: gx + 3, y: plotTop + 9,
          fill: "var(--chart-axis)", "font-size": 8, "font-family": "var(--mono)" });
        label.textContent = mark.label;
        tvGrid.append(label);
      });
    }
    // 軸の境界線
    tvGrid.append(svgElement("line", { x1: plotWidth, x2: plotWidth, y1: 0, y2: height,
      stroke: "var(--chart-grid)", "stroke-width": 1 }));
    tvGrid.append(svgElement("line", { x1: 0, x2: plotWidth, y1: plotBottom + PLOT_PAD_BOTTOM,
      y2: plotBottom + PLOT_PAD_BOTTOM, stroke: "var(--chart-grid)", "stroke-width": 1 }));
    // ── キルゾーン(R56)。ロンドン 02:00–05:00 / NY AM 08:30–11:00 / NY PM 13:30–16:00 ET
    // (EDT 固定の近似、order.py と同じ扱い)。地の薄い帯として最初に敷く。
    const zoneGroup = svgElement("g", { class: "chart-killzones" });
    if (Number.isFinite(t0)) {
      const windows = [["LDN", 6, 9], ["NY AM", 12.5, 15], ["NY PM", 17.5, 20]];
      const dayStart = Math.floor(t0 / 86_400) * 86_400;
      for (let day = -1; day <= 1; day += 1) {
        windows.forEach(([label, from, to]) => {
          const a = dayStart + day * 86_400 + from * 3600;
          const b = dayStart + day * 86_400 + to * 3600;
          if (b < t0 || a > tEnd) return;
          const x1 = Math.max(0, xAtTime(a));
          const x2 = Math.min(plotWidth, xAtTime(b));
          if (x2 - x1 < 2) return;
          zoneGroup.append(svgElement("rect", { class: "chart-killzone", x: x1, y: plotTop,
            width: x2 - x1, height: plotHeight, fill: "var(--chart-axis)", opacity: ".05" }));
          if (x2 - x1 >= 30) {
            const tag = svgElement("text", { class: "chart-kz-label", x: x1 + 3, y: plotBottom - 3,
              fill: "var(--chart-axis)", "font-size": 7, "font-family": "var(--mono)", opacity: ".6" });
            tag.textContent = label;
            zoneGroup.append(tag);
          }
        });
      }
    }
    chartRoot.append(zoneGroup);
    chartRoot.append(tvGrid);
    const indicators = snapshot.indicators && typeof snapshot.indicators === "object" ? snapshot.indicators : {};
    const bandLow = number(indicators.vwapLo ?? snapshot.vwap_lo);
    const bandHigh = number(indicators.vwapHi ?? snapshot.vwap_hi);
    if (bandLow !== null && bandHigh !== null) {
      chartRoot.append(svgElement("rect", {
        class: "chart-vwap-band",
        x: 0,
        y: Math.min(y(bandLow), y(bandHigh)),
        width: plotWidth,
        height: Math.abs(y(bandLow) - y(bandHigh)),
        fill: GOLD,
        opacity: ".05"
      }));
    }

    // 描く順は 線 → 出来高 → ローソク足 → 前方ボックス → 値札。
    const gridGroup = svgElement("g", { class: "chart-grid" });
    const labelGroup = svgElement("g", { class: "chart-labels" });
    const pending = [];

    const addLine = (price, style, x1 = 0) => {
      const parsed = number(price);
      if (parsed === null) return null;
      const lineY = y(parsed);
      const line = svgElement("line", {
        class: "chart-level",
        x1,
        x2: plotWidth,
        y1: lineY,
        y2: lineY,
        stroke: style.stroke,
        "stroke-width": 1,
        opacity: String(style.opacity)
      });
      if (style.dash) line.setAttribute("stroke-dasharray", style.dash);
      gridGroup.append(line);
      return lineY;
    };

    const appendZone = (lo, hi, className, fill, opacity, x = 0, widthOverride = plotWidth) => {
      const low = number(lo), high = number(hi);
      if (low === null || high === null) return;
      gridGroup.append(svgElement("rect", {
        class: className, x, y: Math.min(y(low), y(high)), width: widthOverride,
        height: Math.max(1, Math.abs(y(low) - y(high))), fill, opacity
      }));
    };

    // ゾーンの名札(R56)。何の箱かを読めるようにする。7px・淡色・左端。
    const zoneLabel = (x, lo, hi, label, fill) => {
      const low = number(lo), high = number(hi);
      if (low === null || high === null) return;
      const top = Math.min(y(low), y(high));
      if (Math.abs(y(low) - y(high)) < 9 || x > plotWidth - 24) return;
      const node = svgElement("text", { class: "chart-zone-label", x: x + 3, y: top + 8,
        fill, "font-size": 7, "font-family": "var(--mono)", opacity: ".75" });
      node.textContent = label;
      gridGroup.append(node);
    };
    // 表示範囲の外の水準は描かない(上下端に張り付いて HUD や軸タグと重なる)。
    const inRange = (price) => Number.isFinite(price) && price >= scale.min && price <= scale.max;
    const zoneStartX = (createdAt) => (finite(createdAt) && Number.isFinite(t0)
      ? Math.max(0, Math.min(plotWidth - 4, xAtTime(Number(createdAt)) - slotW / 2)) : 0);

    // Image strategy overlays: FVG, OTE/PD, CRT range, and liquidity pools.
    const anchor = strategyOverlays.rangeAnchor || {};
    if (anchor.valid && number(anchor.high) !== null && number(anchor.low) !== null) {
      appendZone(anchor.low, anchor.high, "strategy-range", "var(--chart-blue)", ".045");
      if (Array.isArray(anchor.oteBuy) && anchor.oteBuy.length === 2)
        appendZone(anchor.oteBuy[0], anchor.oteBuy[1], "strategy-ote-buy", "var(--chart-teal)", ".12");
      if (Array.isArray(anchor.oteSell) && anchor.oteSell.length === 2)
        appendZone(anchor.oteSell[0], anchor.oteSell[1], "strategy-ote-sell", "var(--chart-red)", ".12");
      addLine(number(anchor.eq), { stroke: "var(--chart-blue)", dash: "3 3", opacity: .52 });
    }
    // FVG は生成足から右端へ(createdAt が無ければ全幅)。名札に年齢を添える。
    ["BULL", "BEAR"].forEach((side) => {
      (strategyOverlays.fvg?.[side] || []).slice(0, 4).forEach((gap) => {
        const fill = side === "BULL" ? "var(--chart-teal)" : "var(--chart-red)";
        const x = zoneStartX(gap.createdAt);
        appendZone(gap.lo, gap.hi, side === "BULL" ? "strategy-fvg-bull" : "strategy-fvg-bear",
          fill, ".14", x, plotWidth - x);
        const age = finite(gap.ageBars) ? ` ${Math.round(Number(gap.ageBars))}b` : "";
        zoneLabel(x, gap.lo, gap.hi, `FVG${side === "BULL" ? "↑" : "↓"}${age}`, fill);
      });
    });
    const crt = strategyOverlays.crt || {};
    if (number(crt.rangeHigh) !== null && number(crt.rangeLow) !== null) {
      // CRT レンジは直近の区間だけに描く(全幅だと過去の足と無関係な箱に見える)。
      const crtX = plotWidth * 0.6;
      appendZone(crt.rangeLow, crt.rangeHigh, "strategy-crt", "var(--chart-purple)", ".08", crtX, plotWidth - crtX);
      addLine(number(crt.rangeHigh), { stroke: "var(--chart-purple)", dash: "4 3", opacity: .72 }, crtX);
      addLine(number(crt.rangeLow), { stroke: "var(--chart-purple)", dash: "4 3", opacity: .72 }, crtX);
      zoneLabel(crtX, crt.rangeLow, crt.rangeHigh, "CRT", "var(--chart-purple)");
    }
    // 流動性プール: 右 35% の短い点線と BSL/SSL の名札。名前付き水準は levels 側で描く。
    (strategyOverlays.liquidity?.pools || []).filter((pool) => pool?.kind !== "NAMED_LEVEL").slice(-6).forEach((pool) => {
      const poolPrice = number(pool?.price);
      if (poolPrice === null || !inRange(poolPrice)) return;
      const color = pool.side === "BSL" ? "var(--chart-red)" : "var(--chart-teal)";
      const poolX = plotWidth * 0.65;
      addLine(poolPrice, { stroke: color, dash: "2 4", opacity: .55 }, poolX);
      const tag = svgElement("text", { class: "chart-zone-label", x: poolX + 2, y: y(poolPrice) - 2,
        fill: color, "font-size": 7, "font-family": "var(--mono)", opacity: ".8" });
      tag.textContent = pool.side === "BSL" ? "BSL" : "SSL";
      gridGroup.append(tag);
    });
    const dol = strategyOverlays.liquidity?.dol || {};
    [["BUY", dol.BUY, "var(--chart-teal)"], ["SELL", dol.SELL, "var(--chart-red)"]].forEach(([side, price, color]) => {
      const parsed = number(price);
      if (parsed === null || !inRange(parsed)) return;
      const tag = svgElement("text", { class: "chart-zone-label", x: plotWidth - 3, y: y(parsed) + (side === "BUY" ? -3 : 9),
        fill: color, "text-anchor": "end", "font-size": 7, "font-family": "var(--mono)", opacity: ".8" });
      tag.textContent = `DOL${side === "BUY" ? "▲" : "▼"}`;
      gridGroup.append(tag);
    });

    (strategyOverlays.ifvg?.zones || []).slice(0, 4).forEach((zone) => {
      const fill = zone.direction === "SELL" ? "var(--chart-red)" : "var(--chart-teal)";
      const x = zoneStartX(zone.createdAt);
      appendZone(zone.lo, zone.hi, "strategy-ifvg", fill, zone.active ? ".2" : ".08", x, plotWidth - x);
      zoneLabel(x, zone.lo, zone.hi, `IFVG${zone.direction === "SELL" ? "↓" : "↑"}`, fill);
    });
    const blockColors = { OB: "var(--chart-blue)", BRK: "var(--chart-purple)", MB: "var(--chart-teal)", RJB: "var(--chart-red)" };
    (strategyOverlays.blocks?.zones || []).slice(0, 8).forEach((zone) => {
      const fill = blockColors[zone.kind] || "var(--chart-blue)";
      const x = zoneStartX(zone.createdAt);
      appendZone(zone.lo, zone.hi, "strategy-block", fill, zone.active ? ".16" : ".06", x, plotWidth - x);
      zoneLabel(x, zone.lo, zone.hi, String(zone.kind || "BLOCK"), fill);
    });
    // Gann 1x1(R56)。ピボットから pricePerBar の傾きで右端まで。advisory なので薄く。
    const gann = (matrix.models && matrix.models.gann) || null;
    const gannAnchor = gann && gann.anchor && typeof gann.anchor === "object" ? gann.anchor : null;
    if (gannAnchor && Number.isFinite(t0) && finite(gannAnchor.pivotT) && finite(gannAnchor.pivot)
        && finite(gannAnchor.pricePerBar) && Number(gannAnchor.pricePerBar) > 0) {
      const gx0 = xAtTime(Number(gannAnchor.pivotT));
      if (gx0 >= 0 && gx0 < plotWidth - 8) {
        const dir = String(gannAnchor.direction || "UP").toUpperCase() === "DOWN" ? -1 : 1;
        const spanSlots = (plotWidth - gx0) / slotW;
        const endPrice = Number(gannAnchor.pivot) + dir * Number(gannAnchor.pricePerBar) * spanSlots;
        if (!inRange(Number(gannAnchor.pivot))) return;
        gridGroup.append(svgElement("line", { class: "chart-gann", x1: gx0, y1: y(Number(gannAnchor.pivot)),
          x2: plotWidth, y2: y(endPrice), stroke: "var(--chart-purple)", "stroke-width": 1,
          "stroke-dasharray": "1 3", opacity: ".55" }));
        const tag = svgElement("text", { class: "chart-zone-label", x: plotWidth - 3, y: y(endPrice) - 3,
          fill: "var(--chart-purple)", "text-anchor": "end", "font-size": 7, "font-family": "var(--mono)", opacity: ".7" });
        tag.textContent = "1x1";
        gridGroup.append(tag);
      }
    }
    (strategyOverlays.fibSd?.levels || []).slice(0, 10).forEach((level) => {
      addLine(level, { stroke: "var(--chart-orange, #f59e0b)", dash: "1 5", opacity: .42 });
    });
    const sessionOverlay = strategyOverlays.sessions || {};
    if (number(sessionOverlay.midnightOpen) !== null)
      addLine(sessionOverlay.midnightOpen, { stroke: "var(--chart-orange, #f59e0b)", dash: "6 2", opacity: .82 });
    if (number(sessionOverlay.asianHigh) !== null)
      addLine(sessionOverlay.asianHigh, { stroke: "var(--chart-axis)", dash: "3 3", opacity: .42 });
    if (number(sessionOverlay.asianLow) !== null)
      addLine(sessionOverlay.asianLow, { stroke: "var(--chart-axis)", dash: "3 3", opacity: .42 });

    // ── セッション VWAP の系列(遡及計算)。平行線ではなく曲線で描く。
    const vwapCurve = computeVwapSeries(bars, stepSec);
    const publishedVwap = number(snapshot.vwap);
    if (vwapCurve) {
      const points = vwapCurve.points
        .map((point) => `${xSlot(slots[point.index])},${y(point.value)}`);
      gridGroup.append(svgElement("polyline", {
        class: "chart-vwap-curve",
        points: points.join(" "),
        fill: "none",
        stroke: GOLD,
        "stroke-width": 1.5,
        "stroke-linejoin": "round",
        opacity: ".85"
      }));
      const lastValue = vwapCurve.points[vwapCurve.points.length - 1].value;
      const tagPrice = publishedVwap !== null ? publishedVwap : lastValue;
      pending.push({
        y: y(tagPrice),
        name: vwapCurve.anchored ? "VWAP" : "VWAP≈",
        price: tagPrice,
        color: GOLD, nameColor: GOLD, weight: 5, distance: 0
      });
    }

    const levels = Array.isArray(snapshot.levels) ? snapshot.levels : [];
    // ── Volume Profile(R56)。C:(当日)と P:(前日)の VAL–VAH を帯、POC を線で描く。
    // Sessions&VP の見え方に寄せ、generic の破線では描かない(名札は短く POC/VAH/VAL)。
    const vp = { C: {}, P: {} };
    levels.forEach((level) => {
      const match = /^([CP]):\s*(POC|VAH|VAL)$/i.exec(String(level?.label ?? level?.name ?? "").trim());
      const price = number(level?.price);
      if (match && price !== null) vp[match[1].toUpperCase()][match[2].toUpperCase()] = price;
    });
    const vpHandled = new Set();
    [["C", ".07", ".85", ""], ["P", ".035", ".5", "2 3"]].forEach(([key, bandOpacity, pocOpacity, pocDash]) => {
      const row = vp[key];
      if (row.VAH !== undefined && row.VAL !== undefined) {
        appendZone(row.VAL, row.VAH, "chart-vp-band", "var(--chart-blue)", bandOpacity);
        vpHandled.add(`${key}: VAH`);
        vpHandled.add(`${key}: VAL`);
      }
      if (row.POC !== undefined) {
        addLine(row.POC, { stroke: "var(--chart-blue)", dash: pocDash, opacity: Number(pocOpacity) });
        vpHandled.add(`${key}: POC`);
      }
    });
    const vpShort = (name) => name.replace(/^C:\s*/i, "").replace(/^P:\s*/i, "P.");
    levels.forEach((level) => {
      const price = number(level?.price);
      if (price === null) return;
      const name = `${level?.label ?? level?.name ?? ""}`;
      const vpKey = name.trim().toUpperCase().replace(/\s+/g, " ");
      if (vpHandled.has(vpKey)) {
        // 帯と POC は上で描いた。値札だけ短い名前で残す。
        pending.push({ y: y(price), name: vpShort(name.trim()), price, color: "var(--chart-blue)",
          nameColor: "#7fa6ff", weight: /POC/i.test(name) ? 4 : 3,
          distance: Math.abs(y(price) - y(Number(snapshot.price ?? price))) });
        return;
      }
      // VWAP 本体は曲線で描いたので、水平線の二重描画をしない(帯の上下限は残す)。
      if (vwapCurve && /VWAP/i.test(name) && !/上限|下限|band|hi\b|lo\b/i.test(name) &&
          publishedVwap !== null && Math.abs(price - publishedVwap) < 0.75) {
        return;
      }
      const style = levelStyle(level);
      const lineY = y(price);
      if (String(level.kind || "").toLowerCase() === "zone") {
        const to = number(level.to);
        if (to !== null) {
          gridGroup.append(svgElement("rect", {
            class: "chart-zone",
            x: 0,
            y: Math.min(lineY, y(to)),
            width: plotWidth,
            height: Math.max(1, Math.abs(lineY - y(to))),
            fill: "var(--bone)",
            opacity: ".08"
          }));
        }
      }

      // 起点の足が窓内にあれば、その足の top / bottom からレイを引く。
      const anchorHit = levelAnchorIndex(price, bars);
      if (anchorHit && slots[anchorHit.index] !== null) {
        // 起点の足からのレイ。ステムや丸は描かない(線と軸タグだけで十分)。
        addLine(price, style, xSlot(slots[anchorHit.index]));
      } else {
        // 窓より古いレベル(前日高値など)。全幅・より淡くして区別する。
        addLine(price, { ...style, opacity: style.opacity * 0.55, dash: style.dash || "2 6" });
      }

      const out = price > scale.max ? "↑" : price < scale.min ? "↓" : "";
      pending.push({
        y: lineY,
        name: `${out}${text(level.label ?? level.name, "LEVEL")}`,
        price,
        color: style.stroke,
        nameColor: "var(--muted)",
        // 現在値に近いレベルほど残す。間引きは遠いものから。
        weight: style.weight,
        distance: Math.abs(lineY - y(Number(snapshot.price ?? price)))
      });
    });

    // ── MSNR のアンカー(R56)。評価が今どの水準を見ているかを線で示す。
    const msnr = snapshot.evaluation?.msnr;
    const msnrPrice = number(msnr?.price);
    if (msnrPrice !== null && inRange(msnrPrice) && !pending.some((item) => Math.abs(item.price - msnrPrice) < 0.01)) {
      addLine(msnrPrice, { stroke: "var(--bone)", dash: "", opacity: .7 });
      pending.push({ y: y(msnrPrice), name: `MSNR ${msnr.label || ""}`.trim(), price: msnrPrice,
        color: "var(--bone)", nameColor: "var(--bone)", weight: 6, distance: 0 });
    }
    // ── 最有力候補の幾何(R56)。武装していない WATCH でも、評価が出した E/SL/TP1 を
    // 右 40% の破線で見せる(武装したときは前方ボックスがこれに置き換わる)。
    const candidate = snapshot.evaluation?.decision;
    if (candidate && candidate.model && candidate.model !== "FLAT" && !hasArmedBox) {
      const candX = plotWidth * 0.6;
      const tp1 = Array.isArray(candidate.targets) ? number(candidate.targets[0]) : null;
      [["E", number(candidate.entry), "var(--bone)"], ["SL", number(candidate.stop), "var(--chart-red)"],
        ["TP1", tp1, "var(--chart-teal)"]].forEach(([label, price, color]) => {
        if (price === null || !inRange(price)) return;
        addLine(price, { stroke: color, dash: "6 3", opacity: .7 }, candX);
        pending.push({ y: y(price), name: `${label} ${String(candidate.model).split("_")[0]}`, price,
          color, nameColor: color, weight: 8, distance: 0 });
      });
    }
    // ── 15分 SwingArm(R56)。トレールと HARD SL を淡い点線で。
    const ct = indicators.ct && typeof indicators.ct === "object" ? indicators.ct : {};
    const nearPrice = (price) => Math.abs(y(price) - y(Number(snapshot.price ?? price)));
    if (number(ct.trail) !== null && inRange(Number(ct.trail))) {
      addLine(ct.trail, { stroke: "var(--bone)", dash: "1 3", opacity: .45 });
      pending.push({ y: y(Number(ct.trail)), name: "CT TRAIL", price: Number(ct.trail),
        color: "var(--bone)", nameColor: "var(--muted)", weight: 2, distance: nearPrice(Number(ct.trail)) });
    }
    if (number(ct.hardStop) !== null && inRange(Number(ct.hardStop))) {
      addLine(ct.hardStop, { stroke: "var(--chart-red)", dash: "1 3", opacity: .45 });
      pending.push({ y: y(Number(ct.hardStop)), name: "HARD SL", price: Number(ct.hardStop),
        color: "var(--chart-red)", nameColor: "var(--chart-red)", weight: 2, distance: nearPrice(Number(ct.hardStop)) });
    }

    // volume の無いフィードでは曲線を復元できない。従来の公表値の水平線に落とす。
    if (!vwapCurve && finite(snapshot.vwap) &&
        !pending.some((item) => Math.abs(item.price - Number(snapshot.vwap)) < 0.01)) {
      const vwap = Number(snapshot.vwap);
      addLine(vwap, { stroke: GOLD, dash: "", opacity: 0.7 });
      pending.push({
        y: y(vwap), name: "VWAP", price: vwap,
        color: GOLD, nameColor: GOLD, weight: 5, distance: 0
      });
    }
    chartRoot.append(gridGroup);

    // ── 出来高(R56)。プロット下端の帯にローソクと同色で薄く敷く。
    // 高さは窓内の最大出来高で正規化する。volume の無いフィードでは描かない。
    const bodyW = Math.max(2, Math.floor(slotW * 0.66));
    const volumeMax = bars.reduce((max, bar) =>
      (bar.valid && finite(bar.v) && bar.v > max ? bar.v : max), 0);
    if (volumeMax > 0) {
      const volumeGroup = svgElement("g", { class: "chart-volume" });
      const bandH = plotHeight * VOLUME_RATIO;
      bars.forEach((bar, index) => {
        if (!bar.valid || slots[index] === null || !(finite(bar.v) && bar.v > 0)) return;
        const cx = Math.round(xSlot(slots[index])) + 0.5;
        const h = Math.max(1, (bar.v / volumeMax) * bandH);
        volumeGroup.append(svgElement("rect", {
          class: "chart-volume-bar",
          x: Math.round(cx - bodyW / 2), y: plotBottom - h, width: bodyW, height: h,
          fill: bar.c >= bar.o ? "var(--chart-teal)" : "var(--chart-red)", opacity: ".2"
        }));
      });
      chartRoot.append(volumeGroup);
    }

    // ── ローソク足(確定足のみ・欠測スロットは空白のまま)。
    // TradingView の既定: 中実ボディ、ヒゲはボディと同色、ボディは
    // スロット幅の 2/3。半透明をやめて輪郭をはっきりさせる。
    const candleGroup = svgElement("g", { class: "chart-candles" });
    let lastIndex = -1;
    let hiIndex = -1;
    let loIndex = -1;
    bars.forEach((bar, index) => {
      if (!bar.valid || slots[index] === null) return;
      lastIndex = index;
      if (hiIndex < 0 || bar.h > bars[hiIndex].h) hiIndex = index;
      if (loIndex < 0 || bar.l < bars[loIndex].l) loIndex = index;
      const cx = Math.round(xSlot(slots[index])) + 0.5;
      const up = bar.c >= bar.o;
      const color = up ? "var(--chart-teal)" : "var(--chart-red)";
      candleGroup.append(svgElement("line", {
        class: "chart-wick",
        x1: cx, x2: cx, y1: y(bar.h), y2: y(bar.l),
        stroke: color, "stroke-width": 1
      }));
      candleGroup.append(svgElement("rect", {
        class: "chart-body",
        x: Math.round(cx - bodyW / 2),
        y: Math.min(y(bar.o), y(bar.c)),
        width: bodyW,
        height: Math.max(1, Math.abs(y(bar.o) - y(bar.c))),
        fill: color
      }));
    });
    // 窓内の高値・安値(R56)。どこまで行ったかを数字で残す。
    if (hiIndex >= 0 && loIndex >= 0 && bars.length >= 6) {
      const mark = (index, price, above) => {
        const cx = xSlot(slots[index]);
        const anchorSide = cx < 40 ? "start" : cx > plotWidth - 40 ? "end" : "middle";
        const node = svgElement("text", {
          class: "chart-extreme",
          x: Math.max(2, Math.min(plotWidth - 2, cx)),
          y: above ? Math.max(plotTop + 8, y(price) - 4) : Math.min(plotBottom - 2, y(price) + 10),
          "text-anchor": anchorSide, fill: "var(--bone)", "font-size": 8,
          "font-family": "var(--mono)", opacity: ".8"
        });
        node.textContent = `${above ? "H" : "L"} ${priceLabel(price)}`;
        candleGroup.append(node);
      };
      mark(hiIndex, bars[hiIndex].h, true);
      mark(loIndex, bars[loIndex].l, false);
    }
    let lastBar = null;
    let prevBar = null;
    if (lastIndex >= 0) {
      lastBar = bars[lastIndex];
      for (let i = lastIndex - 1; i >= 0; i -= 1) {
        if (bars[i].valid) { prevBar = bars[i]; break; }
      }
      const rising = !prevBar || lastBar.c >= prevBar.c;
      const liveColor = rising ? "var(--chart-teal)" : "var(--chart-red)";
      // TradingView の現在値ライン: 全幅の細い点線。軸側は塗りつぶしタグ。
      candleGroup.append(svgElement("line", {
        class: "chart-live-line",
        x1: 0, x2: plotWidth, y1: y(lastBar.c), y2: y(lastBar.c),
        stroke: liveColor, "stroke-width": 1, "stroke-dasharray": "2 3", opacity: ".9"
      }));
      pending.push({
        y: y(lastBar.c), name: "LAST", price: lastBar.c,
        color: liveColor, nameColor: liveColor, weight: 99, distance: 0,
        emphasis: true, solid: true
      });
    }
    chartRoot.append(candleGroup);

    // ── 価格予測の氷のろうそく足。未来ゾーンに「計画」を投影する。
    // 相場の予測ではなく、計画が既に持つ 4 つの価格の内側だけを描く。
    // 本体は three.js の物理マテリアル(透過・屈折・厚み)で描く。WebGL が
    // 無い環境だけ SVG の擬似アイソメへ落ちる。配置計算 iceBlocks() は
    // 両者で共有するので、どちらでも同じ位置に出る。
    let iceDrawn = false;
    if (hasArmedBox && lastIndex >= 0 && futureSlots >= 4) {
      const ice = projectionCandles({
        last: bars[lastIndex].c, entry: normalizedArmed.entry,
        stop: normalizedArmed.stop, target: normalizedArmed.target,
        side: normalizedArmed.side
      }, 6);
      if (ice) {
        const x0 = xSlot(slots[lastIndex]) + slotW;
        const spanW = Math.max(24, plotWidth - x0);
        const blocks = iceBlocks({ candles: ice, y, x0, spanW });
        const long = normalizedArmed.stop < normalizedArmed.entry;
        iceDrawn = true;
        // 軌道は氷の**下**に敷く。氷が主役で、線は筋を読ませるだけ。
        const path = renderProjectionPath({
          make: svgElement, blocks, long, uid: "nqx-path",
          fromX: xSlot(slots[lastIndex]), fromY: y(bars[lastIndex].c),
        });
        if (path) chartRoot.append(path);
        if (ice3d && ice3d.ok) {
          ice3d.update({ width, height, long, blocks });
        } else if (!ice3dPending) {
          // WebGL 無し(または 3D が読めなかった)ときだけ SVG の氷。3D 待ちの間は描かない
          chartRoot.append(renderIceCandles({
            make: svgElement, candles: ice, y, x0, spanW, long, uid: "nqx-ice"
          }));
        }
      }
    }
    if (!iceDrawn && ice3d && ice3d.ok) ice3d.update(null);

    // ── SL/TP は現在価格の前方(未来ゾーン)にボックスで示す。
    const overlay = svgElement("g", { class: "chart-overlay" });
    if (hasArmedBox && lastIndex >= 0) {
      const isShort = normalizedArmed.side === "SHORT";
      const color = isShort ? "var(--rust)" : "var(--acid)";
      const entryY = y(normalizedArmed.entry);
      const stopY = y(normalizedArmed.stop);
      const targetY = y(normalizedArmed.target);
      // TradingView の Long/Short Position ツール: 現在足の直後から右端まで、
      // リスク(赤)とリワード(緑)の二段ボックス。枠線は無し、面だけ。
      const boxX = Math.min(plotWidth - 8, xSlot(slots[lastIndex]) + slotW * 0.9);
      const boxW = Math.max(10, plotWidth - boxX);
      overlay.append(svgElement("rect", {
        class: "chart-risk-box",
        x: boxX, y: Math.min(entryY, stopY),
        width: boxW, height: Math.max(1, Math.abs(entryY - stopY)),
        fill: "var(--chart-red)", opacity: iceDrawn ? ".10" : ".22"
      }));
      overlay.append(svgElement("rect", {
        class: "chart-reward-box",
        x: boxX, y: Math.min(entryY, targetY),
        width: boxW, height: Math.max(1, Math.abs(entryY - targetY)),
        fill: "var(--chart-teal)", opacity: iceDrawn ? ".10" : ".22"
      }));
      // 境界線は ENTRY / SL / TP の3本だけ。
      [[entryY, color, 1.5], [stopY, "var(--chart-red)", 1], [targetY, "var(--chart-teal)", 1]]
        .forEach(([lineY, stroke, w]) => {
          overlay.append(svgElement("line", {
            x1: boxX, x2: plotWidth, y1: lineY, y2: lineY, stroke, "stroke-width": w
          }));
        });
      // R:R と pt 幅を箱の中に書く(R56)。距離を読むのに軸へ目を往復させない。
      const riskPt = Math.abs(normalizedArmed.entry - normalizedArmed.stop);
      const rewardPt = Math.abs(normalizedArmed.target - normalizedArmed.entry);
      if (riskPt > 0 && boxW >= 60) {
        const note = (label, lineA, lineB, fill) => {
          const mid = (lineA + lineB) / 2;
          if (Math.abs(lineA - lineB) < 12) return;
          const node = svgElement("text", {
            class: "chart-box-note", x: boxX + 6, y: mid + 3, fill,
            "font-size": 8, "font-family": "var(--mono)", opacity: ".85"
          });
          node.textContent = label;
          overlay.append(node);
        };
        note(`SL ${riskPt.toFixed(2)}pt`, entryY, stopY, "var(--chart-red)");
        note(`TP ${rewardPt.toFixed(2)}pt · ${(rewardPt / riskPt).toFixed(2)}R`, entryY, targetY, "var(--chart-teal)");
      }
      [
        ["ENTRY", entryY, normalizedArmed.entry, color],
        ["SL", stopY, normalizedArmed.stop, "var(--chart-red)"],
        ["TP", targetY, normalizedArmed.target, "var(--chart-teal)"]
      ].forEach(([label, lineY, price, labelColor]) => {
        pending.push({
          y: lineY, name: label, price,
          color: labelColor, nameColor: labelColor, weight: 90, distance: 0, emphasis: true
        });
      });
    }
    chartRoot.append(overlay);

    // --- 値札レーン。優先度で間引き、残ったものを押し広げてから描く。
    // 軸を混雑させない: 強調タグ(現在値・ENTRY/SL/TP)は必ず、それ以外は
    // 現在値に近いレベルを最大 4 件だけ。残りは線のままで、タグを持たない。
    const emphasised = pending.filter((item) => item.emphasis);
    const others = pending.filter((item) => !item.emphasis)
      .sort((a, b) => (b.weight - a.weight) || (a.distance - b.distance))
      .slice(0, plotHeight >= 300 ? 7 : 4);
    const capacity = Math.max(2, Math.floor((plotHeight - LABEL_GAP) / LABEL_GAP));
    const kept = [...emphasised, ...others].slice(0, capacity);
    // TradingView の値札: 軸レーンの中に、線と同色のタグ。強調(現在値・
    // ENTRY/SL/TP)は塗りつぶし+黒文字、それ以外は枠だけ+色文字。
    // 名前は左端に、線の色の小さなピルで出す(ローソクの上でも読める)。
    // 名前ピルは値札より小さいので、線の近くに独立して押し広げる(値札の間隔で
    // 動かすと「LAST」のピルが LAST の線から 20px 離れて別の線の名前に見える)。
    const nameSlots = spreadLabels(kept.map((item) => ({ ref: item, y: item.y })), plotBottom, NAME_SIZE + 5);
    const nameYFor = new Map(nameSlots.map((slot) => [slot.ref, slot.labelY]));
    spreadLabels(kept, plotBottom).forEach((item) => {
      const tagY = Math.max(plotTop, Math.min(plotBottom - TAG_H, item.labelY - TAG_H / 2));
      // タグと重なる軸グリッドの数字は消す(TradingView と同じ振る舞い)。
      axisLabels.forEach((axis) => {
        if (Math.abs(axis.y - (tagY + TAG_H / 2)) < TAG_H) axis.node.remove();
      });
      const tagX = plotWidth + 2;
      const tagW = PRICE_LANE - 4;
      labelGroup.append(svgElement("rect", {
        class: `chart-price-bg${item.emphasis ? " is-emphasis" : ""}`,
        x: tagX, y: tagY, width: tagW, height: TAG_H, rx: 2,
        fill: item.solid || item.emphasis ? item.color : "var(--chart-bg)",
        stroke: item.color, "stroke-width": item.solid || item.emphasis ? 0 : 1,
        opacity: item.solid || item.emphasis ? 1 : .95
      }));
      const priceText = svgElement("text", {
        class: `chart-price-tag${item.emphasis ? " is-emphasis" : ""}`,
        x: tagX + tagW / 2,
        y: tagY + TAG_H / 2 + 3.5,
        fill: item.solid || item.emphasis ? "#0b0c0e" : item.color,
        "text-anchor": "middle",
        "font-size": LABEL_SIZE,
        "font-family": "var(--mono)"
      });
      priceText.textContent = priceLabel(item.price);
      labelGroup.append(priceText);

      // 左端の名前ピル。等幅で詰め、線の上に浮かせる。
      const label = fitText(shortName(item.name), Math.max(48, plotWidth * 0.34));
      if (label) {
        const nameY = Math.max(plotTop + NAME_SIZE + 2,
          Math.min(plotBottom - 2, (nameYFor.get(item) ?? item.labelY) + NAME_SIZE / 2 - 1));
        const pillW = Math.ceil(textWidth(label)) + 8;
        labelGroup.append(svgElement("rect", {
          class: "chart-name-bg",
          x: 2, y: nameY - NAME_SIZE - 1, width: pillW, height: NAME_SIZE + 4, rx: 2,
          fill: "var(--chart-bg)", opacity: ".82"
        }));
        const nameText = svgElement("text", {
          class: `chart-name-tag${item.emphasis ? " is-emphasis" : ""}`,
          x: 6, y: nameY,
          fill: item.nameColor || item.color,
          "font-size": NAME_SIZE, "font-family": "var(--mono)",
          opacity: item.emphasis ? ".95" : ".78"
        });
        nameText.textContent = label;
        labelGroup.append(nameText);
      }
    });
    chartRoot.append(labelGroup);

    svg.append(chartRoot);
    // 十字カーソルは常に最前面。再描画で消えた分は次のポインタ移動で戻る。
    while (crosshair.firstChild) crosshair.removeChild(crosshair.firstChild);
    svg.append(crosshair);
    layout = { bars, slots, xSlot, y, plotTop, plotBottom, plotWidth, slotW, scale, height, lastBar, prevBar };
    setHud(lastBar, prevBar);
    mountEl.classList.toggle("has-armed", Boolean(normalizedArmed));
    const truncation = Number.isFinite(Number(snapshot.barsTruncatedFrom)) && snapshot.barsTruncatedFrom > bars.length
      ? `BAR WINDOW ${bars.length}/${snapshot.barsTruncatedFrom}` : "";
    setStatus([freshnessState.label, truncation].filter(Boolean).join(" · "), freshnessState.stale ? "stale" : "");
  };

  const update = (data) => {
    const next = normalizeMarketSnapshot(data);
    snapshot = next;
    if (!next) {
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      mountEl.classList.remove("has-armed", "is-stale");
      layout = null;
      setHud(null);
      setStatus("NO FEED", "no-feed");
      clearIce();
      return;
    }
    render();
  };

  const fetchMarket = async () => {
    if (!autoPoll || destroyed || document.visibilityState !== "visible") return;
    try {
      const response = await fetch("./market.json", { cache: "no-store" });
      if (!response.ok) throw new Error(`market.json ${response.status}`);
      update(await response.json());
    } catch {
      // ポーリングの失敗で、すでに update() で渡された有効なデータを捨てない。
      // デモ(market.json 不在)で 30 秒ごとに NO FEED へ落ちていた原因。
      // データが一度も無いときだけ NO FEED を出す。
      if (!snapshot) update(null);
    }
  };

  const arm = (scenario) => {
    armed = scenario || null;
    render();
  };

  const disarm = () => {
    armed = null;
    render();
  };

  const onVisibilityChange = () => {
    if (document.visibilityState === "visible") fetchMarket();
  };
  document.addEventListener("visibilitychange", onVisibilityChange);
  const poll = autoPoll ? window.setInterval(fetchMarket, POLL_MS) : null;

  // 十字カーソル(R56)。ポインタ/タッチの位置を SVG 座標へ落とす。表示専用。
  const onPointer = (event) => {
    if (!layout || typeof svg.getBoundingClientRect !== "function") return;
    const box = svg.getBoundingClientRect();
    if (!box.width || !box.height) return;
    const px = (event.clientX - box.left) * (layout.plotWidth + PRICE_LANE) / box.width;
    const py = (event.clientY - box.top) * layout.height / box.height;
    drawCrosshair(px, py);
  };
  const canListen = typeof svg.addEventListener === "function";
  if (canListen) {
    svg.addEventListener("pointermove", onPointer);
    svg.addEventListener("pointerdown", onPointer);
    svg.addEventListener("pointerleave", clearCrosshair);
    svg.addEventListener("pointercancel", clearCrosshair);
  }

  if (typeof ResizeObserver === "function") {
    resizeObserver = new ResizeObserver(render);
    resizeObserver.observe(mountEl);
  } else {
    window.addEventListener("resize", render);
  }
  fetchMarket();

  return {
    update,
    arm,
    disarm,
    /** 置き場の要素。app.js が描画のたびにカードを作り直しても、同じチャートを差し戻して使い回す。 */
    mount: mountEl,
    destroy() {
      destroyed = true;
      if (ice3d) ice3d.destroy();
      if (poll !== null) window.clearInterval(poll);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      if (canListen) {
        svg.removeEventListener("pointermove", onPointer);
        svg.removeEventListener("pointerdown", onPointer);
        svg.removeEventListener("pointerleave", clearCrosshair);
        svg.removeEventListener("pointercancel", clearCrosshair);
      }
      resizeObserver?.disconnect();
      if (!resizeObserver) window.removeEventListener("resize", render);
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      status.remove();
      hud.remove();
      svg.remove();
    }
  };
}
