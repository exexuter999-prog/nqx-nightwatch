// 実行モード(AUTO / ULTRA)の設定タブと保存。
//
// 保存してよいのは「利用者がどのモードを選んだか」だけ。建玉・注文・
// シナリオ・市況の正本は今までどおりサーバーから毎回取り直す。ここが崩れると
// 「ブラウザに残った古い状態を verified として出す」事故に戻る。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { accountPlan, signalGeometry } from "../ultra.js";

const APP = readFileSync(new URL("../app.js", import.meta.url), "utf8");
const HTML = readFileSync(new URL("../index.html", import.meta.url), "utf8");
const STATE_CLIENT = readFileSync(new URL("../state_client.js", import.meta.url), "utf8");
const STYLES = readFileSync(new URL("../styles.css", import.meta.url), "utf8");

test("設定タブがあり、モードの切り替えはそこに置かれている", () => {
  assert.match(HTML, /data-view="settings"/, "設定タブがドックにある");
  assert.match(HTML, /id="settingsView"/, "設定ビューがある");
  // トグルは設定ビューの中にあること(ヘッダーには残さない)。
  const settingsSection = HTML.slice(HTML.indexOf('id="settingsView"'),
    HTML.indexOf("</section>", HTML.indexOf('id="settingsView"')));
  assert.match(settingsSection, /id="onePassToggle"/, "ONE-PASS は設定タブ");
  assert.match(settingsSection, /id="ultraToggle"/, "ULTRA は設定タブ");
  const deck = HTML.slice(HTML.indexOf('class="command-deck"'),
    HTML.indexOf("</div>", HTML.indexOf('class="command-deck"')));
  assert.ok(!deck.includes('id="ultraToggle"'), "ヘッダーにトグルを二重に置かない");
  assert.ok(!deck.includes('id="onePassToggle"'), "ヘッダーにトグルを二重に置かない");
});

test("保存するのは ULTRA だけで、AUTO と取引状態は保存しない", () => {
  assert.match(APP, /const SETTINGS_KEY = "nqx\.modes\.v2"/, "版付きのキーを使う");
  // localStorage を触るのは saveModes / loadModes の2か所 + R62 のカーソルの型(表示設定)の読み書き2か所。
  const uses = APP.match(/localStorage\?\./g) || [];
  assert.equal(uses.length, 4, `localStorage の使用は4か所のまま (${uses.length})`);
  assert.match(APP, /setItem\(CARET_STORAGE_KEY, caretVariant\)/, "カーソルの型は表示設定として保存(取引状態ではない)");
  const saved = APP.slice(APP.indexOf("function saveModes()"), APP.indexOf("function saveModes()") + 400);
  // R54: sound は端末側の音声設定(表示設定と同じ扱い)。取引状態ではない。
  assert.match(saved, /JSON\.stringify\(\{ ultra: ultraMode, sound: soundMode \}\)/,
    "保存する値は ULTRA と SOUND(表示/端末設定)だけ");
  assert.ok(!saved.includes("onePass"), "AUTO 権限を端末へ保存しない");
  for (const forbidden of ["position", "scenario", "order", "market", "accounts"]) {
    assert.ok(!new RegExp(`setItem\\([^)]*${forbidden}`).test(APP),
      `${forbidden} を保存していない`);
  }
});

