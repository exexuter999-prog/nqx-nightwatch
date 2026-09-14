// R60: 待機紋章の目(eye3d.js)。純関数と配線だけを検査する(WebGL は node に無い)。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  MODES, resolveMode, planSaccade, pupilAngle, blinkAmount, planBlink, gazeFromPointer, stepSpring, lidCircle,
  accentIntensity,
  planDroop, droopBaseline, lidPlane, lidOpening, heptagramPoints, initStandbyEye,
  IRIS_ANGLE, GAZE_LIMIT, SACCADE, DROOP, LID_OPEN, FRAME,
  BLINK_CLOSE_MS, BLINK_OPEN_MS, BLINK_MIN_MS, BLINK_SPAN_MS,
} from "../eye3d.js";

const seq = (values) => { let i = 0; return () => values[i++ % values.length]; };

test("場面表: neutral / long / short / armed / 一喜一憂。未知は neutral。瞳は散大し、武装でさらに開く", () => {
  assert.deepEqual(Object.keys(MODES),
    ["neutral", "long", "short", "long-armed", "short-armed", "elated", "dejected"]);
  assert.equal(resolveMode("nonsense"), MODES.neutral);
  assert.ok(MODES.neutral.pupil >= 0.7 * IRIS_ANGLE, "瞳孔は開いている(虹彩の 7 割以上)");
  assert.ok(MODES["long-armed"].pupil > MODES.neutral.pupil, "武装でさらに開く");
  assert.ok(MODES["long-armed"].glow > MODES.long.glow && MODES.long.glow > MODES.neutral.glow);
  assert.ok(MODES.neutral.glow < 0.1, "黒目: 普段はほぼ光らない");
  for (const mode of Object.values(MODES)) assert.ok(mode.pupil + 0.05 < IRIS_ANGLE, "瞳は虹彩の内側");
  // R65: 勝ちは見開いて光り、負けは瞼が落ちて瞳が締まり光が消える(倍率 lid は瞼の重さ)
  assert.ok(MODES.elated.lid < MODES.neutral.lid && MODES.dejected.lid > MODES.neutral.lid);
  assert.ok(MODES.elated.pupil > MODES.neutral.pupil && MODES.dejected.pupil < MODES.neutral.pupil);
  assert.ok(MODES.elated.glow > MODES["long-armed"].glow && MODES.dejected.glow < MODES.neutral.glow);
  assert.ok(MODES.elated.saccade > 1 && MODES.dejected.saccade < 1, "喜べば忙しく、沈めば動かない");
  for (const mode of Object.values(MODES)) assert.ok(Number.isFinite(mode.lid) && mode.lid > 0, "lid は倍率");
});

test("視線の漂い: 3〜7 秒後、±8°/±6° + 下向きの癖。頻度倍率で間隔が縮む。ばねは柔らかい", () => {
  const a = planSaccade(seq([0, 1, 0]), 1000, 1);
  assert.equal(a.at, 1000 + SACCADE.minMs);
  assert.ok(Math.abs(a.yaw - SACCADE.yaw) < 1e-9 && Math.abs(a.pitch - (-SACCADE.pitch + SACCADE.pitchBias)) < 1e-9);
  const b = planSaccade(seq([1, 0.5, 0.5]), 0, 1);
  assert.equal(b.at, SACCADE.minMs + SACCADE.spanMs);
  assert.ok(Math.abs(b.yaw) < 1e-9 && Math.abs(b.pitch - SACCADE.pitchBias) < 1e-9, "中立でも少し下を見る");
  assert.equal(planSaccade(seq([1, 0, 0]), 0, 2).at, (SACCADE.minMs + SACCADE.spanMs) / 2, "倍率 2 で半分");
  assert.ok(Math.abs(a.yaw) <= GAZE_LIMIT.yaw && Math.abs(a.pitch) <= GAZE_LIMIT.pitch);
  assert.ok(SACCADE.pitchBias < 0 && SACCADE.omega < 12, "だるい: 下向き・ゆっくり");
});

