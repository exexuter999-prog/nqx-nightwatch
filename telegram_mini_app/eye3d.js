/**
 * STANDBY EYE — 待機紋章の目(R60, 2026-09-06)。
 *
 * NO SCENARIO のとき中央に出る「赤い目」を、54px の線画から Three.js の眼球にする。
 * 眼球は 1 枚のシェーダ(強膜の血管・輪部・虹彩の放射繊維・クリプト・襞・瞳)、その上に
 * 角膜(透明・クリアコート・スタジオ環境の映り込み)、削り出しの瞼 2 枚(縁に光る lip)、
 * ベゼルのリング。生きている手掛かりは 3 つ —— サッケード(視線の小さな跳び)、まばたき、
 * 瞳の呼吸。ポインタが近づけばそちらを見る。
 *
 * 表示専用。view も注文も読まない。app.js が empty card を描いた直後に attach(host) を呼び、
 * カードが消えれば detach で止まる(WebGL コンテキストは 1 つを使い回す)。
 * prefers-reduced-motion では静止した目を 1 枚描くだけ(動かない)。
 *
 * 純関数(MODES / planSaccade / pupilAngle / blinkAmount / gazeFromPointer / stepSpring /
 * lidCircle)は DOM を触らないので node --test から読める。
 */

import * as THREE from "three";
import { BloomEffect, EffectComposer, EffectPass, RenderPass } from "postprocessing";

const DEG = Math.PI / 180;

/**
 * 場面ごとの目。pupil は瞳の角半径(rad)。**瞳は散大している**(2026-09-06 ユーザー指示:
 * 瞳孔を開かせ、赤目ではなく黒目、白目は充血)—— 虹彩 0.56 のうち 0.40 が瞳。
 * 武装でさらに開く(興奮)。glow は瞳の縁のごく薄い残り火(黒目なので基本は光らない)。
 * tint は残り火とリムライトの色で、虹彩の色ではない。saccade は跳びの頻度倍率。
 * lid は瞼の重さの倍率(R65): 1 が既定のだるさ、小さいほど見開き、大きいほど落ちる。
 *
 * R65(2026-09-07): 決済の一喜一憂を目で出すため elated / dejected を足した。
 * 勝ちは見開いて瞳が開き残り火が強くなる、負けは瞼が落ちて瞳が締まり光が消える。
 */
export const MODES = {
  neutral: { glow: 0.05, pupil: 0.40, tint: 0xff3b4f, saccade: 1.0, lid: 1, accent: { base: 0.35, amp: 0.12, hz: 0.22 } },
  long: { glow: 0.10, pupil: 0.42, tint: 0xff3b4f, saccade: 1.2, lid: 1, accent: { base: 0.6, amp: 0.3, hz: 0.55 } },
  short: { glow: 0.10, pupil: 0.42, tint: 0xce5a43, saccade: 1.2, lid: 1, accent: { base: 0.6, amp: 0.3, hz: 0.55 } },
  "long-armed": { glow: 0.22, pupil: 0.46, tint: 0xff3b4f, saccade: 1.6, lid: 0.8, accent: { base: 1.0, amp: 0.8, hz: 1.7 } },
  "short-armed": { glow: 0.22, pupil: 0.46, tint: 0xce5a43, saccade: 1.6, lid: 0.8, accent: { base: 1.0, amp: 0.8, hz: 1.7 } },
  elated: { glow: 0.34, pupil: 0.47, tint: 0xff3b4f, saccade: 2.2, lid: 0.3, accent: { base: 1.9, amp: 1.0, hz: 2.6 } },
  dejected: { glow: 0.01, pupil: 0.30, tint: 0xce5a43, saccade: 0.45, lid: 1.9, accent: { base: 0.10, amp: 0.10, hz: 0.5, flicker: true } },
};
/**
 * 配管の朱の帯の明るさ(R65 追補、2026-09-07 ユーザー「パイプの赤い部分の状況別の発光」)。
 * 中立 = 弱い残り火がゆっくり息をする / 建玉 = 脈が乗る / 武装 = 速い鼓動 / 勝ち = 強く速く /
 * 負け = 消えかけの揺らぎ(flicker: 不規則に落ちる)。純関数なので node で検査できる。
 */
export function accentIntensity(mode, tSec) {
  const row = typeof mode === "string" ? resolveMode(mode) : (mode && mode.accent ? mode : MODES.neutral);
  const a = row.accent;
  const beat = 0.5 + 0.5 * Math.sin(tSec * Math.PI * 2 * a.hz);
  let value = a.base + a.amp * beat;
  if (a.flicker) {
    // 消えかけ: 2 つの無理数比の正弦を掛けた不規則な落ち込み(周期に見えない)
    const drop = Math.max(0, Math.sin(tSec * 7.3) * Math.sin(tSec * 2.9 + 1.0));
    value *= 1.0 - 0.75 * drop;
  }
  return value;
}
export function resolveMode(name) {
  return MODES[name] || MODES.neutral;
}

/** 虹彩の角半径(rad)。眼球半径 1 に対し約 11.5mm/24mm の虹彩。 */
export const IRIS_ANGLE = 0.56;
export const GAZE_LIMIT = { yaw: 0.36, pitch: 0.24 };

/**
 * だるそうな目(2026-09-06 ユーザー指示「ダークサイド感、だるそーな感じ」): 視線は 3〜7 秒に
 * 一度、±8° / ±6° の範囲へ**ゆっくり漂う**(跳ばない)。少し下を見がち(pitchBias)。
 */
export const SACCADE = { minMs: 3000, spanMs: 4000, yaw: 8 * DEG, pitch: 6 * DEG, pitchBias: -5 * DEG, omega: 8 };
export function planSaccade(rand, nowMs, rate = 1) {
  const interval = (SACCADE.minMs + rand() * SACCADE.spanMs) / Math.max(0.25, rate);
  return {
    at: nowMs + interval,
    yaw: (rand() * 2 - 1) * SACCADE.yaw,
    pitch: (rand() * 2 - 1) * SACCADE.pitch + SACCADE.pitchBias,
  };
}

/**
 * 瞼の重さ。常に少し落ちていて(base)、ゆっくり揺れ(wobble)、7〜14 秒に一度は深く落ちて
 * 1.4 秒ほど留まる(半眼で止まる = だるい)。まばたきとは別の層で、深い方を採る。
 */
export const DROOP = { base: 0.16, wobble: 0.08, deepMin: 0.5, deepSpan: 0.18, holdMs: 1400, minMs: 7000, spanMs: 7000, omega: 3.5 };
export function planDroop(rand, nowMs) {
  return { at: nowMs + DROOP.minMs + rand() * DROOP.spanMs, depth: DROOP.deepMin + rand() * DROOP.deepSpan, holdMs: DROOP.holdMs };
}
/** 重さの基準線(揺れ込み)。0.16〜0.24。 */
export function droopBaseline(tSec) {
  return DROOP.base + DROOP.wobble * (0.5 + 0.5 * Math.sin(tSec * 0.45));
}

/**
 * 瞼は視線に連動する(2026-09-06 ユーザー指示「上を見るときは瞼を開ける」)。
 * 上を見る(pitch > 0)と上瞼が視線の 1.8 倍上がり、下瞼はわずかに上がる。下を見ると上瞼が
 * 少し追い、下瞼が少し下がる。返すのは瞼の縁の角(rad): upper は中心より上、lower は中心より下。
 * alert は 0..1 で、上を見るほど瞼の重さ(DROOP)が抜ける割合。
 */
export const LID_OPEN = { upper: 11 * DEG, lower: 28 * DEG };
export const LID_SHUT = 22 * DEG;
export function lidOpening(pitchRad) {
  const up = Math.max(0, pitchRad);
  const down = Math.max(0, -pitchRad);
  return {
    upper: LID_OPEN.upper + up * 1.8 - down * 0.5,
    lower: LID_OPEN.lower - up * 0.25 + down * 0.4,
    alert: Math.min(1, up / 0.18),
  };
}

/**
 * 瞳の角半径。場面の基準値 + 4.2 秒周期の呼吸。虹彩(0.56)の内側 [0.10, 0.50] に収める。
 * 場面は名前でも表の行そのものでも受ける —— 描画ループは行を持っているのに `mode?.name`
 * を見ていたため、武装しても瞳が neutral のままだった(R65 で修正)。
 */
export function pupilAngle(mode, tSec) {
  const row = typeof mode === "string" ? resolveMode(mode)
    : (Number.isFinite(mode?.pupil) ? mode : MODES.neutral);
  const base = row.pupil;
  const breathe = 0.012 * Math.sin((tSec * Math.PI * 2) / 4.2);
  return Math.min(0.5, Math.max(0.1, base + breathe));
}

/** まばたきの閉じ量 0..1。重く 260ms で閉じ、だるく 460ms で開く。終われば 0。 */
export const BLINK_CLOSE_MS = 260;
export const BLINK_OPEN_MS = 460;
export function blinkAmount(elapsedMs) {
  if (elapsedMs <= 0) return 0;
  if (elapsedMs < BLINK_CLOSE_MS) return smooth(elapsedMs / BLINK_CLOSE_MS);
  const open = (elapsedMs - BLINK_CLOSE_MS) / BLINK_OPEN_MS;
  return open >= 1 ? 0 : 1 - smooth(open);
}
function smooth(x) {
  const t = Math.min(1, Math.max(0, x));
  return t * t * (3 - 2 * t);
}

