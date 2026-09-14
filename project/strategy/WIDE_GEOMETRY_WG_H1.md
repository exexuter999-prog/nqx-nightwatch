# WIDE GEOMETRY WG-H1 — 事前登録仕様

## 0. メタデータ

| 項目 | 固定値 |
|---|---|
| `policy_id` | `WG-H1` |
| `status` | `HEURISTIC / NOT PROVEN` |
| `qm` | `H` |
| 対象 | NQ/MNQの継続系シナリオ。実執行とOOS集計はMNQを既定とする |
| 適格レジーム | `RG-H1` の `TR` または `EX` のみ |
| 主目的 | 通常の揺らぎで早期退出しない構造的SLと、十分に遠い利確階層を、事前に固定された方法で比較検証する |
| 非目的 | 勝率、期待値、収益性、最適パラメータの主張 |
| 凍結日 | 2026-07-27 |

`WG-H1` は「広いSL/TPが優位である」と証明した戦略ではない。検証可能なリスク幾何を固定するための運用仮説である。下記定数を変更した時点で同じ試行ではなくなり、必ず新しい `setup_version` を付ける。

## 1. 仮説と反証条件

### 1.1 事前登録仮説

- `H0`: 同一のエントリー条件に対し、WG-H1は比較対象の既存ジオメトリより、コスト控除後のOOS平均損益を改善しない。
- `H1`: `TR/EX` に限定し、構造外かつボラティリティ正規化した広いSLを使うことで、通常ノイズによる早期SLを減らし、遠いmapped targetまでのMFEを捕捉できる。

`H1` は仮説であり、採用理由ではない。OOS昇格条件を満たさない限り `status=HEURISTIC / NOT PROVEN` を維持する。

### 1.2 永久的な失敗として残すもの

- OOSで `N < 30`
- `t < 2.0`
- 2.0pt往復摩擦控除後の平均または累積net pointsが非正
- OOSの各年平均が正となる暦年が2年未満
- パラメータ変更、事後的なregime変更、outcome定義変更

失敗後に定数を少しずつ動かして同じ名称で再試行してはならない。別仮説は別 `setup_version` として事前登録する。

## 2. 適格性ゲート

次の全条件を満たした場合だけWG-H1を `ARMED` にできる。

1. `regime_policy_version=RG-H1`
2. 凍結時点の `regime_code` が `TR` または `EX`
3. `M.rm` が次のいずれかを満たす
   - `mode=METRIC`: `policy=RG-H1`、`cmp>=20`、`rvr/rar/gar`、
     `rvp/rap/dep/ovp`、同一`session/clock/prior`、`bias=UP|DOWN`、
     `structure`、`stable>=2`を固定する。`TR`はさらに
     `dep>=70, ovp<=40, structure=TREND|BREAK_HOLD, htf=ALIGNED`、
     `EX`は`rvp>=80, rap>=80, dep>=60, structure=BREAK_HOLD`を満たす
   - `mode=SCREENSHOT`: `TR`だけに限定し、`sf`に`3M/15M/45M`、
     `aligned=YES, acceptance=CONFIRMED, bias=UP|DOWN,
     structure=TREND|BREAK_HOLD, stable>=2`を固定し、各scenarioは
     `S.ha=A`でなければならない
4. 同一のexecution timeframeで、確定済みの正確な `O.c` が時系列順に15本以上ある。使用する末尾15本の時刻は一意・厳密昇順・`M.tf` と完全一致の間隔で、最終足は `M.at` 時点の最新確定足でなければならない
5. named structural invalidation と、その外側に置けるprotective structural levelがexact価格として読める
6. 利益方向に、少なくとも3つのexactな順次mapped roadblockを事前に確定できる
7. `S.en/sl/tp/iv` と使用する `Z/TM` 価格が、装飾のない10進数（許可された箇所だけ `low..high`）として完全一致する。`~`、`≈`、ラベル、prefix/suffix、文章から数字だけを抽出して認可しない
8. entry、valid-until、event handling、outcome definition、risk capがentry前に凍結されている
9. LONG/SHORTの相互排他が明示されている

次の場合は `NO TRADE` とする。

