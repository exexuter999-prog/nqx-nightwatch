/**
 * NQX — アングラ×派手 系アイコン。
 * D: PROVIDENCE(監視の目) / E: ECLIPSE(蝕) / F: HEX MOON(魔法陣グリッチ)
 * 色収差(magenta/cyan の二重露光)は result カードのノヴァ軌跡と同じ言語。
 * フィルタ非依存(librsvg 互換のためグローは全て放射グラデで作る)。
 */
import sharp from "sharp";

const BG = "#070708";
const BONE = "#ece9df";
const GOLD = "#e8d9a8";
const ACID = "#d1ff32";
const MAG = "#ff2ea6";
const CYA = "#2ee6ff";

/** 決定論の擬似乱数(毎回同じ星空・同じルーンになる)。 */
function lcg(seed) {
  let x = seed >>> 0 || 1;
  return () => ((x = (x * 1664525 + 1013904223) >>> 0) / 4294967296);
}

function stars(seed, n = 46) {
  const rnd = lcg(seed);
  const dots = [];
  for (let i = 0; i < n; i += 1) {
    const x = 40 + rnd() * 944;
    const y = 40 + rnd() * 944;
    const r = 1.2 + rnd() * 2.6;
    const o = 0.12 + rnd() * 0.5;
    dots.push(`<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${r.toFixed(1)}" fill="${BONE}" opacity="${o.toFixed(2)}"/>`);
  }
  return dots.join("");
}

/** 中心から放つ光条の束(長短を交互に)。 */
function burst(cx, cy, n, rIn, rOut, w, color, opacity) {
  const rays = [];
  for (let i = 0; i < n; i += 1) {
    const a = (i / n) * Math.PI * 2 - Math.PI / 2;
    const len = i % 2 === 0 ? rOut : rOut * 0.62;
    const px = Math.cos(a), py = Math.sin(a);
    const nx = -py * w, ny = px * w;
    rays.push(`<path d="M ${cx + px * rIn + nx} ${cy + py * rIn + ny}
      L ${cx + px * len} ${cy + py * len}
      L ${cx + px * rIn - nx} ${cy + py * rIn - ny} Z" fill="${color}" opacity="${opacity}"/>`);
  }
  return rays.join("");
}

/** 直線と円弧だけで組むローマ数字(M D C L X V I)。h=14 の箱で描く。 */
const ROMAN = {
  M: "M0,14 L0,0 L5,8 L10,0 L10,14",
  D: "M0,0 L0,14 M0,0 L3,0 A7,7 0 0 1 3,14 L0,14",
  C: "M10,3 A7,7 0 1 0 10,11",
  L: "M0,0 L0,14 L9,14",
  X: "M0,0 L10,14 M10,0 L0,14",
  V: "M0,0 L5,14 L10,0",
  I: "M5,0 L5,14",
};
function romanText(text, cx, y, scale, color, opacity, gap = 13) {
  const width = text.length * gap * scale;
  let x = cx - width / 2;
  const parts = [];
  for (const ch of text) {
    parts.push(`<g transform="translate(${x.toFixed(1)} ${y}) scale(${scale})">
      <path d="${ROMAN[ch]}" fill="none" stroke="${color}" stroke-width="${(2.6 / scale).toFixed(2)}"
        stroke-linecap="square" opacity="${opacity}"/></g>`);
    x += gap * scale;
  }
  return parts.join("");
}

const DEFS = `
  <defs>
    <radialGradient id="room" cx="50%" cy="42%" r="78%">
      <stop offset="0%" stop-color="#0e0f0a"/>
      <stop offset="55%" stop-color="#0a0a0b"/>
      <stop offset="100%" stop-color="${BG}"/>
    </radialGradient>
    <radialGradient id="acidGlow" cx="50%" cy="50%" r="50%">
      <stop offset="0%" stop-color="${ACID}" stop-opacity=".95"/>
      <stop offset="38%" stop-color="${ACID}" stop-opacity=".28"/>
      <stop offset="100%" stop-color="${ACID}" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="goldGlow" cx="50%" cy="50%" r="50%">
      <stop offset="0%" stop-color="${GOLD}" stop-opacity=".6"/>
      <stop offset="100%" stop-color="${GOLD}" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="coronaRing" cx="50%" cy="50%" r="50%">
      <stop offset="52%" stop-color="${ACID}" stop-opacity="0"/>
      <stop offset="62%" stop-color="${ACID}" stop-opacity=".85"/>
      <stop offset="72%" stop-color="${ACID}" stop-opacity=".18"/>
      <stop offset="100%" stop-color="${ACID}" stop-opacity="0"/>
    </radialGradient>
    <mask id="biteL">
      <rect width="1024" height="1024" fill="#fff"/>
      <circle cx="640" cy="430" r="206" fill="#000"/>
    </mask>
  </defs>`;

