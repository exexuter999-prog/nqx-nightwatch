# NQ Nightwatch チャート画面 抜本改善計画書

**文書バージョン:** 1.0  
**作成日:** 2026-07-14  
**対象:** `nq-nightwatch-nqx-final.html` の Operations Map / シナリオチャート  
**目的:** 概念的な未来予想図を廃止し、実際の価格行動・構造レベル・条件分岐・リスクを同一座標上で正確に読めるチャートへ再設計する。

---

## 1. 結論

現行画面は、条件付きシナリオを説明するために生成した疑似ローソク足と上昇・下降ルートが、実際の市場状態より強く表示されている。結果として、未発火のロングシナリオが上昇予想に見え、右側の実チャートとの整合性も失われている。

改善後は次の優先順位を固定する。

1. **観測された価格行動**
2. **構造レベルと現在位置**
3. **未成立・成立済み条件**
4. **Entry / Invalidation / Hard SL / Target**
5. **説明用のシナリオ補助表示**

シナリオはチャートを置き換えず、実チャート上に必要な条件だけを重ねる。

---

## 2. 現状の問題

### 2.1 疑似未来ローソク足が予測に見える

`conditionalCandles()` が Entry から TP3 までのローソク足を生成し、WATCH状態でも実際の未来経路のように表示している。条件付き計画であるにもかかわらず、画面上では方向予測として受け取られる。

### 2.2 実チャートと価格行動が同期していない

現在値と数本の観測OHLCだけでは、実チャートのレンジ、押し・戻り、直近スイング、ヒゲ、滞在時間を再現できない。右側のTradingViewと左側のNightwatchで、市場の見え方が別物になる。

### 2.3 遠いTPが表示スケールを支配する

TP2・TP3・遠いSLまで自動フィットするため、現在値周辺の数十ポイントが圧縮される。重要な3分足の反応やレベル間の距離が読みにくい。

### 2.4 レベルが背景情報に留まっている

支持・抵抗、セッション高安、前日・週次水準、プロファイル水準の優先度が、シナリオ経路より弱い。どのレベルを受容・拒否したらシナリオが進むのかが一目で分からない。

### 2.5 シナリオ状態と描画内容が連動していない

WATCH、ARMED、TRIGGERED、CONFIRMED、ACTIVEで表示内容が大きく変わらない。未発火でもEntryや未来経路が強く表示される。

### 2.6 データ不足時の表現が強すぎる

OHLCが `MISSING` でも疑似ローソク足を描けるため、観測データと推定表示の境界が曖昧になる。

### 2.7 ラベルレールが過密

現在値、レベル、Entry、Abort、SL、Hard SL、TP、Threatが同じ優先度で並び、ラベル衝突回避後も読み順が定まらない。

---

## 3. 新しい設計原則

### 原則A — Market Truth First

観測OHLCがある場合は、常に実ローソク足をチャートの主役にする。観測OHLCがない場合はローソク足を生成せず、レベルマップ表示へ切り替える。

### 原則B — No Synthetic Future

未観測の未来ローソク足を通常画面に描かない。シナリオの進行は、価格経路ではなく条件ノードとゾーンで表す。

### 原則C — State-Driven Rendering

シナリオ状態ごとに表示可能な要素を制限する。

| 状態 | 表示するもの | 表示しないもの |
|---|---|---|
| WATCH | Trigger帯、監視レベル、未成立条件 | Entry注文、未来経路、利益帯 |
| ARMED | Trigger、Confirmation、想定Entry帯 | TP経路、約定済み表現 |
| TRIGGERED | Confirmation、Entry、Invalidation | 未来ローソク足 |
| CONFIRMED | Entry、Invalidation、Hard SL、TP | 仮想経路 |
| ACTIVE | 実Fill、現在損益帯、SL、TP | 未約定Entry表現 |
| INVALIDATED | 失効地点と理由を薄く表示 | 有効ルート |

### 原則D — Levels Before Routes

構造レベルを最初に描画し、その上へシナリオ条件を重ねる。レベルがシナリオに隠されないようにする。

### 原則E — Risk Is Geometry

SLは固定ポイント幅ではなく、構造レベルの外側と変動幅バッファで評価する。広すぎる場合はSLを狭めず、サイズ縮小または見送りを提示する。

---

## 4. 改善後の画面構成

### 4.1 メインチャート

- 実OHLCローソク足
- 現在値ラインとデータ時刻
- セッション区分
- 主要構造レベル
- 直近スイング高安
- Decision Zone
- 選択中シナリオの条件オーバーレイ

### 4.2 上部ステータス

- `LIVE / SNAPSHOT / STALE / NO OHLC`
- 現在値
- データ時刻と経過時間
- 現在位置: `BETWEEN AH AND WEEK MID` など
- 最も近い上側・下側レベルと距離
- 現在の行動: WAIT / MONITOR / EXECUTE / STAND DOWN

### 4.3 右側シナリオインスペクター

- 現在状態
- 成立済み条件
- 未成立条件
- 次に必要な価格行動
- Entry
- Invalidation
- Hard SL
- 最初の障害
- TP2・TP3は折りたたみ

