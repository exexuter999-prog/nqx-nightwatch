// ULTRA mode の表示計算(ultra.js)と、デモが絶対に送信しないこと。
//
// ultra.js は「画面に出す枚数」を計算するだけで、実発注枚数は Bot 側の
// ultra_mode.py が正本。両者の式が一致することは Python 側の
// tests/test_ultra_mode.py が実際に両実装を走らせて突き合わせている。
// ここでは表示側の境界と、デモ経路の遮断を固定する。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { accountPlan, buildPlan, requiredQty, signalGeometry, ultraSplit } from "../ultra.js";

// 仕様に示されたサンプルシグナル。
const SIGNAL = { side: "SHORT", entry: 30126.0, stop: 30147.0, target: 30076.0 };
const ACCOUNTS = [
  { id: "APEX-01", cap: 180, buffer: 2500, profitTarget: 3000 },
  { id: "APEX-02", cap: 180, buffer: 3000, profitTarget: 6000 },
  { id: "APEX-03", cap: 180, buffer: 5000, profitTarget: 9000 },
];

test("サンプルシグナルの枚数と損益が仕様どおりに出る", () => {
  const plan = buildPlan(SIGNAL, ACCOUNTS, { signalQty: 1 });
  assert.equal(plan.geometry.slPoints, 21);
  assert.equal(plan.geometry.tpPoints, 50);
  assert.equal(plan.geometry.rr, 2.38);
  assert.equal(plan.geometry.side, "SELL", "SHORT は SELL へ正規化する");
  assert.deepEqual(plan.accounts.map((row) => row.qty), [30, 60, 90]);
  assert.deepEqual(plan.accounts.map((row) => row.projectedProfit), [3000, 6000, 9000]);
  assert.deepEqual(plan.accounts.map((row) => row.projectedLoss), [1260, 2520, 3780]);
  assert.deepEqual(plan.accounts.map((row) => row.verdict),
    ["ELIGIBLE", "ELIGIBLE", "ELIGIBLE"]);
  assert.equal(plan.totalQty, 180);
  assert.equal(plan.signalQty, 1, "通常枚数は記録するが使わない");
});

test("ULTRA OFF は通常シグナルの枚数をそのまま返す", () => {
  const plan = buildPlan(SIGNAL, ACCOUNTS, { enabled: false, signalQty: 1 });
  assert.equal(plan.enabled, false);
  assert.equal(plan.totalQty, 1);
  assert.deepEqual(plan.accounts, [], "OFF では口座別計算をしない");
  assert.equal(plan.routable, false);
});

test("判定は残ドローダウンで決まり、1トレード上限は使わない", () => {
  const tightCap = accountPlan({ ...ACCOUNTS[0], cap: 60 }, signalGeometry(SIGNAL));
  assert.equal(tightCap.verdict, "ELIGIBLE");
  assert.ok(!tightCap.contractBlockers.some((b) => b.includes("ACCOUNT_TRADE_CAP")));

  const thin = accountPlan({ ...ACCOUNTS[0], buffer: 1000 }, signalGeometry(SIGNAL));
  assert.equal(thin.verdict, "INELIGIBLE");
  assert.ok(thin.reasons.includes("DRAWDOWN_EXCEEDED"));
  assert.equal(thin.qty, 30, "超過でも枚数と金額は見せる");
  assert.equal(thin.projectedLoss, 1260);

  const exact = accountPlan({ ...ACCOUNTS[0], buffer: 1260 }, signalGeometry(SIGNAL));
  assert.equal(exact.verdict, "ELIGIBLE", "ちょうどは通す");

  const blown = accountPlan({ ...ACCOUNTS[0], buffer: 0 }, signalGeometry(SIGNAL));
  assert.ok(blown.reasons.includes("ACCOUNT_BLOWN"));
});

test("利益目標が無い口座は枚数を作らない", () => {
  const row = accountPlan({ id: "APEX-09", cap: 180, buffer: 5000 }, signalGeometry(SIGNAL));
  assert.equal(row.qty, null);
  assert.ok(row.reasons.includes("PROFIT_TARGET_MISSING"));
  assert.equal(row.verdict, "INELIGIBLE");
});

