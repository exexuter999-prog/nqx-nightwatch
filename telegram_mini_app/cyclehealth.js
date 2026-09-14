/**
 * 監視ループの健全性(「監視の監視」)。表示専用の純関数だけを置く。
 *
 * 3分ループが生きているかを、view の publish 痕跡から導出する。正本は
 * market.publishedAt(サイクルが Durable Object に受理された時刻)。市況が
 * tombstone で null のサイクルに備えて accounts.publishedAt へフォールバックする。
 *
 * BLOCKED のサイクルは publish 自体が無い(nqx_cycle.py は publish 前に
 * 止まる)ため、ここで分かるのは「沈黙の長さ」まで。理由の表示はサイクル・
 * ビーコン(cycleBeacon)が view に載っていればそちらが優先する。
 */

export const CYCLE_INTERVAL_MS = 3 * 60_000;
/** 1.5サイクル。1本の取りこぼしはネットワーク次第で起こるので LATE 止まり。 */
export const LATE_AFTER_MS = 4.5 * 60_000;
/** 3サイクル。ここまで沈黙したら「取りこぼし」では説明できない。 */
export const SILENT_AFTER_MS = 9 * 60_000;

/**
 * JST 監視窓(CLAUDE.md §6.1 / nqx_cycle.py WINDOW_OPEN・WINDOW_CLOSE): 平日 07:00 〜 翌 05:45。
 * 2026-09-01 に開始を 12:30 から前倒し、2026-09-05 に終了を 04:00 から延長。
 * 0 時以降の尻尾は前日のセッションなので、朝側の曜日は1日ずれて火〜土になる。
 * 曜日で絞るのは表示側だけ(nqx_cycle.in_window は時刻しか見ない)。
 */
const OPEN_MIN = 7 * 60;
const OPEN_LABEL = "07:00";
const CLOSE_MIN = 5 * 60 + 45;
const EVENING_DOWS = new Set(["Mon", "Tue", "Wed", "Thu", "Fri"]);
const MORNING_DOWS = new Set(["Tue", "Wed", "Thu", "Fri", "Sat"]);

const JST_PARTS = new Intl.DateTimeFormat("en-US", {
  timeZone: "Asia/Tokyo",
  weekday: "short",
  hourCycle: "h23",
  hour: "2-digit",
  minute: "2-digit",
});

function jstParts(epochMs) {
  const parts = Object.fromEntries(
    JST_PARTS.formatToParts(new Date(epochMs))
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );
  return {
    dow: parts.weekday,
    minutes: Number(parts.hour) * 60 + Number(parts.minute),
  };
}

/**
 * 監視窓の内外。窓の外では沈黙は正常(OFF DUTY)。
 * 次の開始時刻ラベルは OFF DUTY の表示にだけ使う。
 */
export function monitorWindow(nowMs) {
  const { dow, minutes } = jstParts(nowMs);
  const active = (EVENING_DOWS.has(dow) && minutes >= OPEN_MIN)
    || (MORNING_DOWS.has(dow) && minutes < CLOSE_MIN);
  if (active) return { active: true, nextLabel: null };
  const sameDay = EVENING_DOWS.has(dow) && minutes < OPEN_MIN;
  return { active: false, nextLabel: sameDay ? OPEN_LABEL : `MON ${OPEN_LABEL}` };
}

function lastCycleMs(view) {
  const candidates = [
    view?.market?.publishedAt,
    view?.accounts?.publishedAt,
    view?.cycleHealth?.publishedAt,
  ];
  let best = null;
  for (const iso of candidates) {
    const ms = Date.parse(iso || "");
    if (Number.isFinite(ms) && (best === null || ms > best)) best = ms;
  }
  return best;
}

function beaconOf(view) {
  const raw = view?.cycleHealth;
  if (!raw || typeof raw !== "object") return null;
  const atMs = Date.parse(raw.publishedAt || raw.at || "");
  return {
    status: String(raw.status || "").toUpperCase(),
    reason: raw.reason || null,
    kill: raw.kill === true,
    atMs: Number.isFinite(atMs) ? atMs : null,
  };
}

/**
 * ループの状態を1語に落とす。
 *
 *   ALIVE    — 直近1.5サイクル以内に publish がある(窓の外でも生存は生存)
 *   BLOCKED  — ループは生きているが、直近サイクルが BLOCKED(ビーコン由来)
 *   HALT     — 直近サイクルが HALT、または KILL が立っている(ビーコン由来)
 *   LATE     — 窓内で 4.5〜9分沈黙。1本の取りこぼしの可能性
 *   SILENT   — 窓内で 9分超の沈黙。ビーコンすら来ていない
 *   OFF_DUTY — 窓の外で publish が止まっている(正常)
 *   NO_DATA  — publish の痕跡が一度も無い
 *
 * ビーコン(cycleHealth)は BLOCKED/HALT のサイクルでも届くので、
 * 「沈黙」と「止まる理由があって止まっている」をここで区別する。
 */
export function evaluateCycleHealth(view, nowMs) {
  const window = monitorWindow(nowMs);
  const lastAtMs = lastCycleMs(view);
  const beacon = beaconOf(view);
  if (lastAtMs === null) {
    return {
      status: window.active ? "NO_DATA" : "OFF_DUTY",
      ageMs: null,
      ageMin: null,
      lastAtMs: null,
      window,
      beacon,
    };
  }
  const ageMs = Math.max(0, nowMs - lastAtMs);
  const ageMin = Math.floor(ageMs / 60_000);
  let status;
  if (ageMs <= LATE_AFTER_MS) status = "ALIVE";
  else if (!window.active) status = "OFF_DUTY";
  else if (ageMs <= SILENT_AFTER_MS) status = "LATE";
  else status = "SILENT";
  // 新鮮なビーコンだけが ALIVE/LATE を上書きできる。古いビーコンの理由は
  // 表示の補足には使えるが、状態語を乗っ取らせない。
  const beaconFresh = beacon?.atMs !== null && beacon !== null
    && nowMs - beacon.atMs <= SILENT_AFTER_MS;
  if ((status === "ALIVE" || status === "LATE") && beaconFresh) {
    if (beacon.kill) status = "HALT";
    else if (beacon.status === "HALT") status = "HALT";
    else if (beacon.status === "BLOCKED") status = "BLOCKED";
  }
  return { status, ageMs, ageMin, lastAtMs, window, beacon };
}
