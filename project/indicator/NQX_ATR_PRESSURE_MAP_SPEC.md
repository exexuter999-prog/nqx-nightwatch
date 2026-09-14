# NQX ATR Pressure Map v1.5 — 実装仕様

## 1. 目的

NQ/MNQ向けに、ATR構造、確定した押し・戻りゾーン、上位時間足の整合、リスク幾何をTradingView上で機械可読に提示する。非公開インジケータのコードや固有式は使用しない。`Pressure Score`は観測済みコンフルエンスの説明用スコアであり、勝率・反発確率・校正済み確率ではない。

## 2. 既定プロファイル

| 項目 | 既定値 |
|---|---:|
| Timeframe profile | NIGHTWATCH AUTO |
| 3分チャート | Structure 15分 / Context 45分 |
| 15分チャート | Structure 45分 / Context 4時間 |
| 45分チャート | Structure 4時間 / Context 8時間 |
| ATR kernel | MODIFIED FTS true range + Wilder smoothing |
| ATR length | 28 |
| ATR trail factor | 3.0 |
| Stale-zone距離 | frozen ATRの6.0倍 |
| Approach距離 | frozen ATRの1.0倍 |
| Stale ghost outline | ON |
| EMA stack | 8 / 21 / 55 |
| Relative-volume baseline | 30本 |
| Retracement | 61.8 / 78.6 / 88.6% |
| Hard-stop buffer | structural invalidation外側へ0.20 ATR |
| Fresh age | 40チャートバー |
| High-pressure gate | 72/100かつ最低2/3時間足整合 |
| Consumed-zone rearm | 消費確定後のextremumから1.0 frozen ATR更新 |
| Layer profile | NIGHTWATCH AUTO |
| Zone history | 直近180 chart bars（`ALL`へ変更可） |

これらはNightwatchの3分・15分・45分ワークフロー用初期値であり、収益性を保証する最適化値ではない。

## 3. 統一Frozen Zoneエンジン

Chart、Structure、Contextはすべて同じゾーンエンジンを使う。

1. 各時間足でmodified true rangeとWilder smoothingによるATR trailing stopを計算する。比較用に`STANDARD ATR`へ切替可能とする。
2. ATR trend flip後、方向別extremumを追跡する。
3. extremumからATR trailまでの61.8%、78.6%、88.6%を候補境界とする。
4. flipバーより後、最初の確定counter-direction candleをpullback開始とする。
5. pullback開始バーの直前に完成したバーの3境界、invalidation、ATRをsnapshotし、`ARMED V0`として固定する。同一バーの安値・高値でzone位置を後付けしない。
6. 固定後はATR trailや新高値・新安値へ追随させない。水平区間として維持する。
7. zone外からzone内への確定再侵入だけを1 `VISIT`として数える。連続滞在バーは加算しない。
8. 3回目VISITで`CONSUMED`を確定する。同じバーではrearmしない。
9. `CONSUMED`確定時extremumを保存し、その後の確定バーで同方向へ1.0 frozen ATR以上更新された場合だけ旧zoneを破棄して再武装待ちへ戻る。
10. trend flipで旧ゾーン、VISITS、consumed snapshotを即時無効化する。

StructureとContextは完成した上位足だけを使用する。実装は全tuple要素を`[1]`へオフセットし、`barmerge.lookahead_on`で取得する。未確定上位足の途中値を現在ゾーンとして表示しない。

## 4. ゾーン状態

- `BUILDING`: trendはあるが確定retracementがまだない。
- `ARMED`: 確定pullbackからzoneは固定済みだが、まだ訪問されていない。
- `FRESH VISIT`: 初回訪問かつfresh age内。
- `WICK TESTED`: wick接触のみ。
- `BODY TESTED`: candle bodyが帯へ侵入。
- `DEEP MITIGATION`: 88.6%境界へ到達。
- `CONSUMED`: 3回以上の独立VISIT。high-pressure判定、rejection、既定描画から除外。
- `STALE`(横断状態): 帯が現在closeからfrozen ATRの`staleZoneAtr`倍を超えて乖離。lifecycleは維持したまま、score加点・high-pressure gate・リスク幾何・既定描画から除外。リスクはライブtrailへフォールバックし、`Show stale-zone ghost outline`有効時は点線ゴーストと`STALE Vn · x.x ATR AWAY`タグだけを残す。価格が復帰すれば自動解除。

Truth Tableの各時間足は、方向に加えて`BUILD / ARMED V0 / ZONE Vn / USED Vn`を表示する。方向状態と有効ゾーンの存在、CTとHTFのVISITSを混同しない。

## 5. Pressure Score

| Component | 最大 | 定義 |
|---|---:|---|
| 時間足方向整合 | 30 | 重複を除いたChart / Structure / Context各10点 |
| Chart zone interaction | 25 | wick 10、body 18、deep 25 |
| EMA structure | 20 | 部分整合10、完全stack 20 |
| Relative directional volume | 10 | 1.0倍で5、1.2倍で10 |
| Freshness | 10 | 状態に応じて段階減点 |
| Confirmed rejection | 5 | 完成バーでのみ加点 |

スコア上限は100。履歴勝率やベイズ確率へ変換しない。UI、Data Window、JSONで`SCORE IS NOT PROBABILITY`または`score_is_probability:false`を必ず伴わせる。`STALE`ゾーンはzone interactionとFreshnessへ加点せず、high-pressure gateにも入らない。

## 6. シグナルとリスク

