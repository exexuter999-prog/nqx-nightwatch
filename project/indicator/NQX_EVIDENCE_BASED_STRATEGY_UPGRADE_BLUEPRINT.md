# NQX Evidence-Based Strategy Upgrade — 設計書

- 目的: 現行のNQX相場分析体系（SwingArm Pressure V2 Full Build 1.0 + Nightwatch NQX/1）に、**公開バックテストで裏付けられ、根拠を持って「プロフィットファクター(PF)が良い」と宣言できる戦略分析手法**を統合し、戦略を改良する。
- 成果物: 本設計書 + 検証用ストラテジーファイル群（`project/strategy/` 配下、K章）+ Edge Ledger（証拠台帳の運用文書）
- 対象読者: 実装担当（Codex）。追加質問なしで実装可能な粒度で記述する
- 本書はSNS・学術・検証ブログ・コミュニティ実装の広範リサーチ（2026-07-18実施、5系統並列検索）の成果に基づく

---

## 0. 「根拠を持って宣言できる」の定義（本書の憲法）

本プロジェクトの既存安全条件（score非確率、勝率捏造禁止、無効出力による上書き禁止）を戦略評価にも拡張する。**次の3条件を同時に満たすまで、いかなる手法についても「PFが良い」と宣言しない。**

1. **出典条件**: E1（学術）またはE2（方法論公開の検証ブログ）の出典が、ルール・期間・市場・コスト処理を明記した上でPF等を報告している（E3/E4のみを根拠にした宣言は禁止）。
2. **再現条件**: 当方のバックテスト・ハーネス（J章）で、**MNQの実コスト（手数料+スリッページ）控除後**にPF ≥ 1.5、取引数 ≥ 200、OOS期間PF ≥ 1.3 を再現できている。
3. **統計条件**: PFの95%ブートストラップ信頼区間の下限 > 1.2、かつ試行回数補正（Deflated Sharpe Ratio的割引、J-6）後も優位性が残る。

宣言時の標準文言（これ以外の形式での成績宣言を禁止）:

> 戦略Xは、出典S（市場M・期間P・コストC控除後）でPF=aと報告されている。当方の再現（MNQ・期間P'・コストC'・N取引）でPF=b [95%CI l–u]、OOSでPF=cを確認した。**これは過去実績であり、将来の収益・勝率・確率の保証ではない。**

SNS上の検証不能なPF自慢（口座画像・結果スクリーンショットのみ・ルール非公開・コスト不明）は、**何件集まっても宣言の根拠にならない**。E4として記録だけ残す。

---

## 1. 現状棚卸しとギャップ分析

### 1-1. 現在の資産（2026-07-18時点）

| 資産 | 内容 | 戦略評価の観点での性質 |
|---|---|---|
| SwingArm Pressure V2 Full Build 1.0（`nqx_swingarm_pressure_v2_geometry_stack.pine`） | 3m/15m/45mの3エンジン動的SwingArm帯、FrozenZone lifecycle（VISIT/CONSUMED）、External Target、Risk gate（Hard Stop 0.20 ATR・最小R:R 1.50・NO TRADE状態）、CONFIRMED_REJECTION JSON | **構造認識器**。どこで(where)を教えるが、いつ(when)・どの状況で(regime)のエッジ検証はない |
| NQX ATR Pressure v1.5（旧系） | 凍結ゾーン+stale機構 | 比較用に凍結保存 |
| Nightwatch（NQX/1 packet、validator、排他、安全監査） | 分析→シナリオ昇格の関所 | **リスク規律**は既にある。エッジの実証層がない |
| TradingView MCP | Pine注入・コンパイル・Strategy Tester読取（`data_get_strategy_results`）・リプレイ | **バックテスト実行基盤として未活用**。本設計の実行エンジンになる |

### 1-2. ギャップ（本設計が埋めるもの）

