/**
 * CARD FIELD — カード背景の鉄粉(R64, 2026-09-06)。
 *
 * ユーザー「カードの背景をインタラクティブに何か面白いものを」。
 * 黒鉄のカード(R59)の面に、微細な鉄粉(短いダッシュ)を格子に眠らせる。指やポインタが
 * 近づくと、鉄粉は磁石に吸われるようにその方向へ向きを揃え、朱の光を帯びる。離すとゆっくり
 * 元の眠り(研磨目に沿った向き)へ戻る。タップすると波紋が鉄粉の列を走る。
 *
 * 表示専用。カードの中身・view・注文には触れない。canvas を 1 枚カードの背景層に置くだけ
 * (`.state-card` は isolation: isolate、canvas は z-index -1 で本文の下・面の上)。
 * 動くのはポインタが触れている間と、その後 1.4 秒の緩和だけ。何も無ければ静止画 1 枚で止まる
 * (電池)。prefers-reduced-motion では静止した鉄粉だけ(反応しない)。
 *
 * 純関数(layoutGrid / restAngle / fieldAngle / influence / lerpAngle)は DOM を触らないので
 * node --test から読める。
 */

export const SPACING = 14;          // 鉄粉の格子間隔(px)
export const DASH = 5.2;            // 鉄粉の長さ(px)
export const SIGMA = 92;            // 磁力の届く距離(px、ガウス幅)
export const RELAX_MS = 1400;       // 離してから眠りへ戻り切るまで
export const RIPPLE_MS = 900;       // タップの波紋
export const BASE_ALPHA = 0.075;    // 眠っている鉄粉の濃さ
export const PEAK_ALPHA = 0.5;      // 磁力の芯での濃さ

/** 決定的な擬似乱数(格子点ごとに同じ値)。 */
export function hash2(x, y, seed = 0) {
  let h = (x * 374761393 + y * 668265263 + seed * 1442695041) | 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  return ((h ^ (h >>> 16)) >>> 0) / 4294967295;
}

/** 眠っている向き。研磨目(ほぼ水平)に沿い、粒ごとに ±28° ほど散る。 */
export function restAngle(ix, iy, seed = 0) {
  return (hash2(ix, iy, seed) - 0.5) * (56 * Math.PI / 180);
}

/** 幅 w × 高さ h を間隔 spacing の格子に切る。端は半分ずらして縁に貼り付かせない。 */
export function layoutGrid(w, h, spacing = SPACING) {
  const cols = Math.max(1, Math.floor(w / spacing));
  const rows = Math.max(1, Math.floor(h / spacing));
  const ox = (w - (cols - 1) * spacing) / 2;
  const oy = (h - (rows - 1) * spacing) / 2;
  const points = [];
  for (let iy = 0; iy < rows; iy += 1) {
    for (let ix = 0; ix < cols; ix += 1) {
      // 一行おきに半格子ずらす(六方に近い並び。格子の縞が見えない)
      const shift = iy % 2 ? spacing / 2 : 0;
      const x = ox + ix * spacing + shift;
      if (x > w - 1) continue;
      points.push({ x, y: oy + iy * spacing, ix, iy });
    }
  }
  return points;
}

/** 磁力の強さ 0..1。距離のガウス。 */
export function influence(dx, dy, sigma = SIGMA) {
  const d2 = dx * dx + dy * dy;
  return Math.exp(-d2 / (2 * sigma * sigma));
}

/** 角の差を [-π, π] に畳む。 */
export function angleDelta(from, to) {
  let d = (to - from) % (Math.PI * 2);
  if (d > Math.PI) d -= Math.PI * 2;
  if (d < -Math.PI) d += Math.PI * 2;
  return d;
}

/**
 * 鉄粉の目標の向き。磁極(px, py)へ向く角(鉄粉は磁極へ放射状に並ぶ)と眠りの向きを、
 * 磁力 w で混ぜる。鉄粉は向きに極性が無い(0 と π は同じ)ので、差は [-π/2, π/2] に畳む。
 */
export function fieldAngle(rest, dx, dy, w) {
  if (w <= 0) return rest;
  const radial = Math.atan2(dy, dx);
  let d = angleDelta(rest, radial);
  if (d > Math.PI / 2) d -= Math.PI;
  if (d < -Math.PI / 2) d += Math.PI;
  return rest + d * w;
}