- `BA/TX/EC/ER/MX`
- regimeが未確定、または事後的に推定された
- `M.rm` のRG-H1 evidenceが欠損または閾値不合格
- `O.c` が14本以下、未確定足を含む、時間順でない、価格が欠損する
- executable/invalidation/structure/target priceがcanonical exact tokenではない
- structure、invalidation、roadblockのいずれかを推測しなければ価格化できない
- 1 MNQでもrisk capを超える
- 3つの適格mapped targetを作れない

スクリーンショットfallbackで付いた `TR` はプロセス観察には残せるが、metric-modeのregime evidenceを欠く限りOOS昇格サンプルには数えない。`EX` はRG-H1上、正確なvolatility/range指標なしにスクリーンショットから付与してはならない。

### 2.1 追加のfail-closed監査

- primary official calendarを直近30分以内に再確認し、最寄りのmaterial
  eventを`E.rt`、確認時刻を`E.at`、一次参照を`E.ref`へ固定する。
  `M.at ±30分`にreleaseがあればNO TRADE。該当なしは
  `E.ev=NONE_WITHIN_30M|rt=N/A`だけを使う。
- `S.vu`が実イベントの`E.rt`を越える場合、`S.eh`には
  `FLAT/CLOSE/REDUCE`と、`E.rt`以前の正確なJST deadlineが必要である。
- `M.px`、全execution価格、使用する`Z/TM`境界は、最新のexact
  `O.c` closeの`0.5x..1.5x`以内でなければならない。これはedgeの
  パラメータではなく、decimal/contract scale取り違えを止める防御監査である。
- `G.ip`は現在の数値位置、NOWの明示的action、次の30分で観察する
  trigger/retest/acceptance/rejection、待機または行動の理由を含む。
- ローカル`EXECUTE`は`S.st=C`だけを許可する。cash risk capは
  `symbol/date/session`へ一度だけlockし、ACTIVE化時にentry/SL/TP/iv、
  MNQ数量、`R+8pt` stress、packet fingerprintをsnapshotとして凍結する。
  ACTIVE後は価格・side・exactnessを編集できず、SL拡大をUI経路から行えない。

## 3. Range Unit `U`

### 3.1 現在ボラティリティ

同一execution timeframeの確定足を `bar_i=(O_i,H_i,L_i,C_i)` とする。

```text
TR_i = max(
  H_i - L_i,
  abs(H_i - C_(i-1)),
  abs(L_i - C_(i-1))
)

U_live = SMA(TR の直近14個)
```

直近14個のTrue Rangeを得るため、最低15本の正確な `O.c` を必要とする。時刻は同日なら `HH:MM`、日付境界をまたぐなら `YYYY-MM-DD HH:MM JST` を使う。重複、逆順、`M.tf` と不一致の間隔、1本以上古い最終足、未確定足は認可しない。日付境界を連続取引中にまたぐ場合は直前closeを保持してgapをTrue Rangeに含めるが、休場・メンテナンス等で `M.tf` 間隔が途切れる窓は認可せず、再開後の連続15本が揃うまで待つ。

### 3.2 同時刻20セッション中央値

別途、同じ `session × clock_bucket × execution timeframe` について、完了済みの過去20セッションから同じ方法で算出した `U_live` がある場合：

```text
U_clock20 = median(過去20セッションの同時刻 U_live)
U = max(U_live, U_clock20)
```

20セッションを混ぜる際、NY_AM、NY_PM、London、Asiaを混合してはならない。clock bucketも固定する。

**現行HTMLは `U_clock20` を実装していない。** 現行HTMLで利用できるのは `U_live` のみであり、その場合は：

```text
U = U_live
clock_median_status = MISSING_NOT_IMPLEMENTED
```

と記録する。存在しない中央値を推定してはならない。将来 `U_clock20` を実装して `max` を使い始める場合、同じOOS試行に混ぜず `setup_version` を更新する。

## 4. Entry・Structure・Hard Stop

entry bandを `[E_low, E_high]` とし、全R計算に使う基準価格は：

```text
E = (E_low + E_high) / 2
```

protective structural levelを `P` とする。LONGでは `P < E_low`、SHORTでは `P > E_high` が必要である。

### 4.1 LONG

```text
SL_raw = min(P - 0.50U, E - 1.50U)
SL = SL_raw以下の最初の0.25pt tick
```

必須順序：

```text
SL < structural invalidation < E_low <= E
```

### 4.2 SHORT