test("瞼は視線に連動: 上を見ると上瞼が開いて重さが抜け、下を見ると少し追う", () => {
  const rest = lidOpening(0);
  assert.ok(Math.abs(rest.upper - LID_OPEN.upper) < 1e-9 && Math.abs(rest.lower - LID_OPEN.lower) < 1e-9 && rest.alert === 0);
  const up = lidOpening(0.2);
  assert.ok(up.upper > rest.upper + 0.3, "上瞼が視線より大きく上がる");
  assert.ok(up.lower < rest.lower, "下瞼もわずかに上がる");
  assert.equal(up.alert, 1, "重さが抜ける");
  const down = lidOpening(-0.15);
  assert.ok(down.upper < rest.upper && down.upper > rest.upper - 0.15, "下を見ると上瞼が少し追う");
  assert.ok(down.lower > rest.lower, "下瞼は少し下がる");
  assert.equal(down.alert, 0);
  assert.ok(lidOpening(GAZE_LIMIT.pitch).upper < 60 * Math.PI / 180, "上限でも開き過ぎない");
});

test("瞼の重さ: 常に少し落ち、7〜14 秒に一度深く落ちて 1.4 秒留まる", () => {
  for (let t = 0; t < 30; t += 0.7) {
    const b = droopBaseline(t);
    assert.ok(b >= DROOP.base - 1e-9 && b <= DROOP.base + DROOP.wobble + 1e-9);
  }
  const d = planDroop(seq([0, 1]), 1000);
  assert.deepEqual(d, { at: 1000 + DROOP.minMs, depth: DROOP.deepMin + DROOP.deepSpan, holdMs: DROOP.holdMs });
  assert.ok(planDroop(seq([1, 0]), 0).at === DROOP.minMs + DROOP.spanMs);
  assert.ok(DROOP.deepMin + DROOP.deepSpan < 0.9, "深く落ちても閉じ切らない(半眼)");
});

test("瞳: 場面の基準に呼吸が乗り、[0.10, 0.50] に収まる。武装は常に neutral より開く", () => {
  for (let t = 0; t < 10; t += 0.37) {
    const n = pupilAngle("neutral", t);
    const a = pupilAngle("long-armed", t);
    assert.ok(n >= 0.1 && n <= 0.5 && a >= 0.1 && a <= 0.5);
    assert.ok(a > n);
  }
  assert.ok(Math.abs(pupilAngle("neutral", 0) - MODES.neutral.pupil) < 1e-9, "t=0 は基準値");
  assert.ok(pupilAngle("neutral", 1.05) > pupilAngle("neutral", 0), "呼吸で開く");
  // R65: 描画ループは表の行そのものを渡す。名前が無いからと neutral に落とさない
  assert.ok(Math.abs(pupilAngle(MODES["long-armed"], 0) - MODES["long-armed"].pupil) < 1e-9, "行を直接渡せる");
  assert.ok(pupilAngle(MODES.dejected, 0) < pupilAngle(MODES.neutral, 0), "沈むと瞳が締まる");
  assert.ok(Math.abs(pupilAngle({}, 0) - MODES.neutral.pupil) < 1e-9, "壊れた場面は neutral");
});

test("まばたき: 0 → 重く閉じ切る → だるく開く → 0。開くほうが遅い", () => {
  assert.equal(blinkAmount(0), 0);
  assert.equal(blinkAmount(-5), 0);
  assert.ok(blinkAmount(BLINK_CLOSE_MS / 2) > 0.4 && blinkAmount(BLINK_CLOSE_MS / 2) < 0.6);
  assert.ok(Math.abs(blinkAmount(BLINK_CLOSE_MS) - 1) < 1e-9);
  assert.ok(blinkAmount(BLINK_CLOSE_MS + BLINK_OPEN_MS / 2) > 0.4);
  assert.equal(blinkAmount(BLINK_CLOSE_MS + BLINK_OPEN_MS), 0);
  assert.equal(blinkAmount(10_000), 0);
  assert.ok(BLINK_OPEN_MS > BLINK_CLOSE_MS && BLINK_CLOSE_MS >= 200, "重く遅い");
  const b = planBlink(seq([0, 0.1]), 500);
  assert.deepEqual(b, { at: 500 + BLINK_MIN_MS, double: true });
  assert.equal(planBlink(seq([1, 0.5]), 0).at, BLINK_MIN_MS + BLINK_SPAN_MS);
  assert.equal(planBlink(seq([1, 0.5]), 0).double, false);
});

