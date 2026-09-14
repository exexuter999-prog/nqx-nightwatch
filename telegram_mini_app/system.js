/**
 * SYSTEM ROUTE — 経路の接続状況(R55, 2026-09-05)。
 *
 * 「データがどこから来て、注文がどこへ出て行くか」を1本の回路として描く。
 *   DATA ROUTE  : TRADINGVIEW → MONITOR LOOP → EVALUATION → CLOUDFLARE → THIS DEVICE
 *   ORDER ROUTE : AUTO ARM → ENGINE → BROKER → ACCOUNTS
 *
 * ここは **表示専用の純関数と描画**だけ。判断も発注もネットワークも持たない。
 * 各ノードの状態は Worker から届いた view と、アプリ自身の接続状態(runtime)
 * から**導出するだけ**で、見えないものを推測して UP にはしない
 * (証拠が無ければ UNKNOWN、窓の外は OFF)。
 *
 * DOM に触るのは renderSystemView だけなので、導出は node --test から読める。
 */

import { evaluateCycleHealth } from "./cyclehealth.js";
import { formatJstClock } from "./clock.js";

export const NODE_STATES = ["UP", "WARN", "DOWN", "OFF", "UNKNOWN"];

/** 市況・評価を「現在のもの」と呼べる上限。state_machine の MARKET_MAX_AGE と同じ 10 分。 */
const FRESH_MS = 10 * 60_000;
/** 建玉照会の古さの警告線。engine は毎サイクル照会するので、15 分は 5 周ぶん。 */
const BROKER_AGING_MS = 15 * 60_000;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]
  ));
}

function parseMs(iso) {
  const ms = Date.parse(iso || "");
  return Number.isFinite(ms) ? ms : null;
}

function clock(iso, { seconds = false } = {}) {
  const ms = parseMs(iso);
  return ms === null ? "—" : formatJstClock(ms, { seconds });
}

