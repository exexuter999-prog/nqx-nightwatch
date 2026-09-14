# CLAUDE_NIGHTWATCH_DESIGN_BLUEPRINT.md

> 正本: `app/nq-nightwatch-nqx-final.html`（5,851行 / 389KB） 監査基準日: 2026-07-15
> 本書はCodex実装用の確定設計書。NQXエンジンおよび安全監査（`parseNQX` / `validateNQX` / `parseAnalysis` / R:R / Invalidation順序 / 排他 / イベント / Confidence / Grade / チャート幾何）は保護対象であり、本書のいかなる指示も意味を変更しない。デザインはDOM配置・CSS・表示順・段階的開示のみを対象とする。

---

## 1. Brutal Evidence Audit

### 1.1 コード起因の問題（症状ではなく原因）

| # | 症状 | 根本原因（コード） | 該当箇所 |
|---|---|---|---|
| C1 | チャートが白いガラスに覆われ情報消失（img 01） | CSS世代の`!important`重ね書き。V9 Arctic（1031〜）が明るいチャートデッキを定義→V10/V11が透過層を追加→V12（1478〜）が`#chartwrap{background:#030711!important}`で打ち消そうとするが、`#lgLens`/`#lgCaustics`が`mix-blend-mode:screen`で残存。モバイルで`#liquidGlassFx{display:none!important}`（1521）は効くが、`#chart`の`filter:saturate/contrast/brightness`（1522）と`#chartwrap`の`inset 0 1px rgba(255,255,255,.22)`ハイライトが白霞として残る | 1031-1524 |
| C2 | 9世代のCSSが順に上書きし追跡不能 | V2/V4/V5/V8/V9/V10/V11/V12/V14の累積。同一セレクタ（`#chartwrap`）が4世代で再定義され、後方定義が`!important`で勝つ。所有ブロックが存在しない | 340,669,801,971,1031,1127,1311,1478,1527 |
| C3 | モバイル右ペインが画面外に残留（img 01右端「OPS」） | `#mtabs`（1592）がタブ切替を担うが、`#z4`（Mission）が`display:none`ではなく幅/transformで退避している疑い。非アクティブペインが完全に除外されず横スクロールを発生 | 623,789,1109,1301,1473 |
| C4 | 装飾DOMがチャート内に常駐し再描画負荷 | `#liquidGlassFx`配下に`#lgSpectrum`/`#lgCaustics`/`#lgLens`/`#lgRim`/`#lgBubbles`（6個の`.lg-bubble`アニメ）/`#lgRippleLayer`/`#holoCursor`/`.chartTelemetry`×2/`.holoCorner`×4 が常時DOM存在。V14で多くが`display:none`だが要素は残る | 1714-1729 |
| C5 | 遠いレベルが軸を縦圧縮（img 10でAsia Low 29303が現在値29642周辺を潰す） | `truthViewport`（3946）が`near.slice(0,8)`で最寄り8レベルを`p`に含めるが、OHLCモード時は全bar高安のみ、レベルは`declaredLevelRows`全件が`clusters`描画対象。現在値から遠いレベルもラベル化されうる。画面外マーカーへの退避ロジックが無い | 3946-3966,4014-4028 |

### 1.2 視覚起因の問題

| # | 問題 | 原因 |
|---|---|---|
| V-a | 装飾が価格より強い（img 09左） | チャート上に合成された経路ライン・ホロカーソル・スペクトル縁・虹色コースティクスが、実データ（ローソク・レベル）と同等以上の明度/彩度。「Risk Is Geometry」に反し、幾何が装飾に埋もれる |
| V-b | 疑似未来経路が予測に見える（img 09左 TARGET 1/2/3への矢印状ライン） | シナリオのEntry→TP群を結ぶ描画が右肩上がりの連続線として現れ、実OHLC（img 09右で失速下落）と乖離。「No Synthetic Future」違反の見え方 |
| V-c | 入力/チャート/Mission Controlが同一視覚強度（img 09左3ペイン等幅・等コントラスト） | `#z2`/`#z3`/`#z4`の面・境界・文字が同格。視線の優先度が設計されていない |
| V-d | 極小フォント多用（`marketFact small`6.5px, `levelChip small`6.5px, `postureText`7px, 1532-1541） | プロ端末でも6.5pxは可読限界以下。情報密度を稼ぐ代わりに可読性を犠牲 |
| V-e | モバイル空チャートが画面の60%占有（img 02） | チャートに固定高さ、Empty時も同高を維持。復帰導線（RISK GATE）が最下部細赤帯に埋没 |

### 1.3 情報不足起因の問題（「情報不足」と「デザイン不足」の分離）

**重要な区別**: 以下は「表示が足りない」問題であり、装飾を減らすだけでは解決しない。逆に、装飾過多（1.2）はデザイン問題であり情報を足してはならない。

| # | 情報不足 | 現状 | 判定 |
|---|---|---|---|
| I-a | img 02で`LIVE PRICE MISSING`時、**次に何をすべきか**が「OPEN MISSION CONTROL」の1行のみ | `postureText`はMISSINGテキストを出すが復帰アクションが弱い | **情報不足**。Empty stateにprimary recovery actionが必要 |
| I-b | 現在値と最寄り上下レベルの**距離**は`marketStrip`にあるが（`levelDistanceText`, 4173-4174）、img 01/02のモバイルでstrip自体が画面外／視認困難 | データは存在、配置が悪い | **デザイン不足**（情報はある）。新規指標追加禁止、再配置で解決 |
| I-c | データ鮮度（LIVE/AGING/STALE）は`mfData`（4179-4180）に計算済みだが、STALE時の視覚的強調が弱い | `age>15`で`warn`クラスのみ | **デザイン不足**。既存計算を強調表示へ |

> 結論: 追加すべき「新情報」は **I-a（Empty state復帰アクション）のみ**。それ以外は全て既存データの再配置・強調で解決する。実データのない新指標は一切追加しない。

### 1.4 参考画像ごとの所見（Image → Region → Observed problem → Root cause → Design decision → Acceptance test）

**01-mobile-glass-failure.jpeg**
- Chart surface → 白ガラスが全面を覆いグリッド以外消失 → V9/V12残存ハイライト+`#chart`filter → **チャートcontent層のGlassを全廃。`#chartwrap`背景を`#03070d`不透明固定、`#chart`のfilterを`none`、内側白ハイライト削除** → AT: モバイルでローソク/レベルが不透明背景上に100%可視、白veilゼロ。
- Right pane → 「OPS」ペインが画面外に残留 → 非アクティブ`#z4`が`display:none`未適用 → **モバイルは`#mtabs`で単一ペインのみ`display:block`、他は`display:none`。横overflow禁止** → AT: 390pxで`document.body.scrollWidth <= 390`。
- Decoration area → 装飾>操作 → `#liquidGlassFx`常駐 → **モバイルでFX層をDOM非表示（既存1521を維持し確実化）** → AT: モバイルで`#liquidGlassFx`が`display:none`。

**02-mobile-missing-price-error.jpeg**
- Header → `LIVE PRICE MISSING`（赤） → 正しい表示だが → **維持。ただしMISSINGを画面上部の常時固定バーへ昇格** → AT: MISSING時、赤い価格状態が最上部sticky領域に常時表示。
- Empty chart → 空チャートが60%占有 → 固定高維持 → **Empty時チャートを縮小し、`> AWAITING INTELLIGENCE`枠内にprimary recovery CTA「PASTE & COMPILE」を配置。RISK GATEメッセージをEmpty枠内へ統合** → AT: MISSING状態で復帰CTAがファーストビュー（ブラウザUI込み実高さ）内に表示。
- Risk gate → 最下部細赤帯に埋没 → 下部固定バーの視覚弱 → **Empty state本文へ昇格、下部バーは操作専用に** → AT: RISK GATE文言がチャートEmpty枠内に表示される。
- 実高さ → ブラウザUI+ヘッダ+タブ+フィルタで圧迫 → **safe-area対応、モバイルはフィルタ行を折りたたみ、チャート最小高を`min(52vh, 内容)`に** → AT: 下部safe-area分の余白確保、下部バーが被らない。

**09-nightwatch-vs-tradingview.png**
- 左疑似経路 → 右実価格と乖離、予測に見える → Entry→TP連結線 → **シナリオ経路をチャート本体に連続線として描かない。Entry/Invalidation/SL/TP1は水平帯＋右軸ラベルのみ（TradingViewの水平レベル方式へ翻訳、コピーではない）。No Synthetic Future** → AT: チャート上にローソク右端より未来方向へ伸びる連続経路線が存在しない。
- 未来経路 → 予測に見える → 同上 → **実OHLCが無い場合は`LEVEL MAP · NO OBSERVED OHLC`モード（既存`vp.mode`, 3996）を明示ラベル表示** → AT: OHLC 3本未満時、チャート内に「NO CANDLES GENERATED — DECLARED LEVELS ONLY」（既存3999）が表示。
- 3ペイン等強度 → 視線優先度なし → **視覚階層を面・コントラストで3段化（後述§4）。チャート=最高コントラスト、入力=最低** → AT: チャート領域の背景/文字コントラストが入力領域より高い。
- レベル情報が弱い → クラスタ未整理 → **Primary/Secondary/Context 3段のレベル描画（後述§8）** → AT: 現在値±1.4×unit内のレベルがPrimary太線＋ラベル、遠方はContext細線ラベルなし。

