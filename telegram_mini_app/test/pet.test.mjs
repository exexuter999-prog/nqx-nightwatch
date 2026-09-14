// R65: 目の駐在所(pet.js)。台詞の選び方・気分・出来事の拾い方と、app.js / CSS の配線を検査する。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  LINES, MOODS, HOLD_MS, ROTATE_MS, DAY_EPSILON,
  hashSeed, pickLine, describeMood, classifyResult, latestResult,
  nextPetEvent, ambientMood, createPetVoice,
} from "../pet.js";
import { MODES } from "../eye3d.js";

const result = (over = {}) => ({
  resultId: "r-1",
  side: "LONG",
  symbol: "MNQU6",
  qty: 2,
  entry: 30000,
  exit: 30040,
  stop: 29980,
  pointValue: 2,
  openedAt: "2026-09-07T00:00:00Z",
  closedAt: "2026-09-07T00:14:00Z",
  ...over,
});

test("台詞は全ての気分に有る。どれも短く、目の場面は eye3d の表に有る", () => {
  for (const key of Object.keys(MOODS)) {
    const pool = LINES[key];
    assert.ok(Array.isArray(pool) && pool.length >= 2, `${key} の台詞が足りない`);
    for (const line of pool) {
      assert.ok(line.length <= 46, `${key}: 吹き出しに収まらない — ${line}`);
      assert.ok(/[.!?]$/.test(line), `${key}: 一言として閉じていない — ${line}`);
    }
    const { mode } = describeMood(key, "LONG");
    assert.ok(MODES[mode], `${key} の場面 ${mode} が eye3d に無い`);
    assert.ok(["up", "down", "warn", "dim"].includes(MOODS[key].tone), `${key} の tone`);
  }
});

test("台詞は seed から決定論。同じ場面では変わらず、拍が変われば入れ替わる", () => {
  assert.equal(pickLine("idle", 7), pickLine("idle", 7));
  assert.ok(LINES.idle.includes(pickLine("idle", 7)));
  const seen = new Set();
  for (let i = 0; i < 40; i += 1) seen.add(pickLine("idle", i));
  assert.equal(seen.size, LINES.idle.length, "拍を回せば池を一周する");
  assert.ok(LINES.idle.includes(pickLine("nonsense-mood", 3)), "未知の気分は idle の池");
  assert.equal(hashSeed("a"), hashSeed("a"));
  assert.notEqual(hashSeed("a"), hashSeed("b"));
});

test("向きは場面に解ける。向きが分からなければ neutral(嘘の方向を出さない)", () => {
  assert.equal(describeMood("armed", "BUY").mode, "long-armed");
  assert.equal(describeMood("armed", "SELL").mode, "short-armed");
  assert.equal(describeMood("posUp", "LONG").mode, "long");
  assert.equal(describeMood("posDown", "SHORT").mode, "short");
  assert.equal(describeMood("armed", null).mode, "neutral");
  assert.equal(describeMood("win", "SHORT").mode, "elated", "勝ちは向きに依らず見開く");
  assert.equal(describeMood("loss", "LONG").mode, "dejected");
});

test("決済の重さ: 目標まで取れば winBig、コスト帯は flat、想定超の損は lossBig", () => {
  assert.equal(classifyResult(result()), "winBig");                       // +40pt / 2R
  assert.equal(classifyResult(result({ exit: 30020 })), "win");           // +20pt / 1R
  assert.equal(classifyResult(result({ exit: 30001 })), "flat");          // コスト帯
  assert.equal(classifyResult(result({ exit: 29980 })), "loss");          // 構造SL = -1R
  assert.equal(classifyResult(result({ exit: 29950 })), "lossBig");       // -50pt / -2.5R
  assert.equal(classifyResult(result({ side: "SHORT", exit: 29960 })), "winBig");
  assert.equal(classifyResult(null), null);
  assert.equal(classifyResult({ resultId: "x" }), null, "価格の無い記録では喜ばない");
});

test("最新の決済: delta の result が先、無ければ履歴の先頭", () => {
  assert.equal(latestResult({ result: result({ resultId: "a" }) }).resultId, "a");
  assert.equal(latestResult({ recentResults: [result({ resultId: "b" })] }).resultId, "b");
  assert.equal(latestResult({}), null);
  assert.equal(latestResult(null), null);
});

