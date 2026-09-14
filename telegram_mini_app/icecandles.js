/**
 * 価格予測の「氷のろうそく足」。
 *
 * これは相場の予測ではなく **計画そのものの投影** である。描く価格は
 * すべてシナリオが既に持っている 4 つのアンカー(現値 / 建値 / SL / 目標)の
 * 内側にあり、そこから外れる値を発明しない(pathcandles.js と同じ規律)。
 *
 * 形:
 *   第1相 approach — 現値から建値へ戻る足。指値が約定を待つ区間。
 *   第2相 run      — 建値から目標へ向かう足。
 *   ヒゲは実体の 12〜24% の小さなもの(決まった並び、乱数なし)。建値の足だけ SL 側を
 *   少し長くするが、SL との距離の 45% で止める(SL までのヒゲは 2026-09-06 に廃止。
 *   SL はチャートのボックスと線が持つ)。
 *
 * 立体は等角(アイソメ)の三面体で作る。前面 + 上面 + 側面の 3 枚のポリゴンを
 * 面取りして重ね、透明度を落として氷の厚みに見せる。矩形は使わない。
 */

/** ヒゲの長さ(実体に対する比)の並び。乱数を使わず、足ごとに少し違う長さにする。 */
export const WICK_PATTERN = [0.14, 0.22, 0.12, 0.18, 0.24, 0.16];

/** 数値化。有限でなければ null。 */
const num = (value) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};

/**
 * 計画を足の列に落とす。
 *
 * @param {{last:number, entry:number, stop:number, target:number, side:string}} plan
 * @param {number} count 生成する足の本数(2 以上)
 * @returns {Array<{o:number,h:number,l:number,c:number,phase:string}>|null}
 */
export function projectionCandles(plan, count = 6) {
  const last = num(plan?.last), entry = num(plan?.entry);
  const stop = num(plan?.stop), target = num(plan?.target);
  if (last === null || entry === null || stop === null || target === null) return null;
  // 方向は **幾何から導く**。side のラベルは SHORT / SELL / short と揺れるので、
  // SL が建値のどちら側にあるかで決める。ラベルは判定に使わない。
  if (stop === entry) return null;
  const long = stop < entry;
  // 計画が方向として破綻していれば描かない(下流の契約と同じ判定)。
  if (long && !(entry < target)) return null;
  if (!long && !(entry > target)) return null;

  const total = Math.max(2, Math.min(24, Math.floor(count)));
  // approach は全体の 1/3。建値が現値と同じなら approach は不要。
  const approach = Math.abs(entry - last) < 1e-9 ? 0 : Math.max(1, Math.round(total / 3));
  const run = Math.max(1, total - approach);
  const out = [];

  const walk = (from, to, steps, phase) => {
    for (let i = 0; i < steps; i += 1) {
      const o = from + ((to - from) * i) / steps;
      const c = from + ((to - from) * (i + 1)) / steps;
      out.push({ o, h: Math.max(o, c), l: Math.min(o, c), c, phase });
    }
  };

  walk(last, entry, approach, "approach");
  walk(entry, target, run, "run");

  // 小さなヒゲ(2026-09-06 ユーザー: SL まで延びるのは直す、でも少しも無いのは寂しい)。
  // 実体の 12〜24% を決まった並びで上下に足す(乱数は使わない。同じ計画は同じ絵になる)。
  // 建値の足だけ SL 側のヒゲを少し長くして押し戻しを見せるが、SL との距離の 45% まで。
  // どのヒゲも計画のアンカー [lo, hi] の外へは出ない —— 価格を発明しない規律は不変。
  const lo = Math.min(last, entry, stop, target);
  const hi = Math.max(last, entry, stop, target);
  const span = Math.abs(target - last) || Math.abs(entry - stop) || 1;
  const pattern = WICK_PATTERN;
  out.forEach((k, i) => {
    const body = Math.max(Math.abs(k.c - k.o), span * 0.04);
    let up = body * pattern[i % pattern.length];
    let down = body * pattern[(i + 2) % pattern.length];
    if (i === approach) {
      // 建値の足: SL 側だけ長く、ただし SL の手前で止める
      const toStop = Math.abs(entry - stop) * 0.45;
      if (long) down = Math.min(Math.max(down, body * 0.35), toStop);
      else up = Math.min(Math.max(up, body * 0.35), toStop);
    }
    k.h = Math.min(hi, Math.max(k.o, k.c) + up);
    k.l = Math.max(lo, Math.min(k.o, k.c) - down);
  });
  const pivot = out[approach] || out[0];
  if (pivot) pivot.phase = "pivot";
  return out;
}

