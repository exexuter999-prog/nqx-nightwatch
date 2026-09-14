# R6 — Setup Layer統合設計案

- 上位文書: `project/indicator/NQX_EVIDENCE_BASED_STRATEGY_UPGRADE_BLUEPRINT.md`（5章・8章・9章R6）、`project/strategy/GPT_STRATEGY_UPGRADE_DIRECTIVE.md`（11章）
- 本書の位置づけ: **設計案の提出**（GPT指示書11-3「Nightwatch EDGEパネルは設計案の提出まで」に準拠）。indicator本体・Nightwatch HTML・NQX validatorへの実コード変更はユーザー承認後に別指示で行う。
- 前提条件の確認: 設計書8章1「`VERIFIED`状態のモジュールのみNightwatchへ出力できる」。2026-07-18時点でEdge Ledgerに`VERIFIED`のモジュールは0件（M1-M5すべて`CANDIDATE`、実測未実施）。**したがって本書が提案するadditiveフィールドは、設計上定義するが実際の出力は全経路で恒久的にOFFのまま留まる**（VERIFIED実績が出るまで）。

---

## 1. 統合先と差分最小の原則

対象ファイル: `project/indicator/nqx_swingarm_pressure_v2_geometry_stack.pine`（Full Build 1.0.1、現行output series実測≈60/64）

設計書R6-1の要求「統一インターフェースを1モジュールぶんだけ実装」に従い、初回統合はM1（ORB）1本に限定する。M2以降はM1統合の回帰確認後に同型で追加する。

### 1-1. 出力予算の制約
現行実測が60/64（4枠の余裕）であるため、Setup Layer用の新規Data Window plotを追加する前に、既存の低優先度診断plotを削減して枠を捻出する必要がある（設計書R6-1「output series予算を超える場合は診断plotの削減で捻出し、削減一覧を報告」）。

**削減候補（優先順、影響評価つき）**:
| # | 対象 | 現状の用途 | 削減の影響 |
|---|---|---|---|
| 1 | `NQX_DATA_LTF_CONFIRMED_TOUCH_LEVEL`（974行目相当の並び, 実際は979行目） | 3m LTFのtouchレベルQA用 | UI非表示（data_window専用）。CT/HTFの同種plotで代替監視可能。削減優先度最高 |
| 2 | `NQX_DATA_HTF_FROZEN_COUNT` | HTF frozen zone数のQA | 表(`FZ L/C/H`)に同じ情報が既に表示されている（954行目 `frozenCountText`）ため重複 |
| 3 | `showExtremes`系3本（963-965行目） | 開発時のextremumデバッグ用 | 既定OFF(`showExtremes=false`)。実運用では画面に出ない診断専用 |

上記3本のうち2本削減（表と重複するHTF_FROZEN_COUNTと、LTF_CONFIRMED_TOUCH_LEVEL）で2枠を捻出し、Setup Layer 1モジュール分の新規plot（`setupActive/Dir/Stop/Target`の4本 — `Tag`はstring型でplotできないためlabel/tableで表現）のうち2本をdata_window、2本を可視plotとして追加する設計とする。不足する場合は`showExtremes`系も削減対象に追加する。

---

## 2. 統一インターフェース設計

設計書5-3「各モジュール共通仕様」に定義された`setupActive(bool), setupDir(int), setupStop(float), setupTarget(float), setupTag(string)`を、M1（ORB）1本ぶん実装する場合の具体的なPine実装案。

### 2-1. 計算ロジック（indicator内、strategy呼び出しなし）

```pine
// Setup Layer — M1 ORB (VERIFIED後にのみ有効化される想定の骨格)
// この関数は Edge Ledger の M1 が VERIFIED になるまで setupActive を常に false に固定する。
f_setupM1Orb(bool _edgeVerified, float _orHigh, float _orLow, bool _orReady, bool _inRth, int _ctTrend) =>
    bool active = false
    int dir = 0
    float stop = na
    string tag = "M1_ORB_UNVERIFIED"
    if _edgeVerified and _orReady and _inRth
        bool longBreak = close > _orHigh and close[1] <= _orHigh
        bool shortBreak = close < _orLow and close[1] >= _orLow
        if longBreak
            active := true
            dir := 1
            stop := _orLow
            tag := "M1_ORB"
        else if shortBreak
            active := true
            dir := -1
            stop := _orHigh
            tag := "M1_ORB"
    [active, dir, stop, tag]
```

