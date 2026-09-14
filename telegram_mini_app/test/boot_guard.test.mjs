// 起動の見張り(boot_guard.js)。
//
// Telegram Desktop(WebView2)で開いた直後に WebView が落ちて「ボタンを押しても開かない」
// 状態になった(2026-09-08、Telegram Desktop の log.txt に crashed webview が 4 回)。
// ここで固定するのは (1) 前回の起動が完了していなければ次は safe で開く、(2) Telegram
// Desktop は既定で mid(R75: 3D 背景だけ止め、目玉と氷は出す)、(3) 生き延びた / 正常に
// 閉じた起動は落ちた扱いにしない、(4) app.js / chart.js が WebGL の遅延読み込みをこの
// 判定で本当に止めていること。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  BOOT_KEY, STABLE_MS, STICKY_MS, TIERS,
  armBootGuard, decideTier, installBootGuard, markBootStable, resetBootGuard,
} from "../boot_guard.js";

const APP = readFileSync(new URL("../app.js", import.meta.url), "utf8");
const CHART = readFileSync(new URL("../chart.js", import.meta.url), "utf8");

function memoryStorage() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => { map.set(k, String(v)); },
    removeItem: (k) => { map.delete(k); },
    map,
  };
}

const NOW = Date.parse("2026-09-08T12:25:00+09:00");

test("初回・痕跡なし・通常端末は full", () => {
  const d = decideTier({ storage: memoryStorage(), platform: "android", now: NOW });
  assert.equal(d.tier, TIERS.FULL);
  assert.equal(d.webgl, true);
  assert.equal(d.scene3d, true);
  assert.equal(d.eye3d, true);
  assert.equal(d.ice, true);
  assert.equal(d.particles, true);
  assert.equal(d.audio, true);
  assert.equal(d.crashed, false);
});

test("Telegram Desktop は既定で mid(3D 背景だけ止め、目玉と氷は出す)", () => {
  const d = decideTier({ storage: memoryStorage(), platform: "tdesktop", now: NOW });
  assert.equal(d.tier, TIERS.MID);
  assert.equal(d.webgl, true, "WebGL 自体は使う(目・氷)");
  assert.equal(d.scene3d, false, "3D 背景(後処理付き)は読まない");
  assert.equal(d.eye3d, true);
  assert.equal(d.ice, true);
  assert.equal(d.particles, true);
  assert.equal(d.audio, true);
  assert.match(d.reason, /TELEGRAM DESKTOP/);
});

test("lite は URL でだけ選べ、WebGL を全部止める", () => {
  const d = decideTier({ storage: memoryStorage(), search: "?boot=lite", platform: "tdesktop", now: NOW });
  assert.equal(d.tier, TIERS.LITE);
  assert.equal(d.webgl, false);
  assert.equal(d.scene3d, false);
  assert.equal(d.eye3d, false);
  assert.equal(d.ice, false);
  assert.equal(d.particles, true);
  assert.equal(d.audio, true);
});

test("前回の起動が完了していなければ safe(WebGL・鉄粉・音声を止める)", () => {
  const storage = memoryStorage();
  armBootGuard(storage, decideTier({ storage, platform: "android", now: NOW - 60_000 }), NOW - 60_000);
  const d = decideTier({ storage, platform: "android", now: NOW });
  assert.equal(d.tier, TIERS.SAFE);
  assert.equal(d.crashed, true);
  assert.equal(d.crashes, 1);
  assert.equal(d.webgl, false);
  assert.equal(d.scene3d, false);
  assert.equal(d.eye3d, false);
  assert.equal(d.ice, false);
  assert.equal(d.particles, false);
  assert.equal(d.audio, false);
  assert.match(d.reason, /DID NOT FINISH/);
});

test("生き延びた起動(markBootStable)は落ちた扱いにならず、クラッシュ履歴だけ 24 時間粘る", () => {
  const storage = memoryStorage();
  armBootGuard(storage, decideTier({ storage, platform: "android", now: NOW }), NOW);
  assert.equal(markBootStable(storage, NOW + STABLE_MS), true);
  assert.equal(decideTier({ storage, platform: "android", now: NOW + STABLE_MS + 1 }).crashed, false);
  assert.equal(decideTier({ storage, platform: "android", now: NOW + STABLE_MS + 1 }).tier, TIERS.FULL);

  // 一度落ちた → safe で生き延びた → 24 時間以内は safe のまま、過ぎれば通常へ戻る
  // (Telegram Desktop でも同じ: mid で落ちれば safe、24 時間後に mid へ)
  armBootGuard(storage, decideTier({ storage, platform: "android", now: NOW }), NOW);
  const afterCrash = decideTier({ storage, platform: "android", now: NOW + 30_000 });
  assert.equal(afterCrash.tier, TIERS.SAFE);
  armBootGuard(storage, afterCrash, NOW + 30_000);
  markBootStable(storage, NOW + 30_000 + STABLE_MS);
  assert.equal(decideTier({ storage, platform: "android", now: NOW + 3_600_000 }).tier, TIERS.SAFE);
  assert.equal(decideTier({ storage, platform: "android", now: NOW + 3_600_000 }).reason, "RECENT CRASH");
  assert.equal(decideTier({ storage, platform: "android", now: NOW + STICKY_MS + 60_000 }).tier, TIERS.FULL);
  assert.equal(decideTier({ storage, platform: "tdesktop", now: NOW + 3_600_000 }).tier, TIERS.SAFE);
  assert.equal(decideTier({ storage, platform: "tdesktop", now: NOW + STICKY_MS + 60_000 }).tier, TIERS.MID);
});

