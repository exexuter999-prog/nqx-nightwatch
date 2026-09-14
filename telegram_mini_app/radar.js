/**
 * RADAR — 市場レーダー(R58, 2026-09-05)。
 *
 * R56 の「チャート下の凡例」(VP / VWAP / HTF / CVD / ICT / LIQ / 15M / GATE の 8 行)は
 * 指標の**出所**ごとに値を並べていたので、価格・方向・数量・状態が一行に混ざり、
 * 30,0xx.xx が数十個並ぶ壁になっていた(2026-09-05 ユーザー: ごちゃっててややこしい)。
 * ここでは同じ値を**問いの種類**で組み直し、専用タブに置く。
 *
 *   PRICE MAP : 価格を持つもの全部を 1 本の梯子に(現在値との距離で読む)
 *   BIAS      : 向きを持つもの全部をチップの列に(▲ / ▼ の並びで読む)
 *   FLOW      : 価格ではない計測値(CVD・ATR・ノイズ・レジーム)をタイルに
 *   CLOCK     : 時間の位相(キルゾーン・QT・イベント)
 *   SETUP     : MSNR 連鎖と最有力モデル(結論の一行)
 *
 * 全部 market(Worker の view)から**読むだけ**。判定も再計算もしない —— 出す数字は
 * サーバーが publish したものそのもので、距離(pt)は現在値との引き算だけ。
 * DOM を触るのは renderRadar だけなので、buildRadar は node --test から読める。
 */

const TICK = 0.25;
/** TradingView の CVD study は欠損を 59999 で埋めることがある。数値としては使わない。 */
export const CVD_SENTINEL = 59999;
/** 梯子は上下それぞれこの本数まで(それ以上は遠い順に落とす)。 */
export const LADDER_MAX_PER_SIDE = 24;

const finite = (value) => value !== null && value !== undefined && value !== ""
  && typeof value !== "boolean" && Number.isFinite(Number(value));
const num = (value) => (finite(value) ? Number(value) : null);
const priceText = (value) => Number(value).toLocaleString("en-US",
  { minimumFractionDigits: 2, maximumFractionDigits: 2 });
// 負号は他の画面と同じ U+2212(ハイフンでは数字の列で見失う)。
const intText = (value) => `${Number(value) < 0 ? "−" : ""}${Math.abs(Math.round(Number(value))).toLocaleString("en-US")}`;
const signed = (value, digits = 1) => `${Number(value) < 0 ? "−" : "+"}${Math.abs(Number(value)).toFixed(digits)}`;

/** 契約の識別子(TARGET_HEADROOM_INSUFFICIENT)を読める語へ。意味は変えず区切りだけ空ける。 */
export function humanize(id) {
  return String(id ?? "").trim().replace(/_+/g, " ").replace(/\s+/g, " ").toUpperCase();
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]
  ));
}

function arrow(value) {
  const parsed = num(value);
  if (parsed === null) return "·";
  return parsed > 0 ? "▲" : parsed < 0 ? "▼" : "—";
}

const toneOfSide = (side) => (side === "BUY" || side === "BULLISH" || side === "UP" ? "is-up"
  : side === "SELL" || side === "BEARISH" || side === "DOWN" ? "is-down" : "");
const markOfSide = (side) => (toneOfSide(side) === "is-up" ? "▲" : toneOfSide(side) === "is-down" ? "▼" : "—");

// ---------------------------------------------------------------- PRICE MAP

