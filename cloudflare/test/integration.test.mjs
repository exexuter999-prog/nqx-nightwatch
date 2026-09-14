/**
 * Worker + Durable Object + WebSocket の結合テスト。
 * wrangler dev(ローカル workerd)を起動し、本物の SQLite DO と hibernatable
 * WebSocket に対して検証する。外部ネットワークにも CrossTrade にも到達しない。
 *
 *   node --test test/integration.test.mjs
 */
import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { setTimeout as sleep } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import net from "node:net";
import { hmacHex, issueLaunchToken, publishSigningString, sha256Hex } from "../src/auth.js";
import { entryKeyForTuple, strategyEvidenceHash, managementIntentHash,
  managementKeyForIntent, brokerObservationHash } from "../src/state_machine.js";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
async function reserveLoopbackPort() {
  return await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : null;
      server.close((error) => error ? reject(error) : resolve(port));
    });
  });
}

const PORT = await reserveLoopbackPort();
const BASE = `http://127.0.0.1:${PORT}`;

const ALLOWED_ORIGIN = "http://127.0.0.1";
const USER_ID = "4242";
// `wrangler dev` の Durable Object ストレージは実行をまたいで残ることが
// ある。固定 account ID では前回実行の revision / nonce を読んでしまうため、
// 各テストプロセスを独立した DO namespace key にする。
const ACCOUNT = `integration-${process.pid}-${Date.now()}`;
const EXECUTION_ACCOUNTS = ["SIM-ACCOUNT-A", "SIM-ACCOUNT-B"];
const PUBLISH_SECRET = "integration-publish-secret";
const LAUNCH_SECRET = "integration-launch-secret";

let child = null;
let childExit = null;
let childLog = "";
let launchToken = "";
let nonceCounter = 0;

function nextNonce() {
  nonceCounter += 1;
  return `itest-nonce-${Date.now()}-${nonceCounter}`;
}

function marketPayload(price, overrides = {}) {
  const now = Date.now();
  return {
    verified: true,
    observedAt: new Date(now).toISOString(),
    source: "TradingView quote_get",
    sourceSymbol: "CME_MINI:MNQ1!",
    resolution: "15",
    barResolution: "3",
    price,
    cvdAt: new Date(now).toISOString(),
    regime: "MX",
    bars: [
      { t: Math.floor(now / 1000) - 180, o: price - 1, h: price + 1, l: price - 2, c: price - 0.5 },
      { t: Math.floor(now / 1000), o: price - 0.5, h: price + 1, l: price - 1, c: price },
    ],
    levels: [{ name: "C: POC", price: price - 4 }],
    ...overrides,
  };
}

async function publish(event, { nonce = nextNonce(), account = ACCOUNT, tsSec = null } = {}) {
  const body = JSON.stringify({ ...event, nonce });
  const timestamp = String(tsSec ?? Math.floor(Date.now() / 1000));
  const signature = await hmacHex(
    PUBLISH_SECRET, publishSigningString(timestamp, nonce, account, await sha256Hex(body)),
  );
  const response = await fetch(`${BASE}/api/publish`, {
    method: "POST",
    body,
    headers: {
      "Content-Type": "application/json",
      "X-NQX-Timestamp": timestamp,
      "X-NQX-Nonce": nonce,
      "X-NQX-Account": account,
      "X-NQX-Signature": signature,
    },
  });
  return { status: response.status, body: await response.json(), nonce };
}

async function getState() {
  const response = await fetch(`${BASE}/api/state`, { headers: { "X-NQX-Launch": launchToken } });
  return { status: response.status, body: await response.json() };
}

async function setAutotrade(enabled, token = launchToken, ttlMinutes = 420) {
  const response = await fetch(`${BASE}/api/autotrade`, {
    method: "POST",
    // Origin は Mini App のオリジン。状態を変える経路は CSRF ゲートを通る。
    headers: { "Content-Type": "application/json", "X-NQX-Launch": token, "Origin": ALLOWED_ORIGIN },
    body: JSON.stringify({ enabled, ttlMinutes }),
  });
  return { status: response.status, body: await response.json() };
}

function openSocket() {
  const ws = new WebSocket(`${BASE.replace("http", "ws")}/api/ws`, ["nqx.v1", `nqx-token.${launchToken}`]);
  const inbox = [];
  ws.addEventListener("message", (event) => inbox.push(JSON.parse(event.data)));
  return { ws, inbox };
}

