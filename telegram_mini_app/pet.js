/**
 * PET DEN — 夜警の目のペット化(R65, 2026-09-07)。
 *
 * 待機紋章の目(eye3d.js)は NO SCENARIO のカードにしか出ない。カードが別の物(建玉・注文・
 * シナリオ・GATES)へ変わると目ごと消える。広い画面では左下がいつも空いているので、
 * **目が居場所を失っている間だけ**そこへ移して駐在させ、状況に応じた一言を吹き出しで
 * つぶやかせる(2026-09-07 ユーザー依頼「ペットとして駐在」「シックな一言」「一喜一憂」)。
 *
 * このファイルは台詞と気分だけを持つ。目そのもの(WebGL)は eye3d.js、置き場所は app.js。
 *
 * 規律(sound.js と同じ):
 *   - 判定も発注もしない。届いた view を読むだけで、状態・注文・保存には触れない。
 *   - 出来事は **前後の view の差** からだけ拾う。起動直後の最初の view は基準として
 *     飲み込む(アプリを開いた瞬間に前夜の決済を喜ばない)。
 *   - 同じ場面では同じ台詞(seed からの決定論)。3秒ごとの再描画でパタパタしない。
 *     決済の台詞は resultId から引く —— nqx_state.py の verdict と同じ作法。
 *   - 損益の導出は ledger.js の deriveTrade を使う。式を二重に持たない。
 */

import { deriveTrade } from "./ledger.js";

/** 反応を出し続ける長さ。決済の一喜一憂は長く残す。 */
export const HOLD_MS = Object.freeze({ result: 40_000, event: 16_000 });
/** 場面が変わらないときに独り言を入れ替える間隔。 */
export const ROTATE_MS = 90_000;
/** 「今日はプラス/マイナス」を独り言に混ぜ始める額($)。 */
export const DAY_EPSILON = 1;

/**
 * 気分 → 目の場面(eye3d の MODES)と吹き出しの色。
 * mode の "side" / "side-armed" は建玉・シナリオの向きで long / short に解ける。
 */
export const MOODS = Object.freeze({
  // ---- 場面(独り言)
  boot: { mode: "neutral", tone: "dim" },
  offline: { mode: "neutral", tone: "dim" },
  kill: { mode: "dejected", tone: "down" },
  halt: { mode: "dejected", tone: "down" },
  silent: { mode: "neutral", tone: "warn" },
  offduty: { mode: "neutral", tone: "dim" },
  blocked: { mode: "neutral", tone: "dim" },
  idle: { mode: "neutral", tone: "dim" },
  watching: { mode: "side", tone: "dim" },
  armed: { mode: "side-armed", tone: "warn" },
  order: { mode: "side-armed", tone: "warn" },
  posUp: { mode: "side", tone: "up" },
  posDown: { mode: "side", tone: "down" },
  posRun: { mode: "side", tone: "up" },
  dayUp: { mode: "neutral", tone: "up" },
  dayDown: { mode: "neutral", tone: "down" },
  // ---- 出来事(反応)
  sent: { mode: "side-armed", tone: "warn" },
  filled: { mode: "side-armed", tone: "warn" },
  tp1: { mode: "elated", tone: "up" },
  closed: { mode: "neutral", tone: "dim" },
  winBig: { mode: "elated", tone: "up" },
  win: { mode: "elated", tone: "up" },
  flat: { mode: "neutral", tone: "dim" },
  loss: { mode: "dejected", tone: "down" },
  lossBig: { mode: "dejected", tone: "down" },
});

/**
 * 台詞。夜警の声(nqx_state.py の verdict)と同じ register —— 短く、乾いていて、少し尊大。
 * 感情は語尾ではなく事実の選び方で出す。1 行 ≒ 46 字まで(吹き出しは 2 行で収める)。
 */
