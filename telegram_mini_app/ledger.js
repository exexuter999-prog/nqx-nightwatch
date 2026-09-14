import { glyph } from "./glyphs.js";
/**
 * NQX LEDGER — 決済記録と口座残機(LIFELINE)の表示層。
 *
 * 導出規則は result.js の derive() と同一(COST_FLOOR_PT=2、±2pt は FLAT)。
 * サーバーは pnl / R / state を送らない契約なので、ここで毎回同じ式から
 * 導出する。式を変えるときは result.js と必ず同時に変えること。
 *
 * 純関数(deriveTrade / participation / groupByDay / equitySeries)は DOM を
 * 触らない。node --test から直接検証できる。
 */

export const COST_FLOOR_PT = 2;
export const POINT_VALUE = 2.0; // MNQ 1pt = $2.00

// ---------------------------------------------------------------- 導出(純関数)

export function deriveTrade(result) {
  const dir = result.side === "SHORT" ? -1 : 1;
  const pts = (result.exit - result.entry) * dir;
  // fees は「価格から出せない観測値」(滑り+手数料、1口座分)。実約定価格を
  // 取得する経路が無いので、価格由来は粗損益にしかならない。あれば必ず引く
  // —— 引かないと LEDGER と残高が食い違う(2026-08-25)。
  const fees = Number.isFinite(result.fees) ? Math.abs(result.fees) : 0;
  const usd = pts * result.pointValue * result.qty - fees;
  // R56: 凍結プランの無い手動建玉(ブローカー約定から組んだ記録)は stop を持たない。
  // R は「受け入れたリスク」に対する比なので、stop が無ければ出さない(— 表示)。
  const riskPt = Number.isFinite(result.stop) ? Math.abs(result.entry - result.stop) : 0;
  const r = riskPt ? pts / riskPt : null;
  const state = Math.abs(pts) <= COST_FLOOR_PT ? "flat" : (pts > 0 ? "win" : "loss");
  const heldMs = Math.max(0, Date.parse(result.closedAt) - Date.parse(result.openedAt));
  return { pts, usd, r, state, heldMs };
}

export function formatHeld(heldMs) {
  const secs = Math.floor(heldMs / 1000);
  if (secs >= 3600) return `${Math.floor(secs / 3600)}h ${String(Math.floor((secs % 3600) / 60)).padStart(2, "0")}m`;
  return `${Math.floor(secs / 60)}m ${String(secs % 60).padStart(2, "0")}s`;
}

/**
 * シナリオに参加できる口座を判定する。
 *
 * order.py の split_accounts_by_risk と同じ規則: リスク$が口座の
 * RISK_<口座ID>(cap)以下なら参加。残機(buffer)は参加判定に使わない
 * (order.py が見ないものをここで見ると、表示と実際の発注が食い違う)。
 */
export function participation(scenario, accounts) {
  const list = accounts?.list;
  if (!Array.isArray(list) || !list.length) return null;
  const riskPer = Math.abs(scenario.entry - scenario.stop) * POINT_VALUE * scenario.qty;
  const rewardPer = Math.abs(scenario.target - scenario.entry) * POINT_VALUE * scenario.qty;
  const eligible = list.filter((account) => riskPer <= account.cap + 1e-9);
  const excluded = list.filter((account) => riskPer > account.cap + 1e-9);
  return {
    riskPer,
    rewardPer,
    eligible,
    excluded,
    tpTotal: rewardPer * eligible.length,
    slTotal: riskPer * eligible.length,
  };
}

/** 決済記録を JST の日付でまとめる。入力は新しい順(recentResults)を想定。 */
export function groupByDay(results) {
  const groups = [];
  const index = new Map();
  for (const result of results) {
    const day = new Date(Date.parse(result.closedAt)).toLocaleDateString("ja-JP", {
      timeZone: "Asia/Tokyo", month: "2-digit", day: "2-digit", weekday: "short",
    });
    if (!index.has(day)) {
      index.set(day, { day, trades: [], usd: 0, wins: 0, losses: 0, flats: 0 });
      groups.push(index.get(day));
    }
    const group = index.get(day);
    const derived = deriveTrade(result);
    group.trades.push({ result, derived });
    group.usd += derived.usd;
    if (derived.state === "win") group.wins += 1;
    else if (derived.state === "loss") group.losses += 1;
    else group.flats += 1;
  }
  return groups;
}