async function waitFor(predicate, { timeoutMs = 8000, label = "condition" } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (childExit) {
      throw new Error(`wrangler exited before ${label}: ${childExit}; output:\n${childLog.slice(-4000)}`);
    }
    const value = await predicate();
    if (value) return value;
    await sleep(100);
  }
  throw new Error(`timed out waiting for ${label}`);
}

function scenario(overrides = {}) {
  const now = Date.now();
  return {
    scenarioId: "sc-int-1",
    fingerprint: "fp-int-1",
    state: "ACTIVE",
    symbol: "MNQU6",
    side: "BUY",
    qty: 1,
    entry: 29906.5,
    stop: 29871.5,
    target: 29922.75,
    grade: "A",
    issuedAt: new Date(now).toISOString(),
    observedAt: new Date(now).toISOString(),
    expiresAt: new Date(now + 15 * 60_000).toISOString(),
    ...overrides,
  };
}

function position(overrides = {}) {
  return {
    verified: true,
    source: "tradovate-rest",
    symbol: "MNQU6",
    side: "LONG",
    qty: 2,
    avgEntry: 29906.5,
    observedAt: new Date().toISOString(),
    ...overrides,
  };
}

before(async () => {
  launchToken = await issueLaunchToken(LAUNCH_SECRET, USER_ID, Math.floor(Date.now() / 1000) + 3600);

  // Windows では .cmd を直接 spawn できない(Node の EINVAL 対策)。
  // ローカルの wrangler エントリを node で直接起動する。
  const wranglerBin = path.join(ROOT, "node_modules", "wrangler", "bin", "wrangler.js");
  child = spawn(
    process.execPath,
    [
      wranglerBin, "dev",
      "--port", String(PORT),
      "--ip", "127.0.0.1",
      "--var", `NQX_ALLOWED_USER_ID:${USER_ID}`,
      "--var", `NQX_ACCOUNT_ID:${ACCOUNT}`,
      "--var", "NQX_SYMBOL:MNQU6",
      "--var", `NQX_AUTOTRADE_ACCOUNTS:${EXECUTION_ACCOUNTS.join(",")}`,
      "--var", "NQX_AUTOTRADE_MINUTES:420",
      "--var", "NQX_AUTOTRADE_MAX_MINUTES:720",
      "--var", `NQX_ALLOWED_ORIGIN:${ALLOWED_ORIGIN}`,
      "--var", `NQX_PUBLISH_SECRET:${PUBLISH_SECRET}`,
      "--var", `NQX_LAUNCH_SECRET:${LAUNCH_SECRET}`,
      "--var", "TELEGRAM_BOT_TOKEN:000:INTEGRATION-TEST-ONLY",
    ],
    { cwd: ROOT, env: { ...process.env, CI: "1", WRANGLER_SEND_METRICS: "false" }, stdio: ["ignore", "pipe", "pipe"] },
  );
  const capture = (chunk) => { childLog = (childLog + chunk.toString()).slice(-8000); };
  child.stdout.on("data", capture);
  child.stderr.on("data", capture);
  child.once("exit", (code, signal) => { childExit = `code=${code} signal=${signal}`; });

  await waitFor(async () => {
    try {
      const response = await fetch(`${BASE}/api/health`);
      return response.ok;
    } catch {
      return false;
    }
  }, { timeoutMs: 90_000, label: "wrangler dev to become healthy" });
});

after(async () => {
  if (child && !child.killed) {
    child.kill("SIGTERM");
    await sleep(500);
    if (!child.killed) child.kill("SIGKILL");
  }
});

// ---------------------------------------------------------------- 認証

test("認証なしの /api/state は 401", async () => {
  const response = await fetch(`${BASE}/api/state`);
  assert.equal(response.status, 401);
});

test("受信箱は読み書きとも無期限停止し、DOへ到達しない", async () => {
  for (const init of [
    { method: "GET" },
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: "x" }) },
  ]) {
    const response = await fetch(`${BASE}/api/chat`, init);
    assert.equal(response.status, 410);
    const body = await response.json();
    assert.equal(body.enabled, false);
    assert.equal(body.blocking, false);
    assert.equal(body.status, "INBOX_DISABLED_INDEFINITELY");
  }
});

