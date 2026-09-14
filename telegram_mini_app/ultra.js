/**
 * ULTRA mode — 口座ごとの必要枚数を計算する表示側モジュール。
 *
 * ULTRA OFF : 通常シグナルの枚数をそのまま使う。
 * ULTRA ON  : 通常の枚数を無視し、口座ごとの利益目標と残ドローダウンから
 *             必要枚数を再計算して、その枚数へ強制的に上書きする。
 *
 *     qty           = ceil(profitTarget / (tpPoints * pointValue))
 *     projectedLoss = qty * slPoints * pointValue
 *
 * ここは **表示専用**。発注される枚数は Bot が `ultra_mode.py` で同じ式を
 * 再計算した値であって、この画面が送った数字ではない(Mini App の値は
 * 一切信用しない、という既存の契約をそのまま守る)。
 *
 * ultra_mode.py と式・判定・用語を必ず一致させること。片方だけ変えると
 * 画面と実発注が食い違う。
 */

export const ULTRA_VERSION = "ULTRA-MODE/1";

/** MNQ は 1pt = $2.00。execution_contract.json の pointValue と同じ。 */
export const POINT_VALUE = 2.0;

/**
 * 通常経路の上限。ULTRA では使わない(ULTRA 無効時のフォールバックのみ)。
 * maxAccounts: null = 口座数の上限なし(2026-08-27 に撤廃)。
 */
const CONTRACT = { maxQty: 2, fixedQty: 2, riskCapDollars: 240, maxAccounts: null };

/**
 * ULTRA エンベロープ。execution_contract.json の `ultra` と必ず一致させること
 * (tests/test_ultra_mode.py の乖離ガードが機械で止める)。
 */
const ULTRA = {
  enabled: true,
  minQtyPerAccount: 2,
  maxQtyPerAccount: 100,
  maxRiskDollarsPerAccount: 5000,
  splitMinLegQty: 1,
  splitLegCount: 2,
};

/** execution_contract.ultra_split() と同一。端数は runner へ寄せる。 */
export function ultraSplit(qty) {
  const total = Number(qty);
  if (!Number.isInteger(total) || total < ULTRA.splitMinLegQty * ULTRA.splitLegCount) return null;
  const tp1 = Math.floor(total / 2);
  const runner = total - tp1;
  if (tp1 < ULTRA.splitMinLegQty || runner < ULTRA.splitMinLegQty) return null;
  return [tp1, runner];
}