test("視線: ポインタの位置を ±limit に写し、外へは出ない", () => {
  const rect = { left: 100, top: 200, width: 136, height: 136 };
  const center = gazeFromPointer(168, 268, rect);
  assert.ok(Math.abs(center.yaw) < 1e-9 && Math.abs(center.pitch) < 1e-9);
  const right = gazeFromPointer(168 + 60, 268, rect);
  assert.ok(right.yaw > 0 && right.yaw < GAZE_LIMIT.yaw);
  const up = gazeFromPointer(168, 268 - 60, rect);
  assert.ok(up.pitch > 0, "上は正の pitch");
  const far = gazeFromPointer(5000, -5000, rect);
  assert.equal(far.yaw, GAZE_LIMIT.yaw);
  assert.equal(far.pitch, GAZE_LIMIT.pitch);
});

test("ばね: 目標へ収束し、臨界減衰で行き過ぎない", () => {
  const s = { x: 0, v: 0 };
  let maxX = 0;
  for (let i = 0; i < 120; i += 1) { stepSpring(s, 0.2, 1 / 60); maxX = Math.max(maxX, s.x); }
  assert.ok(Math.abs(s.x - 0.2) < 1e-3, `収束 ${s.x}`);
  assert.ok(maxX <= 0.2 + 1e-6, "行き過ぎない");
});

test("瞼の円: 目尻と縁の頂点を通り、正面は瞼の外、極は瞼の中", () => {
  const dot = (a, b) => a.x * b.x + a.y * b.y + a.z * b.z;
  const inside = (circle, p) => dot(circle.axis, p) > Math.cos(circle.halfAngle) + 1e-9;
  const c = 67 * Math.PI / 180;
  for (const [m, sign] of [[30 * Math.PI / 180, 1], [24 * Math.PI / 180, -1]]) {
    const circle = lidCircle(m, c, sign);
    assert.ok(!inside(circle, { x: 0, y: 0, z: 1 }), "正面は開いている");
    assert.ok(inside(circle, { x: 0, y: sign, z: 0 }), "極は覆う");
    const canthus = { x: Math.sin(c), y: 0, z: Math.cos(c) };
    assert.ok(Math.abs(dot(circle.axis, canthus) - Math.cos(circle.halfAngle)) < 1e-9, "目尻は縁の上");
    const margin = { x: 0, y: sign * Math.sin(m), z: Math.cos(m) };
    assert.ok(Math.abs(dot(circle.axis, margin) - Math.cos(circle.halfAngle)) < 1e-9, "縁の頂点は縁の上");
    // 縁の頂点の少し先(瞼側)は覆い、少し手前(瞳側)は開く
    assert.ok(inside(circle, { x: 0, y: sign * Math.sin(m + 0.05), z: Math.cos(m + 0.05) }));
    assert.ok(!inside(circle, { x: 0, y: sign * Math.sin(m - 0.05), z: Math.cos(m - 0.05) }));
  }
  // 閉じかけ(縁が中心より下)でも極は瞼の中、正面は覆われ、目尻は縁の上に留まる(輪郭が浮かない)
  const shut = lidPlane(-22 * Math.PI / 180, c, 1);
  const keep = (pl, p) => pl.normal.x * p.x + pl.normal.y * p.y + pl.normal.z * p.z + pl.constant > 1e-9;
  assert.ok(keep(shut, { x: 0, y: 1, z: 0 }), "極は瞼");
  assert.ok(keep(shut, { x: 0, y: 0, z: 1 }), "正面は覆われる");
  const canthusP = { x: Math.sin(c), y: 0, z: Math.cos(c) };
  assert.ok(Math.abs(shut.normal.x * canthusP.x + shut.normal.y * canthusP.y + shut.normal.z * canthusP.z + shut.constant) < 1e-9, "目尻は縁の上");
  const open = lidPlane(11 * Math.PI / 180, c, 1);
  assert.ok(Math.abs(open.normal.x * canthusP.x + open.normal.y * canthusP.y + open.normal.z * canthusP.z + open.constant) < 1e-9,
    "開いていても目尻は同じ点");
  assert.ok(Math.hypot(shut.normal.x, shut.normal.y, shut.normal.z) - 1 < 1e-9);
});

test("WebGL が無い環境では NOOP(attach しても何も起きない)", () => {
  const eye = initStandbyEye({ win: { document: { createElement: () => ({ getContext: () => null }) }, WebGLRenderingContext: undefined } });
  assert.equal(eye.available, false);
  eye.attach({}); eye.setMode("long-armed"); eye.detach(); eye.destroy();
  assert.equal(initStandbyEye({ win: null }).available, false);
});

