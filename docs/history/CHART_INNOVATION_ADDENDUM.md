# NQ Nightwatch — Chart Innovation Addendum

本書は `docs/architecture/CLAUDE_NIGHTWATCH_DESIGN_BLUEPRINT.md` を上書きせず、勝者「Data-First Decision Column」に追加するチャート仕様である。NQX/1、安全監査、Market Truth First、No Synthetic Futureを優先し、参考画像の表面的コピーは禁止する。

## 1. 参考画像から抽出する本質

- `01-15m-level-rays-full.png`: Asia High/Low、London High/Low、New York High/Low、Weekly Mid、POC/VAH/VAL、Pivot等が、成立時点または計測窓の終端から右へ伸びる白い水平レイとして描かれる。価格と時間の両方に履歴がある。
- `02-15m-level-rays-clean.png`: 白線がローソクの背面で連続し、白文字が線上に直接載るため、別表を見ずに「どの価格構造か」が読める。
- `03-level-ray-hierarchy-crop.png`: 月次・週次・日次・セッション・Pivot・Profileが同居する。良い点は文脈の一体化、悪い点は29,730〜29,770付近のラベル衝突と、遠方レベルによる軸圧縮。

## 2. 採用する革新: Temporal Level Rays

`buildTruthChart`へ、宣言済みレベルを「価格だけの水平線」ではなく「起点・存続期間・鮮度を持つLevel Ray」として描く層を追加する。

### 2.1 Rayのデータ条件

1. 価格は既存NQX `Z` record、観測OHLC、または既存Market modelからのみ取得する。
2. 推定価格は必ず `≈`、NQX内では `~`。読めない価格を生成しない。
3. 起点時刻がデータに存在する場合のみ、そのx座標から右へ伸ばす。
4. 起点時刻がないが実OHLCに同価格の形成barを特定できる場合は、そのbarを推定起点とし、ラベルへ `≈ORIGIN` を付ける。
5. 起点を決められない場合は時間を捏造せず、viewport左端から細いContext lineとして描く。Ray風の途中開始はしない。
6. 未来側へ伸びるのは「既知レベルの参照線」であり、価格経路・到達予測・矢印を描かない。

### 2.2 Ray hierarchy

| Class | 例 | Stroke | Label | Z-order |
|---|---|---|---|---|
| Primary | 現在値に最も近い上下、Active Scenarioに使用中、複数根拠cluster | `rgba(244,248,252,.88)` 1.4px | 11px semibold、価格＋名称＋距離 | OHLC直下 |
| Secondary | Session High/Low、Weekly Mid、POC/VAH/VAL、Pivot | `rgba(232,238,244,.58)` 1px | 10px、名称＋価格 | Primary下 |
| Context | Monthly/Weekly遠方、viewport端付近 | `rgba(210,220,230,.28)` 1px | 原則非表示、端マーカーのみ | 最背面 |
| Critical | Invalidation、Hard SL | `--danger` 1.5px | 状態語を必ず併記 | 最前面 |

- 白は装飾色ではなく「観測・宣言済み構造」の共通言語として使う。
- LONG/SHORT色はローソク、現在状態、重大状態に限定。通常Level Rayを虹色化しない。
- レイ先端は右ラベルレールの手前で止め、価格軸を横断しない。
- hover/focus時のみ同一clusterのレイを100% opacityへ。rest時は静止。

### 2.3 ラベル衝突と密度

- `layoutLabels`を使い、ラベルの最小縦間隔を14pxとする。
- 6pt以内または `clusterGap` 内のレベルは1クラスタへ統合する。
- 表示は `名称1 / 名称2 · price · Δpoints`、最大2行。4件以上は `+N`。
- 右レールに収まらないラベルはleader lineで退避し、ローソク上へ重ねない。
- viewport外は `▲ 4 ABOVE` / `▼ 3 BELOW` の集約マーカー。遠方レベルを軸範囲へ含めない。

## 3. White-Text Intelligenceからシナリオ候補へ

白文字をOCR結果として直接トレード判断へ使ってはいけない。ラベル名と価格がNQX/Market modelに存在し、観測OHLCでパターンが成立した場合だけ `Scenario Candidate` を生成する。

### 3.1 許可する候補パターン

1. **Level Rejection**: Primary Rayをwickで越え、15分足終値が元側へ戻る。次barで否定されていない。
2. **Sweep & Reclaim**: Session High/LowまたはWeekly High/Lowを一度越え、終値でreclaim。Entryはreclaim確認後のみ。
3. **Break / Hold / Retest**: Rayを終値でbreakし、後続barが同Rayを反対側から保持。単一barのbreakだけでは候補化しない。
4. **Confluence Compression**: 6pt以内に2種類以上の独立レベルがclusterし、その境界でrejection/reclaimが成立。
5. **Value Migration Reaction**: POC/VAH/VALの実データがあり、価格が値域外→再侵入または値域内→acceptanceを示す。Profile値が欠損なら無効。