export const LINES = Object.freeze({
  boot: [
    "Optics online. Evening.",
    "Booting the night shift.",
    "Give me a moment. I am waking up.",
  ],
  offline: [
    "The wire went dead.",
    "No word from the house.",
    "Static. I keep the seat warm.",
  ],
  kill: [
    "Kill switch down. Hands off the tape.",
    "We are dark tonight. By order.",
  ],
  halt: [
    "Something jammed. On purpose, I hope.",
    "The loop is holding its breath.",
    "Halted. Someone has to walk over.",
  ],
  silent: [
    "No cycle in a while. Suspicious.",
    "The loop went quiet on me.",
    "Listening to static. Liking it less.",
  ],
  offduty: [
    "Off duty. The chart can wait.",
    "The desk is dark. So am I.",
    "Nothing to watch until the bell.",
    "One eye shut. The other, mostly.",
  ],
  blocked: [
    "The gate held. Good.",
    "Nothing here worth the risk.",
    "The night will not be read tonight.",
    "No trade is a trade. I take it.",
  ],
  idle: [
    "Quiet tape. Quiet me.",
    "I watch. That is the entire job.",
    "Nothing yet. Nothing is an answer.",
    "The room hums. The price refuses to.",
  ],
  watching: [
    "A shape is forming. Not yet.",
    "I see it. It does not see us.",
    "Close. Not close enough to touch.",
  ],
  armed: [
    "Armed. Do not blink.",
    "The level is set. Let them come to us.",
    "Loaded. Hands where I can see them.",
  ],
  order: [
    "It is with the broker now.",
    "Sent. Nothing left but breathing.",
    "The waiting is the expensive part.",
  ],
  sent: [
    "Away it goes.",
    "Order out the door. Sit up.",
  ],
  filled: [
    "Filled. Now we find out.",
    "We are in. Hands off the mouse.",
  ],
  tp1: [
    "First one banked. The runner runs.",
    "TP1 is ours. Stop goes to even.",
  ],
  posUp: [
    "Ahead. Say nothing, touch nothing.",
    "It is working. Let it work.",
    "Up. I refuse to count it yet.",
  ],
  posDown: [
    "Offside. The stop was built for this.",
    "Against us. The plan knew it might be.",
    "It stings. It is allowed to.",
  ],
  posRun: [
    "Half home. The rest is a gift.",
    "The runner rides for free now.",
  ],
  closed: [
    "Flat. The damage lands shortly.",
    "Closed. Counting the pieces.",
  ],
  winBig: [
    "Ha. The whole target.",
    "That one goes on the wall.",
    "Full distance. I am insufferable now.",
  ],
  win: [
    "Paid. A good night of work.",
    "Taken. Quietly pleased.",
    "That will do nicely.",
  ],
  flat: [
    "A scratch. The night shrugged.",
    "Even. No harm, no story.",
  ],
  loss: [
    "Stopped. It did its job.",
    "That one cost us. Noted.",
    "We paid to find out. Fine.",
  ],
  lossBig: [
    "That one hurt. I will sit with it.",
    "Tuition. Expensive tuition.",
    "I did not enjoy that at all.",
  ],
  dayUp: [
    "The night has paid. So far.",
    "Ahead on the day. Do not spend it.",
  ],
  dayDown: [
    "The night took some back.",
    "Down on the day. I have seen worse.",
  ],
});

// ---------------------------------------------------------------- 純関数