test("出来事は重い順に 1 つ。決済 > TP1 > 約定 > 建玉消滅 > 送信 > 武装", () => {
  const armed = { scenario: { scenarioId: "s1", state: "ARMED", side: "SELL" }, display: { orderable: true } };
  assert.equal(nextPetEvent({}, armed).key, "armed");
  assert.equal(nextPetEvent(armed, armed), null, "同じシナリオでは二度言わない");

  const open = { position: { qty: 2, initialQty: 2, side: "LONG", filledAt: "t0" } };
  assert.equal(nextPetEvent(armed, open).key, "filled");
  const runner = { position: { qty: 1, initialQty: 2, side: "LONG", filledAt: "t0" } };
  assert.equal(nextPetEvent(open, runner).key, "tp1");
  assert.equal(nextPetEvent(runner, {}).key, "closed");

  const closedWithResult = { result: result({ resultId: "r-9" }) };
  const event = nextPetEvent(runner, closedWithResult);
  assert.equal(event.key, "winBig", "決済は建玉消滅より重い");
  assert.equal(event.seed, "r-9");
  assert.equal(event.holdMs, HOLD_MS.result);
  assert.ok(HOLD_MS.result > HOLD_MS.event, "一喜一憂は長く残す");

  const sent = { entryClaim: { entryKey: "k1", state: "CONSUMED" }, scenario: { side: "BUY" } };
  assert.equal(nextPetEvent({}, sent).key, "sent");
  assert.equal(nextPetEvent(sent, sent), null);
  assert.equal(nextPetEvent({}, { order: { state: "ENTRY_RESTING", side: "BUY" } }).key, "sent");
  assert.equal(nextPetEvent(null, null), null);
});

test("武装は「送れる」ときだけ。WATCH のシナリオは出来事にしない", () => {
  const watch = { scenario: { scenarioId: "s2", state: "ARMED", side: "BUY" }, display: { orderable: false } };
  assert.equal(nextPetEvent({}, watch), null);
  assert.equal(ambientMood(watch, {}).key, "watching");
});

test("場面: KILL・切断・HALT が最優先。建玉があれば含み損益、無ければ武装 > 監視", () => {
  assert.equal(ambientMood(null, {}).key, "boot");
  assert.equal(ambientMood({}, { kill: true, offline: true }).key, "kill");
  assert.equal(ambientMood({}, { offline: true, health: "HALT" }).key, "offline");
  assert.equal(ambientMood({}, { health: "HALT" }).key, "halt");

  const down = ambientMood({ position: { qty: 2, initialQty: 2, side: "SHORT", unrealizedPnl: -80 } }, {});
  assert.deepEqual([down.key, down.side], ["posDown", "SHORT"]);
  assert.equal(ambientMood({ position: { qty: 2, initialQty: 2, side: "LONG", unrealizedPnl: 40 } }, {}).key, "posUp");
  assert.equal(ambientMood({ position: { qty: 2, initialQty: 2, side: "LONG" } }, {}).key, "posUp",
    "含み損益が取れないときは煽らない");
  assert.equal(ambientMood({ position: { qty: 1, initialQty: 2, side: "LONG", unrealizedPnl: -5 } }, {}).key, "posRun",
    "TP1 後は runner の場面");

  assert.equal(ambientMood({ order: { state: "SENT", side: "BUY" } }, {}).key, "order");
  assert.equal(ambientMood({ scenario: { state: "ACTIVE", side: "BUY" }, display: { orderable: true } }, {}).key, "armed");
  assert.equal(ambientMood({}, { health: "OFF_DUTY" }).key, "offduty");
  assert.equal(ambientMood({}, { health: "SILENT" }).key, "silent");
  assert.equal(ambientMood({}, { health: "LATE" }).key, "silent");
  assert.equal(ambientMood({ market: { evaluation: { grade: "B" } } }, { health: "ALIVE" }).key, "blocked");
  assert.equal(ambientMood({}, { health: "ALIVE" }).key, "idle");
});

