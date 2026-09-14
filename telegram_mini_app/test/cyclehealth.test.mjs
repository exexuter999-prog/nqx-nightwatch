// 監視ループ健全性(cyclehealth.js)の導出。
// ここが甘いと「ループが死んでいるのに画面は静かに古い値を出し続ける」
// 事故に戻るので、窓としきい値の境界を全部固定する。
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  CYCLE_INTERVAL_MS,
  LATE_AFTER_MS,
  SILENT_AFTER_MS,
  evaluateCycleHealth,
  monitorWindow,
} from "../cyclehealth.js";

// 2026-08-28 は金曜。JST = UTC+9。
const jst = (iso) => Date.parse(iso.replace(" ", "T") + "+09:00");

test("監視窓: 平日 07:00〜翌05:45 が active", () => {
  assert.equal(monitorWindow(jst("2026-08-28 07:00")).active, true, "金 07:00 開始");
  assert.equal(monitorWindow(jst("2026-08-28 06:59")).active, false, "金 06:59 はまだ");
  assert.equal(monitorWindow(jst("2026-08-28 12:30")).active, true, "金 昼(旧開始時刻も窓内)");
  assert.equal(monitorWindow(jst("2026-08-28 23:59")).active, true, "金 深夜");
  assert.equal(monitorWindow(jst("2026-08-29 03:59")).active, true, "土 03:59 は金曜セッションの尻尾");
  assert.equal(monitorWindow(jst("2026-08-29 04:00")).active, true, "土 04:00 も窓内(旧終了時刻)");
  assert.equal(monitorWindow(jst("2026-08-29 05:44")).active, true, "土 05:44 が最後の分");
  assert.equal(monitorWindow(jst("2026-08-29 05:45")).active, false, "土 05:45 で終了");
  // 05:45〜07:00 の75分だけが平日の休止帯。
  assert.equal(monitorWindow(jst("2026-08-28 05:00")).active, true, "金 05:00 は木曜セッションの尻尾");
  assert.equal(monitorWindow(jst("2026-08-28 06:00")).active, false, "金 06:00 は休止帯");
  assert.equal(monitorWindow(jst("2026-08-31 00:30")).active, false, "月 未明に日曜セッションは無い");
  assert.equal(monitorWindow(jst("2026-08-31 07:00")).active, true, "月 07:00 再開");
});

test("監視窓: OFF DUTY の次回ラベル", () => {
  assert.equal(monitorWindow(jst("2026-08-29 05:45")).nextLabel, "MON 07:00", "土曜 05:45 の終了以降は月曜");
  assert.equal(monitorWindow(jst("2026-08-30 15:00")).nextLabel, "MON 07:00", "日曜も月曜");
  assert.equal(monitorWindow(jst("2026-08-31 06:00")).nextLabel, "07:00", "月曜早朝は当日");
  assert.equal(monitorWindow(jst("2026-08-31 00:30")).nextLabel, "07:00", "月曜未明も当日");
});

const NOW = jst("2026-08-28 22:00");   // 金曜、窓内
const view = (publishedAgoMs, extra = {}) => ({
  market: { publishedAt: new Date(NOW - publishedAgoMs).toISOString() },
  ...extra,
});

test("しきい値: 3分周期の1.5倍までは ALIVE", () => {
  assert.equal(evaluateCycleHealth(view(0), NOW).status, "ALIVE");
  assert.equal(evaluateCycleHealth(view(CYCLE_INTERVAL_MS), NOW).status, "ALIVE");
  assert.equal(evaluateCycleHealth(view(LATE_AFTER_MS), NOW).status, "ALIVE");
});

test("しきい値: 4.5〜9分は LATE、9分超は SILENT", () => {
  assert.equal(evaluateCycleHealth(view(LATE_AFTER_MS + 1000), NOW).status, "LATE");
  assert.equal(evaluateCycleHealth(view(SILENT_AFTER_MS), NOW).status, "LATE");
  const silent = evaluateCycleHealth(view(SILENT_AFTER_MS + 1000), NOW);
  assert.equal(silent.status, "SILENT");
  assert.equal(silent.ageMin, 9);
});

test("窓の外の沈黙は OFF_DUTY(正常)。ただし新鮮な publish は生存扱い", () => {
  const offDuty = jst("2026-08-30 15:00");   // 日曜
  const stale = { market: { publishedAt: jstIso("2026-08-29 03:57") } };
  assert.equal(evaluateCycleHealth(stale, offDuty).status, "OFF_DUTY");
  const fresh = { market: { publishedAt: new Date(offDuty - 60_000).toISOString() } };
  assert.equal(evaluateCycleHealth(fresh, offDuty).status, "ALIVE",
    "窓の外でも動いているものを OFF DUTY と言わない");
});

function jstIso(s) {
  return new Date(jst(s)).toISOString();
}

// ---- 窓の尻尾(04:00〜05:45) ----
// 2026-09-05 に終了を 04:00 → 05:45 へ延長した(nqx_cycle.py WINDOW_CLOSE)。
// 表示側が旧値のままだと、この 105 分のループ死が OFF DUTY に化けて隠れる。

const publishedAtJst = (s) => ({ market: { publishedAt: jstIso(s) } });

test("窓の尻尾 04:30 の沈黙は LATE / SILENT(OFF DUTY に化けない)", () => {
  const fri0430 = jst("2026-08-28 04:30");   // 金曜未明 = 木曜セッションの尻尾
  assert.equal(monitorWindow(fri0430).active, true);
  assert.equal(evaluateCycleHealth(publishedAtJst("2026-08-28 04:24"), fri0430).status, "LATE");
  assert.equal(evaluateCycleHealth(publishedAtJst("2026-08-28 04:00"), fri0430).status, "SILENT");
});

