# R122 — 多層構造文脈と参加判断

2026-09-19 JST 実装。指示書は `docs/CLAUDE_MULTITIMEFRAME_PARTICIPATION_LIVE_DIRECTIVE_2026-09-19.md`、
評価の出発点は `docs/reports/STRATEGY_KNOWLEDGE_AUDIT_2026-09-19.md`、知識仕様表は
`docs/R122_STRATEGY_KNOWLEDGE_SPEC.md`。

**本番構成(2026-09-19)**

| 段 | mode | 意味 |
|---|---|---|
| `context` | **LIVE** | 親の仮説 / 子の構造 / 親子関係を毎周期作り、decision と凍結プランへ残す |
| `participation` | **LIVE** | 候補ごとの参加状態を残し、**親が否定された同方向の継続だけ**武装しない |
| `shallowCandidate` | **SHADOW** | 浅い構造の代替候補を作って記録するが primary にしない(逐次再生で悪化したため) |
| `selection` | **LIVE** | 既存ゲートを全部通った候補を優先して primary にする(2026-09-20 採用)。根拠と全 14 件の一覧は `docs/R122_SELECTION_STAGE.md` |
| `nearTerm` | **SHADOW** | 3 分 / 15 分の方向予測。**表示・評価専用で LIVE にできない**(検証器と試験が拒否する)。§5.4 の実測では「常に NEUTRAL」の基準に負けており、技能は実証されていない |

戻し方は各段の `mode` を `OFF` にする 1 語。実装は Python 側だけで完結し、**Worker のデプロイは不要**。

---

## 0. 固定した版と入力

| 対象 | 値 |
|---|---|
| 開始時のコード版 | git `9db4e5980e7818a02bd3215922f66518efb8201c`(master) |
| 設定版 | `execution_contract.json` `R22-EXECUTION-CONTRACT-1`。`ictStdv` LIVE/TARGETS LIVE、`modelGate.disabled=[]`(TURTLE 復活)、`limitGate` gapCap 1.5 LIVE / targetPassed LIVE、`entryDepth` LIVE、`stopLogic` vwapClearance LIVE / poolClearance LIVE / sweepGate SHADOW / flipOrigin OFF |
| 入力 A | `.secrets/monitor_cycle_*.json` **1,358 本**(2026-08-15 01:06 → 2026-09-18 18:44 UTC) |
| 入力 B | 上のうち**直接取得した確定 15 分足がある窓 118 本**(2026-09-18 11:04 → 18:44 UTC) |
| 入力 C | **検算済みの再構成 15 分足がある 1,340 本**(2026-08-22 04:47 → 2026-09-18 18:29 UTC) |
| Python | 3.12.10 |

### 0.1 保存済みデータにある 15 分足を全部洗った結果

| 出所 | 期間 / 本数 | 使えるか |
|---|---|---|
| `.secrets/tv_raw/bars15m.json`(直接取得) | 2026-09-18 06:00–20:45 UTC / 59 本 | **使う**(唯一の正解データ) |
| `.secrets/monitor_snapshot.json` / `tv_bundle.json` | 上と同一の窓・同一の値 | 重複(新規の情報なし) |
| リポ直下 `bars15m.json` | 2026-08-25 04:15–19:00 UTC / 59 本 | 監査バンドルとの重なり **0** → 使えない |
| 監査バンドル 1,358 本 | `bars15m` は **全件が `bars15mCount` だけ**(compact が落としていた) | 生足は無い |
| **監査バンドル自身の確定 3 分足から再構成** | 3,980 本 → **15 分足 779 本** | **研究用に使う**(下の検算に合格。本番の出所にはしない) |
| Databento 1m / 3m コーパス | 15 分足 2,232 本を作れるが、直接取得 15 分足との**重なりが 0 本** | 正解と突き合わせられない → **使わない** |

**再構成の検算と、その限界**: 5 本の確定 3 分足がそろった窓だけを 15 分足にし、直接取得した
15 分足と重なる **50 本すべてで OHLC の差が 0.00pt**(不一致 0)。1 本でも欠けた窓は作らない。
合わなければ使わない(ズラして合わせることはしない —— R102 のロール事故と同じ形を作らない
ため)。

> **ただし 50 窓の一致は、779 本すべてが直接取得と同じであることの証明ではない。**
> 重なりは全体の 6.4% で、残りは照合相手が存在しない。したがって**再構成 15 分足は研究用**
> であり、本番の親の出所は**直接取得した `snapshot.bars15m` だけ**に据え置く。再構成の経路は
> `replay_structure_context.py`(読むだけの再生)にしか無く、本番コードには存在しない
> (`tests/test_r122_structure_context.py::test_production_never_reconstructs_15m` が、
> `market_structure_context` / `monitor_publish` / `monitor_pipeline` / `tv_snapshot` /
> `autotrade_engine` / `order.py` / `msnr_gate` のどれも 3 分足から 15 分足を作らないことを
> 検査する)。再構成で測った数字と、直接取得だけで測った数字を同列に並べない。

**先読みの禁止**: 各バンドルへ渡すのは `t + 900 <= at` の足だけ、しかも**途切れていない
連続部分**だけ(穴のある系列でピボットを取ると、隣り合っていない足を隣として読む)。右側 2 本が
閉じるまでピボットは確定しない。これは実装側(`market_structure_context.pivots`)と再生側
(`replay_structure_context.inject_bars15m`)の両方で強制している。

---

## 1. なぜ作ったか(評価で指摘された穴)

`docs/R122_STRATEGY_KNOWLEDGE_SPEC.md` §6 の G1〜G8。要点だけ:

* 上位足の**仮説**(方向・目標・否定水準・寿命)を保持する構造が無く、消費は
  `htfContext.status/bias` の ±1 点だけだった。
* そのため「親の買い仮説が生きたままの押し目」と「親の否定水準を子が破って反転へ移った」を
  区別できなかった。
* 「待つ位置」「待つ期限」「何が来れば入れるか」はどこにも残らず、候補にならないチェーンは
  `build_candidates` で捨てられていた。
* 浅い押しで継続に参加する経路が無かった(BREAKER は `FLIP_HELD` 必須、OTE は 0.62 まで待つ)。
* 短期予測(`ict_stdv.nearTerm`)はアンカーの side をコピーするだけで、評価対象の時刻も
  基準価格も無かった。

---

## 2. 作ったもの

### 2.1 `market_structure_context.py`(純粋関数)

親の仮説を 1 件作る。出所は優先順に:

1. **直接取得した確定 15 分足**(`snapshot.bars15m`)。左右 2 本の厳密フラクタルで
   HH/HL か LH/LL を決め、方向が出たときだけ仮説にする(`fidelity=BARS`)。
2. 無ければ既存 `snapshot.htfContext`(45m/1h/4h/1d の確定足から `htf_context` が作った要約)。
   集計 bias と同じ向きの重み最大フレームを錨にし、`range20High/Low` を境界にする
   (`fidelity=SUMMARY`。生足が無いので `phase` は `UNKNOWN` のまま)。
3. どちらも無ければ `CONTEXT_UNAVAILABLE`。**3 分足から上位足を合成しない。**

保持する状態(指示書 §2.2 の対応):

```
structureId / parentId / evidenceRootId / schemaVersion
symbol / contract / timeframe / sessionId / sourceHash
observedAt / knownAt / confirmedAt / expiresAt
bias / phase / structure / amdOrderComplete
range / sweptLiquidity / brokenLevel / protectedLevel / objective
invalidationRule / invalidatedAt / touchedAt / consumedAt
events[] / childRelation
```

**新しいしきい値を作っていない。** 否定の許容は `msnr_gate` の `tol`(= max(touch_pt 2.0,
0.10×ノイズ床))、ピボットは左右 2 本(`htf_context._pivots` / `ict_stdv` / 研究
`structure_memory` と同じ)、鮮度窓は `htf_context.FRAME_MAX_AGE` の式(2×step + 600s)、
中立幅は 1.0×ノイズ床(`msnr_gate.STOP_BUFFER_NF_MULT` と同じ倍率)。

* **段階(`phase`)は観測できたものだけ**: `UNKNOWN / RANGE / SWEEP_OBSERVED /
  SHIFT_CONFIRMED / EXPANSION_UNSWEPT`。**AMD / AMDX の語彙は名乗らない。** 掃引を見て
  いないブレイクは `EXPANSION_UNSWEPT` + `amdOrderComplete=false` で、順序を補完しない。
* **終値否定(`invalidatedAt`)と足中の接触(`touchedAt`)は別**。右側の足が必要な
  ピボットは、その足が閉じた時刻(`confirmedAt`)以降しか使わない。
* **親子関係**: `CONTINUATION` / `PULLBACK` / `REVERSAL_CANDIDATE` / `UNRESOLVED`。
  親 BUY・子 SELL を「矛盾だから不成立」にはしない —— 親の否定水準に達していない
  レンジ内なら `PULLBACK`、否定済みまたはレンジ外なら `REVERSAL_CANDIDATE`。
* **ID は根拠から決まる**: `sha1(schema|symbol|timeframe|bias|保護水準|アンカー足)`。
  同じ根拠なら再起動しても同じ ID、根拠が変われば新 ID。
* **記憶(`Memory`)** は初出時刻・否定・消化を跨いで覚え、一度否定された ID は価格の
  再訪だけでは復活しない(`revivalBlocked`)。置き場所は
  `.secrets/structure_context_state.json` で、**発注台帳とは別ファイル**。読めなくても
  周期は止まらない(記憶なし = その場の観測だけで判断する)。

### 2.2 `participation_policy.py`(純粋関数)

既存の武装ゲート(hardBlockers / R89 / R119 / R103 / 幾何)を**置き換えない**。その出力を
読んで状態にする。

| 状態 | 決め方 | 動作 |
|---|---|---|
| `ENTRY_READY` | 既存ゲートを全部通った(`allowed`) | `orderIntent`(MARKET / LIMIT / RESTING_LIMIT)を明示。既存経路がそのまま発注する |
| `WAIT_FOR_PULLBACK` | `LIMIT_GAP_EXCEEDED` | 次に見るもの(建値まで戻ること)と期限を持つ。**claim も注文枠も取らない** |
| `WAIT_FOR_CONFIRMATION` | 上記以外の不足 | 不足を名指しし、チェーン状態から「次に観測すべきもの」を出す |
| `NO_ROOM` | `TARGET_HEADROOM_INSUFFICIENT` / `TARGET_ALREADY_PASSED` / `RISK_CAP_EXCEEDED` / `RISK_BELOW_NOISE` / `GEOMETRY_INVALID` | 不成立。遠い TP や狭い SL を作って通さない |
| `INVALIDATED` | 親が否定された同方向の候補、`ANCHOR_CONSUMED`、連鎖 `EXPIRED` | 新規武装を止める。**保有建玉の凍結計画は書き換えない** |
| `EXPIRED` | 連鎖 TTL 切れ / 親の寿命切れ | 同上 |
| `CONTEXT_UNAVAILABLE` | この層に依存する候補だけ | 従来候補は従来の検査で判断する |

