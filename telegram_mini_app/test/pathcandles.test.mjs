// リザルト path → ローソク足リサンプルの検証。新しい価格を発明しないこと。
import { test } from "node:test";
import assert from "node:assert/strict";
import { candlesFromPath } from "../pathcandles.js";

test("点列が OHLC バケツに正しく畳まれる", () => {
  const path = Array.from({ length: 24 }, (_, i) => 100 + i);  // 単調増加
  const candles = candlesFromPath(path, 8);
  assert.ok(candles.length >= 6 && candles.length <= 8);
  assert.equal(candles[0].o, 100);
  assert.equal(candles[candles.length - 1].c, 123);
  for (const k of candles) {
    assert.ok(k.h >= Math.max(k.o, k.c) && k.l <= Math.min(k.o, k.c));
    assert.ok(path.includes(k.h) && path.includes(k.l));       // 実在値のみ
  }
});

test("両端しか無い記録はローソクにしない(トレイルに落ちる)", () => {
  assert.equal(candlesFromPath([100, 105, 110]), null);
  assert.equal(candlesFromPath(null), null);
  assert.equal(candlesFromPath([1, 2, 3, 4, 5, 6, 7]), null); // 8点未満
});
