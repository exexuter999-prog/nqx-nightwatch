# R122 選択層 — 「既存ゲートを全部通った候補から最有力を選ぶ」

2026-09-20 JST。R122 の 5 段のうち **`structureContext.selection` だけ**を対象にした検証。
他の段(`context` / `participation` / `shallowCandidate` / `nearTerm`)は両条件で同じに固定し、
浅い候補の追加・短期予測・新しい加点は一切混ぜていない。

再現: `python replay_selection.py --audit / --blockers / --compare`(すべて読むだけ)。

---

## 1. 何が起きていたか

`msnr_gate.select_primary` は候補を **(等級, 点数, TP1 の R, モデル順位)** だけで並べ、
先頭を primary にする。**`allowed`(= 既存ゲートを全部通ったか)を見ていない。**
そのため、候補固有の理由で WATCH になっている候補が高得点だと、同じ周期に武装できる
候補があっても周期ごと WATCH になる。

監査バンドル **1,358 周期**(2026-08-15 → 09-18 UTC、本番契約そのまま・15 分足なし)で:

* 候補があった周期 407、うち primary が WATCH だった周期 310
* そのうち **15 周期**で、同じ周期に `allowed` な候補が存在した
* 同じセットアップの連続を 1 件にまとめると **14 件**

### 1.1 隠れていた 14 件(全件)

| # | 時刻(UTC) | 周期 | 現 primary(等級/点 — ブロッカー) | 代替候補(等級/点) | 向き | 代替の E / SL / TP1 |
|---|---|---|---|---|---|---|
| 1 | 09-08 16:42 | 1 | OTE FVG PULLBACK SELL B/6 — ANCHOR_CONSUMED | BREAKER CONTINUATION SELL B/4 | 同方向 | 29588.5 / 29627.0 / 29513.75 |
| 2 | 09-08 16:45 | 1 | OTE FVG PULLBACK SELL B/6 — ANCHOR_CONSUMED | BREAKER CONTINUATION SELL B/6 | 同方向 | 29588.5 / 29626.5 / 29513.75 |
| 3 | 09-08 22:30 | 1 | BREAKER CONTINUATION SELL B/5 — TARGET_HEADROOM_INSUFFICIENT, ANCHOR_CONSUMED | VP80 REVERSION BUY B/4 | **反転** | 29521.5 / 29492.5 / 29605.5 |
| 4 | 09-08 22:33 | 1 | BREAKER CONTINUATION SELL B/5 — TARGET_HEADROOM_INSUFFICIENT, ANCHOR_CONSUMED | VP80 REVERSION BUY B/4 | **反転** | 29518.75 / 29492.5 / 29605.5 |
| 5 | 09-09 13:24 | 1 | OTE FVG PULLBACK BUY A+/9 — ANCHOR_CONSUMED | VP80 REVERSION SELL A/7 | **反転** | 29412.5 / 29431.25 / 29382.25 |
| 6 | 09-09 16:13 | 1 | VP80 REVERSION BUY A/7 — RISK_CAP_EXCEEDED | TURTLE SOUP REVERSAL SELL B/3 | **反転** | 29452.5 / 29485.25 / 29365.0 |
| 7 | 09-10 18:27 | 1 | BREAKER CONTINUATION SELL B/6 — TARGET_HEADROOM_INSUFFICIENT, ANCHOR_CONSUMED | VP80 REVERSION BUY B/5 | **反転** | 29154.75 / 29130.5 / 29214.75 |
| 8 | 09-16 18:16 | 1 | VP80 REVERSION SELL A/8 — RISK_CAP_EXCEEDED | BREAKER CONTINUATION BUY B/2 | **反転** | 29432.0 / 29398.0 / 29489.5 |
| 9 | 09-18 13:38 | 1 | TURTLE SOUP REVERSAL BUY A/8 — ANCHOR_CONSUMED | VP80 REVERSION SELL B/5 | **反転** | 29815.25 / 29863.0 / 29731.5 |
| 10 | 09-18 15:16 | 1 | BREAKER CONTINUATION BUY B/5 — ANCHOR_CONSUMED | TURTLE SOUP REVERSAL SELL B/2 | **反転** | 29763.25 / 29789.5 / 29723.5 |
| 11 | 09-18 15:22 | 1 | BREAKER CONTINUATION BUY B/6 — ANCHOR_CONSUMED, NO_CONFIRMATION | TURTLE SOUP REVERSAL SELL B/3 | **反転** | 29763.25 / 29785.75 / 29723.5 |
| 12 | 09-18 15:26–15:29 | 2 | BREAKER CONTINUATION BUY A/7 — ANCHOR_CONSUMED | TURTLE SOUP REVERSAL SELL B/2 | **反転** | 29763.25 / 29785.0 / 29723.5 |
| 13 | 09-18 15:32 | 1 | TURTLE SOUP REVERSAL BUY B/6 — ANCHOR_CONSUMED | TURTLE SOUP REVERSAL SELL B/4 | **反転** | 29763.25 / 29784.75 / 29728.75 |
| 14 | 09-18 17:46 | 1 | BREAKER CONTINUATION BUY A/7 — ANCHOR_CONSUMED, TARGET_ALREADY_PASSED, LIMIT_GAP_EXCEEDED | VP80 REVERSION SELL B/4 | **反転** | 29761.5 / 29772.25 / 29732.25 |

