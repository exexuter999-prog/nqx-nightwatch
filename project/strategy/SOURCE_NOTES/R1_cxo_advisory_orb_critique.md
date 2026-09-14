# R1精読ノート: CXO Advisory ORB批判記事群

- 書誌: cxoadvisory.com / 関連URL: https://www.cxoadvisory.com/technical-trading/day-trading-with-an-opening-range-breakout-strategy/, https://www.cxoadvisory.com/volatility-effects/testing-a-complex-breakout-indicator/
- アクセス日: 2026-07-18 / アクセス方法: WebSearchスニペット集約。記事本文未読。

## 内容の要旨
- CXO Advisory記事（2023年4月、QQQ 5分ORB検証）: 最初の5分足でQQQが上昇/下落したら、次の5分足の開始時点でその方向にエントリーするというシンプルなORB検証を実施。
- 過剰適合への一般的警告: 「数十通りの窓幅を検証なしに試すと過剰適合になる。30分ORBが最も一般的だが、最適な設定は市場依存でロバスト性検証が必要」「テスト仕様の変更・制約追加は異なる結果を生むが、データスヌーピングバイアスを招く」。
- 「ORBはもはやあまり機能しない。デイリーフィルタの追加は推奨するが、ORB戦略を過剰適合させるべきではない」との趣旨。

## NQX統合適性・本設計への拘束
- 設計書4章の反証チェックリストとして使用。J-6の多重検定防御（試行台帳・パラメータ数上限3個）の実務的裏付けとなる。

## 格付け判定
**E2反証(暫定・要旨のみ)**。