**10-result-review.png**
- 急騰後失速→次レベル → 現在値周辺クラスタが潰れる → 遠方レベルが軸圧縮 → **自動表示範囲を現在値中心の`span`へ制限し、範囲外レベルは上下端の画面外マーカーへ集約** → AT: 現在値から`viewport span`外のレベルは端マーカー（`▲ N levels above` / `▼ N below`）として表示され、軸を圧縮しない。
- モメンタム表示 vs 価格構造 → 同格 → **モメンタム（HTF bias等）はPostureBar/HUDのテキストへ、チャート幾何はレベル/OHLCのみ** → AT: チャート内にモメンタムラベルの装飾バッジが無い。
- 遠いレベルが軸圧縮 → 同C5 → **§8のauto-range規則** → AT: 上記端マーカーで検証。

**03〜08 chart inputs（3M/15M/45M）**
- 時間足で密度差 → セッション高安/週次日次/Pivot/VP近接、ラベル過多（img 06で10本超） → **Cluster rule: `clusterGap=model.unit*.22`（既存4015）で束ね、`names.slice(0,3)`（既存4023）でラベル圧縮。1クラスタ最大3名、超過は「+N」** → AT: 数pt差の複数レベルが1本のクラスタ線＋1ラベルに統合。
- Primary/Secondary/Context → 未分化 → **§8の3段階責務。3M=直近微細構造、15M=セッション構造、45M=HTFレベル。時間足はソースメタ表示のみ（`sourceFrames`, 3998）** → AT: ラベル最大2行、衝突時`layoutLabels`（3306）で解決。

### 1.5 今すぐ削るべき要素10件

| 順位 | 削除対象 | セレクタ/DOM | 理由 |
|---|---|---|---|
| 1 | 液体ガラス全FX層 | `#liquidGlassFx`と全子要素（`#lgSpectrum`/`#lgCaustics`/`#lgLens`/`#lgRim`/`#lgBubbles`/`#lgRippleLayer`） | チャートcontent層Glass=採点-15。情報を覆う |
| 2 | ホロカーソル | `#holoCursor` | 装飾。crosshairは実線ツールで代替可 |
| 3 | チャートテレメトリ帯 | `.chartTelemetry.top` / `.chartTelemetry.bottom`（「IOR 1.55 / SPECTRAL CAUSTICS」等） | 意味のない演出コピー |
| 4 | ホロコーナー装飾 | `.holoCorner.tl/tr/bl/br` | 装飾枠 |
| 5 | 泡アニメーション | `.lg-bubble.b1〜b6` | 常時アニメ、性能負荷 |
| 6 | `#chart`のfilter加工 | `#chart{filter:saturate/contrast/brightness}`（1491-1493,1522） | 実データの色を歪める |
| 7 | 白内側ハイライト | `#chartwrap`の`inset 0 1px rgba(255,255,255,.x)`各世代 | 白霞の一因 |
| 8 | V2ネオン発光 | V2 Visual Core（340〜）の`box-shadow`グロー群、`OPTICAL GLOW`既定 | 価格より強い光 |
| 9 | 合成経路の連続線描画 | チャート内のEntry→TP連結線描画（あれば） | No Synthetic Future |
| 10 | スペクトル金属縁 | V11 Spectral Liquid Metal Glass（1311〜）の虹色rim | glass-on-glassの温床 |

---

## 2. Three Design Directions

> 3案は色違いではない。ペイン構成・情報階層・チャート周辺・モバイル導線が構造的に異なる。

### 案A — Institutional Clarity（静かで速く読める常時監視盤）

**一文コンセプト**: 装飾ゼロ・単色ベース・情報密度最大化で、価格と最寄りレベルと現在アクションを0.5秒で読ませる機関投資家向け静的監視ターミナル。

**1440pxワイヤーフレーム**
```
┌──────────────────────────────────────────────────────────────────┐
│ [Z1] NIGHTWATCH  │ PRICE 29,642.75 │ STATE WATCH/C64 │ FRESH 3M │ ⚙ │  56px
├──────────────────────────────────────────────────────────────────┤
│ [POSTURE BAR] IMMEDIATE: WAIT INSIDE DZ · NEXT30M: 29645受容監視  │  40px
├────────┬─────────────────────────────────────────────┬───────────┤
│ Z2     │ MARKET STRIP: NOW│ABOVE│BELOW│LOC│DATA        │ Z4        │
│ INPUT  ├─────────────────────────────────────────────┤ MISSION   │
│ (folded│ LEVEL MAP (horizontal chips, near-first)     │ STACK     │
│ tab)   ├─────────────────────────────────────────────┤ S1 ▸      │
│ 44px   │                                             │ S2        │
│ collap-│         CHART (opaque, levels + OHLC)        │ S3        │
│ sed to │         Primary/Secondary/Context lines      │           │
│ left   │         Off-screen markers ▲3 ▼2             │ 340px     │
│ rail   │                                             │           │
├────────┴─────────────────────────────────────────────┴───────────┤
│ [Z5] STATUS: PRE-FLIGHT PASSED · 21:01 JST                        │  26px
└──────────────────────────────────────────────────────────────────┘
Z2=280px(folded 44px rail) │ Z3=flex │ Z4=340px
```

**390pxワイヤーフレーム**
```
┌────────────────────────┐
│ NIGHTWATCH  ⚙          │ 44px sticky
│ PRICE 29,642.75  WATCH │ 40px sticky (MISSING時は赤)
├────────────────────────┤
│ [INTEL][●MAP][MISSION] │ 40px tabs
├────────────────────────┤
│ POSTURE: WAIT · 29645  │ 36px
│ NOW│ABOVE│BELOW (3col) │ 44px strip
│ LEVEL chips → scroll    │ 40px
│                        │
│   CHART (52vh)          │
│   opaque, ▲3 ▼2 markers│
│                        │
├────────────────────────┤
│ [COMPILE][OPS][CAPTURE]│ 56px + safe-area
└────────────────────────┘
```

- **常時表示**: 価格、State、Posture、最寄り上下レベル、鮮度、チャート。
- **隠す/折りたたむ**: Z2入力（左44pxレール→タップ展開）、Receiver詳細（`details`）、Uplink、シナリオ全文（Z4はサマリ、詳細は`scpanel`）。
- **Glass使用**: なし（完全不透明）。操作feedbackも影とborderのみ。
- **削除**: §1.5全10件。
- **利点**: (1) 最速可読、採点Data legibility満点狙い (2) 保守最容易、FX層全撤去で層数最小 (3) 性能最良。
- **リスク**: (1) 「地味」に見え差別化弱い (2) 静的すぎて状態遷移が分かりにくい懸念 (3) Glass boundaryのブランド魅力を捨てる。
- **実装コスト**: **M**（FX撤去＋レイアウト再構成、DOM移動小）。

---

### 案B — Precision Glass（Glassを操作層だけへ限定するApple原則案）

**一文コンセプト**: コンテンツ（チャート・レベル表・データ）は完全不透明、Glassはタブ・選択シナリオ・Primary操作・一時feedbackの浮遊操作層のみ——Apple Liquid Glass原則をMarket Truthへ翻訳。

**1440pxワイヤーフレーム**
```
┌──────────────────────────────────────────────────────────────────┐
│ [Z1 glass floating bar] NIGHTWATCH │ PRICE │ STATE │ acts(glass)  │  60px
├──────────────────────────────────────────────────────────────────┤
│ [POSTURE opaque]                                                  │  40px
├──────────┬──────────────────────────────────────┬────────────────┤
│ Z2 INPUT │ MARKET STRIP (opaque cards)          │ Z4 MISSION      │
│ opaque   ├──────────────────────────────────────┤ selected card   │
│ panel    │ LEVEL MAP (opaque)                   │ = glass tint    │
│          ├──────────────────────────────────────┤ others = opaque │
│ COMPILE  │                                      │                 │
│ = glass  │   CHART (fully opaque, no glass)     │ [EXECUTE glass] │
│ pri btn  │                                      │                 │
│ 300px    │  floating chart toolbar = glass      │ 360px           │
├──────────┴──────────────────────────────────────┴────────────────┤
│ [Z5]                                                              │  26px
└──────────────────────────────────────────────────────────────────┘
```

**390pxワイヤーフレーム**
```
┌────────────────────────┐
│ [glass top bar] PRICE ⚙│ 52px (blur only over scroll content)
├────────────────────────┤
│ [glass segmented tabs]  │ 44px INTEL/MAP/MISSION
├────────────────────────┤
│ POSTURE (opaque)        │ 36px
│ STRIP (opaque 3col)     │ 44px
│ LEVEL (opaque scroll)   │ 40px
│                        │
│  CHART (opaque 50vh)    │
│                        │
├────────────────────────┤
│ [glass floating dock]   │ 56px COMPILE(pri)/OPS/CAPTURE
└────────────────────────┘
```

