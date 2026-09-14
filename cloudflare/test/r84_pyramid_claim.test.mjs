/**
 * R84 追撃(pyramid)の CLAIM / CONSUME / stale release。
 *
 * 建玉が開いている間、正本の display は必ず `orderable=false`
 * (「POSITION OPEN — MANAGEMENT ONLY」)になる。追撃はそこを通れなければならないが、
 * **projectState を緩めてはいけない** —— 緩めると Mini App の手動発注ボタンまで出て、
 * 通常経路の CONSUME 封印まで一緒に弱まる。ここでは専用の門だけが開くことを固定する。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  applyEvent, emptyState, projectState, strategyEvidenceHash, entryKeyForTuple,
  executionIntentHash, normalizeExecutionIntent,
  pyramidCombinedEntry, pyramidCombinedRisk, pyramidFitToCap,
  pyramidAdversePrice, pyramidSlippagePoints,
} from "../src/state_machine.js";

const T0 = Date.parse("2026-09-12T02:12:00Z");
const iso = (ms = T0) => new Date(ms).toISOString();
const ACC = "LFF05062316710006";

function frozenEvidence() {
  const raw = { version: "R14-STRATEGY-EVIDENCE-1", asOf: iso(), sessionId: "NY-R84",
    source: "fixture", provenance: "test", models: { ifvg: { valid: false } } };
  return { ...raw, evidenceHash: strategyEvidenceHash(raw) };
}

/** 基礎建玉 4 枚を保有したまま、同方向 A+ が ARMED になった正本。 */
function heldState({ cycleId = "cy-r84", qty = 8, baseQty = 4, avgEntry = 29484.25,
                     grade = "A+", side = "SELL" } = {}) {
  const evidence = frozenEvidence();
  const market = { at: iso(), observedAt: iso(), verified: true, source: "fixture",
    sourceSymbol: "CME_MINI:MNQU6", resolution: "3", barResolution: "3", price: 29448.5,
    cvdAt: iso(), cycleId,
    dayguard: { at: iso(), available: true, blocked: false, codes: [] },
    bars: [{ t: 1700000000, o: 29450, h: 29452, l: 29447, c: 29449 },
      { t: 1700000180, o: 29449, h: 29450, l: 29446, c: 29448.5 }],
    levels: [], strategyEvidence: evidence };
  const scenario = { scenarioId: `sc-${cycleId}`, fingerprint: `fp-${cycleId}`, state: "ARMED",
    symbol: "MNQU6", side, qty, entry: 29448.5, stop: 29507.5, target: 29400,
    targets: [29400, 29300], planVersion: "R19-ICT-SPLIT-1", grade,
    legs: [{ id: "TP1", qty: Math.floor(qty / 2), target: 29400 },
      { id: "RUNNER", qty: qty - Math.floor(qty / 2), target: 29300 }],
    issuedAt: iso(), observedAt: iso(), expiresAt: iso(T0 + 300_000), marketCycleId: cycleId,
    setupVersion: "R14-SETUP", catalogVersion: "R14-CATALOG", detectorVersion: "R14-DETECTOR",
    executionContractVersion: "R22-EXECUTION-CONTRACT-1", evidenceHash: evidence.evidenceHash,
    executionContract: { version: "R22-EXECUTION-CONTRACT-1", riskCapDollars: 3100,
      riskCapSource: "ACCOUNT_DRAWDOWN_BUFFER", accountScope: [ACC] } };
  let result = applyEvent(emptyState("acct", "MNQU6"),
    { stream: "cycle", revision: 1, payload: { cycleId, market, scenario } }, T0);
  assert.equal(result.accepted, true);
  result = applyEvent(result.state, { stream: "position", revision: 1,
    payload: { position: { verified: true, source: "broker", symbol: "MNQU6",
      qty: baseQty, side: side === "BUY" ? "LONG" : "SHORT", avgEntry,
      state: "OPEN", observedAt: iso() } } }, T0);
  assert.equal(result.accepted, true);
  return { state: result.state, scenario: result.state.scenario };
}

