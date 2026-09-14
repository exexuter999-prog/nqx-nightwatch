import { initTapeChart } from "./chart.js";
import { CONNECTION, consumeLaunchParams, createStateClient } from "./state_client.js";
import { initResultView } from "./result.js";
import { CUES, CUE_TEXT, createSoundCue } from "./sound.js";
import { deriveTrade, participation, participationLine, recentNet, renderLedger, renderLifeline, scopeResults } from "./ledger.js";
// R66: 期待損益(表示専用)。判定はせず、カードに出ている価格・枚数の掛け算だけ。
import { direction as sideDirection, positionProjectionHtml, scenarioProjectionHtml } from "./projection.js";
import { buildUnstickPayload, stuckOrderInfo } from "./unstick.js";
import { STICKERS } from "./stickers.js";
// R6: シナリオ評価の描画。判定は行わず、受け取った evaluation を描くだけ。
import { decisionBanner, evalChecklist, gateBoard, gradeContradicts, num } from "./evalcard.js";
import { buildPlan as buildUltraPlan } from "./ultra.js";
import { typeAll, typeText, applyCaretVariant, replayAll, assignCarets, CARET_STORAGE_KEY } from "./typewriter.js";

/**
 * R62: 最後のカーソルの型を状況で割り振る(2026-09-06 ユーザー「状況に応じて適当に割り振って」)。
 * 最初に当たった規則の型。武装して送れるシナリオは照準、監視・GATES・待機は目、建玉はローソク足、
 * 送信中の注文はグリッチ。SYSTEM の総合判定は全部 UP なら ◆、WARN は火、DOWN はグリッチ。
 * 自動発注の帯は管理中ならローソク足、経路・claim 中はグリッチ。表示だけの印で、判定には無関係。
 */
const CARD_CARETS = [
  [".state-card.scenario.armed", "reticle"],
  [".state-card.scenario", "eye"],
  [".state-card.position", "candle"],
  [".state-card.order", "glitch"],
  [".state-card.gates", "eye"],
  [".state-card.empty", "eye"],
];
const SYSTEM_CARETS = [
  [".route-summary.tone-up", "diamond"],
  [".route-summary.tone-warn", "ember"],
  [".route-summary.tone-down", "glitch"],
];
const AUTOTRADE_CARETS = [
  ["#autotradePanel:has(.autotrade-managing)", "candle"],
  ["#autotradePanel:has(.autotrade-routing)", "glitch"],
  ["#autotradePanel:has(.autotrade-claimed)", "glitch"],
  ["#autotradePanel:has(.autotrade-modify)", "glitch"],
];
// R64: カード背景の鉄粉(表示専用。ポインタに反応する canvas を面の上・本文の下に敷く)。
import { attachCardFields } from "./cardfield.js";
import { glyph } from "./glyphs.js";
import { initLogo } from "./logo.js";
import { createServerClock, formatJstClock } from "./clock.js";
// 監視ループの健全性(「監視の監視」)。表示専用の純関数。
import { evaluateCycleHealth } from "./cyclehealth.js";
// R55: 経路の接続状況(SYSTEM ROUTE)。view と接続状態から導く表示専用。
import { deriveRoute, renderSystemView } from "./system.js";
// R56: チャート下のインジケータ凡例。market を読むだけ。
import { buildRadar, renderRadar } from "./radar.js";
// R65: 駐在する目の独り言(表示専用の純関数 + 声)。view を読むだけで何も送らない。
import { createPetVoice } from "./pet.js";
import { TIERS as BOOT_TIERS, decideTier, installBootGuard, resetBootGuard } from "./boot_guard.js";

/**
 * NQX Nightwatch Mini App。
 *
 * 表示の原則:
 *   - 画面に出るシナリオ / ポジションは、サーバーが「今も有効」と判定したものだけ
 *   - 取得できないときは古いカードを残さない。ポジションだけは消さず STALE にする
 *   - localStorage も過去 payload も正本にしない
 *
 * 発注の原則:
 *   - 手動は CONFIRM & SEND ORDER の一回押しが明示承認
 *   - AUTO は認証済みスイッチ操作が期限付きの継続承認。注文は監視PCが再検証
 *   - ボタンは押下と同時に disabled。同じ payload を二度送らない
 *   - ここで検証した値は信用されない。Bot 側が全項目を再検証する
 */

const launch = consumeLaunchParams();
const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;

const stateStack = document.getElementById("stateStack");
const statusPill = document.getElementById("statusPill");
const statusText = document.getElementById("statusText");
const signalCount = document.getElementById("signalCount");
const toast = document.getElementById("toast");
const lifelineHost = document.getElementById("lifeline");
const ledgerView = document.getElementById("ledgerView");
const ledgerBody = document.getElementById("ledgerBody");
const ledgerCount = document.getElementById("ledgerCount");
const marketStatus = document.getElementById("marketStatus");
const radarView = document.getElementById("radarView");
const radarBody = document.getElementById("radarBody");
const radarState = document.getElementById("radarState");
const decisiveRail = document.getElementById("decisiveRail");
const onePassToggle = document.getElementById("onePassToggle");
const onePassStatus = document.getElementById("onePassStatus");
const onePassMessage = document.getElementById("onePassMessage");
const onePassMeta = document.getElementById("onePassMeta");
const ultraToggle = document.getElementById("ultraToggle");
const soundToggle = document.getElementById("soundToggle");
const autotradePanel = document.getElementById("autotradePanel");
const settingsView = document.getElementById("settingsView");
// ロゴは render() より前に組み立てる(render が状態を流し込む)。
const logo = initLogo(document.querySelector(".wordmark"));
const settingsState = document.getElementById("settingsState");
const modeBadges = document.getElementById("modeBadges");
const watchtower = document.getElementById("watchtower");
const watchtowerStatus = document.getElementById("watchtowerStatus");
const watchtowerDetail = document.getElementById("watchtowerDetail");
const autoExtendRow = document.getElementById("autoExtendRow");
const autoExtendInfo = document.getElementById("autoExtendInfo");
const autoExtendBtn = document.getElementById("autoExtendBtn");
const ultraAccountsHost = document.getElementById("ultraAccounts");
const systemView = document.getElementById("systemView");
const systemBody = document.getElementById("systemBody");
const systemState = document.getElementById("systemState");
const watchSections = [...document.querySelectorAll(".ops")];
/** 表示中のビュー。発注経路には影響しない(表示の切替のみ)。 */
let activeViewName = "watch";

// R67: 起動の見張り。Telegram Desktop(WebView2)で開いた直後に WebView が落ち、「ボタンを
// 押しても開かない」状態になった(2026-09-08、Telegram Desktop の log.txt に crashed webview)。
// 落ちるのは端末の描画層なので、今回の起動で WebGL / 鉄粉 / 音声を動かすかをここで決める。
// 判定は表示だけに効き、状態・発注には一切触れない(boot_guard.js)。
const bootStorage = (() => {
  try { return window.localStorage; } catch { return null; }
})();
const boot = decideTier({
  storage: bootStorage,
  search: window.location.search,
  platform: tg?.platform || "",
});
installBootGuard({ storage: bootStorage, decision: boot, win: window });
/** 状態ピルに出す tier の表記(full は出さない)。 */
const BOOT_TIER_LABELS = Object.freeze({ [BOOT_TIERS.MID]: "MID", [BOOT_TIERS.LITE]: "LITE", [BOOT_TIERS.SAFE]: "SAFE MODE" });
// 状態ピルをタップ → 記録を消して開き直す(また落ちれば次は自動で safe)。
// R75: SAFE / LITE からは端末の既定(Telegram Desktop なら mid、他は full)へ、
// MID からは boot=full で 3D 背景まで点ける(明示の段階上げ)。
statusPill?.addEventListener("click", () => {
  if (boot.tier === BOOT_TIERS.FULL) return;
  haptic("light");
  resetBootGuard(bootStorage);
  const url = new URL(window.location.href);
  url.searchParams.delete("safe");
  if (boot.tier === BOOT_TIERS.MID) url.searchParams.set("boot", BOOT_TIERS.FULL);
  else url.searchParams.delete("boot");
  window.location.replace(url.toString());
});

// The renderer is atmospheric only, never part of the order decision.  Load
// it after the executable state shell so a 3D dependency cannot delay gates.
let scene3d = { setMode() {}, flash() {}, charge() {} };
// R75: 3D 背景は full だけ(mid = Telegram Desktop の既定では読まない。目と氷は読む)。
if (boot.scene3d) {
  import("./scene3d.js").then(({ initNightwatchScene }) => {
    scene3d = initNightwatchScene(document.getElementById("nightwatchCanvas"));
    render();
  }).catch(() => {
    // Chart and execution controls remain intentionally usable without 3D.
  });
}
// R60: 待機紋章の目(3D 眼球)。scene3d と同じく後読み・表示専用。empty card が
// 描かれた直後に attach し、カードが消えれば detach で止まる(eye3d.js)。
let standbyEye = { attach() {}, detach() {}, setMode() {}, available: false };
// 3D が後読みで届くまでの間、平面の紋章を一瞬でも見せない(起動直後に旧紋章が出るバグ、
// 2026-09-06 ユーザー報告)。WebGL の有無だけを起動時に同期で調べ、使える端末では最初の描画から
// 平面を隠して黒鉄の玉(CSS)を置く。3D が来なければ(読み込み失敗・NOOP)平面に戻す。
let standby3d = (() => {
  if (!boot.eye3d) return false;
  try {
    const probe = document.createElement("canvas");
    return Boolean(window.WebGLRenderingContext && (probe.getContext("webgl2") || probe.getContext("webgl")));
  } catch {
    return false;
  }
})();
function standbyFallbackToFlat() {
  standby3d = false;
  document.querySelectorAll(".standby.is-3d-pending").forEach((node) => node.classList.remove("is-3d-pending"));
}
// R65: 目の駐在所(ペット)。待機カードが出ていない間 —— つまり目が居場所を失っている間 ——
// だけ、広い画面の左下へ移して住まわせる。狭い画面・3D 無しでは駐在させない(従来どおり
// カードの中だけに出る)。この面は表示専用で、発注経路には一切触れない。
const petDen = document.getElementById("petDen");
const petEyeHost = document.getElementById("petEye");
const petPerch = document.getElementById("petPerch");
const petSay = document.getElementById("petSay");
const petWide = window.matchMedia("(min-width: 880px) and (min-height: 760px)");
const petVoice = createPetVoice();
let petLine = "";
/** 目の居場所。待機カード(WATCH 表示中のみ) > 駐在所 > どこにも居ない。 */
function mountStandbyEye() {
  const inCard = activeViewName === "watch" ? (stateStack?.querySelector(".standby") || null) : null;
  const den = inCard || !standbyEye.available || !petWide.matches ? null : petEyeHost;
  if (petDen) petDen.hidden = !den;
  standbyEye.attach(inCard || den);
}
if (boot.eye3d) {
  import("./eye3d.js").then(({ initStandbyEye }) => {
    standbyEye = initStandbyEye();
    if (!standbyEye.available) standbyFallbackToFlat();
    mountStandbyEye();
    renderPet();
  }).catch(() => {
    // 読み込めなければ SVG の線画の目に戻す。
    standbyFallbackToFlat();
  });
} else {
  // lite / safe: 3D の目を読みに行かない。SVG の線画の目で待つ。
  standbyFallbackToFlat();
}
petWide.addEventListener("change", () => { mountStandbyEye(); renderPet(); });
// 突くと独り言を引き直す(表示だけの遊び。状態も注文も動かさない)。
petPerch?.addEventListener("click", () => {
  haptic("light");
  petVoice.poke();
  renderPet();
});

/**
 * ペットが読む文脈。view に載っていない事情(この端末の接続・監視ループの生死・
 * 今日の実現損益)だけを渡す。判定はしない。
 */
function petContext() {
  const offline = !demoMode && (connection === CONNECTION.OFFLINE
    || connection === CONNECTION.RECONNECTING || connection === CONNECTION.UNCONFIGURED);
  const health = demoMode || offline ? null : evaluateCycleHealth(currentView, serverNow());
  return {
    now: serverNow(),
    offline,
    health: health?.status || "",
    kill: health?.beacon?.kill === true,
    dayUsd: recentNet(scopeResults(currentView?.recentResults, currentView?.accounts).scoped, 1, serverNow()),
  };
}

/**
 * 目の居場所と一言。描画のたび(と 1 秒ごとの tick)に呼ぶ。台詞が変わったときだけ
 * 打ち直し、決済の反応が来た拍だけ跳ねる / 沈む。
 */
function renderPet() {
  mountStandbyEye();
  const verse = petVoice.speak(currentView, petContext());
  standbyEye.setMode(verse.mode);
  if (!petDen || petDen.hidden) return;
  petDen.dataset.tone = verse.tone;
  if (verse.line === petLine) return;
  petLine = verse.line;
  typeText(petSay, verse.line);
  petDen.classList.remove("is-reacting");
  if (verse.reacting) {
    void petDen.offsetWidth;   // 同じクラスの付け直しでは所作が再始動しない
    petDen.classList.add("is-reacting");
  }
}
// サーバー状態を受けるモードでは、ビルド同梱の market.json を後から被せない。
const chart = initTapeChart(document.querySelector(".tape-chart"), { autoPoll: !launch.apiBase, webgl: boot.ice });
window.NightwatchChart = chart;
let scenarioChart = null;
let positionChart = null;   // R66: 保有中カードのチャート(シナリオと同じく作り直さず差し戻す)
let lastMarket = null;
let marketUiUpdatedAt = 0;

let currentView = null;
const liveClock = createServerClock();
let connection = CONNECTION.CONNECTING;
let connectionDetail = "";
/** 送信済みシナリオ。再描画でボタンが復活しないようにする。 */
const submitted = new Map();
/** 送信済みの解除要求(order の idempotencyKey)。二度送らない。 */
const submittedUnstick = new Set();
/** 解除パネルを開いている order のキー。再描画で勝手に閉じないようにする。 */
let unstickOpenFor = null;
/** 自動で開いた決済結果の ID。同じ結果を二度開かない。 */
let shownResultId = null;
/** 初回 snapshot に含まれる過去結果は自動表示しない。新規 delta のみ開く。 */
let resultHydrated = false;
/** サーバー正本の期限付き AUTO 武装。ローカル保存からは復元しない。 */
let onePassMode = false;
let onePassResult = { status: "IDLE", pass: false, key: "", grade: "—", reasons: [], at: "" };
let autoArm = null;
let autoStateVerified = false;
let autoPending = false;
/**
 * デモモード。実シグナルが無くても UI 全部(武装カード・長押し・記録)を
 * 触れるようにする「テスト画面」。ON の間、発注経路は物理的に遮断される
 * (confirmOrder の先頭で必ず止まり、tg.sendData には到達しない)。
 */
let demoMode = false;
let lastRealView = null;
/**
 * ULTRA mode。ON の間、通常シグナルの枚数は使わず、口座ごとに再計算した
 * 枚数へ強制上書きする。画面の数値は表示専用で、実際に発注される枚数は
 * Bot が ultra_mode.py で同じ式を再計算した値。
 */
let ultraMode = false;
/** 音声キュー(R54)。端末側の表示設定 —— 発注権限ではないので保存してよい。 */
let soundMode = false;

/**
 * 表示モードの保存。
 *
 * localStorage を使わない原則は「サーバー状態の正本をブラウザに持たせない」
 * ためのもので、建玉・注文・シナリオには今も一切使っていない。ここに保存する
 * のは ULTRA の表示/サイズ意図だけ。AUTO は実注文権限なので保存せず、毎回
 * Worker/Durable Object の状態から同期する。
 */
const SETTINGS_KEY = "nqx.modes.v2";

