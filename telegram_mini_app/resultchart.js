/**
 * NQX RESULT CHART — 根拠チャート(R57)。
 *
 * result.chart(建玉前後の確定 3 分足・VP 水準・凍結ターゲット・根拠タグ・減点・HTF)を、
 * 「なぜ入って、どこで守り、どこを狙い、何が起きたか」が一枚で読める形に描く。
 *
 *   buildChartModel(trade)            純関数。座標系・保有中の MAE/MFE・決済後の値動き・
 *                                     決済の種類・目盛りを導出する(テストはここだけ)。
 *   drawTradeChart(ctx, model, box)   Canvas 描画。画面(hero)と書き出しカードの両方で使う。
 *
 * 価格は全て result.chart / trade に実在した値。ここでは何も発明しない。
 * 足が無い記録(旧 result)は null を返し、呼び出し側は従来の path 描画に落ちる。
 */

const BAR_MIN = 4;
export const INK = {
  bone: '#ECE9DF', ash: '#8C93A0', rust: '#FF2E4C',
  levelC: '#F0A500', levelP: '#B088F9', tp: '#3DD68C', exit: '#FFD166',
};

const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : null);
const epoch = (iso) => { const t = Date.parse(iso); return Number.isFinite(t) ? t / 1000 : null; };

export function classifyExit(side, entry, exit, stop, tp1, tp2) {
  if (exit === null || entry === null) return { kind: 'UNKNOWN', slip: null };
  const short = side === 'SHORT';
  if (stop !== null) {
    const adverse = short ? exit >= stop - 0.5 : exit <= stop + 0.5;
    if (adverse && Math.abs(exit - stop) <= 3) {
      const slip = short ? exit - stop : stop - exit;
      return { kind: 'SL', slip: Math.max(0, Math.round(slip * 100) / 100) };
    }
  }
  if (tp1 !== null && Math.abs(exit - tp1) <= 0.5) return { kind: 'TP1', slip: null };
  if (tp2 !== null && Math.abs(exit - tp2) <= 0.5) return { kind: 'TP2', slip: null };
  if (tp1 !== null) {
    const between = short ? (tp1 < exit && exit < entry) : (entry < exit && exit < tp1);
    const beyond = short ? exit < tp1 : exit > tp1;
    if (between || beyond) return { kind: 'PARTIAL', slip: null };
  }
  return { kind: 'MANUAL', slip: null };
}

function niceStep(span) {
  for (const step of [1, 2.5, 5, 10, 25, 50, 100, 250, 500]) if (span / step <= 7) return step;
  return 1000;
}

