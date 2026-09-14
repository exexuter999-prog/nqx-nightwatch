/**
 * 氷チャートの 3D レンダラ(R61, 2026-09-06 作り直し)。
 *
 * 旧版は八角柱を 1 秒で回す半透明の板で、実機では「灰色の四角が明滅している」ようにしか
 * 見えなかった(ユーザー: 氷のチャートを作り直して)。氷は回さなくても氷に見える必要がある。
 *
 * 氷の読み取りは 4 つで作る:
 *   1. **縁が光る**(フレネル): 正面は透け、稜線と側面ほど白く濁る。
 *   2. **中に霜と亀裂がある**: 3D ノイズの霜(乳白)と、ノイズ等高線の細い亀裂(白い筋)。
 *      座標は画面座標なので、揺れても霜は塊の中に留まる。
 *   3. **上面に霜が乗る**: 上を向く面ほど白い。
 *   4. **鋭いグリント**: 2 灯の Blinn-Phong(指数 90 / 40)+ 稜線のきらめき(Points)。
 * 塊は角を落とした箱(RoundedBoxGeometry)を斜め(上と右側面が見える固定角)に置き、
 * ±6° の遅い揺れと 1px の浮き沈みだけを付ける。reduced-motion では静止。
 *
 * カメラは **ピクセル空間の正射影**。chart.js が price→px を済ませた矩形をそのまま渡すので、
 * 3D 側は価格を一切知らないし、SVG のグリッドと 1px もずれない。屈折(transmission)は
 * 使えない(WebGL の透過はシーン内の背後しか見ない。下の DOM チャートは屈折対象にならない)
 * ので、下のチャートはアルファ合成で透かす。
 *
 * WebGL が無い環境では何もしない(呼び出し側が SVG の氷へ落ちる)。
 */

import * as THREE from "three";
import { RoundedBoxGeometry } from "three/examples/jsm/geometries/RoundedBoxGeometry.js";
// icecandles.js(main チャンク)からではなく単独モジュールから読む: 遅延チャンクが起動チャンクを
// 抱えると verify:build の遅延予算が 957 kB で赤になる(2026-09-06 に発見)。
import { wickSegments } from "./wicks.js";

const MAX_BLOCKS = 24;
const GLINTS_PER_BLOCK = 3;
/** 固定の見込み角: 上面と右側面が見える。 */
const POSE = { tiltX: -0.22, yawY: 0.42 };
/** 遅い揺れ(rad)と浮き沈み(px)。 */
const SWAY = { yaw: 0.10, tilt: 0.04, bobPx: 1.2, periodSec: 6.5 };

