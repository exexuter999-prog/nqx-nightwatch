# R121: ICT Standard Deviation Projections(TTrades 版)の接続

2026-09-19。依頼は `docs/CLAUDE_ICT_STDV_INTEGRATION_HANDOFF_2026-09-19.md`、出典調査は
`docs/reports/ICT_STDV_SOURCE_AND_CODE_AUDIT_2026-09-19.md`。正本は
`execution_contract.json` の `ictStdv`、実装は `ict_stdv.py` + `msnr_gate`、検証は
`python tests/test_r121_ict_stdv.py` / `python replay_ict_stdv.py --replay` /
`python verify_r119_live.py`(すべて読むだけ)。

**この文書は「実装が完成した」ことと「損益が改善した」ことを分けて書く。** §6 が実装、
§7 が測定、§9 が未達。導入時の既定は **mode=SHADOW / targets=OFF / participation=OFF**
= 記録だけで注文判断を一切変えない。

## 1. 採用仕様表(出典の規則 / Nightwatch 用に決めた規則)

| 項目 | 出典(V1/V2/P1/P2)の規則 | Nightwatch v1 で決めた規則 | コード |
|---|---|---|---|
| 投影式 | `level(r) = p0 + r×(p1−p0)` | 同じ。**変えない** | `ict_stdv.project` |
| 係数 | `1, 0, −1, −2, −2.5, −4` を図示 | 同じ 6 本 | `ict_stdv.RATIOS` |
| BUY の錨 | p0 = 下降 manipulation が始まった高値 / p1 = 掃引した安値 | 同じ(P1 4 ページ目の図で向きを確認) | 同 |
| SELL の錨 | 鏡像 | 同じ(P1 7 ページ目) | 同 |
| manipulation の起点 | 裁量(**未確定**と調査が明記) | **掃引足から遡った確定フラクタル・ピボット**(片側 `pivotBars`=2 本、右側が閉じていること、`maxLegBars`=40 本以内)。該当が無ければアンカーを作らない | `ict_stdv._pivot_index` |
| 掃引極値 | 掃引した極値 | ピボット〜掃引足の区間の極値(BUY は最安値 / SELL は最高値) | `ict_stdv.anchor_diagnosis` |
| 掃引の認定 | 裁量 | **既存 `msnr_gate` の SWEEP チェーンに委ねる**(1 本型/2 本型の奪還判定は既存のまま。STDV 側で新しい掃引検出を作らない) | `msnr_gate._chain_from_sweep` |
| CISD/MSS の確定 | 「構造の変化と併せて読む」 | 既存チェーンが `MSS_CONFIRMED` か `RETEST_HELD` に達したときだけ。`SWEEP_CONFIRMED`(掃引だけ)では作らない | `ict_stdv.CONFIRMED_STATES` |
| 使用時間足 | 上位足の背景 → 下位足の参加。後半の例に 2 分足 | **確定 3 分足**。「TTrades の考え方を 3 分足へ適用した Nightwatch 版」であり、2 分足例の再現ではない。足の合成はしない | `sourceTf: "3"` |
| wick / body | 箱の実体と投影レッグの高安は別定義 | 投影は**高安(wick)**。実体は使わない | 同 |
| 反応域 | `−2〜−2.5` を観察 | 同じ。記録のみで、到達をハードブロックにしない | `ict_stdv.REACTION_RATIOS` |
| `−4` へ進む条件 | 「`−2.5` を強く抜けたら」(数値条件は**未確定**) | **v1 では自動化しない。`−4` は観測専用**で目標に使わない | `targets.ratios` 既定 |
| 目標の選び方 | 流動性/FVG と近い目標を選ぶ例あり | 投影を**既存 `model_targets` の候補として渡すだけ**。最小 R・重複除去・runner 選定・tier は既存規則。STDV を `PREFERRED_LABELS` に入れない。runner が STDV になったら既定で採らない | `msnr_gate._candidate_for_chain` |
| 失効 | 裁量 | 確認足から `maxAgeBars`=120 本で `projectionValid=false` | `ict_stdv` |
| 方向 | 方向は構造から | **STDV は方向票を持たない**(`targetOnly=true` / 0 票) | `strategy_models._vote_direction` |
| 丸め | 指定なし | 生価格と 0.25tick 丸めを両方保持。丸めは利確側に保守的(BUY は下 / SELL は上)。丸めで同値になった行は `collapsedWith` を持ち、2 本扱いしない | `ict_stdv._round_to_tick` |

**統計の σ ではない。** ±2σ の確率や正規分布の包含率は持ち込まない。**リスク倍率 R でもない** ——
`−2` と `TP=2R` は別物で、R の分母は最終建値と構造 SL の距離。