function tuple(scenario) {
  return Object.fromEntries(["scenarioId", "fingerprint", "evidenceHash", "marketCycleId"]
    .map((field) => [field, scenario[field]]));
}

function claimPayload(scenario, pyramid, overrides = {}) {
  return { action: "CLAIM", entryKey: entryKeyForTuple(tuple(scenario)),
    tuple: tuple(scenario), claimTokenHash: "a".repeat(64), orderType: "MARKET",
    pyramid, ...overrides };
}

const PY = { baseQty: 4, baseAvgEntry: 29484.25, addQty: 4, addsDone: 0,
  positionGeneration: "PG:1:POS:" + "0".repeat(64),
  preSendOrderIds: ["O-1", "O-2", "O-3", "O-4"] };

test("R84 建玉が開いている間は通常の CLAIM が通らないことを先に確かめる", () => {
  const held = heldState();
  const display = projectState(held.state, T0).display;
  assert.equal(display.orderable, false);
  assert.match(display.blockReason, /POSITION OPEN/);
  const plain = applyEvent(held.state, { stream: "entry_claim", revision: 2,
    payload: claimPayload(held.scenario, undefined) }, T0);
  assert.equal(plain.accepted, false);
  assert.match(plain.reason, /ENTRY_CLAIM_NOT_ORDERABLE/);
});

test("R84 追撃 CLAIM は建玉由来のゲートだけを差し替えて通る", () => {
  const held = heldState();
  const claimed = applyEvent(held.state, { stream: "entry_claim", revision: 2,
    payload: claimPayload(held.scenario, PY) }, T0);
  assert.equal(claimed.accepted, true, claimed.reason || "");
  const claim = claimed.state.entryClaim;
  assert.equal(claim.state, "CLAIMED");
  assert.deepEqual(claim.pyramid.baseQty, 4);
  assert.deepEqual(claim.pyramid.addQty, 4);
  // intent が運ぶ枚数は **足す枚数**。合計目標ではない。
  assert.equal(claim.executionIntent.qty, 4);
  assert.deepEqual(claim.executionIntent.legs,
    [{ id: "TP1", qty: 2, target: "29400.00" }, { id: "RUNNER", qty: 2, target: "29300.00" }]);
  assert.deepEqual(claim.executionIntent.pyramid, { baseQty: 4, addQty: 4 });
  assert.equal(claim.executionIntentHash, executionIntentHash(claim.executionIntent));
  // 投影にも印が出る(storage にしか無い値はエンジンから見えない)。
  assert.deepEqual(projectState(claimed.state, T0).entryClaim.pyramid.baseQty, 4);
});

test("R84 追撃 CLAIM は DO 自身の建玉で全部再検証する", () => {
  const held = heldState();
  const cases = [
    [{ ...PY, baseQty: 5 }, /BASE_QTY_MISMATCH/],
    [{ ...PY, addQty: 5 }, /ADD_QTY_MISMATCH/],   // 目標との差(4)より多い = 不可
    [{ ...PY, addQty: 0 }, /PYRAMID_UNAVAILABLE/],
    [{ ...PY, addsDone: 2 }, /MAX_ADDS_REACHED/],
    [{ ...PY, baseAvgEntry: 29400 }, /BASE_ENTRY_DEVIATION/],
    [{ ...PY, baseQty: 0 }, /PYRAMID_UNAVAILABLE/],
  ];
  for (const [pyramid, pattern] of cases) {
    const rejected = applyEvent(held.state, { stream: "entry_claim", revision: 2,
      payload: claimPayload(held.scenario, pyramid) }, T0);
    assert.equal(rejected.accepted, false, JSON.stringify(pyramid));
    assert.match(rejected.reason, pattern);
  }
});