**この層が足すブロッカーは `PARENT_THESIS_INVALIDATED` の 1 つだけ**で、掛かるのは
**親と同じ方向の候補**に限る(`depends_on_thesis`)。逆方向の独立したセットアップは止めない。
先回り指値型(TURTLE の `restingLimit`)は状態名の変更で消さず、`orderIntent=RESTING_LIMIT`
として**実際の注文意図**に残す。

`plans()` は候補になっていないチェーンの**観測用**待機計画(次に観測すべきもの・期限)を
返す。`claimsOrderSlot=false` を必ず持つ。

### 2.3 浅い構造の代替候補(§4.2)

深い押しが来ない場面の代替参加。**全部、既存の検出器の出力だけ**で条件を組む:

1. 親の仮説が生きている(方向 B・否定されていない・寿命内・目標未消化)
2. **方向 B の**子の構造が確認済み(`MSS_CONFIRMED` / `FLIP_ACCEPTED` 以上)。全体で最も
   進んだチェーンではなく方向ごとの最良を見る —— 押し目局面では最も進んだチェーンは
   逆方向(押しそのもの)なので、そこで切ると代替は永久に出ない
3. 方向 B の `eligible` かつ `preArrivalStructure=INTACT` な FVG があり、**その子の構造の
   起点以降に生成された**もの
4. その FVG の中点が**押し戻り側**にある(= 指値。取り逃しを理由に成行へ切り替えない)
5. 同じ方向に**発注できる候補が既に無い**

建値 = FVG 中点(Silver Bullet の `FVG_50_PERCENT_LIMIT` と同じ作法)、SL = FVG の外側
± `model_stop_buffer(nf)`。そこから先は**既存の** `_candidate_for_chain` が採点・目標・
R103/R90 の SL 逃がし・R119・幾何ゲートを全部通す。自分のレベル・SL・目標・トリガー・ID を
持ち、元の指値を現値へ移すことはしない。`OTE_FVG_CONFLUENCE`(+2)は**受け取らない**
(同じ証拠で二重加点しない)。重複除去の枠も既存候補と分けてあるので、既存候補を押し出さない。

### 2.4 短期予測(`nearTerm`)

次の **1 本(3 分)と 5 本(15 分)の確定 3 分足**に対する方向。基準価格・基準時刻・
評価時刻を固定し、中立幅は `±1.0×ノイズ床`(測定前に固定)。出力は `UP / DOWN / NEUTRAL /
UNKNOWN`。使った入力(`CHILD_COMPLETE` / `PARENT_BIAS` / `PARENT_PULLBACK` …)を残し、
入力が無ければ `UNKNOWN`。**確率は付けない。どの段でも参加判断に使わない**
(`gate:false` / `usedForParticipation:false` を自分で宣言し、契約の `nearTerm` に LIVE を
書いても policy loader が拒否する)。

### 2.5 選択層(`selection`)— 2026-09-20 に LIVE

`select_primary` は等級・点数・TP1 の R・モデル順位だけで並べるので、**高得点の WATCH 候補が
同じ周期の ARMED 候補より先に primary になる**。監査 1,358 本で 407 周期に候補があり、
310 が WATCH primary、そのうち **15 周期**で別の ARMED 候補が存在した。`selection` を LIVE に
すると「並べ替えの後に**武装できる候補**(hardBlockers ゼロ)を優先する」1 行が入る。

全 14 件(重複まとめ後)の一覧、WATCH 理由の GLOBAL / CANDIDATE 分類、費用込みの比較、
採用の理由と戻し方は **`docs/R122_SELECTION_STAGE.md`**。要点だけ:

* 隠していた理由は**全件が候補固有**(GLOBAL は 0/15)。市場全体・口座全体の停止は
  primary を選んだ**後**に当たるので、この変更では迂回できない。
* 費用込み(手数料 $0.50/枚/片道)で滑り 0/1/2 tick のどれでも ΣR は悪化しない
  (**+2.13 / +2.06 / +2.00R**)、最大 DD は不変。
* **ただし +2.06R のうち +2.62R が 1 トレードで、それを除くと −0.56R。**
  decisionId 単位は +0.056R/件・区間 [−0.09,+0.21] が 0 を跨ぐ(P(improve)=0.69)。
  **「勝率が上がる」とは言えない。**
* 変わるのは 1,358 周期中 **15 周期だけ**で、全部 WATCH → ARMED。うち **13 は建てる方向が反転**。
* 開始時 HEAD との照合: **1,359 本中 1,344 本が注文意図まで一致し、差は監査した 15 周期だけ**。
* 全体停止(ボラ床・イベント窓・限月・取得受領書・手動 HALT・AUTO・建玉照会)は
  **7 条件 × `reconcile` 実行で注文 0 件**を確認(`tests/test_r122_global_stop_no_order.py`。
  陽性対照つき)。

---

## 3. 本番への配線

```
tv_snapshot(bars15m を取得)
  → monitor_publish.compact(**bars15m を残す**。R122 で bars1d と同じ扱いに戻した)
  → monitor_publish.enrich_decisive_strategy
       .secrets/structure_context_state.json を読む → bundle["_structureMemory"]
       → msnr_gate.evaluate(純粋関数のまま。ファイルには触れない)
            strategy_evaluation → market_structure_context.build → build_candidates
              → 候補ごとに participation_policy.evaluate
              → shallowCandidate(LIVE のときだけ primary の選択肢に入る)
            → select_primary → decision.structure / decision.structureDetail
       → 記憶を書き戻す(失敗しても周期は止めない)
  → decision_to_scenario(scenario.structure)→ 凍結プラン → publish
```

`decision.structure`(最小形式・**カード縮小でも落とさない**):

```json
{"contextVersion", "thesisId", "participationState", "triggerEvidenceIds",
 "invalidation", "selectionReason", "changedFromBaseline", "relation",
 "orderIntent", "shallow", "baselineDecisionId"}
```

