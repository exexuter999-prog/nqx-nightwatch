/**
 * R6: シナリオ評価の描画(APP_EVAL_DISPLAY_SPEC §6)。
 * DOM もネットワークも使わない。ここは「判定しない」ことの検証でもある。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  advisoryChips, decisionBanner, evalChecklist, gateBoard, gradeContradicts, num,
} from "../evalcard.js";

function evaluation(overrides = {}) {
  return {
    at: "2026-08-18T02:49:30+09:00",
    volGate: { noise: 12.5, slCap: 25, ratio: 0.5, ruling: "A+のみ" },
    rotation: { signals: 5, negations: 2, verdict: "OK" },
    msnr: {
      label: "Weekly Mid", price: 30253, tier: 3, confluence: 2,
      freshness: "FLIPPED", side: "SELL", chainType: "FLIP",
      chainState: "FLIP_HELD", barsLeft: 7, allowed: true, grade: "A+", blockers: [],
    },
    summary: "MSNR: …",
    ...overrides,
  };
}

test("evaluation が無ければ全て空文字(R6 以前と同じ表示になる)", () => {
  for (const empty of [null, undefined, "", 0, "not-an-object"]) {
    assert.equal(evalChecklist(empty), "");
    assert.equal(advisoryChips(empty), "");
    assert.equal(gateBoard(empty), "");
  }
});

test("チェックリストは5行固定で、通過/停止/未取得を描き分ける", () => {
  const html = evalChecklist(evaluation());
  assert.equal((html.match(/class="gate-row/g) || []).length, 5);
  assert.match(html, /VOL/);
  assert.match(html, /ROTATION/);
  assert.match(html, /MSNR/);
  // htf / entry を渡していないので未取得(gate-idle)になる
  assert.equal((html.match(/gate-idle/g) || []).length, 2);
  assert.match(html, /gate-pass/);
  assert.match(html, /gate-warn/); // A+のみは停止ではないが無条件PASSでもない
});

test("停止帯と ROTATION は halt として描かれる", () => {
  const html = evalChecklist(evaluation({
    volGate: { noise: 23.1, slCap: 25, ratio: 0.93, ruling: "停止" },
    rotation: { signals: 6, negations: 4, verdict: "ROTATION" },
    msnr: { ...evaluation().msnr, allowed: false, grade: null,
            blockers: ["FLIP_RETEST_NOT_HELD", "ANCHOR_BROKEN"] },
  }));
  assert.equal((html.match(/gate-halt/g) || []).length, 3);
  assert.match(html, /FLIP_RETEST_NOT_HELD/);   // blocker コードはそのまま出す
});

test("htf / entry / tp を渡すと未取得ではなくなる", () => {
  const html = evalChecklist(evaluation({
    htf: { ctTrend: -1, ctAtr: 35.45, aligned: true },
    entry: { structure: 30253, airPt: 2, airPct: 13, reachR: 0.7, pass: true },
  }));
  assert.equal((html.match(/gate-idle/g) || []).length, 0);
  assert.match(html, /−1/);
  assert.match(html, /35\.5pt/);
  assert.match(html, /13% · reach 0\.7R/);
});

test("非アクティブなADVISORYはSUPPORTINGではなくCONTEXTになる", () => {
  const html = advisoryChips(evaluation({
    advisory: { vwap: { side: "BUY", state: "VWAP_ACCEPTED", drift: 0.8 },
                vp: { side: "SELL", state: "VP_ACCEPTED", target: 29950 } },
  }));
  assert.match(html, /CONTEXT/);
  assert.match(html, /VWAP RECLAIM VWAP_ACCEPTED\(drift 0\.8pt\)/);
  assert.match(html, /VP VP_ACCEPTED → 29,950\.00/);
});

test("drift 無効は「無効」と出る", () => {
  const html = advisoryChips(evaluation({
    advisory: { vwap: { side: null, state: "VWAP_DRIFT", drift: 11.1 } },
  }));
  assert.match(html, /VWAP 無効\(drift 11\.1pt\)/);
  assert.match(html, /is-inactive/);
});

test("DATA/CVD/ICT時間/最終MODELを同じゲートボードに表示する", () => {
  const html = gateBoard(evaluation({
    dataGate: { status: "FRESH", requiredFresh: true, freshCount: 4,
      requiredCount: 4, oldestAgeSec: 96, sourceSpanSec: 90, staleRequired: [] },
    cvdGate: { status: "FRESH", available: true, freshness: "FRESH",
      aplusAllowed: true, attempts: 1, maxAttempts: 2, refreshRequired: false },
    sessionGate: { window: "LUNCH", label: "NYランチ", tradeable: false, et: "12:25" },
    decision: { model: "FLAT", side: "FLAT", state: "WATCH", grade: null,
      hardBlockers: ["NO_A_OR_A_PLUS_MODEL"] },
    msnr: { ...evaluation().msnr, allowed: false, chainState: "EXPIRED", barsLeft: 0,
      blockers: ["CHAIN_EXPIRED", "ANCHOR_BROKEN"] },
  }), "01:25", { marketAgeSec: 12, marketStale: false, marketVerified: true });
  assert.match(html, /DATA/);
  assert.match(html, /4\/4 FRESH · age 12s · span 90s/);
  assert.match(html, /CVD/);
  assert.match(html, /FRESH · 1\/2 reads · A\+ ENABLED/);
  assert.match(html, /ICT TIME/);
  assert.match(html, /LUNCH · 12:25 ET · CONTEXT ONLY/);
  assert.match(html, /FLAT · NO_A_OR_A_PLUS_MODEL/);
  assert.match(html, /CHAIN_EXPIRED \/ ANCHOR_BROKEN/);
  assert.ok((html.match(/gate-warn/g) || []).length >= 1);
});

test("市場ストリームがstaleなら取得時receiptがfreshでもDATAを停止表示する", () => {
  const html = gateBoard(evaluation({
    dataGate: { status: "FRESH", requiredFresh: true, freshCount: 4,
      requiredCount: 4, oldestAgeSec: 20, sourceSpanSec: 10, staleRequired: [] },
  }), "", { marketAgeSec: 700, marketStale: true, marketVerified: false });
  assert.match(html, /gate-row gate-halt[^>]*><span class="gate-label">DATA/);
  assert.match(html, /age 700s/);
});

test("ゲートボードは比率をメーターにし、色調を ruling で決める", () => {
  const halt = gateBoard(evaluation({
    volGate: { noise: 23.1, slCap: 25, ratio: 0.93, ruling: "停止" },
  }), "02:49");
  assert.match(halt, /gate-meter halt/);
  assert.match(halt, /width:93\.0%/);
  assert.match(halt, /02:49/);

  const pass = gateBoard(evaluation({
    volGate: { noise: 6, slCap: 25, ratio: 0.24, ruling: "通常" },
  }));
  assert.match(pass, /gate-meter pass/);
});

test("比率が欠損してもメーターは 0% で描かれ、例外を投げない", () => {
  const html = gateBoard(evaluation({ volGate: { ruling: "不明" } }));
  assert.match(html, /width:0\.0%/);
  assert.match(html, /\?/);
});

test("壊れた evaluation でも例外を投げない", () => {
  const broken = [
    { volGate: null, rotation: null },
    { volGate: {}, rotation: {}, msnr: null, advisory: "nope" },
    { volGate: { ratio: "x" }, rotation: { signals: null }, msnr: { blockers: "no" } },
    { volGate: { ratio: NaN }, rotation: {}, advisory: { vwap: {}, vp: {} } },
  ];
  for (const ev of broken) {
    assert.doesNotThrow(() => evalChecklist(ev));
    assert.doesNotThrow(() => advisoryChips(ev));
    assert.doesNotThrow(() => gateBoard(ev));
  }
});

test("HTML はエスケープされる(ラベルは外部由来)", () => {
  const html = evalChecklist(evaluation({
    msnr: { ...evaluation().msnr, allowed: false,
            blockers: ["<script>alert(1)</script>"] },
  }));
  assert.ok(!html.includes("<script>"));
  assert.match(html, /&lt;script&gt;/);
});

test("A+のみ帯で等級が足りなければ矛盾として扱う", () => {
  const band = evaluation();                       // ruling = "A+のみ"
  assert.equal(gradeContradicts(band, "A+"), false);
  assert.equal(gradeContradicts(band, "A"), true);
  assert.equal(gradeContradicts(band, null), true);
  // 通常帯なら等級を問わない
  const normal = evaluation({ volGate: { ratio: 0.2, ruling: "通常" } });
  assert.equal(gradeContradicts(normal, "A"), false);
  assert.equal(gradeContradicts(normal, null), false);
  assert.equal(gradeContradicts(null, null), false);
});

test("num は欠損を 0 で埋めない", () => {
  assert.equal(num(1.234), "1.23");
  assert.equal(num(null), "—");
  assert.equal(num(NaN), "—");
  assert.equal(num("5"), "—");
});

test("R11-D decision は最有力モデルと価格を表示し、補助証拠を武装根拠外と呼ばない", () => {
  const ev = evaluation({
    decision: {
      model: "VP80_REVERSION", side: "SELL", state: "ARMED", grade: "A+", score: 10,
      entry: 30020.25, stop: 30034.5, targets: [30005],
    },
    advisory: { vp: { side: "SELL", state: "VP_ACCEPTED", target: 30005 } },
  });
  const banner = decisionBanner(ev);
  assert.match(banner, /MODEL VP80_REVERSION/);
  assert.match(banner, /A\+ · 10/);
  assert.match(banner, /30,020\.25/);
  assert.match(advisoryChips(ev), /SUPPORTING/);
  assert.doesNotMatch(advisoryChips(ev), /OBSERVING/);
});

test("カードは武装を止めている理由と SL 幅を載せる(価格だけで終わらせない)", () => {
  // 2026-09-01 実機の状態。A+ なのに TARGET_HEADROOM_INSUFFICIENT で WATCH 止まり。
  // 以前は `TP —` とだけ出て、理由はゲート行を読むまで分からなかった。
  const ev = evaluation({
    decision: {
      model: "BREAKER_CONTINUATION", side: "SELL", state: "WATCH", grade: "A+", score: 10,
      entry: 29411.0, stop: 29431.5, targets: [], targetR: [],
      hardBlockers: ["TARGET_HEADROOM_INSUFFICIENT"],
    },
  });
  const card = decisionBanner(ev);
  assert.match(card, /decision-blocker">TARGET_HEADROOM_INSUFFICIENT/, "理由を出す");
  assert.match(card, /is-held/, "WATCH は武装済みの見た目にしない");
  assert.match(card, /data-side="SHORT"/);
  assert.match(card, /SL 20\.50pt/, "SL 幅は2価格の差だけで出す");
  assert.match(card, /decision-state">WATCH/);
  // TP が無い脚を勝手に埋めない。
  assert.match(card, /TP1<\/span><b>—<\/b>/);
  // R はサーバーの targetR が無い限り出さない(ここで R:R を計算し直さない)。
  assert.doesNotMatch(card, /R<\/span>/);
});

test("ARMED は武装済みの見た目で、targetR があれば R を出す", () => {
  const card = decisionBanner(evaluation({
    decision: {
      model: "VP80_REVERSION", side: "BUY", state: "ARMED", grade: "A", score: 8,
      entry: 30000, stop: 29980, targets: [30040], targetR: [2], hardBlockers: [],
    },
  }));
  assert.match(card, /is-armed/);
  assert.match(card, /data-side="LONG"/);
  assert.match(card, /TP1 2\.00R/);
  assert.doesNotMatch(card, /decision-blocker/, "止める理由が無ければ行を作らない");
});

test("カードが出るサイクルは MODEL 行を重複させない", () => {
  const withCard = gateBoard(evaluation({
    decision: { model: "VP80_REVERSION", side: "SELL", state: "ARMED", grade: "A+",
      score: 10, entry: 30020.25, stop: 30034.5, targets: [30005], hardBlockers: [] },
  }), "01:25");
  assert.match(withCard, /decision-banner/);
  assert.doesNotMatch(withCard, /gate-label">MODEL/, "カードと同じ内容の行は出さない");
  // FLAT はカードが出ないので、行だけが最終判定の置き場になる。
  const flat = gateBoard(evaluation({
    decision: { model: "FLAT", side: "FLAT", state: "WATCH", grade: null,
      hardBlockers: ["NO_A_OR_A_PLUS_MODEL"] },
  }), "01:25");
  assert.match(flat, /gate-label">MODEL/);
  assert.doesNotMatch(flat, /decision-banner/);
});

test("DATA は落ちている側を必ず名指しする(FRESH と ✗ を同時に出さない)", () => {
  const fresh = { status: "FRESH", requiredFresh: true, freshCount: 8,
    requiredCount: 8, oldestAgeSec: 96, sourceSpanSec: 60, staleRequired: [] };
  // 受領書は健全・市況だけ古い → 「8/8 FRESH」は残しつつ MARKET STALE を出す。
  const stale = gateBoard(evaluation({ dataGate: fresh }), "",
    { marketAgeSec: 6130, marketStale: true, marketVerified: true });
  assert.match(stale, /8\/8 FRESH · age 6130s · span 60s · MARKET STALE/);
  // 受領書が非FRESH → 本数を FRESH と呼ばない。
  const receipt = gateBoard(evaluation({
    dataGate: { ...fresh, requiredFresh: false, freshCount: 6 },
  }), "", { marketAgeSec: 12, marketStale: false, marketVerified: true });
  assert.match(receipt, /6\/8 RAW/);
  assert.match(receipt, /RECEIPT STALE/);
  assert.doesNotMatch(receipt, /6\/8 FRESH/);
});

test("シナリオカードのチェックリストもゲートボードと同じ DATA 判定になる", () => {
  const ev = evaluation({
    dataGate: { status: "FRESH", requiredFresh: true, freshCount: 8,
      requiredCount: 8, oldestAgeSec: 96, sourceSpanSec: 60, staleRequired: [] },
  });
  const runtime = { marketAgeSec: 6130, marketStale: true, marketVerified: true };
  // runtime を渡さなかった頃は、同じサイクルでもカードは ✓・ボードは ✗ だった。
  assert.match(evalChecklist(ev, runtime),
    /gate-row gate-halt[^>]*><span class="gate-label">DATA/);
  assert.match(gateBoard(ev, "", runtime),
    /gate-row gate-halt[^>]*><span class="gate-label">DATA/);
});

test("FLAT decision と失効VPをSUPPORTINGと誤表示しない", () => {
  const html = advisoryChips(evaluation({
    decision: { model: "FLAT", side: "FLAT", state: "WATCH", hardBlockers: ["NO_A_OR_A_PLUS_MODEL"] },
    advisory: { vp: { side: "SELL", state: "EXPIRED", target: 28947.75 } },
  }));
  assert.match(html, /CONTEXT/);
  assert.match(html, /is-inactive/);
  assert.doesNotMatch(html, /SUPPORTING/);
});

test("R66: decision が方向を持たない夜の 15M ALIGN は ✗ ではなく未判定(·)", () => {
  const flat = evalChecklist(evaluation({
    htf: { ctTrend: -1, ctAtr: 26.77, aligned: false },
    decision: { model: "FLAT", side: "FLAT", state: "WATCH", hardBlockers: ["NO_A_OR_A_PLUS_MODEL"] },
  }));
  const row = flat.match(/<div class="gate-row [^"]*"><span class="gate-label">15M ALIGN<\/span>[^<]*<span class="gate-value">[^<]*<\/span>/)[0];
  assert.match(row, /gate-idle/);
  assert.match(row, /NO SIDE/);
  // 方向のある decision で aligned=false なら従来どおり ✗
  const against = evalChecklist(evaluation({
    htf: { ctTrend: -1, ctAtr: 26.77, aligned: false },
    decision: { model: "BREAKER_CONTINUATION", side: "BUY", state: "WATCH", hardBlockers: [] },
  }));
  const row2 = against.match(/<div class="gate-row [^"]*"><span class="gate-label">15M ALIGN<\/span>/)[0];
  assert.match(row2, /gate-halt/);
});
