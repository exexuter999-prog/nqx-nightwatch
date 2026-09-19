# R119: 新規武装だけに掛かる 2 つの門(gapCap / targetPassed)

2026-09-19 ユーザー決定。正本は `execution_contract.json` の `limitGate`、実装は `msnr_gate`、
検証は `python tests/test_r119_limit_gate.py` と `python replay_limit_gate.py`(読むだけ)。

## 0. 何を入れたか

| 門 | 何を止めるか | 既定 |
|---|---|---|
| `gapCap` | **発注時に LIMIT になる**候補で `gapR = \|現値-建値\| / リスク幅` が `maxGapR` を超えるもの | LIVE / 1.5 |
| `targetPassed` | 現値が既に TP1 に到達・通過している候補(BUY は 現値 ≥ tp1、SELL は 現値 ≤ tp1) | LIVE |

どちらも **新規武装だけ**。`hardBlockers` に `LIMIT_GAP_EXCEEDED` / `TARGET_ALREADY_PASSED` を
付けて `state=WATCH` にする(記録は残る・別モデルは primary になれる)。`WATCH` は
`scenario.allowedStates`(ARMED/ACTIVE)に入らないので **claim 取得前**に止まる。
建値・SL・targets・等級・得点には触らないので `decisionId` は変わらない。

保留した 3 つ(2026-09-19 ユーザー決定): **gapCap の 1.0 への引き締め・一律成行化・
BREAKER 全停止。**

## 1. なぜ入れたか(穴の場所)

`msnr_gate._resting_limit_ok`(gapR ≤ `LIMIT_MAX_GAP_R` = 1.5)は前から在ったが、掛かって
いたのは **TURTLE の掃引チェーンと Silver Bullet だけ**(`resting` フラグ経路、
`msnr_gate.py` の `build_candidates` 内)。BREAKER は chain が完成しているので `resting=False`
で、この検査を通らない。なのに建値がレベル(現値の向こう)なので
`autotrade_engine._entry_order_type` が結局 LIMIT を出す。**距離無制限の指値がそこだけ通る。**

同じ量は `entry_gate_from_decision` の `reachR` として計算済みだったが、あれは Mini App の
表示専用で何もゲートしていない。

`targetPassed` の側は、`_geometry_blockers` の `TARGET_HEADROOM_INSUFFICIENT` が R を
**建値から**測るため、現値がどこにあっても健全な R に見える。払えない幾何で経路を
凍結するだけの無駄が実際に起きていた(§3)。

## 2. 実装の要点

* **注文種別の判定は `autotrade_engine._entry_order_type` と同じ式**
  (`msnr_gate.limit_entry_side`)。指値専用の向き判定を全候補へ当てると、成行になるはずの
  候補を「指値として成立しない」と誤って落とす。試験は 32 通りで engine と突き合わせる。
* `gapCap` は **LIMIT になる候補だけ**。`targetPassed` は注文種別で分けない(指値でも成行でも
  払えない幾何)。
* **鮮度確認済みの現値でだけ判定する**(`msnr_gate.gate_price`)。`bundle.price` と
  `priceAt` があり、`bundle.at` との差が `scenario.maxAgeSec`(600 秒)以内のときだけ。
  確定足の終値へは落ちない —— 終値は「今の値」ではないので、TP1 通過の判定に使うと
  過去の足で新規を止めたり通したりする(§4 の実測で 32pt ずれた)。確認できない周期は
  門を掛けず `limitGate.reason` に理由を残す(相場データの古さは pipeline が先に BLOCK する)。
* `mode` は OFF / SHADOW(記録のみ)/ LIVE。**戻しは mode を OFF にする 1 語**で、
  `maxGapR` / `models` を消さなくてよい。
* 保有建玉の管理(凍結プランの MODIFY / FLATTEN / 追撃)はこの節を**一切読まない**。
  `autotrade_engine.py` と `order.py` に門の名前が出てこないことを試験が固定する。

## 3. 実測(2 つの標本。どちらも「この標本内の結果」)

### 3.1 台帳(実際に送った指値)

`python replay_limit_gate.py --ledger`。2026-08-25 → 09-19 のシグナル 77 件、うち指値 53 件
(同じ `(model, side, entry, tp1)` が 90 分以内なら 1 件へ畳む)。

BREAKER の指値 19 件の `gapR`:

| 結末 | n | gapR median | 範囲 | 現値→TP1 の R median |
|---|---|---|---|---|
| 約定 | 9 | 0.37 | 0.10–1.42 | +1.85 |
| 取りこぼし(TP1 先着) | 6 | 0.95 | 0.17–2.37 | +1.04(0 以下 1 件) |
| 取消 | 2 | 0.30 | 0.23–0.36 | +1.31 |
| 指値保持中 | 2 | 0.57 | — | +0.97 |

比較: VP80 の指値は建値が直近終値なので `gapR` median **0.00**(約定 10/12)。BREAKER が
外すのは遠い指値だけ、という構造はここで見える。

`gapCap` を掛けたときに残る件数(**この標本内**):

| cap | 残す約定 | 残す取りこぼし |
|---|---|---|
| 2.0 | 9/9 | 5/6 |
| **1.5(採用)** | **9/9** | **4/6** |
| 1.25 | 8/9 | 4/6 |
| 1.0(保留) | 8/9 | 3/6 |
| 0.75 | 7/9 | 2/6 |

**「約定を失わない」はこの標本内の結果である。** 母数は 19 件、うち武装周期の現値が
監査バンドルに残っていたのは半分で、残りは直前確定足の終値の代用値(代用は gapR を
**過小に**出す)。将来にわたって約定を失わないという主張ではない。

### 3.2 監査バンドルの再生(現行コードで再評価)

`python replay_limit_gate.py --replay`。1358 バンドル(2026-08-15 → 09-18)。規則は
`replay_r103.py` / `replay_stop_logic.py` と同一 —— セットアップの束ね(`setups_for`)、
約定判定(`entry_depth.simulate`: 確定 3 分足・指値は 1tick 突き抜け・約定前 TP1 で取消・
同じ足は SL 優先・指値待ち 30 分・6 時間)、R90 穴 2 の成行ゲート 1.0N、R は計画の SL 幅。
差し替えるのは `limit_gate_policy` だけ。

周期単位:

| 変種 | `LIMIT_GAP_EXCEEDED` | `TARGET_ALREADY_PASSED` | ARMED→WATCH |
|---|---|---|---|
| GAP15 | 20 | 0 | 1 |
| PASSED | 0 | 9 | 0 |
| BOTH | 20 | 9 | 1 |
| GAP10(保留) | 39 | 9 | 8 |

セットアップ単位(71 件)と**逐次**(同時に 1 建玉だけ = 経路が 1 本しかない実運用の姿):

| 変種 | setup ΣR | 残る約定 | 除外 | 逐次 取った | 逐次 約定 | 逐次 ΣR |
|---|---|---|---|---|---|---|
| BASE | +8.97 | 47 | 0 | 36 | 20 | **+4.80** |
| GAP15 | −0.42 | 46 | 1 | 36 | 20 | **+4.80** |
| PASSED | +8.97 | 47 | 0 | 36 | 20 | **+4.80** |
| BOTH | −0.42 | 46 | 1 | 36 | 20 | **+4.80** |
| GAP10(保留) | −2.38 | 45 | 6 | 32 | 20 | **+4.80** |

**結論は逐次で語る**(R90 の「セットアップ単位は 4 倍数える罠」)。逐次では
**BASE / GAP15 / PASSED / BOTH は完全に同一**(取った 36・約定 20・ΣR +4.80)。
GAP10 は「取った」が 36 → 32 に減るだけで約定も ΣR も同じ。

### 3.3 セットアップ単位の −9.39R の正体

GAP15 が外した 1 件は 09-15 22:37 JST の BREAKER SELL、**建値 29,447.00**(SL 29,462.50 /
現値 29,406.75 / `gapR` 2.60)で、再生では FILLED **+9.39R**。

**これは本番が取った建玉ではない。** 同じ 22:31 の周期を本番の `stopLogic`(`poolClearance`
LIVE)で評価すると候補は 2 本出て、門の当たり方が別れる:

| 候補 | 建値 | `gapR` | 現値→TP1 の R | 門 | 本番 |
|---|---|---|---|---|---|
| 実トレード | 29,426.00 | **1.49** | +0.85 | **通る** | 取って **−1.041R($-231)** |
| もう 1 本 | 29,447.00 | 3.76 | **−0.63** | `LIMIT_GAP_EXCEEDED` + `TARGET_ALREADY_PASSED` | 取っていない |