| # | ギャップ | 帰結 |
|---|---|---|
| G1 | エントリー契機がConfirmed Rejection単一 | 機会が少なく、エッジの実証もされていない |
| G2 | 時間帯（セッション）概念がSession入力の飾りしかない | NY寄り付き等、学術的にエッジ報告が集中する時間帯を活用していない |
| G3 | ボラティリティ/出来高レジームの概念がない | エッジが出る日と出ない日を区別できない（Zarattini系研究の核心はレジーム選別） |
| G4 | PF/DD/取引数を計測する仕組みが存在しない（indicatorのみでstrategyが1本もない） | 「良い」を宣言する装置がない |
| G5 | 過剰適合・多重検定への防御がない | 検証しても信頼できない数値になる |

---

## 2. Evidence Framework（証拠格付け）

すべてのリサーチ結果と将来の追加候補は、次の4段階に格付けして台帳（Edge Ledger, I章）へ記録する。

| 格 | 定義 | 宣言への利用 | 例 |
|---|---|---|---|
| **E1** | 査読誌・SSRN等の学術/準学術論文。ルール・データ・コスト処理・統計が記載 | 出典条件を満たす | Zarattini & Aziz (2023) ORB |
| **E2** | 方法論・ルール・成績指標を公開する検証ブログ/コード公開リサーチ。第三者が追試可能 | 出典条件を満たす（ただし一次資料精読を要す） | QuantifiedStrategies、Concretum、QuantConnect再現 |
| **E3** | コミュニティ実装（TradingView Strategy Tester付き公開スクリプト等）。コードはあるが成績は作者報告 | 単独では不可。**追試素材**として利用 | MNQ ORB+VWAP Bias スクリプト |
| **E4** | 検証不能なSNS主張（ルール非公開・スクショのみ・コスト不明） | **一切不可**。記録のみ | FinTwitのPF自慢 |

格付けの補助規則:
- 同一著者グループの複数論文は独立証拠として二重計上しない（Zarattini系は1系統として扱う）。
- E2でも、bot壁等で一次資料を精読できていない間は「E2(暫定)」とし、検索インデックス由来スニペットである旨を台帳に明記する（本書3章の現状はすべてこの状態。R1フェーズで精読して確定させる）。
- **反証文献はエッジ文献と同格以上に扱う**（4章）。反証が存在する手法は、反証の指摘（コスト・期間依存性）を検証計画に組み込まない限り採用候補にできない。

---

## 3. リサーチ結果 — 証拠台帳（初版）

リサーチ方法: 5系統並列検索（①学術ORB一次資料 ②検証ブログ ③Reddit/FinTwit実測報告 ④TradingView Strategy Tester実装 ⑤過剰適合・再現性批判）。Redditはクローラ遮断のためQuantConnectフォーラムを代替ソースとした。以下の数値はすべて**出典の報告値**であり、当方の宣言値ではない。

### 3-1. Opening Range Breakout（ORB）系 — 最有力候補群