/** 次のまばたき。3〜8 秒後、12% で二度まばたき。 */
export const BLINK_MIN_MS = 3000;
export const BLINK_SPAN_MS = 5000;
export function planBlink(rand, nowMs) {
  return { at: nowMs + BLINK_MIN_MS + rand() * BLINK_SPAN_MS, double: rand() < 0.12 };
}

/** ポインタ位置(px)→ 視線(rad)。キャンバス中心からのずれを、幅の半分で ±limit に写す。 */
export function gazeFromPointer(px, py, rect) {
  const cx = rect.left + rect.width / 2;
  const cy = rect.top + rect.height / 2;
  const reach = Math.max(1, rect.width) * 1.6;
  const yaw = clamp(((px - cx) / reach) * GAZE_LIMIT.yaw * 2, -GAZE_LIMIT.yaw, GAZE_LIMIT.yaw);
  const pitch = clamp(((cy - py) / reach) * GAZE_LIMIT.pitch * 2, -GAZE_LIMIT.pitch, GAZE_LIMIT.pitch);
  return { yaw, pitch };
}
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/** 減衰ばね 1 ステップ(サッケードは速く、行き過ぎない)。state = { x, v } を書き換える。 */
export function stepSpring(state, target, dt, omega = 30, zeta = 1) {
  const f = 1 + 2 * dt * zeta * omega;
  const oo = omega * omega;
  const hoo = dt * oo;
  const hhoo = dt * hoo;
  const det = 1 / (f + hhoo);
  const x = state.x;
  const v = state.v;
  state.x = (f * x + dt * v + hhoo * target) * det;
  state.v = (v + hoo * (target - x)) * det;
  return state;
}

/**
 * 瞼の縁は球面上の円。目尻(±canthus, 水平)と縁の頂点(垂直に margin、負なら中心より
 * 反対側 = 閉じかけ)の 3 点を通る平面を求め、極(上瞼なら +y)を含む側を瞼とする。
 * 瞼 = { p : normal·p + constant > 0 }(three.js の Plane と同じ符号)。sign=+1 が上瞼、-1 が下瞼。
 * まばたきはこの平面だけを動かす —— 目尻 2 点は常に平面上なので、瞼の輪郭が浮かない
 * (球帽を回す方式は目尻が動いて輪郭が浮いた: 2026-09-06 ユーザー報告)。
 */
export function lidPlane(marginRad, canthusRad, sign = 1) {
  const cosC = Math.cos(canthusRad);
  const cosM = Math.cos(marginRad);
  const sinM = Math.sin(marginRad);
  const ny = Math.abs(sinM) < 1e-6 ? 0 : (cosC - cosM) / (sign * sinM);
  const len = Math.hypot(ny, 1);
  const n = { x: 0, y: ny / len, z: 1 / len };
  const d = n.z * cosC;
  // 極(0, sign, 0)を含む側が瞼
  const poleSide = Math.sign(n.y * sign - d) || 1;
  return { normal: { x: 0, y: poleSide * n.y, z: poleSide * n.z }, constant: -poleSide * d };
}

/** 互換: 瞼の球帽の軸(= 平面の法線)と半角。cos(半角) = -constant。 */
export function lidCircle(marginRad, canthusRad, sign = 1) {
  const plane = lidPlane(marginRad, canthusRad, sign);
  return { axis: plane.normal, halfAngle: Math.acos(Math.max(-1, Math.min(1, -plane.constant))) };
}

/**
 * 回転する外枠(旧 SVG 紋章の環・目盛・七芒星)を 3D に統合する寸法(2026-09-06 ユーザー指示)。
 * 眼球半径 1 に対し、外環 1.42・目盛 15° ごと(45° ごとに長い)・七芒星 {7/3} は半径 1.33。
 * 80 秒で一周(旧 CSS の nw-spin と同じ)、視線に 0.35 倍だけ傾いて付いてくる。
 */
export const FRAME = {
  // 回る外枠(旧 SVG の環・目盛・七芒星)。筐体を足した分だけ一段内側へ寄せた(R65)
  outer: 1.38, tickOuter: 1.36, tickMajor: 0.17, tickMinor: 0.09, tickStepDeg: 15, star: 1.30,
  // 環と目盛は時計回りに 80 秒、七芒星は**逆回り**に 62 秒(2026-09-07 ユーザー指示
  // 「星と線を逆方向に回して」)。周期をずらすと、二枚が噛み合わずに滑る機械に見える。
  spinSeconds: 80, starSeconds: 62, follow: 0.35,
  // R65: 据え付けの筐体。カラー(4 分割の環)・受け金具・リベット・基準マーク・バレル。
  // 視野は camera(28°, z=6.15)で半径 1.533 まで —— カラーの外周はその内側に収める。
  collar: 1.462, collarTube: 0.022, collarArcDeg: 70, bracket: 1.315, bracketLen: 0.33, bolt: 1.462,
  barrel: 1.22, barrelDepth: 0.62, index: 1.30,
  // 副尺(逆回りの微目盛)。外枠(1.38)とカラー(1.45)の**間の空き帯**に置く ——
  // ベゼル〜目盛の帯(1.15〜1.36)は既に埋まっていて、そこへ足すと小さい画面で潰れる。
  vernier: 1.412, vernierTick: 0.036, vernierStepDeg: 10, vernierSeconds: 47,
};
/** 七芒星 {7/3}: 頂点を 3 つ飛ばしで結ぶ順に並べた 7 点(z=0 平面)。 */
export function heptagramPoints(radius = FRAME.star) {
  const pts = [];
  for (let i = 0; i < 7; i += 1) {
    const a = (((i * 3) % 7) / 7) * Math.PI * 2 - Math.PI / 2;
    pts.push({ x: Math.cos(a) * radius, y: Math.sin(a) * radius, z: 0 });
  }
  return pts;
}

// ---------------------------------------------------------------- シェーダ(眼球)

const EYE_VERT = /* glsl */`
  uniform float uIris;
  uniform float uSeed;
  // 3D 値ノイズ(フラグメント側と同じ式の軽い版)。強膜の起伏に使う
  float hashV(vec3 p) { p = fract(p * 0.3183099 + vec3(0.1, 0.2, 0.3)); p *= 17.0; return fract(p.x * p.y * p.z * (p.x + p.y + p.z)); }
  float vnoiseV(vec3 p) {
    vec3 i = floor(p); vec3 f = fract(p); vec3 u = f * f * (3.0 - 2.0 * f);
    return mix(
      mix(mix(hashV(i), hashV(i + vec3(1.0, 0.0, 0.0)), u.x), mix(hashV(i + vec3(0.0, 1.0, 0.0)), hashV(i + vec3(1.0, 1.0, 0.0)), u.x), u.y),
      mix(mix(hashV(i + vec3(0.0, 0.0, 1.0)), hashV(i + vec3(1.0, 0.0, 1.0)), u.x), mix(hashV(i + vec3(0.0, 1.0, 1.0)), hashV(i + vec3(1.0, 1.0, 1.0)), u.x), u.y),
      u.z);
  }
  varying vec3 vLocal;
  varying vec3 vWorld;
  varying vec3 vNormal;
  varying vec3 vViewPos;
  varying vec3 vAxis;      // 眼球の光軸(+z)をビュー空間へ。虹彩の漏斗を作るのに使う
  void main() {
    vAxis = normalize(normalMatrix * vec3(0.0, 0.0, 1.0));
    // R65 追補3(2026-09-07「シンプルな球体はつまらない、血管を造形して」):
    // 強膜だけを低周波でうねらせ、完全な球をやめる。太い血管の走る所が僅かに盛り上がる。
    // 角膜側(虹彩・瞳)は滑らかなまま —— ここが歪むと瞳の縁が汚れる。
    float thetaV = acos(clamp(normalize(position).z, -1.0, 1.0));
    float scleraV = smoothstep(uIris + 0.02, uIris + 0.22, thetaV);
    float swell = (vnoiseV(normalize(position) * 3.1 + uSeed) - 0.5) * 2.0
                + (vnoiseV(normalize(position) * 6.3 + uSeed + 11.0) - 0.5);
    vec3 displaced = position * (1.0 + swell * 0.0065 * scleraV);
    vLocal = displaced;
    vWorld = (modelMatrix * vec4(displaced, 1.0)).xyz;
    vNormal = normalize(normalMatrix * normal);
    vec4 mv = modelViewMatrix * vec4(displaced, 1.0);
    vViewPos = mv.xyz;
    gl_Position = projectionMatrix * mv;
  }
`;

