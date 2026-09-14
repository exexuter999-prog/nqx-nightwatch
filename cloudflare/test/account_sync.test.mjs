/**
 * accounts.sync(口座名簿の突合)の検証。表示専用で、accountScope や
 * 実行契約に影響しないこと、壊れた形は拒否されることを固定する。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { applyEvent, emptyState, projectState, validateAccounts } from "../src/state_machine.js";

const T0 = Date.parse("2026-08-28T14:00:00Z");

const payload = (sync) => ({
  observedAt: new Date(T0).toISOString(),
  source: "crosstrade-balance",
  list: [{ id: "ACC-A", cap: 240, buffer: 2500 }],
  ...(sync === undefined ? {} : { sync }),
});

test("sync が無い accounts は従来どおり通る(フィールドも付かない)", () => {
  const checked = validateAccounts(payload());
  assert.equal(checked.ok, true);
  assert.equal("sync" in checked.accounts, false);
});

test("正常な sync は受理され view に投影される", () => {
  const result = applyEvent(emptyState("acct", "MNQU6"), {
    stream: "account",
    revision: 1,
    payload: { accounts: payload({
      verified: true,
      observedAt: new Date(T0).toISOString(),
      missing: ["ACC-GONE"],
      unknown: ["ACC-NEW"],
      dead: [],
    }) },
  }, T0);
  assert.equal(result.accepted, true);
  const view = projectState(result.state, T0);
  assert.deepEqual(view.accounts.sync.missing, ["ACC-GONE"]);
  assert.deepEqual(view.accounts.sync.unknown, ["ACC-NEW"]);
  assert.deepEqual(view.accounts.sync.dead, []);
  assert.equal(view.accounts.sync.verified, true);
});

test("壊れた sync は拒否される", () => {
  assert.equal(validateAccounts(payload("garbage")).ok, false);
  assert.equal(validateAccounts(payload({ missing: "not-array" })).ok, false);
  assert.equal(validateAccounts(payload({
    missing: Array.from({ length: 17 }, (_, i) => `A${i}`),
  })).ok, false);
  assert.equal(validateAccounts(payload({ missing: [""] })).ok, false, "空IDは拒否");
});

test("verified が真以外は false に倒す(推測で警告しない)", () => {
  const checked = validateAccounts(payload({ verified: "yes", missing: ["X"] }));
  assert.equal(checked.ok, true);
  assert.equal(checked.accounts.sync.verified, false);
});