/** 面取りした前面。角ばった氷にするため矩形ではなく八角形にする。 */
function facePoints(x, top, w, h, chamfer) {
  const c = Math.max(0, Math.min(chamfer, w / 2, h / 2));
  const r = x + w, b = top + h;
  return [
    [x + c, top], [r - c, top], [r, top + c], [r, b - c],
    [r - c, b], [x + c, b], [x, b - c], [x, top + c],
  ].map((p) => p.map((v) => Math.round(v * 10) / 10).join(",")).join(" ");
}

/**
 * 氷のろうそく足を SVG グループへ描く。
 *
 * @param {object} opts
 * @param {Function} opts.make            svgElement(tag, attrs) 相当
 * @param {Array}    opts.candles         projectionCandles() の戻り
 * @param {Function} opts.y               価格→ピクセル
 * @param {Function} opts.xSlot           スロット→ピクセル
 * @param {number}   opts.startSlot       最初の足を置くスロット
 * @param {number}   opts.slotW           スロット幅
 * @param {boolean}  opts.long            買い方向か
 * @param {string}   opts.uid             gradient id の衝突回避用
 * @returns {object} <g class="chart-ice">
 */
export function renderIceCandles({ make, candles, y, x0, spanW, long, uid = "ice" }) {
  const group = make("g", { class: "chart-ice" });
  if (!Array.isArray(candles) || !candles.length) return group;

  const warm = long ? "var(--chart-teal)" : "var(--chart-red)";
  const defs = make("defs", {});
  // 上面が明るく下面が暗い。氷塊の厚みはこの一枚で決まる。
  const faceId = `${uid}-face`, topId = `${uid}-top`, sideId = `${uid}-side`;
  const grad = (id, stops, x2 = "0", y2 = "1") => {
    const g = make("linearGradient", { id, x1: "0", y1: "0", x2, y2 });
    stops.forEach(([offset, color, opacity]) => {
      g.append(make("stop", { offset, "stop-color": color, "stop-opacity": opacity }));
    });
    return g;
  };
  defs.append(grad(faceId, [["0", "#dff3ff", ".42"], ["0.55", warm, ".20"], ["1", warm, ".06"]]));
  defs.append(grad(topId, [["0", "#ffffff", ".55"], ["1", "#dff3ff", ".18"]]));
  defs.append(grad(sideId, [["0", warm, ".26"], ["1", "#04070c", ".34"]], "1", "0"));
  group.append(defs);

  // 未来ゾーンの幅を本数で割って配置する。3分足と同じ細さだと氷の厚みが
  // 1px 未満になって立体が消えるため、ここだけは足幅を独立に決める。
  const pitch = spanW / candles.length;
  const bodyW = Math.max(6, Math.floor(pitch * 0.78));
  const depth = Math.max(3, Math.round(bodyW * 0.42));

  candles.forEach((candle, index) => {
    const cx = Math.round(x0 + pitch * (index + 0.5)) + 0.5;
    const x = Math.round(cx - bodyW / 2);
    const yo = y(candle.o), yc = y(candle.c);
    const top = Math.min(yo, yc);
    const h = Math.max(2, Math.abs(yo - yc));
    const chamfer = Math.min(3, bodyW / 3, h / 3);

    // ヒゲ。実体の外側だけ 2 本(実体は透けるので、貫くと中に線が見える)。
    const yh = y(candle.h), yl = y(candle.l);
    if (top - yh > 0.5) {
      group.append(make("line", {
        class: "ice-wick", x1: cx, x2: cx, y1: yh, y2: top,
        stroke: warm, "stroke-width": 1, "stroke-opacity": ".45"
      }));
    }
    if (yl - (top + h) > 0.5) {
      group.append(make("line", {
        class: "ice-wick", x1: cx, x2: cx, y1: top + h, y2: yl,
        stroke: warm, "stroke-width": 1, "stroke-opacity": ".45"
      }));
    }

    // 三面体は 1 本の <g> にまとめる。回転(1秒周期)はこの g に掛かるので、
    // 面どうしの位置関係が崩れない。
    const solid = make("g", { class: "ice-solid", style: `--ice-i:${index}` });
    // 側面(右) → 上面 → 前面 の順に重ねる。奥から手前へ。
    solid.append(make("polygon", {
      class: "ice-side",
      points: [[x + bodyW, top], [x + bodyW + depth, top - depth],
               [x + bodyW + depth, top + h - depth], [x + bodyW, top + h]]
        .map((p) => p.join(",")).join(" "),
      fill: `url(#${sideId})`, stroke: warm, "stroke-width": ".5", "stroke-opacity": ".35"
    }));
    solid.append(make("polygon", {
      class: "ice-top",
      points: [[x, top], [x + depth, top - depth],
               [x + bodyW + depth, top - depth], [x + bodyW, top]]
        .map((p) => p.join(",")).join(" "),
      fill: `url(#${topId})`, stroke: "#eaf7ff", "stroke-width": ".5", "stroke-opacity": ".5"
    }));
    solid.append(make("polygon", {
      class: `ice-face${candle.phase === "pivot" ? " is-pivot" : ""}`,
      points: facePoints(x, top, bodyW, h, chamfer),
      fill: `url(#${faceId})`, stroke: "#cfeaff",
      "stroke-width": candle.phase === "pivot" ? 1 : ".7",
      "stroke-opacity": candle.phase === "pivot" ? ".9" : ".55"
    }));
    group.append(solid);
  });
  return group;
}

