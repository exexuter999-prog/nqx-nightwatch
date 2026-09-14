/**
 * R6: シナリオ評価の表示(APP_EVAL_DISPLAY_SPEC §6)。
 *
 * 判定の正本は msnr_gate.py と CLAUDE.md のゲート群。ここは
 * **受け取った evaluation を描くだけ**で、アプリ側で判定を再計算しない。
 * evaluation が無ければ全関数が空文字を返し、表示は R6 以前と同一になる。
 *
 * DOM に触らないので node --test から直接読める(chart.js / ledger.js と同じ流儀)。
 */

const GATE_PASS = "✓";
const GATE_FAIL = "✗";
const GATE_WARN = "!";

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]
  ));
}

/** 有限数だけ整形する。欠損は "—"(0 で埋めない)。 */
export function num(value, digits = 2) {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
}

function priceText(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed)
    ? parsed.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : "—";
}

/** ok: true=通過 / false=停止 / null=未取得(判定していない)。 */
export function gateRow(label, value, ok) {
  const cls = ok === "warn" ? "gate-warn"
    : ok === null ? "gate-idle" : (ok ? "gate-pass" : "gate-halt");
  const mark = ok === "warn" ? GATE_WARN
    : ok === null ? "·" : (ok ? GATE_PASS : GATE_FAIL);
  return `<div class="gate-row ${cls}">`
    + `<span class="gate-label">${escapeHtml(label)}</span>`
    + `<span class="gate-value">${escapeHtml(value)}</span>`
    + `<span class="gate-mark" aria-hidden="true">${mark}</span>`
    + `</div>`;
}

function volRow(vol) {
  const state = vol.ruling === "A+のみ" ? "warn"
    : vol.ruling === "停止" ? false
      : vol.ruling === "通常" ? true : null;
  return gateRow("VOL", `${num(vol.ratio)} · ${rulingText(vol.ruling)}`,
                 state);
}

function rotRow(rot) {
  const value = `${rot.negations ?? "—"}/${rot.signals ?? "—"} ${rot.verdict || "?"}`;
  return gateRow("ROTATION", value,
                 !rot.verdict || rot.verdict === "不明" ? null : rot.verdict === "OK");
}

function msnrRow(m) {
  if (!m || typeof m !== "object") return gateRow("MSNR", "NO CHAIN", null);
  // blockers は外部由来。配列でなければ無いものとして扱う(文字列に join は無い)。
  const blockers = Array.isArray(m.blockers) ? m.blockers : [];
  const head = [m.label, m.chainState].filter(Boolean).join(" · ") || "—";
  const detail = m.allowed
    ? `${head} · ${m.barsLeft ?? "—"} left`
    : `${head}${blockers.length ? ` · ${blockers.slice(0, 2).join(" / ")}` : ""}`;
  return gateRow("MSNR", detail, m.allowed === true);
}

function dataRow(data, runtime = {}) {
  if (!data || typeof data !== "object") return gateRow("DATA", "NO RECEIPT", null);
  const fresh = Number.isFinite(Number(data.freshCount)) ? Number(data.freshCount) : null;
  const total = Number.isFinite(Number(data.requiredCount)) ? Number(data.requiredCount) : null;
  const liveAge = Number.isFinite(Number(runtime.marketAgeSec)) ? Number(runtime.marketAgeSec) : null;
  const oldest = Number.isFinite(Number(data.oldestAgeSec)) ? Number(data.oldestAgeSec) : null;
  const span = Number.isFinite(Number(data.sourceSpanSec)) ? Number(data.sourceSpanSec) : null;
  // 落ちる理由は2つあり、別物である。
  //   receiptStale — 取得受領書の必須 raw が非FRESH(取り直しが要る)
  //   marketStale  — 受領書は健全だが、公開済みサイクル自体が古い(ループ停止・窓外)
  // 以前はどちらでも "8/8 FRESH" と書いたまま ✗ を付けていたので、
  // 「FRESH なのに×」という読めない行になっていた(2026-09-01 実測:
  // `8/8 FRESH · age 6130s · span 60s ✗`)。落ちている側を必ず語尾に出す。
  const receiptStale = data.requiredFresh !== true;
  const marketStale = runtime.marketStale === true || runtime.marketVerified === false;
  const stale = receiptStale || marketStale;
  const counted = fresh !== null && total !== null
    ? `${fresh}/${total} ${receiptStale ? "RAW" : "FRESH"}`
    : data.status || "?";
  const parts = [counted];
  if (liveAge !== null) parts.push(`age ${Math.round(liveAge)}s`);
  else if (oldest !== null) parts.push(`oldest ${Math.round(oldest)}s`);
  if (span !== null) parts.push(`span ${Math.round(span)}s`);
  const missing = Array.isArray(data.staleRequired) ? data.staleRequired : [];
  if (missing.length) parts.push(missing.slice(0, 2).join(" / "));
  else if (receiptStale) parts.push("RECEIPT STALE");
  else if (marketStale) parts.push("MARKET STALE");
  return gateRow("DATA", parts.join(" · "), stale ? false : true);
}

