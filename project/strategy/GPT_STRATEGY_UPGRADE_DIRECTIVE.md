# GPT改良指示書 — NQX Evidence-Based Strategy Upgrade 実行指令

- 宛先: GPT（実装担当）
- 発行: Fable（設計担当）/ 承認: ユーザー
- 上位文書: `project/indicator/NQX_EVIDENCE_BASED_STRATEGY_UPGRADE_BLUEPRINT.md`（以下「戦略設計書」。本指示書と矛盾する場合は**戦略設計書が優先**）
- 本指示書の目的: 戦略設計書のPhase R1〜R6を、**追加質問なしで**、安全条件を一切破らずに実行させること

---

## 0. あなた（GPT）の役割と行動規範

### 0-1. 役割
あなたは実装担当である。設計判断は戦略設計書に従い、独自の設計変更を行わない。裁量が必要な箇所は本指示書が個別に許可範囲を定める。許可範囲外の判断が必要になったら、**作業を止めず**その項目を `BLOCKED` として報告書（9章）に記録し、次の独立タスクへ進め。

### 0-2. 絶対規範（違反は全成果物の無効化に相当する）
1. **数値を発明しない**。PF・勝率・DD・取引数・引用文の数値は、(a) 実際に読んだ一次資料、(b) 自分で実行したバックテストの出力、のどちらかからのみ書く。記憶・推測からの補完は禁止。出典を読めなかったら「未読」と書く。
2. **読んでいない資料を読んだことにしない**。アクセス失敗（403・bot壁・paywall）は失敗として記録し、代替経路（10-3）を試し、それでも不可なら `BLOCKED-SOURCE` とする。
3. **保護されたソースコードを複製しない**。SwingArm High Pressure V6.8等の非公開インジケータの内部実装を推測で再現しない。clean-room原則は全フェーズに適用。
4. **後付け検証の禁止**。バックテストのバリアントは実行**前**にTRIAL_REGISTRY（6-2）へ登録する。登録なしに実行した結果は採用判定に使えない。
5. **成果の粉飾禁止**。合格基準に届かなかった結果はREJECTEDとして数値ごと記録する。REJECTED記録は成果である。
6. 質問で作業を止めない。不明点は「明示的な仮定」として記録して進み、仮定一覧を報告書に集約する。
7. すべての作業報告は日本語。コード・ファイル名・技術用語は原語のまま。

### 0-3. 会話をまたぐ再開手順
新しいセッションで作業を再開する場合、必ずこの順に読み直してから着手する:
1. 本指示書
2. `project/strategy/GPT_IMPLEMENTATION_REPORT.md`（自分の前回までの報告）
3. `project/strategy/EDGE_LEDGER.md` と `project/strategy/TRIAL_REGISTRY.md`
4. 戦略設計書の該当フェーズ章

---

## 1. 必読ファイル（着手前に全読了し、報告書に読了宣言を書く）

| 順 | ファイル | 読む目的 |
|---|---|---|
| 1 | `project/indicator/NQX_EVIDENCE_BASED_STRATEGY_UPGRADE_BLUEPRINT.md` | 実行対象の設計全体。特に0章（宣言条件）・J章（検証パイプライン）・I章（Edge Ledger） |
| 2 | `project/indicator/NQX_SWINGARM_PRESSURE_V2_BLUEPRINT.md` | 既存構造層（SwingArm V2）の設計思想と安全条件 |
| 3 | `project/indicator/NQX_SWINGARM_V2_SLICE_HANDOFF.md` | 現行実装の到達点（Full Build 1.0 / Resource-safe patch 1.0.1）。リソース予算の実測値 |
| 4 | `project/indicator/nqx_swingarm_pressure_v2_geometry_stack.pine` | 統合先の実コード。Setup Layerが接続する境界を確認 |
| 5 | `project/indicator/NQX_ATR_PRESSURE_MAP_SPEC.md` + `../../docs/reports/CODEX_IMPLEMENTATION_REPORT.md` | 旧系の教訓（RE10140プロット上限超過、stale問題等） |
| 6 | `project/engine-contract/nqx1-spec.md` | NQX/1 packet契約。additiveフィールドの追加規則 |
| 7 | `project/00_MISSION.md`, `project/03_DELIVERABLE_CONTRACT.md` | プロジェクト共通規約 |

