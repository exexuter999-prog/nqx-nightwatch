/**
 * Claude との対話面(R38、無期限停止中)。
 *
 * **発注画面とは別エントリ**。app.js も three.js も読まないので、発注の
 * 起動経路に一切影響しない。この面から注文は出せず、シナリオにも触れない。
 *
 * 送信は role=user 固定。Claude としての発言は PC 側の署名が要る
 * (Worker が role で認証を分ける)。ここから "claude" は名乗れない。
 */

export const INBOX_ENABLED = false;

const el = (id) => document.getElementById(id);

/** 表示用に安全な要素を組む。innerHTML は使わない(本文は任意の文字列)。 */
export function messageNode(row, doc = document) {
  const wrap = doc.createElement("div");
  const mine = row?.role === "user";
  wrap.className = `msg ${mine ? "is-user" : "is-claude"}`;
  const who = doc.createElement("div");
  who.className = "msg-who";
  who.textContent = `${mine ? "あなた" : "Claude"} · ${stamp(row?.at)}`;
  const body = doc.createElement("div");
  body.className = "msg-body";
  body.textContent = String(row?.text ?? "");
  wrap.append(who, body);
  return wrap;
}

export function stamp(atMs) {
  // Number(null) は 0 で有限判定を通り、1970-01-01 として描かれてしまう。
  // 時刻は必ず正の epoch ミリ秒。それ以外は空にする。
  const n = Number(atMs);
  if (!Number.isFinite(n) || n <= 0) return "";
  try {
    return new Date(n).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch {
    return "";
  }
}

/** 既に描いた seq を覚え、重複描画しない。 */
export function mergeMessages(seen, rows) {
  const fresh = [];
  for (const row of rows || []) {
    // Number(null) は 0、Number("") も 0 なので有限判定だけでは抜けてしまう。
    // seq は必ず正の整数(DO の AUTOINCREMENT)。それ以外は捨てる。
    const seq = Number(row?.seq);
    if (!Number.isInteger(seq) || seq <= 0 || seen.has(seq)) continue;
    seen.add(seq);
    fresh.push(row);
  }
  fresh.sort((a, b) => Number(a.seq) - Number(b.seq));
  return fresh;
}

function boot() {
  const log = el("log");
  const input = el("input");
  const form = el("form");
  const status = el("status");
  status.textContent = "無期限停止";
  status.className = "is-bad";
  input.disabled = true;
  input.placeholder = "受信箱は無期限停止中";
  form.querySelector("button")?.setAttribute("disabled", "");
  const notice = document.createElement("div");
  notice.className = "msg is-claude";
  notice.textContent = "受信箱は無期限停止中です。監視・分析・自動発注は継続します。";
  log.append(notice);
}

if (typeof document !== "undefined" && document.getElementById("log")) boot();
