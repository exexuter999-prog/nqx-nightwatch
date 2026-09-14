/**
 * 観測漏れで残った注文表示(ORDER PENDING)の手動解除。
 *
 * Bot が position の遷移を一度も観測しないまま建玉が往復すると、order
 * ストリームに SENT が残り、ORDER_PENDING が発注を永久ブロックする
 * (HANDOFF §11 ②)。このモジュールは解除ボタンを「いつ出してよいか」を
 * 決める純関数だけを持つ。ここの判定は表示の可否であって承認ではない —
 * 送信の最終判定は Bot 側がブローカー verified FLAT を再確認して行う。
 */

/** state_machine.js の BLOCKING_ORDER_STATES と一致させること。 */
export const BLOCKING_ORDER_STATES = ["PENDING", "SENT", "PARTIAL", "UNKNOWN",
  "ENTRY_PARTIAL_ROUTE", "ENTRY_PARTIAL_FILL", "ENTRY_RESTING"];

/**
 * 送信直後の約定待ちを誤って解除しないための待機時間。
 * telegram_bot.py の STUCK_ORDER_MIN_AGE_SEC と一致させること。
 */
export const STUCK_ORDER_MIN_AGE_MS = 10 * 60_000;

/**
 * 解除導線の表示判定。
 *
 * 戻り値:
 *   null                      … 導線自体を出さない(注文が無い・終端済み・建玉あり)
 *   {eligible:false, reason}  … 状況説明だけ出す(解除はまだ許可しない)
 *   {eligible:true}           … 2段階+スライドの解除導線を出す
 */
export function stuckOrderInfo(view, nowMs) {
  const order = view?.order;
  if (!order) return null;
  if (!BLOCKING_ORDER_STATES.includes(String(order.state || "").toUpperCase())) return null;
  // 建玉が見えている間は定期照会が注文を FILLED に進める。解除は出さない。
  if (view.position) return null;

  const atMs = Date.parse(order.at || "");
  if (!Number.isFinite(atMs)) {
    return { eligible: false, reason: "ORDER TIME UNKNOWN", remainingMs: null };
  }
  const ageMs = nowMs - atMs;
  if (ageMs < STUCK_ORDER_MIN_AGE_MS) {
    return { eligible: false, reason: "AWAITING BROKER", remainingMs: STUCK_ORDER_MIN_AGE_MS - ageMs };
  }
  if (view.positionCheck?.verified !== true) {
    return { eligible: false, reason: "BROKER UNVERIFIED", remainingMs: null };
  }
  const frozen = Array.isArray(view?.entryClaim?.routeSnapshot)
    ? view.entryClaim.routeSnapshot.filter((row) => row?.state === "ACCEPTED") : [];
  if (!frozen.length || frozen.some((row) => !row.orderId)) {
    return { eligible: false, reason: "BROKER ORDER IDS UNKNOWN", remainingMs: null };
  }
  return { eligible: true, reason: null, remainingMs: 0 };
}

/** Bot へ送る解除要求。orderKey で「画面に見えていたその注文」だけを対象にする。 */
export function buildUnstickPayload(order, clientNonce) {
  return {
    type: "stuck_order_cancel_confirmed",
    orderKey: order.idempotencyKey,
    orderState: order.state,
    clientNonce,
    source: "nqx-nightwatch-mini-app",
  };
}