**変更禁止ファイル（読み取り専用）**: `nqx_atr_pressure_map_v1.pine`、`nqx_swingarm_pressure_v2_slice.pine`、Nightwatch HTML本体、NQX validator、上記必読の全設計書。あなたが新規作成・編集してよいのは `project/strategy/` 配下と、Phase R6で明示指定するファイルのみ。

---

## 2. 環境と前提事実

- リポジトリ: `C:\Users\exexu\Downloads\nq-nightwatch-claude-code-handoff`
- TradingView: デスクトップ版が稼働し、TradingView MCP（78ツール）が接続済み。主要ツール: `pine_set_source` / `pine_smart_compile` / `pine_get_errors` / `data_get_strategy_results` / `chart_set_symbol` / `chart_set_timeframe` / `replay_*`
- 検証銘柄: `MNQ1!`（主）、`NQ1!`（突合）。MNQ仕様: 1ポイント=$2、1tick=0.25ポイント=$0.50、RTH 09:30–16:00 ET
- タイムゾーン: セッション判定は必ず `"America/New_York"` を明示指定（チャートTZ・JSTに依存させない）
- Pine: v6。過去の実測でoutput series上限64超過事故（RE10140）があるため、strategyファイルでもplot数を常に数える
- サブエージェント/並列AIの利用可否はあなたの環境に依存する。本指示書は単独逐次実行で完遂できるよう書かれている

---

## 3. ミッション全体像

```
R1 一次資料精読 ──▶ R2 ハーネス構築 ──▶ R3 M1(ORB)検証 ──▶ R4 M2/M3検証 ──▶ R5 M4/M5検証 ──▶ R6 統合
   (E2暫定の解消)      (計測装置)          (最重要の問い)       (momentum/VWAP)    (日足系)         (VERIFIEDのみ)
```

最重要の問い（R3で最初に数値を出す）: **「MNQの実コスト($3.00/往復 baseline)環境で、素のORBは生き残るか」**。生き残らない場合、それはREJECTED記録として第一級の成果である（戦略設計書12章）。

---

## 4. Phase R1 — 一次資料精読（推定所要: 資料アクセス次第）

### 4-1. 対象と抽出スキーマ
以下の各資料を読み、資料ごとに `project/strategy/SOURCE_NOTES/` へ1ファイル（`R1_<slug>.md`）を作成する。**全資料共通の抽出スキーマ**:

```
- 書誌: タイトル / 著者 / 年 / URL / アクセス日 / アクセス方法
- 検証市場・期間・データ解像度
- ルール定義（エントリー/エグジット/ストップ/ポジションサイズ/時間制約を逐語で）
- 報告指標: PF / 累積・年率リターン / Sharpe / 最大DD / 取引数 / 勝率（原文の数値のみ。無い指標は「記載なし」）
- コスト処理: 手数料 / スリッページ / 借株・金利（原文の記述を引用）
- 方法論の弱点（自分の評価。期間依存・多重検定・データマイニングの兆候）
- NQX統合適性（戦略設計書5章のどのモジュールに対応するか）
- 格付け判定: E1 / E2 / E2(暫定のまま) / 読めず(BLOCKED-SOURCE)
```

