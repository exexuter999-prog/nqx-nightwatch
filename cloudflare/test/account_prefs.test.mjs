/**
 * accountPrefs(口座別 ULTRA 設定)の検証。
 *
 * ここが緩むと「複数口座の同時 ULTRA」(claim scope 未開通)や
 * 無制限の数値がアプリから入る。1口座制限と数値の形をここで固定する。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { validateAccountPrefs } from "../src/state_machine.js";

test("正常な prefs は正規化されて通る", () => {
  const checked = validateAccountPrefs({
    "ACC-A": { ultra: true, profitTarget: 9000, maxDrawdown: 3000.005 },
    "ACC-B": { ultra: false },
  });
  assert.equal(checked.ok, true);
  assert.equal(checked.map["ACC-A"].ultra, true);
  assert.equal(checked.map["ACC-A"].profitTarget, 9000);
  assert.equal(checked.map["ACC-A"].maxDrawdown, 3000.01, "セントへ丸める");
  assert.equal(checked.map["ACC-B"].ultra, false);
  assert.equal(checked.map["ACC-B"].profitTarget, null);
});

test("ULTRA は同時に1口座だけ(claim scope 制約)", () => {
  const checked = validateAccountPrefs({
    "ACC-A": { ultra: true },
    "ACC-B": { ultra: true },
  });
  assert.equal(checked.ok, false);
  assert.match(checked.reason, /at most one account/);
});

test("壊れた形は拒否される", () => {
  assert.equal(validateAccountPrefs(null).ok, false);
  assert.equal(validateAccountPrefs([]).ok, false);
  assert.equal(validateAccountPrefs({ "x": { ultra: true } }).ok, false, "短すぎる ID");
  assert.equal(validateAccountPrefs({ "ACC-A": "yes" }).ok, false);
  assert.equal(validateAccountPrefs({ "ACC-A": { profitTarget: -5 } }).ok, false);
  assert.equal(validateAccountPrefs({ "ACC-A": { profitTarget: 2_000_000 } }).ok, false);
  assert.equal(validateAccountPrefs({ "ACC-A": { maxDrawdown: "abc" } }).ok, false);
  const nine = Object.fromEntries(Array.from({ length: 9 }, (_, i) => [`ACC-${i}xx`, {}]));
  assert.equal(validateAccountPrefs(nine).ok, false, "9口座は拒否");
});

test("ultra が真以外は false に倒す(黙って有効化しない)", () => {
  const checked = validateAccountPrefs({ "ACC-A": { ultra: "true" } });
  assert.equal(checked.ok, true);
  assert.equal(checked.map["ACC-A"].ultra, false);
});
