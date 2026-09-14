# Repo2 追加戦略アドエンダム

`Downloads/Repo2.zip` の画像群を既存の `IMAGE_STRATEGY_CATALOG/1` に追加統合した。教材画像はエッジの証明ではなく、現在のスナップショットから確認できた場合だけ候補の方向・順位・根拠・チャート表示へ反映する。

## 追加したモデル

- **IFVG**: 反転後の FVG。`lo/hi/direction/active/freshness` を保存し、価格がゾーンへ再侵入したときだけ `CONFIRMED`。
- **OB / BRK / MB / RJB**: Order Block、Breaker Block、Mitigation Block、Rejection Block を共通のゾーン配列として正規化。価格タッチがないものは `OBSERVED` 止まり。
- **Fib SD**: Fib Standard Deviation の -1/-2/-2.5/-3/-4/-5、0、1 等の水準を保持し、価格タッチを `ALIGNED` として記録。
- **Fib CRT**: CRT の sweep→close-back-inside と Fib リトレースの同時成立を複合モデル化。
- **XAMD / Quarterly Theory**: X→A→M→D または A→M→D→X の四半期シーケンス、現在 stage、bias、target を保存。
- **Midnight Open / Sessions**: Midnight Open を目標値として保存し、Asia のレンジ構築→London の liquidity sweep/BOS→NY の delivery というセッション文脈を保持。

## シナリオへの反映

1. `strategy_models.build_strategy_matrix()` が上記モデルを同じ snapshot/bars から生成する。
2. `msnr_gate` は各モデルの `direction` と `evidence` を候補のスコアへ加点・減点し、`strategyMatrix` として decision/scenario に保存する。
3. `monitor_publish` はそのまま matrix を Telegram payload に渡し、既存の CVD、A+、イベント、SL、R:R、ONE-PASS の hard gate は変更しない。
4. 欠落データは `MISSING/WATCH/OBSERVE` として表示し、未観測モデルを根拠に発注しない。

## チャート表示

Telegram TradingView 風チャートには、IFVG、OB/BRK/MB/RJB ゾーン、Fib SD 水準、Midnight Open、Asia High/Low を追加オーバーレイする。未確認ゾーンは薄く表示し、`CONFIRMED` のみ濃く表示する。

