/**
 * NQX RESULT — 決済結果の表示層。
 *
 * 出所: files0814/nqx-result-display.html(HANDOFF.md 付属)。
 * 描画コードは原本のまま。統合にあたって変えたのは次だけ。
 *
 *   1. DOM 参照を #resultView の内側に閉じた(document.getElementById → HOST.querySelector)
 *   2. CSS 変数の書き込み先を documentElement から HOST へ(既存 UI の --bone / --mono と衝突するため)
 *   3. `psy` クラスを body ではなく HOST に付ける
 *   4. レビューハーネス(demoPath / DEMO / .dev)を削除 — 合成パスは持ち込まない
 *   5. Telegram シェルと保存シートをモジュールの show()/hide() に組み替え
 *
 * データは呼び出し側が渡す。ここでは state / pnl / R / held を受け取らず、
 * すべて derive() で導出する(HANDOFF §3)。
 */

import { stickerOf } from './stickers.js';
import { candlesFromPath } from './pathcandles.js';
import { buildChartModel, drawTradeChart, drawRationaleChips, metricRows, ringModel, drawRing } from './resultchart.js';

const HOST = document.getElementById('resultView');

/* ═══════════════════════════════════════════════════════════════
   NQX RESULT — DISPLAY LAYER
   Integration surface is a single call:  NQXResult.render(trade)

   ── DATA CONTRACT ──────────────────────────────────────────────
   {
     side:      'LONG' | 'SHORT',
     symbol:    'MNQ',
     qty:       2,                 // contracts
     entry:     29760.00,
     exit:      29796.00,
     stop:      29726.00,          // for realised R
     pointValue:2,                 // USD per point per contract (MNQ = 2)
     openedAt:  '2026-08-13T05:12:00Z',
     closedAt:  '2026-08-13T05:26:20Z',
     path:      [29752.5, 29753.0, ...],   // REAL ticks, entry→exit window
     verdict:   'They let this one through.',
     mode:      'SIMULATION' | 'LIVE'
   }

   `path` MUST come from the Market Truth pipeline
   (renderChart → buildTruthChart). Synthetic paths are barred by
   the project's governance rules. The generator below is DEMO ONLY.

   Everything else — state, P&L, R, hold time — is derived here.
   Do not pass them in; they would drift from the numbers.
   ═══════════════════════════════════════════════════════════════ */

const DISCLOSURE = false;  // flip true to re-print the educational notice on
                           // screen AND on the exported share card.
const COST_FLOOR_PT = 2;   // project rule: MNQ round-trip research cost floor.
                           // inside this band the trade is FLAT, not a win.