function loadModes() {
  try {
    const raw = window.localStorage?.getItem(SETTINGS_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    return { ultra: parsed.ultra === true, sound: parsed.sound === true };
  } catch {
    // 保存領域が使えない環境(プライベートモード等)では既定の OFF で動く。
    return null;
  }
}

function saveModes() {
  try {
    window.localStorage?.setItem(SETTINGS_KEY,
      JSON.stringify({ ultra: ultraMode, sound: soundMode }));
  } catch {
    /* 保存できなくても操作は続行する(次回 OFF から始まるだけ) */
  }
}

const resultView = initResultView({ onClose: () => render() });

// ---------------------------------------------------------------- 表示ユーティリティ

const JST = { timeZone: "Asia/Tokyo", hour12: false };

function serverNow() {
  return liveClock.now();
}

function armIsLive(arm) {
  const expiresAt = Date.parse(arm?.expiresAt || "");
  return Boolean(
    arm?.enabled === true
    && arm?.autotrade === true
    && arm?.live === true
    && Number.isFinite(expiresAt)
    && expiresAt > serverNow()
  );
}

/** AUTO の表示も権限も、完全 snapshot / mutation 応答を正本にする。 */
function syncAutotradeAuthority(view) {
  autoStateVerified = Boolean(view && typeof view === "object");
  autoArm = view?.autotradeArm || null;
  onePassMode = armIsLive(autoArm);
  if (!onePassMode) {
    onePassResult = { status: "IDLE", pass: false, key: "", grade: "—", reasons: [], at: "" };
  }
}

function jstTime(iso) {
  if (!iso) return "—";
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return "—";
  return formatJstClock(ms);
}

function jstStamp(iso) {
  if (!iso) return "—";
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return "—";
  return new Date(ms).toLocaleString("ja-JP", { ...JST, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function price(value) {
  // Number(null) は 0 になる。欠損(null / undefined / "")は 0.00 ではなく「—」。
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed)
    ? parsed.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : "—";
}

/**
 * 価格として使える値だけ(有限かつ正)。null / undefined / "" / bool は欠損。
 * Worker の position は currentPrice / stop / target を null で返すことがあり、
 * `Number(null)` = 0 を「有限」と読むと last=0 で含み損益が (建値 − 0) × 枚数 に化ける
 * (2026-09-09 実測: SHORT 6 @29,551.75 で OPEN P&L +$354,621 / last 0.00)。
 */
function finitePrice(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function money(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `$${parsed.toFixed(2)}` : "N/A";
}

/** 整数ドル。ULTRA の表は桁が大きく小数が邪魔なので、こちらで桁区切りだけ出す。 */
function dollars(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed)
    ? `$${Math.round(parsed).toLocaleString("en-US")}` : "N/A";
}

function countdown(expiresAt) {
  const remaining = Date.parse(expiresAt) - serverNow();
  if (!Number.isFinite(remaining) || remaining <= 0) return "EXPIRED";
  const totalSec = Math.floor(remaining / 1000);
  return `${String(Math.floor(totalSec / 60)).padStart(2, "0")}:${String(totalSec % 60).padStart(2, "0")}`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]
  ));
}

function notify(message) {
  toast.dataset.caret = "glitch";    // トーストは一瞬の信号 → グリッチ
  typeText(toast, message);
  toast.classList.add("show");
  window.clearTimeout(notify.timer);
  notify.timer = window.setTimeout(() => toast.classList.remove("show"), 2600);
}

function haptic(type = "light") {
  if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred(type);
}

// 音声キュー(R54)。view の差分だけを読み、SOUND スイッチが ON のときだけ鳴らす。
// iOS / Telegram WebView は操作なしに音を出せないので、タップで解錠する(R74: 毎タップ、冪等)。
// 起動直後の render() は view=null で走る —— 基準は最初の snapshot で rebase する(onState)。
const soundCue = createSoundCue({ isEnabled: () => soundMode && boot.audio });
soundCue.bindUnlock(document);

// ---------------------------------------------------------------- Telegram 判定

function inTelegramRuntime() {
  // Telegram のスクリプトは普通のページにも読み込める。platform か initData が
  // あって初めて Telegram 内だと言える。
  return Boolean(tg && ((tg.initData && tg.initData.length > 0) ||
    (tg.platform && tg.platform !== "unknown")));
}

function canSendToBot() {
  // sendData は keyboard button 起動でのみ使える。ここが発注の唯一の出口。
  return inTelegramRuntime() && typeof tg.sendData === "function";
}

function setBridgeState() {
  if (!inTelegramRuntime()) return;
  tg.ready();
  tg.expand();
}

// ---------------------------------------------------------------- 接続表示

const CONNECTION_LABEL = {
  [CONNECTION.CONNECTING]: "CONNECTING…",
  [CONNECTION.LIVE]: "LIVE",
  [CONNECTION.RECONNECTING]: "RECONNECTING",
  [CONNECTION.OFFLINE]: "OFFLINE",
  [CONNECTION.UNCONFIGURED]: "NO STATE SERVICE",
};

/** ヘッダーの唯一のステータス。接続・実行環境・最終確認時刻をここに集約する。 */
function renderStatusPill() {
  if (demoMode) {
    statusText.textContent = `DEMO · JST ${formatJstClock(serverNow())}`;
    statusPill.classList.remove("live", "warn");
    statusPill.classList.add("demo");
    return;
  }
  statusPill.classList.remove("demo");
  const label = CONNECTION_LABEL[connection] || connection;
  const verified = currentView?.lastVerifiedAt;
  const parts = [label];
  // LIVE の右側は「最後に建玉を照会した時刻」ではなく、サーバー時計に
  // 同期した現在の JST を表示する。lastVerifiedAt は数分更新されないことが
  // あり、時計として表示すると止まって見えていた。非 LIVE 状態だけは、
  // いつまで確認できていたかを従来どおり残す。
  if (connection === CONNECTION.LIVE) {
    parts.push(liveClock.synced
      ? `· JST ${formatJstClock(serverNow())}`
      : "· SYNCING TIME");
  } else if (verified) {
    parts.push(`· LAST ${jstTime(verified).slice(0, 5)}`);
  }
  if (!inTelegramRuntime()) parts.push("BROWSER · NO ORDERS");
  // R67: 重い描画層を止めて開いた起動は、その事実を見せる(タップで通常描画に戻せる)。
  if (boot.tier !== BOOT_TIERS.FULL) parts.push(`· ${BOOT_TIER_LABELS[boot.tier] || boot.tier.toUpperCase()}`);
  const text = parts.join(" ");
  if (statusText.textContent !== text) statusText.textContent = text;
  statusPill.classList.toggle("live", connection === CONNECTION.LIVE);
  statusPill.classList.toggle(
    "warn",
    connection === CONNECTION.OFFLINE || connection === CONNECTION.UNCONFIGURED,
  );
  const title = [connectionDetail || ""];
  if (connection === CONNECTION.LIVE && liveClock.synced) title.push("CLOCK WORKER-SYNCED");
  if (verified) title.push(`LAST VERIFIED ${jstStamp(verified)} JST`);
  if (boot.tier !== BOOT_TIERS.FULL) {
    const restore = boot.tier === BOOT_TIERS.MID ? "TAP TO ENABLE 3D BACKGROUND (FULL)" : "TAP TO RESTORE NORMAL RENDER";
    title.push(`RENDER ${boot.tier.toUpperCase()}${boot.reason ? ` (${boot.reason})` : ""} · ${restore}`);
  }
  statusPill.title = title.filter(Boolean).join(" · ");
}

// ---------------------------------------------------------------- 監視の監視

const WATCHTOWER_LABELS = {
  ALIVE: "LIVE",
  BLOCKED: "BLOCKED",
  HALT: "HALT",
  LATE: "LATE",
  SILENT: "SILENT",
  OFF_DUTY: "OFF DUTY",
  NO_DATA: "NO SIGNAL",
  UNKNOWN: "UNKNOWN",
};

/**
 * 監視ループの生死ストリップ。3分ループの publish 痕跡から導出する表示専用。
 * アプリ自体がオフラインのときは「ループが死んだ」と断定せず UNKNOWN に落とす
 * (見えないだけの状態を停止と混同しない)。
 */
function renderWatchtower() {
  if (!watchtower) return;
  if (demoMode) {
    watchtower.dataset.status = "alive";
    watchtowerStatus.textContent = "DEMO";
    watchtowerDetail.textContent = "loop display test";
    return;
  }
  if (connection === CONNECTION.OFFLINE || connection === CONNECTION.RECONNECTING
      || connection === CONNECTION.UNCONFIGURED) {
    watchtower.dataset.status = "unknown";
    watchtowerStatus.textContent = WATCHTOWER_LABELS.UNKNOWN;
    watchtowerDetail.textContent = "app offline — not the loop";
    return;
  }
  const health = evaluateCycleHealth(currentView, serverNow());
  watchtower.dataset.status = health.status.toLowerCase().replace("_", "-");
  watchtowerStatus.textContent = health.beacon?.kill && health.status === "HALT"
    ? "KILL" : (WATCHTOWER_LABELS[health.status] || health.status);
  const lastLabel = health.lastAtMs === null ? "" : formatJstClock(health.lastAtMs, { seconds: false });
  let detail = "no cycle observed yet";
  if (health.status === "ALIVE") {
    detail = `cycle ${lastLabel} · every 3m`;
  } else if (health.status === "BLOCKED") {
    detail = health.beacon?.reason || "cycle blocked — no trade this cycle";
  } else if (health.status === "HALT") {
    detail = health.beacon?.kill
      ? "emergency stop engaged"
      : (health.beacon?.reason || "loop halted — needs manual restart");
  } else if (health.status === "LATE") {
    detail = `no cycle for ${health.ageMin}m — expected every 3m`;
  } else if (health.status === "SILENT") {
    detail = `no cycle for ${health.ageMin}m — loop blocked or down`;
  } else if (health.status === "OFF_DUTY") {
    detail = lastLabel
      ? `resumes ${health.window.nextLabel} · last ${lastLabel}`
      : `resumes ${health.window.nextLabel}`;
  }
  watchtowerDetail.textContent = detail;
}

// ---------------------------------------------------------------- カード

/**
 * トレードの現在地レール。SENT → OPEN → TP1 → RUN → EXIT の5段で、
 * 自動送信されたトレードがいまどの脚にいるかを一目で出す。
 * 表示専用 — データは order / position / entryClaim から導出するだけ。
 */
const TRADE_STAGES = ["SENT", "OPEN", "TP1", "RUN", "EXIT"];

function tradeStages(phase) {
  const currentIndex = TRADE_STAGES.indexOf(phase);
  if (currentIndex < 0) return "";
  return `
    <div class="trade-stages" aria-label="トレードの現在地">
      ${TRADE_STAGES.map((stage, index) => {
        const cls = index < currentIndex ? "done" : (index === currentIndex ? "current" : "");
        return `${index ? "<i></i>" : ""}<span class="stage ${cls}">${stage}</span>`;
      }).join("")}
    </div>`;
}

/**
 * 凍結プランの脚(TP1 / RUNNER)。正本は entryClaim.executionIntent —
 * シナリオが次サイクルで消えても、トレードが生きている間はここに残る。
 */
function planLegsLine(view, position) {
  const intent = view?.entryClaim?.executionIntent;
  const targets = Array.isArray(intent?.targets) ? intent.targets.map(Number) : [];
  if (targets.length < 2 || targets.some((value) => !Number.isFinite(value))) return "";
  const tp1Done = Boolean(position && Number(position.initialQty) > Number(position.qty));
  return `
    <p class="meta-line">TP1 <b>${price(targets[0])}</b>${tp1Done ? ` <span class="gain">✓ FILLED</span>` : ""}
      · RUNNER <b>${price(targets[1])}</b></p>`;
}

/** runner 局面で SL がいくら守っているか。建値以上なら「確保額」、未満なら残リスク。 */
function protectionLine(position) {
  const entry = finitePrice(position.avgEntry);
  const stop = finitePrice(position.stop);
  const qty = Number(position.qty);
  if (entry === null || stop === null || !Number.isFinite(qty) || qty < 1) return "";
  const dir = String(position.side).toUpperCase() === "LONG" ? 1 : -1;
  const locked = (stop - entry) * dir * 2 * qty;   // MNQ $2/pt
  return locked >= 0
    ? ` · SL LOCKS <b class="gain">+${money(locked).slice(1)}</b>`
    : ` · SL RISK <b class="pain">${money(Math.abs(locked))}</b>`;
}

/**
 * 保有中カードに出す SL / TP / 脚。
 *
 * 正本は position ストリーム(Python が凍結プランを重ねて publish する)。それが
 * 無い間(旧 Worker 状態・重ね合わせ不能)は **同じ Worker 状態にある凍結プラン**
 * (entryClaim.executionIntent = CLAIM 時に凍結した entry/stop/targets/legs、
 * managementClaim.managementIntent = TP1 後に張り替えた SL)から補う。
 * ブローカーは SL/TP を返さない(R52)ので、ここが空だと SL/TP は永久に「—」だった。
 * 向きとシンボルがプランと一致しないときは補わない(手動建玉に他人の水準を貼らない)。
 */
function positionLevels(position, view) {
  const claim = view?.entryClaim || null;
  const intent = claim?.executionIntent || null;
  const claimState = String(claim?.state || "").toUpperCase();
  const usable = Boolean(intent)
    && ["CLAIMED", "CONSUMED"].includes(claimState)
    && sideDirection(intent.side) !== null
    && sideDirection(intent.side) === sideDirection(position.side)
    && (!intent.symbol || !position.symbol || String(intent.symbol) === String(position.symbol));
  const management = view?.managementClaim || null;
  const mgmtIntent = management?.managementIntent || null;
  const mgmtStop = mgmtIntent
    && String(management.state || "").toUpperCase() === "CONSUMED"
    && String(mgmtIntent.action || "").toUpperCase() === "MODIFY"
    && (!mgmtIntent.symbol || !position.symbol || String(mgmtIntent.symbol) === String(position.symbol))
    ? finitePrice(mgmtIntent.stop) : null;
  const targets = usable && Array.isArray(intent.targets)
    ? intent.targets.map(finitePrice).filter((value) => value !== null) : [];
  // broker 行の null は「返っていない」。Number(null)=0 を SL 0.00 にしない。
  const brokerStop = finitePrice(position.stop);
  const brokerTarget = finitePrice(position.target);
  const stop = brokerStop ?? mgmtStop ?? (usable ? finitePrice(intent.stop) : null);
  const target = brokerTarget ?? (targets.length ? targets[targets.length - 1] : null);
  const legs = usable && Array.isArray(intent.legs) && intent.legs.length
    ? intent.legs.map((leg) => ({ id: leg?.id, qty: Number(leg?.qty), target: finitePrice(leg?.target) }))
    : (targets.length >= 2
      ? [{ id: "TP1", qty: 1, target: targets[0] }, { id: "RUNNER", qty: 1, target: targets[targets.length - 1] }]
      : []);
  const fromPlan = (brokerStop === null && stop !== null) || (brokerTarget === null && target !== null);
  return { stop, target, legs, fromPlan };
}

function positionCard(position, view) {
  const stale = position.state === "STALE";
  const exiting = position.state === "EXIT_PENDING";
  const stateLabel = stale ? "STALE" : (exiting ? "EXITING" : "OPEN");
  const sideLabel = position.side || position.lastKnownState || "—";
  const tp1Done = Boolean(position.initialQty && position.qty < position.initialQty);
  const phase = exiting ? "EXIT" : (tp1Done ? "RUN" : "OPEN");
  const levels = positionLevels(position, view);
  const planTag = levels.fromPlan ? ` <span class="pnl-src" title="frozen plan">PLAN</span>` : "";
  // 現値はブローカー行に無ければ市況ストリームの価格(同じ Worker 状態)で補う。
  const last = finitePrice(position.currentPrice) ?? finitePrice(lastMarket?.price);
  const projected = { ...position, stop: levels.stop, target: levels.target };
  return `
    <article class="state-card position ${stale ? "is-stale" : ""}">
      <div class="card-top">
        <span class="state" data-type>${glyph("position")} ${escapeHtml(sideLabel)} ${escapeHtml(String(position.qty ?? "—"))}${
          position.initialQty && position.qty < position.initialQty
            ? `/${escapeHtml(String(position.initialQty))}` : ""} — ${escapeHtml(stateLabel)}</span>
        <span class="aux">${escapeHtml(position.symbol || "")}</span>
      </div>
      ${tradeStages(phase)}
      ${stale ? `<p class="card-warning" data-type>BROKER QUERY FAILED — position kept, no new orders</p>` : ""}
      <div class="tape-chart position-chart" aria-label="3m chart"></div>
      <div class="trio">
        <div><p class="micro">AVG ENTRY</p><p class="val main">${price(position.avgEntry)}</p></div>
        <div><p class="micro">SL${planTag}</p><p class="val stop">${price(levels.stop)}</p></div>
        <div><p class="micro">TP${planTag}</p><p class="val">${price(levels.target)}</p></div>
      </div>
      ${positionProjectionHtml(projected, levels.legs, last)}
      ${planLegsLine(view, position)}
      <p class="meta-line">
        LAST <b>${last === null ? "STALE" : price(last)}</b>${tp1Done ? protectionLine(projected) : ""}
      </p>
      <p class="card-meta">FILLED <b>${escapeHtml(jstStamp(position.filledAt))}</b> · SEEN <b>${escapeHtml(jstStamp(position.observedAt))}</b> · ${escapeHtml(position.source || position.unverifiedSource || "—")}${
        stale && position.staleSince ? ` · STALE <b>${escapeHtml(jstStamp(position.staleSince))}</b>〜` : ""}</p>
    </article>`;
}