- Zone visitは`WATCH`イベントであり、エントリー命令ではない。
- Bull rejectionは、Bull frozen zoneで78.6%以深へ到達後、確定陽線が61.8%外側へ回復した場合だけ候補化する。
- Bear rejectionは上記の対称条件。
- Structural Invalidationはゾーン生成時ATR trailで固定する。ゾーンが`STALE`の間はライブATR trailへフォールバックし、価格から数百ポイント離れた凍結値をリスク基準にしない。
- Hard StopはLongでInvalidationより0.20 ATR下、Shortで0.20 ATR上。
- 価格が有効ゾーンへfrozen ATRの`approachAtr`倍以内に接近した確定バーで`NQX zone approach`(WATCH)を、ゾーンがstale化した確定バーで`NQX zone stale`を発火できる。いずれもエントリー命令ではない。
- NQX側のLevel、Entry、Invalidation、Hard SL、TP、R:R、Event、排他、安全監査を通過するまでシナリオへ昇格しない。

## 7. 描画契約

- Chart zone: 白い61.8 / 78.6 / 88.6境界と抑制した状態色のfill。
- Chart zoneのfill色はBull=緑、Bear=赤としてzone存続中は固定する。Pressure Scoreの変動で履歴fillを青／黄へ再着色しない。
- Fill透過は`ARMED`最明→`DEEP MITIGATION`最暗の一方向ladderで、zone寿命内で単調に減光する（v1.3縦縞の再発構造なし）。61.8側から88.6側へ密度が上がる縦グラデーションを併用し、色相は固定のまま変えない。
- 価格が`approachAtr`以内へ接近した間だけ、zone境界の下にzone状態色のhalo(太線)を重ねる。high-pressure色は使わない。
- `STALE`ゾーンは既定でfill・境界・ラベルを消し、`Show stale-zone ghost outline`有効時のみ点線ゴーストboxと`STALE Vn · x.x ATR AWAY`タグを現在バーまで表示する（右方向への未来延長はしない）。
- High-pressure状態色は現在ラベル、Truth Table header、確定markerだけに使う。
- Structure zone: 確定・有効時だけ水平区間。動くstep cloudは禁止。
- Context zone: 確定・有効時だけ水平区間。動くstep cloudは禁止。
- ATR structural trailは白線として別表示し、zone境界と意味を混ぜない。
- `Outline-only capture mode`ではfillを消し、ローソクのbody/wickを隠さない。
- `NIGHTWATCH AUTO`は3分でCT+Structure fill、15分以上でCT fillとHTF outlineを既定とする。`FULL MAP / CT FOCUS / OUTLINE CAPTURE`へ切替可能。
- `RECENT`は描画だけを直近180 chart barsへ限定する。Data Window、state、JSON計算は全履歴を維持する。
- 未来ローソク、未来経路、予測ターゲット線、右方向への人工延長を描かない。

## 8. Nightwatch取り込み契約

- 23個の`NQX_DATA_*` plotsでChart / Structure / Contextのzone、trail、score、VISITS、stale flags(CT/Structure/Context)、CTのzone距離(ATR倍)、rearm進捗をData Windowへ出す。
- Confirmed rejectionのみ`NQX_ATR_PRESSURE/1` JSONを送れる。
- v1.5 JSONは`atr_kernel`、CT / Structure / Context VISITS、`zone_stale`を出し、非アクティブな上位足ゾーン値を`null`として`NaN`を出さない。再侵入回数の正本フィールドは`visit_count`とし、意味の異なる`touch_count`は出さない。
- 無効JSON、未確認バー、読めない価格は既存NQX分析を上書きしない。
- 推定価格はNightwatch側で`~`を付ける。インジケータ自身は観測・計算できる値だけを出す。

## 9. 画像取得プロファイル

1. `3M TRIGGER`: candles、Chart zone、Truth Table。
2. `15M CLEAN`: zone表示OFF、白線level、candles。
3. `15M STRUCTURE`: Outline-onlyでChart / Structure zone。
4. `45M CONTEXT`: HTF levelとcandles。
5. `METADATA`: 設定値とData Window。

同一時刻、同一price scale、Capture IDを使用する。同一時間足の重複画像は役割を明記する。

## 10. 受け入れ条件

- 15分チャートで45分・4時間帯が各時間足の確定pullback後に`ARMED V0`として出現する。
- Chart、Structure、Contextの全帯が生成後に水平で、ATR trailへ階段追随しない。
- 上位足は未確定バーで位置・状態を変更しない。
- trend flipで該当時間足のzone、VISITS、consumed snapshot、lifecycleがresetされる。
- 連続zone滞在は1 VISIT、離脱後の再侵入だけが次VISITとなる。
- 3回目VISITのバーでは必ず`CONSUMED`が成立し、その後の新extremum 1 frozen ATR更新前にrearmしない。
- 同じ時間足を複数roleへ指定しても方向スコアを重複加点しない。
- `CONSUMED`は既定非表示で、rejection markerとJSONを発生させない。
- Longは`Hard Stop < Invalidation`、Shortは`Hard Stop > Invalidation`。
- スコアは100を超えず、確率表現をしない。
- Zone visitだけではconfirmed rejection marker / JSONを発生させない。
- JSONに`NaN`、synthetic future、未確定HTF値を含めない。
- 価格から`staleZoneAtr`倍超に乖離したゾーンはscore・gate・リスク・既定描画から外れ、Truth Tableに`STALE Vn`として表示される。
- `STALE`中のINV/STOPはライブtrail基準となり、Long/Shortのリスク順序を維持する。
- 価格がゾーンへ復帰したらstaleは自動解除され、ゾーンは凍結位置のまま通常表示・通常scoreへ戻る。
- 出力series総数(plot / fill / plotshape / alertcondition / series color含む)はTradingViewの上限64以内に収める。
- TradingView上でPine v6 compileを通し、15分・45分・4時間の履歴とリアルタイム境界更新を目視確認する。