### 4.4 レベルレール

全レベルを同じ強さで表示せず、重要度に応じて3段階にする。

- **Primary:** 現在値に近い高時間足・複数根拠重複レベル
- **Secondary:** 次の障害または支持候補
- **Context:** 遠い週次・月次レベル。画面端マーカーのみ

---

## 5. 価格軸と表示範囲

### 5.1 デフォルト表示範囲

実OHLCの直近60〜120本と、現在値に近いPrimary / Secondaryレベルだけで価格軸を決定する。

### 5.2 遠い目標の扱い

TP2・TP3が表示範囲外なら、価格軸を広げず画面上端・下端に方向マーカーを出す。

例: `TP3 29,820 ↑ +177.25pt`

### 5.3 表示モード

- `MARKET`: 実チャート中心。標準モード。
- `SCENARIO`: 選択シナリオの条件を強調。
- `RISK`: Entry / Invalidation / SL / Target間の距離を強調。
- `REPLAY`: 説明用シミュレーション。疑似経路を残す場合はこの隔離モードだけで使用。

---

## 6. レベルエンジン

### 6.1 レベルの入力

- Asia / London / New York High・Low
- Previous Day / Week High・Low・Mid
- Market Open / Midnight
- POC / VAH / VAL
- Pivot / CPR
- RBS / SBR / QML / OCL
- 明示されたDecision Zone

### 6.2 レベル強度

レベル強度は次の情報から算出する。

- 時間足
- 根拠の種類
- 複数レベルの重なり
- Freshness
- Wick / Bodyテスト回数
- 受容・拒否・反転の状態
- 現在値からの距離

表示スコアは予測確率ではなく、チャート上の重要度に限定する。

### 6.3 クラスタリング

近接レベルは一つのゾーンへまとめる。例えば `London High 29,670.5` と `Weekly Mid 29,672.875` は、別々の細線ではなく `29,670.5–29,672.875 resistance cluster` として表示する。

### 6.4 レベルラベル

ラベルには最低限、次を表示する。

`価格 / 名前 / 状態 / 現在値からの距離`

例: `29,672.875 WEEK MID · FRESH · +30.13pt`

---

## 7. シナリオ描画仕様

### 7.1 WATCH

- Trigger帯を半透明表示
- 「あと何ポイント」「必要な足確定」を表示
- Entry / SL / TPはインスペクター内だけに置く
- チャート上に未来経路を描かない

### 7.2 ARMED / TRIGGERED

- Triggerが成立した地点をマーク
- Confirmation帯を表示
- 未成立条件を一行表示
- Entryは点線で表示

### 7.3 CONFIRMED / ACTIVE

- Entryを実線表示
- InvalidationとHard SLを別色で表示
- TP1だけをPrimary表示
- TP2・TP3は画面端マーカーまたは任意表示
- 実Fillがない限り、注文済み表現を使わない

### 7.4 競合シナリオ

ロングとショートを同時に強く描画しない。選択中を100%、競合側を15%にし、相互排他条件だけを残す。

---

## 8. SL・リスク表示

### 8.1 二段階の無効化

- **Invalidation:** シナリオ論理が崩れる価格条件
- **Hard SL:** 注文リスクを強制的に終了する価格

同一線として扱わない。

### 8.2 Hard SL算出

1. シナリオを否定する構造レベルを特定
2. その外縁を決定
3. 実OHLCがあれば直近実効レンジからバッファを算出
4. OHLCがなければレベル間隔ベースの推定であることを表示
5. Entryから遠すぎる場合は `SIZE DOWN / NO TRADE`

### 8.3 表示項目

- EntryからInvalidationまでのポイント
- EntryからHard SLまでのポイント
- MNQ 1枚当たりの概算ドルリスク
- TP1までのR
- 最初の構造障害

コスト・スリッページが不明なら、ドルリスクを確定値として表示しない。

---

## 9. データ契約の改善

### 9.1 必須データ

- Symbol
- Timeframe
- Snapshot timestamp
- Current price
- 主要レベル
- シナリオ条件

### 9.2 実チャート表示に必要なデータ

`O.c` に最低30本、推奨60〜120本のOHLCを渡す。

```text
O|c=TIME,OPEN,HIGH,LOW,CLOSE,VOLUME;...
```

OHLCがない場合は、画面上部に `NO OBSERVED OHLC — LEVEL MAP MODE` を明示する。

### 9.3 鮮度管理

- 0〜5分: CURRENT
- 5〜15分: AGING
- 15分超: STALE
- 時刻不明: UNVERIFIED

STALE時はEXECUTEを無効化する。

---

## 10. コード構成の再設計

### 10.1 分離するモジュール

| モジュール | 責務 |
|---|---|
| `MarketDataModel` | OHLC、現在値、時刻、鮮度 |
| `LevelMapEngine` | レベル正規化、クラスタ、重要度 |
| `ScenarioStateMachine` | WATCHからACTIVEまでの状態遷移 |
| `RiskGeometryEngine` | Invalidation、Hard SL、R、障害判定 |
| `ChartViewport` | 表示価格帯、時間軸、画面端マーカー |
| `ChartRenderer` | 実OHLCとオーバーレイ描画 |

