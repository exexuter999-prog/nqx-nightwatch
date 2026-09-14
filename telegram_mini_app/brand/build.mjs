/**
 * NQX Nightwatch — Telegram アイコン生成。
 * 意匠: 黒×骨白×acid、計器の目盛りリング、面取り、nova。
 * フォント非依存(全て path/図形)なので環境差で崩れない。
 */
import sharp from "sharp";

const BG0 = "#08080a";
const BG1 = "#101109";
const BONE = "#ece9df";
const BONE_DIM = "#b9b5a8";
const ACID = "#d1ff32";
const TICK_MINOR = "#26271f";
const TICK_MAJOR = "#3d3e33";

/** 計器の目盛りリング。majorEvery 度ごとに長い刻み。 */
function tickRing(cx, cy, rOuter, { minorLen = 16, majorLen = 34, step = 6, majorEvery = 30, wMinor = 3.5, wMajor = 5 } = {}) {
  const lines = [];
  for (let deg = 0; deg < 360; deg += step) {
    const major = deg % majorEvery === 0;
    const len = major ? majorLen : minorLen;
    const a = (deg - 90) * Math.PI / 180;
    const x1 = cx + Math.cos(a) * (rOuter - len);
    const y1 = cy + Math.sin(a) * (rOuter - len);
    const x2 = cx + Math.cos(a) * rOuter;
    const y2 = cy + Math.sin(a) * rOuter;
    lines.push(`<line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}"
      stroke="${major ? TICK_MAJOR : TICK_MINOR}" stroke-width="${major ? wMajor : wMinor}"/>`);
  }
  return lines.join("");
}

/** 4条の nova(輝点)。 */
function nova(cx, cy, r, core = 18) {
  // 4条の光条。縦を長く、横をやや短く — 写真乾板のスパイクの形。
  const ray = (dx, dy, len, w) =>
    `<path d="M ${cx - dy * w} ${cy + dx * w} L ${cx + dx * len} ${cy + dy * len}
      L ${cx + dy * w} ${cy - dx * w} Z" fill="${ACID}"/>`;
  return `
    <circle cx="${cx}" cy="${cy}" r="${r}" fill="url(#novaGlow)"/>
    ${ray(0, -1, r * 1.55, core * .52)}${ray(0, 1, r * 1.55, core * .52)}
    ${ray(-1, 0, r * 1.1, core * .52)}${ray(1, 0, r * 1.1, core * .52)}
    <circle cx="${cx}" cy="${cy}" r="${core}" fill="${ACID}"/>`;
}

const DEFS = `
  <defs>
    <radialGradient id="room" cx="50%" cy="36%" r="80%">
      <stop offset="0%" stop-color="${BG1}"/>
      <stop offset="52%" stop-color="#0b0c0b"/>
      <stop offset="100%" stop-color="${BG0}"/>
    </radialGradient>
    <radialGradient id="novaGlow" cx="50%" cy="50%" r="50%">
      <stop offset="0%" stop-color="${ACID}" stop-opacity=".85"/>
      <stop offset="45%" stop-color="${ACID}" stop-opacity=".18"/>
      <stop offset="100%" stop-color="${ACID}" stop-opacity="0"/>
    </radialGradient>
    <mask id="bite">
      <rect width="1024" height="1024" fill="#fff"/>
      <circle cx="640" cy="420" r="212" fill="#000"/>
    </mask>
  </defs>`;

/* A — Nova Crescent: 骨白の三日月、その先端に acid の輝点 */
const svgA = `
<svg width="1024" height="1024" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
  ${DEFS}
  <rect width="1024" height="1024" fill="url(#room)"/>
  ${tickRing(512, 512, 448)}
  <circle cx="512" cy="512" r="252" fill="${BONE}" mask="url(#bite)"/>
  ${nova(676, 368, 92, 19)}
</svg>`;

/* B — The Beam: 傾いた天秤の梁と2枚の皿。acid の要(ピボット) */
function beam() {
  const cx = 512, cy = 452, half = 296, tilt = -9 * Math.PI / 180;
  const dx = Math.cos(tilt) * half, dy = Math.sin(tilt) * half;
  const L = { x: cx - dx, y: cy - dy };
  const R = { x: cx + dx, y: cy + dy };
  const pan = (p, drop, w = 196, h = 40) => `
    <line x1="${p.x}" y1="${p.y}" x2="${p.x}" y2="${p.y + drop}" stroke="${BONE_DIM}" stroke-width="12"/>
    <path d="M ${p.x - w / 2} ${p.y + drop} h ${w} l -20 ${h} h -${w - 40} Z" fill="${BONE}"/>`;
  return `
    <line x1="${L.x}" y1="${L.y}" x2="${R.x}" y2="${R.y}" stroke="${BONE}" stroke-width="30" stroke-linecap="square"/>
    <rect x="${cx - 17}" y="${cy - 96}" width="34" height="96" fill="${BONE_DIM}"/>
    ${pan(L, 150)}
    ${pan(R, 252)}
    <path d="M ${cx} ${cy - 40} l 44 44 l -44 44 l -44 -44 Z" fill="${ACID}"/>`;
}
const svgB = `
<svg width="1024" height="1024" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
  ${DEFS}
  <rect width="1024" height="1024" fill="url(#room)"/>
  ${tickRing(512, 512, 448)}
  ${beam()}
</svg>`;

/* C — Acid Crescent: acid 一色の三日月。最小・最強コントラスト */
const svgC = `
<svg width="1024" height="1024" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
  ${DEFS}
  <rect width="1024" height="1024" fill="url(#room)"/>
  ${tickRing(512, 512, 448)}
  <circle cx="512" cy="512" r="252" fill="${ACID}" mask="url(#bite)"/>
  <circle cx="676" cy="372" r="17" fill="${BONE}"/>
</svg>`;

const variants = { a: svgA, b: svgB, c: svgC };
const circleMask = Buffer.from(
  `<svg width="256" height="256"><circle cx="128" cy="128" r="128" fill="#fff"/></svg>`,
);

for (const [name, svg] of Object.entries(variants)) {
  const buf = Buffer.from(svg);
  await sharp(buf).png().toFile(`nqx-icon-${name}-1024.png`);
  await sharp(buf).resize(512, 512).png().toFile(`nqx-icon-${name}-512.png`);
}

/* 円形クロップの実寸プレビュー(大=96px相当、小=40px相当を並べる) */
const strip = sharp({
  create: { width: 1080, height: 420, channels: 4, background: "#0e0e10" },
});
const composites = [];
let x = 60;
for (const name of Object.keys(variants)) {
  const round = await sharp(`nqx-icon-${name}-1024.png`)
    .resize(256, 256)
    .composite([{ input: circleMask, blend: "dest-in" }])
    .png().toBuffer();
  const small = await sharp(round).resize(88, 88).png().toBuffer();
  composites.push({ input: round, left: x, top: 60 });
  composites.push({ input: small, left: x + 84, top: 330 });
  x += 340;
}
await strip.composite(composites).png().toFile("nqx-icon-preview.png");
console.log("done");
