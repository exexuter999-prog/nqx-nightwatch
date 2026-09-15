# R103-0 決済トレードの「刈られ方」を計測する

## 目的

2026-09-15 の 4 件の損切りは、方向自体は合っていた可能性がある
(`docs/reports/STOP_HUNT_EVIDENCE_2026-09-15.md`)。「SL を薄く抜かれて、その後に
TP1 まで伸びた(刈られた)」のか「そもそも逆方向だった」のかを、印象ではなく確定足から
機械的に分類する。R103-1(流動性プール検出)/ R103-2(再生)の土台になる計測だけを入れる。

**採点・武装・契約・publish ペイロードは変えていない。** 記録と表示だけ。

## 何が変わる

1. `excursion_metrics.py`(新規・純関数。ネットワーク・台帳・発注に触れない)
   - `classify(trade, bars, tp1, noise) -> dict`
     - `maePt / mfePt / maeR / mfeR / holdMin` 保有中(確定 3 分足の高安)
     - `beyondStopPt` 決済後 15 分に SL をどこまで抜けたか(SL 到達でなければ null)
     - `tp1AfterExitMin` 決済後 180 分以内に TP1 へ届くまでの分(届かなければ null)
     - `fav3hPt` 決済後 3 時間の最大順行 pt
     - `huntClass` `STOP_HUNT` / `WRONG_WAY` / `DEEP` / `WIN` / `FLAT`(判定できなければ null)
   - しきい値は全部モジュール冒頭の定数で、docstring に意味を書いた。
   - `obsidian_metrics.excursions`(R57)の計算本体をこのモジュールへ移し、
     `obsidian_metrics` は呼ぶだけにした。出力は従来どおり(既存のゴールデンテストで固定)。
2. `model_scorecard.record` が書く行に `excursion`(上の dict)が載る。
   - 確定 3 分足は `trade_journal` が既に持っているものを注入する(取り直さない)。
     注入が無ければ `result.chart` の足を使い、足が無ければ `excursion` は null。
   - **Worker へ publish する `result` は変えていない**(`excursion` は台帳の行だけに載る)。
3. `model_scorecard.py --excursions`
   モデル×等級ごとに `huntClass` の内訳と、`beyondStopPt` / `tp1AfterExitMin` の中央値を表にする。
4. `model_scorecard.py --backfill-excursions <bars_dir>`
   `<bars_dir>/*.json` の確定 3 分足で既存行を再計算し、`supersedes` 付きの新行を**追記**する
   (既存行は消さない・書き換えない = R54 と同じ訂正の作法)。
   `tp1` / `noise` は前回の行に残っている入力を引き継ぐ。無ければ null のまま(推測で埋めない)。
5. 出力の文字化け対策: `main()` の頭で `sys.stdout` / `sys.stderr` を utf-8 に再設定する
   (cp932 のコンソールで「—」が `UnicodeEncodeError` になっていた)。

3 分足の中の値動きの順序は見えないので、決済後の到達時刻は**到達した足の開始時刻**で測る
(最短側)。1 分足が入るまでこの値は「最も早くてこの分数」の意味。

## 戻し方

- 表示だけ戻す: `--excursions` / `--backfill-excursions` を使わない(既定の出力は変わらない)。
- 記録だけ戻す: `model_scorecard.classify` の `row["excursion"] = ...` の 1 行を消す
  (`excursion` は新規キーなので、既存の集計・PF・Worker には影響しない)。
- 全部戻す: この PR を revert する。`excursion_metrics.py` を消す場合は
  `obsidian_metrics.excursions` の本体を元に戻す必要がある(R57 の出力は同じ)。

## 検証コマンド

```powershell
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"
python tests/test_r103_excursions.py
python tests/run_all.py
```

`tests/test_r103_excursions.py` は fixture(`tests/fixtures/r103/`)だけで動く。
4 件が全部 `STOP_HUNT`、`beyondStopPt` が 54.0 / 0.0 / 26.25 / 8.25、
`tp1AfterExitMin` が 168 / null / 78 / 21(±3 分)になることを固定し、
ブローカー照会・Worker 取得への到達 0 件を tripwire で確かめる。
