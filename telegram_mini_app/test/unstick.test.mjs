// 観測漏れ注文表示の解除導線(unstick.js)の表示判定。
// ここが誤って eligible を返すと、約定待ちの注文を1スライドで
// 「解除済み表示」にできてしまう。境界を全て固定する。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  BLOCKING_ORDER_STATES,
  STUCK_ORDER_MIN_AGE_MS,
  buildUnstickPayload,
  stuckOrderInfo,
} from "../unstick.js";

const NOW = Date.parse("2026-08-16T12:00:00Z");

const order = (over = {}) => ({
  idempotencyKey: "stuck-key-0001",
  state: "SENT",
  at: new Date(NOW - STUCK_ORDER_MIN_AGE_MS - 60_000).toISOString(),
  ...over,
});

const view = (over = {}) => ({
  order: order(),
  position: null,
  positionCheck: { verified: true },
  entryClaim: { routeSnapshot: [
    { state: "ACCEPTED", orderId: "REST-TP1" },
    { state: "ACCEPTED", orderId: "REST-RUNNER" },
  ] },
  ...over,
});

test("blocking かつ十分に古い注文は eligible", () => {
  const info = stuckOrderInfo(view(), NOW);
  assert.equal(info.eligible, true);
});

test("注文が無ければ導線を出さない", () => {
  assert.equal(stuckOrderInfo({ order: null }, NOW), null);
  assert.equal(stuckOrderInfo(null, NOW), null);
});

test("終端状態の注文には出さない", () => {
  for (const state of ["FILLED", "CANCELED", "REJECTED"]) {
    assert.equal(stuckOrderInfo(view({ order: order({ state }) }), NOW), null);
  }
});

test("blocking 状態の一覧は R20 lifecycle と一致", () => {
  assert.deepEqual([...BLOCKING_ORDER_STATES].sort(), ["ENTRY_PARTIAL_FILL", "ENTRY_PARTIAL_ROUTE",
    "ENTRY_RESTING", "PARTIAL", "PENDING", "SENT", "UNKNOWN"]);
  assert.equal(stuckOrderInfo(view({ order: order({ state: "PARTIAL" }) }), NOW).eligible, true);
  const appSource = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  assert.match(appSource, /"ENTRY_PARTIAL_ROUTE"/);
});

test("凍結broker order ID欠落はFLATでも解除不可", () => {
  const info = stuckOrderInfo(view({ entryClaim: { routeSnapshot: [] } }), NOW);
  assert.equal(info.eligible, false);
  assert.equal(info.reason, "BROKER ORDER IDS UNKNOWN");
});

test("建玉が見えている間は出さない(定期照会が FILLED に進める)", () => {
  assert.equal(stuckOrderInfo(view({ position: { state: "OPEN" } }), NOW), null);
});

test("送信直後は解除させない(約定待ちの誤解除防止)", () => {
  const info = stuckOrderInfo(view({ order: order({ at: new Date(NOW - 60_000).toISOString() }) }), NOW);
  assert.equal(info.eligible, false);
  assert.equal(info.reason, "AWAITING BROKER");
  assert.ok(info.remainingMs > 0 && info.remainingMs <= STUCK_ORDER_MIN_AGE_MS);
});

test("待機時間ちょうどからは解除できる", () => {
  const info = stuckOrderInfo(
    view({ order: order({ at: new Date(NOW - STUCK_ORDER_MIN_AGE_MS).toISOString() }) }), NOW,
  );
  assert.equal(info.eligible, true);
});

test("ブローカー未確認なら解除させない", () => {
  const info = stuckOrderInfo(view({ positionCheck: { verified: false } }), NOW);
  assert.equal(info.eligible, false);
  assert.equal(info.reason, "BROKER UNVERIFIED");
  const missing = stuckOrderInfo(view({ positionCheck: null }), NOW);
  assert.equal(missing.eligible, false);
});

test("送信時刻が読めなければ解除させない", () => {
  const info = stuckOrderInfo(view({ order: order({ at: "garbage" }) }), NOW);
  assert.equal(info.eligible, false);
  assert.equal(info.reason, "ORDER TIME UNKNOWN");
});

test("payload は Bot 側の契約どおり(対象キーを必ず運ぶ)", () => {
  const p = buildUnstickPayload(order(), "n".repeat(32));
  assert.equal(p.type, "stuck_order_cancel_confirmed");
  assert.equal(p.orderKey, "stuck-key-0001");
  assert.equal(p.orderState, "SENT");
  assert.equal(p.clientNonce.length, 32);
  assert.equal(p.source, "nqx-nightwatch-mini-app");
});