function ageText(ms) {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 60_000) return `${Math.round(ms / 1000)}s`;
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m`;
  return `${Math.floor(ms / 3_600_000)}h${String(Math.floor((ms % 3_600_000) / 60_000)).padStart(2, "0")}`;
}

function node(id, name, sub, state, word, detail, meta = "") {
  return { id, name, sub, state: NODE_STATES.includes(state) ? state : "UNKNOWN", word, detail, meta };
}

// ---------------------------------------------------------------- DATA ROUTE

function tradingviewNode(view, health) {
  if (!view) return node("tradingview", "TRADINGVIEW", "chart · tv_fetch", "UNKNOWN", "NO VIEW", "waiting for server state");
  if (health.status === "OFF_DUTY") {
    return node("tradingview", "TRADINGVIEW", "chart · tv_fetch", "OFF", "OFF DUTY",
      `resumes ${health.window?.nextLabel || "—"}`);
  }
  const evaluation = view.market?.evaluation || null;
  const gate = evaluation?.dataGate || null;
  const cvd = evaluation?.cvdGate || null;
  const meta = cvd
    ? `CVD ${cvd.status || cvd.freshness || "?"}${Number.isFinite(Number(cvd.attempts)) && Number.isFinite(Number(cvd.maxAttempts))
      ? ` · ${cvd.attempts}/${cvd.maxAttempts} reads` : ""}`
    : "";
  if (!gate || typeof gate !== "object") {
    return node("tradingview", "TRADINGVIEW", "chart · tv_fetch", "UNKNOWN", "NO RECEIPT",
      "acquisition receipt not published", meta);
  }
  // 受領書は健全でも、そのサイクル自体が古ければ「今も取れている」とは言えない。
  if (health.status === "SILENT" || health.status === "NO_DATA") {
    return node("tradingview", "TRADINGVIEW", "chart · tv_fetch", "UNKNOWN", "UNSEEN",
      `last receipt ${clock(evaluation?.at)} — loop silent`, meta);
  }
  const fresh = Number.isFinite(Number(gate.freshCount)) ? Number(gate.freshCount) : null;
  const total = Number.isFinite(Number(gate.requiredCount)) ? Number(gate.requiredCount) : null;
  const oldest = Number.isFinite(Number(gate.oldestAgeSec)) ? Math.round(Number(gate.oldestAgeSec)) : null;
  const span = Number.isFinite(Number(gate.sourceSpanSec)) ? Math.round(Number(gate.sourceSpanSec)) : null;
  const counted = fresh !== null && total !== null ? `${fresh}/${total} raw` : (gate.status || "?");
  const parts = [counted];
  if (oldest !== null) parts.push(`oldest ${oldest}s`);
  if (span !== null) parts.push(`span ${span}s`);
  if (view.market?.sourceSymbol) parts.push(String(view.market.sourceSymbol));
  if (gate.requiredFresh === true) {
    return node("tradingview", "TRADINGVIEW", "chart · tv_fetch", "UP", "FRESH", parts.join(" · "), meta);
  }
  const missing = Array.isArray(gate.staleRequired) ? gate.staleRequired : [];
  return node("tradingview", "TRADINGVIEW", "chart · tv_fetch", "DOWN", "STALE",
    missing.length ? `not fresh: ${missing.slice(0, 3).join(" / ")}` : `${counted} · required raw not fresh`, meta);
}

function loopNode(view, health, runtime) {
  const sub = "nqx_cycle · every 3m";
  if (runtime.demo) return node("loop", "MONITOR LOOP", sub, "UP", "DEMO", "loop display test");
  if (!view) return node("loop", "MONITOR LOOP", sub, "UNKNOWN", "NO VIEW", "waiting for server state");
  if (runtime.appOffline) {
    return node("loop", "MONITOR LOOP", sub, "UNKNOWN", "UNSEEN", "app offline — not the loop");
  }
  const last = health.lastAtMs === null ? "" : formatJstClock(health.lastAtMs, { seconds: false });
  switch (health.status) {
    case "ALIVE":
      return node("loop", "MONITOR LOOP", sub, "UP", "LIVE", `cycle ${last} · ${ageText(health.ageMs)} ago`);
    case "BLOCKED":
      return node("loop", "MONITOR LOOP", sub, "WARN", "BLOCKED",
        health.beacon?.reason || "cycle blocked — no trade this cycle");
    case "HALT":
      return node("loop", "MONITOR LOOP", sub, "DOWN", health.beacon?.kill ? "KILL" : "HALT",
        health.beacon?.kill ? "emergency stop engaged" : (health.beacon?.reason || "loop halted — needs manual restart"));
    case "LATE":
      return node("loop", "MONITOR LOOP", sub, "WARN", "LATE", `no cycle for ${health.ageMin}m · last ${last}`);
    case "SILENT":
      return node("loop", "MONITOR LOOP", sub, "DOWN", "SILENT", `no cycle for ${health.ageMin}m · last ${last}`);
    case "OFF_DUTY":
      return node("loop", "MONITOR LOOP", sub, "OFF", "OFF DUTY",
        last ? `resumes ${health.window?.nextLabel} · last ${last}` : `resumes ${health.window?.nextLabel}`);
    default:
      return node("loop", "MONITOR LOOP", sub, "UNKNOWN", "NO SIGNAL", "no cycle observed yet");
  }
}

function evaluationNode(view, health, nowMs) {
  const sub = "msnr_gate · R11-D";
  if (!view) return node("evaluation", "EVALUATION", sub, "UNKNOWN", "NO VIEW", "waiting for server state");
  const evaluation = view.market?.evaluation || null;
  if (!evaluation || typeof evaluation !== "object") {
    return node("evaluation", "EVALUATION", sub, "UNKNOWN", "NO DECISION", "no evaluation on the market stream");
  }
  const d = evaluation.decision && typeof evaluation.decision === "object" ? evaluation.decision : null;
  const vol = evaluation.volGate || {};
  const parts = [];
  if (d?.model) parts.push(String(d.model));
  if (Number.isFinite(Number(vol.ratio))) parts.push(`vol ${Number(vol.ratio).toFixed(2)}`);
  const atMs = parseMs(evaluation.at);
  const ageMs = atMs === null ? null : Math.max(0, nowMs - atMs);
  if (health.status === "OFF_DUTY") {
    return node("evaluation", "EVALUATION", sub, "OFF", "OFF DUTY",
      `last decision ${clock(evaluation.at)}${parts.length ? ` · ${parts.join(" · ")}` : ""}`);
  }
  if (ageMs === null || ageMs > FRESH_MS) {
    return node("evaluation", "EVALUATION", sub, "WARN", "AGED",
      `from ${clock(evaluation.at)} (${ageText(ageMs)} ago)${parts.length ? ` · ${parts.join(" · ")}` : ""}`);
  }
  // 武装候補が出ているのにサイクル封印が揃っていないのは、発注できない理由が
  // 経路側にあるという意味なので、ここで見せる。
  const display = view.display || {};
  if (view.scenario && display.cyclePaired === false) {
    return node("evaluation", "EVALUATION", sub, "WARN", "SEAL",
      `${display.cycleSealReason || "CYCLE_MISMATCH"} · ${parts.join(" · ") || "decision present"}`);
  }
  const word = d
    ? (d.model === "FLAT" ? "FLAT" : `${d.grade || "B"} ${String(d.state || "WATCH").toUpperCase()}`)
    : "EVALUATED";
  const blockers = Array.isArray(d?.hardBlockers) ? d.hardBlockers : [];
  const meta = blockers.length ? blockers.slice(0, 2).join(" / ") : "";
  return node("evaluation", "EVALUATION", sub, "UP", word,
    `${clock(evaluation.at)} · ${parts.join(" · ") || "decision present"}`, meta);
}

function cloudflareNode(view, runtime) {
  const sub = "worker · durable object";
  if (runtime.demo) return node("cloudflare", "CLOUDFLARE", sub, "UP", "DEMO", "server state is a local fiction");
  const seq = Number.isFinite(Number(view?.seq)) ? `seq ${view.seq}` : null;
  const rtt = runtime.ping && runtime.ping.ok && Number.isFinite(Number(runtime.ping.rttMs))
    ? `rtt ${Math.round(runtime.ping.rttMs)}ms` : null;
  const tail = [seq, rtt, runtime.clockSynced ? "clock synced" : null].filter(Boolean).join(" · ");
  switch (runtime.connection) {
    case "LIVE":
      return node("cloudflare", "CLOUDFLARE", sub, "UP", "LIVE", `websocket live${tail ? ` · ${tail}` : ""}`);
    case "CONNECTING":
      return node("cloudflare", "CLOUDFLARE", sub, "UNKNOWN", "CONNECTING", "opening websocket");
    case "RECONNECTING":
      return node("cloudflare", "CLOUDFLARE", sub, "WARN", "RECONNECTING",
        `${runtime.connectionDetail || "retrying"}${tail ? ` · ${tail}` : ""}`);
    case "OFFLINE":
      return node("cloudflare", "CLOUDFLARE", sub, "DOWN", "OFFLINE",
        runtime.connectionDetail || "state service unreachable");
    case "UNCONFIGURED":
      return node("cloudflare", "CLOUDFLARE", sub, "OFF", "NO API", "open from the bot to connect");
    default:
      return node("cloudflare", "CLOUDFLARE", sub, "UNKNOWN", "—", "");
  }
}

function deviceNode(runtime) {
  const sub = "mini app · this screen";
  if (runtime.demo) return node("device", "THIS DEVICE", sub, "WARN", "DEMO", "orders blocked on this screen");
  if (runtime.inTelegram && runtime.canSend) {
    return node("device", "THIS DEVICE", sub, "UP", "TELEGRAM",
      `${runtime.platform || "telegram"} · manual send ready`);
  }
  if (runtime.inTelegram) {
    return node("device", "THIS DEVICE", sub, "WARN", "TELEGRAM", "send bridge unavailable — auto only");
  }
  return node("device", "THIS DEVICE", sub, "WARN", "BROWSER", "no orders from here");
}

// ---------------------------------------------------------------- ORDER ROUTE

function armIsLive(arm, nowMs) {
  const expiresAt = parseMs(arm?.expiresAt);
  return Boolean(arm?.enabled === true && arm?.autotrade === true && arm?.live === true
    && expiresAt !== null && expiresAt > nowMs);
}

function armNode(view, nowMs) {
  const sub = "worker authority · 7h ttl";
  if (!view) return node("arm", "AUTO ARM", sub, "UNKNOWN", "NO VIEW", "waiting for server state");
  const arm = view.autotradeArm || null;
  if (!arm) return node("arm", "AUTO ARM", sub, "OFF", "OFF", "turn AUTO on in settings");
  const scope = Array.isArray(arm.accountScope) ? arm.accountScope.length : 0;
  if (armIsLive(arm, nowMs)) {
    const left = parseMs(arm.expiresAt) - nowMs;
    return node("arm", "AUTO ARM", sub, "UP", "ON",
      `until ${clock(arm.expiresAt)} (${ageText(left)} left) · ${scope} acct${scope === 1 ? "" : "s"}${arm.symbol ? ` · ${arm.symbol}` : ""}`);
  }
  if (arm.enabled === true) {
    return node("arm", "AUTO ARM", sub, "WARN", "EXPIRED",
      `expired ${clock(arm.expiresAt)} — new entries stopped`);
  }
  return node("arm", "AUTO ARM", sub, "OFF", "OFF",
    arm.disabledAt ? `off since ${clock(arm.disabledAt)}` : "turn AUTO on in settings");
}

/** renderAutotradePanel と同じ導出。エンジンの現在地を一語にする。 */
export function engineState(view) {
  const order = view?.order || null;
  const position = view?.position || null;
  const claim = view?.entryClaim || null;
  const management = view?.managementClaim || null;
  if (position && Number(position.qty) > 0) return "MANAGING";
  if (order && ["PENDING", "SENT", "UNKNOWN", "PARTIAL",
    "ENTRY_PARTIAL_ROUTE", "ENTRY_PARTIAL_FILL", "ENTRY_RESTING"].includes(String(order.state))) return "ROUTING";
  if (claim && ["CLAIMED", "CONSUMED"].includes(String(claim.state))) {
    return claim.staleReleasable === true ? "STALE" : "CLAIMED";
  }
  if (management && String(management.state) === "CONSUMED") return "MODIFY";
  return "IDLE";
}

function engineNode(view, health, nowMs) {
  const sub = "autotrade_engine · order.py";
  if (!view) return node("engine", "ENGINE", sub, "UNKNOWN", "NO VIEW", "waiting for server state");
  if (health.status === "HALT") {
    return node("engine", "ENGINE", sub, "DOWN", health.beacon?.kill ? "KILL" : "HALT",
      health.beacon?.kill ? "emergency stop — no new entries" : (health.beacon?.reason || "loop halted"));
  }
  const state = engineState(view);
  const claim = view.entryClaim || null;
  const route = claim?.routeState ? String(claim.routeState) : null;
  const attempts = Number.isFinite(Number(claim?.acceptedCount)) && Number.isFinite(Number(claim?.totalAttempts))
    ? `${claim.acceptedCount}/${claim.totalAttempts} routed` : null;
  const meta = [route ? `route ${route}` : null, attempts].filter(Boolean).join(" · ");
  const position = view.position || null;
  switch (state) {
    case "MANAGING":
      return node("engine", "ENGINE", sub, "UP", "IN TRADE",
        `holding ${position?.side || ""} ${position?.qty ?? ""}${position?.initialQty && position.qty < position.initialQty ? ` of ${position.initialQty}` : ""} · minding stop & targets`, meta);
    case "ROUTING":
      return node("engine", "ENGINE", sub, "UP", "SENDING",
        `order ${view.order?.state || ""} · waiting for broker confirm`, meta);
    case "CLAIMED":
      return node("engine", "ENGINE", sub, "UP", "RESERVED", "entry slot reserved · one order at most", meta);
    case "STALE":
      return node("engine", "ENGINE", sub, "WARN", "STALE SLOT", "old reservation — clears on the next entry", meta);
    case "MODIFY":
      return node("engine", "ENGINE", sub, "UP", "MOVING STOP", "re-placing stop and target orders", meta);
    default: {
      const live = armIsLive(view.autotradeArm, nowMs);
      if (health.status === "OFF_DUTY") return node("engine", "ENGINE", sub, "OFF", "OFF DUTY", "outside the monitor window", meta);
      return live
        ? node("engine", "ENGINE", sub, "UP", "STANDBY", "nothing in flight · waiting for a cleared setup", meta)
        : node("engine", "ENGINE", sub, "OFF", "IDLE", "auto off · nothing in flight", meta);
    }
  }
}

function brokerNode(view, nowMs) {
  const sub = "crosstrade → tradovate";
  if (!view) return node("broker", "BROKER", sub, "UNKNOWN", "NO VIEW", "waiting for server state");
  const check = view.positionCheck || null;
  const position = view.position || null;
  const observation = view.brokerObservation || null;
  const platform = observation?.platform ? String(observation.platform) : null;
  const verifiedAt = view.lastVerifiedAt || check?.observedAt || observation?.observedAt || null;
  const ageMs = parseMs(verifiedAt) === null ? null : Math.max(0, nowMs - parseMs(verifiedAt));
  const holding = position && Number(position.qty) > 0
    ? `${position.side || ""} ${position.qty}` : "FLAT";
  const meta = [platform, check?.source ? String(check.source) : null].filter(Boolean).join(" · ");
  if (position && String(position.state) === "STALE") {
    return node("broker", "BROKER", sub, "DOWN", "STALE",
      `position query failed · kept as ${holding}`, meta);
  }
  if (!check) return node("broker", "BROKER", sub, "UNKNOWN", "NO CHECK", "no position verification yet", meta);
  if (check.verified !== true) {
    return node("broker", "BROKER", sub, "DOWN", "UNVERIFIED", "broker did not answer · no new orders", meta);
  }
  if (ageMs !== null && ageMs > BROKER_AGING_MS) {
    return node("broker", "BROKER", sub, "WARN", "AGING",
      `verified ${clock(verifiedAt, { seconds: true })} (${ageText(ageMs)} ago) · ${holding}`, meta);
  }
  return node("broker", "BROKER", sub, "UP", "VERIFIED",
    `${clock(verifiedAt, { seconds: true })} · ${holding}`, meta);
}

function accountsNode(view) {
  const sub = "roster · lifeline";
  if (!view) return node("accounts", "ACCOUNTS", sub, "UNKNOWN", "NO VIEW", "waiting for server state");
  const accounts = view.accounts || null;
  const list = Array.isArray(accounts?.list) ? accounts.list : [];
  if (!accounts || !list.length) return node("accounts", "ACCOUNTS", sub, "UNKNOWN", "NO ROSTER", "accounts not published");
  const total = Number.isFinite(Number(accounts.totalBuffer))
    ? `$${Math.round(Number(accounts.totalBuffer)).toLocaleString("en-US")} left` : null;
  const count = `${list.length} acct${list.length === 1 ? "" : "s"}`;
  const sync = accounts.sync && typeof accounts.sync === "object" ? accounts.sync : null;
  const tail = (id) => `…${String(id).slice(-6)}`;
  const meta = accounts.observedAt ? `seen ${clock(accounts.observedAt)}` : "";
  if (sync?.verified === true) {
    const missing = Array.isArray(sync.missing) ? sync.missing : [];
    const dead = Array.isArray(sync.dead) ? sync.dead : [];
    const unknown = Array.isArray(sync.unknown) ? sync.unknown : [];
    if (missing.length || dead.length) {
      const gone = [...missing, ...dead].map(tail).join(", ");
      return node("accounts", "ACCOUNTS", sub, "DOWN", "MISSING",
        `${missing.length + dead.length} configured gone from broker (${gone}) — do not send`, meta);
    }
    if (unknown.length) {
      return node("accounts", "ACCOUNTS", sub, "WARN", "NEW ON BROKER",
        `${count} · ${total || ""} · ${unknown.length} unconfigured (${unknown.map(tail).join(", ")})`, meta);
    }
    return node("accounts", "ACCOUNTS", sub, "UP", count.toUpperCase(),
      `roster matches broker${total ? ` · ${total}` : ""}`, meta);
  }
  return node("accounts", "ACCOUNTS", sub, "WARN", "UNCHECKED",
    `${count}${total ? ` · ${total}` : ""} · roster not cross-checked`, meta);
}

// ---------------------------------------------------------------- 束ねる

function linkBetween(a, b) {
  if (a.state === "DOWN" || b.state === "DOWN") return "broken";
  if (a.state === "UP" && b.state === "UP") return "flow";
  if (a.state === "OFF" || b.state === "OFF") return "idle";
  return "dim";
}

function withLinks(nodes) {
  return nodes.map((item, index) => ({
    ...item,
    linkAfter: index < nodes.length - 1 ? linkBetween(item, nodes[index + 1]) : null,
  }));
}

/**
 * @param {object|null} view     Worker の view(無ければ全ノード UNKNOWN)
 * @param {object} runtime       { connection, connectionDetail, appOffline, inTelegram, canSend,
 *                                 platform, demo, clockSynced, ping }
 * @param {number} nowMs         serverNow()
 */
export function deriveRoute(view, runtime = {}, nowMs = Date.now()) {
  const safeRuntime = runtime && typeof runtime === "object" ? runtime : {};
  const health = evaluateCycleHealth(view, nowMs);
  const data = withLinks([
    tradingviewNode(view, health),
    loopNode(view, health, safeRuntime),
    evaluationNode(view, health, nowMs),
    cloudflareNode(view, safeRuntime),
    deviceNode(safeRuntime),
  ]);
  const order = withLinks([
    armNode(view, nowMs),
    engineNode(view, health, nowMs),
    brokerNode(view, nowMs),
    accountsNode(view),
  ]);
  const all = [...data, ...order];
  const counts = { UP: 0, WARN: 0, DOWN: 0, OFF: 0, UNKNOWN: 0 };
  for (const item of all) counts[item.state] += 1;
  let verdict;
  let tone;
  if (counts.DOWN) { verdict = `${counts.DOWN} LINK${counts.DOWN === 1 ? "" : "S"} DOWN`; tone = "down"; }
  // view が無いときは、端末が BROWSER だという WARN を総評にしない。
  // 何も届いていない事実のほうが先。
  else if (!view) {
    verdict = safeRuntime.connection === "UNCONFIGURED" ? "NO STATE SERVICE" : "NO LINK DATA";
    tone = "unknown";
  }
  else if (counts.WARN) { verdict = `${counts.WARN} WARNING${counts.WARN === 1 ? "" : "S"}`; tone = "warn"; }
  else if (counts.UP === 0 && health.status === "OFF_DUTY") { verdict = "OFF DUTY"; tone = "off"; }
  else if (counts.UP === 0) { verdict = "NO LINK DATA"; tone = "unknown"; }
  else if (counts.UP === all.length) { verdict = "ALL LINKS UP"; tone = "up"; }
  else { verdict = `${counts.UP}/${all.length} LINKS UP`; tone = "up"; }
  return {
    lanes: [
      { id: "data", title: "DATA ROUTE", subtitle: "chart → decision → this screen", nodes: data },
      { id: "order", title: "ORDER ROUTE", subtitle: "authority → engine → broker", nodes: order },
    ],
    counts,
    total: all.length,
    verdict,
    tone,
    health,
  };
}

// ---------------------------------------------------------------- 描画

/** ノードの状態と語だけの署名。同じなら DOM を作り直さず文字だけ差し替える。 */
export function routeSignature(model) {
  return model.lanes.map((lane) => lane.nodes.map((item) => `${item.id}:${item.state}:${item.word}:${item.linkAfter || ""}`).join("|")).join("||");
}

function nodeHtml(item, index) {
  const link = item.linkAfter
    ? `<li class="route-link is-${item.linkAfter}" aria-hidden="true"><i></i>${
      item.linkAfter === "broken" ? `<b>×</b>` : ""}</li>`
    : "";
  return `
    <li class="route-node is-${item.state.toLowerCase()}" data-node="${esc(item.id)}" style="--i:${index}">
      <span class="route-led" aria-hidden="true"><i></i></span>
      <div class="route-body">
        <div class="route-name"><b>${esc(item.name)}</b><span class="route-sub">${esc(item.sub)}</span></div>
        <div class="route-word" data-route="word">${esc(item.word)}</div>
        <p class="route-detail" data-route="detail">${esc(item.detail)}</p>
        <p class="route-meta" data-route="meta" ${item.meta ? "" : "hidden"}>${esc(item.meta)}</p>
      </div>
    </li>${link}`;
}

function laneHtml(lane) {
  return `
    <section class="route-lane" data-lane="${esc(lane.id)}" aria-label="${esc(lane.title)}">
      <header class="route-lane-head">
        <span class="micro">${esc(lane.title)}</span>
        <span class="micro route-lane-sub">${esc(lane.subtitle)}</span>
      </header>
      <ol class="route-nodes">${lane.nodes.map(nodeHtml).join("")}</ol>
    </section>`;
}

function pulseHtml(model, runtime, nowMs) {
  const cells = [];
  const conn = runtime.demo ? "DEMO" : (runtime.connection || "—");
  cells.push(`<span><i class="micro">LINK</i><b>${esc(conn)}</b></span>`);
  if (Number.isFinite(Number(runtime.seq))) cells.push(`<span><i class="micro">SEQ</i><b>${esc(runtime.seq)}</b></span>`);
  if (runtime.ping?.ok && Number.isFinite(Number(runtime.ping.rttMs))) {
    cells.push(`<span><i class="micro">RTT</i><b>${esc(Math.round(runtime.ping.rttMs))}<small>ms</small></b></span>`);
  } else if (runtime.ping && runtime.ping.ok === false) {
    cells.push(`<span class="is-bad"><i class="micro">RTT</i><b>FAIL</b></span>`);
  }
  const lastCycle = model.health?.lastAtMs ? formatJstClock(model.health.lastAtMs, { seconds: false }) : "—";
  cells.push(`<span><i class="micro">CYCLE</i><b>${esc(lastCycle)}</b></span>`);
  cells.push(`<span><i class="micro">JST</i><b data-route="clock">${esc(formatJstClock(nowMs))}</b></span>`);
  return `<div class="route-pulse">${cells.join("")}</div>`;
}

/**
 * host に経路図を描く。状態の署名が同じなら DOM を作り直さず、
 * 文字(経過秒・時計)だけを差し替える —— 流れる線のアニメーションを
 * 毎秒リセットしないため。
 */
export function renderSystemView(host, model, { runtime = {}, nowMs = Date.now() } = {}) {
  if (!host || !model) return false;
  const signature = routeSignature(model);
  if (host.dataset.signature === signature && host.firstElementChild) {
    for (const lane of model.lanes) {
      for (const item of lane.nodes) {
        const row = host.querySelector(`.route-node[data-node="${item.id}"]`);
        if (!row) continue;
        const detail = row.querySelector('[data-route="detail"]');
        if (detail && detail.textContent !== item.detail) detail.textContent = item.detail;
        const meta = row.querySelector('[data-route="meta"]');
        if (meta) {
          meta.hidden = !item.meta;
          if (meta.textContent !== item.meta) meta.textContent = item.meta;
        }
      }
    }
    const clockNode = host.querySelector('[data-route="clock"]');
    if (clockNode) clockNode.textContent = formatJstClock(nowMs);
    const rtt = host.querySelector(".route-pulse");
    if (rtt && runtime.ping?.ok && Number.isFinite(Number(runtime.ping.rttMs))) {
      const cell = [...rtt.querySelectorAll("span")].find((el) => el.querySelector("i")?.textContent === "RTT");
      const value = cell?.querySelector("b");
      if (value) value.innerHTML = `${Math.round(runtime.ping.rttMs)}<small>ms</small>`;
    }
    return false;
  }
  host.dataset.signature = signature;
  const counts = model.counts;
  host.innerHTML = `
    <div class="route-summary tone-${esc(model.tone)}">
      <div class="route-summary-head">
        <span class="micro">SYSTEM ROUTE</span>
        <span class="micro route-count">${esc(counts.UP)}/${esc(model.total)} UP${counts.WARN ? ` · ${counts.WARN} WARN` : ""}${counts.DOWN ? ` · ${counts.DOWN} DOWN` : ""}</span>
      </div>
      <strong class="route-verdict" data-type>${esc(model.verdict)}</strong>
      <i class="route-scan" aria-hidden="true"></i>
    </div>
    ${pulseHtml(model, runtime, nowMs)}
    <div class="route-lanes">${model.lanes.map(laneHtml).join("")}</div>
    <p class="route-note">derived from the published state only · nothing here sends orders</p>`;
  return true;
}