| 出典 | 格 | 市場/期間 | ルール要点 | 報告指標 | 信頼度所見 |
|---|---|---|---|---|---|
| Zarattini & Aziz "Can Day Trading Really Be Profitable?" (SSRN 4416622, 2023) | E1 | QQQ/TQQQ 5分足 2016–2023 | 寄り付き5分足の方向へブレイクでエントリー、初動足の逆側にストップ、EOD手仕舞い、1トレード口座リスク1% | 手数料控除後 年率アルファ約33%、TQQQ累積+1,484%（同期間QQQ B&H +169%） | 手数料処理明記・方法論記載は学術水準。**ただし対象はETF/レバETFであり、MNQ先物への移植は未証明**。PF値そのものは一次資料精読で確定させる |
| Zarattini, Barbon & Aziz "A Profitable Day Trading Strategy For The U.S. Equity Market" (SSRN 4729284, 2024) | E1 | 米国株 "Stocks in Play" 2016–2023 | 相対出来高異常銘柄の上位20に5分ORB | 手数料控除後 +1,600%超、Sharpe 2.81、年率アルファ36% | **相対出来高フィルタ（レジーム選別）が本質**という示唆。指数先物単体には直接移植不可、フィルタ思想を転用 |
| Concretum Group（Zarattini自身のグループ）Python ORB解説 | E2 | 分足データ(Polygon.io)・コード完全公開 | 5分ORB+ATRベースストップの実装 | コード・データパイプライン公開 | 再現可能性の基準例。NQ/MNQ移植の出発点として最適 |
| QuantConnect Community "ORB for Stocks in Play" 再現 | E2 | 原論文の第三者再実装 | 同上 | Sharpe 2.4・β≈0（再現側報告） | **再現時にATRウォームアップ不具合（分足で日足ATRを温め値が1/3になる）を発見**。追試の落とし穴の実例として検証プロトコルに反映（J-7） |
| QuantifiedStrategies ORB backtest | E2(暫定) | 株価指数系 | 標準ORB | 198取引・勝率65%・PF≈2.0・平均利益0.27%。**S&P系では単純ORBの優位性が経年劣化と明記** | エッジ減衰への言及がある誠実なソース。減衰監視（I章）の根拠 |
| TradeThatSwing "ORB up 400%" | E2(暫定) | NQ先物 $10k・1枚 | 最初の15分レンジを5分足終値でブレイク | 114取引・勝率74.56%・PF 2.512・最大DD $2,725(約12%) | ルールは自動化可能な精度で公開。ただし単年・生コードなし・コスト処理不透明 → 要追試 |
| TradingView "ORB Backtest" (tkey1) | E3 | Strategy Tester | ORB+EMAフィルタ+15:55ET手仕舞い | ショート側 勝率63.2%・PF 2.34（作者報告） | 当方環境で手数料/スリッページを設定し再実測可能 |
| TradingView "15-Min ORB for NQ w/ 5-min Confirmation" | E3 | NQ | 15分ORB+5分足確認（MTF） | 勝率65–78%・PF 2.0超（作者主張） | MTF確認という点で既存V2と親和。主張値は要追試 |
| TradingView "MNQ ORB Strategy - VWAP + Bias" | E3 | **MNQそのもの** | ORB+VWAP方向フィルタ+ON高安 | 実測はこれから | 対象市場が完全一致する追試素材 |

### 3-2. 日中モメンタム（ノイズバンド）系 — 既存ATRトレイルとの整合が最も高い

| 出典 | 格 | 要点 |
|---|---|---|
| Zarattini, Barbon & Aziz "An Effective Intraday Momentum Strategy"（SPY、ザンクトガレン大リポジトリでフルテキスト公開） | E1 | 過去の時間帯別変動から**動的ノイズ帯域**を構築し、帯域ブレイクでトレンドフォロー、**VWAP/帯域でトレイル**。ORBの一般化。SwingArmの「帯+トレイル」思想と構造的に同型で、既存V2への概念統合適性が最高 |
| Quant Macro Substack レビュー | 反証 | 同論文へ「too good to be true」の独立批判。採用前に本レビューの論点（コスト感度・期間依存）を検証計画へ組込み必須 |

### 3-3. VWAP系

| 出典 | 格 | 要点 |
|---|---|---|
| Zarattini & Aziz "VWAP: The Holy Grail for Day Trading Systems?" (SSRN 4631351) | E1 | VWAPベース日中システムの一次検証。ORB論文と同一方法論体系。VWAP方向バイアス（価格>VWAPでlongのみ等）の学術的根拠として精読対象 |
| TradingView "NQ Scalping VWAP Mean Reversion (RSI+ATR Exits)" | E3 | VWAP下方乖離+RSI+反転足でエントリー、ATRエグジット。既存ATRトレイルとのエグジット統合互換が高い追試素材 |

### 3-4. 短期平均回帰（IBS / RSI(2)）系 — スイング文脈の補完

| 出典 | 格 | 市場/期間 | ルール要点 | 報告指標 |
|---|---|---|---|---|
| QuantifiedStrategies "Nasdaq Trading Strategies" | E2(暫定) | **NQ先物** | IBS ≤ 0.1 で月曜/火曜に買い | 86取引・**PF 4.13**・約$600/枚 |
| QuantifiedStrategies "RSI Mean Reversion QQQ" | E2(暫定) | QQQ | RSI(2)系平均回帰 | CAGR 12.7%（B&H 9%）・市場滞在14%・232取引・勝率75%・**PF 3.0**・最大DD 19.5%・Sharpe 2.85。強化版PF 3.15 |
| QuantifiedStrategies "S&P500 IBS+RSI" | E2(暫定) | S&P500 | IBS+RSI複合の具体ルール | ルール定義の参照元 |
| TradingStats "565 Weeks of NQ Data" | E2(暫定) | NQ 約11年 | 平均回帰検証の独立ソース | 突合用 |
| TradingView "RSI 2 Mean Reversion NQ1" | E3 | NQ1! | RSI(2)のStrategy Tester実装 | ブログ報告値のクロスチェック手段 |