function cvdRow(cvd) {
  if (!cvd || typeof cvd !== "object") return gateRow("CVD", "NO STATUS", null);
  const attempts = Number.isFinite(Number(cvd.attempts)) ? Number(cvd.attempts) : null;
  const max = Number.isFinite(Number(cvd.maxAttempts)) ? Number(cvd.maxAttempts) : null;
  const parts = [cvd.status || cvd.freshness || "?"];
  if (attempts !== null && max !== null) parts.push(`${attempts}/${max} reads`);
  parts.push(cvd.aplusAllowed === true ? "A+ ENABLED" : "A+→A CAP");
  if (cvd.reason) parts.push(cvd.reason);
  const state = cvd.status === "FRESH" && cvd.available === true ? true
    : cvd.status ? "warn" : null;
  return gateRow("CVD", parts.join(" · "), state);
}

function sessionRow(session) {
  if (!session || typeof session !== "object") return gateRow("ICT TIME", "N/A", null);
  const parts = [session.window || session.label || "?"];
  if (session.et) parts.push(`${session.et} ET`);
  parts.push(session.tradeable === true ? "ACTIVE WINDOW"
    : session.tradeable === false ? "CONTEXT ONLY" : "UNVERIFIED");
  const state = session.tradeable === true ? true
    : session.tradeable === false ? "warn" : null;
  return gateRow("ICT TIME", parts.join(" · "), state);
}

function decisionRow(evaluation) {
  const d = evaluation && typeof evaluation === "object" ? evaluation.decision : null;
  if (!d || typeof d !== "object") return gateRow("MODEL", "NO DECISION", null);
  const blockers = Array.isArray(d.hardBlockers) ? d.hardBlockers : [];
  if (d.model === "FLAT") {
    return gateRow("MODEL", `FLAT${blockers.length ? ` · ${blockers.slice(0, 2).join(" / ")}` : ""}`, false);
  }
  const grade = d.grade || "B";
  const state = String(d.state || "WATCH").toUpperCase();
  const pass = ["ARMED", "ACTIVE"].includes(state) && ["A", "A+", "B"].includes(grade)
    && blockers.length === 0;
  const text = `${d.model} · ${grade} · ${state}`
    + (blockers.length ? ` · ${blockers.slice(0, 2).join(" / ")}` : "");
  return gateRow("MODEL", text, pass ? true : false);
}

/** シナリオカードに挟むゲートチェックリスト。旧payloadは5行、R43はDATA/CVD/ICTを追加。 */
/** サーバーの ruling(日本語)を表示用の英語へ。判定ロジックは元の値を使う。 */
const RULING_EN = { "停止": "HALT", "A+のみ": "A+ ONLY", "通常": "OK", "不明": "?" };
const rulingText = (value) => RULING_EN[value] || value || "?";

/**
 * 15M ALIGN の描き分け。aligned=true は通過。decision が方向を持たない
 * (FLAT / side 無し)夜は「照らし合わせる向きが無い」ので未判定(·)にする —
 * サーバーは boolean しか運べないため false で届くが、それを ✗ と描くと
 * 「15分足が逆行している」と読めてしまう。方向のある decision で false なら ✗。
 */
function htfState(h, decision) {
  if (h.aligned === true) return true;
  const side = decision && typeof decision === "object" ? decision.side : undefined;
  if (decision && side !== "BUY" && side !== "SELL") return null;
  return false;
}

/**
 * runtime を受け取るのはシナリオカードでも DATA ゲートの判定を
 * gateBoard と揃えるため。渡さないと市況の古さが見えず、**同じサイクルなのに
 * シナリオカードでは DATA ✓・ゲートボードでは ✗** という食い違いが出る。
 */