本番が取ったのは建値 29,426.00 の側で、`resultId rs_97202f900c6c05a3bf75`(A+ SHORT /
SL 29,444.50 / 6 枚 / `huntClass STOP_HUNT` / MAE 26.75pt = 1.45R / 保有 6.4 分。TP1 は決済の
24 分後に到達)。**`gapR` 1.49 なので門は通す** —— 1.5 の門が実弾の勝ち負けを書き換えた事例は
この標本には無い。

外れるもう 1 本は現値が既に TP1 を **0.63R 分**通過している候補で、`targetPassed` にも同時に
当たる。再生で +9.39R に見えるのは、`entry_depth.simulate` が「価格が建値まで戻ってから
最終 TP へ行く」経路を数えているため。逐次ではこのセットアップは取られない(経路が埋まっている)。

### 3.4 成行化の counterfactual(参考。一律成行化は保留)

BREAKER の指値 19 件を「武装足で成行、SL / TP は凍結プランのまま」で逐次再生した。
SL 上限の条件は**分けて**出す —— 上限の出所が 3 つあり、1 つの数字にまとめられない。

| 上限の出所 | 発注不可(RISK_CAP) | FINAL | TP1→SL | TP1→未決 | SL 先着 |
|---|---|---|---|---|---|
| `plan`(各プランの `riskCapPoints`。ULTRA は残 DD 由来。**実際に効いた上限**) | **0** | 7 | 2 | 4 | 6 |
| 60pt(`msnr_gate.sl_cap_pt()`) | 3 | 5 | 2 | 3 | 6 |
| 50pt($200 ÷ $2 ÷ 2 枚。`TRADING_CONTEXT.md` の通常経路) | 5 | 4 | 1 | 3 | 6 |

**前回の報告(「6 件が 60pt 上限で発注不可 / 発注可能は 13 件」)は誤りだった。** 内訳の
合計が 14 で母数と合っておらず、さらに ULTRA のプランに 60pt を当てていた。実際に効いた
上限(`plan`)では **19 件すべて発注可能**で、取りこぼし 6 件のうち 4 件は成行なら FINAL まで
届いていた。一方で今約定している 9 件のうち 4 件は成行だと SL 先着に変わる。
どちらに転ぶかは上限の条件で変わるので、一律成行化はこの標本では決められない。

### 3.5 経路の占有時間(台帳の実測)

| モデル | 結末 | n | 合計 | 中央 | 最長 |
|---|---|---|---|---|---|
| BREAKER | 取消 | 2 | 121 分 | 60 分 | **115 分** |
| BREAKER | 約定 | 7 | 116 分 | 9 分 | 53 分 |
| BREAKER | 取りこぼし | 6 | 73 分 | 8 分 | 25 分 |
| VP80 | 約定 | 9 | 52 分 | 4 分 | 14 分 |

取りこぼしは R52 の自動取消で中央 8 分で解ける。外れ値は 2026-09-19 01:50 の 115 分で、
これは別件(`docs/R120_STALE_ENTRY_OWNERSHIP.md`、原因未確定)。

## 4. 測り方の落とし穴(記録)

武装周期の現値は **監査バンドルの `price`** を使う。直前確定足の終値は代用にならない ——
2026-09-15 13:31:35 の実価格は 29,388.75 だったが、直前の確定足終値は 29,420.75 で、
同じ候補の `gapR` が **2.01 → 0.28** になった。価格が足の中で走る局面(まさに遠い指値が
できる局面)で代用値は必ず過小に出る。`replay_limit_gate.py --ledger` は実価格が残って
いる周期はそれを使い、代用に落ちた件数を毎回出力する。

## 5. 切り替えと戻し

* 切り替えは **FLAT かつ未約定注文なしのとき**。`python broker_status.py --accounts` →
  各口座 `--json` で `verified=true, qty=0`、`python order.py --status`、
  `python autotrade_engine.py --list-halts` を確認する。
* 戻しは `execution_contract.json` の各節の `mode` を `OFF` にする 1 語。Worker のデプロイは
  不要(Python 側だけで完結)。
* 書き換えたら `python -c "import msnr_gate,json; print(json.dumps(msnr_gate.limit_gate_policy(),ensure_ascii=False))"`
  で `invalid` が空であることを確かめる(不正な節は黙って OFF になる)。
* `python tests/test_r119_limit_gate.py` と `python tests/run_all.py`。