test("配線: app.js は eye3d を後読みし、empty card の直後に attach する。CSS は 3D で線画を隠す", () => {
  const APP = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  const CSS = readFileSync(new URL("../styles.css", import.meta.url), "utf8");
  assert.match(APP, /import\("\.\/eye3d\.js"\)/, "後読み(起動チャンクに入れない)");
  // R65: host は「WATCH の待機カード」が第一。無ければ広い画面の駐在所(.pet-eye)へ移る
  assert.match(APP, /activeViewName === "watch" \? \(stateStack\?\.querySelector\("\.standby"\) \|\| null\) : null/, "host は .standby");
  assert.match(APP, /standbyEye\.attach\(inCard \|\| den\);/, "居場所はカード or 駐在所の 1 つ");
  assert.ok(APP.split("mountStandbyEye();").length >= 4, "empty card を描く 2 箇所 + 初回で attach");
  assert.match(CSS, /\.standby\.has-3d \.standby-eye \{ display: none; \}/);
  assert.match(CSS, /\.standby\.has-3d \.standby-sigil \{ display: none; \}/, "環・目盛・七芒星の平面も 3D 時は消す");
  // 起動直後: 3D が届く前に旧紋章が一瞬出ない(WebGL があれば最初の描画から隠す。来なければ戻す)
  assert.match(APP, /class="standby\$\{standby3d \? " is-3d-pending" : ""\}"/, "empty card は起動時の WebGL 判定で pending を付ける");
  assert.match(APP, /if \(!standbyEye\.available\) standbyFallbackToFlat\(\);/, "NOOP なら平面へ戻す");
  assert.match(APP, /\.catch\(\(\) => \{[\s\S]*?standbyFallbackToFlat\(\);/, "読み込み失敗でも平面へ戻す");
  assert.match(CSS, /\.standby\.is-3d-pending \.standby-sigil,\n\.standby\.is-3d-pending \.standby-eye \{ display: none; \}/);
  assert.match(CSS, /\.standby\.is-3d-pending:not\(\.has-3d\)::before \{/, "届くまでの黒鉄の玉");
  assert.match(CSS, /\.standby canvas\.standby-eye3d \{ position: absolute; inset: 0;/);
});

test("外枠: 七芒星 {7/3} は 7 つの異なる頂点を 3 つ飛ばしで結ぶ。寸法は眼球の外・キャンバスの内", () => {
  const pts = heptagramPoints(1);
  assert.equal(pts.length, 7);
  const angles = pts.map((p) => Math.atan2(p.y, p.x));
  assert.equal(new Set(angles.map((a) => a.toFixed(6))).size, 7, "頂点は重複しない");
  for (let i = 0; i < 7; i += 1) {
    let step = angles[(i + 1) % 7] - angles[i];
    step = ((step % (Math.PI * 2)) + Math.PI * 2) % (Math.PI * 2);
    assert.ok(Math.abs(step - (3 / 7) * Math.PI * 2) < 1e-9 || Math.abs(step - (4 / 7) * Math.PI * 2) < 1e-9, "3 つ飛ばし");
    assert.ok(Math.abs(Math.hypot(pts[i].x, pts[i].y) - 1) < 1e-9);
  }
  assert.ok(FRAME.star > 1.2 && FRAME.outer > FRAME.tickOuter && FRAME.outer < 1.5, "ベゼル(1.2)の外、視野(≈1.53)の内");
  assert.equal(FRAME.spinSeconds, 80, "旧 CSS の nw-spin と同じ周期");
});

test("筐体(R65): 据え付けの環・受け金具・副尺は視野の内で、回る外枠と役割が分かれている", () => {
  const VISIBLE = 6.15 * Math.tan(14 * Math.PI / 180);   // camera(28°, z=6.15)が写す半径
  assert.ok(FRAME.collar + FRAME.collarTube < VISIBLE, "カラーはキャンバスの内");
  assert.ok(FRAME.bolt + 0.04 < VISIBLE);
  assert.ok(FRAME.collarArcDeg * 4 < 360, "4 分割 = 受け金具の位置に切れ目が残る");
  assert.ok(FRAME.bolt > FRAME.outer && FRAME.bolt <= FRAME.collar, "リベットは外枠の外・カラーの上");
  assert.ok(FRAME.bracket - FRAME.bracketLen / 2 < 1.2, "受け金具はベゼルまで届く");
  assert.ok(FRAME.bracket + FRAME.bracketLen / 2 > FRAME.outer, "受け金具はカラー側へ橋を架ける");
  // バレルは眼球(半径 1)を包み、ベゼルの環(半径 1.20・太さ 0.05)の陰に隠れる太さ
  assert.ok(FRAME.barrel > 1 && FRAME.barrel < 1.25, "バレルは眼球の外・ベゼルの陰");
  assert.ok(FRAME.index > 1.2 && FRAME.index < FRAME.collar, "基準マークはベゼルとカラーの間");
  // 副尺は外枠(1.38)とカラー(1.45)の**空き帯**に置く。内側(ベゼル〜目盛)は既に埋まっていて、
  // そこへ足すと小さい画面で潰れる(2026-09-07 に一度そこへ置いて外した)。
  assert.ok(FRAME.vernier - FRAME.vernierTick / 2 > FRAME.outer, "副尺は外枠の外");
  assert.ok(FRAME.vernier + FRAME.vernierTick / 2 < FRAME.collar, "副尺はカラーの内");
  assert.notEqual(FRAME.vernierSeconds, FRAME.spinSeconds, "外枠と同じ周期では機械に見えない");
  const SRC = readFileSync(new URL("../eye3d.js", import.meta.url), "utf8");
  assert.match(SRC, /-\(t \* Math\.PI \* 2\) \/ FRAME\.vernierSeconds/, "副尺は外枠と逆回り");
  // 2026-09-07 ユーザー指示「星と線を逆方向に回して」: 環と目盛は正、七芒星は負
  assert.match(SRC, /-\(t \* Math\.PI \* 2\) \/ FRAME\.starSeconds/, "七芒星は環と逆回り");
  assert.match(SRC, /orbit\.rotation\.set\([^)]*\(t \* Math\.PI \* 2\) \/ FRAME\.spinSeconds/, "環は正回り");
  assert.notEqual(FRAME.starSeconds, FRAME.spinSeconds, "周期もずらす(噛み合って見えないように)");
  assert.ok(!/housing\.rotation/.test(SRC), "筐体は据え付け —— 回さないから目が「取り付けられて」見える");
  assert.match(SRC, /indexMat\.emissiveIntensity = 0\.3 \+ glow\.x/, "基準マークは場面の残り火に連動");
});

test("眼球(R65 追補): 瞳の縁に赤い輪を作らず、瞳孔縁のフリルと 5 層の血管を持つ", () => {
  const SRC = readFileSync(new URL("../eye3d.js", import.meta.url), "utf8");
  const frag = SRC.slice(SRC.indexOf("const EYE_FRAG"), SRC.indexOf("function supportsWebGL"));
  // 2026-09-07 ユーザー「黒目の中の赤い淵を無くして」: 虹彩側の発光はゼロ
  assert.match(frag, /emis = 0\.0;/, "瞳の縁の残り火は置かない");
  assert.ok(!/emis = uGlow \* exp/.test(frag), "赤い輪(旧実装)が残っていない");
  assert.ok(!/uGlow \* [^;]*uTint/.test(frag), "場面の色を虹彩へ塗らない");
  // 2026-09-07 追補2「境目はギザギザではなく滑らか、表面的じゃなく立体的に」:
  // 縁は真円(波の変調を持たない)。立体は輪郭ではなく陰影と視差で作る。
  assert.match(frag, /float pupilTheta = uPupil;/, "瞳孔縁は真円 —— crenation は撤去した");
  assert.ok(!/pupilTheta = uPupil \* \(1\.0/.test(frag), "半径を波で変調していない");
  assert.match(frag, /irisSlope = smoothstep\(1\.0, pupilR/, "虹彩は瞳へ落ち込む漏斗");
  assert.match(frag, /N = normalize\(N \+ \(radialV \/ rl\) \* irisSlope\)/, "漏斗は法線を径方向へ傾ける");
  assert.match(frag, /float rimAO = exp\(-pow\(\(theta - pupilTheta\) \/ 0\.0\d+/, "縁の外の接触の影");
  assert.match(frag, /vec3 hole = normalize\(p - tangential \* 0\.0\d+\);/, "瞳は視差でずれる穴");
  assert.match(frag, /smoothstep\(pupilTheta - 0\.020, pupilTheta \+ 0\.014, thetaHole\)/, "縁は滑らか");
  assert.match(SRC, /uViewLocal: \{ value: new THREE\.Vector3/, "視差の基準(ローカルのカメラ位置)");
  assert.match(SRC, /uViewLocal\.value\.copy\(camera\.position\)/, "視線が動くたびに更新する");
  // 「白目の赤黒い血管を増やして」: 網は 5 層(幹 2・枝 2・毛細)
  for (const layer of ["trunk", "trunk2", "branch", "branch2", "cap"]) {
    assert.ok(frag.includes(`float ${layer} = vein(`), `${layer} の層`);
  }
  const hot = frag.match(/vec3 hot = vec3\(([\d.]+),/);
  assert.ok(hot && Number(hot[1]) <= 0.026, "瞳孔以外の黒目は黒い(明部でも 0.026 以下)");
  assert.match(frag, /float limbalRing = 1\.0 - smoothstep\(uIris/, "輪部の環(白い 1px を出さない)");
  const blood = frag.match(/vec3 blood = vec3\(([\d.]+), ([\d.]+), ([\d.]+)\)/);
  assert.ok(blood, "血の色");
  const [r, g, b] = blood.slice(1).map(Number);
  assert.ok(r < 0.2 && r > 4 * g && r > 4 * b, "赤黒い(暗く、赤だけが立つ)");
});

test("配管の朱の帯(R65 追補): 場面ごとに発光の基準・脈・拍が違い、負けは消えかけの揺らぎ", () => {
  for (const [name, mode] of Object.entries(MODES)) {
    assert.ok(mode.accent && mode.accent.base >= 0 && mode.accent.amp >= 0 && mode.accent.hz > 0, `${name} に accent`);
  }
  assert.ok(MODES.elated.accent.base > MODES["long-armed"].accent.base && MODES["long-armed"].accent.base > MODES.long.accent.base
    && MODES.long.accent.base > MODES.neutral.accent.base && MODES.neutral.accent.base > MODES.dejected.accent.base,
    "明るさの序列: 勝ち > 武装 > 建玉 > 中立 > 負け");
  assert.ok(MODES.elated.accent.hz > MODES["long-armed"].accent.hz && MODES["long-armed"].accent.hz > MODES.neutral.accent.hz, "拍は高ぶるほど速い");
  assert.equal(MODES.dejected.accent.flicker, true, "負けは揺らぐ");
  // 脈: base〜base+amp の間で振れる
  for (let t = 0; t < 6; t += 0.13) {
    const v = accentIntensity("long-armed", t);
    assert.ok(v >= MODES["long-armed"].accent.base - 1e-9 && v <= MODES["long-armed"].accent.base + MODES["long-armed"].accent.amp + 1e-9);
  }
  assert.ok(accentIntensity("elated", 0.1) > accentIntensity("neutral", 0.1) && accentIntensity("neutral", 0.1) > accentIntensity("dejected", 0.1));
  // 負け: 落ち込みが不規則に入る(最小値は base より明確に低い)
  let lo = Infinity;
  for (let t = 0; t < 12; t += 0.05) lo = Math.min(lo, accentIntensity("dejected", t));
  assert.ok(lo < MODES.dejected.accent.base * 0.5, "消えかけ");
  assert.ok(Math.abs(accentIntensity({}, 1) - accentIntensity("neutral", 1)) < 1e-9, "壊れた場面は neutral");
  const SRC = readFileSync(new URL("../eye3d.js", import.meta.url), "utf8");
  assert.match(SRC, /accentMats\[i\]\.emissiveIntensity = level/, "毎フレーム、両瞼の帯へ流す");
  assert.match(SRC, /reduced \? mode\.accent\.base : accentIntensity\(mode, t\)/, "reduced-motion では脈を止める");
});

test("目は表示専用(view・注文・保存・ネットワークに触れない)", () => {
  const source = readFileSync(new URL("../eye3d.js", import.meta.url), "utf8");
  for (const forbidden of ["fetch(", "sendData", "localStorage", "/api/", "WebSocket", "currentView", "order"]) {
    assert.ok(!source.includes(forbidden), `${forbidden} を含まない`);
  }
});
