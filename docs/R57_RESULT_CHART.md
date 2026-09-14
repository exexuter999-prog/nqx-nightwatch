# R57 — 決済リザルトの根拠チャート(result.chart)

2026-09-05 ユーザー要望: 「損益画面のチャートとページ UI を大幅に改良。チャートはトレードの
根拠が分かるように、アートを作る気持ちで作りこむ」。

## 何が変わったか

- **result に `chart` が付く**(表示専用、任意)。建玉 45 分前〜決済 15 分後の確定 3 分足、
  VP 水準(C:/P: の VAH・VAL・POC)、凍結ターゲット(TP1/TP2)、根拠タグ、減点、HTF 構造、
  ボラ比、セッション区分。**足はシステムが取得した実データだけ**(`.secrets/tv_raw/bars3m.json`、
  無ければ監査バンドルの `snapshot.bars3m`)。合成しない。無ければ `bars` は空で、画面は従来の
  path 描画に落ちる。
- Mini App のリザルト画面(`telegram_mini_app/result.js` + `resultchart.js`)は `chart` があれば
  「根拠チャート」を描く: 保有外の足は薄く・保有中は濃く、受け入れたリスク(ENTRY↔SL、rust)と
  狙った報酬(ENTRY↔TP1、green)の箱、VP 水準の点線、IN/OUT の縦線、保有中の MAE/MFE の印、
  損切り後に TP1 へ届いた印(「TP1 +11m after SL」)、決済点のフレア。
- 画面下段は「根拠プレート(モデル・等級)+ 根拠タグ / 減点のチップ」と、2 列の計測
  (ENTRY / EXIT / REALISED R / HELD | MAE / MFE / EFFICIENCY / EXIT BY、AFTER EXIT、SESSION、HTF)。
  数字は全て trade/chart に実在した値から表示層が導出する(HANDOFF §3 の契約は変えていない)。
- 書き出しカード(1080×1350)にも同じチャートとチップ・2 列計測が入る。

## データ契約

```
chart = {
  version: "NQX-RESULT-CHART/1", tf: 180,
  bars: [[t, o, h, l, c], ...],          # ≤ 80 本、t 昇順、l ≤ min(o,c) ≤ max(o,c) ≤ h
  levels: [{label: "C: VAH", price}, …], # ≤ 12
  tp1, tp2,                              # tick 整合(null 可)
  evidence: [...], penalties: [...],     # ≤ 16、英大文字の識別子
  htf: {"1h": "UP", ...}, volRatio, noise, session, source
}
```

- 送り側: `result_context.build_chart()` / `attach()`。`trade_journal` の 2 経路(ライブ決済・
  fills 遡及)で自動付与。
- 受け側: `cloudflare/src/state_machine.js` の `validateResultChart()`。上限超え・形崩れは
  **result ごと拒否**(黙って切らない)。旧 result(chart なし)は `chart: null`。
- 送り直し: `python trade_journal.py --backfill --account <ID> --publish --republish --mode LIVE`。
  resultId が同じなので DO の resultLog は置き換え(二重掲載にならない)。fills が空の日
  (取引日の切替後)はスコアカード行から組む必要がある(2026-09-05 朝はその方法で 6 件送り直した)。

## 検証

- Python: `tests/test_result_context.py`(12)、`tests/test_trade_journal.py`(import 白リストに
  `result_context` を追加)。
- Worker: `cloudflare/test/r57_result_chart.test.mjs`(4、174 全件 PASS)。deploy 済み。
- Mini App: `telegram_mini_app/test/resultchart.test.mjs`(8、173 全件 PASS)。`npm run build` /
  `verify:build` 通過、Pages へ deploy 済み。デモ(`?demo=1`)のモデル付き記録には架空の chart
  (`source: "demo-fiction"`)を持たせて画面確認できる。

## 2026-09-05 朝の追加(数字が見えないバグ / 結果プレート)

- **数字が隠れた原因**: 計測行を足した結果 `.ui`(flex 縦)が画面より高くなり、flex がピルを数十 px に
  圧縮して金額が見えなくなった。あわせて 2 列の横並び行は値が `…` に省略され、チャート右端では
  価格目盛りと ENTRY/SL ラベルが重なっていた。
- **対処**: `.ui` を縦スクロール可・子は `flex-shrink:0`。丸いピルを廃止し、**結果プレート**(金額 +
  「枚数 × 銘柄 · pt · 保有時間」+ R リング: 計画 R に対する実現 R)へ。計測は「ラベルの下に値」の
  タイル(値は省略しない)。チャートのラベルは全て暗い下地付き(`tag()`)、価格目盛りの文字は消して
  線だけ(数字は ENTRY/SL/TP と水準ラベルが持つ)。出所チップは見出しと重ねず根拠チップ側へ。
- 書き出しカードも同じ構成(プレート + リング + チップ + タイル 2 列 × 4 段、段 76px)。

## 既知の限界

- 経路 identity を束縛できなかった古い建玉は建玉時刻が発注時刻(数分〜数十分早い)。
- `chart` は DO の状態スナップショットに載る(1 件 ≈ 1.7 KB、resultLog 50 件で最大 ≈ 100 KB)。
- チャートの時刻ラベルは端末のローカル時刻。フッターの EXP は従来どおり UT。
