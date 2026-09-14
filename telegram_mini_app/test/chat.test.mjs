// R38: Claude との対話面。発注経路から分離されていることと、
// 本文を任意の文字列として安全に描くことを固定する。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { INBOX_ENABLED, messageNode, mergeMessages, stamp } from "../chat.js";

/** 最小の DOM シム。chart_svg.test.mjs と同じ流儀。 */
function fakeDoc() {
  const make = (tag) => {
    const node = {
      tag, children: [], _text: "", className: "",
      append(...kids) { this.children.push(...kids); },
      set textContent(v) { this._text = String(v); },
      get textContent() { return this._text; },
    };
    return node;
  };
  return { createElement: make };
}

const flat = (node) => [node._text, ...node.children.flatMap(flat)].filter(Boolean);

test("送信者で見た目を分ける", () => {
  const doc = fakeDoc();
  const mine = messageNode({ role: "user", text: "やあ", at: 0, seq: 1 }, doc);
  const theirs = messageNode({ role: "claude", text: "直しました", at: 0, seq: 2 }, doc);
  assert.match(mine.className, /is-user/);
  assert.match(theirs.className, /is-claude/);
  assert.ok(flat(mine).some((t) => t.includes("あなた")));
  assert.ok(flat(theirs).some((t) => t.includes("Claude")));
});

test("本文は textContent で入れる(HTML として解釈させない)", () => {
  const doc = fakeDoc();
  const evil = "<img src=x onerror=alert(1)>";
  const node = messageNode({ role: "user", text: evil, at: 0, seq: 1 }, doc);
  const body = node.children.find((c) => c.className === "msg-body");
  assert.equal(body.textContent, evil, "文字列としてそのまま入る");
  // innerHTML を一切使っていないこと
  assert.ok(!("innerHTML" in body) || body.innerHTML === undefined);
});

test("本文が欠けても落ちない", () => {
  const doc = fakeDoc();
  for (const row of [null, {}, { role: "user" }, { text: null }]) {
    const node = messageNode(row, doc);
    assert.ok(node, "要素は返る");
  }
});

test("同じ seq を二度描かない", () => {
  const seen = new Set();
  const first = mergeMessages(seen, [{ seq: 2, text: "b" }, { seq: 1, text: "a" }]);
  assert.deepEqual(first.map((r) => r.seq), [1, 2], "seq 昇順で返る");
  const again = mergeMessages(seen, [{ seq: 1, text: "a" }, { seq: 3, text: "c" }]);
  assert.deepEqual(again.map((r) => r.seq), [3], "既出は落ちる");
});

test("壊れた seq は無視する", () => {
  const seen = new Set();
  const out = mergeMessages(seen, [{ seq: "x" }, { seq: null }, {}, { seq: 5 }]);
  assert.deepEqual(out.map((r) => r.seq), [5]);
  assert.deepEqual(mergeMessages(new Set(), null), []);
});

test("時刻が読めなくても空文字を返す", () => {
  for (const bad of ["x", null, undefined, NaN, {}]) {
    assert.equal(stamp(bad), "", `stamp(${String(bad)})`);
  }
  assert.ok(stamp(Date.now()).length > 0, "正常な時刻は描ける");
});

test("この面は送信者を名乗る処理を持たない", () => {
  const src = readFileSync(new URL("../chat.js", import.meta.url), "utf-8");
  assert.ok(!src.includes("JSON.stringify({"), "停止中の画面に送信 body を残してはならない");
  // コメント中の "claude" は許す。コードとして role に入れていないことを見る。
  const code = src
    .split(/\r?\n/)
    .filter((line) => {
      const t = line.trim();
      return !t.startsWith("*") && !t.startsWith("//") && !t.startsWith("/*");
    })
    .join("\n");
  assert.ok(!/role\s*:\s*["'`]claude/.test(code),
    "この面から claude を名乗ってはならない");
});

test("発注経路を一切読み込まない", () => {
  const src = readFileSync(new URL("../chat.js", import.meta.url), "utf-8");
  for (const forbidden of ["./app.js", "./ultra.js", "./scene3d.js", "sendData"]) {
    assert.ok(!src.includes(forbidden), `${forbidden} を参照している`);
  }
});

test("受信箱は無期限停止で通信処理を持たない", () => {
  assert.equal(INBOX_ENABLED, false);
  const src = readFileSync(new URL("../chat.js", import.meta.url), "utf-8");
  assert.ok(!src.includes("/api/chat"), "停止中の画面から chat API を呼んではならない");
  assert.ok(!/\bfetch\s*\(/.test(src), "停止中の画面から通信してはならない");
});
