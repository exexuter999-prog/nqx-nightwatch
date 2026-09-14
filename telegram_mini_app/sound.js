// 音声キュー(R54・2026-09-04)。NinjaTrader の "Order Filled" のような一声を、
// 画面の状態が**変わった瞬間**にだけ鳴らす。各クリップは短い警告音(緊張感)+
// 速めの英語音声。
//
// 規律:
//   - 判定はしない。view の差分(建玉が現れた / 注文が出た / シナリオが武装した)
//     を読むだけで、発注経路・サーバー状態には一切触れない。
//   - 起動直後の最初の**実在する** view は「基準」として飲み込む(開いた瞬間に既存の
//     建玉を「Executed」と読み上げない)。null は基準にならない —— R74 まで app.js の
//     最初の render() が view=null で走って基準が null になり、最初の snapshot が
//     「空 → 全部現れた」の差分として読まれ、起動のたびに既存状態の一声が鳴っていた。
//   - 再生は端末の設定(SOUND スイッチ)が ON のときだけ。OFF でも差分の基準は
//     進める —— 後から ON にしたときに過去分をまとめて鳴らさないため。
//   - 手動と自動の区別: 手動はこの画面でスライドした瞬間に鳴らす(局所イベント)。
//     その直後に server の view に現れる同じ注文は二重に読まない(dedupe)。
//     ローカルで送っていないのに注文が現れたら、それは監視PCの自動発注。
//   - iOS / Telegram WebView は利用者の操作なしに音を出せない。タップで
//     AudioContext を解錠し、それまでは黙って失敗する(例外を投げない)。
//   - WAV が読めない環境では Web Speech API で同じ文言を読む。それも無ければ無音。
//
// R74(2026-09-09)—— 鳴るタイミングの規律:
//   - **一声は「今」の出来事。今鳴らせなければ捨てる。** 眠っている AudioContext に
//     start() を溜めない(溜めると次のタップで全部が一斉に鳴る)。起こすのを
//     RESUME_GRACE_MS だけ待ち、起きなければ <audio> に落ち、それも駄目なら無音。
//     CUE_TTL_MS より待たされた一声は鳴らさない(遅れて鳴る「Executed」は誤報)。
//   - **一声ずつ。** 再生は直列キュー。前の一声が終わる(ended)まで次を始めない。
//   - **解錠は無音で。** 実音源を muted 再生して解錠しない(muted が効かない端末では
//     8 本が一斉に鳴り、効く端末でも解錠直後の本番の一声を settle が止めていた)。
//     共有の <audio> 1 本を無音 WAV(data URI)で解錠し、本番はその 1 本の src を差し替える。
//   - 権限エラー(NotAllowedError)では Web Speech に落ちない。音が許されない状況では
//     読み上げも許されないか、キューに溜まって後で喋り出すだけ。落ちるのは音源の
//     読み込み失敗だけ。
//   - 解錠のリスナーは外さない。pointerdown だけでは操作と認めない端末(touch)があり、
//     iOS は割り込み後に AudioContext が再び眠る。毎タップで unlock() を通す(冪等)。
//
// 音源: public/voice/*.wav(2 音の合図 + 音声, 16kHz mono)。作り方は
// `python tools/voice/neural.py --voice <声> --out public/voice`(Edge のニューラル TTS)。
// R65 でここは 3 版目 —— 女性声のボコーダ化(聞き取れない)も、ピッチ下げ(声が歪む)も
// 破棄した。**ピッチを下げても声道の長さは変わらない**ので、女性声から低い男性声は作れない。
// 生成は開発時に一度だけで、アプリの実行時にネットワークへ出ることはない。
// 台本の正本は tools/voice/lines.json。ファイル名は CUE_FILES の値と一致させる。

import { deriveTrade } from "./ledger.js";