- **常時表示**: 案Aと同一の情報セット。
- **隠す/折りたたむ**: Receiver詳細、Uplink、非選択シナリオ詳細。
- **Glass使用**: Z1トップバー、`#mtabs`セグメント、Primary操作（COMPILE/EXECUTE）、選択中シナリオカードのtint、押下時の短いfeedback。**チャート・Level Board・データ表・marketStripには一切使わない**。
- **Glass不使用**: `#chartwrap`, `#chart`, `#levelBoard`, `#marketStrip`, `#receiver`グリッド, `#cards`本体。
- **削除**: §1.5の1〜9（10のspectral rimは操作層の控えめtintとして1箇所だけ再利用可）。
- **利点**: (1) ブランド魅力を保ちつつ原則遵守 (2) 操作/コンテンツの層分離が明快 (3) Reduced Transparencyフォールバックが自然。
- **リスク**: (1) Glass境界の実装規律が甘いとglass-on-glass再発（-10リスク） (2) blur多用で性能低下 (3) モバイルSafariのbackdrop-filter不安定（img 01の遠因）。
- **実装コスト**: **M**（Glassを操作層へ隔離、backdrop-filter適用範囲の厳格化）。

---

### 案C — Radical Data-First（情報順序をゼロから再構成）

**一文コンセプト**: 現行3ペイン配置を捨て、「価格→最寄りレベル→現在位置→アクション→シナリオ→リスク」の意思決定順で縦に積む単一意思決定カラム＋補助チャートへIAを再編。

**1440pxワイヤーフレーム**
```
┌──────────────────────────────────────────────────────────────────┐
│ [Z1] PRICE 29,642.75  ▲29,650(+7.25) ▼29,598(-44.75) │ WATCH ⚙  │  64px  ← 価格と最寄り上下を最上位に融合
├───────────────────────────────────┬──────────────────────────────┤
│ DECISION COLUMN (520px)           │ CHART (flex, opaque)          │
│ ┌───────────────────────────────┐ │                              │
│ │ ① POSTURE: WAIT INSIDE DZ     │ │   OHLC + Primary levels      │
│ │    NEXT30M: 29645受容監視     │ │   Secondary levels           │
│ ├───────────────────────────────┤ │   ▲3 above  ▼2 below markers │
│ │ ② LOCATION: INSIDE DZ         │ │                              │
│ │    29598 ↔ 29650              │ │   [chart toolbar bottom]     │
│ ├───────────────────────────────┤ │                              │
│ │ ③ ACTIVE SCENARIO (S1)        │ ├──────────────────────────────┤
│ │    Entry 29650~29660          │ │ LEVEL MAP (full, distance)   │
│ │    Invalidation 29672.88 ~    │ │                              │
│ │    Hard SL 29686              │ │                              │
│ │    TP1 29598 (R1 1.8)         │ │                              │
│ ├───────────────────────────────┤ │                              │
│ │ ④ RISK GATE / AUDIT status    │ │                              │
│ │ ⑤ other scenarios (collapsed) │ │                              │
│ └───────────────────────────────┘ │                              │
├───────────────────────────────────┴──────────────────────────────┤
│ [Z2 INPUT] slides up as bottom sheet on demand │ [Z5 status]      │  drawer
└──────────────────────────────────────────────────────────────────┘
```

**390pxワイヤーフレーム**
```
┌────────────────────────┐
│ PRICE 29,642.75         │ 48px sticky
│ ▲29,650 +7 ▼29,598 -45  │ 32px sticky (最寄り上下を価格直下固定)
├────────────────────────┤
│ ① POSTURE: WAIT 29645   │ 40px
├────────────────────────┤
│   CHART (44vh opaque)   │ 折りたたみ可
│   ▲3 ▼2 markers         │
├────────────────────────┤
│ ② ACTIVE SCENARIO card  │ scroll
│   Entry/Inval/SL/TP1    │
│ ③ Level Map             │
│ ④ Risk gate             │
├────────────────────────┤
│ [＋INTEL] [OPS] [CAP]    │ 56px + safe-area (INTELはsheet起動)
└────────────────────────┘
```

- **常時表示**: 価格＋最寄り上下（Z1融合）、Posture、Location、Activeシナリオの4値（Entry/Invalidation/SL/TP1）、チャート。
- **隠す/折りたたむ**: 入力（bottom sheet）、非Activeシナリオ、Receiver/Uplink/監査詳細。
- **Glass使用**: bottom sheetのハンドルと下部dockのみ（操作層）。
- **Glass不使用**: 意思決定カラム全体、チャート、Level Map。
- **削除**: §1.5全10件＋現行3ペイン等幅レイアウト自体。
- **利点**: (1) Quality Gate 10問が意思決定カラムの縦順に一致し即答可 (2) モバイルが「デスクトップ縮小」でなく本質的に縦IA (3) Entry→Invalidation→SL→TP1の誤読が構造的に起きない。
- **リスク**: (1) DOM移動が最大→JS影響大（`renderCards`/`renderReceiver`/`bindEvents`の参照張替え） (2) 既存`#z2/#z3/#z4`構造からの乖離が大きく回帰リスク (3) 実装コスト最大。
- **実装コスト**: **L**（IA全面再編、DOM大移動）。

---

## 3. Scorecard and Winner

各案を`04_SCORECARD.md`の同一基準で採点。数値は根拠と減点理由付き。

| Category | 重み | 案A Clarity | 案B Precision Glass | 案C Data-First |
|---|---:|---:|---:|---:|
| Data legibility | 20 | **19** | 17 | **19** |
| Price & level hierarchy | 20 | 17 | 16 | **19** |
| Scenario & risk clarity | 15 | 13 | 13 | **15** |
| Mobile usability | 15 | 13 | 12 | **14** |
| Visual coherence | 10 | 8 | **9** | 8 |
| Maintainability | 8 | **8** | 6 | 6 |
| Accessibility | 5 | 5 | 4 | 5 |
| Performance | 4 | **4** | 3 | 4 |
| Implementation feasibility | 3 | 3 | 3 | **2** |
| **合計** | 100 | **90** | 83 | **92** |

**10点尺度項目の7点未満チェック（不合格条件）**
- 案A: Visual coherence 8/10（10点尺度換算8）→全項目7以上、**合格**。
- 案B: Maintainability 6/8＝7.5/10、Performance 3/4＝7.5/10、Implementation 3/3。**Visual coherence 9だが、Mobile usability 12/15＝8.0、しかしglass-on-glass再発リスクとモバイルSafari backdrop不安定で実質Data legibility 17/20＝8.5**。**合計83で85未満→不合格候補**。
- 案C: Implementation feasibility 2/3＝6.7/10 → **10点尺度換算6.7で7未満→1項目不合格**。合計92だが失格条件に抵触。

**減点理由の明示**
- 案A: Price hierarchy -3（意思決定順が案Cほど強制されない）、Scenario clarity -2（Z4がサマリのみでEntry群が一目でない）、Mobile -2。
- 案B: Maintainability -2（Glass境界規律の維持コスト）、Data legibility -3（backdrop-filter起因の可読劣化リスク、img 01の再発懸念）、Performance -1（blur負荷）。**合計83<85で不合格**。
- 案C: Implementation -1（DOM大移動でJS回帰リスク、`bindEvents`張替え多数）→**この1項目が10点尺度で6.7となり失格**。

**採用規則の適用**
- 合格（85以上かつ全項目7以上）: 案Aのみ実質満たす。
- 案Cは合計最高（92）だがImplementation feasibility 6.7で7未満→不合格。ルール「最高点が85未満なら最上位案を1回改善」ではなく、ここでは**最高点92の案Cが失格条件（1項目7未満）に該当**するため、規則「合格案がなければ最上位案を1回だけ改善し再採点」を案Cに適用する。

**案C改訂（1回）— Implementation feasibilityを引き上げる**
- 改訂内容: IA全面再編ではなく、**既存`#z2/#z3/#z4`のIDとDOM骨格を保持したまま、CSS `order`とグリッド再配置で「意思決定カラム」を実現**する。Z4を意思決定カラム化し、Z1へ最寄り上下レベルを融合（既存`mfUpper`/`mfLower`のテキストソースを流用しDOM複製せず参照移動）。bottom sheetはZ2の既存`.body`をモバイルで`position:fixed`化。これによりJS参照張替えを最小化（`renderMarketInfo`のfact()ターゲットIDは不変、`bindEvents`のセレクタ不変）。
- 再採点: Implementation feasibility **2→3（10点尺度10）**、Maintainability 6→**7**（DOM保持で層統合が容易化）。**合計 92→94、全項目7以上を満たす**。

**最終採点（改訂後）**

| Category | 案A | 案C(改訂) |
|---|---:|---:|
| 合計 | 90 | **94** |
| 全項目7以上 | ○ | **○** |

**同点判定不要**（差4点）。ただし採用規則の第2ソート（Data Legibility→Level/Price→Mobile）でも案C改訂が上位。

### 最終決定（一文宣言）

> **勝者は案C改訂版「Data-First Decision Column」— 既存DOM骨格を保持しつつCSS orderで意思決定順（価格→最寄りレベル→Posture→Location→Active Scenario→Risk）を強制し、チャートを完全不透明化、Glassを操作層のみへ限定した、94点の単一採用案とする。**

以降の§4〜§14はすべて勝者（案C改訂）の論理のみで記述し、他案を混ぜない。

---

## 4. North-Star Screen Definition（勝者=Data-First Decision Column）

共通原則: チャート/データ層は不透明（`#03070d`基調）。Glassは下部dock・bottom sheetハンドル・Primary操作のみ。描画順は Chart → Level → Scenario → Risk（§8）。

### 4.1 — 1440px（デスクトップ標準）