test("偽の launch token は通らない", async () => {
  const forged = await issueLaunchToken("wrong-secret", USER_ID, Math.floor(Date.now() / 1000) + 3600);
  const response = await fetch(`${BASE}/api/state`, { headers: { "X-NQX-Launch": forged } });
  assert.equal(response.status, 401);
});

test("署名なしの publish は通らない", async () => {
  const response = await fetch(`${BASE}/api/publish`, {
    method: "POST", body: JSON.stringify({ stream: "market", revision: 1, nonce: "x".repeat(20) }),
  });
  assert.equal(response.status, 401);
});

test("正しい launch token なら state を読める", async () => {
  const { status, body } = await getState();
  assert.equal(status, 200);
  assert.equal(body.ok, true);
  assert.equal(body.view.symbol, "MNQU6");
  assert.ok(body.view.serverTime);
});

test("認証済みAUTO ONはサーバー確定の2口座・銘柄・期限でLIVE武装する", async () => {
  const { status, body } = await setAutotrade(true, launchToken, 9999);
  assert.equal(status, 200);
  assert.equal(body.ok, true);
  assert.equal(body.arm.enabled, true);
  assert.equal(body.arm.live, true);
  assert.equal(body.arm.source, "TELEGRAM_MINI_APP");
  assert.equal(body.arm.symbol, "MNQU6");
  assert.deepEqual(body.arm.accountScope, EXECUTION_ACCOUNTS);
  const lifetimeMs = Date.parse(body.arm.expiresAt) - Date.parse(body.arm.armedAt);
  assert.equal(lifetimeMs, 720 * 60_000, "client request is capped by the server maximum");

  const snapshot = await getState();
  assert.equal(snapshot.body.view.autotradeArm.armId, body.arm.armId);
});

test("AUTO mutationは認証なしでは拒否し、OFFは即時に新規ENTRY権限を落とす", async () => {
  // Origin が無い書き込みは、資格情報を持っていても CSRF ゲートで落ちる。
  const noOrigin = await fetch(`${BASE}/api/autotrade`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-NQX-Launch": launchToken },
    body: JSON.stringify({ enabled: true }),
  });
  assert.equal(noOrigin.status, 403);

  // 別サイトから踏まされた場合も同じ。
  const foreignOrigin = await fetch(`${BASE}/api/autotrade`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-NQX-Launch": launchToken,
      "Origin": "https://evil.example",
    },
    body: JSON.stringify({ enabled: true }),
  });
  assert.equal(foreignOrigin.status, 403);

  // オリジンが正しくても、資格情報が無ければ 401。
  const unauthorized = await fetch(`${BASE}/api/autotrade`, {
    method: "POST", headers: { "Content-Type": "application/json", "Origin": ALLOWED_ORIGIN },
    body: JSON.stringify({ enabled: true }),
  });
  assert.equal(unauthorized.status, 401);

  // 資格情報をクエリに載せても通らない(URL に載った token は受け付けない)。
  const queryToken = await fetch(`${BASE}/api/state?t=${encodeURIComponent(launchToken)}`);
  assert.equal(queryToken.status, 401);

  const { status, body } = await setAutotrade(false);
  assert.equal(status, 200);
  assert.equal(body.arm.enabled, false);
  assert.equal(body.arm.autotrade, false);
  assert.equal(body.arm.live, false);
  assert.equal(body.arm.status, "OFF");
});

// ---------------------------------------------------------------- publish と整合性

test("scenario を publish すると state に載る", async () => {
  // FLAT は「未確認」ではなく、broker の verified=true/qty=0 で明示する。
  const flat = await publish({
    stream: "position", revision: 9,
    payload: { position: {
      verified: true, source: "tradovate-rest", symbol: "MNQU6", qty: 0,
      observedAt: new Date().toISOString(),
    } },
  });
  assert.equal(flat.status, 200);
  const currentMarket = await publish({
    stream: "market", revision: 9,
    payload: { market: marketPayload(29906.5) },
  });
  assert.equal(currentMarket.status, 200);
  const result = await publish({ stream: "scenario", revision: 10, payload: { scenario: scenario() } });
  assert.equal(result.status, 200);
  const { body } = await getState();
  assert.equal(body.view.scenario.scenarioId, "sc-int-1");
  assert.equal(body.view.display.priority, "SCENARIO_ACTIVE");
  assert.equal(body.view.display.orderable, false);
  assert.match(body.view.display.blockReason, /CYCLE_MISMATCH/);
});