/* ---------------------------------------------------------------- D: PROVIDENCE */
function eye(cx, cy, w, h, color, sw, opacity = 1, dx = 0, dy = 0) {
  const x1 = cx - w / 2 + dx, x2 = cx + w / 2 + dx, y = cy + dy;
  return `<path d="M ${x1} ${y} Q ${cx + dx} ${y - h} ${x2} ${y} Q ${cx + dx} ${y + h} ${x1} ${y} Z"
    fill="none" stroke="${color}" stroke-width="${sw}" opacity="${opacity}"/>`;
}
const svgD = `
<svg width="1024" height="1024" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
  ${DEFS}
  <rect width="1024" height="1024" fill="url(#room)"/>
  ${stars(1776)}
  ${burst(512, 512, 26, 300, 486, 7, GOLD, 0.34)}
  ${burst(512, 512, 26, 300, 430, 4, GOLD, 0.2)}
  <path d="M 512 236 L 812 788 L 212 788 Z" fill="none" stroke="${GOLD}" stroke-width="7" opacity=".14"/>
  <circle cx="512" cy="512" r="270" fill="url(#goldGlow)" opacity=".5"/>
  ${eye(512, 512, 560, 300, MAG, 16, 0.55, -9, -5)}
  ${eye(512, 512, 560, 300, CYA, 16, 0.55, 9, 5)}
  ${eye(512, 512, 560, 300, BONE, 18)}
  <circle cx="512" cy="512" r="128" fill="url(#acidGlow)"/>
  <circle cx="512" cy="512" r="96" fill="#0a0a0b" stroke="${ACID}" stroke-width="10"/>
  <circle cx="512" cy="512" r="96" fill="${ACID}" mask="url(#pupilBite)"/>
  <defs>
    <mask id="pupilBite">
      <rect width="1024" height="1024" fill="#fff"/>
      <circle cx="556" cy="482" r="82" fill="#000"/>
    </mask>
  </defs>
  <circle cx="548" cy="470" r="12" fill="${BONE}"/>
  ${romanText("MDCCLXXVI", 512, 894, 1.7, GOLD, 0.26)}
</svg>`;

/* ---------------------------------------------------------------- E: ECLIPSE */
const svgE = `
<svg width="1024" height="1024" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
  ${DEFS}
  <rect width="1024" height="1024" fill="url(#room)"/>
  ${stars(1913)}
  ${burst(512, 512, 13, 250, 500, 9, ACID, 0.5)}
  ${burst(512, 512, 13, 250, 392, 5, ACID, 0.3)}
  <circle cx="512" cy="512" r="430" fill="url(#coronaRing)"/>
  <circle cx="506" cy="508" r="252" fill="none" stroke="${MAG}" stroke-width="10" opacity=".6"/>
  <circle cx="518" cy="516" r="252" fill="none" stroke="${CYA}" stroke-width="10" opacity=".6"/>
  <circle cx="512" cy="512" r="286" fill="url(#acidGlow)" opacity=".9"/>
  <circle cx="512" cy="512" r="234" fill="#050506"/>
  <circle cx="512" cy="512" r="240" fill="none" stroke="${ACID}" stroke-width="12"/>
  <circle cx="388" cy="392" r="16" fill="${BONE}" opacity=".9"/>
  <path d="M 400 512 Q 512 448 624 512 Q 512 576 400 512 Z"
    fill="none" stroke="${GOLD}" stroke-width="5" opacity=".055"/>
  <circle cx="512" cy="512" r="26" fill="${GOLD}" opacity=".05"/>
  ${romanText("MDCCLXXVI", 512, 900, 1.6, ACID, 0.2)}
</svg>`;

