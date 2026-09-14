// SYSTEM ROUTE(system.js)の導出。
//
// ここが甘いと「経路が切れているのに画面は緑のまま」になる。証拠が無いものを
// UP にしないこと、窓の外を OFF と呼ぶこと、DOWN が一つでもあれば総評が
// DOWN に倒れることを固定する。描画側は DOM を持たないのでソースで検査する。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { deriveRoute, engineState, routeSignature, NODE_STATES } from "../system.js";

const SYSTEM = readFileSync(new URL("../system.js", import.meta.url), "utf8");
const APP = readFileSync(new URL("../app.js", import.meta.url), "utf8");
const HTML = readFileSync(new URL("../index.html", import.meta.url), "utf8");
const STYLES = readFileSync(new URL("../styles.css", import.meta.url), "utf8");

// 2026-08-28(金)22:00 JST = 窓内。
const NOW = Date.parse("2026-08-28T22:00:00+09:00");
const iso = (offsetMs = 0) => new Date(NOW + offsetMs).toISOString();

function healthyView(extra = {}) {
  return {
    seq: 412,
    serverTime: iso(),
    lastVerifiedAt: iso(-40_000),
    scenario: null,
    position: null,
    order: null,
    entryClaim: null,
    managementClaim: null,
    positionCheck: { verified: true, source: "crosstrade", observedAt: iso(-40_000) },
    brokerObservation: { observedAt: iso(-40_000), platform: "tradovate" },
    market: {
      publishedAt: iso(-30_000), observedAt: iso(-30_000), at: iso(-30_000),
      verified: true, stale: false, ageMs: 30_000, sourceSymbol: "CME_MINI:MNQU2026",
      evaluation: {
        at: iso(-30_000),
        volGate: { ratio: 0.15, noise: 9.1, slCap: 60, ruling: "通常" },
        dataGate: { requiredFresh: true, freshCount: 8, requiredCount: 8, oldestAgeSec: 31, sourceSpanSec: 3, staleRequired: [] },
        cvdGate: { status: "FRESH", available: true, attempts: 1, maxAttempts: 2, aplusAllowed: true },
        decision: { model: "BREAKER_CONTINUATION", side: "BUY", state: "WATCH", grade: "B", hardBlockers: [] },
      },
    },
    cycleHealth: { status: "PUBLISHED", at: iso(-30_000), publishedAt: iso(-30_000), reason: null, kill: false },
    autotradeArm: { enabled: true, autotrade: true, live: true, expiresAt: iso(6 * 3_600_000), accountScope: ["LFE00000000000024"], symbol: "MNQU6" },
    accounts: { observedAt: iso(-30_000), totalBuffer: 2396, list: [{ id: "LFE00000000000024", label: "…0024", buffer: 2396 }],
      sync: { verified: true, missing: [], unknown: [], dead: [] } },
    display: { orderable: false, blockReason: "NO ACTIVE SCENARIO", cyclePaired: false },
    ...extra,
  };
}

const LIVE = { connection: "LIVE", appOffline: false, inTelegram: true, canSend: true, platform: "android", clockSynced: true, ping: { ok: true, rttMs: 84 } };

const byId = (model, id) => model.lanes.flatMap((lane) => lane.nodes).find((item) => item.id === id);

test("健全な view + LIVE 接続では全ノードが UP で総評は ALL LINKS UP", () => {
  const model = deriveRoute(healthyView(), LIVE, NOW);
  assert.equal(model.lanes.length, 2);
  assert.equal(model.total, 9);
  for (const lane of model.lanes) {
    for (const item of lane.nodes) {
      assert.ok(NODE_STATES.includes(item.state));
      assert.equal(item.state, "UP", `${item.id} は UP (${item.word} / ${item.detail})`);
    }
  }
  assert.equal(model.verdict, "ALL LINKS UP");
  assert.equal(model.tone, "up");
  assert.equal(byId(model, "tradingview").detail.startsWith("8/8 raw"), true);
  assert.match(byId(model, "cloudflare").detail, /rtt 84ms/);
  assert.match(byId(model, "arm").detail, /1 acct/);
  assert.equal(byId(model, "engine").word, "STANDBY", "AUTO ON で何も飛んでいなければ STANDBY");
  // 隣り合う UP 同士のリンクは流れる
  assert.equal(model.lanes[0].nodes[0].linkAfter, "flow");
  assert.equal(model.lanes[0].nodes.at(-1).linkAfter, null, "末端にリンクは無い");
});

