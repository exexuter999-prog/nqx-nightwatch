// 音声キュー(R54)。view の差分から「鳴らすべき一声」を決める純粋関数と、
// 手動/自動の切り分け・設定スイッチ・解錠・配線の規律を固定する。
//
// 守ること:
//   - 起動直後の最初の view では鳴らない(既存の建玉を "Order filled" と読まない)
//   - OFF のときは鳴らないが、差分の基準は進む(ON にした瞬間に過去分が溢れない)
//   - 判定はしない。orderable=false の ARMED は「武装」と読まない
//   - 手動はスライドの瞬間に鳴らし、直後に server に現れる同じ注文は二重に読まない
//   - ローカルで送っていないのに現れた注文は「自動発注」と読む
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { CUES, CUE_FILES, CUE_TEXT, DEDUPE_MS, MANUAL_WINDOW_MS, createSoundCue, nextCue } from "../sound.js";

const APP = readFileSync(new URL("../app.js", import.meta.url), "utf8");
const HTML = readFileSync(new URL("../index.html", import.meta.url), "utf8");
const CSS = readFileSync(new URL("../styles.css", import.meta.url), "utf8");

const armed = (id = "s1", orderable = true) => ({
  scenario: { scenarioId: id, state: "ARMED" },
  display: { orderable },
});
const position = (qty, initialQty = qty) => ({ position: { qty, initialQty, side: "LONG" } });
const order = (state = "SENT") => ({ order: { state } });

test("差分キュー: 建玉 > 注文 > シナリオ の順で1つだけ返す", () => {
  assert.equal(nextCue(null, {}), null, "何も無ければ無音");
  assert.equal(nextCue({}, armed()), CUES.SCENARIO_ARMED, "武装シナリオが現れた");
  assert.equal(nextCue(armed("s1"), armed("s1")), null, "同じシナリオが続くだけなら無音");
  assert.equal(nextCue(armed("s1"), armed("s2")), CUES.SCENARIO_ARMED, "別 ID の武装は鳴る");
  assert.equal(nextCue({}, armed("s1", false)), null, "orderable=false は武装と読まない");
  assert.equal(nextCue({}, order("SENT")), CUES.AUTO_ORDER_SENT, "ローカルで送っていない注文 = 自動発注");
  assert.equal(nextCue({}, order("SENT"), { manualPending: true }), CUES.ORDER_SENT_MANUAL, "手動送信直後の注文は手動");
  assert.equal(nextCue(order("SENT"), order("ENTRY_RESTING")), null, "注文が続くだけなら無音");
  assert.equal(nextCue({}, position(2)), CUES.ORDER_FILLED, "建玉が現れた = 約定");
  assert.equal(nextCue(position(2, 2), position(1, 2)), CUES.TP1_FILLED, "枚数が初期枚数を割った = TP1");
  assert.equal(nextCue(position(1, 2), position(1, 2)), null, "TP1 後に同じ状態が続くなら無音");
  assert.equal(nextCue(position(1, 2), {}), CUES.POSITION_CLOSED_LOSS,
    "建玉が消えた(勝ちの証拠が無ければ勝ちとは読まない)");
  assert.equal(nextCue({ ...order("SENT") }, { ...position(2), ...armed("s9") }),
    CUES.ORDER_FILLED, "同じ更新に複数あれば建玉が勝つ");
  assert.equal(nextCue({ position: { qty: 0 } }, { position: { qty: 0 } }), null, "qty=0 は建玉ではない");
});

test("R65: 決済は勝ち負けで読み分ける(記録があれば価格から、無ければ直前の含み損益)", () => {
  const trade = (exit) => ({
    result: {
      resultId: `r-${exit}`, side: "LONG", entry: 30000, exit, stop: 29980, qty: 2, pointValue: 2,
      openedAt: "2026-09-07T00:00:00Z", closedAt: "2026-09-07T00:12:00Z",
    },
  });
  const held = (pnl) => ({ position: { qty: 2, initialQty: 2, side: "LONG", unrealizedPnl: pnl } });
  assert.equal(nextCue(held(120), trade(30040)), CUES.POSITION_CLOSED_WIN, "記録が勝ち");
  assert.equal(nextCue(held(120), trade(29960)), CUES.POSITION_CLOSED_LOSS, "含み益でも記録が負けなら負け");
  assert.equal(nextCue(held(120), trade(30001)), CUES.POSITION_CLOSED_LOSS, "コスト帯は勝ちと読まない");
  assert.equal(nextCue(held(120), {}), CUES.POSITION_CLOSED_WIN, "記録が無ければ直前の含み益");
  assert.equal(nextCue(held(-60), {}), CUES.POSITION_CLOSED_LOSS);
  assert.equal(nextCue({ position: { qty: 2, initialQty: 2, side: "LONG" } }, {}),
    CUES.POSITION_CLOSED_LOSS, "何も分からなければ勝ちとは読まない");
});