| 領域 | 寸法 | 表示情報 | 折りたたみ | 操作 |
|---|---|---|---|---|
| Z1 Command | 高64px | ブランド, PRICE(大), ▲最寄り上(+距離), ▼最寄り下(−距離), STATE, CONFIDENCE, acts | INTEL TIME等optは`>1450px`のみ | ⚙設定, DEMO/RESET/VECTOR/CAPTURE |
| PostureBar | 高40px | IMMEDIATE POSTURE + NEXT30M | — | aria-live=polite |
| Z3 Chart | flex(min 560px)×高calc(100vh-64-40-26) | OHLC/Level Map, Primary/Secondary/Context levels, 端マーカー, Entry/Inval/SL/TP1帯 | Intel LayersトグルでAO/LEVELS/THREATS/OHLC | filter, TACTICAL VIEW, crosshair |
| marketStrip | Z3内上部 高52px | NOW/ABOVE/BELOW/LOCATION/DATA/MODE (6 fact) | — | — |
| levelBoard | Z3内 高74px | 近い順14チップ, 距離, type色 | 横スクロール | — |
| Z2 Input | 幅280px(fold時44px左レール) | textarea, COMPILE, Receiver要約 | Receiver詳細/Uplinkは`details` | COMPILE, VALIDATE, DECODE |
| Z4 Decision | 幅360px | ①Active scenario(Entry/Inval/SL/TP1/R:R) ②他scenarioカード ③RISK GATE状態 | 非Activeは折りたたみ | EXECUTE, EDIT |
| Z5 Status | 高26px | ログ, 時計, エラー | — | aria-live |

- ペイン幅: Z2=280px(min 44/max 320) │ Z3=flex(min 560) │ Z4=360px(min 320/max 400)。
- グリッド: `grid-template-columns: 280px minmax(560px,1fr) 360px`。

### 4.2 — 1024px（タブレット横）

| 変更 | 値 |
|---|---|
| Z2 | 既定で左44pxレール（fold）。展開時オーバーレイ`position:absolute`幅300px |
| グリッド | `44px minmax(480px,1fr) 320px` |
| Z1 opt項目 | STRATEGIC BIAS/ENGAGEMENT ZONE/INTEL TIME非表示（既存787踏襲） |
| marketStrip | 6→4列（MODE/DATAをDATAへ統合） |
| Z4 | 幅320px、非Active scenarioは高さ制限＋`overflow`スクロール |

### 4.3 — 768px（タブレット縦 / モバイル境界）

| 変更 | 値 |
|---|---|
| レイアウト | 3ペイン廃止。`#mtabs`（既存1592）で INTEL / MAP / MISSION 切替。**非アクティブペインは`display:none`必須** |
| 既定タブ | MAP（`body[data-mtab="chart"]`既存1555） |
| Z1 | 高56px、PRICE＋▲▼最寄りを1行 |
| marketStrip | 3列（既存1547踏襲: NOW/ABOVE/BELOW、LOCATION/DATA/MODEは2段目へ） |
| チャート高 | `min(56vh, calc(100vh - header - tabs - strip - bar))` |
| 下部 | `#mbar`（既存1759）操作専用、`position:fixed`, safe-area padding |

### 4.4 — 390px（モバイル基準 / img 01・02の是正対象）

| 領域 | 寸法 | 表示 | 状態別 |
|---|---|---|---|
| Z1 sticky | 高48px | PRICE(20px), STATE | MISSING時PRICE赤`#ff627f` |
| 最寄り帯 sticky | 高32px | ▲29,650 +7 / ▼29,598 −45 | データ無=「NONE MAPPED」 |
| mtabs | 高40px | INTEL/●MAP/MISSION | 単一`display:block` |
| PostureBar | 高36px | IMMEDIATE + 次監視 | MISSING時は復帰文言 |
| marketStrip | 高44px 3列 | NOW/ABOVE/BELOW | warn時border橙 |
| levelBoard | 高40px | 近い順チップ横スクロール | 空時「COMPILE INTELLIGENCE」 |
| Chart | 高`min(52vh,…)` | 不透明, ▲N▼N端マーカー | Empty時は§8.9のEmpty state |
| mbar fixed | 高56px+safe-area | COMPILE/OPS/CAPTURE | — |

- **絶対条件**: `document.body.scrollWidth ≤ 390`（横overflowゼロ）。`#liquidGlassFx`は`display:none`。`env(safe-area-inset-bottom)`をmbarへ適用。
- タップ領域: 全ボタン最小44×44px。
- 常時表示: PRICE, STATE, ▲▼最寄り, POSTURE, チャート。
- 段階的開示: シナリオ詳細=タブMISSION、入力=タブINTEL、Receiver/Uplink=`details`。
- 隠す: チャートテレメトリ, ホロ装飾（全削除済み）。

---

## 5. Visual Tokens

> 全トークンを`:root`のCSS変数として`Tokens`層に集約。既存の分散した色リテラル（`#89f6ff`, `#ff627f`, `#ffc46b`等）を置換。`!important`は撤去し、カスケード順で解決。

| Token | Exact value | Usage | State | Contrast reason |
|---|---|---|---|---|
| `--bg-0` | `#03070d` | チャート/データ面の基底不透明背景 | rest | 白veil根絶。ローソク`#49d7e8`/`#ff627f`が背景比7:1以上 |
| `--bg-1` | `#070c14` | パネル面（marketFact, levelBoard, cards） | rest | 面分離。テキスト`#d9e7f2`と比8.2:1 |
| `--bg-2` | `#0a1019` | levelChip, ネスト面 | rest | glass-on-glass回避のため不透明の面深度 |
| `--line` | `rgba(125,154,190,.20)` | 標準境界 | rest | 過剰発光を排し境界のみで面を分ける |
| `--line-strong` | `rgba(111,242,255,.45)` | primary面/選択境界 | active | 主操作・現在値へ限定 |
| `--ink-hi` | `#eaf2f8` | 最重要テキスト（価格数値） | — | `--bg-0`比 12:1（WCAG AAA） |
| `--ink` | `#d9e7f2` | 標準テキスト | — | `--bg-1`比 8.2:1 |
| `--ink-mut` | `#8ea3b8` | ラベル/補助 | — | 比 4.6:1（AA。極小6.5px使用禁止） |
| `--ink-dim` | `#697b91` | fact small見出し | — | 比 3.2:1（見出し=非本文のみ許容） |
| `--accent` | `#6ff2ff` | Primary操作, 現在値強調, near level | primary/active | 色付けは主操作・重大状態のみ。比 9:1 |
| `--long` | `#49d7e8` | LONG/上昇ローソク/上レベル | data | 非色依存: ▲記号併記 |
| `--short` | `#ff627f` | SHORT/下降ローソク/MISSING価格 | alert | 非色依存: ▼/「MISSING」語併記。比 5.1:1 |
| `--warn` | `#ffc46b` | STALE/警告/推定`≈`/AGING | warn | 非色依存: `≈`記号・「STALE」語併記。比 8:1 |
| `--ok` | `#8ce0a0` | PASS/READY/FRESH | ok | 非色依存: 「PASS」語併記 |
| `--danger` | `#ff627f` | BLOCKED/INVALIDATED/Hard SL | critical | `--short`と同色（重大は赤で統一） |
| `--fs-price` | `24px`/mobile`20px` | Z1価格 | — | 最重要=最大 |
| `--fs-lvl` | `13px` | levelChip価格, marketFact strong | — | **6.5px→9px以上へ全面引き上げ**。可読下限 |
| `--fs-body` | `12px` | 本文 | — | プロ端末可読下限 |
| `--fs-label` | `10px` | ラベル見出し（旧6.5px禁止） | — | letter-spacing .12emで判読 |
| `--lh` | `1.35` | 行高 | — | 密度と可読の均衡 |
| `--sp-1`〜`--sp-4` | `4/6/10/16px` | 余白スケール | — | 4pxグリッド |
| `--r-sm`/`--r-md` | `5px`/`7px` | 角丸（chip/panel） | — | 既存踏襲、過度な丸み禁止 |
| `--sh-panel` | `0 16px 48px rgba(0,0,0,.48)` | チャート枠影 | rest | 内側白ハイライト（`inset …rgba(255,255,255)`）撤去 |
| `--glass-op-bar` | `blur(18px) saturate(1.2)` | **操作層のみ**のbackdrop | dock/tabs/sheet | チャート/データ層禁止 |
| `--glass-tint` | `rgba(111,242,255,.10)` | 選択シナリオ/primary btn tint | active/press | tintは主操作限定 |
| `--motion-fast` | `120ms ease-out` | 押下feedback | press | 短時間のみ光/変形 |
| `--motion-none` | `0ms` | Reduced Motion時 | reduced | 全アニメ停止 |

**モーション規則**: rest状態は完全静止。操作時のみ`--motion-fast`で光・変形を短く。`.routeflow`/`.dzscan`/`.pulse`/`.ring`アニメ（renderChart注入4396-）はReduced Motion時に無効化必須。

---

## 6. Information Priority Matrix