export function evalChecklist(evaluation, runtime = {}) {
  if (!evaluation || typeof evaluation !== "object") return "";
  const vol = evaluation.volGate || {};
  const rot = evaluation.rotation || {};
  const h = evaluation.htf;
  const e = evaluation.entry;

  const rows = [
    ...(evaluation.dataGate ? [dataRow(evaluation.dataGate, runtime)] : []),
    volRow(vol),
    rotRow(rot),
    msnrRow(evaluation.msnr),
    ...(evaluation.cvdGate ? [cvdRow(evaluation.cvdGate)] : []),
    ...(evaluation.sessionGate ? [sessionRow(evaluation.sessionGate)] : []),
    h
      ? gateRow("15M ALIGN",
                `CT ${h.ctTrend > 0 ? "+1" : h.ctTrend < 0 ? "−1" : "0"} · ATR ${num(h.ctAtr, 1)}pt${
                  htfState(h, evaluation.decision) === null ? " · NO SIDE" : ""}`,
                htfState(h, evaluation.decision))
      : gateRow("15M ALIGN", "N/A", null),
    e
      ? gateRow("ENTRY", `air ${num(e.airPct, 0)}% · reach ${num(e.reachR, 1)}R`,
                e.pass === true)
      : gateRow("ENTRY", "N/A", null),
  ];
  return `<div class="gate-list" role="group" aria-label="gates">${rows.join("")}</div>`;
}

/**
 * R11-D: 最有力1モデルをカード最上段へ表示する。判定はPython側のdecisionを読む。
 *
 * 表示の作りだけをここで決める。**判定は一切しない**ので、grade も state も
 * blockers もサーバーの値をそのまま出す。唯一の算術は SL 幅(|entry − stop|)で、
 * これは同じカードに出ている2つの価格の差でしかない。
 *
 * 2026-09-01 の作り直し:
 *   - 価格を `E … · SL … · TP …` の一本棒から、シナリオカードと同じ
 *     3列のラダーへ。一番読みたい数字が一番大きい。
 *   - side を色付きのバッジにして、LONG/SHORT を字面ではなく色で見せる。
 *   - **武装できない理由(hardBlockers)をカードに載せた。** 以前は `TP —` と
 *     だけ出て、なぜ WATCH なのかはゲート行を読むまで分からなかった。
 */
export function decisionBanner(evaluation) {
  const d = evaluation && typeof evaluation === "object" ? evaluation.decision : null;
  if (!d || typeof d !== "object" || d.model === "FLAT") return "";
  const grade = d.grade || "B";
  const score = typeof d.score === "number" && Number.isFinite(d.score) ? `${d.score}` : "—";
  const side = d.side === "BUY" ? "LONG" : d.side === "SELL" ? "SHORT" : "FLAT";
  const state = String(d.state || "WATCH").toUpperCase();
  const armed = ["ARMED", "ACTIVE"].includes(state);
  const blockers = Array.isArray(d.hardBlockers) ? d.hardBlockers : [];
  const entry = priceText(d.entry);
  const stop = priceText(d.stop);
  const target = Array.isArray(d.targets) && d.targets.length ? priceText(d.targets[0]) : "—";

  // SL 幅。両端が無ければ出さない(0 で埋めない)。
  const entryNum = Number(d.entry);
  const stopNum = Number(d.stop);
  const stopPts = Number.isFinite(entryNum) && Number.isFinite(stopNum)
    ? Math.abs(entryNum - stopNum) : null;
  // R は**サーバーの targetR だけ**を使う。ここで R:R を計算し直さない。
  const rawR = Array.isArray(d.targetR) ? d.targetR[0] : d.targetR;
  const targetR = Number.isFinite(Number(rawR)) ? Number(rawR) : null;

  const facts = [`<span class="decision-state">${escapeHtml(state)}</span>`];
  if (stopPts !== null) facts.push(`<span>SL ${escapeHtml(num(stopPts))}pt</span>`);
  if (targetR !== null) facts.push(`<span>TP1 ${escapeHtml(num(targetR))}R</span>`);

  const leg = (label, value, cls = "") =>
    `<div class="decision-leg${cls}">`
    + `<span class="micro">${escapeHtml(label)}</span>`
    + `<b>${escapeHtml(value)}</b></div>`;

  return `<div class="decision-banner ${armed ? "is-armed" : "is-held"}"`
    + ` data-side="${escapeHtml(side)}" aria-label="primary model">`
    + `<div class="decision-head">`
    + `<span class="decision-side">${escapeHtml(side)}</span>`
    + `<span class="decision-model">MODEL ${escapeHtml(String(d.model))}</span>`
    + `<span class="decision-grade">${escapeHtml(grade)} · ${escapeHtml(score)}</span>`
    + `</div>`
    + `<div class="decision-ladder">`
    + leg("ENTRY", entry)
    + leg("SL", stop, " is-stop")
    + leg("TP1", target)
    + `</div>`
    + `<p class="decision-facts">${facts.join("")}</p>`
    + (blockers.length
      ? `<p class="decision-blocker">${escapeHtml(blockers.slice(0, 2).join(" / "))}</p>`
      : "")
    + `</div>`;
}

