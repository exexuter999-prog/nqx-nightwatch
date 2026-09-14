# R43 インジケータ入力

## 2: OB / Breaker / IFVG

第一候補はTradingViewのopen-source `PD Arrays` by mickes。
OB、FVG、IFVG、Breaker、Mitigation、CHoCH/BOSを一つの指標で確認でき、retest/breakout
alertも持つため、Nightwatchの`blocks / ifvg`契約に最も近い。

ただし作者自身がzoneはchart timeframeのもの、MTF表示は不安定と説明しているため、
HTFの正本にはしない。画面では3分又は15分のzone providerとして使い、45m/1h/4h/1Dは
Nightwatchが確定OHLCから独立に判定する。

Breakerだけをさらに厳密に確認したい場合の第二候補はopen-source
`Breaker Block Engine [AGPro Series]`。ATR displacement、close-based validation、
retest/hold/lost lifecycleが明示されるが、IFVG/OB全体を一つで賄えない。

- [PD Arrays](https://www.tradingview.com/script/UbMpGKJz-PD-Arrays/)
- [Breaker Block Engine](https://www.tradingview.com/script/g5r0ZPaS-Breaker-Block-Engine-AGPro-Series/)

インジケータが無い／読めないサイクルでも、R43はIFVGを確定3分OHLCから導出する。
provider zoneがある場合はproviderを優先し、OHLC fallbackとの二重得票はしない。
