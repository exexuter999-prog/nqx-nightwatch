/**
 * サーバー状態クライアント。
 *
 * 表示してよい状態は Worker/Durable Object から来たものだけ。
 * このモジュールは次を保証する。
 *
 *   - 初回表示は必ず /api/state の完全 snapshot(Cache-Control: no-store)
 *   - 以降は WebSocket の差分を即時反映
 *   - 再接続時・ページ復帰時・再読込時は必ず完全 snapshot を取り直す
 *   - 取得できないときは「最後に見えた状態」を verified のまま出さない。
 *     接続状態を別に持ち、UI 側が STALE 表示に落とせるようにする
 *
 * localStorage / sessionStorage は一切使わない。ブラウザ側に正本を持たせない。
 */

const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 20_000;
const RESYNC_INTERVAL_MS = 60_000;   // WS が生きていても定期的に完全 snapshot を取る

export const CONNECTION = {
  CONNECTING: "CONNECTING",
  LIVE: "LIVE",
  RECONNECTING: "RECONNECTING",
  OFFLINE: "OFFLINE",
  UNCONFIGURED: "UNCONFIGURED",
};

/** URL から API ベースと launch token を読む。Bot が両方を付けて起動する。 */
export function readLaunchParams(search = window.location.search) {
  const params = new URLSearchParams(search);
  return {
    apiBase: (params.get("api") || "").replace(/\/+$/, ""),
    token: params.get("t") || "",
    // 旧経路の凍結プレビュー。scenario の根拠には使わない(表示のみ)。
    frozen: params.get("monitor") || "",
  };
}

const LAUNCH_CACHE_KEY = "nqx.launch.v1";

function readCachedLaunch() {
  try {
    return JSON.parse(window.sessionStorage.getItem(LAUNCH_CACHE_KEY) || "{}") || {};
  } catch {
    return {};
  }
}

function writeCachedLaunch(launch) {
  try {
    if (!launch.token) return;
    window.sessionStorage.setItem(LAUNCH_CACHE_KEY, JSON.stringify(launch));
  } catch {
    // 保存できなくても起動は続く。次のリロードで URL から取り直すだけ。
  }
}

/** 起動 URL から資格情報(`t` / `tgWebAppData`)を消す。履歴は増やさない。 */
export function stripLaunchParamsFromUrl() {
  try {
    const url = new URL(window.location.href);
    let changed = false;
    for (const key of ["t", "tgWebAppData"]) {
      if (url.searchParams.has(key)) {
        url.searchParams.delete(key);
        changed = true;
      }
    }
    if (!changed) return false;
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
    return true;
  } catch {
    return false;
  }
}

/**
 * 起動 URL の資格情報を受け取り、URL からは消す。
 *
 * launch token は Bot が発行して起動 URL に載せてくる。読んだ後に URL へ
 * 残す理由は無く、残っていれば「外部ブラウザで開く」・URL の共有・
 * スクリーンショットにそのまま出てしまう。読み終えたら replaceState で
 * 消し、WebView のリロードで失わないよう sessionStorage にだけ退避する。
 *
 * 冒頭の「storage は使わない」は **状態の正本** についての規律で、ここは
 * 資格情報の持ち回り。タブを閉じれば消える sessionStorage だけを使い、
 * localStorage には書かない。
 */
export function consumeLaunchParams() {
  const fromUrl = readLaunchParams();
  const cached = readCachedLaunch();
  const launch = {
    apiBase: fromUrl.apiBase || cached.apiBase || "",
    token: fromUrl.token || cached.token || "",
    frozen: fromUrl.frozen || cached.frozen || "",
  };
  writeCachedLaunch(launch);
  stripLaunchParamsFromUrl();
  return launch;
}