## 6. この節の影響を受けた既存試験

`tests/test_r103_sweep_gate.py` の T4 は `gapCap` 1.5 に当たる。**当たるのはあの試験自身が
`poolClearance` を OFF に固定しているから** —— OFF だと SL が 29,444.50 のままで
リスク幅 18.5pt、`gapR` は 2.01 になる。本番設定(`poolClearance` LIVE)では SL が外へ逃げて
リスク幅が広がり、同じ候補の `gapR` は **1.49** で門を通る(§3.3)。あの試験は sweepGate だけを
見るので、`stop_logic_policy` と同じように `limit_gate_policy` も OFF に差し替えて切り離した。

他の 125 ファイルは影響なし(門 LIVE と門 OFF で失敗集合が一致。残る 1 件
`test_r118_gateway_integration.py` は R119 以前からの失敗で、門の設定で変わらない)。

**教訓として残す**: `gapR` はリスク幅で割るので、**SL を動かす節(`stopLogic` の各門)の設定が
違うと同じ候補の `gapR` が変わる**。門の当たり外れを語るときは、どの `stopLogic` 設定で
評価した値かを必ず添える。

## 7. 市場再開後に確認するログ項目

土曜(2026-09-19)は監視ループを起動していない。**反映は済んでいるが稼働確認は未了。**
再開後の最初の窓で、監視セッション側が次を見る。

### 7.1 まず 1 回(発注を伴わない)

```powershell
python verify_r119_live.py      # 起動先・契約・門の効きを再確認(通信なし)
```

終了コード 0 で `gapCap = LIVE / maxGapR = 1.5` と `targetPassed = LIVE`、`invalid` が空。
**`.claude/worktrees/` の複製から起動すると門は OFF になる**(あちらに R119 は入っていない)。
起動は必ず `C:\Users\exexu\Downloads\nq-nightwatch-claude-code-handoff` から。

### 7.2 周期のログで見るもの

| 見る場所 | 期待 | 異常のとき |
|---|---|---|
| `.secrets/monitor_cycle_HHMM.json` の各候補の `limitGate` | `orderType` / `gapR` / `tp1R` が毎候補に入る | 全部 `None` なら `gate_price` が現値を採れていない → `reason` を読む(`NO_PRICE` / `NO_PRICE_AT` / `PRICE_STALE_*`) |
| 同 `reason` | `null`(= 判定した) | `PRICE_STALE_*` が続くなら `priceAt` と `at` の差、すなわち取得の遅れ |
| `decision.hardBlockers` | 当たった周期に `LIMIT_GAP_EXCEEDED` / `TARGET_ALREADY_PASSED` | 頻発するなら §3 の想定(20 / 9 周期 / 1358 バンドル)より多い = 設定か入力を疑う |
| 報告行の `demoted:` | 門で落ちた周期はここに理由が出る | 出ないのに WATCH なら別のゲート |
| `.secrets/autotrade_ledger.jsonl` の `ENTRY_CLAIMED` | **`LIMIT_GAP_EXCEEDED` / `TARGET_ALREADY_PASSED` を持つ決定の claim が 1 件も無いこと** | 有れば claim 取得前に止まっていない = 配線の穴 |
| `decisionId` | 門の有無で変わらない | 変わっていたら SL か建値を触っている = 実装の退行 |

確認用の 1 行(読むだけ):

```powershell
python -c "import json,glob,io; [print(f, [ (c.get('model'), (c.get('limitGate') or {}).get('gapR'), c.get('hardBlockers')) for c in (json.load(io.open(f,encoding='utf-8')).get('candidates') or [])]) for f in sorted(glob.glob('.secrets/monitor_cycle_*.json'))[-3:]]"
```

### 7.3 1 窓ぶん溜まってから

```powershell
python replay_limit_gate.py --ledger                 # 実指値の gapR 分布と経路占有
python replay_limit_gate.py --replay --reevaluate    # 変種比較(逐次で読む)
```

`--ledger` の出力末尾の「現値の出所」で、代用値(直前確定足の終値)に落ちた件数を必ず見る。
代用は `gapR` を過小に出すので、その分の数字は弱い(§4)。

### 7.4 戻すとき

`execution_contract.json` の `limitGate` の各節の `mode` を `OFF` にする 1 語。**FLAT かつ
未約定注文なしのとき**に。Worker のデプロイは不要。