/** publish される水準名 → 表示名と種別。無いものは名前をそのまま大文字で出す。 */
const LEVEL_NAMES = {
  "C: POC": ["POC", "vp"], "C: VAH": ["VAH", "vp"], "C: VAL": ["VAL", "vp"],
  "P: POC": ["P.POC", "vp"], "P: VAH": ["P.VAH", "vp"], "P: VAL": ["P.VAL", "vp"],
  "WEEKLY OPEN": ["WK OPEN", "open"], "6PM OPEN": ["6PM OPEN", "open"], "MIDNIGHT OPEN": ["00:00 OPEN", "open"],
  "SESSION VWAP": ["VWAP", "vwap"], "VWAP HI": ["VWAP HI", "vwap"], "VWAP LO": ["VWAP LO", "vwap"],
};
/** 同じ価格に重なったとき、どの種別の色で出すか(先勝ち)。 */
const KIND_RANK = ["plan", "warn", "vp", "liq", "vwap", "swing", "zone", "open", "named", "geo", "htf"];
const FRAME_ORDER = ["1d", "4h", "1h", "45m"];

const sameTick = (a, b) => a !== null && b !== null && Math.abs(a - b) < TICK - 1e-9;

/** market が持つ「価格」を全部集める。判定はしない —— あるものを名前付きで並べるだけ。 */
export function collectLevels(market) {
  const out = [];
  const add = (name, kind, price, extra = {}) => {
    const parsed = num(price);
    if (parsed !== null) out.push({ name, kind, price: parsed, tags: [], ...extra });
  };
  const zone = (name, kind, lo, hi, extra = {}) => {
    const a = num(lo);
    const b = num(hi);
    if (a === null || b === null) return;
    out.push({ name, kind, lo: Math.min(a, b), hi: Math.max(a, b), price: (a + b) / 2, zone: true, tags: [], ...extra });
  };

  for (const level of Array.isArray(market.levels) ? market.levels : []) {
    const label = String(level?.label ?? level?.name ?? "").trim();
    if (!label || label.toUpperCase().startsWith("WIN ")) continue;   // チャート内部の窓高安
    const known = LEVEL_NAMES[label.toUpperCase()];
    add(known ? known[0] : label.toUpperCase(), known ? known[1] : "named", level?.price);
  }

  const ind = market.indicators || {};
  const models = market.strategyEvidence?.models || {};
  // VWAP 系はチャートの水準リスト(levels)にも同名で載ることがある。載っていれば
  // そちら(チャートに描かれている値)を正とし、indicators 側は重ねて出さない。
  const named = new Set(out.map((l) => l.name));
  const addUnlessNamed = (name, kind, price) => { if (!named.has(name)) add(name, kind, price); };
  addUnlessNamed("VWAP", "vwap", market.vwap);
  addUnlessNamed("VWAP HI", "vwap", ind.vwapHi);
  addUnlessNamed("VWAP LO", "vwap", ind.vwapLo);
  add("EMA", "vwap", models.vwapReversion?.ema);
  const frames = models.htf?.frames || {};
  for (const key of FRAME_ORDER) add(`EMA20 ${key.toUpperCase()}`, "htf", frames[key]?.ema20);

  const liq = models.liquidity || {};
  const dol = liq.dol || {};
  const dolBuy = num(dol.BUY);
  const dolSell = num(dol.SELL);
  let dolBuySeen = false;
  let dolSellSeen = false;
  for (const pool of Array.isArray(liq.pools) ? liq.pools : []) {
    if (!pool || pool.kind === "NAMED_LEVEL") continue;
    const price = num(pool.price);
    if (price === null || (pool.side !== "BSL" && pool.side !== "SSL")) continue;
    const tags = [];
    if (pool.side === "BSL" && sameTick(price, dolBuy)) { tags.push("DOL"); dolBuySeen = true; }
    if (pool.side === "SSL" && sameTick(price, dolSell)) { tags.push("DOL"); dolSellSeen = true; }
    add(pool.side, "liq", price, { tags });
  }
  if (dolBuy !== null && !dolBuySeen) add("DOL ▲", "liq", dolBuy);
  if (dolSell !== null && !dolSellSeen) add("DOL ▼", "liq", dolSell);

  const fvg = models.ict?.fvg || {};
  for (const side of ["BULL", "BEAR"]) {
    for (const gap of Array.isArray(fvg[side]) ? fvg[side] : []) {
      const age = num(gap?.ageBars);
      zone(`FVG ${side === "BULL" ? "▲" : "▼"}${age !== null ? ` ${age}b` : ""}`, "zone", gap?.lo, gap?.hi);
    }
  }
  const ifvg = models.ifvg || {};
  for (const z of Array.isArray(ifvg.zones) ? ifvg.zones : []) {
    if (z?.active === false) continue;
    zone(`IFVG ${z?.direction || ifvg.direction || ""}`.trim(), "zone", z?.lo, z?.hi);
  }
  const crt = models.crt || {};
  zone("CRT", "zone", crt.rangeLow, crt.rangeHigh);

  const gann = Array.isArray(models.gann?.angles) ? models.gann.angles.find((a) => a?.primary) : null;
  if (gann) add("GANN 1x1", "geo", gann.price);

  const ct = ind.ct || {};
  add("15M 61.8", "swing", ct.fib618);
  add("15M TRAIL", "swing", ct.trail);
  add("LTF 61.8", "swing", ct.ltfFib618);
  add("HTF 61.8", "swing", ct.htfFib618);
  add("HARD SL", "warn", ct.hardStop);

  const msnr = market.evaluation?.msnr;
  if (msnr && num(msnr.price) !== null) {
    const known = LEVEL_NAMES[String(msnr.label || "").toUpperCase()];
    add(known ? known[0] : humanize(msnr.label || "MSNR"), known ? known[1] : "named", msnr.price, { tags: ["MSNR"] });
  }

  const d = market.evaluation?.decision || {};
  if (["ARMED", "ACTIVE"].includes(String(d.state || "").toUpperCase())) {
    add("ENTRY", "plan", d.entry);
    add("SL", "plan", d.stop);
    (Array.isArray(d.targets) ? d.targets : []).forEach((t, i) => add(`TP${i + 1}`, "plan", t));
  }
  return out;
}