/* ---------------------------------------------------------------- F: HEX MOON */
function runes(cx, cy, r, n, seed) {
  const rnd = lcg(seed);
  const out = [];
  for (let i = 0; i < n; i += 1) {
    const a = (i / n) * Math.PI * 2;
    const px = cx + Math.cos(a) * r;
    const py = cy + Math.sin(a) * r;
    const len = 14 + rnd() * 22;
    const rot = (a * 180 / Math.PI + 90).toFixed(1);
    // 裏: 四方位は乱数ルーンではなく 1776 のタリー(上=I 右=VII 下=VII 左=VI)
    const cardinal = { 0: 1, [n / 4]: 7, [n / 2]: 7, [(3 * n) / 4]: 6 }[i];
    if (cardinal) {
      const bars = Array.from({ length: Math.min(cardinal, 5) }, (_, k) =>
        `<line x1="${(k - (Math.min(cardinal, 5) - 1) / 2) * 7}" y1="-13" x2="${(k - (Math.min(cardinal, 5) - 1) / 2) * 7}" y2="13"/>`).join("");
      const overbar = cardinal > 5 ? `<line x1="-16" y1="-17" x2="16" y2="17"/>` : "";
      out.push(`<g transform="translate(${px.toFixed(1)} ${py.toFixed(1)}) rotate(${(a * 180 / Math.PI + 90).toFixed(1)})"
        stroke="${ACID}" stroke-width="4.5" opacity=".8">${bars}${overbar}</g>`);
      continue;
    }
    const kind = rnd();
    const glyph = kind < 0.34
      ? `<line x1="${-len / 2}" y1="0" x2="${len / 2}" y2="0"/>`
      : kind < 0.67
        ? `<path d="M ${-len / 2} ${len / 3} L 0 ${-len / 2} L ${len / 2} ${len / 3}" fill="none"/>`
        : `<path d="M ${-len / 2} ${-len / 3} h ${len} M 0 ${-len / 3} v ${len * 0.7}" fill="none"/>`;
    out.push(`<g transform="translate(${px.toFixed(1)} ${py.toFixed(1)}) rotate(${rot})"
      stroke="${GOLD}" stroke-width="4" opacity=".62">${glyph}</g>`);
  }
  return out.join("");
}
function heptagram(cx, cy, r, color, sw, opacity) {
  const pts = [];
  for (let i = 0; i < 7; i += 1) {
    const a = ((i * 3) % 7) / 7 * Math.PI * 2 - Math.PI / 2;
    pts.push(`${(cx + Math.cos(a) * r).toFixed(1)},${(cy + Math.sin(a) * r).toFixed(1)}`);
  }
  return `<polygon points="${pts.join(" ")}" fill="none" stroke="${color}" stroke-width="${sw}" opacity="${opacity}"/>`;
}
const crescentF = (dx, dy, color, opacity) => `
  <g transform="translate(${dx} ${dy})" opacity="${opacity}">
    <circle cx="512" cy="512" r="216" fill="${color}" mask="url(#biteL)"/>
  </g>`;
const svgF = `
<svg width="1024" height="1024" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
  ${DEFS}
  <rect width="1024" height="1024" fill="url(#room)"/>
  ${stars(666)}
  <circle cx="512" cy="512" r="448" fill="none" stroke="${GOLD}" stroke-width="4" opacity=".5"/>
  <circle cx="512" cy="512" r="386" fill="none" stroke="${GOLD}" stroke-width="2.5" opacity=".4"/>
  ${runes(512, 512, 417, 36, 1349)}
  ${heptagram(512, 512, 360, ACID, 4, 0.3)}
  ${crescentF(-12, 6, MAG, 0.6)}
  ${crescentF(12, -6, CYA, 0.6)}
  ${crescentF(0, 0, BONE, 1)}
  <rect x="240" y="470" width="544" height="26" fill="url(#room)"/>
  <g clip-path="url(#sliceClip)" transform="translate(30 0)">
    ${crescentF(0, 0, BONE, 1)}
  </g>
  <defs><clipPath id="sliceClip"><rect x="210" y="470" width="604" height="26"/></clipPath></defs>
  <circle cx="668" cy="360" r="120" fill="url(#acidGlow)"/>
  ${burst(668, 360, 4, 10, 118, 9, ACID, 1)}
  <circle cx="668" cy="360" r="20" fill="${ACID}"/>
  ${romanText("MDCCLXXVI", 512, 940, 1.35, GOLD, 0.3)}
</svg>`;