function orderCard(order, view, unstickHtml = "") {
  const state = String(order.state || "").toUpperCase();
  const lifecycleNote = state === "ENTRY_RESTING"
    ? "RESTING — waiting for broker fill"
    : (["PARTIAL", "ENTRY_PARTIAL_ROUTE", "ENTRY_PARTIAL_FILL", "UNKNOWN"].includes(state)
      ? "PARTIAL / UNKNOWN — no resend"
      : "SENT — awaiting broker confirmation");
  return `
    <article class="state-card order">
      <div class="card-top">
        <span class="state" data-type>${glyph("order")} ${escapeHtml(order.side || "")} ${escapeHtml(String(order.qty ?? ""))} — ORDER ${escapeHtml(order.state)}</span>
      </div>
      ${tradeStages("SENT")}
      <p class="card-note" data-type>${lifecycleNote}</p>
      <div class="trio">
        <div><p class="micro">ENTRY</p><p class="val main">${price(order.entry)}</p></div>
        <div><p class="micro">SL</p><p class="val stop">${price(order.stop)}</p></div>
        <div><p class="micro">TP</p><p class="val">${price(order.target)}</p></div>
      </div>
      <p class="card-meta">SENT <b>${escapeHtml(jstStamp(order.at))}</b>${
        order.receipt ? ` · RECEIPT <b>${escapeHtml(order.receipt)}</b>` : ""}</p>
      ${unstickHtml}
    </article>`;
}

/**
 * 観測漏れで残った注文表示の解除導線(HANDOFF §11 ②)。
 *
 * 押し間違い防止は3層:
 *   1. 送信から10分間は導線を出さない(約定待ちの誤解除防止・Bot 側も同じ規則)
 *   2. タップで警告パネルを開く(この時点では何も送らない)
 *   3. 発注と同じ「右端まで引き切るスライド」でのみ送信(配色は rust で発注と区別)
 * 最終判定は Bot がブローカー verified FLAT を再確認して行う。
 */
function unstickSection(order, info) {
  if (!info) return "";
  if (submittedUnstick.has(order.idempotencyKey)) {
    return `<button class="ghost-btn" disabled>UNSTICK SENT — Bot verifying</button>`;
  }
  if (!info.eligible) {
    if (info.reason === "AWAITING BROKER") {
      const min = Math.max(1, Math.ceil((info.remainingMs || 0) / 60_000));
      return `<p class="card-meta">Manual unstick available in ~${min} min</p>`;
    }
    if (info.reason === "BROKER UNVERIFIED") {
      return `<p class="card-meta">Unstick needs a broker check (/sync)</p>`;
    }
    return "";
  }
  const open = unstickOpenFor === order.idempotencyKey;
  return `
    <div class="unstick">
      <button class="ghost-btn" data-action="unstick-open">${open ? `${glyph("collapse")} CLOSE` : `${glyph("alert")} STUCK ORDER?`}</button>
      <div class="unstick-panel" ${open ? "" : "hidden"}>
        <p class="card-warning" data-type>Marks this display CANCELED. Does not touch broker orders. Confirm no resting order on Tradovate first.</p>
        <div class="slide-confirm unstick-slide" data-action="unstick-confirm" role="slider" tabindex="0"
             aria-label="Slide to unstick" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
          <span class="slide-label">SLIDE TO UNSTICK</span>
          <span class="slide-rail" aria-hidden="true">⟫ ⟫ ⟫</span>
          <span class="slide-fill" aria-hidden="true"></span>
          <span class="slide-thumb" aria-hidden="true">⟫</span>
        </div>
        <p class="hold-hint">Bot re-verifies broker FLAT</p>
      </div>
    </div>`;
}

// ---------------------------------------------------------------- 一発合格 / 自動執行

// 2026-09-04: B も発注可能(ユーザー決定)。正本は execution_contract.json の allowedGrades。
const ONE_PASS_GRADES = new Set(["A", "A+", "B"]);

function scenarioGrade(scenario, evaluation) {
  return String(scenario?.grade || evaluation?.decision?.grade || "—").trim().toUpperCase();
}

/**
 * 一発合格は「確率」ではなく、現在のサーバー表示契約を一回で切る操作。
 * display.orderable がサーバー側の総合ゲート、grade が A/A+ 条件の正本入力。
 * ここで弾いても Bot 側の再検証を省略しない。
 */
function evaluateOnePass(scenario, display, evaluation) {
  const at = new Date(serverNow()).toISOString();
  if (!scenario) {
    // AUTOの送信権限がLIVEであることと、現在の市場シグナルがARMEDである
    // ことは別状態。ここをARMEDと呼ぶとゲートカードのEXPIRED/FLATと矛盾して
    // 見えるため、無シナリオ時はLISTENINGへ分離する。
    return { status: "LISTENING", pass: false, key: "none", grade: "—", reasons: ["NO SETUP RIGHT NOW"], at };
  }
  const grade = scenarioGrade(scenario, evaluation);
  const reasons = [];
  if (!display?.orderable) reasons.push(display?.blockReason || "SERVER IS NOT CLEARING TRADES");
  if (!ONE_PASS_GRADES.has(grade)) reasons.push(`SETUP IS GRADE ${grade} — NEEDS A+ / A / B`);
  if (!["ACTIVE", "ARMED"].includes(String(scenario.state || "").toUpperCase())) {
    reasons.push(`SETUP IS ${scenario.state || "—"} — NOT READY TO TRADE`);
  }
  const expired = Number.isFinite(Date.parse(scenario.expiresAt)) && Date.parse(scenario.expiresAt) <= serverNow();
  const key = `${scenario.scenarioId || "?"}:${scenario.fingerprint || "?"}:${scenario.state || "?"}:${display?.orderable ? "1" : "0"}:${grade}:${expired ? "expired" : "live"}`;
  if (expired) reasons.push("SETUP EXPIRED — WAITING FOR A NEW ONE");
  return {
    status: reasons.length ? "FAIL" : "PASS",
    pass: reasons.length === 0,
    key,
    scenarioId: scenario.scenarioId,
    grade,
    reasons,
    at,
  };
}

function syncOnePass(scenario, display, evaluation) {
  if (!onePassMode) {
    onePassResult = { status: "IDLE", pass: false, key: "", grade: "—", reasons: [], at: "" };
    return;
  }
  const next = evaluateOnePass(scenario, display, evaluation);
  if (next.key !== onePassResult.key || onePassResult.status === "IDLE") onePassResult = next;
}

/**
 * 見出しに出す一語。内部の status(CSS の data-status と対)はそのままに、
 * 画面には「今なにが起きているか」を普通の言葉で出す。
 *   LISTENING は「認証は生きているが売買条件が揃っていない」であって、
 *   故障でも停止でもない —— WATCHING と読ませる。
 */
const ONE_PASS_LABELS = {
  IDLE: "OFF",
  LISTENING: "WATCHING",
  PASS: "READY",
  FAIL: "HOLDING",
};

function renderOnePassRail() {
  const r = onePassResult;
  const tone = String(r.status || "IDLE").toLowerCase();
  const message = !onePassMode
    ? ""
    : r.status === "PASS"
      ? "SETUP CLEARED — THE MONITOR WILL PLACE THIS TRADE"
      : r.status === "FAIL"
        ? (r.reasons?.[0] || "NOT CLEARED TO TRADE")
        : "AUTO IS ON — WAITING FOR AN A / A+ SETUP";
  // 残り1時間を切ったら期限の隣に残分を出す(静かな失効=機会損失を防ぐ)。
  const armRemainMs = Date.parse(autoArm?.expiresAt || "") - serverNow();
  const armExpiry = autoArm?.expiresAt
    ? `${jstTime(autoArm.expiresAt).slice(0, 5)}${
      Number.isFinite(armRemainMs) && armRemainMs > 0 && armRemainMs < 60 * 60_000
        ? ` (${Math.max(1, Math.floor(armRemainMs / 60_000))}M LEFT)` : ""}`
    : "—";
  const armAccounts = Array.isArray(autoArm?.accountScope) ? autoArm.accountScope.length : 0;
  const meta = !onePassMode
    ? (autoStateVerified ? "AUTO OFF — TURN IT ON IN SETTINGS" : "AUTO STATE UNKNOWN — CHECK CONNECTION")
    : `${r.scenarioId ? `GRADE ${r.grade || "—"} · ` : ""}ON UNTIL ${armExpiry} · ${armAccounts} ACCOUNT${armAccounts === 1 ? "" : "S"}`;
  if (decisiveRail) {
    decisiveRail.dataset.status = tone;
    // OFF のときは説明文だけの帯になるので出さない。切り替えは設定タブにある。
    decisiveRail.hidden = !onePassMode;
  }
  if (onePassStatus) {
    onePassStatus.textContent = onePassMode
      ? (ONE_PASS_LABELS[r.status] || r.status) : ONE_PASS_LABELS.IDLE;
  }
  if (onePassMessage) {
    onePassMessage.dataset.caret = onePassMode ? "reticle" : "eye";   // AUTO が生きていれば照準
    typeText(onePassMessage, message);
  }
  if (onePassMeta) {
    onePassMeta.textContent = meta;
    onePassMeta.classList.toggle("urgent",
      onePassMode && Number.isFinite(armRemainMs) && armRemainMs > 0 && armRemainMs < 30 * 60_000);
  }
  if (onePassToggle) {
    onePassToggle.classList.toggle("is-on", onePassMode);
    onePassToggle.classList.toggle("is-pending", autoPending);
    onePassToggle.setAttribute("aria-checked", onePassMode ? "true" : "false");
    onePassToggle.disabled = autoPending
      || (!demoMode && !(launch.apiBase && (launch.token || tg?.initData)));
  }
}

async function toggleOnePass() {
  if (autoPending) return;
  if (demoMode) {
    haptic("notification");
    notify("DEMO — AUTO server state unchanged");
    return;
  }
  if (!client?.configured) {
    haptic("notification");
    notify("AUTO UNAVAILABLE — reopen from Telegram");
    return;
  }
  const desired = !onePassMode;
  autoPending = true;
  haptic("light");
  renderOnePassRail();
  renderSettingsView();
  try {
    const arm = await client.setAutotrade(desired, { ttlMinutes: 420 });
    notify(desired
      ? `AUTO LIVE — ${Array.isArray(arm?.accountScope) ? arm.accountScope.length : 0} accounts`
      : "AUTO OFF — new entries stopped");
    haptic("medium");
  } catch (error) {
    notify(`AUTO CHANGE FAILED — ${String(error?.message || error)}`);
    haptic("notification");
    await client.refresh("autotrade-mutation-failed");
  } finally {
    autoPending = false;
    render();
  }
}

/**
 * AUTO の期限延長。setAutotrade(true) を再発行して TTL を張り直すだけで、
 * 口座・銘柄・実期限は従来どおり Worker の応答が正本(アプリからは動かせない)。
 */
async function extendAuto() {
  if (autoPending || !onePassMode) return;
  if (demoMode) {
    haptic("notification");
    notify("DEMO — AUTO server state unchanged");
    return;
  }
  autoPending = true;
  haptic("light");
  renderSettingsView();
  try {
    const arm = await client.setAutotrade(true, { ttlMinutes: 420 });
    notify(`AUTO EXTENDED — UNTIL ${jstTime(arm?.expiresAt).slice(0, 5)}`);
    haptic("medium");
    autoExpiryWarnedArmId = null;
  } catch (error) {
    notify(`AUTO EXTEND FAILED — ${String(error?.message || error)}`);
    haptic("notification");
    await client.refresh("autotrade-extend-failed");
  } finally {
    autoPending = false;
    render();
  }
}

/** 期限30分前の一度きり警告。武装(armId)ごとにリセットされる。 */
let autoExpiryWarnedArmId = null;

function warnAutoExpiry() {
  if (!onePassMode || demoMode) return;
  const remaining = Date.parse(autoArm?.expiresAt || "") - serverNow();
  if (!Number.isFinite(remaining) || remaining <= 0 || remaining > 30 * 60_000) return;
  const armId = autoArm?.armId || "arm";
  if (autoExpiryWarnedArmId === armId) return;
  autoExpiryWarnedArmId = armId;
  haptic("notification");
  notify(`AUTO EXPIRES IN ${Math.max(1, Math.floor(remaining / 60_000))}M — EXTEND IN SETTINGS`);
}

/**
 * ULTRA mode の口座別発注計画。ON のときだけ描く。
 *
 * ここは表示専用。実発注枚数は Bot が ultra_mode.py で再計算する。
 * 現行の execution_contract がその枚数を通さない場合は routing 不可を
 * そのまま出す — ELIGIBLE と「実際に出せる」は別物なので隠さない。
 */
function ultraPanel(scenario) {
  if (!ultraMode) return "";
  const accounts = currentView?.accounts?.list || [];
  const plan = buildUltraPlan(
    { side: scenario.side, entry: scenario.entry, stop: scenario.stop, target: scenario.target },
    accounts, { signalQty: scenario.qty });
  if (!plan.geometry.valid) {
    return `<div class="ultra-panel"><p class="card-warning" data-type>${escapeHtml(plan.geometry.reason)}</p></div>`;
  }
  if (!accounts.length) {
    return `<div class="ultra-panel"><p class="card-warning" data-type>NO ACCOUNTS</p></div>`;
  }
  const rows = plan.accounts.map((row) => {
    const eligible = row.verdict === "ELIGIBLE";
    const note = eligible
      ? (row.contractBlockers.length
        ? `<span class="ultra-blocked">${escapeHtml(row.contractBlockers[0].split("(")[0])}</span>`
        : "")
      : `<span class="ultra-ng">${escapeHtml(row.reasons.join(" · "))}</span>`;
    return `
      <tr class="${eligible ? "ultra-ok" : "ultra-no"}">
        <td>${escapeHtml(row.id)}</td>
        <td class="num"><b>${row.qty === null ? "—" : escapeHtml(String(row.qty))}</b></td>
        <td class="num">${row.projectedProfit === null ? "—" : dollars(row.projectedProfit)}</td>
        <td class="num">${row.projectedLoss === null ? "—" : dollars(row.projectedLoss)}</td>
        <td>${eligible ? "ELIGIBLE" : "INELIGIBLE"}${note}</td>
      </tr>`;
  }).join("");
  return `
    <div class="ultra-panel">
      <table class="ultra-table">
        <thead><tr><th>ACCOUNT</th><th class="num">QTY</th><th class="num">PROFIT</th><th class="num">LOSS</th><th>STATUS</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <p class="card-meta">TOTAL <b>${escapeHtml(String(plan.totalQty))}</b>
        · PROFIT <b>${dollars(plan.totalProjectedProfit)}</b>
        · LOSS <b>${dollars(plan.totalProjectedLoss)}</b></p>
    </div>`;
}

/**
 * DATA ゲートに渡す市況の鮮度。シナリオカードとゲートボードで**同じ材料**を
 * 使わせるための1か所。片方だけに渡していた頃は、同じサイクルなのに
 * カードによって DATA が ✓ と ✗ に分かれていた。
 */
function marketRuntime() {
  const ageMs = Number(currentView?.market?.ageMs);
  return {
    marketAgeSec: Number.isFinite(ageMs) ? ageMs / 1000 : null,
    marketStale: currentView?.market?.stale === true,
    marketVerified: currentView?.market?.verified === true,
  };
}

