/**
 * Worker 時刻へ同期する、表示専用の単調時計。
 *
 * Date.now() の差分だけで時計を進めると、端末時刻の自動補正や手動変更で
 * 表示が飛ぶ。同期時の Worker 時刻を performance.now() に固定し、その後は
 * 単調時間だけを足す。壊れた serverTime は既存の正常アンカーを破壊しない。
 */
export function createServerClock({
  wallNow = () => Date.now(),
  monotonicNow = () => globalThis.performance?.now?.() ?? Date.now(),
} = {}) {
  let serverAnchorMs = null;
  let monotonicAnchorMs = null;

  function sync(serverTime) {
    const parsed = Date.parse(serverTime || "");
    if (!Number.isFinite(parsed)) return false;
    const monotonic = Number(monotonicNow());
    if (!Number.isFinite(monotonic)) return false;
    serverAnchorMs = parsed;
    monotonicAnchorMs = monotonic;
    return true;
  }

  function now() {
    if (serverAnchorMs === null || monotonicAnchorMs === null) {
      return Number(wallNow());
    }
    const elapsed = Number(monotonicNow()) - monotonicAnchorMs;
    return serverAnchorMs + (Number.isFinite(elapsed) ? Math.max(0, elapsed) : 0);
  }

  return {
    now,
    sync,
    get synced() { return serverAnchorMs !== null; },
  };
}

const JST_CLOCK = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Tokyo",
  hourCycle: "h23",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

/** 常に 00–23 時制の ASCII HH:MM:SS を返す。 */
export function formatJstClock(epochMs, { seconds = true } = {}) {
  const value = Number(epochMs);
  if (!Number.isFinite(value)) return "—";
  const parts = Object.fromEntries(
    JST_CLOCK.formatToParts(new Date(value))
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );
  if (!(parts.hour && parts.minute && parts.second)) return "—";
  return seconds
    ? `${parts.hour}:${parts.minute}:${parts.second}`
    : `${parts.hour}:${parts.minute}`;
}