function supportsWebGL() {
  try {
    const probe = document.createElement("canvas");
    return Boolean(window.WebGLRenderingContext
      && (probe.getContext("webgl2") || probe.getContext("webgl")));
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------- 氷のシェーダ

const ICE_VERT = /* glsl */`
  varying vec3 vObj;
  varying vec3 vWorld;
  varying vec3 vN;
  void main() {
    vObj = position;
    vWorld = (modelMatrix * vec4(position, 1.0)).xyz;
    vN = normalize(normalMatrix * normal);
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`;

const ICE_FRAG = /* glsl */`
  precision highp float;
  uniform vec3 uTint;
  uniform float uOpacity;
  uniform float uPivot;
  uniform float uTime;
  uniform vec3 uKey;
  uniform vec3 uRim;
  uniform float uSeed;
  varying vec3 vObj;
  varying vec3 vWorld;
  varying vec3 vN;

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
  float ridge(float n, float w) { return 1.0 - smoothstep(0.0, w, abs(n - 0.5)); }

  void main() {
    vec3 N = normalize(vN);
    // 正射影なので視線はどこでも +z(ビュー空間)
    vec3 V = vec3(0.0, 0.0, 1.0);
    float ndv = max(dot(N, V), 0.0);
    float fres = pow(1.0 - ndv, 2.4);

    // 霜と亀裂(画面座標 px → 8〜14px の模様)。塊が揺れても模様は中に留まる
    vec3 w = vWorld * 0.085 + uSeed;
    float frost = fbm3(w);
    float crack = ridge(fbm3(w * 0.55 + 7.3), 0.035) * 0.65 + ridge(fbm3(w * 1.3 + 3.1), 0.022) * 0.45;
    float bubbles = smoothstep(0.62, 0.7, vnoise3(w * 2.6 + 1.7)) * 0.5;

    vec3 iceBase = mix(vec3(0.70, 0.86, 0.96), uTint, 0.20);
    vec3 col = iceBase * (0.30 + 0.70 * fres);
    col += vec3(0.92, 0.97, 1.0) * frost * frost * 0.55;      // 乳白の芯
    col += vec3(1.0) * crack * 0.75;                          // 白い亀裂
    col += vec3(0.85, 0.95, 1.0) * bubbles * 0.35;            // 気泡
    col += vec3(1.0) * smoothstep(0.55, 0.95, N.y) * 0.28;    // 上面の霜
    // 方向の色は底に沈める(色の付いた水の上に氷が乗っているように)
    col = mix(col, uTint, (1.0 - smoothstep(-0.5, 0.35, vObj.y)) * 0.28);

    // グリント: 鋭いキー + 冷たいリム
    float s1 = pow(max(dot(N, normalize(uKey + V)), 0.0), 90.0);
    float s2 = pow(max(dot(N, normalize(uRim + V)), 0.0), 40.0) * 0.6;
    col += vec3(1.0) * s1 * 1.5 + vec3(0.6, 0.85, 1.0) * s2;
    // 建値の塊は内側から白く息をする
    col += vec3(0.9, 0.97, 1.0) * uPivot * (0.10 + 0.08 * sin(uTime * 2.1));

    float alpha = uOpacity * (0.30 + 0.70 * fres)
      + frost * frost * 0.22 * uOpacity
      + crack * 0.55 + bubbles * 0.25
      + (s1 + s2) * 0.7
      + uPivot * 0.14;
    gl_FragColor = vec4(col, clamp(alpha, 0.0, 0.96));
    #include <tonemapping_fragment>
    #include <colorspace_fragment>
  }
`;

const GLINT_VERT = /* glsl */`
  attribute float aPhase;
  attribute float aSize;
  uniform float uTime;
  varying float vTwinkle;
  void main() {
    float tw = pow(max(sin(uTime * 2.3 + aPhase) * 0.5 + 0.5, 0.0), 10.0);
    vTwinkle = tw;
    gl_PointSize = aSize * (0.6 + 1.4 * tw);
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`;
const GLINT_FRAG = /* glsl */`
  precision highp float;
  varying float vTwinkle;
  void main() {
    vec2 d = gl_PointCoord - 0.5;
    float r = length(d);
    // 十字の光条 + 芯
    float core = smoothstep(0.5, 0.0, r);
    float cross = (smoothstep(0.06, 0.0, abs(d.x)) + smoothstep(0.06, 0.0, abs(d.y))) * smoothstep(0.5, 0.1, r);
    float a = (core * 0.9 + cross * 0.6) * vTwinkle;
    gl_FragColor = vec4(vec3(0.92, 0.98, 1.0), a);
  }
`;

/**
 * @param {HTMLCanvasElement} canvas
 * @returns {{update(frame: object): void, destroy(): void, ok: boolean}}
 */
export function initIceScene(canvas) {
  const noop = { update() {}, destroy() {}, ok: false };
  if (!canvas || !supportsWebGL()) return noop;

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
  } catch {
    return noop;
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));   // メモリ節約(WebView は落ちる)
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.15;
  renderer.setClearAlpha(0);

  const scene = new THREE.Scene();
  // ピクセル空間の正射影。y は CSS と同じ向き(下が正)にするため負で入れる。
  const camera = new THREE.OrthographicCamera(0, 1, 0, -1, -2000, 2000);
  camera.position.set(0, 0, 800);

  const group = new THREE.Group();
  scene.add(group);

  const TINT = {
    up: new THREE.Color(0x2fd6bd).convertSRGBToLinear(),
    down: new THREE.Color(0xff5566).convertSRGBToLinear(),
    pivot: new THREE.Color(0xbfefff).convertSRGBToLinear(),
  };
  const keyDir = new THREE.Vector3(-0.5, 0.8, 0.6).normalize();
  const rimDir = new THREE.Vector3(0.6, -0.5, 0.4).normalize();
  const makeIce = () => new THREE.ShaderMaterial({
    vertexShader: ICE_VERT,
    fragmentShader: ICE_FRAG,
    uniforms: {
      uTint: { value: TINT.up.clone() },
      uOpacity: { value: 0.55 },
      uPivot: { value: 0 },
      uTime: { value: 0 },
      uKey: { value: keyDir.clone() },
      uRim: { value: rimDir.clone() },
      uSeed: { value: 0 },
    },
    transparent: true,
    depthWrite: false,
    side: THREE.DoubleSide,
  });

  // 角を落とした箱を 1 個だけ作り、全ブロックで非一様スケールして使う。
  const iceGeometry = new RoundedBoxGeometry(1, 1, 1, 2, 0.14);
  const wickGeometry = new THREE.CylinderGeometry(0.5, 0.5, 1, 8, 1, false);
  const meshes = [];
  const wicks = [];
  for (let i = 0; i < MAX_BLOCKS; i += 1) {
    const material = makeIce();
    material.uniforms.uSeed.value = i * 3.7;
    const mesh = new THREE.Mesh(iceGeometry, material);
    mesh.visible = false;
    mesh.renderOrder = i;
    group.add(mesh);
    meshes.push(mesh);
    // ヒゲは実体の上と下の 2 本(実体は透けるので、1 本で貫くと中に線が見える)
    const wickMat = makeIce();
    wickMat.uniforms.uSeed.value = i * 3.7 + 1.9;
    wickMat.uniforms.uOpacity.value = 0.42;
    const pair = [new THREE.Mesh(wickGeometry, wickMat), new THREE.Mesh(wickGeometry, wickMat)];
    pair.forEach((wick) => { wick.visible = false; wick.renderOrder = i; group.add(wick); });
    wicks.push(pair);
  }

  // 稜線のきらめき(塊ごとに 3 点。位置は layout で置く)
  const glintCount = MAX_BLOCKS * GLINTS_PER_BLOCK;
  const glintGeo = new THREE.BufferGeometry();
  const glintPos = new Float32Array(glintCount * 3);
  const glintPhase = new Float32Array(glintCount);
  const glintSize = new Float32Array(glintCount);
  for (let i = 0; i < glintCount; i += 1) { glintPhase[i] = (i * 2.399) % (Math.PI * 2); glintSize[i] = 5 + (i % 3) * 2; }
  glintGeo.setAttribute("position", new THREE.BufferAttribute(glintPos, 3));
  glintGeo.setAttribute("aPhase", new THREE.BufferAttribute(glintPhase, 1));
  glintGeo.setAttribute("aSize", new THREE.BufferAttribute(glintSize, 1));
  glintGeo.setDrawRange(0, 0);
  const glintMat = new THREE.ShaderMaterial({
    vertexShader: GLINT_VERT, fragmentShader: GLINT_FRAG,
    uniforms: { uTime: { value: 0 } },
    transparent: true, depthWrite: false, depthTest: false, blending: THREE.AdditiveBlending,
  });
  const glints = new THREE.Points(glintGeo, glintMat);
  glints.renderOrder = MAX_BLOCKS + 1;
  scene.add(glints);

  let frame = null;
  let raf = 0;
  let disposed = false;
  // 見えていない間は回さない。WATCH 以外のタブでは watch の面が hidden(display:none)になるが、
  // display:none では RAF は止まらない —— 氷の WebGL が見えない画面で 60fps 回り続けていた。
  // 見えたら(タブに戻る・カードが差し戻される)observer が回し直す。destroy で外す。
  let visible = true;
  const observer = typeof IntersectionObserver === "function"
    ? new IntersectionObserver((entries) => {
      visible = entries.some((entry) => entry.isIntersecting);
      if (visible && frame && !raf && !disposed) raf = requestAnimationFrame(tick);
    }, { threshold: 0.01 })
    : null;
  observer?.observe(canvas);
  const reduced = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
  const startedAt = typeof performance !== "undefined" ? performance.now() : 0;
  const dpr = () => renderer.getPixelRatio();

  const layout = () => {
    if (!frame) return;
    const width = frame.width, height = frame.height;
    if (!(width > 0 && height > 0)) return;
    renderer.setSize(width, height, false);
    camera.left = 0; camera.right = width;
    camera.top = 0; camera.bottom = -height;
    camera.updateProjectionMatrix();
    const blocks = frame.blocks.slice(0, MAX_BLOCKS);
    const tint = frame.long ? TINT.up : TINT.down;
    let g = 0;
    blocks.forEach((block, i) => {
      const mesh = meshes[i];
      const w = Math.max(6, block.w);
      const h = Math.max(4, block.h);
      const depth = Math.max(5, w * 0.85);
      mesh.scale.set(w, h, depth);
      mesh.position.set(block.cx, -block.cy, 0);
      mesh.userData.base = { x: block.cx, y: -block.cy, phase: i * 0.7 };
      const u = mesh.material.uniforms;
      u.uTint.value.copy(block.pivot ? TINT.pivot : tint);
      u.uPivot.value = block.pivot ? 1 : 0;
      // 先の足ほど幽かに。建値は濃く
      u.uOpacity.value = (block.pivot ? 0.72 : 0.55) * (0.55 + 0.45 * (block.confidence ?? 1));
      mesh.visible = true;

      const seg = wickSegments({ cy: block.cy, h, wickCy: block.wickCy, wickH: block.wickH });
      const [wickUp, wickDown] = wicks[i];
      wickUp.material.uniforms.uTint.value.copy(tint);
      wickUp.material.uniforms.uOpacity.value = 0.42 * (0.55 + 0.45 * (block.confidence ?? 1));
      [[wickUp, seg.up], [wickDown, seg.down]].forEach(([wick, s]) => {
        wick.visible = s.len > 0.75;
        if (!wick.visible) return;
        wick.scale.set(1.8, s.len, 1.8);
        wick.position.set(block.cx, -s.cy, 0);
      });

      // きらめきは上面の両角と、下の角のどれか
      const corners = [[-0.5, -0.5], [0.5, -0.5], [0.42, 0.5]];
      for (let k = 0; k < GLINTS_PER_BLOCK; k += 1) {
        glintPos[g * 3] = block.cx + corners[k][0] * w * 0.9;
        glintPos[g * 3 + 1] = -(block.cy + corners[k][1] * h * 0.92);
        glintPos[g * 3 + 2] = 1;
        glintSize[g] = (4 + (k % 3) * 2) * dpr();
        g += 1;
      }
    });
    for (let i = blocks.length; i < MAX_BLOCKS; i += 1) {
      meshes[i].visible = false;
      wicks[i].forEach((wick) => { wick.visible = false; });
    }
    glintGeo.attributes.position.needsUpdate = true;
    glintGeo.attributes.aSize.needsUpdate = true;
    glintGeo.setDrawRange(0, g);
  };

  const tick = (now) => {
    raf = 0;
    if (disposed || !frame || !visible) return;   // 氷が無い・見えていない間は回さない(電池・CPU)
    raf = requestAnimationFrame(tick);
    const t = (now - startedAt) / 1000;
    const still = reduced && reduced.matches;
    const phase = still ? 0 : (t * Math.PI * 2) / SWAY.periodSec;
    // グリントの光源はゆっくり首を振る(稜線の光が移る)
    const kx = still ? keyDir.x : keyDir.x + Math.sin(t * 0.7) * 0.12;
    meshes.forEach((mesh) => {
      if (!mesh.visible) return;
      const b = mesh.userData.base || { x: mesh.position.x, y: mesh.position.y, phase: 0 };
      mesh.rotation.set(
        POSE.tiltX + (still ? 0 : Math.sin(phase + b.phase) * SWAY.tilt),
        POSE.yawY + (still ? 0 : Math.sin(phase * 0.8 + b.phase) * SWAY.yaw),
        0,
      );
      mesh.position.y = b.y + (still ? 0 : Math.sin(phase * 1.3 + b.phase) * SWAY.bobPx);
      const u = mesh.material.uniforms;
      u.uTime.value = t;
      u.uKey.value.set(kx, keyDir.y, keyDir.z).normalize();
    });
    wicks.forEach(([wickUp]) => { wickUp.material.uniforms.uTime.value = t; });   // 上下は同じ材質
    glintMat.uniforms.uTime.value = still ? 0.68 : t;   // 静止でも一定のきらめき
    renderer.render(scene, camera);
  };

  return {
    ok: true,
    /**
     * @param {{width:number,height:number,long:boolean,
     *          blocks:Array<{cx:number,cy:number,w:number,h:number,
     *                        wickCy:number,wickH:number,pivot:boolean,confidence?:number}>}} next
     */
    update(next) {
      if (disposed) return;
      if (!next || !next.blocks || !next.blocks.length) {
        frame = null;
        if (raf) { cancelAnimationFrame(raf); raf = 0; }
        meshes.forEach((m) => { m.visible = false; });
        wicks.forEach((pair) => pair.forEach((m) => { m.visible = false; }));
        glintGeo.setDrawRange(0, 0);
        renderer.clear();
        return;
      }
      frame = next;
      layout();
      if (!raf && visible) raf = requestAnimationFrame(tick);
    },
    destroy() {
      disposed = true;
      observer?.disconnect();
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      frame = null;
      iceGeometry.dispose();
      wickGeometry.dispose();
      glintGeo.dispose();
      glintMat.dispose();
      meshes.forEach((m) => m.material.dispose());
      wicks.forEach(([wickUp]) => wickUp.material.dispose());
      renderer.dispose();
      // WebGL コンテキストを即座に返す。dispose だけでは GC まで残り、描画のたびに
      // 作り直されると上限(Safari ≈ 16)とメモリを食う(2026-09-06 のクラッシュの疑い)。
      try { renderer.forceContextLoss(); } catch { /* 既に失われていれば何もしない */ }
    },
  };
}
