// R52: MANAGEMENT intent の qty 上限を ULTRA エンベロープ(口座別最大枚数)まで広げる。
// ULTRA の runner は 1 枚ではない(2026-09-05: SHORT 12 → TP1 後 runner 6)。従来は
// risk.fixedQty(2)で縛っていたので、runner 6 の建値移動 intent が Python 側でも
// Worker 側でも invalid になり、TP1 後の SL 変更が原理的に出せなかった。
import { test } from "node:test";
import assert from "node:assert/strict";
import { normalizeManagementIntent, managementIntentHash } from "../src/state_machine.js";
import executionContract from "../../execution_contract.json" with { type: "json" };

function intent(qty) {
  return {
    version: executionContract.management.version, accountId: "LFE05062316710024", symbol: "MNQU6",
    positionGeneration: "PG:1:POS:" + "a".repeat(64), action: "MODIFY", side: "SELL", qty,
    stop: "29556.75", target: "29350.75", executionContractVersion: executionContract.version,
  };
}

test("ULTRA の runner 枚数(6)は MANAGEMENT intent として有効", () => {
  const checked = normalizeManagementIntent(intent(6));
  assert.equal(checked.ok, true, checked.reason);
  assert.equal(checked.intent.qty, 6);
  assert.match(managementIntentHash(intent(6)) || "", /^mi_[0-9a-f]{64}$/);
});

test("通常の runner 1 枚も従来どおり有効", () => {
  assert.equal(normalizeManagementIntent(intent(1)).ok, true);
});

test("ULTRA 上限(maxQtyPerAccount)を超える qty と 0 は無効", () => {
  const max = Number(executionContract.ultra.maxQtyPerAccount);
  assert.equal(normalizeManagementIntent(intent(max)).ok, true);
  assert.equal(normalizeManagementIntent(intent(max + 1)).ok, false);
  assert.equal(normalizeManagementIntent(intent(0)).ok, false);
});