### 4-2. 対象リスト（優先順）
1. SSRN 4416622 — Zarattini & Aziz "Can Day Trading Really Be Profitable?" (2023)
2. alexandria.unisg.ch の "An Effective Intraday Momentum Strategy for SPY" フルテキストPDF（前回調査でフルテキスト到達可能と確認済み。最優先で完読可能なE1）
3. SSRN 4729284 — "A Profitable Day Trading Strategy For The U.S. Equity Market" (2024)
4. SSRN 4631351 — "VWAP: The Holy Grail for Day Trading Systems?"
5. arXiv 2605.04004 — MNQ日中シグナル反証研究（**反証側の最重要文献。完読必須**）
6. Finance Research Letters ORBコスト検証（abstractのみでも可、その場合は「abstract限り」と明記）
7. SSRN 2460551 — Deflated Sharpe Ratio（手法部を理解し、J-6の割引実装メモを書く）
8. QuantifiedStrategies: nasdaq-trading-strategies / rsi-mean-reversion / nasdaq-100-e-mini / opening-range-breakout の4記事
9. Concretum Group: Python ORB解説2本（コードの構造をメモ。**コードのアイデアは参照可、逐語コピーはライセンス確認まで不可**）
10. QuantConnect "ORB for Stocks in Play" 再現スレッド（ATRウォームアップ不具合の詳細を必ず記録）
11. TradeThatSwing ORB記事 / CXO Advisory批判 / Quant Macro Substackレビュー

### 4-3. アクセス障害時の代替経路
- SSRNが403の場合: ①Google Scholar経由の別ミラー ②著者サイト(concretumgroup.com)のworking paper版 ③ResearchGate。すべて失敗なら `BLOCKED-SOURCE` として記録し、当該戦略の格付けは「E2(暫定)」のまま維持（**格上げしない**）
- bot壁(QuantifiedStrategies等): ①Google キャッシュ ②archive.org スナップショット。失敗時は同上
- **代替経路でも、読めた版の書誌（版・日付）を必ず記録**。異なる版の数値を混在させない

### 4-4. R1完了条件
- SOURCE_NOTES 11ファイル以上（BLOCKED含む）
- `EDGE_LEDGER.md` 初版作成: 戦略設計書3章の全候補について、格付け確定/暫定維持を反映（雛形は8-2）
- 報告書へ「R1完了宣言 + 格付け変更一覧 + BLOCKED一覧」

---

## 5. Phase R2 — バックテスト・ハーネス構築

### 5-1. ディレクトリと命名
```
project/strategy/
  nqx_bt_m1_orb.pine          (R2で作成)
  nqx_bt_m2_noiseband.pine    (R4で作成)
  nqx_bt_m3_vwap.pine         (R4で作成)
  nqx_bt_m4_ibs.pine          (R5で作成)
  tools/bootstrap_pf.py       (R2で作成)
  EDGE_LEDGER.md
  TRIAL_REGISTRY.md
  SOURCE_NOTES/
  RESULTS/                    (Strategy Tester出力の転記とCSV)
  GPT_IMPLEMENTATION_REPORT.md
```

### 5-2. strategy() 共通仕様（全 nqx_bt_*.pine が従う）
- `//@version=6` / `strategy(..., overlay=true, initial_capital=10000, default_qty_type=strategy.fixed, default_qty_value=1, commission_type=strategy.commission.cash_per_contract, commission_value=1.00, slippage=1, calc_on_every_tick=false)`
- コストは**入力で切替**: `costProfile = input.string("BASELINE", options=["ZERO","BASELINE","STRESS"])`。ZERO=参考値専用（宣言利用禁止をコメントに明記）、BASELINE=手数料$1.00/片道+slippage 1tick、STRESS=$1.24+2ticks。※Pineの`commission_value`/`slippage`はstrategy宣言の定数のため、STRESS実行は宣言値を書き替えた別コンパイルで行い、TRIAL_REGISTRYに版を記録する
- 期間分割入力: `sampleWindow = input.string("IS", options=["IS","OOS","FWD","ALL"])` → IS: 2016-01-01〜2022-12-31 / OOS: 2023-01-01〜2025-12-31 / FWD: 実行日から先のペーパー用。`time`でフィルタし、**OOSはR3以降の判定時まで実行しない**（のぞき見禁止。実装の動作確認はISのみで行う）
- セッション: `import`せず素の`time(timeframe, session, "America/New_York")`で判定。RTH定義 "0930-1600"
- エントリーは確定バーのシグナル→翌バー寄りで約定（`process_orders_on_close=false`のデフォルト挙動）。同一バー終値約定を仮定するコードを書かない
- リペイント禁止: `request.security`使用時は既存V2と同一の `[1] + lookahead_on` 規約。`barstate.isconfirmed`ゲート
- **ウォームアップ監査**（QuantConnect事例の再発防止）: 日足ATR等の上位足指標は上位足系列で計算してから参照する。分足でWilder漸化式を温めない。各ファイル冒頭コメントに「WARMUP AUDIT: <指標名>=<計算TF>」を列挙
- プロット予算: strategyでもoutput series ≤ 40を上限とし、ファイル末尾コメントに実数を記録

