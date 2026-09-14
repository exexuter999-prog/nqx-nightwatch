# R90 初期 SL の 3 つの穴(VWAP 手前の SL / 成行で縮む SL 距離 / BREAKER の錨)

2026-09-15。ユーザー相談「一番最近の NQX のロングの SL が VWAP にあって、そこを踏んでから
めっちゃ上がった。SL のロジックに穴があるのでは」への回答。実装は `stop_logic.py`(純粋計算と
契約の読み取り)、`msnr_gate.py`(穴 1・穴 3)、`autotrade_engine.py` と `order.py`(穴 2)。
設定は `execution_contract.json` の `stopLogic`、検証は `tests/test_r90_stop_logic.py`、
再生は `replay_stop_logic.py`(読むだけ)。

## 0. 結論

穴は 3 つあり、いずれも「その日だけ上がった」結果論ではなくルールの欠落として再現できる。

| 穴 | 何が起きていたか | 直し方 | 再生の結論 | 本番 |
|---|---|---|---|---|
| 1 | SL の計算が VWAP を見ていない。構造の極値 − 1N が VWAP の 10pt 手前に来た | VWAP が SL の近く(±1N)なら SL を VWAP の外側 0.25N へ。60pt 上限・R:R が壊れれば WATCH | 全表示で改善(塊 +4.4R、逐次 +4.4R) | **LIVE** |
| 2 | 成行に切り替わると実際の SL 距離がノイズ床を割る(予定 33.5pt → 約定から 16.9pt) | 発注時点の価格から SL まで 1N 未満なら成行を出さない。engine は claim の前、order.py は送信直前 | 全表示で改善(逐次 +2.3R。塞いだ 14 件中 11 件が損切り) | **LIVE** |
| 3 | BREAKER の SL の錨がブレイク足で、上昇の起点(VWAP 付近)ではない | ブレイク足から「安値が切り下がる限り」遡った起点を錨に(R88 の TURTLE と同じ形) | 塊 +4.2R だが逐次 −1.0R(BREAKER を WATCH に変えるだけ) | 実装済み・**OFF** |

穴 1 と穴 2 は独立に効き、同時に入れるとこの実例(§1)は「SL 28,950.50 で生き残り、
成行も 46.25pt(1.55N)で通る」形になる。穴 3 だけを入れるとこの実例は SL 121pt で
RISK_CAP_EXCEEDED → WATCH(見送り)になる。

## 1. きっかけ: 2026-09-14 23:26 JST BREAKER_CONTINUATION B BUY

| 項目 | 価格 |
|---|---|
| 予定の Entry | 29,002.00(ブレイクしたレベルそのもの) |
| 実際の約定 | 28,985.38(成行。発注時にはレベルを割っていた) |
| SL | 28,968.50(ブレイク足 22:48 の安値 28,998.50 − 1N) |
| 当時の VWAP | 28,958.15(SL の 10.35pt = 0.35N 下) |
| 押しの安値 | 28,955.75(23:39。VWAP を 4.6pt 割っただけ) |
| その後 | 00:06 に TP1 29,070.25、02:54 に 29,305.75(+337pt) |

ノイズ床 N = 29.88pt(直前の確定 3 分足 12 本のレンジ中央値)。

- 穴 1: SL は VWAP の手前に置かれた。価格が VWAP を試しに来ると VWAP に届く前に刺さる位置。
- 穴 2: `RISK_BELOW_NOISE` は予定建値 29,002(SL まで 33.5pt = 1.12N)で通ったが、発注時点の
  公開価格 28,996.75 からは 28.25pt(0.95N)、実約定からは 16.9pt(0.57N)。普通の 3 分足
  1 本で届く距離だった。
- 穴 3: 上昇は 22:45 の足(安値 28,950.75)から始まっていた。錨にしたブレイク足はその途中。

## 2. 直し方

### 2.1 穴 1 — VWAP の外側へ(`stop_logic.vwap_clearance` → `msnr_gate._candidate_for_chain`)

BUY の場合。`required = tick_down(VWAP − clearN·N)`。今の SL が `required` より上(= VWAP の
外側 0.25N に届いていない)で、かつ `SL − VWAP ≤ withinN·N` のとき SL を `required` へ置く。
SELL は鏡像(切り上げ)。