/** 1 tick 以内に重なった点水準を 1 行へ(名前は「·」で連結、タグは合流)。ゾーンは畳まない。 */
export function mergeLevels(levels) {
  const points = levels.filter((l) => !l.zone).sort((a, b) => a.price - b.price);
  const merged = [];
  for (const level of points) {
    const prev = merged[merged.length - 1];
    if (prev && sameTick(prev.price, level.price)) {
      if (!prev.names.includes(level.name)) prev.names.push(level.name);
      for (const tag of level.tags) if (!prev.tags.includes(tag)) prev.tags.push(tag);
      if (KIND_RANK.indexOf(level.kind) < KIND_RANK.indexOf(prev.kind)) prev.kind = level.kind;
      continue;
    }
    merged.push({ ...level, names: [level.name], tags: [...level.tags] });
  }
  return [...merged, ...levels.filter((l) => l.zone).map((l) => ({ ...l, names: [l.name], tags: [...l.tags] }))];
}

/**
 * 現在値を挟んだ梯子。上も下も価格の降順(上端が最高値)なので、上から下へ
 * 読むとチャートの縦軸と同じ向きになる。dist は現在値との差(ゾーンは近い縁まで、
 * 中なら 0)。bar は |dist| を梯子内の最大で割った 0〜1。
 */
export function buildLadder(market) {
  const last = num(market?.price);
  if (last === null) return { last: null, above: [], below: [], total: 0 };
  const rows = mergeLevels(collectLevels(market)).map((level) => {
    const dist = level.zone
      ? (last < level.lo ? level.lo - last : last > level.hi ? level.hi - last : 0)
      : level.price - last;
    const side = level.zone ? (level.price >= last ? "above" : "below") : (dist >= 0 ? "above" : "below");
    return { ...level, dist, inside: level.zone && dist === 0, side };
  });
  const byPriceDesc = (a, b) => b.price - a.price;
  const nearest = (list) => [...list].sort((a, b) => Math.abs(a.dist) - Math.abs(b.dist))
    .slice(0, LADDER_MAX_PER_SIDE).sort(byPriceDesc);
  const above = nearest(rows.filter((r) => r.side === "above"));
  const below = nearest(rows.filter((r) => r.side === "below"));
  // 帯の尺度は日足・4時間足の EMA20(数百 pt 先)を除いて決める。入れると近い水準の帯が
  // 全部 1〜2% に潰れて読めない。除いた行は帯が 100% で止まる。
  const scaled = [...above, ...below].filter((r) => r.kind !== "htf");
  const maxDist = Math.max(1, ...scaled.map((r) => Math.abs(r.dist)));
  const finish = (r) => ({ ...r, bar: Math.min(1, Math.abs(r.dist) / maxDist) });
  return { last, above: above.map(finish), below: below.map(finish), total: rows.length };
}

