# R1精読ノート: Quant Macro Substack "Paper Review: An Effective Intraday Momentum Strategy"

- 書誌: quantmacro.substack.com / URL: https://quantmacro.substack.com/p/paper-review-an-effective-intraday
- アクセス日: 2026-07-18 / アクセス方法: WebSearchスニペット集約（記事本文の直接WebFetchは未実施）。

## 内容の要旨
Zarattini, Aziz, Barbon (2024)のSPYイントラデイモメンタム論文への独立批判。要旨は「結果が良すぎる(too good to be true)」という直接的な評価:
1. 執行想定（スプレッド・キュー位置・短期インパクト）が暗黙的すぎる。
2. シグナルが真に予測的か、単なる同時相関(contemporaneous)かの区別が不明瞭。
3. EOD手仕舞い・オープン基準アンカリングは合理的だが中立的でない設計選択であり、情報を捨てコストを増やす可能性がある。
4. 総評: 「プロフィタブルな戦略」というより「イントラデイのシグナル対ノイズを考える枠組み」として読むべき。

## NQX統合適性・本設計への拘束
- 設計書4章がM2（Noise-Band Momentum）採用前に組み込むことを義務付けている反証文献。R4でM2を検証する際、TRIAL_REGISTRYに「執行想定の明示化（スプレッド・キュー位置考慮）」と「シグナルの予測性 vs 同時相関性の検証」を実行前チェック項目として追加すること。

## 格付け判定
**E2反証(暫定・記事本文未読、要旨のみ)**。