注意: PF 4.13は86取引と**小標本**。PFの標本分散は取引数が少ないほど大きく、95%CIは広い。J-5の統計条件を通すまで宣言不可。

### 3-5. セッション/オーバーナイト効果

| 出典 | 格 | 要点 |
|---|---|---|
| QuantifiedStrategies "Nasdaq 100 E-mini" | E2(暫定) | S&P500の利益の大半が1993年以降オーバーナイトセッション由来という時間帯効果。ETH/RTH区別の検証上の注意も記載 |

### 3-6. Liquidity sweep / Volume Profile系 — 現時点で採用候補外

| 出典 | 格 | 所見 |
|---|---|---|
| TradingView "Liquidity Radar PRO" | E3 | 手数料0.1%+スリッページ3tickを既定に含む点は良心的だが、販促色が強くマルチモードで過剰適合リスク大 |
| （E1/E2の該当なし） | — | **PFを報告する再現可能な公開バックテストが見つからなかった**。よってliquidity sweep/POC系は「観察用の裁量文脈」（既存Alchemist SNR）に留め、自動化候補から除外。将来E1/E2が出現したら台帳へ追加 |

---

## 4. 反証・方法論文献（宣言の背骨）

エッジ主張より先に、**なぜ大半のSNS報告PFを信じてはならないか**を確定させる。

| 出典 | 格 | 本設計への拘束 |
|---|---|---|
| "Structural Limits of OHLCV-Based Intraday Signals in MNQ Futures: A Systematic Falsification Study" (arXiv 2605.04004) | E1反証 | **MNQそのもの**を対象に、ORB含む多数の日中シグナルが摩擦コスト考慮後に平均リターン負またはほぼゼロと報告。→ (a) 本設計の既定姿勢は懐疑。(b) QQQ/株式で報告されたエッジのMNQ移植は**必ず失敗し得る**前提で検証する。(c) 平均利益がコストの3倍未満の候補は棄却（J-5） |
| "Assessing the profitability of intraday ORB strategies" (Finance Research Letters) | E1反証 | 査読誌でも取引コスト考慮でORB異常収益が消失し得ると結論。→ コストゼロのバックテスト値は台帳で「参考値」に降格 |
| Bailey & López de Prado "The Deflated Sharpe Ratio" (SSRN 2460551) | E1方法論 | 多数バリアント試行で最良を選ぶと指標が膨張。→ J-6の試行回数記録・DSR割引を必須化 |
| Bailey & Borwein 過剰適合解説（PBO等） | E1方法論 | walk-forward・OOS分離・PBO検出をパイプライン標準に |
| Quant Macro Substack（intraday momentumレビュー） | E2反証 | 「結果が良すぎる」独立批判。→ M2採用判定に同レビューの論点を組込み |
| CXO Advisory ORB批判 | E2反証 | サンプル期間依存・コスト処理・過剰適合の反証チェックリストとして使用 |
| QuantConnect再現でのATRウォームアップ不具合事例 | E2教訓 | **分足で日足ATRを温めると値が1/3になる**実例。→ J-7のウォームアップ監査を必須化（既存V2エンジンのwarm-upガードと同思想） |

---

## 5. 統合アーキテクチャ — Setup Layer の新設

### 5-1. 設計原則

1. **構造(SwingArm V2)とエッジ(Setup Layer)の分離**: SwingArm V2は「どこに構造があるか」を示す観測器であり続ける。新設するSetup Layerが「いつ・どの状況で仕掛ける根拠があるか」を担う。既存のPressure Score（構造コンフルエンス）へ戦略成績を混入させない（Score汚染禁止）。
2. **indicator と strategy の分離**: 検証はTradingViewの `strategy()` ファイル群（`project/strategy/`）で行い、本番の表示・NQX出力は既存indicatorが担う。検証コードと本番コードを同一ファイルにしない。
3. **Risk gateは既存を再利用**: Setup Layerが何を提案しても、Full Build 1.0のRisk gate（Hard Stop順序・最小R:R 1.50・NO TRADE）とNightwatch NQX/1監査を**必ず通る**。Setup Layerに独自の発注概念を持たせない。
4. **レジームは前置フィルタ**: 各Setupは「レジームゲートを通過した時だけ評価される」。ゲート自体も検証対象（G3対応）。

