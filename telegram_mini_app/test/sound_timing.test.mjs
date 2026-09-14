// R74(2026-09-09): 鳴るタイミングの規律。
//
// ユーザー報告「起動時に全部再生される / 正しいタイミングでひとつずつ鳴らない」。
//   - 起動直後の render() は view=null で走り、基準が null になっていた → 最初の snapshot が
//     「空 → 全部現れた」の差分として読まれ、起動のたびに既存状態の一声が鳴った(実測:
//     タップ前に scenario-armed.wav が最後まで再生済み)。
//   - 眠っている AudioContext に start() を溜めると、次に起きた瞬間に全部が一斉に鳴る。
//   - 解錠の muted 再生(8 本)が、同じタップの本番の一声(SOUND ON の確認音)を settle で止めていた。
//   - 権限エラーでの Web Speech フォールバックは、読み上げがキューに溜まって後で喋り出す穴。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { CUES, UNLOCK_EVENTS, createSoundCue } from "../sound.js";

const APP = readFileSync(new URL("../app.js", import.meta.url), "utf8");

const armed = (id = "s1") => ({ scenario: { scenarioId: id, state: "ARMED" }, display: { orderable: true } });
const position = (qty, initialQty = qty) => ({ position: { qty, initialQty, side: "LONG" } });
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

/** ended / error を手で起こせる <audio> の差し替え。 */
class FakeMedia {
  constructor() {
    this.src = ""; this.muted = false; this.volume = 1; this.paused = true; this.currentTime = 0;
    this.duration = 1; this.plays = []; this.pauses = 0; this.listeners = {}; this.rejectWith = null;
  }
  addEventListener(type, fn) { (this.listeners[type] ||= new Set()).add(fn); }
  removeEventListener(type, fn) { this.listeners[type]?.delete(fn); }
  dispatch(type) { for (const fn of [...(this.listeners[type] || [])]) fn({ type }); }
  play() {
    this.plays.push({ src: this.src, muted: this.muted, volume: this.volume });
    if (this.rejectWith) return Promise.reject(this.rejectWith);
    this.paused = false;
    return Promise.resolve();
  }
  pause() { this.paused = true; this.pauses += 1; }
  end() { this.paused = true; this.dispatch("ended"); }
}

function mediaFactory() {
  const created = [];
  class Audio extends FakeMedia { constructor() { super(); created.push(this); } }
  return { Audio, created };
}

test("起動: null は基準にならず、最初の実在 view が基準になる(既存の建玉を読み上げない)", () => {
  const played = [];
  const cue = createSoundCue({ isEnabled: () => true, player: (c) => played.push(c), win: {} });
  assert.equal(cue.observe(null), null, "起動直後の空描画");
  assert.equal(cue.observe(position(2)), null, "最初の snapshot に建玉があっても鳴らない");
  assert.equal(cue.observe(position(2)), null);
  assert.equal(cue.observe({}), CUES.POSITION_CLOSED_LOSS, "その後の差分は鳴る");
  assert.deepEqual(played, [CUES.POSITION_CLOSED_LOSS]);
});

test("rebase: 基準の差し替えでは鳴らず、その後の差分だけ鳴る", () => {
  const played = [];
  const cue = createSoundCue({ isEnabled: () => true, player: (c) => played.push(c), win: {} });
  cue.observe(armed("s1"));
  assert.equal(cue.rebase(position(2)), null);
  assert.equal(cue.observe(position(2)), null, "差し替えた基準と同じ内容は無音");
  assert.equal(cue.observe({}), CUES.POSITION_CLOSED_LOSS);
  cue.rebase(null);
  assert.equal(cue.observe(armed("s2")), null, "null に戻した後の最初の実在 view も基準");
  assert.equal(cue.observe(armed("s3")), CUES.SCENARIO_ARMED);
  assert.deepEqual(played, [CUES.POSITION_CLOSED_LOSS, CUES.SCENARIO_ARMED]);
});