## 2. アンカーの契約

`schema: NQX_ICT_STDV/1` / `version: R121-ICT-STDV-1`。保持するもの(依頼 §3 の表に対応):

* 識別 — `schema` `anchorId` `version` `symbol` `sessionId` `sourceTf` `sourceHash` `evidenceRootId`
* 根拠 — `originBarT`/`p0`、`sweepBarT`/`p1`、`liquidityId`、`sweepAt`、`confirmation{kind,barT,chainState}`
* 可知時刻 — `originAt` `extremeAt` に加え **`confirmedAt` / `knownAt`**。`knownAt` は
  「確認足の終値時刻」と「ピボットの右側 `pivotBars` 本が閉じた時刻」の**遅い方**
* 投影 — `side` `ratios` `levels[{ratio, raw, price, reaction, collapsedWith}]` `byRatio`
  `widthPt` `tick` `roundingRule`
* 状態 — `projectionValid` `invalidReasons` `lifecycle` `lastObservedAt` `ageBars` `reachedAt`
* 関係 — `parentAnchorId` `childAnchorIds`、`targetOnly` `eligibleForDirectionVote`

不変条件(試験で固定):

* アンカー確定後、**現値が EQ を通っても方向・p0・p1・anchorId は変わらない**。別の掃引は
  別 `anchorId`。良く伸びた方へ差し替えない
* 再起動・再生でも同じ履歴から同じ ID と水準になる
* `knownAt` より前の足では到達に数えない(後から「事前予測成功」に計上しない)
* `projectionValid` と `eligibleForDirectionVote` は別。`_repo2_lifecycle` が `targetOnly` を
  `valid=false` にしても、目標消費者は `projectionValid` を見るので使える

## 3. 「読む」と「参加する」を分ける

`ict_stdv.read_context` が候補ごとに返す:

| フィールド | 中身 |
|---|---|
| `thesis` / `thesisInvalidation` | BUY / SELL / UNRESOLVED と否定条件 |
| `nearTerm` | 直近の予測対象・方向・根拠。**未検証の確率は付けない** |
| `participation` | `ENTRY_READY` / `WAIT_FOR_PULLBACK` / `NO_ROOM` / `INVALIDATED` |
| `entryZone` | 待っている位置と、入るための**既存**トリガー |
| `stdvContext` | 固定アンカー、水準、消化済み、残り、反応域 |
| `headroom` | `riskPt` / `entryToTp1R` / `entryToFinalR` / `priceToTp1R` / `priceToNearestRemainingPt` |
| `invalidation` | 撤回条件 |

`WAIT_FOR_PULLBACK` は**逆向きへ発注する指示でも、指値で経路を無期限に占有する指示でもない**。
参加の可否は既存ゲート(距離・TP1 通過・ボラ・イベント・所有権)が決める。STDV 到達だけで
新規逆張りは作らない —— この層は注文を作る関数を持たない(試験で固定)。

## 4. 目標への効かせ方(TARGETS 段)

`targets.mode == "LIVE"` のときだけ、`target_candidates` が
`(price, label)` を `model_targets(extra=…)` へ渡す。条件は「方向一致・未消化・建値の先・
`collapsedWith` なし・`targets.ratios` に含まれる係数」だけで、**最小 R・重複除去・runner
選定は既存規則に任せる**。

runner の保護: ラベルは `STDV_m1` 等で `level_tier` に拾われない(= tier 5)。`model_targets`
は `runner = (quality or tail)[-1]` なので、tier<=3 の目標が 1 本も無い周期だけ STDV が
runner になり得る。その場合は既定で**採らずに** `runnerRejected` を記録して基準の目標へ戻す。
`runnerEligible=true` と明示比較が無いと runner にしない。`−4` は目標係数から外してある。

TP が実際に差し替わった候補だけ `stdvIdentity` が付き、`setup_identity`(= `decisionId` の素)
に入る。**「価格を変えたが ID 不変」で過去 claim を流用しない。** OFF / SHADOW と差し替えの
無い周期では `decisionId` は R121 以前とバイト一致する。

## 5. 既存消費者の追跡(依頼 §5.1)

既存 `fibSd` を追った結果:

| 経路 | 事実 |
|---|---|
| `strategy_models.detect_fib_sd` → `_eligible_votes` → `alignment` → 候補 score | **効いている**(±2 で `IMAGE_MODEL_ALIGNMENT` / `IMAGE_MODEL_CONFLICT`)。表示専用ではない |
| `msnr_gate` の `levels` | `fibSd` は**流入していない**(`msnr_gate` に `ratioRows` / `fib_sd` の参照が無い) |
| `target_pool` / `model_targets` の `extra` | `fibSd` は**流入していない**。`extra` は VP80 の VA 目標だけ |
| `_derive_fib_sd` の方向 | `range.favors` = `_range_from_prices` の現値 vs EQ 由来。**現値が EQ を跨ぐと投影方向が変わり得る** |

