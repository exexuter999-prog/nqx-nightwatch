# Design Scorecard — 100 Points

| Category | Weight | 10/10 condition |
|---|---:|---|
| Data legibility | 20 | 価格・状態・警告が装飾より常に優先される |
| Price and level hierarchy | 20 | 現在値と上下レベル、距離、クラスタが即読できる |
| Scenario and risk clarity | 15 | Entry→Invalidation→SL→TP1と状態が誤読されない |
| Mobile usability | 15 | 390pxでチャート・状態・操作が破綻せず、横はみ出しなし |
| Visual coherence | 10 | 色、面、文字、境界、Glassが1つの論理で統一される |
| Maintainability | 8 | V2〜V14を統合でき、追加上書きへ依存しない |
| Accessibility | 5 | Contrast、Focus、非色依存、Reduced Motion/Transparency |
| Performance | 4 | 装飾レイヤーと再描画負荷を削減できる |
| Implementation feasibility | 3 | 単一HTMLと既存エンジンを維持して段階実装できる |
| Total | 100 | 85以上、かつ全項目7/10以上 |

## Penalties

- Chart content layerへGlass: -15
- glass-on-glass: -10
- 現在値やレベルより装飾が強い: -15
- モバイルが単なる縮小: -10
- NQXエンジン変更を前提: -20
- 実データのない新指標表示: -20
- 具体的な寸法・セレクタがない: -10
- 複数案の結論なし: 失格