test("view が無ければ推測せず UNKNOWN(端末ノードだけは自分の環境から出す)", () => {
  const model = deriveRoute(null, { connection: "CONNECTING", inTelegram: false }, NOW);
  for (const item of model.lanes.flatMap((lane) => lane.nodes)) {
    if (item.id === "device") { assert.equal(item.state, "WARN"); assert.equal(item.word, "BROWSER"); continue; }
    if (item.id === "cloudflare") { assert.equal(item.state, "UNKNOWN"); continue; }
    assert.equal(item.state, "UNKNOWN", `${item.id} は UNKNOWN`);
  }
  assert.equal(model.verdict, "NO LINK DATA");
  // ブラウザで直接開いた(api 無し)ときは、端末の WARN ではなく「状態サービス無し」を総評にする。
  const unconfigured = deriveRoute(null, { connection: "UNCONFIGURED", inTelegram: false }, NOW);
  assert.equal(byId(unconfigured, "cloudflare").state, "OFF");
  assert.equal(unconfigured.verdict, "NO STATE SERVICE");
  assert.equal(unconfigured.tone, "unknown");
});

test("受領書が非 FRESH なら TRADINGVIEW は DOWN、総評も DOWN に倒れる", () => {
  const view = healthyView();
  view.market.evaluation.dataGate = { requiredFresh: false, freshCount: 6, requiredCount: 8,
    staleRequired: ["study_3m.json", "pine_labels.json"] };
  const model = deriveRoute(view, LIVE, NOW);
  const tv = byId(model, "tradingview");
  assert.equal(tv.state, "DOWN");
  assert.equal(tv.word, "STALE");
  assert.match(tv.detail, /study_3m\.json/);
  assert.equal(tv.linkAfter, "broken", "DOWN の隣のリンクは切れる");
  assert.equal(model.verdict, "1 LINK DOWN");
  assert.equal(model.tone, "down");
});

test("ビーコンの BLOCKED / HALT / KILL はループとエンジンに映る", () => {
  const blocked = healthyView({ cycleHealth: { status: "BLOCKED", at: iso(-30_000), publishedAt: iso(-30_000), reason: "acquisition — stale raw", kill: false } });
  const b = deriveRoute(blocked, LIVE, NOW);
  assert.equal(byId(b, "loop").state, "WARN");
  assert.equal(byId(b, "loop").word, "BLOCKED");
  assert.match(byId(b, "loop").detail, /acquisition/);
  assert.equal(byId(b, "engine").state, "UP", "BLOCKED はエンジンを止めない");

  const killed = healthyView({ cycleHealth: { status: "HALT", at: iso(-30_000), publishedAt: iso(-30_000), reason: "kill", kill: true } });
  const k = deriveRoute(killed, LIVE, NOW);
  assert.equal(byId(k, "loop").state, "DOWN");
  assert.equal(byId(k, "loop").word, "KILL");
  assert.equal(byId(k, "engine").state, "DOWN");
  assert.equal(byId(k, "engine").word, "KILL");
});

test("監視窓の外は OFF(故障ではない)", () => {
  const sunday = Date.parse("2026-08-30T15:00:00+09:00");
  const view = healthyView();
  view.market.publishedAt = new Date(Date.parse("2026-08-29T03:57:00+09:00")).toISOString();
  view.cycleHealth = null;
  view.market.evaluation.at = view.market.publishedAt;
  const model = deriveRoute(view, LIVE, sunday);
  assert.equal(byId(model, "tradingview").state, "OFF");
  assert.equal(byId(model, "loop").state, "OFF");
  assert.match(byId(model, "loop").detail, /resumes MON 07:00/);
  assert.equal(byId(model, "evaluation").state, "OFF");
  assert.equal(byId(model, "engine").state, "OFF");
  assert.equal(byId(model, "engine").word, "OFF DUTY");
  // 窓の外でも Worker とブローカー照会は生きていれば UP のまま
  assert.equal(byId(model, "cloudflare").state, "UP");
});

test("アプリがオフラインなら『ループが死んだ』と断定しない", () => {
  const model = deriveRoute(healthyView(), { ...LIVE, connection: "OFFLINE", appOffline: true, connectionDetail: "state 502" }, NOW);
  assert.equal(byId(model, "loop").state, "UNKNOWN");
  assert.match(byId(model, "loop").detail, /app offline/);
  assert.equal(byId(model, "cloudflare").state, "DOWN");
  assert.match(byId(model, "cloudflare").detail, /502/);
});