/** エクイティカーブ(古い順の累積 $)。入力は新しい順を想定し、内部で反転する。 */
export function equitySeries(results) {
  const chronological = results.slice().reverse();
  let sum = 0;
  return chronological.map((result) => {
    sum += deriveTrade(result).usd;
    return sum;
  });
}

/**
 * R85: 集計に使う記録を **いま設定されている口座**に絞り、決済時刻の新しい順に並べる。
 *
 * 2026-09-13、LEDGER の NET が +$6,508 と出ていたが、口座の実際の純増は +$3,657 だった。
 * 差の大半は口座の入れ替え前(評価口座・8 月の手入力)の記録で、今の口座の残高とは
 * 無関係な数字が合算されていた。口座名簿(accounts.list)が届いていないときは絞らない
 * (絞る根拠が無いのに記録を消さない)。外した記録は `others` として数を見せる。
 *
 * 並べ直すのは、Worker の resultLog が **publish 順**だから。訂正のために送り直した
 * 記録は末尾(= 最新)へ移るので、そのまま描くと日付の並びが崩れる。
 */
export function scopeResults(results, accounts) {
  const list = (Array.isArray(results) ? results : [])
    .filter((result) => result && Number.isFinite(Date.parse(result.closedAt)))
    .slice()
    .sort((a, b) => Date.parse(b.closedAt) - Date.parse(a.closedAt));
  const ids = new Set((Array.isArray(accounts?.list) ? accounts.list : [])
    .map((account) => String(account?.id ?? ""))
    .filter(Boolean));
  if (!ids.size) return { scoped: list, others: [] };
  const scoped = [];
  const others = [];
  for (const result of list) {
    (ids.has(String(result.accountId ?? "")) ? scoped : others).push(result);
  }
  return { scoped, others };
}

/**
 * R85: 残機カードの主役の数字。
 *
 * **Balance はブローカーの純資産(equity = netLiq)**。全口座の純資産が届いているときだけ
 * 合計して出す。1 口座でも欠けていれば偽の残高を作らず、DD 残(buffer)を「DD left」
 * として出す。以前はここに buffer(トレーリング DD の床までの距離)を「Balance」と
 * 書いて出していたので、口座の残高 $53,657 と見比べると $3,683 という一致しない数字に
 * 見えていた(2026-09-13)。buffer 自体は正しい量で、弾数ゲージと ULTRA が使う。
 */
export function lifelineHeadline(accounts) {
  const list = Array.isArray(accounts?.list) ? accounts.list : [];
  const bufferTotal = Number.isFinite(Number(accounts?.totalBuffer))
    ? Number(accounts.totalBuffer)
    : list.reduce((sum, account) => sum + (Number.isFinite(Number(account?.buffer)) ? Number(account.buffer) : 0), 0);
  // Number(null) は 0 になる(R73 の罠)。欠けた値を 0 円の口座として足さない。
  const equities = list.map((account) => (
    account?.equity === undefined || account?.equity === null || account?.equity === ""
      ? NaN : Number(account.equity)));
  if (list.length && equities.every(Number.isFinite)) {
    return { label: "Balance", value: equities.reduce((sum, value) => sum + value, 0), buffer: bufferTotal };
  }
  return { label: "DD left", value: bufferTotal, buffer: null };
}

/** 直近 N 日の実現損益合計。記録が無ければ null(0 と区別する)。 */
export function recentNet(results, days, nowMs) {
  if (!Array.isArray(results) || !results.length) return null;
  const floor = nowMs - days * 86_400_000;
  const window_ = results.filter((result) => Date.parse(result.closedAt) >= floor);
  if (!window_.length) return null;
  return window_.reduce((sum, result) => sum + deriveTrade(result).usd, 0);
}

// ---------------------------------------------------------------- 表示ユーティリティ

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]
  ));
}

