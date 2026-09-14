/**
 * 認証と署名の検証。ネットワークを一切使わない。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  allowedOrigins, authorizeReader, hmacHex, issueLaunchToken, sha256Hex, timingSafeEqual,
  verifyLaunchToken, verifyMutationOrigin, verifyPublishSignature, verifyTelegramInitData,
  publishSigningString,
} from "../src/auth.js";

const BOT_TOKEN = "123456:TEST-TOKEN-DO-NOT-USE";
const PUBLISH_SECRET = "publish-secret-for-tests-only";
const LAUNCH_SECRET = "launch-secret-for-tests-only";
const NOW = Date.parse("2026-08-13T02:00:00Z");

async function makeInitData(userId, authDateMs, token = BOT_TOKEN) {
  const params = new URLSearchParams();
  params.set("auth_date", String(Math.floor(authDateMs / 1000)));
  params.set("query_id", "AAE");
  params.set("user", JSON.stringify({ id: userId, first_name: "T" }));

  const pairs = [...params.entries()].map(([k, v]) => `${k}=${v}`).sort();
  const secretKey = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode("WebAppData"), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const secretBytes = new Uint8Array(await crypto.subtle.sign("HMAC", secretKey, new TextEncoder().encode(token)));
  params.set("hash", await hmacHex(secretBytes, pairs.join("\n")));
  return params.toString();
}

test("timingSafeEqual は長さ違いも内容違いも false にする", () => {
  assert.equal(timingSafeEqual("abc", "abc"), true);
  assert.equal(timingSafeEqual("abc", "abd"), false);
  assert.equal(timingSafeEqual("abc", "abcd"), false);
  assert.equal(timingSafeEqual("", ""), true);
});

test("正しい initData は検証を通る", async () => {
  const initData = await makeInitData(4242, NOW - 60_000);
  const result = await verifyTelegramInitData(initData, BOT_TOKEN, { nowMs: NOW });
  assert.equal(result.ok, true);
  assert.equal(result.userId, "4242");
});

test("改竄された initData は落ちる", async () => {
  const initData = await makeInitData(4242, NOW - 60_000);
  const tampered = initData.replace("4242", "9999");
  const result = await verifyTelegramInitData(tampered, BOT_TOKEN, { nowMs: NOW });
  assert.equal(result.ok, false);
  assert.match(result.reason, /hash mismatch/);
});

test("別のトークンで署名された initData は落ちる", async () => {
  const initData = await makeInitData(4242, NOW - 60_000, "999:OTHER");
  const result = await verifyTelegramInitData(initData, BOT_TOKEN, { nowMs: NOW });
  assert.equal(result.ok, false);
});

test("古い initData は期限切れで落ちる", async () => {
  const initData = await makeInitData(4242, NOW - 3 * 86400_000);
  const result = await verifyTelegramInitData(initData, BOT_TOKEN, { nowMs: NOW });
  assert.equal(result.ok, false);
  assert.match(result.reason, /expired/);
});

test("launch token は署名と期限の両方を見る", async () => {
  const token = await issueLaunchToken(LAUNCH_SECRET, "4242", Math.floor(NOW / 1000) + 3600);
  assert.equal((await verifyLaunchToken(token, LAUNCH_SECRET, { nowMs: NOW })).ok, true);

  // 署名不一致
  const tampered = token.slice(0, -1) + (token.endsWith("a") ? "b" : "a");
  assert.equal((await verifyLaunchToken(tampered, LAUNCH_SECRET, { nowMs: NOW })).ok, false);

  // user id をすり替えると署名が合わない
  const swapped = token.replace(".4242.", ".9999.");
  assert.equal((await verifyLaunchToken(swapped, LAUNCH_SECRET, { nowMs: NOW })).ok, false);

  // 期限切れ
  const expired = await issueLaunchToken(LAUNCH_SECRET, "4242", Math.floor(NOW / 1000) - 1);
  const result = await verifyLaunchToken(expired, LAUNCH_SECRET, { nowMs: NOW });
  assert.equal(result.ok, false);
  assert.match(result.reason, /expired/);
});

test("許可されていない user id は initData が正しくても通さない", async () => {
  const env = { TELEGRAM_BOT_TOKEN: BOT_TOKEN, NQX_LAUNCH_SECRET: LAUNCH_SECRET, NQX_ALLOWED_USER_ID: "4242" };
  const initData = await makeInitData(7777, NOW - 60_000);
  const request = new Request("https://api.test/api/state", { headers: { "X-Telegram-Init-Data": initData } });
  const result = await authorizeReader(request, new URL(request.url), env, NOW);
  assert.equal(result.ok, false);
  assert.equal(result.status, 403);
});

test("認証情報が何も無ければ通らない", async () => {
  const env = { TELEGRAM_BOT_TOKEN: BOT_TOKEN, NQX_LAUNCH_SECRET: LAUNCH_SECRET, NQX_ALLOWED_USER_ID: "4242" };
  const request = new Request("https://api.test/api/state");
  const result = await authorizeReader(request, new URL(request.url), env, NOW);
  assert.equal(result.ok, false);
  assert.equal(result.status, 401);
});

test("NQX_ALLOWED_USER_ID 未設定なら fail-closed で 500", async () => {
  const result = await authorizeReader(new Request("https://api.test/api/state"), new URL("https://api.test/api/state"), {}, NOW);
  assert.equal(result.ok, false);
  assert.equal(result.status, 500);
});

// ---------------------------------------------------------------- publish 署名

async function signedRequest(body, { nonce = "nonce-0123456789abcdef", tsSec = Math.floor(NOW / 1000), account = "acct" } = {}) {
  const bodyHash = await sha256Hex(body);
  const signature = await hmacHex(PUBLISH_SECRET, publishSigningString(String(tsSec), nonce, account, bodyHash));
  return new Request("https://api.test/api/publish", {
    method: "POST",
    body,
    headers: {
      "X-NQX-Timestamp": String(tsSec),
      "X-NQX-Nonce": nonce,
      "X-NQX-Account": account,
      "X-NQX-Signature": signature,
    },
  });
}

test("正しい署名の publish は通る", async () => {
  const body = JSON.stringify({ stream: "market", revision: 1 });
  const env = { NQX_PUBLISH_SECRET: PUBLISH_SECRET };
  const result = await verifyPublishSignature(await signedRequest(body), body, env, NOW);
  assert.equal(result.ok, true);
  assert.equal(result.accountId, "acct");
});

test("body を差し替えると署名が合わない", async () => {
  const body = JSON.stringify({ stream: "market", revision: 1 });
  const env = { NQX_PUBLISH_SECRET: PUBLISH_SECRET };
  const request = await signedRequest(body);
  const result = await verifyPublishSignature(request, JSON.stringify({ stream: "order", revision: 99 }), env, NOW);
  assert.equal(result.ok, false);
  assert.match(result.reason, /signature mismatch/);
});

test("期限外 timestamp の publish は拒否される", async () => {
  const body = "{}";
  const env = { NQX_PUBLISH_SECRET: PUBLISH_SECRET };
  const old = await signedRequest(body, { tsSec: Math.floor(NOW / 1000) - 600 });
  const result = await verifyPublishSignature(old, body, env, NOW);
  assert.equal(result.ok, false);
  assert.match(result.reason, /accepted window/);
});

test("nonce が短すぎる publish は拒否される", async () => {
  const body = "{}";
  const env = { NQX_PUBLISH_SECRET: PUBLISH_SECRET };
  const request = await signedRequest(body, { nonce: "short" });
  const result = await verifyPublishSignature(request, body, env, NOW);
  assert.equal(result.ok, false);
  assert.match(result.reason, /nonce length/);
});

test("NQX_PUBLISH_SECRET 未設定なら fail-closed で 500", async () => {
  const result = await verifyPublishSignature(await signedRequest("{}"), "{}", {}, NOW);
  assert.equal(result.ok, false);
  assert.equal(result.status, 500);
});

// ---------------------------------------------------------------- URL に載せた資格情報

test("クエリの launch token では通らない(URL に資格情報を載せさせない)", async () => {
  const env = { TELEGRAM_BOT_TOKEN: BOT_TOKEN, NQX_LAUNCH_SECRET: LAUNCH_SECRET, NQX_ALLOWED_USER_ID: "4242" };
  const token = await issueLaunchToken(LAUNCH_SECRET, "4242", Math.floor(NOW / 1000) + 600);
  const request = new Request(`https://api.test/api/state?t=${encodeURIComponent(token)}`);
  const result = await authorizeReader(request, new URL(request.url), env, NOW);
  assert.equal(result.ok, false);
  assert.equal(result.status, 401);

  // 同じ token でもヘッダなら通る — 拒否しているのは経路であって token ではない。
  const viaHeader = new Request("https://api.test/api/state", { headers: { "X-NQX-Launch": token } });
  const allowed = await authorizeReader(viaHeader, new URL(viaHeader.url), env, NOW);
  assert.equal(allowed.ok, true);
  assert.equal(allowed.source, "launchToken");
});

test("クエリの tgWebAppData では通らない", async () => {
  const env = { TELEGRAM_BOT_TOKEN: BOT_TOKEN, NQX_LAUNCH_SECRET: LAUNCH_SECRET, NQX_ALLOWED_USER_ID: "4242" };
  const initData = await makeInitData(4242, NOW - 60_000);
  const request = new Request(`https://api.test/api/state?tgWebAppData=${encodeURIComponent(initData)}`);
  const result = await authorizeReader(request, new URL(request.url), env, NOW);
  assert.equal(result.ok, false);
  assert.equal(result.status, 401);

  const viaHeader = new Request("https://api.test/api/state", { headers: { "X-Telegram-Init-Data": initData } });
  const allowed = await authorizeReader(viaHeader, new URL(viaHeader.url), env, NOW);
  assert.equal(allowed.ok, true);
  assert.equal(allowed.source, "initData");
});

// ---------------------------------------------------------------- CSRF(Origin)

test("許可オリジンからの書き込みだけ通る", () => {
  const env = { NQX_ALLOWED_ORIGIN: "https://nqx.pages.dev, https://alt.pages.dev" };
  assert.deepEqual(allowedOrigins(env), ["https://nqx.pages.dev", "https://alt.pages.dev"]);

  const ok = verifyMutationOrigin(
    new Request("https://api.test/api/autotrade", { method: "POST", headers: { Origin: "https://alt.pages.dev" } }), env);
  assert.equal(ok.ok, true);

  const foreign = verifyMutationOrigin(
    new Request("https://api.test/api/autotrade", { method: "POST", headers: { Origin: "https://evil.example" } }), env);
  assert.equal(foreign.ok, false);
  assert.equal(foreign.status, 403);

  // Origin を持たない要求(curl・別アプリ)も書き込みには入れない。
  const bare = verifyMutationOrigin(new Request("https://api.test/api/autotrade", { method: "POST" }), env);
  assert.equal(bare.ok, false);
  assert.equal(bare.status, 403);
});

test("NQX_ALLOWED_ORIGIN 未設定なら書き込みは fail-closed で 500", () => {
  const result = verifyMutationOrigin(
    new Request("https://api.test/api/autotrade", { method: "POST", headers: { Origin: "https://nqx.pages.dev" } }), {});
  assert.equal(result.ok, false);
  assert.equal(result.status, 500);
});
