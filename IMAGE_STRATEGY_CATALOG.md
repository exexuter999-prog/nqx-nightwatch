# 画像戦略カタログとNightwatch実装契約

この文書は `Downloads/Repo.zip` に含まれていたチャート画像を、下位モデルが
同じ言葉・同じ順序で扱えるように言語化したものです。画像はアイデアの資料で
あり、勝率や期待値の証明ではありません。実運用の許可は MSNR の構造連鎖、CVD の鮮度、
イベント、R:R、SL、口座リスク、ONE-PASS の契約が決めます。一方、画像モデルの一致・対立は
候補の順位と証拠へ反映し、従来の「情報だけ取ってシナリオへ反映しない」状態を廃止します。

## 1. MSNR / Malaysian SNR / QT

- MSNR は `Entry System`、`LIT (Liquidity Inducement)`、`QT + SMT` の三層で読む。
- HTF の未緩和レベルを **Fresh**、一度以上反応・消化したレベルを **Unfresh** とする。
  Freshness は時間足ごとに別管理し、同じ価格を何度も Fresh と再利用しない。
- QML、SNR、RBS（抵抗が支持へ反転）、SBR（支持が抵抗へ反転）、OCL を重要価格として
  記録する。トレンドライン、OB、FVG、流動性プールとの重なりを confluence として残す。
- HTF の rejection wick は、支持・抵抗・流動性を掃除した後、ヒゲの 50% 付近を
  LTF の再テスト候補にする。単独のヒゲではエントリーにしない。
- 標準連鎖は `level touch -> liquidity sweep -> displacement -> MSS -> retest hold`。
  画像の「高精度エントリー」は HTF パターンを先に決め、5 分足等の MSS/flip を待つ。

## 2. ICT / SMT / FVG / OTE / DOL

- 先に HTF の dealing range を固定し、`rangeTf / rangeStart / rangeEnd / anchorType /
  freshness` を保存する。ローリング 3 分足だけで OTE を作らない。
- Premium/Discount の EQ を中心に、買いは Discount、売りは Premium の OTE
  (おおむね 0.62--0.79) を候補帯とする。範囲が Fresh/Active でない場合は観測に戻す。
- FVG は時間足、生成時刻、年齢、impulse body/displacement、到達前の構造維持、未充填
  状態を記録する。未充填であっても単独トリガーにはしない。
- IRL/ERL、BSL/SSL、EQH/EQL を流動性マップに置き、次の DOL を target/roadblock として
  シナリオへ渡す。Judas Swing は一方向の偽ブレイクから反対方向への delivery として記録する。
- SMT は NQ と ES/YM の**同時刻・同一セッション・同じ鮮度**のスイング比較だけを有効とする。
  CVD 欠落の代替にはしない。
- Killzone は時間帯のラベルであり、時間帯だけでグレードを上げない。

## 3. MMXM / MMBM / MMSM

- MMXM はマーケットメーカーの買いモデル/売りモデルへの遷移を読む設計図。
- Buy model: consolidation → SSL/売り曲線の manipulation → bullish MSS → accumulation/
  re-accumulation → markup → BSL/DOL。
- Sell model: consolidation → BSL/買い曲線の pump/FOMO → bearish MSS → distribution/
  re-distribution → markdown → SSL/DOL。
- OB、FVG、BB/MB/PB、PD array はエントリー候補であり、raid と MSS が揃う前に front-run しない。

## 4. Wyckoff / AMD / Power of Three

- Wyckoff の phase は Accumulation → Markup → Distribution → Markdown。
- SC、Spring、低出来高テスト、Jump the Creek、BC、Upthrust/Failed Rally、TWS、
  Break the Ice は「どの段階のイベントか」を証拠として残す。
- AMD/PO3 は Accumulation（レンジと流動性の蓄積）→ Manipulation（偽ブレイク・ストップ狩り）
  → Distribution（本方向への expansion）で構成する。
- 実装は `ACCUMULATION / MANIPULATION / DISTRIBUTION` を返し、raid の側、MSS の方向、
  delivery の方向を別フィールドにする。phase 名だけで注文しない。

## 5. CRT (Candle Range Theory)

- CRT H/L は親 candle または複数 candle のレンジ境界。境界を sweep/TWS した後、レンジ内へ
  close back し、LTF の方向確認を待つ。
- Classic、Inside Bar、Two Candle、Multiple Candle、Two Stage、Double Purge、
  Continuation、Recursive CRT を同じ契約で記録する。
- `rangeHigh / rangeLow / sweep / closeBackInside / stage / failureCondition` を保存する。
  内側の CRT は外側のレンジを壊したら無効。単なるヒゲだけでは valid にしない。

## 6. VWAP / EMA / ATR 平均回帰

- VWAP は session anchor から計算した fair value。価格が VWAP から ATR 相当以上に離れ、
  momentum が鈍化したときに平均回帰候補を作る。
- R1/R2 は距離の目安であり、強い trend では VWAP 自体が価格に追随するため、乖離だけで逆張りしない。
- `vwap / distance / atr / ema / momentum / signal` を観測に残す。VWAP path の reclaim/reject
  も既存 MSNR の advisory と同じタイムスタンプで扱う。

## 7. 画像から抽出した共通の実行順

1. 時刻・symbol・確定足・CVD・peer 鮮度を凍結する。
2. HTF range、Freshness、PD array、SNR/QML、IRL/ERL、BSL/SSL、DOL を先に置く。
3. CRT/AMD/MMXM の phase と liquidity raid を分類する。
4. MSS、displacement、FVG/OB、SMT、VWAP path、rejection wick、Fib/OTE を confluence として重ねる。
5. LONG / SHORT / FLAT を同じ証拠で比較し、画像モデルの alignment を候補の順位へ反映する。
6. CVD 欠落・STALE は一度だけ再取得を要求し、その間 A+ を禁止して A までに制限する。
7. SL は構造の外側、TP は DOL/roadblock 順。口座リスクと R:R を通過した A/A+ だけを
   ONE-PASS の最終候補にする。

## 8. コード出力

`strategy_models.build_strategy_matrix()` が `IMAGE_STRATEGY_CATALOG/1` の観測を返す。
`msnr_gate.evaluate()` はそれを `result.strategyMatrix` として保持し、候補の `score`、
`evidence`、`penalties`、`strategyAlignment`、`strategyModels` に接続する。
`monitor_publish` は同じマトリクスを evaluation、scenario、market payload に渡す。
マトリクスの `hardGateImpact` は `RANKING_ONLY`。モデルの一致は候補 score を押し上げ、
対立は penalty を付けるが、口座リスクやCVD制約をすり抜けるハードゲートにはならない。

新しいモデルはシグナル名の表示だけでなく、必ず `evidence` と `failureCondition` を持つ。
未観測は `MISSING / WATCH / OBSERVE` とし、価格や方向を補間しない。