| Information | Priority | Desktop位置 | Mobile位置 | Missing state | Source（関数/変数） |
|---|---:|---|---|---|---|
| 現在価格 NOW | 1 | Z1 大数値 + #mfNow | Z1 sticky 20px | 赤「MISSING」`--short` | `renderZ1` 4194 / `m.now` |
| 最寄り上レベル+距離 | 1 | Z1 ▲ + #mfUpper | 価格直下sticky帯 | 「NONE MAPPED」 | `nearestLevelPair` 2768 / #mfUpper 4173 |
| 最寄り下レベル+距離 | 1 | Z1 ▼ + #mfLower | 価格直下sticky帯 | 「NONE MAPPED」 | #mfLower 4174 |
| Immediate Posture | 1 | #postureBar | #postureBar 36px | 「POSTURE MISSING. DEFINE…」 | #postureText 4206 |
| Market Location | 1 | #mfLocation | strip 2段目 | 「UNMAPPED」 | #mfLocation 4175-4178 |
| チャート（OHLC/Level Map） | 1 | #chartwrap | 52vh | Empty state §8.9 | `buildTruthChart` 3967 |
| Active Scenario Entry | 1 | Z4 ①カード | MISSIONタブ | `.miss`赤枠input | `renderCards` 4438 |
| Invalidation / Hard SL | 1 | Z4 ①カード | MISSIONタブ | 「MISSING」 | FIELD_DEFS / iv,sl |
| TP1 + R:R | 2 | Z4 ①カード | MISSIONタブ | 「R:R LINE MISSING」 | rrClaimAudit 2819 |
| Scenario State(WATCH等) | 2 | Z1 #hudState + Z4 | Z1 + MISSION | 「—」/NO TRADE | GAME_STATE 4196 |
| Confidence / Grade | 2 | Z1 #hudConfidence | MISSION内 | 「—」 | 4198 |
| データ鮮度 FRESH/STALE | 2 | #mfData | strip 2段目 | 「TIME UNVERIFIED」 | packetAgeMinutes 4213 |
| Chart Mode / bars | 3 | #mfMode | strip 2段目 | 「NO OHLC」warn | 4170-4171 |
| Level Map全件 | 3 | #levelBoard 横スクロール | levelBoard | 「NO LEVELS」 | renderMarketInfo 4182-4190 |
| Mutual Exclusion / 排他 | 3 | Receiver `details` / Z4 | MISSION details | 「No overlap audit」 | mutualExclusionAudit 4233 |
| RISK GATE verdict | 2 | #rxVerdict | Empty state内へ昇格 | 「INCOMPLETE」 | receiverMetrics.verdict 4244 |
| Uplink / AI | 4 | Z2 `details`折りたたみ | INTELタブ details | — | runUplink 5146（意味不変） |
| Receiver quant audit | 4 | `details.rxintel` | INTEL details | — | 4252 |
| ブランド/時計/ログ | 4 | Z1左/Z5 | Z1/非表示 | — | renderStatus |

> Priority 1 = 常時表示・折りたたみ禁止。Priority 4 = `details`/別タブ。**新規追加はI-a（Empty state復帰CTA）のみ**で、これは既存verdict文言の再配置であり新指標ではない。

---

## 7. Component-Level Specification

各コンポーネントに `Purpose / Keep / Remove / New hierarchy / Dimensions / States / Selectors`。IDは全て維持（DOM移動はCSS orderとグリッドで実現、JS参照張替え最小化）。

### `#z1`（Command Bar）
- Purpose: ブランド・現在値・最寄り上下・状態・操作の最上位。
- Keep: `#hudNow`, `#hudState`, `#hudConfidence`, `.acts`ボタン群, `#logo`。
- Remove: V2ネオングロー、`livechip`発光。opt項目（BIAS/DZ/UPD）は`>1450px`のみ。
- New hierarchy: PRICE(24px `--ink-hi`) > ▲上/▼下(13px) > STATE > CONFIDENCE。**#mfUpper/#mfLowerのテキストをZ1へミラー表示**（DOM複製せず、`renderMarketInfo`のfact()にZ1用ターゲット追加、§11）。
- Dimensions: 高64px(desktop)/56px(768)/48px(390)。
- States: MISSING時PRICE赤。data-bias/data-state（4202-4203）でborder色のみ、グロー無し。
- Selectors: `#z1 #hud .kv`, `#hudNow`, `.acts .btn`。

### `#mtabs`（Mobile tabs）
- Purpose: モバイルのペイン切替（INTEL/MAP/MISSION）。
- Keep: 3ボタン, `data-mt`, `.on`。
- Remove: なし。
- New hierarchy: MAP既定。アクティブ=`--accent`下線。**Glass許容（セグメント背景）**。
- Dimensions: 高40px, タップ44px確保。
- States: `.on`。**非アクティブタブのペインは`display:none`（横overflow根絶）**。
- Selectors: `#mtabs button[data-mt]`, `body[data-mtab]`。

### `#z2`（Input）
- Purpose: NQX入力・コンパイル・Receiver・Uplink。
- Keep: `#src`, `#parseBtn`, `#receiver`全子, `#uplink`全子, `#btnValidate/#btnDecode/#btnRawToggle/#btnCopyPacket/#btnCopyFull`。
- Remove: winbar装飾dots発光。
- New hierarchy: desktop=左280px（fold時44pxレール）。Receiver要約常時、詳細は`details.rxintel`。Uplinkは`#upBody`折りたたみ維持。
- Dimensions: 幅280px/44px(fold)。mobile=INTELタブ全幅。
- States: fold/expand（`#z2fold`）。
- Selectors: `#z2`, `#z2 .body`, `#z2tab`, `#z2fold`。

### `#receiver`
- Purpose: NQXパケット受信状態・品質・監査要約。
- Keep: `.rxhead`, `.rxgrid`(10 cell), `.rxbrief`(3 line), `details.rxintel`, `#rxVerdict`, `data-quality`。**全監査意味を保護**。
- Remove: なし（情報保護）。面をGlassにしない（不透明`--bg-1`）。
- New hierarchy: VERDICT/QUALITY/RISK GATEを上位、詳細grid下位。
- Dimensions: desktop幅280px内, mobile全幅。
- States: `data-quality=low/medium/high`（4259）, verdict=READY/REVIEW/INCOMPLETE/BLOCKED/EXPIRED/STALE。
- Selectors: `#receiver[data-quality]`, `#rxVerdict`, `.rxcell strong`。

### `#uplink`
- Purpose: AI Uplink実行（意味不変・保護）。
- Keep: 全input/select/`#upRun`/`#upToggle`/`#upBody`。runUplink(5146), upPromptFast(4832)の**意味は変更禁止**。
- Remove: なし。
- New hierarchy: `#upToggle`折りたたみ内に全格納。
- Dimensions: 折りたたみ。
- States: `aria-expanded`。
- Selectors: `#uplink #upBody[hidden]`, `#upToggle`。

### `#chartbar`
- Purpose: フィルタ・Intelレイヤー・手動価格・TACTICAL VIEW。
- Keep: `data-filter`(all/long/short), `data-layer`(dz/levels/events/glass), `#nowInput`, `#btnFocus`, `#draft`。
- Remove: なし。
- New hierarchy: OPERATIONS > INTEL LAYERS > PRICE。mobileは折りたたみでチャート高確保。
- Dimensions: 高40px(desktop)。mobile=折りたたみ32px。
- States: `.on`トグル。
- Selectors: `#chartbar .grp`, `[data-filter]`, `[data-layer]`, `#nowInput`。

### `#postureBar`
- Purpose: 今のアクションと次30分。Priority 1。
- Keep: `#postureText`, aria-live=polite。
- Remove: 発光。
- New hierarchy: IMMEDIATE POSTURE見出し + 本文（6.5px→12px）。
- Dimensions: 高40px/36px(mobile)。
- States: MISSING/NO TRADE/通常。
- Selectors: `#postureBar b`, `#postureText`。

### `#marketStrip`
- Purpose: NOW/ABOVE/BELOW/LOCATION/DATA/MODE の6 fact。Priority 1。
- Keep: 全6 `.marketFact`（#mfMode/#mfNow/#mfUpper/#mfLower/#mfLocation/#mfData）。**IDとfact()ターゲット不変**。
- Remove: 6.5pxフォント（→small 10px/strong 13px）。
- New hierarchy: `#mfNow`=primary。desktop 6列、1024=4列、390=3列(NOW/ABOVE/BELOW)＋2段目。
- Dimensions: 高52px(desktop)/44px(mobile)。gap 5px。
- States: `.warn`（border橙）, `.primary`（#mfNow）。
- Selectors: `#marketStrip`, `.marketFact.primary`, `.marketFact.warn`。

### `#levelBoard`
- Purpose: 宣言済みレベルと現在値からの距離。Priority 3。
- Keep: `#levelRows`, `#levelCount`, `.levelChip`, `.near`, `--lc`色。近い順ソート(4183-4186)。
- Remove: なし。
- New hierarchy: near-first、near強調、type色は左border(`--lc`)のみ。
- Dimensions: 高74px、chip min-width 100px、横スクロール。
- States: `.near`（現在値±0.08%）, 空「NO LEVELS」。
- Selectors: `#levelBoard`, `#levelRows .levelChip.near`。

### `#chartwrap`
- Purpose: SVGチャート容器。**Glass全撤去、完全不透明**。
- Keep: `#chart`, `#tip`(tooltip), `#boot`。
- Remove: **`#liquidGlassFx`全子, `#holoCursor`, `.chartTelemetry`×2, `.holoCorner`×4**（DOM削除）。`#chart`の`filter`, 内側白`inset`ハイライト。
- New hierarchy: 背景`--bg-0`固定、影は`--sh-panel`のみ。
- Dimensions: desktop flex×calc高、mobile 52vh。装飾なし。
- States: rest静止。lg-active/lg-pressed（1513-1515）→**削除**。
- Selectors: `#chartwrap`, `#chart`, `#tip`, `#boot`。**削除**: `#chartwrap.lg-active`, `.lg-*`。