test("解錠のリスナーは pointerdown だけでなく pointerup / touchend / click / keydown にも掛け、外さない", () => {
  const { Audio, created } = mediaFactory();
  const listeners = {};
  const removed = [];
  const doc = { addEventListener: (ev, fn) => { listeners[ev] = fn; }, removeEventListener: (ev) => removed.push(ev) };
  const cue = createSoundCue({ isEnabled: () => true, win: { Audio, document: doc } });
  cue.bindUnlock(doc);
  assert.deepEqual(Object.keys(listeners).sort(), [...UNLOCK_EVENTS].sort());
  listeners.pointerdown();
  listeners.pointerup();
  listeners.click();
  assert.deepEqual(removed, [], "リスナーは外さない(touch の pointerdown は操作と認められない端末がある)");
  assert.equal(created.length, 1, "共有 <audio> は 1 本");
  assert.equal(cue.unlocked, true);
});

test("解錠と同じタップの確認音(SOUND ON)は、解錠の settle に止められない", async () => {
  const { Audio, created } = mediaFactory();
  class FakeAC { constructor() { this.state = "running"; this.destination = {}; } resume() { return Promise.resolve(); } }
  const cue = createSoundCue({ isEnabled: () => true, win: { Audio, AudioContext: FakeAC } });
  assert.equal(cue.confirm(), true);
  const [audio] = created;
  assert.equal(audio.plays.length, 2, "無音の解錠 → 本番の一声");
  assert.match(audio.plays[0].src, /^data:audio\/wav/);
  assert.match(audio.plays[1].src, /sound-on\.wav$/);
  assert.equal(audio.plays[1].muted, false);
  assert.equal(audio.plays[1].volume, 1);
  await tick(); await tick();
  assert.equal(audio.paused, false, "解錠の settle が本番の一声を pause していない");
  assert.equal(audio.muted, false);
  audio.end();
});

test("眠った AudioContext に start() を溜めない —— 起きなければ <audio> に落ち、後で起きても一斉には鳴らない", async () => {
  const { Audio, created } = mediaFactory();
  const started = [];
  let ac = null;
  class SleepyAC {
    constructor() { ac = this; this.state = "suspended"; this.destination = {}; }
    resume() { return new Promise(() => {}); }               // 操作の外では起きない(iOS)
    decodeAudioData() { return Promise.resolve({ duration: 0.5 }); }
    createBufferSource() { const s = { connect() {}, start() { started.push(s.buffer); } }; return s; }
  }
  const win = {
    Audio, AudioContext: SleepyAC,
    fetch: () => Promise.resolve({ ok: true, arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)) }),
  };
  const cue = createSoundCue({ isEnabled: () => true, win, resumeGraceMs: 5 });
  cue.confirm();                                              // 解錠 + 確認音(<audio>)
  await tick(); await tick();                                 // 音源の読み込み完了
  const [audio] = created;
  audio.end();                                                // 確認音が終わる
  await tick();
  cue.observe(armed());
  assert.equal(cue.observe({ ...armed(), ...position(2) }), CUES.ORDER_FILLED);
  await new Promise((resolve) => setTimeout(resolve, 40));    // resume の猶予(5ms)を過ぎる
  assert.equal(started.length, 0, "眠った context に start() していない");
  assert.match(audio.plays.at(-1).src, /order-filled\.wav$/, "<audio> に落ちて今鳴らす");
  audio.end();
  ac.state = "running";                                       // 次のタップで起きても
  await tick(); await tick();
  assert.equal(started.length, 0, "溜まっていた音が一斉に鳴ることはない");
});