**設計上の要点**:
- `_edgeVerified`は**ハードコードされた定数ではなく、Edge Ledger状態を反映する単一の`input.bool`**（既定値`false`、ツールチップに「EDGE_LEDGER.mdでM1がVERIFIEDになるまでtrueにしない」と明記）として実装する。ユーザーが誤ってONにしても、それは既存のscore捏造禁止と同格の「明示的な自己責任操作」として扱う（設計書8章1の精神を維持しつつ、Pineには外部ファイル参照機能がないための代替）。
- `setupTarget`はM1の基準形（EODクローズ、ターゲットなし）では未定義。設計書5-3 M1「基準形はEOD」を反映し、当面`na`固定。R倍数ターゲット変形が検証されR-系接頭辞付きで`VERIFIED`になった場合のみ実装する。
- OR box自体の計算（`orHigh`/`orLow`/`orReady`）は`nqx_bt_m1_orb.pine`のロジックと**意図的に重複実装**する。理由: 検証コード(`strategy()`)と本番コード(`indicator()`)を同一ファイルにしない、という設計書5-1原則2の帰結であり、共有関数抽出は将来的にPineライブラリ(`library()`)化で解消できるが本フェーズでは対象外とする。

### 2-2. Risk Gateとの接続

設計書5-1原則3「Setup Layerに独自の発注概念を持たせない」「Risk gate（Hard Stop順序・最小R:R 1.50・NO TRADE）を必ず通る」を実現するため、Setup Layerの出力は**既存Risk Gate計算の入力候補としてのみ**接続し、Risk Gateの判定式自体は変更しない。

```pine
// 既存の baseRiskOrderValid / fullRiskOrderValid 計算(596-603行目相当)と同列に、
// Setup由来のリスク候補を追加する場合の接続案（既存CT/Rejection系統とは独立した第3の経路）
float setupInvalidation = setupActive ? setupStop : na  // Setup自身のstopがinvalidationを兼ねる基準形
float setupHardStop = setupActive and not na(ctAtr) ? (setupDir == 1 ? setupInvalidation - stopBufferAtr * ctAtr : setupInvalidation + stopBufferAtr * ctAtr) : na
bool setupRiskOrderValid = setupActive and not na(setupHardStop) and (setupDir == 1 ? setupHardStop < setupInvalidation and setupInvalidation < close : setupHardStop > setupInvalidation and setupInvalidation > close)
// Target: 基準形はEODのためTargetOrderValidは常にfalse (R:R計算不能) → NO TRADEのまま
// これは「弱い」のではなく、設計書5-1原則3の直接的帰結: EOD手仕舞い戦略はそもそも
// 固定Targetを持たないため、既存の最小R:R 1.50ゲートを機械的に満たせない。
// R倍数ターゲット変形(M1-05等)がVERIFIEDになった場合のみ setupTarget を実値化し、
// 既存 fullRiskOrderValid と同型の判定式を setupTargetOrderValid として追加する。
```

この設計の帰結として、**M1の基準形（EOD手仕舞い）はRisk Gateの最小R:R 1.50を構造的に満たせず、VERIFIEDになったとしても「WATCH」表示止まりでNightwatchのアクティブシナリオには昇格しない**。これは設計上の欠陥ではなく、「EODモジュールは方向性の参考情報であり、独立したエントリーシグナルではない」という性質を正しく反映した結果である。将来的にM1にR倍数ターゲットの変形を追加検証し、それがVERIFIEDになった場合のみエントリー接続が可能になる。

---

## 3. NQX/1 JSON additive フィールド設計

設計書8章2の規定に従い、既存`NQX_ATR_PRESSURE/1`の`CONFIRMED_REJECTION`イベントは不変のまま、Setup Layer由来の別イベントとして additive フィールドを追加する。

### 3-1. フィールド定義

| フィールド | 型 | 条件 | 例 |
|---|---|---|---|
| `setup_type` | string | Setupがactiveな場合のみ出力 | `"ORB"` |
| `setup_evidence` | string | Edge Ledgerエントリid（本書ではモジュールID"M1"をそのまま使う簡易形） | `"M1"` |
| `setup_pf_verified` | float or 省略 | **`setup_type`のEdge Ledger状態がVERIFIEDでない場合、このフィールド自体を出力しない**（設計書8章2「出typeがVERIFIEDでない場合はフィールド自体を出さない」を厳格に実装） | `1.62` |

### 3-2. Pine実装案（JSON生成関数の拡張、既存`f_rejectionJson`は不変）

```pine
// 既存 f_rejectionJson (882行目) は不変。Setup Layer由来のイベントは別関数で生成し、
// 既存CONFIRMED_REJECTIONの文字列結合ロジックに一切触れない。
f_setupEventJson(string _setupType, string _evidence, bool _verified, float _reproducedPf, int _dir, float _stop) =>
    string base = '{"schema":"NQX_ATR_PRESSURE/1","indicator_version":"2.0","event":"SETUP_SIGNAL","setup_type":"' + _setupType + '","setup_evidence":"' + _evidence + '","side":"' + (_dir == 1 ? "LONG" : "SHORT") + '","setup_stop":' + f_jsonNumber(_stop)
    string pfField = _verified ? ',"setup_pf_verified":' + str.tostring(_reproducedPf, "#.##") : ""
    base + pfField + ',"score_is_probability":false}'

// 出力ゲート: emitNqxJson(既存input) AND setupEdgeVerified(新規input, 既定false) AND setupActive
// これによりVERIFIED未満のモジュールはJSON自体を一切生成しない(設計書8章1・受け入れ条件5)。
```