function scenarioCard(scenario, display, evaluation) {
  const risk = Math.abs(scenario.entry - scenario.stop) * scenario.qty * 2;
  // デモ中はブラウザでもボタンを出す(送信は confirmOrder 冒頭で必ず遮断される)。
  const orderable = display.orderable && (canSendToBot() || demoMode);
  const alreadySent = submitted.has(scenario.scenarioId);
  const accent = scenario.side === "BUY" ? "LONG" : "SHORT";
  const armed = orderable && !alreadySent;

  let action = "";
  if (alreadySent) {
    action = `<button class="hold-btn" disabled>${escapeHtml(submitted.get(scenario.scenarioId))}</button>`;
  } else if (orderable) {
    // R6: 等級バッジ。ボラ 0.4〜0.6 帯で grade が A+ でないのは矛盾状態なので、
    // サーバーが WATCH に落としているはずでも二重防御として発注させない。
    const grade = scenario.grade || null;
    if (gradeContradicts(evaluation, grade)) {
      return contradictionCard(scenario, evaluation, grade);
    }
    const badge = grade ? `<span class="grade-badge">${escapeHtml(grade)}</span>` : "";
    action = `
      <div class="slide-confirm" data-action="confirm" role="slider" tabindex="0"
           aria-label="Slide to send" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
        <span class="slide-label">SLIDE TO SEND${badge}</span>
        <span class="slide-rail" aria-hidden="true">⟫ ⟫ ⟫</span>
        <span class="slide-fill" aria-hidden="true"></span>
        <span class="slide-thumb" aria-hidden="true">⟫</span>
      </div>
      <p class="hold-hint">Bot re-verifies everything</p>`;
  } else if (!display.orderable) {
    // サーバーが発注不可と判定した理由を優先して出す(保有中・注文中など)。
    action = `<p class="card-warning" data-type>${escapeHtml(display.blockReason || "NOT ARMED")}</p>`;
  } else {
    action = `<p class="card-warning" data-type>OPEN IN TELEGRAM TO SEND</p>`;
  }

  // ULTRA が ON の間は縁が虹色に回る。枚数が桁で変わるモードなので、
  // 色ではなく「動いている縁」で通常状態と見分けさせる。
  return `
    <article class="state-card scenario ${armed ? "armed" : ""} ${ultraMode ? "ultra-live" : ""}">
      <div class="card-top">
        <span class="state" data-type>${glyph("signal", armed ? "is-live" : "")} ${escapeHtml(accent)} — ${armed ? "READY" : escapeHtml(scenario.state)}</span>
        <span class="aux" data-countdown="${escapeHtml(scenario.expiresAt)}">${escapeHtml(countdown(scenario.expiresAt))}</span>
      </div>
      ${scenario.title || scenario.reason
        ? `<p class="card-note" data-type>${escapeHtml(scenario.title || "")}${scenario.title && scenario.reason ? " — " : ""}${escapeHtml(scenario.reason || "")}</p>`
        : ""}
      <div class="tape-chart scenario-chart" aria-label="3m chart"></div>
      <div class="trio">
        <div><p class="micro">ENTRY</p><p class="val main">${price(scenario.entry)}</p></div>
        <div><p class="micro">SL</p><p class="val stop">${price(scenario.stop)}</p></div>
        <div><p class="micro">TP</p><p class="val">${price(scenario.target)}</p></div>
      </div>
      ${ultraMode ? "" : scenarioProjectionHtml(scenario, participation(scenario, currentView?.accounts)?.eligible.length || 1)}
      <p class="meta-line">
        ${ultraMode
          ? `<i class="ultra-icon" aria-label="ULTRA"></i>`
          : `<b>${escapeHtml(String(scenario.qty))}</b> · RISK <b>${money(risk)}</b>`}
        · R:R <b>1:${scenario.rr ? scenario.rr.toFixed(2) : "—"}</b>
        · UNTIL <b>${escapeHtml(jstTime(scenario.expiresAt).slice(0, 5))}</b>
        ${evaluation?.tp && typeof evaluation.tp.bars15 === "number"
          ? ` · TP <b>${escapeHtml(num(evaluation.tp.bars15, 1))}</b> bars${
              evaluation.tp.pass === false ? ` ${glyph("alert")}` : ""}`
          : ""}
      </p>
      ${decisionBanner(evaluation)}
      ${evalChecklist(evaluation, marketRuntime())}
      ${ultraMode ? ultraPanel(scenario) : participationLine(scenario, currentView?.accounts)}
      ${action}
      <button class="ghost-btn" data-action="discard">DISMISS</button>
    </article>`;
}

/**
 * ボラ帯が A+ を要求しているのに等級が足りない状態。サーバーが WATCH に
 * 落としているはずの組合せなので、出た時点で異常として扱い発注させない。
 */
function contradictionCard(scenario, evaluation, grade) {
  const accent = scenario.side === "BUY" ? "LONG" : "SHORT";
  return `
    <article class="state-card scenario">
      <div class="card-top">
        <span class="state" data-type>${glyph("signal")} ${escapeHtml(accent)} — HELD</span>
        <span class="aux" data-countdown="${escapeHtml(scenario.expiresAt)}">${escapeHtml(countdown(scenario.expiresAt))}</span>
      </div>
      <div class="trio">
        <div><p class="micro">ENTRY</p><p class="val main">${price(scenario.entry)}</p></div>
        <div><p class="micro">SL</p><p class="val stop">${price(scenario.stop)}</p></div>
        <div><p class="micro">TP</p><p class="val">${price(scenario.target)}</p></div>
      </div>
      ${decisionBanner(evaluation)}
      ${evalChecklist(evaluation, marketRuntime())}
      <p class="card-warning" data-type>VOL BAND REQUIRES A+ — GRADE ${escapeHtml(grade || "NONE")}</p>
      <button class="ghost-btn" data-action="discard">DISMISS</button>
    </article>`;
}

/**
 * 待機の魔法陣。アプリアイコン(HEX MOON)と同じ言語 —
 * 目盛り環と七芒星({7/3})がゆっくり回り、中央で月が待つ。
 */
function standbySigil() {
  const pts = [];
  for (let i = 0; i < 7; i += 1) {
    const a = (((i * 3) % 7) / 7) * Math.PI * 2 - Math.PI / 2;
    pts.push(`${(100 + Math.cos(a) * 62).toFixed(1)},${(100 + Math.sin(a) * 62).toFixed(1)}`);
  }
  const ticks = [];
  for (let deg = 0; deg < 360; deg += 15) {
    const a = ((deg - 90) * Math.PI) / 180;
    const inner = deg % 45 === 0 ? 79 : 85;
    ticks.push(`<line x1="${(100 + Math.cos(a) * inner).toFixed(1)}" y1="${(100 + Math.sin(a) * inner).toFixed(1)}"
      x2="${(100 + Math.cos(a) * 92).toFixed(1)}" y2="${(100 + Math.sin(a) * 92).toFixed(1)}"/>`);
  }
  return `
    <svg class="standby-sigil" viewBox="0 0 200 200" aria-hidden="true">
      <g class="sigil-spin">
        <circle cx="100" cy="100" r="92" fill="none" stroke-width="1" opacity=".55"/>
        <circle cx="100" cy="100" r="73" fill="none" stroke-width=".7" opacity=".3"/>
        <g stroke-width="1.4" opacity=".6">${ticks.join("")}</g>
        <polygon points="${pts.join(" ")}" fill="none" stroke-width="1" opacity=".45"/>
      </g>
    </svg>`;
}

function emptyCard(reason) {
  return `
    <article class="state-card empty">
      <div class="standby${standby3d ? " is-3d-pending" : ""}">
        ${standbySigil()}
        <span class="standby-eye" aria-hidden="true">
          <svg viewBox="0 0 48 48">
            <path class="e-g1" d="M4 24s6.5-11 20-11 20 11 20 11-6.5 11-20 11S4 24 4 24z" transform="translate(-1 .5)"/>
            <path class="e-g2" d="M4 24s6.5-11 20-11 20 11 20 11-6.5 11-20 11S4 24 4 24z" transform="translate(1 -.5)"/>
            <path class="e-core" d="M4 24s6.5-11 20-11 20 11 20 11-6.5 11-20 11S4 24 4 24z"/>
            <circle class="e-iris" cx="24" cy="24" r="7.5"/>
            <circle class="e-pupil" cx="24" cy="24" r="3.4"/>
            <circle class="e-spark" cx="26.4" cy="21.6" r="1.2"/>
          </svg>
        </span>
      </div>
      <strong data-type>NO SCENARIO</strong>
      <p>${escapeHtml(reason)}</p>
    </article>`;
}

// ---------------------------------------------------------------- 描画

let discarded = null;   // DISCARD は選択解除だけ。サーバー状態は変えない。

/** WATCH / RADAR / LEDGER / SYSTEM / SETTINGS の切替。カードの再構築はせず表示だけを入れ替える。 */
function setActiveView(name) {
  activeViewName = ["radar", "ledger", "settings", "system"].includes(name) ? name : "watch";
  const radarActive = activeViewName === "radar";
  const ledgerActive = activeViewName === "ledger";
  const settingsActive = activeViewName === "settings";
  const systemActive = activeViewName === "system";
  watchSections.forEach((node) => { node.hidden = radarActive || ledgerActive || settingsActive || systemActive; });
  if (radarView) radarView.hidden = !radarActive;
  if (ledgerView) ledgerView.hidden = !ledgerActive;
  if (settingsView) settingsView.hidden = !settingsActive;
  if (systemView) systemView.hidden = !systemActive;
  document.body.dataset.view = activeViewName;
  document.querySelectorAll(".dock-tab").forEach((tab) => {
    tab.classList.toggle("is-active", tab.dataset.view === activeViewName);
  });
  if (radarActive) renderRadarView();
  if (ledgerActive) renderLedgerView();
  if (settingsActive) renderSettingsView();
  if (systemActive) { renderSystemRoute(); startSystemPing(); } else { stopSystemPing(); }
  // WATCH 以外では待機カードが隠れる。目は駐在所へ移す(R65)。
  renderPet();
}

// ---------------------------------------------------------------- RADAR(R58)

/** 市況が verified のときだけ値を出す(古い値を読ませない)。applyMarket と同じ規則。 */
function marketVerified(market) {
  const observedMs = Date.parse(market?.observedAt || market?.at || "");
  const ageMs = Number.isFinite(observedMs) ? Math.max(0, serverNow() - observedMs) : Infinity;
  return Boolean(market?.verified === true && market?.stale !== true && ageMs <= 10 * 60_000);
}

/** RADAR タブ。表示専用 —— view.market を radar.js の純関数で組み直して描くだけ。 */
function renderRadarView() {
  if (!radarBody) return;
  const market = currentView?.market || null;
  const verified = marketVerified(market);
  renderRadar(radarBody, verified ? buildRadar(market) : null);
  if (radarState) {
    const stamp = market?.observedAt || market?.at;
    const text = !market ? "NO PRICE" : !verified ? "FEED STALE"
      : `${jstTime(stamp).slice(0, 5)} JST · MNQ 3M`;
    if (radarState.textContent !== text) radarState.textContent = text;
  }
}

// ---------------------------------------------------------------- SYSTEM ROUTE(R55)

/** 直近の遅延計測。SYSTEM タブを開いている間だけ 30 秒ごとに GET /api/health を叩く。 */
let systemPing = null;
let systemPingTimer = null;
let systemPingInFlight = false;
const SYSTEM_PING_INTERVAL_MS = 30_000;

async function runSystemPing() {
  if (systemPingInFlight || demoMode || !client?.configured) return;
  systemPingInFlight = true;
  try {
    systemPing = await client.ping();
  } finally {
    systemPingInFlight = false;
  }
  if (activeViewName === "system") renderSystemRoute();
}

function startSystemPing() {
  if (systemPingTimer) return;
  runSystemPing();
  systemPingTimer = window.setInterval(runSystemPing, SYSTEM_PING_INTERVAL_MS);
}

function stopSystemPing() {
  if (!systemPingTimer) return;
  window.clearInterval(systemPingTimer);
  systemPingTimer = null;
}

/** 経路図が読む、アプリ自身の環境。view に無い情報(接続・端末・時計)はここから。 */
function systemRuntime() {
  return {
    connection,
    connectionDetail,
    appOffline: connection === CONNECTION.OFFLINE || connection === CONNECTION.RECONNECTING
      || connection === CONNECTION.UNCONFIGURED,
    inTelegram: inTelegramRuntime(),
    canSend: canSendToBot(),
    platform: tg?.platform && tg.platform !== "unknown" ? String(tg.platform) : "",
    demo: demoMode,
    clockSynced: liveClock.synced,
    ping: systemPing,
    seq: currentView?.seq,
  };
}

function renderSystemRoute() {
  if (!systemBody) return;
  const runtime = systemRuntime();
  const model = deriveRoute(currentView, runtime, serverNow());
  const rebuilt = renderSystemView(systemBody, model, { runtime, nowMs: serverNow() });
  if (rebuilt) { assignCarets(systemBody, SYSTEM_CARETS, "eye"); typeAll(systemBody); }
  if (systemState) {
    const text = `${model.counts.UP}/${model.total} UP · ${model.verdict}`;
    if (systemState.textContent !== text) systemState.textContent = text;
  }
  if (systemView) systemView.dataset.tone = model.tone;
}

/** 設定タブの見出し。いまどのモードが効いているかを一行で出す。 */
function renderSettingsView() {
  renderAutoExtendRow();
  renderUltraAccounts();
  if (!settingsState) return;
  if (autoPending) {
    typeText(settingsState, "AUTO SYNC…");
    return;
  }
  const auto = onePassMode
    ? `AUTO LIVE · ${jstTime(autoArm?.expiresAt).slice(0, 5)}`
    : (!autoStateVerified && !demoMode ? "AUTO UNVERIFIED" : null);
  const on = [auto, ultraMode ? "ULTRA ON" : null, soundMode ? "SOUND ON" : null].filter(Boolean);
  settingsState.dataset.caret = "classic";   // 設定の状態語は静かに
  typeText(settingsState, on.length ? on.join(" + ") : "ALL OFF");
}

// ---------------------------------------------------------------- 口座別 ULTRA 設定

/** 編集中の下書き。null なら server の accountPrefs をそのまま映す。 */
let prefsDraft = null;
let prefsPending = false;

function serverPrefsMap() {
  const map = currentView?.accountPrefs?.map;
  return map && typeof map === "object" ? map : {};
}

function currentPrefsMap() {
  return prefsDraft || serverPrefsMap();
}

/**
 * 口座別 ULTRA 設定の描画。ULTRA を有効にできるのは同時に1口座だけ
 * (ENTRY claim key が口座次元を持たないため。Worker 側でも同じ制限を検証)。
 * 編集中(prefsDraft あり)は server 更新で入力を消さない。
 */
function renderUltraAccounts() {
  if (!ultraAccountsHost) return;
  const accounts = currentView?.accounts?.list || [];
  if (!accounts.length) {
    ultraAccountsHost.hidden = true;
    return;
  }
  ultraAccountsHost.hidden = false;
  const map = currentPrefsMap();
  const rows = accounts.map((account) => {
    const pref = map[account.id] || {};
    const on = pref.ultra === true;
    const target = pref.profitTarget ?? account.profitTarget ?? "";
    const dd = pref.maxDrawdown ?? "";
    return `
      <div class="ultra-account-row ${on ? "is-on" : ""}" data-account="${escapeHtml(account.id)}">
        <div class="ua-head">
          <span class="ua-name">${escapeHtml(account.label || account.id)}</span>
          <span class="ua-buffer micro">DD LEFT $${escapeHtml(Math.round(account.buffer).toLocaleString("en-US"))}</span>
          <button type="button" class="ua-select" data-action="ua-ultra" role="switch"
                  aria-checked="${on ? "true" : "false"}">ULTRA</button>
        </div>
        <div class="ua-inputs">
          <label>TARGET $<input type="number" inputmode="decimal" min="1" step="1"
                 data-field="profitTarget" value="${escapeHtml(String(target))}" placeholder="—"></label>
          <label>DD CAP $<input type="number" inputmode="decimal" min="1" step="1"
                 data-field="maxDrawdown" value="${escapeHtml(String(dd))}" placeholder="AUTO"></label>
        </div>
      </div>`;
  }).join("");
  ultraAccountsHost.innerHTML = `
    <div class="section-label"><span class="micro">ULTRA ACCOUNTS</span>
      <span class="micro">1口座のみ · 達成不能なら見送り</span></div>
    ${rows}
    <button id="uaSave" type="button" class="ua-save" ${prefsPending ? "disabled" : ""}>${
      prefsPending ? "SAVING…" : "SAVE ULTRA SETTINGS"}</button>`;
}

function draftFromDom() {
  const map = {};
  ultraAccountsHost?.querySelectorAll(".ultra-account-row").forEach((row) => {
    const id = row.dataset.account;
    const pref = { ultra: row.querySelector('[data-action="ua-ultra"]')?.getAttribute("aria-checked") === "true" };
    row.querySelectorAll("input[data-field]").forEach((input) => {
      const value = Number(input.value);
      pref[input.dataset.field] = Number.isFinite(value) && value > 0 ? value : null;
    });
    map[id] = pref;
  });
  return map;
}

async function saveAccountPrefs() {
  if (prefsPending) return;
  const draft = draftFromDom();
  if (demoMode) {
    haptic("notification");
    notify("DEMO — prefs not saved");
    return;
  }
  if (!client?.configured) {
    haptic("notification");
    notify("PREFS UNAVAILABLE — reopen from Telegram");
    return;
  }
  prefsPending = true;
  renderUltraAccounts();
  try {
    await client.setAccountPrefs(draft);
    prefsDraft = null;
    const ultraId = Object.keys(draft).find((id) => draft[id].ultra);
    notify(ultraId ? `ULTRA ACCOUNT SET — …${ultraId.slice(-6)}` : "ULTRA ACCOUNT CLEARED");
    haptic("medium");
  } catch (error) {
    notify(`PREFS SAVE FAILED — ${String(error?.message || error)}`);
    haptic("notification");
  } finally {
    prefsPending = false;
    renderUltraAccounts();
  }
}