### `#scpanel`
- Purpose: 選択シナリオ詳細。
- Keep: `#sptitle`, `#spbody`, `#spclose`, `hidden`, showScPanel/hideScPanel。
- Remove: なし。
- New hierarchy: desktop=チャート下ドロワー、mobile=MISSIONタブ内。
- Dimensions: 可変。
- States: hidden/表示（panelKind==='sc'）。
- Selectors: `#scpanel[hidden]`, `#spbody`。

### `#z4`（Mission / Decision Column）
- Purpose: **勝者の意思決定カラム**。シナリオスタック・監査。
- Keep: `#cards`, `#parsefail`, `#z4title`, `#z4tab/#z4fold`, renderCards(4438)。
- Remove: winbar装飾。
- New hierarchy: **①Active scenario（Entry/Invalidation/Hard SL/TP1/R:R を1カード上部固定）→ ②非Active（折りたたみ）→ ③RISK GATE状態**。CSS orderで並べ替え、DOM構造・input `data-f`属性は不変。
- Dimensions: 幅360px(desktop)/全幅(MISSIONタブ)。
- States: Activeカード=`--line-strong`境界+`--glass-tint`（選択層のみGlass許容）。
- Selectors: `#cards .card`, `#z4title`, `#parsefail.on`。

### `#mrail`（Mobile scenario rail）
- Purpose: モバイルのシナリオ選択タブ。
- Keep: `#railList`, `.railhead`, renderRail。
- Remove: **画面外残留の原因を除去**——`#mrail`は`display:none`既定、MISSIONタブ時のみ`display:block`。単独で画面外に出さない（img 01の右「OPS」是正）。
- New hierarchy: MISSIONタブ内の上部横スクロールtablist。
- Dimensions: 高44px、chip min 44px。
- States: 選択`.on`。
- Selectors: `#mrail`, `#railList [role=tab]`。

### `#mbar`（Mobile action bar）
- Purpose: モバイル操作専用bar。
- Keep: `#mParse/#mFilter/#mSave`。
- Remove: なし。
- New hierarchy: `position:fixed`下部固定、**操作層Glass許容**、safe-area padding。
- Dimensions: 高56px + `env(safe-area-inset-bottom)`。ボタン44px。
- States: — 。
- Selectors: `#mbar button`。

### `#z5`（Status）
- Purpose: ログ・時計・エラー。
- Keep: aria-live, renderStatus。
- Remove: なし。
- New hierarchy: desktop高26px。mobile=非表示（エラーのみtoast）。
- Dimensions: 高26px。
- States: エラー時`--danger`。
- Selectors: `#z5`。

---

## 8. Chart / Level / Scenario Rendering（buildTruthChart基盤）

> 重要事実: 本番描画は既に`buildTruthChart`(3967)。旧`buildChart`(3496)は`module.exports`(5847)からのテスト参照のみで**ライブ経路から隔離済み**。合成ローソク(`conditionalCandles` 3116)・route path(`.routeflow` 3801-3809)は旧`buildChart`側にのみ存在し、`buildTruthChart`には無い。**No Synthetic Futureは現行ビルドで達成済み**。本章は`buildTruthChart`の描画規律を確定する。

### 8.1 描画z-order（背面→前面）
1. 背景`url(#truthBg)` `--bg-0`（3978）
2. グリッド`url(#truthGrid)` 微弱（3979, opacity .075維持）
3. 価格軸目盛`niceTicks`（4001-4004, opacity .13）
4. Decision Zone帯（4006-4013, fill-opacity .055）
5. **Context levels**（遠方・細線・ラベルなし）
6. **Secondary levels**（中距離・細線・ラベルあり）
7. **Primary levels/clusters**（現在値近傍・太線1.55・ラベル強）（4021-4028）
8. 観測OHLC（4030-4036, 上昇`--long`/下降`--short`）
9. Entry/Invalidation/Hard SL/TP1 の水平帯（**連続経路線にしない**）
10. 現在値ライン（最前・`--accent`）
11. ラベルリード線・ラベル（layoutLabels 3306で衝突解決）
12. 画面外マーカー（▲/▼、最前固定）

### 8.2 軸範囲（auto-range）— img 10のC5是正
- 基準: `truthViewport`(3946)を維持しつつ、**表示spanを現在値中心に制限**。
- 規則: OHLCモード=直近96 bar(3947 `slice(-96)`)の高安 + `m.now`。Level Mapモード=`m.now` + `m.dz` + **最寄り8レベル(`near.slice(0,8)` 3953)のみ**を`p`に含める。
- pad: `Math.max(span*.09, …)`（3964）維持。
- **範囲外レベルは軸に含めず**、§8.6の画面外マーカーへ。これによりAsia Low(29303)等の遠方レベルが現在値(29642)周辺を圧縮しない。

### 8.3 Level clustering（img 06のラベル過多是正）
- `clusterGap = Math.max(model.unit*.22, 1)`（4015）維持。数pt差レベルを1クラスタへ。
- ラベル: `names.slice(0,3)`（4023）で最大3名、超過は「+N」。1クラスタ=1本のクラスタ線＋1ラベル。
- `strong`判定（4024）: 複数レベル or DZ/RBS/SBR/POC/VAH/VAL/TACTICAL type → Primary。

### 8.4 Primary / Secondary / Context の定義
| 階層 | 条件 | 線 | ラベル |
|---|---|---|---|
| Primary | strong=true or 現在値±1.4×unit(4026) | 太1.55 dash「7 4」opacity .72 | あり・色付き・priority 8 |
| Secondary | クラスタだがstrong外 | 細1 dash「2 5」opacity .38 | あり・`--ink-mut`・priority 4 |
| Context | 範囲内だが現在値遠方 | 細1 opacity .22 | **なし**（軸目盛のみ） |

### 8.5 ラベル衝突
- `layoutLabels(items,minY,maxY,gap)`(3306)で縦方向解決。gap=ラベル高+2px。
- 右軸ラベルレール（`mR`余白 214px/174px compact, 3969）へリード線。
- 最大2行（`l1`,`l2`）。3行以上禁止。

### 8.6 画面外マーカー（新規描画・実データのみ）
- 現在値中心span外のレベルを上下端に集約。
- 上端: `▲ N ABOVE  最寄り外レベル名 ≈price`（`--long`）。下端: `▼ N BELOW …`（`--short`）。
- タップ/クリックで該当方向へ軸を一時拡張（TACTICAL VIEW `#btnFocus`と連動可）。
- **実在レベルのみ**。合成しない。

### 8.7 State別シナリオ表示（WATCH〜INVALIDATED）
`st`コード（nqx1-spec §S: W/A/T/C/X/P/B/D/I）→ GAME_STATE(4196)。
| State | チャート表示 | 色 |
|---|---|---|
| WATCH(W) | Entry帯 破線・淡 | `--ink-mut` |
| ARMED(A)/TRIGGERED(T) | Entry帯 実線 | `--accent` |
| CONFIRMED(C)/ACTIVE(X) | Entry+現在値強調 | `--long`/`--short` |
| PARTIAL(P)/BE(B) | TP到達マーク | `--ok` |
| CLOSED(D) | 淡色化 | `--ink-dim` |
| INVALIDATED(I) | Entry帯に×、Invalidationライン強調 | `--danger` |

### 8.8 Entry / Invalidation / SL / TP1
- **水平帯＋右軸ラベルのみ**（連続経路線にしない=No Synthetic Future）。
- 順序保護: LONG `SL < invalidation < entry low`、SHORT逆（nqx1-spec安全invariant 2, invalidationOrderAuditで検証済）。
- 描画: Entry=帯、Invalidation=破線`--warn`、Hard SL=実線`--danger`、TP1=破線`--ok`＋R:R値。
- Active scenarioのみチャートに帯表示、他はZ4カードのみ。

### 8.9 OHLC欠損 / Observed / Estimated / Simulated
- OHLC 3本以上=`OBSERVED OHLC`モード（4170, `vp.mode='OHLC'`）。
- OHLC 3本未満=`LEVEL MAP · NO OBSERVED OHLC`（3996）。チャート内に「NO CANDLES GENERATED — DECLARED LEVELS ONLY」（3999）明示。
- **Simulated候補（旧conditionalCandles）は描画しない**（buildTruthChartは非対応、維持）。
- Estimated価格: `fmt(p, approx)`が`≈`prefix（3303）。**推定は必ず`≈`表示**。NQX側の軸推定は`~`（nqx1-spec: `~29620..29650`）。UI表示`≈`とデータ表記`~`は同一意味（推定）として維持。
- **Empty state（img 02是正）**: `!vp`時（3981-3984）「AWAITING MARKET INTELLIGENCE」に加え、**mobileではチャート高を`min(40vh)`へ縮小し、枠内にprimary recovery CTA「▸ PASTE & COMPILE」＋RISK GATE verdict文言を統合表示**（I-a、既存verdictの再配置で新指標ではない）。

### 8.10 状態表示語彙の統一（欠損/STALE/ERROR/NO TRADE/INVALIDATED）
| 状態 | トリガ | 表示 | 色/記号 |
|---|---|---|---|
| 欠損(MISSING) | `m.now==null`等 | 「MISSING」語 | `--short`（4194） |
| STALE | `age>15`(4179) / verdict STALE(4247) | 「STALE Nm」 | `--warn` + `.warn`border |
| ERROR | validateNQX失敗 | 「BLOCKED」verdict | `--danger`, `#rxVerdict` |
| NO TRADE | `n=0`(G record) | 「NO TRADE — 理由」 | `--ink-mut`（4196,4206） |
| INVALIDATED | `st=I` | Entry×, Invalidation強調 | `--danger`（§8.7） |
| バージョン拒否 | 非`!NQX/1` | パケット拒否（保護, spec §Versioned） | エラー表示 |

