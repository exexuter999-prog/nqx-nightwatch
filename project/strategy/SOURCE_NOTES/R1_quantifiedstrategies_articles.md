# R1精読ノート: QuantifiedStrategies.com 各記事（4記事まとめ）

- アクセス日: 2026-07-18 / アクセス方法: WebSearchスニペット集約。**WebFetchによる直接アクセスはBot Verificationページが返され失敗（BLOCKED-SOURCE）**。archive.org経由の代替も本セッションのWebFetchツールでは接続不可（"unable to fetch from web.archive.org"エラー）。全て二次スニペットからの引用であり、著者の原文全体・グラフ・メソドロジー詳細は未読。

## 1. "Nasdaq Trading Strategies" — IBS戦略（NQ先物）
- URL: https://www.quantifiedstrategies.com/nasdaq-trading-strategies/
- 市場: NQ先物1枚、2006年〜検証時点まで。
- ルール: IBS = (close-low)/(high-low) ≤ 0.1 かつ 月曜または火曜 → 買い。エグジット = 前日高値超えの初回引け。
- 報告指標 [SRC]: 86取引、平均利益$600/枚、**PF 4.13**。
- 注意: **小標本（86取引）**。設計書J-5の取引数≥200基準を満たさない。統計条件（95%CI下限>1.2）を通すまで宣言不可（設計書の既定注意と一致）。

## 2. "RSI Mean Reversion Trading Strategy" — QQQ
- URL: https://www.quantifiedstrategies.com/rsi-mean-reversion-trading-strategy/
- 市場: QQQ。
- 報告指標 [SRC]: CAGR 12.7%（対B&H 9%）、市場滞在14%、232取引、勝率75%、平均利益2.4%/平均損失2.1%、**PF 3.0**、最大DD 19.5%、Sharpe 2.85。
- ルール詳細（RSI具体的閾値・期間）: **未確認（未読）**。

## 3. "Nasdaq 100 E-mini" — セッション/オーバーナイト効果
- 検索で直接の記事到達には至らず、設計書記載の要約（S&P500利益の大半が1993年以降オーバーナイトセッション由来）を裏付ける一次スニペットは本セッションで確認できなかった。**BLOCKED-SOURCE、格付け維持**。

## 4. "Opening Range Breakout Strategy" — ORB backtest
- URL: https://www.quantifiedstrategies.com/opening-range-breakout-strategy/
- 報告指標 [SRC]: 198取引、平均利益0.27%、勝率65%。PF値: 個別のスニペットでは確認できず（設計書引用のPF≈2.0は本セッションでは再確認できていない）。
- 著者自身の言及: 「ORB戦略の効果は経年劣化している。S&P500系銘柄では単純なORBブレイクアウトはもはや一貫した利益を生まない」という趣旨の記述を複数の要約から確認。**エッジ減衰の自己申告という誠実な姿勢**は設計書の評価通り。

## 総評（自己評価）
- 4記事とも**Bot Verificationにより原文全体には到達できず**、断片的な二次スニペットの集約に留まる。特にIBSのPF 4.13、RSI Mean ReversionのPF 3.0は、ルールの正確な定義（IBS計算の丸め・RSI期間・エグジット条件の全詳細）を検証できていないため、**再現実装時に前提の食い違いが生じるリスクが高い**。
- 次回セッションでのアクセス改善案: Google キャッシュ経由の取得を試す、または設計書が提案する代替経路(archive.orgスナップショット)をWebFetch以外のツール（Bash+curl等、ネットワークアクセス権限があれば）で試す。

## 格付け判定
**E2(暫定)を維持**（全記事、原文完読ならず）。