test("同じ nonce の再送は 409 で拒否される(重複 event が状態を動かさない)", async () => {
  const first = await publish({ stream: "market", revision: 100, payload: { market: marketPayload(29900) } });
  assert.equal(first.status, 200);

  const body = JSON.stringify({ stream: "market", revision: 101, payload: { market: marketPayload(30000) }, nonce: first.nonce });
  const timestamp = String(Math.floor(Date.now() / 1000));
  const signature = await hmacHex(
    PUBLISH_SECRET, publishSigningString(timestamp, first.nonce, ACCOUNT, await sha256Hex(body)),
  );
  const response = await fetch(`${BASE}/api/publish`, {
    method: "POST",
    body,
    headers: {
      "Content-Type": "application/json",
      "X-NQX-Timestamp": timestamp,
      "X-NQX-Nonce": first.nonce,
      "X-NQX-Account": ACCOUNT,
      "X-NQX-Signature": signature,
    },
  });
  assert.equal(response.status, 409);
  assert.equal((await response.json()).duplicate, true);

  const { body: after } = await getState();
  assert.equal(after.view.market.price, 29900, "重複 nonce で状態が動いた");
});

test("古い revision の scenario は拒否され、現行 scenario が復活・巻き戻りしない", async () => {
  await publish({
    stream: "scenario", revision: 11,
    payload: { scenario: scenario({ scenarioId: "sc-int-2", fingerprint: "fp-int-2" }) },
  });
  const stale = await publish({
    stream: "scenario", revision: 5,
    payload: { scenario: scenario({ scenarioId: "sc-int-1", fingerprint: "fp-int-1" }) },
  });
  assert.equal(stale.status, 409);

  const { body } = await getState();
  assert.equal(body.view.scenario.scenarioId, "sc-int-2", "古い payload で巻き戻った");
});

// ---------------------------------------------------------------- position

test("約定 position は publish 後も保持され、scenario 更新で消えない", async () => {
  await publish({ stream: "position", revision: 10, payload: { position: position() } });
  let { body } = await getState();
  assert.equal(body.view.position.state, "OPEN");
  assert.equal(body.view.display.priority, "POSITION_OPEN");
  assert.equal(body.view.display.orderable, false);

  // 新しい scenario を流し込んでも position は無傷
  await publish({
    stream: "scenario", revision: 20,
    payload: { scenario: scenario({ scenarioId: "sc-int-3", fingerprint: "fp-int-3", side: "SELL", entry: 29900, stop: 29930, target: 29850 }) },
  });
  ({ body } = await getState());
  assert.equal(body.view.position.state, "OPEN");
  assert.equal(body.view.position.qty, 2);
  assert.equal(body.view.display.priority, "POSITION_OPEN");
  assert.equal(body.view.display.blockReason, "POSITION OPEN — MANAGEMENT ONLY");
});

test("照会失敗を publish しても position は消えず STALE で残る", async () => {
  await publish({
    stream: "position", revision: 11,
    payload: { position: { verified: false, source: "tradovate-rest", observedAt: new Date().toISOString() } },
  });
  const { body } = await getState();
  assert.equal(body.view.position.state, "STALE");
  assert.equal(body.view.position.qty, 2);
  assert.equal(body.view.position.lastKnownState, "OPEN");
});

test("部分決済で残数量が更新される", async () => {
  await publish({ stream: "position", revision: 12, payload: { position: position({ qty: 1 }) } });
  const { body } = await getState();
  assert.equal(body.view.position.state, "PARTIAL");
  assert.equal(body.view.position.qty, 1);
  assert.equal(body.view.position.initialQty, 2);
});

test("qty=0 でも closedAt が無ければ position は閉じない", async () => {
  const result = await publish({ stream: "position", revision: 13, payload: { position: position({ qty: 0 }) } });
  assert.equal(result.status, 409);
  const { body } = await getState();
  assert.equal(body.view.position.qty, 1, "closedAt 無しで建玉が消えた");
});

// ---------------------------------------------------------------- WebSocket