export function fmtTime(sec) {
  const d = new Date(sec * 1000);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/**
 * @param {object} trade  result.js の trade 契約(+ chart / model / grade)
 * @returns {object|null}
 */
export function buildChartModel(trade) {
  const chart = trade?.chart;
  if (!chart || !Array.isArray(chart.bars) || chart.bars.length < BAR_MIN) return null;
  const tf = Number(chart.tf) || 180;
  const bars = chart.bars
    .map((row) => (Array.isArray(row)
      ? { t: Number(row[0]), o: Number(row[1]), h: Number(row[2]), l: Number(row[3]), c: Number(row[4]) }
      : { t: Number(row.t), o: Number(row.o), h: Number(row.h), l: Number(row.l), c: Number(row.c) }))
    .filter((b) => [b.t, b.o, b.h, b.l, b.c].every(Number.isFinite))
    .sort((a, b) => a.t - b.t);
  if (bars.length < BAR_MIN) return null;

  const side = String(trade.side || '').toUpperCase() === 'SHORT' ? 'SHORT' : 'LONG';
  const dir = side === 'SHORT' ? -1 : 1;
  const entry = num(trade.entry), exit = num(trade.exit), stop = num(trade.stop);
  const tp1 = num(chart.tp1), tp2 = num(chart.tp2);
  const entryT = epoch(trade.openedAt), exitT = epoch(trade.closedAt);
  const qty = Number(trade.qty) || 0, pointValue = Number(trade.pointValue) || 2;

  const t0 = bars[0].t, t1 = bars[bars.length - 1].t + tf;
  const barLo = Math.min(...bars.map((b) => b.l)), barHi = Math.max(...bars.map((b) => b.h));
  const anchors = [entry, exit, stop, tp1].filter((v) => v !== null);
  let lo = Math.min(barLo, ...anchors), hi = Math.max(barHi, ...anchors);
  const barSpan = barHi - barLo;
  // TP2 は「枠を足の値幅の 60% 以上広げない」ときだけ枠内に入れる。遠い TP2 を入れると
  // ローソクが潰れて根拠(足の形)が読めなくなる。枠外なら矢印ラベルで示す。
  const tp2Growth = tp2 === null ? Infinity : (Math.max(hi, tp2) - Math.min(lo, tp2)) - (hi - lo);
  const tp2Visible = tp2 !== null && tp2Growth <= Math.max(barSpan * 0.6, 8);
  if (tp2Visible) { lo = Math.min(lo, tp2); hi = Math.max(hi, tp2); }
  const pad = Math.max((hi - lo) * 0.10, 2);
  lo -= pad; hi += pad;

  // 保有中の MAE / MFE(建玉時刻が無ければ出さない)
  const held = (entryT !== null && exitT !== null)
    ? bars.filter((b) => b.t + tf > entryT && b.t < exitT) : [];
  let mae = null, mfe = null;
  if (held.length && entry !== null) {
    let worst = null, best = null;
    for (const b of held) {
      const adverse = side === 'SHORT' ? b.h - entry : entry - b.l;
      const favorable = side === 'SHORT' ? entry - b.l : b.h - entry;
      if (worst === null || adverse > worst.pt) worst = { pt: adverse, price: side === 'SHORT' ? b.h : b.l, t: b.t };
      if (best === null || favorable > best.pt) best = { pt: favorable, price: side === 'SHORT' ? b.l : b.h, t: b.t };
    }
    mae = { ...worst, pt: Math.max(0, Math.round(worst.pt * 100) / 100) };
    mfe = { ...best, pt: Math.max(0, Math.round(best.pt * 100) / 100) };
  }
  const riskPt = (stop !== null && entry !== null) ? Math.abs(entry - stop) : null;
  const rOf = (pt) => (riskPt ? Math.round((pt / riskPt) * 100) / 100 : null);

  // 決済後の値動き(残っている足だけ)
  let post = null;
  if (exitT !== null && exit !== null) {
    const after = bars.filter((b) => b.t >= exitT);
    if (after.length) {
      const fav = side === 'SHORT' ? exit - Math.min(...after.map((b) => b.l)) : Math.max(...after.map((b) => b.h)) - exit;
      const adv = side === 'SHORT' ? Math.max(...after.map((b) => b.h)) - exit : exit - Math.min(...after.map((b) => b.l));
      let tp1AfterMin = null, tp1Touch = null;
      if (tp1 !== null) {
        const hit = after.find((b) => (side === 'SHORT' ? b.l <= tp1 : b.h >= tp1));
        if (hit) { tp1AfterMin = Math.round((hit.t + tf - exitT) / 60); tp1Touch = hit.t + tf / 2; }
      }
      post = { favPt: Math.max(0, Math.round(fav * 100) / 100), advPt: Math.max(0, Math.round(adv * 100) / 100),
               tp1AfterMin, tp1Touch, minutes: Math.round((after[after.length - 1].t + tf - exitT) / 60) };
    }
  }

  const exitKind = classifyExit(side, entry, exit, stop, tp1, tp2);
  const pts = (entry !== null && exit !== null) ? (exit - entry) * dir : null;
  const usd = pts !== null ? pts * pointValue * qty : null;
  const mfeUsd = mfe ? mfe.pt * pointValue * qty : null;
  const efficiency = (usd !== null && mfeUsd) ? Math.round((usd / mfeUsd) * 100) / 100 : null;
  const holdMin = (entryT !== null && exitT !== null) ? Math.round((exitT - entryT) / 6) / 10 : null;

  const levels = (Array.isArray(chart.levels) ? chart.levels : [])
    .map((l) => ({ label: String(l.label || ''), price: num(l.price) }))
    .filter((l) => l.label && l.price !== null && l.price >= lo && l.price <= hi)
    .map((l) => ({ ...l, kind: l.label.startsWith('C') ? 'C' : l.label.startsWith('P') ? 'P' : 'other' }));

  const step = niceStep(hi - lo);
  const priceTicks = [];
  for (let p = Math.ceil(lo / step) * step; p <= hi; p += step) priceTicks.push(Math.round(p * 100) / 100);
  const timeTicks = [];
  for (let t = Math.ceil(t0 / 900) * 900; t <= t1; t += 900) timeTicks.push({ t, label: fmtTime(t) });

  return {
    tf, bars, t0, t1, lo, hi, side, dir, entry, exit, stop, tp1, tp2, tp2Visible,
    entryT, exitT, held: held.length, mae, mfe, maeR: mae ? rOf(mae.pt) : null, mfeR: mfe ? rOf(mfe.pt) : null,
    riskPt, post, exitKind: exitKind.kind, slip: exitKind.slip, efficiency, holdMin, pts, usd,
    levels, priceTicks, timeTicks,
    evidence: (chart.evidence || []).map(String), penalties: (chart.penalties || []).map(String),
    htf: chart.htf || {}, volRatio: num(chart.volRatio), noise: num(chart.noise),
    session: chart.session || null, model: trade.model || null, grade: trade.grade || null,
    source: chart.source || null,
  };
}

/** 座標変換。box = {x, y, w, h}。 */
export function scales(m, box) {
  const xOf = (t) => box.x + ((t - m.t0) / (m.t1 - m.t0)) * box.w;
  const yOf = (p) => box.y + ((m.hi - p) / (m.hi - m.lo)) * box.h;
  return { xOf, yOf };
}

const fmt = (n) => Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const short = (n) => Number(n).toLocaleString('en-US', { maximumFractionDigits: 2 });

/**
 * 根拠チャートを描く。
 * @param {CanvasRenderingContext2D} c
 * @param {object} m   buildChartModel の戻り
 * @param {object} box {x, y, w, h}(ローソク帯。ラベルは帯の内側に描く)
 * @param {object} o   {u, tint, flare(c,x,y), labels:true}
 */
export function drawTradeChart(c, m, box, o = {}) {
  const u = o.u || 1, tint = o.tint || INK.rust;
  const { xOf, yOf } = scales(m, box);
  const xEntry = m.entryT !== null ? Math.min(Math.max(xOf(m.entryT), box.x), box.x + box.w) : box.x;
  const xExit = m.exitT !== null ? Math.min(Math.max(xOf(m.exitT), box.x), box.x + box.w) : box.x + box.w;
  // 文字は u(=幅/1080)で縮むが、画面では読める下限(o.minPx = 10 css px 相当)を守る
  const minPx = o.minPx || 0;
  const fpx = (px) => Math.max(px * u, minPx);
  const mono = (px, w = 500) => `${w} ${fpx(px)}px ui-monospace,monospace`;
  const lh = (px) => fpx(px) * 1.15;   // ラベルの行送り
  /* ラベルは必ず暗い下地の上に置く。GIF・ローソク・他のラベルに重なっても数字が読めるように。
     align: 'left' なら x が左端、'right' なら x が右端、'center' なら中央。y はベースライン。 */
  const tag = (text, x, y, color, opts = {}) => {
    const px = opts.px || 11, weight = opts.weight || 600;
    c.save();
    c.font = mono(px, weight); c.textBaseline = 'alphabetic';
    const w = c.measureText(text).width, h = fpx(px), padX = 4 * u + 1, padY = 2 * u + 1;
    let x0 = x;
    if (opts.align === 'right') x0 = x - w;
    else if (opts.align === 'center') x0 = x - w / 2;
    c.fillStyle = opts.bg || 'rgba(5,6,8,.78)';
    c.beginPath(); c.roundRect(x0 - padX, y - h - padY + 1, w + padX * 2, h + padY * 2, 3 * u + 1); c.fill();
    c.fillStyle = color; c.textAlign = 'left';
    c.fillText(text, x0, y);
    c.restore();
    return { x0, w, h };
  };

  c.save();
  c.beginPath(); c.rect(box.x - 2, box.y - 30 * u, box.w + 4, box.h + 60 * u); c.clip();

  // ── 方眼(時間 15 分・価格の目盛り)
  c.lineWidth = 1;
  c.strokeStyle = 'rgba(236,233,223,.07)';
  for (const tick of m.timeTicks) {
    const x = xOf(tick.t);
    c.beginPath(); c.moveTo(x, box.y); c.lineTo(x, box.y + box.h); c.stroke();
  }
  for (const p of m.priceTicks) {
    const y = yOf(p);
    c.beginPath(); c.moveTo(box.x, y); c.lineTo(box.x + box.w, y); c.stroke();
  }
  for (const tick of m.timeTicks) tag(tick.label, xOf(tick.t), box.y + box.h + lh(12), 'rgba(140,147,160,.95)', { align: 'center', px: 11, weight: 500, bg: 'rgba(5,6,8,.55)' });

  // ── 保有区間(薄い帯)と、受け入れたリスク / 狙った報酬の箱
  c.fillStyle = 'rgba(236,233,223,.035)';
  c.fillRect(Math.min(xEntry, xExit), box.y, Math.abs(xExit - xEntry), box.h);
  const boxSpan = (pA, pB, fill, stroke) => {
    const y0 = Math.min(yOf(pA), yOf(pB)), h = Math.max(1, Math.abs(yOf(pA) - yOf(pB)));
    c.fillStyle = fill; c.fillRect(xEntry, y0, Math.max(1, xExit - xEntry), h);
    c.strokeStyle = stroke; c.lineWidth = 1 * u; c.strokeRect(xEntry, y0, Math.max(1, xExit - xEntry), h);
  };
  if (m.stop !== null && m.entry !== null) boxSpan(m.entry, m.stop, 'rgba(255,46,76,.11)', 'rgba(255,46,76,.42)');
  if (m.tp1 !== null && m.entry !== null) boxSpan(m.entry, m.tp1, 'rgba(61,214,140,.08)', 'rgba(61,214,140,.35)');

  // ── VP 水準(点線)
  for (const level of m.levels) {
    const col = level.kind === 'C' ? INK.levelC : level.kind === 'P' ? INK.levelP : INK.ash;
    const y = yOf(level.price);
    c.strokeStyle = col; c.globalAlpha = .55; c.lineWidth = 1 * u; c.setLineDash([2 * u, 5 * u]);
    c.beginPath(); c.moveTo(box.x, y); c.lineTo(box.x + box.w, y); c.stroke(); c.setLineDash([]);
    c.globalAlpha = 1;
    tag(`${level.label.replace(': ', ':')} ${short(level.price)}`, box.x + 5 * u, y - 3 * u, col, { px: 10.5, weight: 500 });
  }

  // ── ローソク(インク&エンバー)。保有外は薄く、保有中は濃く
  const n = m.bars.length;
  const slotW = box.w / ((m.t1 - m.t0) / m.tf);
  const bodyW = Math.max(2.2 * u, slotW * 0.62);
  for (const b of m.bars) {
    const inHold = m.entryT !== null && m.exitT !== null && b.t + m.tf > m.entryT && b.t < m.exitT;
    const cx = xOf(b.t + m.tf / 2);
    const up = b.c >= b.o;
    const col = up ? INK.bone : INK.rust;
    c.save();
    c.globalAlpha = inHold ? (up ? .95 : .92) : .42;
    c.shadowBlur = inHold ? 7 * u : 0; c.shadowColor = col;
    c.strokeStyle = col; c.lineWidth = Math.max(1, 1.05 * u); c.lineCap = 'round';
    c.beginPath(); c.moveTo(cx, yOf(b.h)); c.lineTo(cx, yOf(b.l)); c.stroke();
    const yTop = Math.min(yOf(b.o), yOf(b.c)), bodyH = Math.max(1.4 * u, Math.abs(yOf(b.o) - yOf(b.c)));
    if (up) {
      c.fillStyle = 'rgba(9,10,12,.65)'; c.fillRect(cx - bodyW / 2, yTop, bodyW, bodyH);
      c.lineWidth = Math.max(1, 1.15 * u); c.strokeRect(cx - bodyW / 2, yTop, bodyW, bodyH);
    } else {
      c.fillStyle = col; c.fillRect(cx - bodyW / 2, yTop, bodyW, bodyH);
    }
    c.restore();
  }

  // ── 建玉 / 決済の縦線
  const vline = (x, col, label, yLabel) => {
    c.strokeStyle = col; c.lineWidth = 1 * u; c.setLineDash([3 * u, 4 * u]);
    c.beginPath(); c.moveTo(x, box.y); c.lineTo(x, box.y + box.h); c.stroke(); c.setLineDash([]);
    tag(label, x + 4 * u, yLabel, col, { px: 11 });
  };
  if (m.entryT !== null) vline(xEntry, 'rgba(236,233,223,.75)', `IN ${fmtTime(m.entryT)}`, box.y + lh(11.5));
  if (m.exitT !== null) vline(xExit, INK.exit, `OUT ${fmtTime(m.exitT)}`, box.y + lh(11.5) * 2.1);

  // ── 基準線(ENTRY / SL / TP1 / TP2)。ラベルは右端に、重なりは縦へ逃がす
  const marks = [];
  if (m.entry !== null) marks.push(['ENTRY', m.entry, 'rgba(236,233,223,.55)', [1, 0]]);
  if (m.stop !== null) marks.push(['SL', m.stop, 'rgba(255,46,76,.85)', [6 * u, 4 * u]]);
  if (m.tp1 !== null) marks.push(['TP1', m.tp1, 'rgba(61,214,140,.85)', [6 * u, 4 * u]]);
  if (m.tp2 !== null && m.tp2Visible) marks.push(['TP2', m.tp2, 'rgba(61,214,140,.55)', [2 * u, 4 * u]]);
  marks.sort((a, b) => yOf(a[1]) - yOf(b[1]));
  let prevY = -Infinity;
  for (const [label, price, col, dash] of marks) {
    const y = yOf(price);
    c.strokeStyle = col; c.lineWidth = 1.2 * u; c.setLineDash(dash);
    c.beginPath(); c.moveTo(xEntry, y); c.lineTo(box.x + box.w, y); c.stroke(); c.setLineDash([]);
    const labelY = Math.max(y - 4 * u, prevY + lh(12) + 4 * u);
    prevY = labelY;
    tag(`${label} ${short(price)}`, box.x + box.w - 4 * u, labelY, col, { align: 'right', px: 11 });
  }
  if (m.tp2 !== null && !m.tp2Visible) {
    const below = (m.side === 'SHORT');
    tag(`TP2 ${short(m.tp2)} ${below ? '↓' : '↑'} 枠外`, box.x + box.w - 4 * u,
        below ? box.y + box.h - 6 * u : box.y + lh(11) * 3.3, 'rgba(61,214,140,.85)', { align: 'right', px: 10.5, weight: 500 });
  }

  // ── MAE / MFE の印(保有中の最悪・最良)
  const marker = (pt, price, t, col, label, up) => {
    const x = xOf(t + m.tf / 2), y = yOf(price);
    c.fillStyle = col;
    c.beginPath();
    if (up) { c.moveTo(x, y - 7 * u); c.lineTo(x - 5 * u, y + 1 * u); c.lineTo(x + 5 * u, y + 1 * u); }
    else { c.moveTo(x, y + 7 * u); c.lineTo(x - 5 * u, y - 1 * u); c.lineTo(x + 5 * u, y - 1 * u); }
    c.closePath(); c.fill();
    tag(`${label} ${pt > 0 ? '−' : ''}${short(pt)}`.replace('MFE −', 'MFE +'), x, up ? y - lh(11) : y + lh(11) * 1.5, col, { align: 'center', px: 10.5 });
  };
  if (m.mae && m.mae.pt > 0) marker(m.mae.pt, m.mae.price, m.mae.t, INK.rust, 'MAE', m.side === 'LONG');
  if (m.mfe && m.mfe.pt > 0) marker(m.mfe.pt, m.mfe.price, m.mfe.t, INK.tp, 'MFE', m.side === 'SHORT');

  // ── 決済後に TP1 へ届いた印(損切りの検証)
  if (m.post && m.post.tp1AfterMin !== null && m.exitKind === 'SL' && m.tp1 !== null) {
    const x = xOf(m.post.tp1Touch), y = yOf(m.tp1);
    c.strokeStyle = 'rgba(61,214,140,.6)'; c.lineWidth = 1 * u; c.setLineDash([2 * u, 3 * u]);
    c.beginPath(); c.moveTo(xExit, yOf(m.exit)); c.lineTo(x, y); c.stroke(); c.setLineDash([]);
    c.fillStyle = INK.tp; c.beginPath(); c.arc(x, y, 3.2 * u, 0, 7); c.fill();
    tag(`TP1 +${m.post.tp1AfterMin}m after SL`, x, y + lh(11) * 1.4, INK.tp, { align: 'center', px: 10.5, weight: 500 });
  }

  // ── 決済点(フレア)
  if (m.exit !== null && m.exitT !== null) {
    const x = xExit, y = yOf(m.exit);
    if (o.flare) o.flare(c, x, y); else { c.fillStyle = INK.exit; c.beginPath(); c.arc(x, y, 3.4 * u, 0, 7); c.fill(); }
  }
  c.restore();
}

/** 根拠チップ(evidence / penalties)を横並びで描く。折り返しあり。戻り値は使った高さ。 */
export function drawRationaleChips(c, m, x, y, maxW, u = 1) {
  const chips = [
    ...m.evidence.map((t) => ({ t, kind: 'ev' })),
    ...m.penalties.map((t) => ({ t, kind: 'pen' })),
  ];
  if (!chips.length) return 0;
  c.save();
  c.font = `500 ${11 * u}px ui-monospace,monospace`; c.textBaseline = 'middle';
  let cx = x, cy = y, rowH = 22 * u, gap = 6 * u, padX = 8 * u;
  for (const chip of chips) {
    const w = c.measureText(chip.t).width + padX * 2;
    if (cx + w > x + maxW && cx > x) { cx = x; cy += rowH + gap; }
    const col = chip.kind === 'ev' ? 'rgba(236,233,223,.85)' : 'rgba(255,46,76,.85)';
    c.strokeStyle = col; c.lineWidth = 1 * u;
    c.beginPath(); c.roundRect(cx, cy, w, rowH, rowH / 2); c.stroke();
    c.fillStyle = col; c.fillText(chip.t, cx + padX, cy + rowH / 2 + 0.5);
    cx += w + gap;
  }
  c.restore();
  return cy + rowH - y;
}

/** DOM 用: 計測行(ラベル, 値)。数字が無い項目は出さない。 */
export function metricRows(m, derived = {}) {
  const rows = [];
  if (m.holdMin !== null) rows.push(['HELD', derived.held || `${m.holdMin}m`]);
  if (m.mae) rows.push(['MAE', `−${short(m.mae.pt)} pt${m.maeR !== null ? ` · −${m.maeR.toFixed(2)}R` : ''}`]);
  if (m.mfe) rows.push(['MFE', `+${short(m.mfe.pt)} pt${m.mfeR !== null ? ` · +${m.mfeR.toFixed(2)}R` : ''}`]);
  if (m.efficiency !== null) rows.push(['EFFICIENCY', `${m.efficiency.toFixed(2)} of MFE`]);
  if (m.exitKind && m.exitKind !== 'UNKNOWN') rows.push(['EXIT BY', m.exitKind + (m.slip !== null ? ` · slip ${short(m.slip)} pt` : '')]);
  if (m.post) {
    let text = `+${short(m.post.favPt)} / −${short(m.post.advPt)} pt`;
    if (m.post.tp1AfterMin !== null) text += ` · TP1 +${m.post.tp1AfterMin}m`;
    rows.push([`AFTER EXIT ${m.post.minutes}m`, text]);
  }
  if (m.session) rows.push(['SESSION', m.session + (m.volRatio !== null ? ` · vol ${m.volRatio}` : '')]);
  const htf = Object.entries(m.htf || {}).filter(([, v]) => v).map(([k, v]) => `${k} ${v}`).join(' · ');
  if (htf) rows.push(['HTF', htf]);
  return rows;
}

/**
 * R リング: 計画 R(TP1 までの距離 ÷ リスク)に対する実現 R の割合。
 * 実現が計画を超えたら 1、負けは負の割合(−1 で SL いっぱい)。数字は表示側で丸める。
 */
export function ringModel(trade, m) {
  const entry = num(trade?.entry), exit = num(trade?.exit), stop = num(trade?.stop);
  const side = String(trade?.side || '').toUpperCase() === 'SHORT' ? -1 : 1;
  if (entry === null || exit === null || stop === null || entry === stop) return null;
  const risk = Math.abs(entry - stop);
  const realized = ((exit - entry) * side) / risk;
  const tp1 = m ? m.tp1 : null;
  const planned = tp1 !== null ? Math.abs(tp1 - entry) / risk : null;
  const scale = Math.max(planned || 0, Math.abs(realized), 1);
  const frac = Math.max(-1, Math.min(1, realized / scale));
  return { realized: Math.round(realized * 100) / 100, planned: planned === null ? null : Math.round(planned * 100) / 100,
           frac, plannedFrac: planned === null ? null : Math.min(1, planned / scale) };
}

/** リングを Canvas に描く(書き出しカード用)。cx,cy 中心、r 半径。 */
export function drawRing(c, ring, cx, cy, r, tint, u = 1) {
  c.save();
  c.lineCap = 'round';
  c.strokeStyle = 'rgba(236,233,223,.16)'; c.lineWidth = 6 * u;
  c.beginPath(); c.arc(cx, cy, r, 0, Math.PI * 2); c.stroke();
  const start = -Math.PI / 2;
  if (ring.plannedFrac !== null) {
    c.strokeStyle = 'rgba(61,214,140,.35)'; c.lineWidth = 6 * u;
    c.beginPath(); c.arc(cx, cy, r, start, start + Math.PI * 2 * ring.plannedFrac); c.stroke();
  }
  c.strokeStyle = tint; c.lineWidth = 7 * u; c.shadowBlur = 10 * u; c.shadowColor = tint;
  c.beginPath();
  if (ring.frac >= 0) c.arc(cx, cy, r, start, start + Math.PI * 2 * ring.frac);
  else c.arc(cx, cy, r, start + Math.PI * 2 * ring.frac, start);
  c.stroke();
  c.shadowBlur = 0;
  c.fillStyle = '#ECE9DF'; c.textAlign = 'center'; c.textBaseline = 'middle';
  c.font = `700 ${Math.round(r * 0.62)}px Archivo,system-ui,sans-serif`;
  c.fillText(`${ring.realized >= 0 ? '+' : '−'}${Math.abs(ring.realized).toFixed(1)}R`, cx, cy - (ring.planned !== null ? r * 0.12 : 0));
  if (ring.planned !== null) {
    c.fillStyle = 'rgba(140,147,160,.95)'; c.font = `500 ${Math.round(r * 0.28)}px ui-monospace,monospace`;
    c.fillText(`plan ${ring.planned.toFixed(1)}R`, cx, cy + r * 0.34);
  }
  c.restore();
}

export { fmt as fmtPrice };
