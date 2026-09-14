import { test } from "node:test";
import assert from "node:assert/strict";
import {
  deriveTrade, equitySeries, groupByDay, lifelineHeadline, modelShort, modelStats, participation,
  recentNet, scopeResults,
} from "../ledger.js";

function result(overrides = {}) {
  return {
    resultId: "rs-1",
    side: "LONG",
    symbol: "MNQU6",
    qty: 2,
    entry: 29760.0,
    exit: 29796.0,
    stop: 29726.0,
    pointValue: 2,
    openedAt: "2026-08-13T05:12:00Z",
    closedAt: "2026-08-13T05:26:20Z",
    mode: "SIMULATION",
    ...overrides,
  };
}

test("deriveTrade は result.js と同じ規則で導出する(±2pt は FLAT)", () => {
  const win = deriveTrade(result());
  assert.equal(win.pts, 36);
  assert.equal(win.usd, 144);   // 36pt × $2 × 2枚
  assert.equal(win.state, "win");
  assert.ok(Math.abs(win.r - 36 / 34) < 1e-9);

  const short = deriveTrade(result({ side: "SHORT", exit: 29740.0, stop: 29790.0 }));
  assert.equal(short.pts, 20);
  assert.equal(short.state, "win");

  const flat = deriveTrade(result({ exit: 29761.5 }));
  assert.equal(flat.state, "flat", "+1.5pt が FLAT にならない");

  const loss = deriveTrade(result({ exit: 29740.0 }));
  assert.equal(loss.state, "loss");
  assert.equal(loss.usd, -80);
});

test("R48: modelStats はモデル持ちの記録だけを集計する", () => {
  const rows = [
    result({ resultId: "m1", model: "TURTLE_SOUP_REVERSAL", grade: "A+" }),           // +144
    result({ resultId: "m2", model: "TURTLE_SOUP_REVERSAL", exit: 29740.0 }),         // -80
    result({ resultId: "m3", model: "VP80_REVERSION" }),                              // +144
    result({ resultId: "m4" }),                                                       // model無し → 除外
  ];
  const stats = modelStats(rows);
  assert.equal(stats.length, 2, "UNATTRIBUTED は表に出さない");
  const soup = stats.find((row) => row.model === "TURTLE_SOUP_REVERSAL");
  assert.equal(soup.n, 2);
  assert.equal(soup.wins, 1);
  assert.equal(soup.losses, 1);
  assert.ok(Math.abs(soup.pf - 144 / 80) < 1e-9, "PF = 粗利/粗損");
  const vp = stats.find((row) => row.model === "VP80_REVERSION");
  assert.equal(vp.pf, null, "損失ゼロの PF は null(∞を発明しない)");
  assert.equal(modelShort("TURTLE_SOUP_REVERSAL"), "SOUP");
  assert.equal(modelShort("UNKNOWN_MODEL_X"), "UNKNOW");
});

test("participation は口座の cap だけで判定する(order.py と同じ規則)", () => {
  const accounts = {
    totalBuffer: 1472,
    list: [
      { id: "A", label: "…0002", cap: 60, buffer: 816 },
      { id: "B", label: "…0003", cap: 60, buffer: 407 },
      { id: "C", label: "…0004", cap: 50, buffer: 249 },
    ],
  };
  // リスク 27pt × $2 × 1枚 = $54 → cap 50 の …0004 だけ外れる
  const part = participation(
    { side: "BUY", qty: 1, entry: 30100, stop: 30073, target: 30150 }, accounts,
  );
  assert.equal(part.eligible.length, 2);
  assert.equal(part.excluded[0].label, "…0004");
  assert.equal(part.riskPer, 54);
  assert.equal(part.tpTotal, 200);  // 50pt × $2 × 1枚 × 2口座
  assert.equal(part.slTotal, 108);

  // 全口座が上限内なら除外なし
  const all = participation(
    { side: "BUY", qty: 1, entry: 30100, stop: 30080, target: 30150 }, accounts,
  );
  assert.equal(all.eligible.length, 3);
  assert.equal(all.excluded.length, 0);

  // accounts が無ければ null(表示自体を出さない)
  assert.equal(participation({ entry: 1, stop: 0, target: 2, qty: 1 }, null), null);
});

test("groupByDay は JST 日付でまとめ、日次損益を合計する", () => {
  const groups = groupByDay([
    result({ resultId: "r3", closedAt: "2026-08-14T01:00:00Z", exit: 29740.0 }),   // 8/14 JST, -$80
    result({ resultId: "r2", closedAt: "2026-08-13T05:26:20Z" }),                  // 8/13 JST, +$144
    result({ resultId: "r1", closedAt: "2026-08-13T02:00:00Z", exit: 29726.0 }),   // 8/13 JST, -$136
  ]);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].trades.length, 1);
  assert.equal(groups[0].usd, -80);
  assert.equal(groups[1].trades.length, 2);
  assert.equal(groups[1].usd, 8);
  assert.equal(groups[1].wins, 1);
  assert.equal(groups[1].losses, 1);
});

test("equitySeries は古い順の累積を返す(入力は新しい順)", () => {
  const series = equitySeries([
    result({ resultId: "r2", exit: 29740.0 }),   // 新しい方: -$80
    result({ resultId: "r1" }),                  // 古い方: +$144
  ]);
  assert.deepEqual(series, [144, 64]);
});