### 5-2. モジュール構成

```
[Regime Gates]                [Setup Modules]                 [既存スタック]
 R-A Session Window   ──┐
 R-B Relative Volume  ──┼──▶  M1 ORB(5m/15m)          ──┐
 R-C ATR/VIX Regime   ──┘     M2 Noise-Band Momentum  ──┤    SwingArm V2 構造文脈
                              M3 VWAP Bias/Pullback   ──┼──▶ (方向整合・Fib深度・OE・Target)
                              M4 IBS/RSI(2) Swing Bias──┤        │
                              M5 Overnight/Session統計 ──┘        ▼
                                                             Risk Gate (R:R≥1.5, NO TRADE)
                                                                  │
                                                                  ▼
                                                             Nightwatch NQX/1 (排他・監査)
```

### 5-3. モジュール詳細設計

各モジュール共通仕様: 確定バーのみで判定 / 状態はエンジン方式（V2と同じtuple分離）/ 出力は `setupActive(bool), setupDir(int), setupStop(float), setupTarget(float), setupTag(string)` の統一インターフェース / Edge Ledger上の状態（I章）が `VERIFIED` になるまでNightwatchへ出力しない。

#### M1: ORB（Opening Range Breakout）
- 根拠: 3-1（E1×1系統、E2×3、E3×3）。反証: 4章（コスト消失・MNQ反証）
- ルール（基準形=Zarattini 2023準拠）: RTH 09:30 ET開始。OR=最初の5分（変形: 15分）。OR高値/安値の確定ブレイクでその方向へ。ストップ=ORの逆側端（変形: 0.1×ATR(14d)）。15:55 ET強制手仕舞い。日次1回まで。
- レジームゲート: R-A（09:30–11:30 ETのみ）+ R-B（寄り30分の相対出来高 ≥ 1.2）。R-Bは"Stocks in Play"のフィルタ思想の指数移植（これ自体が仮説であり検証対象）。
- SwingArm統合: CT(15m)エンジンのtrend方向とORB方向が一致する場合のみ採用する変形「ORB×Structure」を、素のORBと**並行検証**する（当方の新規合成であり、良くなるとは宣言しない）。
- 廃止基準: 直近ローリング100取引のPF < 1.1が2回連続。

#### M2: Noise-Band Intraday Momentum
- 根拠: 3-2（E1、フルテキスト入手可能）。反証: Quant Macroレビュー
- ルール（基準形）: 時間帯別の過去平均変動から当日のノイズ帯域（open ± σ_t×係数）を構築。帯域上抜けでlong/下抜けでshort。トレイル=max(VWAP, 帯域境界)（論文準拠）。EOD手仕舞い。
- SwingArm統合適性が全候補中最高: 「動的帯+トレイル」はSwingArmと同型概念であり、実装はV2のATRトレイルを時間帯別ノイズ帯へ置換した並行エンジンとして書ける。
- 検証上の注意: SPYでの報告。MNQ移植+レビュー論点（コスト感度）を最初に潰す。

#### M3: VWAP Bias / Pullback
- 根拠: 3-3（E1×1、E3×1）
- ルール: (a) Biasフィルタ形: セッションVWAPの上ではlongのみ/下ではshortのみ（M1/M2の前置） (b) Pullback形: トレンド中の初回VWAPタッチ+反転確定足でエントリー、ATRストップ。
- SwingArm統合: VWAPと45m HTF帯の合流点は「構造×出来高加重平均」の二重根拠となる（合流表示のみ。加点はEdge検証後）。

