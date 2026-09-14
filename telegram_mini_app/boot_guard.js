/**
 * 起動の見張り(R67) —— 端末の描画層が WebView を落とすときの自己防衛。
 *
 * 2026-09-07〜08、Telegram Desktop(Windows / Edge WebView2)で Mini App を開いた直後に
 * WebView が落ち、「ボタンを押しても開かない」状態になった。Telegram Desktop の log.txt に
 * `BotWebView Error: Post event "fullscreen_changed" on crashed webview` が 4 回。サーバー
 * (Pages / Worker)・ビルド・起動 URL は全て正常で、落ちているのは端末側の描画層
 * (three.js の WebGL シーン × 3 + 2D canvas の鉄粉 + 音声)である。
 *
 * ここが決めるのは「今回の起動で重い層を動かしてよいか」だけ。状態・発注には一切触れない。
 *
 *   tier "full": 通常。
 *   tier "mid":  3D 背景(scene3d: 後処理付きでいちばん重い)だけを起動しない。3D の目と氷、
 *                2D の描画と音はそのまま。Telegram Desktop では既定でこれ(R75, 2026-09-09:
 *                lite では目玉と氷が丸ごと消えて寂しい、というユーザー判断。WebGL コンテキストを
 *                3 → 2 に減らして様子を見る。落ちれば次の起動は従来どおり safe)。
 *   tier "lite": WebGL シーン(3D 背景・3D の目・氷)を全部起動しない。2D の描画と音はそのまま。
 *                URL の boot=lite で選ぶ(R75 以降、既定にはならない)。
 *   tier "safe": WebGL に加えて鉄粉(2D canvas)と音声も止める。前回の起動が完了しなかった
 *                痕跡があるとき(24 時間は粘る)、または URL に boot=safe / safe=1。
 *
 * 前回の起動の痕跡: 起動時に localStorage へ pending を書き、STABLE_MS 生き延びるか、
 * 正常に閉じた(pagehide / hidden)ときに消す。次の起動で pending が残っていれば、前回は
 * 途中で落ちたとみなす。クラッシュは何も残せないので「残っている = 落ちた」が唯一の証拠。
 * (localStorage を使わない原則は「状態の正本」についての規律。これは端末の自己防衛で、
 * サーバー状態を一切持たない。)
 *
 * 戻し方: URL に boot=full|mid|lite、または画面の状態ピルをタップ。SAFE / LITE のピルは記録を
 * 消して端末の既定(Telegram Desktop なら mid、他は full)で開き直し、MID のピルは boot=full で
 * 3D 背景まで点ける。そこでまた落ちれば、次の起動は自動で safe に戻る。
 */
export const BOOT_KEY = "nqx.boot.v1";
export const STABLE_MS = 6000;
export const STICKY_MS = 24 * 60 * 60 * 1000;
export const TIERS = Object.freeze({ FULL: "full", MID: "mid", LITE: "lite", SAFE: "safe" });

function readRecord(storage) {
  try {
    const raw = storage?.getItem?.(BOOT_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

function writeRecord(storage, record) {
  try {
    storage?.setItem?.(BOOT_KEY, JSON.stringify(record));
    return true;
  } catch {
    return false;
  }
}

/** 今回の起動で何を動かすか。副作用なし(読むだけ)。 */
export function decideTier({ storage = null, search = "", platform = "", now = Date.now() } = {}) {
  const params = new URLSearchParams(search || "");
  const forced = String(params.get("boot") || "").toLowerCase();
  const record = readRecord(storage) || {};
  const crashed = record.pending === true;
  const crashes = (Number(record.crashes) || 0) + (crashed ? 1 : 0);
  const lastCrashAt = crashed ? now : (Number(record.lastCrashAt) || 0);
  const sticky = lastCrashAt > 0 && now - lastCrashAt < STICKY_MS;
  let tier = TIERS.FULL;
  let reason = "";
  if (forced === TIERS.SAFE || params.get("safe") === "1") {
    tier = TIERS.SAFE; reason = "URL";
  } else if (forced === TIERS.FULL || forced === TIERS.MID || forced === TIERS.LITE) {
    tier = forced; reason = "URL";
  } else if (crashed) {
    tier = TIERS.SAFE; reason = `PREVIOUS BOOT DID NOT FINISH ×${crashes}`;
  } else if (sticky) {
    tier = TIERS.SAFE; reason = "RECENT CRASH";
  } else if (String(platform || "").toLowerCase() === "tdesktop") {
    tier = TIERS.MID; reason = "TELEGRAM DESKTOP";
  }
  const anyWebgl = tier === TIERS.FULL || tier === TIERS.MID;
  return {
    tier,
    reason,
    crashed,
    crashes,
    lastCrashAt,
    webgl: anyWebgl,               // WebGL を一つでも動かすか(目・氷)
    scene3d: tier === TIERS.FULL,  // 3D 背景(後処理付き。いちばん重い)は full だけ
    eye3d: anyWebgl,               // 3D の目(待機紋章・ペット)
    ice: anyWebgl,                 // 氷のチャート(chart.js の webgl)
    particles: tier !== TIERS.SAFE,
    audio: tier !== TIERS.SAFE,
  };
}

/** 起動の印を残す。落ちれば残ったまま次の起動に読まれる。 */
export function armBootGuard(storage, decision, now = Date.now()) {
  return writeRecord(storage, {
    pending: true,
    at: now,
    crashes: decision?.crashes || 0,
    lastCrashAt: decision?.lastCrashAt || 0,
    tier: decision?.tier || TIERS.FULL,
  });
}

/** 起動が生き延びた / 正常に閉じた。pending だけを下ろし、クラッシュ履歴は残す。 */
export function markBootStable(storage, now = Date.now()) {
  const record = readRecord(storage) || {};
  if (record.pending !== true) return false;
  return writeRecord(storage, { ...record, pending: false, stableAt: now });
}

/** 記録を消す(通常描画へ戻すとき)。 */
export function resetBootGuard(storage) {
  try {
    storage?.removeItem?.(BOOT_KEY);
    return true;
  } catch {
    return false;
  }
}

/**
 * 印を書き、生き延びたら下ろす配線。戻り値は手動で下ろす関数(テスト用)。
 * 正常な終了(pagehide / hidden)でも下ろすので、6 秒以内に閉じただけでは落ちた扱いにならない。
 */
export function installBootGuard({ storage, decision, win = globalThis, now = () => Date.now() } = {}) {
  armBootGuard(storage, decision, now());
  const settle = () => markBootStable(storage, now());
  try { win.setTimeout?.(settle, STABLE_MS); } catch { /* タイマーが無くても pagehide で下ろす */ }
  try { win.addEventListener?.("pagehide", settle); } catch { /* 無ければ諦める */ }
  try {
    win.document?.addEventListener?.("visibilitychange", () => {
      if (win.document.visibilityState === "hidden") settle();
    });
  } catch { /* 無ければ諦める */ }
  return settle;
}