/** 角を目標へ寄せる(1 フレームぶん)。極性なしの最短側へ。 */
export function lerpAngle(current, target, k) {
  let d = angleDelta(current, target);
  if (d > Math.PI / 2) d -= Math.PI;
  if (d < -Math.PI / 2) d += Math.PI;
  return current + d * k;
}

/**
 * 磁力の全体の強さ 0..1。触れている間は 1、離してからは RELAX_MS かけて直線で 0 へ。
 * (以前は「最後に触れてから 1.4 秒は 1、その後いきなり 0」で、緩和が段階的でなかった。)
 */
export function magnetStrength(held, sinceReleaseMs, relaxMs = RELAX_MS) {
  if (held) return 1;
  if (!(sinceReleaseMs >= 0)) return 0;
  return Math.max(0, 1 - sinceReleaseMs / relaxMs);
}

/** 波紋の明るさ 0..1。中心から半径 r(px) の輪が RIPPLE_MS で広がって薄れる。 */
export function rippleGlow(dist, elapsedMs, speedPxPerMs = 0.55, width = 26) {
  if (elapsedMs < 0 || elapsedMs > RIPPLE_MS) return 0;
  const r = elapsedMs * speedPxPerMs;
  const fade = 1 - elapsedMs / RIPPLE_MS;
  const band = Math.exp(-((dist - r) * (dist - r)) / (2 * width * width));
  return band * fade;
}

// ---------------------------------------------------------------- 描画

const attached = new WeakSet();

/**
 * host(カード)に鉄粉の背景を付ける。二重には付けない。
 * 戻り値は detach 用(host が innerHTML で消えれば自然に捨てられる)。
 */