### 5-3. nqx_bt_m1_orb.pine の機能要件
- OR窓: `orMinutes = input.int(5, options経由で5/15/30)`
- 方向: OR高値ブレイクでlong、OR安値ブレイクでshort（`tradeDirection`入力でlong-only/short-only/both）
- ストップ: `stopMode = "OR_OPPOSITE"（既定）/ "ATR_FRACTION"（0.10×日足ATR14）`
- 手仕舞い: 15:55 ET強制フラット + ストップ。ターゲットなし（基準形はEOD。`targetMode`入力でR倍数ターゲットを later 検証）
- 日次1エントリー制限
- レジームゲート入力（既定OFF、R3のバリアントでON）: R-A 09:30–11:30限定 / R-B 寄り30分相対出来高≥閾値(1.0/1.2/1.5) / R-D SwingArm CT方向一致（15m V2エンジンのtrendをstrategy内に同式で再計算。indicatorへの参照はしない）
- 検証補助: 取引ごとの`strategy.closedtrades.*`は使わず、TradingViewのList of Trades CSVエクスポートを一次データとする（7-2）

### 5-4. tools/bootstrap_pf.py の仕様
- 入力: List of Trades CSV（列: 少なくとも Profit列。TradingViewのエクスポート列名の揺れに対応するため列名を引数指定可能に）
- 処理: 取引損益列を1万回リサンプリング（復元抽出、標本サイズ=元の取引数）→ 各標本のPF算出 → 2.5/97.5パーセンタイル
- 出力: `PF点推定 / 95%CI / 取引数 / 平均取引損益 / 平均損益÷$3.00` をJSONと標準出力に
- 乱数seed固定（再現性）。依存はnumpy/pandasのみ

### 5-5. R2完了条件
- `nqx_bt_m1_orb.pine` がMNQ1! 15分・5分の両方でコンパイル0エラー（MCPで確認）
- ISウィンドウ・BASELINE・素ORB設定でStrategy Testerが取引を生成し、`data_get_strategy_results`で総取引数・PF・DDが取得できる
- bootstrap_pf.py がサンプルCSVで動作
- 報告書へ「R2完了宣言 + コンパイルログ + 出力series実数」

---

## 6. Phase R3〜R5 — 検証実行の共通手順

### 6-1. バリアント行列（R3: M1の例。R4/R5は各モジュールの基準形+ゲート組合せで同型に作る）
| ID | OR窓 | 方向 | ストップ | ゲート | 備考 |
|---|---|---|---|---|---|
| M1-00 | 5m | both | OR逆側 | なし | 基準形（Zarattini準拠） |
| M1-01 | 15m | both | OR逆側 | なし | TradeThatSwing形 |
| M1-02 | 5m | both | OR逆側 | R-A | 時間帯限定 |
| M1-03 | 5m | both | OR逆側 | R-A+R-B(1.2) | レジーム完全形 |
| M1-04 | 5m | both | ATR 0.10 | R-A+R-B(1.2) | Concretum形ストップ |
| M1-05 | 5m | both | OR逆側 | R-A+R-B+R-D | Structure合成（当方仮説） |
| （追加は自由パラメータ3個以内・事前登録制） |

### 6-2. TRIAL_REGISTRY.md（事前登録制）
実行**前**に1行追加してから実行する。列: `試行ID / 日付 / モジュール / バリアント定義の全パラメータ / 実行理由 / 結果参照(RESULTS/ファイル名) / 判定`。**累計試行数nに応じたOOS要求PFの引き上げ（n≤10: 1.3、11–50: 1.4、51+: 1.5）を判定に適用**（DSR的割引の運用形）。