```text
SL_raw = max(P + 0.50U, E + 1.50U)
SL = SL_raw以上の最初の0.25pt tick
```

必須順序：

```text
E <= E_high < structural invalidation < SL
```

したがってhard stopは必ず：

- named structureの外側へ最低 `0.50U`
- entry midpointから最低 `1.50U`

の両方を満たす。R:Rを良く見せるためにstopを構造内へ縮めてはならない。riskが大きすぎる場合は枚数を減らすか、`NO TRADE` にする。

## 5. Target Ladder

```text
R_points = abs(E - SL)
```

Rはentry midpointとhard stopから毎回再計算する。固定floor：

| Target | 最低距離 |
|---|---:|
| `TP1` | `1.80R` |
| `TP2` | `2.80R` |
| `TP3` | `4.00R` |

### 5.1 mapped roadblock選択

1. entry前に利益方向のroadblockを近い順に並べる。
2. LONGではentryより上、SHORTではentryより下だけを候補にする。
3. `TP1` は `1.80R` 以上にある最初のmapped roadblock。
4. `TP2` はTP1より先にあり、かつ `2.80R` 以上にある次のmapped roadblock。
5. `TP3` はTP2より先にあり、かつ `4.00R` 以上にある次のmapped roadblock。
6. mapped priceをfloor値へ丸めたり、存在しないlevelを作ったりしない。

floorより手前のroadblockはTPとして採用せず、`evidence_against` または `intervening_obstacle` として保存する。3つすべてをmapped levelで事前に作れない場合、WG-H1としては `NO TRADE` とする。

TP配分はWG-H1の幾何とは別の実行パラメータである。`TP1_ONLY`、`33/33/34` など異なる配分を同じOOS群に混ぜてはならず、配分を含む `setup_version` をentry前に固定する。

## 6. 建玉後の管理

### 6.1 Breakeven

- `MFE < +1.25R` の間、自動BE移動を禁止する。
- `MFE >= +1.25R` はBEの**適格化条件**にすぎず、WG-H1単体では自動BEを実行しない。
- +1.25R以後に自動BEまたはtrailを使う場合、そのトリガー、確定足、offsetを別途事前登録し、`setup_version` に含める。
- 裁量で結果を見てからBE条件を追加してはならない。

### 6.2 Stopの単調性

fill後のstopについて：

```text
LONG : SL_new >= SL_previous
SHORT: SL_new <= SL_previous
```

つまり建玉後にSLをentryから遠ざけることを禁止する。同値維持またはriskを減らす方向だけが許される。追加エントリーを理由に全体stopを遠ざけることも禁止する。

## 7. MNQへの縮小とRisk Cap

WG-H1のwide geometryを使う時は、NQの枚数を維持して金額riskを増やすのではなくMNQへ縮小する。CME仕様上、MNQは1 index point当たり `$2`、NQは `$20`、最小tickは両方 `0.25pt` である。

各セッション開始前に、変更不能な `risk_cap_cash` を固定する。stress摩擦を含む1 MNQ当たりの計画riskは：

```text
risk_per_1MNQ_stress = $2 × (R_points + 8.0pt)
qty_MNQ = floor(risk_cap_cash / risk_per_1MNQ_stress)
```

- `qty_MNQ >= 1` の場合だけ執行可能。
- `qty_MNQ < 1`、すなわち1 MNQでもrisk cap超過なら `NO TRADE`。
- wide stopをrisk capへ合わせるために狭めてはならない。
- slippage、gap、stop-orderの約定価格は保証されないため、この式は最大損失保証ではない。
- WG-H1のOOS群にNQ約定とMNQ約定を混ぜない。NQで検証する場合は別 `setup_version` とする。

現行HTMLの `SYSTEM CONFIG > WG SESSION RISK CAP` は、この式で8pt stress後の
MNQ上限枚数を表示する。cap未入力または1 MNQでも超過する場合、ローカル
`EXECUTE` はブロックされる。capは相場を見て都合よく広げず、各セッション開始前
に入力し、`symbol/date/session`へlockする。変更にはSYSTEM RESETによる新しい
risk sessionが必要で、既存ACTIVE snapshotの数量・stress riskを遡及変更しない。

## 8. 摩擦シナリオ

各filled occurrenceについて同じgross pointsから以下を必ず算出する。

