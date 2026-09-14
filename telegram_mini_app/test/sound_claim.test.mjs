// R57: 自動発注音の合図と、操作の外での再生。
//
// 本番の自動発注(autotrade_engine → order.py)は `order` ストリームを publish しない。
// view に現れるのは ENTRY claim の CLAIMED → CONSUMED と、約定後の position だけ。
// `order` だけを見ていた "Auto order sent." は一度も鳴らなかった(2026-09-05 実測)。
import { test } from "node:test";
import assert from "node:assert/strict";
import { CUES, createSoundCue, nextCue } from "../sound.js";

const armed = (id = "s1") => ({ scenario: { scenarioId: id, state: "ARMED" }, display: { orderable: true } });
const claim = (state, key = "ENTRY:aaa", extra = {}) => ({ entryClaim: { entryKey: key, state, routeState: null, ...extra } });

test("claim が CONSUMED になった瞬間に Auto order sent(order 無し)", () => {
  assert.equal(nextCue({ ...armed(), ...claim("CLAIMED") }, { ...armed(), ...claim("CONSUMED") }), CUES.AUTO_ORDER_SENT);
  assert.equal(nextCue(armed(), { ...armed(), ...claim("CONSUMED") }), CUES.AUTO_ORDER_SENT, "CLAIMED を見ずに CONSUMED が来ても鳴る");
  assert.equal(nextCue(armed(), { ...armed(), ...claim("CLAIMED") }), null, "CLAIMED(dry-run 前)は読まない");
  assert.equal(nextCue(armed(), { ...armed(), ...claim("RECOVERED", "ENTRY:aaa", { consumedAt: "x" }) }), null, "回復済みは読まない");
});

test("CONSUME を経て RESOLVED で届いた claim も送信として読む(更新の取りこぼし)", () => {
  assert.equal(nextCue(armed(), { ...armed(), ...claim("RESOLVED", "ENTRY:aaa", { consumedAt: "2026-09-04T19:56:19Z", routeState: "SENT" }) }),
    CUES.AUTO_ORDER_SENT);
  assert.equal(nextCue(armed(), { ...armed(), ...claim("RESOLVED", "ENTRY:aaa") }), null, "consumedAt が無い RESOLVED は送っていない");
});

test("同じ claim が続く間は無音、別 key の claim なら再び鳴る", () => {
  const sent = { ...armed(), ...claim("CONSUMED") };
  assert.equal(nextCue(sent, { ...armed(), ...claim("CONSUMED") }), null);
  assert.equal(nextCue(sent, { ...armed(), ...claim("RESOLVED", "ENTRY:aaa", { consumedAt: "x" }) }), null, "同じ key の状態遷移は読まない");
  assert.equal(nextCue(sent, { ...armed("s2"), ...claim("CONSUMED", "ENTRY:bbb") }), CUES.AUTO_ORDER_SENT, "次のトレードの claim");
});

test("手動送信直後の claim は手動として扱う(二重に自動と読まない)", () => {
  assert.equal(nextCue(armed(), { ...armed(), ...claim("CONSUMED") }, { manualPending: true }), CUES.ORDER_SENT_MANUAL);
});

test("建玉の出現は claim より優先(同じ更新に両方あれば Order filled)", () => {
  const next = { ...armed(), ...claim("CONSUMED"), position: { qty: 14, initialQty: 14, side: "SHORT" } };
  assert.equal(nextCue(armed(), next), CUES.ORDER_FILLED);
});

test("observe: 起動時の基準に CONSUMED があっても鳴らず、次の新しい claim で鳴る", () => {
  const played = [];
  const cue = createSoundCue({ isEnabled: () => true, player: (c) => played.push(c), win: {} });
  assert.equal(cue.observe({ ...armed(), ...claim("RESOLVED", "ENTRY:old", { consumedAt: "x" }) }), null);
  assert.equal(cue.observe({ ...armed(), ...claim("RESOLVED", "ENTRY:old", { consumedAt: "x" }) }), null);
  assert.equal(cue.observe({ ...armed("s2"), ...claim("CONSUMED", "ENTRY:new") }), CUES.AUTO_ORDER_SENT);
  assert.deepEqual(played, [CUES.AUTO_ORDER_SENT]);
});

test("解錠: タップ内で共有の <audio> 1 本を無音 WAV で再生→停止して primed にし、AudioContext を起こす(R74)", async () => {
  const created = [];
  class FakeAudio {
    constructor() { this.src = ""; this.muted = false; this.volume = 1; this.paused = true; this.currentTime = 1; this.plays = []; created.push(this); }
    play() { this.plays.push({ src: this.src, muted: this.muted, volume: this.volume }); this.paused = false; return Promise.resolve(); }
    pause() { this.paused = true; }
  }
  let resumed = 0;
  class FakeAC { constructor() { this.state = "suspended"; } resume() { resumed += 1; this.state = "running"; return Promise.resolve(); } }
  const listeners = {};
  const doc = { addEventListener: (ev, fn) => { listeners[ev] = fn; }, removeEventListener: () => {} };
  const win = { Audio: FakeAudio, AudioContext: FakeAC, document: doc, fetch: undefined };
  const cue = createSoundCue({ isEnabled: () => true, win });
  cue.bindUnlock(doc);
  assert.equal(cue.unlocked, false);
  listeners.pointerdown();
  assert.equal(cue.unlocked, true);
  assert.equal(resumed, 1, "AudioContext を resume する");
  assert.equal(created.length, 1, "<audio> は共有の 1 本だけ(実音源 8 本を並べて鳴らさない)");
  const [audio] = created;
  assert.equal(audio.plays.length, 1);
  assert.match(audio.plays[0].src, /^data:audio\/wav;base64,/, "解錠は無音 WAV で");
  assert.equal(audio.plays[0].muted, true);
  assert.equal(audio.plays[0].volume, 0);
  await Promise.resolve();
  assert.ok(audio.paused && audio.muted === false && audio.volume === 1 && audio.currentTime === 0, "停止→巻き戻し→ミュート解除");
  listeners.click();
  assert.equal(created.length, 1, "タップを重ねても要素は増えない");
  assert.equal(audio.plays.length, 1, "解錠済みなら鳴らし直さない");
});

test("解錠後の再生は WebAudio(バッファ)を優先し、無ければ <audio>", async () => {
  const started = [];
  class FakeAC {
    constructor() { this.state = "running"; this.destination = {}; }
    resume() { return Promise.resolve(); }
    decodeAudioData(data) { return Promise.resolve({ length: data.byteLength }); }
    createBufferSource() { const s = { connect() {}, start() { started.push(s.buffer); } }; return s; }
  }
  class FakeAudio { constructor() { this.plays = 0; } play() { this.plays += 1; return Promise.resolve(); } pause() {} }
  const fetched = [];
  const win = {
    Audio: FakeAudio, AudioContext: FakeAC,
    fetch: (url) => { fetched.push(url); return Promise.resolve({ ok: true, arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)) }); },
  };
  const cue = createSoundCue({ isEnabled: () => true, win });
  cue.confirm();                                       // タップ内(解錠 + 確認音)
  assert.equal(fetched.length, Object.keys(CUES).length, "解錠時に全音源を読み込む");
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  cue.observe(armed());
  assert.equal(cue.observe({ ...armed(), ...claim("CONSUMED") }), CUES.AUTO_ORDER_SENT);
  assert.equal(started.length, 1, "WebSocket 更新(操作の外)でも AudioContext から鳴る");
});