`decision.structureDetail`(親の縮約・参加の監査)はカード縮小で `poolStop` / `ictStdv` と
同じ層で落ちる。`changedFromBaseline` は「R122 が無ければどの decision だったか」を**同じ
候補集合を選び直して**求めた実差分で、後付けの説明ではない。

**この最小形式が届く先**(publish 境界でどこまで残るか):

| 先 | 残るか | 備考 |
|---|---|---|
| 段 | 残るか | 確認方法 | 備考 |
|---|---|---|---|
| (1) ローカル `decision.structure` | ○ | `verify_r122_live.py` §5b (1) | 最小 7 フィールドがそろう |
| 監査コピー `.secrets/monitor_cycle_HHMM.json` | ○ | 実バンドルで確認 | `structureContext`(縮約)+ `evaluation.decision.structure` |
| 評価カード(4096 バイト縮小後) | ○ | `test_decision_carries_minimal_fields` | `structureDetail` が先に落ちる |
| (2) publish する scenario ペイロード | ○ | `verify_r122_live.py` §5b (2) | `decision_to_scenario` が `scenario.structure` を載せる |
| (3) **Worker 保存後** | **×** | `verify_r122_live.py` §5b (3) が **node で実際に `validateScenario` を実行**。`ok=true` で通るが `structure` は消える | `state_machine.js` は明示キーだけを組み直す正規化。`normalizeEvaluation` も同じ |
| (4) **執行が読む凍結プラン**(`plan.decisionStructure`) | ○ | `verify_r122_live.py` §5b (4) / `test_structure_survives_the_worker_boundary` | **Worker が落とした scenario を渡しても残る** |

**「公開シナリオまで届く」と「Worker が削除する」の関係**: publish する側のペイロードには
確かに載る(2)。Worker はそれを**未知キーとして捨てる**(3)。Mini App に根拠が出ないのは
これが理由で、直すには Worker のデプロイ(人の作業)が要る。

**執行には影響しない。** `autotrade_engine.reconcile` は Worker 正規化後の scenario を
「発注してよいか」の正本に使うが(`_authoritative_cycle_seal`)、凍結プランの
`decisionStructure` は**ローカルの評価カード**(`bundle.evaluation.decision.structure`)から
拾う。検証器はこれを「structure を抜いた scenario + ローカルバンドル」で実際に組み直して
確かめている。つまり**執行に必要な根拠は publish 境界で失われていない**ので、Worker 側の
修正は今回行わない(表示のためだけの変更で、deploy が要り、他セッションの未コミット作業と
同じファイルに触るため)。

**保有建玉の経路は一切触らない。** `autotrade_engine.py` と `order.py` は
`market_structure_context` / `participation_policy` / `structureContext` のどれも参照しない
(`verify_r122_live.py` §6 と `tests/test_r122_structure_context.py` が機械で見る)。
リスク・枚数・SL 上限・AUTO・口座範囲は変えていない。

### 3.1 `bars15m` を compact で残した理由

`monitor_publish.compact` は HTF の生足を `…Count` に畳んでいたため、**監査 1,358 本すべてで
`BARS15M_MISSING`** となり、親は常に粗い HTF 要約へ落ちていた(`bars1d` が同じ理由で
R48 の日足タグを 896 サイクル 0 件にしていたのと同じ形)。market payload にも凍結
プレビューにも載らないので**送信サイズは変わらない**。

### 3.2 `ictStdv.participation` の統合

参加判断の正式な入口は `structureContext.participation` の 1 か所。`ictStdv.participation` に
`OFF` 以外を書くと `msnr_gate.ict_stdv_policy` が `ICT_STDV_PARTICIPATION_SUPERSEDED_BY_R122`
を `invalid` に入れて **OFF 固定**にする(二重判定を作らない)。

---

## 4. 受け入れ試験

`python tests/test_r122_structure_context.py`(通信なし・`.secrets` に触れない)。
21 本のテスト関数が指示書 §7 の各項目に対応する。主なもの:

* 未来足・未確定ピボット・知り得る前の時刻・別シンボル・欠損足・鮮度切れを使わない
* 親 BUY / 子 SELL の押し目、親の否定、子の継続を**別ケース**として判定する
* 逐次と一括で同じ ID、窓から起点が消えたら同じ ID を作り直さない
* 否定済みの仮説が価格の再訪だけで復活しない(墓標)。新しい根拠なら新 ID
* 参加状態の各遷移と、先回り指値型が状態名の変更で消えないこと
* 証拠のある浅い構造だけが代替候補になり、証拠なしの追いかけ(現値通過後の FVG・
  古い空隙・構造が壊れた FVG・親が死んでいる・子が未確認)は生成しない
* OFF で decision に R122 のキーが 1 つも増えない / SHADOW で注文意図が変わらない
* **LIVE で構造を否定した fixture に差し替えると、依存する候補が ARMED → WATCH へ落ち、
  `changedFromBaseline` に理由が残り、逆方向の候補は落ちない**
* 保存 → 読み込み → evaluate → decision → 凍結プランが通信なしで通る
* `nearTerm` の LIVE 拒否、`ictStdv.participation` の OFF 固定
* R121(STDV TARGETS LIVE)/ R89(TURTLE 復活)/ R119 の回帰

**全体**: `python tests/run_all.py` → 128 ファイル中 **1 本だけ失敗**
(`test_r118_gateway_integration.py`。R118 Gateway は別セッションの未コミット作業で、
R122 の変更前から同じ理由(一時ディレクトリの `divergence.jsonl` 不在)で失敗している ——
**既存の失敗であって今回の新規失敗ではない**)。