#### M4: IBS / RSI(2) スイングバイアス
- 根拠: 3-4（E2×4、E3×1。うちNQ直接検証がIBS PF 4.13/86取引）
- ルール（基準形）: 日足IBS=(close−low)/(high−low) ≤ 0.1、曜日=月/火で買い、exit=close>前日高値 or 5営業日タイムストップ。RSI(2)<10変形も並行検証。
- 統合: 日中モジュールではなく**Nightwatchの日次バイアスパネル**として統合（「本日は平均回帰的買い地合いが統計的に優勢」という文脈表示）。日中エントリーへの直接接続はEdge検証後。
- 注意: 小標本（86取引）。CI必須。ショート側は非対称（株価指数の平均回帰は買い側に偏る）であり、対称化しない。

#### M5: セッション/オーバーナイト統計パネル
- 根拠: 3-5（E2暫定）
- 内容: RTH/ETH別・時間帯別の平均レンジ/方向統計を**表示のみ**（トレードしない）。M1/M2のゲート設計（R-A）の校正材料。

#### R-A/R-B/R-C レジームゲート
- R-A Session Window: 既定 09:30–11:30 ET（M1/M2用）。校正で15:00–16:00の追加を検証。
- R-B Relative Volume: 寄り30分出来高 ÷ 過去14日同時間帯平均。閾値1.0/1.2/1.5を検証（Zarattini 2024の思想移植）。
- R-C Volatility Regime: 日足ATR(14)の過去100日パーセンタイル3分位。どの分位でエッジが出るかを**全モジュール共通の層別軸**にする（"良い日だけ取る"の実証装置）。

---

## 6. バックテスト検証パイプライン（J章）

### J-1. ハーネス構成
- ファイル: `project/strategy/nqx_bt_m1_orb.pine`, `nqx_bt_m2_noiseband.pine`, `nqx_bt_m3_vwap.pine`, `nqx_bt_m4_ibs.pine`（各モジュール1ファイル、`strategy()`宣言）
- 実行: TradingView MCP（`pine_set_source`→`pine_smart_compile`→`data_get_strategy_results`）で機械的に取得。結果はEdge Ledgerへ転記
- 共通入力: レジームゲートON/OFF、SwingArm方向フィルタON/OFF（並行比較のため）

### J-2. 市場・データ
- 検証銘柄: MNQ1!（主）+ NQ1!（突合）。連続足のロールバック調整による歪みを認識し、口座通貨PnLはpoint value基準で計算
- 期間分割: In-Sample 2016–2022 / Out-of-Sample 2023–2025 / Forward（ペーパー）3ヶ月
- セッション: RTH/ETHを明示的に分離（QuantifiedStrategiesの注意事項準拠）

### J-3. コストモデル（MNQ、1枚あたり）
| 項目 | Baseline | Stress |
|---|---|---|
| 手数料+取引所費用（片道） | $1.00 | $1.24 |
| スリッページ（片道） | 1 tick = $0.50 | 2 ticks = $1.00 |
| 往復合計 | **$3.00** | **$4.48** |
- Strategy設定に `commission` と `slippage` を必ず入れる（ゼロコスト検証は「参考値」扱い、宣言に使用不可）
- 採用の必要条件: **平均取引利益 ≥ 3 × Baseline往復コスト**（= $9.00 ≈ 4.5 NQポイント）。MNQ反証研究（4章）が示す「摩擦でエッジ消失」への防御線

### J-4. 計測指標（全モジュール共通で報告必須）
PF（総利益/総損失）、純利益、最大DD（金額と%）、取引数、勝率、平均取引損益、ペイオフレシオ、SQN、時系列別PF（年次）、レジーム層別PF（R-C 3分位別）、ロング/ショート別PF。

### J-5. 合格基準（宣言条件の実務形）
| 段階 | 基準 |
|---|---|
| 候補維持 | IS: PF ≥ 1.5、取引数 ≥ 200、平均利益 ≥ 3×コスト |
| OOS合格 | OOS: PF ≥ 1.3、IS→OOSのPF低下率 < 40% |
| 統計合格 | PF 95%ブートストラップCI下限 > 1.2（取引列の再標本1万回） |
| 頑健性 | 主要パラメータ±20%変動でPF ≥ 1.3を維持（パラメータ崖の排除） |
| Forward | 3ヶ月ペーパーでPF ≥ 1.2 かつ 最大DDが過去分布の95%以内 |