**選ばれなかった理由は全件同じ**: 現行の並べ替えが等級と点数だけを見るので、WATCH の側が
先頭に来る。代替候補の側には落ち度が無い(`hardBlockers` はゼロ)。
`python replay_selection.py --audit` が各件の全候補・全ブロッカー・E/SL/TP を出す。

---

## 2. WATCH 理由の分類

| 種別 | 意味 | 扱い |
|---|---|---|
| **GLOBAL** | 市場全体・口座全体の停止(取得受領書、ボラ床、High イベント窓、限月満期、手動 HALT、AUTO、建玉照会 UNVERIFIED) | **代替候補で迂回してはいけない** |
| **CANDIDATE** | その候補固有の不成立(`ANCHOR_CONSUMED` / `NO_CONFIRMATION` / `RISK_CAP_EXCEEDED` / `RISK_BELOW_NOISE` / `TARGET_HEADROOM_INSUFFICIENT` / `TARGET_ALREADY_PASSED` / `GEOMETRY_INVALID` / `LIMIT_GAP_EXCEEDED` / `SWEEP_GATE_*` / `PARENT_THESIS_INVALIDATED`) | 他の適格候補を止める理由にならない |
| **POLICY** | 契約で外したモデル/型(`MODEL_DISABLED` / `RESTING_LIMIT_DISABLED` / `SHALLOW_CANDIDATE_SHADOW`) | R89/R122 の設計どおり別モデルが primary になってよい。選択層に来る前に候補集合から外れている |

### 2.1 GLOBAL は選択層では迂回できない(構造上の理由)

* ボラ床・High イベント窓・限月満期は `monitor_publish.publish_state` が **primary を
  選んだ後の `chosen`** に当てる。`publish_state` は `select_primary` を呼ばない。
* 取得受領書のゲートは `enrich_decisive_strategy` が `msnr_gate.evaluate` の**後**に
  `decision` へ当てる。
* 手動 HALT / AUTO / 建玉照会 / 限月不一致は `nqx_cycle` と `autotrade_engine` が
  **周期そのもの**を止める。

つまり「どの候補を primary にするか」は GLOBAL ゲートより**上流**なので、並べ替えを
変えても GLOBAL は同じように掛かる。`tests/test_r122_structure_context.py::
test_selection_cannot_bypass_global_gates` が、ボラ床のゲートが**モデルを見ずに**
ARMED を WATCH へ落とすことと、上の位置関係をコードで検査する。

### 2.2 実測: 隠していた理由は全件 CANDIDATE

隠れていた 15 周期の primary に付いていたブロッカー:

| ブロッカー | 件数 | 種別 |
|---|---|---|
| `ANCHOR_CONSUMED` | 13 | CANDIDATE |
| `TARGET_HEADROOM_INSUFFICIENT` | 3 | CANDIDATE |
| `RISK_CAP_EXCEEDED` | 2 | CANDIDATE |
| `NO_CONFIRMATION` | 1 | CANDIDATE |
| `TARGET_ALREADY_PASSED` | 1 | CANDIDATE |
| `LIMIT_GAP_EXCEEDED` | 1 | CANDIDATE |

**GLOBAL を含む周期は 0 / 15。** つまり今回の変更は「全体停止の迂回」ではなく、
**候補固有の不成立が他の適格候補まで止めていたのを止める**だけ。
止められていた側の内訳: TURTLE SELL 6 / VP80 BUY 3 / BREAKER SELL 2 / VP80 SELL 2 /
VP80 SELL(A)1 / BREAKER BUY 1。**うち 13/15 は建てる方向が反転する。**

---

## 3. 選択層だけの比較(費用込み)

`python replay_selection.py --compare`。違いは `selection` の 1 語だけで、
`shallowCandidate` は両条件 SHADOW、`nearTerm` も両条件 SHADOW、新しい加点なし。
**比較の間、契約ファイルは書き換えていない**(プロセス内で policy を差し替える)。

費用: `FEE_PER_SIDE` 中央値 **$0.50 / 枚 / 片道**(MNQ 1pt = $2.00 → 往復 **0.50pt 相当**)。
滑りは片道あたりの tick を明示的に足す。逐次 1 建玉制約・`entry_depth.simulate` の約定規則・
R = 計画 SL 幅、は両条件で同一。