// ---------------------------------------------------------------- BIAS

/** チップは 4 列(1 列 ≈ 60px)なので語は 7 字前後まで。契約語の短縮表(意味は変えない)。 */
const SHORT_WORDS = {
  BULLISH: "BULL", BEARISH: "BEAR", MIXED: "MIX",
  CONFIRMED: "CONF", ACCEPTED: "ACCEPT", REJECTED: "REJECT",
  MANIPULATION: "MANIP", ACCUMULATION: "ACCUM", DISTRIBUTION: "DISTRIB",
};
const shortWord = (word) => {
  const s = humanize(word);
  return s.split(" ").map((w) => SHORT_WORDS[w] || w).join(" ");
};
const shortStructure = (structure) => {
  const s = String(structure || "").toUpperCase();
  return s ? shortWord(s) : "?";
};

/** 向きを持つものだけ。chip = { label, mark, word, tone, title? } */
export function buildBias(market) {
  const chips = [];
  const ind = market.indicators || {};
  const models = market.strategyEvidence?.models || {};
  const ev = market.evaluation || {};

  const frames = models.htf?.frames || {};
  for (const key of FRAME_ORDER) {
    const frame = frames[key];
    if (!frame || typeof frame !== "object") continue;
    const structure = String(frame.structure || frame.basis || "").toUpperCase();
    chips.push({ label: key.toUpperCase(), mark: arrow(frame.emaSlope5), word: shortStructure(structure),
      tone: toneOfSide(structure), title: num(frame.ema20) !== null ? `EMA20 ${priceText(frame.ema20)}` : "" });
  }
  if (models.htf?.bias) chips.push({ label: "HTF", mark: markOfSide(models.htf.bias), word: String(models.htf.bias), tone: toneOfSide(models.htf.bias) });

  const vwap = ev.advisory?.vwap;
  if (vwap?.state) {
    chips.push({ label: "VWAP", mark: markOfSide(vwap.side), word: shortWord(String(vwap.state).replace(/^VWAP_/, "")), tone: toneOfSide(vwap.side) });
  }
  const cvd = ind.cvd || {};
  if (cvd.bias) chips.push({ label: "CVD", mark: markOfSide(cvd.bias), word: shortStructure(cvd.bias), tone: toneOfSide(cvd.bias) });
  const smt = ind.smt;
  if (smt?.bias) {
    const agree = smt.agree === true ? " ✓" : smt.agree === false ? " ✗" : "";
    chips.push({ label: "SMT", mark: markOfSide(smt.bias), word: `${shortStructure(smt.bias)}${agree}`, tone: toneOfSide(smt.bias), title: smt.peer || "" });
  }
  if (models.mmxm?.mss) chips.push({ label: "MSS", mark: markOfSide(models.mmxm.mss), word: shortStructure(models.mmxm.mss), tone: toneOfSide(models.mmxm.mss) });
  const ifvg = models.ifvg;
  if (ifvg?.status && ifvg.status !== "MISSING") {
    // 向きは矢印と色が持つので、語は状態だけ(BUY CONFIRMED → ▲ CONF)。
    chips.push({ label: "IFVG", mark: markOfSide(ifvg.direction), word: shortWord(ifvg.status), tone: toneOfSide(ifvg.direction), title: `${ifvg.direction || ""} ${humanize(ifvg.status)}`.trim() });
  }
  if (ind.po3) {
    const raw = String(ind.po3).toUpperCase();
    const dir = raw.endsWith("_UP") ? "UP" : raw.endsWith("_DOWN") ? "DOWN" : "";
    const phase = raw.replace(/_(UP|DOWN)$/, "");
    chips.push({ label: "PO3", mark: markOfSide(dir), word: shortWord(phase), tone: toneOfSide(dir), title: humanize(raw) });
  }
  const ct = ind.ct || {};
  if (num(ct.trend) !== null) chips.push({ label: "15M CT", mark: arrow(ct.trend), word: "", tone: Number(ct.trend) > 0 ? "is-up" : Number(ct.trend) < 0 ? "is-down" : "" });
  if (num(ct.ltfTrend) !== null) chips.push({ label: "LTF", mark: arrow(ct.ltfTrend), word: "", tone: Number(ct.ltfTrend) > 0 ? "is-up" : Number(ct.ltfTrend) < 0 ? "is-down" : "" });
  // レジーム(MX など)は向きではなく相場の型だが、「今どういう場か」の札としてここに置く。
  if (market.regime) chips.push({ label: "REGIME", mark: "", word: String(market.regime), tone: "" });
  return chips;
}