`python verify_r122_live.py` は発注も通信もせず、起動先の解決・契約の各段・配線の実在・
保存済みバンドルでの到達・`compact` の実挙動・記憶の分離を確認して終了コードを返す。

---

## 5. 同条件比較(A/B/C/D/E)

> **逐次規則の注記(2026-09-20)**: この節のはしごは共有ヘルパ
> (`replay_stop_logic.setups_for` / `simulate_setup`)を使っており、Codex レビューが
> 指摘した 3 点(武装前の初出時刻で占有判定 / 指値の待ち時間を占有に数えない /
> WATCH 周期の TP を流用)を**含んだまま**である(R90/R103/R119/R121 の公表済みの数字が
> 同じヘルパに乗っているため、共有側は変えていない)。結論への影響:
> **A/B/C は注文意図の変化が 0 件なので逐次の差は規則に依らず厳密に 0**、
> **D は修正後の規則で測り直して −1.10〜−1.29R(悪化が強まる)**、
> **E は `docs/R122_SELECTION_STAGE.md` §3 が正本**。

`python replay_structure_context.py --replay [--bars15m]`(読むだけ)。差し替えるのは
`msnr_gate.structure_context_policy` **だけ**で、modelGate / ictStdv / riskCap / stopLogic /
limitGate / entryDepth は契約の実値。`entry_depth.simulate` の同じ約定規則、同じ逐次 1 建玉
制約、R は計画の SL 幅。入力が変種間で同一であることは 1 周期ごとのハッシュで確認している。
**手数料とスリッページは含まれていない**(片道 1 枚 $5 の口座なら 1 往復 $10 = SL 20pt で
約 0.25R 相当)。

### 5.1 判定の同一性

| 比較 | 結果 |
|---|---|
| **R122 全段 OFF vs 開始時 HEAD** | 監査 **1,359 本すべてで decision・全候補・summary がバイト一致**(差 0) |
| **本番構成(context/participation LIVE)vs HEAD** | 監査 **1,359 本すべてで注文意図が一致**(model/side/state/grade/建値/SL/TP/R/decisionId/hardBlockers/entryMode と候補集合。差 0) |

### 5.1b 2×2: 保存修正と判断ロジックを分離する(1,340 本・検算済み 15 分足)

`python replay_structure_context.py --split --source recon`。4 条件は**同じ 3 分足・同じ価格・
同じ時刻**を読み、違うのは (a) `snapshot.bars15m` を渡すか、(b) `structureContext` の段だけ。
共通入力(15 分足を除いた部分)のハッシュ種類は 1,338 = 全条件で同一。

| 条件 | 15 分足 | R122 | 親あり | 親の出所 | 親の否定 | 注文意図の変化 | 逐次 ΣR |
|---|---|---|---|---|---|---|---|
| A0 | なし | OFF | — | — | — | — | **+7.71R** |
| A1 | あり | OFF | — | — | — | **0** | +7.71R |
| C0 | なし | LIVE | 1,146/1,340(86%) | 4h 1,104 / 1d 40 / 1h 2、全件 SUMMARY | **2** | **0** | +7.71R |
| C1 | あり | LIVE | **1,293/1,340(96%)** | **15m 649(BARS)** / 4h 602 / 1d 40 / 1h 2 | **130** | **0** | +7.71R |

効果の分離:

| 比較 | 意味 | 結果 |
|---|---|---|
| A1 − A0 | **保存修正だけ** | **±0R / 注文意図の変化 0** —— `bars15m` を読むのは R122 だけなので、R122 OFF では届いても何も変わらない(データ修正が単独で判断を動かさないことの確認) |
| C0 − A0 | 判断ロジックだけ(粗い親) | ±0R / 注文意図の変化 0 |
| C1 − A1 | 判断ロジックだけ(良い親) | ±0R / 注文意図の変化 0 |
| C1 − C0 | **良い親を与えた効果** | ΣR ±0R。ただし**中身は大きく変わる**: 親の網羅 86% → 96%、精度が BARS へ、親の否定 2 → **130 周期**、`REVERSAL_CANDIDATE` 1 → **76 周期**、`PULLBACK` 381 → 432 |

**結論**: 15 分足が届くようになったことで**読みは実質的に変わった**が、**注文意図と損益は
1,340 周期で 1 件も変わっていない**。R122 が今やっているのは「根拠を残すこと」と
「親が壊れた方向の継続を止める用意をすること」で、後者はまだ発火していない(§5.1c)。

### 5.1c 親の否定が実際の判断に効いたか(`--invalidation --source recon`)

親が否定された **130 周期**を R122 OFF / LIVE で並べ直した実測:

| 数えたもの | 件数 |
|---|---|
| 親が否定された周期 | **130** |
| そのうち**親と同じ方向に武装できる候補があった**周期 | **0** |
| → **ブロッカーが「武装できた候補を止めた」周期** | **0** |
| ブロッカーが立った周期(候補は既に別の理由で WATCH) | 3 |
| そのうち内訳 | `NO_CONFIRMATION` 3 件 / `RISK_CAP_EXCEEDED` 1 件と併存 |
| **逆方向に武装できる候補があった**周期 | 1 |
| → そのままARMED で残った | **1 / 1** |

実データの 3 例(すべて実際の監査バンドル):

1. **親の否定後・同方向 = 止める側**(2026-09-15 05:30:30Z / 05:33:38Z)
   親 `15m BARS BUY`、否定 = 「15m 確定終値が 29,427.75 を下抜け」。
   `VP80_REVERSION BUY` に `PARENT_THESIS_INVALIDATED` が付く。
   ただし同じ候補は `NO_CONFIRMATION` でも落ちていたので **primary は OFF/LIVE とも
   `VP80_REVERSION BUY B WATCH` のまま**。= この回のブロッカーは**冗長**だった。