test("同じ view オブジェクトの再描画では鳴らない", () => {
  const view = armed();
  assert.equal(nextCue(view, view), null);
});

test("最初の view は基準として飲み込み、OFF でも基準は進む", () => {
  const played = [];
  let enabled = false;
  const cue = createSoundCue({ isEnabled: () => enabled, player: (c) => played.push(c), win: {} });
  assert.equal(cue.observe(position(2)), null, "起動直後の建玉は読み上げない");
  assert.equal(cue.observe(position(2)), null);
  assert.equal(cue.observe({}), CUES.POSITION_CLOSED_LOSS, "差分は検出する");
  assert.deepEqual(played, [], "OFF なら鳴らない");
  enabled = true;
  assert.equal(cue.observe(armed()), CUES.SCENARIO_ARMED);
  assert.deepEqual(played, [CUES.SCENARIO_ARMED], "ON になった後の差分だけ鳴る");
  assert.equal(cue.confirm(), true, "確認音は ON のとき鳴る");
  assert.deepEqual(played, [CUES.SCENARIO_ARMED, CUES.SOUND_ON]);
});

test("手動送信: スライドの瞬間に鳴り、直後に server へ現れる注文は二重に読まない", () => {
  const played = [];
  let t = 1_000_000;
  const cue = createSoundCue({ isEnabled: () => true, player: (c) => played.push(c), win: {}, now: () => t });
  cue.observe(armed());                       // 基準
  assert.equal(cue.manualSend(), true, "スライドで即時に鳴る");
  t += 3_000;
  assert.equal(cue.observe({ ...armed(), ...order("SENT") }), CUES.ORDER_SENT_MANUAL,
    "手動窓の中で現れた注文は手動として分類される");
  assert.deepEqual(played, [CUES.ORDER_SENT_MANUAL], "分類はされるが DEDUPE で二重には鳴らない");
  t += MANUAL_WINDOW_MS + 1;
  assert.equal(cue.observe({ ...armed(), order: null }), null);
  assert.equal(cue.observe({ ...armed(), ...order("SENT") }), CUES.AUTO_ORDER_SENT,
    "手動窓を過ぎてから現れた注文は自動発注");
  assert.deepEqual(played, [CUES.ORDER_SENT_MANUAL, CUES.AUTO_ORDER_SENT]);
});

test("自動発注: ローカル操作なしに注文が現れたら Auto order sent", () => {
  const played = [];
  const cue = createSoundCue({ isEnabled: () => true, player: (c) => played.push(c), win: {} });
  cue.observe(armed());
  assert.equal(cue.observe({ ...armed(), ...order("PENDING") }), CUES.AUTO_ORDER_SENT);
  assert.deepEqual(played, [CUES.AUTO_ORDER_SENT]);
});

test("同じキューは DEDUPE_MS 内で繰り返さない(確認音は例外)", () => {
  const played = [];
  let t = 0;
  const cue = createSoundCue({ isEnabled: () => true, player: (c) => played.push(c), win: {}, now: () => t });
  assert.equal(cue.play(CUES.ORDER_FILLED), true);
  t += DEDUPE_MS - 1;
  assert.equal(cue.play(CUES.ORDER_FILLED), false, "間隔内は鳴らない");
  t += 2;
  assert.equal(cue.play(CUES.ORDER_FILLED), true, "間隔を過ぎれば鳴る");
  assert.equal(cue.confirm(), true);
  assert.equal(cue.confirm(), true, "確認音は force");
  assert.deepEqual(played, [CUES.ORDER_FILLED, CUES.ORDER_FILLED, CUES.SOUND_ON, CUES.SOUND_ON]);
});

test("試聴: スイッチ OFF でも dedupe 中でも明示のタップなら必ず鳴り、全通りを順に鳴らせる", async () => {
  const played = [];
  const cue = createSoundCue({ isEnabled: () => false, player: (c) => played.push(c), win: {}, now: () => 0 });
  assert.equal(cue.audition(CUES.ORDER_FILLED), true);
  assert.equal(cue.audition(CUES.ORDER_FILLED), true, "dedupe を無視する");
  assert.equal(cue.audition("NOPE"), false, "未知のキューは鳴らさない");
  const all = await cue.auditionAll();
  assert.deepEqual(all, Object.keys(CUES), "全キューを定義順に");
  assert.equal(played.length, 2 + Object.keys(CUES).length);
});

