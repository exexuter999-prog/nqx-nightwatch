import executionContract from "../../execution_contract.json" with { type: "json" };

/**
 * NQX Nightwatch — 状態スキーマと状態遷移。
 *
 * このファイルは Cloudflare ランタイムに一切依存しない純粋関数だけで構成する。
 * Durable Object から呼ばれると同時に、Node からそのままテストできる。
 * ここに副作用・I/O・時刻取得を書かないこと(now は必ず引数で受ける)。
 *
 * 設計の核:
 *   - scenario / position / order / market は独立した stream で、revision も独立。
 *     → 新しい scenario が open position を上書きできない(構造的に不可能)。
 *   - 期限判定はサーバー時刻のみ。クライアント時計は判定に使わない。
 *   - position は broker が qty=0 を返すまで消えない。照会不能は STALE であって消去ではない。
 */

export const STATE_VERSION = 1;
export const TICK = 0.25;
export const POINT_VALUE = 2.0; // MNQ 1pt = $2.00 (TRADING_CONTEXT.md §2)
export const MARKET_MAX_AGE_MS = executionContract.market.maxAgeSec * 1000;
export const MARKET_FUTURE_TOLERANCE_MS = executionContract.market.futureToleranceSec * 1000;
// An atomic cycle is a lease, not an everlasting authorization.  A producer
// heartbeat is another higher-revision cycle commit and can renew it, but no
// payload may extend the lease beyond this server-owned maximum.
export const CYCLE_LEASE_MAX_MS = MARKET_MAX_AGE_MS;

export const STREAMS = ["scenario", "position", "order", "market", "result", "account", "cycle", "entry_claim", "management_claim", "broker_observation", "cycle_health"];

/** resultLog に保持する決済履歴の上限。LEDGER 画面のデータ源。 */
export const RESULT_LOG_LIMIT = 50;

/** account イベント1件に載せられる口座数の上限(誤爆した巨大 payload を弾く)。 */
export const ACCOUNT_LIST_LIMIT = 8;
/** 経路スナップショットの上限行数 = 口座数 × TP1/RUNNER の2脚。 */
export const ROUTE_SNAPSHOT_LIMIT = Math.max(
  8, Number(executionContract.ultra?.maxAccounts || 1) * 2);

/**
 * 決済結果の出所。ここに無い値は受け付けない。
 * 「相場から推定した決済価格」を作らないための列挙。
 */
export const EXIT_SOURCES = new Set(["broker", "manual"]);

/**
 * 価格パスの出所。合成パスは本プロジェクトの統治ルールで禁止されているので、
 * 実観測に由来するものしか受け付けない。
 *   observed-bars : TradingView から publish 済みの実バー終値
 *   endpoints-only: entry と exit の2点だけ(観測が足りなかった場合の正直な表現)
 */
export const PATH_SOURCES = new Set(["observed-bars", "endpoints-only"]);

/** scenario がこの状態になったら表示対象から外す(復活させない)。 */
export const TERMINAL_SCENARIO_STATES = new Set([
  "INVALIDATED", "EXPIRED", "CANCELED", "REPLACED", "FILLED", "CLOSED",
]);

/** scenario がこの状態のときだけ画面に出る。 */
export const LIVE_SCENARIO_STATES = new Set(["ACTIVE", "ARMED", "WATCH"]);

/** この状態の position は絶対に消さない。 */
export const OPEN_POSITION_STATES = new Set(["OPEN", "PARTIAL", "EXIT_PENDING"]);

/** 注文がこの状態の間は新規発注を許可しない。 */
export const BLOCKING_ORDER_STATES = new Set([
  "PENDING",
  "SENT",
  "UNKNOWN",
  "PARTIAL",
  "ENTRY_PARTIAL_ROUTE",
  "ENTRY_PARTIAL_FILL",
  "ENTRY_RESTING",
]);

/** state を確認できないときに残す表示。CLOSED とは別物。 */
export const STALE_POSITION_STATE = "STALE";

// ---------------------------------------------------------------- 数値ユーティリティ

export function isFiniteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

/** CME の 0.25 tick に正規化する。丸めた値を以後の計算の唯一の入力にする。 */
export function normalizeTick(value) {
  if (!isFiniteNumber(value)) return null;
  return Math.round(value / TICK) * TICK;
}

/** 浮動小数の誤差を price として比較可能な形に落とす。 */
export function priceEquals(a, b) {
  if (!isFiniteNumber(a) || !isFiniteNumber(b)) return false;
  return Math.abs(a - b) < TICK / 100;
}

/**
 * ULTRA の枚数を TP1 / RUNNER へ比率配分する。端数は runner 側へ寄せる。
 * execution_contract.ultra_split() と必ず同じ結果でなければならない。
 */
export function ultraSplit(qty) {
  const total = Number(qty);
  const plan = executionContract.ultra?.splitPlan || {};
  const minLeg = Number(plan.minLegQty || 1);
  if (!Number.isInteger(total) || total < minLeg * Number(plan.legCount || 2)) return null;
  const tp1 = Math.floor(total / 2);
  const runner = total - tp1;
  if (tp1 < minLeg || runner < minLeg) return null;
  return [tp1, runner];
}

/**
 * ULTRA の発注計画を ULTRA エンベロープだけで判定する。
 *
 * 通常経路の固定枚数・$240 上限はここでは使わない。1つでも上限を超えたら
 * その口座ではなく計画全体を止める — 部分的に出すと、どの口座が入ったのか
 * 分からないまま建玉が残る。execution_contract.ultra_evaluate() と同一規則。
 */
export function evaluateUltraPlan(plan) {
  const envelope = executionContract.ultra || {};
  if (!envelope.enabled) {
    return { ok: false, blockers: ["ULTRA_DISABLED"], accounts: [], totalQty: 0, totalRiskDollars: 0 };
  }
  const rows = Array.isArray(plan?.accounts) ? plan.accounts : [];
  const eligible = rows.filter((row) => row && row.verdict === "ELIGIBLE");
  if (!eligible.length) {
    return { ok: false, blockers: ["ULTRA_NO_ELIGIBLE_ACCOUNT"], accounts: [],
      totalQty: 0, totalRiskDollars: 0 };
  }
  const blockers = [];
  if (eligible.length > Number(envelope.maxAccounts)) blockers.push("ULTRA_ACCOUNTS_EXCEED_CONTRACT");
  let totalQty = 0;
  let totalRisk = 0;
  const accounts = [];
  for (const row of eligible) {
    const rowBlockers = [];
    const qty = Number(row.qty);
    const legs = ultraSplit(qty);
    const loss = Number(row.projectedLoss);
    const buffer = Number(row.buffer);
    const qtyInt = Number.isInteger(qty) ? qty : 0;
    if (qtyInt < Number(envelope.minQtyPerAccount)) rowBlockers.push("ULTRA_QTY_BELOW_MIN");
    if (qtyInt > Number(envelope.maxQtyPerAccount)) rowBlockers.push("ULTRA_QTY_EXCEEDS_ACCOUNT_MAX");
    if (legs === null) rowBlockers.push("ULTRA_SPLIT_NOT_REPRESENTABLE");
    if (!isFiniteNumber(loss)) {
      rowBlockers.push("ULTRA_RISK_UNKNOWN");
    } else {
      if (loss > Number(envelope.maxRiskDollarsPerAccount) + 1e-9) {
        rowBlockers.push("ULTRA_ACCOUNT_RISK_EXCEEDS_CONTRACT");
      }
      if (!isFiniteNumber(buffer) || buffer <= 0) rowBlockers.push("ULTRA_DRAWDOWN_UNAVAILABLE");
      else if (loss > buffer + 1e-9) rowBlockers.push("ULTRA_DRAWDOWN_EXCEEDED");
      totalRisk += loss;
    }
    totalQty += qtyInt;
    accounts.push({ id: String(row.id || ""), qty: qtyInt, legs,
      riskDollars: isFiniteNumber(loss) ? loss : null,
      drawdownBuffer: isFiniteNumber(buffer) ? buffer : null,
      blockers: [...new Set(rowBlockers)].sort() });
    blockers.push(...rowBlockers);
  }
  if (totalQty > Number(envelope.maxTotalQty)) blockers.push("ULTRA_TOTAL_QTY_EXCEEDS_CONTRACT");
  if (totalRisk > Number(envelope.maxRiskDollarsTotal) + 1e-9) {
    blockers.push("ULTRA_TOTAL_RISK_EXCEEDS_CONTRACT");
  }
  return { ok: blockers.length === 0, blockers: [...new Set(blockers)].sort(),
    accounts, totalQty, totalRiskDollars: Math.round(totalRisk * 100) / 100,
    version: String(envelope.version || "") };
}

/**
 * R84 追撃(pyramid)の算術。**pyramid.py の combined_entry / combined_risk /
 * _fit_to_cap の逐語移植**で、engine / order.py / Worker の三重検証を同一算術に
 * するためのものである。式を片側だけ直すと、数値が割れた瞬間に claim が通って
 * 送信が拒否される(あるいはその逆)ので、直すときは必ず両方を直す。
 * 一致は tests/test_r84_worker_conformance.py が固定する。
 */
export function pyramidCombinedEntry(positionQty, positionEntry, addQty, addPrice) {
  const total = Number(positionQty) + Number(addQty);
  if (!isFiniteNumber(Number(positionQty)) || !isFiniteNumber(Number(positionEntry))
      || !isFiniteNumber(Number(addQty)) || !isFiniteNumber(Number(addPrice))
      || !Number.isFinite(total) || total <= 0) return null;
  return (Number(positionQty) * Number(positionEntry)
          + Number(addQty) * Number(addPrice)) / total;
}

export function pyramidCombinedRisk(positionQty, positionEntry, addQty, addPrice, stop,
                                    pointValue = POINT_VALUE) {
  const average = pyramidCombinedEntry(positionQty, positionEntry, addQty, addPrice);
  if (average === null || !isFiniteNumber(Number(stop)) || !(Number(pointValue) > 0)) return null;
  return Math.abs(Number(stop) - average)
    * (Number(positionQty) + Number(addQty)) * Number(pointValue);
}

/** pyramid.slippage_points の逐語移植。 */
export function pyramidSlippagePoints() {
  const points = Number((executionContract.marketOrder || {}).maxDeviationPoints);
  return Number.isFinite(points) ? points : 0;
}

/** pyramid.adverse_add_price の逐語移植。SELL は安く売れ BUY は高く買わされる。 */
export function pyramidAdversePrice(side, addPrice) {
  if (!isFiniteNumber(Number(addPrice))) return null;
  const points = pyramidSlippagePoints();
  return String(side || "").toUpperCase() === "SELL"
    ? Number(addPrice) - points : Number(addPrice) + points;
}

export function pyramidFitToCap(positionQty, positionEntry, addQty, addPrice, stop,
                                pointValue, capDollars) {
  for (let candidate = Number(addQty); candidate >= 1; candidate -= 1) {
    const risk = pyramidCombinedRisk(positionQty, positionEntry, candidate, addPrice,
                                     stop, pointValue);
    if (risk !== null && risk <= Number(capDollars) + 1e-9) return candidate;
  }
  return 0;
}

export function riskDollars(entry, stop, qty) {
  if (!isFiniteNumber(entry) || !isFiniteNumber(stop) || !isFiniteNumber(qty)) return null;
  return Math.abs(entry - stop) * qty * POINT_VALUE;
}

export function rewardRatio(entry, stop, target) {
  const risk = Math.abs(entry - stop);
  if (!risk) return null;
  return Math.abs(target - entry) / risk;
}

function parseInstant(value) {
  if (typeof value !== "string" || !value) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

/** Pure projection of the shared JSON execution contract. */
export function evaluateExecutionContract(scenario, market, position, order, nowMs) {
  const blockers = [];
  const caps = [];
  const acquisitionRequired = [];
  const now = Number(nowMs);
  const observed = parseInstant(market?.observedAt || market?.at);
  const marketAgeSec = observed === null ? null : (now - observed) / 1000;
  if (observed === null) blockers.push("MARKET_TIMESTAMP_MISSING");
  else if (marketAgeSec < -executionContract.market.futureToleranceSec) blockers.push("MARKET_IN_FUTURE");
  else if (marketAgeSec > executionContract.market.maxAgeSec) blockers.push("MARKET_STALE");

  const issued = parseInstant(scenario?.issuedAt);
  const expires = parseInstant(scenario?.expiresAt);
  const scenarioAgeSec = issued === null ? null : (now - issued) / 1000;
  if (!scenario) blockers.push("SCENARIO_MISSING");
  else if (issued === null || expires === null) blockers.push("SCENARIO_TIMESTAMP_MISSING");
  else if (expires <= now) blockers.push("SCENARIO_EXPIRED");
  else if (scenarioAgeSec > executionContract.scenario.maxAgeSec) blockers.push("SCENARIO_STALE");

  const grade = String(scenario?.grade || "").toUpperCase();
  let effectiveGrade = grade;
  const cvdAt = parseInstant(market?.cvdAt || market?.cvdMeta?.at || market?.ictEvidence?.cvd?.at);
  let cvdUnusable = false;
  if (executionContract.cvd.timestampRequiredForAPlus && cvdAt === null) {
    caps.push("CVD_TIMESTAMP_MISSING");
    cvdUnusable = true;
  } else if (cvdAt !== null && (now - cvdAt) / 1000 > executionContract.cvd.maxAgeSec) {
    caps.push("CVD_STALE");
    cvdUnusable = true;
  }
  if (cvdUnusable) {
    acquisitionRequired.push("CVD_REACQUIRE_REQUIRED");
    if (grade === "A+") {
      effectiveGrade = executionContract.cvd.missingTimestampCap;
      caps.push("CVD_A_PLUS_CAPPED");
      blockers.push("CVD_A_PLUS_PROHIBITED");
    }
  }
  if (!executionContract.scenario.allowedGrades.includes(effectiveGrade)) blockers.push("GRADE_NOT_ORDERABLE");
  if (!executionContract.scenario.allowedStates.includes(String(scenario?.state || "").toUpperCase())) {
    blockers.push("SCENARIO_STATE_NOT_ORDERABLE");
  }
  if (String(scenario?.executionContractVersion || executionContract.version) !== executionContract.version) {
    blockers.push("EXECUTION_CONTRACT_VERSION_MISMATCH");
  }
  if (market?.eventBlackout === true || market?.eventGate === "BLACKOUT") blockers.push("EVENT_BLACKOUT");
  const dayguard = market?.dayguard && typeof market.dayguard === "object" ? market.dayguard : null;
  if (dayguard) {
    const guardAt = parseInstant(dayguard.at);
    if (dayguard.available !== true) blockers.push("DAYGUARD_UNAVAILABLE");
    else if (guardAt === null || (now - guardAt) / 1000 > executionContract.dayguard.maxAgeSec) {
      blockers.push("DAYGUARD_STALE");
    } else if (dayguard.blocked === true) blockers.push("DAYGUARD_BLOCKED");
  }
  const sessionEnd = parseInstant(scenario?.[executionContract.session.endField] || market?.[executionContract.session.endField]);
  if (sessionEnd !== null && now >= sessionEnd) blockers.push("SESSION_ENDED");

  const qty = Number(scenario?.qty);
  const fixedQty = Number(executionContract.risk.fixedQty);
  // ULTRA 検出。凍結された riskCapSource が ULTRA エンベロープの正本値と一致
  // したときだけ、枚数・脚・リスク上限を ULTRA 側の規則で判定する。
  // Python execution_contract.evaluate の ultra_mode と鏡。
  const ultraEnv = executionContract.ultra || {};
  const ultraMode = ultraEnv.enabled === true
    && String(scenario?.executionContract?.riskCapSource || "") === String(ultraEnv.riskCapSource);
  const ultraLegSplit = ultraMode ? ultraSplit(qty) : null;
  if (ultraMode) {
    if (!Number.isInteger(qty) || qty < Number(ultraEnv.minQtyPerAccount)
        || qty > Number(ultraEnv.maxQtyPerAccount) || ultraLegSplit === null) {
      blockers.push("ULTRA_QTY_OUT_OF_ENVELOPE");
    }
    // R109(2026-09-18): ULTRA は複数口座で張れる。上限は契約の ultra.maxAccounts。
    // intent は scope 全体で枚数を 1 つしか持たないので、producer は同じ利益目標の
    // 口座だけを scope に入れ、riskCapDollars を残った口座の最小残ドローダウンで
    // 凍結する。下の cap 判定はその凍結値を使うので、一番薄い口座が正本になる。
    // execution_contract.evaluate(Python)の ULTRA_SCOPE_OUT_OF_ENVELOPE と鏡。
    const ultraScope = Array.isArray(scenario?.executionContract?.accountScope)
      ? scenario.executionContract.accountScope : [];
    if (!ultraScope.length || ultraScope.length > Number(ultraEnv.maxAccounts)) {
      blockers.push("ULTRA_SCOPE_OUT_OF_ENVELOPE");
    }
  } else if (!Number.isInteger(qty) || qty !== fixedQty) blockers.push("FIXED_QTY_REQUIRED");
  const split = executionContract.splitPlan || {};
  const targets = Array.isArray(scenario?.targets) ? scenario.targets : [];
  const legs = Array.isArray(scenario?.legs) ? scenario.legs : [];
  const targetsValid = targets.length === Number(split.targetCount)
    && targets.every((value) => isFiniteNumber(Number(value)))
    && new Set(targets.map(Number)).size === targets.length
    && ((String(scenario?.side).toUpperCase() === "BUY" && Number(scenario?.entry) < Number(targets[0]) && Number(targets[0]) < Number(targets[1]))
      || (String(scenario?.side).toUpperCase() === "SELL" && Number(targets[1]) < Number(targets[0]) && Number(targets[0]) < Number(scenario?.entry)));
  if (!targetsValid) blockers.push(targets.length ? "SPLIT_TARGETS_INVALID" : "SPLIT_PLAN_REQUIRED");
  const expectedLegQty = (index) => (ultraMode && ultraLegSplit
    ? ultraLegSplit[index] : Number(split.legQty));
  const legsValid = targetsValid && legs.length === Number(split.legCount)
    && legs.every((leg, index) => leg && leg.id === (index ? "RUNNER" : "TP1")
      && Number(leg.qty) === expectedLegQty(index) && priceEquals(Number(leg.target), Number(targets[index])))
    && (!split.planVersionRequired || Boolean(scenario?.planVersion));
  if (!legsValid) blockers.push("SPLIT_LEGS_INVALID");
  const risk = riskDollars(scenario?.entry, scenario?.stop, qty);
  // A frozen monitor cap is proof of a tighter limit, never permission to
  // expand the profile's $240 hard maximum.  This mirrors risk_cap() in the
  // Python contract and makes a forged $300 scenario harmless.
  const profileCap = Number(executionContract.risk.defaultCapDollars);
  const frozenCap = Number(scenario?.executionContract?.riskCapDollars);
  let cap;
  let capSource;
  if (ultraMode) {
    // ULTRA: リスク上限の正本は凍結された残ドローダウンと、契約の口座あたり
    // 上限($5,000)の狭い側。RISK_* と $240 既定はここでは使わない。
    const maxAccountRisk = Number(ultraEnv.maxRiskDollarsPerAccount);
    cap = Number.isFinite(frozenCap) && frozenCap > 0
      ? Math.min(frozenCap, maxAccountRisk) : NaN;
    capSource = String(ultraEnv.riskCapSource || "ACCOUNT_DRAWDOWN_BUFFER");
  } else {
    cap = Number.isFinite(frozenCap) && frozenCap > 0 && frozenCap < profileCap
      ? frozenCap : profileCap;
    capSource = Number.isFinite(frozenCap) && frozenCap > 0 && frozenCap < profileCap
      ? String(scenario?.executionContract?.riskCapSource || "scenario.executionContract.riskCapDollars")
      : "execution_contract.defaultCapDollars";
  }
  const invalidGeometry = risk === null || !Number.isFinite(qty) || qty <= 0;
  if (invalidGeometry) blockers.push("RISK_GEOMETRY_INVALID");
  else if (!Number.isFinite(cap) || cap <= 0) blockers.push("RISK_CAP_UNAVAILABLE");
  else if (risk > cap + 1e-9) blockers.push("RISK_CAP_EXCEEDED");
  if (!ultraMode && qty > executionContract.risk.maxQty) blockers.push("QTY_EXCEEDS_CONTRACT");
  if (executionContract.openPositionStates.includes(String(position?.state || "").toUpperCase())) blockers.push("POSITION_OPEN");
  if (executionContract.blockingOrderStates.includes(String(order?.state || "").toUpperCase())) blockers.push("ORDER_PENDING");
  return {
    version: executionContract.version,
    orderable: blockers.length === 0,
    blockers: [...new Set(blockers)].sort(),
    caps: [...new Set(caps)].sort(),
    acquisitionRequired: [...new Set(acquisitionRequired)].sort(),
    effectiveGrade: effectiveGrade || null,
    marketAgeSec,
    scenarioAgeSec,
    // Python represents invalid geometry as null, including qty=0.  Keep the
    // shared contract byte-for-byte meaningful across producer and Worker.
    riskDollars: invalidGeometry ? null : risk,
    riskCapDollars: Number.isFinite(cap) ? cap : null,
    riskCapSource: capSource,
    cvdAt: cvdAt === null ? null : new Date(cvdAt).toISOString(),
    sessionEndAt: sessionEnd === null ? null : new Date(sessionEnd).toISOString(),
  };
}

function marketNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function normalizeMarketBar(raw, index) {
  if (!raw || typeof raw !== "object") return { ok: false, reason: `market.bars[${index}] is not an object` };
  const t = marketNumber(raw.t ?? raw.time);
  const o = marketNumber(raw.o ?? raw.open);
  const h = marketNumber(raw.h ?? raw.high);
  const l = marketNumber(raw.l ?? raw.low);
  const c = marketNumber(raw.c ?? raw.close);
  if (![t, o, h, l, c].every(isFiniteNumber) || t <= 0) {
    return { ok: false, reason: `market.bars[${index}] has invalid OHLC/time` };
  }
  if (h < l || h < Math.max(o, c) || l > Math.min(o, c)) {
    return { ok: false, reason: `market.bars[${index}] has an impossible OHLC range` };
  }
  if (![o, h, l, c].every((value) => priceEquals(value, normalizeTick(value)))) {
    return { ok: false, reason: `market.bars[${index}] is not aligned to the 0.25 tick` };
  }
  const bar = { t, o, h, l, c };
  const volume = marketNumber(raw.v ?? raw.volume);
  if (volume !== null) {
    if (volume < 0) return { ok: false, reason: `market.bars[${index}] has invalid volume` };
    bar.v = volume;
  }
  return { ok: true, bar };
}

/** Market observations are strict because this stream drives the displayed price. */
// ---------------------------------------------------------------- 評価カード
// R6: シナリオ評価の表示用ペイロード(APP_EVAL_DISPLAY_SPEC §2)。
// **判定はしない。表示のために正規化するだけ。** 不正なら evaluation だけを
// 落とし、market ストリーム自体は通す(評価の欠損で価格配信を止めない)。

const EVAL_MAX_BYTES = 4096;
const EVAL_RULINGS = new Set(["通常", "A+のみ", "停止", "不明"]);
const EVAL_VERDICTS = new Set(["OK", "ROTATION", "不明"]);
// 2026-09-04: B も発注可能(ユーザー決定)。allowedGrades は execution_contract.json が正本。
const EVAL_GRADES = new Set(["A+", "A", "B"]);
const EVAL_PHASES = new Set(["EVAL_STRIKE", "PA_HARVEST"]);
const EVAL_MODELS = new Set([
  "VP80_REVERSION", "TURTLE_SOUP_REVERSAL", "BREAKER_CONTINUATION",
  "OTE_FVG_PULLBACK", "FLAT",
]);
const EVAL_DECISION_STATES = new Set(["WATCH", "ARMED", "ACTIVE"]);
const EVAL_FRESHNESS = new Set([
  "FRESH", "WICK_TESTED", "BODY_TESTED", "BROKEN", "RECLAIMED", "FLIPPED", "CONSUMED",
]);
const EVAL_CHAIN_STATES = new Set([
  "SWEEP_CANDIDATE", "SWEEP_CONFIRMED", "MSS_CONFIRMED", "RETEST_HELD",
  "FLIP_BREAK", "FLIP_ACCEPTED", "FLIP_HELD", "EXPIRED",
]);
const EVAL_DATA_STATUS = new Set(["FRESH", "STALE", "MISSING"]);
const EVAL_CVD_STATUS = new Set(["FRESH", "RETRY_REQUIRED", "UNAVAILABLE_A_CAP"]);
const EVAL_CVD_FRESHNESS = new Set(["FRESH", "STALE"]);
const STRATEGY_MAX_BYTES = 24 * 1024;
const STRATEGY_MAX_DEPTH = 6;
const STRATEGY_MAX_KEYS = 32;
const STRATEGY_MAX_ARRAY = 16;
// R39: strategy_evidence.py の TICK_PRICE_KEYS / DERIVED_PRICE_KEYS と
// **一字一句同じ規則**でなければならない。Python が受理した evidence を
// Worker が拒否すると strategyEvidenceError が立ち、_authoritative_cycle_seal
// が CYCLE_MISMATCH で止まる（= 自律 ENTRY が永久に成立しない）。
// 取引価格だけがティック格子に乗る。VP(POC/VAH/VAL)・VWAP・ゾーン中点・
// 50% ウィック・フィボ retracement は定義上乗らない。
const STRATEGY_TICK_PRICE_KEYS = new Set([
  "lo", "hi", "low", "high", "target", "entry", "stop",
  "rangehigh", "rangelow", "asianhigh", "asianlow", "midnightopen",
].map((key) => key.toLowerCase()));

const evalNumber = (value) => {
  if (value === null || value === undefined || typeof value === "boolean") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};
const evalText = (value, allowed) => {
  if (value === null || value === undefined) return null;
  const text = String(value);
  return allowed && !allowed.has(text) ? null : text;
};

const boundedStrategyText = (value, limit = 160) => String(value);
const strategyPriceKey = (key) => {
  const text = String(key || "").toLowerCase();
  return STRATEGY_TICK_PRICE_KEYS.has(text);
};

/**
 * Keep the image/ICT strategy matrix visible without turning the market
 * stream into an unbounded, untyped side channel.  Tradeable price values must
 * be finite MNQ ticks; derived/statistical prices (VP levels, VWAP, zone
 * midpoints, fib retracements) and generic telemetry only need to be finite.
 */
export function normalizeStrategyMatrix(raw) {
  if (raw === undefined || raw === null) return { ok: true, matrix: null };
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return { ok: false, reason: "strategyMatrix is not an object" };
  }
  const normalize = (value, key, depth) => {
    if (value === null) return { ok: true, value: null };
    if (typeof value === "boolean") return { ok: true, value };
    if (typeof value === "string") {
      if (value.length > 160) return { ok: false, reason: `strategyMatrix.${key} exceeds text limit` };
      return { ok: true, value: boundedStrategyText(value) };
    }
    if (typeof value === "number") {
      if (!Number.isFinite(value)) return { ok: false, reason: `strategyMatrix.${key} is not finite` };
      if (strategyPriceKey(key) && !priceEquals(value, normalizeTick(value))) {
        return { ok: false, reason: `strategyMatrix.${key} is not aligned to the 0.25 tick` };
      }
      return { ok: true, value };
    }
    if (depth >= STRATEGY_MAX_DEPTH) {
      return { ok: false, reason: "strategyMatrix exceeds maximum depth" };
    }
    if (Array.isArray(value)) {
      if (value.length > STRATEGY_MAX_ARRAY) {
        return { ok: false, reason: "strategyMatrix exceeds maximum array length" };
      }
      const out = [];
      for (const item of value) {
        const child = normalize(item, key, depth + 1);
        if (!child.ok) return child;
        out.push(child.value);
      }
      return { ok: true, value: out };
    }
    if (!value || typeof value !== "object") {
      return { ok: false, reason: `strategyMatrix.${key} has unsupported type` };
    }
    const out = {};
    const childKeys = Object.keys(value);
    if (childKeys.length > STRATEGY_MAX_KEYS) {
      return { ok: false, reason: "strategyMatrix exceeds maximum object keys" };
    }
    for (const childKey of childKeys) {
      if (String(childKey).length > 64) {
        return { ok: false, reason: "strategyMatrix exceeds key length limit" };
      }
      const child = normalize(value[childKey], childKey, depth + 1);
      if (!child.ok) return child;
      out[childKey] = child.value;
    }
    return { ok: true, value: out };
  };
  const normalized = normalize(raw, "strategyMatrix", 0);
  if (!normalized.ok) return normalized;
  if (new TextEncoder().encode(JSON.stringify(normalized.value)).length > STRATEGY_MAX_BYTES) {
    return { ok: false, reason: "strategyMatrix exceeds byte limit" };
  }
  return { ok: true, matrix: normalized.value };
}