const EYE_FRAG = /* glsl */`
  precision highp float;
  uniform float uTime;
  uniform float uPupil;
  uniform float uIris;
  uniform float uGlow;
  uniform vec3 uTint;
  uniform vec3 uKeyDir;
  uniform vec3 uRimDir;
  uniform float uSeed;
  uniform vec4 uUpperPlane;   // 瞼の平面(世界座標、xyz = 法線 / w = 定数)。瞼側で正
  uniform vec4 uLowerPlane;
  uniform vec3 uViewLocal;    // 眼球ローカル座標でのカメラ位置(瞳の視差に使う)
  varying vec3 vAxis;
  varying vec3 vLocal;
  varying vec3 vWorld;
  varying vec3 vNormal;
  varying vec3 vViewPos;

  float hash21(vec2 p) { p = fract(p * vec2(123.34, 345.45)); p += dot(p, p + 34.345); return fract(p.x * p.y); }
  float vnoise(vec2 p) {
    vec2 i = floor(p); vec2 f = fract(p); vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash21(i), hash21(i + vec2(1.0, 0.0)), u.x),
               mix(hash21(i + vec2(0.0, 1.0)), hash21(i + vec2(1.0, 1.0)), u.x), u.y);
  }
  float fbm(vec2 p) {
    float v = 0.0; float a = 0.5;
    for (int i = 0; i < 4; i++) { v += a * vnoise(p); p = p * 2.03 + 17.1; a *= 0.5; }
    return v;
  }
  // 球面上で継ぎ目を作らないための 3D ノイズ(血管は球の座標そのままで引く)
  float hash31(vec3 p) { p = fract(p * 0.3183099 + vec3(0.1, 0.2, 0.3)); p *= 17.0; return fract(p.x * p.y * p.z * (p.x + p.y + p.z)); }
  float vnoise3(vec3 p) {
    vec3 i = floor(p); vec3 f = fract(p); vec3 u = f * f * (3.0 - 2.0 * f);
    return mix(
      mix(mix(hash31(i), hash31(i + vec3(1.0, 0.0, 0.0)), u.x), mix(hash31(i + vec3(0.0, 1.0, 0.0)), hash31(i + vec3(1.0, 1.0, 0.0)), u.x), u.y),
      mix(mix(hash31(i + vec3(0.0, 0.0, 1.0)), hash31(i + vec3(1.0, 0.0, 1.0)), u.x), mix(hash31(i + vec3(0.0, 1.0, 1.0)), hash31(i + vec3(1.0, 1.0, 1.0)), u.x), u.y),
      u.z);
  }
  float fbm3(vec3 p) {
    float v = 0.0; float a = 0.5;
    for (int i = 0; i < 4; i++) { v += a * vnoise3(p); p = p * 2.02 + vec3(13.7); a *= 0.5; }
    return v;
  }
  // 血管 = ノイズの 0.5 等高線。等高線は勝手に蛇行し枝分かれするので、血管らしい網になる
  float vein(float n, float w) { return 1.0 - smoothstep(0.0, w, abs(n - 0.5)); }

  void main() {
    vec3 p = normalize(vLocal);
    float theta = acos(clamp(p.z, -1.0, 1.0));   // 光軸(+z)からの角
    float phi = atan(p.y, p.x);
    float r = theta / uIris;                     // 0 = 瞳の中心, 1 = 輪部
    vec3 albedo; float rough; float emis = 0.0;
    float irisSlope = 0.0;   // 虹彩の漏斗の傾き(0 = 球面のまま)
    float bumpHeight = 0.0;  // 強膜の隆起(血管)。画面微分で法線に落とす

    if (r < 1.0) {
      // ---- 虹彩(黒目): 散大した瞳の外に残る細い環。炭色の放射繊維 + 細かな粒 + クリプト + 襞。
      //      瞳の縁だけ、ごく薄い焦茶の襟(collarette)。輪部は黒へ沈む。
      vec2 disc = vec2(cos(phi), sin(phi));
      float streak = fbm(disc * 9.0 + vec2(r * 1.4, -r * 0.9) + uSeed);
      float fine = vnoise(disc * 26.0 + vec2(r * 3.0) + uSeed);
      float crypt = smoothstep(0.55, 0.8, fbm(disc * 3.2 + r * 2.0 + 7.3)) * smoothstep(0.6, 0.85, r);
      float pupilR = uPupil / uIris;
      // R65 追補2(2026-09-07 ユーザー「境目はギザギザではなく滑らか、表面的じゃなく立体的に」):
      // 瞳孔縁の crenation は撤去 —— 縁は真円で滑らかに落とす。立体感は輪郭の形ではなく
      // **陰影と視差**で作る: (a) 虹彩は瞳へ向かって落ち込む漏斗(法線を径方向へ傾ける)、
      // (b) 縁の外側に接触の影、(c) 瞳は視差でずれる穴(奥に開いた孔として振る舞う)。
      float pupilTheta = uPupil;
      float collar = exp(-pow((r - (pupilR + 0.06)) / 0.05, 2.0));
      float limbus = smoothstep(0.8, 1.0, r);
      float furrow = smoothstep(0.35, 0.75, sin(phi * 21.0 + streak * 6.0) * 0.5 + 0.5) * smoothstep(0.7, 0.95, r) * 0.3;
      // 黒目: ほぼ黒。光が当たった所だけ、繊維がかすかに灰に浮く(茶は完全に無し —— 2026-09-06 指示)
      vec3 deep = vec3(0.0018, 0.0018, 0.0022);
      vec3 mid = vec3(0.013, 0.013, 0.0145);
      vec3 hot = vec3(0.024, 0.024, 0.027);
      float t = smoothstep(pupilR, 1.0, r);
      albedo = mix(hot, mid, t);
      albedo = mix(albedo, deep, limbus * 0.9);
      albedo *= 0.5 + 0.7 * streak;
      albedo *= 1.0 - crypt * 0.6;
      albedo *= 1.0 - furrow;
      albedo += collar * vec3(0.014, 0.014, 0.016);
      albedo += fine * 0.013;
      // 残り火は**置かない**(2026-09-07 ユーザー「黒目の中の赤い淵を無くして」)。
      // 場面の高ぶりは、色を持たない繊維のわずかな明るさとして出す(赤い輪は作らない)。
      emis = 0.0;
      albedo += uGlow * 0.018 * streak * vec3(1.0, 1.0, 1.0);
      // (a) 漏斗: 瞳へ近いほど面が内へ落ちる。法線を径方向へ傾け、光の当たり方で立体に見せる
      irisSlope = smoothstep(1.0, pupilR + 0.04, r) * 0.75;
      // (b) 接触の影: 縁の外側だけ滑らかに沈む(0.075rad の裾。ここが「厚み」に見える)
      float rimAO = exp(-pow((theta - pupilTheta) / 0.045, 2.0));
      albedo *= 1.0 - 0.30 * rimAO;
      // (c) 視差の穴: 面に沿った視線成分だけずらして瞳を判定する。斜めから見ると穴が
      //     奥に開いているように動く(平面に描いた円ではなくなる)
      vec3 vdir = normalize(uViewLocal - vLocal);
      vec3 tangential = vdir - p * dot(vdir, p);
      vec3 hole = normalize(p - tangential * 0.085);
      float thetaHole = acos(clamp(hole.z, -1.0, 1.0));
      // 縁は滑らかに落ちる(±0.02rad)。瞳の底は完全な黒で、奥側の壁だけごく僅かに明るい
      float pupilEdge = smoothstep(pupilTheta - 0.020, pupilTheta + 0.014, thetaHole);
      albedo = mix(vec3(0.0012, 0.0011, 0.0014), albedo, pupilEdge);
      rough = 0.55;
    } else {
      // ---- 強膜(充血): 骨色の地に 3 階層の血管(太い幹・枝・毛細血管)を走らせ、
      //      露出の多い外側ほど濃く、輪部の手前で細くなる。目頭・目尻と輪部の周りは桃色に霞む。
      vec3 bone = vec3(0.40, 0.385, 0.39);   // 黒ずんだ地(疲れた目)。黄味は無し。きれいな白は残さない
      // 血管 3 階層: 太い幹(蛇行)・枝・毛細血管。太さは場所で揺らす。線として見える太さを守る
      vec3 q = p + vec3(uSeed);
      // 両側(目頭・目尻)ほど濃く充血する(2026-09-06 ユーザー指示 ×2)。側 = 水平方向の外側
      float side = smoothstep(0.28, 0.9, abs(p.x));
      float thick = (0.7 + 0.6 * vnoise3(q * 5.0 + 2.0)) * (1.0 + 0.35 * side);
      // R65 追補(2026-09-07「白目の赤黒い血管を増やして」): 網を 3 → 5 層へ。
      // 幹は 2 本の別々の網を交差させ(1 本だと同じ流れに揃って櫛のように見える)、
      // 枝・毛細血管を足す。線の太さは保つ —— 細い層を増やすだけだと小さい画面で桃色の靄になる。
      float trunk = vein(fbm3(q * 3.0), 0.032 * thick);
      float trunk2 = vein(fbm3(q * 4.1 + 15.3), 0.026 * thick);
      float branch = vein(fbm3(q * 6.5 + 4.2), 0.023 * thick);
      float branch2 = vein(fbm3(q * 8.8 + 21.7), 0.019 * thick);
      float cap = vein(fbm3(q * 12.0 + 9.1), 0.016 * (1.0 + 0.3 * side));
      float exposure = 0.75 + 0.55 * side + 0.2 * smoothstep(0.7, 1.5, theta);
      // 輪部の際まで血管を走らせる(「白目のきれいな部分を無くして」2026-09-06)
      // 縁のわずか内側から充血を掛ける(縁で 0 だと地の骨色が 1px の白線として出る)
      float nearLimbus = smoothstep(uIris - 0.012, uIris + 0.03, theta);
      float veins = clamp(trunk * 1.05 + trunk2 * 0.95 + branch * 1.0 + branch2 * 0.85
                          + cap * (0.6 + 0.6 * side), 0.0, 1.0) * exposure * nearLimbus;
      // 隆起として扱うのは太い索だけ(毛細血管まで盛ると小さい画面で砂粒に見える)
      float cords = clamp(trunk * 1.0 + trunk2 * 0.85 + branch * 0.45, 0.0, 1.0) * nearLimbus;
      albedo = bone * (0.94 + 0.06 * vnoise(p.xy * 14.0 + uSeed));
      // 濁り(2026-09-06 指示「もう少し充血を濁らせて」): 低周波のまだらで赤みを不均一に、彩度を落とす
      float murk = fbm3(q * 2.2 + 3.7);
      albedo = mix(albedo, vec3(0.25, 0.115, 0.125), smoothstep(0.35, 0.7, murk) * 0.5);
      vec3 blood = vec3(0.155, 0.016, 0.022);  // より黒い暗紅(2026-09-07「赤黒い血管」)
      albedo = mix(albedo, blood, min(1.0, veins * 1.0));
      // 充血の滲み: 全面に濁った暗い赤、両側はさらに濃い。きれいな白はどこにも残さない(「もう少し黒く」)
      float flush = 0.55 + side * 0.35 + smoothstep(0.9, 1.6, theta) * 0.1;
      albedo = mix(albedo, vec3(0.31, 0.09, 0.10), min(0.9, flush) * nearLimbus);
      float shade = smoothstep(uIris + 0.28, uIris, theta);
      albedo *= 1.0 - shade * 0.34;
      // 輪部の環(limbal ring): 虹彩との境目は本物でも最も暗い。ここを赤黒く落として、
      // 骨色が線として残らないようにする(2026-09-07 ユーザー「境目が真っ白」)
      float limbalRing = 1.0 - smoothstep(uIris, uIris + 0.055, theta);
      albedo = mix(albedo, vec3(0.075, 0.017, 0.023), min(1.0, limbalRing * 0.95));
      // 瞼の影: 上下ほど暗く、垂れた上瞼の下は特に沈む(だるい目の陰)
      albedo *= 1.0 - smoothstep(0.2, 0.9, abs(p.y)) * 0.6;
      albedo *= 1.0 - smoothstep(-0.05, 0.45, p.y) * 0.42;
      // 2026-09-07 ユーザー「メタルチックだけど有機的に」: 形は枝分かれする血管のまま、
      // **隆起した稜線だけ**を滑らかにして鋭い光沢を返させる。地(粘膜)は鈍いまま。
      rough = mix(0.52, 0.20, cords);
      // 血管の「造形」: 濃さをそのまま高さとして扱い、画面微分から法線を曲げる
      // (接線を持たないバンプ = Mikkelsen 法)。追加のノイズ評価が要らないので実質ただ。
      bumpHeight = cords * 0.9 + murk * 0.08;
    }

    // ---- 照明(ビュー空間): 左上の暖色キー + 右下の朱のリム + フレネル
    vec3 N = normalize(vNormal);
    // 血管の隆起を法線へ。強膜だけ(虹彩側は bumpHeight = 0 なので何も起きない)
    if (bumpHeight > 0.0) {
      vec3 dpx = dFdx(vViewPos);
      vec3 dpy = dFdy(vViewPos);
      float dhx = dFdx(bumpHeight);
      float dhy = dFdy(bumpHeight);
      vec3 c1 = cross(dpy, N);
      vec3 c2 = cross(N, dpx);
      float det = dot(dpx, c1);
      vec3 grad = (c1 * dhx + c2 * dhy) / max(1e-7, abs(det));
      N = normalize(N - grad * 0.016);
    }
    // 虹彩は球面ではなく漏斗。径方向へ法線を傾けると、瞳の周りに落ち込みの陰影が出る
    if (irisSlope > 0.0) {
      vec3 axis = normalize(vAxis);
      vec3 radialV = N - axis * dot(N, axis);
      float rl = length(radialV);
      if (rl > 1e-4) N = normalize(N + (radialV / rl) * irisSlope);
    }
    vec3 V = normalize(-vViewPos);
    vec3 L1 = normalize(uKeyDir);
    vec3 L2 = normalize(uRimDir);
    float d1 = max(dot(N, L1), 0.0);
    float d2 = max(dot(N, L2), 0.0);
    vec3 H1 = normalize(L1 + V);
    // 眼球中央(虹彩・瞳)には光の反射を載せない(2026-09-06 指示)。鏡面は強膜だけ
    float s1 = pow(max(dot(N, H1), 0.0), mix(72.0, 18.0, rough)) * (1.0 - rough) * 0.22 * step(1.0, r);
    // 稜線の光沢は鋼のように少し冷たく、しかし血の色を帯びる(有機と金属の中間)
    vec3 specTint = mix(vec3(1.0), vec3(0.86, 0.70, 0.72), clamp(bumpHeight, 0.0, 1.0));
    s1 *= 1.0 + bumpHeight * 1.6;
    vec3 keyCol = vec3(1.0, 1.0, 1.0);
    vec3 rimCol = uTint * 0.9;
    // 朱のリムは**強膜だけ**に乗せる。黒目を暗くした分、同じ量でも虹彩が赤く染まって見える
    // (2026-09-07「黒目の中の赤い淵を無くして」の延長。輪では無く面の色として出ていた)
    float rimMask = mix(0.18, 1.0, step(1.0, r));
    vec3 color = albedo * (0.14 + 0.98 * d1 * keyCol + 0.32 * d2 * rimCol * rimMask)
      + s1 * specTint + uTint * emis * 0.7;
    float fres = pow(1.0 - max(dot(N, V), 0.0), 3.0);
    color += fres * 0.05 * rimCol * rimMask;
    // 瞼の直下の接触の影(オクルージョン): 縁に近いほど暗い。上瞼は厚く重いので濃く、下瞼は薄く
    float dU = -(dot(uUpperPlane.xyz, vWorld) + uUpperPlane.w);   // 見えている側で正、縁で 0
    float dL = -(dot(uLowerPlane.xyz, vWorld) + uLowerPlane.w);
    float occ = (1.0 - smoothstep(0.0, 0.26, dU)) * 0.62 + (1.0 - smoothstep(0.0, 0.12, dL)) * 0.34;
    color *= 1.0 - clamp(occ, 0.0, 0.8);
    gl_FragColor = vec4(color, 1.0);
    #include <tonemapping_fragment>
    #include <colorspace_fragment>
  }
`;