/** 符号を必ず出す $ 表記。マイナスは U+2212(数字と同幅のマイナス)。 */
export function signedMoney(value) {
  const sign = value < 0 ? "−" : "+";
  return `${sign}$${Math.abs(value).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
}

// ---------------------------------------------------------------- LIFELINE 描画

/** 弾数ゲージに出すピップの上限。これを超える分は「+」で示す。 */
const PIP_CAP = 14;

function pipGauge(account) {
  const shots = account.cap > 0 ? Math.floor(account.buffer / account.cap) : 0;
  if (shots <= 0) return `<span class="pips"><i class="more">0</i></span>`;
  const shown = Math.min(shots, PIP_CAP);
  const pips = Array.from({ length: shown }, (_, i) => `<i style="--i:${i}"></i>`).join("");
  const more = shots > PIP_CAP ? `<i class="more" style="--i:${shown}">+</i>` : "";
  return `<span class="pips" title="${shots} shots at max risk">${pips}${more}</span>`;
}

/** 数字のカウントアップ。減速イージングで着地する。reduced-motion では即値。 */
function countUp(node, from, to, ms = 650) {
  if (!node) return;
  if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches || from === to) {
    node.textContent = "$" + Math.round(to).toLocaleString("en-US");
    return;
  }
  const startAt = performance.now();
  const step = (now) => {
    const t = Math.min(1, (now - startAt) / ms);
    const eased = 1 - (1 - t) ** 3;
    node.textContent = "$" + Math.round(from + (to - from) * eased).toLocaleString("en-US");
    if (t < 1) window.requestAnimationFrame(step);
  };
  window.requestAnimationFrame(step);
}

/**
 * 残機(LIFELINE)。総残高を主役の巨大数字で、口座別は弾数ゲージで見せる。
 * ピップ1つ = その口座の上限リスク1回ぶん(buffer ÷ cap)。
 */
export function renderLifeline(host, accounts, { weeklyUsd = null } = {}) {
  if (!host) return;
  if (!accounts || !Array.isArray(accounts.list) || !accounts.list.length) {
    host.hidden = true;
    return;
  }
  host.hidden = false;

  const cells = accounts.list.map((account) => `
    <div class="lifeline-cell${account.buffer <= 0 ? " is-drained" : ""}">
      <span class="acct">${esc(account.label)}</span>
      <b class="buf">${esc(Math.round(account.buffer).toLocaleString("en-US"))}</b>
      ${pipGauge(account)}
    </div>`).join("");

  // 口座名簿の突合(再同期)。設定済み口座がブローカーから消えた(missing)は
  // 発注が宛先不明で失敗する事故(実測2回)なので最優先で警告する。
  // unknown は「CrossTrade に新しい口座がある」お知らせ。
  const sync = accounts.sync && typeof accounts.sync === "object" ? accounts.sync : null;
  const syncWarnings = [];
  if (sync?.verified === true) {
    const tail = (id) => `…${String(id).slice(-6)}`;
    if (Array.isArray(sync.missing) && sync.missing.length) {
      syncWarnings.push(`<p class="lifeline-sync is-missing" data-type>⚠ ${sync.missing.length} CONFIGURED ACCOUNT${sync.missing.length === 1 ? "" : "S"} GONE FROM BROKER (${esc(sync.missing.map(tail).join(", "))}) — DO NOT SEND</p>`);
    }
    if (Array.isArray(sync.dead) && sync.dead.length) {
      syncWarnings.push(`<p class="lifeline-sync is-missing" data-type>⚠ ACCOUNT DEAD/DISABLED: ${esc(sync.dead.map(tail).join(", "))}</p>`);
    }
    if (Array.isArray(sync.unknown) && sync.unknown.length) {
      syncWarnings.push(`<p class="lifeline-sync is-new">NEW ON BROKER: ${esc(sync.unknown.map(tail).join(", "))} — not configured, not traded</p>`);
    }
  }

  const weekly = weeklyUsd == null
    ? ""
    : `<p>7D <span class="${weeklyUsd < 0 ? "pain" : "gain"}">${esc(signedMoney(weeklyUsd)).replace("$", "")}</span></p>`;

  // 目盛り(計器の飾りではなく、10分割の定規。数字の下に敷く)
  const ticks = Array.from({ length: 10 }, (_, i) => {
    const x = 2 + i * ((100 - 4) / 9);
    const major = i % 3 === 0;
    return `<line x1="${x}%" y1="${major ? 0 : 2.5}" x2="${x}%" y2="8"/>`;
  }).join("");

  const headline = lifelineHeadline(accounts);
  const nextTotal = Math.round(headline.value);
  const ddLeft = headline.buffer === null
    ? ""
    : `<p>DD LEFT $${esc(Math.round(headline.buffer).toLocaleString("en-US"))}</p>`;
  const prevTotal = Number(host.dataset.total);
  const changed = !Number.isFinite(prevTotal) || prevTotal !== nextTotal;
  host.dataset.total = String(nextTotal);
  // 値が変わったときだけ演出を回す(ポーリングのたびに点滅させない)。
  host.classList.toggle("animate", changed);

  host.innerHTML = `
    <div class="lifeline-hero">
      <div>
        <span class="micro">${esc(headline.label)}</span>
        <strong>$${esc(nextTotal.toLocaleString("en-US"))}</strong>
      </div>
      <div class="lifeline-meta">
        <p>${accounts.list.length} ACCTS</p>
        ${ddLeft}
        ${weekly}
      </div>
    </div>
    <div class="ruler-wrap">
      <svg class="lifeline-ruler" aria-hidden="true">${ticks}</svg>
      <i class="ruler-scan" aria-hidden="true"></i>
    </div>
    <div class="lifeline-strip">${cells}</div>
    ${syncWarnings.join("")}`;

  if (changed) {
    countUp(
      host.querySelector(".lifeline-hero strong"),
      Number.isFinite(prevTotal) ? prevTotal : 0,
      nextTotal,
    );
  }
}

// ---------------------------------------------------------------- 参加口座行

/** シナリオカード内に差し込む「MNQ 1 × 2口座 · +200 / −108 · …0004 外」の行。 */
export function participationLine(scenario, accounts) {
  const part = participation(scenario, accounts);
  if (!part) return "";
  if (!part.eligible.length) {
    return `<p class="participation is-blocked">ALL ACCOUNTS OVER RISK CAP (−$${Math.round(part.riskPer)} each)</p>`;
  }
  const excluded = part.excluded.length
    ? ` · ${part.excluded.map((account) => `${esc(account.label)} out`).join(" · ")}`
    : "";
  return `
    <p class="participation">
      <b>MNQ ${esc(String(scenario.qty))} × ${part.eligible.length}</b>
      · <b class="gain">+${Math.round(part.tpTotal)}</b> / <b class="pain">−${Math.round(part.slTotal)}</b>${excluded}
    </p>`;
}

// ---------------------------------------------------------------- LEDGER 描画

function sparkSvg(series) {
  if (series.length < 2) return "";
  const width = 320;
  const height = 44;
  const pad = 3;
  const min = Math.min(0, ...series);
  const max = Math.max(0, ...series);
  const span = max - min || 1;
  const x = (i) => pad + (i / (series.length - 1)) * (width - pad * 2);
  const y = (v) => height - pad - ((v - min) / span) * (height - pad * 2);
  const points = series.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const zero = y(0).toFixed(1);
  const last = series[series.length - 1];
  return `
    <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
      <line x1="0" y1="${zero}" x2="${width}" y2="${zero}" class="spark-zero"/>
      <polyline points="${points}" pathLength="1" class="spark-line${last < 0 ? " is-under" : ""}"/>
    </svg>`;
}

// R48: モデル別スコアカード(表示)。result.model は凍結プラン由来で、
// 無い記録(手動・R48以前)は集計から除外して UNATTRIBUTED 扱いにする。
const MODEL_SHORT = {
  VP80_REVERSION: "VP80",
  TURTLE_SOUP_REVERSAL: "SOUP",
  BREAKER_CONTINUATION: "BRKR",
  OTE_FVG_PULLBACK: "OTE",
};

export function modelShort(model) {
  return MODEL_SHORT[model] || String(model || "").slice(0, 6);
}

/** モデル別の N / 勝敗 / 粗PF / 合計$。PF は損失ゼロの間は null(∞を発明しない)。 */
export function modelStats(results) {
  const buckets = new Map();
  for (const result of results || []) {
    if (!result?.model) continue;
    const derived = deriveTrade(result);
    const bucket = buckets.get(result.model)
      || { model: result.model, n: 0, wins: 0, losses: 0, usd: 0, profit: 0, loss: 0 };
    bucket.n += 1;
    bucket.usd += derived.usd;
    if (derived.state === "win") { bucket.wins += 1; bucket.profit += Math.max(0, derived.usd); }
    if (derived.state === "loss") { bucket.losses += 1; bucket.loss += Math.abs(Math.min(0, derived.usd)); }
    buckets.set(result.model, bucket);
  }
  return [...buckets.values()]
    .map((bucket) => ({ ...bucket, pf: bucket.loss > 1e-9 ? bucket.profit / bucket.loss : null }))
    .sort((a, b) => b.n - a.n);
}

/**
 * LEDGER 本体。recentResults(新しい順)を日別にまとめて描画する。
 * onOpen(result) は行タップで呼ばれ、既存のリザルトカードを再表示する。
 */
export function renderLedger(host, results, { onOpen, others = [] } = {}) {
  if (!host) return;
  if (!Array.isArray(results) || !results.length) {
    host.innerHTML = `
      <article class="state-card empty">
        <strong>${glyph("closed")}NO RECORDS</strong>
        <p>Closed trades appear here.</p>
      </article>`;
    return;
  }

  const total = results.reduce((sum, result) => sum + deriveTrade(result).usd, 0);
  const wins = results.filter((result) => deriveTrade(result).state === "win").length;
  const losses = results.filter((result) => deriveTrade(result).state === "loss").length;
  const series = equitySeries(results);

  // 件数が変わったときだけ入場演出(タブ切替のたびに再生しない)。
  const entriesChanged = host.dataset.count !== String(results.length);
  host.dataset.count = String(results.length);
  host.classList.toggle("animate", entriesChanged);

  const groups = groupByDay(results).map((group) => {
    const rows = group.trades.map(({ result, derived }, index) => {
      const time = new Date(Date.parse(result.closedAt)).toLocaleTimeString("ja-JP", {
        timeZone: "Asia/Tokyo", hour: "2-digit", minute: "2-digit",
      });
      return `
        <button class="ledger-row is-${derived.state}" style="--i:${results.indexOf(result)}" data-result-index="${esc(String(results.indexOf(result)))}">
          <span class="ledger-mark">${derived.state === "win" ? glyph("up") : derived.state === "loss" ? glyph("down") : glyph("position")}</span>
          <span class="ledger-side">${esc(result.side)} ${esc(String(result.qty))}</span>
          <span class="ledger-path">${esc(String(result.entry.toFixed(2)))} → ${esc(String(result.exit.toFixed(2)))}</span>
          <span class="ledger-r">${derived.r == null ? "—" : `${derived.r >= 0 ? "+" : "−"}${Math.abs(derived.r).toFixed(2)}R`}</span>
          <span class="ledger-usd">${esc(signedMoney(derived.usd))}</span>
          <span class="ledger-time">${esc(time)} · ${esc(formatHeld(derived.heldMs))}${result.mode === "LIVE" ? " · LIVE" : ""}${
            result.model ? ` · ${esc(modelShort(result.model))}${result.grade ? ` ${esc(result.grade)}` : ""}` : ""}</span>
        </button>`;
    }).join("");
    return `
      <section class="ledger-day">
        <header class="ledger-day-head">
          <span>${esc(group.day)}</span>
          <span>${group.wins}W ${group.losses}L${group.flats ? ` ${group.flats}F` : ""} · <b class="${group.usd < 0 ? "pain" : "gain"}">${esc(signedMoney(group.usd))}</b></span>
        </header>
        ${rows}
      </section>`;
  }).join("");

  host.innerHTML = `
    <div class="ledger-summary">
      <div class="ledger-stat">
        <span class="micro">NET</span>
        <strong class="${total < 0 ? "pain" : "gain"}">${esc(signedMoney(total))}</strong>
      </div>
      <div class="ledger-stat">
        <span class="micro">W / L</span>
        <strong>${wins}W ${losses}L</strong>
      </div>
      <div class="ledger-stat">
        <span class="micro">WIN RATE</span>
        <strong>${wins + losses ? Math.round((wins / (wins + losses)) * 100) : "—"}%</strong>
      </div>
    </div>
    <div class="equity-spark">${sparkSvg(series)}</div>
    ${(() => {
      const stats = modelStats(results);
      if (!stats.length) return "";
      const rows = stats.map((row) => `
        <div class="ledger-model-row">
          <span class="lm-name">${esc(modelShort(row.model))}</span>
          <span class="lm-n">${row.n}</span>
          <span class="lm-wl">${row.wins}W ${row.losses}L</span>
          <span class="lm-pf">${row.pf === null ? "PF —" : `PF ${row.pf.toFixed(2)}`}</span>
          <span class="lm-usd ${row.usd < 0 ? "pain" : "gain"}">${esc(signedMoney(row.usd))}</span>
        </div>`).join("");
      return `
        <div class="ledger-models">
          <div class="section-label"><span class="micro">BY MODEL</span>
            <span class="micro">visible records only</span></div>
          ${rows}
        </div>`;
    })()}
    ${groups}
    <p class="ledger-note">last ${results.length} · derived from records${
      Array.isArray(others) && others.length
        ? ` · ${others.length} from other accounts not counted`
        : ""}</p>`;

  if (typeof onOpen === "function") {
    host.querySelectorAll("[data-result-index]").forEach((node) => {
      node.addEventListener("click", () => {
        const result = results[Number(node.dataset.resultIndex)];
        if (result) onOpen(result);
      });
    });
  }
}
