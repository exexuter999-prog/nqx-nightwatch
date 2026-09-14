// オリジナル絵文字(glyphs.js)。端末の絵文字フォントに依存しないこと、
// 色を継承すること、画面の文字記号が置き換わっていることを固定する。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { GLYPH, glyph } from "../glyphs.js";

test("全グリフはインライン SVG で、currentColor を継承し、装飾扱い", () => {
  for (const [name, svg] of Object.entries(GLYPH)) {
    assert.match(svg, /^<svg class="glyph[^"]*" viewBox="0 0 24 24" aria-hidden="true" focusable="false">/, name);
    assert.match(svg, /currentColor/, `${name} は文字色を継承する`);
    assert.ok(!/#[0-9a-f]{6}(?![^<]*opacity)/i.test(svg.replace(/#000/g, "")), `${name} は固定色を持たない`);
    assert.match(svg, /<\/svg>$/, name);
  }
});

test("未知の名前は空文字(壊れた記号を出さない)", () => {
  assert.equal(glyph("nope"), "");
  assert.match(glyph("signal", "is-live"), /class="glyph is-live /, "追加クラスを付けられる");
});

test("画面の文字記号はグリフへ置き換わっている", () => {
  const APP = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  const HTML = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  const LEDGER = readFileSync(new URL("../ledger.js", import.meta.url), "utf8");
  const strip = (s) => s.replace(/\/\/[^\n]*|\/\*[\s\S]*?\*\/|<!--[\s\S]*?-->/g, "");
  for (const ch of ["◆", "◈", "▤", "◇", "✦", "▴", "▲", "▼"]) {
    assert.ok(!strip(APP).includes(ch), `app.js に ${ch} が残っていない`);
    assert.ok(!strip(HTML).includes(ch), `index.html に ${ch} が残っていない`);
    assert.ok(!strip(LEDGER).includes(ch), `ledger.js に ${ch} が残っていない`);
  }
  assert.match(APP, /glyph\("signal", armed \? "is-live" : ""\)/, "武装中の的は点灯クラスを持つ");
});