// 設定タブ内の操作は委譲で受ける(行は描画のたびに作り直されるため)。
ultraAccountsHost?.addEventListener("click", (event) => {
  const save = event.target.closest("#uaSave");
  if (save) { saveAccountPrefs(); return; }
  const toggle = event.target.closest('[data-action="ua-ultra"]');
  if (!toggle) return;
  haptic("light");
  const row = toggle.closest(".ultra-account-row");
  const id = row?.dataset.account;
  const draft = draftFromDom();
  const wasOn = draft[id]?.ultra === true;
  // 同時に1口座だけ: 選んだ行以外は必ず落とす。同じ行を押せば解除。
  for (const key of Object.keys(draft)) draft[key].ultra = false;
  if (!wasOn && id && draft[id]) draft[id].ultra = true;
  prefsDraft = draft;
  renderUltraAccounts();
});
ultraAccountsHost?.addEventListener("input", () => {
  prefsDraft = draftFromDom();
});

/** AUTO カード直下の残り時間+延長ボタン。ON のときだけ出す。 */
function renderAutoExtendRow() {
  if (!autoExtendRow) return;
  autoExtendRow.hidden = !onePassMode;
  if (!onePassMode) return;
  const remaining = Date.parse(autoArm?.expiresAt || "") - serverNow();
  let left = "—";
  if (Number.isFinite(remaining) && remaining > 0) {
    const totalMin = Math.floor(remaining / 60_000);
    left = `${Math.floor(totalMin / 60)}H${String(totalMin % 60).padStart(2, "0")}M LEFT`;
  }
  if (autoExtendInfo) {
    autoExtendInfo.textContent = `LIVE UNTIL ${jstTime(autoArm?.expiresAt).slice(0, 5)} · ${left}`;
  }
  if (autoExtendBtn) autoExtendBtn.disabled = autoPending;
}

/**
 * ヘッダーのモード表示。設定タブへ移したあとも「いま ULTRA が効いている」
 * ことが監視画面から見えるようにする。ULTRA は枚数を桁で変えるので、
 * 別タブに隠れたまま気付かない状態を作らない。
 */
function renderModeBadges() {
  if (!modeBadges) return;
  const badges = [];
  if (onePassMode) badges.push(`<span class="mode-badge mode-onepass">AUTO</span>`);
  if (ultraMode) badges.push(`<i class="ultra-icon" aria-label="ULTRA"></i>`);
  modeBadges.innerHTML = badges.join("");
  modeBadges.hidden = badges.length === 0;
}

function renderLedgerView() {
  // R85: 集計は今の口座の記録だけ(入れ替え前の口座の記録を NET に混ぜない)。
  const { scoped: results, others } = scopeResults(currentView?.recentResults, currentView?.accounts);
  if (ledgerCount) {
    ledgerCount.textContent = results.length
      ? `${String(results.length).padStart(2, "0")} RECORDED`
      : "NO RECORDS";
  }
  renderLedger(ledgerBody, results, { onOpen: (result) => { haptic("light"); openResult(result); }, others });
}

function renderUltraToggle() {
  if (!ultraToggle) return;
  ultraToggle.classList.toggle("is-on", ultraMode);
  ultraToggle.setAttribute("aria-checked", ultraMode ? "true" : "false");
}

function renderSoundToggle() {
  if (!soundToggle) return;
  soundToggle.classList.toggle("is-on", soundMode);
  soundToggle.setAttribute("aria-checked", soundMode ? "true" : "false");
}

/** SOUND の切り替え。ON にした瞬間のタップで再生を解錠し、確認の一声を鳴らす。 */
function toggleSound() {
  soundMode = !soundMode;
  haptic("medium");
  saveModes();
  renderSoundToggle();
  renderSettingsView();
  if (soundMode) soundCue.confirm();
  notify(soundMode ? "SOUND ON — voice cues enabled" : "SOUND OFF");
}

function toggleUltra() {
  ultraMode = !ultraMode;
  haptic("medium");
  saveModes();
  renderUltraToggle();
  renderModeBadges();
  renderSettingsView();
  // デモ中は切り替えたあとすぐ確定操作を試せるようにする。本番の
  // 二度押し防止(submitted)はそのまま。
  if (demoMode) submitted.delete("demo-scenario");
  notify(ultraMode
    ? "ULTRA ON"
    : "ULTRA OFF");
  render();
}

/** デモ中だけ自動発注パネルを叩いて状態を一巡できる。表示の予行演習用。 */
const AUTOTRADE_DEMO_STATES = ["IDLE", "CLAIMED", "STALE", "ROUTING", "MANAGING", "MODIFY"];

/**
 * バッジに出す一語。内部の state 名(CSS クラス `autotrade-<state>` と対)は
 * 変えずに、画面の言葉だけを普通の英語にする。
 *   CLAIMED は「ENTRY の枠を1つ押さえた」であって、注文でも建玉でもない。
 *   RESERVED と読ませれば、注文が出ていないことが一目で分かる。
 */
const AUTOTRADE_LABELS = {
  IDLE: "IDLE",
  CLAIMED: "RESERVED",
  STALE: "STALE SLOT",
  ROUTING: "SENDING",
  MANAGING: "IN TRADE",
  MODIFY: "MOVING STOP",
};
let autotradeDemoIndex = -1;

function cycleAutotradeDemoState() {
  if (!demoMode) return;
  autotradeDemoIndex = (autotradeDemoIndex + 1) % AUTOTRADE_DEMO_STATES.length;
  haptic("light");
  renderAutotradePanel();
}

/**
 * 自動発注(autotrade_engine)の現在地。
 *
 * サーバー状態から導出する表示。AUTO スイッチは期限付きの新規ENTRY権限を
 * Workerへ保存するが、このパネル自体は注文を送らない。
 */
function renderAutotradePanel() {
  if (!autotradePanel) return;
  const view = currentView;
  if (!view) { autotradePanel.hidden = true; return; }
  const order = view.order || null;
  const position = view.position || null;
  const claim = view.entryClaim || null;
  const management = view.managementClaim || null;

  let state = "IDLE";
  let detail = "nothing in flight";
  if (position && Number(position.qty) > 0) {
    state = "MANAGING";
    detail = `holding ${escapeHtml(String(position.side || ""))} ${escapeHtml(String(position.qty))} — minding the stop and targets`;
  } else if (order && ["PENDING", "SENT", "UNKNOWN", "PARTIAL",
    "ENTRY_PARTIAL_ROUTE", "ENTRY_PARTIAL_FILL", "ENTRY_RESTING"].includes(String(order.state))) {
    state = "ROUTING";
    detail = `order sent (${escapeHtml(String(order.state))}) — waiting for the broker to confirm`;
  } else if (claim && ["CLAIMED", "CONSUMED"].includes(String(claim.state))) {
    // 宙吊りの claim を「保持中」と出し続けない。サーバーが「ブローカーが空だと
    // 証明できた」と言っている claim は、次の ENTRY で解放されるだけの残骸で、
    // 何も押さえていない。ここを CLAIMED のままにすると、A/A+ が出ない日は
    // 画面が永久に赤いままになる(2026-08-25)。
    if (claim.staleReleasable === true) {
      state = "STALE";
      detail = "old reservation — the broker is empty, it clears on the next entry";
    } else {
      state = "CLAIMED";
      detail = "slot reserved so only one entry can go out";
    }
  } else if (management && String(management.state) === "CONSUMED") {
    state = "MODIFY";
    detail = "moving the stop and target orders";
  }
  const recovered = claim && String(claim.state) === "RECOVERED";

  // デモ中はパネルを叩いて各状態の見え方を一巡できる。実状態の導出には
  // 一切影響しない(デモを抜ければ元の導出に戻る)。
  if (demoMode && autotradeDemoIndex >= 0) {
    state = AUTOTRADE_DEMO_STATES[autotradeDemoIndex];
    detail = {
      IDLE: "nothing in flight",
      CLAIMED: "slot reserved so only one entry can go out",
      STALE: "old reservation — the broker is empty, it clears on the next entry",
      ROUTING: "order sent — waiting for the broker to confirm",
      MANAGING: "holding SELL 2 — minding the stop and targets",
      MODIFY: "moving the stop and target orders",
    }[state];
  }

  // IDLE は畳むどころか出さない(WATCHTOWER と AUTO レールが常設なので、
  // このパネルはエンジンが実際に動いているときだけ現れる)。デモ中は
  // 状態一巡のタップ台として常に出す。
  const active = state !== "IDLE";
  if (!active && !demoMode) { autotradePanel.hidden = true; return; }
  autotradePanel.hidden = false;
  autotradePanel.classList.toggle("is-active", active);
  autotradePanel.innerHTML = `
    <div class="autotrade-row">
      <span class="micro">${glyph("robot")}AUTOTRADE</span>
      <span class="autotrade-state autotrade-${state.toLowerCase()}">${escapeHtml(AUTOTRADE_LABELS[state] || state)}</span>
    </div>
    ${active ? `<p class="card-meta" data-type>${detail}${recovered ? " · recovered the last reservation" : ""}</p>` : ""}`;
  assignCarets(autotradePanel, AUTOTRADE_CARETS, "eye");
  typeAll(autotradePanel);
}

function render() {
  // 音声キューは view の差分でだけ鳴る(同じ view の再描画では無音)。
  soundCue.observe(currentView);
  // ペットの反応も同じ規律 —— 差分から出来事を 1 つだけ拾う(起動直後の 1 件目は飲み込む)。
  petVoice.observe(currentView);
  renderStatusPill();
  renderWatchtower();
  renderUltraToggle();
  renderModeBadges();
  renderAutotradePanel();
  renderLifeline(lifelineHost, currentView?.accounts || null, {
    weeklyUsd: recentNet(scopeResults(currentView?.recentResults, currentView?.accounts).scoped, 7, serverNow()),
  });
  if (activeViewName === "ledger") renderLedgerView();
  if (activeViewName === "system") renderSystemRoute();

  const nextScenario = currentView?.scenario || null;
  const nextDisplay = currentView?.display || null;
  const nextEvaluation = currentView?.market?.evaluation || null;
  syncOnePass(nextScenario, nextDisplay, nextEvaluation);
  renderOnePassRail();

  if (!currentView) {
    // 表示する view が無い(起動直後、またはデモを実状態が届く前に閉じた)。シナリオのチャート
    // (氷の WebGL と RAF)・武装の印・消えたカードの鉄粉をここでも必ず落とす。落とさないと、
    // 画面から消えたチャートが回り続ける(2026-09-06 のクラッシュと同じ種類の積み上がり)。
    scenarioChart?.destroy();
    scenarioChart = null;
    positionChart?.destroy();
    positionChart = null;
    stateStack.innerHTML = emptyCard(
      connection === CONNECTION.UNCONFIGURED
        ? "NO STATE SERVICE — open from the Bot"
        : "WAITING FOR SERVER STATE",
    );
    if (boot.particles) attachCardFields(stateStack);   // empty には敷かない。消えたカードの鉄粉を片付けるだけ
    document.body.classList.remove("is-armed");
    logo.setState(connection === CONNECTION.OFFLINE || connection === CONNECTION.RECONNECTING ? "alert" : "idle");
    if (signalCount) signalCount.textContent = "—";
    scene3d.setMode("neutral");
    renderPet();   // 目の居場所と一言(場面 = boot / offline)
    chart.disarm();
    return;
  }

  const { position, order, scenario, display } = currentView;
  // R6: 評価カードは market ストリームに載る(シナリオが無い夜も出したいため)。
  const evaluation = nextEvaluation;
  const cards = [];

  // 優先順位: POSITION → EXIT PENDING → ORDER PENDING → SCENARIO → NONE
  // ポジションカードは常に最上部に固定する。
  if (position) cards.push(positionCard(position, currentView));
  if (order && ["PENDING", "SENT", "PARTIAL", "UNKNOWN", "ENTRY_PARTIAL_ROUTE",
    "ENTRY_PARTIAL_FILL", "ENTRY_RESTING"].includes(order.state)) {
    cards.push(orderCard(order, currentView, unstickSection(order, stuckOrderInfo(currentView, serverNow()))));
  }

  // クライアント時計は「隠す」方向にだけ使う。表示を復活させる判断はしない。
  const locallyExpired = scenario && Date.parse(scenario.expiresAt) <= serverNow();
  if (scenario && !locallyExpired && discarded !== scenario.scenarioId) {
    cards.push(scenarioCard(scenario, display, evaluation));
  } else if (!position && !cards.length) {
    // R6: シナリオが無い夜こそ「何が止めているか」を出す。評価が無ければ従来表示。
    if (evaluation) {
      cards.push(gateBoard(
        evaluation,
        evaluation.at ? jstTime(evaluation.at).slice(0, 5) : "",
        marketRuntime(),
      ));
    } else {
      cards.push(emptyCard(
        locallyExpired ? "SCENARIO EXPIRED" : (display.blockReason || "NO SETUP"),
      ));
    }
  }

  // シナリオのチャートは描画ごとに作り直さない(2026-09-06: 作り直すたびに WebGL コンテキスト(氷)
  // と監視が積み上がり、Telegram の WebView が落ちた疑い)。次もシナリオがあれば同じチャートを差し戻す。
  const keepScenarioChart = Boolean(scenarioChart && scenario && !locallyExpired && discarded !== scenario.scenarioId);
  if (!keepScenarioChart) {
    scenarioChart?.destroy();
    scenarioChart = null;
  }
  if (!position && positionChart) {
    positionChart.destroy();
    positionChart = null;
  }
  stateStack.innerHTML = cards.join("");
  mountStandbyEye();
  if (boot.particles) attachCardFields(stateStack);
  assignCarets(stateStack, CARD_CARETS, "eye");
  typeAll(stateStack);
  // R66: 保有中カードのチャート。シナリオと同じ規律 — 描画ごとに作り直さず、生きている
  // チャートを差し戻す(WebGL コンテキストと監視を積み上げない)。建玉が無ければ片付ける。
  if (position) {
    const positionMount = stateStack.querySelector(".position-chart");
    if (positionMount) {
      if (positionChart?.mount && positionChart.mount !== positionMount) {
        positionMount.replaceWith(positionChart.mount);
      } else if (!positionChart) {
        positionChart = initTapeChart(positionMount, { autoPoll: false, webgl: boot.ice });
      }
      positionChart.update(lastMarket);
      const levels = positionLevels(position, currentView);
      positionChart.arm({
        entry: position.avgEntry, stop: levels.stop, target: levels.target,
        accent: sideDirection(position.side) === -1 ? "SHORT" : "LONG",
      });
    }
  }
  // ロゴの目は状態を映す: 武装中は朱赤に脈動、接続断や STALE は警戒。
  {
    const offline = connection === CONNECTION.OFFLINE || connection === CONNECTION.RECONNECTING;
    const stale = Boolean(position && (position.stale || position.verified === false));
    logo.setState(offline || stale ? "alert" : (scenario && display?.orderable ? "armed" : "idle"));
  }
  if (signalCount) {
    signalCount.textContent = position
      ? `POSITION ${position.state}`
      : (scenario && !locallyExpired ? `01 · ${scenario.state}` : "00");
  }

  const confirmButton = stateStack.querySelector('[data-action="confirm"]');
  if (confirmButton) bindSlide(confirmButton, () => confirmOrder(scenario));
  const unstickToggle = stateStack.querySelector('[data-action="unstick-open"]');
  if (unstickToggle && order) {
    unstickToggle.addEventListener("click", () => {
      haptic("light");
      unstickOpenFor = unstickOpenFor === order.idempotencyKey ? null : order.idempotencyKey;
      render();
    });
  }
  const unstickSlider = stateStack.querySelector('[data-action="unstick-confirm"]');
  if (unstickSlider && order) bindSlide(unstickSlider, () => confirmUnstick(order));
  const discardButton = stateStack.querySelector('[data-action="discard"]');
  if (discardButton) discardButton.addEventListener("click", () => {
    // 選択解除のみ。注文も状態も動かさない。
    haptic("light");
    discarded = scenario ? scenario.scenarioId : null;
    scene3d.setMode("neutral");
    chart.disarm();
    render();
    notify("Scenario dismissed on this screen only");
  });

  if (scenario && !locallyExpired) {
    // 武装(発注可)なら 3D の灯りも強くなる。WATCH は通常の傾きだけ。
    const armedVisual = ["ACTIVE", "ARMED"].includes(String(scenario.state).toUpperCase())
      && (display.orderable || demoMode) && !submitted.has(scenario.scenarioId);
    document.body.classList.toggle("is-armed", armedVisual);
    scene3d.setMode(`${scenario.side === "BUY" ? "long" : "short"}${armedVisual ? "-armed" : ""}`);
    chart.arm({ entry: scenario.entry, stop: scenario.stop, target: scenario.target, side: scenario.side });
    const scenarioMount = stateStack.querySelector(".scenario-chart");
    if (scenarioMount) {
      if (scenarioChart?.mount && scenarioChart.mount !== scenarioMount) {
        scenarioMount.replaceWith(scenarioChart.mount);   // 生きているチャートを差し戻す
      } else if (!scenarioChart) {
        scenarioChart = initTapeChart(scenarioMount, { autoPoll: false, webgl: boot.ice });
      }
      scenarioChart.update(lastMarket);
      scenarioChart.arm({ entry: scenario.entry, stop: scenario.stop, target: scenario.target, side: scenario.side });
    } else if (scenarioChart) {
      scenarioChart.destroy();
      scenarioChart = null;
    }
  } else {
    document.body.classList.remove("is-armed");
    scene3d.setMode("neutral");
    chart.disarm();
  }
  // R65: 目の場面(neutral / long / short / 武装 / 一喜一憂)と独り言はペットが一手に決める。
  // カードを積み終えた後に呼ぶこと —— 居場所(カード or 駐在所)はこの時点の DOM で決まる。
  renderPet();
}