export function createStateClient({ apiBase, token, initData = "", onState, onConnection }) {
  let socket = null;
  let closed = false;
  let attempt = 0;
  let reconnectTimer = null;
  let resyncTimer = null;
  let lastSeq = -1;

  const configured = Boolean(apiBase && (token || initData));

  const authHeaders = () => initData
    ? { "X-Telegram-Init-Data": initData }
    : { "X-NQX-Launch": token };

  const setConnection = (state, detail) => onConnection?.(state, detail);

  /** 完全 snapshot を取り直す。差分の取りこぼしはここで必ず収束する。 */
  async function fetchSnapshot(reason) {
    if (!configured) return null;
    try {
      const response = await fetch(`${apiBase}/api/state`, {
        cache: "no-store",
        headers: authHeaders(),
      });
      if (!response.ok) {
        setConnection(CONNECTION.OFFLINE, `state ${response.status}`);
        return null;
      }
      const body = await response.json();
      if (!body?.ok || !body.view) {
        setConnection(CONNECTION.OFFLINE, "state payload rejected");
        return null;
      }
      lastSeq = body.view.seq;
      onState?.(body.view, { source: "snapshot", reason });
      return body.view;
    } catch (error) {
      setConnection(CONNECTION.OFFLINE, String(error?.message || error));
      return null;
    }
  }

  function scheduleReconnect() {
    if (closed || reconnectTimer) return;
    attempt += 1;
    const delay = Math.min(RECONNECT_BASE_MS * 2 ** (attempt - 1), RECONNECT_MAX_MS);
    setConnection(CONNECTION.RECONNECTING, `retry in ${Math.round(delay / 1000)}s`);
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  function connect() {
    // WebSocket は initData を subprotocol に載せられないため launch token 必須。
    // initData-only 起動でも snapshot と mutation は使える。
    if (closed || !configured || !token) return;
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;

    setConnection(attempt === 0 ? CONNECTION.CONNECTING : CONNECTION.RECONNECTING);
    const wsUrl = `${apiBase.replace(/^http/, "ws")}/api/ws`;
    try {
      // Sec-WebSocket-Protocol にトークンを載せる(ブラウザはヘッダーを付けられない)。
      socket = new WebSocket(wsUrl, ["nqx.v1", `nqx-token.${token}`]);
    } catch (error) {
      setConnection(CONNECTION.OFFLINE, String(error?.message || error));
      scheduleReconnect();
      return;
    }

    socket.addEventListener("open", () => {
      attempt = 0;
      setConnection(CONNECTION.LIVE);
      // 接続直後は必ず完全 snapshot を取り直す。切断中の更新をここで拾う。
      fetchSnapshot("reconnect");
    });

    socket.addEventListener("message", (event) => {
      let message = null;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      if (!message?.view) return;

      // 順序が飛んだら差分を信用せず、完全 snapshot に落とす。
      if (message.type === "delta" && lastSeq >= 0 && message.view.seq <= lastSeq) return;
      lastSeq = message.view.seq;
      onState?.(message.view, { source: message.type, transitions: message.transitions || [] });
    });

    socket.addEventListener("close", () => {
      if (closed) return;
      socket = null;
      scheduleReconnect();
    });

    socket.addEventListener("error", () => {
      try { socket?.close(); } catch { /* already closing */ }
    });
  }

  function onVisibility() {
    if (document.visibilityState !== "visible") return;
    // Telegram を再度開いた・タブに戻った。保存済みの表示を信じず取り直す。
    fetchSnapshot("visibility");
    if (!socket || socket.readyState > WebSocket.OPEN) connect();
  }

  function start() {
    if (!configured) {
      setConnection(CONNECTION.UNCONFIGURED, "no api/token in launch url");
      return;
    }
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("online", onVisibility);
    resyncTimer = window.setInterval(() => fetchSnapshot("interval"), RESYNC_INTERVAL_MS);
    fetchSnapshot("initial").then(() => connect());
  }

  /**
   * Telegram 認証済みユーザーの明示操作を、期限付き AUTO 権限として保存する。
   * 注文 payload は送らない。口座・銘柄・実期限は Worker の応答が正本。
   */
  async function setAutotrade(enabled, { ttlMinutes = 420 } = {}) {
    if (!configured) throw new Error("AUTO endpoint is not configured");
    const response = await fetch(`${apiBase}/api/autotrade`, {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ enabled: enabled === true, ttlMinutes }),
    });
    let body = null;
    try { body = await response.json(); } catch { /* handled below */ }
    if (!response.ok || !body?.ok || !body.view) {
      throw new Error(body?.reason || `AUTO ${response.status}`);
    }
    lastSeq = Number(body.view.seq);
    onState?.(body.view, { source: "autotrade-mutation" });
    return body.arm;
  }

  /**
   * 口座別 ULTRA 設定(対象口座・利益目標・DD上限)の全置換保存。
   * 注文 payload は送らない。実枚数・リスクは監視PC側が必ず再計算する。
   */
  async function setAccountPrefs(prefs) {
    if (!configured) throw new Error("prefs endpoint is not configured");
    const response = await fetch(`${apiBase}/api/accountPrefs`, {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ prefs }),
    });
    let body = null;
    try { body = await response.json(); } catch { /* handled below */ }
    if (!response.ok || !body?.ok || !body.view) {
      throw new Error(body?.reason || `prefs ${response.status}`);
    }
    lastSeq = Number(body.view.seq);
    onState?.(body.view, { source: "account-prefs-mutation" });
    return body.prefs;
  }

  /**
   * 遅延計測(R55)。認証不要の GET /api/health を一度だけ叩き、往復時間を返す。
   * 状態は書き換えないし、失敗しても接続状態(connection)には触れない —
   * SYSTEM ROUTE 画面の表示専用で、発注・購読とは無関係。
   */
  async function ping() {
    if (!apiBase) return null;
    const started = globalThis.performance?.now?.() ?? Date.now();
    const elapsed = () => Math.round((globalThis.performance?.now?.() ?? Date.now()) - started);
    try {
      const response = await fetch(`${apiBase}/api/health`, { cache: "no-store" });
      const rttMs = elapsed();
      if (!response.ok) return { ok: false, rttMs, status: response.status, at: Date.now() };
      let body = null;
      try { body = await response.json(); } catch { /* health の本文は任意 */ }
      return { ok: body?.ok === true, rttMs, serverTime: body?.serverTime || null, at: Date.now() };
    } catch (error) {
      return { ok: false, rttMs: null, error: String(error?.message || error), at: Date.now() };
    }
  }

  function stop() {
    closed = true;
    document.removeEventListener("visibilitychange", onVisibility);
    window.removeEventListener("online", onVisibility);
    if (reconnectTimer) window.clearTimeout(reconnectTimer);
    if (resyncTimer) window.clearInterval(resyncTimer);
    try { socket?.close(); } catch { /* already closed */ }
  }

  return { start, stop, refresh: fetchSnapshot, setAutotrade, setAccountPrefs, ping, configured };
}