// ---------------------------------------------------------------- 構築

function supportsWebGL(win) {
  try {
    const probe = win.document.createElement("canvas");
    return Boolean(win.WebGLRenderingContext && (probe.getContext("webgl2") || probe.getContext("webgl")));
  } catch {
    return false;
  }
}

/** 小さなスタジオ。左上の大きな暖色ソフトボックス(角膜の catchlight)、右下の冷たいフィル、右の朱の帯。 */
function buildStudio(renderer) {
  const room = new THREE.Scene();
  const parts = [];
  const panel = (color, size, position, lookAt = [0, 0, 0]) => {
    const geometry = new THREE.PlaneGeometry(size[0], size[1]);
    const material = new THREE.MeshBasicMaterial({ color, side: THREE.DoubleSide });
    const mesh = new THREE.Mesh(geometry, material);
    mesh.position.set(position[0], position[1], position[2]);
    mesh.lookAt(lookAt[0], lookAt[1], lookAt[2]);
    room.add(mesh);
    parts.push(geometry, material);
  };
  const shellGeometry = new THREE.BoxGeometry(16, 16, 16);
  const shellMaterial = new THREE.MeshBasicMaterial({ color: 0x07070a, side: THREE.BackSide });
  room.add(new THREE.Mesh(shellGeometry, shellMaterial));
  parts.push(shellGeometry, shellMaterial);
  // 中立の白と灰だけ(暖色・朱の帯は黒鉄を茶色に見せるので置かない)
  panel(0xffffff, [5.5, 3.6], [-4.2, 4.8, 5.0]);
  panel(0x6a707a, [3.6, 2.0], [4.6, -3.6, 4.2]);
  panel(0x2a2c31, [6, 6], [0, 0, -7]);
  const pmrem = new THREE.PMREMGenerator(renderer);
  const target = pmrem.fromScene(room, 0.04);
  pmrem.dispose();
  parts.forEach((part) => part.dispose());
  return target.texture;
}

const NOOP = { attach() {}, detach() {}, setMode() {}, destroy() {}, available: false };

/**
 * 目を組み立てる。canvas は内部で 1 枚作り、attach(host) で host へ入れる。
 * WebGL が無い環境では何もしない NOOP を返す(SVG の目がそのまま残る)。
 */