/** 旧advisoryを残す場合も、R11-Dのdecisionがある画面では観測専用ラベルを出さない。 */
export function advisoryChips(evaluation) {
  const a = evaluation && typeof evaluation === "object" ? evaluation.advisory : null;
  if (!a || typeof a !== "object") return "";
  const chips = [];
  let activeEvidence = false;
  if (a.vwap && typeof a.vwap === "object") {
    const drift = typeof a.vwap.drift === "number" && Number.isFinite(a.vwap.drift)
      ? `(drift ${a.vwap.drift.toFixed(1)}pt)` : "";
    const body = a.vwap.state === "VWAP_DRIFT"
      ? `VWAP 無効${drift}`
      : `VWAP ${a.vwap.side === "BUY" ? "RECLAIM" : "REJECT"} ${a.vwap.state || "—"}${drift}`;
    const active = ["VWAP_ACCEPTED", "VWAP_HELD"].includes(a.vwap.state);
    activeEvidence ||= active;
    chips.push(`<span class="advisory-chip${active ? "" : " is-inactive"}">${escapeHtml(body)}</span>`);
  }
  if (a.vp && typeof a.vp === "object") {
    const target = typeof a.vp.target === "number" && Number.isFinite(a.vp.target)
      ? ` → ${priceText(a.vp.target)}` : "";
    const active = a.vp.state === "VP_ACCEPTED";
    activeEvidence ||= active;
    chips.push(`<span class="advisory-chip${active ? "" : " is-inactive"}">${escapeHtml(`VP ${a.vp.state || "—"}${target}`)}</span>`);
  }
  if (!chips.length) return "";
  const d = evaluation?.decision;
  const actionable = d && d.model !== "FLAT" && ["BUY", "SELL"].includes(d.side);
  const tag = actionable && activeEvidence ? "SUPPORTING" : "CONTEXT";
  return `<div class="advisory-line">`
    + `<span class="advisory-tag">${escapeHtml(tag)}</span>${chips.join("")}`
    + `</div>`;
}

/**
 * シナリオが無い夜に「何が止めているか」を出す現況ボード。
 * stamp は呼び出し側が JST 整形して渡す(時刻整形は app.js の責務)。
 */
export function gateBoard(evaluation, stamp = "", runtime = {}) {
  if (!evaluation || typeof evaluation !== "object") return "";
  const vol = evaluation.volGate || {};
  const ratio = typeof vol.ratio === "number" && Number.isFinite(vol.ratio) ? vol.ratio : null;
  const pct = ratio === null ? 0 : Math.max(0, Math.min(100, ratio * 100));
  const tone = vol.ruling === "停止" ? "halt" : vol.ruling === "A+のみ" ? "warn" : "pass";
  // カードが出る場合、MODEL 行はモデル名・等級・状態・blocker まで完全に
  // 同じ内容を繰り返すだけなので出さない。FLAT(カード無し)の時だけ行で見せる。
  const banner = decisionBanner(evaluation);

  return `
    <article class="state-card gates">
      <div class="card-top">
        <span class="state">◇ GATES</span>
        <span class="aux">${escapeHtml(stamp)}</span>
      </div>
      <div class="gate-meter ${tone}">
        <div class="meter-track" role="img" aria-label="vol ratio ${escapeHtml(num(ratio))}">
          <span class="meter-fill" style="width:${pct.toFixed(1)}%"></span>
          <span class="meter-tick" style="left:40%"></span>
          <span class="meter-tick" style="left:60%"></span>
        </div>
        <p class="meter-caption">
          VOL <b>${escapeHtml(num(ratio))}</b> ·
          floor ${escapeHtml(num(vol.noise, 1))}pt / cap ${escapeHtml(num(vol.slCap, 0))}pt ·
          <b>${escapeHtml(rulingText(vol.ruling))}</b>
        </p>
      </div>
      ${dataRow(evaluation.dataGate, runtime)}
      ${rotRow(evaluation.rotation || {})}
      ${msnrRow(evaluation.msnr)}
      ${cvdRow(evaluation.cvdGate)}
      ${sessionRow(evaluation.sessionGate)}
      ${banner ? "" : decisionRow(evaluation)}
      ${banner}
      ${advisoryChips(evaluation)}
    </article>`;
}

/**
 * ボラ帯が A+ を要求しているのに等級が足りない状態か。
 * サーバーが WATCH に落としているはずの組合せなので、出た時点で発注させない。
 */
export function gradeContradicts(evaluation, grade) {
  return evaluation?.volGate?.ruling === "A+のみ" && grade !== "A+";
}