/* ---------------------------------------------------------------- G: HEX MOON // OVERDRIVE */
function sparkle(cx, cy, r, color, opacity = 1) {
  const w = r * 0.22;
  return `<g opacity="${opacity}">
    <path d="M ${cx - w} ${cy} L ${cx} ${cy - r} L ${cx + w} ${cy} L ${cx} ${cy + r} Z" fill="${color}"/>
    <path d="M ${cx} ${cy - w} L ${cx + r * .72} ${cy} L ${cx} ${cy + w} L ${cx - r * .72} ${cy} Z" fill="${color}"/>
  </g>`;
}
const svgG = `
<svg width="1024" height="1024" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
  ${DEFS}
  <defs>
    <linearGradient id="streak" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="${ACID}" stop-opacity="0"/>
      <stop offset="46%" stop-color="${ACID}" stop-opacity=".9"/>
      <stop offset="54%" stop-color="#f6ffd0" stop-opacity="1"/>
      <stop offset="62%" stop-color="${ACID}" stop-opacity=".9"/>
      <stop offset="100%" stop-color="${ACID}" stop-opacity="0"/>
    </linearGradient>
    <radialGradient id="moonHalo" cx="50%" cy="50%" r="50%">
      <stop offset="30%" stop-color="${ACID}" stop-opacity="0"/>
      <stop offset="58%" stop-color="${ACID}" stop-opacity=".34"/>
      <stop offset="78%" stop-color="${ACID}" stop-opacity=".08"/>
      <stop offset="100%" stop-color="${ACID}" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="1024" height="1024" fill="url(#room)"/>
  ${stars(666, 62)}
  ${burst(512, 512, 26, 330, 500, 5, ACID, 0.16)}
  <circle cx="512" cy="512" r="470" fill="url(#moonHalo)" opacity=".55"/>
  <circle cx="512" cy="512" r="448" fill="none" stroke="${ACID}" stroke-width="5" opacity=".55"/>
  <circle cx="512" cy="512" r="386" fill="none" stroke="${GOLD}" stroke-width="3" opacity=".5"/>
  ${runes(512, 512, 417, 36, 1349)}
  ${heptagram(512, 512, 360, ACID, 16, 0.14)}
  ${heptagram(512, 512, 360, ACID, 4.5, 0.55)}
  <circle cx="470" cy="540" r="330" fill="url(#moonHalo)"/>
  ${crescentF(-16, 8, MAG, 0.72)}
  ${crescentF(16, -8, CYA, 0.72)}
  <g opacity=".9"><circle cx="512" cy="512" r="226" fill="${ACID}" mask="url(#biteL)"/></g>
  ${crescentF(0, 0, BONE, 1)}
  <rect x="240" y="470" width="544" height="26" fill="url(#room)"/>
  <g clip-path="url(#sliceClipG)" transform="translate(34 0)">
    ${crescentF(0, 0, BONE, 1)}
  </g>
  <defs><clipPath id="sliceClipG"><rect x="210" y="470" width="604" height="26"/></clipPath></defs>
  <rect x="330" y="356" width="640" height="9" fill="url(#streak)"/>
  <rect x="380" y="368" width="540" height="4" fill="url(#streak)" opacity=".55"/>
  <circle cx="672" cy="360" r="150" fill="url(#acidGlow)"/>
  ${burst(672, 360, 8, 14, 150, 8, ACID, 1)}
  ${sparkle(672, 360, 56, "#f6ffd0")}
  <circle cx="672" cy="360" r="17" fill="#f6ffd0"/>
  ${sparkle(300, 258, 30, ACID, 0.85)}
  ${sparkle(788, 648, 24, BONE, 0.7)}
  ${sparkle(384, 760, 18, ACID, 0.6)}
  ${romanText("MDCCLXXVI", 512, 940, 1.35, GOLD, 0.4)}
</svg>`;


/* ---------------------------------------------------------------- D2: PROVIDENCE // WARDEN */
/** 罠の歯列。内向きの牙のリング。cardinalEvery ごとの牙だけ acid に灯る。 */
function toothRing(cx, cy, rOut, rIn, n, color, opacity, cardinalEvery = null) {
  const teeth = [];
  for (let i = 0; i < n; i += 1) {
    const a = (i / n) * Math.PI * 2 - Math.PI / 2;
    const da = (Math.PI / n) * 0.62;
    const x1 = cx + Math.cos(a - da) * rOut, y1 = cy + Math.sin(a - da) * rOut;
    const x2 = cx + Math.cos(a + da) * rOut, y2 = cy + Math.sin(a + da) * rOut;
    const tx = cx + Math.cos(a) * rIn, ty = cy + Math.sin(a) * rIn;
    const hot = cardinalEvery && i % cardinalEvery === 0;
    teeth.push(`<path d="M ${x1.toFixed(1)} ${y1.toFixed(1)} L ${tx.toFixed(1)} ${ty.toFixed(1)}
      L ${x2.toFixed(1)} ${y2.toFixed(1)} Z" fill="${hot ? ACID : color}" opacity="${hot ? 0.95 : opacity}"/>`);
  }
  return teeth.join("");
}