function finite(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** シグナルの SL/TP 幅。方向が壊れていれば理由を返し、価格は作らない。 */
export function signalGeometry(signal) {
  if (!signal || typeof signal !== "object") return { valid: false, reason: "SIGNAL_MISSING" };
  const raw = String(signal.side || "").toUpperCase();
  const side = ["LONG", "BUY"].includes(raw) ? "BUY" : ["SHORT", "SELL"].includes(raw) ? "SELL" : null;
  if (!side) return { valid: false, reason: "SIGNAL_SIDE_INVALID" };
  const entry = finite(signal.entry);
  const stop = finite(signal.stop ?? signal.sl);
  const target = finite(signal.target ?? signal.tp);
  if (entry === null || stop === null || target === null) {
    return { valid: false, reason: "SIGNAL_PRICES_INCOMPLETE" };
  }
  if (side === "BUY" && !(stop < entry && entry < target)) {
    return { valid: false, reason: "SIGNAL_DIRECTION_INVALID" };
  }
  if (side === "SELL" && !(target < entry && entry < stop)) {
    return { valid: false, reason: "SIGNAL_DIRECTION_INVALID" };
  }
  const slPoints = Math.abs(entry - stop);
  const tpPoints = Math.abs(target - entry);
  if (slPoints <= 0 || tpPoints <= 0) return { valid: false, reason: "SIGNAL_DISTANCE_INVALID" };
  // runner(2 本目の目標)。分割計画の実際の決済に必要(R30)。
  // 無ければ TP1 と同値。捏造せず、分割の恩恵が無いだけ。
  let runnerPoints = tpPoints;
  const targets = signal?.targets;
  if (Array.isArray(targets) && targets.length >= 2) {
    const runnerPrice = finite(targets[1]);
    if (runnerPrice !== null) {
      const span = Math.abs(runnerPrice - entry);
      const ok = side === "BUY" ? runnerPrice > entry : runnerPrice < entry;
      if (ok && span > 0) runnerPoints = span;
    }
  }
  return { valid: true, side, entry, stop, target, slPoints, tpPoints, runnerPoints,
    rr: Math.round((tpPoints / slPoints) * 100) / 100,
    runnerRr: Math.round((runnerPoints / slPoints) * 100) / 100 };
}

/** 全量が 1 つの目標で決済される前提の枚数。分割計画では使わない。 */
export function requiredQty(profitTarget, tpPoints, pointValue = POINT_VALUE) {
  const target = finite(profitTarget);
  const points = finite(tpPoints);
  if (target === null || points === null || target <= 0 || points <= 0 || pointValue <= 0) return null;
  return Math.ceil(target / (points * pointValue));
}

/**
 * 2 レグ分割の**実際の決済**で利益目標に届く最小枚数(R30)。
 *
 * 実行計画は qty を半分ずつ TP1 と runner へ割るので、両レグが当たった
 * ときの利益は (qty/2)*TP1 + (qty/2)*runner。従来式は「全量が TP1 で決済」
 * 前提で計画と矛盾しており、実測(武装候補 101 本)では TP1 だけ当たると
 * 目標の 52%、両方当たると 332% になっていた。
 *
 * ultra_mode.required_qty_split と**同一の式**。片方だけ直すと乖離する。
 */
export function requiredQtySplit(profitTarget, tp1Points, runnerPoints, pointValue = POINT_VALUE) {
  const target = finite(profitTarget);
  const tp1 = finite(tp1Points);
  let runner = finite(runnerPoints);
  if (target === null || target <= 0 || pointValue <= 0) return null;
  if (tp1 === null || tp1 <= 0) return null;
  if (runner === null || runner <= 0) runner = tp1;
  const perPair = (tp1 + runner) * pointValue;
  if (perPair <= 0) return null;
  let qty = Math.ceil((2 * target) / perPair);
  if (qty < 2) qty = 2;
  if (qty % 2) qty += 1;
  return qty;
}

/** ultra_mode.py の f"${value:,.2f}" と同じ文字列にする(パリティ試験あり)。 */
function usd(value) {
  return Number(value).toLocaleString("en-US",
    { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/**
 * 実行契約がこの枚数を通さない理由。ULTRA 宣言経路は ULTRA エンベロープで
 * 判定する — 通常経路の2枚固定・$240 を当てると必ず全口座が不可になる。
 * ultra_mode._contract_blockers() と同一規則。
 */
function contractBlockers(qty, projectedLoss, buffer) {
  const blockers = [];
  if (!ULTRA.enabled) {
    if (qty > CONTRACT.maxQty) blockers.push(`QTY_EXCEEDS_CONTRACT(max=${CONTRACT.maxQty})`);
    if (qty !== CONTRACT.fixedQty) blockers.push(`FIXED_QTY_REQUIRED(fixed=${CONTRACT.fixedQty})`);
    if (projectedLoss > CONTRACT.riskCapDollars + 1e-9) {
      blockers.push(`RISK_CAP_EXCEEDED(cap=$${usd(CONTRACT.riskCapDollars)})`);
    }
    blockers.push("ULTRA_DISABLED");
    return blockers;
  }
  if (qty < ULTRA.minQtyPerAccount) {
    blockers.push(`ULTRA_QTY_BELOW_MIN(min=${ULTRA.minQtyPerAccount})`);
  }
  if (qty > ULTRA.maxQtyPerAccount) {
    blockers.push(`ULTRA_QTY_EXCEEDS_ACCOUNT_MAX(max=${ULTRA.maxQtyPerAccount})`);
  }
  if (ultraSplit(qty) === null) blockers.push("ULTRA_SPLIT_NOT_REPRESENTABLE");
  if (projectedLoss > ULTRA.maxRiskDollarsPerAccount + 1e-9) {
    blockers.push(`ULTRA_ACCOUNT_RISK_EXCEEDS_CONTRACT(max=$${usd(ULTRA.maxRiskDollarsPerAccount)})`);
  }
  // ULTRA のリスク上限の正本は残ドローダウン。1トレード上限ではない。
  const drawdown = finite(buffer);
  if (drawdown === null || drawdown <= 0) {
    blockers.push("ULTRA_DRAWDOWN_UNAVAILABLE");
  } else if (projectedLoss > drawdown + 1e-9) {
    blockers.push(`ULTRA_DRAWDOWN_EXCEEDED(buffer=$${usd(drawdown)})`);
  }
  return blockers;
}

/** 1口座ぶんの ULTRA 枚数と判定。 */
export function accountPlan(account, geometry, pointValue = POINT_VALUE) {
  const source = account && typeof account === "object" ? account : {};
  const id = String(source.id || "").trim();
  const row = {
    id,
    label: source.label ? String(source.label) : id ? `…${id.slice(-4)}` : "",
    profitTarget: finite(source.profitTarget),
    buffer: finite(source.buffer),
    cap: finite(source.cap),
    qty: null, projectedProfit: null, projectedLoss: null, drawdownUsedPct: null,
    verdict: "INELIGIBLE", reasons: [], contractBlockers: [], routable: false,
  };
  if (!id) { row.reasons.push("ACCOUNT_ID_MISSING"); return row; }
  if (!geometry?.valid) { row.reasons.push(geometry?.reason || "SIGNAL_INVALID"); return row; }
  if (row.profitTarget === null || row.profitTarget <= 0) {
    row.reasons.push("PROFIT_TARGET_MISSING");
    return row;
  }
  // R30: 分割計画の実際の決済で目標に届く枚数。ultra_mode.py と同一の式。
  const qty = requiredQtySplit(row.profitTarget, geometry.tpPoints,
    geometry.runnerPoints, pointValue);
  if (qty === null || qty < 1) { row.reasons.push("QTY_NOT_COMPUTABLE"); return row; }
  row.qty = qty;
  const half = qty / 2;
  const runnerPoints = geometry.runnerPoints || geometry.tpPoints;
  // 両レグ当たり = 分割計画が完走したときの利益。
  row.projectedProfit = Math.round(half * (geometry.tpPoints + runnerPoints) * pointValue * 100) / 100;
  // TP1 だけ当たって runner が建値撤退した場合。**目標には届かない**ことを隠さない。
  row.profitIfTp1Only = Math.round(half * geometry.tpPoints * pointValue * 100) / 100;
  row.projectedLoss = Math.round(qty * geometry.slPoints * pointValue * 100) / 100;
  if (row.buffer === null) {
    row.reasons.push("DRAWDOWN_UNKNOWN");
  } else if (row.buffer <= 0) {
    row.reasons.push("ACCOUNT_BLOWN");
  } else {
    row.drawdownUsedPct = Math.round((row.projectedLoss / row.buffer) * 1000) / 10;
    if (row.projectedLoss > row.buffer + 1e-9) row.reasons.push("DRAWDOWN_EXCEEDED");
  }
  row.contractBlockers = contractBlockers(qty, row.projectedLoss, row.buffer);
  row.legs = ultraSplit(qty);
  row.verdict = row.reasons.length ? "INELIGIBLE" : "ELIGIBLE";
  row.routable = row.verdict === "ELIGIBLE" && row.contractBlockers.length === 0;
  return row;
}

/**
 * ULTRA の発注計画。`enabled=false` なら通常枚数をそのまま返す。
 *
 * @param {object} signal   {side, entry, stop, target}
 * @param {Array}  accounts DO の account stream の list
 */
export function buildPlan(signal, accounts, { enabled = true, signalQty = null,
  pointValue = POINT_VALUE } = {}) {
  const geometry = signalGeometry(signal);
  if (!enabled) {
    return { version: ULTRA_VERSION, enabled: false, pointValue, geometry,
      signalQty: finite(signalQty), accounts: [], totalQty: finite(signalQty),
      eligibleCount: 0, accountCount: 0, routable: false,
      note: "ULTRA OFF — 通常シグナルの枚数をそのまま使用する" };
  }
  const rows = (Array.isArray(accounts) ? accounts : [])
    .filter((item) => item && typeof item === "object")
    .map((item) => accountPlan(item, geometry, pointValue));
  const eligible = rows.filter((row) => row.verdict === "ELIGIBLE");
  const round2 = (value) => Math.round(value * 100) / 100;
  return {
    version: ULTRA_VERSION, enabled: true, pointValue, geometry,
    signalQty: finite(signalQty), accounts: rows,
    totalQty: eligible.reduce((sum, row) => sum + (row.qty || 0), 0),
    totalProjectedProfit: round2(eligible.reduce((s, row) => s + (row.projectedProfit || 0), 0)),
    totalProjectedLoss: round2(eligible.reduce((s, row) => s + (row.projectedLoss || 0), 0)),
    eligibleCount: eligible.length,
    accountCount: rows.length,
    routable: eligible.length > 0 && eligible.every((row) => row.routable),
    note: "ULTRA ON — 通常枚数を無視し口座別の計算枚数へ強制上書きする",
  };
}

const REASON_JP = {
  PROFIT_TARGET_MISSING: "利益目標が未設定",
  DRAWDOWN_EXCEEDED: "想定損失が残DDを超過",
  DRAWDOWN_UNKNOWN: "残DD不明",
  ACCOUNT_BLOWN: "残DDなし",
  QTY_NOT_COMPUTABLE: "枚数を計算できない",
  ACCOUNT_ID_MISSING: "口座IDなし",
  SIGNAL_MISSING: "シグナルなし",
  SIGNAL_SIDE_INVALID: "方向が不正",
  SIGNAL_PRICES_INCOMPLETE: "価格が不足",
  SIGNAL_DIRECTION_INVALID: "価格の並びが不正",
  SIGNAL_DISTANCE_INVALID: "SL/TP幅が不正",
};

export function reasonText(reason) {
  return REASON_JP[reason] || reason;
}
