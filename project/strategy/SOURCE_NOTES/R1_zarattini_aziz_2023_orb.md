# R1精読ノート: Zarattini & Aziz (2023) "Can Day Trading Really Be Profitable?"

- 書誌: "Can Day Trading Really Be Profitable? Evidence of Sustainable Long-term Profits from Opening Range Breakout (ORB) Day Trading Strategy vs. Benchmark in the US Stock Market" / Carlo Zarattini, Andrew Aziz / 2023-04-10 / SSRN 4416622 / https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4416622
- アクセス日: 2026-07-18 / アクセス方法: WebSearch（複数スニペット集約）+ WebFetch試行（SSRN本体は403でBLOCKED）。**フルテキストPDFへの到達は失敗**。以下はSSRNアブストラクトページ・therobusttrader.com・concretumgroup.com（著者自身のブログ）からのスニペット集約であり、原論文の図表を直接見ていない。

## 検証市場・期間・データ解像度
- QQQ（ナスダック100 ETF）、レバレッジ版としてTQQQも使用。
- 期間: 2016年〜2023年（Volmageddon 2018、COVID弱気相場2020を含む）。
- データ解像度: 5分足（寄り付き5分レンジ）。記載なし: ザラ場外(ETH)の扱い。

## ルール定義（逐語抽出できず、スニペットの要約）
- ORB窓 = 寄り付き最初の5分足。高値/安値のブレイクでその方向にエントリー。
- ストップ = 初動足の逆側端（複数の二次資料が一致）。
- 感度分析: 「日足ATR14の5%」をタイトストップに使うのが最適という記述あり（tightストップ+EOD手仕舞いの組み合わせ）。→ 設計書J-3のATR 0.10×とは異なる可能性がある。**要再確認（フルテキスト未読のため数値の食い違いの真偽は未確定）**。
- EOD（引け）手仕舞い。

## 報告指標 [SRC]（出典間で数値の食い違いあり、要フルテキスト確認）
- QQQ 5分ORB: 総リターン675%（2023年時点）、年率アルファ33%（手数料控除後）、Sharpe比率1.12（1件の二次資料のみ言及、他は未確認）。
- TQQQ（レバレッジ）: 累積+1,484%、同期間QQQ B&H+169%。
- 取引数・勝率: **記載を発見できず（未読）**。
- 最大DD: **未読**。
- PF: **未読。原論文で報告されているか自体が未確認**。

## コスト処理
- 「手数料控除後(net of commissions)」との記述複数あり。スリッページの扱いは明記した二次資料を見つけられず。

## 方法論の弱点（自己評価）
- 対象がQQQ/TQQQというETF（株価指数の非先物・レバレッジ商品）であり、**MNQ先物への移植は別途検証が必要**（設計書の指摘通り）。
- 二次資料間で数値の細部（Sharpe 1.12等）が一致しない箇所があり、一次資料未読の状態でこれらを確定値として扱うのは危険。
- ORB窓5分・ストップ「初動足逆側」または「ATR5%」のどちらが基準形か、二次資料だけでは確定できない。

## NQX統合適性
- 設計書5-3 M1（ORB）モジュールの基準形候補。ただし基準形の正確なパラメータ（ストップ方式）はR2実装前に再確認が必要。

## 格付け判定
**E1（学術・SSRN掲載）だが、当方は原文フルテキストを未読** → 台帳では「E1(暫定・原文未読)」として扱う。GPT指示書のE2暫定と同様の留保を適用。BLOCKED-SOURCE理由: SSRNダウンロードページがWebFetchで403 Forbidden。代替経路（Google Scholarキャッシュ、ResearchGate、著者サイト）も本セッションでは302/405/コンテンツ欠如で完読に至らず。