### 10.2 廃止・隔離する処理

- `conditionalCandles()` は通常画面から廃止
- `scenarioPoints()` による未来経路はREPLAYモードへ隔離
- `collectPrices()` は遠いTPで自動スケールしない
- `buildChart()` の単一巨大関数を描画レイヤー単位に分割

### 10.3 新規関数案

```text
buildObservedSeries()
buildLevelClusters()
getMarketViewport()
buildScenarioOverlay()
buildRiskOverlay()
buildOffscreenMarkers()
renderObservedCandles()
renderLevelMap()
renderScenarioConditions()
```

---

## 11. 実装フェーズ

### Phase 0 — 現状固定とテスト基盤

- 現在のHTMLを保存
- 既存NQXデモを回帰テスト化
- Desktop 1200×800、iPhone 390×844の基準画像を作成
- 価格座標変換とラベル配置の単体テストを追加

**完了条件:** 現行挙動を再現でき、変更前後を比較できる。

### Phase 1 — 疑似未来表示の撤去

- WATCH / ARMEDで未来ローソク足を非表示
- 状態別描画マトリクスを実装
- 遠いTPを画面端マーカーへ変更

**完了条件:** 未発火シナリオが上昇・下降予測に見えない。

### Phase 2 — 実OHLCチャート

- 観測OHLCを時間軸付きで描画
- Current / Aging / Stale表示
- OHLC不足時のLevel Map Modeを実装
- 実ローソク足と現在値を価格軸の基準にする

**完了条件:** TradingViewと同じ価格位置関係を再現できる。

### Phase 3 — レベル中心UI

- レベルクラスタリング
- Primary / Secondary / Context分類
- 距離表示
- ラベル衝突処理
- レベル接触・受容・拒否状態を表示

**完了条件:** 選択シナリオを消しても市場構造を読める。

### Phase 4 — シナリオ・リスク統合

- Trigger / Confirmation / Entry / Invalidation / Hard SLの段階表示
- 最初の障害を強調
- STOP GEOMETRYとサイズ縮小判断を連携
- 相互排他シナリオの表示制御

**完了条件:** 現在何が成立し、次に何が必要かを5秒以内に判断できる。

### Phase 5 — モバイル最適化

- モバイルでは右ラベルレールを下部カードへ変更
- 主要3レベルだけ常時表示
- タップで詳細展開
- 横スクロールなし
- SafariでのSVG・フォント・高さ検証

**完了条件:** 390px幅で価格・Primaryレベル・現在条件が重ならない。

### Phase 6 — QAと移行

- NQX解析回帰テスト
- 0 / 1 / 2 / 3シナリオ
- OHLCあり・なし
- Stale・イベント・失効
- Long / Short競合
- SVG / PNG出力
- 旧チャートへ戻せるFeature Flag

**完了条件:** エラー0件、主要ユースケースの視覚差分承認。

---

## 12. 受け入れ基準

1. WATCH状態で未来ローソク足を一切表示しない。
2. 実OHLCの高値・安値・終値が価格軸上で正確に表示される。
3. TradingView側の主要レベルとNightwatch側のY座標関係が一致する。
4. 現在値から最寄り上下レベルまでの距離を表示する。
5. 29,670.5と29,672.875のような近接レベルを一つのクラスタとして表示する。
6. 遠いTP3が現在値付近を圧縮しない。
7. WATCH時はTrigger、CONFIRMED時はEntry / SL / TP1が主役になる。
8. OHLCがない場合、疑似ローソク足を出さずLevel Map Modeになる。
9. 390px幅でラベルがチャートを覆わない。
10. シナリオ選択でチャート寸法と価格軸が不必要に変化しない。
11. `NQX/1` と `!NQX/1` の双方を読み込める。
12. NQX検証エラー0件、ブラウザ例外0件。

---

## 13. 優先順位

### 最優先

1. 疑似未来ローソク足の撤去
2. 実OHLCを主チャート化
3. 価格軸を市場中心に固定
4. 状態別シナリオ表示

### 次点

5. レベルクラスタリング
6. Hard SL / Invalidationの二段表示
7. モバイルラベル設計

### 後続

8. Replayモード
9. 詳細アニメーション
10. 装飾的なLiquid Glass強化

視覚効果は、市場情報と条件表示が完成した後に調整する。

---

## 14. 最終成果物

- 改修済み単一HTML
- 新チャートレンダラー
- NQX互換パーサー
- 状態別シナリオ描画
- 実OHLC / Level Mapの2モード
- Desktop / Mobileの回帰テスト
- テスト用NQXパケット集
- SVG / PNG出力検証

---

## 15. 実装開始時の第一変更

最初のコミットでは、機能を増やさず次の3点だけを行う。

1. WATCH状態の `conditionalCandles()` を停止
2. TP2・TP3を価格軸計算から除外し、画面端マーカー化
3. OHLCがない場合は `LEVEL MAP MODE` と明示

これにより、現在の最大の誤解である「未発火シナリオが未来予測に見える問題」を先に解消する。