/** FNV-1a(32bit)。同じ seed からは常に同じ台詞を引くためだけに使う。 */
export function hashSeed(text) {
  let hash = 0x811c9dc5;
  const source = String(text ?? "");
  for (let i = 0; i < source.length; i += 1) {
    hash ^= source.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash >>> 0;
}

/** 気分と seed から台詞を 1 つ。未知の気分は idle の池から引く。 */
export function pickLine(key, seed) {
  const pool = LINES[key] || LINES.idle;
  return pool[hashSeed(`${key}|${seed}`) % pool.length];
}

/** 気分 + 向き → 目の場面と吹き出しの色。 */
export function describeMood(key, side) {
  const mood = MOODS[key] || MOODS.idle;
  let mode = mood.mode;
  if (mode === "side" || mode === "side-armed") {
    const dir = String(side || "").toUpperCase();
    const base = dir === "SHORT" || dir === "SELL" ? "short"
      : (dir === "LONG" || dir === "BUY" ? "long" : "");
    mode = base ? (mood.mode === "side-armed" ? `${base}-armed` : base) : "neutral";
  }
  return { key, mode, tone: mood.tone };
}

function num(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/** 決済 1 件の重さ。R と $ の両方で見る(片方しか持たない記録があるため)。 */
export function classifyResult(result) {
  if (!result || typeof result !== "object") return null;
  if (num(result.entry) === null || num(result.exit) === null
    || num(result.qty) === null || num(result.pointValue) === null) return null;
  const { usd, r, state } = deriveTrade(result);
  if (state === "flat") return "flat";
  if (state === "win") return (r !== null && r >= 1.8) || usd >= 400 ? "winBig" : "win";
  return (r !== null && r <= -1.5) || usd <= -500 ? "lossBig" : "loss";
}

/** publish された最新の決済(delta は result、snapshot は履歴の先頭)。 */
export function latestResult(view) {
  const direct = view?.result;
  if (direct && direct.resultId) return direct;
  const recent = view?.recentResults;
  return Array.isArray(recent) && recent.length && recent[0]?.resultId ? recent[0] : null;
}

const PENDING_ORDER_STATES = new Set([
  "PENDING", "SENT", "PARTIAL", "UNKNOWN",
  "ENTRY_PARTIAL_ROUTE", "ENTRY_PARTIAL_FILL", "ENTRY_RESTING",
]);

function openPosition(view) {
  const p = view?.position;
  if (!p || typeof p !== "object") return null;
  const qty = num(p.qty);
  return qty !== null && qty > 0 ? p : null;
}

function runnerLeft(position) {
  if (!position) return false;
  const initial = num(position.initialQty);
  const qty = num(position.qty);
  return initial !== null && qty !== null && initial > 0 && qty < initial;
}

function pendingOrder(view) {
  const o = view?.order;
  if (!o || typeof o !== "object") return null;
  return PENDING_ORDER_STATES.has(String(o.state || "").toUpperCase()) ? o : null;
}

function sentClaim(view) {
  const c = view?.entryClaim;
  if (!c || typeof c !== "object" || !c.entryKey) return null;
  const state = String(c.state || "").toUpperCase();
  if (state === "CONSUMED") return c;
  if (state === "RESOLVED" && c.consumedAt) return c;
  return null;
}

function armedScenario(view) {
  const s = view?.scenario;
  if (!s || typeof s !== "object") return null;
  const state = String(s.state || "").toUpperCase();
  if (state !== "ARMED" && state !== "ACTIVE") return null;
  return view?.display?.orderable === true ? s : null;
}

/**
 * 前後の view から拾う出来事を 1 つだけ返す(無ければ null)。純粋関数。
 * 重い順: 決済 > TP1 > 約定 > 建玉が消えた > 送信 > 武装。
 */
export function nextPetEvent(prev, next) {
  if (!next || typeof next !== "object") return null;
  const p = prev && typeof prev === "object" ? prev : {};

  const nResult = latestResult(next);
  const pResult = latestResult(p);
  if (nResult && nResult.resultId !== pResult?.resultId) {
    const key = classifyResult(nResult);
    if (key) return { key, side: nResult.side, seed: nResult.resultId, holdMs: HOLD_MS.result };
  }

  const pPos = openPosition(p);
  const nPos = openPosition(next);
  if (pPos && nPos && runnerLeft(nPos) && !runnerLeft(pPos)) {
    return { key: "tp1", side: nPos.side, seed: `${nPos.symbol || ""}:${nPos.qty}`, holdMs: HOLD_MS.event };
  }
  if (!pPos && nPos) {
    return { key: "filled", side: nPos.side, seed: nPos.filledAt || nPos.observedAt || "", holdMs: HOLD_MS.event };
  }
  if (pPos && !nPos) {
    return { key: "closed", side: pPos.side, seed: pPos.filledAt || "", holdMs: HOLD_MS.event };
  }

  const nOrder = pendingOrder(next);
  if (nOrder && !pendingOrder(p)) {
    return { key: "sent", side: nOrder.side, seed: nOrder.idempotencyKey || nOrder.at || "", holdMs: HOLD_MS.event };
  }
  const nClaim = sentClaim(next);
  if (nClaim && !pendingOrder(p)) {
    const pClaim = sentClaim(p);
    if (!pClaim || pClaim.entryKey !== nClaim.entryKey) {
      return { key: "sent", side: next?.scenario?.side, seed: nClaim.entryKey, holdMs: HOLD_MS.event };
    }
  }

  const nArmed = armedScenario(next);
  if (nArmed) {
    const pArmed = armedScenario(p);
    if (!pArmed || pArmed.scenarioId !== nArmed.scenarioId) {
      return { key: "armed", side: nArmed.side, seed: nArmed.scenarioId, holdMs: HOLD_MS.event };
    }
  }
  return null;
}

/**
 * 出来事が無いときの気分。view と、アプリ側しか知らない文脈(接続・監視ループの生死・
 * 今日の実現損益)から決める。純粋関数。
 *
 * @param {object|null} view
 * @param {{ now?: number, offline?: boolean, health?: string, kill?: boolean, dayUsd?: number|null }} ctx
 */
export function ambientMood(view, ctx = {}) {
  if (!view) return { key: "boot", side: null };
  if (ctx.kill === true) return { key: "kill", side: null };
  if (ctx.offline === true) return { key: "offline", side: null };
  const health = String(ctx.health || "").toUpperCase();
  if (health === "HALT") return { key: "halt", side: null };

  const position = openPosition(view);
  if (position) {
    if (runnerLeft(position)) return { key: "posRun", side: position.side };
    const pnl = num(position.unrealizedPnl);
    // 含み損だけを「下」にする。観測が無い(N/A)ときは煽らない。
    return { key: pnl !== null && pnl < 0 ? "posDown" : "posUp", side: position.side };
  }

  const order = pendingOrder(view);
  if (order) return { key: "order", side: order.side };
  const armed = armedScenario(view);
  if (armed) return { key: "armed", side: armed.side };
  if (view.scenario) return { key: "watching", side: view.scenario.side };

  if (health === "OFF_DUTY") return { key: "offduty", side: null };
  if (health === "SILENT" || health === "LATE") return { key: "silent", side: null };

  // 今日の実現損益は独り言に**混ぜる**(言い続けない)。入れ替えの拍で交互に出す。
  const day = num(ctx.dayUsd);
  const bucket = Math.floor((num(ctx.now) ?? 0) / ROTATE_MS);
  if (day !== null && Math.abs(day) >= DAY_EPSILON && bucket % 2 === 0) {
    return { key: day > 0 ? "dayUp" : "dayDown", side: null };
  }
  if (view.market?.evaluation) return { key: "blocked", side: null };
  return { key: "idle", side: null };
}

// ---------------------------------------------------------------- 声(状態を持つ)

/**
 * つぶやきの声。`observe(view)` を view が変わるたびに、`speak(view, ctx)` を描画のたびに
 * 呼ぶ。出来事は hold の間だけ独り言を上書きし、過ぎれば場面へ戻る。
 *
 * @param {{ now?: () => number }} [opts]
 */
export function createPetVoice({ now = () => Date.now() } = {}) {
  let prev = null;
  let hydrated = false;
  let event = null;
  let nudge = 0;

  return {
    /** 起動直後の 1 件目は基準として飲み込む。以後は差分から出来事を 1 つ拾う。 */
    observe(view) {
      if (!view || view === prev) return null;
      if (!hydrated) { hydrated = true; prev = view; return null; }
      const found = nextPetEvent(prev, view);
      prev = view;
      if (found) event = { ...found, until: now() + found.holdMs };
      return found;
    },
    /** 今の一言。反応が生きていればそれ、無ければ場面の独り言。 */
    speak(view, ctx = {}) {
      const at = num(ctx.now) ?? now();
      if (event && at >= event.until) event = null;
      if (event) {
        return {
          ...describeMood(event.key, event.side),
          line: pickLine(event.key, `${event.seed}|${nudge}`),
          reacting: true,
        };
      }
      const mood = ambientMood(view, { ...ctx, now: at });
      const bucket = Math.floor(at / ROTATE_MS) + nudge;
      return { ...describeMood(mood.key, mood.side), line: pickLine(mood.key, bucket), reacting: false };
    },
    /** 突かれたら台詞を引き直す(表示だけの遊び)。 */
    poke() { nudge += 1; },
    /** デモ・テスト用。今保持している反応(無ければ null)。 */
    pending() { return event ? { key: event.key, until: event.until } : null; },
  };
}