test("R84 上限で刻んだ追撃(目標より少ない枚数)は claim できる", () => {
  // `pyramid._fit_to_cap` は合計リスクが口座上限に収まるまで枚数を刻んで落とす。
  // 門が完全一致を要求していると、刻んだ追撃が全部拒否されて刻む機構が丸ごと死ぬ。
  const held = heldState({ cycleId: "cy-clamp" });
  const clamped = applyEvent(held.state, { stream: "entry_claim", revision: 2,
    payload: claimPayload(held.scenario, { ...PY, addQty: 2 }) }, T0);
  assert.equal(clamped.accepted, true, clamped.reason || "");
  assert.equal(clamped.state.entryClaim.executionIntent.qty, 2);
  assert.deepEqual(clamped.state.entryClaim.executionIntent.pyramid,
    { baseQty: 4, addQty: 2 });
});

test("R84 逆方向・等級不足・建玉なしは追撃にならない", () => {
  // SELL のシグナルに対し、建玉が LONG(逆方向)。追撃は「同方向の建て増し」だけ。
  const opposite = heldState({ cycleId: "cy-side" });
  const flipped = applyEvent(opposite.state, { stream: "position", revision: 2,
    payload: { position: { verified: true, source: "broker", symbol: "MNQU6", qty: 4,
      side: "LONG", avgEntry: 29484.25, state: "OPEN", observedAt: iso() } } }, T0);
  const sideMismatch = applyEvent(flipped.state, { stream: "entry_claim", revision: 3,
    payload: claimPayload(opposite.scenario, PY) }, T0);
  assert.equal(sideMismatch.accepted, false);
  assert.match(sideMismatch.reason, /SIDE_MISMATCH/);

  const low = heldState({ cycleId: "cy-grade", grade: "B" });
  const gradeRejected = applyEvent(low.state, { stream: "entry_claim", revision: 2,
    payload: claimPayload(low.scenario, PY) }, T0);
  assert.equal(gradeRejected.accepted, false);
  assert.match(gradeRejected.reason, /GRADE_BELOW_MIN/);

  const flat = heldState({ cycleId: "cy-flat" });
  const flattened = applyEvent(flat.state, { stream: "position", revision: 2,
    payload: { closedAt: iso(), position: { verified: true, source: "broker",
      symbol: "MNQU6", qty: 0, observedAt: iso() } } }, T0);
  assert.equal(flattened.accepted, true, flattened.reason || "");
  assert.equal(flattened.state.position.state, "CLOSED");
  const noBase = applyEvent(flattened.state, { stream: "entry_claim", revision: 3,
    payload: claimPayload(flat.scenario, PY) }, T0);
  assert.equal(noBase.accepted, false);
  assert.match(noBase.reason, /NO_BASE_POSITION/);
});

test("R84 未終端の注文が残っている間は追撃 claim も取らない", () => {
  // 通常経路は projectState の orderLive で塞がれる。追撃だけ素通りさせると、
  // CONSUME の ENTRY_CLAIM_ORDER_CONFLICT で必ず落ちる claim を取ってしまい、
  // stale 解放(最大 180 秒)まで枠を潰す。
  const held = heldState({ cycleId: "cy-order" });
  const blocked = applyEvent(held.state, { stream: "order", revision: 1,
    payload: { order: { idempotencyKey: "OTHER", state: "PENDING", side: "SELL",
      qty: 2, at: iso() } } }, T0);
  assert.equal(blocked.accepted, true, blocked.reason || "");
  const rejected = applyEvent(blocked.state, { stream: "entry_claim", revision: 2,
    payload: claimPayload(held.scenario, PY) }, T0);
  assert.equal(rejected.accepted, false);
  assert.match(rejected.reason, /ORDER PENDING/);
});