### 3-3. validator拡張の要件（別ファイル、本書はインターフェース定義のみ）
`project/engine-contract/nqx1-spec.md`のvalidatorは現状`SETUP_SIGNAL`イベントを認識しない。設計書8章2「validator拡張が済むまで既定OFF」に従い、以下を満たすまで`emitNqxJson`とは独立した**第二のゲート`input.bool`を追加しON既定にしない**：
1. `scripts/validate_nqx.py`（またはその後継）が`event":"SETUP_SIGNAL"`を認識し、`setup_type`が`M1`等の既知モジュールIDであることを検証する
2. `setup_pf_verified`フィールドが存在する場合、その値が数値かつ有限であることを検証する（`NaN`/`Infinity`の混入を防ぐ）
3. 既存`CONFIRMED_REJECTION`のvalidator regression（既存フィールド30種+additive 4種のexit 0）が壊れていないこと

---

## 4. Nightwatch EDGEパネル設計案（表示層のみ、HTML本体変更はユーザー承認後）

設計書8章4の要求「稼働中モジュール・状態・ローリングPF・PAST PERFORMANCE IS NOT A GUARANTEEを常時表示」を満たすパネル案。

```
┌─ EDGE ─────────────────────────────────────┐
│ M1 ORB       [CANDIDATE]   PF: N/A          │
│ M2 NOISEBAND [CANDIDATE]   PF: N/A          │
│ M3 VWAP      [CANDIDATE]   PF: N/A          │
│ M4 IBS/RSI2  [CANDIDATE]   PF: N/A          │
│ M5 SESSION   [CANDIDATE]   (display only)   │
│──────────────────────────────────────────── │
│ PAST PERFORMANCE IS NOT A GUARANTEE          │
│ OF FUTURE RESULTS.                           │
└───────────────────────────────────────────────┘
```

- 状態バッジの色分け: `CANDIDATE`=灰、`REPRODUCING`=黄、`VERIFIED`=緑、`LIVE-MONITOR`=青、`REJECTED`/`DEPRECATED`=赤（既存のNightwatch安全表示規約 — score非確率表示と同系統のニュートラル配色を踏襲）。
- 2026-07-18時点で全モジュール`CANDIDATE`のため、このパネルは**現状ではすべて「未検証」の状態表示のみ**を行う。PF列は実測が入るまで恒久的に`N/A`。
- `SCORE IS NOT PROBABILITY`と同格の免責文言として`PAST PERFORMANCE IS NOT A GUARANTEE OF FUTURE RESULTS`をパネル最下段に常時固定表示する（設計書8章4)。
- 本パネルはHTML側の実装であり、`project/app/`配下の該当コンポーネントへの実装はユーザー承認後の別指示で行う（GPT指示書11-3）。

---

## 5. 受け入れ条件との対応（設計書10章）

| # | 受け入れ条件 | 本設計での対応状況 |
|---|---|---|
| 1 | E1/E2出典+当方再現なしのPF宣言が存在しない | 本書はPF宣言を一切行っていない。全モジュールCANDIDATEのまま |
| 5 | VERIFIED未満のモジュールがNightwatch/NQX JSONへ出力していない | 3-2/3-3の二重ゲート設計（`emitNqxJson` AND `setupEdgeVerified`）で構造的に保証 |
| 6 | Risk gate・排他・NO TRADE・SCORE IS NOT PROBABILITYが全経路で維持 | 2-2でSetup Layerの出力を既存Risk Gate入力候補としてのみ接続し、判定式自体を変更しない設計とした |
| 10 | 既存ファイルが本設計の実装で破壊されていない | 本書は設計案のみで実コード変更なし。実装時は`f_rejectionJson`等の既存関数を一切変更せず、新規関数の追加のみで統合する方針を明記 |

---

## 6. 実装時のTODO（Codex/次回実装者向け、本書は設計までで実装しない）

1. `nqx_swingarm_pressure_v2_geometry_stack.pine`への実コード追加は、Edge LedgerでM1が最低`REPRODUCING`（理想は`VERIFIED`）になってから着手する。CANDIDATEのまま骨格コードだけ埋め込むことは可能だが、`setupEdgeVerified`が恒久的にfalseな状態で表示テストのみ行う。
2. 出力予算捻出（1-1節の削減）を先に実施し、TradingView MCPで削減後のoutput series実数をコンパイルログから確認して報告する。
3. validator拡張（3-3節）は`project/engine-contract/`配下の別タスクとして先行させ、拡張版validatorがexit 0で既存回帰を通ることを確認してからindicator側の`emitNqxJson`経路と接続する。

---
*本書はR6フェーズの設計案であり、実コード変更を含まない。全ての数値・PFはプレースホルダーであり、宣言に使用してはならない（設計書0章）。*
