# R1精読ノート: Bailey & López de Prado (2014) "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality"

- 書誌: David H. Bailey, Marcos López de Prado / 2014-07-31 / Journal of Portfolio Management 40(5), 94-107 / SSRN 2460551 / PDF確認: https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf
- アクセス日: 2026-07-18 / アクセス方法: WebSearchスニペット集約。著者本人サイトのPDF直リンクは確認できたが本セッションでは未フェッチ（時間配分の都合）。**要旨レベルの理解に留まる。手法の数式詳細は未確認**。

## 内容の要旨
- Sharpe Ratioの推定は非正規性（歪度・尖度・ボラティリティクラスタリング）によって歪む。DSRはこれを高次モーメントで補正する。
- **選択バイアス（selection bias under multiple testing）**: 多数のバリアントを試して最良のものを選ぶと、全バリアントが純粋なノイズであってもSharpe Ratioが膨張する。これがバックテスト過剰適合の本質。
- DSRは「試行回数」を明示的にモデルに組み込み、观测されたSharpe Ratioを試行回数で割り引く。

## NQX統合適性・本設計への拘束
- 設計書J-6の「試行台帳・DSR的割引」の理論的根拠。GPT指示書TRIAL_REGISTRY.mdの運用（累計試行数nに応じたOOS要求PF引き上げ: n≤10で1.3、11-50で1.4、51+で1.5）は、本論文の正式なDSR計算式の**簡易近似運用**であり、正式なDSR値の計算そのものではない。この近似運用の妥当性は今後Python側(J-6のPBO/CSCV実装時)で正式なDSR式と照合することが望ましい。

## 方法論の弱点（自己評価）
- 本ノートは要旨レベルの理解であり、正式な数式（DSRの導出、最小年数トラックレコード等）は精読できていない。J-6実装前に正式な数式部分の再読が必要。

## 格付け判定
**E1方法論(暫定・要旨のみ)**。数式部分の精読はR2以降のJ-6実装時に再度スケジュールする。