test("R84 合計建玉のリスクが上限を超える追撃は claim できない", () => {
  // 合計 4+4 枚 @ 合成建値 29466.375 → |29507.5 - 29466.375| * 8 * 2 = $658
  const risk = pyramidCombinedRisk(4, 29484.25, 4, 29448.5, 29507.5);
  assert.ok(Math.abs(risk - 658) < 1e-9, String(risk));
  const held = heldState({ cycleId: "cy-risk" });
  // 凍結上限を $500 にしたシナリオでは通らない。
  const tight = JSON.parse(JSON.stringify(held.state));
  tight.scenario.executionContract.riskCapDollars = 500;
  const rejected = applyEvent(tight, { stream: "entry_claim", revision: 2,
    payload: claimPayload(tight.scenario, PY) }, T0);
  assert.equal(rejected.accepted, false);
  assert.match(rejected.reason, /RISK_CAP_EXCEEDED/);
});

test("R84 上限は滑った追撃価格で見る(現在値ぴったりでは枠内でも落とす)", () => {
  // 成行は不利側へ滑る。SELL なら安く売れるので、合成建値は SL 寄りになり
  // **合計リスクは増える**。現在値ぴったりで見積もると「概算では枠内・約定したら
  // 枠超え」が通ってしまう —— 単一脚の経路は昔からここに緩衝を足していた。
  const slipped = pyramidAdversePrice("SELL", 29448.5);
  assert.ok(Math.abs(slipped - (29448.5 - pyramidSlippagePoints())) < 1e-9, String(slipped));
  const observed = pyramidCombinedRisk(4, 29484.25, 4, 29448.5, 29507.5);
  const adverse = pyramidCombinedRisk(4, 29484.25, 4, slipped, 29507.5);
  assert.ok(Math.abs(observed - 658) < 1e-9, String(observed));
  assert.ok(Math.abs(adverse - 674) < 1e-9, String(adverse));

  // 上限 $665 は現在値基準なら通り、滑りを当てると通らない値。
  const held = heldState({ cycleId: "cy-slip" });
  const tight = JSON.parse(JSON.stringify(held.state));
  tight.scenario.executionContract.riskCapDollars = 665;
  const rejected = applyEvent(tight, { stream: "entry_claim", revision: 2,
    payload: claimPayload(tight.scenario, PY) }, T0);
  assert.equal(rejected.accepted, false);
  assert.match(rejected.reason, /RISK_CAP_EXCEEDED/);

  // 滑りを飲み込める上限なら通る(門が常に閉じているわけではないことの裏取り)。
  const loose = JSON.parse(JSON.stringify(held.state));
  loose.scenario.executionContract.riskCapDollars = 675;
  const accepted = applyEvent(loose, { stream: "entry_claim", revision: 2,
    payload: claimPayload(loose.scenario, PY) }, T0);
  assert.equal(accepted.accepted, true, accepted.reason || "");
});

test("R84 追撃 CONSUME は FLAT ではなく宣言どおりの基礎建玉を要求する", () => {
  const held = heldState({ cycleId: "cy-consume" });
  const payload = claimPayload(held.scenario, PY);
  const claimed = applyEvent(held.state, { stream: "entry_claim", revision: 2, payload }, T0);
  assert.equal(claimed.accepted, true, claimed.reason || "");
  const consumed = applyEvent(claimed.state, { stream: "entry_claim", revision: 3,
    payload: { action: "CONSUME", entryKey: payload.entryKey,
      claimTokenHash: payload.claimTokenHash,
      executionIntent: claimed.state.entryClaim.executionIntent,
      executionIntentHash: claimed.state.entryClaim.executionIntentHash } }, T0 + 1000);
  assert.equal(consumed.accepted, true, consumed.reason || "");
  assert.equal(consumed.state.entryClaim.state, "CONSUMED");

  // 建玉が宣言と変わっていたら CONSUME は通らない(送信瞬間の実態とのズレ)。
  const moved = applyEvent(claimed.state, { stream: "position", revision: 3,
    payload: { position: { verified: true, source: "broker", symbol: "MNQU6", qty: 6,
      side: "SHORT", avgEntry: 29470, state: "OPEN", observedAt: iso() } } }, T0);
  const blocked = applyEvent(moved.state, { stream: "entry_claim", revision: 4,
    payload: { action: "CONSUME", entryKey: payload.entryKey,
      claimTokenHash: payload.claimTokenHash,
      executionIntent: claimed.state.entryClaim.executionIntent,
      executionIntentHash: claimed.state.entryClaim.executionIntentHash } }, T0 + 1000);
  assert.equal(blocked.accepted, false);
  assert.match(blocked.reason, /SEAL_INVALID/);
});