- 対象になる SL の位置: VWAP の手前(建値側)〜VWAP ちょうど〜VWAP のわずか外側(0.25N 未満)。
  VWAP が SL の内側(建値寄り)に 0.25N 以上入っていれば SL は既に VWAP の外側なので触らない。
  VWAP が SL の外側 1N より遠ければ触らない(構造 SL を VWAP のために大きく広げない)。
- 採点の**前**に置き換える。targets(ラダー)・R・60pt 上限(RISK_CAP_EXCEEDED)・ノイズ床下限・
  `setup_identity`(= decisionId)はすべて最終の SL で決まる。VP80 の targets もここで組む
  (以前は候補化の前に組んでいたので、SL が動くと古い SL の R が公開されるところだった)。
- SL が広がって TP1 の R が 1.5 を割ると、ラダーは次の適格レベルを TP1 にする(§1 の実例では
  29,070.25 → 29,083.25)。それでも足りなければ `TARGET_HEADROOM_INSUFFICIENT` で WATCH。
- モード: OFF(記録も残さない)/ SHADOW(候補 `vwapStop` に「動かした場合」を記録、SL は
  そのまま)/ LIVE(置換 + 記録専用タグ `VWAP_STOP_CLEARED`)。監査は decision と評価カードの
  `vwapStop`(compact)に残る。「評価して動かさなかった」は `reason`
  (`VWAP_FAR_FROM_STOP` / `STOP_ALREADY_BEYOND_VWAP` / `VWAP_MISSING` / `MODEL_NOT_IN_POLICY`)で
  「入力が無かった」と区別する。
- VWAP はバンドル `vwap`(tv_snapshot の Session VWAP、取引日開始 = ET 18:00 アンカー)。
  無ければ `snapshot.vwap`。無ければ動かさない(推測で補わない)。
- SL が変わると decisionId が変わる。**建玉・未約定指値が無いときに切り替える**(R88 と同じ)。
  2026-09-15 03:27 の切り替え時点は FLAT・primary なし。

### 2.2 穴 2 — 成行の SL 距離ゲート(`stop_logic.market_stop_guard`)

`dist = 発注時点の価格 → SL`。`dist < minN·N`(既定 1.0N)なら成行を出さない。

- **engine**(`autotrade_engine._market_stop_guard`): `_entry_order_type` が MARKET と決めた直後、
  **claim の前**に公開価格(`bundle.price` = `--last` と同じ値)とバンドルのノイズ床
  (`evaluation.volGate.noise`、無ければ確定足 12 本の中央値)で判定する。見送りは HALT でも
  claim でもない —— 次周期は価格が変われば普通に再評価される(価格が建値を越えれば指値に戻り、
  その距離は予定どおり)。台帳には `ENTRY_GUARD_SKIPPED`(`action=ENTRY_GUARD`、`plan` を持たない)
  を同じ key・同じ理由につき 1 回だけ書く。claim の後に見送ると CLAIMED が 900 秒残って次の新規を
  塞ぐ(R52)ので、この位置は動かさない。
- **order.py**(`--min-stop-pt`): engine が通した周期は `--min-stop-pt <1.0N>` を付けて送る。
  order.py は `modify_reference_price`(quote が取れれば送信直前の値、無ければ `--last`)で同じ算術を
  当て、割れば `MARKET_STOP_TOO_CLOSE` で落ちる。ドライランも同じ門を通す。engine の判断から
  送信までの数秒で quote が動いた場合だけここで落ち、その周期は claim 後の失敗として ENTRY HALT →
  次周期の不在証明で `ENTRY_RECOVERED`(R52 の既存経路)。`--min-stop-pt` の無い手動送信は従来どおり。
- 測れないときは出さない(fail-closed): 方針が読めない `STOP_LOGIC_UNAVAILABLE`、ノイズ床が無い
  `NOISE_FLOOR_MISSING`、SL が価格の向こう側 `STOP_ON_WRONG_SIDE_OF_PRICE`。
- 指値には掛からない(指値の SL 距離は予定どおり)。追撃(R84 `_command_for_pyramid_entry`)には
  **まだ掛けていない**(§6)。

### 2.3 穴 3 — BREAKER の錨(`stop_logic.flip_origin_index` → `msnr_gate.scan_flip` / `_chain_prices`)