2. **親の否定後・逆方向 = 止めない側**(2026-09-09 13:32:14Z)
   親 `15m BARS SELL` が否定済みの周期に `OTE_FVG_PULLBACK BUY ARMED` が存在。
   R122 LIVE でも**ARMED のまま**、primary も `OTE_FVG_PULLBACK/BUY/ARMED` のまま。
   「親が壊れたことを理由に反転候補を止めない」が実データで成立している。
3. **正常な押し目**(2026-09-18 13:16〜13:35Z、5 周期)
   親 `15m BARS SELL`(保護水準の内側)→ 子 `PULLBACK` → 参加 `ENTRY_READY` →
   `VP80_REVERSION SELL ARMED`。判断は R122 前と同一で、根拠だけが増えた。

**未確認の範囲(限定して書く)**: 「親が否定され、かつ同じ方向に**武装できる**候補がある」
周期は 1,340 本の入力に **1 件も無かった**。したがって
**`PARENT_THESIS_INVALIDATED` が実際に発注を止めた例は 0 件**で、その経路の正しさは
合成 fixture(`tests/test_r122_structure_context.py::test_live_blocker_flips_armed_to_watch`:
同じ入力で親を否定すると ARMED → WATCH、`changedFromBaseline` に `state:ARMED→WATCH`、
SHADOW では ARMED のまま)でのみ確認できている。**実データでの発火率は 0/1,340 周期が上限**で、
「効くことを実運用で確かめた」とは言えない。

### 5.2 監査コーパス 1,358 本(親は HTF 要約精度)

| 変種 | 注文意図の変化 | 逐次 ΣR | 最大 DD | 取った | 約定 | 損切り | 保有制約で見送り | 最大勝ちの占有 |
|---|---|---|---|---|---|---|---|---|
| A_NOW(現行) | — | **+7.71R** | −3.97R | 37 | 21 | 10 | 44 | +4.19R(54%) |
| B_SHADOW | **0** | +7.71R | −3.97R | 37 | 21 | 10 | 44 | 54% |
| C_LIVE | **0** | +7.71R | −3.97R | 37 | 21 | 10 | 44 | 54% |
| D_SHALL | 13(全て WATCH→ARMED) | **+6.71R** | −4.97R | 41 | 22 | 11 | 50 | 62% |
| E_SELECT | 28(全て WATCH→ARMED) | **+8.91R** | −4.97R | 48 | 25 | 12 | 56 | 47% |

セットアップ単位(106 件、bootstrap 2000 回):

| 変種 | ΣR | vs A | 90% 区間 | P(improve) |
|---|---|---|---|---|
| D_SHALL | −2.44 | **−0.047R/setup** | [−0.08, −0.02] | **0.00** |
| E_SELECT | +1.56 | −0.009R/setup | [−0.10, +0.09] | 0.42 |

文脈の網羅: 親の仮説あり **1,148/1,358(85%)**、出所は 4h=1,106 / 1d=40 / 1h=2、
**精度は全件 SUMMARY**(`BARS15M_MISSING` 1,358 = compact が落としていたため)。
親子関係は CONTINUATION 355 / PULLBACK 381 / REVERSAL_CANDIDATE 1 / UNRESOLVED 621。
**親が否定された周期は 2**、`PARENT_THESIS_INVALIDATED` の発火は **0**。
参加状態(候補ごと): INVALIDATED 240 / NO_ROOM 137 / ENTRY_READY 106 /
WAIT_FOR_CONFIRMATION 41 / EXPIRED 35 / WAIT_FOR_PULLBACK 4。

### 5.3 直接取得した 15 分足がある窓 118 本(親は BARS 精度)

| 変種 | 注文意図の変化 | 逐次 ΣR | 最大 DD | setups ΣR |
|---|---|---|---|---|
| A_NOW | — | +1.66R | −1.00R | −0.37 |
| B_SHADOW / C_LIVE / D_SHALL | **0 / 0 / 0** | +1.66R | −1.00R | −0.37 |
| E_SELECT | 7 | +1.66R | −1.00R | +1.07(+0.090R/setup、区間 [−0.12,+0.39]、P=0.60) |

文脈の網羅: **118/118(100%)**。出所 15m=85(BARS)/ 4h=33(SUMMARY)。
段階は RANGE 83 / EXPANSION_UNSWEPT 2 / UNKNOWN 33。親子関係は **PULLBACK 69 /
CONTINUATION 21 / REVERSAL_CANDIDATE 6 / UNRESOLVED 22** —— 評価で「区別できない」と
指摘された 3 局面が、実データで分かれて出ている。親が否定された周期 2(どちらも候補なし
だったので発火 0)。浅い代替候補は 4 件生成され、**全件が既存の幾何ゲート
(`TARGET_HEADROOM_INSUFFICIENT` 4 / `RISK_CAP_EXCEEDED` 3)で WATCH** になり、
primary にはならなかった。

### 5.4 短期予測の成績(表示・評価専用。**基準より悪い**)

中立幅は `1.0×ノイズ床`(中央 12.75pt / 最小 4.75 / 最大 72.88)。1,340 本・親 = 15 分足
(`C1_LIVE_15`)で採点。**「逆方向ではなく動かなかった」も、その地平では外れとして数える。**

