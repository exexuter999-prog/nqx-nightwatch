/**
 * ローカル確認用のシード。実注文経路には一切触れない。
 *
 *   node test/dev_seed.mjs <scenario|position|stale|partial|clear|flat>
 *
 * wrangler dev が 8799 で動いている前提。最後に Mini App を開く URL を出す。
 */
import { hmacHex, issueLaunchToken, publishSigningString, sha256Hex } from "../src/auth.js";

const BASE = "http://127.0.0.1:8799";
const ACCOUNT = "lucid-50k-daily";
const PUBLISH_SECRET = "integration-publish-secret";
const LAUNCH_SECRET = "integration-launch-secret";
const USER_ID = "4242";

let counter = 0;
async function publish(stream, payload) {
  counter += 1;
  const nonce = `seed-${Date.now()}-${counter}`;
  const body = JSON.stringify({ stream, revision: Date.now() + counter, nonce, payload });
  const timestamp = String(Math.floor(Date.now() / 1000));
  const signature = await hmacHex(
    PUBLISH_SECRET, publishSigningString(timestamp, nonce, ACCOUNT, await sha256Hex(body)),
  );
  const response = await fetch(`${BASE}/api/publish`, {
    method: "POST",
    body,
    headers: {
      "Content-Type": "application/json",
      "X-NQX-Timestamp": timestamp,
      "X-NQX-Nonce": nonce,
      "X-NQX-Account": ACCOUNT,
      "X-NQX-Signature": signature,
    },
  });
  console.log(stream, response.status, JSON.stringify(await response.json()).slice(0, 160));
}

function bars() {
  const out = [];
  let price = 29850;
  for (let i = 0; i < 24; i += 1) {
    const open = price;
    price += (i % 5 === 0 ? -1 : 1) * (3 + (i % 7));
    out.push({ t: Math.floor(Date.now() / 1000) - (24 - i) * 180, o: open,
      h: Math.max(open, price) + 2, l: Math.min(open, price) - 2, c: price, v: 4000 + i * 90 });
  }
  return out;
}

const mode = process.argv[2] || "scenario";
const now = Date.now();

await publish("market", {
  market: { verified: true, observedAt: new Date(now).toISOString(), price: 29918.5,
    source: "TradingView local seed", sourceSymbol: "CME_MINI:MNQ1!",
    resolution: "15", barResolution: "3", vwap: 29872.78, cvd: 31903,
    regime: "TREND", bars: bars(), levels: [{ label: "C: VAH", price: 29906.47 }] },
});

if (mode === "scenario" || mode === "position" || mode === "stale" || mode === "partial") {
  await publish("scenario", {
    scenario: {
      scenarioId: "sc-dev-1", fingerprint: "fp-dev-1", state: "ACTIVE", symbol: "MNQU6",
      side: "BUY", qty: 1, entry: 29906.5, stop: 29871.5, target: 29922.75,
      title: "C: VAH hold / upper-band test",
      reason: "VAH 29,906.47 を上抜けたあとの retest-hold を待つ。VWAP 上限 29,922.83 まで。",
      issuedAt: new Date(now - 60_000).toISOString(),
      observedAt: new Date(now - 60_000).toISOString(),
      expiresAt: new Date(now + Number(process.argv[3] || 10) * 60_000).toISOString(),
    },
  });
}

if (mode === "position" || mode === "stale" || mode === "partial") {
  await publish("position", {
    position: { verified: true, source: "tradovate-rest", symbol: "MNQU6", side: "LONG",
      qty: 2, avgEntry: 29906.5, stop: 29871.5, target: 29922.75, currentPrice: 29918.5,
      unrealizedPnl: 48, observedAt: new Date(now).toISOString() },
  });
}

if (mode === "partial") {
  await publish("position", {
    position: { verified: true, source: "tradovate-rest", symbol: "MNQU6", side: "LONG",
      qty: 1, avgEntry: 29906.5, stop: 29871.5, target: 29922.75, currentPrice: 29920.0,
      unrealizedPnl: 27, observedAt: new Date(now).toISOString() },
  });
}

if (mode === "stale") {
  await publish("position", {
    position: { verified: false, source: "tradovate-rest", observedAt: new Date(now).toISOString() },
  });
}

if (mode === "clear") {
  await publish("scenario", { scenario: null, state: "INVALIDATED" });
}

// 決済結果。path は上の bars() と同じ実バー相当の系列から切り出す。
// 合成の値動きは作らない(HANDOFF §3 / §11-1)。
if (mode === "win" || mode === "loss" || mode === "flat") {
  const observed = bars().slice(-6).map((b) => b.c);
  const spec = {
    win:  { side: "LONG",  entry: 29760, exit: 29796,   stop: 29726, minutes: 14 },
    loss: { side: "LONG",  entry: 29760, exit: 29726,   stop: 29726, minutes: 6 },
    flat: { side: "SHORT", entry: 29760, exit: 29759.5, stop: 29794, minutes: 41 },
  }[mode];
  const openedAt = new Date(now - spec.minutes * 60_000).toISOString();
  await publish("result", {
    result: {
      resultId: `rs-dev-${mode}-${now}`,
      side: spec.side, symbol: "MNQU6", qty: 2,
      entry: spec.entry, exit: spec.exit, stop: spec.stop, pointValue: 2,
      openedAt, closedAt: new Date(now).toISOString(),
      path: [spec.entry, ...observed, spec.exit],
      pathSource: "observed-bars",
      exitSource: "broker",
      verdict: { win: "They let this one through.", loss: "You were the liquidity.",
                 flat: "Nothing happened. Again." }[mode],
      mode: "SIMULATION",
    },
  });
}

if (mode === "endpoints") {
  const openedAt = new Date(now - 9 * 60_000).toISOString();
  await publish("result", {
    result: {
      resultId: `rs-dev-endpoints-${now}`,
      side: "LONG", symbol: "MNQU6", qty: 1,
      entry: 29760, exit: 29782.25, stop: 29740, pointValue: 2,
      openedAt, closedAt: new Date(now).toISOString(),
      path: [29760, 29782.25],
      pathSource: "endpoints-only",
      exitSource: "manual",
      verdict: "Taken early. Still taken.",
      mode: "SIMULATION",
    },
  });
}

if (mode === "flat") {
  await publish("position", {
    position: { verified: true, source: "tradovate-rest", symbol: "MNQU6", qty: 0,
      observedAt: new Date(now).toISOString(), receipt: "dev-receipt" },
    closedAt: new Date(now).toISOString(),
  });
}

const token = await issueLaunchToken(LAUNCH_SECRET, USER_ID, Math.floor(now / 1000) + 6 * 3600);
console.log(`\nOPEN: http://127.0.0.1:8765/?api=${encodeURIComponent(BASE)}&t=${token}`);