### 6-3. 1バリアントの標準実行手順
1. TRIAL_REGISTRYへ登録
2. IS実行（BASELINE）→ List of Trades CSVを `RESULTS/<試行ID>_IS.csv` へ保存 → `data_get_strategy_results` の要約を `RESULTS/<試行ID>_IS.md` へ転記
3. J-5第1関門判定: IS PF≥1.5・取引数≥200・平均利益≥$9.00。不合格→REJECTED記録して終了
4. 合格時のみOOS実行（初回開封をREPORTに明記）→ 同様に保存
5. bootstrap_pf.py をIS+OOS結合CSVに適用 → CI記録
6. 頑健性: 主要パラメータ±20%（OR窓5→4/6分等、離散は隣接値）でIS再実行、PF≥1.3維持を確認
7. STRESSコストで再実行（IS+OOS）: PF≥1.2維持を確認
8. 判定: 全関門通過→ `VERIFIED候補`（ユーザー承認後にVERIFIED）/ どこかで不合格→ `REJECTED`（不合格関門と数値を記録）
9. EDGE_LEDGERの当該エントリを更新

### 6-4. レジーム層別レポート（合否に関わらず必須）
R-C（日足ATR14の過去100日パーセンタイル3分位）でPFを層別し、`RESULTS/<試行ID>_regime.md` に記録。「どのボラ環境でエッジが集中するか」は次フェーズの設計入力になる。

### 6-5. R3特有の追加タスク
- M1-00〜M1-05完了後、`R3_SUMMARY.md` を書く: 素ORBの生死 / ゲートの寄与 / Structure合成(M1-05)がM1-03を上回ったか（**上回らなくても記録**）
- arXiv反証研究の結論と自分の結果の突合を1節書く（一致・乖離とその理由候補）

---

## 7. データ取り扱い規則

### 7-1. 数値の出所タグ
報告書・LEDGERに書くすべての成績数値へ出所タグを付ける: `[TV-ST]`=Strategy Tester読取値 / `[BOOT]`=bootstrap_pf.py出力 / `[SRC]`=出典報告値 / `[EST]`=自分の推定（原則使用禁止、使う場合は理由必須）

### 7-2. 一次データの保全
List of Trades CSVは削除・改変しない。再実行で上書きせず、試行IDを分けて保存。RESULTSディレクトリが唯一の証拠倉庫である。

---

## 8. Edge Ledger 運用

### 8-1. 状態機械（戦略設計書I章の通り）
`CANDIDATE → REPRODUCING → VERIFIED → LIVE-MONITOR → DEPRECATED`、および `REJECTED`。**VERIFIEDへの遷移だけはあなたの権限で行わない**。「VERIFIED候補」として報告し、ユーザー承認を待つ。

### 8-2. エントリ雛形
```
## <モジュールID>: <名称>
- 格付け: E1 / E2 / E2(暫定) | 出典: <URL> | 精読: R1_<slug>.md
- 出典報告値 [SRC]: PF=… 期間=… コスト=…
- 当方再現 [TV-ST/BOOT]: IS PF=… (n=…) / OOS PF=… / CI=[…,…] / STRESS PF=…
- コスト前提: BASELINE $3.00/RT
- 試行数: TRIAL_REGISTRY該当行=…
- 状態: … (変更日: … 根拠: …)
- 備考: レジーム層別所見 / 反証との突合
```

---

## 9. 報告様式 — GPT_IMPLEMENTATION_REPORT.md

- 追記専用（過去の記述を書き換えない。訂正は新しい節で行う）
- フェーズ完了ごとに: `## R<n>完了 (日付)` / 完了条件との対照表 / 生成・変更ファイル一覧 / BLOCKED一覧 / 仮定一覧 / 次フェーズへの引き継ぎ事項
- 各フェーズ末に**自己監査節**を必ず書く: 「0-2絶対規範の各項目に違反していないか」を1項目ずつ自己点検した結果

