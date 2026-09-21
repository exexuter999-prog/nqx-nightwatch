/**
 * R47: 口座別 ULTRA シナリオの検証。
 *
 * ULTRA の目印は「凍結された riskCapSource が ULTRA エンベロープの正本値」。
 * この目印があるときだけ枚数・脚・リスク上限が ULTRA 側の規則になり、
 * 無ければ従来の固定2枚・$240 のまま —— ここの分離をテストで固定する。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  evaluateExecutionContract, ultraSplit, validateScenario,
} from "../src/state_machine.js";
import executionContract from "../../execution_contract.json" with { type: "json" };

const T0 = Date.parse("2026-08-28T14:00:00Z");
const MIN = 60_000;

const ULTRA_CONTRACT = {
  riskCapDollars: 4000,
  riskCapSource: "ACCOUNT_DRAWDOWN_BUFFER",
  accountScope: ["ACC-ULTRA-01"],
};

function ultraScenario(overrides = {}) {
  return {
    scenarioId: "sc-ultra-1",
    fingerprint: "fp-ultra-1",
    state: "ACTIVE",
    symbol: "MNQU6",
    side: "SELL",
    qty: 90,
    entry: 30126.0,
    stop: 30147.0,
    target: 30105.0,
    targets: [30105.0, 30076.0],
    legs: [{ id: "TP1", qty: 45, target: 30105.0 },
      { id: "RUNNER", qty: 45, target: 30076.0 }],
    planVersion: "R47-ULTRA-1",
    grade: "A",
    issuedAt: new Date(T0).toISOString(),
    observedAt: new Date(T0).toISOString(),
    expiresAt: new Date(T0 + 15 * MIN).toISOString(),
    executionContract: ULTRA_CONTRACT,
    ...overrides,
  };
}

const market = {
  observedAt: new Date(T0).toISOString(),
  cvdAt: new Date(T0).toISOString(),
};

test("ULTRA シナリオ(90枚・脚45/45)は validateScenario を通る", () => {
  const checked = validateScenario(ultraScenario(), { symbol: "MNQU6" });
  assert.equal(checked.ok, true, checked.reason);
  assert.equal(checked.scenario.qty, 90);
  assert.deepEqual(checked.scenario.legs.map((leg) => leg.qty), [45, 45]);
  assert.equal(checked.scenario.executionContract.riskCapSource, "ACCOUNT_DRAWDOWN_BUFFER");
});

test("ULTRA の目印が無い 90枚は従来どおり拒否される", () => {
  const noMarker = ultraScenario({ executionContract: { riskCapDollars: 240, riskCapSource: "RISK_X", accountScope: ["ACC-ULTRA-01"] } });
  const checked = validateScenario(noMarker, { symbol: "MNQU6" });
  assert.equal(checked.ok, false);
  assert.match(checked.reason, /qty/);
});

test("ULTRA シナリオは実行契約が orderable と判定する", () => {
  const checked = validateScenario(ultraScenario(), { symbol: "MNQU6" });
  const contract = evaluateExecutionContract(checked.scenario, market, null, null, T0);
  assert.deepEqual(contract.blockers, [], contract.blockers.join(","));
  assert.equal(contract.orderable, true);
  // リスク: 21pt × 90枚 × $2 = $3,780。上限は min($4,000, $5,000)。
  assert.equal(contract.riskDollars, 3780);
  assert.equal(contract.riskCapDollars, 4000);
  assert.equal(contract.riskCapSource, "ACCOUNT_DRAWDOWN_BUFFER");
});

test("残DDを超える枚数は RISK_CAP_EXCEEDED", () => {
  const checked = validateScenario(ultraScenario({
    executionContract: { ...ULTRA_CONTRACT, riskCapDollars: 3000 },
  }), { symbol: "MNQU6" });
  const contract = evaluateExecutionContract(checked.scenario, market, null, null, T0);
  assert.ok(contract.blockers.includes("RISK_CAP_EXCEEDED"));
});

// R109(2026-09-18): ULTRA は複数口座で張れる。弾くのは 0 口座と ultra.maxAccounts 超えだけ。
test("複数口座の凍結スコープは scope で弾かれない", () => {
  const checked = validateScenario(ultraScenario({
    executionContract: { ...ULTRA_CONTRACT, accountScope: ["ACC-A1", "ACC-B2"] },
  }), { symbol: "MNQU6" });
  const contract = evaluateExecutionContract(checked.scenario, market, null, null, T0);
  assert.ok(!contract.blockers.includes("ULTRA_SCOPE_OUT_OF_ENVELOPE"),
    JSON.stringify(contract.blockers));
});

test("上限を超えた口座数は ULTRA_SCOPE_OUT_OF_ENVELOPE", () => {
  const max = Number(executionContract.ultra.maxAccounts);
  const scope = Array.from({ length: max + 1 }, (_value, index) => `ACC-${index}`);
  const checked = validateScenario(ultraScenario({
    executionContract: { ...ULTRA_CONTRACT, accountScope: scope },
  }), { symbol: "MNQU6" });
  const contract = evaluateExecutionContract(checked.scenario, market, null, null, T0);
  assert.ok(contract.blockers.includes("ULTRA_SCOPE_OUT_OF_ENVELOPE"),
    JSON.stringify(contract.blockers));
});

test("口座ゼロの凍結スコープは ULTRA_SCOPE_OUT_OF_ENVELOPE", () => {
  const checked = validateScenario(ultraScenario({
    executionContract: { ...ULTRA_CONTRACT, accountScope: [] },
  }), { symbol: "MNQU6" });
  const contract = evaluateExecutionContract(checked.scenario, market, null, null, T0);
  assert.ok(contract.blockers.includes("ULTRA_SCOPE_OUT_OF_ENVELOPE"),
    JSON.stringify(contract.blockers));
});

test("エンベロープ外の枚数は ULTRA_QTY_OUT_OF_ENVELOPE", () => {
  // validateScenario は 100 枚まで通すので、契約評価側の判定を 101 相当で直接見る。
  const checked = validateScenario(ultraScenario(), { symbol: "MNQU6" });
  const oversized = { ...checked.scenario, qty: 101 };
  const contract = evaluateExecutionContract(oversized, market, null, null, T0);
  assert.ok(contract.blockers.includes("ULTRA_QTY_OUT_OF_ENVELOPE"));
});

test("通常シナリオの契約評価は一切変わらない(固定2枚・$240)", () => {
  const normal = ultraScenario({
    qty: 2,
    legs: [{ id: "TP1", qty: 1, target: 30105.0 }, { id: "RUNNER", qty: 1, target: 30076.0 }],
    executionContract: { riskCapDollars: 240, riskCapSource: "RISK_ACC", accountScope: ["ACC-A1", "ACC-B2"] },
  });
  const checked = validateScenario(normal, { symbol: "MNQU6" });
  assert.equal(checked.ok, true, checked.reason);
  const contract = evaluateExecutionContract(checked.scenario, market, null, null, T0);
  assert.deepEqual(contract.blockers, []);
  assert.equal(contract.riskDollars, 84);   // 21pt × 2枚 × $2
  assert.equal(contract.riskCapDollars, 240);
});

test("ultraSplit は端数を runner へ寄せる", () => {
  assert.deepEqual(ultraSplit(90), [45, 45]);
  assert.deepEqual(ultraSplit(91), [45, 46]);
  assert.equal(ultraSplit(1), null);
});
