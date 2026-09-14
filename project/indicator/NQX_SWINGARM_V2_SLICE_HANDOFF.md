# NQX SwingArm Pressure V2 — CT Geometry Slice 0.1 引継ぎ

## 目的

このスライスは、目標画像の主役である動的SwingArm帯の計算核だけを検証する。既存の `nqx_atr_pressure_map_v1.pine` は変更していない。HTF、Pressure Score、4色分類、ラベル、table、alert、JSON、Nightwatch連携は、幾何一致後にのみ追加する。

対象ファイル: `nqx_swingarm_pressure_v2_slice.pine`

## TradingViewでの比較条件

1. MNQ1! の通常ローソク足、15分足を開く。Heikin Ashi、Renko等は使わない。
2. 公開参照のBlackflag FTSを追加し、`Trailtype=modified`、`ATR Period=28`、`ATR Factor=5` にする。
3. 本スライスを新規インジケータとして貼り付け、初期値のまま追加する。`Warm-up bars=0` を維持する。
4. 両者の表示セッション、シンボル、価格調整を同一にする。
5. 本スライスの `Show extremum comparison dots` をONにする。色や透明度ではなく、各バーの数値と反転バーを比較する。

## 合格条件

過去の確定バーについて、参照と次の6系列が最小tick単位で一致すること。

- trail
- extremum
- Fib 61.8
- Fib 78.6
- Fib 88.6
- trend flipのバー

Data Windowで同一バーへカーソルを合わせ、上昇・下落・往復相場を含む最低500バーを確認する。最新の形成中バーは本スライスが意図的に直前確定値を保持するため、参照の暫定値と比較対象にしない。

## 凍結確認

flip直後に表示される半透明boxは旧アームの `Fib 61.8–trail` である。boxの境界はflip後に1tickも動いてはならない。Data Windowの `Frozen Prior *` は旧アーム最終確定バー、すなわちflipバーから見た `[1]` の値でなければならない。

## 不合格時の切り分け順

1. すべての系列が違う: 通常足か、modified、28、5、セッション、シンボルを確認する。
2. ATRから違う: modified true rangeまたはWilder seedの不一致。係数で補正しない。
3. trailだけ途中から違う: ratchet条件の前バー参照を確認する。
4. extremum以降だけ違う: flip判定またはflipバーのhigh/low初期化を確認する。
5. fibだけ違う: `ex + (trail - ex) × ratio` と61.8/78.6/88.6を確認する。
6. 形成中バーだけ違う:仕様どおり。確定後に一致することを確認する。

## 次フェーズへ進むゲート

上記6系列が確定バーで一致するスクリーンショットまたはData Window値が得られるまで、HTF1/HTF2、高圧判定、Score、4色、ラベル、tableを実装しない。見た目を近づけるためにATR factorやfib式を変更して不一致を隠すことは禁止する。

## 静的監査結果

- Pine v6指定
- `request.security()` 0件（CT単独スライスのため）
- alert/JSON/NQX書込み 0件
- boxは最新の凍結旧アーム1件だけ
- 未来経路・未来価格 0件。右延長は凍結済み現在値の水平表示だけ
- 既存 `nqx_atr_pressure_map_v1.pine` は無変更

---

## Phase 2 実装追加 — Geometry Stack 0.2

追加ファイル: `nqx_swingarm_pressure_v2_geometry_stack.pine`

このファイルは、CT-only比較核を変更せずに次の範囲だけを追加した。

- CT / HTF1 / HTF2 の3独立エンジン
- 既定プリセット `CT/2H/4H`（15分チャートでは 15m / 2H / 4H）
- HTF1/HTF2 は tuple 全要素を自TF内で `[1]` し、`lookahead_on` で直前確定HTFバーだけを投影
- 描画順 HTF2 → HTF1 → CT
- 各エンジンの Fib 61.8 / 78.6 / 88.6 / trail と3段fill
- 白系の価格境界、緑/赤のRegular fill、trend色のtrail
- Reduced visual effectsでfillを完全OFF
- Data WindowへCT/HTF trend、armId、sourceTimeを公開

意図的に未実装:

- Pressure Score、確率表示
- 4色の高圧分類、Optimal Entry box
- FrozenZone配列、visit/consumed
- タッチラベル、右端レール、table
- alert、JSON、NQX/Nightwatch書き込み