| profile | 往復摩擦 | 用途 |
|---|---:|---|
| `BASE` | `2.0pt` | 主要OOS判定の最低摩擦 |
| `ADVERSE` | `4.0pt` | 感応度 |
| `STRESS` | `8.0pt` | wide risk sizingおよび耐性確認 |

```text
net_points_F = gross_points - F
F ∈ {2.0, 4.0, 8.0}
```

昇格判定は最低でも2.0pt往復摩擦控除後に行う。4.0pt、8.0ptの結果も同じ表に開示し、悪い結果を省略してはならない。実約定のledgerには実測commission/slippageをpoint換算して別途保存する。

## 9. MAE / MFEと不変ログ

測定窓は `fill_at` からterminal exit、hard stop、valid-untilのうち最初まで。execution timeframeのbar high/lowだけでなく、入手できる場合は正確なtick pathを優先し、データ源を固定する。

### 9.1 LONG

```text
MFE_points = max(high_after_fill - fill_price, 0)
MAE_points = max(fill_price - low_after_fill, 0)
```

### 9.2 SHORT

```text
MFE_points = max(fill_price - low_after_fill, 0)
MAE_points = max(high_after_fill - fill_price, 0)
```

派生値：

```text
MFE_R = MFE_points / R_points
MAE_R = MAE_points / R_points
```

最低保存項目：

```text
decision_id
data_vintage
symbol
session
setup_version
setup_family
direction
regime_code
regime_policy_version
regime_mode
regime_metrics
entry_price
structural_invalidation
hard_stop
targets
risk_cap_cash
stress_friction_points
stress_risk_per_mnq
planned_qty_mnq
valid_until_jst
filled
fill_at_jst
exit_price
exit_at_jst
gross_points
round_trip_cost_points
net_points
r_multiple
mfe_points
mae_points
sample_role
year
nqx_sha256
```

`DECISION` を結果観測前にappendし、後から `RESOLUTION` または `CANCEL` をappendする。既存行を編集しない。unfilled、canceled、expiredはNに含めない。

## 10. OOS昇格条件

集計単位を必ず次で固定する。

```text
setup_version × regime_code × session × direction
```

反対方向、別session、`TR` と `EX`、異なるTP配分、異なるBE/trail、MNQとNQをpoolしてはならない。同一の凍結OOS群が次をすべて満たした場合だけ、最大 `qm=E` への昇格候補とする。

1. 独立したfilled OOS occurrenceが `N >= 30`
2. 2.0pt往復摩擦控除後net pointsのone-sample t-statisticが `t >= 2.0`
3. 同net pointsの平均と累積がともに正
4. OOSの少なくとも2暦年で、各年の平均net pointsが正
5. `setup_version`、entry、SL、TP、validity、event rule、outcome definitionが全期間固定
6. metric-modeのregime evidence、正確なfill/cost、NQX hashが揃う
7. 事後的なregime relabel、parameter mutation、未解決のamendmentがない

4.0pt、8.0pt摩擦下の成績は昇格条件そのものではないが、robustnessとして必ず併記する。昇格しても確率校正を別途示さない限り `qm=C` にはしない。

## 11. 固定パラメータ要約

```yaml
policy_id: WG-H1
status: HEURISTIC_NOT_PROVEN
eligible_regime: [TR, EX]
range_unit:
  min_exact_completed_oc_bars: 15
  live: SMA14_TRUE_RANGE
  clock_baseline: MEDIAN_SAME_CLOCK_20_COMPLETED_SESSIONS
  final: MAX_LIVE_CLOCK20
  current_html_clock20: NOT_IMPLEMENTED
stop:
  outside_structure: 0.50U
  minimum_from_entry_midpoint: 1.50U
targets:
  TP1: MAPPED_ROADBLOCK_AT_OR_BEYOND_1.80R
  TP2: NEXT_MAPPED_ROADBLOCK_AT_OR_BEYOND_2.80R
  TP3: NEXT_MAPPED_ROADBLOCK_AT_OR_BEYOND_4.00R
breakeven:
  automatic_before: FORBIDDEN_BEFORE_1.25R
  policy_auto_execution: NONE
post_fill_stop_widening: FORBIDDEN
execution_default: MNQ
if_one_mnq_exceeds_risk_cap: NO_TRADE
friction_points_round_trip: [2.0, 4.0, 8.0]
promotion:
  oos_n_min: 30
  t_stat_min: 2.0
  positive_oos_years_min: 2
  setup_version_frozen: true
```

