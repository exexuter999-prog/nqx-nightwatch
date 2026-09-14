/**
 * 状態遷移の検証。ネットワークも Cloudflare ランタイムも使わない。
 * 実注文経路は存在しないので、このファイルから CrossTrade へは絶対に到達しない。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  applyEvent, emptyState, normalizeTick, projectState, sweepExpired, validateScenario,
  validateMarket, validateResult, validateAccounts, evaluateExecutionContract,
} from "../src/state_machine.js";

const T0 = Date.parse("2026-08-13T02:00:00Z");
const MIN = 60_000;

function baseState() {
  return emptyState("lucid-50k-daily", "MNQU6");
}

function scenarioPayload(overrides = {}) {
  return {
    scenarioId: "sc-001",
    fingerprint: "fp-001",
    state: "ACTIVE",
    symbol: "MNQU6",
    side: "BUY",
    qty: 1,
    entry: 29906.5,
    stop: 29871.5,
    target: 29922.75,
    grade: "A",
    issuedAt: new Date(T0).toISOString(),
    observedAt: new Date(T0).toISOString(),
    expiresAt: new Date(T0 + 15 * MIN).toISOString(),
    ...overrides,
  };
}

function scenarioEvent(revision, overrides = {}) {
  return { stream: "scenario", revision, payload: { scenario: scenarioPayload(overrides) } };
}

function positionEvent(revision, position) {
  return { stream: "position", revision, payload: { position } };
}

function marketPayload(overrides = {}) {
  return {
    verified: true,
    observedAt: new Date(T0).toISOString(),
    source: "TradingView quote_get",
    sourceSymbol: "CME_MINI:MNQ1!",
    resolution: "15",
    barResolution: "3",
    price: 30205.25,
    vwap: 30197.21,
    cvd: 153437,
    cvdAt: new Date(T0).toISOString(),
    regime: "MX",
    bars: [
      { t: Math.floor(T0 / 1000) - 180, o: 30200, h: 30204.25, l: 30198.75, c: 30203.5 },
      { t: Math.floor(T0 / 1000), o: 30203.5, h: 30206, l: 30202, c: 30205.25 },
    ],
    levels: [{ name: "C: POC", price: 30198.28 }],
    ...overrides,
  };
}

function marketEvent(revision, overrides = {}) {
  return { stream: "market", revision, payload: { market: marketPayload(overrides) } };
}

function openPosition(overrides = {}) {
  return {
    verified: true,
    source: "tradovate-rest",
    symbol: "MNQU6",
    side: "LONG",
    qty: 2,
    avgEntry: 29906.5,
    stop: 29871.5,
    target: 29922.75,
    observedAt: new Date(T0).toISOString(),
    ...overrides,
  };
}

// ---------------------------------------------------------------- 基本

test("tick 正規化は 0.25 刻みに丸める", () => {
  assert.equal(normalizeTick(29906.6), 29906.5);
  assert.equal(normalizeTick(29906.63), 29906.75);
  assert.equal(normalizeTick(29906.5), 29906.5);
  assert.equal(normalizeTick("bad"), null);
});

test("期限付きAUTO武装はサーバー時刻で失効し、投影もLIVEを残さない", () => {
  const state = {
    ...baseState(),
    autotradeArm: {
      schemaVersion: "NQX_AUTOTRADE_ARM/1",
      armId: "app-test",
      enabled: true,
      autotrade: true,
      live: true,
      status: "LIVE",
      source: "TELEGRAM_MINI_APP",
      armedAt: new Date(T0 - MIN).toISOString(),
      expiresAt: new Date(T0).toISOString(),
      accountScope: ["A", "B"],
      symbol: "MNQU6",
    },
  };
  const swept = sweepExpired(state, T0);
  assert.equal(swept.state.autotradeArm.enabled, false);
  assert.equal(swept.state.autotradeArm.live, false);
  assert.equal(swept.state.autotradeArm.status, "EXPIRED");
  assert.equal(swept.transitions[0].kind, "autotrade");
  const view = projectState(state, T0);
  assert.equal(view.autotradeArm.enabled, false);
  assert.equal(view.autotradeArm.status, "EXPIRED");
});

test("検証済み3分足 marketだけを受理し、出所と足を保存する", () => {
  const validated = validateMarket(marketPayload(), T0);
  assert.equal(validated.ok, true);
  assert.equal(validated.market.barResolution, "3");
  assert.equal(validated.market.sourceSymbol, "CME_MINI:MNQ1!");
  assert.equal(validated.market.bars[1].c, 30205.25);
});

test("truncated market feed retains source bar count for a visible warning", () => {
  const validated = validateMarket(marketPayload({ barsTruncatedFrom: 300 }), T0);
  assert.equal(validated.ok, true);
  assert.equal(validated.market.barsTruncatedFrom, 300);
  assert.match(validateMarket(marketPayload({ barsTruncatedFrom: 1 }), T0).reason, /barsTruncatedFrom/);
});

test("未検証・古い・off-tick・15分足だけの market は拒否する", () => {
  assert.match(validateMarket(marketPayload({ verified: false }), T0).reason, /verified/);
  assert.match(validateMarket(marketPayload({ observedAt: new Date(T0 - 11 * MIN).toISOString() }), T0).reason, /stale/);
  assert.match(validateMarket(marketPayload({ price: 30205.13 }), T0).reason, /0.25/);
  assert.match(validateMarket(marketPayload({ barResolution: "15" }), T0).reason, /3-minute/);
});

test("新しい revision でも古い価格観測への巻き戻しは拒否する", () => {
  let state = applyEvent(baseState(), marketEvent(1), T0).state;
  const result = applyEvent(state, marketEvent(2, {
    observedAt: new Date(T0 - MIN).toISOString(),
  }), T0);
  assert.equal(result.accepted, false);
  assert.match(result.reason, /older/);
  assert.equal(result.state.market.price, 30205.25);
});

test("10分を超えた market はチャート履歴を残しつつ現在価格を隠す", () => {
  const state = applyEvent(baseState(), marketEvent(1), T0).state;
  const view = projectState(state, T0 + 11 * MIN);
  assert.equal(view.market.stale, true);
  assert.equal(view.market.verified, false);
  assert.equal(view.market.price, null);
  assert.equal(view.market.bars.length, 2);
});

test("必須フィールドが 1 つでも欠けたら scenario は拒否される", () => {
  for (const field of ["scenarioId", "fingerprint", "issuedAt", "observedAt", "expiresAt", "symbol", "state"]) {
    const payload = scenarioPayload();
    delete payload[field];
    const result = validateScenario(payload, { symbol: "MNQU6" });
    assert.equal(result.ok, false, `${field} 欠落が通ってしまった`);
    assert.match(result.reason, new RegExp(field));
  }
});

test("別 symbol の scenario は拒否される", () => {
  const result = validateScenario(scenarioPayload({ symbol: "MESU6" }), { symbol: "MNQU6" });
  assert.equal(result.ok, false);
  assert.match(result.reason, /does not match/);
});

test("正規化後に方向が壊れる scenario は拒否される", () => {
  // BUY なのに stop > entry
  const result = validateScenario(scenarioPayload({ stop: 29950 }), { symbol: "MNQU6" });
  assert.equal(result.ok, false);
  assert.match(result.reason, /stop < entry < target/);
});

// ---------------------------------------------------------------- 必須テスト 1

test("順不同 revision は拒否され、古い scenario が復活しない", () => {
  let state = baseState();
  state = applyEvent(state, scenarioEvent(1), T0).state;
  state = applyEvent(state, scenarioEvent(2, { scenarioId: "sc-002", fingerprint: "fp-002" }), T0).state;
  assert.equal(state.scenario.scenarioId, "sc-002");

  // 遅れて届いた revision 1(古い scenario)
  const late = applyEvent(state, scenarioEvent(1), T0);
  assert.equal(late.accepted, false);
  assert.match(late.reason, /not newer/);
  assert.equal(late.state.scenario.scenarioId, "sc-002", "古い payload で復活した");

  // 同じ revision の再送も拒否
  const replay = applyEvent(state, scenarioEvent(2, { scenarioId: "sc-002", fingerprint: "fp-002" }), T0);
  assert.equal(replay.accepted, false);
});

test("置き換えられた scenario は REPLACED として記録され、表示から外れる", () => {
  let state = baseState();
  state = applyEvent(state, scenarioEvent(1), T0).state;
  const result = applyEvent(state, scenarioEvent(2, { scenarioId: "sc-002", fingerprint: "fp-002" }), T0);
  const replaced = result.transitions.find((t) => t.to === "REPLACED");
  assert.ok(replaced, "REPLACED が記録されていない");
  assert.equal(replaced.scenarioId, "sc-001");
  assert.equal(replaced.replacedBy, "sc-002");
  assert.equal(result.state.scenario.scenarioId, "sc-002");
});

test("終端状態の scenario は表示されない", () => {
  for (const terminal of ["INVALIDATED", "EXPIRED", "CANCELED", "REPLACED", "FILLED", "CLOSED"]) {
    let state = baseState();
    state = applyEvent(state, scenarioEvent(1), T0).state;
    const result = applyEvent(state, scenarioEvent(2, { state: terminal }), T0);
    assert.equal(result.accepted, true);
    assert.equal(result.state.scenario, null, `${terminal} が表示に残った`);
  }
});

test("到着時点で期限切れの scenario は採用しない", () => {
  const state = baseState();
  const result = applyEvent(state, scenarioEvent(1), T0 + 20 * MIN);
  assert.equal(result.accepted, false);
  assert.match(result.reason, /already expired/);
  assert.equal(result.state.scenario, null);
});

// ---------------------------------------------------------------- 必須テスト 2 / 3

test("期限到達後、サーバー時刻だけで scenario が消える", () => {
  let state = baseState();
  state = applyEvent(state, scenarioEvent(1), T0).state;
  assert.ok(state.scenario);

  // 期限前: 残る
  assert.ok(projectState(state, T0 + 14 * MIN).scenario);

  // 期限後: producer が何も送らなくても消える
  const view = projectState(state, T0 + 16 * MIN);
  assert.equal(view.scenario, null);
  assert.equal(view.display.priority, "SCENARIO_NONE");
  assert.equal(view.display.blockReason, "NO ACTIVE SCENARIO");

  const swept = sweepExpired(state, T0 + 16 * MIN);
  assert.equal(swept.transitions[0].to, "EXPIRED");
});

test("Bot 停止中に期限が来ても、次の読み取りで必ず消える", () => {
  let state = baseState();
  state = applyEvent(state, scenarioEvent(1), T0).state;
  // producer からのイベントは一切来ない = Bot 停止相当
  const hoursLater = projectState(state, T0 + 6 * 60 * MIN);
  assert.equal(hoursLater.scenario, null);
  assert.equal(hoursLater.display.orderable, false);
});

// ---------------------------------------------------------------- 必須テスト 5〜8

test("約定 position は保存され、再読込・Bot 再起動後も同じ state から復元される", () => {
  let state = baseState();
  state = applyEvent(state, positionEvent(1, openPosition()), T0).state;
  assert.equal(state.position.state, "OPEN");
  assert.equal(state.position.qty, 2);

  // 「再起動」= 保存済み doc を読み直しただけ
  const restored = JSON.parse(JSON.stringify(state));
  const view = projectState(restored, T0 + 60 * MIN);
  assert.equal(view.position.state, "OPEN");
  assert.equal(view.position.qty, 2);
  assert.equal(view.display.priority, "POSITION_OPEN");
});

test("照会失敗では position を消さず STALE にする", () => {
  let state = baseState();
  state = applyEvent(state, positionEvent(1, openPosition()), T0).state;

  const result = applyEvent(state, positionEvent(2, {
    verified: false, source: "tradovate-rest", observedAt: new Date(T0 + MIN).toISOString(),
  }), T0 + MIN);

  assert.equal(result.accepted, true);
  assert.equal(result.state.position.state, "STALE");
  assert.equal(result.state.position.qty, 2, "STALE で数量が失われた");
  assert.equal(result.state.position.lastKnownState, "OPEN");
  assert.ok(result.state.position.staleSince);

  const view = projectState(result.state, T0 + MIN);
  assert.equal(view.display.priority, "POSITION_OPEN");
  assert.equal(view.display.orderable, false);
});

test("元から建玉が無ければ、未確認の照会で建玉を作らない", () => {
  const state = baseState();
  const result = applyEvent(state, positionEvent(1, {
    verified: false, source: "unavailable", observedAt: new Date(T0).toISOString(),
  }), T0);
  assert.equal(result.accepted, true);
  assert.equal(result.state.position, null);
});

test("部分決済で残数量が更新され、カードは残る", () => {
  let state = baseState();
  state = applyEvent(state, positionEvent(1, openPosition({ qty: 2, initialQty: 2 })), T0).state;
  const result = applyEvent(state, positionEvent(2, openPosition({ qty: 1, observedAt: new Date(T0 + MIN).toISOString() })), T0 + MIN);

  assert.equal(result.state.position.state, "PARTIAL");
  assert.equal(result.state.position.qty, 1);
  assert.equal(result.state.position.initialQty, 2);
  assert.equal(projectState(result.state, T0 + MIN).display.priority, "POSITION_OPEN");
});

test("broker が qty=0 と closedAt を返すまで position は消えない", () => {
  let state = baseState();
  state = applyEvent(state, positionEvent(1, openPosition()), T0).state;

  // qty=0 だが決済時刻が無い → 拒否。カードは残る。
  const noClosedAt = applyEvent(state, positionEvent(2, openPosition({
    qty: 0, observedAt: new Date(T0 + MIN).toISOString(),
  })), T0 + MIN);
  assert.equal(noClosedAt.accepted, false);
  assert.match(noClosedAt.reason, /closedAt/);
  assert.equal(noClosedAt.state.position.state, "OPEN");

  // qty=0 かつ closedAt あり → ここで初めて CLOSED
  const closed = applyEvent(state, {
    stream: "position", revision: 2,
    payload: {
      position: openPosition({ qty: 0, observedAt: new Date(T0 + MIN).toISOString(), receipt: "rc-1" }),
      closedAt: new Date(T0 + MIN).toISOString(),
    },
  }, T0 + MIN);
  assert.equal(closed.accepted, true);
  assert.equal(closed.state.position.state, "CLOSED");
  assert.equal(closed.state.position.closedQty, 2);
  const view = projectState(closed.state, T0 + MIN);
  assert.equal(view.position, null, "CLOSED 後もカードが残っている");
  assert.equal(view.closedPosition.receipt, "rc-1");
});

test("CLOSED の建玉を再照会しても CLOSED→CLOSED を遷移として出さない", () => {
  let state = baseState();
  state = applyEvent(state, positionEvent(1, openPosition()), T0).state;

  const closed = applyEvent(state, {
    stream: "position", revision: 2,
    payload: {
      position: openPosition({ qty: 0, observedAt: new Date(T0 + MIN).toISOString(), receipt: "rc-1" }),
      closedAt: new Date(T0 + MIN).toISOString(),
    },
  }, T0 + MIN);
  assert.equal(closed.transitions.filter((t) => t.kind === "position" && t.to === "CLOSED").length, 1,
    "最初の決済では CLOSED 遷移がちょうど1回");
  assert.equal(closed.state.position.closedQty, 2);

  // 60秒後の定期照会。ブローカーは同じ FLAT を返しつづける。
  const again = applyEvent(closed.state, {
    stream: "position", revision: 3,
    payload: {
      position: openPosition({ qty: 0, observedAt: new Date(T0 + 2 * MIN).toISOString(), receipt: "rc-1" }),
      closedAt: new Date(T0 + 2 * MIN).toISOString(),
    },
  }, T0 + 2 * MIN);

  assert.equal(again.accepted, true);
  assert.deepEqual(again.transitions.filter((t) => t.kind === "position"), [],
    "同じ決済で POSITION CLOSED 通知が再送されてしまう");
  assert.equal(again.state.position.closedQty, 2,
    "closedQty が previous.qty=0 で潰れている");
  assert.equal(again.state.position.closedAt, new Date(T0 + MIN).toISOString(),
    "closedAt は最初の決済時刻を保つ");
  assert.equal(again.state.position.state, "CLOSED");
  // 再照会そのものは受理され、鮮度だけ進む(カードが STALE に落ちない)。
  assert.equal(again.state.position.observedAt, new Date(T0 + 2 * MIN).toISOString());
});

test("EXIT_PENDING は独立した優先順位を持つ", () => {
  let state = baseState();
  state = applyEvent(state, positionEvent(1, openPosition({ exitPending: true })), T0).state;
  assert.equal(state.position.state, "EXIT_PENDING");
  assert.equal(projectState(state, T0).display.priority, "POSITION_EXIT");
});

// ---------------------------------------------------------------- 必須テスト 9

test("新しい scenario が open position を上書きしない", () => {
  let state = baseState();
  state = applyEvent(state, positionEvent(1, openPosition()), T0).state;
  state = applyEvent(state, scenarioEvent(1), T0).state;
  state = applyEvent(state, scenarioEvent(2, { scenarioId: "sc-002", fingerprint: "fp-002", side: "SELL", entry: 29900, stop: 29930, target: 29850 }), T0).state;

  assert.equal(state.position.state, "OPEN");
  assert.equal(state.position.qty, 2);
  assert.equal(state.position.side, "LONG");
  assert.equal(state.revisions.position, 1, "position revision が scenario で動いた");

  const view = projectState(state, T0);
  assert.equal(view.display.priority, "POSITION_OPEN", "position より scenario が上に来た");
  assert.equal(view.display.orderable, false);
  assert.equal(view.display.blockReason, "POSITION OPEN — MANAGEMENT ONLY");
});

test("scenario イベントに position を混ぜたら拒否する", () => {
  const state = baseState();
  const result = applyEvent(state, {
    stream: "scenario", revision: 1,
    payload: { scenario: scenarioPayload(), position: openPosition() },
  }, T0);
  assert.equal(result.accepted, false);
  assert.match(result.reason, /must not carry position/);
});

test("order イベントは position を作らない", () => {
  const state = baseState();
  const result = applyEvent(state, {
    stream: "order", revision: 1,
    payload: {
      order: {
        idempotencyKey: "idem-1", state: "SENT", side: "BUY", qty: 1,
        entry: 29906.5, stop: 29871.5, target: 29922.75,
        at: new Date(T0).toISOString(), receipt: "http-200",
      },
    },
  }, T0);
  assert.equal(result.accepted, true);
  assert.equal(result.state.position, null, "注文送信だけで建玉が作られた");
  assert.equal(projectState(result.state, T0).display.priority, "ORDER_PENDING");
});

// ---------------------------------------------------------------- 表示優先順位

test("表示優先順位: POSITION > EXIT > ORDER > SCENARIO > NONE", () => {
  const empty = baseState();
  assert.equal(projectState(empty, T0).display.priority, "SCENARIO_NONE");

  let verifiedFlat = applyEvent(empty, marketEvent(1), T0).state;
  verifiedFlat = applyEvent(verifiedFlat, positionEvent(1, {
    verified: true, source: "tradovate-rest", symbol: "MNQU6", qty: 0,
    observedAt: new Date(T0).toISOString(),
  }), T0).state;
  let withScenario = applyEvent(verifiedFlat, scenarioEvent(1), T0).state;
  assert.equal(projectState(withScenario, T0).display.priority, "SCENARIO_ACTIVE");
  assert.equal(projectState(withScenario, T0).display.orderable, false);
  assert.match(projectState(withScenario, T0).display.blockReason, /CYCLE_MISMATCH/);

  let withOrder = applyEvent(withScenario, {
    stream: "order", revision: 1,
    payload: { order: { idempotencyKey: "k", state: "PENDING", at: new Date(T0).toISOString() } },
  }, T0).state;
  assert.equal(projectState(withOrder, T0).display.priority, "ORDER_PENDING");
  assert.equal(projectState(withOrder, T0).display.orderable, false);

  // revision 1 is already consumed by the verified-flat broker check above.
  let withPosition = applyEvent(withOrder, positionEvent(2, openPosition()), T0).state;
  assert.equal(projectState(withPosition, T0).display.priority, "POSITION_OPEN");

  let exiting = applyEvent(withPosition, positionEvent(3, openPosition({
    exitPending: true, observedAt: new Date(T0 + MIN).toISOString(),
  })), T0 + MIN).state;
  assert.equal(projectState(exiting, T0 + MIN).display.priority, "POSITION_EXIT");
});

test("建玉照会が未確認なら scenario は表示しても発注ボタンを出さない", () => {
  let state = baseState();
  state = applyEvent(state, scenarioEvent(1), T0).state;
  const view = projectState(state, T0);
  assert.equal(view.display.orderable, false);
  assert.equal(view.display.blockReason, "CYCLE_MISMATCH - MARKET/SCENARIO SEAL NOT ARMED");
  assert.equal(view.positionCheck, null);

  state = applyEvent(state, positionEvent(1, {
    verified: false, source: "unavailable",
    observedAt: new Date(T0 + MIN).toISOString(),
  }), T0 + MIN).state;
  const stale = projectState(state, T0 + MIN);
  assert.equal(stale.display.orderable, false);
  assert.equal(stale.positionCheck.verified, false);
});

test("WATCH のシナリオは表示されるが発注可能にはならない", () => {
  let state = baseState();
  state = applyEvent(state, scenarioEvent(1, { state: "WATCH" }), T0).state;
  const view = projectState(state, T0);
  assert.ok(view.scenario);
  assert.equal(view.display.orderable, false);
  assert.match(view.display.blockReason, /NOT ARMED/);
});

// ---------------------------------------------------------------- 決済結果

function resultPayload(overrides = {}) {
  return {
    resultId: "rs-001",
    side: "LONG",
    symbol: "MNQU6",
    qty: 2,
    entry: 29760.0,
    exit: 29796.0,
    stop: 29726.0,
    pointValue: 2,
    openedAt: new Date(T0).toISOString(),
    closedAt: new Date(T0 + 14 * MIN).toISOString(),
    path: [29752.5, 29761.0, 29774.25, 29796.0],
    pathSource: "observed-bars",
    exitSource: "broker",
    verdict: "They let this one through.",
    mode: "SIMULATION",
    ...overrides,
  };
}

function resultEvent(revision, overrides = {}) {
  return { stream: "result", revision, payload: { result: resultPayload(overrides) } };
}

test("必須項目が欠けた result は拒否される", () => {
  // R56: stop は broker 由来なら省略できる(凍結プランの無い手動建玉)。
  // manual(手入力)では従来どおり必須 —— r56_result_stop_optional.test.mjs が固定する。
  for (const field of ["resultId", "side", "symbol", "qty", "entry", "exit",
    "pointValue", "openedAt", "closedAt", "exitSource", "pathSource", "mode"]) {
    const payload = resultPayload();
    delete payload[field];
    const result = validateResult(payload, { symbol: "MNQU6" });
    assert.equal(result.ok, false, `${field} 欠落が通ってしまった`);
    assert.match(result.reason, new RegExp(field));
  }
  const manual = resultPayload({ exitSource: "manual" });
  delete manual.stop;
  const rejected = validateResult(manual, { symbol: "MNQU6" });
  assert.equal(rejected.ok, false, "manual の stop 欠落が通ってしまった");
  assert.match(rejected.reason, /stop/);
});

test("表示層が導出する値を外から渡すと拒否される", () => {
  for (const forbidden of ["state", "pnl", "realisedR", "held", "usd"]) {
    const result = validateResult(resultPayload({ [forbidden]: 1 }), { symbol: "MNQU6" });
    assert.equal(result.ok, false, `${forbidden} が通ってしまった`);
    assert.match(result.reason, /must be derived/);
  }
});

test("出所不明の path / exit は受け付けない", () => {
  const synthetic = validateResult(resultPayload({ pathSource: "synthetic" }), { symbol: "MNQU6" });
  assert.equal(synthetic.ok, false);
  assert.match(synthetic.reason, /not an accepted source/);

  const guessed = validateResult(resultPayload({ exitSource: "estimated" }), { symbol: "MNQU6" });
  assert.equal(guessed.ok, false);
  assert.match(guessed.reason, /not an accepted source/);
});

test("path は 2 点以上の有限値でなければならない", () => {
  assert.match(validateResult(resultPayload({ path: [29760] }), { symbol: "MNQU6" }).reason,
    /at least two observed points/);
  assert.match(validateResult(resultPayload({ path: [29760, NaN, 29796] }), { symbol: "MNQU6" }).reason,
    /non-finite/);
  // 観測が足りない場合の正直な表現は 2 点ちょうど
  assert.equal(validateResult(resultPayload({ path: [29760, 29796], pathSource: "endpoints-only" }),
    { symbol: "MNQU6" }).ok, true);
  assert.match(validateResult(resultPayload({ path: [29760, 29770, 29796], pathSource: "endpoints-only" }),
    { symbol: "MNQU6" }).reason, /exactly the two endpoints/);
});

test("stop == entry の result は R が定義できないので拒否される", () => {
  const result = validateResult(resultPayload({ stop: 29760.0 }), { symbol: "MNQU6" });
  assert.equal(result.ok, false);
  assert.match(result.reason, /R would be undefined/);
});

test("別 symbol / 逆転した時刻の result は拒否される", () => {
  assert.match(validateResult(resultPayload({ symbol: "MESU6" }), { symbol: "MNQU6" }).reason, /does not match/);
  assert.match(validateResult(resultPayload({
    openedAt: new Date(T0 + MIN).toISOString(), closedAt: new Date(T0).toISOString(),
  }), { symbol: "MNQU6" }).reason, /precedes openedAt/);
});

test("result は表示優先順位に影響しない", () => {
  let state = baseState();
  state = applyEvent(state, resultEvent(1), T0).state;
  const view = projectState(state, T0);
  assert.equal(view.result.resultId, "rs-001");
  assert.equal(view.display.priority, "SCENARIO_NONE", "result が現在の状態を押しのけた");

  state = applyEvent(state, scenarioEvent(1), T0).state;
  const withScenario = projectState(state, T0);
  assert.equal(withScenario.display.priority, "SCENARIO_ACTIVE");
  assert.ok(withScenario.result, "scenario 更新で result が消えた");
});

test("result イベントは position / scenario / order を触れない", () => {
  const state = baseState();
  for (const foreign of ["position", "scenario", "order"]) {
    const result = applyEvent(state, {
      stream: "result", revision: 1,
      payload: { result: resultPayload(), [foreign]: {} },
    }, T0);
    assert.equal(result.accepted, false, `${foreign} 同梱が通った`);
    assert.match(result.reason, new RegExp(foreign));
  }
});

test("新しい result が open position を消さない", () => {
  let state = baseState();
  state = applyEvent(state, positionEvent(1, openPosition()), T0).state;
  state = applyEvent(state, resultEvent(1), T0).state;
  assert.equal(state.position.state, "OPEN");
  assert.equal(state.position.qty, 2);
  assert.equal(state.revisions.position, 1, "result で position revision が動いた");
});

test("古い revision の result は拒否される", () => {
  let state = baseState();
  state = applyEvent(state, resultEvent(5), T0).state;
  const stale = applyEvent(state, resultEvent(3, { resultId: "rs-old" }), T0);
  assert.equal(stale.accepted, false);
  assert.equal(stale.state.result.resultId, "rs-001");
});

test("価格は 0.25 tick に正規化されて保存される", () => {
  const validated = validateResult(resultPayload({ entry: 29760.13, exit: 29796.19, stop: 29726.06 }),
    { symbol: "MNQU6" });
  assert.equal(validated.ok, true);
  assert.equal(validated.result.entry, 29760.25);
  assert.equal(validated.result.exit, 29796.25);
  assert.equal(validated.result.stop, 29726.0);
});

// ---------------------------------------------------------------- resultLog(LEDGER 履歴)

test("result は resultLog に追記され、projection が新しい順で返す", () => {
  let state = baseState();
  state = applyEvent(state, resultEvent(1), T0).state;
  state = applyEvent(state, resultEvent(2, { resultId: "rs-002", exit: 29740.0 }), T0 + MIN).state;
  assert.equal(state.resultLog.length, 2);
  const view = projectState(state, T0 + MIN);
  assert.equal(view.recentResults.length, 2);
  assert.equal(view.recentResults[0].resultId, "rs-002", "新しい記録が先頭に来ない");
  assert.equal(view.recentResults[1].resultId, "rs-001");
});

test("同じ resultId の再送は履歴を重複させず置き換える", () => {
  let state = baseState();
  state = applyEvent(state, resultEvent(1), T0).state;
  state = applyEvent(state, resultEvent(2, { exit: 29800.0 }), T0 + MIN).state;
  assert.equal(state.resultLog.length, 1, "リトライで履歴が二重になった");
  assert.equal(state.resultLog[0].exit, 29800.0, "再送の内容で置き換わっていない");
});

test("result のクリア(null)でも履歴は消えない", () => {
  let state = baseState();
  state = applyEvent(state, resultEvent(1), T0).state;
  state = applyEvent(state, { stream: "result", revision: 2, payload: { result: null } }, T0).state;
  assert.equal(state.result, null);
  assert.equal(state.resultLog.length, 1, "表示クリアが履歴まで消した");
});

test("resultLog は上限で最古から落ちる", () => {
  let state = baseState();
  for (let i = 1; i <= 55; i += 1) {
    state = applyEvent(state, resultEvent(i, { resultId: `rs-${String(i).padStart(3, "0")}` }), T0 + i).state;
  }
  assert.equal(state.resultLog.length, 50);
  assert.equal(state.resultLog[0].resultId, "rs-006", "最古の落とし方が違う");
  assert.equal(state.resultLog[49].resultId, "rs-055");
});

test("resultLog が無い旧 state でも result は受理される(後方互換)", () => {
  const legacy = baseState();
  delete legacy.resultLog;
  delete legacy.accounts;
  const applied = applyEvent(legacy, resultEvent(1), T0);
  assert.equal(applied.accepted, true);
  assert.equal(applied.state.resultLog.length, 1);
  const view = projectState(applied.state, T0);
  assert.equal(view.recentResults.length, 1);
  assert.equal(view.accounts, null);
});

// ---------------------------------------------------------------- account(LIFELINE)

function accountsPayload(overrides = {}) {
  return {
    observedAt: new Date(T0).toISOString(),
    source: "crosstrade-env",
    list: [
      { id: "LFE00000000000002", cap: 60, buffer: 816 },
      { id: "LFE00000000000003", cap: 60, buffer: 407 },
      { id: "LFE00000000000004", cap: 50, buffer: 249 },
    ],
    ...overrides,
  };
}

function accountEvent(revision, overrides = {}) {
  return { stream: "account", revision, payload: { accounts: accountsPayload(overrides) } };
}

test("accounts を受理し、合計残機とラベルを導出する", () => {
  const validated = validateAccounts(accountsPayload());
  assert.equal(validated.ok, true);
  assert.equal(validated.accounts.totalBuffer, 1472);
  assert.equal(validated.accounts.list[0].label, "…0002");
  assert.equal(validated.accounts.list[2].cap, 50);
});

test("accounts の異常値は理由付きで拒否する", () => {
  assert.match(validateAccounts(accountsPayload({ list: [] })).reason, /at least one/);
  assert.match(validateAccounts(accountsPayload({ observedAt: "bad" })).reason, /observedAt/);
  assert.match(validateAccounts(accountsPayload({
    list: [{ id: "A", cap: 60, buffer: 100 }, { id: "A", cap: 60, buffer: 100 }],
  })).reason, /duplicates/);
  assert.match(validateAccounts(accountsPayload({
    list: [{ id: "A", cap: 0, buffer: 100 }],
  })).reason, /cap/);
  assert.match(validateAccounts(accountsPayload({
    list: [{ id: "A", cap: 60, buffer: -5 }],
  })).reason, /buffer/);
});

test("ULTRA の profitTarget は任意項目として受理し、不正値は拒否する", () => {
  // ULTRA mode は口座ごとの利益目標から必要枚数を逆算する。未設定の口座は
  // ULTRA 側で INELIGIBLE になるだけなので、任意項目として通す。
  const withTarget = validateAccounts(accountsPayload({
    list: [{ id: "APEX-01", cap: 180, buffer: 2500, profitTarget: 3000 }],
  }));
  assert.equal(withTarget.ok, true);
  assert.equal(withTarget.accounts.list[0].profitTarget, 3000);

  const without = validateAccounts(accountsPayload({
    list: [{ id: "APEX-02", cap: 180, buffer: 2500 }],
  }));
  assert.equal(without.ok, true);
  assert.equal("profitTarget" in without.accounts.list[0], false,
    "未設定の口座に利益目標を作らない");

  for (const bad of [0, -100, "abc", Infinity]) {
    assert.match(validateAccounts(accountsPayload({
      list: [{ id: "APEX-03", cap: 180, buffer: 2500, profitTarget: bad }],
    })).reason, /profitTarget/, `profitTarget=${bad} は拒否される`);
  }
});

test("R44 equity は任意項目として受理し、不正値は拒否する", () => {
  // 残機(buffer)は「床までの余裕」なので、口座に実際いくらあるのかは
  // これが無いと画面から分からない。残高照会が通ったときだけ載る。
  const withEquity = validateAccounts(accountsPayload({
    list: [{ id: "APEX-01", cap: 240, buffer: 2868.1, equity: 99868.1 }],
  }));
  assert.equal(withEquity.ok, true, withEquity.reason);
  assert.equal(withEquity.accounts.list[0].equity, 99868.1);

  const without = validateAccounts(accountsPayload({
    list: [{ id: "APEX-02", cap: 240, buffer: 2868.1 }],
  }));
  assert.equal(without.ok, true);
  assert.equal("equity" in without.accounts.list[0], false,
    "残高が取れていない口座に数字を作らない");

  // 0 は吹き飛んだ口座の実在値。通す。
  const zero = validateAccounts(accountsPayload({
    list: [{ id: "APEX-04", cap: 240, buffer: 0, equity: 0 }],
  }));
  assert.equal(zero.ok, true, zero.reason);
  assert.equal(zero.accounts.list[0].equity, 0);

  for (const bad of [-1, "abc", Infinity, NaN]) {
    assert.match(validateAccounts(accountsPayload({
      list: [{ id: "APEX-03", cap: 240, buffer: 2868.1, equity: bad }],
    })).reason, /equity/, `equity=${bad} は拒否される`);
  }
});

test("account イベントは projection に載り、null で消える", () => {
  let state = baseState();
  state = applyEvent(state, accountEvent(1), T0).state;
  let view = projectState(state, T0);
  assert.equal(view.accounts.totalBuffer, 1472);
  assert.equal(view.accounts.list.length, 3);
  state = applyEvent(state, { stream: "account", revision: 2, payload: { accounts: null } }, T0).state;
  view = projectState(state, T0);
  assert.equal(view.accounts, null);
});

test("account イベントは他 stream を触れない", () => {
  let state = baseState();
  state = applyEvent(state, positionEvent(1, openPosition()), T0).state;
  const smuggled = applyEvent(state, {
    stream: "account", revision: 1,
    payload: { accounts: accountsPayload(), position: null },
  }, T0);
  assert.equal(smuggled.accepted, false);
  assert.match(smuggled.reason, /position/);
  assert.equal(smuggled.state.position.state, "OPEN");
});

test("古い revision の account は拒否される", () => {
  let state = baseState();
  state = applyEvent(state, accountEvent(5), T0).state;
  const stale = applyEvent(state, accountEvent(3, {
    list: [{ id: "X", cap: 60, buffer: 1 }],
  }), T0);
  assert.equal(stale.accepted, false);
  assert.equal(stale.state.accounts.totalBuffer, 1472);
});

// ---------------------------------------------------------------- R6 評価カード
// APP_EVAL_DISPLAY_SPEC §5: evaluation は「表示専用の任意フィールド」。
// 壊れていても market ストリームは通す(価格配信を評価の欠損で止めない)。

function evaluationPayload(overrides = {}) {
  return {
    at: new Date(T0).toISOString(),
    volGate: { noise: 12.5, slCap: 25, ratio: 0.5, ruling: "A+のみ" },
    rotation: { signals: 5, negations: 2, verdict: "OK" },
    msnr: {
      label: "Weekly Mid", price: 30253, tier: 3, confluence: 2,
      freshness: "FLIPPED", side: "SELL", chainType: "FLIP",
      chainState: "FLIP_HELD", barsLeft: 7,
      allowed: true, grade: "A+", blockers: [],
    },
    dataGate: { status: "FRESH", requiredFresh: true, freshCount: 4,
      requiredCount: 4, oldestAgeSec: 96, sourceSpanSec: 90, staleRequired: [] },
    cvdGate: { status: "FRESH", available: true, freshness: "FRESH",
      aplusAllowed: true, attempts: 1, maxAttempts: 2, refreshRequired: false },
    sessionGate: { window: "NY_AM_KZ", label: "NY AM KZ", tradeable: true,
      et: "08:49", note: null },
    advisory: { vwap: { side: "BUY", state: "VWAP_ACCEPTED", drift: 0.8 } },
    summary: "MSNR: Weekly Mid30253 SELL連鎖 フリップ保持",
    ...overrides,
  };
}

test("R6: 正しい evaluation は market に載る", () => {
  const validated = validateMarket(
    marketPayload({ evaluation: evaluationPayload() }), T0);
  assert.equal(validated.ok, true);
  const ev = validated.market.evaluation;
  assert.equal(ev.volGate.ruling, "A+のみ");
  assert.equal(ev.rotation.verdict, "OK");
  assert.equal(ev.msnr.grade, "A+");
  assert.equal(ev.msnr.chainState, "FLIP_HELD");
  assert.equal(ev.advisory.vwap.drift, 0.8);
  assert.equal(ev.dataGate.requiredFresh, true);
  assert.equal(ev.dataGate.freshCount, 4);
  assert.equal(ev.cvdGate.status, "FRESH");
  assert.equal(ev.cvdGate.aplusAllowed, true);
  assert.equal(ev.sessionGate.window, "NY_AM_KZ");
  assert.equal(ev.sessionGate.tradeable, true);
});

test("R11-D: decision はICT/SMT/VPの最有力モデルとしてmarketに残る", () => {
  const validated = validateMarket(marketPayload({
    evaluation: evaluationPayload({
      decision: {
        decisionId: "abc123", phase: "EVAL_STRIKE", model: "VP80_REVERSION",
        side: "SELL", state: "ARMED", grade: "A+", score: 10,
        entryMode: "AGGRESSIVE_ACCEPT_CLOSE", entry: 30020.25, stop: 30034.5,
        targets: [30005, 29978.5], targetR: [1.07, 2.93],
        hardBlockers: [], penalties: [], evidence: ["VP_ACCEPTED", "SMT_ALIGNED"],
        modelRank: ["VP80_REVERSION:A+"],
      },
    }),
  }), T0);
  assert.equal(validated.ok, true);
  assert.equal(validated.market.evaluation.decision.model, "VP80_REVERSION");
  assert.equal(validated.market.evaluation.decision.grade, "A+");
  assert.deepEqual(validated.market.evaluation.decision.targets, [30005, 29978.5]);
});

test("R6: evaluation が無くても market は通り、evaluation は null", () => {
  const validated = validateMarket(marketPayload(), T0);
  assert.equal(validated.ok, true);
  assert.equal(validated.market.evaluation, null);
});

test("R6: 壊れた evaluation は落ちるだけで market は止まらない", () => {
  for (const broken of [
    "not-an-object",
    [],
    { rotation: { verdict: "OK" } },                    // volGate 欠落
    { volGate: { ratio: 0.5 } },                        // rotation 欠落
  ]) {
    const validated = validateMarket(marketPayload({ evaluation: broken }), T0);
    assert.equal(validated.ok, true, `market must survive: ${JSON.stringify(broken)}`);
    assert.equal(validated.market.evaluation, null);
    assert.equal(validated.market.price, 30205.25);     // 価格は無傷
  }
});

test("R6: 未知の列挙値は null に倒れ、市場データは残る", () => {
  const validated = validateMarket(marketPayload({
    evaluation: evaluationPayload({
      volGate: { noise: 12.5, slCap: 25, ratio: 0.5, ruling: "HACKED" },
      msnr: { ...evaluationPayload().msnr, grade: "S", chainState: "MADE_UP",
              freshness: "NONSENSE" },
    }),
  }), T0);
  assert.equal(validated.ok, true);
  const ev = validated.market.evaluation;
  assert.equal(ev.volGate.ruling, "不明");
  assert.equal(ev.msnr.grade, null);
  assert.equal(ev.msnr.chainState, null);
  assert.equal(ev.msnr.freshness, null);
  assert.equal(ev.volGate.ratio, 0.5);                  // 数値は生き残る
});

test("R6: 4096 バイト超は advisory から削られ、限界超過なら丸ごと null", () => {
  const fat = evaluationPayload({ summary: "x".repeat(200) });
  fat.msnr.blockers = Array.from({ length: 8 }, () => "B".repeat(40));
  const trimmed = validateMarket(marketPayload({ evaluation: fat }), T0);
  assert.equal(trimmed.ok, true);
  assert.notEqual(trimmed.market.evaluation, null);

  const huge = evaluationPayload({ summary: "y".repeat(5000) });
  const dropped = validateMarket(marketPayload({ evaluation: huge }), T0);
  assert.equal(dropped.ok, true);
  // summary は 240 文字に切られるので通る。上限判定が効くことだけ確認する。
  assert.ok(new TextEncoder().encode(
    JSON.stringify(dropped.market.evaluation)).length <= 4096);
});

test("R6: scenario の grade は A+/A/B のみ通り、それ以外は null(2026-09-04 に B 追加)", () => {
  for (const [input, want] of [["A+", "A+"], ["A", "A"], ["B", "B"], ["S", null],
                               [undefined, null], [123, null]]) {
    const validated = validateScenario(scenarioPayload({ grade: input }),
                                       { symbol: "MNQU6" });
    assert.equal(validated.ok, true);
    assert.equal(validated.scenario.grade, want, `grade ${String(input)}`);
  }
});

test("R14 execution contract has deterministic market, CVD, risk and session boundaries", () => {
  const armed = scenarioPayload({ state: "ARMED", grade: "A", qty: 2,
    stop: 29846.5, targets: [30030, 30100],
    legs: [{ id: "TP1", qty: 1, target: 30030 }, { id: "RUNNER", qty: 1, target: 30100 }],
    planVersion: "R17-SPLIT-1", issuedAt: new Date(T0).toISOString(),
    expiresAt: new Date(T0 + 15 * MIN).toISOString() });
  const at599 = evaluateExecutionContract(armed, marketPayload(), null, null, T0 + 599_000);
  assert.equal(at599.orderable, true);
  assert.equal(at599.riskDollars, 240);
  const at601 = evaluateExecutionContract(armed, marketPayload(), null, null, T0 + 601_000);
  assert.ok(at601.blockers.includes("MARKET_STALE"));
  const capped = evaluateExecutionContract({ ...armed, grade: "A+" }, marketPayload({ cvdAt: null }), null, null, T0);
  assert.equal(capped.effectiveGrade, "A");
  assert.ok(capped.blockers.includes("CVD_A_PLUS_PROHIBITED"));
  assert.ok(capped.acquisitionRequired.includes("CVD_REACQUIRE_REQUIRED"));
  const blackedOut = evaluateExecutionContract(armed, marketPayload({ eventBlackout: true }), null, null, T0);
  assert.ok(blackedOut.blockers.includes("EVENT_BLACKOUT"));
  const ended = evaluateExecutionContract({ ...armed, sessionEndAt: new Date(T0 - 1).toISOString() }, marketPayload(), null, null, T0);
  assert.ok(ended.blockers.includes("SESSION_ENDED"));
});

test("R14 malformed strategy evidence is nulled without discarding market", () => {
  const malformed = validateMarket(marketPayload({
    strategyEvidence: { version: "R14-STRATEGY-EVIDENCE-1", models: { ifvg: { zones: Array(17).fill(1) } } },
  }), T0);
  assert.equal(malformed.ok, true);
  assert.equal(malformed.market.strategyEvidence, null);
  assert.match(malformed.market.strategyEvidenceError, /canonical|array/);
});

// ---------------------------------------------------------------- R73: 引き継ぎの境界

test("R73: 決済済み(CLOSED)の後の新規建玉は initialQty / filledAt / SL / TP を引き継がない", () => {
  const t1 = new Date(T0 + MIN).toISOString();
  const t2 = new Date(T0 + 2 * MIN).toISOString();
  const t3 = new Date(T0 + 3 * MIN).toISOString();
  let state = baseState();
  state = applyEvent(state, positionEvent(1, openPosition({ qty: 8, initialQty: 8, filledAt: new Date(T0).toISOString() })), T0).state;
  state = applyEvent(state, positionEvent(2, openPosition({ qty: 4, observedAt: t1 })), T0 + MIN).state;
  assert.equal(state.position.state, "PARTIAL");
  assert.equal(state.position.initialQty, 8);
  state = applyEvent(state, { stream: "position", revision: 3, payload: { position: openPosition({ qty: 0, observedAt: t2 }), closedAt: t2 } }, T0 + 2 * MIN).state;
  assert.equal(state.position.state, "CLOSED");

  const reopened = applyEvent(state, positionEvent(4, openPosition({
    side: "SHORT", qty: 6, avgEntry: 29551.75, stop: 29579.25, target: 29424.5,
    filledAt: t3, observedAt: t3,
  })), T0 + 3 * MIN);
  assert.equal(reopened.accepted, true);
  assert.equal(reopened.state.position.state, "OPEN", "6 枚の新規が前トレードの initialQty 8 で PARTIAL にならない");
  assert.equal(reopened.state.position.initialQty, 6);
  assert.equal(reopened.state.position.filledAt, t3);
  assert.equal(reopened.state.position.stop, 29579.25);
  assert.equal(reopened.state.position.target, 29424.5);
});

test("R73: 決済済みの後、SL/TP/建値が無い新規建玉に前トレードの水準を貼らない(null は 0 にならない)", () => {
  const t1 = new Date(T0 + MIN).toISOString();
  const t2 = new Date(T0 + 2 * MIN).toISOString();
  let state = baseState();
  state = applyEvent(state, positionEvent(1, openPosition({ qty: 2, initialQty: 2 })), T0).state;
  state = applyEvent(state, { stream: "position", revision: 2, payload: { position: openPosition({ qty: 0, observedAt: t1 }), closedAt: t1 } }, T0 + MIN).state;
  const reopened = applyEvent(state, positionEvent(3, openPosition({
    side: "SHORT", qty: 6, avgEntry: null, stop: null, target: null, filledAt: null, observedAt: t2,
  })), T0 + 2 * MIN);
  assert.equal(reopened.accepted, true);
  assert.equal(reopened.state.position.avgEntry, null);
  assert.equal(reopened.state.position.stop, null);
  assert.equal(reopened.state.position.target, null);
  assert.equal(reopened.state.position.filledAt, t2);
  assert.equal(reopened.state.position.initialQty, 6);
});

test("R73: 同じトレードで枚数が増えたら initialQty は大きい側(手動追加・分割約定)。SL/TP は保たれる", () => {
  const t1 = new Date(T0 + MIN).toISOString();
  const t2 = new Date(T0 + 2 * MIN).toISOString();
  let state = baseState();
  state = applyEvent(state, positionEvent(1, openPosition({ qty: 2, initialQty: 2 })), T0).state;
  state = applyEvent(state, positionEvent(2, openPosition({ qty: 8, stop: null, target: null, observedAt: t1 })), T0 + MIN).state;
  assert.equal(state.position.state, "OPEN");
  assert.equal(state.position.initialQty, 8);
  assert.equal(state.position.stop, 29871.5, "同じトレードの間は直前の SL を保つ");
  assert.equal(state.position.filledAt, new Date(T0).toISOString());
  const partial = applyEvent(state, positionEvent(3, openPosition({ qty: 1, observedAt: t2 })), T0 + 2 * MIN).state;
  assert.equal(partial.position.state, "PARTIAL");
  assert.equal(partial.position.initialQty, 8);
});

test("R73: positionGeneration が変わった建玉は別トレード(CLOSED を挟まなくても引き継がない)", () => {
  const t1 = new Date(T0 + MIN).toISOString();
  let state = baseState();
  state = applyEvent(state, positionEvent(1, openPosition({ qty: 8, initialQty: 8, positionGeneration: "PG:1" })), T0).state;
  state = applyEvent(state, positionEvent(2, openPosition({ qty: 4, positionGeneration: "PG:1", observedAt: t1 })), T0 + MIN).state;
  assert.equal(state.position.state, "PARTIAL");
  const next = applyEvent(state, positionEvent(3, openPosition({
    side: "SHORT", qty: 6, avgEntry: 29551.75, positionGeneration: "PG:2", stop: null, target: null,
    filledAt: t1, observedAt: t1,
  })), T0 + MIN).state;
  assert.equal(next.position.state, "OPEN");
  assert.equal(next.position.initialQty, 6);
  assert.equal(next.position.stop, null, "別トレードに前の SL を貼らない");
  assert.equal(next.position.filledAt, t1);
});