### 静的監査

- `plot()` 26本
- `fill()` 9本（series colorのため推定plot count合計35）
- 実 `request.security()` 2回
- box / label / alert / JSON 0件
- 既存 `nqx_atr_pressure_map_v1.pine` は無変更

### TradingView実行状態

2026-07-17、Pine Editorへの全文入力とエディタ内構文表示までは確認した。未ログイン状態のTradingViewが `Add to chart` でSign inを要求したため、コンパイル成功とはまだ記録しない。ログイン後に次を実施すること。

1. `nqx_swingarm_pressure_v2_geometry_stack.pine` をPine Editorへ貼る。
2. MNQ1!・通常ローソク・15分・ETH・同一価格調整条件で `Add to chart`。
3. まずCTだけONにし、公開Blackflag FTS（modified / 28 / 5）と trail / ex / f1 / f2 / f3 / flip を500確定バー比較。
4. CT一致後だけHTF1/HTF2をONにし、2H/4Hの値が自TF確定後まで変化しないことを確認。
5. 3本の帯が巨大な静的矩形ではなく、各自TFのratchetに従う階段帯であることを確認。

この実行ゲートを通るまでは、Geometry Stack 0.2へScore/OE/label/alertを追加しない。

---

## TradingView確認後の追加 — Pressure Slice 0.3

ユーザー提供スクリーンショットにより、3エンジンが独立した階段帯として描画され、Pineコンパイルエラーが解消したことを確認した。`nqx_swingarm_pressure_v2_geometry_stack.pine` はタイトルを `Pressure Slice 0.3` へ更新し、次を追加した。

- 3エンジンtupleへ確定足の close / candle direction / EMA structure / relative volume / RSIを追加
- Score: alignment 25 + Fib depth 20 + freshness 15 + structure 15 + volume 10 + proximity 10 + RSI 5
- Scoreは勝率・確率ではない。Data Window名にも `SCORE_NOT_PROBABILITY` を固定
- 高圧化と解除は既定2本の確定チャートバー継続を要求
- 高圧bullは青 `#1565C0`、高圧bearは黄 `#C9A227`
- 高圧時のみ最新OE `[f2, trail]` をbox化し、右方向は現在から5 chart barsに限定
- boxはエンジン毎に最大1、状態解除時にdelete

未実装のまま維持:

- タッチラベル、右端レール、table
- alert、JSON、NQX/Nightwatch書き込み
- FrozenZone履歴とVISIT/CONSUMED

### 0.3静的予算

- `plot()` 32本
- `fill()` 9本、推定plot count合計41
- `request.security()` 2回
- `box.new()` 呼び出し1箇所、実オブジェクト最大3
- label / alert / JSON 0

### 0.3 TradingView確認項目

1. まず既定Gate=70、Confirm/Release=2で追加する。
2. Data Windowの3つのScoreが0..100であり、`%`表示されないこと。
3. 青/黄への切替が1本だけの閾値通過では発生しないこと。
4. OE boxが青bullまたは黄bearの高圧帯にだけ現れ、帯の`f2..trail`内に収まること。
5. HTFのtrail/fib/sourceTimeがHTF形成中に先行更新されないこと。
6. エラーが出た場合は最上段のエラー1件と行番号を記録し、後続エラーを先に直さないこと。

---

## Information Slice 0.4

ユーザー提供の0.3実画面で3層帯とScore系列の稼働を確認後、最新情報を読める表示層を追加した。対象ファイル名は維持し、indicatorタイトルを `Information Slice 0.4` へ更新した。

追加:

- `Rail density = FULL / REDUCED / CAPTURE`
- FULL: 各TFの61.8 / 78.6 / 88.6 / ARMを表示
- REDUCED（既定）: 各TFの88.6 / ARMだけを表示
- CAPTURE: 各TFのARMだけを表示
- レールは最新値だけ。最大12 labelを再利用し、履歴ラベルを生成しない
- x位置は現在から最大15 bars以内、y値は現在の確定fib/trailのみ
- 右上table: TF / STATE / SCORE / TRAIL / DIST/ATR
- 欠損は `MISSING` と `—`、状態行はgray
- table footerへ `SCORE IS NOT PROBABILITY`

禁止事項の維持:

- レールの右配置は未来価格を意味しない。現在確定値のラベル位置だけをずらす
- alert / JSON / NQX/Nightwatch書き込みなし
- Scoreへ`%`を付けない。Fib比率の`%`だけ許可
- Liquid Glass、glass-on-glassなし

### 0.4静的予算

- plot 32 + series-color fill 9 = 推定plot count 41
- request.security 2
- box最大3、label最大12、table 1
- alert / JSON 0

### 0.4確認

1. REDUCEDでレールが6枚（3TF×2）を超えない。
2. CAPTUREでARMレール3枚だけになる。
3. pan/zoom後もレールが最新値へ戻り、過去へ孤児labelを残さない。
4. tableのScoreとData WindowのScoreが一致する。
5. `DIST/ATR=0.00`は価格がband内、正値はband外距離を示す。

---

## Event Slice 0.5

0.4のコンパイル成功確認後、Fib到達を状態ではなく確定イベントとして追加した。

実装:

- 各エンジン自身の確定足で61.8 / 78.6 / 88.6 crossを検出
- long armはcloseのcrossunder、short armはcrossover
- 比較対象は公開FTSと同じ前バーfib `[1]`
- HTFはtuple内でイベントを`[1]`確定し、Projection Managerで`sourceTime`変化時の1回だけ発行
- 形成中HTFバー、同一投影区間、未確定chart barからラベルを生成しない
- 最低Score既定55、同一TF×同一levelの間隔は`Label Spacing × 5 chart bars`
- label FIFO既定120、設定最大130。rail最大12と合わせても`max_labels_count=150`以内
- FULL/REDUCEDで履歴ラベル、CAPTUREまたはShow OFFで履歴labelを全削除
- GradeはSTRONG / GOOD / WEAK / POOR。ExternalTarget未実装のためEXCELLENTは発行しない
- Data WindowへTF別のConfirmed Touch Level（0/1/2/3）

安全条件:

- touch labelはシナリオでも売買指示でもない
- Scoreに`%`を付けない
- alert / JSON / NQX writeは依然0
- HTFイベントをchart barごとに複製しない

### 0.5静的予算

- plot 35 + series-color fill 9 = 推定plot count 44
- request.security 2
- box最大3、touch label最大130、rail label最大12、table 1
- alert / JSON 0

### 0.5確認

1. 既定Minimum Score=55でGOOD以上だけが出る。
2. 1本で複数levelを通過した場合も、各labelのyが各fib価格に一致する。
3. 2H/4H touchが投影区間中に繰り返し発行されない。
4. CAPTUREへ切り替えると履歴touch labelが消え、ARM railとtableだけ残る。
5. Data WindowのTouch Levelはイベントバーだけ1/2/3、それ以外0。

---

## LTF Slice 0.6 — 3m / 15m / 45m

ユーザー指定により、既定の3エンジン階層を `15m / 2H / 4H` から
`3m LTF / 15m CT / 45m HTF` へ変更した。対象ファイル名は維持し、
indicatorタイトルを `LTF Slice 0.6` へ更新した。

実装:

- 既定プリセット `3m/15m/45m` は15分チャート専用。異なるchart TFではruntime errorで停止する
- MANUALでは `LTF <= chart TF <= HTF` を必須とし、既定LTF=3m、HTF=45m
- 3mは `request.security_lower_tf()` で全intrabarを時刻順arrayとして取得
- 3m geometry/stateは `time_close <= timenow` を満たす最後の実intrabar値を採用し、空array時だけ直前値を保持
- 3m flip/touchは15mバー内の確定済みintrabarだけを集約し、途中で発生した確定イベントを捨てない
- 45mは従来どおりtuple全要素を自TFで`[1]`し、`lookahead_on`で直前確定値だけを投影
- 描画順を `45m HTF -> 15m CT -> 3m LTF` とし、LTF境界を最前面へ配置
- status tableは上から `3m / 15m / 45m`
- latest-value rail labelとFib touch labelは機能を残して既定OFF
- Data Window名を `LTF / CT / HTF` 契約へ更新

重要な表示制約:

- 15分チャートの1本にはplot座標が1つしかないため、5本分の3m階段を15mローソク内部へ捏造描画しない
- 3m帯は各15mバーで取得できた最後の実3m状態を代表値として表示する
- LTF feedはTradingView提供データであり、未来値・補間値・人工OHLCを生成しない
- Scoreは引き続き確率ではない。alert / JSON / NQX/Nightwatch書込みは0
- 既存 `nqx_atr_pressure_map_v1.pine` は無変更