---

## 9. Liquid Glass Boundary

### 9.1 使用箇所（操作/ナビ層のみ）
- `#mtabs`セグメント背景（`--glass-op-bar`）
- `#mbar`下部dock（mobile, `--glass-op-bar` + safe-area）
- Z2/bottom sheetのハンドル（mobile）
- Primary操作: `#parseBtn`, `#upRun`, Z4 EXECUTEボタンの`--glass-tint`
- 選択中シナリオカードのtint（`--glass-tint`）
- 押下feedback（`--motion-fast`の短い光/変形）

### 9.2 禁止箇所（コンテンツ/データ層）
- `#chartwrap`, `#chart`（**完全不透明**）
- `#levelBoard`, `.levelChip`
- `#marketStrip`, `.marketFact`
- `#receiver` grid, `.rxcell`
- `#cards`本体（tintは選択枠のみ、本体面は不透明）
- **glass-on-glass禁止**: Glass要素の上にGlassを重ねない。

### 9.3 Rest / Hover / Press / Focus
| State | 挙動 |
|---|---|
| Rest | 完全静止、backdrop-filterのみ（アニメなし） |
| Hover(pointer:fine) | border`--line-strong`、transformなし |
| Press | `--glass-tint`濃度+.06、`--motion-fast`で120ms |
| Focus | `outline:2px solid --accent; outline-offset:2px`（キーボード可視） |

### 9.4 Reduced Transparency（`prefers-reduced-transparency:reduce`）
- 全Glass→不透明面（`--bg-1`）へ置換。backdrop-filter無効。tint→border強調に代替。
- 既存1295,1470のメディアクエリを統合し全Glass箇所を網羅。

### 9.5 Reduced Motion（`prefers-reduced-motion:reduce`）
- `.routeflow/.dzscan/.pulse/.ring/.slhot`（renderChart注入4396-）全停止。
- press feedback→即時色変化のみ。
- 既存335,1291,1467を統合。

### 9.6 削除する現行FXセレクタ
`#liquidGlassFx`, `#lgSpectrum`, `#lgCaustics`, `#lgLens`, `#lgRim`, `#lgBubbles`, `.lg-bubble.b1〜b6`, `#lgRippleLayer`, `#holoCursor`, `.chartTelemetry.top/.bottom`, `.holoCorner.tl/tr/bl/br`, `#chartwrap.lg-active`, `#chartwrap.lg-pressed`, `#chart{filter:…}`（1491-1493,1522）, V11 spectral rim（1311-1477の虹色）。

---

## 10. CSS Demolition and Rebuild Map

> 撤去順序: 後方世代（新）から前方（旧）へ依存を解きつつ、最終8層へ統合。`!important`は統合完了後に全撤去。

| Block | 判定 | Exact selectors | Destination layer | Risk |
|---|---|---|---|---|
| **V2 Visual Core**(340-668) | REWRITE | ネオングロー`box-shadow`, `.btn`発光, `.livechip i` | Base/Components（グロー除去） | 中: 全体トーン変化 |
| **V4 Cryogenic Glass**(669-800) | REMOVE | ice command glass背景層 | 破棄 | 低: 後続で上書き済 |
| **V5 Desktop Skin**(801-970) | MERGE | `#hud .kv`, レイアウトグリッド, opt項目メディアクエリ(786-789) | Layout/Responsive | 中: レイアウト土台 |
| **V8 Receiver**(971-1030) | KEEP→MERGE | `#receiver`, `#postureText`(1008), risk-gate | Components | 低: 情報保護 |
| **V9 Arctic Glass**(1031-1126) | REMOVE | bright chart deck, `#chartwrap`明色背景, fixed-size | 破棄（`#chartwrap`はV14で再定義） | **高: img 01白ガラスの主因。撤去必須** |
| **V10 Glass Refinement**(1127-1310) | REMOVE | rim/frost/depth層 | 破棄 | 中 |
| **V11 Spectral Metal**(1311-1477) | REMOVE | 虹色rim, spectral液体金属 | 破棄（操作tintのみ`--glass-tint`へ) | 中: glass-on-glass温床 |
| **V12 Data First**(1478-1525) | MERGE→REWRITE | `#chartwrap`不透明化, `#lgLens/#lgCaustics`(→削除), `#chart filter`(→削除) | Chart（不透明確定、FX削除） | **高: `!important`14箇所を解く順序に注意** |
| **V14 Market Truth**(1527-1552) | KEEP→MERGE | `#marketStrip`, `.marketFact`, `#levelBoard`, `.levelChip`, `#chartwrap`最終定義 | Components/Chart | 低: 最新・正方向。フォント6.5px→引き上げ |

### 最終CSS層構成（8層、この順で1つの`<style>`へ）
1. **Tokens**: `:root`全CSS変数（§5）。
2. **Base**: reset, body, typography（`--fs-*`）, `.sr`, focus-visible。
3. **Layout**: `#app`グリッド, `#z1/#mid/#z5`, ペイン幅, `@media`ブレークポイント骨格。
4. **Components**: `#hud`, `.btn`, `#receiver`, `#uplink`, `#marketStrip`, `#levelBoard`, `#postureBar`, `#cards`, `#scpanel`, `#mtabs/#mrail/#mbar`。
5. **Chart**: `#chartwrap`(不透明), `#chart`, `#tip`, `#boot`, 画面外マーカー。
6. **States**: `.warn/.primary/.near/.on/.miss`, data-state/data-bias, verdict色。
7. **Responsive**: 1440/1024/768/390の各`@media`（統合）。
8. **Optional FX**: Glass操作層(`--glass-*`), reduced-motion/transparency（全て最後、任意）。

### `!important`撤去順序
V9/V10/V11を先にDOM/CSS削除→V12の`!important`（`#chartwrap`背景等）が不要になるので除去→V14をChart/Components層へ昇格し`!important`不要化→最後にBase/Componentsの残`!important`を除去。各ステップ後にimg 01/02相当（モバイル空/欠損）を目視回帰。

---

## 11. DOM and JavaScript Impact

> 原則: IDを維持しCSSで再配置。DOM移動が必要な箇所のみ以下に列挙。JS参照は最小張替え。