// ---------------------------------------------------------------- FLOW / CLOCK

/** 履歴のスパークライン用の系列。センチネルは欠損として落とす。 */
export function cvdSeries(history) {
  return (Array.isArray(history) ? history : [])
    .map(num)
    .filter((value) => value !== null && Math.abs(value) !== CVD_SENTINEL);
}

/** 価格ではない計測値。tile = { label, value, sub?, tone?, spark? } */
export function buildFlow(market) {
  const tiles = [];
  const ind = market.indicators || {};
  const models = market.strategyEvidence?.models || {};
  const ev = market.evaluation || {};
  const cvd = ind.cvd || {};
  const cvdValue = num(cvd.value) ?? num(market.cvd);
  if (cvdValue !== null) {
    const gate = ev.cvdGate?.status;
    tiles.push({ label: "CVD", value: intText(cvdValue), tone: cvdValue > 0 ? "is-up" : cvdValue < 0 ? "is-down" : "",
      sub: gate ? String(gate) : "", subTone: gate && gate !== "FRESH" ? "is-warn" : "", spark: cvdSeries(cvd.history) });
  }
  if (num(cvd.fast) !== null) tiles.push({ label: "CVD FAST", value: intText(cvd.fast), tone: "" });
  if (num(cvd.slow) !== null) tiles.push({ label: "CVD SLOW", value: intText(cvd.slow), tone: "" });
  const vwap = num(market.vwap);
  const last = num(market.price);
  if (vwap !== null && last !== null) {
    const diff = last - vwap;
    tiles.push({ label: "Δ VWAP", value: `${signed(diff)}pt`, tone: diff >= 0 ? "is-up" : "is-down" });
  }
  if (num(models.vwapReversion?.atr) !== null) tiles.push({ label: "ATR 3M", value: Number(models.vwapReversion.atr).toFixed(1), tone: "" });
  if (num(ind.ct?.atr) !== null) tiles.push({ label: "ATR 15M", value: Number(ind.ct.atr).toFixed(1), tone: "" });
  const vol = ev.volGate || {};
  if (num(vol.noise) !== null) {
    tiles.push({ label: "NOISE", value: `${Number(vol.noise).toFixed(1)}pt`, tone: vol.ruling === "停止" ? "is-warn" : "",
      sub: num(vol.ratio) !== null ? `×${Number(vol.ratio).toFixed(2)}` : "" });
  }
  return tiles;
}