test("WebSocket は接続時に完全 snapshot を送る", async () => {
  const { ws, inbox } = openSocket();
  try {
    const snapshot = await waitFor(() => inbox.find((m) => m.type === "snapshot"), { label: "snapshot" });
    assert.equal(snapshot.view.symbol, "MNQU6");
    assert.equal(snapshot.view.position.qty, 1);
  } finally {
    ws.close();
  }
});

test("state 更新が WebSocket に差分 push される", async () => {
  const { ws, inbox } = openSocket();
  try {
    await waitFor(() => inbox.find((m) => m.type === "snapshot"), { label: "snapshot" });
    await publish({ stream: "market", revision: 200, payload: { market: marketPayload(29950.25, { regime: "TREND" }) } });
    const delta = await waitFor(
      () => inbox.find((m) => m.type === "delta" && m.view.market?.price === 29950.25),
      { label: "market delta" },
    );
    assert.equal(delta.view.market.regime, "TREND");
  } finally {
    ws.close();
  }
});

test("切断中の更新を挟んでも、再接続後の snapshot が最新に収束する", async () => {
  const first = openSocket();
  await waitFor(() => first.inbox.find((m) => m.type === "snapshot"), { label: "first snapshot" });
  first.ws.close();
  await sleep(300);

  // 切断中に 3 回更新する
  await publish({ stream: "market", revision: 300, payload: { market: marketPayload(30001) } });
  await publish({ stream: "market", revision: 301, payload: { market: marketPayload(30002) } });
  await publish({ stream: "market", revision: 302, payload: { market: marketPayload(30003.75) } });

  const second = openSocket();
  try {
    const snapshot = await waitFor(
      () => second.inbox.find((m) => m.type === "snapshot"),
      { label: "reconnect snapshot" },
    );
    assert.equal(snapshot.view.market.price, 30003.75, "再接続後に最新へ収束しなかった");
    assert.equal(snapshot.view.revisions.market, 302);

    // 明示的な resync でも同じ結果になること
    second.ws.send(JSON.stringify({ type: "resync" }));
    const resynced = await waitFor(
      () => second.inbox.filter((m) => m.type === "snapshot")[1],
      { label: "resync snapshot" },
    );
    assert.equal(resynced.view.market.price, 30003.75);
  } finally {
    second.ws.close();
  }
});

// ---------------------------------------------------------------- 期限

test("scenario は producer の追加送信なしでサーバー時刻だけで消える", async () => {
  // position を先に完全決済しておく(表示優先順位が scenario に降りるように)
  await publish({
    stream: "position", revision: 400,
    payload: { position: position({ qty: 0 }), closedAt: new Date().toISOString() },
  });

  const now = Date.now();
  const result = await publish({
    stream: "scenario", revision: 500,
    payload: {
      scenario: scenario({
        scenarioId: "sc-expire", fingerprint: "fp-expire",
        issuedAt: new Date(now - 1000).toISOString(),
        observedAt: new Date(now - 1000).toISOString(),
        expiresAt: new Date(now + 3000).toISOString(),
      }),
    },
  });
  assert.equal(result.status, 200);

  const before = await getState();
  assert.equal(before.body.view.scenario.scenarioId, "sc-expire");

  // ここから先、PC からは何も送らない(Bot 停止相当)
  await sleep(4200);
  const after = await getState();
  assert.equal(after.body.view.scenario, null, "期限後も scenario が残った");
  assert.equal(after.body.view.display.priority, "SCENARIO_NONE");
  assert.equal(after.body.view.display.blockReason, "NO ACTIVE SCENARIO");
});

test("決済結果は publish され、現在の状態を押しのけない", async () => {
  const now = Date.now();
  await publish({ stream: "position", revision: 600, payload: { position: position() } });

  const result = await publish({
    stream: "result", revision: 600,
    payload: {
      result: {
        resultId: "rs-int-1", side: "LONG", symbol: "MNQU6", qty: 2,
        entry: 29760, exit: 29796, stop: 29726, pointValue: 2,
        openedAt: new Date(now - 14 * 60_000).toISOString(),
        closedAt: new Date(now).toISOString(),
        path: [29760, 29768.25, 29781, 29796],
        pathSource: "observed-bars", exitSource: "broker",
        verdict: "They let this one through.", mode: "SIMULATION",
      },
    },
  });
  assert.equal(result.status, 200);

  const { body } = await getState();
  assert.equal(body.view.result.resultId, "rs-int-1");
  assert.equal(body.view.result.pathPoints, 4);
  assert.equal(body.view.position.state, "OPEN", "result で建玉が消えた");
  assert.equal(body.view.display.priority, "POSITION_OPEN", "result が現在の状態を押しのけた");
});