つまり旧 `fibSd` の消費者は**方向票だけ**。今回は旧経路を**削らず**、新 STDV を別スキーマで
足した。旧票の除去で改善した結果を新 STDV の目標精度改善と呼べないようにするため、
比較条件でも旧 `fibSd` は BASE / SHADOW / TARGETS すべて同一にしてある(触っていない)。
二重計上の防止は `targetOnly=true`(0 票)で、`fibSd` が 1 票でも STDV は票を足さない(試験)。

## 6. 実装完了したもの(オフラインで検証済み)

* `ict_stdv.py` — 投影・アンカー抽出・到達判定・`read_context`。純粋計算のみ
* `msnr_gate.ict_stdv_policy` / `_stdv_for_chain` — 契約読み出しと候補への接続
* `setup_identity` — TP 差し替え時の `decisionId` 反映
* `strategy_models` の matrix に `ictStdv`(0 票・`projectionValid` 別枠)
* `tests/test_r121_ict_stdv.py` — 依頼 §6 の受け入れ表に 1 対 1。**全件 PASS**
* `replay_ict_stdv.py` — BASE / SHADOW / TARGETS / TGT_RUN の逐次比較
* `verify_r119_live.py` §2b — 本番起動先のコード版・設定・係数を通信なしで確認
* 回帰 — 隔離コピー(`.secrets` 抜き・通信遮断)で `tests/run_all.py` 127 ファイル、失敗は
  `test_r118_gateway_integration.py` の 1 件のみで、これは R121 以前からの既存失敗

## 7. 測定した効果(2026-08-15 → 09-18、監査バンドル 1358 本)

`python replay_ict_stdv.py --replay`。差し替えるのは `ict_stdv_policy` だけで、
riskCap / stopLogic / limitGate は契約の実値。約定規則は `entry_depth.simulate`
(確定 3 分足・指値 1tick 突き抜け・約定前 TP1 で取消・同じ足は SL 優先・指値待ち 30 分)。

### 7.1 アンカー

| 指標 | 値 |
|---|---|
| 適格アンカー(候補ごと) | **124** / 別アンカー **32 本** |
| 根拠欠損 | **439**(`CHAIN_NOT_SWEEP` 337 / `CHAIN_STATE_UNCONFIRMED` 102) |
| 確認 → 可知の遅延(`knownAt − confirmedAt`) | 中央 **0 秒** / 最大 0 秒 |
| 観測時点で既に消化済みの水準を持つアンカー | **22** / 124 |
| 現値を代用したアンカー | **0**(すべて確定足で判定) |
| participation の内訳 | `WAIT_FOR_PULLBACK` 101 / `ENTRY_READY` 23 |
| thesis の内訳 | BUY 76 / SELL 48 |

### 7.2 目標消費者

| 条件 | 提示された候補 | 目標が差し替わった候補 | runner 却下 | **最終 decision で差し替わった周期** |
|---|---|---|---|---|
| TARGETS | 124 | 15 | 36 | **0** |
| TGT_RUN(参考) | 124 | 62 | 0 | **0** |

### 7.3 逐次(同時に 1 建玉。結論はここで語る)

| 条件 | 取った | 約定 | TP1到達 | 損切り | ΣR | 最大DD |
|---|---|---|---|---|---|---|
| BASE | 36 | 20 | 7 | 11 | **+4.80** | −4.97R |
| SHADOW | 36 | 20 | 7 | 11 | **+4.80** | −4.97R |
| TARGETS | 36 | 20 | 7 | 11 | **+4.80** | −4.97R |
| TGT_RUN | 36 | 20 | 7 | 11 | **+4.80** | −4.97R |

周期単位でも `decisionId` 変化 0 / TP 変化 0 / ARMED↔WATCH 0(全条件)。
**費用は含まれていない**(`entry_depth.simulate` は手数料も滑りも入れない)。MFE/MAE は
この再生器が返さないので**未取得**(実トレードの MAE/MFE は `model_scorecard.py --excursions`)。
期間分割は前 70% で n=1 と標本が足りず、**holdout として使えない**。

### 7.4 なぜ最終判断が変わらないのか(測定された原因)

v1 のアンカー源は確定済み SWEEP チェーンだけなので、担い手はほぼ
`TURTLE_SOUP_REVERSAL` になる。実測(先頭 400 バンドル)では STDV アンカーを持つ候補
**69 件のうち 68 件が TURTLE**、残り 1 件が OTE。そして **TURTLE は `modelGate` で `ALL`
停止中**(2026-09-14 ユーザー決定)なので、68 件すべてに `MODEL_DISABLED` が付き
primary にならない。

