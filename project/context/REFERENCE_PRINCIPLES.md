# Reference Principles

調査基準日: 2026-07-15

## Apple Liquid Glass

Appleの公式WWDCセッションでは、Liquid Glassはコンテンツではなく、コンテンツの上に浮くナビゲーション／操作層へ限定することが推奨されている。テーブルなどのコンテンツ層にGlassを使うと階層が濁り、glass-on-glassは混乱を招く。また、Tintは主操作の強調へ選択的に使い、すべてを着色しない。Reduced Transparency、Increased Contrast、Reduced Motionも考慮する。

Nightwatchへの適用:

- Chart、Level Board、データ表は不透明または低透過のcontent surface
- Glassはタブ、選択シナリオ、Primary action、短時間のinteraction feedbackへ限定
- 泡、常時虹色、レンズ歪み、Chart全面の屈折は削除候補
- Rest stateは静かにし、操作時だけ光・変形を短く使う
- Glassを重ねない

Primary source:

- [Apple Developer — Meet Liquid Glass, WWDC25](https://developer.apple.com/videos/play/wwdc2025/219/)
- [Apple Developer — Liquid Glass overview](https://developer.apple.com/documentation/technologyoverviews/liquid-glass)

## TradingView Mobile Principles

TradingViewの公式ドキュメントでは、モバイルは画面サイズに応じて要素をリサイズ／非表示にし、一部の右ペインや下部機能を別導線へ移す。Long pressによる詳細表示、single/multi-touch、pinch scaleなど、モバイル固有の操作を定義している。またモバイルでは表示できるprice scaleが1つに制限される。

Nightwatchへの適用:

- デスクトップ3ペインを390pxへ押し込まない
- 主要Chartを残し、Receiver / Mission ControlはSheetまたはタブへ退避
- Long pressを価格詳細・crosshairに割り当てる
- 1つの価格軸へPrimary情報を集約
- 右側ラベルとScenario railが競合しない構造にする

Primary source:

- [TradingView Advanced Charts — Mobile app development](https://www.tradingview.com/charting-library-docs/latest/mobile_specifics/)
- [TradingView Advanced Charts — Styles](https://www.tradingview.com/charting-library-docs/latest/customization/styles/)

## Interpretation Rule

外部製品の見た目をコピーしない。公式原則をNightwatchのMarket Truth、Level、Riskへ翻訳する。

