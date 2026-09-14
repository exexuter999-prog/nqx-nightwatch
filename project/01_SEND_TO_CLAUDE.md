# Claudeへ送る本文

以下をそのままClaudeへ送信してください。

---

あなたは、プロ向け金融ターミナル、チャートUX、情報設計、レスポンシブUI、デザインシステム、フロントエンド保守性に精通したDesign Principalです。

添付したNQ Nightwatchを、現在の見た目に遠慮せず根本から監査してください。ただし、NQXエンジンと安全監査は壊してはいけません。

今回は完成コードを実装しないでください。あなたの仕事は、3案のデザインコンペを実施し、勝者を選び、別担当のCodexがそのまま実装できる設計書へ完成させることです。

最初に `00_MISSION.md` から指定順で資料を読み、参考画像を実際に比較してください。特に `01-mobile-glass-failure.jpeg`、`02-mobile-missing-price-error.jpeg`、`09-nightwatch-vs-tradingview.png`、`10-result-review.png` を必ず視覚監査してください。

## 作業フェーズ

### Phase A — Evidence Audit

- 現行HTMLのDOM、CSS世代、描画関数、モバイル構成を監査する。
- 参考画像ごとに、良い点、問題、残す情報、消す演出を特定する。
- 「情報不足」と「デザイン不足」を混同しない。
- 問題を症状ではなく原因まで遡る。

### Phase B — Three Competing Directions

明確に異なる3案を作成してください。

1. Institutional Clarity — 最も静かで速く読める案
2. Precision Glass — Appleの原則に沿い、ガラスを操作層だけへ限定する案
3. Radical Data-First — 現行配置に縛られず情報設計を再編する案

色違いだけの3案は禁止です。レイアウト、情報階層、チャート周辺、モバイル導線が異なる必要があります。

### Phase C — Blind Scoring

`04_SCORECARD.md` を使い、各案を同じ基準で採点してください。好き嫌いではなく、根拠と減点理由を書いてください。

- 合格は85点以上
- 各10点尺度項目で7点未満が1つでもあれば不合格
- 最高点が85未満なら、最上位案を修正して再採点
- 最終的に採用するのは1案だけ

### Phase D — Winner Specification

勝者について、1440px、1024px、768px、390pxの各画面を設計してください。

必ず数値化するもの:

- ペイン幅、最小幅、最大幅
- ヘッダー高、ツールバー高、ボトムバー高
- 余白、文字サイズ、行高、角丸、境界、影
- 色トークン
- 常時表示、段階的開示、隠す情報
- Chart、Level、Scenario、Riskの描画順
- タップ領域、フォーカス、モーション
- 欠損、STALE、ERROR、NO TRADE、INVALIDATEDの表示

### Phase E — Codex Implementation Blueprint

- 現行セレクタと関数を `KEEP / MERGE / REWRITE / REMOVE` に分類する。
- V2〜V14のCSS上書き群をどの順で撤去・統合するか書く。
- DOM移動で影響するJavaScript関数を列挙する。
- 実装を小さなフェーズへ分割し、各段階に回帰テストと完了条件を置く。
- 抽象語ではなく、Codexが変更箇所を特定できる指示にする。

## 絶対条件

- Market Truth First
- Levels Before Routes
- No Synthetic Future
- State-Driven Rendering
- Risk Is Geometry
- 読めない価格を捏造しない
- 推定価格には `~`
- NQX/1、検証、バージョン拒否、R:R、Invalidation、イベント、排他を維持
- 無効生成物で既存分析を上書きしない
- Liquid Glassをチャートやデータ表のコンテンツ層へ使わない
- glass-on-glassを行わない
- 色付けは主操作または重大状態へ限定する
- 視覚効果OFF、Reduced Motion、Reduced Transparency相当を設計する
- モバイルはデスクトップの縮小版にしない

## 禁止

- 完成HTMLの全文出力
- CSSの全面出力
- コードを書き始めて設計判断を省略すること
- 「モダン」「洗練」「見やすい」だけの曖昧な提案
- 複数案を並べたまま結論を出さないこと
- 現行の派手さを少し弱めただけの案
- BloombergやTradingViewの表面的コピー
- 実データがない情報をUIへ追加すること

## 最終出力

回答は日本語で、`03_DELIVERABLE_CONTRACT.md` の章立てに完全準拠した `../docs/architecture/CLAUDE_NIGHTWATCH_DESIGN_BLUEPRINT.md` だけを返してください。

設計書を読んだCodexが追加質問なしで実装を開始できる精度まで仕上げてください。

---