### 0.6静的予算

- plot 35 + series-color fill 9 = 推定plot count 44
- `request.security_lower_tf()` 1回、confirmed HTF `request.security()` 1回
- request tuple合計43要素（LTF 22 + HTF 21、上限127未満）
- box最大3、touch label最大130、rail label最大12、table 1
- labelは両系統とも初期状態で0
- alert / JSON / NQX write 0

### 0.6確認

1. MNQ1!通常足の15分チャートへ追加し、tableが `3m / 15m / 45m` の順になる。
2. 3m・15m・45mのtrail/fibが同一値の複製ではなく、それぞれ独立したarmとして動く。
3. 45mの値が45m形成中に先行更新しない。
4. 右端railと履歴touch labelが初期状態で1枚も出ない。
5. `Show latest-value rail labels` または `Show confirmed Fib touch labels` を明示的にONにした時だけlabelが出る。
6. 3m Data WindowのSource Timeが取得済みintrabarの実時刻であり、15分未来時刻を示さない。
7. Pineエラーが出た場合は最上段の1件と行番号だけを先に修正する。

---

## Frozen Core 0.7

0.6のTradingViewコンパイル成功確認後、Phase 3で未実装だったFrozenZoneの
不変スナップショット核を追加した。visit / deepest / CONSUMEDの更新は0.8へ分離し、
今回は生成・検証・FIFO・expiry・重複抑制だけを実装した。

実装:

- `FrozenZone` UDTをチャート文脈だけに定義。engine/request境界をオブジェクトやarrayが越えない
- engine tupleへflip直前の旧arm `dir / trail / ex / f1 / f2 / f3 / ATR / freezeTime / birthTime` を追加
- flip後の新arm値ではなく、旧arm最終確定バーの各`[1]`をsnapshotへ格納
- 3mは15分バー内の確定済みintrabarを全走査し、複数flipがあれば時刻順に個別保存
- 15mはchart bar確定時のみ保存
- 45mは`[1] + lookahead_on`で投影されたconfirmed flipをfreezeTimeで一度だけ保存
- 保存前にna、ATR、最小幅、最大幅、extremum外れ値を検査。不合格snapshotは生成しない
- 各role最大20件（入力で1..50）。超過は最古からFIFO削除
- expiryは `maxFrozenZones × freshAge` 本の各source TF時間を超えたsnapshotを削除
- geometry価格フィールドは生成後に更新するコード経路を持たない
- table footerへ `FZ L/C/H` 件数、Data Windowへrole別Frozen Countを追加
- LTF/HTFの`scoreAtFreeze`は過去時点のalignmentを正確に再構築できないためna。現在Scoreで捏造しない
- box / label / alert / JSON / NQX writeは追加していない

### 0.7静的予算

- plot 38 + series-color fill 9 = 推定plot count 47
- `request.security_lower_tf()` 1、confirmed HTF `request.security()` 1
- request tuple合計61要素（LTF 31 + HTF 30、上限127未満）
- FrozenZone array 3本、各既定最大20
- box最大3、label両系統は既定OFF、table 1
- alert / JSON / NQX write 0

### 0.7確認

1. 15分チャートへ追加してコンパイルエラーがない。
2. table footerの `FZ L/C/H` がflip発生後に増える。
3. 同じ45m flipが15分バーごとに重複追加されない。
4. 3mで同一15分バー内に複数flipがあれば、それぞれ最大1件として追加される。
5. 各countは設定したMax Frozen Zonesを超えない。
6. 動的帯、Score、OE、既定OFFのlabel挙動が0.6から変わらない。
7. 0.7通過後にのみ0.8のvisit / deepest / CONSUMEDを実装する。

---

## Full Build 1.0 — Frozen Lifecycle / Target / Risk / Export

0.7以降の残作業を `nqx_swingarm_pressure_v2_geometry_stack.pine` へ統合した。
既存 `nqx_atr_pressure_map_v1.pine`、Nightwatch HTML、NQX/1 validatorは変更していない。

実装:

