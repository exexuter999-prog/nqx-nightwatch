import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const appSource = readFileSync(new URL("../app.js", import.meta.url), "utf8");

test("WATCH は戦績カードを描画せず、結果は LEDGER と結果画面にだけ残す", () => {
  assert.doesNotMatch(appSource, /function\s+resultCard\s*\(/);
  assert.doesNotMatch(appSource, /cards\.push\(resultCard\(/);
  assert.doesNotMatch(appSource, /data-action=["']open-result["']/);

  assert.match(appSource, /renderLedger\s*\(/);
  assert.match(appSource, /function\s+openResult\s*\(/);
});

test("右上 LIVE は停止した lastVerifiedAt ではなく、同期済み現在JSTで進む", () => {
  assert.match(appSource, /createServerClock\(\)/);
  assert.match(appSource,
    /connection\s*===\s*CONNECTION\.LIVE[\s\S]*JST \$\{formatJstClock\(serverNow\(\)\)\}/);
  assert.match(appSource, /function\s+tick\s*\(\)\s*\{[\s\S]*renderStatusPill\(\)/);
  assert.match(appSource, /liveClock\.sync\(view\.serverTime\)/);
  assert.match(appSource, /CLOCK WORKER-SYNCED/);
});
