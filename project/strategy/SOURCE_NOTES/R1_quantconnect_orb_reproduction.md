# R1精読ノート: QuantConnect Community "Opening Range Breakout for Stocks in Play" 再現

- 書誌: QuantConnect Research/Forum、複数スレッド / URL: https://www.quantconnect.com/research/18444/opening-range-breakout-for-stocks-in-play/ および関連フォーラムスレッド（quantconnect.com/forum/discussion/19456, 19451等）
- アクセス日: 2026-07-18 / アクセス方法: WebSearchスニペット集約。フォーラム原文の完全な精読はしていないが、**設計書が指摘する「ATRウォームアップ不具合」の詳細を確認できた**。

## 発見内容（教訓としての価値）
- QuantConnectでのPython版実装において、**日足ATRを分足解像度でウォームアップした結果、初回エントリー時のサンプル数が約7,000（過剰）となり、C#版のサンプル数33と対照的だった**。
- 結果として、**Python版のATR値がC#版の約1/3になった**。ATRが小さすぎるとストップが近くなりすぎ、損切りが過度に頻発する不具合を引き起こした。
- これはIdentityDataConsolidatorのfill-forward処理に関する既知の類似バグと同種の問題として言及されている。

## NQX統合適性・本設計への拘束
- **設計書J-7「ウォームアップ監査」の直接的な実例根拠**。GPT指示書5-2は「日足ATR等の上位足指標は上位足系列で計算してから参照する。分足でWilder漸化式を温めない」ことを必須化しており、本事例はその必要性を裏付ける一次証拠となる。
- 既存のnqx_swingarm_pressure_v2_geometry_stack.pineは`request.security`の確定値[1]+lookahead_on規約を採用しており、この規約を`nqx_bt_*.pine`群にも同一適用することで本バグ再発を防止する。

## 格付け判定
**E2教訓（確定）**。バグの実例としての価値は確定しているが、これ自体はPFやエッジの根拠ではなく、実装品質監査のチェックリスト項目として台帳に記録する。
