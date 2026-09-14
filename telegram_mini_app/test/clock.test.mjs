import test from "node:test";
import assert from "node:assert/strict";
import { createServerClock, formatJstClock } from "../clock.js";

test("JST時計は秒まで表示し、深夜0時を24時にしない", () => {
  const midnight = Date.parse("2026-08-24T15:05:06.000Z");
  assert.equal(formatJstClock(midnight), "00:05:06");
  assert.equal(formatJstClock(midnight, { seconds: false }), "00:05");
});

test("Worker時刻から毎秒進み、端末時計が飛んでも表示は飛ばない", () => {
  let wall = Date.parse("2030-01-01T00:00:00.000Z");
  let mono = 1000;
  const clock = createServerClock({ wallNow: () => wall, monotonicNow: () => mono });

  assert.equal(clock.synced, false);
  assert.equal(clock.now(), wall);
  assert.equal(clock.sync("2026-08-24T16:00:00.000Z"), true);
  assert.equal(clock.synced, true);

  mono += 2500;
  wall += 3_600_000; // OS時刻が1時間補正されても影響しない
  assert.equal(clock.now(), Date.parse("2026-08-24T16:00:02.500Z"));
});

test("壊れたserverTimeで正常な同期アンカーを破壊しない", () => {
  let mono = 0;
  const clock = createServerClock({ wallNow: () => 0, monotonicNow: () => mono });
  assert.equal(clock.sync("2026-08-24T16:00:00.000Z"), true);
  assert.equal(clock.sync("not-a-time"), false);
  mono = 3000;
  assert.equal(clock.now(), Date.parse("2026-08-24T16:00:03.000Z"));
});