/** 時間の位相。tile と同じ形。 */
export function buildClock(market) {
  const tiles = [];
  const ev = market.evaluation || {};
  const session = ev.sessionGate;
  if (session?.window) {
    tiles.push({ label: "KILLZONE", value: humanize(session.window), sub: session.et ? `${session.et} ET` : "",
      tone: session.tradeable ? "is-up" : "" });
  }
  const qt = market.strategyEvidence?.models?.quarterly;
  if (qt?.stage) tiles.push({ label: "QUARTER", value: String(qt.stage), sub: num(qt.quarter) !== null ? `q${qt.quarter}` : "", tone: "" });
  const event = market.indicators?.event;
  if (event && event !== "NONE") tiles.push({ label: "EVENT", value: humanize(event), tone: "is-warn" });
  return tiles;
}

// ---------------------------------------------------------------- SETUP

/** 結論の二行。row = { key, text, tone, blockers[] } */
export function buildSetup(market) {
  const rows = [];
  const ev = market.evaluation || {};
  const msnr = ev.msnr;
  if (msnr && (msnr.label || num(msnr.price) !== null)) {
    const parts = [msnr.label, msnr.chainType && humanize(msnr.chainType), msnr.chainState && humanize(msnr.chainState)].filter(Boolean);
    if (num(msnr.barsLeft) !== null) parts.push(`${msnr.barsLeft}b`);
    const blockers = Array.isArray(msnr.blockers) ? msnr.blockers.map(humanize) : [];
    rows.push({ key: "MSNR", text: parts.join(" · "), tone: msnr.allowed ? "is-up" : blockers.length ? "is-warn" : "", blockers });
  }
  const d = ev.decision || {};
  if (d.model) {
    const state = String(d.state || "").toUpperCase();
    const armed = ["ARMED", "ACTIVE"].includes(state);
    const blockers = Array.isArray(d.hardBlockers) ? d.hardBlockers.map(humanize) : [];
    rows.push({ key: "MODEL", text: [humanize(d.model), d.grade, state].filter(Boolean).join(" · "),
      tone: armed ? "is-up" : blockers.length ? "is-warn" : "", blockers });
  }
  return rows;
}

// ---------------------------------------------------------------- 全体

/** market が無ければ null(描画側は「未確認」を出す)。 */
export function buildRadar(market) {
  if (!market || typeof market !== "object") return null;
  return {
    ladder: buildLadder(market),
    bias: buildBias(market),
    flow: buildFlow(market),
    clock: buildClock(market),
    setup: buildSetup(market),
  };
}

// ---------------------------------------------------------------- 描画

/** 12 点ほどの履歴を 64×14 のスパークラインにする。2 点未満なら描かない。 */
export function sparkline(series, width = 64, height = 14) {
  if (!Array.isArray(series) || series.length < 2) return "";
  const min = Math.min(...series);
  const max = Math.max(...series);
  const span = max - min || 1;
  const x = (i) => (i / (series.length - 1)) * (width - 2) + 1;
  const y = (v) => height - 1 - ((v - min) / span) * (height - 2);
  const points = series.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const last = series[series.length - 1];
  const first = series[0];
  const tone = last > first ? "is-up" : last < first ? "is-down" : "";
  return `<svg class="rd-spark ${tone}" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" aria-hidden="true">`
    + `<polyline points="${points}" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/>`
    + `<circle cx="${x(series.length - 1).toFixed(1)}" cy="${y(last).toFixed(1)}" r="1.6" fill="currentColor"/>`
    + `</svg>`;
}

function levelHtml(row, index) {
  const names = row.names.slice(0, 3).join(" · ") + (row.names.length > 3 ? ` +${row.names.length - 3}` : "");
  const priceLabel = row.zone ? `${priceText(row.lo)}–${priceText(row.hi)}` : priceText(row.price);
  const dist = row.inside ? "INSIDE" : signed(row.dist);
  return `<li class="rd-lv kind-${esc(row.kind)} side-${esc(row.side)}${row.inside ? " is-inside" : ""}" style="--w:${(row.bar * 100).toFixed(1)}%;--i:${index}">`
    + `<span class="rd-lv-name">${esc(names)}${row.tags.map((t) => `<small>${esc(t)}</small>`).join("")}</span>`
    + `<span class="rd-lv-price">${esc(priceLabel)}</span>`
    + `<b class="rd-lv-dist">${esc(dist)}</b>`
    + `</li>`;
}

