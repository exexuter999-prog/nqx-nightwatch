# Current Source Map

対象: `app/nq-nightwatch-nqx-final.html`

## 1. 全体構成

| Zone | Selector | Role |
|---|---|---|
| Command | `#z1` | ブランド、現在値、状態、Confidence、操作 |
| Mobile tabs | `#mtabs` | INTEL / OPERATIONS MAP / MISSION CONTROL |
| Input | `#z2` | NQX入力、コンパイル、Receiver、AI Uplink |
| Chart | `#z3` | フィルタ、姿勢、Market Strip、Level Board、Chart、詳細 |
| Mission | `#z4` | シナリオカード、編集、監査 |
| Status | `#z5` | ログ、時計、エラー |
| Mobile scenario rail | `#mrail` | シナリオ選択 |
| Mobile action bar | `#mbar` | Compile / Filter / Capture |

## 2. Chart領域

- `#chartbar`: Operations filter、Intel layers、手動現在値、Tactical View
- `#postureBar`: 今の行動と次の30分
- `#marketStrip`: Chart Mode、Current Price、Nearest Above/Below、Location、Freshness
- `#levelBoard`: 宣言済みレベルと現在値からの距離
- `#chartwrap`: SVGチャート、Liquid Glass FX、tooltip、boot overlay
- `#scpanel`: 選択シナリオ詳細

## 3. Liquid Glass関連

`#chartwrap` 内に次の装飾層がある。

- `#liquidGlassFx`
- `#lgSpectrum`
- `#lgCaustics`
- `#lgLens`
- `#lgRim`
- `#lgBubbles`
- `#lgRippleLayer`
- `#holoCursor`
- `.chartTelemetry`
- `.holoCorner`

設計レビューでは、チャートを覆う装飾を基本的に削減し、残す場合は選択・フォーカス・一時的フィードバックへ限定する。

## 4. CSS履歴層

現行CSSには少なくとも次の世代別上書きが存在する。

- V2 Visual Core
- V4 Cryogenic Glass
- V5 Desktop Skin
- V8 Risk-gated receiver
- V9 Arctic Liquid Glass
- V10 Glass Refinement
- V11 Spectral Liquid Metal Glass
- V12 Data First
- V14 Market Truth UI

後方の定義が前方を上書きするため、意図の追跡と保守が難しい。レビューでは単純な追加上書きではなく、最終的な8層構成への統合案を必須とする。

## 5. 主要描画・操作関数

| Function | Role | Design impact |
|---|---|---|
| `buildChart` | 旧チャート構築 | 使用経路を確認し、不要なら隔離・削除候補 |
| `buildTruthChart` | Market Truthチャート構築 | Chart/Level設計の中心 |
| `renderAll` | 全体再描画 | DOM再配置時の影響大 |
| `renderReceiver` | NQX受信状態 | Receiverの情報階層変更で影響 |
| `renderChart` | Chart更新 | コンテナ・サイズ変更で影響 |
| `renderCards` | Mission Controlカード | Card再設計で影響 |
| `bindEvents` | イベント接続 | ID変更、DOM移動時の確認必須 |
| `runUplink` | AI Uplink実行 | 表示再配置は可、意味変更は不可 |
| `upPromptFast` | FAST NQX契約 | デザイン変更だけでは触らない |

## 6. 保護対象エンジン

HTML内のPure Sectionには、NQXパース、検証、価格計算、R:R、Invalidation順序、イベント、排他、Confidence、Grade、チャート幾何が含まれる。

デザイン変更だけを理由に次を変更しない。

- NQX version gate
- `validateNQX`
- `parseNQX`
- `parseAnalysis`
- required scenario fields
- controlled code sets
- quant gate
- price order / R:R / invalidation audit
- event handling audit
- mutual exclusion audit

## 7. 現在のUplink

- FAST: compact prompt、最大2,200 tokens、75秒、画像長辺1,280px JPEG 0.86
- DEEP: full audit、最大3,400 tokens、120秒、画像長辺1,600px JPEG 0.92
- Claude API / OpenAI API
- 生成後にローカル `validateNQX`
- エラー生成物は既存分析へ登録しない

## 8. デザイン変更の安全策

1. IDを維持してDOM配置とCSSを変える。
2. ID変更が必要なら、全参照箇所とイベントを影響表へ記載する。
3. 表示情報を削除する前に、同じ情報が別の常時表示位置に存在するか確認する。
4. CSSを追加上書きせず、統合後の所有ブロックを決める。
5. Market Truth、Level、Scenario、Riskの表示優先度を固定する。