## 12. 一次資料 — 根拠と移植限界

以下は政策の方向性と検証方法を支える一次資料であり、`0.50U / 1.50U / 1.80R / 2.80R / 4.00R / 1.25R` を推定した資料ではない。これらの定数はWG-H1固有の事前登録ヒューリスティックである。

1. Mathias Mesfin, *Structural Limits of OHLCV-Based Intraday Signals in MNQ Futures: A Systematic Falsification Study*  
   https://arxiv.org/abs/2605.04004  
   2021–2025年の947 MNQ取引日、14 signal familyをwalk-forward、摩擦、`N>=30`、`t>=2`、複数年整合性で検証し、全条件を通過したsignalがなかったと報告する。WG-H1に厳格なOOS、コスト、バージョン凍結を要求する根拠。wide SL/TPのedgeを証明しない。

2. Kathryn Kaminski and Andrew W. Lo, *When Do Stop-Loss Rules Stop Losses?*  
   https://papers.ssrn.com/sol3/papers.cfm?abstract_id=968338  
   stop-lossが価値を加えるか減らすかはreturn dynamicsに依存する枠組みを示す。固定pt stopの普遍性を否定し、regime条件付きで検証する理由になる。MNQ intradayのWG-H1定数を直接検証した研究ではない。

3. Torben G. Andersen and Tim Bollerslev, *DM-Dollar Volatility: Intraday Activity Patterns, Macroeconomic Announcements, and Longer Run Dependencies*  
   https://www.nber.org/papers/w5783  
   intraday volatilityの周期性とannouncement効果を扱う。同時刻比較を導入する理論的動機になるが、対象はFXでありMNQへの直接的証明ではない。

4. Alan Moreira and Tyler Muir, *Volatility Managed Portfolios*  
   https://www.nber.org/papers/w22208  
   高volatility時にexposureを下げるvolatility managementを検証する。wide stopを狭める代わりに枚数を縮小する設計の一般的動機になるが、月次factor portfolio研究でありMNQ intradayへの直接移植はできない。

5. Scott Cederburg, Michael S. O'Doherty, Feifei Wang, and Xuemin Yan, *On the Performance of Volatility-Managed Portfolios*  
   https://doi.org/10.1016/j.jfineco.2020.04.015  
   volatility-managed portfolioの改善がreal-time OOSで一般化しない場合を報告する。volatility scaling自体をedgeとみなさず、WG-H1をOOSで反証する必要性を補強する。

6. CME Group, *Micro E-mini Nasdaq-100 Index Futures Contract Specs*  
   https://www.cmegroup.com/markets/equities/nasdaq/micro-e-mini-nasdaq-100.contractSpecs.html  
   MNQがNasdaq-100 Indexの `$2 × index`、最小変動 `0.25 index points` であることの一次仕様。cash risk換算とtick丸めだけに使用する。

7. CME Group, *E-mini Nasdaq-100 Futures Contract Specs*  
   https://www.cmegroup.com/markets/equities/nasdaq/e-mini-nasdaq-100.contractSpecs.html  
   NQが `$20 × index`、最小変動 `0.25 index points` であることの一次仕様。NQからMNQへ縮小する倍率確認だけに使用する。

8. Andrew W. Lo and Alexander Remorov, *Stop-loss Strategies with Serial
   Correlation, Regime Switching, and Transaction Costs*  
   https://doi.org/10.1016/j.finmar.2017.02.003  
   tight stopが取引コストにより劣後し得る一方、十分なreturn autocorrelationでは
   stopの価値が条件付きで変わることを示す。`TR/EX` と非trend regimeを混ぜず、
   「狭いほど安全」も「広いほど有利」も無条件には採用しない根拠。

9. Guido Baltussen, Zhi Da, Sander Lammers, and Martin Martens,
   *Hedging Demand and Market Intraday Momentum*  
   https://repub.eur.nl/pub/131621  
   1974–2020年、60超のequity/bond/commodity/currency futuresでmarket intraday
   momentumを報告する。継続系を研究対象にする動機だが、MNQの任意の時刻・
   entry・SL/TPへの直接的証明ではない。