export function attachCardField(host, { win = window } = {}) {
  if (!host || attached.has(host)) return null;
  const doc = host.ownerDocument || win.document;
  const reduced = win.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches === true;
  const canvas = doc.createElement("canvas");
  canvas.className = "card-field";
  canvas.setAttribute("aria-hidden", "true");
  host.prepend(canvas);
  attached.add(host);
  const ctx = canvas.getContext("2d");
  if (!ctx) { canvas.remove(); return null; }

  // DPR は 1.5 まで(カードは背が高く、2 だと 1 枚 5MB を超える。Telegram の WebView は
  // メモリ超過で落ちる —— 2026-09-06 に一度クラッシュ)
  let dpr = Math.min(win.devicePixelRatio || 1, 1.5);
  let w = 0;
  let h = 0;
  let grains = [];
  let pointer = null;             // { x, y } カード内 px、無ければ null
  let held = false;               // 指・ポインタがカードの上に居る間 true(磁力 1)
  let lastPointerAt = -Infinity;  // 最後に動いた時刻(止まった指の下で向きが揃い切るまで回す)
  let releasedAt = -Infinity;     // 離した時刻(ここから RELAX_MS で眠りへ)
  let ripple = null;              // { x, y, at }
  let raf = 0;
  let lastT = 0;

  const resize = () => {
    const rect = host.getBoundingClientRect();
    w = Math.max(1, Math.round(rect.width));
    h = Math.max(1, Math.round(rect.height));
    dpr = Math.min(win.devicePixelRatio || 1, 1.5);
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.style.width = `${w}px`;
    canvas.style.height = `${h}px`;
    grains = layoutGrid(w, h, SPACING).map((p) => {
      const rest = restAngle(p.ix, p.iy, 7);
      return { ...p, rest, angle: rest, glow: 0 };
    });
    draw(performance.now(), 1);
  };

  const draw = (now, dt) => {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const strength = pointer ? magnetStrength(held, now - releasedAt) : 0;
    const k = Math.min(1, dt * 9);      // 向きの追従の速さ
    ctx.lineCap = "round";
    ctx.lineWidth = 1;
    for (const g of grains) {
      let wgt = 0;
      let dx = 0;
      let dy = 0;
      if (pointer) {
        dx = pointer.x - g.x;
        dy = pointer.y - g.y;
        wgt = influence(dx, dy) * strength;
      }
      const target = fieldAngle(g.rest, dx, dy, wgt);
      g.angle = reduced ? g.rest : lerpAngle(g.angle, target, k);
      let rip = 0;
      if (ripple) rip = rippleGlow(Math.hypot(g.x - ripple.x, g.y - ripple.y), now - ripple.at);
      g.glow += ((wgt + rip * 0.9) - g.glow) * Math.min(1, dt * 7);
      const a = BASE_ALPHA + (PEAK_ALPHA - BASE_ALPHA) * Math.min(1, g.glow);
      // 眠りは骨色、磁力の芯ほど朱へ
      const t = Math.min(1, g.glow * 1.2);
      const r = Math.round(236 + (255 - 236) * t);
      const gr = Math.round(233 + (59 - 233) * t);
      const b = Math.round(223 + (79 - 223) * t);
      ctx.strokeStyle = `rgba(${r},${gr},${b},${a.toFixed(3)})`;
      const len = DASH * (1 + g.glow * 0.5);
      const cx = Math.cos(g.angle) * len / 2;
      const cy = Math.sin(g.angle) * len / 2;
      ctx.beginPath();
      ctx.moveTo(g.x - cx, g.y - cy);
      ctx.lineTo(g.x + cx, g.y + cy);
      ctx.stroke();
    }
    // 磁極の下の柔らかい灯り
    if (pointer && strength > 0) {
      const glow = ctx.createRadialGradient(pointer.x, pointer.y, 0, pointer.x, pointer.y, SIGMA * 1.1);
      glow.addColorStop(0, `rgba(255,59,79,${(0.11 * strength).toFixed(3)})`);
      glow.addColorStop(1, "rgba(255,59,79,0)");
      ctx.fillStyle = glow;
      ctx.fillRect(0, 0, w, h);
    }
  };

  const frame = (now) => {
    raf = 0;
    const dt = Math.min(0.05, lastT ? (now - lastT) / 1000 : 0.016);
    lastT = now;
    draw(now, dt);
    // 回すのは: 止まった指の下で向きが揃い切るまで / 離してから眠りへ戻り切るまで / 波紋の間。
    // 指が居座って揃い切ったら止まる(磁力は掛かったまま静止画で残る)。
    const converging = held && now - lastPointerAt < 700;
    const relaxing = !held && pointer && now - releasedAt < RELAX_MS + 200;
    const rippling = ripple && now - ripple.at < RIPPLE_MS + 100;
    if (converging || relaxing || rippling) raf = win.requestAnimationFrame(frame);
    else { lastT = 0; if (!held) pointer = null; }
  };
  const kick = () => { if (!raf) raf = win.requestAnimationFrame(frame); };

  // 寸法の追従は ResizeObserver を使わない: observer が消えたカードと canvas を掴み続けて、
  // 描画(innerHTML)ごとに 1 枚ずつメモリが積み上がる。触れたときに測り直せば十分。
  const local = (event) => {
    const rect = host.getBoundingClientRect();
    if (Math.round(rect.width) !== w || Math.round(rect.height) !== h) resize();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  };
  if (!reduced) {
    const hold = (event) => {
      pointer = local(event);
      held = true;
      lastPointerAt = performance.now();
      kick();
    };
    host.addEventListener("pointermove", hold, { passive: true });
    host.addEventListener("pointerdown", (event) => {
      hold(event);
      ripple = { ...pointer, at: lastPointerAt };
    }, { passive: true });
    // 離す = 緩和の始まり。以前は「最後に触れた時刻を今に置き直す」だけで、離した瞬間から
    // さらに 1.4 秒磁力が掛かり続けてから急に消えていた。
    const release = () => {
      if (!held) return;
      held = false;
      releasedAt = performance.now();
      kick();
    };
    host.addEventListener("pointerleave", release, { passive: true });
    host.addEventListener("pointercancel", release, { passive: true });
    // 指(touch / pen)が上がれば磁石も離れる。マウスはカードの上に居座るので leave まで保つ。
    host.addEventListener("pointerup", (event) => { if (event.pointerType !== "mouse") release(); }, { passive: true });
  }

  resize();

  const handle = {
    host,
    detach() {
      if (raf) win.cancelAnimationFrame(raf);
      raf = 0;
      grains = [];
      canvas.width = 0;            // ビットマップを即座に手放す
      canvas.height = 0;
      canvas.remove();
      attached.delete(host);
    },
  };
  live.add(handle);
  return handle;
}

/** 生きている鉄粉。描画のたびに、DOM から消えたカードのものを片付ける。 */
const live = new Set();

/** container 内の対象カードに鉄粉を付ける(付いていないものだけ)。消えたカードの鉄粉は片付ける。 */
export function attachCardFields(container, selector = ".state-card:not(.empty)", options) {
  if (!container) return 0;
  for (const handle of [...live]) {
    if (handle.host.isConnected === false) { handle.detach(); live.delete(handle); }
  }
  let count = 0;
  container.querySelectorAll(selector).forEach((host) => {
    if (attachCardField(host, options)) count += 1;
  });
  return count;
}