test("今日の損益は独り言に混ぜる(言い続けない)。閾値未満は無視", () => {
  const even = 0 * ROTATE_MS;         // bucket 0(偶数)
  const odd = 1 * ROTATE_MS;          // bucket 1(奇数)
  assert.equal(ambientMood({}, { now: even, dayUsd: 320, health: "ALIVE" }).key, "dayUp");
  assert.equal(ambientMood({}, { now: even, dayUsd: -320, health: "ALIVE" }).key, "dayDown");
  assert.equal(ambientMood({}, { now: odd, dayUsd: 320, health: "ALIVE" }).key, "idle", "毎回は言わない");
  assert.equal(ambientMood({}, { now: even, dayUsd: DAY_EPSILON / 2, health: "ALIVE" }).key, "idle");
  assert.equal(ambientMood({ position: { qty: 2, initialQty: 2, side: "LONG", unrealizedPnl: 10 } },
    { now: even, dayUsd: 320 }).key, "posUp", "建玉があるときは今の話をする");
});

test("声: 起動直後の 1 件目は飲み込み、反応は hold の間だけ独り言を上書きする", () => {
  let clock = 1_000_000;
  const voice = createPetVoice({ now: () => clock });
  const before = { recentResults: [result({ resultId: "old" })], market: { evaluation: {} } };

  assert.equal(voice.observe(before), null, "開いた瞬間の履歴では喜ばない");
  assert.equal(voice.speak(before, { now: clock, health: "ALIVE", dayUsd: null }).key, "blocked");

  const after = { recentResults: [result({ resultId: "new" }), result({ resultId: "old" })], market: { evaluation: {} } };
  assert.equal(voice.observe(after).key, "winBig");
  const reaction = voice.speak(after, { now: clock, health: "ALIVE" });
  assert.equal(reaction.key, "winBig");
  assert.equal(reaction.reacting, true);
  assert.equal(reaction.mode, "elated");
  assert.ok(LINES.winBig.includes(reaction.line));
  assert.equal(voice.pending().key, "winBig");

  clock += HOLD_MS.result - 1;
  assert.equal(voice.speak(after, { now: clock }).key, "winBig", "冷めるまでは同じ");
  clock += 2;
  const back = voice.speak(after, { now: clock, health: "ALIVE", dayUsd: null });
  assert.equal(back.key, "blocked", "冷めたら場面へ戻る");
  assert.equal(back.reacting, false);
  assert.equal(voice.pending(), null);
});

test("声: 突くと台詞が変わる。場面と目の場面は変わらない", () => {
  const voice = createPetVoice({ now: () => 0 });
  const view = { market: { evaluation: {} } };
  const first = voice.speak(view, { now: 0, health: "ALIVE" });
  const lines = new Set([first.line]);
  for (let i = 0; i < 6; i += 1) { voice.poke(); lines.add(voice.speak(view, { now: 0, health: "ALIVE" }).line); }
  assert.ok(lines.size > 1, "引き直せば別の台詞が出る");
  assert.equal(voice.speak(view, { now: 0, health: "ALIVE" }).key, first.key);
});

test("配線: app.js は駐在所へ目を移し、tick と描画で一言を更新する", () => {
  const APP = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  const HTML = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  const CSS = readFileSync(new URL("../styles.css", import.meta.url), "utf8");
  assert.match(APP, /import \{ createPetVoice \} from "\.\/pet\.js";/);
  assert.match(APP, /petVoice\.observe\(currentView\);/, "出来事は view の差分から");
  assert.match(APP, /matchMedia\("\(min-width: 880px\) and \(min-height: 760px\)"\)/, "駐在は広い画面だけ");
  assert.ok(APP.split("renderPet();").length >= 6, "描画・tick・タブ切替・突きで更新する");
  assert.match(HTML, /id="petDen"[\s\S]*?id="petSay"[\s\S]*?id="petEye"/, "吹き出しと目の台座");
  assert.match(CSS, /\.pet-den \{[\s\S]*?display: none;/, "既定は出さない");
  assert.match(CSS, /@media \(min-width: 880px\) and \(min-height: 760px\) \{\n  \.pet-den:not\(\[hidden\]\) \{ display: block;/);
  assert.match(CSS, /\.pet-den\.is-reacting \.pet-perch \{ animation: pet-startle/, "一喜");
  assert.match(CSS, /\.pet-den\.is-reacting\[data-tone="down"\] \.pet-perch \{ animation: pet-sag/, "一憂");
});

test("ペットは表示専用(注文・保存・ネットワークに触れない)", () => {
  const source = readFileSync(new URL("../pet.js", import.meta.url), "utf8");
  for (const forbidden of ["fetch(", "localStorage", "/api/", "WebSocket", "sendData", "confirm", "--confirm"]) {
    assert.ok(!source.includes(forbidden), `${forbidden} を含まない`);
  }
});