| 地平 | 採点行 | UNKNOWN 率 | 予測の内訳 | 実測の内訳 | **R122 の一致率** | **常に NEUTRAL** | 差 |
|---|---|---|---|---|---|---|---|
| 3m | 1,327 | 130(9.8%) | UP 432 / DOWN 480 / NEUTRAL 285 / UNKNOWN 130 | UP 87 / DOWN 95 / NEUTRAL 1,145 | 314 = **23.7%** | 1,145 = **86.3%** | **−62.6pt** |
| 15m | 1,317 | 127(9.6%) | UP 432 / DOWN 473 / NEUTRAL 285 / UNKNOWN 127 | UP 301 / DOWN 304 / NEUTRAL 712 | 353 = **26.8%** | 712 = **54.1%** | **−27.3pt** |

混同表(予測 → 実測):

| 地平 | 予測 | n | → UP | → DOWN | → NEUTRAL | その予測の正解率 |
|---|---|---|---|---|---|---|
| 3m | UP | 432 | 26 | 31 | 375 | 6.0% |
| 3m | DOWN | 480 | 27 | 39 | 414 | 8.1% |
| 3m | NEUTRAL | 285 | 22 | 14 | 249 | 87.4% |
| 3m | UNKNOWN | 130 | 12 | 11 | 107 | — |
| 15m | UP | 432 | 87 | 100 | 245 | 20.1% |
| 15m | DOWN | 473 | 105 | 112 | 256 | 23.7% |
| 15m | NEUTRAL | 285 | 58 | 73 | 154 | 54.0% |
| 15m | UNKNOWN | 127 | 51 | 19 | 57 | — |

**結論(隠さずに書く)**: この短期予測は、**「常に NEUTRAL と答える」だけの予測器に
両方の地平で負けている**(3m で −62.6pt、15m で −27.3pt)。方向を出した行に限っても
的中は 3m 26.2% / 15m 29.7%、実際に中立幅を超えて動いた行に限った向きの一致は
3m 52.8% / 15m 49.3% で、**コイン投げと区別がつかない**。中立幅は測定前に固定しており、
これは後付けの不利な切り方ではない。したがってこの予測器には**実証された技能が無い**。
`nearTerm` は SHADOW(表示・評価専用)のままで、参加判断には使っていないし、
この成績のまま LIVE にする道は無い(契約に LIVE と書いても policy loader が拒否する)。

### 5.5 設計評価の場面(`python replay_structure_context.py --scenes 30 --bars15m`)

選び方は「候補がある周期を古い順に 30 件」で、結果で選んでいない。代表例:

1. **正しい参加**(2026-09-18 13:16〜13:35Z、5 件): 親 `15m/BARS SELL`(保護水準の内側)、
   子は `PULLBACK`、参加 `ENTRY_READY`、判断 `VP80_REVERSION SELL ARMED`。
   結果 +0.75R / +0.92R / −0.37R / +1.91R。**多層の読みが実弾の判断に添えられた形。**
2. **正当な見送り(NO_ROOM)**(13:28Z、13:46Z): 目標候補が空。以前は
   `TARGET_HEADROOM_INSUFFICIENT` という内部ブロッカー名しか残らなかったが、
   参加状態として `NO_ROOM` + 注文意図 + 根拠 ID が残る。
3. **親と逆向きの候補を止めない**(13:38Z〜14:28Z、14 件): 親 `15m SELL` のまま
   `TURTLE_SOUP_REVERSAL BUY` が毎周期 primary に出続ける。参加は `INVALIDATED`
   (`ANCHOR_CONSUMED`)で、**親が SELL であることは理由に使っていない**。注文意図が
   `RESTING_LIMIT → LIMIT → MARKET` と変わっていく様子も残る。
4. **関係の遷移**: 14:04〜14:10Z に価格が親のレンジを外れて `PULLBACK → REVERSAL_CANDIDATE`。
5. **新しい構造は新 ID**: 14:16Z に親の保護水準が切り替わり `thesisId` が
   `b7832f6c3033 → 10c70920ad1b`。

---

## 6. 本番設定をこう決めた理由

* **`context` = LIVE / `participation` = LIVE**:
  2 コーパス合計 1,476 周期で**注文意図の変化 0**。`PARENT_THESIS_INVALIDATED` は
  「親の時間足の確定終値が保護水準を抜けた」ときだけ、しかも**同じ方向の候補だけ**に掛かる
  fail-closed のガードで、武装を増やす経路が無い(最悪でも取り逃し)。実測の発火は 0 回、
  fixture では ARMED → WATCH が成立する。得られるのは **根拠(thesisId / participationState /
  triggerEvidenceIds / invalidation / relation / orderIntent)が decision と凍結プランに残ること**と、
  15 分足が入る本番で親の否定が実際に効くこと。
* **`shallowCandidate` = SHADOW**:
  実装・配線・試験は完了しているが、**逐次 ΣR が +7.71 → +6.71(−1.00R)、最大 DD が
  −3.97 → −4.97R、セットアップ単位 −0.047R/setup で 90% 区間 [−0.08, −0.02] が 0 を跨がない**。
  標本不足ではなく**測って悪かった**ので LIVE にしない。記録は残すので、次に見直すときは
  同じ再生でそのまま比較できる。
* **`selection` = LIVE**(2026-09-20 採用。詳細は `docs/R122_SELECTION_STAGE.md`):
  採用理由は損益ではなく**選択層の欠陥を直すこと** —— 候補固有の理由で成立していない
  高得点候補が、同じ周期の**完全に適格な**候補を隠していた(1,358 周期中 15 周期、
  隠した理由は全件 CANDIDATE 種別で GLOBAL は 0)。選べるのは `hardBlockers` ゼロの候補だけで、
  市場全体・口座全体の停止は primary を選んだ後に当たるので迂回できない(7 条件 ×
  `reconcile` 実行で注文 0 件を確認)。費用込みで滑り 0/1/2 tick のどれでも ΣR は悪化せず
  (+2.13 / +2.06 / +2.00R)、最大 DD も不変。**ただし損益の根拠は弱い** —— +2.06R のうち
  +2.62R が 1 トレードで、除くと −0.56R。decisionId 単位は +0.056R/件で区間が 0 を跨ぐ
  (P=0.69)。**13/15 で建てる方向が反転する**のが最大の変化。戻すのは `mode` を `OFF` に
  する 1 語。
