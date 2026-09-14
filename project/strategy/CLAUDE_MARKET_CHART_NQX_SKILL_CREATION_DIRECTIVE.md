# Claude向け 市場データ・チャート構造分析・NQX生成スキル 作成指示書

発行日: 2026-07-24  
対象: Claude Code  
指示種別: 実装指示書  
成果物名: `mnq-nightwatch-nqx`

---

## 0. この指示書の目的

Claude Codeに、NQ/MNQ先物について次の作業を一貫して行う再利用可能なClaude Skillを作成させる。

1. スクリーンショット、OHLCV、価格レベル、出来高プロファイル、イベント、VIX、オプション文脈を証拠として取り込む
2. 観測事実、インジケータ主張、推論、仮定、判読不能を分離する
3. 3分・15分・45分を中心に、チャート構造、オークション状態、レジーム、重要レベルを分析する
4. LONG、SHORT、FLATを同じ証拠で比較する
5. NightwatchのNQX/1パケットを生成、検証、監査、修復する
6. 観測済みキャンドルと条件付き予測形状を厳格に分離する
7. 無効な生成物で既存の有効分析を上書きしない

このタスクはスキル作成のみを対象とする。Nightwatch本体、NQXエンジン、検証器、既存仕様書の挙動を変更してはならない。

---

## 1. 絶対原則

作成するスキルは、以下を例外なく守ること。

- Market Truth First
- Levels Before Routes
- No Synthetic Future
- State-Driven Rendering
- Risk Is Geometry
- 読めない価格を捏造しない
- 推定価格には `~` を付ける
- 観測OHLCと条件付き予測形状を混同しない
- 時間足ごとの価格は非同期スナップショットとして扱い、平均価格へ混ぜない
- インジケータが表示する確率、スコア、状態は「インジケータ主張」であり観測事実ではない
- VIXは市場ストレス文脈であり、gammaの代用ではない
- 予測形状は表示専用であり、判断、現在値、トリガー、信頼度、R:R、リスク、検証へ入力しない
- NQX/1のバージョン拒否、排他、R:R、Invalidation、イベント、鮮度、安全ゲートを維持する
- 無効パケットは返却してもよいが、既存の有効分析へ適用してはならない
- シナリオが0件でも正常とする。証拠が足りない場合は明示的に `NO TRADE` または `FLAT` とする

---

## 2. 作業前に読む資料と権威順位

### 2.1 プロジェクトルート

```text
C:\Users\exexu\Downloads\nq-nightwatch-claude-code-handoff\project
```

### 2.2 必須読了順

次の順に全文を読むこと。見出しだけで判断してはならない。

1. `00_MISSION.md`
2. `engine-contract\nqx1-spec.md`
3. `engine-contract\validate_nqx.py`
4. `app\nq-nightwatch-nqx-final.html`
5. `strategy\GPT_SL_GATE_UPGRADE_DIRECTIVE.md`
6. `strategy\GPT_SL_GATE_UPGRADE_DIRECTIVE_ADDENDUM.md`
7. `strategy\GPT_SL_GATE_UPGRADE_DIRECTIVE_ADDENDUM2.md`
8. `strategy\GPT_SL_GATE_UPGRADE_DIRECTIVE_ADDENDUM3.md`
9. `strategy\GPT_OHLC_EXTRACT_MODE_DIRECTIVE.md`
10. `indicator\NQX_EVIDENCE_BASED_STRATEGY_UPGRADE_BLUEPRINT.md`
11. `indicator\NQX_SWINGARM_PRESSURE_V2_BLUEPRINT.md`

その後、利用可能なら次も読む。

```text
C:\Users\exexu\.codex\skills\mnq-nightwatch-nqx\SKILL.md
C:\Users\exexu\.codex\skills\mnq-nightwatch-nqx\references\nqx1.md
C:\Users\exexu\.codex\skills\mnq-nightwatch-nqx\references\regime-gamma-policy.md
C:\Users\exexu\.codex\skills\mnq-nightwatch-nqx\references\evidence-log.md
C:\Users\exexu\.codex\skills\mnq-nightwatch-nqx\references\research-basis.md
```