test("保存領域が使えなくても操作は止まらない", () => {
  // プライベートモード等で localStorage が例外を投げても既定 OFF で動く。
  const load = APP.slice(APP.indexOf("function loadModes()"), APP.indexOf("function saveModes()"));
  assert.match(load, /try \{/, "読み出しは try で囲む");
  assert.match(load, /catch \{/, "失敗しても落とさない");
  assert.match(load, /return null;/, "失敗時は既定へ倒す");
});

test("復元した ULTRA は黙って ON にならない", () => {
  const boot = APP.slice(APP.indexOf("const storedModes = loadModes();"));
  assert.match(boot, /if \(ultraMode\) \{[\s\S]*?notify\(/,
    "ULTRA を復元したら通知を出す");
  assert.match(APP, /renderModeBadges\(\)/, "ヘッダーにモードバッジを出す");
  assert.match(HTML, /id="modeBadges"/, "バッジの置き場がある");
  // 設定画面の注意書きは利用者の指示で撤去済み。復元の通知とバッジで足りる。
});

test("AUTO は Worker の期限付き権限を正本にし、手動AUTO送信ボタンを持たない", () => {
  assert.match(STATE_CLIENT, /\/api\/autotrade/, "AUTO mutation endpoint を使う");
  assert.match(STATE_CLIENT, /async function setAutotrade\(/, "AUTO mutation helper がある");
  assert.match(APP, /syncAutotradeAuthority\(view\)/, "server snapshot から AUTO を同期する");
  assert.match(APP, /await client\.setAutotrade\(desired, \{ ttlMinutes: 420 \}\)/,
    "スイッチ操作を7時間の期限付き権限として送る");
  assert.ok(!APP.includes("storedModes.onePass"), "localStorage から AUTO を復元しない");
  assert.ok(!HTML.includes("AUTO-EXECUTE"), "AUTO はシナリオごとの手動送信を要求しない");
});

test("AUTO権限と現在のシグナル状態をARMED表記で混同しない", () => {
  const noScenario = APP.slice(APP.indexOf("function evaluateOnePass"),
    APP.indexOf("function syncOnePass"));
  assert.match(noScenario, /status: "LISTENING"/, "無シナリオ時はLISTENING(内部状態は据え置き)");
  assert.doesNotMatch(noScenario, /status: "ARMED"/, "AUTO権限だけでシグナルをARMEDにしない");
  assert.match(APP, /AUTO IS ON — WAITING FOR AN A \/ A\+ SETUP/,
    "権限ONとシグナル待ちを平易な英語で明示する");
  assert.match(APP, /ON UNTIL \$\{armExpiry\} · \$\{armAccounts\} ACCOUNT/,
    "期限と口座数は短いメタ行にする");
});

test("状態バッジは内部状態名ではなく普通の言葉で出す", () => {
  // CSS は data-status / autotrade-<state> に依存しているので、内部名は
  // 変えずにラベルだけを差し替える。両方が揃っていることを固定する。
  assert.match(APP, /const ONE_PASS_LABELS = \{/, "レール見出しのラベル表がある");
  assert.match(APP, /LISTENING: "WATCHING"/, "LISTENING は WATCHING と読ませる");
  assert.match(APP, /const AUTOTRADE_LABELS = \{/, "AUTOTRADE バッジのラベル表がある");
  assert.match(APP, /CLAIMED: "RESERVED"/, "CLAIMED は RESERVED と読ませる");
  assert.match(APP, /autotrade-\$\{state\.toLowerCase\(\)\}/, "CSS クラスは内部状態名のまま");
  assert.match(APP, /decisiveRail\.dataset\.status = tone/, "data-status も内部状態名のまま");
  // 専門語がそのまま画面へ出ていないこと
  assert.ok(!APP.includes('detail = "ENTRY claim held"'), "ENTRY claim held は残っていない");
  assert.ok(!HTML.includes("AUTO GATE"), "見出しは AUTO TRADING に改名済み");
});

test("AUTO TRADING レールは全幅で状態行と説明行の2行に固定する", () => {
  const railCss = STYLES.slice(STYLES.indexOf(".decisive-rail {"),
    STYLES.indexOf('.decisive-rail[hidden]'));
  // 2026-09-01: meta 列が auto だと「(34M LEFT) · 3 ACCOUNTS」のような長い注記が
  // status 列を潰し、nowrap の状態語が meta の上へ重なって描かれた(実測で 5px)。
  // 両列とも 0 まで縮められることを固定する。
  // 2026-09-05 (R55): 1fr/auto では auto 側(meta)が先に幅を取り、状態語が
  // 「WA…」に切れていた。状態語は max-content で全文、meta が 1fr で省略される。
  assert.match(railCss, /grid-template-columns:\s*minmax\(0,\s*max-content\) minmax\(0,\s*1fr\)/,
    "状態語は全文を出し、長いメタ情報のほうを省略する(どちらも 0 まで縮められる)");
  assert.doesNotMatch(railCss, /grid-template-columns:\s*minmax\(0,\s*1fr\) minmax\(0,\s*auto\)/,
    "状態語が先に削られる旧配分に戻っていない");
  assert.match(railCss, /"status meta"\s*"message message"/,
    "説明文は必ず独立した2行目に置く");
  assert.match(STYLES, /\.decisive-rail p \{[\s\S]*?grid-area:\s*message/,
    "説明文へgrid areaを割り当てる");
  assert.match(STYLES, /\.decisive-rail p \{[\s\S]*?overflow-wrap:\s*anywhere/,
    "狭い画面でもカード外へはみ出さない");
});

test("狭い画面で状態語とメタが重ならない(はみ出しは省略記号で切る)", () => {
  const headCss = STYLES.slice(STYLES.indexOf(".decisive-rail-head {"),
    STYLES.indexOf(".decisive-rail p {"));
  assert.match(headCss, /overflow:\s*hidden/, "head からはみ出させない");
  assert.match(headCss, /\.decisive-rail-head strong \{[\s\S]*?text-overflow:\s*ellipsis/,
    "長い状態語は省略する");
  const metaCss = STYLES.slice(STYLES.indexOf(".decisive-rail > .micro {"));
  assert.match(metaCss.slice(0, 260), /text-overflow:\s*ellipsis/, "長いメタも省略する");
  // 同型のはみ出しがトップバーでも起きていた(ピルがロゴへ 70px 食い込む)。
  const pillCss = STYLES.slice(STYLES.indexOf(".status-pill {"),
    STYLES.indexOf(".status-pill .dot {"));
  assert.match(pillCss, /min-width:\s*0/, "ピルは縮められる");
  // min-width:0 だけでは縮まなかった(親が縮んでもピルは max-content のまま)。
  assert.match(pillCss, /max-width:\s*100%/, "親の幅を超えさせない");
  assert.match(pillCss, /overflow:\s*hidden/, "ピルからはみ出させない");
  assert.match(STYLES, /\.wordmark \{[\s\S]*?flex:\s*0 0 auto/, "ロゴは縮まない");
});

test("碑文は装飾なので本文の下に敷く", () => {
  // z-index:4 では .app-shell(z-index:1)より手前に固定表示され、
  // スクロールしてきたチャートやゲート行の上に重なって読めなくしていた。
  const inscription = STYLES.slice(STYLES.indexOf(".inscription {"),
    STYLES.indexOf(".inscription {") + 700);
  assert.match(inscription, /position:\s*fixed/);
  assert.match(inscription, /z-index:\s*0/, "本文より奥へ置く");
});

test("モードカードは名前とスイッチだけで、ON の状態はスイッチの属性で表す", () => {
  assert.match(HTML, /class="mode-card mode-card-auto"/, "AUTO カードがある");
  assert.match(HTML, /class="mode-card mode-card-ultra"/, "ULTRA カードがある");
  assert.match(HTML, /id="onePassToggle" class="mode-switch" type="button" role="switch"/, "AUTO はスイッチ");
  assert.match(HTML, /id="ultraToggle" class="mode-switch mode-switch-ultra" type="button" role="switch"/, "ULTRA はスイッチ");
  assert.ok(!/setting-note|setting-warning/.test(HTML), "説明文を持たない");
  assert.ok(!/ONE-PASS/.test(HTML.replace(/<!--[\s\S]*?-->/g, "")), "画面上の ONE-PASS 表記は AUTO へ改名済み");
  assert.match(APP, /onePassToggle\.setAttribute\("aria-checked"/, "ON/OFF は aria-checked で表す");
  assert.match(APP, /ultraToggle\.setAttribute\("aria-checked"/, "ON/OFF は aria-checked で表す");
});

test("残ドローダウン $4,000 の口座は 90枚でぎりぎり通る", () => {
  // デモの APEX-05 と同じ条件。境界の見え方をここで固定する。
  const geometry = signalGeometry(
    { side: "SHORT", entry: 30126.0, stop: 30147.0, target: 30076.0 });
  const row = accountPlan(
    { id: "APEX-05", cap: 180, buffer: 4000, profitTarget: 9000 }, geometry);
  assert.equal(row.qty, 90);
  assert.equal(row.projectedProfit, 9000);
  assert.equal(row.projectedLoss, 3780);
  assert.equal(row.verdict, "ELIGIBLE");
  assert.equal(row.drawdownUsedPct, 94.5, "残DD の 94.5% を使う");
  assert.deepEqual(row.legs, [45, 45]);
  assert.deepEqual(row.contractBlockers, [], "ULTRA エンベロープ内");

  // あと $220 減っていれば通らない。境界が効いていることを確かめる。
  const tighter = accountPlan(
    { id: "APEX-05", cap: 180, buffer: 3779, profitTarget: 9000 }, geometry);
  assert.equal(tighter.verdict, "INELIGIBLE");
  assert.ok(tighter.reasons.includes("DRAWDOWN_EXCEEDED"));
});

test("デモに残DD $4,000 の架空口座がある", () => {
  assert.match(APP, /id: "APEX-05", label: "APEX-05", cap: 180, buffer: 4000, profitTarget: 9000/,
    "デモ口座 APEX-05 が定義されている");
});

test("ULTRA が ON の間だけシナリオカードに虹色リングが付く", () => {
  const CSS = readFileSync(new URL("../styles.css", import.meta.url), "utf8");
  // クラスは ultraMode にだけ連動する(armed や demo とは独立)。
  assert.match(APP, /\$\{ultraMode \? "ultra-live" : ""\}/, "ultra-live は ultraMode 連動");
  assert.match(CSS, /\.state-card\.scenario\.ultra-live::after[\s\S]*?conic-gradient\(/,
    "縁は conic-gradient の虹色");
  // R55: 同じ ::after を使う上辺ハイライト規則の height:1px が生き残り、リングが
  // カードの上に浮く1本の横線に潰れていた。height/width の明示を固定する。
  const ring = CSS.slice(CSS.indexOf(".state-card.scenario.ultra-live::after {"),
    CSS.indexOf("}", CSS.indexOf(".state-card.scenario.ultra-live::after {")));
  assert.match(ring, /height:\s*auto/, "リングは高さをカード全体に取る(1px に潰れない)");
  assert.match(ring, /width:\s*auto/, "リングは幅をカード全体に取る");
  assert.match(CSS, /animation: ultra-spin [\d.]+s linear infinite/, "縁は回り続ける");
  // 動きを抑える設定でも静止させない(動いている縁が識別の根拠なので)。
  // reduced-motion ブロックのうち、ULTRA のリングを扱うものを探す(位置に依存しない)。
  const reducedBlocks = CSS.split("@media (prefers-reduced-motion: reduce)").slice(1);
  assert.ok(reducedBlocks.some((block) => /\.ultra-live::after[\s\S]*?animation: ultra-pulse/.test(block)),
    "reduced-motion でも脈動は残す");
  // @property 非対応環境のフォールバックがある。
  assert.match(CSS, /@supports not \(background: conic-gradient\(from var\(--ultra-angle\)/,
    "角度が回らない環境では脈動に落ちる");
});