test("AUTO の期限・OFF・ブローカー未確認・口座消失を一つずつ写す", () => {
  const expired = deriveRoute(healthyView({ autotradeArm: { enabled: true, autotrade: true, live: true, expiresAt: iso(-1000), accountScope: [] } }), LIVE, NOW);
  assert.equal(byId(expired, "arm").state, "WARN");
  assert.equal(byId(expired, "arm").word, "EXPIRED");

  const off = deriveRoute(healthyView({ autotradeArm: null }), LIVE, NOW);
  assert.equal(byId(off, "arm").state, "OFF");
  assert.equal(byId(off, "engine").word, "IDLE", "AUTO OFF のエンジンは IDLE");

  const unverified = deriveRoute(healthyView({ positionCheck: { verified: false, source: "crosstrade", observedAt: iso() } }), LIVE, NOW);
  assert.equal(byId(unverified, "broker").state, "DOWN");
  assert.equal(byId(unverified, "broker").word, "UNVERIFIED");

  const aging = deriveRoute(healthyView({ lastVerifiedAt: iso(-20 * 60_000), positionCheck: { verified: true, observedAt: iso(-20 * 60_000) } }), LIVE, NOW);
  assert.equal(byId(aging, "broker").state, "WARN");
  assert.equal(byId(aging, "broker").word, "AGING");

  const gone = healthyView();
  gone.accounts.sync = { verified: true, missing: ["LTATANOBA1000000000001"], unknown: [], dead: [] };
  const g = deriveRoute(gone, LIVE, NOW);
  assert.equal(byId(g, "accounts").state, "DOWN");
  assert.match(byId(g, "accounts").detail, /…000001/);
  assert.match(byId(g, "accounts").detail, /do not send/i);

  const fresh = healthyView();
  fresh.accounts.sync = { verified: true, missing: [], unknown: ["LTATANOBA1000000000002"], dead: [] };
  assert.equal(byId(deriveRoute(fresh, LIVE, NOW), "accounts").state, "WARN");
});

test("エンジンの現在地は自動発注パネルと同じ導出", () => {
  assert.equal(engineState({ position: { qty: 2 } }), "MANAGING");
  assert.equal(engineState({ order: { state: "SENT" } }), "ROUTING");
  assert.equal(engineState({ entryClaim: { state: "CLAIMED" } }), "CLAIMED");
  assert.equal(engineState({ entryClaim: { state: "CONSUMED", staleReleasable: true } }), "STALE");
  assert.equal(engineState({ managementClaim: { state: "CONSUMED" } }), "MODIFY");
  assert.equal(engineState({}), "IDLE");
  const holding = deriveRoute(healthyView({ position: { qty: 1, initialQty: 2, side: "SHORT", state: "OPEN" } }), LIVE, NOW);
  assert.equal(byId(holding, "engine").word, "IN TRADE");
  assert.match(byId(holding, "engine").detail, /SHORT 1 of 2/);
  assert.match(byId(holding, "broker").detail, /SHORT 1/);
});

test("署名は状態と語だけで決まり、経過秒の変化では変わらない", () => {
  const a = deriveRoute(healthyView(), LIVE, NOW);
  const b = deriveRoute(healthyView(), LIVE, NOW + 20_000);
  assert.notEqual(byId(a, "loop").detail, byId(b, "loop").detail, "経過秒は進む");
  assert.equal(routeSignature(a), routeSignature(b), "署名は同じ(DOM を作り直さない)");
});

test("system.js は発注・ネットワーク・保存に触れない", () => {
  // ラベル文言(「order.py」など)は許す。禁止するのは呼び出しそのもの。
  for (const forbidden of ["fetch(", "sendData", "new WebSocket", "localStorage", "sessionStorage", "/api/", "XMLHttpRequest"]) {
    assert.ok(!SYSTEM.includes(forbidden), `${forbidden} を含まない`);
  }
});

test("SYSTEM タブが配線されている", () => {
  assert.match(HTML, /data-view="system"/, "ドックに SYSTEM タブがある");
  assert.match(HTML, /id="systemView"/, "ビューの置き場がある");
  assert.match(APP, /import \{[^}]*deriveRoute[^}]*\} from "\.\/system\.js"/, "app.js が導出を読む");
  assert.match(APP, /renderSystemView\(/, "描画を呼ぶ");
  assert.match(APP, /\["radar", "ledger", "settings", "system"\]\.includes\(name\)/, "タブ切替に system がある(R58 で radar も並ぶ)");
  // ping は SYSTEM タブを開いている間だけ。発注経路とは無関係の GET /api/health。
  assert.match(APP, /client\.ping\(\)/, "遅延計測を呼ぶ");
  assert.match(STYLES, /\.route-link\.is-flow i/, "流れるリンクの様式がある");
  assert.match(STYLES, /\.route-link\.is-broken/, "切れたリンクの様式がある");
});
