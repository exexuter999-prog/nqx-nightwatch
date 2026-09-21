/**
 * accountPrefs(口座別 ULTRA 設定)の検証。
 *
 * R109(2026-09-18): ULTRA は複数口座で張れるようになった。ただし execution intent は
 * scope 全体で枚数を 1 つしか持たないので、**同じ利益目標の口座だけ**まとめられる。
 * ここが緩むと「どれかの口座が目標に届かない計画」や無制限の数値がアプリから入る。
 * 口座数の上限(ultra.maxAccounts)・目標一致・数値の形をここで固定する。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { validateAccountPrefs } from "../src/state_machine.js";
import executionContract from "../../execution_contract.json" with { type: "json" };

const MAX_ULTRA = Number(executionContract.ultra.maxAccounts);

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

test("R109: 同じ利益目標なら複数口座を同時に ULTRA にできる", () => {
  const checked = validateAccountPrefs({
    "ACC-A": { ultra: true, profitTarget: 350 },
    "ACC-B": { ultra: true, profitTarget: 350 },
    "ACC-C": { ultra: false },
  });
  assert.equal(checked.ok, true, checked.reason || "");
  assert.equal(checked.map["ACC-A"].ultra, true);
  assert.equal(checked.map["ACC-B"].ultra, true);
});

test("R109: 利益目標が割れた ULTRA 設定は保存できない", () => {
  const checked = validateAccountPrefs({
    "ACC-A": { ultra: true, profitTarget: 250 },
    "ACC-B": { ultra: true, profitTarget: 350 },
  });
  assert.equal(checked.ok, false);
  assert.match(checked.reason, /share one profit target/);
});

test("R109: 目標が無いまま ULTRA を有効にできない", () => {
  const checked = validateAccountPrefs({ "ACC-A": { ultra: true } });
  assert.equal(checked.ok, false);
  assert.match(checked.reason, /needs a profit target/);
});

test("R109: 契約の ultra.maxAccounts ちょうどまで ULTRA にできる", () => {
  const within = Object.fromEntries(Array.from({ length: MAX_ULTRA }, (_v, i) => (
    [`ACC-${i}xx`, { ultra: true, profitTarget: 350 }])));
  assert.equal(validateAccountPrefs(within).ok, true, "上限ちょうどは通る");
});

test("R109: ULTRA 口座数の上限が契約値より小さければそれで弾く", () => {
  // 現在の契約は ultra.maxAccounts = ACCOUNT_LIST_LIMIT = 8 なので、実際には
  // 件数の上限(prefs exceeds 8 accounts)が先に効く。ここで固定したいのは
  // 「ultra.maxAccounts を 1 に戻したら 2 口座目が弾かれる」= 戻し方が効くこと。
  const over = Object.fromEntries(Array.from({ length: MAX_ULTRA + 1 }, (_v, i) => (
    [`ACC-${i}xx`, { ultra: true, profitTarget: 350 }])));
  const checked = validateAccountPrefs(over);
  assert.equal(checked.ok, false);
  assert.match(checked.reason, /at most|exceeds \d+ accounts/,
    `上限超えが弾かれていない: ${checked.reason}`);
});

test("壊れた形は拒否される", () => {
  assert.equal(validateAccountPrefs(null).ok, false);
  assert.equal(validateAccountPrefs([]).ok, false);
  assert.equal(validateAccountPrefs({ "x": { ultra: true } }).ok, false, "短すぎる ID");
  assert.equal(validateAccountPrefs({ "ACC-A": "yes" }).ok, false);
  assert.equal(validateAccountPrefs({ "ACC-A": { profitTarget: -5 } }).ok, false);
  assert.equal(validateAccountPrefs({ "ACC-A": { profitTarget: 2_000_000 } }).ok, false);
  assert.equal(validateAccountPrefs({ "ACC-A": { maxDrawdown: "abc" } }).ok, false);
  // 件数そのものの上限(ACCOUNT_LIST_LIMIT = 8)。ULTRA フラグとは別の話。
  const nine = Object.fromEntries(Array.from({ length: 9 }, (_, i) => [`ACC-${i}xx`, {}]));
  assert.equal(validateAccountPrefs(nine).ok, false, "9口座は拒否");
});

test("ultra が真以外は false に倒す(黙って有効化しない)", () => {
  const checked = validateAccountPrefs({ "ACC-A": { ultra: "true" } });
  assert.equal(checked.ok, true);
  assert.equal(checked.map["ACC-A"].ultra, false);
});
