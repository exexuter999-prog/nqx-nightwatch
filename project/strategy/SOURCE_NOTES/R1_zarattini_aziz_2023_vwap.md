# R1精読ノート: Zarattini & Aziz (2023) "Volume Weighted Average Price (VWAP): The Holy Grail for Day Trading Systems?"

- 書誌: Carlo Zarattini, Andrew Aziz / 2023-11-13 / SSRN 4631351 / https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4631351
- アクセス日: 2026-07-18 / アクセス方法: WebSearchスニペット集約のみ。SSRN本体未読。BLOCKED-SOURCE。

## 検証市場・期間
- QQQ・TQQQ。2018-01-02〜2023-09-28（弱気相場2回・高ボラティリティイベント複数を含む）。

## ルール定義（要旨）
- 価格がVWAPより上ならロングのみ、下ならショートのみという方向バイアスフィルタ（"VWAP Trend Trading"という名称の戦略）。エントリー・エグジットの詳細トリガーは未確認。

## 報告指標 [SRC]
- QQQでの検証: 初期投資$25,000が$192,656に成長（手数料控除後net）、671%リターン、最大DD 9.4%、Sharpe比率2.1。
- 取引数・勝率・PF: **未確認（未読）**。

## コスト処理
「net of commissions」の記述のみ。スリッページモデル不明。

## 方法論の弱点
- QQQ/TQQQでの検証であり、MNQ先物への直接移植は未証明。同一著者グループ(Zarattini系)のORB論文と同一方法論体系である可能性が高く、**設計書の格付け補助規則「同一著者グループの複数論文は独立証拠として二重計上しない」に従い、Zarattini系全体を1系統としてカウントする**。

## NQX統合適性
- 設計書5-3 M3（VWAP Bias/Pullback）の学術根拠候補。Biasフィルタ形（価格>VWAPでlongのみ）は本論文の骨子と一致。

## 格付け判定
**E1(暫定・原文未読)**。Zarattini系論文群として同一著者バイアスに留意。