// Sync SHA-256 keeps Durable Object validation deterministic without a
// network dependency.  It hashes the exact canonical UTF-8 bytes used by the
// Python producer (sorted object keys; no pretty-printing).
function sha256Hex(bytes) {
  const K = [
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2,
  ];
  const data = Array.from(bytes); const bitLength = data.length * 8;
  data.push(0x80); while ((data.length % 64) !== 56) data.push(0);
  for (let i = 7; i >= 0; i -= 1) data.push(Math.floor(bitLength / (2 ** (i * 8))) & 0xff);
  let h0=0x6a09e667,h1=0xbb67ae85,h2=0x3c6ef372,h3=0xa54ff53a,h4=0x510e527f,h5=0x9b05688c,h6=0x1f83d9ab,h7=0x5be0cd19;
  for (let offset=0; offset<data.length; offset+=64) {
    const w = new Array(64);
    for (let i=0;i<16;i+=1) w[i]=((data[offset+i*4]<<24)|(data[offset+i*4+1]<<16)|(data[offset+i*4+2]<<8)|data[offset+i*4+3])>>>0;
    for (let i=16;i<64;i+=1) { const a=w[i-15], b=w[i-2]; const s0=((a>>>7)|(a<<25))^((a>>>18)|(a<<14))^(a>>>3); const s1=((b>>>17)|(b<<15))^((b>>>19)|(b<<13))^(b>>>10); w[i]=(w[i-16]+s0+w[i-7]+s1)>>>0; }
    let a=h0,b=h1,c=h2,d=h3,e=h4,f=h5,g=h6,h=h7;
    for (let i=0;i<64;i+=1) { const s1=((e>>>6)|(e<<26))^((e>>>11)|(e<<21))^((e>>>25)|(e<<7)); const ch=(e&f)^((~e)&g); const t1=(h+s1+ch+K[i]+w[i])>>>0; const s0=((a>>>2)|(a<<30))^((a>>>13)|(a<<19))^((a>>>22)|(a<<10)); const maj=(a&b)^(a&c)^(b&c); const t2=(s0+maj)>>>0; h=g;g=f;f=e;e=(d+t1)>>>0;d=c;c=b;b=a;a=(t1+t2)>>>0; }
    h0=(h0+a)>>>0;h1=(h1+b)>>>0;h2=(h2+c)>>>0;h3=(h3+d)>>>0;h4=(h4+e)>>>0;h5=(h5+f)>>>0;h6=(h6+g)>>>0;h7=(h7+h)>>>0;
  }
  return [h0,h1,h2,h3,h4,h5,h6,h7].map((word) => word.toString(16).padStart(8,"0")).join("");
}