export function initStandbyEye({ win = window, rand = Math.random } = {}) {
  if (!win?.document || !supportsWebGL(win)) return NOOP;
  const doc = win.document;
  const canvas = doc.createElement("canvas");
  canvas.className = "standby-eye3d";
  canvas.setAttribute("aria-hidden", "true");
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, powerPreference: "low-power" });
  } catch {
    return NOOP;
  }
  const reduced = win.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches === true;
  renderer.setPixelRatio(Math.min(win.devicePixelRatio || 1, 1.5));   // メモリ節約(WebView は落ちる)
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.setClearColor(0x000000, 0);

  const scene = new THREE.Scene();
  scene.environment = buildStudio(renderer);
  scene.environmentIntensity = 1.0;
  const camera = new THREE.PerspectiveCamera(28, 1, 0.1, 30);
  camera.position.set(0, 0, 6.15);
  camera.lookAt(0, 0, 0);

  // 光は中立の白(暖色のキーは黒鉄を茶色に見せる —— 「茶色を完全になくして」2026-09-06)。
  // だるい目: 光は高い所から弱く落ち、下は影に沈む(ローキー)
  const key = new THREE.DirectionalLight(0xffffff, 1.7);
  key.position.set(-2.4, 5.6, 3.4);
  scene.add(key);
  // 朱のリムはごく弱く(強いと瞼とベゼルが茶色に映る)
  const rim = new THREE.DirectionalLight(0xff3b4f, 0.3);
  rim.position.set(3.6, -2.2, 2.4);
  scene.add(rim);
  scene.add(new THREE.AmbientLight(0x2a2b30, 0.25));

  const disposables = [];
  const keep = (...items) => { disposables.push(...items); return items[0]; };

  // ---- 眼球(シェーダ)+ 角膜
  const eyeball = new THREE.Group();
  const tint = new THREE.Color(MODES.neutral.tint).convertSRGBToLinear();
  const eyeMat = keep(new THREE.ShaderMaterial({
    vertexShader: EYE_VERT,
    fragmentShader: EYE_FRAG,
    uniforms: {
      uTime: { value: 0 },
      uPupil: { value: MODES.neutral.pupil },
      uIris: { value: IRIS_ANGLE },
      uGlow: { value: MODES.neutral.glow },
      uTint: { value: tint },
      uKeyDir: { value: new THREE.Vector3(-0.38, 0.86, 0.5).normalize() },
      uRimDir: { value: new THREE.Vector3(0.6, -0.5, 0.35).normalize() },
      uSeed: { value: 4.7 },
      uUpperPlane: { value: new THREE.Vector4(0, 1, 0, -10) },
      uLowerPlane: { value: new THREE.Vector4(0, -1, 0, -10) },
      uViewLocal: { value: new THREE.Vector3(0, 0, 6.15) },
    },
  }));
  const eyeGeo = keep(new THREE.SphereGeometry(1, 96, 64));
  eyeball.add(new THREE.Mesh(eyeGeo, eyeMat));
  // 角膜(透明球 + 映り込み)は置かない: 「眼球中央の光の反射を無くして」(2026-09-06 指示)。
  // 一度置いた版は、白地だと瞳が灰色に濁り、黒地でも catchlight が中央に残った。
  scene.add(eyeball);

  // ---- 瞼 2 枚(削り出しの金属、縁に光る lip)+ 眼窩の暗幕 + ベゼル
  // 瞼とベゼル: 黒鉄。2026-09-06 に外枠を 2 段暗くした(0x15161a → 0x08090b、環境の映り込みも抑える)
  // 鈍い黒鉄(roughness 高め): 瞼のドームに白い反射の玉が乗らないように
  // 2026-09-07 ユーザー「黒い瞼に作って」: 瞼は**黒**。環境の映り込みと艶を落として、
  // ドーム状の白い照りを消す(色を黒くするだけでは、金属の反射で灰色に見える)。
  const metal = keep(new THREE.MeshPhysicalMaterial({
    color: 0x040405, metalness: 0.86, roughness: 0.66, clearcoat: 0.06, clearcoatRoughness: 0.6, envMapIntensity: 0.16,
  }));
  // 瞼の縁(厚みの断面)。黒の中で縁だけが線として残るよう、ここだけ僅かに明るい鉄
  const lip = keep(new THREE.MeshPhysicalMaterial({ color: 0x212327, metalness: 0.9, roughness: 0.42, clearcoat: 0.2, envMapIntensity: 0.42 }));
  // 瞼 = 球(眼球より少し大きい)を平面で切ったもの。まばたきは平面を動かすだけ(目尻は不動)。
  // 上瞼の球を少し大きくして、目尻で重なる所の z-fight を避ける(上瞼が上に被る)。
  renderer.localClippingEnabled = true;
  const CANTHUS = 67 * DEG;
  const Z_AXIS = new THREE.Vector3(0, 0, 1);
  // 瞼の厚み(2026-09-06 ユーザー指示): 殻を眼球から浮かせ、切り口を太い丸縁で塞ぐ。
  // 丸縁の外周は殻の面に触れ、内周は眼球の**中**(半径 < 1)まで食い込ませる —— 縁と眼球の間に
  // 隙間が出ると瞼が浮いて見える(ユーザー指摘「下瞼の軽微な浮き」)。
  // 上瞼は厚く(殻 1.09 / 縁 0.048)、下瞼は薄く(殻 1.06 / 縁 0.034)。下瞼は艶消し。
  const buildLid = (radius, tube, sign, finish) => {
    const plane = new THREE.Plane(new THREE.Vector3(0, sign, 0), 0);
    const material = keep(metal.clone());
    material.clippingPlanes = [plane];
    Object.assign(material, finish);
    const shell = keep(new THREE.SphereGeometry(radius, 96, 64));
    scene.add(new THREE.Mesh(shell, material));
    const ringGeo = keep(new THREE.TorusGeometry(1, tube / 1.04, 20, 192));
    const ringMat = keep(lip.clone());
    Object.assign(ringMat, finish);
    const ring = new THREE.Mesh(ringGeo, ringMat);
    scene.add(ring);
    return { plane, ring, ringRadius: radius - tube, sign, uniform: sign > 0 ? eyeMat.uniforms.uUpperPlane : eyeMat.uniforms.uLowerPlane };
  };
  const setLid = (lid, marginRad) => {
    const pl = lidPlane(marginRad, CANTHUS, lid.sign);
    lid.plane.normal.set(pl.normal.x, pl.normal.y, pl.normal.z);
    lid.plane.constant = pl.constant;
    lid.uniform.value.set(pl.normal.x, pl.normal.y, pl.normal.z, pl.constant);
    const cosH = Math.max(-1, Math.min(1, -pl.constant));
    const sinH = Math.sqrt(1 - cosH * cosH);
    lid.ring.quaternion.setFromUnitVectors(Z_AXIS, lid.plane.normal);
    lid.ring.position.copy(lid.plane.normal).multiplyScalar(lid.ringRadius * cosH);
    lid.ring.scale.setScalar(Math.max(1e-3, lid.ringRadius * sinH));
  };
  const upper = buildLid(1.09, 0.048, 1, { roughness: 0.66, clearcoat: 0.05, clearcoatRoughness: 0.6, envMapIntensity: 0.16 });
  const lower = buildLid(1.06, 0.034, -1, { roughness: 0.78, clearcoat: 0.0, envMapIntensity: 0.12 });
  // ---- R65 追補(2026-09-07: 瞼の配管)。
  // 経緯: 「有機的なパイプ」→ 蛇行する細管は黒い瞼に沈んで読めない → 「太くて格好の付く形」→
  // 等間隔のリブは目盛のようで面白みが無い → 「時間をかけて格好いい配管を」。
  //
  // 格好いい配管の条件を先に決めた:
  //   1. 階層 —— 太い幹 1 本が主役、細い支線は 1 本だけ。等間隔・等太さにしない。
  //   2. 目的地 —— 管は瞼の縁に沿って走り、**肘で折れて極へ向かい、ポートに刺さる**。
  //      浮いた弧ではなく「どこかへ繋がっている」形にする。
  //   3. 継手 —— 肘にはフランジ付きのカラー、末端には六角のボスとボルト。
  //      機械に見えるのは管ではなく継手。
  //   4. 素材差 —— 管は磨いた黒鋼、継手は一段明るい削り出し、朱の帯は主ポートに 1 本だけ
  //      (筐体の基準マークと同じ朱。多用しない)。
  //   5. 瞼と**同じ平面で切る**ので、まばたきで一緒に消える(瞼の一部として振る舞う)。
  // 座標は瞼の極(0, ±1, 0)からの角 a と方位 u(0.5π = 正面、小さいほど画面右)。
  const pipeMat = keep(new THREE.MeshPhysicalMaterial({
    color: 0x2a2d33, metalness: 0.95, roughness: 0.22, clearcoat: 0.5, clearcoatRoughness: 0.2, envMapIntensity: 1.0,
    side: THREE.DoubleSide,   // 瞼の平面で切られても筒の内壁が見える(抜けて眼球が透けない)
  }));
  const fittingMat = keep(new THREE.MeshPhysicalMaterial({
    color: 0x4a4e57, metalness: 0.96, roughness: 0.28, envMapIntensity: 1.1, side: THREE.DoubleSide,
  }));
  const accentMat = keep(new THREE.MeshStandardMaterial({
    color: 0x2a1014, emissive: 0x8f2230, emissiveIntensity: 0.9, roughness: 0.5, metalness: 0.3,
  }));
  const PIPE_TRUNK_R = 0.072;
  const PIPE_BRANCH_R = 0.042;
  const accentMats = [];   // 瞼ごとの朱の帯(clone)。場面に合わせて毎フレーム明るさを変える

  function lidPipes(lid, shellRadius, layout) {
    const pole = new THREE.Vector3(0, lid.sign, 0);
    const e1 = new THREE.Vector3(1, 0, 0);
    const e2 = new THREE.Vector3(0, 0, 1);
    const clip = (m) => { const c = keep(m.clone()); c.clippingPlanes = [lid.plane]; return c; };
    const pipe = clip(pipeMat);
    const fitting = clip(fittingMat);
    const accent = clip(accentMat);
    accentMats.push(accent);
    const at = (a, u, lift) => new THREE.Vector3()
      .addScaledVector(pole, Math.cos(a))
      .addScaledVector(e1, Math.sin(a) * Math.cos(u))
      .addScaledVector(e2, Math.sin(a) * Math.sin(u))
      .multiplyScalar(shellRadius + lift);
    // (a, u) の経路を球面上で補間して管にする。肘は経路点の折れとして自然に出る
    const run = (way, radius) => {
      const pts = [];
      for (let i = 0; i < way.length - 1; i += 1) {
        const [a0, u0] = way[i];
        const [a1, u1] = way[i + 1];
        const n = 10;
        for (let k = 0; k < n; k += 1) {
          const t = k / n;
          pts.push(at(a0 + (a1 - a0) * t, u0 + (u1 - u0) * t, radius * 0.7));
        }
      }
      const last = way[way.length - 1];
      pts.push(at(last[0], last[1], radius * 0.7));
      const curve = new THREE.CatmullRomCurve3(pts, false, "catmullrom", 0.35);
      scene.add(new THREE.Mesh(keep(new THREE.TubeGeometry(curve, 96, radius, 10, false)), pipe));
      return curve;
    };
    // 継手: 管の周りのフランジ(環 + 短い筒)。axis = 管の接線
    const collar = (pos, axis, r) => {
      const q = new THREE.Quaternion().setFromUnitVectors(Z_AXIS, axis.clone().normalize());
      const ring = new THREE.Mesh(keep(new THREE.TorusGeometry(r * 1.35, r * 0.42, 10, 24)), fitting);
      ring.position.copy(pos); ring.quaternion.copy(q);
      scene.add(ring);
      const sleeve = new THREE.Mesh(keep(new THREE.CylinderGeometry(r * 1.22, r * 1.22, r * 1.9, 18)), fitting);
      sleeve.position.copy(pos); sleeve.quaternion.copy(q).multiply(new THREE.Quaternion().setFromAxisAngle(e1, Math.PI / 2));
      scene.add(sleeve);
    };
    // ポート: 瞼の面から立つ六角のボス + 上のボルト。axis = 面の法線
    const port = (pos, r, withAccent) => {
      const nrm = pos.clone().normalize();
      const q = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 1, 0), nrm);
      const boss = new THREE.Mesh(keep(new THREE.CylinderGeometry(r * 1.7, r * 1.85, r * 1.5, 6)), fitting);
      boss.position.copy(pos).addScaledVector(nrm, r * 0.2); boss.quaternion.copy(q);
      scene.add(boss);
      const bolt = new THREE.Mesh(keep(new THREE.CylinderGeometry(r * 0.75, r * 0.75, r * 0.7, 6)), pipe);
      bolt.position.copy(pos).addScaledVector(nrm, r * 1.25); bolt.quaternion.copy(q);
      scene.add(bolt);
      // フランジ止めの小ボルト 3 本(120° 刻み)。継手は留め具で「本物」になる
      const tx = new THREE.Vector3(1, 0, 0).cross(nrm).normalize();
      const ty = new THREE.Vector3().crossVectors(nrm, tx).normalize();
      for (let i = 0; i < 3; i += 1) {
        const ang = i * (Math.PI * 2 / 3) + 0.4;
        const small = new THREE.Mesh(keep(new THREE.CylinderGeometry(r * 0.22, r * 0.22, r * 0.34, 6)), pipe);
        small.position.copy(pos).addScaledVector(nrm, r * 1.05)
          .addScaledVector(tx, Math.cos(ang) * r * 1.35).addScaledVector(ty, Math.sin(ang) * r * 1.35);
        small.quaternion.copy(q);
        scene.add(small);
      }
      if (withAccent) {
        const band = new THREE.Mesh(keep(new THREE.TorusGeometry(r * 1.62, r * 0.16, 8, 24)), accent);
        band.position.copy(pos).addScaledVector(nrm, r * 0.95);
        band.quaternion.copy(q).multiply(new THREE.Quaternion().setFromAxisAngle(e1, Math.PI / 2));
        scene.add(band);
      }
    };

    // 蛇腹: 幹の途中(t0〜t1)に細い環を等間隔で巻く。可撓管の見た目 = 「工業製品」の記号
    const bellows = (curve, r, t0, t1, step) => {
      const list = [];
      for (let t = t0; t <= t1; t += step) list.push(t);
      const geo = keep(new THREE.TorusGeometry(r * 1.10, r * 0.15, 8, 20));
      const mesh = keep(new THREE.InstancedMesh(geo, fitting, list.length));
      const m4 = new THREE.Matrix4();
      const q = new THREE.Quaternion();
      list.forEach((t, i) => {
        q.setFromUnitVectors(Z_AXIS, curve.getTangentAt(t).normalize());
        m4.compose(curve.getPointAt(t), q, new THREE.Vector3(1, 1, 1));
        mesh.setMatrixAt(i, m4);
      });
      mesh.instanceMatrix.needsUpdate = true;
      scene.add(mesh);
    };
    // 並走ケーブル: 幹に沿って面の上を横へずらした細い線。2 本並ぶと「配線された」感じになる
    const twin = (curve, r, offset, t0, t1) => {
      const pts = [];
      for (let k = 0; k <= 40; k += 1) {
        const t = t0 + (t1 - t0) * (k / 40);
        const pos = curve.getPointAt(t);
        const nrm = pos.clone().normalize();
        const side = new THREE.Vector3().crossVectors(curve.getTangentAt(t), nrm).normalize();
        pts.push(pos.clone().addScaledVector(side, offset).addScaledVector(nrm, -r * 0.35));
      }
      const c = new THREE.CatmullRomCurve3(pts, false, "catmullrom", 0.3);
      scene.add(new THREE.Mesh(keep(new THREE.TubeGeometry(c, 60, r, 8, false)), pipe));
      return c;
    };
    // ジャンクションブロック: 支線が幹から出る所は、環ではなく角張ったブロックで受ける
    const junction = (pos, tangent, r) => {
      const nrm = pos.clone().normalize();
      const side = new THREE.Vector3().crossVectors(tangent, nrm).normalize();
      const basis = new THREE.Matrix4().makeBasis(tangent.clone().normalize(), nrm, side);
      const q = new THREE.Quaternion().setFromRotationMatrix(basis);
      const block = new THREE.Mesh(keep(new THREE.BoxGeometry(r * 2.8, r * 1.9, r * 2.4)), fitting);
      block.position.copy(pos).addScaledVector(nrm, r * 0.15); block.quaternion.copy(q);
      scene.add(block);
      // ブロックの上面に小さなボルト 2 本
      [-0.85, 0.85].forEach((o) => {
        const bolt = new THREE.Mesh(keep(new THREE.CylinderGeometry(r * 0.28, r * 0.28, r * 0.3, 6)), pipe);
        bolt.position.copy(pos).addScaledVector(nrm, r * 1.15).addScaledVector(tangent.clone().normalize(), o * r);
        bolt.quaternion.copy(new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 1, 0), nrm));
        scene.add(bolt);
      });
    };

    // 幹: 目尻側から縁に沿って走り、内側で肘を折って極へ上がり、主ポートへ
    const trunk = run(layout.trunk, PIPE_TRUNK_R);
    layout.trunkCollars.forEach((t) => collar(trunk.getPointAt(t), trunk.getTangentAt(t), PIPE_TRUNK_R));
    bellows(trunk, PIPE_TRUNK_R, layout.trunkCollars[0] + 0.06, layout.trunkCollars[1] - 0.06, 0.032);
    twin(trunk, PIPE_TRUNK_R * 0.30, PIPE_TRUNK_R * 1.75, 0.02, 0.70);
    port(trunk.getPointAt(1), PIPE_TRUNK_R, true);
    // 支線: 幹の途中から分かれて、反対側の小ポートへ。出口はジャンクションブロック
    const branch = run(layout.branch, PIPE_BRANCH_R);
    junction(branch.getPointAt(0.03), branch.getTangentAt(0.03), PIPE_TRUNK_R);
    port(branch.getPointAt(1), PIPE_BRANCH_R, false);
  }
  const PI = Math.PI;
  // 管の緯度は **瞼が最も開いたときの縁より内側** に置く(2026-09-07「黒目がパイプの上に浮く」の
  // 修正)。上瞼は目を上げると縁が中心の 36° 上(極から 0.94rad)まで上がり、下瞼は 33.5° 下
  // (0.98rad)まで下がる。管の半径 0.07 を引いて、幹の最も縁寄りの点は 0.90 以内に収める。
  // 上瞼: 幹は右の目尻(u 小)から左へ、左で肘を折って上(極)へ。支線は中央から右上へ
  lidPipes(upper, 1.09, {
    trunk: [[0.94, 0.20 * PI], [0.88, 0.34 * PI], [0.86, 0.52 * PI], [0.88, 0.68 * PI], [0.74, 0.76 * PI], [0.54, 0.72 * PI]],
    trunkCollars: [0.10, 0.62],
    branch: [[0.86, 0.50 * PI], [0.74, 0.40 * PI], [0.58, 0.31 * PI]],
  });
  // 下瞼: 鏡写しにせず、幹は左から右へ流れて右で下(極)へ折れる
  lidPipes(lower, 1.06, {
    trunk: [[0.92, 0.80 * PI], [0.86, 0.64 * PI], [0.84, 0.46 * PI], [0.82, 0.30 * PI], [0.66, 0.24 * PI]],
    trunkCollars: [0.12, 0.66],
    branch: [[0.84, 0.44 * PI], [0.72, 0.56 * PI], [0.62, 0.62 * PI]],
  });

  // 垂れた上瞼(縁は中心の 11° 上 = 瞳の上を隠す半眼)と、下がった下瞼(28° 下 = 赤い白目が見える)。
  // 閉じ切ると両方の縁が中心の 22° 下で合う(LID_OPEN / LID_SHUT)。開き具合は視線に連動(lidOpening)。
  const socketGeo = keep(new THREE.CircleGeometry(1.16, 64));
  const socketMat = keep(new THREE.MeshStandardMaterial({ color: 0x0a0a0c, roughness: 0.9, metalness: 0.2 }));
  const socket = new THREE.Mesh(socketGeo, socketMat);
  socket.position.z = -0.35;
  scene.add(socket);
  const bezelGeo = keep(new THREE.TorusGeometry(1.2, 0.05, 24, 160));
  const bezelMat = keep(new THREE.MeshPhysicalMaterial({
    color: 0x0c0d10, metalness: 0.92, roughness: 0.3, clearcoat: 0.7, clearcoatRoughness: 0.18, envMapIntensity: 0.8,
  }));
  const bezel = new THREE.Mesh(bezelGeo, bezelMat);
  bezel.position.z = 0.1;
  scene.add(bezel);
  const innerGeo = keep(new THREE.TorusGeometry(1.125, 0.012, 12, 160));
  const inner = new THREE.Mesh(innerGeo, lip);
  inner.position.z = 0.16;
  scene.add(inner);

  // ---- 回転する外枠: 旧 SVG の環・目盛・七芒星を黒鉄のワイヤーで組む(背面の平面は消す)
  const orbit = new THREE.Group();   // ※ frame は描画ループの関数名なので別名
  orbit.position.z = -0.08;
  const frameMetal = keep(new THREE.MeshPhysicalMaterial({ color: 0x1a1b20, metalness: 0.9, roughness: 0.42, envMapIntensity: 0.6 }));
  const tickMat = keep(new THREE.MeshStandardMaterial({ color: 0x2a1014, emissive: 0x8f2230, emissiveIntensity: 0.5, roughness: 0.6, metalness: 0.3 }));
  const outerGeo = keep(new THREE.TorusGeometry(FRAME.outer, 0.012, 10, 220));
  orbit.add(new THREE.Mesh(outerGeo, frameMetal));
  const tickGeo = keep(new THREE.BoxGeometry(0.012, 1, 0.012));
  for (let deg = 0; deg < 360; deg += FRAME.tickStepDeg) {
    const a = deg * DEG;
    const len = deg % 45 === 0 ? FRAME.tickMajor : FRAME.tickMinor;
    const tick = new THREE.Mesh(tickGeo, tickMat);
    tick.scale.y = len;
    const r = FRAME.tickOuter - len / 2;
    tick.position.set(Math.cos(a) * r, Math.sin(a) * r, 0);
    tick.rotation.z = a - Math.PI / 2;
    orbit.add(tick);
  }
  // 七芒星は環と逆回り(R65)。別の群に入れて、回転だけを反対向きに与える
  const starRing = new THREE.Group();
  starRing.position.z = -0.08;
  const starPts = heptagramPoints().map((p) => new THREE.Vector3(p.x, p.y, p.z));
  const strutGeo = keep(new THREE.CylinderGeometry(0.007, 0.007, 1, 6));
  // 七芒星は旧 SVG と同じく淡く(黒鉄だと背景に沈んで見えない —— 2026-09-06 実測)
  const strutMat = keep(new THREE.MeshStandardMaterial({ color: 0x3a1a20, emissive: 0x6e1c28, emissiveIntensity: 0.35, roughness: 0.6, metalness: 0.3 }));
  const Y_AXIS = new THREE.Vector3(0, 1, 0);
  for (let i = 0; i < starPts.length; i += 1) {
    const a = starPts[i];
    const b = starPts[(i + 1) % starPts.length];
    const dir = new THREE.Vector3().subVectors(b, a);
    const strut = new THREE.Mesh(strutGeo, strutMat);
    strut.scale.y = dir.length();
    strut.position.copy(a).add(b).multiplyScalar(0.5);
    strut.quaternion.setFromUnitVectors(Y_AXIS, dir.normalize());
    starRing.add(strut);
  }
  scene.add(orbit);
  scene.add(starRing);

  // ---- R65: 計器の筐体(2026-09-07 ユーザー「目の周りの構造物をもっと凝って設計して」)。
  // 要点は **動く物と動かない物を分ける** こと —— 回るのは外枠と副尺だけで、筐体(バレル・
  // 4 つの受け金具・リベット・基準マーク)は据え付けで動かない。目が宙に浮かず、枠へ
  // 取り付けられた計器に見える。全て黒鉄(既存の材質を使い回す。新しい色は足さない)。
  const housing = new THREE.Group();
  housing.position.z = -0.02;

  // バレル: 目の後ろへ伸びる筒。内壁だけを見せて眼窩の奥行きを作る(平面の socket の手前)
  const barrelGeo = keep(new THREE.CylinderGeometry(FRAME.barrel, FRAME.barrel, FRAME.barrelDepth, 96, 1, true));
  const barrelMat = keep(new THREE.MeshStandardMaterial({
    color: 0x121317, roughness: 0.78, metalness: 0.55, side: THREE.BackSide,
  }));
  const barrel = new THREE.Mesh(barrelGeo, barrelMat);
  barrel.rotation.x = Math.PI / 2;
  barrel.position.z = -0.42;
  housing.add(barrel);

  // カラー: 4 分割の重い環。切れ目は受け金具の位置(45°/135°/225°/315°)に来る
  const collarGeo = keep(new THREE.TorusGeometry(
    FRAME.collar, FRAME.collarTube, 14, 72, FRAME.collarArcDeg * DEG,
  ));
  for (let i = 0; i < 4; i += 1) {
    const arc = new THREE.Mesh(collarGeo, bezelMat);
    arc.rotation.z = (i * 90 - FRAME.collarArcDeg / 2) * DEG;
    arc.position.z = 0.02;
    housing.add(arc);
  }

  // 受け金具とリベット: ベゼル(1.2)からカラー(1.45)へ橋を架け、頭を留める
  const bracketGeo = keep(new THREE.BoxGeometry(0.085, FRAME.bracketLen, 0.085));
  const boltGeo = keep(new THREE.CylinderGeometry(0.034, 0.034, 0.055, 12));
  for (let i = 0; i < 4; i += 1) {
    const a = (45 + i * 90) * DEG;
    const arm = new THREE.Mesh(bracketGeo, bezelMat);
    arm.position.set(Math.cos(a) * FRAME.bracket, Math.sin(a) * FRAME.bracket, 0.04);
    arm.rotation.z = a - Math.PI / 2;
    housing.add(arm);
    const bolt = new THREE.Mesh(boltGeo, lip);
    bolt.position.set(Math.cos(a) * FRAME.bolt, Math.sin(a) * FRAME.bolt, 0.10);
    bolt.rotation.x = Math.PI / 2;
    housing.add(bolt);
  }

  // 基準マーク: 12 時の朱の楔(計器のゼロ点)。場面の残り火に合わせて明るさだけ変わる
  const indexMat = keep(new THREE.MeshStandardMaterial({
    color: 0x2a1014, emissive: 0x8f2230, emissiveIntensity: 0.4, roughness: 0.55, metalness: 0.35,
  }));
  const indexGeo = keep(new THREE.ConeGeometry(0.05, 0.11, 3));
  const indexMark = new THREE.Mesh(indexGeo, indexMat);
  indexMark.position.set(0, FRAME.index, 0.08);
  indexMark.rotation.z = Math.PI;      // 頂点は中心を指す
  housing.add(indexMark);
  scene.add(housing);

  // 副尺: 外枠と**逆向き・別周期**で回る微目盛(1 本の InstancedMesh = 描画 1 回)
  const vernier = new THREE.Group();
  vernier.position.z = -0.02;
  vernier.add(new THREE.Mesh(keep(new THREE.TorusGeometry(FRAME.vernier - FRAME.vernierTick / 2, 0.005, 8, 140)), frameMetal));
  const vernierCount = Math.round(360 / FRAME.vernierStepDeg);
  const vernierTicks = keep(new THREE.InstancedMesh(
    keep(new THREE.BoxGeometry(0.008, FRAME.vernierTick, 0.008)), frameMetal, vernierCount,
  ));
  {
    const matrix = new THREE.Matrix4();
    const quat = new THREE.Quaternion();
    const pos = new THREE.Vector3();
    const one = new THREE.Vector3(1, 1, 1);
    for (let i = 0; i < vernierCount; i += 1) {
      const a = i * FRAME.vernierStepDeg * DEG;
      pos.set(Math.cos(a) * FRAME.vernier, Math.sin(a) * FRAME.vernier, 0);
      quat.setFromAxisAngle(Z_AXIS, a - Math.PI / 2);
      vernierTicks.setMatrixAt(i, matrix.compose(pos, quat, one));
    }
    vernierTicks.instanceMatrix.needsUpdate = true;
  }
  vernier.add(vernierTicks);
  scene.add(vernier);

  // ---- ブルーム(虹彩の光と catchlight)。失敗すれば素の描画に落ちる
  let composer = null;
  try {
    composer = new EffectComposer(renderer);
    composer.addPass(new RenderPass(scene, camera));
    composer.addPass(new EffectPass(camera, new BloomEffect({ intensity: 0.3, luminanceThreshold: 0.7, luminanceSmoothing: 0.25, mipmapBlur: true })));
  } catch {
    composer = null;
  }

  // ---- 生きている手掛かり
  const viewLocalMatrix = new THREE.Matrix4();   // 毎フレームの逆行列を使い回す(確保しない)
  const gazeX = { x: 0, v: 0 };
  const gazeY = { x: 0, v: 0 };
  let target = { yaw: 0, pitch: SACCADE.pitchBias };
  let saccade = planSaccade(rand, 0);
  let blink = planBlink(rand, 1200);
  let blinkStart = -1;
  let blinkSecond = false;
  let droop = planDroop(rand, 2500);
  let droopTarget = 0;
  let droopHoldUntil = -1;
  const droopState = { x: 0, v: 0 };
  let pointerUntil = 0;
  let mode = MODES.neutral;
  let glow = { x: mode.glow, v: 0 };
  let lastT = 0;
  let raf = 0;
  let host = null;
  let visible = true;
  let pageVisible = doc.visibilityState !== "hidden";
  let destroyed = false;

  function resize() {
    if (!host) return;
    const w = Math.max(48, host.clientWidth || 136);
    const h = Math.max(48, host.clientHeight || w);
    renderer.setSize(w, h, false);
    composer?.setSize(w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }

  function draw() {
    if (composer) composer.render();
    else renderer.render(scene, camera);
  }

  function applyLids(amount, pitch = 0) {
    // 開いた位置(視線で変わる)から、閉じ量 a で両縁を 22° 下へ寄せる。1.0 で合わさる(閉眼)。
    // 目尻は動かないので、輪郭が浮かない。
    const a = Math.max(0, Math.min(1, amount));
    const open = lidOpening(pitch);
    setLid(upper, open.upper - a * (open.upper + LID_SHUT));
    setLid(lower, open.lower - a * (open.lower - LID_SHUT));
  }

  function frame(nowMs) {
    raf = 0;
    if (destroyed || !host || !visible || !pageVisible) return;
    const t = nowMs / 1000;
    const dt = Math.min(0.05, lastT ? t - lastT : 0.016);
    lastT = t;

    if (!reduced) {
      if (nowMs >= pointerUntil && nowMs >= saccade.at) {
        saccade = planSaccade(rand, nowMs, mode.saccade);
        target = { yaw: saccade.yaw, pitch: saccade.pitch };
      }
      // 微小なドリフト(固視微動)。ばねは柔らかく、視線は漂う
      const drift = 0.006;
      stepSpring(gazeX, target.yaw + Math.sin(t * 1.7) * drift, dt, SACCADE.omega, 1);
      stepSpring(gazeY, target.pitch + Math.cos(t * 1.3) * drift, dt, SACCADE.omega, 1);
      // まばたき(重く遅い)
      let blinkAmt = 0;
      if (blinkStart < 0 && nowMs >= blink.at) { blinkStart = nowMs; blinkSecond = blink.double; }
      if (blinkStart >= 0) {
        const elapsed = nowMs - blinkStart;
        blinkAmt = blinkAmount(elapsed);
        if (elapsed >= BLINK_CLOSE_MS + BLINK_OPEN_MS) {
          blinkStart = -1;
          blink = blinkSecond ? { at: nowMs + 220, double: false } : planBlink(rand, nowMs);
        }
      }
      // 瞼の重さ(常に少し落ち、時々深く落ちて留まる)
      if (droopHoldUntil < 0 && nowMs >= droop.at) { droopTarget = droop.depth; droopHoldUntil = nowMs + droop.holdMs; }
      if (droopHoldUntil >= 0 && nowMs >= droopHoldUntil) { droopTarget = 0; droopHoldUntil = -1; droop = planDroop(rand, nowMs); }
      stepSpring(droopState, droopTarget, dt, DROOP.omega, 1);
      // 上を見るほど重さが抜ける(alert)。まばたきはそのまま
      const heavy = (droopBaseline(t) + droopState.x)
        * (1 - 0.85 * lidOpening(gazeY.x).alert) * (mode.lid ?? 1);
      applyLids(Math.max(blinkAmt, Math.min(0.9, heavy)), gazeY.x);
      stepSpring(glow, mode.glow * (0.92 + 0.08 * Math.sin(t * 1.35)), dt, 4, 1);
      eyeMat.uniforms.uPupil.value = pupilAngle(mode, t);
    } else {
      gazeX.x = 0; gazeY.x = SACCADE.pitchBias;
      glow.x = mode.glow;
      eyeMat.uniforms.uPupil.value = mode.pupil;
      applyLids((DROOP.base + DROOP.wobble * 0.5) * (mode.lid ?? 1), gazeY.x);
    }
    eyeball.rotation.set(-gazeY.x, gazeX.x, 0);
    // 瞳の視差(穴の奥行き)は「眼球から見たカメラの向き」で決まる。視線が動くたびに更新する
    eyeball.updateMatrixWorld(true);
    eyeMat.uniforms.uViewLocal.value.copy(camera.position)
      .applyMatrix4(viewLocalMatrix.copy(eyeball.matrixWorld).invert());
    // 外枠: ゆっくり回りながら、視線に少しだけ付いてくる(眼球と一体の器具として)
    orbit.rotation.set(-gazeY.x * FRAME.follow, gazeX.x * FRAME.follow, reduced ? 0 : (t * Math.PI * 2) / FRAME.spinSeconds);
    // R65: 七芒星は環と逆回り(周期も別)。視線への追従は環と揃える
    starRing.rotation.set(-gazeY.x * FRAME.follow, gazeX.x * FRAME.follow,
      reduced ? 0 : -(t * Math.PI * 2) / FRAME.starSeconds);
    // R65: 副尺も逆向き・別周期で、視線への追従は半分(筐体は据え付けなので動かさない)
    vernier.rotation.set(-gazeY.x * FRAME.follow * 0.5, gazeX.x * FRAME.follow * 0.5,
      reduced ? 0 : -(t * Math.PI * 2) / FRAME.vernierSeconds);
    indexMat.emissiveIntensity = 0.3 + glow.x * 2.2;   // 基準マークは残り火に連動
    // 配管の朱の帯: 場面ごとの拍で光る(bloom が乗るので明るいほど滲む)
    {
      const level = reduced ? mode.accent.base : accentIntensity(mode, t);
      for (let i = 0; i < accentMats.length; i += 1) accentMats[i].emissiveIntensity = level;
    }
    eyeMat.uniforms.uTime.value = t;
    eyeMat.uniforms.uGlow.value = glow.x;
    draw();
    if (!reduced) raf = win.requestAnimationFrame(frame);
  }

  function kick() {
    if (destroyed || raf || !host || !visible || !pageVisible) return;
    lastT = 0;
    raf = win.requestAnimationFrame(frame);
  }

  const observer = typeof win.IntersectionObserver === "function"
    ? new win.IntersectionObserver((entries) => {
      visible = entries.some((entry) => entry.isIntersecting);
      if (visible) kick();
    }, { threshold: 0.01 })
    : null;
  doc.addEventListener("visibilitychange", () => {
    pageVisible = doc.visibilityState !== "hidden";
    if (pageVisible) kick();
  });
  if (!reduced) {
    win.addEventListener("pointermove", (event) => {
      if (!host) return;
      const rect = canvas.getBoundingClientRect();
      if (!rect.width) return;
      // 目から遠いポインタは追わない(画面の反対側までは首を回さない)
      const dx = event.clientX - (rect.left + rect.width / 2);
      const dy = event.clientY - (rect.top + rect.height / 2);
      if (Math.hypot(dx, dy) > rect.width * 2.2) return;
      target = gazeFromPointer(event.clientX, event.clientY, rect);
      pointerUntil = performance.now() + 2500;
      saccade.at = pointerUntil;
    }, { passive: true });
  }

  return {
    available: true,
    /** host(.standby / .pet-eye)へ目を入れる。null なら外す。同じ host なら何もしない。 */
    attach(next) {
      if (destroyed) return;
      if (!next) { this.detach(); return; }
      if (next === host && canvas.parentNode === next) { kick(); return; }
      // R65: 目は居場所を移る(カード ⇄ 駐在所)。置いていく側の印は必ず外す ——
      // 残ると「3D が入っている」ことになって、駐在所に空の丸が残る。
      if (host && host !== next) host.classList.remove("has-3d");
      host = next;
      host.appendChild(canvas);
      host.classList.add("has-3d");
      observer?.disconnect();
      observer?.observe(canvas);
      visible = true;
      resize();
      applyLids(DROOP.base, gazeY.x);
      if (reduced) { frame(performance.now()); return; }
      kick();
    },
    detach() {
      if (!host && !canvas.parentNode) return;   // 毎秒呼ばれても何も起こさない(R65)
      if (host) host.classList.remove("has-3d");
      host = null;
      observer?.disconnect();
      if (raf) { win.cancelAnimationFrame(raf); raf = 0; }
      if (canvas.parentNode) canvas.parentNode.removeChild(canvas);
    },
    /** 場面: neutral / long / short / long-armed / short-armed。武装で瞳が締まり光が強くなる。 */
    setMode(name) {
      const next = resolveMode(name);
      if (next === mode) return;
      mode = next;
      eyeMat.uniforms.uTint.value.set(mode.tint).convertSRGBToLinear();
      if (!reduced && blinkStart < 0 && host) blink = { at: performance.now() + 80, double: false };
      if (reduced && host) frame(performance.now());
    },
    destroy() {
      destroyed = true;
      this.detach();
      disposables.forEach((item) => item.dispose?.());
      composer?.dispose?.();
      renderer.dispose();
    },
  };
}