> **TARGETS を LIVE にしても、この標本では最終判断が 1 件も変わらない。**
> これは実装の不具合ではなく、アンカー源とモデル停止の組み合わせによる構造的な帰結。

## 8. 市場再開後に確認するログ項目

再開前に 1 回(通信なし):

```bash
python verify_r119_live.py
```

`§2b` が `ict_stdv` の解決先・`schema/version`・係数・`ictStdv` の 3 モード・`invalid` が空
であることを出す。**`.claude/worktrees/` の複製には R121 が入っていない**ので、起動は必ず
`C:\Users\exexu\Downloads\nq-nightwatch-claude-code-handoff` から。

周期のログ:

| 見る場所 | 期待値 | 異常のとき |
|---|---|---|
| 候補の `ictStdv.reason` | `CHAIN_NOT_SWEEP` / `CHAIN_STATE_UNCONFIRMED` が大半、`null` が適格 | `MODULE_UNAVAILABLE:*` が出たら `ict_stdv.py` が読めていない |
| `ictStdv.anchor.anchorId` | 同じ掃引の間は**同じ ID が続く**(周期ごとに変わらない) | 毎周期変わるならピボット選定が揺れている |
| `ictStdv.anchor.p0` / `p1` | 現値が EQ を跨いでも不変 | 変わったら方向凍結が壊れている |
| `ictStdv.anchor.priceSubstituted` | 通常 `false` | `true` が増えたら確定足で到達を取れていない |
| `ictStdv.targetsApplied` | **`targets.mode=OFF` の間は必ず `false`** | `true` なら設定か配線の異常 |
| `ictStdv.read.participation` | `WAIT_FOR_PULLBACK` / `ENTRY_READY` / `NO_ROOM` | `INVALIDATED` 連発なら `maxAgeBars` を見る |
| `decision.decisionId` | SHADOW の間は R121 以前と同じ | 変わったら `stdvIdentity` が誤って付いている |
| `evidence` | `ICT_STDV_ANCHOR` は記録タグ | 採点・等級が動いていたら退行 |

1 窓溜まってから:

```bash
python replay_ict_stdv.py --replay --reevaluate
```

## 9. 未達(理由と、次に必要な最小の証拠)

| 未達 | 理由 | 次に必要な最小の証拠 |
|---|---|---|
| **損益改善は実証していない** | 逐次で 4 条件すべて ΣR +4.80 の同値。差が出ていないので改善も悪化も言えない | TURTLE の停止解除、またはアンカー源の拡張後に、同じ再生で ΣR の差と区間を出す |
| TARGETS の LIVE 昇格 | 最終 decision への到達件数が **0**。効果を測れない | primary に届く担い手が要る(§9 の 1 行目と同じ) |
| PARTICIPATION 段 | TARGETS との違いが解釈できる材料がまだ無い(依頼 §5.2 の指示どおり混ぜていない)。**配線は入っているが既定 OFF で、参加判断への反映は未実装** | TARGETS が効く標本ができてから、到達済み/残り値幅を参加判断へ入れた条件を 1 本追加 |
| BREAKER(FLIP チェーン)への適用 | 出典の manipulation-sweep の枠に当てはまるかが**未確定**(調査 §4 の項目 1・2)。根拠欠損 337 件はこれ | FLIP の起点を manipulation の出発点と見なせるかの仕様決定。推測で広げない |
| `−4` の自動化 | 出典が数値条件を示していない(調査 §4 項目 6) | `−2.5` 超えの displacement の数値条件を決め、`runnerEligible` と独立比較 |
| holdout | 前 70% の標本が n=1 | 1 か月以上の追加期間 |
| 2 分足の再現 | 3 分足しか取得していない。合成しない | 2 分足の取得経路 |

## 10. 戻し方

* **段ごとに 1 語**: `execution_contract.json` の `ictStdv.mode` / `targets.mode` /
  `participation.mode` を `OFF` に。全部 OFF にすると候補から `ictStdv` キーごと消え、
  出力は R121 以前とバイト一致する(試験で固定)。
* 書き換えたら
  `python -c "import msnr_gate,json; print(json.dumps(msnr_gate.ict_stdv_policy(),ensure_ascii=False))"`
  で `invalid` が空であることを確認。不正な段は黙って OFF になる。
* Worker のデプロイは不要(Python 側だけで完結)。
* SL / 建値 / 枚数 / 口座別上限 / R119 / AUTO / 所有権 / 発注前検査は一切触っていないので、
  この節を OFF にしても他の挙動は変わらない。