test("URL の boot=full / boot=mid / boot=lite / boot=safe / safe=1 が最優先", () => {
  const storage = memoryStorage();
  armBootGuard(storage, decideTier({ storage, now: NOW }), NOW);   // 痕跡あり
  assert.equal(decideTier({ storage, search: "?boot=full", platform: "tdesktop", now: NOW }).tier, TIERS.FULL);
  assert.equal(decideTier({ storage, search: "?boot=mid", platform: "android", now: NOW }).tier, TIERS.MID);
  assert.equal(decideTier({ storage, search: "?boot=lite", platform: "android", now: NOW }).tier, TIERS.LITE);
  assert.equal(decideTier({ storage, search: "?boot=safe", now: NOW }).tier, TIERS.SAFE);
  assert.equal(decideTier({ storage, search: "?v=abc&safe=1&api=x", now: NOW }).tier, TIERS.SAFE);
});

test("resetBootGuard で記録が消え、通常判定に戻る", () => {
  const storage = memoryStorage();
  armBootGuard(storage, decideTier({ storage, now: NOW }), NOW);
  assert.equal(resetBootGuard(storage), true);
  assert.equal(storage.getItem(BOOT_KEY), null);
  assert.equal(decideTier({ storage, platform: "android", now: NOW }).tier, TIERS.FULL);
});

test("storage が無い / 壊れていても例外にせず full", () => {
  assert.equal(decideTier({ storage: null, platform: "android", now: NOW }).tier, TIERS.FULL);
  const broken = { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); } };
  assert.equal(decideTier({ storage: broken, platform: "android", now: NOW }).tier, TIERS.FULL);
  assert.equal(armBootGuard(broken, null, NOW), false);
  const garbage = memoryStorage();
  garbage.setItem(BOOT_KEY, "{not json");
  assert.equal(decideTier({ storage: garbage, now: NOW }).tier, TIERS.FULL);
});

test("installBootGuard: 印を書き、タイマー / pagehide / hidden のどれかで下ろす", () => {
  const storage = memoryStorage();
  const timers = [];
  const listeners = {};
  const docListeners = {};
  const win = {
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return 1; },
    addEventListener: (name, fn) => { listeners[name] = fn; },
    document: { visibilityState: "visible", addEventListener: (name, fn) => { docListeners[name] = fn; } },
  };
  let clock = NOW;
  const decision = decideTier({ storage, platform: "android", now: clock });
  const settle = installBootGuard({ storage, decision, win, now: () => clock });
  assert.equal(JSON.parse(storage.getItem(BOOT_KEY)).pending, true);
  assert.equal(timers[0].ms, STABLE_MS);
  assert.equal(typeof listeners.pagehide, "function");
  assert.equal(typeof docListeners.visibilitychange, "function");

  clock += STABLE_MS;
  timers[0].fn();
  assert.equal(JSON.parse(storage.getItem(BOOT_KEY)).pending, false);
  assert.equal(decideTier({ storage, platform: "android", now: clock + 1 }).crashed, false);

  // 6 秒以内に正常に閉じた場合も pagehide で下りる(落ちた扱いにならない)
  const storage2 = memoryStorage();
  const win2 = { setTimeout: () => 1, addEventListener: (n, fn) => { listeners[n] = fn; }, document: { visibilityState: "visible", addEventListener() {} } };
  installBootGuard({ storage: storage2, decision, win: win2, now: () => clock });
  listeners.pagehide();
  assert.equal(JSON.parse(storage2.getItem(BOOT_KEY)).pending, false);
  assert.equal(typeof settle, "function");
});

test("app.js は boot 判定で 3D 背景・3D の目・氷・鉄粉・音声を別々に止め、chart.js は氷の WebGL を止める", () => {
  assert.match(APP, /import \{[^}]*decideTier[^}]*\} from "\.\/boot_guard\.js"/);
  assert.match(APP, /if \(boot\.scene3d\)[\s\S]{0,200}import\("\.\/scene3d\.js"\)/, "3D 背景は scene3d フラグ");
  assert.match(APP, /if \(boot\.eye3d\)[\s\S]{0,200}import\("\.\/eye3d\.js"\)/, "3D の目は eye3d フラグ");
  assert.match(APP, /if \(!boot\.eye3d\) return false;/, "平面の紋章を隠す判定も目のフラグ");
  assert.ok(!/if \(boot\.webgl\)/.test(APP), "まとめての webgl 判定は残さない(mid で目と氷だけ出すため)");
  assert.match(APP, /if \(boot\.particles\) attachCardFields\(stateStack\)/);
  assert.match(APP, /isEnabled: \(\) => soundMode && boot\.audio/);
  assert.equal((APP.match(/webgl: boot\.ice/g) || []).length, 3, "氷のチャート 3 面とも ice フラグ");
  assert.ok(!/webgl: boot\.webgl/.test(APP));
  assert.match(APP, /SAFE MODE/);
  assert.match(APP, /\[BOOT_TIERS\.MID\]: "MID"/, "ピルに MID を出す");
  const pill = APP.slice(APP.indexOf('statusPill?.addEventListener("click"'), APP.indexOf('statusPill?.addEventListener("click"') + 700);
  assert.match(pill, /if \(boot\.tier === BOOT_TIERS\.MID\) url\.searchParams\.set\("boot", BOOT_TIERS\.FULL\)/, "MID のピルは full へ");
  assert.match(pill, /else url\.searchParams\.delete\("boot"\)/, "SAFE / LITE のピルは端末の既定へ");
  assert.match(CHART, /webgl = true/);
  assert.match(CHART, /if \(webgl && typeof document !== "undefined"/);
});