function applyMarket(market) {
  lastMarket = market || null;
  marketUiUpdatedAt = Date.now();
  const priceNode = document.getElementById("marketPrice");
  const observedMs = Date.parse(market?.observedAt || market?.at || "");
  const ageMs = Number.isFinite(observedMs) ? Math.max(0, serverNow() - observedMs) : Infinity;
  const verified = Boolean(market?.verified === true && market?.stale !== true && ageMs <= 10 * 60_000);

  const feedLed = document.getElementById("feedLed");
  if (feedLed) {
    feedLed.classList.toggle("live", verified);
    feedLed.classList.toggle("warn", Boolean(market) && !verified);
  }
  if (priceNode) {
    priceNode.textContent = verified ? price(market.price) : "—";
    // 価格が動いた瞬間だけ色が走る(上=acid / 下=rust)。HUD の鼓動。
    if (verified) {
      const last = Number(market.price);
      const prev = Number(priceNode.dataset.last);
      if (Number.isFinite(prev) && Number.isFinite(last) && last !== prev) {
        priceNode.classList.remove("tick-up", "tick-down");
        void priceNode.offsetWidth;
        priceNode.classList.add(last > prev ? "tick-up" : "tick-down");
      }
      priceNode.dataset.last = String(last);
    }
  }

  // 状況は1行に集約する。鮮度が疑わしいときは価格を出さず、その事実だけを書く。
  if (marketStatus) {
    if (!market) {
      marketStatus.innerHTML = `<span class="warn">NO PRICE</span> · waiting for verified feed`;
    } else if (!verified) {
      marketStatus.innerHTML = `<span class="warn">FEED STALE</span> · as of ${escapeHtml(jstTime(market.observedAt || market.at).slice(0, 5))}`;
    } else {
      const parts = [jstTime(market.observedAt || market.at).slice(0, 5)];
      const vwap = market.vwap == null ? NaN : Number(market.vwap);
      const last = Number(market.price);
      if (Number.isFinite(vwap) && Number.isFinite(last)) {
        const diff = last - vwap;
        parts.push(`VWAP ${diff >= 0 ? "+" : "−"}${Math.abs(diff).toFixed(1)}pt`);
      }
      const cvd = market.cvd == null ? NaN : Number(market.cvd);
      if (Number.isFinite(cvd)) parts.push(`CVD ${cvd.toLocaleString("en-US")}`);
      if (market.regime) parts.push(String(market.regime));
      marketStatus.textContent = parts.join(" · ");
    }
  }
  chart.update(market);
  scenarioChart?.update(market);
  // R58: 指標の読み取り値は RADAR タブが描く(開いているときだけ)。
  if (activeViewName === "radar") renderRadarView();
}

// ---------------------------------------------------------------- 決済結果

/**
 * サーバーの result を表示層の trade 契約に落とす。
 *
 * state / pnl / R / held は **渡さない**。表示層が同じ元データから導出するので、
 * ここで計算すると表示と内部が乖離する余地ができる。
 */
function toTrade(result) {
  return {
    side: result.side,
    symbol: result.symbol,
    qty: result.qty,
    entry: result.entry,
    exit: result.exit,
    stop: result.stop,
    pointValue: result.pointValue,
    openedAt: result.openedAt,
    closedAt: result.closedAt,
    path: result.path,
    verdict: result.verdict || "",
    mode: result.mode,
    // R57: 根拠チャート(確定 3 分足・VP 水準・凍結ターゲット・根拠タグ)と、
    // モデル / 等級 / 分割の内訳 / 滑り+手数料。表示層はこれらから MAE/MFE 等を導出する。
    chart: result.chart || null,
    model: result.model || null,
    grade: result.grade || null,
    legs: Array.isArray(result.legs) ? result.legs : null,
    fees: Number.isFinite(result.fees) ? result.fees : null,
    scenarioId: result.scenarioId || null,
  };
}

function openResult(result) {
  if (!result) return;
  // 観測が両端しか無い場合は、その事実を画面に出したまま表示する。
  const note = document.getElementById("result-note");
  if (note) {
    const src = result.pathSource;
    // R57: 根拠チャート(確定 3 分足)があれば、その出所を出す。無ければ従来どおり
    // path の出所を出す。デモの擬似 path は「架空」であることを明示する。
    const bars = result.chart && Array.isArray(result.chart.bars) ? result.chart.bars.length : 0;
    const label = src === "demo-fiction" ? "PATH: DEMO FICTION"
      : bars >= 4 ? ""   // 出所は結果画面の根拠チップ側に出す(見出しと重ねない)
      : src !== "observed-bars" ? "PATH: ENDPOINTS ONLY" : "";
    note.hidden = !label;
    note.textContent = label;
  }
  haptic("medium");
  // ランプが結果の色で一瞬だけ灯る(勝ち acid / 負け rust / FLAT violet)。
  const derived = deriveTrade(result);
  scene3d.flash?.(derived.state === "loss" ? 0xce5a43 : derived.state === "win" ? 0xff3b4f : 0xafa4d9);
  resultView.show(toTrade(result));
}

// ---------------------------------------------------------------- 発注

/**
 * スライドで発火する引き金。右端まで(88%以上)引き切って離したときだけ
 * onFire を1回呼ぶ。途中で離せばバネで戻り、何も起きない。
 * 誤タップ・誤スクロールでは絶対に発注されない。
 */
const SLIDE_FIRE_RATIO = 0.88;

function bindSlide(el, onFire) {
  const thumb = el.querySelector(".slide-thumb");
  const fill = el.querySelector(".slide-fill");
  if (!thumb) return;
  let originX = 0;
  let travel = 0;
  let dx = 0;
  let active = false;
  let fired = false;

  const setX = (x) => {
    dx = x;
    thumb.style.transform = `translateX(${x}px)`;
    if (fill) fill.style.width = `${x + thumb.offsetWidth + 10}px`;
    el.setAttribute("aria-valuenow", String(travel ? Math.round((x / travel) * 100) : 0));
  };
  const down = (event) => {
    if (fired) return;
    active = true;
    travel = el.clientWidth - thumb.offsetWidth - 10;
    originX = event.clientX - dx;
    el.setPointerCapture?.(event.pointerId);
    el.classList.add("sliding");
    el.classList.remove("returning");
    haptic("light");
    scene3d.charge?.(true);   // 引いている間、ランプが張り詰める
    event.preventDefault();
  };
  const move = (event) => {
    if (!active || fired) return;
    setX(Math.min(travel, Math.max(0, event.clientX - originX)));
  };
  const up = () => {
    if (!active || fired) return;
    active = false;
    el.classList.remove("sliding");
    scene3d.charge?.(false);
    if (travel > 0 && dx >= travel * SLIDE_FIRE_RATIO) {
      fired = true;
      setX(travel);
      el.classList.add("fired");
      haptic("medium");
      onFire();
    } else {
      el.classList.add("returning");
      setX(0);
    }
  };
  el.addEventListener("pointerdown", down);
  el.addEventListener("pointermove", move);
  el.addEventListener("pointerup", up);
  el.addEventListener("pointercancel", up);
}

function newIdempotencyKey() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function confirmOrder(scenario, executionMode = "MANUAL_SLIDE") {
  if (!scenario) return;
  if (submitted.has(scenario.scenarioId)) return;
  if (demoMode) {
    // デモは絶対に送信しない。手応え(長押し→発火)だけを再現する。
    // ULTRA ON のときは「本番なら何枚がどの口座へ行くのか」まで見せる。
    // 枚数の正本は Bot 側の ultra_mode.py なので、ここは予行演習の表示。
    let label = "DEMO";
    if (ultraMode) {
      const plan = buildUltraPlan(
        { side: scenario.side, entry: scenario.entry, stop: scenario.stop, target: scenario.target },
        currentView?.accounts?.list || [], { signalQty: scenario.qty });
      const eligible = plan.accounts.filter((row) => row.verdict === "ELIGIBLE");
      const skipped = plan.accounts.filter((row) => row.verdict !== "ELIGIBLE");
      // 確定後の表示は一行。内訳は上の表にあるので繰り返さない。
      label = eligible.length
        ? `DEMO · ${plan.totalQty} CONTRACTS`
        : "DEMO · NONE";
      notify(eligible.length
        ? `DEMO: ${scenario.qty} → ${plan.totalQty} (not sent)`
        : "DEMO: no eligible account");
    } else {
      notify(`DEMO: qty ${scenario.qty} (not sent)`);
    }
    submitted.set(scenario.scenarioId, label);
    scene3d.flash?.(0xff3b4f);
    soundCue.manualSend();   // 手動送信の一声(デモは送らないが手応えは同じ)
    render();
    return;
  }
  if (!canSendToBot()) {
    notify("OPEN IN TELEGRAM");
    return;
  }

  // 押した瞬間に無効化する。二度目のタップはこの時点で弾かれる。
  submitted.set(scenario.scenarioId, "SENDING…");
  // 手動送信の一声。以後 90 秒内に server へ現れる同じ注文は自動発注と読まない。
  soundCue.manualSend();
  const button = stateStack.querySelector('[data-action="confirm"]');
  if (button) {
    button.disabled = true;
    button.textContent = "SENDING…";
  }
  haptic("medium");

  const payload = {
    type: "scenario_order_confirmed",
    scenario: {
      scenarioId: scenario.scenarioId,
      fingerprint: scenario.fingerprint,
      evidenceHash: scenario.evidenceHash,
      symbol: scenario.symbol,
      side: scenario.side,
      qty: scenario.qty,
      entry: scenario.entry,
      stop: scenario.stop,
      target: scenario.target,
      targets: scenario.targets,
      targetR: scenario.targetR,
      legs: scenario.legs,
      planVersion: scenario.planVersion,
      state: scenario.state,
      grade: scenarioGrade(scenario, currentView?.market?.evaluation || null),
      orderable: currentView?.display?.orderable === true,
      issuedAt: scenario.issuedAt,
      expiresAt: scenario.expiresAt,
    },
    clientNonce: newIdempotencyKey(),
    executionMode,
    // ULTRA は「意図」だけを送る。実際に発注される枚数は Bot が
    // ultra_mode.py で口座別に再計算した値で、この画面の数字ではない。
    ultraMode,

    source: "nqx-nightwatch-mini-app",
  };

  try {
    tg.sendData(JSON.stringify(payload));
    submitted.set(scenario.scenarioId, "SENT — Bot verifying");
    notify("Confirmation sent // the bot re-validates before routing");
  } catch (error) {
    submitted.delete(scenario.scenarioId);
    notify("SEND FAILED — reopen from Telegram");
  }
  render();
}

/**
 * 観測漏れ注文表示の解除要求。発注ではない — order ストリームの表示状態を
 * CANCELED として記録するだけで、ブローカーの注文・建玉には触れない。
 * それでも送信は発注と同じ厳格さで扱う(スライド発火・一回限り・Bot 再検証)。
 */
function confirmUnstick(order) {
  if (!order || submittedUnstick.has(order.idempotencyKey)) return;
  if (demoMode) {
    notify("DEMO: not sent");
    return;
  }
  if (!canSendToBot()) {
    notify("OPEN IN TELEGRAM");
    return;
  }
  submittedUnstick.add(order.idempotencyKey);
  haptic("medium");
  try {
    tg.sendData(JSON.stringify(buildUnstickPayload(order, newIdempotencyKey())));
    notify("UNSTICK SENT // Bot re-verifies broker FLAT");
  } catch (error) {
    submittedUnstick.delete(order.idempotencyKey);
    notify("SEND FAILED — reopen from Telegram");
  }
  render();
}

// ---------------------------------------------------------------- デモモード(テスト画面)

const tick025 = (value) => Math.round(value * 4) / 4;

/**
 * デモ用の架空 path。決定論の正弦合成で entry→exit へ向かい、中盤に一度
 * SL 側へ寄る(ボックスとローソクの関係を確認するための「それらしい」形)。
 * 乱数は使わない。SL は踏み抜かない(踏んだら決済のはずなので嘘になる)。
 */
function demoPath(entry, exit, stop, n = 72) {
  const drift = exit - entry;
  const riskDir = Math.sign(entry - stop) || 1;
  const seed = Math.abs(Math.sin(entry * 0.37)) * 6.28;
  const amp = Math.max(Math.abs(drift), 8);
  const out = [];
  for (let i = 0; i < n; i += 1) {
    const f = i / (n - 1);
    const wave = Math.sin(seed + f * 9.4) * 0.30 + Math.sin(seed * 1.7 + f * 23) * 0.16;
    const pull = Math.sin(f * Math.PI) * 0.22 * -riskDir;
    let px = entry + drift * (f * f * (3 - 2 * f)) + (wave + pull) * amp;
    if (riskDir > 0) px = Math.max(px, stop + 1);
    else px = Math.min(px, stop - 1);
    out.push(tick025(px));
  }
  out[0] = entry;
  out[n - 1] = exit;
  return out;
}

/**
 * R57: デモ用の根拠チャート(架空)。擬似 path を保有中の足に畳み、前後に文脈の足を足す。
 * source は "demo-fiction" で、本番の observed 足と混同させない。
 */
function demoChart(side, entry, exit, stop, openedAt, closedAt, path) {
  const tf = 180;
  const open = Math.floor(Date.parse(openedAt) / 1000 / tf) * tf;
  const close = Math.floor(Date.parse(closedAt) / 1000 / tf) * tf;
  const dir = side === "SHORT" ? -1 : 1;
  const bars = [];
  // 建玉前 12 本: 逆方向へ伸びてから建値へ戻る(スイープ→受容失敗の形)
  for (let i = 12; i >= 1; i -= 1) {
    const f = i / 12;
    const c = tick025(entry - dir * (Math.sin(f * Math.PI) * 18 + f * 4));
    const o = tick025(c + dir * 1.5);
    bars.push([open - i * tf, o, Math.max(o, c) + 2.25, Math.min(o, c) - 2, c]);
  }
  const holdBars = Math.max(1, Math.round((close - open) / tf));
  for (let k = 0; k < holdBars; k += 1) {
    const s = Math.floor((k * path.length) / holdBars);
    const e = Math.max(s + 1, Math.floor(((k + 1) * path.length) / holdBars));
    const bucket = path.slice(s, e);
    bars.push([open + k * tf, bucket[0], Math.max(...bucket), Math.min(...bucket), bucket[bucket.length - 1]]);
  }
  // 決済後 5 本: 決済価格から惰性で続く
  for (let i = 1; i <= 5; i += 1) {
    const c = tick025(exit + dir * (exit - entry >= 0 ? 1 : -1) * i * 1.75);
    const o = tick025(c - dir * 1);
    bars.push([close + i * tf, o, Math.max(o, c) + 1.75, Math.min(o, c) - 1.5, c]);
  }
  const risk = Math.abs(entry - stop);
  return {
    version: "NQX-RESULT-CHART/1", tf, bars,
    levels: [
      { label: "C: VAH", price: entry + dir * 6.5 }, { label: "C: POC", price: entry - dir * 32 },
      { label: "P: VAH", price: entry + dir * 19.25 },
    ],
    tp1: tick025(entry + dir * risk * 1.8), tp2: tick025(entry + dir * risk * 6),
    evidence: ["CVD_ALIGNED", "VP_ACCEPTED", "ICT_KILLZONE"], penalties: ["HTF_CONFLICT"],
    htf: { "1h": dir > 0 ? "DOWN" : "UP", "4h": "MIXED" }, volRatio: 0.24, noise: 14.25,
    session: "NY PM", source: "demo-fiction",
  };
}

