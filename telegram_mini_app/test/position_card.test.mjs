/**
 * R73: 保有中カードの現値・SL/TP は Number(null)=0 を価格にしない。
 *
 * 2026-09-09 実測: Worker の position が currentPrice: null を返し、app.js が
 * `Number.isFinite(Number(null))` (= 0 で真) を通して last=0 で含み損益を計算、
 * SHORT 6 @29,551.75 の OPEN P&L が +$354,621 / last 0.00 になった。
 * app.js は DOM 前提で import できないので、配線は本文の形で検証する
 * (cardfield.test.mjs と同じ流儀)。純関数の helper は本文から切り出して実行する。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const APP = readFileSync(new URL("../app.js", import.meta.url), "utf8");

function extract(name) {
  const match = APP.match(new RegExp(`function ${name}\\(value\\) \\{[\\s\\S]*?\\n\\}`));
  assert.ok(match, `${name} が app.js に定義されている`);
  return new Function(`${match[0]}; return ${name};`)();
}

test("配線: 現値は finitePrice(position.currentPrice) ?? finitePrice(lastMarket?.price)", () => {
  assert.match(APP, /const last = finitePrice\(position\.currentPrice\) \?\? finitePrice\(lastMarket\?\.price\);/);
  assert.doesNotMatch(APP, /Number\.isFinite\(Number\(position\.currentPrice\)\)/);
});

test("配線: SL/TP は finitePrice(position.stop / target)。Number(position.stop) は使わない", () => {
  assert.match(APP, /const brokerStop = finitePrice\(position\.stop\);/);
  assert.match(APP, /const brokerTarget = finitePrice\(position\.target\);/);
  assert.doesNotMatch(APP, /Number\(position\.stop\)/);
  assert.doesNotMatch(APP, /Number\(position\.target\)/);
  assert.doesNotMatch(APP, /Number\(position\.avgEntry\)/);
});

test("finitePrice: 欠損と 0 は null、正の有限数だけ通す", () => {
  const finitePrice = extract("finitePrice");
  for (const bad of [null, undefined, "", 0, "0", -1, NaN, "abc", true, false]) {
    assert.equal(finitePrice(bad), null, `finitePrice(${String(bad)})`);
  }
  assert.equal(finitePrice("29551.75"), 29551.75);
  assert.equal(finitePrice(29560), 29560);
});

test("price: 欠損は「—」であって 0.00 ではない", () => {
  const price = extract("price");
  assert.equal(price(null), "—");
  assert.equal(price(undefined), "—");
  assert.equal(price(""), "—");
  assert.equal(price("x"), "—");
  assert.equal(price(29551.75), "29,551.75");
  assert.equal(price(0), "0.00");
});