---

## 10. 安全条件（既存憲法の継承。全フェーズ適用）

1. score/PFをprobability・勝率・将来保証として表現しない。成績記述には必ず「過去実績であり将来を保証しない」を伴わせる
2. No Synthetic Future / confirmed HTFのみ / リペイント禁止（strategyファイルにも適用）
3. NQX/1 schema・validator・Risk gate（R:R≥1.50・NO TRADE）・排他を変更しない。R6のadditiveフィールドはvalidator拡張が完了するまで既定OFF
4. `VERIFIED` 未満のモジュールをNightwatch/NQX JSONへ接続しない
5. ZEROコストの数値を宣言・比較の主役にしない（診断用の脚注のみ）
6. liquidity sweep/POC系はE1/E2出典が現れるまで自動化しない
7. 変更禁止ファイル（1章）に触れない。v1.5とV2 Full Buildの動作を壊さない

---

## 11. Phase R6 — 統合（VERIFIED発生後のみ着手）

1. `nqx_swingarm_pressure_v2_geometry_stack.pine` へのSetup Layer統合は**差分最小**で行う: 統一インターフェース（`setupActive/Dir/Stop/Target/Tag`）を1モジュールぶんだけ実装し、既存Risk gateの入力に接続。output series予算（現行実測≈60/64）を超える場合は診断plotの削減で捻出し、削減一覧を報告
2. NQX JSON: `setup_type` / `setup_evidence` / `setup_pf_verified` をadditive追加（既定OFF）。v1.5互換全フィールドの回帰テスト（既存validatorでexit 0）を実施
3. Nightwatch EDGEパネルは**設計案の提出まで**（HTML本体の変更はユーザー承認後、別指示で行う）
4. R6完了条件: 統合後のインジケータがMNQ1! 15分でコンパイル0エラー / 既存表示・JSONの回帰一致 / EDGEパネル設計案 / 報告書のR6節

---

## 12. 受け入れチェックリスト（最終自己監査）

- [ ] 全成績数値に出所タグがある
- [ ] TRIAL_REGISTRYの行数 = 実行したバリアント数（登録漏れゼロ）
- [ ] OOSの初回開封日が各モジュールで1回だけ記録されている
- [ ] REJECTEDが数値付きで残っている（ゼロ件の場合、その理由を説明できる）
- [ ] BLOCKED-SOURCEの格付けが暫定のまま格上げされていない
- [ ] 変更禁止ファイルのdiffがゼロ
- [ ] 「PFが良い」という表現が、戦略設計書0章の宣言文言テンプレート以外で使われていない
- [ ] 報告書の各フェーズに自己監査節がある

---

## 13. 用語集

| 用語 | 定義 |
|---|---|
| PF | Profit Factor = 総利益 ÷ 総損失。1.0が損益分岐 |
| IS / OOS / FWD | In-Sample（設計・調整に使える期間）/ Out-of-Sample（判定専用・のぞき見禁止）/ Forward（ペーパー実行） |
| ORB | Opening Range Breakout。寄り付き直後レンジのブレイク手法 |
| IBS | Internal Bar Strength = (close−low)/(high−low) |
| DSR | Deflated Sharpe Ratio。試行回数で成績を割り引く枠組み（本運用ではOOS要求PFの引き上げで代替） |
| PBO | Probability of Backtest Overfitting |
| R-A/R-B/R-C/R-D | セッション窓 / 相対出来高 / ボラ分位 / SwingArm方向一致 の各ゲート |
| BASELINE/STRESS | 往復$3.00 / $4.48 のコストプロファイル |
| E1〜E4 | 証拠格付け（学術 / 検証ブログ / コミュニティ実装 / 検証不能SNS） |

---

*本指示書はNQXプロジェクトの安全条件(score非確率・No Synthetic Future・confirmed HTF・排他)の下位互換である。矛盾を発見した場合は作業を止めず、矛盾箇所をBLOCKEDとして報告せよ。*