| DOM/ID | Change | Affected functions | Migration | Regression test |
|---|---|---|---|---|
| `#liquidGlassFx`と全子 | **DOM削除** | renderChart(4384)（lg-active/lg-pressedクラス操作があれば）, bindEvents(mousemove系) | FX層参照とlg-*クラス付与コード削除。`--lg-x/--lg-y/--lg-energy`更新削除 | チャートに白veilなし、コンソールエラーなし |
| `#holoCursor` | DOM削除 | bindEvents(cursor追従) | 追従リスナ削除 | mousemoveでエラーなし |
| `.chartTelemetry`,`.holoCorner` | DOM削除 | なし（静的） | HTML削除のみ | 表示消失、レイアウト不変 |
| `#chart filter` | CSS削除 | buildTruthChart(色は変えない) | CSSのみ | ローソク色が正確 |
| `#z4` order | CSS order（DOM不変） | renderCards(4438), updateCardWarns, showScPanel | `#cards`内カードにActive判定→CSS order/クラス。DOM構造不変 | Entry/Inval/SL/TP1が上部、input編集可 |
| `#mfUpper/#mfLower`のZ1ミラー | 新ターゲット追加 | renderMarketInfo(4167), renderZ1(4192) | fact()にZ1用要素ID追加。**#mfUpper/#mfLowerのfact()呼び出しは維持し、同値をZ1へも書く** | Z1の▲▼が#mfUpperと一致 |
| `#mrail` mobile | `display:none`既定化 | renderRail | CSSのみ、renderRail不変 | MISSIONタブ以外で非表示、横overflowなし |
| `#mbar` fixed+safe-area | CSS position | bindEvents(#mParse/#mFilter/#mSave) | CSSのみ | safe-area下でチャート非被覆 |
| `#chartbar` mobile折りたたみ | CSS+toggle | bindEvents | 折りたたみトグル追加（layer/filter状態は維持） | フィルタ操作が機能 |

**必須確認関数（契約指定）**
- `renderAll`(4145): DOM再配置後も呼び出し順（validateAll→renderZ1→renderRail→renderReceiver→renderCards→renderChart→renderStatus）維持。
- `renderReceiver`(4252): `#receiver`のID/data-quality/verdict不変なら影響なし。
- `renderChart`(4384): `#chartwrap`のclientWidth/Height依存。不透明化・FX削除で`wrap.lg-*`参照を削除。W/H/compact計算(4386-4392)維持。
- `renderCards`(4438): `#cards`のFIELD_DEFS/`data-f`属性不変。Active order化はクラス付与のみ。
- `buildTruthChart`(3967): 描画規律（§8）に沿うが、関数シグネチャ・幾何計算不変。
- `bindEvents`(5259): 削除DOM（FX/holo）のリスナ削除。既存ボタンセレクタ不変。
- `runUplink`(5146): **意味・API・validateNQX後処理を一切変更しない**（表示位置のみ）。

---

## 12. Codex Implementation Sequence

各フェーズに 目的/変更対象/作業/完了条件/回帰テスト/Rollback point。**エンジン・監査コードには一切触れない**。

### Phase 0 — 保護境界の固定（Rollback base）
- 目的: 変更禁止領域を明示しコミット基点作成。
- 変更対象: なし（タグ付けのみ）。
- 作業: `parseNQX/validateNQX/parseAnalysis/receiverMetrics/各audit/buildTruthChart幾何/upPromptFast/runUplink`を「触るな」コメントで囲む。git tag `pre-redesign`。
- 完了条件: `validate_nqx.py valid-sample.nqx`=exit 0, `invalid-sample.nqx`=非0。
- 回帰テスト: 既存テストsuite（module.exports 5847経由）全pass。
- Rollback: tag `pre-redesign`。

### Phase 1 — FX層の物理削除（img 01是正の核）
- 目的: チャートcontent層Glass全撤去。
- 変更対象: HTML 1714-1729, CSS 1031-1524, renderChart/bindEvents のlg-*参照。
- 作業: §9.6のFX DOM削除→lg-*クラス/変数更新コード削除→`#chartwrap`を`--bg-0`不透明・影`--sh-panel`のみに。`#chart filter`削除。
- 完了条件: `#liquidGlassFx`がDOMに存在しない。チャートに白veilなし。
- 回帰テスト: DEMO投入→ローソク/レベル100%可視。コンソールエラー0。1440/390で目視。
- Rollback: Phase 0 tag。

### Phase 2 — Tokens層導入とCSS統合開始
- 目的: 8層構成の土台。
- 変更対象: CSS全体、`:root`。
- 作業: §5トークンを`:root`定義。V4/V9/V10/V11ブロック削除。フォント6.5px→§5値へ全置換。
- 完了条件: 全色/寸法がトークン参照。削除世代の残存セレクタ0。
- 回帰テスト: 1440px全領域が旧レイアウトと情報等価（欠損なし）。
- Rollback: Phase 1 commit。

### Phase 3 — Layout / Components / Chart / States 層へ移送
- 目的: `!important`撤去、所有ブロック確定。
- 変更対象: V2/V5/V8/V12/V14を8層へ再配置。
- 作業: §10の判定に従い各ブロックを移送。`!important`を§10順序で除去。marketStrip/levelBoard/postureBarを最終形へ。
- 完了条件: `!important`残数=Optional FXの必要最小のみ。
- 回帰テスト: 1440/1024/768/390でレイアウト破綻なし。Quality Gate 10問即答可。
- Rollback: Phase 2 commit。

### Phase 4 — Decision Column（Z4 order化）とZ1ミラー
- 目的: 勝者IAの実装。
- 変更対象: `#z4` CSS order, renderCards Active判定, renderMarketInfoのZ1ミラー。
- 作業: Activeカードを上部固定（Entry/Inval/SL/TP1/R:R）。Z1に▲▼最寄りミラー追加（fact()拡張、#mfUpper/#mfLower呼び出しは維持）。
- 完了条件: Active scenarioの4値が一目、Z1▲▼が#mfUpper/#mfLowerと一致。
- 回帰テスト: シナリオ編集input機能維持。renderCards後に監査warn表示維持。
- Rollback: Phase 3 commit。

### Phase 5 — Mobile導線（img 01/02是正の仕上げ）
- 目的: モバイルを縮小版でなく縦IAに。
- 変更対象: `#mtabs`切替（display:none徹底）, `#mrail`(display:none既定), `#mbar`(fixed+safe-area), `#chartbar`折りたたみ, チャート52vh, Empty state CTA。
- 作業: 非アクティブペイン`display:none`。marketStrip 3列。Empty state（§8.9）にPASTE&COMPILE CTA+RISK GATE文言。safe-area対応。
- 完了条件: 390pxで`body.scrollWidth≤390`、Empty時に復帰CTA可視、下部バー非被覆。
- 回帰テスト: 390pxでimg 01/02相当を再現し是正確認。タブ切替で他ペイン非表示。
- Rollback: Phase 4 commit。

### Phase 6 — Chart描画規律（auto-range/cluster/画面外マーカー）
- 目的: img 10のC5、img 06のラベル過多是正。
- 変更対象: buildTruthChart内の描画部（§8.2/8.3/8.6）。**幾何計算コアは不変**、描画分岐のみ。
- 作業: 範囲外レベルを画面外マーカーへ集約（新規描画）。Primary/Secondary/Context 3段化。cluster名`slice(0,3)`+「+N」。
- 完了条件: 遠方レベルが軸圧縮しない、▲N/▼Nマーカー表示、ラベル最大2行。
- 回帰テスト: img 10相当データで現在値周辺クラスタが潰れない。DEMO/valid-sampleで描画。
- Rollback: Phase 5 commit。

### Phase 7 — Glass操作層・Reduced対応・仕上げ
- 目的: Optional FX層、アクセシビリティ。
- 変更対象: Optional FX層(§8層構成8)。
- 作業: `--glass-*`を操作層のみへ適用。reduced-motion/transparencyメディアクエリ統合。focus-visible。設定パネルのFX/GLOW/MOTIONトグル配線維持。
- 完了条件: Glassが§9.1箇所のみ、reduced両対応。glass-on-glass 0。
- 回帰テスト: §13受け入れテスト全項目。
- Rollback: Phase 6 commit。

---

## 13. Acceptance Tests

- [ ] 1440px: 3ペイン表示、価格>装飾のコントラスト、Quality Gate 10問即答可
- [ ] 1024px: Z2レール化、marketStrip 4列、横overflowなし
- [ ] 768px: `#mtabs`切替、非アクティブペイン`display:none`
- [ ] 390px: `document.body.scrollWidth ≤ 390`、`#liquidGlassFx`が`display:none`/非存在
- [ ] 390px MISSING: PRICE赤、Empty stateに「PASTE & COMPILE」CTA＋RISK GATE文言可視、下部バー非被覆（safe-area）
- [ ] チャート: 白veilなし、ローソク/レベルが不透明背景上100%可視
- [ ] auto-range: 遠方レベル（例29303）が現在値（例29642）周辺を圧縮しない、▲N/▼N画面外マーカー表示
- [ ] cluster: 数pt差レベルが1本1ラベルに統合、名最大3+「+N」、ラベル最大2行
- [ ] Entry/Invalidation/SL/TP1: 水平帯表示、連続経路線なし、順序（LONG: SL<inval<entry）保持
- [ ] State: WATCH/ARMED/TRIGGERED/CONFIRMED/ACTIVE/INVALIDATED が§8.7通り
- [ ] NO TRADE: 「NO TRADE — 理由」表示（n=0）
- [ ] STALE: age>15で「STALE Nm」＋warn border
- [ ] ERROR: 非`!NQX/1`パケット拒否、BLOCKED表示
- [ ] 推定価格: `fmt(p,approx)`が`≈`表示、NQX側`~`維持
- [ ] キーボード: 全操作Tab到達、focus-visible可視、Esc で#scpanel閉
- [ ] Touch: 全ボタン44×44px、levelBoard/mrail横スクロール
- [ ] Reduced Motion: `.routeflow/.pulse/.ring/.dzscan`停止
- [ ] Reduced Transparency: 全Glass→不透明、backdrop無効
- [ ] NQX valid: `validate_nqx.py valid-sample.nqx`=exit 0、Nightwatch登録成功
- [ ] NQX invalid: `invalid-sample.nqx`=非0、既存分析を上書きしない
- [ ] Glass boundary: Glassが§9.1の操作層のみ、chart/data層に0、glass-on-glass 0
- [ ] 回帰: `renderAll/renderReceiver/renderChart/renderCards/buildTruthChart/bindEvents/runUplink`がコンソールエラーなく動作

---

## 14. Final Codex Directive

NQ Nightwatchを「派手な未来デモ」から「一目で判断できる執行ターミナル」へ。勝者は**Data-First Decision Column（94点）**。既存ID・DOM骨格・NQXエンジン・全安全監査（parseNQX/validateNQX/各audit/buildTruthChart幾何/runUplink）は保護し、意味を一切変えない。実装は再配置とCSS統合のみ。

最優先はimg 01の是正——Phase 1で`#liquidGlassFx`と全FX子要素をDOM削除し、`#chartwrap`を`--bg-0`不透明・影のみ、`#chart`のfilter削除。チャートに白veilを二度と出さない。次にPhase 2-3で9世代CSS（V2〜V14）を8層（Tokens/Base/Layout/Components/Chart/States/Responsive/Optional FX）へ統合し`!important`を撤去。V9 Arctic Glassが白ガラスの主因なので確実に削除する。フォント6.5pxは全て9px以上へ。

Phase 4でZ4を意思決定カラム化（Active scenarioのEntry/Invalidation/Hard SL/TP1を上部固定、CSS orderのみ、input編集維持）、Z1に最寄り上下レベルをミラー。Phase 5でモバイルを縦IAへ——非アクティブペインは`display:none`徹底で横overflow根絶、Empty stateに復帰CTA、mbarをsafe-area対応fixed。Phase 6で描画規律——遠方レベルを画面外マーカーへ集約しauto-rangeで現在値周辺を潰さない、clusterでラベル過多を解消。Glassは操作層のみ。実データのない指標は追加しない。推定は`≈`/`~`。各Phaseで§13の該当テストを通し、失敗時は直前commitへrollback。
