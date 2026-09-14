/**
 * 期待損益(表示専用)。
 *
 * シナリオカードと保有中カードに「この計画が当たると幾ら / 外れると幾ら」を
 * 大きく出すための純関数。判定はしない。金額は画面に出ている価格・枚数・
 * MNQ 1pt=$2.00 の掛け算だけで、サーバーの値を上書きしない。
 *
 * DOM に触らないので node --test から直接読める(evalcard.js と同じ流儀)。
 */

export const POINT_VALUE = 2.0; // MNQ 1pt = $2.00(execution_contract.json の pointValue)

/** 有限数だけ。null / undefined / 空文字 / bool は欠損(Number(null) の 0 を作らない)。 */
function finite(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]
  ));
}

/** 符号付き整数ドル。"+$1,234" / "−$567"。欠損は "—"。 */
export function signedDollars(value) {
  const parsed = finite(value);
  if (parsed === null) return "—";
  const rounded = Math.round(Math.abs(parsed));
  const sign = parsed < 0 ? "−" : "+";
  return `${sign}$${rounded.toLocaleString("en-US")}`;
}

/** 方向係数。BUY/LONG=+1、SELL/SHORT=−1、それ以外は null。 */
export function direction(side) {
  const upper = String(side || "").toUpperCase();
  if (upper === "BUY" || upper === "LONG") return 1;
  if (upper === "SELL" || upper === "SHORT") return -1;
  return null;
}

/**
 * 脚(TP1 / RUNNER)を正規化する。legs が無ければ targets から 1枚ずつ、
 * targets も無ければ target 1本を全量として扱う。価格が無い脚は落とす。
 */
export function normalizeLegs(plan) {
  const qty = finite(plan?.qty);
  const legs = Array.isArray(plan?.legs) ? plan.legs : [];
  const out = [];
  for (const leg of legs) {
    const target = finite(leg?.target);
    const legQty = finite(leg?.qty);
    if (target === null || legQty === null || legQty <= 0) continue;
    out.push({ id: String(leg.id || `LEG${out.length + 1}`), qty: legQty, target });
  }
  if (out.length) return out;
  const targets = (Array.isArray(plan?.targets) ? plan.targets : []).map(finite).filter((v) => v !== null);
  if (targets.length >= 2 && qty !== null && qty >= 2) {
    const first = Math.floor(qty / 2);
    return [{ id: "TP1", qty: first, target: targets[0] },
      { id: "RUNNER", qty: qty - first, target: targets[targets.length - 1] }];
  }
  const single = targets[0] ?? finite(plan?.target);
  if (single !== null && qty !== null && qty > 0) return [{ id: "TP", qty, target: single }];
  return [];
}

/**
 * シナリオ(発注前)の期待損益。1口座あたりの金額と、参加口座数を掛けた合計。
 *   perLeg[]  : 脚ごとの利益(その脚だけ)
 *   profit    : 全脚が抜けたときの利益(1口座)
 *   loss      : 全量が構造 SL で切られたときの損失(1口座、正の数)
 *   accounts  : 掛ける口座数(不明なら 1)
 * 価格や枚数が欠けていれば null(0 で埋めない)。
 */
export function projectScenario(scenario, accounts = 1) {
  const dir = direction(scenario?.side);
  const entry = finite(scenario?.entry);
  const stop = finite(scenario?.stop);
  const qty = finite(scenario?.qty);
  if (dir === null || entry === null || stop === null || qty === null || qty <= 0) return null;
  const legs = normalizeLegs(scenario);
  if (!legs.length) return null;
  const n = Math.max(1, Math.floor(finite(accounts) ?? 1));
  const perLeg = legs.map((leg) => ({
    id: leg.id, qty: leg.qty, target: leg.target,
    pts: (leg.target - entry) * dir,
    usd: (leg.target - entry) * dir * POINT_VALUE * leg.qty,
  }));
  const profit = perLeg.reduce((sum, leg) => sum + leg.usd, 0);
  const loss = Math.abs(stop - entry) * POINT_VALUE * qty;
  return {
    perLeg, profit, loss, accounts: n,
    profitTotal: profit * n, lossTotal: loss * n,
    ratio: loss > 0 ? profit / loss : null,
  };
}

/**
 * 保有中の損益。含み(unrealized)と、残り脚が抜けたとき / SL に当たったときの金額。
 *   position : { side, qty, initialQty, avgEntry, stop, target, unrealizedPnl }
 *   legs     : 凍結プランの脚(TP1 / RUNNER)。無ければ position.target を全量で 1脚
 *   last     : 現値(含みの再計算に使う。position.unrealizedPnl があればそれを優先)
 * 枚数が initialQty より減っていれば TP1 は約定済みとみなし、残る脚だけを数える。
 */
