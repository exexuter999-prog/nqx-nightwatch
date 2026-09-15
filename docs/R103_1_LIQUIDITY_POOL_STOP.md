# R103-1 SL 側の流動性プール検出と、SL をプールの向こうへ逃がす節

## 目的

2026-09-15 の 4 件の損切りは、方向は合っていたのに SL を薄く抜かれて戻された
(`docs/reports/STOP_HUNT_EVIDENCE_2026-09-15.md` §3〜§4、分類は R103-0 の `excursion`)。
共通していたのは **SL が未回収の流動性プール(スイング高安 / セッション高安 / VAH・VAL /
前日高安)の手前に置かれていた**こと。22:38 の SELL は SL 29,444.50 が VAH かつスイング高値の
29,447.00 の **2.5pt 手前**で、そこを取りに来た足に刈られてから 60pt 順行した。

この PR は **計測と契約の節だけ**を入れる。既定は SHADOW(記録専用)で、判定・SL・decisionId は
現行と 1 バイトも変わらない。LIVE に倒すのは人で、R103-2(再生)の結果を見てから。

## 何が変わる

1. `liquidity_pools.py`(新規・純関数。ネットワーク・台帳・発注に触れない)
   - `pools(bars, levels, price, tol, noise)`
     → `[{price, kind, label, barT, ageBars, equalCount, swept}]`
     `kind` は `SWING_HIGH` / `SWING_LOW`(**`msnr_gate.swing_liquidity` をそのまま呼ぶ**。
     再実装していない)、`SESSION`(Asia / London / New York の High|Low)、
     `VA_EDGE`(`C:` / `P:` の VAH・VAL)、`PD_EXTREME`(PDH / PDL / Previous Day High|Low)。
     同じ価格に複数の根拠があれば**まとめない**(29,447.00 が SWING_HIGH かつ VA_EDGE、という
     重なりがプールの厚み)。`equalCount` は同じ側の極値が `tol` 以内に並んだ本数(EQH / EQL)。
     `swept` は「最後に触れた**後**の足がその向こうを取ったか」= swing_liquidity の回収判定と同じ見方。
   - `stop_pool_audit(side, entry, stop, pools, noise, rule)`
     → `{between, beyondWithinN, nearestBeyond, inside025N, required, applied, reason}`
     `required` = 最寄りの外側プールの向こう `clearN×N` を `stop_logic.outward_tick` で不利側へ丸めた値。
     プールが無い / `withinN×N` より遠い / SL が既に向こう側なら `required` は null で、`reason` は
     `NO_POOL` / `POOL_FAR_FROM_STOP` / `STOP_ALREADY_BEYOND_POOL` / `POOLS_MISSING`(levels も bars も無い)。
2. `execution_contract.json` に `stopLogic.poolClearance` と `stopLogic.restingStopRecheck` を追加
   (どちらも既定 **SHADOW**)。`stop_logic.load_policy` は節ごとに独立して検証し、壊れていれば
   **その節だけ** OFF(vwapClearance / marketStopGuard / flipOrigin には影響しない)。
3. `msnr_gate._candidate_for_chain` で `_apply_vwap_clearance` の直後に R90 と同じ形で適用する。
   - `OFF` = 何もしない。候補にも decision にも `poolStop` キーが付かない(R103 以前と同一)。
   - `SHADOW` = 候補に `poolStop`(compact な監査)と記録専用タグ `STOP_POOL_WITHIN_1N` /
     `POOL_BETWEEN_ENTRY_STOP` を足すだけ。SL・score・grade・state・decisionId は動かない。
   - `LIVE` = SL を `required` に置換し、`POOL_STOP_CLEARED`。SL を内側へ縮めることはしない。
     60pt 上限・R:R は**置換後の SL** で判定し、壊れれば候補は WATCH。decisionId も最終 SL で決まる。
   - `select_primary` の返り値に `vwapStop` と同じ compact 形の `poolStop` を載せる(OFF ではキーごと無い)。
     評価カード(公開状態・監査コピー `monitor_cycle_HHMM.json` の `evaluation.decision`)にも同じ
     `poolStop` を載せる。4096 バイト上限で縮小するときは evidence の記録タグより**先に** `poolStop` を
     落とす(監査は再生で復元できる。タグはスコアカードの分離キーなので残す)。直近 240 周期の実測では
     カード最大 2,743 バイト、`poolStop` 最大 512 バイトで上限に届かない。
4. 指値の SL 再検査(SHADOW のみ)
   - `stop_logic.resting_stop_recheck(side, entry, stop, bars_now, at_iso, rule)`
     → `{noiseNow, noiseSessionOpen, distPt, stale, reason}`。
     `noiseNow` は直前 12 本の確定足レンジの中央値(`msnr_gate.noise_floor` と同定義)。
     寄付き(`opensEt`)から `minutes` 以内は `max(noiseNow, 寄付き以降の確定足のレンジ中央値)`。
     建値→SL が `minN×N` を割っていれば `stale=true`(理由 `RESTING_STOP_BELOW_MIN_N`)。
   - `autotrade_engine` は **ENTRY_RESTING を保持している周期だけ**これを評価し、stale なら台帳へ
     `RESTING_STOP_STALE`(`action=RESTING_RECHECK`、`plan` を持たない、同じ key・同じ理由につき 1 回)を
     **書くだけ**。取消も変更も送らない。発注・取消・HALT の経路は触っていない。
5. `msnr_gate.feature_tags` に記録専用タグ `LEVEL_CHOPPED_3`(直前 20 本の終値がレベルを 3 回以上横断)。
   採点・等級・decisionId には影響しない。

22:38 の例: 既定 SHADOW だと候補は SL 29,444.50 のまま武装し、`poolStop` に
`required = 29,451.00`(= 29,447.00 + 0.25N を不利側 tick へ)と `beyondWithinN = [29,447.00 ×2]`、
`inside025N` が残る。LIVE に倒せばこの 29,451.00 が実際の SL になる。

## 戻し方

- 監査ごと止める: `execution_contract.json` の `stopLogic.poolClearance.mode` を `"OFF"` に。
  指値の再検査も止めるなら `stopLogic.restingStopRecheck.mode` を `"OFF"` に。どちらも 1 値の変更で、
  他の節には影響しない(節ごとに独立して検証している)。
- コードごと戻す場合はこの PR の revert。`liquidity_pools.py` は新規ファイルで、OFF の間は
  `msnr_gate` から呼ばれない。

## 検証コマンド

```powershell
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"; python tests/run_all.py
```

この PR のテストだけを見る場合:

```powershell
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"; python tests/test_r103_liquidity_pools.py
```

`tests/test_r103_liquidity_pools.py` は fixture(`tests/fixtures/r103/`)の 4 件を武装時点の窓
(`cases[].closedBarsAtArm` = 60 本)で評価し、22:38 の `required = 29,451.00`、21:20 の
`beyondWithinN = 29,388.50` / `between = 29,402.25`、16:49・21:34 の `beyondWithinN` が空であること、
22:36 JST 時点の指値再検査が `stale=true` になることを固定する。SHADOW の出力から `poolStop` と
記録専用タグを落とすと OFF とバイト一致することも固定してある。台帳は tempdir、
`broker_status.query_*` / `nqx_state.*` / `autotrade_arm.state` への到達は tripwire で 0 件。