test("窓の最後の分 05:44 の沈黙も LATE / SILENT(土曜未明 = 金曜セッションの尻尾)", () => {
  const sat0544 = jst("2026-08-29 05:44");
  assert.equal(monitorWindow(sat0544).active, true);
  assert.equal(evaluateCycleHealth(publishedAtJst("2026-08-29 05:36"), sat0544).status, "LATE");
  assert.equal(evaluateCycleHealth(publishedAtJst("2026-08-29 05:30"), sat0544).status, "SILENT");
});

test("05:46 は窓の外: 同じ沈黙でも OFF DUTY。次回ラベルは曜日で決まる", () => {
  const fri = evaluateCycleHealth(publishedAtJst("2026-08-28 05:30"), jst("2026-08-28 05:46"));
  assert.equal(fri.status, "OFF_DUTY");
  assert.equal(fri.window.nextLabel, "07:00", "平日の休止帯は当日 07:00");
  const sat = evaluateCycleHealth(publishedAtJst("2026-08-29 05:30"), jst("2026-08-29 05:46"));
  assert.equal(sat.status, "OFF_DUTY");
  assert.equal(sat.window.nextLabel, "MON 07:00", "土曜の終了後は週明け");
});

test("最終サイクル(05:42)の後は LATE を経ずに OFF DUTY へ移る", () => {
  const last = publishedAtJst("2026-08-28 05:42");
  assert.equal(evaluateCycleHealth(last, jst("2026-08-28 05:46")).status, "ALIVE", "4分はまだ生存");
  assert.equal(evaluateCycleHealth(last, jst("2026-08-28 05:47")).status, "OFF_DUTY");
});

test("月曜未明 04:30 は日曜セッションが無いので、沈黙しても OFF DUTY", () => {
  const mon0430 = jst("2026-08-31 04:30");
  assert.equal(evaluateCycleHealth(publishedAtJst("2026-08-29 05:42"), mon0430).status, "OFF_DUTY");
});

test("publish 痕跡ゼロは NO_DATA(窓内) / OFF_DUTY(窓外)", () => {
  assert.equal(evaluateCycleHealth(null, NOW).status, "NO_DATA");
  assert.equal(evaluateCycleHealth({}, NOW).status, "NO_DATA");
  assert.equal(evaluateCycleHealth(null, jst("2026-08-30 15:00")).status, "OFF_DUTY");
});

test("market が tombstone で null でも accounts.publishedAt で生存が分かる", () => {
  const v = {
    market: null,
    accounts: { publishedAt: new Date(NOW - 2 * 60_000).toISOString() },
  };
  assert.equal(evaluateCycleHealth(v, NOW).status, "ALIVE");
});

test("複数の痕跡は最新を採用する", () => {
  const v = {
    market: { publishedAt: new Date(NOW - 20 * 60_000).toISOString() },
    accounts: { publishedAt: new Date(NOW - 2 * 60_000).toISOString() },
  };
  const health = evaluateCycleHealth(v, NOW);
  assert.equal(health.status, "ALIVE");
  assert.equal(health.ageMin, 2);
});

test("壊れた時刻は痕跡として数えない", () => {
  const v = { market: { publishedAt: "garbage" } };
  assert.equal(evaluateCycleHealth(v, NOW).status, "NO_DATA");
});

// ---- サイクル・ビーコン(view.cycleHealth)の重ね合わせ ----

const beaconView = (beacon, publishedAgoMs = 60_000) => ({
  market: { publishedAt: new Date(NOW - 20 * 60_000).toISOString() },
  cycleHealth: {
    ...beacon,
    publishedAt: new Date(NOW - publishedAgoMs).toISOString(),
  },
});

test("新鮮な BLOCKED ビーコンは沈黙ではなく BLOCKED として出る", () => {
  const health = evaluateCycleHealth(
    beaconView({ status: "BLOCKED", reason: "market stale" }), NOW);
  assert.equal(health.status, "BLOCKED");
  assert.equal(health.beacon.reason, "market stale");
});

test("HALT ビーコンと KILL は HALT として出る", () => {
  assert.equal(evaluateCycleHealth(
    beaconView({ status: "HALT", reason: "publish failed" }), NOW).status, "HALT");
  assert.equal(evaluateCycleHealth(
    beaconView({ status: "PUBLISHED", kill: true }), NOW).status, "HALT");
});

test("PUBLISHED ビーコンは ALIVE のまま(理由の上書きをしない)", () => {
  const health = evaluateCycleHealth(beaconView({ status: "PUBLISHED" }), NOW);
  assert.equal(health.status, "ALIVE");
});

test("古いビーコンは状態語を乗っ取らない(沈黙は沈黙)", () => {
  const v = {
    market: { publishedAt: new Date(NOW - 20 * 60_000).toISOString() },
    cycleHealth: {
      status: "BLOCKED",
      reason: "old reason",
      publishedAt: new Date(NOW - 20 * 60_000).toISOString(),
    },
  };
  const health = evaluateCycleHealth(v, NOW);
  assert.equal(health.status, "SILENT");
  assert.equal(health.beacon.reason, "old reason", "補足表示用に理由は残す");
});

test("ビーコンの publishedAt も生存の痕跡として数える", () => {
  const v = {
    market: null,
    cycleHealth: {
      status: "BLOCKED",
      reason: "acquisition failed",
      publishedAt: new Date(NOW - 60_000).toISOString(),
    },
  };
  assert.equal(evaluateCycleHealth(v, NOW).status, "BLOCKED",
    "BLOCKED が連続してもループ自体は生きていると分かる");
});