export function projectPosition(position, legs = [], last = null) {
  const dir = direction(position?.side);
  const entry = finite(position?.avgEntry);
  const qty = finite(position?.qty);
  if (dir === null || entry === null || qty === null || qty <= 0) return null;
  const stop = finite(position?.stop);
  // 現値は正の有限数だけ。0 は「価格が無い」(Number(null) 由来)であって現値ではない。
  const lastFinite = finite(last);
  const lastPrice = lastFinite !== null && lastFinite > 0 ? lastFinite : null;
  const unrealized = finite(position?.unrealizedPnl)
    ?? (lastPrice !== null ? (lastPrice - entry) * dir * POINT_VALUE * qty : null);

  const initialQty = finite(position?.initialQty) ?? qty;
  let remaining = Array.isArray(legs) ? legs.map((leg) => ({
    id: String(leg?.id || "LEG"), qty: finite(leg?.qty), target: finite(leg?.target),
  })).filter((leg) => leg.qty !== null && leg.qty > 0 && leg.target !== null) : [];
  if (remaining.length && qty < initialQty) {
    // 先頭の脚から約定済みとして落とす(TP1 → RUNNER の順)。
    let filled = initialQty - qty;
    while (filled > 0 && remaining.length) {
      const head = remaining[0];
      if (head.qty <= filled) { filled -= head.qty; remaining = remaining.slice(1); } else {
        remaining = [{ ...head, qty: head.qty - filled }, ...remaining.slice(1)]; filled = 0;
      }
    }
  }
  if (!remaining.length) {
    const target = finite(position?.target);
    if (target !== null) remaining = [{ id: "TP", qty, target }];
  }
  const atTargets = remaining.map((leg) => ({
    id: leg.id, qty: leg.qty, target: leg.target,
    usd: (leg.target - entry) * dir * POINT_VALUE * leg.qty,
  }));
  const atTargetTotal = atTargets.length ? atTargets.reduce((sum, leg) => sum + leg.usd, 0) : null;
  const atStop = stop !== null ? (stop - entry) * dir * POINT_VALUE * qty : null;
  return { unrealized, last: lastPrice, atTargets, atTargetTotal, atStop, remainingQty: qty, entry, dir };
}

function heroCell(label, value, tone, sub = "") {
  return `<div class="pnl-cell ${tone}">`
    + `<span class="micro">${escapeHtml(label)}</span>`
    + `<b class="pnl-value">${escapeHtml(value)}</b>`
    + (sub ? `<span class="pnl-sub">${escapeHtml(sub)}</span>` : "")
    + `</div>`;
}

/**
 * シナリオカードの期待損益ブロック。
 * 左に「当たれば」(全脚)、右に「外れれば」(構造 SL)。下に脚ごとの内訳。
 */
export function scenarioProjectionHtml(scenario, accounts = 1) {
  const p = projectScenario(scenario, accounts);
  if (!p) return "";
  const legsText = p.perLeg.length > 1
    ? p.perLeg.map((leg) => `${leg.id} ${signedDollars(leg.usd)}`).join(" · ")
    : "";
  const scale = p.accounts > 1 ? ` × ${p.accounts} accts` : "";
  return `<div class="pnl-hero" aria-label="expected P&L">`
    + heroCell("IF TARGETS HIT", signedDollars(p.profitTotal), "gain",
      p.accounts > 1 ? `${signedDollars(p.profit)} each${scale}` : "")
    + heroCell("IF STOPPED", signedDollars(-p.lossTotal), "pain",
      p.accounts > 1 ? `${signedDollars(-p.loss)} each${scale}` : "")
    + `</div>`
    + (legsText || p.ratio !== null
      ? `<p class="pnl-legs">${legsText ? escapeHtml(legsText) : ""}${legsText && p.ratio !== null ? " · " : ""}${
        p.ratio !== null ? `R:R <b>1:${escapeHtml(p.ratio.toFixed(2))}</b>` : ""}</p>`
      : "");
}

/**
 * 保有中カードの損益ブロック。中央に含み損益を大きく、両脇に「残り脚が抜けたら」と
 * 「SL に当たったら」。runner 局面で SL が建値以上なら pain ではなく locked(確保)扱い。
 */
export function positionProjectionHtml(position, legs = [], last = null) {
  const p = projectPosition(position, legs, last);
  if (!p) return "";
  const unrealizedTone = p.unrealized === null ? "" : (p.unrealized >= 0 ? "gain" : "pain");
  const stopTone = p.atStop === null ? "" : (p.atStop >= 0 ? "locked" : "pain");
  const stopLabel = p.atStop !== null && p.atStop >= 0 ? "SL LOCKS" : "IF STOPPED";
  const targetsText = p.atTargets.length > 1
    ? p.atTargets.map((leg) => `${leg.id} ${signedDollars(leg.usd)}`).join(" · ")
    : "";
  return `<div class="pnl-hero is-live" aria-label="position P&L">`
    + heroCell("OPEN P&L", p.unrealized === null ? "N/A" : signedDollars(p.unrealized), unrealizedTone,
      p.last !== null ? `last ${p.last.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : "")
    + heroCell("IF TARGETS HIT", p.atTargetTotal === null ? "—" : signedDollars(p.atTargetTotal), "gain",
      targetsText)
    + heroCell(stopLabel, p.atStop === null ? "—" : signedDollars(p.atStop), stopTone)
    + `</div>`;
}