/**
 * ローソク列を **ピクセル矩形** に落とす。3D と SVG のどちらの描画も
 * この一つの配置計算を共有するので、両者の見た目がずれない。
 *
 * @returns {Array<{cx,cy,w,h,wickCy,wickH,pivot}>}
 */
export function iceBlocks({ candles, y, x0, spanW }) {
  if (!Array.isArray(candles) || !candles.length) return [];
  // 未来ゾーンの幅を本数で割る。3分足と同じ細さだと氷の厚みが 1px 未満に
  // なって立体が消えるため、ここだけは足幅を独立に決める。
  const pitch = spanW / candles.length;
  const w = Math.max(6, pitch * 0.8);
  return candles.map((candle, index) => {
    const cx = x0 + pitch * (index + 0.5);
    const yo = y(candle.o), yc = y(candle.c);
    const top = Math.min(yo, yc);
    const h = Math.max(3, Math.abs(yo - yc));
    const yh = y(candle.h), yl = y(candle.l);
    return {
      cx, cy: top + h / 2, w, h,
      closeY: yc,
      wickCy: (yh + yl) / 2,
      wickH: Math.max(h, Math.abs(yl - yh)),
      pivot: candle.phase === "pivot",
      phase: candle.phase,
      // 先の足ほど確度は落ちる。透明度に素直に出す(遠いほど幽か)。
      confidence: 1 - (index / Math.max(1, candles.length - 1)) * 0.55,
    };
  });
}

// ヒゲの分割は wicks.js(依存なし)。3D の icescene.js もそこから読む —— ここから読ませると
// 遅延チャンクが起動チャンクを抱え、verify:build の遅延予算が赤になる(2026-09-06)。
export { wickSegments } from "./wicks.js";

/**
 * 投影の軌道。ブロックの終値を結ぶ 1 本の線。
 *
 * 氷塊だけだと「散らばった板」に見え、計画が **現値 → 建値 → 目標** という
 * ひと続きの筋であることが読めない。線は氷の下に敷く(氷が主役)。
 *
 * @returns {SVGElement|null}
 */
export function renderProjectionPath({ make, blocks, fromX, fromY, long, uid = "path" }) {
  if (!Array.isArray(blocks) || blocks.length < 2) return null;
  const group = make("g", { class: "chart-ice-path" });
  const points = [[fromX, fromY], ...blocks.map((b) => [b.cx, b.closeY])];
  const d = points.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  const warm = long ? "var(--chart-teal)" : "var(--chart-red)";

  const defs = make("defs", {});
  const gradient = make("linearGradient", {
    id: `${uid}-fade`, gradientUnits: "userSpaceOnUse",
    x1: points[0][0], y1: 0, x2: points[points.length - 1][0], y2: 0,
  });
  // 先へ行くほど薄い。確度の減衰を氷と同じ向きで見せる。
  gradient.append(make("stop", { offset: "0", "stop-color": "#dff3ff", "stop-opacity": ".85" }));
  gradient.append(make("stop", { offset: "0.45", "stop-color": warm, "stop-opacity": ".55" }));
  gradient.append(make("stop", { offset: "1", "stop-color": warm, "stop-opacity": ".12" }));
  defs.append(gradient);
  group.append(defs);

  // 太い下敷き(にじみ)+ 細い芯。氷の稜線と同じ冷たい白を芯にする。
  group.append(make("path", {
    class: "ice-path-glow", d, fill: "none", stroke: `url(#${uid}-fade)`,
    "stroke-width": 6, "stroke-linecap": "round", "stroke-linejoin": "round", opacity: ".35",
  }));
  group.append(make("path", {
    class: "ice-path-core", d, fill: "none", stroke: `url(#${uid}-fade)`,
    "stroke-width": 1.4, "stroke-linecap": "round", "stroke-linejoin": "round",
    "stroke-dasharray": "5 4",
  }));
  return group;
}