10. Eduardo Rossi and Dean Fantazzini, *Long Memory and Periodicity in
    Intraday Volatility*  
    https://doi.org/10.1093/jjfinec/nbu006  
    hourly E-mini S&P 500 futuresでintraday periodicityとlong memoryを扱う。
    session/clock bucketを固定し、現在ボラと同時刻履歴を比較する動機になるが、
    WG-H1定数を推定した研究ではない。

11. Torben G. Andersen, Tim Bollerslev, Francis X. Diebold, and Paul Labys,
    *Modeling and Forecasting Realized Volatility*  
    https://www.nber.org/papers/w8160  
    高頻度intraday returnからrealized volatilityを構成する理論と実証を提示する。
    観測値からrange/volatilityを測る一般的根拠であり、14-period True Range SMAの
    最適性を証明しない。

12. Carol L. Osler, *Stop-Loss Orders and Price Cascades in Currency Markets*  
    https://www.newyorkfed.org/research/staff_reports/sr150.html  
    stop orderが集中した水準で急速かつ自己強化的なprice cascadeが発生し得る
    ことを高頻度FXで報告する。NQへの直接移植はできないが、hard stop価格を
    「保証された約定価格」と扱わずstress摩擦を保存する理由になる。

13. Nicholas Fett, Kevin McPhail, and Lihong Li, *Stop Orders in Select Futures
    Markets* (CFTC)  
    https://www.cftc.gov/node/248396  
    E-mini S&P 500、10-year Treasury、WTI futuresのstop orderを調べ、volatile
    dayでの執行増加と、まれでも大きくなり得るslippageを報告する。WG-H1の
    2/4/8pt摩擦開示と、stop priceを最大損失保証にしない根拠。

14. CME Group, *Proper Position Size*  
    https://www.cmegroup.com/education/courses/trade-and-risk-management/proper-position-size  
    stopをnormal movementで容易に触れる任意幅ではなく、見立てが誤りと判定できる
    logical levelへ置き、その後にcash riskからposition sizeを決める手順を示す。
    `structure → SL → MNQ size` の順序だけに使用し、1–3%などの例示値は採用しない。

15. Staffs of the CFTC and SEC, *Findings Regarding the Market Events of
    May 6, 2010*  
    https://www.sec.gov/about/reports-publications/newsstudies2010marketevents-reportpdf  
    E-miniの流動性急減とprice cascade、Stop Logic発動を再構成する。極端局面では
    平時のdepth/約定仮定が崩れるため、1 MNQのstress riskを超える口座では
    `NO TRADE` とする設計を補強する。

## 13. SNS / 実務家言説 — `ANECDOTAL`

以下は仮説発見のためのSNS観察であり、検証済みedgeではない。自己選択、survivorship bias、口座条件の差、約定データ欠損、成功例の選択報告を含む。固定pt値や投稿者の勝率主張はWG-H1に採用しない。

1. Reddit / r/FuturesTrading, “What are your best tips to not get stopped out on /nq?”  
   https://www.reddit.com/r/FuturesTrading/comments/1jtx63u/what_are_your_best_tips_to_not_get_stopped_out_on/  
   `ANECDOTAL`: structure基準、ATR基準、20–60pt以上という互いに異なる回答があり、一方で「stopを広げるならsize down」という反復テーマがある。相互矛盾自体が固定ptルールを採用しない理由。

2. Reddit / r/FuturesTrading, “How are tight stop losses useful?”  
   https://www.reddit.com/r/FuturesTrading/comments/1jr242a/how_are_tight_stop_losses_useful/  
   `ANECDOTAL`: NQの短時間の5–20pt変動とwide stopの体験談。entry precisionを重視する反論もあり、wideが優位との結論には使えない。

3. Reddit / r/FuturesTrading, “Stop Loss Placement - Scalping NQ”  
   https://www.reddit.com/r/FuturesTrading/comments/1cc29e4/stop_loss_placement_scalping_nq/  
   `ANECDOTAL`: bar structure、ATR、1RでのBEなど複数の個人ルールが提示される。WG-H1は早すぎる自動BEを検証対象として分離し、+1.25R以前を禁止するが、この閾値は投稿から推定したものではない。