test("出所不明の path / 導出値つきの result は拒否される", async () => {
  const now = Date.now();
  const base = {
    resultId: "rs-int-bad", side: "LONG", symbol: "MNQU6", qty: 1,
    entry: 29760, exit: 29796, stop: 29726, pointValue: 2,
    openedAt: new Date(now - 60_000).toISOString(), closedAt: new Date(now).toISOString(),
    path: [29760, 29796], pathSource: "endpoints-only", exitSource: "manual", mode: "SIMULATION",
  };

  const synthetic = await publish({
    stream: "result", revision: 700,
    payload: { result: { ...base, pathSource: "synthetic" } },
  });
  assert.equal(synthetic.status, 409);

  const derived = await publish({
    stream: "result", revision: 701,
    payload: { result: { ...base, pnl: 72 } },
  });
  assert.equal(derived.status, 409);

  const { body } = await getState();
  assert.equal(body.view.result.resultId, "rs-int-1", "拒否されたはずの result が載った");
});

test("workerd Durable Object serializes concurrent cross-producer ENTRY claims", async () => {
  const now = Date.now();
  const cycleId = `cy-claim-${now}`;
  const evidenceRaw = {
    version: "R14-STRATEGY-EVIDENCE-1", asOf: new Date(now).toISOString(),
    sessionId: "NY-INTEGRATION", source: "fixture", provenance: "workerd-test",
    models: { ifvg: { valid: false } },
  };
  const evidence = { ...evidenceRaw, evidenceHash: strategyEvidenceHash(evidenceRaw) };
  const frozenScenario = {
    scenarioId: `sc-${cycleId}`, fingerprint: `fp-${cycleId}`, state: "ARMED",
    symbol: "MNQU6", side: "BUY", qty: 2, entry: 20000, stop: 19940,
    target: 20060, targets: [20060, 20120], targetR: [1, 2],
    legs: [{ id: "TP1", qty: 1, target: 20060 },
      { id: "RUNNER", qty: 1, target: 20120 }],
    planVersion: "R18-INTEGRATION-SPLIT-1", grade: "A",
    issuedAt: new Date(now).toISOString(), observedAt: new Date(now).toISOString(),
    expiresAt: new Date(now + 5 * 60_000).toISOString(), marketCycleId: cycleId,
    evidenceHash: evidence.evidenceHash,
    setupVersion: "R14-SETUP", catalogVersion: "R14-CATALOG",
    detectorVersion: "R14-DETECTOR", executionContractVersion: "R22-EXECUTION-CONTRACT-1",
    executionContract: { version: "R22-EXECUTION-CONTRACT-1", riskCapDollars: 240,
      riskCapSource: "fixture", accountScope: ["APEX0001"] },
  };
  const flat = await publish({ stream: "position", revision: 900,
    payload: { position: { verified: true, source: "broker", symbol: "MNQU6", qty: 0,
      observedAt: new Date(now).toISOString() }, closedAt: new Date(now).toISOString() } });
  assert.equal(flat.status, 200);
  const cycle = await publish({ stream: "cycle", revision: 900,
    payload: { cycleId, market: marketPayload(20000, {
      cycleId, resolution: "3", strategyEvidence: evidence,
      dayguard: { at: new Date(now).toISOString(), available: true, blocked: false, codes: [] },
    }), scenario: frozenScenario } });
  assert.equal(cycle.status, 200);

  const tuple = Object.fromEntries(["scenarioId", "fingerprint", "evidenceHash", "marketCycleId"]
    .map((field) => [field, frozenScenario[field]]));
  const entryKey = entryKeyForTuple(tuple);
  const claims = await Promise.all([
    publish({ stream: "entry_claim", revision: 901, payload: { action: "CLAIM", entryKey,
      tuple, claimTokenHash: "a".repeat(64), orderType: "LIMIT" } }),
    publish({ stream: "entry_claim", revision: 901, payload: { action: "CLAIM", entryKey,
      tuple, claimTokenHash: "b".repeat(64), orderType: "LIMIT" } }),
  ]);
  assert.equal(claims.filter((result) => result.status === 200).length, 1);
  assert.equal(claims.filter((result) => result.status === 409).length, 1);
  const state = await getState();
  assert.equal(state.body.view.entryClaim.entryKey, entryKey);
  assert.equal(state.body.view.entryClaim.state, "CLAIMED");
  const consumeBase = { action: "CONSUME", entryKey,
    executionIntent: state.body.view.entryClaim.executionIntent,
    executionIntentHash: state.body.view.entryClaim.executionIntentHash };
  let winner = "a";
  let consumed = await publish({ stream: "entry_claim", revision: 902,
    payload: { ...consumeBase, claimTokenHash: "a".repeat(64) } });
  if (consumed.status !== 200) {
    winner = "b";
    consumed = await publish({ stream: "entry_claim", revision: 902,
      payload: { ...consumeBase, claimTokenHash: "b".repeat(64) } });
  }
  assert.equal(consumed.status, 200, JSON.stringify(consumed.body));
  const routeSnapshot = [
    { accountId: "APEX0001", legId: "TP1", state: "ACCEPTED", orderId: "WI-E1", receipt: "WI-R1" },
    { accountId: "APEX0001", legId: "RUNNER", state: "ACCEPTED", orderId: "WI-E2", receipt: "WI-R2" },
  ];
  const resolved = await publish({ stream: "entry_claim", revision: 903, payload: {
    action: "RESOLVE", entryKey, claimTokenHash: winner.repeat(64), routeState: "SENT",
    acceptedCount: 2, explicitRejectCount: 0, totalAttempts: 2, routeSnapshot } });
  assert.equal(resolved.status, 200, JSON.stringify(resolved.body));
  const at = new Date(Date.now() - 1000).toISOString();
  const observation = { observedAt: at, positionObservedAt: at, ordersObservedAt: at,
    snapshotId: "bs-workerd-entry-terminal", cursor: null, snapshotMode: "STABLE_DOUBLE_READ",
    stableBeforeHash: "6".repeat(64), stableAfterHash: "6".repeat(64), platform: "STUB",
    accountId: "APEX0001", symbol: "MNQU6",
    currentIntentHash: state.body.view.entryClaim.executionIntentHash,
    position: { verified: true, qty: 0, side: "FLAT",
      positionGeneration: null, rawPositionIdentity: null },
    orders: routeSnapshot.map((row) => ({ orderId: row.orderId, receipt: row.receipt,
      accountId: row.accountId, symbol: "MNQU6", status: "CANCELED" })) };
  observation.snapshotHash = brokerObservationHash(observation);
  const journaled = await publish({ stream: "broker_observation", revision: 800,
    payload: { observation } });
  assert.equal(journaled.status, 200, JSON.stringify(journaled.body));
  const recovered = await publish({ stream: "entry_claim", revision: 904, payload: {
    action: "RECOVER", entryKey, claimTokenHash: winner.repeat(64),
    brokerSnapshotHash: observation.snapshotHash,
    brokerSnapshotId: observation.snapshotId, brokerCursor: "" } });
  assert.equal(recovered.status, 200, JSON.stringify(recovered.body));
  const tombstoneReplay = await publish({ stream: "entry_claim", revision: 905,
    payload: { action: "CLAIM", entryKey, tuple,
      claimTokenHash: "f".repeat(64), orderType: "LIMIT" } });
  assert.equal(tombstoneReplay.status, 409);
  assert.match(String(tombstoneReplay.body.reason), /TOMBSTONED/);
});

