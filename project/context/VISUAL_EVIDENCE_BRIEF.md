# Visual Evidence Brief

Claudeは画像を装飾参考ではなく、問題の証拠として使う。

## 01-mobile-glass-failure.jpeg

確認事項:

- Chart contentが白いGlassに覆われ、情報が消えている
- Glassの境界とチャートの境界が競合
- モバイル右ペインが画面外に残る
- 操作より装飾面積が大きい

求める回答:

- 削除するFX
- Chart surfaceの最小不透明度
- モバイルのペイン切替

## 02-mobile-missing-price-error.jpeg

確認事項:

- MISSING状態の情報階層
- 空Chartが大面積を占有
- Risk gateメッセージと次の行動の関係
- ブラウザUIを含む実高さ

求める回答:

- Empty stateの再設計
- Primary recovery action
- 高さ・safe-area仕様

## 09-nightwatch-vs-tradingview.png

確認事項:

- 左の疑似経路が右の実価格行動と違う
- 未来経路が予想として見える
- 入力、Chart、Mission Controlが同じ強さ
- 実チャートのレベル情報がNightwatchで弱い

求める回答:

- No Synthetic Futureの画面上の実装
- 実OHLCがない場合のLevel Map mode
- Level / Scenarioのz-order

## 10-result-review.png

確認事項:

- 急騰後の失速と次のレベル
- モメンタム表示と価格構造の優先順位
- 遠いレベルが軸を圧縮する問題

求める回答:

- 自動表示範囲
- 画面外レベルマーカー
- 現在値周辺のPrimary cluster

## 03〜08 chart inputs

確認事項:

- 3M / 15M / 45Mで情報密度が異なる
- セッション高安、週次・日次、Pivot、VPが近接する
- ラベル数が多い

求める回答:

- Cluster rule
- Primary / Secondary / Context
- ラベル内容、最大行数、衝突処理
- 時間足ごとの責務

## Evidence Standard

各所見は次の形式で書く。

`Image → Region → Observed problem → Root cause → Design decision → Acceptance test`

