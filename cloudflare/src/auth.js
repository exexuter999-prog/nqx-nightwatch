/**
 * 認証と整合性検証。WebCrypto だけを使い、Workers でも Node でもそのまま動く。
 *
 * 2 方向ある:
 *   1) Mini App(ブラウザ) → Worker : 状態読み取りと期限付きAUTO権限の変更。
 *      Telegram initData か Bot発行launch tokenで許可済みuserかを確認する。
 *   2) ローカル PC(Bot/monitor) → Worker : 状態更新。HMAC 署名 +
 *      timestamp + nonce + body hash + 単調増加 revision を全て検証する。
 *
 * どちらの経路も、ここを通っただけでは発注しない。実送信は監視PCの
 * autotrade_engineか、手動確認時のBotだけが行う。
 */

const encoder = new TextEncoder();

async function hmacKey(rawKey) {
  const keyData = typeof rawKey === "string" ? encoder.encode(rawKey) : rawKey;
  return crypto.subtle.importKey("raw", keyData, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
}

async function hmacBytes(rawKey, message) {
  const key = await hmacKey(rawKey);
  const sig = await crypto.subtle.sign("HMAC", key, encoder.encode(message));
  return new Uint8Array(sig);
}

export function toHex(bytes) {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export async function hmacHex(rawKey, message) {
  return toHex(await hmacBytes(rawKey, message));
}

export async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", encoder.encode(text));
  return toHex(new Uint8Array(digest));
}

/** 長さと内容の両方で早期 return しない比較。 */
export function timingSafeEqual(a, b) {
  const left = String(a ?? "");
  const right = String(b ?? "");
  // 長さが違う場合も同じ回数だけ回す。長さ差自体は隠せないが、内容は漏らさない。
  const length = Math.max(left.length, right.length);
  let diff = left.length ^ right.length;
  for (let i = 0; i < length; i += 1) {
    diff |= (left.charCodeAt(i) || 0) ^ (right.charCodeAt(i) || 0);
  }
  return diff === 0;
}

// ---------------------------------------------------------------- Telegram initData

/**
 * Telegram Mini App の initData を検証する。
 *
 * 注意: initData が供給されるのは inline button / menu button / direct link で
 * 起動した場合のみ。keyboard button 起動(= sendData が使える唯一のモード)では
 * Telegram が initData を渡さない。そのモードでは verifyLaunchToken を使う。
 */
export async function verifyTelegramInitData(initData, botToken, { nowMs, maxAgeSec = 86400 } = {}) {
  if (!initData || typeof initData !== "string") return { ok: false, reason: "initData is empty" };
  if (!botToken) return { ok: false, reason: "bot token is not configured" };

  const params = new URLSearchParams(initData);
  const hash = params.get("hash");
  if (!hash) return { ok: false, reason: "initData has no hash" };

  const pairs = [];
  for (const [key, value] of params.entries()) {
    if (key === "hash" || key === "signature") continue;
    pairs.push(`${key}=${value}`);
  }
  pairs.sort();
  const dataCheckString = pairs.join("\n");

  const secretKey = await hmacBytes("WebAppData", botToken);
  const expected = await hmacHex(secretKey, dataCheckString);
  if (!timingSafeEqual(expected, hash)) return { ok: false, reason: "initData hash mismatch" };

  const authDate = Number(params.get("auth_date"));
  if (!Number.isFinite(authDate)) return { ok: false, reason: "initData auth_date is missing" };
  const ageSec = (nowMs - authDate * 1000) / 1000;
  if (ageSec > maxAgeSec) return { ok: false, reason: "initData is expired" };
  if (ageSec < -300) return { ok: false, reason: "initData auth_date is in the future" };

  let user = null;
  try {
    user = JSON.parse(params.get("user") || "null");
  } catch {
    return { ok: false, reason: "initData user is malformed" };
  }
  if (!user || !user.id) return { ok: false, reason: "initData has no user" };

  return { ok: true, userId: String(user.id), source: "initData", authDate };
}

// ---------------------------------------------------------------- launch token

/**
 * keyboard button 起動用のフォールバック認証。
 * Bot が Mini App の URL を組み立てるときに発行し、短時間だけ有効にする。
 * 形式: v1.<userId>.<expUnixSec>.<hmacHex>
 */
export async function issueLaunchToken(secret, userId, expUnixSec) {
  const body = `v1:${userId}:${expUnixSec}`;
  const sig = await hmacHex(secret, body);
  return `v1.${userId}.${expUnixSec}.${sig}`;
}

export async function verifyLaunchToken(token, secret, { nowMs } = {}) {
  if (!token || typeof token !== "string") return { ok: false, reason: "launch token is empty" };
  if (!secret) return { ok: false, reason: "launch secret is not configured" };
  const parts = token.split(".");
  if (parts.length !== 4 || parts[0] !== "v1") return { ok: false, reason: "launch token is malformed" };
  const [, userId, expText, sig] = parts;
  const exp = Number(expText);
  if (!Number.isFinite(exp)) return { ok: false, reason: "launch token exp is invalid" };
  const expected = await hmacHex(secret, `v1:${userId}:${expText}`);
  if (!timingSafeEqual(expected, sig)) return { ok: false, reason: "launch token signature mismatch" };
  if (exp * 1000 <= nowMs) return { ok: false, reason: "launch token is expired" };
  return { ok: true, userId: String(userId), source: "launchToken", exp };
}

/**
 * 読み取り経路の入口。initData を優先し、無い場合だけ launch token を見る。
 * どちらで通っても、許可済み user id と一致しなければ拒否する。
 *
 * 資格情報は **リクエストヘッダからしか読まない**。以前はクエリ文字列の
 * `tgWebAppData` / `t` も受けていたが、URL に載った資格情報は Referer・
 * ブラウザ履歴・中継ログ・スクリーンショットへ流れ出る。Mini App は URL から
 * 読んだ launch token を必ずヘッダへ載せ替えて送るため(state_client.js の
 * authHeaders)、クエリを見る必要は無い。`url` は呼び出し側の互換のために
 * 受け取るだけで、認証判断には一切使わない。
 */
export async function authorizeReader(request, url, env, nowMs) {
  const allowed = String(env.NQX_ALLOWED_USER_ID || "").trim();
  if (!allowed) return { ok: false, status: 500, reason: "NQX_ALLOWED_USER_ID is not configured" };

  const initData = request.headers.get("X-Telegram-Init-Data") || "";
  if (initData) {
    const result = await verifyTelegramInitData(initData, env.TELEGRAM_BOT_TOKEN, { nowMs });
    if (!result.ok) return { ok: false, status: 401, reason: result.reason };
    if (!timingSafeEqual(result.userId, allowed)) return { ok: false, status: 403, reason: "user is not allowed" };
    return { ok: true, userId: result.userId, source: "initData" };
  }

  const token = request.headers.get("X-NQX-Launch") || "";
  const result = await verifyLaunchToken(token, env.NQX_LAUNCH_SECRET, { nowMs });
  if (!result.ok) return { ok: false, status: 401, reason: result.reason };
  if (!timingSafeEqual(result.userId, allowed)) return { ok: false, status: 403, reason: "user is not allowed" };
  return { ok: true, userId: result.userId, source: "launchToken" };
}

// ---------------------------------------------------------------- オリジン(CSRF)

/** 許可オリジンの表。CORS ヘッダの組み立てと CSRF 判定で同じ表を使う。 */
export function allowedOrigins(env) {
  return String(env.NQX_ALLOWED_ORIGIN || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

/**
 * 状態を変える経路(AUTO・口座別設定)の CSRF ゲート。
 *
 * ブラウザは Origin を偽装できないので、Mini App 以外のページに踏まされても
 * ここで落ちる。Origin を持たないリクエスト(curl や別アプリ)も落とす —
 * この2経路を叩いてよいのは Mini App だけで、監視PC は署名付きの
 * `/api/publish` を使う。読み取りには掛けない(WebSocket と PC の状態照会が
 * Origin を持たないため)。
 */
export function verifyMutationOrigin(request, env) {
  const list = allowedOrigins(env);
  if (!list.length) return { ok: false, status: 500, reason: "NQX_ALLOWED_ORIGIN is not configured" };
  const origin = request.headers.get("Origin") || "";
  if (!origin) return { ok: false, status: 403, reason: "origin header is required" };
  if (!list.includes(origin)) return { ok: false, status: 403, reason: "origin is not allowed" };
  return { ok: true, origin };
}

// ---------------------------------------------------------------- PC → Worker 署名

export const PUBLISH_MAX_SKEW_SEC = 120;

export function publishSigningString(timestamp, nonce, accountId, bodyHash) {
  return `v1:${timestamp}:${nonce}:${accountId}:${bodyHash}`;
}

/**
 * 状態更新リクエストの署名を検証する。
 * nonce の一意性はここでは見ない(永続ストアが要るので DO 側で消費する)。
 */
export async function verifyPublishSignature(request, bodyText, env, nowMs) {
  const secret = env.NQX_PUBLISH_SECRET;
  if (!secret) return { ok: false, status: 500, reason: "NQX_PUBLISH_SECRET is not configured" };

  const timestamp = request.headers.get("X-NQX-Timestamp") || "";
  const nonce = request.headers.get("X-NQX-Nonce") || "";
  const accountId = request.headers.get("X-NQX-Account") || "";
  const signature = request.headers.get("X-NQX-Signature") || "";
  if (!timestamp || !nonce || !accountId || !signature) {
    return { ok: false, status: 401, reason: "signature headers are incomplete" };
  }
  if (nonce.length < 16 || nonce.length > 128) {
    return { ok: false, status: 401, reason: "nonce length is out of range" };
  }

  const tsSec = Number(timestamp);
  if (!Number.isFinite(tsSec)) return { ok: false, status: 401, reason: "timestamp is invalid" };
  const skew = Math.abs(nowMs / 1000 - tsSec);
  if (skew > PUBLISH_MAX_SKEW_SEC) return { ok: false, status: 401, reason: "timestamp is outside the accepted window" };

  const bodyHash = await sha256Hex(bodyText);
  const expected = await hmacHex(secret, publishSigningString(timestamp, nonce, accountId, bodyHash));
  if (!timingSafeEqual(expected, signature)) return { ok: false, status: 401, reason: "signature mismatch" };

  return { ok: true, nonce, accountId, timestamp: tsSec };
}