const NQXResult = (() => {
  const cv = HOST.querySelector('#hero');
  const gifEl = HOST.querySelector('#hero-gif');
  const $ = id => HOST.querySelector('#' + id);
  let trade = null, grain = null;
  // reduced-motion / 書き出しでは canvas が GIF の1フレーム目を焼くので、
  // 読み込み完了時に一度描き直す(drawScreen は関数宣言なので巻き上がる)
  gifEl.addEventListener('load', () => { if (!HOST.hidden) drawScreen(); });

  /* ---------- derivation ---------- */
  function derive(t){
    const dir = t.side === 'SHORT' ? -1 : 1;
    const pts = (t.exit - t.entry) * dir;
    // fees は滑り+手数料(1口座分)。価格からは出せない観測値なので、
    // 渡ってきていれば必ず引く。ledger.js の deriveTrade と同じ式。
    const fees = Number.isFinite(t.fees) ? Math.abs(t.fees) : 0;
    const usd = pts * t.pointValue * t.qty - fees;
    // R56: stop の無い記録(手動建玉・ブローカー約定由来)は R を出さない(null → —)。
    const risk = Number.isFinite(t.stop) ? Math.abs(t.entry - t.stop) : 0;
    const R = risk ? pts / risk : null;
    const state = Math.abs(pts) <= COST_FLOOR_PT ? 'flat' : (pts > 0 ? 'win' : 'loss');
    const secs = Math.max(0, (new Date(t.closedAt) - new Date(t.openedAt)) / 1000);
    const held = secs >= 3600
      ? `${Math.floor(secs/3600)}h ${String(Math.floor(secs%3600/60)).padStart(2,'0')}m`
      : `${Math.floor(secs/60)}m ${String(Math.round(secs%60)).padStart(2,'0')}s`;
    return { dir, pts, usd, R, state, held };
  }

  const money = n => (n < 0 ? '\u2212' : '+') + '$' +
    Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
  const px = n => n.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});

  const STAMP = { win:'FILED', loss:'REDACTED', flat:'NO ACTION' };
  const TINT  = { win:'#FF3B4F', loss:'#C96A4D', flat:'#8C93A0' };

  /* ═══ hero ═══════════════════════════════════════════════════
     Type 1 は GIF ステッカー背景(2026-08-16 に宇宙プレート版を全廃)。
     seed は VERDICTS と同じ決定論の素 — ステッカー選択にも使う。
     ═════════════════════════════════════════════════════════════ */

  const rng = seed => { let x = (seed>>>0) || 1;
    return () => (x = (x*1664525 + 1013904223)>>>0) / 4294967296; };

  const seedOf = t => {
    let h = 2166136261;
    for (const ch of (t.openedAt + t.entry + t.symbol))
      h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
    return h >>> 0;
  };

  const pad2 = n => String(n).padStart(2,'0');
  function plateId(t){
    const d = new Date(t.openedAt);
    const n = `NQX\u2013${d.getUTCFullYear()%100}${pad2(d.getUTCMonth()+1)}${pad2(d.getUTCDate())}\u2013A`;
    return (bgType === 'eye' ? 'SIGIL ' : 'STICKER ') + n;
  }
  function expWindow(t){
    const o = new Date(t.openedAt), c = new Date(t.closedAt);
    const f = d => `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
    // vigil: the Latin for a night watch. same fact, the type's own register.
    return bgType === 'eye'
      ? `VIGIL ${f(o)}\u2013${f(c)} \u00B7 OBS. NOVA`
      : `EXP ${f(o)}\u2013${f(c)} UT \u00B7 OBS. NOVA`;
  }

  /* four-point diffraction cross — the mark a reflector leaves on a bright star */
  function spikes(c, x, y, len, w, colour){
    c.save(); c.globalCompositeOperation = 'lighter';
    for (let i=0;i<4;i++){
      const a = i * Math.PI/2 + Math.PI/4;
      const g = c.createLinearGradient(x, y, x + Math.cos(a)*len, y + Math.sin(a)*len);
      g.addColorStop(0, colour); g.addColorStop(1, 'rgba(0,0,0,0)');
      c.strokeStyle = g; c.lineWidth = w; c.lineCap = 'round';
      c.beginPath(); c.moveTo(x,y); c.lineTo(x + Math.cos(a)*len, y + Math.sin(a)*len); c.stroke();
    }
    c.restore();
  }

  /* the nova: what flared tonight. sits on the exit price. */
  function novaFlare(c, x, y, u, tint){
    c.save(); c.globalCompositeOperation = 'lighter';
    const halo = c.createRadialGradient(x,y,0,x,y,60*u);
    halo.addColorStop(0, tint+'C0'); halo.addColorStop(.22, tint+'50');
    halo.addColorStop(1, 'rgba(0,0,0,0)');
    c.fillStyle = halo; c.beginPath(); c.arc(x,y,60*u,0,7); c.fill();
    spikes(c, x, y, 96*u, 2.0*u, tint+'D0');
    spikes(c, x, y, 42*u, 1.1*u, 'rgba(255,255,255,.85)');
    c.fillStyle = '#FFFFFF';
    c.beginPath(); c.arc(x,y,3.4*u,0,7); c.fill();
    c.restore();
  }

  function makeGrain(w,h){
    const g = document.createElement('canvas'); g.width=w; g.height=h;
    const gc = g.getContext('2d'), img = gc.createImageData(w,h);
    for (let i=0;i<img.data.length;i+=4){
      const n = 118 + Math.random()*74;
      img.data[i]=img.data[i+1]=img.data[i+2]=n; img.data[i+3]=255;
    }
    gc.putImageData(img,0,0); return g;
  }

  function strokePath(c, pts, dx, dy, colour, width, blur){
    c.save(); c.beginPath();
    pts.forEach((p,i)=> i ? c.lineTo(p.x+dx,p.y+dy) : c.moveTo(p.x+dx,p.y+dy));
    c.lineWidth = width; c.lineJoin = c.lineCap = 'round'; c.strokeStyle = colour;
    if (blur){ c.shadowBlur = blur; c.shadowColor = colour; }
    c.stroke(); c.restore();
  }

  /* ─── TYPE 1 · STICKER ───────────────────────────────────────
     宇宙プレート版(starField / emulsion / renderPlate)は 2026-08-16 に
     全廃し、state 別の GIF ステッカー(#hero-gif、canvas の下)に置換した。
     canvas はここでは透過のまま、可読性スクリムと価格の軌跡・ENTRY/EXIT・
     フレアだけを GIF の上に描く。GIF のアニメーションはブラウザ任せ。
     reduced-motion と書き出し(bake)のときだけ、読み込み済みの
     1 フレーム目を静止画として canvas に焼き込む(img は CSS で非表示)。 */
  function renderGif(target, W, H, time, bake){
    const t = trade, d = derive(t), c = target.getContext('2d');
    target.width = W; target.height = H;
    const u = W/1080, tint = TINT[d.state];

    c.clearRect(0,0,W,H);
    if (bake || reduced){
      // 動かない文脈では canvas が背景も持つ(動く img は出せない)
      c.fillStyle = '#050608'; c.fillRect(0,0,W,H);
      if (gifEl.complete && gifEl.naturalWidth){
        const area = H*0.56, gw = gifEl.naturalWidth, gh = gifEl.naturalHeight;
        const s = Math.max(W/gw, area/gh), dw = gw*s, dh = gh*s;
        c.save(); c.globalAlpha = .92;
        c.drawImage(gifEl, (W-dw)/2, (area-dh)/2, dw, dh);
        c.restore();
      }
    }

    // 可読性スクリム(GIF の上・軌跡の下)。下半分は既存の .scrim が受け持つ
    const sg = c.createLinearGradient(0,0,0,H*0.62);
    sg.addColorStop(0,'rgba(4,5,7,.45)'); sg.addColorStop(.5,'rgba(4,5,7,.08)');
    sg.addColorStop(1,'rgba(4,5,7,.85)');
    c.fillStyle = sg; c.fillRect(0,0,W,H*0.62);

    /* ═══ R57: 根拠チャート ═══
       result.chart(建玉前後の確定 3 分足・VP 水準・凍結ターゲット・根拠タグ)が
       あれば、それを描く。「なぜ入って、どこで守り、どこを狙い、何が起きたか」を
       一枚で読ませる。無い旧 result は従来のローソク/トレイル描画に落ちる。 */
    const chartModel = buildChartModel(t);
    if (chartModel){
      // 画面では canvas 1px = 1/dpr css px。ラベルは 10 css px を下限に読めるようにする。
      // 書き出し(bake, 1080 幅)は u=1 なので下限は効かない。
      const dpr = bake ? 1 : Math.max(1, W / Math.max(1, cv.clientWidth || W));
      drawTradeChart(c, chartModel,
        { x: W*0.035, y: H*0.08, w: W*0.93, h: H*0.30 },
        { u, tint, minPx: bake ? 17 : 10 * dpr, flare: (cc, x, y) => novaFlare(cc, x, y, u, tint) });
      return;
    }

    /* ═══ ローソク足+前方の SL/EXIT ボックス(2026-08-16 刷新)═══
       path をリサンプルした確定形のローソクで描く(pathcandles.js)。
       点が両端しか無い記録はローソクにならないので従来トレイルに落ちる。
       ズームは「ボックスに寄りすぎず離れすぎず」— 全要素(ローソク+
       ENTRY/EXIT/SL)を含めた範囲に 28% の余白を足した中間距離で固定。
       ONE price→y mapping はローソクと基準線で共有する(ズレの温床)。 */
    const stop = Number.isFinite(t.stop) ? t.stop : null;
    const candles = candlesFromPath(series_(t));
    const anchors = [t.entry, t.exit].concat(stop !== null ? [stop] : []);
    const plotX0 = W*0.035, plotX1 = W*0.965;          // ローソク帯(全幅)
    const top = H*0.075, band = H*0.36;

    let lo, hi;
    if (candles){
      lo = Math.min(...candles.map(k => k.l), ...anchors);
      hi = Math.max(...candles.map(k => k.h), ...anchors);
    } else {
      const series = series_(t);
      lo = Math.min(...series, ...anchors);
      hi = Math.max(...series, ...anchors);
    }
    const pad = (hi-lo || 1) * 0.28; lo -= pad; hi += pad;
    const yOf = p => top + band * (1 - (p-lo)/(hi-lo));

    /* ボックスは保有期間そのもの(ローソク帯の全幅)に敷く。
       entry↔stop = 受け入れたリスク(rust)、entry↔exit = 結果(state色)。
       ローソクの背面に置き、上に描くローソクが主役のまま読めるようにする。 */
    const yEntry = yOf(t.entry), yExit = yOf(t.exit);
    c.save();
    if (stop !== null){
      const yStop = yOf(stop);
      c.fillStyle = 'rgba(255,46,76,.12)';
      c.fillRect(plotX0, Math.min(yEntry, yStop), plotX1-plotX0, Math.max(1, Math.abs(yEntry-yStop)));
      c.strokeStyle = 'rgba(255,46,76,.45)'; c.lineWidth = 1*u;
      c.strokeRect(plotX0, Math.min(yEntry, yStop), plotX1-plotX0, Math.max(1, Math.abs(yEntry-yStop)));
    }
    c.fillStyle = tint + '1E';
    c.fillRect(plotX0, Math.min(yEntry, yExit), plotX1-plotX0, Math.max(1, Math.abs(yEntry-yExit)));
    c.strokeStyle = tint + '90'; c.lineWidth = 1.1*u;
    c.strokeRect(plotX0, Math.min(yEntry, yExit), plotX1-plotX0, Math.max(1, Math.abs(yEntry-yExit)));
    c.restore();

    /* インク&エンバー(メインフィードと同じ言語): 陽線=骨白のホロー、
       陰線=ラストの塗り。旧トレイルの色グローは shadow で移植。
       背面 GIF が透けつつローソクも読める透過度に調整。 */
    const UP = '#ECE9DF', DOWN = '#FF2E4C';
    let lastX, lastY;
    if (candles){
      const n = candles.length;
      const slotW = (plotX1 - plotX0) / n;
      const bodyW = Math.max(2.6*u, slotW*0.56);
      c.save();
      candles.forEach((k, i) => {
        const cx = plotX0 + (i + 0.5) * slotW;
        const up = k.c >= k.o;
        const col = up ? UP : DOWN;
        c.shadowBlur = 7*u; c.shadowColor = col;
        c.globalAlpha = up ? .55 : .62;
        c.strokeStyle = col; c.lineWidth = Math.max(1, 1.1*u); c.lineCap = 'round';
        c.beginPath(); c.moveTo(cx, yOf(k.h)); c.lineTo(cx, yOf(k.l)); c.stroke();
        const yTop = Math.min(yOf(k.o), yOf(k.c));
        const bodyH = Math.max(1.4*u, Math.abs(yOf(k.o) - yOf(k.c)));
        if (up){
          c.globalAlpha = .5;                     // ホローの中は GIF が透ける
          c.fillStyle = 'rgba(9,10,12,.6)';
          c.fillRect(cx - bodyW/2, yTop, bodyW, bodyH);
          c.globalAlpha = .92;
          c.lineWidth = Math.max(1, 1.2*u);
          c.strokeRect(cx - bodyW/2, yTop, bodyW, bodyH);
        } else {
          c.globalAlpha = .88;
          c.fillStyle = col;
          c.fillRect(cx - bodyW/2, yTop, bodyW, bodyH);
        }
      });
      c.restore();
      lastX = plotX0 + (n - 0.5) * slotW;
      lastY = yOf(candles[n-1].c);
    } else {
      const series = series_(t);
      const pts = series.map((v, i) => ({
        x: plotX0 + (i/(series.length-1))*(plotX1-plotX0), y: yOf(v)
      }));
      c.globalCompositeOperation = 'lighter';
      strokePath(c, pts, 0, 0, tint+'26', 20*u, 64*u);
      strokePath(c, pts, 0, 0, '#F2F6FF', 2.2*u, 13*u);
      c.globalCompositeOperation = 'source-over';
      lastX = pts[pts.length-1].x;
      lastY = pts[pts.length-1].y;
    }

    const marks = [
      ['ENTRY', yEntry, 'rgba(233,228,214,.40)'],
      ['EXIT',  yExit,  tint+'B0'],
    ];
    if (stop !== null) marks.push(['SL', yOf(stop), 'rgba(255,46,76,.60)']);
    // ラベルの重なりは縦に少しずつ逃がす(価格が近いときに読めなくなる)
    marks.sort((a, b) => a[1] - b[1]);
    let prevY = -Infinity;
    marks.forEach(([lab, rawY, col]) => {
      const yLine = rawY;
      c.strokeStyle = col; c.lineWidth = 1*u;
      c.setLineDash([2*u, 12*u]);
      c.beginPath(); c.moveTo(0, yLine); c.lineTo(W, yLine); c.stroke(); c.setLineDash([]);
      const labelY = Math.max(yLine - 11*u, prevY + 20*u);
      prevY = labelY;
      c.font = `500 ${16*u}px ui-monospace,monospace`;
      c.fillStyle = col; c.textAlign = 'right';
      c.fillText(lab, W - 48*u, labelY); c.textAlign = 'left';
    });

    novaFlare(c, lastX, lastY, u, tint);
  }

  /* ═══ TYPE 2 · EYE ════════════════════════════════════════════
     Skeleton is Klüver's form constants — the four geometries that
     recur across hallucinatory reports: tunnel, lattice, cobweb,
     spiral. Colour follows Fillmore-poster practice: adjacent
     complementaries at matched luminance, so the edges vibrate.
     Depth comes from oil-wheel liquid light and moiré interference.
     Surface is blotter paper, ink bleed and a halftone screen.

     The trade is still in the halo — ray length is the price path in
     polar coordinates. The mandala is symmetric; the data is not.
     ═════════════════════════════════════════════════════════════ */

  const GOLD = { core:'#FFF3D2', mid:'#E8C15A', deep:'#8A6A1E' };
  const PHI = (1 + Math.sqrt(5)) / 2;
  const hsl = (h,s,l,a=1) => `hsla(${((h%360)+360)%360} ${s}% ${l}% / ${a})`;
  /* colour wheels, built once. hsl() strings are cheap to write and
     surprisingly expensive to parse 400 times a frame. */
  const WHEEL = { n:96, s:[], m:[], hot:[] };
  for (let i=0;i<WHEEL.n;i++){
    const h = i/WHEEL.n*360;
    WHEEL.s.push(hsl(h,90,62)); WHEEL.m.push(hsl(h,88,52)); WHEEL.hot.push(hsl(h,96,78));
  }
  const wheel = (arr,h) => arr[((Math.round(h/360*WHEEL.n)%WHEEL.n)+WHEEL.n)%WHEEL.n];

  function rot3(p, ax, ay, az){
    let {x,y,z} = p, s, cs;
    s = Math.sin(ax); cs = Math.cos(ax); [y,z] = [y*cs - z*s, y*s + z*cs];
    s = Math.sin(ay); cs = Math.cos(ay); [x,z] = [x*cs + z*s, -x*s + z*cs];
    s = Math.sin(az); cs = Math.cos(az); [x,y] = [x*cs - y*s, x*s + y*cs];
    return {x,y,z};
  }
  const project = (p, cx, cy, k, f=3.2) => {
    const s = f / (f + p.z);
    return { x: cx + p.x*k*s, y: cy + p.y*k*s, s };
  };

  /* Static layers are built once and then only transformed each frame.
     Rebuilding the moiré grid per frame is what would make this crawl. */
  const cache = {};
  function cached(key, W, H, build){
    const id = key + W + 'x' + H;
    if (cache[id]) return cache[id];
    const cv2 = document.createElement('canvas');
    cv2.width = W; cv2.height = H;
    build(cv2.getContext('2d'), W, H);
    Object.keys(cache).filter(k => k.startsWith(key)).forEach(k => delete cache[k]);
    return (cache[id] = cv2);
  }

  /* moiré: fine concentric + radial rules. two copies, counter-rotated,
     beat against each other. this is the layer that feels alive. */
  const MO = 0.6;
  const moireLayer = (W,H) => cached('moire', Math.ceil(W*MO), Math.ceil(H*MO), (c,w,h) => {
    const cx = w/2, cy = h/2, u = w/1080, R = Math.hypot(w,h);
    c.strokeStyle = 'rgba(255,255,255,.5)';
    c.lineWidth = 0.8*u;
    for (let r = 6*u; r < R; r += 7.5*u){ c.beginPath(); c.arc(cx,cy,r,0,7); c.stroke(); }
    c.lineWidth = 0.7*u;
    for (let i=0;i<220;i++){
      const a = i/220*Math.PI*2;
      c.beginPath(); c.moveTo(cx,cy); c.lineTo(cx+Math.cos(a)*R, cy+Math.sin(a)*R); c.stroke();
    }
  });

  /* halftone screen — the surface the whole thing is printed on */
  const screenLayer = (W,H) => cached('screen', W, H, (c,w,h) => {
    const u = w/1080, p = 5.4*u;
    c.fillStyle = '#000';
    for (let y=0;y<h;y+=p) for (let x=0;x<w;x+=p){
      c.beginPath(); c.arc(x + (y/p%2)*p/2, y, p*0.30, 0, 7); c.fill();
    }
  });

  /* oil-wheel projection: warm volumes drifting behind everything */
  function liquidLight(c, W, H, T, seed, tint){
    const r = rng(seed ^ 0x2545F491);
    c.save(); c.globalCompositeOperation = 'lighter';
    for (let i=0;i<7;i++){
      const ph = r()*6.3, sp = 0.10 + r()*0.22, amp = 0.16 + r()*0.20;
      const x = W*(0.5 + Math.sin(T*sp + ph)*amp + (r()-0.5)*0.30);
      const y = H*(0.28 + Math.cos(T*sp*0.8 + ph)*amp*0.7);
      const rad = W*(0.20 + r()*0.30);
      const h = (T*9 + i*47 + seed%360);
      const g = c.createRadialGradient(x,y,0,x,y,rad);
      g.addColorStop(0,   hsl(h, 96, 60, .52));
      g.addColorStop(.42, hsl(h+34, 92, 50, .24));
      g.addColorStop(1,   'rgba(0,0,0,0)');
      c.fillStyle = g; c.beginPath(); c.arc(x,y,rad,0,7); c.fill();
    }
    const core = c.createRadialGradient(W/2, H*0.245, 0, W/2, H*0.245, W*0.5);
    core.addColorStop(0, tint+'30'); core.addColorStop(1,'rgba(0,0,0,0)');
    c.fillStyle = core; c.fillRect(0,0,W,H);
    c.restore();
  }

  /* form constant #1 — the tunnel. polygons receding on a log scale,
     so the eye reads it as endless approach rather than a stack of rings. */
  function tunnel(c, cx, cy, R, T, u, seed){
    c.save(); c.globalCompositeOperation = 'lighter';
    const N = 22, sides = 6;
    for (let i=0;i<N;i++){
      const z  = ((i/N) + (T*0.055)) % 1;
      const sc = Math.pow(z, 2.1) * 7.4;
      const rr = R * sc;
      if (rr < R*0.10 || rr > R*7) continue;
      const fade = Math.min(1, z*3.2) * (1 - z*0.82);
      const spin = T*0.10 + z*2.4 + (seed%100)/100;
      c.strokeStyle = hsl(T*13 + z*220 + seed%360, 98, 62, fade*0.80);
      c.lineWidth = (0.8 + fade*3.0)*u;
      c.beginPath();
      for (let k=0;k<=sides;k++){
        const a = k/sides*Math.PI*2 + spin;
        const x = cx + Math.cos(a)*rr, y = cy + Math.sin(a)*rr;
        k ? c.lineTo(x,y) : c.moveTo(x,y);
      }
      c.stroke();
    }
    c.restore();
  }

  /* form constant #2 — the lattice, mirrored 12-fold into a mandala */
  function mandala(c, cx, cy, R, T, u, seed){
    const F = 12;
    c.save(); c.translate(cx, cy); c.globalCompositeOperation = 'lighter';
    c.rotate(T*0.045 + (seed%100)/60);
    for (let f=0; f<F; f++){
      c.save(); c.rotate(f/F*Math.PI*2); if (f%2) c.scale(1,-1);
      for (let i=0;i<7;i++){
        const rr = R*(0.62 + i*0.30), w = Math.PI/F*0.92;
        c.strokeStyle = hsl(T*11 + i*38 + f*4 + seed%360, 94, 64, 0.38 - i*0.034);
        c.lineWidth = (2.1 - i*0.20)*u;
        c.beginPath();
        c.moveTo(Math.cos(-w)*rr*0.5, Math.sin(-w)*rr*0.5);
        c.quadraticCurveTo(rr*1.18, 0, Math.cos(w)*rr*0.5, Math.sin(w)*rr*0.5);
        c.stroke();
      }
      c.restore();
    }
    c.restore();
  }

  function nodeStar(c, x, y, r, a, colour){
    c.save(); c.globalCompositeOperation = 'lighter';
    const g = c.createRadialGradient(x,y,0,x,y,r*2.6);
    g.addColorStop(0, colour); g.addColorStop(1,'rgba(0,0,0,0)');
    c.fillStyle = g; c.globalAlpha = a*0.7;
    c.beginPath(); c.arc(x,y,r*2.6,0,7); c.fill();
    c.globalAlpha = a; c.lineCap='round';
    for (let i=0;i<2;i++){
      const ang = i*Math.PI/2;
      const lg = c.createLinearGradient(x-Math.cos(ang)*r*4, y-Math.sin(ang)*r*4,
                                        x+Math.cos(ang)*r*4, y+Math.sin(ang)*r*4);
      lg.addColorStop(0,'rgba(0,0,0,0)'); lg.addColorStop(.5,colour); lg.addColorStop(1,'rgba(0,0,0,0)');
      c.strokeStyle = lg; c.lineWidth = r*0.5;
      c.beginPath();
      c.moveTo(x-Math.cos(ang)*r*4, y-Math.sin(ang)*r*4);
      c.lineTo(x+Math.cos(ang)*r*4, y+Math.sin(ang)*r*4);
      c.stroke();
    }
    c.restore();
  }

  function orbit(c, cx, cy, k, R, tilt, yaw, spin, dots, u, alpha, T){
    c.save(); c.globalCompositeOperation = 'lighter';
    for (let i=0;i<dots;i++){
      const a = (i/dots)*Math.PI*2 + spin;
      const p = project(rot3({x:Math.cos(a)*R, y:Math.sin(a)*R, z:0}, tilt, yaw, 0), cx, cy, k);
      const depth = (p.s - 0.72) / 0.56;
      const r = (0.9 + depth*1.9) * u;
      if (r <= 0) continue;
      c.globalAlpha = alpha * (0.18 + depth*0.82);
      c.fillStyle = wheel(WHEEL.s, T*13 + i*3.2);
      c.beginPath(); c.arc(p.x, p.y, r, 0, 7); c.fill();
    }
    c.restore();
  }

  function icosa(c, cx, cy, k, R, rx, ry, u, alpha, T){
    const V = [];
    [[0,1,PHI],[0,-1,PHI],[0,1,-PHI],[0,-1,-PHI]].forEach(([x,y,z])=>{
      V.push({x,y,z}, {x:y,y:z,z:x}, {x:z,y:x,z:y});
    });
    const n = Math.hypot(1,PHI);
    const P = V.map(v => project(rot3({x:v.x/n*R, y:v.y/n*R, z:v.z/n*R}, rx, ry, 0), cx, cy, k));
    const L = R*2.1/n;
    c.save(); c.globalCompositeOperation = 'lighter';
    for (let i=0;i<P.length;i++) for (let j=i+1;j<P.length;j++){
      const d3 = Math.hypot(V[i].x-V[j].x, V[i].y-V[j].y, V[i].z-V[j].z) / n * R;
      if (Math.abs(d3 - L) > R*0.06) continue;
      const a = P[i], b2 = P[j];
      const depth = Math.max(0, ((a.s + b2.s)/2 - 0.72) / 0.56);
      c.globalAlpha = alpha * (0.12 + depth*0.62);
      c.strokeStyle = wheel(WHEEL.m, T*10 + i*17);
      c.lineWidth = (0.5 + depth*1.2)*u;
      c.beginPath(); c.moveTo(a.x,a.y); c.lineTo(b2.x,b2.y); c.stroke();
    }
    P.forEach((p,i) => {
      const depth = (p.s - 0.72)/0.56;
      if (depth > 0.42) nodeStar(c, p.x, p.y, (1.6+depth*2.3)*u, alpha*depth, wheel(WHEEL.hot, T*10+i*29));
    });
    c.restore();
  }

  /* the halo: price path in polar coordinates, split into three inks
     so the edges vibrate the way a screen-printed poster does.
     Built once into an offscreen and then only rotated — rebuilding
     540 gradients every frame was what made this crawl. */
  function haloLayer(D, r0, r1, series, tint, u, seed){
    return cached('halo' + seed + Math.round(r1), D, D, (c) => {
      const N = 180, cx = D/2, cy = D/2;
      let lo = Math.min(...series), hi = Math.max(...series);
      const rg = (hi - lo) || 1;
      c.globalCompositeOperation = 'lighter';
      const inks = [[-1.8, hsl(seed % 360 + 200, 98, 60)],
                    [ 1.8, hsl(seed % 360 + 330, 98, 60)],
                    [ 0,   null]];
      inks.forEach(([dx, ink])=>{
        for (let i=0;i<N;i++){
          const a2 = (i/N)*Math.PI*2;
          const k = (series[Math.floor(i/N*(series.length-1))] - lo) / rg;
          const long = i % 2 === 0;
          const len = long ? r0 + (r1-r0)*(0.28 + k*0.72) : r0 + (r1-r0)*0.18;
          const ox = Math.cos(a2+Math.PI/2)*dx*u, oy = Math.sin(a2+Math.PI/2)*dx*u;
          const x0 = cx + Math.cos(a2)*r0*0.72 + ox, y0 = cy + Math.sin(a2)*r0*0.72 + oy;
          const x1 = cx + Math.cos(a2)*len + ox,     y1 = cy + Math.sin(a2)*len + oy;
          const g = c.createLinearGradient(x0,y0,x1,y1);
          if (ink){ g.addColorStop(0, ink); g.addColorStop(.7,'rgba(0,0,0,0)'); }
          else {
            g.addColorStop(0, long ? tint : GOLD.mid);
            g.addColorStop(.30, GOLD.core); g.addColorStop(.64, GOLD.mid+'70');
            g.addColorStop(1, 'rgba(0,0,0,0)');
          }
          c.strokeStyle = g;
          c.lineWidth = (long ? 2.0 : 1.0) * u;
          c.globalAlpha = (long ? 0.85 : 0.40) * (ink ? 0.5 : 1);
          c.lineCap = 'round';
          c.beginPath(); c.moveTo(x0,y0); c.lineTo(x1,y1); c.stroke();
        }
      });
    });
  }

  /* ─── the figure ──────────────────────────────────────────────
     Two things were missing: mass and life.

     Mass: the triangle is a real 3D frame that turns slightly. The
     band between its outer and inner rim is treated as a bevel and
     shaded per edge by Lambert against a moving light, so it reads
     as cast metal rather than a stroked path.

     Life: eyes are alive because they saccade — hold, then jump.
     Plus pupil dilation, a blink, a limbal ring, and a corneal
     highlight fixed to the light rather than to the iris, which is
     what makes a drawn eye read as a wet sphere.
     ───────────────────────────────────────────────────────────── */

  function gazeAt(T, seed){
    const per = 2.35;
    const pt = n => { const r = rng((seed ^ (n*2654435761)) >>> 0); r();
                      return { x:(r()-0.5)*0.44, y:(r()-0.5)*0.24 }; };
    const Tp = Math.max(0, T);
    const i = Math.floor(Tp/per), f = (Tp % per) / per;
    const p0 = pt(i), p1 = pt(i+1);
    const k = Math.min(1, f/0.085), e = k*k*(3-2*k);      // fast jump, long hold
    return { x:p0.x + (p1.x-p0.x)*e, y:p0.y + (p1.y-p0.y)*e,
             sac: f < 0.085 ? 1 - f/0.085 : 0 };
  }

  /* the aperture, sampled so its rim can be offset and shaded like a
     real cut edge. a stroked path has no thickness and therefore no depth. */
  function almond(cx, cy, ew, eh, N){
    const q=(p0,p1,p2,t)=>({ x:(1-t)*(1-t)*p0.x + 2*(1-t)*t*p1.x + t*t*p2.x,
                             y:(1-t)*(1-t)*p0.y + 2*(1-t)*t*p1.y + t*t*p2.y });
    const A={x:cx-ew,y:cy}, B={x:cx,y:cy-eh*1.58},
          C={x:cx+ew,y:cy}, D={x:cx,y:cy+eh*1.58};
    const p=[];
    for(let i=0;i<N;i++) p.push(q(A,B,C,i/N));
    for(let i=0;i<N;i++) p.push(q(C,D,A,i/N));
    return p;
  }
  const tracePath = (c,p) => { c.beginPath(); c.moveTo(p[0].x,p[0].y);
    for(let i=1;i<p.length;i++) c.lineTo(p[i].x,p[i].y); c.closePath(); };

  function thirdEye(c, cx, cy, R, tint, u, T, alpha, depth, seed){
    const breathe = Math.sin(T*0.9);
    const s = R * (1 + breathe*0.010);

    /* the frame turns. real 3D — vertices go through the same rotation
       and perspective divide as everything else in the scene. */
    const yaw = Math.sin(T*0.33 + seed%7)*0.15, pit = Math.sin(T*0.24)*0.075;
    const k3 = s*0.95;   // circumradius ≈ 1.0s once the 1.06 factor is applied
    const V = k => [0, 1, 2].map(i => {
      const a2 = -Math.PI/2 + i*Math.PI*2/3;
      return project(rot3({ x:Math.cos(a2)*1.06*k, y:Math.sin(a2)*1.06*k, z:0 },
                          pit, yaw, 0), cx, cy, k3);
    });
    const O = V(1.0), I = V(0.845);

    const la = -2.25 + Math.sin(T*0.25)*0.55;              // light, slowly swinging
    const L = { x:Math.cos(la), y:Math.sin(la) };

    const tri = P => { c.beginPath(); c.moveTo(P[0].x,P[0].y);
      c.lineTo(P[1].x,P[1].y); c.lineTo(P[2].x,P[2].y); c.closePath(); };

    // ── ink bleed + poster misregistration
    c.save(); c.globalCompositeOperation = 'lighter';
    c.strokeStyle = GOLD.mid; c.lineWidth = 13*u; c.globalAlpha = alpha*0.13;
    tri(O); c.stroke();
    c.globalAlpha = alpha*0.30; c.lineWidth = 3*u;
    c.strokeStyle = wheel(WHEEL.s, T*15+310);
    c.save(); c.translate(-1.9*u, 1.3*u); tri(O); c.stroke(); c.restore();
    c.strokeStyle = wheel(WHEEL.s, T*15+190);
    c.save(); c.translate(1.9*u, -1.3*u); tri(O); c.stroke(); c.restore();
    c.restore();

    // ── recessed field: the eye is set INTO something
    c.save(); c.globalAlpha = alpha;
    tri(I); c.clip();
    c.fillStyle = 'rgba(9,5,17,.72)';
    c.fillRect(cx-s*1.4, cy-s*1.4, s*2.8, s*2.8);
    c.lineWidth = 0.7*u;
    for (let y=-s, i=0; y<s; y += 5*u, i++){
      c.strokeStyle = wheel(WHEEL.m, T*12 + i*7);
      c.globalAlpha = alpha*0.34;
      c.beginPath(); c.moveTo(cx-s*1.4, cy+y); c.lineTo(cx+s*1.4, cy+y); c.stroke();
    }
    c.globalAlpha = alpha;
    const inner = c.createRadialGradient(cx, cy+s*0.1, s*0.2, cx, cy+s*0.1, s*1.3);
    inner.addColorStop(0,'rgba(0,0,0,0)'); inner.addColorStop(1,'rgba(0,0,0,.70)');
    c.fillStyle = inner; c.fillRect(cx-s*1.4, cy-s*1.4, s*2.8, s*2.8);
    c.restore();

    // ── the bevel: three quads, Lambert-shaded. this is the mass.
    c.save(); c.globalAlpha = alpha;
    for (let e=0;e<3;e++){
      const o0=O[e], o1=O[(e+1)%3], i0=I[e], i1=I[(e+1)%3];
      const ex=o1.x-o0.x, ey=o1.y-o0.y, len=Math.hypot(ex,ey)||1;
      let nx = ey/len, ny = -ex/len;                        // outward normal
      const mx=(o0.x+o1.x)/2-cx, my=(o0.y+o1.y)/2-cy;
      if (nx*mx + ny*my < 0){ nx=-nx; ny=-ny; }
      const lam = Math.max(0, nx*L.x + ny*L.y);
      const lit = 0.16 + Math.pow(lam, 1.5)*0.84;
      const g = c.createLinearGradient(
        (o0.x+o1.x)/2, (o0.y+o1.y)/2, (i0.x+i1.x)/2, (i0.y+i1.y)/2);
      g.addColorStop(0,   `rgba(${(255*lit)|0},${(238*lit)|0},${(196*lit)|0},1)`);
      g.addColorStop(.34, `rgba(${(232*lit)|0},${(193*lit)|0},${(90*lit)|0},1)`);
      g.addColorStop(1,   `rgba(${(74*lit+16)|0},${(56*lit+12)|0},${(18*lit+8)|0},1)`);
      c.fillStyle = g;
      c.beginPath(); c.moveTo(o0.x,o0.y); c.lineTo(o1.x,o1.y);
      c.lineTo(i1.x,i1.y); c.lineTo(i0.x,i0.y); c.closePath(); c.fill();

      // specular sweep travelling along the lit edge
      const sw = ((T*0.22 + e*0.37) % 1);
      c.save(); c.beginPath(); c.moveTo(o0.x,o0.y); c.lineTo(o1.x,o1.y);
      c.lineTo(i1.x,i1.y); c.lineTo(i0.x,i0.y); c.closePath(); c.clip();
      const sx = o0.x + ex*sw, sy = o0.y + ey*sw;
      const sg = c.createRadialGradient(sx,sy,0,sx,sy,len*0.34);
      sg.addColorStop(0, `rgba(255,252,238,${(0.55*lam).toFixed(3)})`);
      sg.addColorStop(1, 'rgba(0,0,0,0)');
      c.globalCompositeOperation = 'lighter'; c.fillStyle = sg;
      c.beginPath(); c.arc(sx,sy,len*0.34,0,7); c.fill();
      c.restore();
    }
    c.strokeStyle = 'rgba(28,18,4,.9)'; c.lineWidth = 1.1*u;
    tri(O); c.stroke(); tri(I); c.stroke();
    c.strokeStyle = GOLD.core; c.lineWidth = 1.0*u; c.globalAlpha = alpha*0.75;
    tri(O); c.stroke();
    c.restore();

    // ── the eye, set into a cut socket
    const g0 = gazeAt(T, seed);
    const ew = s*0.635, eh = s*0.288, ey0 = cy + s*0.10;
    const ir = s*0.288*1.08;
    const gx = cx + g0.x*ir*0.52, gy = ey0 + g0.y*ir*0.44;

    const N = depth > 0 ? 40 : 20, n2 = N*2, rimW = ir*0.20;
    const OUT = almond(cx, ey0, ew, eh, N);
    const NRM = OUT.map((_,i)=>{
      const p0=OUT[(i-1+n2)%n2], p1=OUT[(i+1)%n2];
      const dx=p1.x-p0.x, dy=p1.y-p0.y, l=Math.hypot(dx,dy)||1;
      return { x:dy/l, y:-dx/l };                       // outward
    });
    // rim thins toward the corners, the way a socket actually does
    const WID = OUT.map(p => rimW*(0.26 + 0.74*(1 - Math.pow(Math.min(1,Math.abs(p.x-cx)/ew), 2))));
    const INN = OUT.map((p,i)=>({ x:p.x - NRM[i].x*WID[i], y:p.y - NRM[i].y*WID[i] }));

    c.save(); c.globalAlpha = alpha;

    // eyeball, clipped to the inner opening
    tracePath(c, INN); c.save(); c.clip();
    const sc = c.createRadialGradient(cx - ir*0.3, ey0 - ir*0.3, ir*0.1, cx, ey0, ir*2.2);
    sc.addColorStop(0,'#2A2338'); sc.addColorStop(.5,'#14101F'); sc.addColorStop(1,'#07050C');
    c.fillStyle = sc; c.fillRect(cx-ew, ey0-eh*2, ew*2, eh*4);

    const g = c.createRadialGradient(gx, gy, 0, gx, gy, ir);
    g.addColorStop(0,'#FFFFFF');
    g.addColorStop(.15, tint);
    g.addColorStop(.36, wheel(WHEEL.s, T*15+40));
    g.addColorStop(.62, wheel(WHEEL.m, T*15+330));
    g.addColorStop(.94, wheel(WHEEL.m, T*15+265));
    g.addColorStop(1,  'rgba(6,3,14,1)');
    c.fillStyle = g; c.beginPath(); c.arc(gx, gy, ir, 0, 7); c.fill();

    c.strokeStyle = 'rgba(14,8,26,.55)'; c.lineWidth = 0.9*u;
    for (let i=1;i<8;i++){ c.beginPath(); c.arc(gx, gy, ir*i/8, 0, 7); c.stroke(); }
    for (let i=0;i<48;i++){
      const a2 = i/48*Math.PI*2 + T*0.10;
      c.beginPath(); c.moveTo(gx+Math.cos(a2)*ir*0.34, gy+Math.sin(a2)*ir*0.34);
      c.lineTo(gx+Math.cos(a2)*ir, gy+Math.sin(a2)*ir); c.stroke();
    }
    c.strokeStyle = 'rgba(4,2,10,.92)'; c.lineWidth = ir*0.11;   // limbal ring
    c.beginPath(); c.arc(gx, gy, ir*0.945, 0, 7); c.stroke();

    const pr = Math.max(ir*0.16, ir*(0.335 + Math.sin(T*1.15)*0.045 - g0.sac*0.055));
    c.fillStyle = '#04020A';
    c.beginPath(); c.arc(gx, gy, pr, 0, 7); c.fill();
    if (depth > 0){
      c.save(); c.beginPath(); c.arc(gx, gy, pr, 0, 7); c.clip();
      thirdEye(c, gx, gy + pr*0.06, pr*0.86, tint, u, T*1.45 + 11.3, alpha*0.95,
               depth-1, seed ^ 0x51ED270B);
      c.restore();
    }

    /* Directional inner shadow. The rim on the lit side throws across the
       eyeball; the corners go deepest because the opening is narrowest
       there. Built from stacked offset strokes — no filters, so it works
       in every WebView. */
    const off = ir*0.235;
    for (let k=0;k<6;k++){
      const f2 = k/5;
      c.save();
      c.translate(-L.x*off*(0.35+f2*0.65), -L.y*off*(0.35+f2*0.65));
      tracePath(c, INN);
      c.strokeStyle = `rgba(0,0,0,${(0.045 + f2*0.105).toFixed(3)})`;
      c.lineWidth = ir*(0.80 - f2*0.60);
      c.stroke();
      c.restore();
    }
    // bounce light on the shadowed wall, so the socket is not a flat black
    c.save(); c.globalCompositeOperation = 'lighter';
    c.translate(L.x*off*0.7, L.y*off*0.7);
    tracePath(c, INN);
    c.strokeStyle = 'rgba(158,128,66,.24)'; c.lineWidth = ir*0.30; c.stroke();
    c.restore();

    // corneal highlight — anchored to the light, not the iris
    c.globalCompositeOperation = 'lighter';
    const hx = cx + L.x*ir*0.42, hy = ey0 + L.y*ir*0.34;
    const hg = c.createRadialGradient(hx, hy, 0, hx, hy, ir*0.34);
    hg.addColorStop(0,'rgba(255,255,255,.95)'); hg.addColorStop(1,'rgba(255,255,255,0)');
    c.fillStyle = hg; c.beginPath(); c.arc(hx, hy, ir*0.34, 0, 7); c.fill();
    c.restore();                                     // end eyeball clip

    /* The rim itself, shaded segment by segment against the same light as
       the frame. This is the depth that a stroked outline cannot give. */
    for (let i=0;i<n2;i++){
      const j=(i+1)%n2, o0=OUT[i], o1=OUT[j], i0=INN[i], i1=INN[j];
      const lam = Math.max(0, NRM[i].x*L.x + NRM[i].y*L.y);
      const lit = 0.22 + Math.pow(lam,1.3)*0.78;
      // cool bounce on the faces turned away, so the rim never reads as a hole
      const bnc = Math.pow(Math.max(0, -(NRM[i].x*L.x + NRM[i].y*L.y)), 1.6)*0.30;
      c.fillStyle = `rgb(${(200*lit+16+bnc*40)|0},${(164*lit+13+bnc*52)|0},${(72*lit+9+bnc*74)|0})`;
      c.beginPath(); c.moveTo(o0.x,o0.y); c.lineTo(o1.x,o1.y);
      c.lineTo(i1.x,i1.y); c.lineTo(i0.x,i0.y); c.closePath(); c.fill();
      // catch light rides the outer edge only where it faces the source
      if (lam > 0.25){
        c.strokeStyle = `rgba(255,246,214,${(lam*lam*0.9).toFixed(3)})`;
        c.lineWidth = 1.3*u;
        c.beginPath(); c.moveTo(o0.x,o0.y); c.lineTo(o1.x,o1.y); c.stroke();
      }
    }
    c.strokeStyle = 'rgba(22,13,3,.92)'; c.lineWidth = 1.2*u;
    tracePath(c, OUT); c.stroke();
    c.strokeStyle = 'rgba(8,4,2,.8)'; c.lineWidth = 1.0*u;
    tracePath(c, INN); c.stroke();

    // the socket casts onto the recessed field beside it
    c.save(); c.globalCompositeOperation = 'source-over'; c.globalAlpha = alpha*0.5;
    c.translate(-L.x*ir*0.16, -L.y*ir*0.16);
    c.strokeStyle = 'rgba(0,0,0,.55)'; c.lineWidth = ir*0.22;
    tracePath(c, OUT); c.stroke();
    c.restore();

    c.restore();

    c.save(); c.globalCompositeOperation = 'lighter'; c.globalAlpha = alpha;
    const bloom = c.createRadialGradient(cx, ey0, 0, cx, ey0, s*2.3);
    bloom.addColorStop(0, tint+'26');
    bloom.addColorStop(.22, hsl(T*15+300, 96, 56, .10));
    bloom.addColorStop(1,'rgba(0,0,0,0)');
    c.fillStyle = bloom; c.beginPath(); c.arc(cx, ey0, s*2.3, 0, 7); c.fill();
    c.restore();
  }

  function renderEye(target, W, H, time, bake){
    const t = trade, d = derive(t), c = target.getContext('2d');
    target.width = W; target.height = H;
    const u = W/1080, tint = TINT[d.state], seed = seedOf(t);
    const cx = W*0.5, cy = H*0.245;
    const R  = Math.min(W*0.285, H*0.150);
    const k  = R*1.9;
    const T  = time * 0.001;
    const ph = (seed % 628) / 100;

    c.clearRect(0,0,W,H);
    c.fillStyle = '#06030C'; c.fillRect(0,0,W,H);          // violet-black ground
    liquidLight(c, W, H, T, seed, tint);
    tunnel(c, cx, cy, R, T, u, seed);

    // moiré: one cached grid, drawn twice at different scales and spins
    const ml = moireLayer(W, H);
    c.save(); c.globalCompositeOperation = 'lighter';
    [[0.055, 1.00, .115], [-0.041, 1.13, .095]].forEach(([sp, sc, al])=>{
      c.save(); c.globalAlpha = al;
      c.translate(cx, cy); c.rotate(T*sp + ph); c.scale(sc, sc);
      c.drawImage(ml, -W/2, -H/2 - (cy - H/2), W, H);
      c.restore();
    });
    c.restore();

    mandala(c, cx, cy, R, T, u, seed);
    icosa(c, cx, cy, k, 0.92, T*0.13 + ph, T*0.19 + ph, u, 0.85, T);
    orbit(c, cx, cy, k, 1.34, 1.28, T*0.21 + ph,       0, 96,  u, 0.85, T);
    orbit(c, cx, cy, k, 1.15, 0.55, T*-0.16 + ph, Math.PI/5, 78,  u, 0.70, T);
    orbit(c, cx, cy, k, 1.52, 1.48, T*0.09 + ph,  Math.PI/3, 120, u, 0.52, T);

    const hR = R*1.95, hD = Math.ceil(hR*2 + 24*u);
    const hl = haloLayer(hD, R*1.10, hR, series_(t), tint, u, seed);
    c.save(); c.globalCompositeOperation = 'lighter'; c.globalAlpha = 0.9;
    c.translate(cx, cy); c.rotate(T*0.05 + ph);
    c.drawImage(hl, -hD/2, -hD/2); c.restore();
    thirdEye(c, cx, cy, R, tint, u, T + ph, 1, 2, seed);

    /* Surface — halftone, blotter tooth, grade. On screen these live in a
       CSS layer so the GPU composites them once instead of the CPU redoing
       three full-canvas blends every frame. Only the PNG export bakes them. */
    if (!bake) return;
    c.save(); c.globalCompositeOperation = 'multiply'; c.globalAlpha = .17;
    c.drawImage(screenLayer(Math.ceil(W), Math.ceil(H)), 0, 0);
    c.restore();
    if (!grain || grain.width !== Math.ceil(W) || grain.height !== Math.ceil(H))
      grain = makeGrain(Math.ceil(W), Math.ceil(H));
    c.globalCompositeOperation = 'overlay'; c.globalAlpha = .13;
    c.drawImage(grain,0,0);
    c.globalAlpha = 1; c.globalCompositeOperation = 'source-over';
    const grade = c.createRadialGradient(cx, cy, R*0.4, cx, cy, W*0.95);
    grade.addColorStop(0,'rgba(0,0,0,0)'); grade.addColorStop(.55,'rgba(4,1,10,.12)');
    grade.addColorStop(1,'rgba(4,1,10,.52)');
    c.fillStyle = grade; c.fillRect(0,0,W,H);
  }

  /* the affine-corrected series, shared by both backgrounds */
  function series_(t){
    const raw = t.path, N = raw.length - 1;
    const d0 = t.entry - raw[0], dN = t.exit - raw[N];
    return raw.map((v,i)=> v + d0*(1 - i/N) + dN*(i/N));
  }

  /* ─── dispatcher ─────────────────────────────────────────── */
  const BG = { gif: renderGif, eye: renderEye };
  let bgType = 'gif';
  const GROUND = { gif:'#050608', eye:'#040406' };
  function renderHero(target, W, H, time, bake){
    HOST.style.setProperty('--void', GROUND[bgType]);
    HOST.classList.toggle('psy', bgType === 'eye');
    HOST.classList.toggle('gifbg', bgType === 'gif');
    BG[bgType](target, W, H, time || 0, bake);
  }

  let raf = 0, t0 = performance.now(), still = false;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;

  function drawScreen(time){
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (cv.clientWidth) renderHero(cv, cv.clientWidth*dpr, cv.clientHeight*dpr,
                                   time !== undefined ? time : performance.now() - t0);
  }

  let lastFrame = 0;
  function loop(now){
    raf = 0;
    if (still || document.hidden) return;
    if (now - lastFrame >= 32){ lastFrame = now; drawScreen(); }   // ~30fps ceiling
    raf = requestAnimationFrame(loop);
  }
  function startAnim(){
    cancelAnimationFrame(raf); raf = 0;
    if (bgType === 'eye' && !reduced && !still && !document.hidden)
      raf = requestAnimationFrame(loop);
    else drawScreen(reduced ? 4200 : undefined);   // one composed still frame
  }
  function setStill(v){ still = v; startAnim(); }
  document.addEventListener('visibilitychange', startAnim);

  function setType(k){
    bgType = k; t0 = performance.now();
    if (trade){
      $('f-left').textContent = plateId(trade);
      $('f-right').textContent = DISCLOSURE ? 'EDUCATIONAL · NOT ADVICE' : expWindow(trade);
    }
    startAnim();
  }

  /* ---------- 1080×1350 share card ---------- */
  function exportCard(){
    const t = trade, d = derive(t), tint = TINT[d.state];
    const EW = 1080, EH = 1350, P = 74;
    const e = document.createElement('canvas'); e.width = EW; e.height = EH;
    const c = e.getContext('2d');

    const hero = document.createElement('canvas');
    renderHero(hero, EW, EH, performance.now() - t0, true);  // fresh render, surface baked in
    c.drawImage(hero, 0, 0);

    const sc = c.createLinearGradient(0,0,0,EH);
    sc.addColorStop(0,'rgba(5,6,8,0)');     sc.addColorStop(.38,'rgba(5,6,8,0)');
    sc.addColorStop(.50,'rgba(5,6,8,.60)'); sc.addColorStop(.58,'rgba(5,6,8,.93)');
    sc.addColorStop(.65,'#050608');         sc.addColorStop(1,'#050608');
    c.fillStyle = sc; c.fillRect(0,0,EW,EH);

      const vg = c.createRadialGradient(EW/2,EH*.4,EH*.2,EW/2,EH*.4,EH*.8);
    vg.addColorStop(0,'rgba(0,0,0,0)'); vg.addColorStop(1,'rgba(0,0,0,.86)');
    c.fillStyle = vg; c.fillRect(0,0,EW,EH);

    c.textBaseline = 'alphabetic';
    c.fillStyle = '#6E7178'; c.font = '500 17px ui-monospace,monospace';
    c.fillText('N Q X   /   N I G H T W A T C H', P, P+14);
    c.textAlign = 'right';
    c.fillText(STAMP[d.state].split('').join(' '), EW-P, P+14);
    c.textAlign = 'left';

    /* laid out bottom-up so nothing can run off the card.
       R57: 根拠チャートがあるカードは、計測を 2 列(左: 事実 / 右: 執行の質)に組み、
       verdict の下に根拠チップ(モデル・等級・根拠タグ・減点)を 1〜2 行置く。 */
    const m = buildChartModel(t);
    const extra = m ? metricRows(m, d).filter(([k]) => k !== 'HELD' && !k.startsWith('AFTER') && k !== 'HTF').slice(0, 4) : [];
    const ROW_H = 76, N = 4;   // タイル(ラベル + 値)の段。54 だと値がラベルに重なる
    const chipsH = m && (m.model || m.evidence.length || m.penalties.length) ? 74 : 0;
    const discY = EH-P, rowsEnd = discY-46, rowsTop = rowsEnd-ROW_H*N;
    const verdY = rowsTop-44-chipsH, subY = verdY-30, pillH = 176, pillTop = subY-pillH;

    // R57: 結果プレート(金額 + 内訳 + R リング)。ピルは廃止。
    const plateH = pillH, plateTop = pillTop, plateW = EW - P*2;
    c.save();
    c.fillStyle = 'rgba(5,6,8,.74)';
    c.beginPath(); c.roundRect(P, plateTop, plateW, plateH, 26); c.fill();
    c.strokeStyle = tint + '70'; c.lineWidth = 2;
    c.beginPath(); c.roundRect(P, plateTop, plateW, plateH, 26); c.stroke();
    c.restore();
    const ring = ringModel(t, m);
    const ringR = 58, ringCx = EW - P - 34 - ringR, ringCy = plateTop + plateH/2;
    const amountMaxW = (ring ? ringCx - ringR - 26 : EW - P) - (P + 34);
    let amountPx = 104;
    c.font = `700 ${amountPx}px Archivo,system-ui,sans-serif`;
    const label = money(d.usd);
    while (c.measureText(label).width > amountMaxW && amountPx > 56){ amountPx -= 4; c.font = `700 ${amountPx}px Archivo,system-ui,sans-serif`; }
    c.save(); c.shadowBlur = 28; c.shadowColor = tint + '80';
    c.fillStyle = tint; c.textBaseline = 'alphabetic';
    c.fillText(label, P+34, plateTop + 30 + amountPx*0.78);
    c.restore();
    c.fillStyle = '#9AA0AB'; c.font = '500 22px ui-monospace,monospace';
    c.fillText(`${t.qty} × ${t.symbol} · ${d.pts>=0?'+':'−'}${Math.abs(d.pts).toFixed(2)} pt · ${d.held}`, P+34, plateTop + plateH - 30);
    if (ring) drawRing(c, ring, ringCx, ringCy, ringR, tint, 1.3);

    c.fillStyle = '#E9E4D6'; c.font = '700 60px UnifrakturCook,Georgia,serif';
    c.fillText(t.verdict, P, verdY);

    // R57: \u6839\u62e0\u30c1\u30c3\u30d7(verdict \u3068\u8a08\u6e2c\u884c\u306e\u3042\u3044\u3060)
    if (chipsH){
      let cx = P;
      if (m.model){
        c.font = '600 19px ui-monospace,monospace';
        const plate = `${m.model}${m.grade ? '  ' + m.grade : ''}`;
        const w = c.measureText(plate).width + 26;
        c.strokeStyle = 'rgba(233,228,214,.4)'; c.lineWidth = 1.5;
        c.beginPath(); c.roundRect(cx, verdY + 20, w, 34, 6); c.stroke();
        c.fillStyle = '#E9E4D6'; c.textBaseline = 'middle';
        c.fillText(plate, cx + 13, verdY + 37); c.textBaseline = 'alphabetic';
        cx += w + 10;
      }
      drawRationaleChips(c, m, cx, verdY + 24, EW - P - cx, 1.55);
    }

    // \u8a08\u6e2c\u30bf\u30a4\u30eb(\u30e9\u30d9\u30eb\u306e\u4e0b\u306b\u5024)\u30022 \u5217 \u00d7 4 \u6bb5\u3002\u5024\u306f\u7701\u7565\u305b\u305a\u3001\u9577\u3051\u308c\u3070\u7e2e\u3081\u308b\u3002
    const tiles = [['ENTRY',px(t.entry)],['EXIT',px(t.exit)],
     ['REALISED R', d.R === null ? '\u2014' : (d.R>=0?'+':'\u2212')+Math.abs(d.R).toFixed(2)+' R'],
     ['HELD', d.held]].concat(extra).slice(0, 8);
    const gap = 14, tileW = (EW - P*2 - gap) / 2, tileH = ROW_H - 8;
    tiles.forEach(([k,v],i)=>{
      const col = i % 2, rowI = Math.floor(i / 2);
      const x0 = P + col*(tileW + gap), y0 = rowsTop + rowI*ROW_H;
      c.fillStyle = 'rgba(5,6,8,.55)';
      c.beginPath(); c.roundRect(x0, y0, tileW, tileH, 10); c.fill();
      c.strokeStyle = 'rgba(233,228,214,.12)'; c.lineWidth = 1;
      c.beginPath(); c.roundRect(x0, y0, tileW, tileH, 10); c.stroke();
      c.font = '500 15px ui-monospace,monospace'; c.fillStyle = '#6E7178';
      c.fillText(k, x0 + 16, y0 + 24);
      let vpx = 26;
      c.font = `500 ${vpx}px ui-monospace,monospace`;
      while (c.measureText(v).width > tileW - 32 && vpx > 14){ vpx -= 1; c.font = `500 ${vpx}px ui-monospace,monospace`; }
      c.fillStyle = k === 'REALISED R' ? tint : '#E9E4D6';
      c.fillText(v, x0 + 16, y0 + tileH - 14);
    });

    c.fillStyle = '#6E7178'; c.font = '500 16px ui-monospace,monospace';
    c.fillText(plateId(t), P, discY);
    c.textAlign = 'right';
    c.fillText(DISCLOSURE ? 'EDUCATIONAL USE ONLY — NOT INVESTMENT ADVICE' : expWindow(t),
               EW - P, discY);
    c.textAlign = 'left';
    return e;
  }

  /* ---------- render ---------- */
  function render(t){
    trade = t;
    const d = derive(t);
    const root = HOST;
    root.style.setProperty('--c', TINT[d.state]);
    root.style.setProperty('--glow', d.state === 'flat' ? '.18' : '.55');

    const pillEl = $('pill');
    pillEl.textContent = money(d.usd);
    // 着弾スタンプを毎回リプレイ(reduced-motion では result.css 側で停止)
    pillEl.classList.remove('stamp');
    void pillEl.offsetWidth;
    pillEl.classList.add('stamp');
    // R57: 結果プレート。金額の下に「枚数 × 銘柄 · pt · 保有時間」、右に R リング。
    // 数字はすべて trade/chart から導出(HANDOFF §3: 外から渡さない)。
    const m = buildChartModel(t);
    const esc = s => String(s).replace(/[&<>"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));
    $('sub').innerHTML = `${t.qty} × ${esc(t.symbol)} · ` +
      `<b>${d.pts>=0?'+':'−'}${Math.abs(d.pts).toFixed(2)} pt</b> · ${esc(d.held)}`;
    const ringEl = $('ring');
    const ring = ringModel(t, m);
    if (ring){
      const C = 2 * Math.PI * 42;
      const arc = (frac, cls) => {
        if (!frac) return '';
        const len = Math.abs(frac) * C;
        // 負(負け)は反時計回りに描く: 左右反転して同じ dasharray を使う
        const flip = frac < 0 ? ' transform="translate(100 0) scale(-1 1)"' : '';
        return `<g${flip}><circle class="${cls}" cx="50" cy="50" r="42" transform="rotate(-90 50 50)" ` +
               `stroke-dasharray="${len.toFixed(2)} ${C.toFixed(2)}"/></g>`;
      };
      ringEl.innerHTML =
        `<circle class="track" cx="50" cy="50" r="42"/>` +
        (ring.plannedFrac !== null ? arc(ring.plannedFrac, 'plan') : '') +
        arc(ring.frac, 'real') +
        `<text class="r" x="50" y="${ring.planned !== null ? 50 : 58}">${ring.realized >= 0 ? '+' : '−'}${Math.abs(ring.realized).toFixed(1)}R</text>` +
        (ring.planned !== null ? `<text class="p" x="50" y="68">plan ${ring.planned.toFixed(1)}R</text>` : '');
      ringEl.hidden = false;
    } else {
      ringEl.innerHTML = ''; ringEl.hidden = true;
    }
    $('verdict').textContent = t.verdict;
    $('stamp').textContent = STAMP[d.state];

    // 計測タイル(ラベルの下に値)。根拠チャートがあれば執行の質も並べる。
    const base = [
      ['ENTRY', px(t.entry)], ['EXIT', px(t.exit)],
      ['REALISED R', d.R === null ? '—' : (d.R>=0?'+':'−')+Math.abs(d.R).toFixed(2)+' R'],
    ];
    const extra = m ? metricRows(m, d).filter(([k]) => k !== 'HELD') : [['HELD', d.held]];
    const wide = k => k.startsWith('AFTER EXIT') || k === 'HTF';
    $('rows').innerHTML = base.concat(extra)
      .map(([k,v])=>`<div class="row${wide(k) ? ' wide' : ''}${k === 'REALISED R' ? ' hot' : ''}"><span>${esc(k)}</span><b>${esc(v)}</b></div>`)
      .join('');
    const rat = $('rationale');
    if (m && (m.model || m.evidence.length || m.penalties.length)){
      const plate = m.model
        ? `<span class="modelplate">${esc(m.model)}${m.grade ? ` <i>${esc(m.grade)}</i>` : ''}</span>` : '';
      const chips = m.evidence.map(x => `<span class="chip">${esc(x)}</span>`)
        .concat(m.penalties.map(x => `<span class="chip pen">${esc(x)}</span>`));
      // 出所(確定 3 分足の本数)は見出しに重ねず、ここに静かに置く
      const src = m.source === 'demo-fiction' ? 'DEMO FICTION' : `3M × ${m.bars.length} OBSERVED`;
      chips.push(`<span class="chip ctx">${esc(src)}</span>`);
      rat.innerHTML = plate + chips.join('');
      rat.hidden = false;
    } else {
      rat.innerHTML = ''; rat.hidden = true;
    }
    $('f-left').textContent = plateId(t);
    $('f-right').textContent = DISCLOSURE ? 'EDUCATIONAL · NOT ADVICE' : expWindow(t);

    // 背景ステッカー: state 別プールから決定論選択(同じトレード → 同じ GIF)
    gifEl.src = '/stickers/' + stickerOf(seedOf(t), d.state) + '.gif';

    startAnim();

    const h = window.Telegram?.WebApp?.HapticFeedback;
    h?.notificationOccurred?.({win:'success',loss:'error',flat:'warning'}[d.state]);
  }

  return { render, drawScreen, exportCard, setType, setStill,
           get type(){ return bgType }, get trade(){ return trade } };
})();

/* ═══════════ Telegram Mini App shell ═══════════
   WebView は Safari ではない。100dvh は嘘をつき、ヘッダーは上に被り、
   下スワイプはアプリを閉じる。ここで吸収する。

   原本との違い: --vh / --pad-* は :root ではなく #resultView に書く。
   コンソール画面側の CSS 変数と混ざらないようにするため。 */
function syncViewport(){
  const tg = window.Telegram?.WebApp;
  if (!tg) return;                       // 素のブラウザ → CSS のフォールバックが効く
  HOST.style.setProperty('--vh', (tg.viewportStableHeight || innerHeight) + 'px');
  const csa = tg.contentSafeAreaInset || {top:0,bottom:0};
  const sa  = tg.safeAreaInset || {top:0,bottom:0};
  HOST.style.setProperty('--pad-top', (csa.top + sa.top) + 'px');
  HOST.style.setProperty('--pad-bot', (csa.bottom + sa.bottom) + 'px');
  if (!HOST.hidden) NQXResult.drawScreen();
}

(function bindViewport(){
  const tg = window.Telegram?.WebApp;
  if (!tg || typeof tg.onEvent !== 'function') return;   // 古い WebView には無い
  ['viewportChanged','safeAreaChanged','contentSafeAreaChanged']
    .forEach(ev => tg.onEvent(ev, syncViewport));
})();

addEventListener('resize', () => { if (!HOST.hidden) NQXResult.drawScreen(); });

/* 保存: Telegram の iOS WebView は <a download> を弾く。
   長押しで保存させる <img> を出す方式なら全環境で動く。 */
async function openSheet(){
  // exportCard は Archivo / UnifrakturCook を canvas に直接指定する。
  // 未ロードのまま呼ぶとフォールバックで焼き付いてしまう(HANDOFF §10-4)。
  try { await document.fonts?.ready; } catch { /* fonts API が無い環境はそのまま進む */ }
  const card = NQXResult.exportCard();
  HOST.querySelector('#sheet-img').src = card.toDataURL('image/png');
  HOST.querySelector('#sheet').setAttribute('open','');
  NQXResult.setStill(true);
  window.Telegram?.WebApp?.HapticFeedback?.impactOccurred?.('light');
}
function closeSheet(){
  HOST.querySelector('#sheet').removeAttribute('open');
  NQXResult.setStill(false);
}

/**
 * リザルト画面を初期化して制御ハンドルを返す。
 *
 * 表示中だけ Telegram の下スワイプ無効化と地色合わせを効かせる。
 * 閉じたら元に戻す(コンソール画面の見た目を変えないため)。
 */
export function initResultView({ onClose } = {}) {
  let previousChrome = null;

  HOST.querySelector('#save').addEventListener('click', openSheet);
  HOST.querySelector('#sheet-close').addEventListener('click', closeSheet);
  HOST.querySelector('#result-close').addEventListener('click', () => hide());
  HOST.querySelector('#result-type').addEventListener('click', (event) => {
    const next = NQXResult.type === 'gif' ? 'eye' : 'gif';
    NQXResult.setType(next);
    event.currentTarget.textContent = next === 'gif' ? 'GIF' : 'SIGIL';
    window.Telegram?.WebApp?.HapticFeedback?.impactOccurred?.('light');
  });

  function show(trade){
    if (trade) NQXResult.render(trade);
    else if (!NQXResult.trade) return false;   // 出すものが無ければ開かない
    HOST.hidden = false;
    const tg = window.Telegram?.WebApp;
    if (tg && previousChrome === null) {
      previousChrome = true;
      tg.disableVerticalSwipes?.();            // スクロール中にアプリが閉じるのを防ぐ
      tg.setBackgroundColor?.('#07080A');
      tg.setHeaderColor?.('#07080A');
    }
    syncViewport();
    // 隠している間は止めているので、開いたら必ず戻す。
    // prefers-reduced-motion と Type 1 は startAnim 側が静止フレームに落とす。
    NQXResult.setStill(false);
    NQXResult.drawScreen();
    return true;
  }

  function hide(){
    closeSheet();
    HOST.hidden = true;
    NQXResult.setStill(true);                  // 隠している間はアニメーションを止める
    const tg = window.Telegram?.WebApp;
    if (tg && previousChrome !== null) {
      previousChrome = null;
      tg.enableVerticalSwipes?.();
      tg.setBackgroundColor?.('#070707');      // コンソール画面の地色に戻す
      tg.setHeaderColor?.('#070707');
    }
    onClose?.();
  }

  HOST.hidden = true;
  NQXResult.setStill(true);

  return {
    show, hide,
    render: (trade) => NQXResult.render(trade),
    setType: (kind) => NQXResult.setType(kind),
    exportCard: () => NQXResult.exportCard(),
    get visible(){ return !HOST.hidden },
    get type(){ return NQXResult.type },
    get trade(){ return NQXResult.trade },
  };
}