ブレイク足から遡り、直前の足の安値が今の起点の安値より低い限り起点を 1 本前へ(最大
`maxBarsBack` 本、既定 5)。起点〜ブレイク足の極値を `chain.flipExtreme` に載せ、
`_chain_prices` が SWEEP の `sweepExtreme`(R88)と同じ形で SL に使う。OFF では chain にキーを
足さない(出力は R90 以前と同一)。LIVE で SL が動いた候補には記録専用タグ `FLIP_STOP_ORIGIN`。

## 3. 再生(読むだけ)

監査バンドル 1,223 本(2026-08-13〜09-15。同じ HH:MM は翌日に上書きされるので全周期ではない)を
現行コードで再評価。BASE(全部 OFF)の primary は記録済み decisionId と 283/318 一致(残りは
R86/R88/R89 以後のコード差)。成績は R86 と同じ保守的規則(確定 3 分足・指値は 1tick 突き抜け・
約定前 TP1 で取消・同じ足は SL 優先・指値待ち 30 分・6 時間・R は計画の SL 幅)。
`+GUARD` は穴 2 を後掛けし、ゲートが通る最初の周期から建てる(全周期で塞がれば取らない)。

### 3.1 周期単位の変化(BASE 比)

| 変種 | primary の decisionId が変わった周期 | SL が動いた | ARMED→WATCH | 新たに RISK_CAP_EXCEEDED |
|---|---|---|---|---|
| VWAP | 64 | 64 | 9 | 2 |
| FLIP | 191 | 189 | 28 | 49 |
| BOTH | 224 | 222 | 32 | 51 |

### 3.2 成績(3 つの数え方)

| 変種 | セットアップ単位 ΣR(n=124) | 塊の先頭 ΣR(n) | 逐次(1 ポジションずつ)ΣR(取った/約定/TP1) |
|---|---|---|---|
| BASE | +2.12 | +12.24(65) | +7.42(54/36/12) |
| BASE+GUARD | +2.94 | +13.24(65) | +9.71(56/29/11) |
| **VWAP** | +21.63 | +16.59(61) | +11.77(46/34/13) |
| **VWAP+GUARD** | +20.89 | **+17.59**(61) | **+14.06**(48/27/12) |
| FLIP | +15.37 | +16.46(55) | +6.41(46/33/11) |
| FLIP+GUARD | +14.08 | +16.63(55) | +8.78(48/26/10) |
| BOTH | +19.18 | +17.64(54) | +7.41(46/32/11) |
| BOTH+GUARD | +17.17 | +17.80(54) | +9.78(48/25/10) |

読み方:
- **セットアップ単位は同じシグナルを周期ごとに数え直す**(09-14 23:15 / 23:19 / 23:22 / 23:25 の
  BREAKER は SL が 3 分ごとに動くので 4 件)。VWAP の +19R の大半はこの 1 シグナル × 4 で、
  この表だけで判断してはいけない。塊の先頭と逐次が「実際に取れた数」に近い。
- **穴 1(VWAP)は 3 つの数え方すべてで改善**: 塊 +4.4R、逐次 +4.4R、ゲート込みの逐次
  +14.06 vs +9.71。損切り 60 → 51、TP1 25 → 29。
- **穴 2(ゲート)も 3 つすべてで改善**: 塞いだ 14 件のうち 11 件が損切り(−6.45R)、3 件が勝ち
  (+5.63R: 09-11 21:31 +1.47R、09-10 23:37 +0.95R、09-12 00:28 +3.21R)。取り逃しはあるが、
  「まともな SL と近い建値が両立しない場所は入らない」の規則そのもの。
- **穴 3(FLIP)は塊で +4.2R、逐次で −1.0R。BOTH は逐次で VWAP 単独より −4.4R。** FLIP が
  救うのは「錨が遠くなって RISK_CAP_EXCEEDED → WATCH になった負け」(09-14 07:48〜08:00 の 5 件、
  09-14 23:xx の 4 件、09-10 02:22/26 など)で、逐次では空いた枠が別の負けで埋まる。勝ちの
  R も薄まる(09-11 14:25 +5.22R → +3.16R)。§1 の実例は 5 本前の安値 28,880 まで遡って
  SL 121pt になる。**改善とは言えない。実装は残して OFF。**