test("workerd Durable Object serializes Bot-vs-AUTO MANAGEMENT claims", async () => {
  const now = Date.now();
  const positionGeneration = `POS:${"9".repeat(64)}`;
  const position = await publish({ stream: "position", revision: 902, payload: { position: {
    verified: true, source: "broker", symbol: "MNQU6", side: "LONG", qty: 1,
    accountId: "APEX0001", orderId: "FILL-M19", filledAt: new Date(now).toISOString(),
    positionGeneration, observedAt: new Date(now).toISOString(), avgEntry: 20000,
  } } });
  assert.equal(position.status, 200);
  const intent = { version: "R22-MANAGEMENT-INTENT-1", accountId: "APEX0001",
    symbol: "MNQU6", positionGeneration, action: "MODIFY", side: "BUY", qty: 1,
    stop: "20000.00", target: "20120.00",
    executionContractVersion: "R22-EXECUTION-CONTRACT-1" };
  const managementKey = managementKeyForIntent(intent);
  const payload = { action: "CLAIM", managementKey,
    managementIntent: intent, managementIntentHash: managementIntentHash(intent) };
  const claims = await Promise.all([
    publish({ stream: "management_claim", revision: 903,
      payload: { ...payload, claimTokenHash: "c".repeat(64) } }),
    publish({ stream: "management_claim", revision: 903,
      payload: { ...payload, claimTokenHash: "d".repeat(64) } }),
  ]);
  assert.equal(claims.filter((result) => result.status === 200).length, 1);
  assert.equal(claims.filter((result) => result.status === 409).length, 1);
  const state = await getState();
  assert.equal(state.body.view.managementClaim.managementKey, managementKey);
  assert.equal(state.body.view.managementClaim.state, "CLAIMED");
});