/** デモ用の決済記録。実在の値動きを模した固定値(乱数は使わない)。 */
function demoResults(now) {
  const mk = (i, side, entry, exit, stop, minutesAgo, verdict, held = 14, model = null, grade = null) => {
    const openedAt = new Date(now - (minutesAgo + held) * 60_000).toISOString();
    const closedAt = new Date(now - minutesAgo * 60_000).toISOString();
    const path = demoPath(entry, exit, stop);
    return {
      resultId: `demo-result-${i}`,
      side, symbol: "MNQU6", qty: 2,
      entry, exit, stop, pointValue: 2,
      openedAt, closedAt,
      // 架空シナリオのローソク描画を確認できるよう、72点の擬似 path を持たせる
      path,
      pathSource: "demo-fiction", pathPoints: 72, exitSource: "manual",
      verdict, mode: "SIMULATION", accountId: "APEX-01",
      // R48: モデル別スコアカードの表示確認用。無い記録(手動)も混ぜる。
      model, grade,
      // R57: 根拠チャート(架空)。モデル付きの記録だけに持たせ、無い記録は旧描画に落ちる。
      chart: model ? demoChart(side, entry, exit, stop, openedAt, closedAt, path) : null,
    };
  };
  // verdict は本番では nqx_state.py の100本テーブルから決定論で選ばれる。
  // デモはその声を6本だけ引用する。
  return [
    mk(6, "SHORT", 30126.0, 30098.5, 30147.0, 240, "They let this one through.", 14, "TURTLE_SOUP_REVERSAL", "A+"),
    mk(5, "LONG", 30062.0, 30049.0, 30049.0, 900, "The stop did its job.", 14, "VP80_REVERSION", "A"),
    mk(4, "SHORT", 29645.0, 29590.0, 29673.0, 3060, "Charted, waited, taken.", 14, "TURTLE_SOUP_REVERSAL", "A"),
    mk(3, "LONG", 29612.0, 29640.0, 29598.0, 3300, "Taken early. Still taken.", 14, "OTE_FVG_PULLBACK", "A"),
    mk(2, "LONG", 29520.0, 29521.0, 29500.0, 6180, "The night shrugged."),
    mk(1, "SHORT", 29800.0, 29726.0, 29830.0, 6300, "Night work, paid in full.", 14, "VP80_REVERSION", "A+"),
  ];
}

/** デモのローソク本数。既定は本番と同じ60(2026-08-16 ユーザー選定)。 */
let demoBarCount = 60;

/**
 * デモの場面。
 *
 *   ARMED : 武装シナリオを出す。ULTRA の口座別枚数と確定操作を試せる。
 *   STUCK : 観測漏れで残った SENT 注文。解除導線のリハーサル用。
 *
 * 本番の表示優先順位(注文が残っている間はシナリオを出さない)は一切
 * 変えない。デモでどちらの状態を作るかを選ぶだけ。
 */
let demoScene = "ARMED";

/** デモの市況。正弦波ベースの決め打ちで、乱数も本物の観測値も使わない。 */
function demoView() {
  const now = serverNow();
  const bars = [];
  const n = demoBarCount;
  let close = 30060;
  for (let i = 0; i < n; i += 1) {
    const open = close;
    close = tick025(30060 + (i * 34) / n + Math.sin(i / 4.6) * 16 + Math.sin(i / 1.7) * 5);
    if (i === n - 1) close = 30092.25;
    bars.push({
      t: Math.floor((now - (n - i) * 180_000) / 1000),
      o: open,
      h: tick025(Math.max(open, close) + 2.5 + Math.abs(Math.sin(i / 2.3)) * 3),
      l: tick025(Math.min(open, close) - 2.5 - Math.abs(Math.cos(i / 2.9)) * 3),
      c: close,
      v: 40 + ((i * 13) % 42),
    });
  }
  const iso = new Date(now).toISOString();
  return {
    accountId: "demo", symbol: "MNQU6", seq: 0,
    serverTime: iso, lastVerifiedAt: iso,
    revisions: {},
    scenario: demoScene === "RUNNER" || demoScene === "GATES" ? null : {
      scenarioId: "demo-scenario", fingerprint: "demo", state: "ARMED",
      symbol: "MNQU6", side: "SELL", qty: 2,
      entry: 30126.0, stop: 30147.0, target: 30076.0,
      targets: [30105.0, 30076.0], targetR: [1, 50 / 21],
      legs: [{ id: "TP1", qty: 1, target: 30105.0 },
        { id: "RUNNER", qty: 1, target: 30076.0 }],
      planVersion: "R18-DEMO-SPLIT-1",
      riskDollars: 42, rr: 50 / 21,
      title: "Lower-high rejection", reason: "Swing to the range low retest (demo)",
      issuedAt: new Date(now - 2 * 60_000).toISOString(),
      observedAt: iso,
      expiresAt: new Date(now + 9 * 60_000).toISOString(),
      snapshotAt: iso,
    },
    // RUNNER 場面: TP1 が約定済みで runner 1枚がトレール中。ライフサイクル
    // カード(ステージレール・脚表示・SL確保額)のリハーサル用。
    position: demoScene === "RUNNER" ? {
      verified: true, source: "demo", symbol: "MNQU6",
      side: "SHORT", qty: 1, initialQty: 2,
      avgEntry: 30126.0, stop: 30122.0, target: 30076.0,
      currentPrice: 30092.25, unrealizedPnl: 67.5, state: "OPEN",
      filledAt: new Date(now - 22 * 60_000).toISOString(),
      observedAt: iso, positionGeneration: "demo-gen",
    } : null,
    entryClaim: demoScene === "RUNNER" ? {
      state: "CONSUMED",
      tuple: { scenarioId: "demo-scenario" },
      executionIntent: { targets: ["30105.00", "30076.00"] },
    } : null,
    positionCheck: { verified: true, source: "demo", observedAt: iso },
    // R55: SYSTEM ROUTE のリハーサル用。ブローカー観測・ループビーコン・AUTO 権限。
    // GATES 場面だけ「窓が閉じたあと」を写すので、ビーコンも古くする。
    brokerObservation: { observedAt: iso, platform: "demo-tradovate", accountId: "APEX-01" },
    cycleHealth: {
      status: demoScene === "GATES" ? "BLOCKED" : "PUBLISHED",
      at: iso, publishedAt: iso,
      reason: demoScene === "GATES" ? "acquisition — required raw acquisition is not fresh" : null,
      kill: false,
    },
    autotradeArm: {
      schemaVersion: "NQX_AUTOTRADE_ARM/1", armId: "demo-arm", enabled: true, autotrade: true,
      live: true, status: "LIVE", source: "demo", armedAt: new Date(now - 60 * 60_000).toISOString(),
      expiresAt: new Date(now + 6 * 60 * 60_000).toISOString(), accountScope: ["APEX-01"], symbol: "MNQU6",
    },
    // 観測漏れで残った SENT の再現。解除導線(2段階+スライド)をデモで
    // リハーサルできる。confirmUnstick はデモ中必ず遮断される。
    order: demoScene === "STUCK" ? {
      idempotencyKey: "demo-stuck-order", state: "SENT", side: "SELL", qty: 2,
      entry: 30126.0, stop: 30147.0, target: 30076.0, receipt: "HTTP 200",
      at: new Date(now - 15 * 60_000).toISOString(),
    } : null,
    market: {
      at: iso, observedAt: iso, publishedAt: iso, price: 30092.25, change: -12.0, changePct: -0.04,
      vwap: 30088.16, cvd: 2086, regime: "MX",
      source: "DEMO", sourceSymbol: "DEMO:MNQ1!",
      resolution: "3", barResolution: "3",
      // GATES 場面だけ「窓が閉じたあとの古いサイクル」を再現する。
      // DATA ゲートが受領書は健全なまま MARKET STALE で落ちる見え方の確認用。
      verified: true,
      stale: demoScene === "GATES",
      ageMs: demoScene === "GATES" ? 6_130_000 : 0,
      // シナリオが無い夜のゲートボードと、最有力モデルのカードのリハーサル。
      // 2026-09-01 に実機で出ていた状態(A+ なのに TARGET_HEADROOM_INSUFFICIENT
      // で WATCH 止まり)をそのまま写してある。
      evaluation: {
        at: iso,
        volGate: { ratio: 0.26, noise: 15.5, slCap: 60, ruling: "通常" },
        dataGate: { status: "FRESH", requiredFresh: true, freshCount: 8,
          requiredCount: 8, oldestAgeSec: 96, sourceSpanSec: 60, staleRequired: [] },
        rotation: { negations: 1, signals: 5, verdict: "OK" },
        msnr: { label: "C: POC", chainState: "FLIP_HELD", allowed: false,
          barsLeft: 0, blockers: ["ANCHOR_BROKEN"] },
        cvdGate: { status: "FRESH", available: true, freshness: "FRESH",
          aplusAllowed: true, attempts: 1, maxAttempts: 2, refreshRequired: false },
        sessionGate: { window: "PM_TREND", label: "NY午後", tradeable: true, et: "14:58" },
        decision: {
          model: "BREAKER_CONTINUATION", side: "SELL", state: "WATCH",
          grade: "A+", score: 10, entry: 29411.0, stop: 29431.5,
          targets: [], targetR: [], hardBlockers: ["TARGET_HEADROOM_INSUFFICIENT"],
        },
        advisory: { vwap: { side: "SELL", state: "VWAP_ACCEPTED", drift: 0.0 } },
      },
      bars,
      // R56: 凡例とチャート重ね描きのリハーサル用。値は 09-04 の実 publish を写した架空値。
      indicators: {
        vwapHi: 30112.5, vwapLo: 30064.25, vwapAnchorT: now / 1000 - 6 * 3600,
        cvd: { value: -19025, fast: -26574, slow: -34031, bias: "BULLISH", status: "FRESH",
          history: [-21883, -21524, 59999, -17188, -19748, -18463, -15304, -14717, -16656, -17467, -19025] },
        po3: "MANIPULATION_UP",
        smt: { peer: "MES1!", bias: "BULLISH", agree: true, position: 0.02 },
        ct: { trend: -1, atr: 37.6, fib618: 30131.5, trail: 30140.75, ltfTrend: 1, ltfFib618: 30070.25,
          htfFib618: 30017.5, hardStop: 30149.0 },
        event: "NONE",
      },
      strategyEvidence: {
        version: "R14-STRATEGY-EVIDENCE-1", asOf: iso, sessionId: "DEMO", source: "demo", provenance: "demo",
        evidenceHash: "demo", models: {
          _matrix: { activeModels: ["ifvg", "htf"], alignment: { BUY: 1, SELL: 0 } },
          htf: { status: "MIXED", valid: true, bias: null, frames: {
            "1d": { structure: "MIXED", ema20: 29408.6, emaSlope5: -28.8, atr14: 413.6 },
            "4h": { structure: "BEARISH", ema20: 29610.2, emaSlope5: -12.1, atr14: 148.2 },
            "1h": { structure: "MIXED", ema20: 30052.5, emaSlope5: 8.5, atr14: 94.5 },
            "45m": { structure: "BULLISH", ema20: 30061.1, emaSlope5: 11.2, atr14: 80.1 } } },
          vwapReversion: { status: "OBSERVE", vwap: 30088.16, ema: 30079.3, atr: 14.1, distance: 4.1 },
          liquidity: { status: "OBSERVED", pools: [
            { side: "BSL", price: 30119.75, kind: "SWING_HIGH" }, { side: "SSL", price: 30047.0, kind: "SWING_LOW" },
            { side: "BSL", price: 30104.25, kind: "SWING_HIGH" } ], dol: { BUY: 30119.75, SELL: 30047.0 } },
          ict: { status: "INTEGRATED", fvg: { BULL: [{ lo: 30071.0, hi: 30078.5, mid: 30074.75,
            createdAt: now / 1000 - 27 * 180, ageBars: 27, displacementR: 1.2 }], BEAR: [] } },
          ifvg: { status: "CONFIRMED", direction: "BUY", zones: [{ lo: 30062.0, hi: 30064.5, direction: "BUY", active: true }] },
          crt: { status: "WATCH", rangeHigh: 30099.5, rangeLow: 30086.25 },
          quarterly: { status: "WATCH", stage: "D", quarter: 3 },
          mmxm: { status: "WATCH", mss: "BEARISH" },
          gann: { status: "COMPUTED", anchor: { direction: "UP", pivot: 30047.0, pivotT: now / 1000 - 36 * 180, pricePerBar: 1.75 },
            angles: [{ label: "Gann 1x1", price: 30110.0, primary: true, slopePerBar: 1.75 }] },
        } },
      levels: [
        { label: "C: POC", price: 30078.25 },
        { label: "C: VAH", price: 30104.25 },
        { label: "C: VAL", price: 30052.75 },
        { label: "P: POC", price: 30036.5 },
        { label: "Weekly open", price: 30061.0 },
        { label: "6pm open", price: 30070.0 },
        { label: "VWAP HI", price: 30145.57 },
        { label: "Asia Low", price: 30124.25 },
        { label: "Session VWAP", price: 30088.16 },
        { label: "NY Low", price: 30025.0 },
        // 窓内で実際に付いた高値・安値。レベル起点描画(該当足の
        // top/bottom からレイが伸びる)をデモで確認するためのもの
        { label: "WIN HIGH", price: Math.max(...bars.map((b) => b.h)) },
        { label: "WIN LOW", price: Math.min(...bars.map((b) => b.l)) },
      ],
    },
    result: null,
    recentResults: demoResults(now),
    accounts: {
      observedAt: iso, source: "demo", totalBuffer: 2500 + 3000 + 5000 + 800 + 4000,
      // デモのシグナルは SELL 30,126 / SL 30,147 / TP 30,076(SL 21pt・TP 50pt)。
      // 口座は ULTRA の仕様例そのままにしてあるので、ULTRA を ON にすると
      // 30 / 60 / 90枚 という仕様どおりの数字がそのまま出る。
      // APEX-04 だけ残DDが薄く、INELIGIBLE 側の見え方も同じ画面で確認できる。
      // APEX-05 は残DD $4,000 の架空口座。目標 $9,000 なら 90枚・損失 $3,780 で
      // 残DD の 94.5% を使う。ぎりぎり ELIGIBLE になる境界をデモで踏める。
      list: [
        { id: "APEX-01", label: "APEX-01", cap: 180, buffer: 2500, profitTarget: 3000, equity: 52500 },
        { id: "APEX-02", label: "APEX-02", cap: 180, buffer: 3000, profitTarget: 6000, equity: 53000 },
        { id: "APEX-03", label: "APEX-03", cap: 180, buffer: 5000, profitTarget: 9000, equity: 55000 },
        { id: "APEX-04", label: "APEX-04", cap: 180, buffer: 800, profitTarget: 9000, equity: 50800 },
        { id: "APEX-05", label: "APEX-05", cap: 180, buffer: 4000, profitTarget: 9000, equity: 54000 },
      ],
      // 口座名簿の突合。デモでは「ブローカーに未設定の口座が1つある」状態を写し、
      // SYSTEM ROUTE の WARN と LIFELINE 直下の注記を同時に確認できるようにする。
      sync: { verified: true, missing: [], unknown: ["APEX-06"], dead: [] },
    },
    // 未解決の注文が残っている間、本番のサーバーはシナリオを発注不可にする。
    // デモでもそこを揃える(STUCK 場面で「発注可」を出すと本番と食い違う)。
    display: demoScene === "STUCK"
      ? { priority: "ORDER_PENDING", orderable: false,
          blockReason: "UNRESOLVED ORDER" }
      : demoScene === "RUNNER"
        ? { priority: "POSITION_OPEN", orderable: false,
            blockReason: "POSITION OPEN — MANAGEMENT ONLY" }
        : demoScene === "GATES"
          ? { priority: "NO_SETUP", orderable: false, blockReason: "NO SETUP" }
          : { priority: "SCENARIO_ACTIVE", orderable: true, blockReason: null },
  };
}

function setDemoMode(on) {
  if (on === demoMode) return;
  demoMode = on;
  document.body.classList.toggle("demo", on);
  discarded = null;
  if (on) {
    lastRealView = currentView;
    currentView = demoView();
    submitted.delete("demo-scenario");
    submittedUnstick.delete("demo-stuck-order");
    unstickOpenFor = null;
    applyMarket(currentView.market);
    notify("DEMO — orders blocked · SOUND ON");
  } else {
    currentView = lastRealView;
    applyMarket(currentView ? currentView.market : null);
    notify("DEMO off");
  }
  // R74: デモの出入りは差分ではない(実状態→デモ画面の差を「決済」等と読まない)。
  soundCue.rebase(currentView);
  render();
  // デモは音を聞く場所。SCENE の切り替え(武装→注文→約定→決済)がそのまま
  // 聞こえるよう、このセッションだけ SOUND を ON にする(端末設定には保存しない)。
  // render() の**後**に入れる: 起動時の最初のデモ描画を無操作で鳴らそうとしない
  // (タップ前の play() は拒否されるうえ、12秒の重複抑止だけが残ってしまう)。
  if (on && !soundMode) { soundMode = true; renderSoundToggle(); }
}