### 3.3 §1 の実例を各変種で

| 変種 | 判定 | SL | TP1 | 結果(再生) |
|---|---|---|---|---|
| BASE | ARMED B | 28,968.50 | 29,070.25 | −0.84R(成行約定 → 23:39 の安値で損切り) |
| VWAP | ARMED B | **28,950.50** | 29,083.25 | **+3.44R**(安値 28,955.75 を 5.25pt 残して TP1 → runner) |
| BASE+GUARD | ARMED B | 28,968.50 | — | 見送り(28.25pt < 29.88pt) |
| VWAP+GUARD | ARMED B | 28,950.50 | 29,083.25 | 成行 46.25pt ≥ 29.88pt で通り +3.44R |
| FLIP | WATCH(RISK_CAP_EXCEEDED) | 28,850.50 | — | 見送り |

### 3.4 限界

- 期間は 1 か月、ほぼ 1 つの相場環境。塊の先頭で 55〜65 件、逐次で 46〜56 件。
- 3 分足の中の順序は分からない(不利側に倒してある)。ゲートの「quote」は公開価格で代用。
- R86 の押し目深度(VP80 の平行移動)は再生に入れていない(直交。両方 LIVE では VP80 の SL は
  VWAP の外側 0.25N + 0.25N になる)。

## 4. 設計の不変条件

- **失敗は「従来の SL のまま」に倒す**(穴 1・穴 3)、**「出さない」に倒す**(穴 2)。この層の例外で
  監視サイクルは落ちない。契約は節ごとに検証し、壊れた節だけ OFF。
- OFF は R90 以前とバイト一致(監査バンドル 120 本で確認。テストの `OFF:` 系)。
- 穴 1 の置換は採点の前。R・等級・上限・decisionId は最終 SL で決まる(R86 の「判定は元の幾何」とは
  逆の設計で、理由は SL がリスクそのものだから)。
- 穴 2 の engine ゲートは claim の前。order.py のゲートは既存の送信直前検査(R78 の
  `modify_reference_price`)と同じ参照価格。
- order.py は `stop_logic` を import しない(隔離サンドボックスの都合)。`--min-stop-pt` の
  3 行で完結する。

## 5. 切り替えと撤退(事前登録)

- 切り替えは `execution_contract.json` の `stopLogic.<節>.mode` の 1 値。Worker のデプロイ不要。
  SL が変わる節(vwapClearance / flipOrigin)は **FLAT・primary なし**のときに変える。
- 穴 1 の撤退基準: `VWAP_STOP_CLEARED` 付きの実トレードが 30 件たまった時点で、
  (a) スコアカードでタグ付きの PF がタグ無しの同モデルを下回る、または
  (b) `python replay_stop_logic.py` の逐次 ΣR で VWAP+GUARD が BASE+GUARD を下回る、
  のどちらかで `mode` を SHADOW へ戻す。
- 穴 2 の撤退基準: `ENTRY_GUARD_SKIPPED` が塞いだシグナルの追跡で、取り逃した勝ちの合計 R が
  塞いだ負けの合計 R を 30 件時点で上回るとき `minN` を下げる(0.75)。OFF にはしない。
- 穴 3 の有効化: 塊の先頭と逐次の**両方**で BASE を上回る再生が出たとき。または
  `maxBarsBack` を 2〜3 に絞った版で再生し直す(5 本は起点を取りすぎる: §1 の実例で 121pt)。
- 数字の変更(withinN / clearN / minN / maxBarsBack)は新しい version(`R90-STOP-LOGIC-2`)。

## 6. 見つけたが今回は触っていないもの

- **追撃の成行**(`_command_for_pyramid_entry`)に穴 2 のゲートを掛けていない。追撃は影運転
  (`dryRun=true`)で、R84 のロールアウトと一緒に入れる。
- **VWAP の ±1σ バンド**は見ていない。SL がバンドの手前に来るケースは別途数える。
- 穴 3 の起点の定義(単調な切り下がり)は V 字の底まで遡る。「直前のスイング安値まで」に
  変えるなら `flip_origin_index` の 1 関数。
- **R86 との重ね掛け**の実測。VP80 で両方 LIVE のとき SL は VWAP の外側 0.5N になる。