test("一声ずつ: 前の一声が終わるまで次を始めず、待たせすぎた一声は捨てる", async () => {
  const { Audio, created } = mediaFactory();
  let t = 0;
  const cue = createSoundCue({ isEnabled: () => true, win: { Audio }, now: () => t, cueTtlMs: 1_000 });
  assert.equal(cue.play(CUES.ORDER_FILLED), true);
  assert.equal(cue.play(CUES.TP1_FILLED), true);
  const [audio] = created;
  assert.equal(audio.plays.length, 1, "同時には 1 本だけ");
  assert.match(audio.plays[0].src, /order-filled\.wav$/);
  assert.equal(cue.pending, 2);
  audio.end();
  await tick();
  assert.equal(audio.plays.length, 2, "前の一声が終わってから次");
  assert.match(audio.plays[1].src, /tp1-filled\.wav$/);
  assert.equal(cue.play(CUES.POSITION_CLOSED_WIN), true);
  t += 2_000;                                                 // 前の一声が長引いた
  audio.end();
  await tick();
  assert.equal(audio.plays.length, 2, "1 秒以上待たされた一声は捨てる(遅れて鳴る一声は誤報)");
  assert.equal(cue.pending, 0);
});

test("権限エラー(NotAllowedError)では Web Speech に落ちず、音源の失敗だけ落ちる", async () => {
  const { Audio, created } = mediaFactory();
  const spoken = [];
  class Utter { constructor(text) { this.text = text; } }
  const synth = { cancel() {}, speak(u) { spoken.push(u.text); u.onend?.(); } };
  const cue = createSoundCue({ isEnabled: () => true, win: { Audio, speechSynthesis: synth, SpeechSynthesisUtterance: Utter } });
  cue.play(CUES.ORDER_FILLED);
  const [audio] = created;
  audio.end();
  await tick();
  audio.rejectWith = { name: "NotAllowedError" };
  cue.play(CUES.TP1_FILLED);
  await tick(); await tick();
  assert.deepEqual(spoken, [], "操作の外で許されない再生を読み上げで代替しない");
  assert.equal(cue.pending, 0, "捨てた一声はキューを塞がない");
  audio.rejectWith = { name: "NotSupportedError" };
  cue.play(CUES.POSITION_CLOSED_WIN);
  await tick(); await tick();
  assert.deepEqual(spoken, ["Gain secured."], "音源が読めないときだけ同じ文言を読む");
});

test("試聴一覧の PLAY ALL は共有 <audio> で 1 本ずつ順に鳴らし、終わった順を返す", async () => {
  const { Audio, created } = mediaFactory();
  const cue = createSoundCue({ isEnabled: () => false, win: { Audio } });
  const all = cue.auditionAll([CUES.ORDER_FILLED, CUES.TP1_FILLED]);
  const [audio] = created;
  await tick();
  assert.equal(audio.plays.filter((p) => !p.src.startsWith("data:")).length, 1, "SOUND OFF でも試聴は鳴り、同時には 1 本");
  audio.end(); await tick();
  audio.end(); await tick();
  assert.deepEqual(await all, [CUES.ORDER_FILLED, CUES.TP1_FILLED]);
});

test("配線: 起動時と画面復帰時の snapshot は rebase(render より前)、デモの出入りも rebase", () => {
  const onState = APP.slice(APP.indexOf("onState(view, meta = {}) {"), APP.indexOf("onState(view, meta = {}) {") + 1800);
  assert.match(onState, /meta\.source === "snapshot" && \(meta\.reason === "initial" \|\| meta\.reason === "visibility"\)/);
  assert.ok(onState.indexOf("soundCue.rebase(view)") > 0, "snapshot で基準を差し替える");
  assert.ok(onState.indexOf("soundCue.rebase(view)") < onState.indexOf("render();"), "差し替えは render(=observe)より前");
  const demo = APP.slice(APP.indexOf("function setDemoMode("), APP.indexOf("function setDemoMode(") + 1200);
  assert.ok(demo.indexOf("soundCue.rebase(currentView)") > 0, "デモの出入りで基準を差し替える");
  assert.ok(demo.indexOf("soundCue.rebase(currentView)") < demo.indexOf("render();"), "差し替えは render より前");
  assert.match(APP, /soundCue\.bindUnlock\(document\)/);
});