export const CUES = Object.freeze({
  SCENARIO_ARMED: "SCENARIO_ARMED",
  ORDER_SENT_MANUAL: "ORDER_SENT_MANUAL",
  AUTO_ORDER_SENT: "AUTO_ORDER_SENT",
  ORDER_FILLED: "ORDER_FILLED",
  TP1_FILLED: "TP1_FILLED",
  // R65: 決済は勝ち負けで読み分ける(ユーザー指定の台本)。分からないときは負け側を読む。
  POSITION_CLOSED_WIN: "POSITION_CLOSED_WIN",
  POSITION_CLOSED_LOSS: "POSITION_CLOSED_LOSS",
  SOUND_ON: "SOUND_ON",
});

export const CUE_FILES = Object.freeze({
  SCENARIO_ARMED: "scenario-armed.wav",
  ORDER_SENT_MANUAL: "order-sent.wav",
  AUTO_ORDER_SENT: "auto-order-sent.wav",
  ORDER_FILLED: "order-filled.wav",
  TP1_FILLED: "tp1-filled.wav",
  POSITION_CLOSED_WIN: "position-closed-win.wav",
  POSITION_CLOSED_LOSS: "position-closed-loss.wav",
  SOUND_ON: "sound-on.wav",
});

/**
 * 読み上げ文言。**WAV と一字一句同じ**にすること —— WAV は
 * `tools/voice/synth.ps1` の同じ表から作る(テストで突き合わせる)。
 * R65(2026-09-07)で管制の機械の言い回しへ差し替えた(ユーザー: もっとイかした表現に)。
 */
export const CUE_TEXT = Object.freeze({
  SCENARIO_ARMED: "Armed. Stand by.",
  ORDER_SENT_MANUAL: "Manual fire.",
  AUTO_ORDER_SENT: "Auto deployed.",
  ORDER_FILLED: "Executed.",
  TP1_FILLED: "One down.",
  POSITION_CLOSED_WIN: "Gain secured.",
  POSITION_CLOSED_LOSS: "Cut loose.",
  SOUND_ON: "Comms active.",
});

/** 手動送信の直後、server 側に同じ注文が現れるまでの猶予。 */
export const MANUAL_WINDOW_MS = 90_000;
/** 同じキューを続けて読まない間隔(ROUTING と注文出現の二重読みを防ぐ)。 */
export const DEDUPE_MS = 12_000;
/** R74: 鳴らせないまま待たせる上限。これを過ぎた一声は捨てる(遅れて鳴る一声は誤報)。 */
export const CUE_TTL_MS = 6_000;
/** R74: 眠っている AudioContext を起こすのを待つ上限。起きなければ <audio> に落ちる。 */
export const RESUME_GRACE_MS = 1_500;
/** R74: 再生終了の合図(ended)が来ないときの保険。 */
const CLIP_FALLBACK_MS = 4_000;
/** R74: 解錠に使う無音の WAV(8kHz mono 16bit、16 サンプル = 2ms)。実音源では解錠しない。 */
export const SILENT_WAV = "data:audio/wav;base64,UklGRkQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YSAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==";
/**
 * R74: 解錠を試みる操作。pointerdown だけでは「利用者の操作」と認めない端末がある
 * (Chromium の touch は pointerup / touchend、WebKit は touchend / click)。
 */
export const UNLOCK_EVENTS = Object.freeze(["pointerdown", "pointerup", "touchend", "click", "keydown"]);

/** app.js の render() が「注文カードを出す」と判定する state と同じ集合。 */
const PENDING_ORDER_STATES = new Set([
  "PENDING", "SENT", "PARTIAL", "UNKNOWN",
  "ENTRY_PARTIAL_ROUTE", "ENTRY_PARTIAL_FILL", "ENTRY_RESTING",
]);

const PRIMING = "__nqxPriming";   // 共有 <audio> が解錠中(無音再生中)の印
const BUSY = "__nqxBusy";         // 共有 <audio> が本番の一声を鳴らしている印