### 2.3 競合時の権威順位

資料間に食い違いがある場合は、黙って片方を採用してはならない。スキル内の `references/source-precedence.md` に競合と採用理由を記録する。

権威順位は次の通り。

1. 現行 `validate_nqx.py` が実際に受理・拒否するNQX契約
2. 現行HTMLの実装関数が実際に行う受信、検証、描画、安全処理
3. 現行 `nqx1-spec.md` の伝送仕様
4. 追補指示書のうち、後の日付・後番号のもの
5. 旧設計書、旧スキル、説明資料

### 2.4 既知の競合

条件付き予測形状について、現行仕様書の古い表現と最新HTML実装に差がある。スキルでは次を正とする。

- 観測キャンドルと予測キャンドルは同じ価格スケールへ描く
- 両者の境界に明確な白い `NOW` 境界線を置く
- データクラスは完全に分離する
- 予測価格はすべて `~` 付き
- 推奨形状は連続する3〜6本
- `S.pc` がなければ形状を生成しない
- 形状は表示専用で、NQX判断ロジックへ逆流させない

このスキル作成タスク内で、既存仕様書を勝手に書き換えてはならない。競合を参照資料へ明記するだけに留める。

---

## 3. Claude公式スキル作成手順の使用

Claude公式の `skill-creator` を使用すること。利用可能な場合は、次の指示を全文確認してから作業する。

```text
C:\Users\exexu\.claude\plugins\marketplaces\claude-plugins-official\plugins\skill-creator\skills\skill-creator\SKILL.md
```

公式検証器の想定パス:

```text
C:\Users\exexu\.claude\plugins\marketplaces\claude-plugins-official\plugins\skill-creator\skills\skill-creator\scripts\quick_validate.py
```

スキル本体の `SKILL.md` は500行未満を目安とし、詳細契約は `references\` へ分離する。README、CHANGELOG、INSTALL_NOTESなど、スキル実行に不要な文書は作らない。

---

## 4. 成果物と配置

### 4.1 正本

```text
C:\Users\exexu\Downloads\nq-nightwatch-claude-code-handoff\project\claude-skills\mnq-nightwatch-nqx
```

### 4.2 Claudeへのインストール先

```text
C:\Users\exexu\.claude\skills\mnq-nightwatch-nqx
```

正本を完成・検証した後にインストール先へ複製する。両者のファイルハッシュが一致することを確認する。

既存のインストール先が存在する場合、削除や上書きを先に行ってはならない。タイムスタンプ付きバックアップを作り、バックアップ先を最終報告に記載する。

### 4.3 必須構成

```text
mnq-nightwatch-nqx/
├── SKILL.md
├── references/
│   ├── source-precedence.md
│   ├── market-data-evidence.md
│   ├── chart-structure.md
│   ├── nqx1-current.md
│   ├── projection-shape.md
│   ├── event-volatility.md
│   ├── regime-gamma-policy.md
│   ├── evidence-ledger.md
│   └── research-basis.md
├── scripts/
│   └── validate_nqx.py
└── evals/
    ├── evals.json
    └── fixtures/
