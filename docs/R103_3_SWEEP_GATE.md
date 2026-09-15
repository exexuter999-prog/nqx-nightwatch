# R103-3 掃引ゲートと指値取消の配線、および SL 狩り対策の再生(2026-09-16)

ユーザー依頼「明日の開場までに SL 狩り対策を実装」への回答。R103-0(計測)と R103-1(プール検出、
SHADOW)を土台に、(1) 契約スイッチを LIVE にしてよいかを R90 と同じ規則で**逐次再生**し、(2) 本命候補だった
「掃引後に入る」ゲートを実装して同じ再生に掛け、(3) 指値の SL 再検査(R103-1、SHADOW)を取消経路へ配線した。
設定は `execution_contract.json` の `stopLogic`、検証は `tests/test_r103_sweep_gate.py`、再生は
`replay_r103.py`(読むだけ)。

## 0. 結論

| 対策 | 実装 | 再生(逐次・1 建玉ずつ) | 本番 |
|---|---|---|---|
| **SL をプールの向こうへ**(`poolClearance`) | R103-1 | ΣR +11.6 → **+19.1**、損切り 14 → 9、TP1 11 → 12、取る数 46 → 34 | **LIVE を推奨** |
| 掃引後に入る(`sweepGate`) | 本 PR | ΣR +11.6 → +16.5。ただし「掃引後に通った」周期は **0**。実効は「プールが SL の外側 1N 以内にある候補の見送り」 | **SHADOW**(記録) |
| 指値の SL 再検査 → 取消(`restingStopRecheck`) | 本 PR(配線) | 再生対象外(engine 側)。09-15 22:38 の 1 件を回避 | LIVE はユーザー判断 |

再生は監査コピー 1,260 本(08-13〜09-15。同じ HH:MM は上書きされるので全周期ではない)を現行コードで
再評価し、R86/R90 と同じ**不利側**の約定規則(確定 3 分足・指値は 1tick 突き抜け・約定前 TP1 で取消・
同じ足は SL 優先・指値待ち 30 分・6 時間・成行の SL 距離ゲート 1.0N 後掛け)で数えた。R は計画の SL 幅。

```text
--- sequential (one position at a time) ---
BASE     taken= 46 fills= 27 tp1= 11 losses= 14 ΣR= +11.61
POOL     taken= 34 fills= 23 tp1= 12 losses=  9 ΣR= +19.13
SWEEP    taken= 27 fills= 18 tp1=  9 losses=  7 ΣR= +16.51
BOTH     taken= 27 fills= 18 tp1=  9 losses=  7 ΣR= +15.36
```

セットアップ単位(同じシグナルを対で数える)では POOL は −0.017R/件・P(改善)0.46 で「差が無い」。
逐次で差が出るのは、プールが近い(= 刈られやすい)セットアップを見送った枠に別のセットアップが入るため。
標本は逐次で 27〜46 件、期間はほぼ 1 か月・1 つの相場環境。**証明ではなく、方向が一貫した仮説**である。
前半 70% / 後半 30% の分割で向きは反転しない(後半: BASE +13.6 / POOL +20.8 / SWEEP +17.0)。

## 1. 掃引ゲート(`stopLogic.sweepGate`)

### 1.1 何をするか

元の SL(VWAP 逃がし後・プール逃がし前)の外側 `poolWithinN`×N 以内に未回収プール
(`liquidity_pools.pools` の SWING / SESSION / VA_EDGE / PD_EXTREME)があるとき、その候補は
レベルでの初回タッチで建てず、**そのプールを極値が抜けて内側で引けた(奪還)確定足**を待つ。

- `liquidity_pools.sweep_reclaim(bars, pool, side, origin_t, tol, reclaim_bars)`: 候補の構造の起点
  (`chain.breakBarT` / `sweepBarT`、VP80 は再突入エピソードの `originBarT`)より後の**最新の**掃引
  エピソードを見る。`reclaim_bars`(既定 2 = 掃引足そのものか次の足)以内に内側で引ければ
  `SWEPT_RECLAIMED`、抜けたまま(受容 or 進行中)なら `SWEPT_NO_RECLAIM`、無ければ `NOT_SWEPT`。
- `liquidity_pools.sweep_gate_audit(...)`: プールが遠ければ `NOT_APPLICABLE`(候補は現行どおり)。
  掃引前 / 未奪還は `PENDING`、奪還から `maxAgeBars`(既定 3)本より経てば `STALE`、新しければ
  `PASSED` で `required` = 掃引極値の向こう `sweepStopN`×N(既定 0.25、不利側 tick)。
- `msnr_gate._apply_sweep_gate`: `_apply_pool_clearance` の直後。OFF は出力が R103-1 と同一、
  SHADOW は候補と decision に `sweepGate` の監査と記録タグ(`SWEEP_GATE_WOULD_WAIT` /
  `SWEEP_GATE_WOULD_PASS`)を足すだけで SL・score・grade・decisionId は動かない。
  LIVE は PENDING / STALE の候補に hardBlockers `SWEEP_PENDING` / `SWEEP_STALE`(→ WATCH)、
  PASSED は SL を `required` に置換して `SWEEP_GATE_PASSED`(decisionId も変わる)。