* **`nearTerm` = SHADOW**: 校正されていないので LIVE にできない(loader / 検証器 / 試験が拒否)。

---

## 7. 周期時間と I/O

| 項目 | 測定 |
|---|---|
| `msnr_gate.evaluate` の CPU | R122 OFF 8.0 ms/周期 → 本番構成 9.0 ms/周期(**+1.1 ms**)。15 分足を入れても同じ |
| `execution_contract.json` の読み | 3 回/周期 → **4 回/周期**(`structure_context_policy` が 1 周期に 1 回だけ読み、下流へ渡す) |
| 記憶ファイル | `.secrets/structure_context_state.json` を 1 周期に 1 読み 1 書き(数 KB) |
| ブローカー照会 | **増やしていない**(0 本) |
| publish サイズ | **変わらない**(`bars15m` は market payload にも凍結プレビューにも載らない) |

3 分周期の実測は 47〜57 秒(R114)なので、+1.1 ms は予算に影響しない。

---

## 8. 未達と、次に必要な最小の証拠

| # | 未達 | 理由 | 最小の追加証拠 |
|---|---|---|---|
| ~~U1~~ | ~~BARS 精度が測れていない~~ | **解消**(§0.1 / §5.1b)。検算済みの再構成 15 分足で 1,340 本を測った。親の網羅 96%、うち 649 周期が 15m BARS | — |
| U2 | `PARENT_THESIS_INVALIDATED` が**実データで発注を止めた例は 0 件** | 「親が否定され、かつ同方向に武装できる候補がある」周期が 1,340 本に 1 件も無かった(§5.1c)。機構は合成 fixture でのみ確認 | 実運用の周期でその同時発生を 1 件観測する。月曜以降の監査バンドルへ `--invalidation --source recon` を掛け直すだけ(コード変更不要) |
| U3 | 浅い代替候補が**悪化している** | 測定済み(−0.047R/setup、区間が 0 を跨がない) | 悪化の内訳(どの局面で負けたか)を `--scenes` で読み、(a) 建値を FVG 中点以外にする、(b) 親が BARS 精度のときだけ許す、のどちらかを**事前に決めてから**再測定する |
| U4 | 短期予測に**実証された技能が無い** | 「常に NEUTRAL」の基準に 3m −62.6pt / 15m −27.3pt で負けている。動いた行に限っても 49〜53% でコイン投げ(§5.4) | 方向器そのものを作り直す前に、**何を予測すれば使い道があるか**(例: 次の 5 本で TP1 側に触るか)を先に定義する。今の形のまま LIVE にする道は無い |
| U5 | 選択層の**優位は未確定**(採用済み) | 費用込みの逐次は +2.0〜+2.1R だが、セットアップ単位は区間が 0 を跨ぐ(P=0.73)。標本は 15 周期 | 実運用で `selectionReason=ELIGIBLE_PREFERRED_OVER_HIGHER_SCORE_WATCH` の周期を貯め、同じ再生で測り直す。悪化したら 1 語で OFF |
| U6 | 費用が入っていない | 再生で枚数が決まらないため R で表せない | `FEE_PER_SIDE_*` を使った実トレードの突合(`model_scorecard.py`)。今回の差は費用込みではない |
| U7 | Worker / Mini App に根拠が出ない | `validateScenario` / `normalizeEvaluation` が明示キーだけを残す正規化で、未知キーは落ちる | `cloudflare/src/state_machine.js` に `structure` の通過を足して**人が deploy** する。執行・監査はローカルで完結しているので、月曜の運用には不要 |

**これは「データ待ち」ではなく、何を測れば決まるかを書いたもの。** U1/U2 はコード変更なしで
時間が解決する。U3〜U5 は先に規則を決めてから測り直す。

---

## 9. 戻し方と、再開後に見るログ

* 戻し方: `execution_contract.json` の `structureContext.<段>.mode` を `OFF`。全段 OFF で
  判定は R122 以前と**バイト一致**する(1,359 本で確認済み)。`bars15m` を compact から
  外したい場合だけ `monitor_publish.compact` の 1 行を戻す。
* 再開後に見るログ:
  * `nqx_cycle` の行末 / `.secrets/monitor_cycle_HHMM.json` の `structureContext`
    (`status` / `tf` / `fidelity` / `bias` / `relation` / `invalidatedAt`)
  * `evaluation.decision.structure`(`participationState` / `thesisId` / `triggerEvidenceIds` /
    `changedFromBaseline`)。**`changedFromBaseline` が空でない周期が、R122 で判断が変わった周期。**
  * `.secrets/structure_context_state.json`(初出・否定・消化の記憶)
  * 15 分足が実際に届いているか: 監査バンドルの `snapshot.bars15m` が配列であること
    (`bars15mCount` だけなら compact の行が戻っている)
* 検証コマンド(すべて読むだけ):

```bash
python verify_r122_live.py
python tests/test_r122_structure_context.py
python replay_selection.py --audit
python replay_selection.py --blockers
python replay_selection.py --compare
python replay_structure_context.py --split --source recon      # 研究用(再構成 15 分足)
python replay_structure_context.py --invalidation --source recon
python replay_structure_context.py --replay
python replay_structure_context.py --replay --bars15m
python replay_structure_context.py --scenes 30 --bars15m
```