```

---

## 5. `SKILL.md` のフロントマター

次のフロントマターを基準にする。`name` は変更しない。ツールを不必要に制限する `allowed-tools` は追加しない。

```yaml
---
name: mnq-nightwatch-nqx
description: Analyze NQ/MNQ futures from chart screenshots, exact OHLCV, level maps, volume profiles, event context, VIX, and options-gamma evidence; extract observed market data; classify chart structure and regime; compare LONG, SHORT, and FLAT; and generate, validate, audit, or repair Nightwatch NQX/1 packets plus display-only conditional ~shape tapes. Use whenever the user supplies NQ/MNQ market evidence or asks for Nightwatch, NQX, chart structure, levels, scenarios, OHLC extraction, VIX/event context, packet generation, validation, or repair, even when NQX is not explicitly named.
---
```

説明文は、以下の曖昧な依頼でもスキルが起動するよう維持する。

- 「このMNQチャートを分析して」
- 「NQXで出して」
- 「前の分析につなげて」
- 「OHLCを抜いて」
- 「チャート構造はどうなっている？」
- 「イベントとVIXも確認して」
- 「このNQXを直して」
- 「予測キャンドルを出して」

---

## 6. `SKILL.md` 本文に必須のルーティング

### 6.1 リクエストモード

スキルは最初に依頼を次のモードへ分類する。

1. `ANALYZE`  
   市場データとチャート構造を分析し、必要ならNQXを作る。

2. `NQX_ONLY`  
   貼り付け可能なNQX/1だけを返す。

3. `OHLC_EXTRACT`  
   1枚のチャートから、判読できる確定足だけを `O|c=` へ変換する。

4. `AUDIT_REPAIR`  
   既存NQXを検証し、意味を変えずに最小修復する。

5. `UPDATE`  
   新しい証拠を前回分析へ接続し、維持・強化・弱化・無効化を判定する。

6. `OUTCOME_LOG`  
   凍結済みシナリオの結果を記録する。事後的に条件を書き換えない。

明示されていない場合は `ANALYZE` を選ぶ。追加質問なしで安全に進められる場合は、合理的な仮定を明示して進める。

### 6.2 参照ファイルの読み分け

`SKILL.md` には、次のルーティングを明記する。

- 常時読む: `source-precedence.md`、`market-data-evidence.md`
- チャート画像または構造分析: `chart-structure.md`
- NQX生成・監査・修復: `nqx1-current.md`
- 予測形状を要求された場合のみ: `projection-shape.md`
- イベント、VIX、gammaが関係する場合のみ: `event-volatility.md`、`regime-gamma-policy.md`
- ログ作成時のみ: `evidence-ledger.md`
- 根拠説明や設計監査時のみ: `research-basis.md`

---

## 7. 市場データ証拠契約

`references/market-data-evidence.md` に、以下を具体的に記述する。

### 7.1 証拠ラベル

各主張を次のいずれかへ分類する。

- `OBSERVED`: 画像または数値データから直接確認
- `INDICATOR_CLAIM`: インジケータが表示する状態、確率、スコア、ラベル
- `INFERRED`: 複数の観測から導いた解釈
- `ASSUMED`: 分析継続のための明示的仮定
- `UNREADABLE`: 存在は見えるが正確に読めない
- `MISSING`: 証拠自体がない

`INDICATOR_CLAIM` を `OBSERVED` へ昇格させてはならない。

### 7.2 スクリーンショット

- 画像ごとにシンボル、時間足、表示時刻、価格、右端が確定足か進行中かを確認する
- 複数時間足の価格差は非同期取得として保持する
- 価格差を平均して「現在値」を作らない
- 読めない桁を補完しない
- 画像だけの場合、品質モードは仕様どおり screenshot-only とする
- 正確な価格が必要なNQX数値欄へ、推定値を無印で入れない
- 推定を許す表示専用形状以外では、`MISSING`、省略、または分析本文の `~` 表記を使う

### 7.3 OHLC抽出モード

`OHLC_EXTRACT` では次を守る。

- 対象は原則として1枚のチャート、1時間足
- 直近の完全に確定した足を最大12本、古い順で出す
- 少なくとも3本を確信を持って読めない場合は `O|c=MISSING`
- 進行中の最新足は除外する
- バンド、雲、予測形状、出来高プロファイルからOHLCを復元しない
- 推測、補間、平均化で観測OHLCを作らない
- 各足で `HIGH >= max(OPEN,CLOSE)`、`LOW <= min(OPEN,CLOSE)`、`HIGH >= LOW` を確認する
- 出力は1行のみとし、全NQXパケットへ勝手に置換しない

正規伝送形式:

```text
O|c=TIME,OPEN,HIGH,LOW,CLOSE[,VOLUME];TIME,OPEN,HIGH,LOW,CLOSE[,VOLUME]
```

内部実装用のパイプ区切り表現と、外部伝送用のカンマ区切り表現を混同しない。

### 7.4 鮮度と出典

全データについて可能な限り次を保持する。

- source
- observed_at / as_of
- timezone
- delayed / realtime / unknown
- freshness
- transformation

リアルタイムMNQフィードへアクセスできない場合、アクセスできるふりをしない。ユーザー提供画像または明示された遅延データを証拠として扱う。

---

## 8. チャート構造分析契約

`references/chart-structure.md` に、以下の分析順を記述する。

### 8.1 分析順

1. 証拠インベントリ
2. 現在値と各時間足の取得時刻
3. 観測価格レベルの抽出
4. レベルの重複、近接、優先順位
5. 値位置とオークション状態
6. スイング構造
7. レジーム
8. LONG、SHORT、FLATの比較
9. 条件、Invalidation、Risk、Target
10. NQX符号化と検証

ルートを先に作り、後からレベルを当てはめてはならない。

### 8.2 標準時間足の役割

標準構成は次とする。

- 3分: LTF、トリガー、短期受容・拒否、実行微細構造
- 15分: CTF、主たるオークションとシナリオ設計
- 45分: HTF、構造・文脈・上位バイアス

他の時間足が提供された場合は実際の時間足を使い、無理に3/15/45へ変換しない。

### 8.3 抽出対象

- 直近高値・安値とスイング系列
- 高値切り上げ/切り下げ、安値切り上げ/切り下げ
- ブレイク、受容、拒否、フェイルドブレイク
- レンジ上端、下端、中央値
- セッション高安、前日高安、週次・月次レベル
- POC、VAH、VALとvalue migration
- 出来高集中、低出来高通過帯。ただし画像から明確に読める場合のみ
- ATR/pressure zone、cloud、scoreはインジケータ主張として別管理
- イベント前後の状態変化
- 各時間足の整合、一致、不一致

### 8.4 レベル優先順位

同一価格帯に複数の独立した観測レベルが重なる場合は、名前を列挙してconfluenceとして扱う。近接しているだけのレベルを1つの精密価格へ捏造統合しない。

優先度は、少なくとも以下を評価する。

- 上位時間足
- 複数セッション・複数期間の重複
- 現在値からの距離
- 直近の受容・拒否
- 出来高プロファイルとの一致
- イベント前後の再評価

### 8.5 レジーム

最低限、次のいずれかへ分類する。

- TREND
- ROTATION
- TRANSITION
- EXPANSION
- COMPRESSION
- EVENT-RISK
- UNKNOWN

レジームは単独の売買方向ではない。方向と状態を分けて記述する。

### 8.6 LONG・SHORT・FLAT比較

各候補について同じ項目を比較する。

- supporting evidence
- conflicting evidence
- trigger
- entry geometry
- invalidation
- structural stop
- target
- R:R
- event risk
- reason to stand down

FLATを消極的な残余ではなく、独立候補として採点する。

---

## 9. NQX/1生成・検証契約

`references/nqx1-current.md` は、現行 `engine-contract\nqx1-spec.md` を基礎に作る。旧Codexスキルの `nqx1.md` をそのままコピーしてはならない。

### 9.1 必須動作

- 先頭行は厳密に `!NQX/1`
- 未知バージョンを拒否
- レコード順、必須キー、列挙値、数値制約を維持
- `D / M / C / Z / E / O / G / S` の現行契約を保持
- `G.n` と実際のシナリオ数を一致させる
- 既存パケット修復時は、未知フィールドを可能な限り保持する
- 意味変更が必要な場合は修復ではなく再分析として扱う
- 生成後、必ず `scripts\validate_nqx.py` を実行する
- 検証終了コード0以外のパケットを「完成」と報告しない
- 無効パケットで既存の有効分析を上書きしない

### 9.2 排他

LONGとSHORTのエントリー帯が重なる場合、曖昧な両建て候補として返さない。現行仕様の `G.mx` と `STAND DOWN` を適用する。

### 9.3 InvalidationとSL

LONG:

```text
SL < Invalidation < Entry Low
```

SHORT:

```text
Entry High < Invalidation < SL
```

SLは「許容損失額から逆算しただけの価格」ではなく、構造の外側へ置く。

現行安全ゲートを維持する。

- `SL_INVALIDATION_TOO_CLOSE`
- `SL_STRUCTURAL_DISTANCE`

構造距離単位は、現行HTMLと検証器に合わせる。

1. 受理済み観測OHLCが3本以上ある場合  
   直近最大12本のTrue Range単純平均

2. 観測OHLCが不足する場合  
   Zレコード全レベルの隣接間隔中央値 × `0.42`

Invalidationとアンカーの距離だけをproxyにしてはならない。

### 9.4 R:R

- entry midpointを基準に計算
- LONG/SHORTで符号を逆転させない
- TP1が1R未満なら現行仕様どおり格下げまたは拒否
- リスク距離0、負値、順序違反を拒否

### 9.5 品質と信頼度

- screenshot-only時の品質モード制限を維持
- 画像だけで精密な成功確率、期待値、サイズを捏造しない
- 因果連鎖に `ASSUMED` が入る場合、現行仕様の信頼度上限とPRIMARY禁止を適用
- `vu`、`eh`、`fm`、`sd`、`od` など現行必須フィールドを落とさない

### 9.6 出力モード

ユーザーが「NQXだけ」「貼り付け用」と指定した場合:

- 説明文を付けない
- コードフェンスを付けない
- NQX本文だけを返す

通常分析では、先に結論、次に重要レベル、次に条件変化、最後に検証済みNQXを示す。

---

## 10. 条件付き予測形状契約

`references/projection-shape.md` に以下を記述する。

### 10.1 目的

予測形状は将来価格の断定ではなく、現在のシナリオ条件をチャート形状へ翻訳し、受容・拒否・押し戻し・継続を視覚的に理解しやすくする表示補助である。

### 10.2 データ分離

- 観測OHLCは `O.c`
- 条件付き予測形状は `S.pk / S.pb / S.pc / S.pu`
- 両者を同じ配列、同じ真偽フラグ、同じ証拠クラスへ入れない
- `S.pc` を観測OHLCとして再利用しない
- 予測形状から現在値、レベル、trigger、invalidation、SL、TP、R:Rを再計算しない

### 10.3 形状表現

推奨は3〜6本の連続バー。

```text
+1,~OPEN,~HIGH,~LOW,~CLOSE;+2,~OPEN,~HIGH,~LOW,~CLOSE
```

条件:

- 連番である
- 全価格に `~`
- `HIGH >= max(OPEN,CLOSE)`
- `LOW <= min(OPEN,CLOSE)`
- 同じ値幅・同じ実体を機械的に反復しない
- `ADVANCE / DECLINE / ROTATION`
- `DIRECT / PULLBACK / TWO-WAY`
- `EXPANDING / COMPRESSING / STEADY`

上記の組み合わせで、シナリオの形態を表す。

### 10.4 スケール

十分な観測OHLCがある場合、直近最大12本の平均レンジを表示スケールの参考にしてよい。ただし、これは予測形状の見た目専用である。

この平均レンジを次へ使用してはならない。

- structural stop
- risk
- R:R
- confidence
- success probability
- scenario grade
- validator pass/fail

正確なアンカー価格がない場合は `S.pc` を省略する。チャートの見た目だけから無印の未来価格を生成しない。

### 10.5 表示

- 観測と予測は同じ価格面
- 白い `NOW` 境界
- 予測H/L envelope
- 予測close path
- 条件付きであることを常時表示
- invalidated時は明確に終了・無効化
- reduced motion、reduced transparency、VFX offでも意味が残る

---

## 11. イベント・VIX・gamma契約

`references/event-volatility.md` と `references/regime-gamma-policy.md` に以下を記述する。

### 11.1 イベント調査

時刻依存のイベント情報は、可能なら実行時に調査する。一次情報を優先する。

- Federal Reserve
- BLS
- BEA
- U.S. Treasury
- CME Group
- Cboe
- 対象企業の公式IR

各イベントに以下を持たせる。

- event name
- exact time
- timezone
- verified / unverified
- primary source URL
- checked_at
- expected impact window

一次URLと時刻がないイベントを `verified` にしない。Webへアクセスできない場合は、調べたふりをせず `UNVERIFIED` または `MISSING` とする。

### 11.2 VIX

- VIX値、変化、as-of、source、freshnessを分離して記録
- 遅延値は遅延と明記
- VIXだけでMNQの方向を決めない
- VIXからgammaを推定しない。gamma regimeの代用にも使わない
- VIXはposition sizing、event-risk、volatility contextの補助に限定

### 11.3 gamma

gamma情報は、明示的な出典、時刻、対象満期、主要レベルがある場合のみ使う。取得できない場合は `gamma=UNKNOWN` とし、代替指標で埋めない。

---

## 12. 検証器の組み込み

次のファイルを、意味を変更せずスキルへコピーする。

```text
コピー元:
C:\Users\exexu\Downloads\nq-nightwatch-claude-code-handoff\project\engine-contract\validate_nqx.py

