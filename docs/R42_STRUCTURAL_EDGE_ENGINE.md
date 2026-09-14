# R42 Structural Edge Engine

2026-08-25。利益を保証する改造ではなく、コスト後期待値を測れる状態へ近づけながら、
既知の機会損失と退化SLを同時に除く改訂。

## 外部教材と採用境界

- ICT公式 2022 Mentorship Episode 3: Internal Range Liquidity / Market Structure Shift
  - https://www.youtube.com/watch?v=nQfHZ2DEJ8c
- ICT公式 2022 Mentorship Episode 6: Fair Value Gap / Market Structure Shift
  - https://www.youtube.com/watch?v=Bkt8B3kLATQ
- MNQ OHLCV 947日・14シグナル族のOOS反証研究
  - https://arxiv.org/abs/2605.04004
- Cont/Kukanov/StoikovのOrder Flow Imbalance一次論文
  - https://arxiv.org/abs/1011.6402
- CME MNQ仕様（$2/point、0.25pt tick）
  - https://www.cmegroup.com/markets/equities/nasdaq/micro-e-mini-nasdaq-100.contractSpecs.html

ICT教材はMSS/FVG/内部流動性の定義源として使うが、勝率の証明には使わない。MNQの
OHLCV単独シグナル研究はコスト後の一般化が弱い。OFI研究は板イベントを対象とし、CVDは
OFIではないため同一視しない。よってYouTube型へ全置換せず、既存のVP/ICT/SMT/FVG/
CVD/PO3合議を維持し、入力証明と構造幾何を強化する。

## 実装

1. `acquisitionReceipt`
   - rawごとのSHA-256、mtime、age、size、必須/任意、fresh/staleを保存
   - 必須3分系4ファイルが240秒超ならlive buildをBLOCK
2. range/SMT検証の統一
   - レベルから導出できた確定レンジを`RANGE_ANCHOR_MISSING`と誤表示しない
   - `smtObservation`を正式な取得証跡として扱い、SMT窓外は欠落でなくN/A
3. 構造SL
   - SL緩衝を`0.5 × noiseFloor`から`1.0 × noiseFloor`へ変更
   - 411保存サイクル再生: 候補368→396、ARMED 130→122、SL中央値17.25→30.00pt、最小8.00→15.25pt
   - 候補生成を維持しつつ、狭いSLほどRが膨らむ逆報酬を除去
4. ボラ帯の機械強制
   - `0.40 < ratio <= 0.60`: A+だけを維持し、AはWATCH
   - `ratio > 0.60`: A+を含む全候補をWATCH
5. OOS識別
   - `setupVersion=R42-STRUCTURAL-EDGE/1`へ更新。旧ルールの結果と混ぜない

## 収益性の扱い

411サイクルには実約定結果が無いため、ARMED件数やR:Rを利益へ読み替えない。新versionは
既存のappend-only ledgerへ凍結し、同一entry/SL/exit/costでOOSを集める。昇格条件は
`N>=30 / t>=2.0 / 2pt往復コスト後正 / 複数年正 / パラメータ後付け変更なし`のまま。