function num(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function openPosition(view) {
  const p = view?.position;
  if (!p || typeof p !== "object") return null;
  const qty = num(p.qty);
  return qty !== null && qty > 0 ? p : null;
}

function tp1Done(position) {
  if (!position) return false;
  const initial = num(position.initialQty);
  const qty = num(position.qty);
  return initial !== null && qty !== null && initial > 0 && qty < initial;
}

/**
 * 決済が勝ちだったか(R65)。同じ更新で決済の記録が届いていればそれが正本 —— 判定式は
 * ledger.js の deriveTrade だけを使う(式を二重に持たない)。記録がまだ無ければ、直前に
 * 見えていた建玉の含み損益で代用する。どちらも取れなければ **勝ちとは読まない**
 * (無い喜びを鳴らすより、静かに引いた方を読む)。
 */
function closedWon(prevPosition, next) {
  const result = next?.result;
  if (result && [result.entry, result.exit, result.qty, result.pointValue]
    .every((v) => Number.isFinite(Number(v)))) {
    return deriveTrade(result).state === "win";
  }
  const pnl = Number(prevPosition?.unrealizedPnl);
  return Number.isFinite(pnl) ? pnl > 0 : false;
}

function pendingOrder(view) {
  const o = view?.order;
  if (!o || typeof o !== "object") return null;
  return PENDING_ORDER_STATES.has(String(o.state || "").toUpperCase()) ? o : null;
}

function armedScenario(view) {
  const s = view?.scenario;
  if (!s || typeof s !== "object") return null;
  const state = String(s.state || "").toUpperCase();
  if (state !== "ARMED" && state !== "ACTIVE") return null;
  if (view?.display?.orderable !== true) return null;
  return s;
}

/**
 * 送信済みの ENTRY claim(R57)。
 *
 * `order` ストリームを publish するのは Mini App からの手動送信(telegram_bot)だけで、
 * 監視PCの自動発注(autotrade_engine → order.py)は publish しない。本番の自動発注で
 * view に現れるのは `entryClaim` の CLAIMED → CONSUMED(送信直前)と、約定後の `position`
 * だけなので、`order` だけを見ていた "Auto order sent." は一度も鳴らなかった
 * (2026-09-05 実測)。CONSUMED、または CONSUME を経て RESOLVED になった claim を
 * 「送った」と読む。RECOVERED(回復済み)や CLAIMED(dry-run 前)は読まない。
 */
function sentClaim(view) {
  const c = view?.entryClaim;
  if (!c || typeof c !== "object" || !c.entryKey) return null;
  const state = String(c.state || "").toUpperCase();
  if (state === "CONSUMED") return c;
  if (state === "RESOLVED" && c.consumedAt) return c;
  return null;
}

/**
 * 直前の view と今の view を比べ、鳴らすべきキューを1つ返す(無ければ null)。
 * 純粋関数。優先順位は 建玉 > 注文 > シナリオ(同じ更新に複数含まれても
 * いちばん重い出来事だけを読む)。
 *
 * @param {object|null} prev
 * @param {object|null} next
 * @param {{ manualPending?: boolean }} [opts]  直近にこの画面から手動送信したか
 */
export function nextCue(prev, next, { manualPending = false } = {}) {
  if (!next || typeof next !== "object") return null;
  if (prev === next) return null;
  const p = prev && typeof prev === "object" ? prev : {};

  const pPos = openPosition(p);
  const nPos = openPosition(next);
  if (!pPos && nPos) return CUES.ORDER_FILLED;
  if (pPos && nPos && tp1Done(nPos) && !tp1Done(pPos)) return CUES.TP1_FILLED;
  if (pPos && !nPos) return closedWon(pPos, next) ? CUES.POSITION_CLOSED_WIN : CUES.POSITION_CLOSED_LOSS;

  if (!pendingOrder(p) && pendingOrder(next)) {
    // この画面から送った直後に現れた注文は「手動」(既にスライド時に読んでいる)。
    // それ以外は監視PC(autotrade_engine)が送った自動発注。
    return manualPending ? CUES.ORDER_SENT_MANUAL : CUES.AUTO_ORDER_SENT;
  }
  // R57: 自動発注は `order` を publish しないので、ENTRY claim の送信(CONSUMED)で読む。
  // 新しい entryKey の claim が送信済みになった瞬間に 1 回だけ。
  const nClaim = sentClaim(next);
  if (nClaim && !pendingOrder(p)) {
    const pClaim = sentClaim(p);
    if (!pClaim || pClaim.entryKey !== nClaim.entryKey) {
      return manualPending ? CUES.ORDER_SENT_MANUAL : CUES.AUTO_ORDER_SENT;
    }
  }

  const nArmed = armedScenario(next);
  if (nArmed) {
    const pArmed = armedScenario(p);
    if (!pArmed || pArmed.scenarioId !== nArmed.scenarioId) return CUES.SCENARIO_ARMED;
  }
  return null;
}

function isPermissionError(err) {
  const name = String(err?.name || "");
  return name === "NotAllowedError" || name === "AbortError";
}

/** 保険のタイマー。Node のテストではプロセスを引き留めない(unref)。 */
function later(fn, ms) {
  const t = setTimeout(fn, ms);
  if (t && typeof t.unref === "function") t.unref();
  return t;
}

function delay(ms) {
  return new Promise((resolve) => { later(() => resolve(false), ms); });
}

/**
 * 再生器。`observe(view)` を view が変わるたびに呼ぶ(render() の入口)。
 *
 * @param {object} opts
 * @param {() => boolean} opts.isEnabled   SOUND スイッチの現在値
 * @param {string}  [opts.base]            音源のベース URL(既定 "./voice/")
 * @param {object}  [opts.win]             window(テスト差し替え用)
 * @param {(cue: string) => (Promise<void>|void)} [opts.player] 再生の差し替え(テスト用)
 * @param {() => number} [opts.now]        時計(テスト用)
 * @param {number}  [opts.resumeGraceMs]   AudioContext を起こすのを待つ上限(テスト用)
 * @param {number}  [opts.cueTtlMs]        待たせた一声を捨てる閾値(テスト用)
 */
export function createSoundCue({
  isEnabled, base = "./voice/", win = globalThis, player = null, now = () => Date.now(),
  resumeGraceMs = RESUME_GRACE_MS, cueTtlMs = CUE_TTL_MS,
} = {}) {
  let last = null;          // 差分の基準。最初の**実在する** view を飲み込むまで null
  let gestureSeen = false;  // 利用者の操作を一度でも見たか
  let primed = false;       // 共有 <audio> を無音で解錠済みか
  let ctx = null;
  let resumePromise = null;
  let manualAt = -Infinity; // 最後にこの画面から手動送信した時刻
  let el = null;            // 共有の <audio>(キューは直列なので 1 本で足りる)
  let loadedCue = null;     // 共有 <audio> に今入っている音源
  const lastPlayed = new Map();
  const buffers = new Map();   // cue → AudioBuffer(WebAudio 経路。解錠後に読み込む)
  const queue = [];            // { cue, at, ttl, resolve }
  let playing = false;

  function ctxRunning() {
    return Boolean(ctx) && ctx.state === "running";
  }

  /** 眠っている AudioContext を起こす。起きたかどうか(state==="running")で解決する。 */
  function wakeContext() {
    if (!ctx) return Promise.resolve(false);
    if (ctxRunning()) return Promise.resolve(true);
    let p = null;
    try { p = ctx.resume(); } catch { p = null; }
    resumePromise = Promise.resolve(p).then(() => ctxRunning(), () => false);
    return resumePromise;
  }

  /**
   * 解錠(タップの中で呼ぶ。冪等 —— 毎タップ呼んでよい)。
   *
   * iOS / Telegram WebView は「操作の中で始まった再生」しか許さない。WebSocket の更新で
   * 鳴らす音は操作の外なので、(1) AudioContext をタップ内で起こし、(2) 共有の <audio> を
   * タップ内で一度**無音の WAV** で再生→停止して primed にし、(3) 音源を AudioBuffer に
   * 読み込む。以後の再生は AudioContext(操作不要)を優先し、無ければ primed な <audio>。
   */
  function unlock() {
    gestureSeen = true;
    try {
      const AC = win.AudioContext || win.webkitAudioContext;
      if (AC && !ctx) ctx = new AC();
    } catch {
      ctx = null;               // AudioContext が無い環境でも <audio> だけで動く
    }
    if (ctx) {
      wakeContext();
      // iOS は resume() だけでは起きない版があるので、無音の 1 サンプルをタップ内で流す。
      try {
        if (typeof ctx.createBuffer === "function") {
          const scratch = ctx.createBufferSource();
          scratch.buffer = ctx.createBuffer(1, 1, 22050);
          scratch.connect(ctx.destination);
          scratch.start(0);
        }
      } catch { /* 解錠の補助。失敗しても本線は resume() */ }
    }
    if (player) return;                       // テスト差し替え時は音源に触らない
    if (!primed) primeElement();
    preloadBuffers();
  }

  function element() {
    if (el) return el;
    const AudioCtor = win.Audio;
    if (typeof AudioCtor !== "function") return null;
    el = new AudioCtor();
    el.preload = "auto";
    return el;
  }

  /** 共有 <audio> を無音 WAV で解錠する。本番の一声が鳴っている最中は触らない。 */
  function primeElement() {
    const audio = element();
    if (!audio || audio[BUSY] || audio[PRIMING]) return;
    audio[PRIMING] = true;
    loadedCue = null;
    try { audio.src = SILENT_WAV; } catch { /* src を持てない差し替え */ }
    audio.muted = true;
    try { audio.volume = 0; } catch { /* iOS は volume を持たない */ }
    const settle = (ok) => {
      if (!audio[PRIMING]) return;            // 本番の一声が先に取っていれば触らない
      audio[PRIMING] = false;
      primed = ok;                            // 拒否されたら次のタップでやり直す
      try { audio.pause(); audio.currentTime = 0; } catch { /* noop */ }
      audio.muted = false;
      try { audio.volume = 1; } catch { /* noop */ }
    };
    try {
      const p = audio.play();
      if (p && typeof p.then === "function") p.then(() => settle(true), () => settle(false));
      else settle(true);
    } catch {
      settle(false);
    }
  }

  function preloadBuffers() {
    if (!ctx || typeof win.fetch !== "function" || typeof ctx.decodeAudioData !== "function") return;
    for (const cue of Object.keys(CUE_FILES)) {
      if (buffers.has(cue)) continue;
      buffers.set(cue, null);                 // 読み込み中の印(二重 fetch しない)
      win.fetch(base + CUE_FILES[cue])
        .then((res) => (res.ok ? res.arrayBuffer() : Promise.reject(new Error(String(res.status)))))
        .then((data) => ctx.decodeAudioData(data))
        .then((buffer) => { buffers.set(cue, buffer); })
        .catch(() => { buffers.delete(cue); });   // 次のタップで取り直す
    }
  }

  /** 音源の長さから「終わったはず」の時刻を出す(ended が来ないときの保険)。 */
  function clipMs(duration) {
    return Number.isFinite(duration) && duration > 0 ? duration * 1000 + 300 : CLIP_FALLBACK_MS;
  }

  /**
   * WebAudio で鳴らす。**動いている** AudioContext にだけ start() する —— 眠っている
   * context に溜めた source は、次に起きた瞬間(次のタップ)に全部が一斉に鳴る。
   * 終わったら解決する。
   */
  function playBuffer(cue) {
    const buffer = buffers.get(cue);
    if (!ctxRunning() || !buffer) return playElement(cue);
    return new Promise((resolve) => {
      let src;
      try {
        src = ctx.createBufferSource();
        src.buffer = buffer;
        src.connect(ctx.destination);
        src.start(0);
      } catch {
        resolve(playElement(cue));
        return;
      }
      let done = false;
      const finish = () => { if (done) return; done = true; resolve(true); };
      try { src.onended = finish; } catch { /* noop */ }
      later(finish, clipMs(buffer.duration));
    });
  }

  /**
   * 共有 <audio> で鳴らす。解錠中(無音再生中)なら奪う —— settle は PRIMING を見て
   * 手を引くので、本番の一声が止められることはない。終わったら解決する。
   * 権限エラー(操作の外で許されない)は静かに捨て、音源の失敗だけ Web Speech に落とす。
   */
  function playElement(cue) {
    const audio = element();
    if (!audio) return speak(cue);
    audio[PRIMING] = false;
    audio[BUSY] = true;
    audio.muted = false;
    try { audio.volume = 1; } catch { /* noop */ }
    return new Promise((resolve) => {
      let done = false;
      let spoke = false;
      const canListen = typeof audio.addEventListener === "function";
      const off = () => {
        if (!canListen) return;
        audio.removeEventListener("ended", onEnded);
        audio.removeEventListener("error", onError);
      };
      const finish = (ok) => { if (done) return; done = true; audio[BUSY] = false; off(); resolve(ok); };
      const fallback = () => { if (spoke) return; spoke = true; speak(cue).then(finish, () => finish(false)); };
      const onEnded = () => finish(true);
      const onError = () => fallback();
      if (canListen) {
        audio.addEventListener("ended", onEnded);
        audio.addEventListener("error", onError);
      }
      let result;
      try {
        if (loadedCue !== cue) {
          audio.src = base + CUE_FILES[cue];
          loadedCue = cue;
        } else {
          audio.currentTime = 0;
        }
        result = audio.play();
      } catch {
        fallback();
        return;
      }
      const guard = () => { if (canListen) later(() => finish(true), clipMs(audio.duration)); else finish(true); };
      if (result && typeof result.then === "function") {
        result.then(guard, (err) => { if (isPermissionError(err)) finish(false); else fallback(); });
      } else {
        guard();
      }
    });
  }

  /** Web Speech で同じ文言を読む(音源が読めないときだけ)。終わったら解決する。 */
  function speak(cue) {
    return new Promise((resolve) => {
      try {
        const synth = win.speechSynthesis;
        const Utter = win.SpeechSynthesisUtterance;
        if (!synth || !Utter) { resolve(false); return; }
        const u = new Utter(CUE_TEXT[cue]);
        u.lang = "en-US";
        // R65: WAV と同じ「機械」に寄せる —— 低く、平らに、少しゆっくり
        u.rate = 1.0;
        u.pitch = 0.45;
        const voices = typeof synth.getVoices === "function" ? synth.getVoices() : [];
        const preferred = voices.find((v) => /en[-_]US/i.test(v.lang) && /david|mark|guy|male|daniel|alex/i.test(v.name))
          || voices.find((v) => /en[-_]US/i.test(v.lang));
        if (preferred) u.voice = preferred;
        let done = false;
        const finish = (ok) => { if (done) return; done = true; resolve(ok); };
        u.onend = () => finish(true);
        u.onerror = () => finish(false);
        synth.cancel();
        synth.speak(u);
        later(() => finish(true), CLIP_FALLBACK_MS);
      } catch {
        resolve(false);
      }
    });
  }

  /** 一声の再生を始める。終わったら解決する(失敗・見送りは false)。 */
  function startClip(cue) {
    const buffer = buffers.get(cue);
    if (ctx && buffer) {
      if (ctxRunning()) return playBuffer(cue);
      // 眠っている: 起こすのを少しだけ待つ。起きなければ <audio> へ。眠ったまま start() しない。
      return Promise.race([wakeContext(), delay(resumeGraceMs)])
        .then(() => (ctxRunning() ? playBuffer(cue) : playElement(cue)));
    }
    return playElement(cue);
  }

  /** 直列キュー。前の一声が終わるまで次を始めず、待たせすぎた一声は捨てる。 */
  function pump() {
    if (playing) return;
    while (queue.length) {
      const item = queue.shift();
      if (now() - item.at > item.ttl) { item.resolve(false); continue; }
      playing = true;
      let finished = false;
      const done = (ok) => {
        if (finished) return;
        finished = true;
        playing = false;
        item.resolve(ok === true);
        pump();
      };
      try {
        startClip(item.cue).then(done, () => done(false));
      } catch {
        done(false);
      }
      return;
    }
  }

  function enqueue(cue, { ttl = cueTtlMs } = {}) {
    return new Promise((resolve) => {
      queue.push({ cue, at: now(), ttl, resolve });
      pump();
    });
  }

  /**
   * ON のときだけ鳴らす。同じキューは DEDUPE_MS 内では読まない。
   * 失敗しても例外は外に出さない。
   */
  function play(cue, { force = false } = {}) {
    if (!CUE_FILES[cue]) return false;
    if (typeof isEnabled === "function" && !isEnabled()) return false;
    const t = now();
    if (!force && t - (lastPlayed.get(cue) ?? -Infinity) < DEDUPE_MS) return false;
    lastPlayed.set(cue, t);
    if (player) {
      try { player(cue); } catch { /* テスト用の差し替えは握る */ }
      return true;
    }
    enqueue(cue);
    return true;
  }

  /**
   * view が変わるたびに呼ぶ。鳴らしたキュー名(または null)を返す。
   * 最初の実在する view は基準として飲み込む。null は基準にしない(起動直後の
   * 空描画 → 最初の snapshot を「全部現れた」と読まないため)。
   */
  function observe(view) {
    const next = view && typeof view === "object" ? view : null;
    if (last === null) {
      last = next;
      return null;
    }
    const manualPending = now() - manualAt < MANUAL_WINDOW_MS;
    const cue = nextCue(last, next, { manualPending });
    last = next;                       // OFF でも基準は進める
    if (cue) play(cue);
    return cue;
  }

  /**
   * 基準を差し替える(鳴らさない)。起動時の snapshot、画面復帰の snapshot、デモの出入り —
   * 「新しく見た」だけで「変わった」ではない状態に使う。
   */
  function rebase(view) {
    last = view && typeof view === "object" ? view : null;
    return null;
  }

  /**
   * この画面から手動で注文を送った瞬間に呼ぶ(スライド発火)。その場で
   * "Manual fire." を読み、以後 MANUAL_WINDOW_MS の間に現れる注文は自動扱いしない。
   */
  function manualSend() {
    manualAt = now();
    unlock();
    return play(CUES.ORDER_SENT_MANUAL);
  }

  /**
   * 試聴(デモ画面の一覧用)。明示のタップなので SOUND スイッチと dedupe を無視して
   * 必ず鳴らす。タップの中で呼ぶこと(解錠を兼ねる)。キューには並ぶ(一声ずつ)。
   */
  function audition(cue) {
    if (!CUE_FILES[cue]) return false;
    unlock();
    lastPlayed.set(cue, now());
    if (player) { try { player(cue); } catch { /* テスト用 */ } return true; }
    enqueue(cue, { ttl: Infinity });
    return true;
  }

  /**
   * 全キューを順に試聴する(1本ずつ、終わってから次)。返り値は再生した順の配列を
   * 解決する Promise。player 差し替え時は同期的に全部呼ぶ。
   */
  function auditionAll(order = Object.keys(CUE_FILES)) {
    if (player) {
      const played = [];
      for (const cue of order) { audition(cue); played.push(cue); }
      return Promise.resolve(played);
    }
    unlock();
    const played = [];
    return Promise.all(order.map((cue) => {
      lastPlayed.set(cue, now());
      return enqueue(cue, { ttl: Infinity }).then(() => { played.push(cue); });
    })).then(() => played);
  }

  /** スイッチを ON にした瞬間の確認音。タップの中で呼ぶこと(解錠を兼ねる)。 */
  function confirm() {
    unlock();
    return play(CUES.SOUND_ON, { force: true });
  }

  /**
   * タップで再生を解錠する。リスナーは外さない —— pointerdown だけでは操作と認めない
   * 端末があり、iOS は割り込み後に AudioContext が再び眠る。unlock() は冪等。
   */
  function bindUnlock(target = win.document) {
    if (!target || typeof target.addEventListener !== "function") return;
    const onGesture = () => { unlock(); };
    for (const type of UNLOCK_EVENTS) target.addEventListener(type, onGesture, { passive: true });
  }

  return {
    observe, rebase, play, manualSend, confirm, audition, auditionAll, bindUnlock,
    get unlocked() { return gestureSeen; },
    /** 鳴らしている最中 + 順番待ちの数(デバッグ・テスト用)。 */
    get pending() { return queue.length + (playing ? 1 : 0); },
  };
}
