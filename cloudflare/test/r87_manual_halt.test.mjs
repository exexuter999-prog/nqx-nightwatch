/**
 * R87: 設定ページの手動HALT。
 *
 * Worker が保存するのは「止めたい」という意図だけで、止めるのは監視PC。ここで固定するのは
 * (1) 真偽値以外で ON/OFF を作らない、(2) 投影に必ず載る、(3) 旧 state でも壊れない、
 * (4) 押した人の ID を view に出さない、の 4 点。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { MANUAL_HALT_SCHEMA, buildManualHalt, emptyState, projectState } from "../src/state_machine.js";

const T0 = Date.parse("2026-09-14T07:00:00Z");

test("真偽値以外の enabled は拒否する(たぶん ON を作らない)", () => {
  for (const body of [null, [], {}, { enabled: "true" }, { enabled: 1 }]) {
    const built = buildManualHalt(null, body, T0, "u1", "initData");
    assert.equal(built.ok, false, JSON.stringify(body));
    assert.match(built.reason, /boolean/);
  }
});

test("ON → OFF の遷移と時刻", () => {
  const on = buildManualHalt(null, { enabled: true, reason: "  様子見  " }, T0, "u1", "initData");
  assert.equal(on.ok, true);
  assert.equal(on.halt.schemaVersion, MANUAL_HALT_SCHEMA);
  assert.equal(on.halt.enabled, true);
  assert.equal(on.halt.status, "HALTED");
  assert.equal(on.halt.engagedAt, new Date(T0).toISOString());
  assert.equal(on.halt.reason, "様子見");
  assert.deepEqual(on.transitions, [{ kind: "manual_halt", from: "RELEASED", to: "HALTED" }]);

  const again = buildManualHalt(on.halt, { enabled: true }, T0 + 60_000, "u1", "initData");
  assert.equal(again.halt.engagedAt, on.halt.engagedAt, "ON の再送で開始時刻を動かさない");
  assert.equal(again.halt.updatedAt, new Date(T0 + 60_000).toISOString(), "押した事実は updatedAt に残る");

  const off = buildManualHalt(on.halt, { enabled: false }, T0 + 120_000, "u1", "initData");
  assert.equal(off.halt.enabled, false);
  assert.equal(off.halt.status, "RELEASED");
  assert.equal(off.halt.releasedAt, new Date(T0 + 120_000).toISOString());
  assert.deepEqual(off.transitions, [{ kind: "manual_halt", from: "HALTED", to: "RELEASED" }]);
});

test("投影に載り、押した人の ID は出さない。旧 state は null", () => {
  const legacy = emptyState("acct", "MNQU6");
  delete legacy.manualHalt;
  assert.equal(projectState(legacy, T0).manualHalt, null, "フィールドが無い旧 state でも落ちない");
  assert.equal(emptyState("acct", "MNQU6").revisions.manual_halt, 0);

  const on = buildManualHalt(null, { enabled: true }, T0, "123456789", "initData");
  const view = projectState({ ...emptyState("acct", "MNQU6"), manualHalt: on.halt }, T0 + 1000);
  assert.equal(view.manualHalt.enabled, true);
  assert.equal(view.manualHalt.status, "HALTED");
  assert.equal(view.manualHalt.source, "TELEGRAM_MINI_APP");
  assert.ok(!("actor" in view.manualHalt), "Telegram user id を view に出さない");
  assert.ok(!("authSource" in view.manualHalt));

  const tampered = projectState({ ...emptyState("acct", "MNQU6"), manualHalt: { ...on.halt, enabled: "yes" } }, T0);
  assert.equal(tampered.manualHalt.enabled, false, "保存値が壊れていても true とは読まない(PC 側は直前の既知値を保持)");
});

test("HALT は期限で消えない(人が外すまで続く)", () => {
  const on = buildManualHalt(null, { enabled: true }, T0, "u1", "initData");
  const later = projectState({ ...emptyState("acct", "MNQU6"), manualHalt: on.halt }, T0 + 72 * 3600_000);
  assert.equal(later.manualHalt.enabled, true);
});