test("R84 追撃 CLAIM は基礎建玉を作った claim を引き継ぎ、記録を残す", () => {
  const first = heldState({ cycleId: "cy-base" });
  const basePayload = { action: "CLAIM", entryKey: entryKeyForTuple(tuple(first.scenario)),
    tuple: tuple(first.scenario), claimTokenHash: "b".repeat(64), orderType: "MARKET" };
  // 基礎 claim(建玉が開く前の世界)を作ってから、建玉ありの state へ移植する。
  const flat = JSON.parse(JSON.stringify(first.state));
  flat.position = { verified: true, source: "broker", symbol: "MNQU6", qty: 0,
    observedAt: iso() };
  flat.positionCheck = { verified: true, observedAt: iso() };
  const based = applyEvent(flat, { stream: "entry_claim", revision: 2, payload: basePayload }, T0);
  assert.equal(based.accepted, true, based.reason || "");
  const consumedBase = applyEvent(based.state, { stream: "entry_claim", revision: 3,
    payload: { action: "CONSUME", entryKey: basePayload.entryKey,
      claimTokenHash: basePayload.claimTokenHash,
      executionIntent: based.state.entryClaim.executionIntent,
      executionIntentHash: based.state.entryClaim.executionIntentHash } }, T0 + 1000);
  assert.equal(consumedBase.accepted, true, consumedBase.reason || "");

  // 建玉 4 枚・新しいサイクルのシグナルで追撃 claim。
  const second = heldState({ cycleId: "cy-add" });
  const held = JSON.parse(JSON.stringify(second.state));
  held.entryClaim = consumedBase.state.entryClaim;
  const plain = applyEvent(held, { stream: "entry_claim", revision: 4,
    payload: claimPayload(second.scenario, undefined, { claimTokenHash: "c".repeat(64) }) }, T0);
  assert.equal(plain.accepted, false);

  const added = applyEvent(held, { stream: "entry_claim", revision: 4,
    payload: claimPayload(second.scenario, PY, { claimTokenHash: "c".repeat(64) }) }, T0);
  assert.equal(added.accepted, true, added.reason || "");
  assert.match(added.reason, /PYRAMID_SUPERSEDED/);
  assert.equal(added.state.entrySupersededClaims.length, 1);
  assert.equal(added.state.entrySupersededClaims[0].entryKey, basePayload.entryKey);
  assert.equal(added.state.entryClaim.entryKey, entryKeyForTuple(tuple(second.scenario)));
});