### J-6. 多重検定・過剰適合防御（Bailey系準拠）
- **試行台帳**: 検証した全バリアント数を記録（採用されなかったものも）。DSR的割引: バリアント数nに応じて要求PFを引き上げ（目安: n=10でOOS要求+0.1、n=50で+0.2）
- パラメータ数上限: 1モジュールあたり自由パラメータ3個まで
- walk-forward: パラメータ最適化を行う場合のみ、2年訓練/6ヶ月検証のローリングで実施。最適化を行わない基準形をまず検証する
- PBO(CSCV)は必要に応じPython側（strategy結果CSVエクスポート後）で実施

### J-7. 実装品質監査（再現の落とし穴防止）
- **ウォームアップ監査**: 上位足指標（日足ATR等）を分足で温めない（QuantConnect再現で発覚した1/3バグの再発防止）。`request.security`の確定値[1]+lookahead_onはV2と同一規約
- リペイント監査: 全エントリー/エグジットが確定バー。`calc_on_every_tick=false`
- 先読み監査: セッション判定・日足指標に当日確定前の値を使わない
- 生存者バイアス: 指数先物単体のため銘柄選択バイアスはないが、連続足ロール処理を明記

---

## 7. Edge Ledger（証拠台帳の運用）

ファイル: `project/strategy/EDGE_LEDGER.md`。1モジュール=1エントリ。状態機械:

```
CANDIDATE（3章に記載） → REPRODUCING（J章実行中） → VERIFIED（J-5全通過=宣言可能）
     │                        │                          │
     └── REJECTED ◀───────────┘                          ▼
（基準未達・反証成立。理由と数値を必ず残す）      LIVE-MONITOR（ローリング100取引PF監視）
                                                         │ PF<1.1×2回連続
                                                         ▼
                                                    DEPRECATED（宣言取消し。Nightwatch出力停止）
```

- 各エントリの必須フィールド: 格付け（E1..E4）/ 出典リンク / 報告値 / 当方再現値+CI / コスト前提 / 試行バリアント数 / 状態 / 状態変更日と根拠
- **REJECTEDの記録は成果である**（何が効かないかの知識蓄積。エッジ減衰の監視にも使う。QuantifiedStrategiesが自らORBの経年劣化を明記している姿勢に倣う）

---

## 8. NQX/1・Nightwatch統合契約

1. `VERIFIED` 状態のモジュールのみ、Nightwatchへ出力できる。
2. JSON: 既存 `NQX_ATR_PRESSURE/1` のCONFIRMED_REJECTIONを不変とし、Setup Layer由来イベントは additiveフィールド `setup_type`（"ORB" | "NOISE_BAND" | "VWAP_PB"）、`setup_evidence`（Edge Ledgerエントリid）、`setup_pf_verified`（当方再現PF値。**出typeがVERIFIEDでない場合はフィールド自体を出さない**）を付与。validator拡張が済むまで既定OFF。
3. 優先順位: Risk gate（R:R≥1.5・順序検証・NO TRADE）＞レジームゲート＞Setup。**Setupがどれほど強くてもRisk gateを迂回しない。**
4. 表示: Nightwatchに「EDGE」パネルを追加し、稼働中モジュール・状態・ローリングPF・`PAST PERFORMANCE IS NOT A GUARANTEE` を常時表示。SCORE IS NOT PROBABILITY と同格の免責。
5. 無効・欠損・DEPRECATEDのSetup出力が既存分析を上書きしない（既存排他規則に従属）。

---

## 9. 段階導入計画