function tileHtml(tile) {
  const wide = tile.spark && tile.spark.length >= 2 ? " rd-tile-wide" : "";
  return `<div class="rd-tile ${esc(tile.tone || "")}${wide}">`
    + `<i>${esc(tile.label)}</i>`
    + `<b>${esc(tile.value)}${tile.spark && tile.spark.length >= 2 ? sparkline(tile.spark) : ""}</b>`
    + (tile.sub ? `<small class="${esc(tile.subTone || "")}">${esc(tile.sub)}</small>` : "")
    + `</div>`;
}

function cardHtml(title, aside, body, extraClass = "") {
  return `<section class="rd-card ${extraClass}">`
    + `<header class="rd-card-head"><span class="micro">${esc(title)}</span><span class="micro rd-card-aside">${esc(aside)}</span></header>`
    + body
    + `</section>`;
}

/** host にレーダーを描く。model が null なら「未確認」の一枚だけ。 */
export function renderRadar(host, model) {
  if (!host) return;
  const fresh = !host.dataset.built;
  host.dataset.built = "1";
  host.classList.toggle("is-fresh", fresh);
  if (!model) {
    host.innerHTML = `<section class="rd-card rd-empty"><p class="micro">NO VERIFIED MARKET · waiting for a fresh feed</p></section>`;
    return;
  }
  const { ladder, bias, flow, clock, setup } = model;
  const parts = [];

  if (ladder.last !== null && (ladder.above.length || ladder.below.length)) {
    let index = 0;
    const body = `<ol class="rd-ladder">`
      + ladder.above.map((row) => levelHtml(row, index++)).join("")
      + `<li class="rd-last" style="--i:${index++}"><span class="rd-last-tag">LAST</span><b>${esc(priceText(ladder.last))}</b><span class="rd-last-line" aria-hidden="true"></span></li>`
      + ladder.below.map((row) => levelHtml(row, index++)).join("")
      + `</ol>`;
    parts.push(cardHtml("PRICE MAP", `▲ ${ladder.above.length} · ▼ ${ladder.below.length} · pt from last`, body, "rd-card-ladder"));
  }
  if (bias.length) {
    const body = `<div class="rd-chips">${bias.map((chip) =>
      `<span class="rd-chip ${esc(chip.tone)}"${chip.title ? ` title="${esc(chip.title)}"` : ""}><i>${esc(chip.label)}</i><b>${esc([chip.mark, chip.word].filter(Boolean).join(" "))}</b></span>`).join("")}</div>`;
    parts.push(cardHtml("BIAS", `${bias.filter((c) => c.tone === "is-up").length} ▲ · ${bias.filter((c) => c.tone === "is-down").length} ▼`, body));
  }
  if (flow.length) parts.push(cardHtml("FLOW", "", `<div class="rd-tiles">${flow.map(tileHtml).join("")}</div>`));
  if (clock.length) parts.push(cardHtml("CLOCK", "", `<div class="rd-tiles rd-tiles-3">${clock.map(tileHtml).join("")}</div>`));
  if (setup.length) {
    const body = `<div class="rd-setup">${setup.map((row) =>
      `<div class="rd-setup-row ${esc(row.tone)}"><i>${esc(row.key)}</i><b>${esc(row.text)}</b>`
      + (row.blockers.length ? `<span class="rd-blockers">${row.blockers.map((b) => `<em>✗ ${esc(b)}</em>`).join("")}</span>` : "")
      + `</div>`).join("")}</div>`;
    parts.push(cardHtml("SETUP", "", body));
  }
  host.innerHTML = parts.length ? parts.join("")
    : `<section class="rd-card rd-empty"><p class="micro">NO INDICATORS IN THIS FEED</p></section>`;
}