- FrozenZoneを各source TFの実OHLCで更新し、外→内だけを独立VISITとして加算
- deepestLevelを61.8 / 78.6 / 88.6の1 / 2 / 3で単調保持
- 3回目の独立VISITで `CONSUMED`。以降はtarget/rejection候補から除外
- CTは15m確定足、LTFは確定済み3m intrabar列、HTFは新しい確定45m sourceだけで更新
- External Targetは反対方向の確定45m動的帯または未消費Frozen HTF帯から、進行方向で最も近い実価格だけを選択
- Target到達後は同じtarget keyをCT flipまで再利用しない
- InvalidationはCT trail、Hard StopはLongで0.20 ATR下 / Shortで0.20 ATR上
- Risk順序、Target順序、最小R:R（既定1.50）を満たさない状態は `NO TRADE`
- 表へ `MISSING / STALE HTF / INVALIDATED GEOMETRY / NO TARGET / WATCH` を明示
- Riskの右方向長は表示窓であり、未来価格経路ではない
- Confirmed Rejectionは「78.6以深到達済み + 61.8外へ確定回帰 + 同方向candle」だけ
- rejection markerは小三角のみ。rail/touch labelは引き続き既定OFF
- RSI candleは任意機能・既定OFF。14 / 70 / 30、色は入力で変更可能
- CAPTUREではfill、OE、Target、Risk線、全rail/touch labelを非表示
- 1本の `alert()` dispatcherへFresh / Touch / OE / Rejection / Invalidation / Target / Consumed / Structure Changeを優先順で集約
- 情報イベントはplain textのみ。JSONはvalidator互換 `CONFIRMED_REJECTION` だけ
- JSON exportは既定OFF。Risk順序・Target・HTF鮮度・最小R:Rを全通過した時だけ送信
- v1.5 JSON全30フィールドを維持し、`engine / score / grade / oe_active` だけadditive追加
- 全数値はna→null、`score_is_probability:false`、未確定値・NaN・推定価格を出力しない
- Rejection時点でRisk gate不合格なら後から遡ってJSON化しない

### Full Build 1.0 静的監査

- 括弧 `747/747`、角括弧 `155/155`
- `plot` 37、`plotshape` 2、`barcolor` 1、series-color fill 9、series-color plot追加分を含む推定output count 55以下
- `alertcondition` 0、dynamic `alert()` 1
- `request.security_lower_tf()` 1、confirmed HTF `request.security()` 1
- request tuple合計67要素（LTF 34 + HTF 33、上限127未満）
- box実行時最大4（OE 3 + Target 1、宣言上限10）
- line実行時最大1（Hard Stop、宣言上限20）
- label最大142（touch 130 + rail 12、宣言上限150）、既定0
- FrozenZone最大60（role毎20既定）
- JSON field diff: v1.5欠落0、additive 4
- 予約語 `range` 不使用、`NaN` literal不使用

### TradingView最終確認

1. MNQ1!通常足15mでPine v6コンパイルが0 error。
2. 右上tableが3m / 15m / 45m、最下段がRisk状態。
3. 45m値は45m形成中に変化せず、3本目の15m確定後だけ更新。
4. 同一帯の連続滞在は1 VISIT、離脱後再侵入だけ次VISIT。
5. 3回目VISITでUSED数が増え、同帯からrejection marker/JSONが出ない。
6. LongはHard Stop < Invalidation < Entry < Target、Shortは逆順。
7. Target到達後に同じtarget boxが再出現せず、CT flip後だけ再選択可能。
8. JSON ONでもR:R未達・STALE・MISSING・NO TARGETでは何も送られない。
9. 有効Rejection JSONに`NaN`がなく、v1.5全フィールドとadditive 4フィールドがある。
10. CAPTUREでローソクを覆うfill/box/rail/touch labelが消える。

### Resource-safe patch 1.0.1

TradingView実測でoutput seriesが67/64になったため、コア描画・Target・Risk・STALE・Rejectionを維持したまま、重複診断用Data Window plotを7本削除した。

削除対象:

- CT Arm Age
- LTF Arm ID
- HTF Arm ID
- LTF / CT / HTF Latest Frozen Visits×10+Depth（3本）
- Rejection Scenario Valid（表とJSON gateに同じ状態が残るため重複）

Pineの実測値を基準に `67 - 7 = 約60` output seriesとし、64上限に4枠の余裕を確保した。計算、FrozenZone lifecycle、表、alert、JSONには影響しない。