| Phase | 内容 | 完了条件 | ロールバック |
|---|---|---|---|
| R1 | 一次資料精読（SSRN×3、unisg PDF、QuantifiedStrategies各記事、反証2本）。E2(暫定)→E2確定。報告PF値の原文確定 | 台帳の全「暫定」解消 | なし（読むだけ） |
| R2 | ハーネス構築: `nqx_bt_m1_orb.pine` 基準形 + コストモデル + MCP自動計測 | MNQ1!でIS/OOS結果が取得できる | strategyファイル削除のみ |
| R3 | M1検証（素ORB / +R-A / +R-B / +Structure並行比較）→ 台帳更新 | J-5判定完了（VERIFIED or REJECTED） | R2状態 |
| R4 | M2/M3検証（同上） | 同上 | R3状態 |
| R5 | M4/M5（日足系）検証+Nightwatchバイアスパネル設計 | 同上 | R4状態 |
| R6 | VERIFIEDモジュールのindicator統合（Setup Layer表示）+ NQX/1 additive + EDGEパネル | 8章の契約を満たす。validator回帰PASS | R5状態（表示層のみ切戻し） |

各Phaseで新規バリアントを試すたびにJ-6の試行台帳へ記録する。

---

## 10. 受け入れ条件

1. いかなるUI/文書にも、E1/E2出典+当方再現なしのPF宣言が存在しない。
2. 全バックテストが手数料+スリッページ込み。ゼロコスト数値が宣言に使われていない。
3. IS/OOS/Forwardの3分割が守られ、OOSを最適化に使った痕跡がない。
4. 試行台帳が存在し、バリアント数と要求PF引き上げが対応している。
5. `VERIFIED` 未満のモジュールがNightwatch/NQX JSONへ出力していない。
6. Risk gate・排他・NO TRADE・`SCORE IS NOT PROBABILITY` が全経路で維持されている。
7. Edge LedgerにREJECTED記録が残る形式になっている（失敗の抹消がない）。
8. ローリングPF監視とDEPRECATED降格が実装されている（宣言の取消し手段がある）。
9. liquidity sweep/POC系が「裁量文脈」以外で自動エントリーに接続されていない（E1/E2出現まで）。
10. 既存 `nqx_swingarm_pressure_v2_geometry_stack.pine`・v1.5・Nightwatch本体・validatorが本設計の実装で破壊されていない。

---

## 11. 禁止事項

- E3/E4のみを根拠にした成績宣言・SNS的PF自慢の転載
- 勝率・bounce probability・% probabilityの捏造（既存憲法の継承）
- コスト抜きPFの宣伝的使用 / OOSの複数回のぞき見 / 合格するまでパラメータを回す行為（試行台帳外の探索）
- Setup LayerによるRisk gate迂回、Score（構造コンフルエンス）への戦績混入
- 単一年・小標本（<200取引）での「良い」判定
- 反証文献（MNQ falsification・FRL・DSR）を引用せずにORB系を採用すること

---

## 12. 最終判断

**なぜこの形か**: リサーチの結論は二面的だった。(a) Zarattini系E1研究とQuantifiedStrategiesのE2検証は、ORB/ノイズバンド/VWAP/IBSに**具体的なPF付きの再現可能な報告**が存在することを示す。(b) 同時に、MNQを直接対象にした反証研究と査読誌のコスト検証は、**それらが摩擦コストとレジーム選別なしにはMNQで消失し得る**ことを示す。したがって「良い手法を輸入して飾る」のではなく、**宣言条件（0章）・コストモデル（J-3）・多重検定防御（J-6）・取消し機構（DEPRECATED）を備えた検証パイプラインごと輸入する**ことが、唯一「根拠を持って宣言できる」設計である。

**最初に実行すべき最小スライス**: R1（一次資料精読）→ R2（ORBハーネス1本+コストモデル）→ M1素ORBのIS計測。ここまでで「当方のMNQ実コスト環境でORBが生き残るか」という最重要の問いに数値が出る。生き残らなければ、その事実自体がEdge Ledgerの最初のVERIFIEDな知見（REJECTED記録）になる。

**既存体系との関係**: SwingArm V2は本設計後も「構造の観測器」として不変。本設計が加えるのは、その構造の上で「いつ・どの状況なら統計的裏付けがあるか」を計測し、裏付けの状態（VERIFIED/DEPRECATED)ごと管理する層である。エッジは宣言された瞬間から減衰し得る（QuantifiedStrategies自身がORBの経年劣化を認めている）——だから本設計の中心は個別戦略ではなく、**宣言・監視・取消しのライフサイクル**に置いた。

---
*本書の全報告値は出典の主張であり、当方の宣言値ではない(0章)。過去実績は将来の収益を保証しない。*
