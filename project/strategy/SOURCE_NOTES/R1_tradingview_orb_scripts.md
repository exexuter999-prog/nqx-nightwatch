# R1精読ノート: TradingView公開ORB/VWAPスクリプト群（E3、追試素材）

- アクセス日: 2026-07-18 / アクセス方法: WebSearchスニペット集約。スクリプトのソースコード自体は未取得（TradingView上でのpine_get_source等はR2フェーズでMCP接続後に実施予定）。

## 1. "ORB - Opening Range Breakout Backtest" by tkey1
- URL: https://www.tradingview.com/script/DOhV0uXT/
- 内容: ORB + 20日EMAトレンドフィルタ + 15:55 ET強制手仕舞い。
- 報告値 [SRC・作者報告]: ショート側19取引・勝率63.2%・PF 2.34（NQが下降トレンドだった検証期間でのEMAフィルタ適用結果）。ロング側はEMAフィルタで13→4取引に減少。
- 評価: 作者報告値であり第三者未検証。単一トレンド期間（下降相場）に偏った検証である可能性が高く、他のレジームでの頑健性は不明。

## 2. "MNQ ORB Strategy - VWAP + Bias" by dbmeyers
- URL: https://www.tradingview.com/script/khcR5SPp-MNQ-ORB-Strategy-VWAP-Bias/
- 内容: **対象市場がMNQそのもの**。設定可能なORB窓、tickベースのTP/SL、VWAP方向フィルタ、オーバーナイト高安プロット。1分足チャート向け。VWAPはRTHオープン(9:30 ET)でリセット。
- 実測: 本セッションでは未実施（設計書通り「実測はこれから」の状態）。R3以降でTradingView MCPにより自分で実測することが望ましい追試素材。

## 3. "15-Min ORB Strategy for NQ (w/ 5-min Confirmation)" by eduardomartinspalmbeach
- URL: https://www.tradingview.com/script/slULs42t-15-Min-ORB-Strategy-for-NQ-w-5-min-Confirmation/
- 内容: 15分ORB + 5分足MTF確認。設計書引用の勝率65-78%・PF2.0超は作者主張であり本セッションで再確認できていない。

## NQX統合適性
- いずれもE3（コミュニティ実装、成績は作者報告）であり、単独では宣言根拠にならない（設計書2章の格付け規則）。**追試素材**としてR3-R4のTradingView MCP実測フェーズで、コード構造の参考にする（ロジックの模倣であり逐語コピーはしない）。

## 格付け判定
**E3（確定。定義上これ以上の格上げはできない — 成績が作者報告のみのため）**。