test("R84 追撃 claim の stale release は建玉 FLAT ではなく送信前の集合で判定する", () => {
  const held = heldState({ cycleId: "cy-stale" });
  const payload = claimPayload(held.scenario, PY);
  const claimed = applyEvent(held.state, { stream: "entry_claim", revision: 2, payload }, T0);
  assert.equal(claimed.accepted, true, claimed.reason || "");
  const claim = claimed.state.entryClaim;
  const LATE = T0 + 16 * 60_000;   // claim.maxAgeSec(180s)超

  function withObservation(orders) {
    const state = JSON.parse(JSON.stringify(claimed.state));
    state.brokerObservation = {
      observedAt: iso(LATE - 5_000), positionObservedAt: iso(LATE - 5_000),
      ordersObservedAt: iso(LATE - 5_000), snapshotId: "snap-1", cursor: "c1",
      snapshotMode: "STABLE", stableBeforeHash: "h", stableAfterHash: "h",
      platform: "CROSSTRADE", accountId: ACC, symbol: "MNQU6",
      currentIntentHash: claim.executionIntentHash,
      position: { qty: 4, side: "SHORT", positionGeneration: "PG:1" },
      orders,
    };
    return state;
  }

  const unchanged = withObservation([
    { orderId: "O-1", receipt: "R-1", accountId: ACC, symbol: "MNQU6", status: "WORKING" },
    { orderId: "O-2", receipt: "R-2", accountId: ACC, symbol: "MNQU6", status: "WORKING" },
  ]);
  assert.equal(projectState(unchanged, LATE).entryClaim.staleReleasable, true);

  const newRow = withObservation([
    { orderId: "O-1", receipt: "R-1", accountId: ACC, symbol: "MNQU6", status: "WORKING" },
    { orderId: "NEW", receipt: "R-N", accountId: ACC, symbol: "MNQU6", status: "WORKING" },
  ]);
  assert.equal(projectState(newRow, LATE).entryClaim.staleReleasable, false);

  const grown = withObservation([
    { orderId: "O-1", receipt: "R-1", accountId: ACC, symbol: "MNQU6", status: "WORKING" },
  ]);
  grown.brokerObservation.position.qty = 8;
  assert.equal(projectState(grown, LATE).entryClaim.staleReleasable, false);

  // 合成建玉が決済されて 0 になった後も追撃 claim が残ることがある。baseQty 固定に
  // すると **永久に解けない claim** ができ、以後の新規が全部止まる(この repo の
  // 事故第 1 位)。建玉が baseQty でないときは通常の不在証明へ落ちる。
  const closedAllTerminal = withObservation([
    { orderId: "O-1", receipt: "R-1", accountId: ACC, symbol: "MNQU6", status: "FILLED" },
    { orderId: "NEW", receipt: "R-N", accountId: ACC, symbol: "MNQU6", status: "CANCELED" },
  ]);
  closedAllTerminal.brokerObservation.position.qty = 0;
  assert.equal(projectState(closedAllTerminal, LATE).entryClaim.staleReleasable, true);

  const closedButWorking = withObservation([
    { orderId: "NEW", receipt: "R-N", accountId: ACC, symbol: "MNQU6", status: "WORKING" },
  ]);
  closedButWorking.brokerObservation.position.qty = 0;
  assert.equal(projectState(closedButWorking, LATE).entryClaim.staleReleasable, false);
});

test("R84 契約が許可していない pyramid は黙って通常経路に化けない", () => {
  const intent = normalizeExecutionIntent({
    version: "R18-EXECUTION-INTENT-1", symbol: "MNQU6", side: "SELL", qty: 4,
    orderType: "MARKET", entry: null, last: "29448.50", stop: "29507.50",
    targets: ["29400.00", "29300.00"],
    legs: [{ id: "TP1", qty: 2, target: "29400.00" },
      { id: "RUNNER", qty: 2, target: "29300.00" }],
    planVersion: "R19-ICT-SPLIT-1", executionContractVersion: "R22-EXECUTION-CONTRACT-1",
    accountScope: [ACC], pyramid: { baseQty: 4, addQty: 3 },
  });
  assert.equal(intent.ok, false);
  assert.match(intent.reason, /pyramid/);
});

test("R84 算術は pyramid.py の逐語移植", () => {
  assert.equal(pyramidCombinedEntry(2, 29484.25, 6, 29448.5), 29457.4375);
  assert.equal(pyramidCombinedRisk(2, 29484.25, 6, 29448.5, 29507.5), 801);
  assert.equal(pyramidFitToCap(2, 29484.25, 6, 29448.5, 29507.5, 2, 400), 2);
  assert.equal(pyramidFitToCap(2, 29484.25, 6, 29448.5, 29507.5, 2, 1), 0);
  assert.equal(pyramidCombinedEntry(0, 0, 0, 0), null);
  assert.equal(pyramidCombinedRisk(2, 29484.25, 6, 29448.5, NaN), null);
});