/** 刃の光条。基部が細く、先が長い。 */
function blades(cx, cy, n, rIn, rOut, w, color, opacity) {
  const rays = [];
  for (let i = 0; i < n; i += 1) {
    const a = (i / n) * Math.PI * 2 - Math.PI / 2;
    const len = i % 2 === 0 ? rOut : rOut * 0.7;
    const px = Math.cos(a), py = Math.sin(a);
    const nx = -py * w, ny = px * w;
    rays.push(`<path d="M ${cx + px * rIn + nx} ${cy + py * rIn + ny}
      L ${cx + px * len} ${cy + py * len}
      L ${cx + px * rIn - nx} ${cy + py * rIn - ny} Z" fill="${color}" opacity="${opacity}"/>`);
  }
  return rays.join("");
}

/** 鋭い目。目尻が刃として左右へ伸びる。 */
function sharpEye(cx, cy, w, h, color, sw, opacity = 1, dx = 0, dy = 0) {
  const x1 = cx - w / 2 + dx, x2 = cx + w / 2 + dx, y = cy + dy;
  return `<g stroke="${color}" stroke-width="${sw}" opacity="${opacity}" fill="none">
    <path d="M ${x1} ${y} Q ${cx + dx} ${y - h} ${x2} ${y} Q ${cx + dx} ${y + h} ${x1} ${y} Z"/>
    <path d="M ${x1} ${y} l -52 -10" stroke-linecap="square"/>
    <path d="M ${x2} ${y} l 52 10" stroke-linecap="square"/>
  </g>`;
}

const svgD2 = `
<svg width="1024" height="1024" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
  ${DEFS}
  <rect width="1024" height="1024" fill="url(#room)"/>
  ${stars(1776, 40)}
  ${blades(512, 512, 26, 322, 500, 5, GOLD, 0.4)}
  ${blades(512, 512, 26, 322, 452, 3, ACID, 0.22)}
  <path d="M 512 218 L 828 800 L 196 800 Z" fill="none" stroke="${GOLD}" stroke-width="8" opacity=".2"/>
  ${toothRing(512, 512, 486, 428, 36, "#8f8b81", 0.55, 9)}
  <circle cx="512" cy="512" r="278" fill="url(#goldGlow)" opacity=".42"/>
  ${sharpEye(512, 512, 560, 310, MAG, 15, 0.65, -13, -6)}
  ${sharpEye(512, 512, 560, 310, CYA, 15, 0.65, 13, 6)}
  ${sharpEye(512, 512, 560, 310, BONE, 19)}
  <circle cx="512" cy="512" r="140" fill="url(#acidGlow)"/>
  <circle cx="512" cy="512" r="100" fill="#0a0a0b" stroke="${ACID}" stroke-width="12"/>
  <circle cx="512" cy="512" r="82" fill="${ACID}" opacity=".28"/>
  <path d="M 512 428 L 534 512 L 512 596 L 490 512 Z" fill="#050506"/>
  <path d="M 512 436 L 528 512 L 512 588 L 496 512 Z" fill="none" stroke="${ACID}" stroke-width="4" opacity=".55"/>
  <circle cx="540" cy="466" r="11" fill="${BONE}"/>
  ${romanText("MDCCLXXVI", 512, 902, 1.7, GOLD, 0.38)}
</svg>`;

const variants = { d: svgD, e: svgE, f: svgF, g: svgG, d2: svgD2 };
const circleMask = Buffer.from(
  `<svg width="256" height="256"><circle cx="128" cy="128" r="128" fill="#fff"/></svg>`,
);

for (const [name, svg] of Object.entries(variants)) {
  const buf = Buffer.from(svg);
  await sharp(buf).png().toFile(`nqx-icon-${name}-1024.png`);
  await sharp(buf).resize(512, 512).png().toFile(`nqx-icon-${name}-512.png`);
}

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
await strip.composite(composites).png().toFile("nqx-icon-preview2.png");

const cmp = sharp({ create: { width: 760, height: 420, channels: 4, background: "#0e0e10" } });
const cc = [];
let cx2 = 60;
for (const name of ["d", "d2"]) {
  const round = await sharp(`nqx-icon-${name}-1024.png`).resize(256, 256)
    .composite([{ input: circleMask, blend: "dest-in" }]).png().toBuffer();
  const small = await sharp(round).resize(88, 88).png().toBuffer();
  cc.push({ input: round, left: cx2, top: 60 });
  cc.push({ input: small, left: cx2 + 84, top: 330 });
  cx2 += 340;
}
await cmp.composite(cc).png().toFile("nqx-icon-compare-dd2.png");
console.log("done");