test("壊れたシグナルは計算せず理由を返す", () => {
  const cases = {
    SIGNAL_SIDE_INVALID: { entry: 30126, stop: 30147, target: 30076 },
    SIGNAL_PRICES_INCOMPLETE: { side: "SHORT", entry: 30126, stop: 30147 },
    // 並びが逆でも、SL が Entry と同値でも、方向判定の時点で落ちる。
    SIGNAL_DIRECTION_INVALID: { side: "SHORT", entry: 30126, stop: 30076, target: 30147 },
  };
  for (const [reason, signal] of Object.entries(cases)) {
    const geometry = signalGeometry(signal);
    assert.equal(geometry.valid, false, reason);
    assert.equal(geometry.reason, reason);
  }
  // SL == Entry も同じく計算させない(理由は方向判定で確定する)。
  const flat = signalGeometry({ side: "SHORT", entry: 30126, stop: 30126, target: 30076 });
  assert.equal(flat.valid, false);
});

test("比率分割は端数を runner へ寄せる", () => {
  assert.deepEqual(ultraSplit(30), [15, 15]);
  assert.deepEqual(ultraSplit(9), [4, 5]);
  assert.deepEqual(ultraSplit(2), [1, 1]);
  for (const qty of [1, 0, -3, 2.5, null]) {
    assert.equal(ultraSplit(qty), null, `${qty} は分割できない`);
  }
});

test("必要枚数は目標へ届くまで切り上げる", () => {
  assert.equal(requiredQty(3000, 50, 2), 30);
  assert.equal(requiredQty(3001, 50, 2), 31, "端数は切り上げ");
  assert.equal(requiredQty(10, 50, 2), 1, "目標未満でも最低1枚");
  for (const bad of [0, -1, null, "x"]) assert.equal(requiredQty(bad, 50, 2), null);
  assert.equal(requiredQty(3000, 0, 2), null);
});

// ---------------------------------------------------------------- デモの遮断

const APP_SOURCE = readFileSync(new URL("../app.js", import.meta.url), "utf8");

test("デモは tg.sendData へ到達する前に必ず止まる", () => {
  const body = APP_SOURCE.slice(APP_SOURCE.indexOf("function confirmOrder("));
  const demoGuard = body.indexOf("if (demoMode) {");
  const send = body.indexOf("tg.sendData");
  assert.ok(demoGuard > -1, "confirmOrder にデモ遮断がある");
  assert.ok(send > -1, "confirmOrder に送信がある");
  assert.ok(demoGuard < send, "デモ遮断は送信より前に置かれている");
  // 遮断ブロックが return で閉じていること(素通りしない)。改行は CRLF。
  const guardBlock = body.slice(demoGuard, send);
  assert.match(guardBlock, /\breturn;\s*\r?\n/, "デモ分岐は return で抜ける");
});

test("送信の出口は confirmOrder と解除導線の2か所だけ", () => {
  // ULTRA / 自動発注パネルを足したことで新しい送信経路が増えていないこと。
  // 武装は運用機の env 二重キーが正本で、この画面から切り替えられてはいけない。
  const callSites = APP_SOURCE.match(/tg\.sendData\(/g) || [];
  assert.equal(callSites.length, 2, "送信の呼び出しは2か所のまま");
  // 武装の正本は運用機の env 二重キー。送信ペイロードに autotrade を有効化する
  // キーが無いことで担保する(コメント中の説明文は対象外)。
  const payloadBlock = APP_SOURCE.slice(APP_SOURCE.indexOf('type: "scenario_order_confirmed"'),
    APP_SOURCE.indexOf("tg.sendData(JSON.stringify(payload))"));
  assert.ok(!/autotrade|NQX_AUTOTRADE|liveOrders/i.test(payloadBlock),
    "発注ペイロードに autotrade を有効化する値が無い");
  // 自動発注パネルのクリックはデモの表示切替だけを行う。
  assert.match(APP_SOURCE, /function cycleAutotradeDemoState\(\)\s*\{\s*\r?\n\s*if \(!demoMode\) return;/,
    "パネル操作はデモ以外では何もしない");
});
