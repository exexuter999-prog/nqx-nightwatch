/**
 * NQX Nightwatch API Worker — 認証付きゲートウェイ。
 *
 * 役割はルーティングと認証だけ。状態の判断は全て Durable Object が行う。
 *
 * ★ この Worker 自体は発注しない。
 *   CrossTrade / Tradovate / order.py を呼ぶコードをここに足さないこと。
 *   Mini App の AUTO 操作は期限付きの権限だけを保存し、実送信は監視PCの
 *   autotrade_engine → order.py が既存の全ゲートを再検証して行う。
 */
import { allowedOrigins, authorizeReader, verifyMutationOrigin, verifyPublishSignature } from "./auth.js";
export { NightwatchState } from "./nightwatch_do.js";

const WS_PROTOCOL = "nqx.v1";
const WS_TOKEN_PREFIX = "nqx-token.";

function corsHeaders(env, request) {
  const origin = request.headers.get("Origin") || "";
  const list = allowedOrigins(env);
  const headers = {
    "Vary": "Origin",
    "Cache-Control": "no-store",
    // レスポンスを別の型として解釈させない。JSON しか返さない API である。
    "X-Content-Type-Options": "nosniff",
  };
  if (origin && list.includes(origin)) {
    headers["Access-Control-Allow-Origin"] = origin;
    headers["Access-Control-Allow-Headers"] = "Content-Type, X-Telegram-Init-Data, X-NQX-Launch, X-NQX-Timestamp, X-NQX-Nonce, X-NQX-Account, X-NQX-Signature";
    headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS";
    headers["Access-Control-Max-Age"] = "600";
  }
  return headers;
}

function json(body, status, extra) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      ...extra,
    },
  });
}

function stub(env) {
  // account 単位で 1 つ。名前を固定するので、どのリージョンから来ても同じ DO に入る。
  const id = env.NIGHTWATCH.idFromName(String(env.NQX_ACCOUNT_ID || "default"));
  return env.NIGHTWATCH.get(id);
}

