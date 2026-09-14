# ../docs/architecture/CLAUDE_NIGHTWATCH_DESIGN_BLUEPRINT.md

Claudeの最終回答は以下の章立てへ固定する。

## 1. Brutal Evidence Audit

- コード起因の問題
- 視覚起因の問題
- 情報不足起因の問題
- 各参考画像の具体的所見
- 今すぐ削るべき要素10件

## 2. Three Design Directions

各案について:

- コンセプト
- 1440pxワイヤーフレーム
- 390pxワイヤーフレーム
- 情報階層
- Visual language
- Glass boundary
- 削除対象
- 利点、リスク、実装コスト

## 3. Scorecard and Winner

- 3案の採点表
- 減点理由
- 不合格項目
- 勝者
- 85点未満の場合の改訂と再採点
- 最終決定を一文で宣言

## 4. North-Star Screen Definition

- 1440px
- 1024px
- 768px
- 390px

各幅で、領域、寸法、表示情報、折りたたみ、操作を指定。

## 5. Visual Tokens

表形式:

`Token | Exact value | Usage | State | Contrast reason`

色、文字、余白、角丸、境界、影、Glass、モーションを含む。

## 6. Information Priority Matrix

表形式:

`Information | Priority 1-4 | Desktop | Mobile | Missing state | Source`

価格、レベル、Posture、Scenario、Risk、Audit、Uplinkを扱う。

## 7. Component-Level Specification

対象:

- `#z1`
- `#mtabs`
- `#z2`
- `#receiver`
- `#uplink`
- `#chartbar`
- `#postureBar`
- `#marketStrip`
- `#levelBoard`
- `#chartwrap`
- `#scpanel`
- `#z4`
- `#mrail`
- `#mbar`
- `#z5`

各対象に `Purpose / Keep / Remove / New hierarchy / Dimensions / States / Selectors` を記載。

## 8. Chart / Level / Scenario Rendering

- 描画z-order
- 軸範囲
- Level clustering
- Primary / Secondary / Context
- ラベル衝突
- 画面外マーカー
- WATCH〜INVALIDATED
- Entry / Invalidation / SL / TP1
- OHLC欠損
- Observed / Estimated / Simulated

## 9. Liquid Glass Boundary

- 使用箇所
- 禁止箇所
- Rest / Hover / Press / Focus
- Reduced transparency
- Reduced motion
- 削除する現行FXセレクタ

## 10. CSS Demolition and Rebuild Map

V2、V4、V5、V8、V9、V10、V11、V12、V14ごとに:

`Block | KEEP/MERGE/REWRITE/REMOVE | Exact selectors | Destination layer | Risk`

最終CSS層を Tokens / Base / Layout / Components / Chart / States / Responsive / Optional FX に統合。

## 11. DOM and JavaScript Impact

`DOM/ID | Change | Affected functions | Migration | Regression test`

`renderAll`、`renderReceiver`、`renderChart`、`renderCards`、`buildTruthChart`、`bindEvents`、`runUplink` を必須確認。

## 12. Codex Implementation Sequence

フェーズごとに:

- 目的
- 変更対象
- 作業
- 完了条件
- 回帰テスト
- Rollback point

## 13. Acceptance Tests

検証可能なチェックボックス。1440 / 1024 / 768 / 390、各状態、キーボード、Touch、Reduced Motion、NQX valid/invalidを含む。

## 14. Final Codex Directive

Codexへ渡す実装指示を400〜700字で記載。