4. Reddit / r/FuturesTrading, “How are you handling stops and/or trailing stops in this volatility?”  
   https://www.reddit.com/r/FuturesTrading/comments/1j90yp3/how_are_you_handling_stops_andor_trailing_stops/  
   `ANECDOTAL`: volatility上昇に応じてstop幅を変える実務家の問題意識。証拠としてではなく、`U` 正規化とMNQ縮小を検証する仮説源としてのみ扱う。

5. Reddit / r/FuturesTradingNQ, “Stop-loss placement in NQ is not about distance — it’s about structure.”  
   https://www.reddit.com/r/FuturesTradingNQ/comments/1rflmu4/stoploss_placement_in_nq_is_not_about/  
   `ANECDOTAL`: fixed distanceやATR単独ではなくstructural pivot外へ置くという主張。投稿単体に検証成績はなく、WG-H1のedge根拠にはしない。

6. Reddit / r/Daytrading, “Stop-loss suggestion for small 5 point take profits”  
   https://www.reddit.com/r/Daytrading/comments/135ay9c/stoploss_suggestion_for_small_5_point_take_profits/  
   `ANECDOTAL`: 非常に近いTPとSLを前提にした議論で、NQの広いgeometryとは目的が
   異なる。近いTPの高勝率感覚をWG-H1の遠いTPへ転用しないための反証側資料。

7. Reddit / r/Daytrading, “Risk management for scalping MNQ”  
   https://www.reddit.com/r/Daytrading/comments/1heszfp/risk_management_for_scalping_mnq/  
   `ANECDOTAL`: MNQ枚数、日次loss limit、stop幅の個人差が大きい。cash capと
   1-contract riskを分離して表示する要望の仮説源であり、口座risk率は採用しない。

8. Reddit / r/FuturesTrading, “NQ and MNQ daily profit target / loss limit”  
   https://www.reddit.com/r/FuturesTrading/comments/1b3nu2e/nq_and_mnq_daily_profit_target_loss_limit/  
   `ANECDOTAL`: 固定日次目標・損失上限の回答が口座や手法で大きく異なる。WG-H1は
   daily dollar targetをedge条件にせず、scenario geometryとsession cash capを分離する。

9. Reddit / r/FuturesTrading, “The correlation between ATR, risk and profits”  
   https://www.reddit.com/r/FuturesTrading/comments/1f0l7jo/the_correlation_between_atr_risk_and_profits/  
   `ANECDOTAL`: ATR正規化とsize調整の実務家議論。ATR倍率そのものを証拠にせず、
   `U` とcash sizeを別々にログする設計の仮説源として扱う。

10. Adam Grimes, *Trade Exits* / *Trailing Stops: 9 Ideas* / *Active Exits*  
    https://www.adamhgrimes.com/trade-exits/  
    https://www.adamhgrimes.com/trailing-stops-9-ideas-you-can-use-today/  
    https://www.adamhgrimes.com/reader-question-active-exits/  
    `PRACTITIONER / NOT PEER REVIEWED`: partial、runner、trailingなどexit設計の候補を
    列挙する。異なるexit配分を同じOOS群に混ぜず、別 `setup_version` にする理由。

11. Tom Hougaard, *All You Ever Need to Know About Stop-Loss Placement*  
    https://tradertom.com/wp-content/uploads/2021/03/ALL-YOU-EVER-NEED-TO-KNOW-ABOUT-STOP-LOSS-PLACEMENT.pdf  
    `PRACTITIONER / NOT PEER REVIEWED`: price structureを先に置く裁量的説明。
    testimonialsや収益主張は除外し、structure-firstという仮説だけを残す。

## 14. 解釈上の禁止事項

- wide stopを「安全」と呼ばない。1回の損失額は枚数管理なしでは増える。
- `TR/EX` をdirectional edgeや勝率に変換しない。
- ATR/True Rangeをsupport、resistance、invalidationそのものとして扱わない。
- SNSのpoint数、勝率、収益報告を検証済みparameterとして採用しない。
- in-sampleの最良値をOOS定数として後付けしない。
- 4pt/8pt摩擦で崩れる結果を隠さない。
- `status=HEURISTIC / NOT PROVEN` を、昇格条件が完了する前に変更しない。

本仕様は研究・教育目的の意思決定記録であり、利益または最大損失を保証しない。