function stableJson(value) {
  if (value === null || typeof value === "boolean" || typeof value === "number" || typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(",")}}`;
}

export function strategyEvidenceHash(evidence) {
  const material = {
    version: evidence.version, asOf: evidence.asOf, sessionId: evidence.sessionId,
    source: evidence.source, provenance: evidence.provenance, models: evidence.models,
  };
  return `se_${sha256Hex(new TextEncoder().encode(stableJson(material))).slice(0, 24)}`;
}

const EXECUTION_INTENT_FIELDS = new Set([
  "version", "symbol", "side", "qty", "orderType", "entry", "last", "stop",
  "targets", "legs", "planVersion", "executionContractVersion", "accountScope",
]);
// R84: 追撃だけが載せる **任意** フィールド。キーが無い intent のバイト列は
// 1 文字も変わらないので、既存の xi_ ハッシュは全部そのまま通る
// (`pyramid: null` を常に足す案はハッシュを全部壊すので採らない)。
// Python 側は execution_intent.PYRAMID_FIELD / PYRAMID_SUBFIELDS と同一規則。
const EXECUTION_INTENT_PYRAMID_FIELD = "pyramid";

function normalizeIntentPyramid(raw) {
  if (raw === undefined || raw === null) return { ok: true, pyramid: null };
  if (typeof raw !== "object" || Array.isArray(raw)) return { ok: false };
  const keys = Object.keys(raw);
  if (keys.length !== 2 || !keys.includes("baseQty") || !keys.includes("addQty")) {
    return { ok: false };
  }
  const baseQty = Number(raw.baseQty);
  const addQty = Number(raw.addQty);
  if (!Number.isInteger(baseQty) || baseQty <= 0) return { ok: false };
  if (!Number.isInteger(addQty) || addQty <= 0) return { ok: false };
  return { ok: true, pyramid: { baseQty, addQty } };
}

function intentTickText(value) {
  const normalized = normalizeTick(Number(value));
  if (normalized === null || normalized <= 0 || !priceEquals(normalized, Number(value))) return null;
  return normalized.toFixed(2);
}

function normalizeAccountScope(raw) {
  if (!Array.isArray(raw) || !raw.length) return null;
  const values = [...new Set(raw.map((value) => String(value || "").trim()).filter(Boolean))].sort();
  // maxAccounts: null = 上限なし(明示的な撤廃)。キーが無い/壊れているときだけ
  // 従来どおり 1 に倒す —— Python 側 execution_intent.normalize_account_scope と同一規則。
  const limit = executionContract.accountMode?.maxAccounts;
  const overLimit = limit === undefined
    ? values.length > 1
    : limit !== null && values.length > Number(limit);
  if (values.length !== raw.length
      || overLimit
      || values.some((value) => !/^[A-Za-z0-9_-]{3,64}$/.test(value))) return null;
  return values;
}

export function normalizeExecutionIntent(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return { ok: false, reason: "executionIntent fields are invalid" };
  }
  const intentKeys = Object.keys(raw);
  const hasPyramid = intentKeys.includes(EXECUTION_INTENT_PYRAMID_FIELD);
  if (intentKeys.length !== EXECUTION_INTENT_FIELDS.size + (hasPyramid ? 1 : 0)
      || intentKeys.some((key) => !EXECUTION_INTENT_FIELDS.has(key)
                                  && key !== EXECUTION_INTENT_PYRAMID_FIELD)) {
    return { ok: false, reason: "executionIntent fields are invalid" };
  }
  const pyramidField = normalizeIntentPyramid(raw[EXECUTION_INTENT_PYRAMID_FIELD]);
  if (!pyramidField.ok) return { ok: false, reason: "executionIntent pyramid is invalid" };
  const symbol = String(raw.symbol || "").trim();
  const side = String(raw.side || "").toUpperCase();
  const qty = Number(raw.qty);
  const orderType = String(raw.orderType || "").toUpperCase();
  const entry = raw.entry === null ? null : intentTickText(raw.entry);
  const last = raw.last === null ? null : intentTickText(raw.last);
  const stop = intentTickText(raw.stop);
  const targets = Array.isArray(raw.targets) ? raw.targets.map(intentTickText) : [];
  const scope = normalizeAccountScope(raw.accountScope);
  // 通常経路は固定枚数のまま。ULTRA が有効な契約でだけ口座あたり上限までを
  // 受け入れる。execution_intent.build() と同一規則。
  const intentFixedQty = Number(executionContract.risk.fixedQty);
  const intentUltra = executionContract.ultra || {};
  const qtyAllowed = qty === intentFixedQty
    || (Boolean(intentUltra.enabled) && Number.isInteger(qty)
      && qty >= Number(intentUltra.minQtyPerAccount)
      && qty <= Number(intentUltra.maxQtyPerAccount));
  if (!symbol || symbol.length > 32 || !["BUY", "SELL"].includes(side)
      || !qtyAllowed
      || !["LIMIT", "MARKET"].includes(orderType)
      || (orderType === "LIMIT" ? entry === null || last !== null : last === null || entry !== null)
      || stop === null || targets.length !== 2 || targets.some((value) => value === null)
      || !scope || String(raw.version || "") !== String(executionContract.intent.version)
      || String(raw.executionContractVersion || "") !== String(executionContract.version)) {
    return { ok: false, reason: "executionIntent canonical fields are invalid" };
  }
  const basis = Number(orderType === "LIMIT" ? entry : last);
  const directional = side === "BUY"
    ? Number(stop) < basis && basis < Number(targets[0]) && Number(targets[0]) < Number(targets[1])
    : Number(targets[1]) < Number(targets[0]) && Number(targets[0]) < basis && basis < Number(stop);
  // 脚の枚数は総枚数から一意に決まる。申告された脚は採用せず、突き合わせる
  // だけ(execution_intent.build() と同一)。
  if (pyramidField.pyramid) {
    // 追撃の intent は **足す枚数** を運ぶ。合計枚数の上限はここで、金額の合計
    // リスクは pyramidCombinedRisk が claim 側で見る(Python 側と同じ分担)。
    const maxAccountQty = Number(intentUltra.maxQtyPerAccount || 0);
    if (intentUltra.enabled !== true || orderType !== "MARKET"
        || pyramidField.pyramid.addQty !== qty
        || (maxAccountQty && pyramidField.pyramid.baseQty + qty > maxAccountQty)) {
      return { ok: false, reason: "executionIntent pyramid envelope is invalid" };
    }
  }
  const intentLegQty = qty === intentFixedQty ? [1, 1] : ultraSplit(qty);
  if (intentLegQty === null) {
    return { ok: false, reason: "executionIntent split is not representable" };
  }
  const expectedLegs = [
    { id: "TP1", qty: intentLegQty[0], target: targets[0] },
    { id: "RUNNER", qty: intentLegQty[1], target: targets[1] },
  ];
  const legs = Array.isArray(raw.legs) ? raw.legs.map((leg) => ({
    id: String(leg?.id || ""), qty: Number(leg?.qty), target: intentTickText(leg?.target),
  })) : [];
  const planVersion = String(raw.planVersion || "").trim();
  if (!directional || legs.length !== 2 || stableJson(legs) !== stableJson(expectedLegs)
      || !planVersion || planVersion.length > 64) {
    return { ok: false, reason: "executionIntent geometry or split plan is invalid" };
  }
  const intent = {
    version: String(executionContract.intent.version), symbol, side, qty, orderType,
    entry, last, stop, targets, legs: expectedLegs, planVersion,
    executionContractVersion: String(executionContract.version), accountScope: scope,
  };
  if (pyramidField.pyramid) intent[EXECUTION_INTENT_PYRAMID_FIELD] = pyramidField.pyramid;
  return { ok: true, intent };
}

export function executionIntentHash(raw) {
  const checked = normalizeExecutionIntent(raw);
  if (!checked.ok) return null;
  return `xi_${sha256Hex(new TextEncoder().encode(stableJson(checked.intent)))}`;
}

function buildExecutionIntent(state, orderType, frozenMarketLast = null, pyramid = null) {
  const scenario = state.scenario;
  const market = state.market;
  const type = String(orderType || "").toUpperCase();
  if (!scenario || !["LIMIT", "MARKET"].includes(type)) {
    return { ok: false, reason: "ENTRY_CLAIM_ORDER_TYPE_INVALID" };
  }
  if (type === "MARKET" && (market?.verified !== true || intentTickText(market?.price) === null)) {
    return { ok: false, reason: "ENTRY_CLAIM_MARKET_UNVERIFIED" };
  }
  const scope = scenario.executionContract?.accountScope;
  // R84: 追撃の intent が運ぶ枚数はシナリオの **合計目標** ではなく足す枚数。
  // 脚は枚数から一意に決まる(normalizeExecutionIntent が突き合わせる)ので、
  // ここでも同じ規則で組む —— CONSUME の byte 比較が通る唯一の形である。
  const addQty = pyramid ? Number(pyramid.addQty) : null;
  const intentQty = addQty !== null ? addQty : scenario.qty;
  const targets = Array.isArray(scenario.targets) ? scenario.targets : [];
  const addSplit = addQty !== null ? ultraSplit(addQty) : null;
  if (addQty !== null && (!addSplit || targets.length !== 2)) {
    return { ok: false, reason: "ENTRY_CLAIM_PYRAMID_SPLIT_INVALID" };
  }
  const intentLegs = addSplit
    ? [{ id: "TP1", qty: addSplit[0], target: targets[0] },
       { id: "RUNNER", qty: addSplit[1], target: targets[1] }]
    : scenario.legs;
  const raw = {
    version: executionContract.intent.version,
    symbol: scenario.symbol, side: scenario.side, qty: intentQty, orderType: type,
    entry: type === "LIMIT" ? scenario.entry : null,
    // MARKET freezes the quoted last in the claim.  A later current quote is
    // checked independently for freshness/deviation; it must not mutate the
    // cryptographically bound execution intent.
    last: type === "MARKET" && frozenMarketLast !== null ? frozenMarketLast
      : (type === "MARKET" ? market.price : null),
    stop: scenario.stop, targets: scenario.targets, legs: intentLegs,
    planVersion: scenario.planVersion,
    executionContractVersion: scenario.executionContractVersion,
    accountScope: scope,
  };
  if (pyramid) {
    raw[EXECUTION_INTENT_PYRAMID_FIELD] = {
      baseQty: Number(pyramid.baseQty), addQty: Number(pyramid.addQty),
    };
  }
  return normalizeExecutionIntent(raw);
}

export function entryKeyForTuple(tuple) {
  if (!tupleMatches(tuple, tuple)) return null;
  return `ENTRY:${sha256Hex(new TextEncoder().encode(stableJson(tuple)))}`;
}

/** The one supported strategy payload shape after R14 producer migration. */
export function normalizeStrategyEvidence(raw) {
  if (raw === undefined || raw === null) return { ok: true, evidence: null };
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return { ok: false, reason: "strategyEvidence must be an object" };
  }
  const allowed = new Set(["version", "asOf", "sessionId", "source", "provenance", "models", "evidenceHash"]);
  if (Object.keys(raw).some((key) => !allowed.has(key))) {
    return { ok: false, reason: "strategyEvidence contains unknown top-level keys" };
  }
  for (const [key, limit] of [["version", 64], ["sessionId", 96], ["source", 96], ["provenance", 96]]) {
    if (raw[key] !== undefined && raw[key] !== null && typeof raw[key] !== "string") {
      return { ok: false, reason: `strategyEvidence.${key} must be text` };
    }
    if (raw[key] !== undefined && String(raw[key]).length > limit) {
      return { ok: false, reason: `strategyEvidence.${key} exceeds text limit` };
    }
  }
  const normalized = normalizeStrategyMatrix(raw);
  if (!normalized.ok) return normalized;
  const evidence = normalized.matrix;
  if (!evidence || evidence.version !== "R14-STRATEGY-EVIDENCE-1") {
    return { ok: false, reason: "strategyEvidence.version is unsupported" };
  }
  if (parseInstant(evidence.asOf) === null || !String(evidence.sessionId || "").trim()
      || !String(evidence.source || "").trim() || !String(evidence.provenance || "").trim()
      || !evidence.models || typeof evidence.models !== "object" || Array.isArray(evidence.models)) {
    return { ok: false, reason: "strategyEvidence canonical fields are required" };
  }
  if (!/^se_[a-f0-9]{24}$/i.test(String(evidence.evidenceHash || ""))) {
    return { ok: false, reason: "strategyEvidence.evidenceHash is invalid" };
  }
  if (strategyEvidenceHash(evidence) !== String(evidence.evidenceHash)) {
    return { ok: false, reason: "strategyEvidence.evidenceHash does not match canonical bytes" };
  }
  return { ok: true, evidence };
}

/** evaluation を正規化する。表示できない形なら null を返す(拒否ではない)。 */
export function normalizeEvaluation(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const vol = raw.volGate && typeof raw.volGate === "object" ? raw.volGate : null;
  const rot = raw.rotation && typeof raw.rotation === "object" ? raw.rotation : null;
  if (!vol || !rot) return null;

  const out = {
    at: raw.at ? String(raw.at) : null,
    volGate: {
      noise: evalNumber(vol.noise),
      slCap: evalNumber(vol.slCap),
      ratio: evalNumber(vol.ratio),
      ruling: evalText(vol.ruling, EVAL_RULINGS) || "不明",
    },
    rotation: {
      signals: evalNumber(rot.signals) ?? 0,
      negations: evalNumber(rot.negations) ?? 0,
      verdict: evalText(rot.verdict, EVAL_VERDICTS) || "不明",
    },
    summary: raw.summary ? String(raw.summary).slice(0, 240) : "",
  };

  const data = raw.dataGate;
  if (data && typeof data === "object" && !Array.isArray(data)) {
    out.dataGate = {
      status: evalText(data.status, EVAL_DATA_STATUS) || "MISSING",
      requiredFresh: data.requiredFresh === true,
      freshCount: evalNumber(data.freshCount),
      requiredCount: evalNumber(data.requiredCount),
      oldestAgeSec: evalNumber(data.oldestAgeSec),
      sourceSpanSec: evalNumber(data.sourceSpanSec),
      staleRequired: Array.isArray(data.staleRequired)
        ? data.staleRequired.slice(0, 8).map((value) => String(value).slice(0, 48)) : [],
    };
  }

  const cvd = raw.cvdGate;
  if (cvd && typeof cvd === "object" && !Array.isArray(cvd)) {
    out.cvdGate = {
      status: evalText(cvd.status, EVAL_CVD_STATUS),
      available: cvd.available === true,
      freshness: evalText(cvd.freshness, EVAL_CVD_FRESHNESS),
      aplusAllowed: cvd.aplusAllowed === true,
      attempts: evalNumber(cvd.attempts),
      maxAttempts: evalNumber(cvd.maxAttempts),
      refreshRequired: cvd.refreshRequired === true,
      reason: cvd.reason ? String(cvd.reason).slice(0, 64) : null,
    };
  }

  const session = raw.sessionGate;
  if (session && typeof session === "object" && !Array.isArray(session)) {
    out.sessionGate = {
      window: session.window ? String(session.window).slice(0, 32) : null,
      label: session.label ? String(session.label).slice(0, 48) : null,
      tradeable: typeof session.tradeable === "boolean" ? session.tradeable : null,
      et: session.et ? String(session.et).slice(0, 8) : null,
      note: session.note ? String(session.note).slice(0, 120) : null,
    };
  }

  const m = raw.msnr;
  if (m && typeof m === "object" && !Array.isArray(m)) {
    out.msnr = {
      label: m.label ? String(m.label).slice(0, 48) : null,
      price: evalNumber(m.price),
      tier: evalNumber(m.tier),
      confluence: evalNumber(m.confluence),
      freshness: evalText(m.freshness, EVAL_FRESHNESS),
      side: ["BUY", "SELL"].includes(m.side) ? m.side : null,
      chainType: ["SWEEP", "FLIP"].includes(m.chainType) ? m.chainType : null,
      chainState: evalText(m.chainState, EVAL_CHAIN_STATES),
      barsLeft: evalNumber(m.barsLeft),
      allowed: m.allowed === true,
      grade: evalText(m.grade, EVAL_GRADES),
      blockers: Array.isArray(m.blockers)
        ? m.blockers.slice(0, 8).map((code) => String(code).slice(0, 40))
        : [],
    };
  }

  const h = raw.htf;
  if (h && typeof h === "object" && !Array.isArray(h)) {
    out.htf = { ctTrend: evalNumber(h.ctTrend), ctAtr: evalNumber(h.ctAtr),
                aligned: h.aligned === true };
  }
  const e = raw.entry;
  if (e && typeof e === "object" && !Array.isArray(e)) {
    out.entry = { structure: evalNumber(e.structure), airPt: evalNumber(e.airPt),
                  airPct: evalNumber(e.airPct), reachR: evalNumber(e.reachR),
                  pass: e.pass === true };
  }
  const t = raw.tp;
  if (t && typeof t === "object" && !Array.isArray(t)) {
    out.tp = { bars15: evalNumber(t.bars15), pass: t.pass === true };
  }

  // R11-D: ICT/SMT/VPで決めた最有力1件。ここでも判定を再計算せず、
  // 価格と等級を表示可能な形へ正規化するだけに留める。
  const d = raw.decision;
  if (d && typeof d === "object" && !Array.isArray(d)) {
    const targets = Array.isArray(d.targets)
      ? d.targets.map(evalNumber).filter((value) => value !== null).slice(0, 3) : [];
    const targetR = Array.isArray(d.targetR)
      ? d.targetR.map(evalNumber).filter((value) => value !== null).slice(0, 3) : [];
    out.decision = {
      decisionId: d.decisionId ? String(d.decisionId).slice(0, 40) : null,
      phase: EVAL_PHASES.has(String(d.phase)) ? String(d.phase) : null,
      model: EVAL_MODELS.has(String(d.model)) ? String(d.model) : "FLAT",
      side: ["BUY", "SELL", "FLAT"].includes(String(d.side)) ? String(d.side) : "FLAT",
      state: EVAL_DECISION_STATES.has(String(d.state)) ? String(d.state) : "WATCH",
      grade: EVAL_GRADES.has(d.grade) ? String(d.grade) : null,
      score: evalNumber(d.score),
      entryMode: d.entryMode ? String(d.entryMode).slice(0, 40) : null,
      entry: evalNumber(d.entry), stop: evalNumber(d.stop), targets, targetR,
      hardBlockers: Array.isArray(d.hardBlockers)
        ? d.hardBlockers.slice(0, 8).map((value) => String(value).slice(0, 40)) : [],
      penalties: Array.isArray(d.penalties)
        ? d.penalties.slice(0, 8).map((value) => String(value).slice(0, 40)) : [],
      evidence: Array.isArray(d.evidence)
        ? d.evidence.slice(0, 8).map((value) => String(value).slice(0, 40)) : [],
      modelRank: Array.isArray(d.modelRank)
        ? d.modelRank.slice(0, 4).map((value) => String(value).slice(0, 48)) : [],
      strategyModels: Array.isArray(d.strategyModels)
        ? d.strategyModels.slice(0, 12).map((value) => boundedStrategyText(value, 48)) : [],
      strategyAlignment: evalNumber(d.strategyAlignment),
      strategyBias: ["BUY", "SELL", "FLAT"].includes(String(d.strategyBias))
        ? String(d.strategyBias) : null,
    };
  }

  const a = raw.advisory;
  if (a && typeof a === "object" && !Array.isArray(a)) {
    const advisory = {};
    if (a.vwap && typeof a.vwap === "object") {
      advisory.vwap = {
        side: ["BUY", "SELL"].includes(a.vwap.side) ? a.vwap.side : null,
        state: a.vwap.state ? String(a.vwap.state).slice(0, 32) : null,
        drift: evalNumber(a.vwap.drift),
      };
    }
    if (a.vp && typeof a.vp === "object") {
      advisory.vp = {
        side: ["BUY", "SELL"].includes(a.vp.side) ? a.vp.side : null,
        state: a.vp.state ? String(a.vp.state).slice(0, 32) : null,
        target: evalNumber(a.vp.target),
      };
    }
    if (a.strategies && typeof a.strategies === "object" && !Array.isArray(a.strategies)) {
      advisory.strategies = {
        version: a.strategies.version ? boundedStrategyText(a.strategies.version, 64) : null,
        active: Array.isArray(a.strategies.active)
          ? a.strategies.active.slice(0, 12).map((value) => boundedStrategyText(value, 48)) : [],
        bias: ["BUY", "SELL", "FLAT"].includes(String(a.strategies.bias))
          ? String(a.strategies.bias) : null,
        buy: evalNumber(a.strategies.buy),
        sell: evalNumber(a.strategies.sell),
        summary: a.strategies.summary ? boundedStrategyText(a.strategies.summary, 160) : "",
      };
    }
    if (Object.keys(advisory).length) out.advisory = advisory;
  }

  // サイズ上限。decisionの順位・説明を先に落とし、価格判定の骨格は残す。
  const size = (obj) => new TextEncoder().encode(JSON.stringify(obj)).length;
  if (size(out) > EVAL_MAX_BYTES && out.decision) {
    delete out.decision.modelRank;
    delete out.decision.evidence;
    delete out.decision.penalties;
  }
  if (size(out) > EVAL_MAX_BYTES && out.advisory) {
    // Retain the compact strategy summary before optional legacy advisor chips.
    delete out.advisory.vwap;
    delete out.advisory.vp;
    if (!Object.keys(out.advisory).length) delete out.advisory;
  }
  if (size(out) > EVAL_MAX_BYTES && out.msnr) out.msnr.blockers = [];
  if (size(out) > EVAL_MAX_BYTES) return null;
  return out;
}

/**
 * R56: チャート用の指標束(表示専用)。monitor_publish.build_indicators と対。
 * 数値は有限のものだけ、文字列は短く切る。判定には一切使わない。
 */
const INDICATOR_HISTORY_LIMIT = 24;
function normalizeIndicators(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const num = (value) => marketNumber(value);
  const text = (value, limit = 32) => (value === null || value === undefined || value === ""
    ? null : String(value).slice(0, limit));
  const out = {};
  for (const key of ["vwapHi", "vwapLo", "vwapAnchorT"]) {
    const parsed = num(raw[key]);
    if (parsed !== null) out[key] = parsed;
  }
  if (raw.cvd && typeof raw.cvd === "object" && !Array.isArray(raw.cvd)) {
    const cvd = {};
    for (const key of ["value", "fast", "slow"]) {
      const parsed = num(raw.cvd[key]);
      if (parsed !== null) cvd[key] = parsed;
    }
    if (text(raw.cvd.bias, 16)) cvd.bias = text(raw.cvd.bias, 16);
    if (text(raw.cvd.status, 16)) cvd.status = text(raw.cvd.status, 16);
    if (Array.isArray(raw.cvd.history)) {
      cvd.history = raw.cvd.history.map(num).filter((value) => value !== null).slice(-INDICATOR_HISTORY_LIMIT);
    }
    if (Object.keys(cvd).length) out.cvd = cvd;
  }
  if (text(raw.po3)) out.po3 = text(raw.po3);
  if (raw.smt && typeof raw.smt === "object" && !Array.isArray(raw.smt)) {
    const smt = {};
    if (text(raw.smt.peer, 16)) smt.peer = text(raw.smt.peer, 16);
    if (text(raw.smt.bias, 16)) smt.bias = text(raw.smt.bias, 16);
    if (typeof raw.smt.agree === "boolean") smt.agree = raw.smt.agree;
    if (num(raw.smt.position) !== null) smt.position = num(raw.smt.position);
    if (Object.keys(smt).length) out.smt = smt;
  }
  if (raw.ct && typeof raw.ct === "object" && !Array.isArray(raw.ct)) {
    const ct = {};
    for (const key of ["trend", "atr", "fib618", "fib786", "fib886", "trail", "swing100",
      "ltfTrend", "ltfAtr", "ltfFib618", "ltfTrail", "htfAtr", "htfFib618", "htfTrail",
      "hardStop", "invalidation"]) {
      const parsed = num(raw.ct[key]);
      if (parsed !== null) ct[key] = parsed;
    }
    if (Object.keys(ct).length) out.ct = ct;
  }
  if (text(raw.event, 24)) out.event = text(raw.event, 24);
  return Object.keys(out).length ? out : null;
}

export function validateMarket(raw, nowMs) {
  if (!raw || typeof raw !== "object") return { ok: false, reason: "market is not an object" };
  if (raw.verified !== true) return { ok: false, reason: "market.verified must be true" };

  const observedAt = String(raw.observedAt || raw.at || "");
  const observedMs = parseInstant(observedAt);
  if (observedMs === null) return { ok: false, reason: "market.observedAt is invalid" };
  const ageMs = nowMs - observedMs;
  if (ageMs < -MARKET_FUTURE_TOLERANCE_MS) return { ok: false, reason: "market observation is in the future" };
  if (ageMs > MARKET_MAX_AGE_MS) return { ok: false, reason: "market observation is stale" };

  const source = String(raw.source || "").trim();
  if (raw.cycleId !== undefined && (!String(raw.cycleId).trim() || String(raw.cycleId).length > 96)) {
    return { ok: false, reason: "market.cycleId is invalid" };
  }
  const sourceSymbol = String(raw.sourceSymbol || "").trim();
  if (!source) return { ok: false, reason: "market.source is required" };
  if (!/MNQ/i.test(sourceSymbol)) return { ok: false, reason: "market.sourceSymbol must identify MNQ" };
  if (String(raw.barResolution || raw.resolution || "") !== "3") {
    return { ok: false, reason: "market.barResolution must be verified 3-minute bars" };
  }

  const price = marketNumber(raw.price);
  if (price === null || !priceEquals(price, normalizeTick(price))) {
    return { ok: false, reason: "market.price must be a finite 0.25-tick value" };
  }
  if (!Array.isArray(raw.bars) || raw.bars.length < 2) {
    return { ok: false, reason: "market.bars requires at least two observed 3-minute bars" };
  }
  const advertisedBars = raw.barsTruncatedFrom === undefined || raw.barsTruncatedFrom === null
    ? raw.bars.length : Number(raw.barsTruncatedFrom);
  if (!Number.isInteger(advertisedBars) || advertisedBars < raw.bars.length || advertisedBars > 100_000) {
    return { ok: false, reason: "market.barsTruncatedFrom is invalid" };
  }
  const bars = [];
  let previousTime = -Infinity;
  // 2026-08-16: 60 → 240。表示本数はユーザーがデモで選定する(HANDOFF §15)。
  // producer 側(monitor_publish)の NQX_FEED_BARS が実際の送信本数を決める。
  for (const [index, item] of raw.bars.slice(-240).entries()) {
    const normalized = normalizeMarketBar(item, index);
    if (!normalized.ok) return normalized;
    if (normalized.bar.t <= previousTime) {
      return { ok: false, reason: "market.bars must be strictly chronological" };
    }
    previousTime = normalized.bar.t;
    bars.push(normalized.bar);
  }

  const optionalNumber = (value) => {
    const parsed = marketNumber(value);
    return parsed === null ? null : parsed;
  };
  // A malformed strategy payload is isolated to evidence=null.  It must not
  // make an independently verified market observation disappear.
  const strategy = normalizeStrategyEvidence(raw.strategyEvidence);
  return {
    ok: true,
    market: {
      at: new Date(observedMs).toISOString(),
      observedAt: new Date(observedMs).toISOString(),
      price,
      change: optionalNumber(raw.change),
      changePct: optionalNumber(raw.changePct),
      vwap: optionalNumber(raw.vwap),
      cvd: optionalNumber(raw.cvd),
      regime: raw.regime ? String(raw.regime) : null,
      source,
      sourceSymbol,
      resolution: raw.resolution ? String(raw.resolution) : null,
      barResolution: "3",
      verified: true,
      bars,
      barsTruncatedFrom: advertisedBars > bars.length ? advertisedBars : null,
      // R31: レベル一覧フィードが「意識している水準」を全部描けるよう 30 本まで。
      // monitor_publish.FEED_LEVELS_MAX と同値に保つこと。
      levels: Array.isArray(raw.levels) ? raw.levels.slice(0, 30) : [],
      // A malformed observational strategy payload must not erase a verified
      // market; it is isolated as null and its reason remains auditable.
      strategyEvidence: strategy.ok ? strategy.evidence : null,
      strategyEvidenceError: strategy.ok ? null : strategy.reason,
      cvdAt: raw.cvdAt ? String(raw.cvdAt) : null,
      cycleId: raw.cycleId ? String(raw.cycleId).slice(0, 96) : null,
      eventBlackout: raw.eventBlackout === true,
      dayguard: raw.dayguard && typeof raw.dayguard === "object" && !Array.isArray(raw.dayguard)
        ? {
            at: parseInstant(raw.dayguard.at) === null ? null : new Date(parseInstant(raw.dayguard.at)).toISOString(),
            available: raw.dayguard.available === true,
            blocked: raw.dayguard.blocked === true,
            codes: Array.isArray(raw.dayguard.codes)
              ? raw.dayguard.codes.slice(0, 8).map((value) => String(value).slice(0, 64)) : [],
          }
        : null,
      sessionEndAt: raw.sessionEndAt ? String(raw.sessionEndAt) : null,
      executionContractVersion: raw.executionContractVersion
        ? String(raw.executionContractVersion) : executionContract.version,
      // R6: 表示専用。壊れていれば null になるだけで market は通る。
      evaluation: normalizeEvaluation(raw.evaluation),
      // R56: チャート用の指標束(表示専用)。壊れていれば null。
      indicators: normalizeIndicators(raw.indicators),
    },
  };
}

// ---------------------------------------------------------------- 初期状態

export function emptyState(accountId, symbol) {
  return {
    version: STATE_VERSION,
    accountId,
    symbol,
    seq: 0,
    revisions: { scenario: 0, position: 0, order: 0, market: 0, result: 0, account: 0, cycle: 0, entry_claim: 0, management_claim: 0, broker_observation: 0, autotrade: 0, cycle_health: 0, account_prefs: 0, manual_halt: 0 },
    scenario: null,
    position: null,
    // position が null でも、照会未確認なら新規発注を許可しない。
    // 旧 state にこのフィールドが無い場合も projectState は fail-closed。
    positionCheck: null,
    order: null,
    entryClaim: null,
    // A recovered execution tuple is permanently burned.  Clearing a local
    // ledger, rotating a nonce, or restarting a producer can never make the
    // same authoritative tuple sendable again.
    entryTombstones: [],
    entryRelease: null,
    managementClaim: null,
    brokerObservation: null,
    market: null,
    cycleLeaseExpiresAt: null,
    rollbackCycleId: null,
    result: null,
    // 決済履歴(直近 RESULT_LOG_LIMIT 件)。result のクリアでは消えない。
    resultLog: [],
    // 口座別の残機(LIFELINE)。PC 側が env から publish する。
    accounts: null,
    // Telegram Mini App の AUTO スイッチが発行する期限付きの新規ENTRY権限。
    // live=true でも、実際の注文はローカル autotrade_engine の全ゲートを通る。
    autotradeArm: null,
    // 監視ループのビーコン(「監視の監視」)。BLOCKED/HALT のサイクルは
    // market/scenario を publish しないので、生死と理由はこの別便で届く。
    // 表示専用 — display.orderable や実行契約の判定には一切使わない。
    cycleHealth: null,
    // 口座別のユーザー設定(ULTRA の対象口座・利益目標・DD上限)。認証済み
    // Mini App だけが書き、監視PCが毎サイクル読む。実際の枚数・リスクは
    // PC 側の ultra_mode + 実行契約エンベロープが必ず再計算・クランプする。
    accountPrefs: null,
    // R87: 設定ページの手動HALT(R82 の manualHalt.autotrade と同じ意味)。認証済み
    // Mini App だけが書き、監視PCが発注判断の直前に読む。立っている間は新規・追撃・
    // 建玉管理が止まり、撤退(KILL / flatten)だけが通る。期限は無い —— 人が外すまで続く。
    manualHalt: null,
    lastVerifiedAt: null,
  };
}

export const MANUAL_HALT_SCHEMA = "NQX_MANUAL_HALT/1";

/**
 * R87: 手動HALT の切り替えを組み立てる純関数(DO はこれを保存するだけ)。
 *
 * body は `{ enabled: boolean, reason?: string }`。enabled が真偽値でなければ拒否する
 * (「たぶん ON」を作らない)。同じ値の再送も受け付けて updatedAt を進める —— 押した
 * 事実を監査に残すため。
 */
export function buildManualHalt(previous, body, nowMs, actor, authSource) {
  if (!body || typeof body !== "object" || Array.isArray(body) || typeof body.enabled !== "boolean") {
    return { ok: false, reason: "manualHalt.enabled must be boolean" };
  }
  const reason = body.reason === undefined || body.reason === null
    ? null : String(body.reason).trim().slice(0, 120) || null;
  const at = new Date(nowMs).toISOString();
  const halt = {
    schemaVersion: MANUAL_HALT_SCHEMA,
    enabled: body.enabled,
    status: body.enabled ? "HALTED" : "RELEASED",
    updatedAt: at,
    engagedAt: body.enabled ? (previous?.enabled === true ? previous.engagedAt || at : at) : (previous?.engagedAt || null),
    releasedAt: body.enabled ? null : at,
    source: "TELEGRAM_MINI_APP",
    reason,
    actor: String(actor || "").slice(0, 64) || null,
    authSource: String(authSource || "").slice(0, 32) || null,
  };
  const transitions = [{ kind: "manual_halt", from: previous?.enabled === true ? "HALTED" : "RELEASED",
    to: halt.status }];
  return { ok: true, halt, transitions };
}

// ---------------------------------------------------------------- 入力検証

const REQUIRED_SCENARIO_FIELDS = [
  "scenarioId", "fingerprint", "issuedAt", "observedAt", "expiresAt", "symbol", "state",
];

/**
 * scenario payload を検証し、正規化済みのコピーを返す。
 * 1項目でも欠けたら理由付きで拒否する(推測で埋めない)。
 */
export function validateScenario(raw, { symbol, pairedEvidence = null, requireHashOnly = false } = {}) {
  if (!raw || typeof raw !== "object") return { ok: false, reason: "scenario is not an object" };

  for (const field of REQUIRED_SCENARIO_FIELDS) {
    if (raw[field] === undefined || raw[field] === null || raw[field] === "") {
      return { ok: false, reason: `scenario.${field} is required` };
    }
  }

  const state = String(raw.state).toUpperCase();
  if (raw.marketCycleId !== undefined && (!String(raw.marketCycleId).trim() || String(raw.marketCycleId).length > 96)) {
    return { ok: false, reason: "scenario.marketCycleId is invalid" };
  }
  if (!LIVE_SCENARIO_STATES.has(state) && !TERMINAL_SCENARIO_STATES.has(state)) {
    return { ok: false, reason: `scenario.state ${state} is unknown` };
  }

  if (String(raw.symbol) !== String(symbol)) {
    return { ok: false, reason: `scenario.symbol ${raw.symbol} does not match ${symbol}` };
  }

  const issuedAt = parseInstant(raw.issuedAt);
  const observedAt = parseInstant(raw.observedAt);
  const expiresAt = parseInstant(raw.expiresAt);
  if (issuedAt === null) return { ok: false, reason: "scenario.issuedAt is not a timestamp" };
  if (observedAt === null) return { ok: false, reason: "scenario.observedAt is not a timestamp" };
  if (expiresAt === null) return { ok: false, reason: "scenario.expiresAt is not a timestamp" };
  if (expiresAt <= issuedAt) return { ok: false, reason: "scenario.expiresAt precedes issuedAt" };

  const versionFields = ["setupVersion", "catalogVersion", "detectorVersion", "executionContractVersion"];
  const suppliedVersions = versionFields.filter((field) => raw[field] !== undefined && raw[field] !== null && raw[field] !== "");
  if (suppliedVersions.length > 0 && suppliedVersions.length !== versionFields.length) {
    return { ok: false, reason: "scenario immutable version set is incomplete" };
  }
  const hasScenarioEvidenceField = Object.prototype.hasOwnProperty.call(raw, "strategyEvidence");
  const hasScenarioEvidence = raw.strategyEvidence !== undefined && raw.strategyEvidence !== null;
  const scenarioEvidence = normalizeStrategyEvidence(raw.strategyEvidence);
  if (hasScenarioEvidence && !scenarioEvidence.ok) {
    return { ok: false, reason: scenarioEvidence.reason };
  }
  const paired = pairedEvidence === null ? { ok: true, evidence: null }
    : normalizeStrategyEvidence(pairedEvidence);
  if (!paired.ok) return { ok: false, reason: `paired market ${paired.reason}` };
  if (suppliedVersions.length && !String(raw.evidenceHash || "").trim()) {
    return { ok: false, reason: "scenario evidenceHash is required with versions" };
  }
  if (requireHashOnly && hasScenarioEvidenceField) {
    return { ok: false, reason: "atomic cycle scenario must carry only evidenceHash" };
  }
  if (suppliedVersions.length && hasScenarioEvidence
      && String(raw.evidenceHash) !== String(scenarioEvidence.evidence?.evidenceHash || "")) {
    return { ok: false, reason: "scenario evidenceHash must match frozen strategyEvidence" };
  }
  if (suppliedVersions.length && !hasScenarioEvidence
      && (!paired.evidence || String(raw.evidenceHash) !== String(paired.evidence.evidenceHash))) {
    return { ok: false, reason: "scenario hash-only evidence requires verified paired market" };
  }
  if (!suppliedVersions.length && !hasScenarioEvidence && raw.evidenceHash !== undefined && raw.evidenceHash !== null) {
    return { ok: false, reason: "unversioned scenario cannot carry hash-only evidence" };
  }

  const side = String(raw.side || "").toUpperCase();
  if (!["BUY", "SELL"].includes(side)) return { ok: false, reason: "scenario.side is invalid" };

  const entry = normalizeTick(Number(raw.entry));
  const stop = normalizeTick(Number(raw.stop));
  const target = normalizeTick(Number(raw.target));
  if (entry === null) return { ok: false, reason: "scenario.entry is invalid" };
  if (stop === null) return { ok: false, reason: "scenario.stop is invalid" };
  if (target === null) return { ok: false, reason: "scenario.target is invalid" };

  // 正規化「後」の値で方向を判定する。丸めで等値になった場合も弾く。
  if (side === "BUY" && !(stop < entry && entry < target)) {
    return { ok: false, reason: "BUY requires stop < entry < target after tick normalization" };
  }
  if (side === "SELL" && !(target < entry && entry < stop)) {
    return { ok: false, reason: "SELL requires target < entry < stop after tick normalization" };
  }

  const qty = Number(raw.qty ?? 1);
  // ULTRA シナリオ(凍結 riskCapSource が ULTRA の正本値)だけ、枚数の上限を
  // ULTRA エンベロープまで開ける。それ以外は従来どおり maxQty(2)。
  const scenarioUltra = executionContract.ultra?.enabled === true
    && String(raw.executionContract?.riskCapSource || "") === String(executionContract.ultra.riskCapSource);
  const qtyLimit = scenarioUltra
    ? Number(executionContract.ultra.maxQtyPerAccount)
    : Number(executionContract.risk.maxQty);
  if (!Number.isInteger(qty) || qty < 1 || qty > qtyLimit) {
    return { ok: false, reason: "scenario.qty is invalid" };
  }

  const rawTargets = raw.targets === undefined ? [target] : raw.targets;
  if (!Array.isArray(rawTargets) || rawTargets.length < 1 || rawTargets.length > 3) {
    return { ok: false, reason: "scenario.targets is invalid" };
  }
  const targets = [];
  for (const value of rawTargets) {
    const normalized = normalizeTick(Number(value));
    if (normalized === null || !priceEquals(normalized, Number(value))) {
      return { ok: false, reason: "scenario.targets must contain finite 0.25-tick prices" };
    }
    targets.push(normalized);
  }
  if (!priceEquals(targets[0], target)) return { ok: false, reason: "scenario.target must equal targets[0]" };
  const directionalTargets = side === "BUY"
    ? targets.every((value, index) => value > entry && (!index || value > targets[index - 1]))
    : targets.every((value, index) => value < entry && (!index || value < targets[index - 1]));
  if (!directionalTargets) return { ok: false, reason: "scenario.targets are not directional and ordered" };
  const ultraLegQty = scenarioUltra && qty > Number(executionContract.risk.fixedQty)
    ? ultraSplit(qty) : null;
  if ((qty === Number(executionContract.risk.fixedQty) || ultraLegQty)
      && (targets.length < 2 || targets[0] === targets[targets.length - 1])) {
    return { ok: false, reason: "scenario qty=2 requires distinct TP1 and runner targets" };
  }
  const targetR = Array.isArray(raw.targetR)
    ? raw.targetR.slice(0, 3).map(evalNumber).filter((value) => value !== null) : [];
  const legs = ultraLegQty
    ? [{ id: "TP1", qty: ultraLegQty[0], target: targets[0] },
       { id: "RUNNER", qty: ultraLegQty[1], target: targets[targets.length - 1] }]
    : (qty === Number(executionContract.risk.fixedQty)
      ? [{ id: "TP1", qty: 1, target: targets[0] }, { id: "RUNNER", qty: 1, target: targets[targets.length - 1] }]
      : [{ id: "FULL", qty, target: targets[0] }]);
  if (raw.legs !== undefined && (!Array.isArray(raw.legs) || raw.legs.length !== legs.length
      || raw.legs.some((leg, index) => !leg || String(leg.id) !== legs[index].id
        || Number(leg.qty) !== Number(legs[index].qty)
        || !priceEquals(Number(leg.target), legs[index].target)))) {
    return { ok: false, reason: "SPLIT_LEGS_INVALID" };
  }

  return {
    ok: true,
    scenario: {
      scenarioId: String(raw.scenarioId),
      fingerprint: String(raw.fingerprint),
      state,
      symbol: String(raw.symbol),
      side,
      qty,
      entry,
      stop,
      target,
      targets,
      targetR,
      legs,
      planVersion: raw.planVersion ? String(raw.planVersion).slice(0, 64) : null,
      riskDollars: riskDollars(entry, stop, qty),
      rr: rewardRatio(entry, stop, target),
      // R6: 発注ボタンの等級バッジ。未知の値は null に倒す(A/A+ のみ)。
      grade: EVAL_GRADES.has(raw.grade) ? String(raw.grade) : null,
      title: raw.title ? String(raw.title) : null,
      reason: raw.reason ? String(raw.reason) : null,
      trigger: raw.trigger ? String(raw.trigger) : null,
      invalidation: raw.invalidation ? String(raw.invalidation) : null,
      issuedAt: new Date(issuedAt).toISOString(),
      observedAt: new Date(observedAt).toISOString(),
      expiresAt: new Date(expiresAt).toISOString(),
      snapshotAt: raw.snapshotAt ? String(raw.snapshotAt) : new Date(observedAt).toISOString(),
      marketCycleId: raw.marketCycleId ? String(raw.marketCycleId).slice(0, 96) : null,
      // Immutable decision provenance travels with the executable scenario.
      // Unknown/missing version fields are kept null rather than synthesized
      // from the Worker clock, so OOS can isolate legacy packets.
      setupVersion: raw.setupVersion ? String(raw.setupVersion).slice(0, 64) : null,
      catalogVersion: raw.catalogVersion ? String(raw.catalogVersion).slice(0, 64) : null,
      detectorVersion: raw.detectorVersion ? String(raw.detectorVersion).slice(0, 64) : null,
      executionContractVersion: raw.executionContractVersion
        ? String(raw.executionContractVersion).slice(0, 64) : executionContract.version,
      evidenceHash: raw.evidenceHash ? String(raw.evidenceHash).slice(0, 128) : null,
      eligibleVotes: raw.eligibleVotes && typeof raw.eligibleVotes === "object"
        && !Array.isArray(raw.eligibleVotes)
        ? normalizeStrategyMatrix(raw.eligibleVotes).matrix ?? null
        : (Number.isInteger(raw.eligibleVotes) && raw.eligibleVotes >= 0
          ? raw.eligibleVotes : null),
      // Full evidence is committed once on the paired market.  A scenario is
      // a hash-bound execution instruction, never a duplicate evidence store.
      executionContract: raw.executionContract && typeof raw.executionContract === "object"
        && !Array.isArray(raw.executionContract)
        ? {
            version: raw.executionContract.version ? String(raw.executionContract.version).slice(0, 64) : null,
            riskCapDollars: isFiniteNumber(Number(raw.executionContract.riskCapDollars))
              ? Number(raw.executionContract.riskCapDollars) : null,
            riskCapSource: raw.executionContract.riskCapSource
              ? String(raw.executionContract.riskCapSource).slice(0, 96) : null,
            accountScope: normalizeAccountScope(raw.executionContract.accountScope),
            effectiveGrade: EVAL_GRADES.has(raw.executionContract.effectiveGrade)
              ? String(raw.executionContract.effectiveGrade) : null,
          }
        : null,
    },
  };
}

/**
 * position payload を検証する。
 * verified=false(照会不能)でも受け付ける — その場合は既存 position を消さず STALE にする。
 */
export function validatePosition(raw, { symbol }) {
  if (raw === null) return { ok: true, position: null };
  if (typeof raw !== "object") return { ok: false, reason: "position is not an object" };

  const verified = raw.verified === true;
  const source = raw.source ? String(raw.source) : null;
  if (!source) return { ok: false, reason: "position.source is required" };

  const observedAt = parseInstant(raw.observedAt);
  if (observedAt === null) return { ok: false, reason: "position.observedAt is not a timestamp" };

  // qty が確定していない照会結果は「不明」として扱い、数量を捏造しない。
  const hasQty = raw.qty !== undefined && raw.qty !== null;
  const qty = hasQty ? Number(raw.qty) : null;
  if (hasQty && (!Number.isInteger(qty) || qty < 0)) {
    return { ok: false, reason: "position.qty is invalid" };
  }

  if (hasQty && qty > 0) {
    if (String(raw.symbol || symbol) !== String(symbol)) {
      return { ok: false, reason: `position.symbol ${raw.symbol} does not match ${symbol}` };
    }
    const side = String(raw.side || "").toUpperCase();
    if (!["LONG", "SHORT"].includes(side)) return { ok: false, reason: "position.side is invalid" };
    // avgEntry は必須にしない。CrossTrade の建玉照会は平均建値を返さないことがあり、
    // 「2 枚ロングを持っている」だけでも新規発注を止めるには十分だから。
    // 決済結果(損益)は avgEntry が無ければ作られない(build_result 側で落ちる)。
  }

  return {
    ok: true,
    position: {
      verified,
      source,
      symbol: String(raw.symbol || symbol),
      side: raw.side ? String(raw.side).toUpperCase() : null,
      qty,
      initialQty: raw.initialQty != null ? Number(raw.initialQty) : null,
      // R73: null / 未設定は「返っていない」。Number(null) は 0 なので normalizeTick に
      // 通す前に落とす(0 の建値・SL・TP を作ると、App が (現値 − 0) × 枚数 の損益や
      // SL 0.00 を描く)。
      avgEntry: raw.avgEntry == null || raw.avgEntry === "" ? null : normalizeTick(Number(raw.avgEntry)),
      stop: raw.stop == null || raw.stop === "" ? null : normalizeTick(Number(raw.stop)),
      target: raw.target == null || raw.target === "" ? null : normalizeTick(Number(raw.target)),
      currentPrice: isFiniteNumber(Number(raw.currentPrice)) ? Number(raw.currentPrice) : null,
      unrealizedPnl: isFiniteNumber(Number(raw.unrealizedPnl)) ? Number(raw.unrealizedPnl) : null,
      receipt: raw.receipt ? String(raw.receipt) : null,
      accountId: raw.accountId || raw.account ? String(raw.accountId || raw.account) : null,
      orderId: raw.orderId ? String(raw.orderId) : null,
      positionGeneration: raw.positionGeneration ? String(raw.positionGeneration) : null,
      filledAt: raw.filledAt ? String(raw.filledAt) : null,
      observedAt: new Date(observedAt).toISOString(),
      exitPending: raw.exitPending === true,
    },
  };
}

export function validateOrder(raw) {
  if (raw === null) return { ok: true, order: null };
  if (!raw || typeof raw !== "object") return { ok: false, reason: "order is not an object" };
  const state = String(raw.state || "").toUpperCase();
  const allowed = ["PENDING", "SENT", "PARTIAL", "ENTRY_PARTIAL_ROUTE", "ENTRY_PARTIAL_FILL",
    "ENTRY_RESTING", "REJECTED", "UNKNOWN", "FILLED", "CANCELED"];
  if (!allowed.includes(state)) return { ok: false, reason: `order.state ${state} is unknown` };
  if (!raw.idempotencyKey) return { ok: false, reason: "order.idempotencyKey is required" };
  const at = parseInstant(raw.at);
  if (at === null) return { ok: false, reason: "order.at is not a timestamp" };
  return {
    ok: true,
    order: {
      idempotencyKey: String(raw.idempotencyKey),
      scenarioId: raw.scenarioId ? String(raw.scenarioId) : null,
      state,
      side: raw.side ? String(raw.side).toUpperCase() : null,
      qty: raw.qty != null ? Number(raw.qty) : null,
      entry: normalizeTick(Number(raw.entry)),
      stop: normalizeTick(Number(raw.stop)),
      target: normalizeTick(Number(raw.target)),
      receipt: raw.receipt ? String(raw.receipt) : null,
      detail: raw.detail ? String(raw.detail) : null,
      at: new Date(at).toISOString(),
    },
  };
}

const ENTRY_CLAIM_ACTIONS = new Set(["CLAIM", "CONSUME", "RESOLVE", "RECOVER", "RELEASE"]);
const ENTRY_CLAIM_ACTIVE = new Set(["CLAIMED", "CONSUMED"]);
const ENTRY_CLAIM_LOCKING_RESULTS = new Set(["SENT", "PARTIAL", "UNKNOWN"]);

function authoritativeTuple(scenario) {
  if (!scenario) return null;
  return {
    scenarioId: String(scenario.scenarioId || ""),
    fingerprint: String(scenario.fingerprint || ""),
    evidenceHash: String(scenario.evidenceHash || ""),
    marketCycleId: String(scenario.marketCycleId || ""),
  };
}

function tupleMatches(left, right) {
  const keys = ["scenarioId", "fingerprint", "evidenceHash", "marketCycleId"];
  return Boolean(left && right && keys.every((key) => String(left[key] || "") === String(right[key] || "")
    && String(left[key] || "").length > 0));
}

/** R84: CLAIM payload の `pyramid` を正規化する。**申告は許可の根拠ではない**。 */
function normalizeClaimPyramid(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const baseQty = Number(raw.baseQty);
  const addQty = Number(raw.addQty);
  const addsDone = Number(raw.addsDone);
  if (!Number.isInteger(baseQty) || baseQty <= 0) return null;
  if (!Number.isInteger(addQty) || addQty <= 0) return null;
  if (!Number.isInteger(addsDone) || addsDone < 0) return null;
  const baseAvgEntry = raw.baseAvgEntry === null || raw.baseAvgEntry === undefined
    ? null : Number(raw.baseAvgEntry);
  if (baseAvgEntry !== null && !isFiniteNumber(baseAvgEntry)) return null;
  const preSendOrderIds = Array.isArray(raw.preSendOrderIds)
    ? [...new Set(raw.preSendOrderIds.map((value) => String(value || "")).filter(Boolean))]
      .sort().slice(0, 64)
    : [];
  return {
    baseQty, addQty, addsDone, baseAvgEntry,
    positionGeneration: String(raw.positionGeneration || ""),
    preSendOrderIds,
  };
}

const PYRAMID_GRADE_RANK = { B: 1, A: 2, "A+": 3 };

/**
 * R84 §7-2/§7-3: 追撃 CLAIM の門。**全部 DO 自身の state で再検証する**。
 *
 * `projectState` は建玉が開いている間ずっと `orderable=false`
 * (「POSITION OPEN — MANAGEMENT ONLY」)を返すので、そこを緩めると Mini App の
 * 手動発注ボタンまで出てしまう。代わりにこの関数が、建玉由来のゲートだけを
 * 追撃の条件へ差し替えた専用の判定を行う。`cyclePaired`(R15 の原子サイクル封印)は
 * 必須のまま残す —— シナリオと相場が対であることを示すのはこれだけである。
 */
function pyramidClaimGate(state, projected, py, nowMs) {
  const deny = (reason) => ({ ok: false, reason });
  const cfg = executionContract.pyramid || {};
  if (cfg.enabled !== true) return deny("ENTRY_CLAIM_PYRAMID_DISABLED");
  if (projected.display?.cyclePaired !== true) {
    return deny(`ENTRY_CLAIM_NOT_ORDERABLE: ${projected.display?.blockReason || "cycle seal"}`);
  }
  const scenario = state.scenario;
  if (!scenario) return deny("ENTRY_CLAIM_NOT_ORDERABLE: NO ACTIVE SCENARIO");
  if (!["ACTIVE", "ARMED"].includes(String(scenario.state))) {
    return deny(`ENTRY_CLAIM_NOT_ORDERABLE: SCENARIO ${scenario.state}`);
  }
  // 通常経路は projectState の orderLive でここを塞いでいる。追撃だけ素通りさせると、
  // CLAIM は通るのに CONSUME の ENTRY_CLAIM_ORDER_CONFLICT で必ず落ちる claim を
  // 取ってしまい、stale 解放まで枠を潰す。同じ条件をここでも見る。
  const gateOrderState = String(state.order?.state || "").toUpperCase();
  if (state.order && BLOCKING_ORDER_STATES.has(gateOrderState)) {
    return deny(`ENTRY_CLAIM_NOT_ORDERABLE: ORDER ${gateOrderState}`);
  }
  if (state.positionCheck?.verified !== true) {
    return deny("ENTRY_CLAIM_PYRAMID_POSITION_UNVERIFIED");
  }
  const position = state.position;
  if (!position || !OPEN_POSITION_STATES.has(String(position.state || ""))) {
    return deny("ENTRY_CLAIM_PYRAMID_NO_BASE_POSITION");
  }
  if (String(position.symbol || "") !== String(state.symbol || "")) {
    return deny("ENTRY_CLAIM_PYRAMID_SYMBOL_MISMATCH");
  }
  const wantSide = String(scenario.side || "") === "BUY" ? "LONG" : "SHORT";
  if (String(position.side || "") !== wantSide) {
    return deny("ENTRY_CLAIM_PYRAMID_SIDE_MISMATCH");
  }
  if (Number(position.qty) !== py.baseQty) {
    return deny("ENTRY_CLAIM_PYRAMID_BASE_QTY_MISMATCH");
  }
  const maxAdds = Number(cfg.maxAdds || 0);
  if (!(py.addsDone < maxAdds)) return deny("ENTRY_CLAIM_PYRAMID_MAX_ADDS_REACHED");
  const grade = String(scenario.grade || "").toUpperCase();
  const floor = String(cfg.minGrade || "A+").toUpperCase();
  if ((PYRAMID_GRADE_RANK[grade] || 0) < (PYRAMID_GRADE_RANK[floor] || 99)) {
    return deny("ENTRY_CLAIM_PYRAMID_GRADE_BELOW_MIN");
  }
  // 平均建値の正本は **DO 自身の建玉**。申告値は 1 tick 以内の突き合わせにだけ使う
  // (DO の avgEntry は normalizeTick で丸められているので完全一致は要求できない)。
  const baseEntry = Number(position.avgEntry);
  if (!isFiniteNumber(baseEntry) || baseEntry <= 0) {
    return deny("ENTRY_CLAIM_PYRAMID_BASE_ENTRY_UNAVAILABLE");
  }
  if (py.baseAvgEntry !== null && Math.abs(py.baseAvgEntry - baseEntry) > TICK + 1e-9) {
    return deny("ENTRY_CLAIM_PYRAMID_BASE_ENTRY_DEVIATION");
  }
  const totalQty = Number(scenario.qty);
  // 目標(ULTRA が引き直した合計枚数)との差が上限。engine は合計リスクが口座上限に
  // 収まるまで枚数を刻んで落とす(pyramid._fit_to_cap)ので、**完全一致を要求すると
  // 刻んだ追撃が全部拒否され、刻む機構が丸ごと死ぬ**。減らす方向だけを許す。
  if (!Number.isInteger(totalQty) || py.addQty < 1
      || py.addQty > totalQty - py.baseQty) {
    return deny("ENTRY_CLAIM_PYRAMID_ADD_QTY_MISMATCH");
  }
  const ultraEnv = executionContract.ultra || {};
  if (ultraEnv.enabled !== true) return deny("ENTRY_CLAIM_PYRAMID_REQUIRES_ULTRA");
  if (String(scenario.executionContract?.riskCapSource || "")
      !== String(ultraEnv.riskCapSource || "")) {
    return deny("ENTRY_CLAIM_PYRAMID_NOT_ULTRA_SCENARIO");
  }
  if (py.baseQty + py.addQty > Number(ultraEnv.maxQtyPerAccount)) {
    return deny("ENTRY_CLAIM_PYRAMID_TOTAL_QTY_EXCEEDS_ACCOUNT_MAX");
  }
  if (ultraSplit(py.addQty) === null) return deny("ENTRY_CLAIM_PYRAMID_SPLIT_INVALID");
  const frozenCap = Number(scenario.executionContract?.riskCapDollars);
  const cap = Number.isFinite(frozenCap) && frozenCap > 0
    ? Math.min(frozenCap, Number(ultraEnv.maxRiskDollarsPerAccount)) : NaN;
  if (!Number.isFinite(cap) || cap <= 0) return deny("ENTRY_CLAIM_PYRAMID_RISK_CAP_UNAVAILABLE");
  if (state.market?.verified !== true) return deny("ENTRY_CLAIM_MARKET_UNVERIFIED");
  // 上限は **不利側へ滑った追撃価格** で見る(engine / order.py と同じ算術)。
  // 現在値ぴったりで見積もると、成行の約定は常に不利側へずれる。
  const riskPrice = pyramidAdversePrice(scenario.side, Number(state.market?.price));
  const risk = pyramidCombinedRisk(py.baseQty, baseEntry, py.addQty,
                                   riskPrice, Number(scenario.stop));
  if (risk === null) return deny("ENTRY_CLAIM_PYRAMID_RISK_GEOMETRY_INVALID");
  if (risk > cap + 1e-9) return deny("ENTRY_CLAIM_PYRAMID_RISK_CAP_EXCEEDED");
  // 残りの執行契約ブロッカーは publish 時と同じ門で見る(建玉・注文は上で個別に
  // 判定済み、単体リスクは合成リスクで置き換え済みなので除外する)。
  const execution = evaluateExecutionContract(scenario, state.market, null, null, nowMs);
  const exempt = new Set(["RISK_CAP_EXCEEDED"]);
  const blockers = (execution.blockers || []).filter((value) => !exempt.has(value));
  if (blockers.length) {
    return deny(`ENTRY_CLAIM_NOT_ORDERABLE: ${blockers.join(", ")}`);
  }
  return { ok: true, combinedRisk: risk, capDollars: cap, baseAvgEntry: baseEntry };
}

/** R84: 追撃 CLAIM が「基礎建玉を作った claim」を引き継いでよいか。 */
function pyramidSupersedable(state, claim, py) {
  if (!claim || !py) return false;
  const intent = claim.executionIntent;
  if (!intent || String(intent.symbol || "") !== String(state.symbol || "")) return false;
  if (String(intent.side || "") !== String(state.scenario?.side || "")) return false;
  // 「注文が出た claim」でなければ基礎建玉の出所ではない。
  const routed = ENTRY_CLAIM_LOCKING_RESULTS.has(String(claim.routeState || "").toUpperCase())
    || claim.state === "CONSUMED";
  if (!routed) return false;
  if (state.positionCheck?.verified !== true) return false;
  const position = state.position;
  if (!position || !OPEN_POSITION_STATES.has(String(position.state || ""))) return false;
  if (Number(position.qty) !== Number(py.baseQty)) return false;
  const wantSide = String(intent.side || "") === "BUY" ? "LONG" : "SHORT";
  return String(position.side || "") === wantSide;
}

/** R84: 追撃 claim の CONSUME 前提。FLAT ではなく **宣言どおりの基礎建玉**。 */
function pyramidBaseVerified(state, claim) {
  const py = claim?.pyramid;
  if (!py) return false;
  if (state.positionCheck?.verified !== true) return false;
  const position = state.position;
  if (!position || !OPEN_POSITION_STATES.has(String(position.state || ""))) return false;
  if (String(position.symbol || "") !== String(claim.executionIntent?.symbol || "")) return false;
  if (Number(position.qty) !== Number(py.baseQty)) return false;
  const wantSide = String(claim.executionIntent?.side || "") === "BUY" ? "LONG" : "SHORT";
  return String(position.side || "") === wantSide;
}

function flatVerified(state) {
  const live = state.position && OPEN_POSITION_STATES.has(String(state.position.state || "").toUpperCase());
  return !live && state.positionCheck?.verified === true;
}

function priorClaimBrokerTerminal(state, claim) {
  if (!claim || !flatVerified(state)) return false;
  const order = state.order;
  return Boolean(order && String(order.idempotencyKey || "") === String(claim.entryKey || "")
    && ["FILLED", "CANCELED", "REJECTED"].includes(String(order.state || "").toUpperCase()));
}

const BROKER_OBSERVATION_FIELDS = [
  "observedAt", "positionObservedAt", "ordersObservedAt", "snapshotId", "cursor",
  "snapshotMode", "stableBeforeHash", "stableAfterHash", "platform", "accountId",
  "symbol", "currentIntentHash", "position", "orders",
];

function brokerObservationMaterial(value) {
  return Object.fromEntries(BROKER_OBSERVATION_FIELDS.map((key) => [key, value[key]]));
}

export function brokerObservationHash(value) {
  if (!value || typeof value !== "object") return null;
  return `bo_${sha256Hex(new TextEncoder().encode(stableJson(brokerObservationMaterial(value))))}`;
}

function normalizeBrokerObservation(raw) {
  const reject = (reason) => ({ ok: false, reason });
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return reject("broker observation is invalid");
  const observedAtMs = parseInstant(raw.observedAt);
  const positionObservedAtMs = parseInstant(raw.positionObservedAt);
  const ordersObservedAtMs = parseInstant(raw.ordersObservedAt);
  const snapshotId = String(raw.snapshotId || "").trim();
  const cursor = raw.cursor === null || raw.cursor === undefined ? "" : String(raw.cursor).trim();
  const snapshotMode = String(raw.snapshotMode || "").trim().toUpperCase();
  const stableBeforeHash = String(raw.stableBeforeHash || "").trim().toLowerCase();
  const stableAfterHash = String(raw.stableAfterHash || "").trim().toLowerCase();
  const platform = String(raw.platform || "").trim().toUpperCase();
  const accountId = String(raw.accountId || "").trim();
  const symbol = String(raw.symbol || "").trim();
  const currentIntentHash = String(raw.currentIntentHash || "").trim();
  const position = raw.position;
  if (observedAtMs === null || positionObservedAtMs === null || ordersObservedAtMs === null
      || !snapshotId || snapshotId.length > 160 || !["NT8", "TRADOVATE", "STUB"].includes(platform)
      || !accountId || accountId.length > 64 || !symbol || symbol.length > 32
      || !currentIntentHash || currentIntentHash.length > 128
      || !position || typeof position !== "object" || position.verified !== true) {
    return reject("broker observation identity/time/position is invalid");
  }
  const maxSkewMs = Number(executionContract.brokerObservation?.maxComponentSkewSec || 0) * 1000;
  if (observedAtMs < positionObservedAtMs || observedAtMs < ordersObservedAtMs
      || Math.abs(positionObservedAtMs - ordersObservedAtMs) > maxSkewMs
      || observedAtMs - Math.min(positionObservedAtMs, ordersObservedAtMs) > maxSkewMs) {
    return reject("broker observation component timestamps are not one atomic snapshot");
  }
  if (cursor) {
    if (snapshotMode !== "BROKER_CURSOR" || cursor.length > 160
        || stableBeforeHash || stableAfterHash) {
      return reject("broker cursor snapshot metadata is invalid");
    }
  } else if (snapshotMode !== "STABLE_DOUBLE_READ"
      || !/^[a-f0-9]{64}$/.test(stableBeforeHash)
      || stableBeforeHash !== stableAfterHash) {
    return reject("broker snapshot requires cursor or stable position double-read proof");
  }
  const qty = Number(position.qty);
  const side = String(position.side || "").toUpperCase();
  const positionGeneration = String(position.positionGeneration || "").trim();
  const rawPositionIdentity = String(position.rawPositionIdentity || "").trim();
  // 建玉枚数の天井はモードで変わる。ULTRA が有効な契約では口座あたりの
  // ULTRA 上限まで観測できないと、大きな建玉が「観測不能」になって
  // recovery が永久に通らなくなる。
  const observationMaxQty = executionContract.ultra?.enabled
    ? Number(executionContract.ultra.maxQtyPerAccount)
    : Number(executionContract.risk.fixedQty);
  if (!Number.isInteger(qty) || qty < 0 || qty > observationMaxQty
      || (qty > 0 && (!positionGeneration || !rawPositionIdentity
        || !["LONG", "SHORT"].includes(side)))) {
    return reject("broker observation position generation/qty/side is invalid");
  }
  if (!Array.isArray(raw.orders)
      || raw.orders.length > Number(executionContract.brokerObservation?.maxOrders || 0)) {
    return reject("broker observation orders are invalid");
  }
  const platformStates = executionContract.brokerObservation?.statusByPlatform?.[platform];
  const allowed = new Set(platform === "STUB"
    ? [...(executionContract.brokerObservation?.activeStates || []),
      ...(executionContract.brokerObservation?.terminalStates || [])]
    : [...(platformStates?.active || []), ...(platformStates?.terminal || [])]);
  const seen = new Set();
  const orders = [];
  for (const row of raw.orders) {
    const orderId = String(row?.orderId || "").trim();
    const receipt = String(row?.receipt || "").trim();
    const rowAccount = String(row?.accountId || "").trim();
    const rowSymbol = String(row?.symbol || "").trim();
    const status = String(row?.status || "").toUpperCase();
    if (!orderId || orderId.length > 96 || !receipt || receipt.length > 160
        || rowAccount !== accountId || rowSymbol !== symbol || !allowed.has(status)
        || seen.has(orderId)) return reject("broker observation order identity/status is invalid");
    seen.add(orderId);
    orders.push({ orderId, receipt, accountId: rowAccount, symbol: rowSymbol, status });
  }
  orders.sort((left, right) => `${left.orderId}\u0000${left.receipt}`.localeCompare(
    `${right.orderId}\u0000${right.receipt}`));
  const observation = {
    observedAt: new Date(observedAtMs).toISOString(),
    positionObservedAt: new Date(positionObservedAtMs).toISOString(),
    ordersObservedAt: new Date(ordersObservedAtMs).toISOString(),
    snapshotId, cursor: cursor || null, snapshotMode,
    stableBeforeHash: stableBeforeHash || null, stableAfterHash: stableAfterHash || null,
    platform, accountId, symbol, currentIntentHash,
    position: { verified: true, qty, side: qty > 0 ? side : "FLAT",
      positionGeneration: qty > 0 ? positionGeneration : null,
      rawPositionIdentity: qty > 0 ? rawPositionIdentity : null }, orders,
  };
  const snapshotHash = String(raw.snapshotHash || "").trim().toLowerCase();
  const expectedHash = brokerObservationHash(observation);
  if (!/^bo_[a-f0-9]{64}$/.test(snapshotHash) || snapshotHash !== expectedHash) {
    return reject("broker observation snapshot hash is invalid");
  }
  return { ok: true, observation: { ...observation, snapshotHash } };
}

/**
 * その intent ハッシュが権威として持つ口座集合(R43)。
 *
 * ブローカー観測スロットの口座載せ替えを許してよいかの判定にだけ使う。
 * 権威 intent 自身が持つ accountScope を正本にするので、範囲外の口座が
 * 観測を上書きする経路は開かない。
 */
function intentAccountScope(state, intentHash) {
  const hash = String(intentHash || "");
  const scope = new Set();
  if (!hash) return scope;
  const claim = state.entryClaim;
  if (claim && String(claim.executionIntentHash || "") === hash) {
    const accounts = claim.executionIntent?.accountScope;
    if (Array.isArray(accounts)) {
      for (const id of accounts) if (id) scope.add(String(id));
    }
  }
  const mgmt = state.managementClaim;
  if (mgmt && String(mgmt.managementIntentHash || "") === hash) {
    const id = mgmt.managementIntent?.accountId;
    if (id) scope.add(String(id));
  }
  return scope;
}

/**
 * R102c: authorizedHashes で権威性が取れた intent の **銘柄**。限月の載せ替え判定に使う。
 */
function intentSymbol(state, intentHash) {
  const hash = String(intentHash || "");
  if (!hash) return null;
  const claim = state.entryClaim;
  if (claim && String(claim.executionIntentHash || "") === hash) {
    return claim.executionIntent?.symbol ? String(claim.executionIntent.symbol) : null;
  }
  const mgmt = state.managementClaim;
  if (mgmt && String(mgmt.managementIntentHash || "") === hash) {
    return mgmt.managementIntent?.symbol ? String(mgmt.managementIntent.symbol) : null;
  }
  return null;
}

function freshBrokerObservation(state, nowMs, intentHash) {
  const observation = state.brokerObservation;
  const at = parseInstant(observation?.observedAt);
  const maxAgeMs = Number(executionContract.brokerObservation?.maxAgeSec || 0) * 1000;
  if (!observation || at === null || nowMs < at || nowMs - at > maxAgeMs
      || String(observation.currentIntentHash || "") !== String(intentHash || "")) return null;
  return observation;
}

function observationCasMatches(observation, payload) {
  return Boolean(observation && payload
    && String(payload.brokerSnapshotHash || "") === String(observation.snapshotHash || "")
    && String(payload.brokerSnapshotId || "") === String(observation.snapshotId || "")
    && String(payload.brokerCursor || "") === String(observation.cursor || ""));
}

/**
 * 観測口座が claim の accountScope に属するか(R50)。
 *
 * かつては `accountScope[0]` と名指しで一致することを要求していた。scope は
 * ソート済みなので [0] は「アルファベット順で先頭の口座」でしかなく、観測を
 * 出す口座(CROSSTRADE_ACCOUNTS の先頭)とは無関係に決まる。両者がズレると
 * 解放証明が永久に成立せず、`ENTRY_CLAIM_ALREADY_HELD` で新規発注が全部
 * 止まる。2026-08-31、scope[0] だった口座が破棄されて実際にこの状態へ落ち、
 * 8/28 の claim が3日間ロックし続けた(存在しない口座の FLAT は証明不能)。
 *
 * scope の**いずれか**を名乗る観測を受け入れる。証明の強さは変わらない ——
 * 従来も「1口座が空である」ことしか示せておらず(scope[1] 以降は元から未証明)、
 * ここで落としているのは口座名の並び順への依存だけである。
 */
function observationAccountInClaimScope(observation, claim) {
  const scope = Array.isArray(claim?.executionIntent?.accountScope)
    ? claim.executionIntent.accountScope.map(String) : [];
  if (!scope.length) return false;
  return scope.includes(String(observation?.accountId || ""));
}

function entryRecoveryProof(state, claim, nowMs, payload) {
  const observation = freshBrokerObservation(state, nowMs, claim.executionIntentHash);
  const rows = Array.isArray(claim.routeSnapshot)
    ? claim.routeSnapshot.filter((row) => row?.state === "ACCEPTED") : [];
  if (!observation || !observationCasMatches(observation, payload)
      || observation.position?.qty !== 0
      || !observationAccountInClaimScope(observation, claim)
      || observation.symbol !== claim.executionIntent?.symbol) return false;
  const terminal = new Set(executionContract.brokerObservation?.terminalStates || []);
  const orders = Array.isArray(observation.orders) ? observation.orders : [];
  if (!rows.length) {
    // R50: 経路 identity を一度も束縛できなかった送信(routeSnapshot が全行
    // UNKNOWN / orderId=null)。ID ごとの終端証明は原理的に作れないので、
    // ACCEPTED 行を必須にすると claim は永久に RECOVERY_UNVERIFIED のままで
    // 新規発注が全部止まる。CLAUDE.md §6 と
    // autotrade_engine._recover_entry_from_current_broker(R40)は、この経路を
    // identity ではなく**不在の証明**で終端すると定めている。Worker だけが
    // 非対称に拒否していた(2026-08-28 の claim が3日間ロックし続けた)。
    //
    // 建玉ゼロは上で確認済み。ここでは未終端の注文が1本も無いことを要求する。
    // 1本でも生きていればその claim の注文かもしれないので解放しない。
    return orders.every((row) => terminal.has(String(row?.status || "")));
  }
  // ACCEPTED 行があるなら identity は取れている。従来どおり ID ごとに終端を照合する。
  return rows.every((frozen) => orders.filter((row) =>
    row.orderId === frozen.orderId && row.receipt === frozen.receipt
    && row.accountId === frozen.accountId && terminal.has(row.status)).length === 1);
}

/**
 * 宙吊りの ENTRY claim を解放してよいか(R36)。
 *
 * 問題: CrossTrade への POST 後に `resolve_entry_claim` が失敗すると、claim は
 * `CONSUMED` のまま `routeSnapshot` 無しで残る。`entryRecoveryProof` は
 * ACCEPTED 行を必須にするので永久に false。`ENTRY_CLAIM_ALREADY_HELD` で
 * 再 CLAIM もできず、**以後すべての新規発注が止まる**。MANAGEMENT には
 * 年齢による再取得があるのに ENTRY だけ非対称だった。
 *
 * ただし ENTRY は「注文が実際に出ているかもしれない」ので、時間だけで
 * 解放すると二重発注になる。**ブローカーが空だと証明できたときだけ**解く:
 *
 *   1. claim が古い(staleReleaseSec 超) —— 通信の一時断とは区別する
 *   2. 新鮮なブローカー観測があり、建玉 0 かつ **未終端の注文が 1 本も無い**
 *   3. 口座と銘柄が claim の意図と一致する
 *
 * 2 が肝。注文が板に残っていれば `working` が立ち、解放しない。
 * つまり「本当に何も起きなかった」場合だけ通す。
 */
//: R69: 口座名簿(accounts.sync)がこの秒数より古ければ「消えた」証明に使わない。
const VANISHED_SCOPE_ROSTER_MAX_AGE_SEC = 900;

/**
 * claim の accountScope の口座が **ブローカーの口座名簿から消えている**か(R69)。
 *
 * `staleEntryClaimReleasable` / `entryRecoveryProof` は「scope 内口座の新鮮な観測で
 * 建玉 0・未終端注文なし」を要求する。ところが口座そのものがブローカーから消えると
 * (プロップは口座を黙って消す: 2026-08-21 / 08-24 / 09-08)、その口座の観測は二度と
 * 作れず、claim は永久に ENTRY_CLAIM_ALREADY_HELD で新規発注を全部止める。
 * 2026-08-31 は scope[0] の破棄で 3 日ロック、2026-09-08 は評価通過で LFE→LFF に
 * 入れ替わった直後、旧口座の claim が新口座の ARMED 候補を全部止めた。
 *
 * 存在しない口座には注文も建玉も残せない。名簿(accounts stream の sync)が
 *   * verified かつ新鮮(VANISHED_SCOPE_ROSTER_MAX_AGE_SEC 以内)で、
 *   * scope の **全口座**が「configured に居て missing でない」でも「broker-only(unknown)」
 *     でもない = ブローカーが一覧に載せていない
 * ときだけ、不在の証明として受け入れる。1 口座でも名簿に居れば従来どおり観測を要求する。
 * 名簿が古い・未検証・無い場合は解放しない(推測しない)。
 */
export function claimScopeVanishedFromBroker(state, claim, nowMs) {
  const scope = Array.isArray(claim?.executionIntent?.accountScope)
    ? claim.executionIntent.accountScope.map((id) => String(id || "")).filter(Boolean) : [];
  if (!scope.length) return false;
  const roster = state?.accounts;
  const sync = roster?.sync;
  if (!roster || !sync || sync.verified !== true) return false;
  const observedAt = parseInstant(sync.observedAt || roster.observedAt);
  if (observedAt === null) return false;
  const ageMs = nowMs - observedAt;
  if (ageMs < -60_000 || ageMs > VANISHED_SCOPE_ROSTER_MAX_AGE_SEC * 1000) return false;
  const configured = new Set((Array.isArray(roster.list) ? roster.list : [])
    .map((row) => String(row?.id || "")).filter(Boolean));
  const missing = new Set((Array.isArray(sync.missing) ? sync.missing : []).map(String));
  const brokerOnly = new Set((Array.isArray(sync.unknown) ? sync.unknown : []).map(String));
  const presentAtBroker = (id) => (configured.has(id) && !missing.has(id)) || brokerOnly.has(id);
  return scope.every((id) => !presentAtBroker(id));
}

function staleEntryClaimReleasable(state, claim, nowMs) {
  if (!claim) return false;
  const terms = executionContract.claim || {};
  let windowSec = Number(terms.staleReleaseSec);
  // R52: 一度も CONSUME されなかった claim(state=CLAIMED / 試行 0)は、claim.maxAgeSec を
  // 過ぎると CONSUME 自体が ENTRY_CLAIM_EXPIRED で拒否され、注文を生む経路が無い。
  // それでも staleReleaseSec(900 秒)まで ALREADY_HELD で新規を止めていた —— 2026-09-05
  // 01:54、送信前の dry-run(argparse)で落ちて置き去りになった claim が、A+ 候補を
  // 15 分ロックした。未消費の claim は窓を maxAgeSec に縮める。下の不在証明
  // (新鮮な観測・建玉 0・未終端注文なし・口座/銘柄一致)は従来どおり要求する。
  if (String(claim.state || "") === "CLAIMED" && Number(claim.totalAttempts || 0) === 0) {
    const consumeWindowSec = Number(terms.maxAgeSec);
    if (Number.isFinite(consumeWindowSec) && consumeWindowSec > 0) {
      windowSec = Math.min(windowSec, consumeWindowSec);
    }
  }
  if (!Number.isFinite(windowSec) || windowSec <= 0) return false;
  const claimedAt = parseInstant(claim.claimedAt);
  if (claimedAt === null) return false;
  const ageMs = nowMs - claimedAt;
  if (!(ageMs > windowSec * 1000)) return false;
  if (terms.staleReleaseRequiresFlatBroker === false) return true;

  // R69: scope の口座がブローカーから消えていれば、その口座の観測は永久に作れない。
  // 名簿(verified・新鮮)が「居ない」と示す口座には注文も建玉も残せないので、
  // それ自体を不在の証明として受け入れる(古い claim であることは上で確認済み)。
  if (claimScopeVanishedFromBroker(state, claim, nowMs)) return true;

  // 意図ハッシュに紐づく新鮮な観測。無ければ解放しない(=証明できていない)。
  const observation = freshBrokerObservation(state, nowMs, claim.executionIntentHash);
  if (!observation) return false;
  if (!observationAccountInClaimScope(observation, claim)) return false;
  if (observation.symbol !== claim.executionIntent?.symbol) return false;
  const terminal = new Set(executionContract.brokerObservation?.terminalStates || []);
  const orders = Array.isArray(observation.orders) ? observation.orders : [];
  const py = claim.pyramid && typeof claim.pyramid === "object" ? claim.pyramid : null;
  if (py) {
    // R84 §7-5: 追撃 claim は **保有中に置き去りになる**ので、建玉 FLAT を要求すると
    // 永久に解けない。不在の証明を「建玉が凍結 baseQty から増えていない」+
    // 「送信前の集合の外に未終端の行が無い」に置き換える。
    //
    // 仕様の原文は「intent と同方向の未終端注文行がゼロ」だが、DO の観測行は
    // `{orderId, receipt, accountId, symbol, status}` しか持たず side が無い
    // (normalizeBrokerObservation)。side を足すと BROKER_OBSERVATION_FIELDS が
    // 変わり、既存の bo_ ハッシュが全部変わって Python と Worker を同時に
    // 差し替えられない(Worker のデプロイは手動)。集合の外か中かで見る方が
    // 実装可能で、しかも **強い**(同方向に限らず新しい行を一切許さない)。
    //
    // 建玉が baseQty **でない** ときはこの証明を使わず、下の通常の不在証明
    // (建玉 0 + 全注文終端)へ落ちる。典型は「合成建玉が決済されて 0 になった後も
    // 追撃 claim が残っている」で、ここを baseQty 固定にすると **永久に解けない
    // claim** ができ、以後の新規が全部止まる(この repo の事故第 1 位の形)。
    const preSend = new Set((py.preSendOrderIds || []).map((value) => String(value)));
    if (preSend.size && Number(observation.position?.qty) === Number(py.baseQty)) {
      return orders.every((row) => terminal.has(String(row?.status || ""))
        || preSend.has(String(row?.orderId || "")));
    }
  }
  if (observation.position?.qty !== 0) return false;
  // 未終端の注文が 1 本でもあれば、その claim の注文かもしれないので解放しない。
  return orders.every((row) => terminal.has(String(row?.status || "")));
}

function managementRecoveryProof(state, claim, nowMs, payload) {
  const observation = freshBrokerObservation(state, nowMs, claim.managementIntentHash);
  const intent = claim.managementIntent;
  const receipts = Array.isArray(claim.routeReceipt?.accounts) ? claim.routeReceipt.accounts : [];
  if (!observation || !observationCasMatches(observation, payload) || !intent || !receipts.length
      || observation.accountId !== intent.accountId || observation.symbol !== intent.symbol
      || observation.position?.positionGeneration !== intent.positionGeneration
      || observation.position?.qty !== intent.qty
      || observation.position?.side !== (intent.side === "BUY" ? "LONG" : "SHORT")) return false;
  const terminal = new Set(executionContract.brokerObservation?.terminalStates || []);
  // A broker-acquired receipt is frozen per replacement order; the legacy
  // response-body route froze one shared request receipt for both legs.
  return receipts.every((receipt) => [
    [receipt.stopOrderId, receipt.stopReceipt || receipt.receipt],
    [receipt.targetOrderId, receipt.targetReceipt || receipt.receipt],
  ].every(([orderId, expected]) =>
    observation.orders.filter((row) => row.orderId === String(orderId || "")
      && row.receipt === String(expected || "")
      && row.accountId === intent.accountId && row.symbol === intent.symbol
      && terminal.has(row.status)).length === 1));
}

function applyEntryClaimEvent(state, event, revision, nowMs) {
  const payload = event.payload || {};
  const action = String(payload.action || "").toUpperCase();
  const reject = (reason) => ({ state, accepted: false, reason, transitions: [] });
  if (!ENTRY_CLAIM_ACTIONS.has(action)) return reject("entry claim action is invalid");
  const entryKey = String(payload.entryKey || "").trim();
  const tokenHash = String(payload.claimTokenHash || "").toLowerCase();
  if (!entryKey || entryKey.length > 160) return reject("entry claim key is invalid");
  if (!/^[a-f0-9]{64}$/.test(tokenHash)) return reject("entry claim token hash is invalid");
  const currentTuple = authoritativeTuple(state.scenario);
  const suppliedTuple = payload.tuple && typeof payload.tuple === "object" ? payload.tuple : null;
  const claim = state.entryClaim;

  // R84: 追撃 claim。契約が許可していないのに `pyramid` が付いていたら、通常の
  // 新規として黙って通さず明示的に拒否する(意図が違うものを別物として扱わない)。
  const claimPyramid = normalizeClaimPyramid(payload.pyramid);
  const pyramidEnabled = (executionContract.pyramid || {}).enabled === true;
  if (payload.pyramid !== undefined && payload.pyramid !== null
      && (!claimPyramid || !pyramidEnabled)) {
    return reject("ENTRY_CLAIM_PYRAMID_UNAVAILABLE");
  }
  const pyramidMode = Boolean(claimPyramid) && pyramidEnabled;

  if (action === "CLAIM") {
    const projected = projectState(state, nowMs);
    if (pyramidMode) {
      const gate = pyramidClaimGate(state, projected, claimPyramid, nowMs);
      if (!gate.ok) return reject(gate.reason);
    } else if (!projected.display.orderable || !projected.display.cyclePaired) {
      return reject(`ENTRY_CLAIM_NOT_ORDERABLE: ${projected.display.blockReason || "execution gate"}`);
    }
    if (!tupleMatches(suppliedTuple, currentTuple)) return reject("ENTRY_CLAIM_TUPLE_MISMATCH");
    const authoritativeEntryKey = entryKeyForTuple(currentTuple);
    if (!authoritativeEntryKey || entryKey !== authoritativeEntryKey) {
      return reject("ENTRY_CLAIM_KEY_MISMATCH");
    }
    const tombstones = Array.isArray(state.entryTombstones) ? state.entryTombstones : [];
    if (tombstones.some((row) => String(row?.entryKey || "") === entryKey)) {
      return reject("ENTRY_CLAIM_TOMBSTONED");
    }
    const builtIntent = buildExecutionIntent(
      state, payload.orderType, null,
      pyramidMode ? { baseQty: claimPyramid.baseQty, addQty: claimPyramid.addQty } : null);
    if (!builtIntent.ok) return reject(builtIntent.reason);
    const builtIntentHash = executionIntentHash(builtIntent.intent);
    if (!builtIntentHash) return reject("ENTRY_CLAIM_INTENT_INVALID");
    let staleReleased = null;
    let supersededClaim = null;
    if (claim && ENTRY_CLAIM_ACTIVE.has(claim.state)) {
      const locked = ENTRY_CLAIM_LOCKING_RESULTS.has(String(claim.routeState || "").toUpperCase())
        || claim.state === "CLAIMED" || claim.state === "CONSUMED";
      if (locked && claim.state !== "RECOVERED") {
        // R36: ブローカーが空だと証明できた古い claim だけは解放する。
        // 証明できなければ従来どおり拒否する(注文が生きているかもしれない)。
        if (staleEntryClaimReleasable(state, claim, nowMs)) {
          staleReleased = { entryKey: String(claim.entryKey || ""), from: claim.state };
        } else if (pyramidMode && String(claim.entryKey || "") !== entryKey
                   && pyramidSupersedable(state, claim, claimPyramid)) {
          // R84: claim の枠は 1 つしか無い。追撃は「その建玉を作った claim」を
          // 引き継ぐ —— 建玉が生きている以上 RECOVER も stale release も原理的に
          // 通らないので、ここで引き継がないと保有中の追撃は永久に不可能になる。
          // 引き継ぎの条件(同一銘柄・同方向・注文が出た claim・建玉が宣言どおりの
          // 枚数)は全部 DO 自身の state で確かめており、記録も必ず残す。
          supersededClaim = {
            entryKey: String(claim.entryKey || ""), from: claim.state,
            routeState: claim.routeState || null,
            acceptedCount: Number(claim.acceptedCount || 0),
            totalAttempts: Number(claim.totalAttempts || 0),
            supersededAt: new Date(nowMs).toISOString(),
            supersededBy: entryKey, revision,
          };
        } else {
          return reject("ENTRY_CLAIM_ALREADY_HELD");
        }
      }
    }
    if (claim?.state === "RECOVERED" && String(claim.entryKey || "") !== entryKey
        && String(state.entryRelease?.entryKey || "") !== String(claim.entryKey || "")) {
      return reject("ENTRY_CLAIM_PRIOR_TERMINAL_PROOF_REQUIRED");
    }
    const next = bump(state, "entry_claim", revision);
    next.entryClaim = {
      entryKey, tuple: { ...currentTuple }, claimTokenHash: tokenHash,
      executionIntent: builtIntent.intent, executionIntentHash: builtIntentHash,
      state: "CLAIMED", routeState: null, acceptedCount: 0,
      totalAttempts: 0, claimedAt: new Date(nowMs).toISOString(), revision,
      pyramid: pyramidMode ? { ...claimPyramid } : null,
    };
    next.entryRelease = null;
    next.brokerObservation = null;
    const transitions = [{ kind: "entry_claim", from: claim?.state || null,
                           to: "CLAIMED", entryKey }];
    if (supersededClaim) {
      const prior = Array.isArray(state.entrySupersededClaims) ? state.entrySupersededClaims : [];
      next.entrySupersededClaims = [
        ...prior.filter((row) => String(row?.entryKey || "") !== supersededClaim.entryKey),
        supersededClaim,
      ].slice(-8);
      transitions.push({ kind: "entry_claim", from: supersededClaim.from,
                         to: "SUPERSEDED", entryKey: supersededClaim.entryKey });
    }
    if (staleReleased) {
      // 解放は必ず記録に残す。黙って解くと「なぜ再発注できたのか」が
      // 後から追えない。reason にも載せて発行側の監査ログへ届ける。
      next.entryStaleRelease = {
        entryKey: staleReleased.entryKey, from: staleReleased.from,
        releasedAt: new Date(nowMs).toISOString(), revision,
      };
      transitions.push({ kind: "entry_claim", from: staleReleased.from,
                         to: "STALE_RELEASED", entryKey: staleReleased.entryKey });
    }
    let claimReason = null;
    if (staleReleased) {
      claimReason = `ENTRY_CLAIM_STALE_RELEASED: ${staleReleased.entryKey} (${staleReleased.from})`;
    } else if (supersededClaim) {
      claimReason = `ENTRY_CLAIM_PYRAMID_SUPERSEDED: ${supersededClaim.entryKey} (${supersededClaim.from})`;
    }
    return { state: next, accepted: true, reason: claimReason, transitions };
  }

  if (!claim || String(claim.entryKey) !== entryKey || claim.claimTokenHash !== tokenHash) {
    return reject("ENTRY_CLAIM_TOKEN_INVALID");
  }
  if (action === "RELEASE") {
    // R123(2026-09-21): 古い claim を **次の CLAIM を待たずに**捨てる。
    //
    // 多口座の CONSUMED claim は RECOVER を構造的に通せない(1 回の観測で全 receipt を
    // 照合する設計で、1 口座の観測に他口座の注文は載らない)。解放は次の ARMED の CLAIM
    // 到達時だけで、トレードが終わるたびに枠が残り、Mini App は STALE SLOT を出し続けた。
    //
    // CLAIM 時の stale release と**同じ組**で判定する: engine が scope 全口座の verified
    // FLAT・blocking 注文なしを自分で確かめ(R116 の _claim_scope_all_flat。CLAIM の前に
    // やる全口座照会と同じ)、DO は CLAIM 時と同じ staleEntryClaimReleasable を再検証する。
    // 条件は緩めない。token が一致しないと届かない(他人の claim は捨てられない)。
    if (!ENTRY_CLAIM_ACTIVE.has(String(claim.state || ""))) return reject("ENTRY_CLAIM_NOT_ACTIVE");
    if (!staleEntryClaimReleasable(state, claim, nowMs)) return reject("ENTRY_CLAIM_RELEASE_UNVERIFIED");
    const from = String(claim.state);
    const next = bump(state, "entry_claim", revision);
    next.entryClaim = null;
    next.entryRelease = null;
    // 解放は必ず記録に残す。黙って解くと「なぜ枠が空いたのか」が後から追えない。
    next.entryStaleRelease = {
      entryKey, from, releasedAt: new Date(nowMs).toISOString(), revision,
      reason: "ENGINE_SCOPE_ALL_FLAT",
    };
    return { state: next, accepted: true,
      reason: `ENTRY_CLAIM_STALE_RELEASED: ${entryKey} (${from}, engine verified all accounts flat)`,
      transitions: [{ kind: "entry_claim", from, to: "STALE_RELEASED", entryKey }] };
  }
  if (action === "CONSUME") {
    if (claim.state !== "CLAIMED") return reject("ENTRY_CLAIM_NOT_CLAIMED");
    const claimedAt = parseInstant(claim.claimedAt);
    if (claimedAt === null || nowMs < claimedAt
        || nowMs - claimedAt > Number(executionContract.claim.maxAgeSec) * 1000) {
      return reject("ENTRY_CLAIM_EXPIRED");
    }
    const samePending = state.order
      && String(state.order.idempotencyKey || "") === entryKey
      && String(state.order.state || "").toUpperCase() === "PENDING";
    const projected = projectState(samePending ? { ...state, order: null } : state, nowMs);
    const consumePyramid = claim.pyramid && typeof claim.pyramid === "object" ? claim.pyramid : null;
    if (consumePyramid) {
      // R84: 追撃の CONSUME は FLAT を要求できない。`flatVerified` の代わりに
      // 「宣言どおりの基礎建玉が verified で生きている」を要求する。
      // cyclePaired と tuple の一致は通常経路と同じまま残す。
      if (projected.display.cyclePaired !== true || !tupleMatches(claim.tuple, currentTuple)
          || !pyramidBaseVerified(state, claim)) {
        return reject(`ENTRY_CLAIM_SEAL_INVALID: ${projected.display.blockReason || "pyramid base gate"}`);
      }
      // CLAIM から CONSUME までは最大 claim.maxAgeSec(180 秒)ある。その間にシナリオが
      // 期限切れになる・イベント封鎖が始まる・セッションが終わることがあり、通常経路は
      // projected.display.orderable で見直している。追撃も同じ門を通す —— 外すのは
      // 建玉・注文・単体リスク(合成リスクで置き換え済み)の分だけ。
      const consumeRecheck = evaluateExecutionContract(
        state.scenario, state.market, null, null, nowMs);
      const consumeExempt = new Set(["RISK_CAP_EXCEEDED"]);
      const consumeBlockers = (consumeRecheck.blockers || [])
        .filter((value) => !consumeExempt.has(value));
      if (consumeBlockers.length) {
        return reject(`ENTRY_CLAIM_SEAL_INVALID: ${consumeBlockers.join(", ")}`);
      }
    } else if (!projected.display.orderable || !projected.display.cyclePaired
        || !tupleMatches(claim.tuple, currentTuple) || !flatVerified(state)) {
      return reject(`ENTRY_CLAIM_SEAL_INVALID: ${projected.display.blockReason || "authoritative gate"}`);
    }
    const guard = state.market?.dayguard;
    const guardAt = parseInstant(guard?.at);
    if (!guard || guard.available !== true || guardAt === null
        || nowMs < guardAt
        || nowMs - guardAt > Number(executionContract.dayguard.maxAgeSec) * 1000
        || guard.blocked === true) return reject("ENTRY_CLAIM_DAYGUARD_INVALID");
    if (claim.executionIntent?.orderType === "MARKET") {
      const marketAt = parseInstant(state.market?.observedAt || state.market?.at);
      const suppliedLast = Number(payload.executionIntent?.last);
      const currentPrice = Number(state.market?.price);
      if (marketAt === null || nowMs < marketAt
          || nowMs - marketAt > Number(executionContract.marketOrder.maxPriceAgeSec) * 1000) {
        return reject("ENTRY_CLAIM_MARKET_PRICE_STALE");
      }
      if (!Number.isFinite(suppliedLast) || !Number.isFinite(currentPrice)
          || Math.abs(suppliedLast - currentPrice) > Number(executionContract.marketOrder.maxDeviationPoints)) {
        return reject("ENTRY_CLAIM_MARKET_PRICE_DEVIATION");
      }
    }
    const rebuilt = buildExecutionIntent(state, claim.executionIntent?.orderType,
      claim.executionIntent?.orderType === "MARKET" ? claim.executionIntent?.last : null,
      claim.executionIntent?.pyramid || null);
    if (!rebuilt.ok || stableJson(rebuilt.intent) !== stableJson(claim.executionIntent)
        || executionIntentHash(rebuilt.intent) !== claim.executionIntentHash) {
      return reject("ENTRY_CLAIM_CURRENT_INTENT_MISMATCH");
    }
    const suppliedIntent = normalizeExecutionIntent(payload.executionIntent);
    const suppliedIntentHash = String(payload.executionIntentHash || "");
    if (!suppliedIntent.ok || suppliedIntentHash !== executionIntentHash(suppliedIntent.intent)
        || suppliedIntentHash !== claim.executionIntentHash
        || stableJson(suppliedIntent.intent) !== stableJson(claim.executionIntent)) {
      return reject("ENTRY_CLAIM_INTENT_MISMATCH");
    }
    const orderState = String(state.order?.state || "").toUpperCase();
    if (state.order && String(state.order.idempotencyKey || "") !== entryKey
        && BLOCKING_ORDER_STATES.has(orderState)) return reject("ENTRY_CLAIM_ORDER_CONFLICT");
    const next = bump(state, "entry_claim", revision);
    next.entryClaim = { ...claim, state: "CONSUMED", consumedAt: new Date(nowMs).toISOString(), revision };
    return { state: next, accepted: true, reason: null,
      transitions: [{ kind: "entry_claim", from: "CLAIMED", to: "CONSUMED", entryKey }] };
  }

  if (action === "RECOVER") {
    const recoverable = claim.state === "CONSUMED"
      || ["SENT", "PARTIAL", "UNKNOWN"].includes(String(claim.routeState || ""));
    if (!recoverable || !entryRecoveryProof(state, claim, nowMs, payload)) {
      return reject("ENTRY_CLAIM_RECOVERY_UNVERIFIED");
    }
    const next = bump(state, "entry_claim", revision);
    next.entryClaim = { ...claim, state: "RECOVERED", recoveredAt: new Date(nowMs).toISOString(), revision };
    const prior = Array.isArray(state.entryTombstones) ? state.entryTombstones : [];
    next.entryTombstones = [...prior.filter((row) => row?.entryKey !== entryKey), {
      entryKey, tuple: { ...claim.tuple }, recoveredAt: new Date(nowMs).toISOString(),
      brokerSnapshotHash: state.brokerObservation.snapshotHash,
    }].slice(-128);
    next.entryRelease = { entryKey, brokerSnapshotHash: state.brokerObservation.snapshotHash,
      releasedAt: new Date(nowMs).toISOString() };
    return { state: next, accepted: true, reason: null,
      transitions: [{ kind: "entry_claim", from: claim.state, to: "RECOVERED", entryKey }] };
  }

  if (claim.state !== "CONSUMED") return reject("ENTRY_CLAIM_NOT_CONSUMED");
  const routeState = String(payload.routeState || "").toUpperCase();
  const acceptedCount = Number(payload.acceptedCount);
  const totalAttempts = Number(payload.totalAttempts);
  const explicitRejectCount = Number(payload.explicitRejectCount || 0);
  if (!Number.isInteger(acceptedCount) || acceptedCount < 0
      || !Number.isInteger(totalAttempts) || totalAttempts < 1 || acceptedCount > totalAttempts) {
    return reject("ENTRY_CLAIM_RESULT_INVALID");
  }
  let claimState = "CONSUMED";
  if (routeState === "REJECTED" && acceptedCount === 0 && explicitRejectCount === totalAttempts) {
    claimState = "REJECTED_ZERO";
  } else if (!["SENT", "PARTIAL", "UNKNOWN"].includes(routeState)) {
    return reject("ENTRY_CLAIM_RESULT_NOT_AUTHORITATIVE");
  }
  if (!Array.isArray(payload.routeSnapshot) || payload.routeSnapshot.length !== totalAttempts
      || payload.routeSnapshot.length > ROUTE_SNAPSHOT_LIMIT) {
    return reject("ENTRY_CLAIM_ROUTE_SNAPSHOT_REQUIRED");
  }
  const expectedAccounts = Array.isArray(claim.executionIntent?.accountScope)
    ? claim.executionIntent.accountScope.map(String) : [];
  const expectedRouteKeys = new Set(expectedAccounts.flatMap((accountId) =>
    ["TP1", "RUNNER"].map((legId) => `${accountId}\u0000${legId}`)));
  if (!expectedAccounts.length || totalAttempts !== expectedRouteKeys.size) {
    return reject("ENTRY_CLAIM_ROUTE_SCOPE_MISMATCH");
  }
  const seenRouteLegs = new Set();
  const seenOrderIds = new Set();
  const seenReceipts = new Set();
  const routeSnapshot = [];
  for (const row of payload.routeSnapshot) {
    const accountId = String(row?.accountId || "");
    const legId = String(row?.legId || "");
    const routeRowState = String(row?.state || "").toUpperCase();
    const orderId = row?.orderId ? String(row.orderId) : null;
    const receipt = row?.receipt ? String(row.receipt) : null;
    const routeKey = `${accountId}\u0000${legId}`;
    if (!accountId || accountId.length > 64 || !["TP1", "RUNNER"].includes(legId)
        || !["ACCEPTED", "REJECTED", "UNKNOWN"].includes(routeRowState)
        || !expectedRouteKeys.has(routeKey) || seenRouteLegs.has(routeKey)
        || (orderId && orderId.length > 96)
        || (receipt && receipt.length > 160)
        || (routeRowState === "ACCEPTED" && (!orderId || !receipt
          || seenOrderIds.has(orderId) || seenReceipts.has(receipt)))) {
      return reject("ENTRY_CLAIM_ROUTE_SNAPSHOT_INVALID");
    }
    seenRouteLegs.add(routeKey);
    if (routeRowState === "ACCEPTED") {
      seenOrderIds.add(orderId);
      seenReceipts.add(receipt);
    }
    routeSnapshot.push({ accountId, legId, state: routeRowState, orderId, receipt,
      filledAt: parseInstant(row?.filledAt) === null ? null : new Date(parseInstant(row.filledAt)).toISOString() });
  }
  if (seenRouteLegs.size !== expectedRouteKeys.size) {
    return reject("ENTRY_CLAIM_ROUTE_SCOPE_MISMATCH");
  }
  const routeAccepted = routeSnapshot.filter((row) => row.state === "ACCEPTED").length;
  const routeRejected = routeSnapshot.filter((row) => row.state === "REJECTED").length;
  const routeUnknown = routeSnapshot.filter((row) => row.state === "UNKNOWN").length;
  if (routeAccepted !== acceptedCount || routeRejected !== explicitRejectCount
      || routeUnknown !== totalAttempts - acceptedCount - explicitRejectCount) {
    return reject("ENTRY_CLAIM_ROUTE_SNAPSHOT_MISMATCH");
  }
  const next = bump(state, "entry_claim", revision);
  next.entryClaim = { ...claim, state: claimState, routeState, acceptedCount, totalAttempts,
    explicitRejectCount, routeSnapshot, resolvedAt: new Date(nowMs).toISOString(), revision };
  return { state: next, accepted: true, reason: null,
    transitions: [{ kind: "entry_claim", from: claim.state, to: claimState, entryKey, routeState }] };
}

const MANAGEMENT_FIELDS = new Set([
  "version", "accountId", "symbol", "positionGeneration", "action", "side",
  "qty", "stop", "target", "executionContractVersion",
]);

export function normalizeManagementIntent(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)
      || Object.keys(raw).length !== MANAGEMENT_FIELDS.size
      || Object.keys(raw).some((key) => !MANAGEMENT_FIELDS.has(key))) {
    return { ok: false, reason: "management intent fields are invalid" };
  }
  const accountId = String(raw.accountId || "").trim();
  const symbol = String(raw.symbol || "").trim();
  const positionGeneration = String(raw.positionGeneration || "").trim();
  const action = String(raw.action || "").toUpperCase();
  const side = String(raw.side || "").toUpperCase();
  const qty = Number(raw.qty);
  const stop = intentTickText(raw.stop);
  const target = intentTickText(raw.target);
  // R52: ULTRA の runner は 1 枚ではない(例 6)。上限は ULTRA エンベロープの口座別
  // 最大枚数(ULTRA が無効なら従来の固定枚数)。Python の management_intent.build と
  // 同じ規則(intent hash の同一性に関わる)。
  const ultraEnvelope = executionContract.ultra || {};
  const managementQtyCeiling = ultraEnvelope.enabled
    ? Math.max(Number(executionContract.risk.fixedQty), Number(ultraEnvelope.maxQtyPerAccount || 0))
    : Number(executionContract.risk.fixedQty);
  if (String(raw.version || "") !== String(executionContract.management.version)
      || String(raw.executionContractVersion || "") !== String(executionContract.version)
      || !/^[A-Za-z0-9_-]{3,64}$/.test(accountId) || !symbol || symbol.length > 32
      || !positionGeneration || positionGeneration.length > 160 || action !== "MODIFY"
      || !["BUY", "SELL"].includes(side) || !Number.isInteger(qty)
      || qty < 1 || qty > managementQtyCeiling
      || stop === null || target === null) {
    return { ok: false, reason: "management intent canonical fields are invalid" };
  }
  return { ok: true, intent: {
    version: String(executionContract.management.version), accountId, symbol,
    positionGeneration, action, side, qty, stop, target,
    executionContractVersion: String(executionContract.version),
  } };
}

export function managementIntentHash(raw) {
  const checked = normalizeManagementIntent(raw);
  return checked.ok ? `mi_${sha256Hex(new TextEncoder().encode(stableJson(checked.intent)))}` : null;
}

export function managementKeyForIntent(raw) {
  const checked = normalizeManagementIntent(raw);
  return checked.ok ? `MANAGEMENT:${sha256Hex(new TextEncoder().encode(stableJson(checked.intent)))}` : null;
}

function authoritativeManagementIntent(state, requested) {
  const position = state.position;
  const checked = normalizeManagementIntent(requested);
  if (!checked.ok || !position || position.verified !== true
      || !OPEN_POSITION_STATES.has(String(position.state || "").toUpperCase())) return null;
  const expectedSide = String(position.side || "").toUpperCase() === "LONG" ? "BUY" : "SELL";
  const authoritative = normalizeManagementIntent({
    ...checked.intent,
    accountId: position.accountId,
    symbol: position.symbol,
    positionGeneration: position.positionGeneration,
    side: expectedSide,
    qty: position.qty,
  });
  return authoritative.ok ? authoritative.intent : null;
}

function applyManagementClaimEvent(state, event, revision, nowMs) {
  const payload = event.payload || {};
  const action = String(payload.action || "").toUpperCase();
  const key = String(payload.managementKey || "");
  const tokenHash = String(payload.claimTokenHash || "").toLowerCase();
  const reject = (reason) => ({ state, accepted: false, reason, transitions: [] });
  if (!["CLAIM", "CONSUME", "RESOLVE", "RECOVER"].includes(action)
      || !key || key.length > 160 || !/^[a-f0-9]{64}$/.test(tokenHash)) {
    return reject("MANAGEMENT_CLAIM_REQUEST_INVALID");
  }
  const current = state.managementClaim;
  if (action === "CLAIM") {
    const authoritative = authoritativeManagementIntent(state, payload.managementIntent);
    if (!authoritative) return reject("MANAGEMENT_CLAIM_POSITION_MISMATCH");
    const hash = managementIntentHash(authoritative);
    if (payload.managementIntentHash !== hash || managementKeyForIntent(authoritative) !== key
        || stableJson(authoritative) !== stableJson(payload.managementIntent)) {
      return reject("MANAGEMENT_CLAIM_INTENT_MISMATCH");
    }
    if (current?.state === "CONSUMED") {
      return reject("MANAGEMENT_CLAIM_ALREADY_HELD");
    }
    if (current?.state === "CLAIMED") {
      const priorAt = parseInstant(current.claimedAt);
      const ageMs = priorAt === null ? null : nowMs - priorAt;
      if (ageMs === null || ageMs < 0) return reject("MANAGEMENT_CLAIM_TIME_INVALID");
      if (ageMs <= Number(executionContract.management.claimMaxAgeSec) * 1000) {
        return reject("MANAGEMENT_CLAIM_ALREADY_HELD");
      }
    }
    if (current?.state !== "RECOVERED" && current && current.managementKey === key
        && ["SENT", "PARTIAL", "UNKNOWN"].includes(String(current.routeState || ""))) {
      return reject("MANAGEMENT_CLAIM_ALREADY_RESOLVED");
    }
    const next = bump(state, "management_claim", revision);
    next.managementClaim = { managementKey: key, claimTokenHash: tokenHash,
      managementIntent: authoritative, managementIntentHash: hash, state: "CLAIMED",
      routeState: null, claimedAt: new Date(nowMs).toISOString(), revision };
    next.brokerObservation = null;
    return { state: next, accepted: true, reason: null,
      transitions: [{ kind: "management_claim", from: current?.state || null, to: "CLAIMED", managementKey: key }] };
  }
  if (!current || current.managementKey !== key || current.claimTokenHash !== tokenHash) {
    return reject("MANAGEMENT_CLAIM_TOKEN_INVALID");
  }
  if (action === "RECOVER") {
    const recoverable = current.state === "CONSUMED"
      || (current.state === "RESOLVED" && String(current.routeState || "") === "UNKNOWN");
    if (!recoverable || !managementRecoveryProof(state, current, nowMs, payload)) {
      return reject("MANAGEMENT_CLAIM_RECOVERY_UNVERIFIED");
    }
    const next = bump(state, "management_claim", revision);
    next.managementClaim = { ...current, state: "RECOVERED",
      recoveredAt: new Date(nowMs).toISOString(), revision };
    return { state: next, accepted: true, reason: null,
      transitions: [{ kind: "management_claim", from: current.state, to: "RECOVERED",
        managementKey: key }] };
  }
  if (action === "CONSUME") {
    if (current.state !== "CLAIMED") return reject("MANAGEMENT_CLAIM_NOT_CLAIMED");
    const claimedAt = parseInstant(current.claimedAt);
    if (claimedAt === null || nowMs < claimedAt
        || nowMs - claimedAt > Number(executionContract.management.claimMaxAgeSec) * 1000) {
      return reject("MANAGEMENT_CLAIM_EXPIRED");
    }
    const authoritative = authoritativeManagementIntent(state, payload.managementIntent);
    const hash = authoritative ? managementIntentHash(authoritative) : null;
    if (!authoritative || hash !== payload.managementIntentHash || hash !== current.managementIntentHash
        || stableJson(authoritative) !== stableJson(current.managementIntent)) {
      return reject("MANAGEMENT_CLAIM_CURRENT_POSITION_MISMATCH");
    }
    const next = bump(state, "management_claim", revision);
    next.managementClaim = { ...current, state: "CONSUMED",
      consumedAt: new Date(nowMs).toISOString(), revision };
    return { state: next, accepted: true, reason: null,
      transitions: [{ kind: "management_claim", from: "CLAIMED", to: "CONSUMED", managementKey: key }] };
  }
  if (current.state !== "CONSUMED") return reject("MANAGEMENT_CLAIM_NOT_CONSUMED");
  const routeState = String(payload.routeState || "").toUpperCase();
  if (!["SENT", "PARTIAL", "UNKNOWN", "REJECTED"].includes(routeState)) {
    return reject("MANAGEMENT_CLAIM_RESULT_INVALID");
  }
  const next = bump(state, "management_claim", revision);
  next.managementClaim = { ...current, state: routeState === "REJECTED" ? "REJECTED_ZERO" : "RESOLVED",
    routeState, routeReceipt: payload.routeReceipt && typeof payload.routeReceipt === "object"
      ? JSON.parse(JSON.stringify(payload.routeReceipt)) : null,
    resolvedAt: new Date(nowMs).toISOString(), revision };
  return { state: next, accepted: true, reason: null,
    transitions: [{ kind: "management_claim", from: "CONSUMED", to: next.managementClaim.state,
      managementKey: key, routeState }] };
}

// R56: stop は必須から外した。ブローカー約定から組んだ記録(exitSource=broker)で
// 凍結プランが無い手動建玉は「受け入れたリスク」を持たない。R を出さないだけで
// 損益は事実なので受け付ける。manual(手入力)は validateResult 内で従来どおり要求する。
const REQUIRED_RESULT_FIELDS = [
  "resultId", "side", "symbol", "qty", "entry", "exit",
  "pointValue", "openedAt", "closedAt", "exitSource", "pathSource", "mode",
];

/** 分割決済の脚数の上限と、脚の着地種別。order.py の分割は 2 脚だが余裕を持たせる。 */
const RESULT_LEG_LIMIT = 8;
const RESULT_LEG_KINDS = new Set(["target", "stop", "manual"]);

/**
 * 決済結果を検証する。
 *
 * 表示層は state / pnl / R / held を自分で導出する契約なので、それらが
 * 混ざっていたら拒否する。外から渡された数字と表示が乖離する余地を残さない。
 * path は実観測由来のものしか通さない。
 */
export function validateResult(raw, { symbol }) {
  if (!raw || typeof raw !== "object") return { ok: false, reason: "result is not an object" };

  for (const field of REQUIRED_RESULT_FIELDS) {
    if (raw[field] === undefined || raw[field] === null || raw[field] === "") {
      return { ok: false, reason: `result.${field} is required` };
    }
  }

  // 表示層が導出する値を外から渡させない(HANDOFF §3「渡してはいけないもの」)。
  for (const forbidden of ["state", "pnl", "realisedR", "held", "usd"]) {
    if (forbidden in raw) return { ok: false, reason: `result.${forbidden} must be derived, not supplied` };
  }

  if (String(raw.symbol) !== String(symbol)) {
    return { ok: false, reason: `result.symbol ${raw.symbol} does not match ${symbol}` };
  }

  const side = String(raw.side).toUpperCase();
  if (!["LONG", "SHORT"].includes(side)) return { ok: false, reason: "result.side is invalid" };

  const exitSource = String(raw.exitSource);
  if (!EXIT_SOURCES.has(exitSource)) {
    return { ok: false, reason: `result.exitSource ${exitSource} is not an accepted source` };
  }
  const pathSource = String(raw.pathSource);
  if (!PATH_SOURCES.has(pathSource)) {
    return { ok: false, reason: `result.pathSource ${pathSource} is not an accepted source` };
  }

  const openedAt = parseInstant(raw.openedAt);
  const closedAt = parseInstant(raw.closedAt);
  if (openedAt === null) return { ok: false, reason: "result.openedAt is not a timestamp" };
  if (closedAt === null) return { ok: false, reason: "result.closedAt is not a timestamp" };
  if (closedAt < openedAt) return { ok: false, reason: "result.closedAt precedes openedAt" };

  const entry = normalizeTick(Number(raw.entry));
  const exit = normalizeTick(Number(raw.exit));
  if (entry === null) return { ok: false, reason: "result.entry is invalid" };
  if (exit === null) return { ok: false, reason: "result.exit is invalid" };
  // R56: stop は「受け入れたリスク」であって決済の事実ではない。ブローカー約定から
  // 組んだ記録で凍結プランが無い(手動建玉)ときだけ null を許し、R は導出しない。
  const stopMissing = raw.stop === undefined || raw.stop === null || raw.stop === "";
  if (stopMissing && exitSource !== "broker") {
    return { ok: false, reason: "result.stop is required unless exitSource is broker" };
  }
  const stop = stopMissing ? null : normalizeTick(Number(raw.stop));
  if (!stopMissing && stop === null) return { ok: false, reason: "result.stop is invalid" };
  if (stop !== null && priceEquals(entry, stop)) {
    return { ok: false, reason: "result.stop equals entry, R would be undefined" };
  }

  const qty = Number(raw.qty);
  if (!Number.isInteger(qty) || qty < 1) return { ok: false, reason: "result.qty is invalid" };
  const pointValue = Number(raw.pointValue);
  if (!isFiniteNumber(pointValue) || pointValue <= 0) return { ok: false, reason: "result.pointValue is invalid" };

  // path は必須。2点未満は「観測が無い」ので受け付けない。
  const path = Array.isArray(raw.path) ? raw.path.map(Number) : null;
  if (!path || path.length < 2) return { ok: false, reason: "result.path needs at least two observed points" };
  if (!path.every(isFiniteNumber)) return { ok: false, reason: "result.path contains a non-finite value" };
  if (pathSource === "endpoints-only" && path.length !== 2) {
    return { ok: false, reason: "endpoints-only path must contain exactly the two endpoints" };
  }

  // 滑り+手数料(任意、1口座分の $)。実約定価格を取得できないので、価格から
  // 出るのは粗損益でしかない。ブローカーの実現損益との差はここに入る。
  // 「導出してはいけない値」ではない —— 価格から出せない**観測値**である。
  let fees = null;
  if (raw.fees !== undefined && raw.fees !== null) {
    fees = Number(raw.fees);
    if (!isFiniteNumber(fees) || fees < 0) return { ok: false, reason: "result.fees is invalid" };
    fees = Math.round(fees * 100) / 100;
  }

  // 分割決済の内訳(任意)。R12 の分割型は 1枚を TP1、1枚を runner で持つので
  // 決済が 1 価格に収まらない。exit はあくまで数量加重平均で、legs はその
  // 内訳を失わないための追加情報。ここでも損益は受け取らない(導出物)。
  let legs = null;
  if (raw.legs !== undefined && raw.legs !== null) {
    if (!Array.isArray(raw.legs) || !raw.legs.length || raw.legs.length > RESULT_LEG_LIMIT) {
      return { ok: false, reason: "result.legs is invalid" };
    }
    legs = [];
    let legQty = 0;
    let weighted = 0;
    for (const row of raw.legs) {
      if (!row || typeof row !== "object") return { ok: false, reason: "result.legs row is invalid" };
      for (const forbidden of ["pnl", "usd", "state"]) {
        if (forbidden in row) return { ok: false, reason: `result.legs.${forbidden} must be derived` };
      }
      const rowQty = Number(row.qty);
      const rowExit = normalizeTick(Number(row.exit));
      if (!Number.isInteger(rowQty) || rowQty < 1) return { ok: false, reason: "result.legs.qty is invalid" };
      if (rowExit === null) return { ok: false, reason: "result.legs.exit is invalid" };
      const rowTarget = row.target === undefined || row.target === null
        ? null : normalizeTick(Number(row.target));
      if (row.target !== undefined && row.target !== null && rowTarget === null) {
        return { ok: false, reason: "result.legs.target is invalid" };
      }
      const kind = String(row.kind || "manual");
      if (!RESULT_LEG_KINDS.has(kind)) return { ok: false, reason: `result.legs.kind ${kind} is invalid` };
      legs.push({ id: String(row.id || `LEG${legs.length + 1}`).slice(0, 24),
                  qty: rowQty, exit: rowExit, target: rowTarget, kind });
      legQty += rowQty;
      weighted += rowExit * rowQty;
    }
    if (legQty !== qty) return { ok: false, reason: "result.legs qty does not sum to result.qty" };
    // 表示の建値と内訳が食い違わないこと。1tick 以内に収まらなければ拒否する。
    if (Math.abs(weighted / legQty - exit) > TICK + 1e-9) {
      return { ok: false, reason: "result.legs weighted exit does not match result.exit" };
    }
  }

  // R57: 根拠チャート(任意)。建玉前後の確定 3 分足・VP 水準・凍結ターゲット・
  // 根拠タグ・減点・HTF。表示専用で、判定・損益には一切使わない。形が壊れていれば拒否。
  const chartChecked = validateResultChart(raw.chart);
  if (!chartChecked.ok) return { ok: false, reason: chartChecked.reason };

  return {
    ok: true,
    result: {
      resultId: String(raw.resultId),
      side,
      symbol: String(raw.symbol),
      qty,
      entry,
      exit,
      stop,
      pointValue,
      openedAt: new Date(openedAt).toISOString(),
      closedAt: new Date(closedAt).toISOString(),
      path,
      pathSource,
      pathPoints: path.length,
      exitSource,
      legs,
      fees,
      verdict: raw.verdict ? String(raw.verdict) : null,
      mode: String(raw.mode).toUpperCase() === "LIVE" ? "LIVE" : "SIMULATION",
      receipt: raw.receipt ? String(raw.receipt) : null,
      // ライフサイクル突合キー(任意)。どのシナリオ・口座の決済かを表示層が
      // キーで辿るためのもの。entry claim・実行契約の判定には一切使わない。
      scenarioId: raw.scenarioId ? String(raw.scenarioId).slice(0, 96) : null,
      accountId: raw.accountId ? String(raw.accountId).slice(0, 64) : null,
      // R48: モデル別スコアカード。凍結プランの model/grade(表示・集計専用)。
      model: raw.model ? String(raw.model).slice(0, 32) : null,
      grade: ["A+", "A", "B"].includes(raw.grade) ? String(raw.grade) : null,
      chart: chartChecked.chart,
    },
  };
}

/** R57: result.chart の上限。DO の resultLog は 50 件保持するので 1 件あたりを小さく保つ。 */
export const RESULT_CHART_VERSION = "NQX-RESULT-CHART/1";
export const RESULT_CHART_MAX_BARS = 80;
export const RESULT_CHART_MAX_LEVELS = 12;
export const RESULT_CHART_MAX_TAGS = 16;
const RESULT_CHART_TAG = /^[A-Z0-9_+\-.: ]{1,32}$/;

/**
 * 根拠チャートを検証する(任意フィールド)。
 *
 * bars は [t, o, h, l, c] の配列(t は秒、昇順、l ≤ min(o,c) ≤ max(o,c) ≤ h)。
 * levels は {label, price}。tp1/tp2 は tick 整合。タグは英大文字の識別子だけ。
 * 数値はすべて有限。上限超えは拒否(黙って切らない —— 送り側の上限と同じ値)。
 */
export function validateResultChart(raw) {
  if (raw === undefined || raw === null) return { ok: true, chart: null };
  if (typeof raw !== "object" || Array.isArray(raw)) return { ok: false, reason: "result.chart is not an object" };
  if (String(raw.version || "") !== RESULT_CHART_VERSION) {
    return { ok: false, reason: `result.chart.version ${raw.version} is not supported` };
  }
  const tf = Number(raw.tf);
  if (!Number.isInteger(tf) || tf < 60 || tf > 3600) return { ok: false, reason: "result.chart.tf is invalid" };

  const bars = [];
  const rawBars = Array.isArray(raw.bars) ? raw.bars : null;
  if (!rawBars) return { ok: false, reason: "result.chart.bars is not an array" };
  if (rawBars.length > RESULT_CHART_MAX_BARS) return { ok: false, reason: "result.chart.bars exceeds the limit" };
  let prevT = -Infinity;
  for (const row of rawBars) {
    if (!Array.isArray(row) || row.length !== 5) return { ok: false, reason: "result.chart.bars row is invalid" };
    const [t, o, h, l, c] = row.map(Number);
    if (!Number.isInteger(t) || t <= prevT) return { ok: false, reason: "result.chart.bars time is not increasing" };
    if (![o, h, l, c].every(isFiniteNumber)) return { ok: false, reason: "result.chart.bars price is not finite" };
    if (l > Math.min(o, c) + 1e-9 || h < Math.max(o, c) - 1e-9) {
      return { ok: false, reason: "result.chart.bars OHLC is inconsistent" };
    }
    bars.push([t, o, h, l, c]);
    prevT = t;
  }

  const levels = [];
  const rawLevels = Array.isArray(raw.levels) ? raw.levels : [];
  if (rawLevels.length > RESULT_CHART_MAX_LEVELS) return { ok: false, reason: "result.chart.levels exceeds the limit" };
  for (const level of rawLevels) {
    if (!level || typeof level !== "object") return { ok: false, reason: "result.chart.levels row is invalid" };
    const label = String(level.label || "").slice(0, 24);
    const price = Number(level.price);
    if (!label || !isFiniteNumber(price)) return { ok: false, reason: "result.chart.levels row is invalid" };
    levels.push({ label, price });
  }

  const optionalTick = (value, name) => {
    if (value === undefined || value === null || value === "") return { ok: true, value: null };
    const tick = normalizeTick(Number(value));
    return tick === null ? { ok: false, reason: `result.chart.${name} is invalid` } : { ok: true, value: tick };
  };
  const tp1 = optionalTick(raw.tp1, "tp1");
  if (!tp1.ok) return tp1;
  const tp2 = optionalTick(raw.tp2, "tp2");
  if (!tp2.ok) return tp2;

  const tags = (list, name) => {
    if (list === undefined || list === null) return { ok: true, value: [] };
    if (!Array.isArray(list) || list.length > RESULT_CHART_MAX_TAGS) {
      return { ok: false, reason: `result.chart.${name} is invalid` };
    }
    const out = [];
    for (const item of list) {
      const text = String(item);
      if (!RESULT_CHART_TAG.test(text)) return { ok: false, reason: `result.chart.${name} tag ${text} is invalid` };
      out.push(text);
    }
    return { ok: true, value: out };
  };
  const evidence = tags(raw.evidence, "evidence");
  if (!evidence.ok) return evidence;
  const penalties = tags(raw.penalties, "penalties");
  if (!penalties.ok) return penalties;

  const htf = {};
  if (raw.htf !== undefined && raw.htf !== null) {
    if (typeof raw.htf !== "object" || Array.isArray(raw.htf)) return { ok: false, reason: "result.chart.htf is invalid" };
    for (const [key, value] of Object.entries(raw.htf).slice(0, 8)) {
      htf[String(key).slice(0, 8)] = String(value).slice(0, 12);
    }
  }
  const optionalNumber = (value) => (value === undefined || value === null ? null
    : (isFiniteNumber(Number(value)) ? Number(value) : null));

  return {
    ok: true,
    chart: {
      version: RESULT_CHART_VERSION,
      tf,
      bars,
      levels,
      tp1: tp1.value,
      tp2: tp2.value,
      evidence: evidence.value,
      penalties: penalties.value,
      htf,
      volRatio: optionalNumber(raw.volRatio),
      noise: optionalNumber(raw.noise),
      session: raw.session ? String(raw.session).slice(0, 24) : null,
      source: raw.source ? String(raw.source).slice(0, 48) : null,
    },
  };
}

/**
 * 口座別の残機(LIFELINE)を検証する。
 *
 * buffer は「撤退ラインまでの残り $」。残高のように扱うが、ブローカーの
 * 口座残高そのものではない(CrossTrade REST からは取得できないため、
 * PC 側の設定とリザルト記録が正本)。合計 totalBuffer はここで導出し、
 * 表示層に別の合計を計算させない。
 */
export function validateAccounts(raw) {
  if (!raw || typeof raw !== "object") return { ok: false, reason: "accounts is not an object" };

  const observedAt = parseInstant(raw.observedAt);
  if (observedAt === null) return { ok: false, reason: "accounts.observedAt is not a timestamp" };
  const source = raw.source ? String(raw.source) : null;
  if (!source) return { ok: false, reason: "accounts.source is required" };

  if (!Array.isArray(raw.list) || raw.list.length < 1) {
    return { ok: false, reason: "accounts.list requires at least one account" };
  }
  if (raw.list.length > ACCOUNT_LIST_LIMIT) {
    return { ok: false, reason: `accounts.list exceeds ${ACCOUNT_LIST_LIMIT} accounts` };
  }

  const seen = new Set();
  const list = [];
  for (const [index, item] of raw.list.entries()) {
    if (!item || typeof item !== "object") return { ok: false, reason: `accounts.list[${index}] is not an object` };
    const id = String(item.id || "").trim();
    if (!id) return { ok: false, reason: `accounts.list[${index}].id is required` };
    if (seen.has(id)) return { ok: false, reason: `accounts.list duplicates id ${id}` };
    seen.add(id);
    const cap = Number(item.cap);
    if (!isFiniteNumber(cap) || cap <= 0) return { ok: false, reason: `accounts.list[${index}].cap is invalid` };
    const buffer = Number(item.buffer);
    // 残機は 0 まで(吹き飛んだ口座は 0 として publish する)。負値はデータ異常。
    if (!isFiniteNumber(buffer) || buffer < 0) return { ok: false, reason: `accounts.list[${index}].buffer is invalid` };
    // ULTRA mode の利益目標。任意項目で、無い口座は ULTRA 側で
    // INELIGIBLE になる(枚数を推測して作らない)。0/負値は設定ミスなので拒否。
    const row = {
      id,
      label: item.label ? String(item.label) : `…${id.slice(-4)}`,
      cap,
      buffer,
    };
    if (item.profitTarget !== undefined && item.profitTarget !== null) {
      const profitTarget = Number(item.profitTarget);
      if (!isFiniteNumber(profitTarget) || profitTarget <= 0) {
        return { ok: false, reason: `accounts.list[${index}].profitTarget is invalid` };
      }
      row.profitTarget = profitTarget;
    }
    // R44: ブローカーの純資産(netLiq)。任意項目で、残高照会が通ったときだけ載る。
    // 残機(buffer)は「床までの余裕」なので、口座に実際いくらあるのかは
    // これが無いと画面から分からなかった。0 は吹き飛んだ口座の実在値なので
    // 通す。負値は先物口座では起こりうるが表示の意味を成さないので拒否する。
    if (item.equity !== undefined && item.equity !== null) {
      const equity = Number(item.equity);
      if (!isFiniteNumber(equity) || equity < 0) {
        return { ok: false, reason: `accounts.list[${index}].equity is invalid` };
      }
      row.equity = equity;
    }
    list.push(row);
  }

  const accounts = {
    observedAt: new Date(observedAt).toISOString(),
    source,
    list,
    totalBuffer: list.reduce((sum, item) => sum + item.buffer, 0),
  };

  // 口座名簿の突合(任意)。設定に居るのにブローカーから消えた口座(missing)、
  // ブローカーに現れた未設定の口座(unknown)、死んだ状態の口座(dead)を運ぶ。
  // 表示専用 — accountScope や実行契約には一切影響しない。
  if (raw.sync !== undefined && raw.sync !== null) {
    const sync = raw.sync;
    if (typeof sync !== "object" || Array.isArray(sync)) {
      return { ok: false, reason: "accounts.sync is invalid" };
    }
    const normalized = { verified: sync.verified === true };
    const syncObserved = parseInstant(sync.observedAt);
    normalized.observedAt = syncObserved === null ? null : new Date(syncObserved).toISOString();
    for (const key of ["missing", "unknown", "dead"]) {
      const rows = sync[key];
      if (rows === undefined || rows === null) { normalized[key] = []; continue; }
      if (!Array.isArray(rows) || rows.length > 16) {
        return { ok: false, reason: `accounts.sync.${key} is invalid` };
      }
      const values = rows.map((value) => String(value || "").trim().slice(0, 64)).filter(Boolean);
      if (values.length !== rows.length) return { ok: false, reason: `accounts.sync.${key} is invalid` };
      normalized[key] = values;
    }
    accounts.sync = normalized;
  }

  return { ok: true, accounts };
}

// ---------------------------------------------------------------- イベント適用

/**
 * 1 イベントを適用して次の状態を返す。state は変更せず新しいオブジェクトを返す。
 *
 * @param {object} state    現在の状態
 * @param {object} event    {stream, revision, payload, nonce, issuedAt}
 * @param {number} nowMs    サーバー時刻(ms)
 * @returns {{state: object, accepted: boolean, reason: string|null, transitions: object[]}}
 */
export function applyEvent(state, event, nowMs) {
  const reject = (reason) => ({ state, accepted: false, reason, transitions: [] });

  if (!event || typeof event !== "object") return reject("event is not an object");
  const stream = String(event.stream || "");
  if (!STREAMS.includes(stream)) return reject(`unknown stream ${stream}`);

  const revision = Number(event.revision);
  if (!Number.isInteger(revision) || revision < 1) return reject("revision must be a positive integer");

  // 単調増加のみ受け付ける。順不同・再送・巻き戻しはここで落ちる。
  const current = state.revisions[stream] || 0;
  if (revision <= current) {
    // The request has already passed Worker authentication before applyEvent.
    // A signed lower/equal revision for another cycle is therefore a hostile
    // or broken producer signal: preserving the old armed pair would be a
    // legacy authorization bypass.  Disarm using only existing state; never
    // adopt the rollback payload. A replay of the same rollback remains inert.
    if (stream === "cycle") {
      const incomingCycleId = String(event.payload?.cycleId || "").trim();
      const activeCycleId = String(state.market?.cycleId || state.scenario?.marketCycleId || "").trim();
      if (incomingCycleId && activeCycleId && incomingCycleId !== activeCycleId
          && incomingCycleId !== state.rollbackCycleId) {
        return disarmForCycleRollback(state, incomingCycleId, revision, nowMs);
      }
    }
    return reject(`revision ${revision} is not newer than ${current} for stream ${stream}`);
  }

  switch (stream) {
    case "scenario": return applyScenarioEvent(state, event, revision, nowMs);
    case "position": return applyPositionEvent(state, event, revision, nowMs);
    case "order": return applyOrderEvent(state, event, revision, nowMs);
    case "market": return applyMarketEvent(state, event, revision, nowMs);
    case "result": return applyResultEvent(state, event, revision, nowMs);
    case "account": return applyAccountEvent(state, event, revision, nowMs);
    case "cycle": return applyCycleEvent(state, event, revision, nowMs);
    case "entry_claim": return applyEntryClaimEvent(state, event, revision, nowMs);
    case "management_claim": return applyManagementClaimEvent(state, event, revision, nowMs);
    case "broker_observation": return applyBrokerObservationEvent(state, event, revision, nowMs);
    case "cycle_health": return applyCycleHealthEvent(state, event, revision, nowMs);
    default: return reject(`unknown stream ${stream}`);
  }
}

/**
 * 口座別ユーザー設定(accountPrefs)の検証。
 *
 * ULTRA を有効にできるのは同時に **1口座だけ**。ENTRY claim key が口座次元を
 * 持たないため、複数口座への同時 ULTRA ルーティングは未開通(ULTRA_MODE §制約)。
 * ここで構造的に1口座へ制限することで、下流が多重 claim を試みる余地を消す。
 * 数値はここでは形だけを見る — 実際の枚数・リスクは PC 側の ultra_mode と
 * 実行契約エンベロープが必ず再計算・クランプする。
 */
export function validateAccountPrefs(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return { ok: false, reason: "prefs is not an object" };
  }
  const entries = Object.entries(raw);
  if (entries.length > 8) return { ok: false, reason: "prefs exceeds 8 accounts" };
  const map = {};
  let ultraCount = 0;
  for (const [id, value] of entries) {
    if (!/^[A-Za-z0-9_-]{3,64}$/.test(id)) return { ok: false, reason: `prefs id ${id.slice(0, 8)}… is invalid` };
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return { ok: false, reason: `prefs.${id} is not an object` };
    }
    const row = { ultra: value.ultra === true };
    if (row.ultra) ultraCount += 1;
    for (const key of ["profitTarget", "maxDrawdown"]) {
      if (value[key] === undefined || value[key] === null) { row[key] = null; continue; }
      const num = Number(value[key]);
      if (!Number.isFinite(num) || num <= 0 || num > 1_000_000) {
        return { ok: false, reason: `prefs.${id}.${key} is invalid` };
      }
      row[key] = Math.round(num * 100) / 100;
    }
    map[id] = row;
  }
  // R109(2026-09-18): ULTRA は複数口座で張れる。上限は契約の ultra.maxAccounts。
  const maxUltra = Number(executionContract.ultra?.maxAccounts) || 1;
  if (ultraCount > maxUltra) {
    return { ok: false,
      reason: `ULTRA can be enabled for at most ${maxUltra} account${maxUltra === 1 ? "" : "s"}` };
  }
  // R109: execution intent は scope 全体で枚数を 1 つしか持たない。ULTRA の枚数は
  // 「その口座の利益目標に届く枚数」なので、目標が割れた口座を同時に選ぶと必ず
  // どれかが目標に届かない。monitor_publish._apply_ultra_prefs は publish 前に
  // WATCH へ落とすが、それでは「設定したのに出ない」になるので保存時に弾く。
  const ultraTargets = Object.entries(map)
    .filter(([, row]) => row.ultra === true)
    .map(([id, row]) => [id, row.profitTarget]);
  const blankTarget = ultraTargets.find(([, target]) => !(Number(target) > 0));
  if (blankTarget) {
    return { ok: false,
      reason: `ULTRA account …${blankTarget[0].slice(-6)} needs a profit target` };
  }
  if (new Set(ultraTargets.map(([, target]) => target)).size > 1) {
    return { ok: false,
      reason: "ULTRA accounts must share one profit target" };
  }
  return { ok: true, map };
}

const CYCLE_HEALTH_STATUSES = ["PUBLISHED", "BLOCKED", "HALT"];

/**
 * 監視ループのビーコン(「監視の監視」)。
 *
 * BLOCKED / HALT のサイクルは market・scenario を publish しない(nqx_cycle は
 * publish 段の手前で止まる)ため、Mini App からは「沈黙の長さ」しか見えなかった。
 * この stream はその穴を埋める軽量な生存報告で、**表示専用**。市場データ・
 * シナリオ・建玉・注文には一切触れず、実行契約の判定にも使わない。
 */
function applyCycleHealthEvent(state, event, revision, nowMs) {
  const payload = event.payload || {};
  for (const foreign of ["position", "scenario", "order", "result", "market", "accounts"]) {
    if (foreign in payload) {
      return { state, accepted: false, reason: `cycle_health events must not carry ${foreign} data`, transitions: [] };
    }
  }
  const beacon = payload.beacon;
  if (!beacon || typeof beacon !== "object") {
    return { state, accepted: false, reason: "beacon is required", transitions: [] };
  }
  const status = String(beacon.status || "").toUpperCase();
  if (!CYCLE_HEALTH_STATUSES.includes(status)) {
    return { state, accepted: false, reason: `beacon.status must be one of ${CYCLE_HEALTH_STATUSES.join("/")}`, transitions: [] };
  }
  if (parseInstant(beacon.at) === null) {
    return { state, accepted: false, reason: "beacon.at must be an ISO timestamp", transitions: [] };
  }
  const reason = beacon.reason === null || beacon.reason === undefined
    ? null : String(beacon.reason).slice(0, 500);
  const next = bump(state, "cycle_health", revision);
  next.cycleHealth = {
    status,
    at: beacon.at,
    reason,
    kill: beacon.kill === true,
    revision,
    publishedAt: new Date(nowMs).toISOString(),
  };
  return { state: next, accepted: true, reason: null, transitions: [] };
}

function disarmForCycleRollback(state, incomingCycleId, revision, nowMs) {
  const existingCycleId = state.market?.cycleId || state.scenario?.marketCycleId || null;
  const next = {
    ...state,
    seq: state.seq + 1,
    // Keep the last verified market for display, but revoke its commit seal.
    // No field from the rollback payload is installed.
    market: state.market ? {
      ...state.market,
      cycleCommitted: false,
      rollbackObservedAt: new Date(nowMs).toISOString(),
    } : null,
    scenario: null,
    cycleLeaseExpiresAt: null,
    rollbackCycleId: incomingCycleId,
  };
  return {
    state: next,
    accepted: true,
    reason: `CYCLE_ROLLBACK_DISARMED: revision ${revision} is not newer than ${state.revisions.cycle || 0}`,
    transitions: [
      { kind: "market", from: existingCycleId, to: existingCycleId, cycleId: existingCycleId,
        disarmedByCycleId: incomingCycleId },
      { kind: "scenario", from: state.scenario?.state || null, to: "CANCELED",
        scenarioId: state.scenario?.scenarioId || null, disarmedByCycleId: incomingCycleId },
    ],
  };
}

function bump(state, stream, revision) {
  return {
    ...state,
    seq: state.seq + 1,
    revisions: { ...state.revisions, [stream]: revision },
  };
}

function applyBrokerObservationEvent(state, event, revision, nowMs) {
  const checked = normalizeBrokerObservation(event.payload?.observation);
  if (!checked.ok) return { state, accepted: false, reason: checked.reason, transitions: [] };
  const observedAt = parseInstant(checked.observation.observedAt);
  if (observedAt > nowMs) {
    return { state, accepted: false, reason: "broker observation is in the future", transitions: [] };
  }
  const authorizedHashes = new Set([
    state.entryClaim?.executionIntentHash,
    state.managementClaim?.managementIntentHash,
  ].filter(Boolean).map(String));
  if (!authorizedHashes.has(String(checked.observation.currentIntentHash))) {
    return { state, accepted: false, reason: "broker observation intent is not authoritative",
      transitions: [] };
  }
  const prior = state.brokerObservation;
  if (prior) {
    const priorAt = parseInstant(prior.observedAt);
    const sameIntent = prior.currentIntentHash === checked.observation.currentIntentHash;
    const sameSymbol = prior.symbol === checked.observation.symbol;
    const sameAccount = prior.accountId === checked.observation.accountId;
    // R43: 口座の載せ替えを許す。
    //
    // accountScope が複数口座のとき観測は口座ごとに別々に届くが、保存スロットは
    // 1つしか無い。先に入った口座で固定すると、解放証明が **名指しで**要求する
    // accountScope[0] の観測を二度と書けなくなる。スロットが空くのは CLAIM 成功時
    // だけで、その CLAIM は解放証明を待っている —— 円環になって新規発注が恒久停止する。
    // 2026-08-26 02:36、口座Bの観測が先着して口座Aの FLAT 証明を締め出し、
    // 実際にこの状態へ落ちた。
    //
    // 載せ替えて安全なのは、intent の権威性が上の authorizedHashes で検証済みで、
    // かつ **その intent 自身が両口座を accountScope に持つ**場合だけ。範囲外の
    // 口座は従来どおり拒否する。
    const scope = sameIntent && sameSymbol && !sameAccount
      ? intentAccountScope(state, checked.observation.currentIntentHash) : null;
    const accountHandoff = Boolean(scope && scope.has(String(prior.accountId))
      && scope.has(String(checked.observation.accountId)));
    // R102c: 限月の載せ替え。ロール直後、旧限月で置いた claim の不在証明は claim が置かれた
    // 限月で観測しなければ entryRecoveryProof(observation.symbol == intent.symbol)を通らない。
    // 先着したのが今の限月の観測だと、同じ intent・同じ口座でも symbol が違うだけで締め出され、
    // 旧 claim が永久に RECOVERY_UNVERIFIED のまま新規発注を全部止めた(2026-09-15 16:31)。
    // 載せ替えて安全なのは、intent の権威性が上で検証済みで、同じ口座で、かつ観測の symbol が
    // **その intent 自身の銘柄**のときだけ。無関係な銘柄は従来どおり拒否する。
    const claimSymbol = sameIntent && sameAccount && !sameSymbol
      ? intentSymbol(state, checked.observation.currentIntentHash) : null;
    const symbolHandoff = Boolean(claimSymbol
      && String(checked.observation.symbol) === String(claimSymbol));
    if (!accountHandoff && !symbolHandoff && !(sameAccount && sameSymbol && sameIntent)) {
      return { state, accepted: false, reason: "broker observation cannot overwrite another scope/intent",
        transitions: [] };
    }
    // 単調性とスナップショット同一性は「同じ口座・同じ銘柄を連続で観測した」ときの性質。
    // 別口座・別限月の時刻列・snapshotId と突き合わせても意味が無いので、載せ替え時は見ない。
    if (!accountHandoff && !symbolHandoff) {
      if (observedAt < priorAt) {
        return { state, accepted: false, reason: "broker observation timestamp is not monotonic",
          transitions: [] };
      }
      if (observedAt === priorAt) {
        if (checked.observation.snapshotHash !== prior.snapshotHash) {
          return { state, accepted: false, reason: "equal-time broker observation differs",
            transitions: [] };
        }
        return { state, accepted: true, reason: "BROKER_OBSERVATION_IDEMPOTENT", transitions: [] };
      }
      if (checked.observation.snapshotId === prior.snapshotId
          && checked.observation.snapshotHash !== prior.snapshotHash) {
        return { state, accepted: false, reason: "broker snapshot id was reused with different bytes",
          transitions: [] };
      }
    }
  }
  const next = bump(state, "broker_observation", revision);
  next.brokerObservation = { ...checked.observation, revision };
  const transitions = [{ kind: "broker_observation",
    from: state.brokerObservation?.observedAt || null,
    to: checked.observation.observedAt }];

  // R112(2026-09-19): **消費できなくなった** claim を、不在の証明が届いたその場で捨てる。
  //
  // CONSUME は claim.maxAgeSec(180 秒)を過ぎると必ず ENTRY_CLAIM_EXPIRED で拒否される
  // (上の applyEntryClaimEvent)。つまり `CLAIMED` かつ試行 0 のまま 180 秒を過ぎた claim は
  // **注文を生む経路が構造的に存在しない**ただの枠の占有である。ところが解放は
  //   * CLAIM 到達時の stale release —— 次の新規 ENTRY が来るまで走らない
  //   * RECOVER —— entryRecoveryProof は snapshot CAS と per-order 終端証明を要求するので、
  //     一度も送っていない claim では原理的に通らず 409 ENTRY_CLAIM_RECOVERY_UNVERIFIED
  // の 2 つしか無い。2026-09-18 22:40 の claim は CONSUME が ENTRY_CLAIM_MARKET_PRICE_STALE で
  // 落ちたあと 2 時間 45 分残り、engine が毎周期 RECOVER を試して台帳に ENTRY_RECOVERED を
  // 168 行積んだ。Mini App の ENGINE は観測の鮮度しだいで RESERVED と STALE SLOT を往復する。
  //
  // 不在の証明はまさにこの観測が運んでくる(建玉 0・未終端注文なし・口座と銘柄が一致・
  // claim が古い)。判定は CLAIM 側とまったく同じ `staleEntryClaimReleasable` を使い、
  // 条件は緩めない。**「消費できない」ことを独立に確かめた claim にだけ**当てるので、
  // 送信済み(CONSUMED)や生きている claim には触れない —— そちらは従来どおり
  // CLAIM 時の stale release と RECOVER が扱う。
  const claim = next.entryClaim;
  const consumeWindowSec = Number(executionContract.claim?.maxAgeSec);
  const claimedAt = parseInstant(claim?.claimedAt);
  const unconsumable = Boolean(claim
    && String(claim.state || "") === "CLAIMED"
    && Number(claim.totalAttempts || 0) === 0
    && Number.isFinite(consumeWindowSec) && consumeWindowSec > 0
    && claimedAt !== null && nowMs - claimedAt > consumeWindowSec * 1000);
  if (unconsumable && staleEntryClaimReleasable(next, claim, nowMs)) {
    const entryKey = String(claim.entryKey || "");
    // 解放は必ず記録に残す。黙って解くと「なぜ枠が空いたのか」が後から追えない。
    next.revisions = { ...next.revisions, entry_claim: revision };
    next.entryClaim = null;
    next.entryRelease = null;
    next.entryStaleRelease = {
      entryKey, from: "CLAIMED", releasedAt: new Date(nowMs).toISOString(), revision,
      reason: "UNCONSUMABLE_CLAIM_BROKER_EMPTY",
    };
    transitions.push({ kind: "entry_claim", from: "CLAIMED", to: "STALE_RELEASED", entryKey });
    return {
      state: next, accepted: true,
      reason: `ENTRY_CLAIM_STALE_RELEASED: ${entryKey} (CLAIMED, unconsumable + broker empty)`,
      transitions,
    };
  }
  return { state: next, accepted: true, reason: null, transitions };
}

function applyScenarioEvent(state, event, revision, nowMs) {
  const payload = event.payload || {};
  const transitions = [];

  // scenario stream は position を絶対に触らない。
  // 新しいシナリオが建玉を書き換える経路を構造的に塞ぐ。
  if ("position" in payload) {
    return { state, accepted: false, reason: "scenario events must not carry position data", transitions: [] };
  }

  if (payload.scenario === null) {
    // 明示的なクリア。既存があれば理由付きで終端に落とす。
    let next = bump(state, "scenario", revision);
    if (state.scenario) {
      const closing = String(payload.state || "CANCELED").toUpperCase();
      const finalState = TERMINAL_SCENARIO_STATES.has(closing) ? closing : "CANCELED";
      transitions.push({ kind: "scenario", from: state.scenario.state, to: finalState, scenarioId: state.scenario.scenarioId });
    }
    next.scenario = null;
    return { state: next, accepted: true, reason: null, transitions };
  }

  const validated = validateScenario(payload.scenario, { symbol: state.symbol });
  if (!validated.ok) return { state, accepted: false, reason: validated.reason, transitions: [] };
  const incoming = validated.scenario;

  // 終端状態で届いたものは表示しない。同一 scenario なら記録だけ残して外す。
  if (TERMINAL_SCENARIO_STATES.has(incoming.state)) {
    const next = bump(state, "scenario", revision);
    if (state.scenario && state.scenario.scenarioId === incoming.scenarioId) {
      transitions.push({ kind: "scenario", from: state.scenario.state, to: incoming.state, scenarioId: incoming.scenarioId });
      next.scenario = null;
    } else {
      transitions.push({ kind: "scenario", from: null, to: incoming.state, scenarioId: incoming.scenarioId });
      next.scenario = state.scenario;
    }
    return { state: next, accepted: true, reason: null, transitions };
  }

  // 到着時点で既に期限切れのものは採用しない(古い通知の再生を防ぐ)。
  if (Date.parse(incoming.expiresAt) <= nowMs) {
    return { state, accepted: false, reason: "scenario is already expired at ingest", transitions: [] };
  }

  const next = bump(state, "scenario", revision);
  if (state.scenario && state.scenario.scenarioId !== incoming.scenarioId) {
    // 置き換え。旧シナリオは REPLACED として記録してから外す。
    transitions.push({
      kind: "scenario", from: state.scenario.state, to: "REPLACED",
      scenarioId: state.scenario.scenarioId, replacedBy: incoming.scenarioId,
    });
  }
  transitions.push({
    kind: "scenario",
    from: state.scenario && state.scenario.scenarioId === incoming.scenarioId ? state.scenario.state : null,
    to: incoming.state,
    scenarioId: incoming.scenarioId,
  });
  next.scenario = { ...incoming, cycleCommitted: false, revision };
  return { state: next, accepted: true, reason: null, transitions };
}

function applyPositionEvent(state, event, revision, nowMs) {
  const payload = event.payload || {};
  const transitions = [];
  const previous = state.position;

  const validated = validatePosition(payload.position === undefined ? null : payload.position, { symbol: state.symbol });
  if (!validated.ok) return { state, accepted: false, reason: validated.reason, transitions: [] };

  const next = bump(state, "position", revision);
  const incoming = validated.position;

  // --- 照会できなかった: 消さずに STALE として残す ---
  if (!incoming || !incoming.verified) {
    next.positionCheck = {
      verified: false,
      source: incoming ? incoming.source : (payload.source ? String(payload.source) : "unavailable"),
      observedAt: incoming?.observedAt || new Date(nowMs).toISOString(),
    };
    if (!previous) {
      // 元から建玉が無い。未確認情報だけで建玉を作らない。
      next.position = null;
      return { state: next, accepted: true, reason: null, transitions };
    }
    next.position = {
      ...previous,
      state: STALE_POSITION_STATE,
      verified: false,
      staleSince: previous.state === STALE_POSITION_STATE ? previous.staleSince : new Date(nowMs).toISOString(),
      lastKnownState: previous.state === STALE_POSITION_STATE ? previous.lastKnownState : previous.state,
      unverifiedSource: incoming ? incoming.source : (payload.source ? String(payload.source) : "unavailable"),
      revision,
    };
    if (previous.state !== STALE_POSITION_STATE) {
      transitions.push({ kind: "position", from: previous.state, to: STALE_POSITION_STATE });
    }
    return { state: next, accepted: true, reason: null, transitions };
  }

  // --- broker が qty=0 を返した: ここで初めて CLOSED にできる ---
  next.positionCheck = {
    verified: true,
    source: incoming.source,
    observedAt: incoming.observedAt,
  };
  if (incoming.qty === 0) {
    if (!previous) {
      next.position = null;
      next.lastVerifiedAt = incoming.observedAt;
      return { state: next, accepted: true, reason: null, transitions };
    }
    if (previous.state === "CLOSED") {
      // すでに決済済み。**同じ決済を再遷移させない。**
      //
      // ここだけ STALE 分岐・建玉あり分岐のような「状態が変わったときだけ
      // transition を積む」ガードが抜けていた。telegram_bot は 60 秒ごとに
      // sync_position() を呼び、to=="CLOSED" の transition が返るたびに
      // POSITION CLOSED カードを送る。決済価格がブローカーから取れない建玉は
      // publish_trade_result が失敗して closedPosition が残りつづけるので、
      // 通知が **60秒ごとに永久に鳴りつづけていた**(2026-09-01 に実測)。
      //
      // closedAt / closedQty も上書きしない。previous.qty はこの時点で既に 0 で、
      // closedQty: previous.qty を通すと記録済みの決済枚数が 0 に潰れる。
      next.position = {
        ...previous,
        verified: true,
        source: incoming.source,
        observedAt: incoming.observedAt,
        revision,
      };
      next.lastVerifiedAt = incoming.observedAt;
      return { state: next, accepted: true, reason: null, transitions };
    }
    if (!payload.closedAt) {
      // 決済時刻を確認できないうちは閉じない(フェイルセーフ)。
      return { state, accepted: false, reason: "FLAT requires closedAt before CLOSED", transitions: [] };
    }
    transitions.push({ kind: "position", from: previous.state, to: "CLOSED" });
    next.position = {
      ...previous,
      state: "CLOSED",
      verified: true,
      qty: 0,
      closedAt: String(payload.closedAt),
      closedQty: previous.qty,
      receipt: incoming.receipt || previous.receipt,
      source: incoming.source,
      observedAt: incoming.observedAt,
      revision,
    };
    next.lastVerifiedAt = incoming.observedAt;
    return { state: next, accepted: true, reason: null, transitions };
  }

  // --- 建玉あり ---
  // R73: 引き継ぎは「同じトレードが続いている」ときだけ。直前が CLOSED(qty 0)なら別の
  // トレードなので initialQty / filledAt / SL / TP を持ち越さない(2026-09-09 実測: 新規
  // SHORT 6 枚に前トレードの initialQty 4 が残り、qty 6 > initialQty 4 の矛盾したカード
  // になった)。positionGeneration が双方にあって違う場合も別トレード。同じトレードで
  // 枚数が増えた(指値の分割約定・手動の追加)ときは initialQty を大きい側へ更新する。
  const continuing = Boolean(previous) && previous.state !== "CLOSED" && Number(previous.qty) > 0
    && (!previous.positionGeneration || !incoming.positionGeneration
      || String(previous.positionGeneration) === String(incoming.positionGeneration));
  const carriedInitial = continuing
    ? (previous.initialQty ?? incoming.initialQty ?? incoming.qty)
    : (incoming.initialQty ?? incoming.qty);
  const initialQty = Math.max(Number(carriedInitial), Number(incoming.qty));
  let positionState = "OPEN";
  if (incoming.exitPending) positionState = "EXIT_PENDING";
  else if (incoming.qty < initialQty) positionState = "PARTIAL";

  if (!previous || previous.state !== positionState) {
    transitions.push({ kind: "position", from: previous ? previous.state : null, to: positionState });
  }

  next.position = {
    ...incoming,
    state: positionState,
    initialQty,
    filledAt: (continuing ? previous.filledAt : null) || incoming.filledAt || incoming.observedAt,
    // SL/TP は broker が返さないことがある。同じトレードが続く間は直前の確認値を保つ。
    stop: incoming.stop ?? (continuing ? previous.stop : null) ?? null,
    target: incoming.target ?? (continuing ? previous.target : null) ?? null,
    staleSince: null,
    lastKnownState: null,
    revision,
  };
  next.lastVerifiedAt = incoming.observedAt;
  return { state: next, accepted: true, reason: null, transitions };
}

function applyOrderEvent(state, event, revision, nowMs) {
  const payload = event.payload || {};
  const validated = validateOrder(payload.order === undefined ? null : payload.order);
  if (!validated.ok) return { state, accepted: false, reason: validated.reason, transitions: [] };

  const next = bump(state, "order", revision);
  const transitions = [];
  const previous = state.order;
  if (!previous || previous.state !== validated.order?.state) {
    transitions.push({
      kind: "order",
      from: previous ? previous.state : null,
      to: validated.order ? validated.order.state : null,
    });
  }
  next.order = validated.order ? { ...validated.order, revision } : null;

  // 注文の成否は position を作らない。position は broker 照会だけが正本。
  return { state: next, accepted: true, reason: null, transitions };
}

function applyResultEvent(state, event, revision, nowMs) {
  const payload = event.payload || {};
  const transitions = [];

  // result stream は建玉もシナリオも触らない。過去の結果が現在の状態を
  // 書き換える経路を作らない。
  for (const foreign of ["position", "scenario", "order"]) {
    if (foreign in payload) {
      return { state, accepted: false, reason: `result events must not carry ${foreign} data`, transitions: [] };
    }
  }

  if (payload.result === null) {
    const next = bump(state, "result", revision);
    if (state.result) transitions.push({ kind: "result", from: state.result.resultId, to: null });
    next.result = null;
    return { state: next, accepted: true, reason: null, transitions };
  }

  const validated = validateResult(payload.result, { symbol: state.symbol });
  if (!validated.ok) return { state, accepted: false, reason: validated.reason, transitions: [] };

  const next = bump(state, "result", revision);
  transitions.push({
    kind: "result",
    from: state.result ? state.result.resultId : null,
    to: validated.result.resultId,
  });
  const stored = { ...validated.result, revision, publishedAt: new Date(nowMs).toISOString() };
  next.result = stored;
  // 履歴に追記する。同じ resultId の再送(リトライ・訂正)は置き換え、
  // 上限を超えた最古の記録から落とす。旧 state に resultLog が無くても動く。
  const log = Array.isArray(state.resultLog) ? state.resultLog : [];
  next.resultLog = [...log.filter((item) => item.resultId !== stored.resultId), stored]
    .slice(-RESULT_LOG_LIMIT);
  return { state: next, accepted: true, reason: null, transitions };
}

function applyAccountEvent(state, event, revision, nowMs) {
  const payload = event.payload || {};
  const transitions = [];

  // account stream は建玉・シナリオ・注文・結果を触らない。
  for (const foreign of ["position", "scenario", "order", "result"]) {
    if (foreign in payload) {
      return { state, accepted: false, reason: `account events must not carry ${foreign} data`, transitions: [] };
    }
  }

  if (payload.accounts === null) {
    const next = bump(state, "account", revision);
    next.accounts = null;
    return { state: next, accepted: true, reason: null, transitions };
  }

  const validated = validateAccounts(payload.accounts);
  if (!validated.ok) return { state, accepted: false, reason: validated.reason, transitions: [] };

  const next = bump(state, "account", revision);
  next.accounts = { ...validated.accounts, revision, publishedAt: new Date(nowMs).toISOString() };

  // R110(2026-09-18): 名簿が「claim の scope はブローカーに居ない」と証明した周期で、
  // その場で古い claim を捨てる。
  //
  // R69 の stale release は **CLAIM 到達時にしか走らない**。口座が入れ替わると旧口座の
  // claim は「次の新規 ENTRY」まで残り、Mini App の SYSTEM タブは STALE SLOT (WARN) を
  // 出し続ける。「it clears on the next entry」と書いてあるのに、新規が来るまで何時間でも
  // 居座る(2026-09-18 実測: 09-17 06:13 の claim が口座入替後も翌日まで残った)。
  // 消えた口座を指す claim は不在を証明済みのゴミなので、**証拠が届いたその場で**捨てる。
  //
  // 判定は CLAIM 側と同じ predicate を使い、条件は緩めない。さらに解放理由を
  // 「scope が消えた」に限定する —— staleEntryClaimReleasable は新鮮な観測でも true を
  // 返すが、そちらは CLAIM 側で扱う話で、名簿が運んでくる証拠ではない。
  const claim = next.entryClaim;
  if (claim && ENTRY_CLAIM_ACTIVE.has(String(claim.state || ""))
      && claimScopeVanishedFromBroker(next, claim, nowMs)
      && staleEntryClaimReleasable(next, claim, nowMs)) {
    const from = String(claim.state);
    const entryKey = String(claim.entryKey || "");
    // 解放は必ず記録に残す。黙って解くと「なぜ消えたのか」が後から追えない。
    next.revisions = { ...next.revisions, entry_claim: revision };
    next.entryClaim = null;
    next.entryRelease = null;
    next.entryStaleRelease = {
      entryKey, from, releasedAt: new Date(nowMs).toISOString(), revision,
      reason: "SCOPE_VANISHED_FROM_BROKER",
    };
    transitions.push({ kind: "entry_claim", from, to: "STALE_RELEASED", entryKey });
    return {
      state: next, accepted: true,
      reason: `ENTRY_CLAIM_STALE_RELEASED: ${entryKey} (${from}, scope vanished from broker)`,
      transitions,
    };
  }
  return { state: next, accepted: true, reason: null, transitions };
}

/**
 * Commit market + executable scenario as one Durable Object state change.
 * Validation happens before either side is installed, so a rejected scenario
 * cannot leave a fresh market paired with an old armed setup (or vice versa).
 */
function tombstoneCycle(state, revision, nowMs, cycleId, reason, market = null) {
  // A newer authenticated cycle which cannot be sealed must revoke the older
  // armed pair in the same state transition.  Keeping the verified market is
  // useful for display, but it is explicitly display-only.
  const next = bump(state, "cycle", revision);
  next.revisions = { ...next.revisions, market: revision, scenario: revision };
  next.market = market ? {
    ...market,
    cycleCommitted: false,
    revision,
    publishedAt: new Date(nowMs).toISOString(),
  } : null;
  next.scenario = null;
  next.cycleLeaseExpiresAt = null;
  next.rollbackCycleId = null;
  return {
    state: next,
    accepted: true,
    reason: `CYCLE_TOMBSTONED: ${reason}`,
    transitions: [
      { kind: "market", from: state.market?.cycleId || null, to: next.market?.cycleId || null, cycleId },
      { kind: "scenario", from: state.scenario?.state || null, to: "CANCELED",
        scenarioId: state.scenario?.scenarioId || null },
    ],
  };
}

function applyCycleEvent(state, event, revision, nowMs) {
  const payload = event.payload || {};
  const cycleId = String(payload.cycleId || "").trim();
  if (!cycleId || cycleId.length > 96) {
    return tombstoneCycle(state, revision, nowMs, null, "cycleId is required");
  }
  // A producer that cannot verify the market must be able to revoke the
  // previous pair in one write.  Do not fall back to a separate scenario
  // clear: that would briefly leave a new/old market-scenario combination.
  if (payload.market === null) {
    if (payload.scenario !== null && payload.scenario !== undefined) {
      return tombstoneCycle(state, revision, nowMs, cycleId, "market-null cycle must disarm scenario");
    }
    const next = bump(state, "cycle", revision);
    next.revisions = { ...next.revisions, market: revision, scenario: revision };
    next.market = null;
    next.scenario = null;
    next.cycleLeaseExpiresAt = null;
    next.rollbackCycleId = null;
    return {
      state: next,
      accepted: true,
      reason: null,
      transitions: [
        { kind: "market", from: state.market?.cycleId || null, to: null, cycleId },
        { kind: "scenario", from: state.scenario?.state || null, to: "CANCELED",
          scenarioId: state.scenario?.scenarioId || null },
      ],
    };
  }
  const marketChecked = validateMarket(payload.market, nowMs);
  if (!marketChecked.ok) {
    return tombstoneCycle(state, revision, nowMs, cycleId, `market invalid: ${marketChecked.reason}`);
  }
  if (marketChecked.market.cycleId !== cycleId) {
    return tombstoneCycle(state, revision, nowMs, cycleId,
      "market.cycleId must match cycleId", marketChecked.market);
  }
  if (marketChecked.market.strategyEvidenceError) {
    return tombstoneCycle(state, revision, nowMs, cycleId,
      `market strategyEvidence invalid: ${marketChecked.market.strategyEvidenceError}`, marketChecked.market);
  }
  let scenario = null;
  if (payload.scenario !== null && payload.scenario !== undefined) {
    const scenarioChecked = validateScenario(payload.scenario, {
      symbol: state.symbol,
      pairedEvidence: marketChecked.market.strategyEvidence,
      requireHashOnly: true,
    });
    if (!scenarioChecked.ok) {
      return tombstoneCycle(state, revision, nowMs, cycleId,
        `scenario invalid: ${scenarioChecked.reason}`, marketChecked.market);
    }
    scenario = scenarioChecked.scenario;
    if (scenario.marketCycleId !== cycleId) {
      return tombstoneCycle(state, revision, nowMs, cycleId,
        "scenario.marketCycleId must match cycleId", marketChecked.market);
    }
    const marketEvidenceHash = marketChecked.market.strategyEvidence?.evidenceHash || null;
    if (!marketEvidenceHash || scenario.evidenceHash !== marketEvidenceHash) {
      return tombstoneCycle(state, revision, nowMs, cycleId,
        "scenario evidenceHash must match paired market strategyEvidence", marketChecked.market);
    }
    if (Date.parse(scenario.expiresAt) <= nowMs) {
      return tombstoneCycle(state, revision, nowMs, cycleId,
        "scenario is already expired at ingest", marketChecked.market);
    }
  }
  const next = bump(state, "cycle", revision);
  next.revisions = { ...next.revisions, market: revision, scenario: revision };
  next.market = { ...marketChecked.market, cycleCommitted: true, revision, publishedAt: new Date(nowMs).toISOString() };
  next.scenario = scenario ? { ...scenario, cycleCommitted: true, revision } : null;
  const requestedLease = parseInstant(payload.leaseExpiresAt);
  const boundedLease = requestedLease === null
    ? nowMs + CYCLE_LEASE_MAX_MS
    : Math.min(requestedLease, nowMs + CYCLE_LEASE_MAX_MS);
  if (boundedLease <= nowMs) {
    return tombstoneCycle(state, revision, nowMs, cycleId, "cycle lease is already expired", marketChecked.market);
  }
  next.cycleLeaseExpiresAt = new Date(boundedLease).toISOString();
  next.rollbackCycleId = null;
  const transitions = [
    { kind: "market", from: state.market?.cycleId || null, to: cycleId },
    { kind: "scenario", from: state.scenario?.state || null, to: scenario?.state || "CANCELED",
      scenarioId: scenario?.scenarioId || state.scenario?.scenarioId || null },
  ];
  return { state: next, accepted: true, reason: null, transitions };
}

function applyMarketEvent(state, event, revision, nowMs) {
  const payload = event.payload || {};
  const market = payload.market;
  if (market !== null && (!market || typeof market !== "object")) {
    return { state, accepted: false, reason: "market is not an object", transitions: [] };
  }
  const next = bump(state, "market", revision);
  if (market === null) {
    next.market = null;
    return { state: next, accepted: true, reason: null, transitions: [] };
  }
  const validated = validateMarket(market, nowMs);
  if (!validated.ok) return { state, accepted: false, reason: validated.reason, transitions: [] };
  const previousAt = parseInstant(state.market?.observedAt || state.market?.at);
  const incomingAt = parseInstant(validated.market.observedAt);
  if (previousAt !== null && incomingAt < previousAt) {
    return { state, accepted: false, reason: "market observation is older than current state", transitions: [] };
  }
  next.market = {
    ...validated.market,
    cycleCommitted: false,
    revision,
    publishedAt: new Date(nowMs).toISOString(),
  };
  return { state: next, accepted: true, reason: null, transitions: [] };
}

// ---------------------------------------------------------------- 期限の掃除

/**
 * サーバー時刻で期限切れシナリオと AUTO 武装を落とす。GET/WS 送出の直前に
 * 必ず通す。Bot が停止していて transition イベントが来なくても、これで
 * 確実に失効する。
 */
/**
 * R102d(2026-09-15): 限月ロール。DO の state.symbol は作成時に固定されるが、Worker の NQX_SYMBOL を
 * 切り替えて deploy しても文書側は旧限月のままで、新限月の scenario / position / result が
 * 「symbol does not match」で全部拒否され、ロール後の初回 ARMED で HALT になった(16:44)。
 * **安全なときだけ**載せ替える: 建玉なし、ENTRY claim が CLAIMED/CONSUMED でない、MANAGEMENT claim が
 * CLAIMED/CONSUMED でない、blocking な注文が無い。旧限月の scenario / market / brokerObservation /
 * position / cycle は捨てる(新限月で取り直す。position は null = 照会未確認 = fail-closed)。
 * 終端した claim と tombstone は残す(同じ tuple を二度送らせない)。
 */
export function adoptSymbol(state, symbol, nowMs) {
  const target = String(symbol || "");
  if (!state || typeof state !== "object" || !target || String(state.symbol || "") === target) {
    return { state, adopted: false, reason: null };
  }
  const claimState = String(state.entryClaim?.state || "");
  if (state.entryClaim && (claimState === "CLAIMED" || claimState === "CONSUMED")) {
    return { state, adopted: false, reason: `entry claim ${claimState}` };
  }
  const mgmtState = String(state.managementClaim?.state || "");
  if (state.managementClaim && (mgmtState === "CLAIMED" || mgmtState === "CONSUMED")) {
    return { state, adopted: false, reason: `management claim ${mgmtState}` };
  }
  const qty = Number(state.position?.qty || 0);
  if (qty > 0) return { state, adopted: false, reason: `position qty ${qty}` };
  const orderState = String(state.order?.state || "").toUpperCase();
  if (state.order && BLOCKING_ORDER_STATES.has(orderState)) {
    return { state, adopted: false, reason: `order ${orderState}` };
  }
  const next = { ...state, symbol: target, scenario: null, market: null, brokerObservation: null,
    order: null, position: null, positionCheck: null, cycleLeaseExpiresAt: null, rollbackCycleId: null,
    symbolRoll: { from: state.symbol || null, to: target, at: new Date(nowMs).toISOString() } };
  return { state: next, adopted: true, reason: null };
}

export function sweepExpired(state, nowMs) {
  let next = state;
  const transitions = [];
  if (state.scenario && Date.parse(state.scenario.expiresAt) <= nowMs) {
    next = { ...next, scenario: null };
    transitions.push({
      kind: "scenario", from: state.scenario.state, to: "EXPIRED",
      scenarioId: state.scenario.scenarioId,
    });
  }
  const arm = state.autotradeArm;
  if (arm?.enabled === true && Date.parse(arm.expiresAt) <= nowMs) {
    next = {
      ...next,
      autotradeArm: {
        ...arm,
        enabled: false,
        autotrade: false,
        live: false,
        status: "EXPIRED",
        expiredAt: new Date(nowMs).toISOString(),
      },
    };
    transitions.push({ kind: "autotrade", from: "LIVE", to: "EXPIRED", armId: arm.armId || null });
  }
  return transitions.length
    ? { state: { ...next, seq: state.seq + 1 }, transitions }
    : { state, transitions: [] };
}

// ---------------------------------------------------------------- 表示への投影

export const DISPLAY_PRIORITY = [
  "POSITION_OPEN",     // OPEN / PARTIAL / STALE
  "POSITION_EXIT",     // EXIT PENDING
  "ORDER_PENDING",
  "SCENARIO_ACTIVE",
  "SCENARIO_NONE",
];

/**
 * 画面が必要とする形に落とす。ここが「何を出してよいか」の唯一の判断点。
 * 期限切れは呼び出し側で sweepExpired 済みであることを前提にせず、ここでも再判定する。
 */
export function projectState(state, nowMs) {
  const swept = sweepExpired(state, nowMs).state;
  const position = swept.position;
  const positionCheck = swept.positionCheck;
  const order = swept.order;
  const scenario = swept.scenario;

  const positionLive = position && (
    OPEN_POSITION_STATES.has(position.state) || position.state === STALE_POSITION_STATE
  );
  const orderLive = order && BLOCKING_ORDER_STATES.has(order.state);

  let priority = "SCENARIO_NONE";
  if (positionLive && position.state === "EXIT_PENDING") priority = "POSITION_EXIT";
  else if (positionLive) priority = "POSITION_OPEN";
  else if (orderLive) priority = "ORDER_PENDING";
  else if (scenario) priority = "SCENARIO_ACTIVE";

  // 発注可否。ここで false になるものは Mini App がボタンを出さない。
  // Bot 側でも同じ判定を独立に行うので、ここが唯一のゲートではない。
  let orderable = false;
  let blockReason = null;
  // R15+ permits arming only through an atomic cycle commit.  Legacy/direct
  // stream events remain available for display and clear operations, but can
  // never become an execution pair.
  // A durable pair is not a seal merely because the ids look equal.  Re-run
  // the canonical evidence check on every projection so a corrupted storage
  // value, a partial write, or a one-bit hash change cannot revive an order.
  const marketEvidence = normalizeStrategyEvidence(swept.market?.strategyEvidence);
  const scenarioCarriesEvidence = scenario
    && Object.prototype.hasOwnProperty.call(scenario, "strategyEvidence");
  const evidencePaired = Boolean(
    marketEvidence.ok
    && marketEvidence.evidence
    && !swept.market?.strategyEvidenceError
    && scenario?.evidenceHash
    && String(scenario.evidenceHash) === String(marketEvidence.evidence.evidenceHash)
    && !scenarioCarriesEvidence
  );
  const cycleLeaseExpiryMs = parseInstant(swept.cycleLeaseExpiresAt);
  const cycleLeaseActive = cycleLeaseExpiryMs !== null && nowMs < cycleLeaseExpiryMs;
  const cycleSealStructurallyPaired = Boolean(
    scenario?.cycleCommitted === true
    && swept.market?.cycleCommitted === true
    && scenario?.marketCycleId
    && swept.market?.cycleId
    && scenario.marketCycleId === swept.market.cycleId
    && evidencePaired
  );
  const cyclePaired = cycleSealStructurallyPaired && cycleLeaseActive;
  if (!scenario) {
    blockReason = "NO ACTIVE SCENARIO";
  } else if (positionLive) {
    blockReason = "POSITION OPEN — MANAGEMENT ONLY";
  } else if (orderLive) {
    blockReason = "ORDER PENDING — AWAITING BROKER";
  } else if (!["ACTIVE", "ARMED"].includes(scenario.state)) {
    blockReason = `SCENARIO ${scenario.state} — NOT ARMED`;
  } else if (!cyclePaired) {
    blockReason = cycleSealStructurallyPaired && !cycleLeaseActive
      ? "CYCLE_LEASE_EXPIRED - HEARTBEAT REQUIRED"
      : "CYCLE_MISMATCH - MARKET/SCENARIO SEAL NOT ARMED";
  } else if (!positionCheck || positionCheck.verified !== true) {
    // position=null は FLAT の証明ではない。broker の verified=true が
    // 到着するまで Mini App の発注ボタンを出さない。
    blockReason = "POSITION UNKNOWN — BROKER VERIFICATION REQUIRED";
  } else {
    orderable = true;
  }

  const execution = evaluateExecutionContract(scenario, swept.market, position, order, nowMs);
  if (orderable && !execution.orderable) {
    orderable = false;
    blockReason = `EXECUTION CONTRACT: ${execution.blockers.join(", ")}`;
  }

  const marketAgeMs = parseInstant(swept.market?.observedAt || swept.market?.at);
  const marketStale = marketAgeMs === null || nowMs - marketAgeMs > MARKET_MAX_AGE_MS;
  const market = swept.market ? {
    ...swept.market,
    ageMs: marketAgeMs === null ? null : Math.max(0, nowMs - marketAgeMs),
    stale: marketStale,
    verified: swept.market.verified === true && !marketStale,
    // Old prices may remain useful as a faded historical chart, but they are
    // never projected as the current price/indicator set.
    price: marketStale ? null : swept.market.price,
    vwap: marketStale ? null : swept.market.vwap,
    cvd: marketStale ? null : swept.market.cvd,
  } : null;

  return {
    version: STATE_VERSION,
    accountId: swept.accountId,
    symbol: swept.symbol,
    seq: swept.seq,
    serverTime: new Date(nowMs).toISOString(),
    revisions: swept.revisions,
    scenario,
    position: position && position.state === "CLOSED" ? null : position,
    positionCheck,
    closedPosition: position && position.state === "CLOSED" ? position : null,
    order,
    entryClaim: swept.entryClaim ? {
      entryKey: swept.entryClaim.entryKey,
      tuple: swept.entryClaim.tuple,
      state: swept.entryClaim.state,
      executionIntent: swept.entryClaim.executionIntent,
      executionIntentHash: swept.entryClaim.executionIntentHash,
      routeState: swept.entryClaim.routeState,
      acceptedCount: swept.entryClaim.acceptedCount,
      totalAttempts: swept.entryClaim.totalAttempts,
      routeSnapshot: swept.entryClaim.routeSnapshot || [],
      // R84: 追撃 claim の印。storage にしか無いフィールドはエンジンから見えないので
      // (entryStaleRelease が実際そうなっている)、明示的に投影へ載せる。
      pyramid: swept.entryClaim.pyramid || null,
      claimedAt: swept.entryClaim.claimedAt,
      consumedAt: swept.entryClaim.consumedAt || null,
      resolvedAt: swept.entryClaim.resolvedAt || null,
      recoveredAt: swept.entryClaim.recoveredAt || null,
      // R41: この claim を次の CLAIM 到達時に解放してよいか(R36 と同じ述語)。
      //
      // 解放判定は CLAIM が来たときにしか走らない。A/A+ が出ない日は CLAIM 自体が
      // 来ないので、routeState=UNKNOWN で宙吊りになった claim が何時間も残り、
      // 画面だけが「ENTRY claim held」と言い続ける(2026-08-25 に実際に発生)。
      // 表示層に同じ判定を書き直させると二重実装になるので、権威側の答えを出す。
      staleReleasable: staleEntryClaimReleasable(swept, swept.entryClaim, nowMs),
    } : null,
    entryTombstones: (Array.isArray(swept.entryTombstones) ? swept.entryTombstones : []).map((row) => ({
      entryKey: row.entryKey, tuple: row.tuple, recoveredAt: row.recoveredAt,
      brokerSnapshotHash: row.brokerSnapshotHash,
    })),
    managementClaim: swept.managementClaim ? {
      managementKey: swept.managementClaim.managementKey,
      state: swept.managementClaim.state,
      managementIntent: swept.managementClaim.managementIntent,
      managementIntentHash: swept.managementClaim.managementIntentHash,
      routeState: swept.managementClaim.routeState,
      routeReceipt: swept.managementClaim.routeReceipt || null,
      claimedAt: swept.managementClaim.claimedAt,
      consumedAt: swept.managementClaim.consumedAt || null,
      resolvedAt: swept.managementClaim.resolvedAt || null,
    } : null,
    brokerObservation: swept.brokerObservation ? {
      observedAt: swept.brokerObservation.observedAt,
      positionObservedAt: swept.brokerObservation.positionObservedAt,
      ordersObservedAt: swept.brokerObservation.ordersObservedAt,
      snapshotId: swept.brokerObservation.snapshotId,
      cursor: swept.brokerObservation.cursor,
      snapshotMode: swept.brokerObservation.snapshotMode,
      snapshotHash: swept.brokerObservation.snapshotHash,
      platform: swept.brokerObservation.platform,
      accountId: swept.brokerObservation.accountId,
      symbol: swept.brokerObservation.symbol,
      currentIntentHash: swept.brokerObservation.currentIntentHash,
      position: swept.brokerObservation.position,
      orders: swept.brokerObservation.orders,
    } : null,
    market,
    cycleLeaseExpiresAt: swept.cycleLeaseExpiresAt || null,
    executionContract: execution,
    // 結果は履歴であって現在の状態ではない。display.priority には影響させない。
    result: swept.result,
    // LEDGER 画面用の決済履歴(新しい順)。旧 state に無くても空配列を返す。
    recentResults: (Array.isArray(swept.resultLog) ? swept.resultLog : []).slice().reverse(),
    // 口座別の残機(LIFELINE)。市場データと違い鮮度で消さない — 残機は
    // 時間で腐らず、observedAt を表示して古さはユーザーに見せる。
    accounts: swept.accounts || null,
    // 新規ENTRYの権限だけを表す。秘密値は含めず、期限・口座・銘柄を
    // ローカル監視機が毎サイクル再照合できる形で返す。
    autotradeArm: swept.autotradeArm ? {
      schemaVersion: swept.autotradeArm.schemaVersion,
      armId: swept.autotradeArm.armId || null,
      enabled: swept.autotradeArm.enabled === true,
      autotrade: swept.autotradeArm.autotrade === true,
      live: swept.autotradeArm.live === true,
      status: swept.autotradeArm.status || (swept.autotradeArm.enabled ? "LIVE" : "OFF"),
      source: swept.autotradeArm.source || null,
      armedAt: swept.autotradeArm.armedAt || null,
      expiresAt: swept.autotradeArm.expiresAt || null,
      disabledAt: swept.autotradeArm.disabledAt || null,
      expiredAt: swept.autotradeArm.expiredAt || null,
      accountScope: Array.isArray(swept.autotradeArm.accountScope)
        ? [...swept.autotradeArm.accountScope] : [],
      symbol: swept.autotradeArm.symbol || null,
    } : null,
    // 監視ループのビーコン。BLOCKED/HALT の理由と KILL 状態を表示層へ届ける。
    // 旧 state に無ければ null(アプリ側は沈黙時間だけで表示する)。
    cycleHealth: swept.cycleHealth ? {
      status: swept.cycleHealth.status,
      at: swept.cycleHealth.at,
      reason: swept.cycleHealth.reason || null,
      kill: swept.cycleHealth.kill === true,
      publishedAt: swept.cycleHealth.publishedAt || null,
    } : null,
    // 口座別ユーザー設定(ULTRA 対象・利益目標・DD上限)。認証済み Mini App が
    // 書き、監視PCが毎サイクル読む。表示と PC 側再計算の入力であって、
    // Worker 上の実行契約判定には使わない。
    accountPrefs: swept.accountPrefs ? {
      updatedAt: swept.accountPrefs.updatedAt || null,
      map: swept.accountPrefs.map || {},
    } : null,
    // R87: 手動HALT。監視PC(autotrade_arm.sync_manual_halt)と設定ページが読む。
    // 押した人の ID は返さない(autotradeArm と同じく秘密に準ずる値は出さない)。
    manualHalt: swept.manualHalt ? {
      schemaVersion: swept.manualHalt.schemaVersion || MANUAL_HALT_SCHEMA,
      enabled: swept.manualHalt.enabled === true,
      status: swept.manualHalt.enabled === true ? "HALTED" : "RELEASED",
      updatedAt: swept.manualHalt.updatedAt || null,
      engagedAt: swept.manualHalt.engagedAt || null,
      releasedAt: swept.manualHalt.releasedAt || null,
      source: swept.manualHalt.source || null,
      reason: swept.manualHalt.reason || null,
    } : null,
    lastVerifiedAt: swept.lastVerifiedAt,
    display: { priority, orderable, blockReason,
               executionContractVersion: execution.version,
               effectiveGrade: execution.effectiveGrade,
               cyclePaired,
               cycleLeaseActive,
               cycleSealReason: cyclePaired ? null
                 : (cycleSealStructurallyPaired && !cycleLeaseActive ? "CYCLE_LEASE_EXPIRED" : "CYCLE_MISMATCH") },
  };
}