コピー先:
<skill-root>\scripts\validate_nqx.py
```

手入力で再実装せず、ファイルコピーを行う。コピー後にSHA-256を比較し、一致を確認する。

スキルはNQX生成後、概ね次の形で検証器を実行する。

```powershell
python "<skill-root>\scripts\validate_nqx.py" "<packet-path>"
```

標準入力も利用可能なら維持する。

終了コード:

- `0`: 合格
- `1`: NQX検証不合格
- `2`: 使用方法またはI/Oエラー

検証器を通すために値を捏造してはならない。証拠不足ならシナリオを減らすか、0件とする。

---

## 13. 評価セット

`evals\evals.json` をClaude公式skill-creatorの現行形式で作成する。少なくとも以下の9ケースを含める。

### Eval 1: 複数時間足の画像分析

入力:

- MNQ 3分、15分、45分の非同期スクリーンショット
- 各画像の表示価格が少し異なる

期待:

- 価格を平均しない
- 3M/15M/45Mを役割分担
- OBSERVEDとINDICATOR_CLAIMを分離
- LONG、SHORT、FLATを比較

### Eval 2: screenshot-only NQX

入力:

- OHLC数値列なし
- レベルだけ判読可能

期待:

- `O|c=MISSING`
- screenshot-only品質制限
- 判読不能価格を補完しない
- 最終NQXが検証器を通る

### Eval 3: OHLC抽出専用

入力:

- 15分足1枚
- 直近8本の確定OHLCが明瞭
- 最新足は進行中

期待:

- 進行中足を除外
- 古い順
- `O|c=` の1行だけ
- 全NQXへ置換しない

### Eval 4: OHLC判読不能

入力:

- ローソク形状は見えるが価格ラベルが不足

期待:

- `O|c=MISSING`
- 見た目からOHLCを復元しない

### Eval 5: NQX修復

入力:

- 現行NQXに未知フィールドあり
- 1つの既知フィールドだけ形式違反

期待:

- 最小修復
- 未知フィールド保持
- 検証器合格

### Eval 6: SL構造距離

入力:

- Zレベルに近すぎるSL
- 観測OHLCなし

期待:

- mapped level spacing中央値 × 0.42
- `SL_STRUCTURAL_DISTANCE` を検出
- 不正なパケットを適用しない

### Eval 7: イベント・VIX

入力:

- 出典のないVIX値
- URLのないイベント時刻

期待:

- VIXをUNVERIFIEDまたはSTALE扱い
- イベントをverifiedにしない
- gammaを推定しない

### Eval 8: 条件付き予測形状

入力:

- 観測OHLCあり
- 条件付き上昇後の押し戻しシナリオ

期待:

- 3〜6本の連続 `~OHLC`
- 非均一なバー形状
- `NOW` 境界前提
- 形状がrisk/R:R/confidenceへ影響しない

### Eval 9: 形状を作れないケース

入力:

- 現在値またはアンカーが判読不能

期待:

- `S.pc` を省略
- synthetic futureを作らない
- 必要ならNO TRADE

各evalには、文字列一致だけでなく、以下の客観条件を可能な範囲で付ける。

- NQX検証器の終了コード
- 禁止語・禁止フィールドの不在
- `~` の有無
- シナリオ数一致
- OHLC順序
- 出典・鮮度ラベル
- UNKNOWNまたはMISSINGの正しい使用

---

## 14. 回帰テスト

### 14.1 スキル構造

公式検証器を実行する。

```powershell
$creator = "C:\Users\exexu\.claude\plugins\marketplaces\claude-plugins-official\plugins\skill-creator\skills\skill-creator"
python "$creator\scripts\quick_validate.py" "C:\Users\exexu\Downloads\nq-nightwatch-claude-code-handoff\project\claude-skills\mnq-nightwatch-nqx"
```

### 14.2 NQX検証器

プロジェクト内の既存fixtureを探索し、少なくとも次の基準を確認する。

- `valid-sample.nqx`: 合格
- `test-sl-structural-distance.nqx`: `SL_STRUCTURAL_DISTANCE` を含む不合格
- `test-sl-too-close.nqx`: 現行期待値どおり
- `test-sl-supplied-ohlc.nqx`: 合格し、SUPPLIED OHLC経路を使用

ファイル名または配置が変わっている場合は、`rg --files` で探索する。期待値を都合よく変えず、現行検証器の実結果と指示書の差を報告する。

### 14.3 スキル有無の比較

少なくともEval 1、2、5、8について次を比較する。

- skillあり
- skillなしのbaseline

比較観点:

- 読めない価格の捏造
- 観測と推論の分離
- FLAT候補の有無
- NQX検証成功
- 予測形状の逆流
- イベント・VIXの出典管理

### 14.4 インストール一致

正本とインストール先の各ファイルについてSHA-256を比較する。差分が1つでもあれば完了扱いにしない。

---

## 15. 禁止事項

- Nightwatch本体HTMLの変更
- `validate_nqx.py` の独自改変
- `nqx1-spec.md` の無断変更
- 旧Codexスキルの盲目的コピー
- 読めない価格の補完
- 画像のローソク形状から精密OHLCを逆算
- 非同期時間足価格の平均
- VIXからgammaを推定
- インジケータ確率を実現確率として出力
- 予測キャンドルを観測キャンドルとして保存
- 予測形状を現在値、trigger、risk、R:R、confidenceへ使用
- validatorを通すためのダミー値追加
- invalid packetによる既存分析の上書き
- LONG/SHORTを必ず1つずつ作ること
- README、CHANGELOGなど不要文書の追加
- 既存スキルの無断削除

---

## 16. 完了条件

以下をすべて満たした場合のみ完了とする。

- [ ] 指定正本ディレクトリに必須構成がある
- [ ] `SKILL.md` のfrontmatterが有効
- [ ] descriptionが全ユースケースを起動できる
- [ ] `SKILL.md` が詳細参照を適切にルーティングする
- [ ] 市場データ証拠ラベルが定義されている
- [ ] 3M/15M/45Mの標準役割が定義されている
- [ ] LONG/SHORT/FLATの対称比較が定義されている
- [ ] OHLC抽出専用モードがある
- [ ] NQX生成後のvalidator実行が必須化されている
- [ ] SL構造距離の2経路が現行実装と一致する
- [ ] イベント、VIX、gammaの出典・鮮度契約がある
- [ ] 観測OHLCと条件付き予測形状が完全分離されている
- [ ] 予測形状が同一価格面＋白いNOW境界を前提とする
- [ ] 予測形状が判断系へ逆流しない
- [ ] 9件以上のevalがある
- [ ] 公式 `quick_validate.py` が合格
- [ ] NQX回帰テスト結果が記録されている
- [ ] 正本とインストール先のハッシュが一致
- [ ] Nightwatch本体、仕様書、検証器に意図しない変更がない

---

## 17. Claude Codeの最終報告形式

実装後は、以下だけを簡潔に報告する。

1. 作成した正本パス
2. インストール先パス
3. 作成ファイル一覧
4. バックアップの有無とパス
5. `quick_validate.py` の結果
6. NQX回帰テストの結果
7. evalのskillあり/baseline比較
8. 正本とインストール先のハッシュ一致結果
9. 既知の仕様競合と採用した権威
10. 変更していないNightwatch本体ファイルの確認

完了していない項目を「完了」と表現してはならない。検証失敗時は、失敗した具体的条件、対象ファイル、再現コマンドを報告する。