test("配線: デモバナーの VOICE から試聴一覧を開き、行タップで audition、PLAY ALL で auditionAll", () => {
  assert.match(HTML, /id="demo-voice"/, "デモバナーのボタン");
  assert.match(HTML, /<section id="voiceGallery" hidden/, "一覧は既定で非表示");
  assert.match(HTML, /id="voice-rows"/); assert.match(HTML, /id="voice-all"/); assert.match(HTML, /id="voice-close"/);
  assert.match(APP, /soundCue\.audition\(row\.dataset\.cue\)/, "行タップで試聴");
  assert.match(APP, /soundCue\.auditionAll\(\)/, "PLAY ALL");
  assert.match(APP, /Object\.keys\(CUES\)\.map/, "行はキュー定義から組む(取りこぼしなし)");
  assert.match(CSS, /#voiceGallery\[hidden\] \{ display: none; \}/, "hidden が display:flex に負けない");
});

test("音源とフォールバック文言が全キューに揃い、WAV が public/voice に実在する", () => {
  for (const key of Object.keys(CUES)) {
    assert.match(CUE_FILES[key], /\.wav$/, `${key} の WAV`);
    assert.ok(CUE_TEXT[key].length > 0, `${key} の読み上げ文言`);
    assert.ok(existsSync(new URL(`../public/voice/${CUE_FILES[key]}`, import.meta.url)),
      `public/voice/${CUE_FILES[key]} が存在する`);
  }
  assert.ok(!existsSync(new URL("../public/voice/raw", import.meta.url)), "raw/ は出荷しない");
});

test("R65: 読み上げ文言は音源の台本(tools/voice/lines.json)と一字一句同じ", () => {
  // WAV は lines.json から作る(neural.py / synth.ps1 の両方が読む)。
  // ここがずれると、鳴る言葉と Web Speech の読み上げが食い違う。
  const script = JSON.parse(readFileSync(new URL("../tools/voice/lines.json", import.meta.url), "utf8"));
  const spoken = Object.entries(script).filter(([key]) => !key.startsWith("_"));
  assert.equal(spoken.length, Object.keys(CUES).length, "台本の行数がキューの数と合う");
  for (const key of Object.keys(CUES)) {
    const file = CUE_FILES[key].replace(/\.wav$/, "");
    assert.equal(script[file], CUE_TEXT[key], `${key}: 台本と CUE_TEXT`);
  }
});

test("配線: render() の入口で observe し、スライドで manualSend、設定タブに SOUND スイッチ", () => {
  assert.match(APP, /import \{ CUES, CUE_TEXT, createSoundCue \} from "\.\/sound\.js"/);
  const renderBody = APP.slice(APP.indexOf("function render() {"), APP.indexOf("function render() {") + 200);
  assert.match(renderBody, /soundCue\.observe\(currentView\)/, "view の差分は render の入口で読む");
  const confirm = APP.slice(APP.indexOf("function confirmOrder("), APP.indexOf("function confirmOrder(") + 2600);
  assert.equal((confirm.match(/soundCue\.manualSend\(\)/g) || []).length, 2, "デモ経路と本番経路の両方で手動の一声");
  assert.match(HTML, /id="soundToggle"[^>]*role="switch"/, "SOUND スイッチ");
  const settings = HTML.slice(HTML.indexOf('id="settingsView"'), HTML.indexOf("</section>", HTML.indexOf('id="settingsView"')));
  assert.match(settings, /id="soundToggle"/, "スイッチは設定タブの中");
  assert.match(APP, /soundToggle\?\.addEventListener\("click", toggleSound\)/);
  assert.match(APP, /soundCue\.confirm\(\)/, "ON にした瞬間に確認音(=解錠)");
  assert.match(CSS, /\.mode-card-sound/, "SOUND カードの点灯スタイル");
});

test("デモに入ったらこのセッションだけ SOUND を ON にする(SCENE の切り替えが聞こえる。保存はしない)", () => {
  const demo = APP.slice(APP.indexOf("function setDemoMode("), APP.indexOf("function setDemoMode(") + 900);
  const onAt = demo.indexOf("if (on && !soundMode) { soundMode = true; renderSoundToggle(); }");
  assert.ok(onAt > 0, "デモ入場で ON");
  assert.ok(demo.lastIndexOf("render();", onAt) > 0, "ON は最初の描画の後(起動時に無操作で鳴らそうとしない)");
  assert.ok(!demo.includes("saveModes()"), "端末設定には保存しない");
  const scene = APP.slice(APP.indexOf('getElementById("demo-scene")'), APP.indexOf('getElementById("demo-scene")') + 600);
  assert.match(scene, /render\(\)/, "SCENE 切り替えは render() を通る = soundCue.observe が差分を読む");
});

test("保存されるのは表示設定だけ(sound は端末設定、取引状態は保存しない)", () => {
  const saved = APP.slice(APP.indexOf("function saveModes()"), APP.indexOf("function saveModes()") + 400);
  assert.match(saved, /sound: soundMode/);
  assert.ok(!saved.includes("onePass"));
});