| 滑り / 片道 | 条件 | 取った | 約定 | TP1 | 損切 | 見送り | **ΣR** | 最大DD | 最大勝ち |
|---|---|---|---|---|---|---|---|---|---|
| 0 tick(手数料のみ) | S0 現行 | 37 | 21 | 9 | 10 | 44 | **+7.29** | −4.07 | +4.18 |
| 0 tick | S1 selection LIVE | 44 | 24 | 10 | 11 | 50 | **+9.42** | −4.07 | +4.18 |
| | **差** | | | | | | **+2.13R** | 同じ | |
| 1 tick | S0 現行 | 37 | 21 | 9 | 10 | 44 | **+6.87** | −4.18 | +4.17 |
| 1 tick | S1 selection LIVE | 44 | 24 | 10 | 11 | 50 | **+8.94** | −4.18 | +4.17 |
| | **差** | | | | | | **+2.06R** | 同じ | |
| 2 tick | S0 現行 | 37 | 21 | 9 | 10 | 44 | **+6.45** | −4.29 | +4.17 |
| 2 tick | S1 selection LIVE | 44 | 24 | 10 | 11 | 50 | **+8.45** | −4.29 | +4.17 |
| | **差** | | | | | | **+2.00R** | 同じ | |

* 周期単位: **primary が変わった 15 周期、全部が WATCH → ARMED**。逆(ARMED → WATCH)は 0。
* **建てる方向が変わるのは 13 / 15 周期。**
* 最大ドローダウンは**どの費用条件でも変わらない**(−4.07 / −4.18 / −4.29R)。
* セットアップ単位(96 件、1 tick 込み): S0 +0.17R → S1 +3.81R、**+0.038R/setup、
  90% 区間 [−0.05, +0.15]、P(improve)=0.73**。→ **統計的には未確定。**

### 3.1 増えたトレード / 減ったトレード(1 tick 込み)

**S1 でだけ取れた 6 件 ΣR = +1.19R(勝ち 3 / 負け 3)**

| 時刻(UTC) | モデル | 向き | E | SL | risk | r |
|---|---|---|---|---|---|---|
| 09-08 22:30 | VP80_REVERSION | BUY | 29521.5 | 29492.5 | 29.00pt | **+2.62** |
| 09-09 13:24 | VP80_REVERSION | SELL | 29412.5 | 29431.25 | 18.75pt | −1.01 |
| 09-09 13:32 | OTE_FVG_PULLBACK | BUY | 29444.25 | 29409.5 | 34.75pt | +1.15 |
| 09-09 16:13 | TURTLE_SOUP_REVERSAL | SELL | 29452.5 | 29485.25 | 32.75pt | +0.56 |
| 09-10 18:27 | VP80_REVERSION | BUY | 29154.75 | 29130.5 | 24.25pt | −1.04 |
| 09-18 17:46 | VP80_REVERSION | SELL | 29761.5 | 29772.25 | 10.75pt | −1.09 |

**取れなくなった 3 件 ΣR = −0.87R**(いずれも同じセットアップを 1〜2 周期あとに
取り直していたもの。占有がずれた結果で、機会が消えたわけではない)

| 時刻(UTC) | モデル | 向き | r |
|---|---|---|---|
| 09-09 13:24 | OTE_FVG_PULLBACK | BUY | +1.15(S1 では 13:32 に同じ幾何で取れている) |
| 09-10 18:30 | VP80_REVERSION | BUY | −0.94(S1 では 18:27 に取れている) |
| 09-18 17:50 | VP80_REVERSION | SELL | −1.08(S1 では 17:46 に取れている) |

### 3.2 この比較に入っていないもの

約定行列・部分約定・イベント時のギャップ・複数口座の同時送信。標本はこの 34 日ぶんだけで、
34 日 × 1,358 周期に対して**変わるのは 15 周期**しかない。セットアップ単位の区間が 0 を
跨いでいるのは、この標本数では当然のこと。

---

## 4. 本番採用

**2026-09-20: `structureContext.selection` を LIVE にした。**

採用の理由は「勝てるから」ではなく **`select_primary` の欠陥を直すから**:

1. 選べるのは **`hardBlockers` がゼロの候補だけ**(`allowed`)。既存のどのゲートも緩めない。
   `test_selection_only_picks_fully_qualified_candidates` が、武装できる候補が無ければ
   並びが一切変わらないことを検査する。
2. **GLOBAL の停止は迂回できない**(§2.1、コード位置とボラ床ゲートの実行で確認)。
3. 費用込みで**どの滑り条件でも改善**(+2.13 / +2.06 / +2.00R)、**最大 DD は不変**。
4. 変わるのは 1,358 周期中 **15 周期だけ**(34 日で +7 トレード = 1 日あたり +0.2)。

**受け入れていないこと**: セットアップ単位の優位は未確定(P(improve)=0.73、区間が 0 を
跨ぐ)。「勝率が上がる」とは言えない。**13/15 で建てる方向が反転する**のが最大の変化で、
これは「高得点だが成立していない読み」を捨てて「成立している読み」を採る結果。

**戻し方**: `execution_contract.json` の `structureContext.selection.mode` を `"OFF"` に
する 1 語。OFF で並べ替えは R122 以前と完全に同じになる(`test_selection_stage`)。

**再開後に見るログ**: `decision.structure.selectionReason` が
`ELIGIBLE_PREFERRED_OVER_HIGHER_SCORE_WATCH` になっている周期が、この変更で
primary が入れ替わった周期。`changedFromBaseline` にも差分が残る。