### 3.2 候補生成ゲート

- 実OHLCが3本未満なら自動候補を作らない。Level Map表示のみ。
- `MISSING`、`STALE > 15m`、timeframe不明、価格推定のみの場合は `WATCH CANDIDATE` まで。EXECUTE/ARMEDへ昇格しない。
- Entry、Invalidation、Hard SL、TP1、R:R、valid-until、event handling、mutual exclusionが揃わなければ保存不可。
- LONGは `SL < Invalidation < Entry`、SHORTは逆順を既存auditで検証。
- `validateNQX`合格前は既存分析へcommitしない。無効候補は既存シナリオを上書きしない。
- 同じEntry zoneが既存シナリオと重なる場合は新規追加せず、`CONFLICT`として比較表示する。
- 生成根拠を `Evidence: level name + price + bar timestamp + pattern` で残す。

### 3.3 UI

- Chart上に常時ルートを描かない。成立したbarの下/上に小さな中立マーカーだけ置く。
- クリックすると右のDecision Columnに `CANDIDATE`カードを一時表示。
- カード順: Pattern / Evidence / Entry / Invalidation / Hard SL / TP1 / R:R / Valid until / Blocking reason。
- `ADD TO SCENARIOS`は全監査PASS時のみ有効。部分データでは `REVIEW MISSING FIELDS`。
- 自動追加は禁止。ユーザー確認後にのみ既存シナリオ配列へ登録する。

## 4. 15分足North-Star

- 15分足を構造判断の主画面とする。OHLCは直近64〜96本。
- 現在値周辺のPrimary clusterを画面高の中央55%へ保つ。
- Temporal Level Raysはローソクの背面、Decision Zoneより前面。
- 上端にはNearest Above、下端にはNearest Belowを常時固定。
- `STRUCTURE`トグル: `PRIMARY / ALL / OFF`。既定はPRIMARY。
- Long press/hoverでRayの `Source / Origin / Age / Distance / Scenario use` をtooltip表示。
- 3分足はtrigger確認、45分足はHTF context。両者のレベルを15分足へ持ち込む場合はsource timeframeをラベルに付ける。

## 5. 実装箇所

- 基盤: `buildTruthChart(st,cfg)`のみを使用。旧`buildChart`と`conditionalCandles`を復活させない。
- 新Pure helper候補: `buildTemporalLevelRays(market,bars)`, `classifyRayPriority(ray,now,scenario)`, `matchObservedPattern(bars,rays)`, `buildScenarioCandidate(match,state)`。
- 既存活用: `declaredLevelRows`, `nearestLevelPair`, `rangeModel`, `layoutLabels`, `levelDistanceText`, `fmt`, `validateNQX`, `invalidationOrderAudit`, `mutualExclusionAudit`, `eventHandlingAudit`。
- DOM: Chartに予測経路DOMを追加しない。候補カードは`#z4`内の一時領域`#candidateQueue`。既存`#cards`とscenario dataは監査PASSまで変更しない。
- CSS: Chart content層は不透明。Glass禁止。白レイ用トークンをChart層にのみ追加。

## 6. 完了条件

- 参考画像に近い「白い時間起点付き水平レイ」が15分足で読めるが、未来価格経路に見えない。
- 現在値、最寄り上下、Primary cluster、Active Scenario risk geometryが3秒以内に判読できる。
- 29,730付近のような密集域でもラベル重なりがなく、最大2行・`+N`に集約される。
- Monthly/Weekly遠方レベルが現在値周辺を圧縮しない。
- OHLC欠損、STALE、推定価格のみではシナリオが自動登録されない。
- 候補追加は全NQX監査PASS＋ユーザー確認後のみ。invalid生成物は既存分析を上書きしない。
- 390pxではPrimary Ray最大4本、ラベルはNearest Above/BelowとActive Scenarioのみ。横overflowなし。
- Reduced Motion/Transparencyでも情報欠損なし。

## 7. Claude Codeへの実行命令

まず `docs/architecture/CLAUDE_NIGHTWATCH_DESIGN_BLUEPRINT.md` を正本として読み、次に本Addendumを差分仕様として適用する。3枚のPNGを視覚比較し、白線の「起点から右へ伸びる履歴性」と「白文字の直接ラベル」を再現する。ただし表面的コピー、合成未来ローソク、到達予測線、OCRだけに基づく価格生成は禁止。実装前に対象HTMLの現行DOM/CSS/関数を再確認し、保護対象エンジンへ変更が入らない計画を提示する。その後、小さなフェーズで実装し、各フェーズにNQX valid/invalid、No Synthetic Future、mobile overflow、label collision、scenario candidate gateの回帰テストを置く。