- カード(公開状態・監査コピー)の decision にも compact な `sweepGate` を載せる。縮小時は
  `poolStop` と同じく evidence より先に落とす。

### 1.2 再生で分かったこと(重要)

**LIVE でも「掃引後に通った」周期は 1,260 本中 0。** 09-15 22:31 の例では、掃引(22:45)の後に
ノイズ床が 15 → 27pt へ広がって候補の SL が 29,444.50 → 29,473.25 に動き、その外側 1N 以内に次の
プール(New York High 29,484.25)が現れて再び PENDING になる。別の例ではアンカーが消費されて候補自体が
消える。つまりこのエンジンの候補(レベルの初回リテスト)は、掃引と奪還が終わる頃には別の幾何に
なっている。**「掃引後に同じセットアップで入る」は今の候補生成では起きない。**

実効は「プールが SL の外側 1N 以内にある候補を見送る」で、それだけでも逐次 +4.9R(損切り 14 → 7)だが、
POOL(SL をプールの向こうへ)の +7.5R に及ばず、BOTH は POOL 単独より低い。**掃引後の反転を取るには、
掃引そのものを起点にする候補(= TURTLE_SOUP の論理。R88 の SL 穴を直した版)が要る**。これは
R103-2 の変種 6 として Devin の再生に残す。本 PR では SHADOW で観測だけ続ける。

## 2. 指値取消の配線(`restingStopRecheck` LIVE)

R103-1 の `resting_stop_recheck` が stale(建値→SL が今のノイズ床 `minN`×N 未満。寄付き 30 分は寄付き後の
レンジ中央値との max)と判定し、契約が LIVE のとき、`autotrade_engine` は ENTRY_RESTING を保持している
周期に **R52 の `ENTRY_STALE_CANCEL` と同じ経路**で指値を取り消す:

1. 身元検査 `_resting_cancel_eligible`: 生きている親行が全部自分の注文 ID(凍結 routeSnapshot の
   ACCEPTED)であること。手動注文が同居 / ID 無しなら触らない(`cancel skipped (...)` の注記だけ)。
2. `--flatten --account <口座>` を dry-run → live の順で 1 回ずつ(dry モードは提案だけ)。
3. 再照会で FLAT + 注文非 blocking を確認できなければ `HALT`(action `RESTING_STOP_CANCEL`)。
4. 確認できたら `ENTRY_HALTED`(action `RESTING_STOP_CANCEL`、`recheck` に判定を残す)を記録し、
   **同じ周期**で startup recovery(不在証明 → `ENTRY_RECOVERED`)まで進めて claim と HALT を解く。

KILL 中・建玉あり・`NQX_RESTING_STOP_CANCEL=0` では取り消さない。SHADOW は従来どおり
`RESTING_STOP_STALE` を 1 行書くだけ。

## 3. 契約

```json
"sweepGate": {"mode": "SHADOW", "poolWithinN": 1.0, "sweepStopN": 0.25, "maxAgeBars": 3,
              "reclaimBars": 2, "kinds": [...5 種...], "models": ["BREAKER_CONTINUATION", "VP80_REVERSION"]}
```

`load_policy` は節ごとに独立して検証し、壊れていれば **この節だけ** OFF。`poolClearance` /
`restingStopRecheck` の LIVE 切替は `mode` の 1 値ずつ(SL が変わる `poolClearance` は
**FLAT・primary なし**のときに切り替える。R88 / R90 と同じ)。

## 4. 戻し方

- `stopLogic.sweepGate.mode` → `"OFF"`(監査ごと止まる。出力は R103-1 と同一)。
- `stopLogic.restingStopRecheck.mode` → `"SHADOW"`(記録だけに戻る)/ `"OFF"`。
- `stopLogic.poolClearance.mode` → `"SHADOW"`。
- 取消経路だけ止める: 環境変数 `NQX_RESTING_STOP_CANCEL=0`。

## 5. 検証コマンド

```powershell
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"
python tests/test_r103_sweep_gate.py
python tests/test_r103_liquidity_pools.py
python tests/test_r52_stale_entry_cancel.py
python replay_r103.py --limit 300      # 読むだけ(全件は --cache へ保存。数分)
```

`tests/test_r103_sweep_gate.py` は fixture の 4 件で、22:31 の候補が LIVE では `SWEEP_PENDING` で
WATCH になること(= 22:38 の損切りは建たない)、掃引→奪還を差し込んだときに SL が 29,456.75 へ置き換わり
decisionId が変わること、SHADOW が OFF とバイト一致すること、engine の取消が R52 と同じ順序で台帳に
残ることを固定する。台帳は tempdir、broker_status / nqx_state / autotrade_arm への到達は tripwire で 0 件。

## 6. 見つけたが今回は触っていないもの

- 掃引を起点にする候補(TURTLE_SOUP の再有効化 + R88 の掃引極値 SL)。R103-2 変種 6。
- 1 分足。掃引と奪還は 3 分足 1 本の中で起きるので、奪還の確認が最短でも 3 分遅れる。
- `sweepGate` が PENDING の間、同じ side の別候補(別モデル)が primary になれるかは `select_primary` の
  順位(等級 → score)次第で、WATCH 候補が上位に来ると新規が止まる。既存の hardBlockers と同じ性質。