test("recentNet は期間内の実現損益だけを合計し、記録が無ければ null", async () => {
  const { recentNet } = await import("../ledger.js");
  const now = Date.parse("2026-08-15T00:00:00Z");
  const rows = [
    result({ resultId: "n2", closedAt: "2026-08-14T10:00:00Z" }),               // +144(期間内)
    result({ resultId: "n1", closedAt: "2026-08-01T10:00:00Z", exit: 29740 }),  // -80(期間外)
  ];
  assert.equal(recentNet(rows, 7, now), 144);
  assert.equal(recentNet([], 7, now), null);
  assert.equal(recentNet(rows, 0.1, now), null, "期間内ゼロ件は 0 ではなく null");
});

// R56: 凍結プランの無い手動建玉(ブローカー約定から組んだ記録)は stop を持たない。
test("R85: Balance はブローカーの純資産。残機(DD 残)は別の数字として出す", () => {
  // 2026-09-13 の実測: netLiq $53,657 / 床 51,657 → DD 残 $2,000。
  const live = lifelineHeadline({ totalBuffer: 2000, list: [{ id: "A", cap: 200, buffer: 2000, equity: 53657 }] });
  assert.deepEqual(live, { label: "Balance", value: 53657, buffer: 2000 });

  const two = lifelineHeadline({ totalBuffer: 3000, list: [
    { id: "A", buffer: 2000, equity: 53657 }, { id: "B", buffer: 1000, equity: 51000.5 }] });
  assert.equal(two.value, 104657.5, "複数口座は純資産を合計する");

  // 1 口座でも純資産が欠けたら、偽の残高を作らず DD 残を DD 残として出す。
  const partial = lifelineHeadline({ totalBuffer: 3000, list: [
    { id: "A", buffer: 2000, equity: 53657 }, { id: "B", buffer: 1000 }] });
  assert.deepEqual(partial, { label: "DD left", value: 3000, buffer: null });

  // Number(null) = 0 の罠(R73)。null の口座を「残高 $0」として足さない。
  const nulled = lifelineHeadline({ totalBuffer: 2000, list: [{ id: "A", buffer: 2000, equity: null }] });
  assert.equal(nulled.label, "DD left");
  assert.equal(nulled.value, 2000);
});

test("R85: LEDGER の集計は今の口座の記録だけ(入れ替え前の口座を NET に混ぜない)", () => {
  const accounts = { list: [{ id: "LFF-6" }] };
  const rows = [
    result({ resultId: "old-eval", accountId: "LFE-24", closedAt: "2026-09-04T19:55:00Z", exit: 29900.0 }),
    result({ resultId: "manual-aug", closedAt: "2026-08-25T12:17:00Z" }),
    result({ resultId: "now-1", accountId: "LFF-6", closedAt: "2026-09-08T10:32:25Z" }),
    result({ resultId: "now-2", accountId: "LFF-6", closedAt: "2026-09-11T20:42:49Z" }),
  ];
  const { scoped, others } = scopeResults(rows, accounts);
  assert.deepEqual(scoped.map((row) => row.resultId), ["now-2", "now-1"]);
  assert.deepEqual(others.map((row) => row.resultId).sort(), ["manual-aug", "old-eval"]);

  // 名簿が届いていなければ絞らない(根拠なく記録を消さない)。並べ替えだけはする。
  const unscoped = scopeResults(rows, null);
  assert.equal(unscoped.scoped.length, 4);
  assert.equal(unscoped.others.length, 0);
  assert.equal(unscoped.scoped[0].resultId, "now-2");
});

test("R85: 送り直しで publish 順が崩れても決済時刻の新しい順に並べ直す", () => {
  // Worker の resultLog は publish 順。訂正で送り直した古い記録が先頭に来る。
  const rows = [
    result({ resultId: "resent-old", accountId: "A", closedAt: "2026-09-08T10:32:25Z" }),
    result({ resultId: "newest", accountId: "A", closedAt: "2026-09-11T20:42:49Z" }),
    result({ resultId: "middle", accountId: "A", closedAt: "2026-09-10T17:02:04Z" }),
  ];
  const { scoped } = scopeResults(rows, { list: [{ id: "A" }] });
  assert.deepEqual(scoped.map((row) => row.resultId), ["newest", "middle", "resent-old"]);
  // 並び替えた後の equitySeries は古い順の累積になる(入力は新しい順の前提)。
  assert.equal(equitySeries(scoped).length, 3);
  // 7D は絞った記録だけで数える。
  const now = Date.parse("2026-09-12T00:00:00Z");
  assert.equal(recentNet(scoped, 7, now), 3 * deriveTrade(rows[0]).usd);
});

test("stop が無い記録は R を出さず、損益は変わらない", () => {
  const trade = deriveTrade(result({ stop: null, side: "SHORT", entry: 29529.5, exit: 29538.75, qty: 28 }));
  assert.equal(trade.r, null, "R は —");
  assert.equal(trade.state, "loss");
  assert.ok(Math.abs(trade.usd - (-9.25 * 2 * 28)) < 1e-9, "損益は約定どおり");
  const missing = deriveTrade(result({ stop: undefined }));
  assert.equal(missing.r, null);
  assert.equal(missing.usd, 144);
});
