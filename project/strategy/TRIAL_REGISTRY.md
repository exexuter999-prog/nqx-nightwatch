# Trial Registry — 事前登録制バリアント台帳

運用規則: 設計書J-6・GPT指示書6-2の通り。**実行前に1行追加してから実行する。** 登録なしに実行した結果は採用判定に使えない。

累計試行数nに応じたOOS要求PF引き上げ: n≤10: 1.3 / 11–50: 1.4 / 51+: 1.5（DSR的割引の運用形）

## 登録テーブル

以下はM1（ORB）の設計書6-1バリアント行列を事前登録したものである。**2026-07-18時点でユーザーの判断により、TradingView Strategy Testerでの実測実行は本セッションのスコープ外とされた**（GPT_IMPLEMENTATION_REPORT.md R2完了節参照）。実行理由・パラメータ定義は事前登録の体裁を保つが、結果参照・判定は空欄のまま残す。**空欄を数値で埋める行為は絶対規範1（数値を発明しない）への違反であり、将来実測を再開する際にのみ埋めてよい。**

| 試行ID | 日付 | モジュール | バリアント定義（全パラメータ） | 実行理由 | 結果参照 | 判定 |
|---|---|---|---|---|---|---|
| M1-00 | 未実行 | M1 | orMinutes=5, direction=both, stopMode=OR_OPPOSITE, gates=なし, costProfile=BASELINE, sampleWindow=IS | 基準形（Zarattini 2023準拠） | 未実行 | 未実行 |
| M1-01 | 未実行 | M1 | orMinutes=15, direction=both, stopMode=OR_OPPOSITE, gates=なし, costProfile=BASELINE, sampleWindow=IS | TradeThatSwing形 | 未実行 | 未実行 |
| M1-02 | 未実行 | M1 | orMinutes=5, direction=both, stopMode=OR_OPPOSITE, gates=R-A(0930-1130), costProfile=BASELINE, sampleWindow=IS | 時間帯限定 | 未実行 | 未実行 |
| M1-03 | 未実行 | M1 | orMinutes=5, direction=both, stopMode=OR_OPPOSITE, gates=R-A+R-B(1.2), costProfile=BASELINE, sampleWindow=IS | レジーム完全形 | 未実行 | 未実行 |
| M1-04 | 未実行 | M1 | orMinutes=5, direction=both, stopMode=ATR_FRACTION(0.10), gates=R-A+R-B(1.2), costProfile=BASELINE, sampleWindow=IS | Concretum形ストップ | 未実行 | 未実行 |
| M1-05 | 未実行 | M1 | orMinutes=5, direction=both, stopMode=OR_OPPOSITE, gates=R-A+R-B(1.2)+R-D, costProfile=BASELINE, sampleWindow=IS | Structure合成（当方仮説、良くなるとは宣言しない） | 未実行 | 未実行 |
| WG-H1-00 | 2026-07-27事前登録・2026-07-29初回実行前control amendment・未実行 | WG-H1 | instrument=MNQ, eligibleRegime=RG-H1 TR/EX with frozen METRIC evidence or TR-only strict SCREENSHOT fallback, setup=BC/PC, U=SMA14(TR) from 15 exact unique/ascending/M.tf-spaced/latest-closed O.c, stop=max(structure±0.50U, entry±1.50U), targets=exact mapped roadblocks at 1.80R/2.80R/4.00R, eventBlackout=±30m, noAutoBEBefore=1.25R, postFillWiden=false, capLock=symbol/date/session, stressSizing=R+8pt, friction=2/4/8pt, sampleWindow=OOS | wide SL/TP仮説を固定し、通常ノイズ回避と遠方MFE捕捉を反証可能にする。2026-07-29追加は初回実行前のfail-closed controlで、edge定数は不変。優位性は未証明 | `WIDE_GEOMETRY_WG_H1.md` | HEURISTIC / NOT PROVEN |

## 累計試行数カウンタ

- 総登録数: 7（事前登録のみ。実行済みは0）
- 現在の要求OOS PF: 1.3（n=0、実行based カウントは0のまま初期値を維持）

---
*本registryはRESULTS/ディレクトリと対をなす。試行IDはRESULTS/配下のファイル名prefixと一致させる。*