/** WebSocket は独自ヘッダーを送れないので、subprotocol にトークンを載せる。 */
function launchTokenFromProtocols(request) {
  const raw = request.headers.get("Sec-WebSocket-Protocol") || "";
  for (const part of raw.split(",")) {
    const value = part.trim();
    if (value.startsWith(WS_TOKEN_PREFIX)) return value.slice(WS_TOKEN_PREFIX.length);
  }
  return "";
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const nowMs = Date.now();
    const cors = corsHeaders(env, request);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors });
    }

    if (url.pathname === "/api/health") {
      return json({ ok: true, service: "nqx-nightwatch-api", serverTime: new Date(nowMs).toISOString() }, 200, cors);
    }

    // ------------------------------------------------ 読み取り: Mini App
    if (url.pathname === "/api/state" && request.method === "GET") {
      const auth = await authorizeReader(request, url, env, nowMs);
      if (!auth.ok) return json({ ok: false, reason: auth.reason }, auth.status, cors);
      const response = await stub(env).fetch(new Request("https://do/state", { method: "GET" }));
      const body = await response.text();
      return new Response(body, {
        status: response.status,
        headers: {
          "Content-Type": "application/json; charset=utf-8",
          "Cache-Control": "no-store",
          "X-Content-Type-Options": "nosniff",
          ...cors,
        },
      });
    }

    // ------------------------------------------------ AUTO: 認証済み Mini App
    // これは注文ではなく、監視PCが新規ENTRYを検討してよい期限付き権限。
    // 口座・銘柄・期限は Worker の設定から確定し、ブラウザの値を信用しない。
    if (url.pathname === "/api/autotrade" && request.method === "POST") {
      const bodyText = await request.text();
      if (bodyText.length > 4 * 1024) {
        return json({ ok: false, reason: "autotrade body is too large" }, 413, cors);
      }
      // CSRF: AUTO を動かせるのは Mini App のオリジンから来た要求だけ。
      const origin = verifyMutationOrigin(request, env);
      if (!origin.ok) return json({ ok: false, reason: origin.reason }, origin.status, cors);
      const auth = await authorizeReader(request, url, env, nowMs);
      if (!auth.ok) return json({ ok: false, reason: auth.reason }, auth.status, cors);
      const response = await stub(env).fetch(new Request("https://do/autotrade", {
        method: "POST",
        body: bodyText,
        headers: {
          "Content-Type": "application/json",
          "X-NQX-Authorized-User": auth.userId,
          "X-NQX-Auth-Source": auth.source,
        },
      }));
      const responseText = await response.text();
      return new Response(responseText, {
        status: response.status,
        headers: {
          "Content-Type": "application/json; charset=utf-8",
          "Cache-Control": "no-store",
          "X-Content-Type-Options": "nosniff",
          ...cors,
        },
      });
    }

    // ------------------------------------------------ 口座別設定: 認証済み Mini App
    // ULTRA の対象口座・利益目標・DD上限の意図だけを保存する。注文は送らない。
    // 実際の枚数・リスクは監視PC側の ultra_mode + 実行契約が再計算・クランプする。
    if (url.pathname === "/api/accountPrefs" && request.method === "POST") {
      const bodyText = await request.text();
      if (bodyText.length > 4 * 1024) {
        return json({ ok: false, reason: "accountPrefs body is too large" }, 413, cors);
      }
      // CSRF: 口座別設定も Mini App のオリジンからだけ変えられる。
      const origin = verifyMutationOrigin(request, env);
      if (!origin.ok) return json({ ok: false, reason: origin.reason }, origin.status, cors);
      const auth = await authorizeReader(request, url, env, nowMs);
      if (!auth.ok) return json({ ok: false, reason: auth.reason }, auth.status, cors);
      const response = await stub(env).fetch(new Request("https://do/accountPrefs", {
        method: "POST",
        body: bodyText,
        headers: {
          "Content-Type": "application/json",
          "X-NQX-Authorized-User": auth.userId,
          "X-NQX-Auth-Source": auth.source,
        },
      }));
      const responseText = await response.text();
      return new Response(responseText, {
        status: response.status,
        headers: {
          "Content-Type": "application/json; charset=utf-8",
          "Cache-Control": "no-store",
          "X-Content-Type-Options": "nosniff",
          ...cors,
        },
      });
    }

    // ------------------------------------------------ 手動HALT(R87): 認証済み Mini App
    // 「自律経路を止めたい」という意図だけを保存する。止めるのは監視PC側で、撤退
    // (KILL / flatten)は塞がない。AUTO と同じく CSRF + 認証を通ったものだけ受ける。
    if (url.pathname === "/api/manualHalt" && request.method === "POST") {
      const bodyText = await request.text();
      if (bodyText.length > 1024) {
        return json({ ok: false, reason: "manualHalt body is too large" }, 413, cors);
      }
      const origin = verifyMutationOrigin(request, env);
      if (!origin.ok) return json({ ok: false, reason: origin.reason }, origin.status, cors);
      const auth = await authorizeReader(request, url, env, nowMs);
      if (!auth.ok) return json({ ok: false, reason: auth.reason }, auth.status, cors);
      const response = await stub(env).fetch(new Request("https://do/manualHalt", {
        method: "POST",
        body: bodyText,
        headers: {
          "Content-Type": "application/json",
          "X-NQX-Authorized-User": auth.userId,
          "X-NQX-Auth-Source": auth.source,
        },
      }));
      const responseText = await response.text();
      return new Response(responseText, {
        status: response.status,
        headers: {
          "Content-Type": "application/json; charset=utf-8",
          "Cache-Control": "no-store",
          "X-Content-Type-Options": "nosniff",
          ...cors,
        },
      });
    }

    if (url.pathname === "/api/ws") {
      if (request.headers.get("Upgrade") !== "websocket") {
        return json({ ok: false, reason: "expected a websocket upgrade" }, 426, cors);
      }
      // WS は launch token のみ。initData は `=`/`&` を含み subprotocol に載せられない。
      // クエリ(`?t=`)では受けない。WebSocket の URL は中継ログに残りやすく、
      // subprotocol なら残らない。Mini App は常に subprotocol で送る。
      const token = launchTokenFromProtocols(request);
      const probe = new Request(url.toString(), { headers: { "X-NQX-Launch": token } });
      const auth = await authorizeReader(probe, new URL(url), env, nowMs);
      if (!auth.ok) return json({ ok: false, reason: auth.reason }, auth.status, cors);

      const response = await stub(env).fetch(new Request("https://do/ws", {
        headers: { Upgrade: "websocket", Connection: "Upgrade" },
      }));
      // 選択した subprotocol を返さないと、ブラウザ側が接続を落とす。
      const headers = new Headers(response.headers);
      if ((request.headers.get("Sec-WebSocket-Protocol") || "").includes(WS_PROTOCOL)) {
        headers.set("Sec-WebSocket-Protocol", WS_PROTOCOL);
      }
      return new Response(response.body, { status: response.status, webSocket: response.webSocket, headers });
    }

    // ------------------------------------------------ 書き込み: ローカル PC のみ
    if (url.pathname === "/api/publish" && request.method === "POST") {
      const bodyText = await request.text();
      if (bodyText.length > 256 * 1024) {
        return json({ ok: false, reason: "body is too large" }, 413, cors);
      }
      const auth = await verifyPublishSignature(request, bodyText, env, nowMs);
      if (!auth.ok) return json({ ok: false, reason: auth.reason }, auth.status, cors);
      if (auth.accountId !== String(env.NQX_ACCOUNT_ID || "default")) {
        return json({ ok: false, reason: "account mismatch" }, 403, cors);
      }
      const response = await stub(env).fetch(new Request("https://do/publish", {
        method: "POST",
        body: bodyText,
        headers: { "Content-Type": "application/json" },
      }));
      const text = await response.text();
      return new Response(text, {
        status: response.status,
        headers: {
          "Content-Type": "application/json; charset=utf-8",
          "Cache-Control": "no-store",
          "X-Content-Type-Options": "nosniff",
          ...cors,
        },
      });
    }

    // ------------------------------------------------ 受信箱(無期限停止)
    // 旧クライアントが呼んでも DO や認証経路へ到達させない。保存済み chat 表は
    // 削除せず保全するが、受信箱の状態を監視・分析・発注のブロッカーにしない。
    if (url.pathname === "/api/chat") {
      return json({
        ok: false,
        enabled: false,
        status: "INBOX_DISABLED_INDEFINITELY",
        blocking: false,
        reason: "inbox disabled indefinitely; monitoring continues",
      }, 410, cors);
    }

    if (url.pathname === "/api/events" && request.method === "GET") {
      // 監査ログは PC 署名でのみ読める。ブラウザには出さない。
      const auth = await verifyPublishSignature(request, "", env, nowMs);
      if (!auth.ok) return json({ ok: false, reason: auth.reason }, auth.status, cors);
      const response = await stub(env).fetch(new Request("https://do/events", { method: "GET" }));
      const text = await response.text();
      return new Response(text, {
        status: response.status,
        headers: {
          "Content-Type": "application/json; charset=utf-8",
          "Cache-Control": "no-store",
          "X-Content-Type-Options": "nosniff",
          ...cors,
        },
      });
    }

    return json({ ok: false, reason: "not found" }, 404, cors);
  },
};