test("workerd R22 broker observation rejects stale higher revision and CAS replay", async () => {
  const state = await getState();
  const intentHash = state.body.view.managementClaim.managementIntentHash;
  const seal = (at, position, snapshotId) => {
    const observation = { observedAt: at, positionObservedAt: at, ordersObservedAt: at,
      snapshotId, cursor: null, snapshotMode: "STABLE_DOUBLE_READ",
      stableBeforeHash: "7".repeat(64), stableAfterHash: "7".repeat(64),
      platform: "STUB", accountId: "APEX0001", symbol: "MNQU6",
      currentIntentHash: intentHash, position,
      orders: [{ orderId: "OBS-1", receipt: "OBS-R1", accountId: "APEX0001",
        symbol: "MNQU6", status: "CANCELED" }] };
    return { ...observation, snapshotHash: brokerObservationHash(observation) };
  };
  const t1 = new Date(Date.now() - 20_000).toISOString();
  const t10 = new Date(Date.now() - 10_000).toISOString();
  const flat = seal(t1, { verified: true, qty: 0, side: "FLAT",
    positionGeneration: null, rawPositionIdentity: null }, "bs-workerd-flat");
  const first = await publish({ stream: "broker_observation", revision: 1001,
    payload: { observation: flat } });
  assert.equal(first.status, 200, JSON.stringify(first.body));
  const open = seal(t10, { verified: true, qty: 1, side: "LONG",
    positionGeneration: state.body.view.managementClaim.managementIntent.positionGeneration,
    rawPositionIdentity: state.body.view.managementClaim.managementIntent.positionGeneration },
  "bs-workerd-open");
  const second = await publish({ stream: "broker_observation", revision: 1002,
    payload: { observation: open } });
  assert.equal(second.status, 200, JSON.stringify(second.body));
  const staleHigher = await publish({ stream: "broker_observation", revision: 1003,
    payload: { observation: { ...flat, snapshotId: "bs-workerd-old-high",
      snapshotHash: brokerObservationHash({ ...flat, snapshotId: "bs-workerd-old-high",
        snapshotHash: undefined }) } } });
  assert.equal(staleHigher.status, 409);
  const recoveryReplay = await publish({ stream: "management_claim", revision: 1004,
    payload: { action: "RECOVER",
      managementKey: state.body.view.managementClaim.managementKey,
      claimTokenHash: "c".repeat(64), brokerSnapshotHash: flat.snapshotHash,
      brokerSnapshotId: flat.snapshotId, brokerCursor: "" } });
  assert.equal(recoveryReplay.status, 409);
  const view = await getState();
  assert.equal(view.body.view.brokerObservation.snapshotHash, open.snapshotHash);
  assert.equal(view.body.view.brokerObservation.position.qty, 1);
});

test("Worker に発注経路が存在しない", async () => {
  for (const route of ["/api/order", "/api/confirm", "/api/send", "/api/execute"]) {
    const response = await fetch(`${BASE}${route}`, { method: "POST", headers: { "X-NQX-Launch": launchToken } });
    assert.equal(response.status, 404, `${route} が存在してしまっている`);
  }
});