// ---------------------------------------------------------------- ステッカー一覧(デモ専用)
// 全ステッカーを動く状態で参照する閲覧専用ギャラリー。発注経路・実データとは
// 一切繋がらない。入口はデモバナーの ✦ STICKERS のみ(= デモ中しか出ない)。
const gallery = document.getElementById("stickerGallery");
const galleryGroups = document.getElementById("sticker-groups");
const galleryZoom = document.getElementById("sticker-zoom");

function openStickerGallery() {
  if (!galleryGroups.childElementCount) {
    // 初回だけ台帳から組み立てる。img は lazy なので61本並べても
    // 取得されるのは画面に近いものだけ。
    const LABELS = { win: ["WIN", "#C6F038"], loss: ["LOSS", "#FF2E4C"], flat: ["FLAT", "#8C93A0"] };
    for (const [state, names] of Object.entries(STICKERS)) {
      const sec = document.createElement("section");
      sec.className = "sticker-group";
      const head = document.createElement("h3");
      head.textContent = `${LABELS[state][0]} · ${names.length}`;
      head.style.color = LABELS[state][1];
      sec.appendChild(head);
      const grid = document.createElement("div");
      grid.className = "sticker-grid";
      for (const name of names) {
        const fig = document.createElement("figure");
        fig.className = "sticker-cell";
        const img = document.createElement("img");
        img.loading = "lazy"; img.decoding = "async"; img.alt = name;
        img.src = `/stickers/${name}.gif`;
        const cap = document.createElement("figcaption");
        cap.textContent = name;
        fig.append(img, cap);
        fig.addEventListener("click", () => {
          galleryZoom.querySelector("img").src = img.src;
          document.getElementById("sticker-zoom-name").textContent = name;
          galleryZoom.hidden = false;
          haptic("light");
        });
        grid.appendChild(fig);
      }
      sec.appendChild(grid);
      galleryGroups.appendChild(sec);
    }
    document.getElementById("sticker-count").textContent =
      `${Object.values(STICKERS).flat().length}本`;
  }
  gallery.hidden = false;
}

// ---------------------------------------------------------------- 音声キューの試聴(デモ専用)
// 全キューを一覧にして、1本ずつ / 順に鳴らす。SOUND スイッチとは独立(明示のタップ)。
const voiceGallery = document.getElementById("voiceGallery");
const voiceRows = document.getElementById("voice-rows");
const VOICE_LABELS = {
  SCENARIO_ARMED: "武装シナリオ", ORDER_SENT_MANUAL: "手動で注文", AUTO_ORDER_SENT: "自動発注",
  ORDER_FILLED: "約定", TP1_FILLED: "TP1 約定",
  POSITION_CLOSED_WIN: "決済(勝ち)", POSITION_CLOSED_LOSS: "決済(負け)", SOUND_ON: "SOUND ON",
};
function openVoiceGallery() {
  if (!voiceGallery || !voiceRows) return;
  if (!voiceRows.childElementCount) {
    voiceRows.innerHTML = Object.keys(CUES).map((cue) => `
      <button type="button" class="voice-row" data-cue="${cue}">
        <span class="voice-row-label">${escapeHtml(VOICE_LABELS[cue] || cue)}</span>
        <span class="voice-row-text">${escapeHtml(CUE_TEXT[cue])}</span>
        <span class="voice-row-play" aria-hidden="true">&#9654;</span>
      </button>`).join("");
    const count = document.getElementById("voice-count");
    if (count) count.textContent = `${Object.keys(CUES).length} CUES`;
    voiceRows.addEventListener("click", (event) => {
      const row = event.target.closest?.(".voice-row");
      if (!row) return;
      haptic("light");
      soundCue.audition(row.dataset.cue);
    });
    document.getElementById("voice-all")?.addEventListener("click", () => {
      haptic("medium");
      soundCue.auditionAll();
    });
  }
  voiceGallery.hidden = false;
}
document.getElementById("demo-voice")?.addEventListener("click", () => {
  haptic("light");
  openVoiceGallery();
});
document.getElementById("voice-close")?.addEventListener("click", () => {
  if (voiceGallery) voiceGallery.hidden = true;
});

document.getElementById("demo-stickers")?.addEventListener("click", () => {
  haptic("light");
  openStickerGallery();
});
// ローソク本数の選定用セレクタ。デモの市況を作り直して即反映する。
document.getElementById("demo-scene")?.addEventListener("change", (event) => {
  const value = String(event.target.value || "").toUpperCase();
  if (!["ARMED", "STUCK", "RUNNER", "GATES"].includes(value)) return;
  demoScene = value;
  if (demoMode) {
    submitted.delete("demo-scenario");
    submittedUnstick.delete("demo-stuck-order");
    unstickOpenFor = null;
    currentView = demoView();
    applyMarket(currentView.market);
    render();
    haptic("light");
  }
});

// R62: 最後のカーソルの型。表示専用の端末設定(発注・状態の正本ではないので localStorage に覚える)。
// 選ぶと表示中の [data-type] を打ち直すので、その場で型を見比べられる。
let caretVariant = applyCaretVariant((() => {
  try { return window.localStorage?.getItem(CARET_STORAGE_KEY); } catch { return null; }
})());
const caretSelect = document.getElementById("demo-caret");
if (caretSelect) {
  caretSelect.value = caretVariant;
  caretSelect.addEventListener("change", (event) => {
    caretVariant = applyCaretVariant(String(event.target.value || ""));
    caretSelect.value = caretVariant;
    try { window.localStorage?.setItem(CARET_STORAGE_KEY, caretVariant); } catch { /* 保存できなくても続行 */ }
    [stateStack, autotradePanel, systemBody].forEach((host) => { if (host) replayAll(host); });
    haptic("light");
  });
}

document.getElementById("demo-bars")?.addEventListener("change", (event) => {
  const parsed = Number(event.target.value);
  if (!Number.isFinite(parsed) || parsed < 5) return;
  demoBarCount = Math.min(240, Math.round(parsed));
  if (demoMode) {
    currentView = demoView();
    applyMarket(currentView.market);
    render();
    haptic("light");
  }
});
document.getElementById("sticker-close")?.addEventListener("click", () => {
  gallery.hidden = true;
  galleryZoom.hidden = true;
});
galleryZoom?.addEventListener("click", () => { galleryZoom.hidden = true; });

// ---------------------------------------------------------------- 夜警の御神籤(OMEN)
// Balance の数字タップで開く純装飾の遊び。ステッカー61本をスロット風に
// 回してから「今夜の一枚」(JST日付シードで決定論)に着地する。
// 判断・発注・状態のどれにも触れない。
const OMEN_LINES = [
  "今夜は骨まで静観。板が語るまで座して待て",
  "61.8 は二度撫でてから。一度目は挨拶にすぎぬ",
  "勝ったら閉じよ。それが最大の御利益である",
  "取り逃しは供物。追えば祟る",
  "確定足のみが真実。形成中の影に誓いを立てるな",
  "SL を動かした手は、次の夜に震える",
  "3分足の一本は「普通の波」。緩衝なき杭は流される",
  "残機は命数。$60 の外に幸運はない",
  "膠着六時間の沈黙は、二撃の勝ちに化ける(八月十二日の故事)",
  "サイズを倍にした夜、帳簿は倍速で燃える",
  "VWAP の下で笑う者は、上で泣いた者である",
  "指標が凍ったら筆を置け。凍った数字は嘘をつく",
  "戻り売りは礼儀正しく。陰線の確定を待ってから",
  "今夜の敵は相場ではなく、残り $141.50 の欲である",
  "目標に触れたら灯を消せ。夜警の朝は早い",
  "封鎖の刻(指標前後)は刃を鞘に。開けてから抜け",
];
const ALL_STICKERS = [...STICKERS.win, ...STICKERS.loss, ...STICKERS.flat];

function omenOfTonight() {
  // JST の日付でシード(同じ夜は何度引いても同じ = 御神籤の作法)
  const jst = new Date(Date.now() + 9 * 3600_000).toISOString().slice(0, 10);
  let h = 2166136261;
  for (const ch of jst) h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
  h = h >>> 0;
  return {
    sticker: ALL_STICKERS[h % ALL_STICKERS.length],
    line: OMEN_LINES[(h >> 8) % OMEN_LINES.length],
  };
}

const omenEl = document.getElementById("omen");
let omenTimer = null;

function openOmen() {
  const card = omenEl.querySelector(".omen-card");
  const img = omenEl.querySelector("img");
  const line = omenEl.querySelector(".omen-line");
  const pick = omenOfTonight();
  omenEl.hidden = false;
  haptic("medium");
  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  if (omenTimer) window.clearInterval(omenTimer);
  if (reduced) {
    img.src = `/stickers/${pick.sticker}.gif`;
    line.textContent = pick.line;
    return;
  }
  // スロット演出: 全プールを高速で流してから今夜の一枚に着地
  card.classList.add("rolling");
  line.textContent = "……";
  let i = (Date.now() / 97 | 0) % ALL_STICKERS.length;
  let spins = 0;
  const rollStart = performance.now();
  omenTimer = window.setInterval(() => {
    spins += 1;
    i = (i + 7) % ALL_STICKERS.length;
    img.src = `/stickers/${ALL_STICKERS[i]}.gif`;
    // 回数 or 時間のどちらかで必ず着地(バックグラウンドのタイマー
    // スロットリングでも御神籤が回りっぱなしにならない)
    if (spins >= 14 || performance.now() - rollStart > 1600) {
      window.clearInterval(omenTimer);
      omenTimer = null;
      card.classList.remove("rolling");
      img.src = `/stickers/${pick.sticker}.gif`;
      line.textContent = pick.line;
      haptic("light");
    }
  }, 90);
}

// Balance は renderLifeline が innerHTML で作り直すので、持続する親に委譲する
document.querySelector(".lifeline")?.addEventListener("click", (event) => {
  if (event.target.closest(".lifeline-hero strong")) openOmen();
});
omenEl?.addEventListener("click", () => {
  if (omenTimer) { window.clearInterval(omenTimer); omenTimer = null; }
  omenEl.hidden = true;
});

// 入口は2つ: URL に #demo、またはワードマークを5連打(素早く)。
// ワードマーク長押し(1.6秒)は別のギミックなので潰さない。
let demoTaps = [];
document.querySelector(".wordmark")?.addEventListener("pointerdown", () => {
  const now = Date.now();
  demoTaps = demoTaps.filter((at) => now - at < 1600);
  demoTaps.push(now);
  if (demoTaps.length >= 5) {
    demoTaps = [];
    haptic("medium");
    setDemoMode(!demoMode);
  }
});

// ---------------------------------------------------------------- 起動

setBridgeState();
onePassToggle?.addEventListener("click", toggleOnePass);
ultraToggle?.addEventListener("click", toggleUltra);
soundToggle?.addEventListener("click", toggleSound);
autoExtendBtn?.addEventListener("click", extendAuto);
autotradePanel?.addEventListener("click", cycleAutotradeDemoState);
// R56: 市況チャートの高さ切替。表示だけ(chart.js の ResizeObserver が描き直す)。
document.getElementById("chartExpand")?.addEventListener("click", (event) => {
  const mount = document.querySelector(".panel.market .tape-chart");
  if (!mount) return;
  const expanded = mount.classList.toggle("is-expanded");
  event.currentTarget.setAttribute("aria-pressed", expanded ? "true" : "false");
  haptic("light");
});
document.querySelectorAll(".dock-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    haptic("light");
    setActiveView(tab.dataset.view);
  });
});
render();

const client = createStateClient({
  apiBase: launch.apiBase,
  token: launch.token,
  initData: tg?.initData || "",
  onConnection(state, detail) {
    connection = state;
    connectionDetail = detail || "";
    renderStatusPill();
    if (state === CONNECTION.OFFLINE || state === CONNECTION.UNCONFIGURED) {
      autoStateVerified = false;
      // 状態を確認できない間はシナリオを出さない。ポジションは STALE のまま残す。
      if (currentView && currentView.scenario) {
        currentView = { ...currentView, scenario: null,
          display: { ...currentView.display, orderable: false, blockReason: "STATE UNVERIFIED" } };
        render();
      }
      renderOnePassRail();
      renderSettingsView();
    }
  },
  onState(view, meta = {}) {
    // Worker の応答時刻を単調時計へ固定する。端末時刻が途中で補正されても
    // LIVE表示・期限・鮮度判定は飛ばない。壊れた値は直前の同期を保持する。
    liveClock.sync(view.serverTime);
    syncAutotradeAuthority(view);
    if (demoMode) {
      // デモ中は実データで画面を上書きしない。裏で最新を保持し、
      // デモ終了時にそれを表示する(発注はデモ中どのみち遮断)。
      lastRealView = view;
      renderModeBadges();
      renderSettingsView();
      renderOnePassRail();
      return;
    }
    if (!currentView || currentView.scenario?.scenarioId !== view.scenario?.scenarioId) {
      discarded = null;   // 新しいシナリオが来たら選択解除をリセットする
    }
    currentView = view;
    // R74: 起動時と画面復帰時の snapshot は「新しく見た」だけで「変わった」ではない。
    // 音声の基準を差し替えて、既存の建玉・武装を起動のたびに読み上げない
    // (render() の observe より前。WS の delta と reconnect/interval の snapshot は差分のまま)。
    if (meta.source === "snapshot" && (meta.reason === "initial" || meta.reason === "visibility")) {
      soundCue.rebase(view);
    }
    applyMarket(view.market);
    render();

    // 初回/再接続 snapshot に含まれる「直近の結果」は履歴として受け取り、
    // 自動ポップアップしない。新規 WS delta だけを一度開く。
    const incomingResultId = view.result?.resultId || null;
    if (!resultHydrated || meta.source === "snapshot") {
      shownResultId = incomingResultId;
      resultHydrated = true;
    } else if (view.result && incomingResultId !== shownResultId) {
      shownResultId = incomingResultId;
      openResult(view.result);
    }
  },
});
client.start();

// 保存済みの ULTRA 表示意図だけを復元する。AUTO は実注文権限なので
// localStorage からは絶対に復元せず、上の server snapshot を待つ。
const storedModes = loadModes();
if (storedModes) {
  ultraMode = storedModes.ultra;
  soundMode = storedModes.sound === true;
  renderSoundToggle();
  renderUltraToggle();
  renderModeBadges();
  renderSettingsView();
  renderOnePassRail();
  if (ultraMode) {
    notify("ULTRA restored ON — per-account sizing applies");
  }
}

// 起動シーケンス(一度だけ)。#demo / ?demo=1 付きで開かれたらテスト画面へ。
// 状態クライアントを作った後に呼ぶこと — 先に呼ぶと UNCONFIGURED の初期
// コールバックがデモの currentView を上書きしてしまう。
document.body.classList.add("boot");
if (window.location.hash === "#demo"
    || new URLSearchParams(window.location.search).get("demo") === "1") {
  setDemoMode(true);
}

if (!launch.apiBase) {
  // 凍結プレビュー(Cloudflare 未設定時のフォールバック)。
  // シナリオは表示しない。発注経路も出さない。
  if (signalCount) signalCount.textContent = "UNVERIFIED PREVIEW";
}

function tick() {
  // LIVE 時計は snapshot 更新間隔ではなく、同期アンカーから毎秒進める。
  renderStatusPill();
  // ループ生死の経過分数と AUTO 期限は、イベントが来なくても時間だけで変わる。
  renderWatchtower();
  // ペットも時間だけで変わる(反応が冷める・独り言が入れ替わる)。
  renderPet();
  warnAutoExpiry();
  if (activeViewName === "settings") renderAutoExtendRow();
  // 経路図の経過秒・時計。署名が同じなら文字だけ差し替わる(線の流れは途切れない)。
  if (activeViewName === "system") renderSystemRoute();
  // カウントダウンを進め、失効した瞬間に画面から落とす。残り1分は緊迫表示。
  let expired = false;
  stateStack.querySelectorAll("[data-countdown]").forEach((node) => {
    const text = countdown(node.dataset.countdown);
    node.textContent = text;
    const remaining = Date.parse(node.dataset.countdown) - serverNow();
    node.classList.toggle("urgent", Number.isFinite(remaining) && remaining > 0 && remaining < 60_000);
    if (text === "EXPIRED") expired = true;
  });
  if (expired) {
    render();
    client.refresh("expired");   // サーバーにも確認しに行く
  }
  if (onePassMode && !armIsLive(autoArm)) {
    onePassMode = false;
    autoStateVerified = false;
    onePassResult = { status: "IDLE", pass: false, key: "", grade: "—", reasons: [], at: "" };
    render();
    client.refresh("autotrade-expired");
  }
  // No incoming event means no proof that the last price is still current.
  // Re-evaluate age locally so an open Mini App cannot freeze a live-looking quote.
  if (lastMarket && Date.now() - marketUiUpdatedAt >= 30_000) applyMarket(lastMarket);
}

tick();
window.setInterval(tick, 1000);
