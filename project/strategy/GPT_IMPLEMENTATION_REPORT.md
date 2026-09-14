# 実装報告書 — NQX Evidence-Based Strategy Upgrade

追記専用ファイル。過去の記述は書き換えない。訂正は新しい節で行う。

---

## R1完了 (2026-07-18)

### 完了条件との対照表

| 完了条件（GPT指示書4-4） | 状態 |
|---|---|
| SOURCE_NOTES 11ファイル以上（BLOCKED含む） | ✅ 13ファイル作成（対象リスト11項目 + Concretum Group blogを分離） |
| EDGE_LEDGER.md初版作成 | ✅ 作成済み。M1-M5をCANDIDATE状態で初期化 |
| 報告書へR1完了宣言+格付け変更一覧+BLOCKED一覧 | ✅ 本節 |

### 生成・変更ファイル一覧
- `project/strategy/SOURCE_NOTES/R1_*.md` × 13ファイル
- `project/strategy/EDGE_LEDGER.md`（新規）
- `project/strategy/TRIAL_REGISTRY.md`（雛形作成）
- `project/strategy/GPT_IMPLEMENTATION_REPORT.md`（本ファイル）
- `project/strategy/RESULTS/`, `project/strategy/tools/`（空ディレクトリ作成）

### 格付け変更一覧
| 資料 | 当初想定格付け（設計書3章） | R1後の格付け |
|---|---|---|
| Zarattini & Aziz 2023 (ORB) | E1 | E1(暫定・原文未読) — SSRN本体403でBLOCKED |
| **Mesfin 2026 (MNQ反証)** | E1反証 | **E1反証（確定・全文精読完了）** |
| Zarattini/Aziz/Barbon 2024 (momentum SPY) | E1 | E1(暫定・原文未読) — alexandria.unisg.ch 405でBLOCKED |
| Zarattini/Barbon/Aziz 2024 (Stocks in Play) | E1 | E1(暫定・原文未読) |
| Zarattini & Aziz 2023 (VWAP) | E1 | E1(暫定・原文未読) |
| Bailey & López de Prado 2014 (DSR) | E1方法論 | E1方法論(暫定・要旨のみ、数式精読未了) |
| QuantifiedStrategies 4記事 | E2(暫定) | E2(暫定)維持 — Bot Verificationで本文未読 |
| Concretum Group blog | E2 | E2(暫定)維持 — コード公開の確認不可 |
| QuantConnect ATRウォームアップ事例 | E2教訓 | E2教訓（確定） |
| TradeThatSwing ORB | E2(暫定) | E2(暫定)維持 |
| Holmberg et al. 2013 (FRL) | E1反証 | E1反証(暫定・abstract水準のみ) — 反証結論を直接確認できず |
| Quant Macro Substackレビュー | E2反証 | E2反証(暫定・要旨のみ) |
| CXO Advisory批判 | E2反証 | E2反証(暫定・要旨のみ) |
| TradingView各スクリプト | E3 | E3（確定・定義上これ以上の格上げなし） |

**総括**: 唯一フルテキストへ到達し精読できたのはMesfin(2026)のMNQ反証研究のみ。これは最重要文献であり、当方の検証事前予想（M1 ORBはMNQ実コスト環境で苦戦する可能性が高い）を強く裏付ける。他の全E1候補は原文未読のままBLOCKED-SOURCEとして格付け据え置き。

### BLOCKED-SOURCE一覧
1. SSRN 4416622（Zarattini & Aziz ORB）— HTTP 403
2. alexandria.unisg.ch momentum SPY PDF — HTTP 405 Method Not Allowed
3. SSRN 4729284（Stocks in Play）— 未フェッチ（時間配分優先度により後回し、次回再試行）
4. SSRN 4631351（VWAP）— 未フェッチ（同上）
5. QuantifiedStrategies.com 全記事 — Bot Verificationページ
6. web.archive.org経由の代替 — WebFetchツール自体がarchive.orgへの接続不可（ツール制限）
7. ScienceDirect FRL論文 — HTTP 403
8. econ.umu.se working paper版 — TLS証明書検証エラー

### 仮定一覧
- Mesfin(2026)論文中「1 point ≈ $2/MNQ」等の換算は論文中の明記に基づく。当方のMNQ point value $2/pt（GPT指示書2章記載）と整合することを確認した。
- QuantifiedStrategies IBS戦略のPF 4.13は小標本(N=86)であり、設計書J-5の統計条件を通過するまで宣言に使用しないという既定方針を維持。
- 同一著者(Zarattini)グループの4論文（ORB/momentum/Stocks in Play/VWAP）は設計書の格付け補助規則に従い1系統として扱い、独立証拠として二重計上しない。

### 次フェーズへの引き継ぎ事項
- R2着手前に、BLOCKED-SOURCE解消の優先度は「Mesfin論文との突合のためにORB原論文(SSRN 4416622)を優先」とする。
- R2のハーネス構築（`nqx_bt_m1_orb.pine`）は原文未読のZarattini基準形ではなく、**設計書J-2/J-3/5-3で既に確定しているパラメータ**（5分OR窓・寄り付き09:30 ET開始・OR逆側ストップ・15:55 ET強制手仕舞い）で構築する。これは複数の二次資料で一致しており、原文未読でも安全に実装可能な水準の合意情報である。

### 自己監査節（0-2絶対規範との対照）
1. 数値を発明しない: 遵守。全数値に出典を明記し、未確認箇所は「未確認」「未読」と明記した。
2. 読んでいない資料を読んだことにしない: 遵守。BLOCKED-SOURCE 8件を明記し、格上げしていない。
3. 保護されたソースコードを複製しない: 該当作業なし（本フェーズはコード実装なし）。
4. 後付け検証の禁止: 該当作業なし（バックテスト未実行）。
5. 成果の粉飾禁止: Mesfin論文のFAIL判定を含め、不利な結果も台帳にそのまま記録した。
6. 質問で作業を止めない: BLOCKED-SOURCEは記録して次の資料へ進んだ。
7. 日本語での報告: 遵守。

---

## R2完了 (2026-07-18)

### 完了条件との対照表

| 完了条件（GPT指示書5-5） | 状態 |
|---|---|
| `nqx_bt_m1_orb.pine` がMNQ1! 15分・5分でコンパイル0エラー | △ 部分達成（下記参照） |
| ISウィンドウ・BASELINE・素ORB設定でStrategy Testerが取引を生成 | ✗ 未達成（下記参照） |
| bootstrap_pf.py がサンプルCSVで動作 | ✅ 達成 |
| 報告書へR2完了宣言+コンパイルログ+出力series実数 | ✅ 本節 |

### 生成・変更ファイル一覧
- `project/strategy/nqx_bt_m1_orb.pine`（新規、211行、出力series 6本）
- `project/strategy/tools/bootstrap_pf.py`（新規。numpy/pandasをこの環境にインストールし、合成サンプルCSV(N=250)で動作確認。実行結果: PF点推定1.40・95%CI[1.01, 1.95]・出所タグ[BOOT]。**このCIは合成乱数データのものであり、MNQの実戦績ではない**）
- `project/strategy/TRIAL_REGISTRY.md`（雛形、登録0件）
- TradingView上に "NQX BT — M1 Opening Range Breakout 2" として保存（クラウド側、ローカルリポジトリには影響なし）

### TradingView実行状況（正直な報告）
1. TradingView MCP経由でPine Editorに新規タブを作成し、`nqx_bt_m1_orb.pine`の全内容をクリップボード貼り付けで注入した（`pine_set_source`/`pine_get_source`/`pine_smart_compile`/`pine_get_errors`の内部APIはこのセッションでは「Could not open Pine Editor」エラーで一貫して失敗し、React fiber経由のMonaco検出に失敗していた — 実体のエディタ要素と重複するダミー要素[width/height=0]が先にマッチしてしまう挙動を確認した）。
2. GUI手動操作（クリップボード貼り付け+ボタン操作）でスクリプムを保存し、目視でエラーバッジ0件を確認した。ただし`pine_get_errors`のAPI経由での確認はできていないため、**コンパイル成功は目視確認のみであり、機械的な確認ではない**。
3. 「チャートに追加」を実行したところ、**現行のTradingViewプラン(Essential、上限5インジケーター)が既存チャートの表示中インジケーターで既に上限に達しており、strategyを追加できない**というアップグレード誘導ダイアログが表示された。
4. ユーザーに確認した結果、「TradingView上の実バックテストは重視しない。理論・ロジックをNQX Nightwatchへ実装できることが本質」との回答を得た。**これによりR3(MNQでの実IS/OOS計測)は本セッションでは実行しないという明示的な仮定を置く**。

### 仮定一覧（追加）
- ユーザー指示により、TradingView Strategy Testerでの実測PF/取引数/CI等の数値は本フェーズでは取得しない。EDGE_LEDGERの状態は`CANDIDATE`のまま据え置き、`REPRODUCING`への遷移は行わない（実測なしにVERIFIED方向へ進めることは絶対規範4「後付け検証の禁止」以前に、そもそも検証自体が実行されていないため不可能）。
- `nqx_bt_m1_orb.pine`はコード資産として完成させ、将来ユーザーがTradingViewプランをアップグレードするか、既存インジケーターを一時的に外すことで実測を再開できる状態にしておく。

### 次フェーズへの引き継ぎ事項
- 実測を再開する場合の手順: (1) 既存チャートの表示インジケーターを一部無効化してスロットを確保 (2) 保存済みスクリプト "NQX BT — M1 Opening Range Breakout 2" をチャートに追加 (3) Strategy Testerパネルで期間をIS(2016-2022)に設定し`data_get_strategy_results`で数値取得 (4) List of TradesをCSVエクスポートし`bootstrap_pf.py`でCI計算。
- 理論実装（Setup Layerのindicator統合、8章のNQX/1契約拡張）を優先する方針に転換したため、以降はR6（統合設計）に relevant な作業を先に進める。

### 自己監査節（0-2絶対規範との対照）
1. 数値を発明しない: 遵守。TradingViewでの実測は行っていないため、M1のPF等はEDGE_LEDGERに一切記載していない。
2. 読んでいない資料を読んだことにしない: 該当なし（本フェーズはコード実装中心）。
3. 保護されたソースコードを複製しない: 遵守。`f_ctTrendOnly`はV2の`f_engineSeries`のATRトレイル核のみをclean-room再実装し、V2ファイル自体は一切参照・import・コピーしていない。
4. 後付け検証の禁止: 該当なし（検証自体を未実行のため、事後的な数値操作の機会がそもそもない）。
5. 成果の粉飾禁止: TradingViewでの追加失敗をそのまま記録し、「コンパイル0エラー」を機械確認済みであるかのように偽装していない。
6. 質問で作業を止めない: インジケーター上限の壁に直面した時点でユーザーに確認を仰いだ（これは「明示的な仮定」で済ませられない、ユーザーのTradingViewアカウント契約プランに関わる不可逆コスト的判断のため、既存の安全条件に照らして適切な中断と判断した）。
7. 日本語での報告: 遵守。

---

## R3(理論設計)・R6(統合設計)完了 (2026-07-18)

ユーザー方針決定「TradingView実測は重視せず、理論・パイプライン設計をNQX Nightwatchへ実装できることを優先する」を受け、以降は実測(R3のIS/OOS計測)を追わず、Edge Ledger/Trial Registryの整合性維持と統合設計に注力した。

### 実施内容
1. `TRIAL_REGISTRY.md`にM1バリアント行列(M1-00〜M1-05、6件)を事前登録した。**日付・結果参照・判定はすべて「未実行」のまま**とし、数値のプレースホルダーを一切書いていない（絶対規範1の遵守）。
2. `EDGE_LEDGER.md`のM1エントリを更新し、ハーネス完成/実測未実施/状態CANDIDATE据え置きの事実関係を明記した。
3. `R6_SETUP_LAYER_INTEGRATION_DESIGN.md`を新規作成した。設計書5章のSetup Layerアーキテクチャに従い、(a) 出力予算捻出案（既存60/64実測から2枠削減） (b) 統一インターフェース`setupActive/Dir/Stop/Target/Tag`のM1実装案（Edge Ledger状態をinput.boolで反映するゲート付き） (c) NQX/1 additiveフィールド(`setup_type`/`setup_evidence`/`setup_pf_verified`)のJSON生成関数案（既存`f_rejectionJson`は不変のまま新規関数を追加する設計） (d) Nightwatch EDGEパネルのワイヤーフレーム (e) 受け入れ条件との対応表、を記述した。
4. **本書は設計案のみであり、`nqx_swingarm_pressure_v2_geometry_stack.pine`・Nightwatch HTML本体・NQX validatorへの実コード変更は一切行っていない**（GPT指示書11-3「HTML本体の変更はユーザー承認後、別指示で行う」に準拠）。

### 生成・変更ファイル一覧
- `project/strategy/TRIAL_REGISTRY.md`（更新、M1バリアント6件を「未実行」で事前登録）
- `project/strategy/EDGE_LEDGER.md`（M1エントリ更新）
- `project/strategy/R6_SETUP_LAYER_INTEGRATION_DESIGN.md`（新規）
- 変更禁止ファイル（`nqx_swingarm_pressure_v2_geometry_stack.pine`等）: **diffゼロ**（設計案の中にコードスニペットは含むが、実ファイルへは未適用）

### 自己監査節（0-2絶対規範との対照）
1. 数値を発明しない: 遵守。TRIAL_REGISTRYの全行が「未実行」であり、PF等の数値は一切書いていない。
2. 読んでいない資料を読んだことにしない: 該当なし。
3. 保護されたソースコードを複製しない: 遵守。R6設計案内のコードスニペットは新規関数のみであり、既存`f_engineSeries`/`f_rejectionJson`等の実装は一切コピーせず、既存関数への参照(呼び出し)として設計した。
4. 後付け検証の禁止: 該当なし。
5. 成果の粉飾禁止: 全モジュールCANDIDATE状態を維持し、Nightwatch EDGEパネル設計でも全PF列を`N/A`固定とした。
6. 質問で作業を止めない: ユーザーの方針決定に従い作業を継続した。
7. 日本語での報告: 遵守。
8. **変更禁止ファイル不可侵の確認**: `nqx_swingarm_pressure_v2_geometry_stack.pine`・`nqx_atr_pressure_map_v1.pine`・`nqx_swingarm_pressure_v2_slice.pine`・Nightwatch HTML本体・NQX validator・設計書群への編集は本フェーズで一切行っていない。

---

## Phase H1 — Regime/Gamma運用skill・不変ログ基盤 (2026-07-19)

### 引き継ぎ入力

- `C:\Users\exexu\Downloads\handoff-gpt56sol-research.md` をUTF-8で読了し、指定されたPhase 1（A-1、B-1/B-2、記録運用開始）を実施した。
- 文書に列挙された最新版 `mnq-nightwatch-nqx.zip`、`nightwatch-enhancement-directive.md`、`integration-report.md` は作業開始時点のローカルに存在しなかった。
- 現行 `project/engine-contract/nqx1-spec.md` と `validate_nqx.py` を正本として、欠落版を上書きせず `C:\Users\exexu\.codex\skills\mnq-nightwatch-nqx` に単一skillとして再構成した。

### 実装内容

1. `RG-H1` を追加。`TR/BA/TX/EX/EC/ER/MX` を、同時刻帯の実現ボラ比、session range/ATR比、gap/ATR比、4種percentile、session/clock bucket、前日構造、現在方向、確認済みauction structure、HTF整合、検証済みイベントから優先順位付きで一意判定する。これは標本形成用ヒューリスティックであり常に `qm=H`。
2. NY開始60分以内の未受容 `gap_atr_ratio>=0.50`、および前日方向と現在方向が衝突し`BREAK_HOLD`未確認の場合を provisional `TX` とした。閾値は事前固定の運用値であり、論文推定値ではない。
3. screenshot fallbackは現行3M/15M/45Mが不足すれば原則 `MX`、視覚だけでは `EX` を禁止、`EC/ER` は権威あるイベント時刻の検証を必須とした。
4. gamma環境を `P/N/U` と出所で正式化。`DIRECT_VENDOR` または再現可能な `CHAIN_CALC` とas-ofが揃う場合だけ `P/N` を受理し、VIX・ローソク足・大OIストライク・ユーザー推定は `U` に強制する。正gammaはRR補助/BC確認強化、負gammaは逆、`U`は無寄与。確率への変換は禁止。
5. 大OI strikeはstrike・expiry・source・as-ofが揃う場合のみ `Z` のOBSERVED流動性レベル候補とし、gamma符号・support/resistance・pinningの証明には使わない。
6. NQX/1 wire keyは増やさず、`M.rg`、`M.rm`、`M.un`、`C.pm` の既存契約へ出所付きで格納する。現行spec/validatorのskill内コピーは正本とSHA-256一致。
7. 不変記録を単一行後編集方式からevent-sourcingへ修正。結果未知の `DECISION` と、後日の `RESOLUTION` / `CANCEL` を別行で追記し、全列をSHA-256連鎖した。`AMENDMENT` は履歴を消さず残し、自動昇格を停止する。
8. `scripts/append_trade_event.py`、`audit_trade_log.py`、`ledger_common.py` を追加。親子関係、hash chain、fill/state、LONG/SHORTのentry-exit geometry、gross-cost=net、2pt cost floor、`setup_version×regime×session×direction`、OOS N/T/年符号を機械監査する。
9. `project/strategy/NIGHTWATCH_OBSERVATION_LOG.csv` を正規headerのみで開始した。実トレード標本は0件であり、有効性は未証明のまま。

### 一次根拠と転用限界

- Mesfin arXiv:2605.04004v2、Dim–Eraker–Vilkov SSRN 4692190、Amaya et al. Cboe公開PDF、Cont–Kukanov–Stoikov SSRN 1712822 の一次公開ページ/本文を確認した。
- SPX gamma研究をMNQ固有の検証結果へ格上げしていない。OFIもNYSE TAQの関係をMNQ edgeへ転用せず、tick取得・事前登録・OOS検証後のPhase 3へ留保した。
- `RG-H1` 閾値は上記論文の推定結果ではなく、再現可能なラベルを集めるための明示的ヒューリスティック。

### 回帰試験結果

- Python 5ファイル: `py_compile` PASS。
- skill構造: `quick_validate.py` → `Skill is valid!`。
- regime分岐: metric `EX`、stable `TR`、opening-gap `TX`、prior-day conflict `TX`、45M欠損 screenshot `MX` を期待値どおり確認。
- gamma分岐: VIX proxyの`P`入力は`U`へ強制。信頼できるsourceの`P/N`は保持され、RR/BC gateが相互に反転することを確認。
- ledger: header-only実台帳 PASS、2-event `DECISION→CANCEL` hash chain PASS、`DECISION→RESOLUTION`のOOS N=1は `N<30 / T未定義 / 年安定不足` で昇格拒否。
- NQX: `valid-sample.nqx` PASS、`mnq_2026-07-17_1856_test.nqx` PASS、`invalid-sample.nqx` は21 errorsで期待どおり拒否。
- 現行specとskill参照、現行validatorとskill validatorはそれぞれSHA-256一致。

### 出荷物

- installed skill: `C:\Users\exexu\.codex\skills\mnq-nightwatch-nqx`
- package: `C:\Users\exexu\Downloads\mnq-nightwatch-nqx.zip`
- package SHA-256: `83defe72d1df29d30f2cec205e6147fa0a91c6226ec046cda7f7289e78b225ee`
- ZIP entries: 15（`__pycache__` / `.pyc`は除外）。

### 既存安全条件との整合・残存限界

- 本フェーズでNightwatch HTML、現行NQX spec/validator、Pine、既存設計書を編集していない。変更は新規skillと `project/strategy` 内の台帳/本追記だけ。
- VERIFIED未満の戦略モジュールをNQX JSONへ接続していない。確率・PF・勝率を生成していない。
- `qm=E`候補は同一frozen groupについて OOS N≥30、net T≥2.0、各取引2pt以上控除後にmean/cumulative正、OOS 2年以上で各年平均正、metric regime、同一policy/outcome、packet hash完備、未解決amendmentなしを全て要求する。通過しても校正確率は生成しない。
- GMM/HMM、MNQ tick OFI、gamma vendor接続、実標本形成は未着手。入力が無い間はgamma=`U`、edge=`未証明`、`qm=H`を維持する。
- 本成果物は教育・研究目的であり投資助言ではない。

### 自己監査

1. 数値を発明しない: PASS。試験用fixtureは`TEST/EXAMPLE ONLY`と明記し、実市場成績へ混入させていない。
2. 読んでいない資料を読んだことにしない: PASS。確認した一次ページのみ根拠refへ記録。
3. 保護コードを複製しない: PASS。既存公開NQX契約/validatorの正本コピー以外に非公開ロジックを再現していない。
4. 後付け検証禁止: PASS。実標本0件で昇格なし。
5. 成果粉飾禁止: PASS。`qm=H`、未証明、Phase 3未着手を明記。
6. 無効生成物で既存状態を上書きしない: PASS。HTML/engine/Pineは未編集、validator PASSを出荷条件とした。

---

## SLゲート強化完了 (2026-07-23)

### 完了条件との対照

| タスク | 実装箇所（実装後の行番号） | 結果 |
|---|---|---|
| D: `SL_INVALIDATION_TOO_CLOSE` | `engine-contract/validate_nqx.py:564-578` | 既存の順序監査直後に、Invalidation–SL間隔がEntry中央値–SLリスクの15%未満なら独立エラーとする監査を追加。既存`INVALIDATION_ORDER`は変更なし。 |
| E: safety invariant 2a | `engine-contract/nqx1-spec.md:214-218` | 15%距離監査と、close-based invalidationがHard Stopより先に確定しない危険を明文化。 |
| A: 臨界`TOO TIGHT`のBLOCKED化 | `app/nq-nightwatch-nqx-final.html:3529-3540` | 従来の1段階格下げを維持し、`risk < minimum * 0.70`のみ`BLOCKED`化。70%はリサーチ由来ではない運用閾値とコメントで明記。 |
| B: 二監査の不一致警告 | `app/nq-nightwatch-nqx-final.html:4858-4865` | Invalidation監査が`PASS`かつStop Planが`TOO TIGHT`なら、指定文言を`sc.warnings`へ追加。 |
| C: Entry帯幅バックストップ | `app/nq-nightwatch-nqx-final.html:3209-3215` | Entry中央値–SL距離がEntry帯幅未満なら指定warningを追加。 |
| Risk Gate配線 | `app/nq-nightwatch-nqx-final.html:5073-5077` | `computeGrade`が生成した`BLOCKED`を`receiverMetrics()`の`hardFail`へ接続し、`rxRiskGate`と総合判定の不一致を防止。新しい分類条件は追加していない。 |
| CLI出力互換 | `engine-contract/validate_nqx.py:705-711` | Windows標準コンソールでも新規メッセージのem dashを出力できるようstdout/stderrをUTF-8へ設定。検証条件への変更なし。 |
| 回帰fixture | `engine-contract/test-sl-too-close.nqx:1-5` | 指示書指定のEntry / SL / Invalidationを保持した最小NQX/1パケットを追加。 |

### 5-1 NQX validator回帰

- `python -m py_compile engine-contract/validate_nqx.py`: PASS。
- `valid-sample.nqx`: exit `0`。既存の`EVENT_REFERENCE` warning 1件のみ。
- `invalid-sample.nqx`: exit `1`。既存の不正パケットを期待どおり拒否。
- `test-sl-too-close.nqx`: exit `0`。この結果は実装不良ではなく、指示書の数値例と15%条件が数学的に両立しないためである。
- 指示値の計算: Entry midpoint=`28957.50`、risk=`29.75 pt`、Invalidation–SL gap=`12.00 pt`、比率=`40.34%`。15%境界は`4.4625 pt`であり、`12.00 pt`はエラー条件外。
- 監査分岐の実証として、fixtureを保存変更せずstdin上だけでInvalidationを直近安値`28928`へ置換した場合、gap=`0.25 pt`となり、exit `1`かつ`SL_INVALIDATION_TOO_CLOSE`を確認。

### 5-2 Nightwatch本体回帰

- HTML内のscript 2ブロックをNode `new Function`で全件コンパイル: PASS。構文エラーなし。
- `SAMPLE`をDOM非依存テストフックで読み込み、`validateAll()`を実行:
  - 変更前相当のReceiver verdict: `READY`
  - 変更後: scenario grade=`D / D`（グレード差分なし）
  - Stop Plan=`TOO TIGHT / TOO TIGHT`
  - 二監査不一致warning追加後のReceiver verdict: `REVIEW`
  - Risk Gate=`PASS`。両シナリオとも70%未満の臨界違反ではないため、これは期待どおり。
- 臨界分岐テスト（既存デモの`minimum=50.00 pt`に対し、指示事例の`risk=29.75 pt`を注入）:
  - grade=`BLOCKED`
  - reason=`HARD STOP CRITICALLY INSIDE STRUCTURE — 29.75 PT vs REQUIRED 50.00 PT`
  - Receiver verdict=`BLOCKED`
  - Risk Gate=`FAIL`
- Task Cの単体分岐: Entry=`28951..28964`、SL=`28955`で
  `SL DISTANCE IS NARROWER THAN THE ENTRY BAND ITSELF`と
  `LONG SL IS NOT BELOW THE ENTRY ZONE`の両warningを確認。
- 内蔵ブラウザによる`file://`ページ読込はBrowser URL Policyにより拒否された。禁止された迂回は行っていないため、スクリーンショットと実DOMコンソール検査は未実施。上記は同一HTMLソースを直接コンパイル・実行したコンソール回帰結果である。

### 変更前後のデモ差分

- grade: `D / D → D / D`（変更なし）。
- Receiver verdict: `READY → REVIEW`。
- 追加表示根拠: 両シナリオで`invalidationAudit=PASS`と`stopPlan=TOO TIGHT`が同時成立したため、Task Bの不一致warningが追加された。
- Risk Gate: `PASS → PASS`。70%未満の臨界ケースではないためBLOCKEDへは昇格しない。

### BLOCKED・明示的な仮定

1. **指示fixtureの期待結果はBLOCKED**: 指定価格の比率は`40.34%`であり、指定された`15%未満`条件では`SL_INVALIDATION_TOO_CLOSE`にならない。数値または閾値を独自変更せず、fixtureと計算結果をそのまま保存した。
2. `computeGrade`だけを変更しても既存`receiverMetrics()`はgradeをRisk Gateへ消費していなかった。Task Aの目的を満たすため、同じHTML内で既存の`BLOCKED`結果を`hardFail`へ接続した。分類式・既存監査・信頼度式は変更していない。
3. 臨界分岐の`minimum=50.00 pt`は新規定数ではなく、既存デモが`structuralStopPlan`で算出した値をテスト入力として再利用した。市場実績または推奨値として扱っていない。
4. 指定実トレード価格だけでは`market.levels` / OHLCが不足し、同一の`structuralStopPlan.minimum`を再構築できない。したがって「指定価格だけを貼り付ければ必ずBLOCKED」は証明していない。証明済みなのは、既存Stop Planが70%未満を返した場合のBLOCKED・Risk Gate・総合判定への伝播である。

### 自己監査

1. 数値を発明しない: PASS。新規閾値は指示済み`70%`と既存HTML由来`15%`のみ。fixture矛盾を閾値変更で隠していない。
2. 既存安全条件を弱めない: PASS。Invalidation順序、R:R、イベント、排他、信頼度計算、Stop Plan定数を変更していない。
3. 後付け検証禁止: PASS。既存sample、invalid sample、新規fixture、臨界分岐、バックストップを個別検証。
4. 成果粉飾禁止: PASS。fixtureが期待どおりFAILしない事実、ブラウザ検査不能、デモの`READY → REVIEW`を記録。
5. 無効生成物で既存分析を上書きしない: PASS。実市場シナリオ、確率、価格を生成していない。

---

## SLゲート強化・タスクD再設計完了 (2026-07-23)

### 完了条件との対照

| 項目 | 実装箇所（実装後の行番号） | 結果 |
|---|---|---|
| Z構造レベル抽出 | `engine-contract/validate_nqx.py:185-208` | `s/r/rbs/sbr/qml/ocl/a/v`をalias展開後にカンマ単位で分離し、点またはレンジの`(low, high)`へ変換する`structural_levels()`を追加。 |
| Zレコード保持・重複拒否 | `engine-contract/validate_nqx.py:401-422` | `zone` / `zone_line`を初期化し、Zを1件だけ保持。2件目は`DUP_RECORD`で拒否。 |
| 構造距離監査 | `engine-contract/validate_nqx.py:609-654` | 既存`SL_INVALIDATION_TOO_CLOSE`を維持したまま、その直後に`SL_STRUCTURAL_DISTANCE`を追加。追補指定の`0.32 / 0.72 / 2 / 4`以外の定数は追加していない。 |
| safety invariant 2b | `engine-contract/nqx1-spec.md:219-224` | Python監査が簡略proxyであり、観測レンジを持つHTML側`structuralStopPlan`が最終権威であることまで明文化。 |
| 実トレードfixture | `engine-contract/test-sl-structural-distance.nqx:1-5` | 2026-07-20の指定Entry / SL / Invalidation / Zレベルを保存した最小NQX/1を追加。 |
| valid sample回帰修復 | `engine-contract/valid-sample.nqx:5` | 新監査の初回実行ではS1/S2がFAILしたため、既に各シナリオ内で宣言済みだったInvalidation値`29580` / `29532`をZのS/Rへ明示。新しい価格は作らず、PythonとHTMLの双方で再検証した。 |
| 原タスクA/B/C/E | `app/nq-nightwatch-nqx-final.html` / invariant 2a | 追補の指示どおり変更・弱体化なし。 |

### fixtureのNQX/1適合補正

- 追補本文の`en=28951~28964`はNQX/1のレンジ構文ではない。`~`は推定値の接頭辞で、レンジ区切りは`..`であるため、fixtureでは`en=28951..28964`へ補正した。
- 現行validatorが必須とする`tr`、`co`、`cc`が追補の例から欠けていたため、`TEST` / `NO MARKET CLAIM`と明示したテスト専用文字列を追加した。実市場の観測事実は追加していない。
- 追補本文の手計算中にある`27927.75`は、同じ節のSL宣言および12pt計算と整合する`28927.75`の誤記として扱った。

### 3-1 Python validator回帰

- Pythonソースのin-memory compile: `PYTHON SYNTAX OK`。
- `test-sl-structural-distance.nqx`: exit `0`、`NQX VALIDATION PASSED`。`SL_STRUCTURAL_DISTANCE`は**出なかった**。
  - Entry midpoint=`28957.50`
  - risk=`29.75 pt`
  - nearest losing-side anchor=`28939.75`
  - Invalidation=`28939.75`
  - `proxy_unit=|28939.75-28939.75|=0`
  - `buffer=max(0×0.32,2)=2`
  - recommended=`28937.75`
  - `SL 28927.75 <= 28937.75`で`beyond=True`
  - `minimum=max(0×0.72,4)=4`、risk=`29.75 >= 4`
  - よって追補で予告されたとおり、Invalidationとanchorの一致によりproxyが縮退し、Python簡略監査はこの実例を見逃した。数値・閾値を変更してエラーを偽装していない。
- 許可されたEntry帯幅フロア`(e_hi-e_lo)×0.5`は採用しなかった。このfixtureでは`13×0.5=6.5`にしかならず、risk=`29.75`かつ`beyond=True`を変えないため、期待するエラーを発生させず、監査能力もこのケースでは改善しない。
- 監査分岐の実証として、保存fixtureを変更せずstdin上だけで`SL=28938`、`Invalidation=28945`へ置換した境界ケースを実行。anchor=`28939.75`、recommended=`28937.75`、risk=`19.50`、minimum=`4.00`となり、exit `1`かつ`SL_STRUCTURAL_DISTANCE`を確認した。既存R:Rを置換しなかったため、同時に出た`RR_MISMATCH` 3件も期待どおり。
- Z重複の最小stdin fixtureはexit `1`、`DUP_RECORD`のみを返した。
- `valid-sample.nqx`:
  - 初回はS1/S2の`SL_STRUCTURAL_DISTANCE` 2件でexit `1`。新監査による実際の回帰差分を確認した。
  - Zへ既存Invalidation値を明示後はexit `0`。既存`EVENT_REFERENCE` warning 1件のみ。
- `invalid-sample.nqx`: exit `1`、22 errors。既存の不正パケットを拒否し、新監査も矛盾したS1へ追加発火。
- `test-sl-too-close.nqx`: exit `0`。gap/risk=`12/29.75=40.34%`で15%条件外という既知の正しい結果を維持。
- `mnq_2026-07-17_1856_test.nqx`: exit `0`。

### 3-2 Nightwatch本体・実トレード数値照合

`nq-nightwatch-nqx-final.html`の`/*PURE_START*/`〜`/*PURE_END*/`をNode VMで直接実行し、同じ`test-sl-structural-distance.nqx`を`parseNQX`後、`validateAll()`と同じ監査順で処理した。fixtureの有効期間だけを独立に検査できるよう、評価時刻は`2026-07-20 18:40 JST`へ固定した。

| 値 | 手計算 | コード出力 | 判定 |
|---|---:|---:|---|
| Entry | `28951..28964` | `[28951, 28964]` | 一致 |
| Market now | `28905.5` | `28905.5` | 一致 |
| Z level count | `15` | `15` | 一致 |
| range unit | `≈59.43` | `59.43` | 一致 |
| range source | `MAPPED LEVEL SPACING` | `MAPPED LEVEL SPACING` | 一致 |
| anchor | `R 28939.75` | `R 28939.75` | 一致 |
| buffer | `≈19.02` | `19.0176` | 一致 |
| recommended SL | `≈28920.73` | `28920.7324` | 一致 |
| risk | `29.75` | `29.75` | 一致 |
| minimum | `≈42.79` | `42.7896` | 一致 |
| `minimum×0.70` | `≈29.95` | `29.95272` | 一致 |
| critical comparison | `29.75 < 29.95` | `true` | 一致 |
| Stop Plan | `TOO TIGHT` | `TOO TIGHT` | 一致 |
| Grade | `BLOCKED` | `BLOCKED` | 一致 |

追加確認:

- `invalidationOrderAudit=PASS`、`INVALIDATION PRECEDES SL BY 12.00 PT`。
- `rrClaimAudit=PASS`。再計算値は`3.0084R / 3.4286R / 4.4202R`で、宣言`3.01 / 3.43 / 4.42`と一致。
- BLOCKED理由は`STOP SITS INSIDE STRUCTURAL PROTECTION`と`HARD STOP CRITICALLY INSIDE STRUCTURE — 29.75 PT vs REQUIRED 42.79 PT`。
- 現在時刻で歴史fixtureをReceiver全体へ通すと、総合ラベルは時刻監査の優先順位により`STALE`へ上書きされ得る。ただし`grade=BLOCKED`と`riskFail=true`は失われない。上表はSLゲートだけを期限切れから分離するためfixture有効時間内で検査した。
- 修復後`valid-sample.nqx`も同じ純粋関数経路で確認し、S1/S2とも`stopStatus=STRUCTURAL`、Invalidation/R:R/Validity=`PASS/LIVE`、排他=`PASS`だった。

### 既知の限界

1. Python validatorはOHLCを持たず、HTMLのTrue Range平均またはレベル間隔中央値を再現しない。`proxy_unit=|anchor-invalidation|`は粗い安全網であり、HTMLの`structuralStopPlan`の代替ではない。
2. anchorとInvalidationが一致すると`proxy_unit=0`へ縮退し、最小フロア`buffer=2` / `minimum=4`だけになる。本実トレードfixtureがその実例であり、PythonはPASS、HTMLはBLOCKEDとなる。
3. Python側の対象は追補指定の`S/R/RBS/SBR/QML/OCL/A/V`のみ。HTML側は`market.levels`へ入る`POC/VAH/VAL`等も構造候補にできるため、候補集合も完全一致しない。
4. したがって出荷判定では、Python PASSをHTML側構造監査の代用にせず、両方を実行する必要がある。

### 指示ミスに対する自己監査

- 原指示書タスクDに対する前回実装は正しく、実装ミスではなかった。`12/29.75=40.34%`は15%未満ではないため、前回fixtureのexit `0`は正しい。
- 問題は「Invalidation–SL距離」と「SL–構造レベル距離」を同一視した要件定義側にあった。今回はその軸を分離し、既存15%監査を残したままZ構造監査を追加した。
- 再発防止として、今後は実装前に、指示書内の全数値例について式・単位・不等号・期待exit codeを手計算し、要件とfixtureの整合をpre-flightで確認する。
- 成果粉飾禁止: PASS。指定fixtureで新エラーが出なかった事実、proxy縮退、Python/HTMLの非同値性、valid sampleの初回FAILをすべて記録した。
- 数値を発明しない: PASS。追補指定定数だけを使用し、fixtureを無理にFAILさせる係数変更をしていない。
- 既存安全条件を弱めない: PASS。`INVALIDATION_ORDER`、`SL_INVALIDATION_TOO_CLOSE`、R:R、イベント、排他、HTMLのRisk Gateを変更していない。

---

## SLゲート強化・proxy_unit修正完了 (2026-07-23)

### 実装差分

| 項目 | 実装箇所（実装後の行番号） | 結果 |
|---|---|---|
| Receiver互換中央値 | `engine-contract/validate_nqx.py:209-218` | 奇数件は中央要素、偶数件は中央2要素の平均、空集合は`None`を返す`median()`を追加。 |
| MAPPED LEVEL SPACING | `engine-contract/validate_nqx.py:220-238` | `pivot`から抽出済みZ構造レベル境界までの絶対距離の中央値を`0.42`倍し、最小`2`とする`level_spacing_unit()`を追加。 |
| `proxy_unit`縮退修正 | `engine-contract/validate_nqx.py:641-685` | `\|anchor-invalidation\|`を廃止し、`M.px`、無ければ`Z.dz`中央値をpivotとするlevel-spacing unitへ差し替え。`buffer=unit×0.32`、`minimum=max(unit×0.72, \|entry-invalidation\|×1.12, 4)`をReceiverと同じ順で評価する。 |
| safety invariant 2b | `engine-contract/nqx1-spec.md:219-227` | `O.c=MISSING`時のMAPPED LEVEL SPACING再現と、実OHLC supplied時の既知の限界を明文化。 |
| 変更禁止領域 | `SL_INVALIDATION_TOO_CLOSE`、Z構造レベル抽出、HTMLタスクA/B/C/E | 変更なし。15%監査、Invalidation順序、R:R、イベント、排他、HTML Risk Gateを維持。 |

### 3-1 実トレードfixture再検証

- Pythonソースのin-memory compile: `PYTHON SYNTAX OK`。
- `test-sl-structural-distance.nqx`: exit `1`。エラーは期待した`SL_STRUCTURAL_DISTANCE` 1件のみ。
- 実コードから再取得した中間値:

| 値 | 第2追補の事前手計算 | Python実測 | 判定 |
|---|---:|---:|---|
| pivot | `28905.5` | `28905.5` | 一致 |
| Z構造レベル数 | `15` | `15` | 一致 |
| 距離中央値 | `141.5` | `141.5` | 一致 |
| unit | `59.43` | `59.43` | 一致 |
| anchor | `28939.75` | `28939.75` | 一致 |
| buffer | `19.0176` | `19.0176` | 一致 |
| recommended SL | `28920.7324` | `28920.7324` | 一致 |
| risk to SL | `29.75` | `29.75` | 一致 |
| Invalidation距離×1.12 | `19.88` | `19.880000000000003` | 浮動小数点誤差内で一致 |
| minimum | `42.7896` | `42.7896` | 一致 |
| beyond | `False` | `False` | 一致 |

実際のvalidatorメッセージも`recommended beyond 28920.73, minimum risk 42.79, range unit 59.43 from MAPPED LEVEL SPACING`となった。第1追補で`anchor == invalidation`により`proxy_unit=0`へ縮退していた見逃しは解消された。

### pivotフォールバック監査

- `M.px=MISSING`かつ`Z.dz=28895..28916`（中央値`28905.5`）のstdin fixture: exit `1`、同じ`unit=59.43 / recommended=28920.73 / minimum=42.79`で`SL_STRUCTURAL_DISTANCE`を確認。
- `M.px=MISSING`かつ`Z.dz`なしのstdin fixture: exit `0`。第2追補の指定どおり構造距離チェックだけをスキップし、既存監査へ委ねた。

### 3-2 既存fixture回帰

| fixture | exit | 結果 |
|---|---:|---|
| `valid-sample.nqx` | `0` | 従来どおりPASS。既存`EVENT_REFERENCE` warning 1件のみ。S1/S2への新規構造エラーなし。 |
| `invalid-sample.nqx` | `1` | 従来どおり22 errorsで拒否。新式の構造エラーは不正S1へ正当に発火し、総エラー数は増減なし。 |
| `test-sl-too-close.nqx` | `0` | 従来どおりPASS。`12/29.75=40.34%`で15%条件外という既知の結果を維持。 |
| `mnq_2026-07-17_1856_test.nqx` | `0` | 従来どおりPASS。 |

### 3-3 HTML Receiverとの再照合

`nq-nightwatch-nqx-final.html`の純粋関数区間をNode VMで直接実行し、同じfixtureを`parseNQX()`から`structuralStopPlan()`、`invalidationOrderAudit()`、`rrClaimAudit()`へ通した。

| 値 | Python | HTML Receiver | 判定 |
|---|---:|---:|---|
| Entry | `28951..28964` | `[28951, 28964]` | 一致 |
| Market now | `28905.5` | `28905.5` | 一致 |
| level count | `15` | `15` | 一致 |
| range unit | `59.43` | `59.43` | 一致 |
| range source | `MAPPED LEVEL SPACING` | `MAPPED LEVEL SPACING` | 一致 |
| anchor | `R 28939.75` | `R 28939.75` | 一致 |
| buffer | `19.0176` | `19.0176` | 一致 |
| recommended SL | `28920.7324` | `28920.7324` | 一致 |
| risk | `29.75` | `29.75` | 一致 |
| minimum | `42.7896` | `42.7896` | 一致 |
| beyond | `False` | `False` | 一致 |
| stop status | Pythonで`SL_STRUCTURAL_DISTANCE` | `TOO TIGHT` | 同義で一致 |
| Invalidation監査 | 既存順序監査PASS | `PASS` | 一致 |
| R:R監査 | 既存再計算PASS | `PASS` | 一致 |

HTML本体は変更していない。今回の`O.c=MISSING`・点レベル15件のfixtureでは、PythonとHTMLが同じ数式・同じ数値へ到達した。

### 依然として残る既知の限界

1. `O.c`に3本以上の実OHLCがある場合、HTML Receiverは`SUPPLIED OHLC RANGE`としてTrue Range平均を使う。Python validatorは本追補の範囲どおりOレコードをレンジ計算へ取り込まないため、level-spacingによる粗い監査のままであり、HTML側が最終権威となる。
2. 第1追補で固定されたPythonの構造候補集合は`S/R/RBS/SBR/QML/OCL/A/V`のまま維持した。今回の変更は`proxy_unit`計算だけであり、候補集合を拡張していない。
3. 成果粉飾禁止: PASS。指定fixtureのexit `1`、既存4fixtureの全結果、pivot欠損時のskip、実OHLC supplied時の非同値性をそのまま記録した。
4. 数値を発明しない: PASS。`0.42 / 0.32 / 0.72 / 1.12 / 2 / 4`は既存Receiverからの移植であり、追加閾値はない。
5. 既存安全条件を弱めない: PASS。`SL_INVALIDATION_TOO_CLOSE`、Invalidation順序、R:R、イベント、排他、HTML Risk Gateに変更なし。

---

## SLゲート強化・O-record True Range再現完了 (2026-07-23)

### Pre-flightで訂正した第3追補タスクFの前提

第3追補は`parseCandleRow()`だけを根拠に「Oレコードの実装形式はパイプ区切り」としていたが、実際のNQX受信経路を末端まで追跡すると、その結論は誤りだった。

| 層 | 実コード | 実際の契約 |
|---|---|---|
| NQX lexical layer | `nqxSplitEscaped(line, '|')` | 未エスケープの`|`はレコードフィールド区切り |
| NQX O-record adapter | `nq-nightwatch-nqx-final.html:2759-2763` | `O.c`の各行をカンマで分割して内部パイプ行へ変換 |
| strict candle parser | `nq-nightwatch-nqx-final.html:2264-2273` | 変換後の内部行を`TIME|OPEN|HIGH|LOW|CLOSE[|VOLUME]`として検証 |

したがって、追補例の`O|c=20:48|30018|...`をそのまま仕様化すると、`c`値が`20:48`で途切れ、後続価格が別のNQXフィールドとして解釈される。これはドキュメント追従ではなく既存輸送契約の破壊になるため実施していない。

代わりに`engine-contract/nqx1-spec.md:127-145`へ次を明記した。

- canonical NQX transportは従来どおり`TIME,OPEN,HIGH,LOW,CLOSE[,VOLUME]`。
- バー間は`;`。
- Receiverがcanonical行を内部パイプ行へ変換してから`parseCandleRow()`へ渡す。
- NQX値内でパイプ形式を使う場合、各`|`は`\|`へescape必須。未escapeの`|`はフィールド区切り。

canonicalカンマ版とescape済みパイプ版の双方をstdinで検証し、いずれもexit `0`を確認した。HTML本体は変更していない。

### 実装差分

| 項目 | 実装箇所（実装後の行番号） | 結果 |
|---|---|---|
| Receiver互換数値抽出 | `engine-contract/validate_nqx.py:242-252` | HTML `toNum()`と同じ数値抽出を行い、OHLCについて非有限値を拒否。 |
| O.c parser | `engine-contract/validate_nqx.py:254-302` | canonicalカンマ行を内部パイプ行へ変換後、HTMLと同じ必須数・数値・OHLC geometry条件で受理/拒否。拒否行は計算へ入れない。 |
| SUPPLIED OHLC RANGE | `engine-contract/validate_nqx.py:305-330` | 末尾12本、先頭は`H-L`、以降はTrue Range、有限かつ正の値を3件以上要求し、単純算術平均を返す。 |
| Oレコード保持 | `engine-contract/validate_nqx.py:526-575` | Oを1件だけ保持し、重複は`DUP_RECORD`。拒否行は`OBSERVED_CANDLE_REJECTED` warningとしてO行番号付きで報告。 |
| SL構造距離監査への接続 | `engine-contract/validate_nqx.py:773-795` | OHLC unitを優先し、3件未満なら既存`MAPPED LEVEL SPACING`へフォールバック。エラー本文は実際の`unit_source`を表示。 |
| safety invariant 2b | `engine-contract/nqx1-spec.md:227-236` | OHLC経路、拒否行除外、3本条件、level-spacing fallbackを明文化。 |
| 合成fixture | `engine-contract/test-sl-supplied-ohlc.nqx:1-6` | 12本の合成3分足を追加。`SYNTHETIC TEST OHLC ONLY — NOT MARKET DATA`をM.qrへ明記。 |

既存の`SL_INVALIDATION_TOO_CLOSE`、Z構造レベル抽出、`SL_STRUCTURAL_DISTANCE`係数、HTMLタスクA/B/C/E、Invalidation順序、R:R、イベント、排他は変更していない。

### 合成OHLC fixture検証

`test-sl-supplied-ohlc.nqx`はexit `0`、warning `0`。受理12本、拒否0本だった。

```
TR = [15, 14, 18, 27, 15, 17, 15, 17, 20, 13, 19, 15]
sum = 205
count = 12
unit = 205 / 12 = 17.083333333333332
source = SUPPLIED OHLC RANGE
```

第3追補の事前手計算`≈17.08`と一致した。新しい閾値や補正値は追加していない。

同じEntry / SL / Invalidationに対する構造監査の実測:

| 値 | Python |
|---|---:|
| Entry midpoint | `28957.50` |
| anchor | `R 28939.75` |
| unit | `17.083333333333332` |
| buffer | `5.466666666666667` |
| recommended SL | `28934.283333333333` |
| actual SL | `28927.75` |
| risk | `29.75` |
| minimum | `19.880000000000003` |
| beyond | `True` |
| `SL_STRUCTURAL_DISTANCE` | **発火なし** |

`SL <= recommended`かつ`risk >= minimum`なので、追補が予告したとおり同じシナリオでもOHLC実測レンジでは構造距離監査を通過した。結果を合わせるための係数調整はしていない。

### HTML Receiverとの直接照合

`nq-nightwatch-nqx-final.html`の純粋関数区間をNode VMで実行し、同じfixtureを`parseNQX()`、`rangeModel()`、`structuralStopPlan()`へ通した。

| 値 | Python | HTML Receiver | 判定 |
|---|---:|---:|---|
| accepted bars | `12` | `12` | 一致 |
| rejected bars | `0` | `0` | 一致 |
| TR series | `15,14,18,27,15,17,15,17,20,13,19,15` | 同左 | 一致 |
| unit | `17.083333333333332` | `17.083333333333332` | 一致 |
| source | `SUPPLIED OHLC RANGE` | `SUPPLIED OHLC RANGE` | 一致 |
| buffer | `5.466666666666667` | `5.466666666666667` | 一致 |
| recommended SL | `28934.283333333333` | `28934.283333333333` | 一致 |
| risk | `29.75` | `29.75` | 一致 |
| minimum | `19.880000000000003` | `19.880000000000003` | 一致 |
| beyond | `True` | `true` | 一致 |
| stop result | errorなし | `STRUCTURAL` | 同義で一致 |

### 不正バー・短いテープ・重複Oの監査

- 12本の正当バーに、高値<安値の行と非数値OHLC行を追加したstdin fixture:
  - exit `0`
  - `OBSERVED_CANDLE_REJECTED` warning 2件
  - 理由はそれぞれ`INVALID OHLC GEOMETRY` / `NON-NUMERIC OHLC`
  - 不正2行を除外し、残る正当バーで検証継続
- 正当バーが2本だけのstdin fixture:
  - `supplied_ohlc_unit=None`
  - 既存`MAPPED LEVEL SPACING`へフォールバック
  - `unit=59.43`、`SL_STRUCTURAL_DISTANCE` 1件、exit `1`
- Oレコード重複stdin fixture:
  - `DUP_RECORD` 1件、exit `1`

個々の不正バーは透明性warningだが、3本未満になればOHLC由来unitを採用しないため、少数の残存バーから不安定なレンジを作らない。

### 既存fixture回帰

| fixture | exit | 結果 |
|---|---:|---|
| `test-sl-structural-distance.nqx` | `1` | 従来どおり`SL_STRUCTURAL_DISTANCE` 1件。`unit=59.43 / source=MAPPED LEVEL SPACING`。元fixtureにはOレコード自体がないが、`O.c=MISSING`と同じfallback結果。 |
| `valid-sample.nqx` | `0` | 従来どおりPASS。既存`EVENT_REFERENCE` warning 1件のみ。 |
| `invalid-sample.nqx` | `1` | 従来どおり22 errorsで拒否。既存`EVENT_SRC` warning 1件。 |
| `test-sl-too-close.nqx` | `0` | 従来どおりPASS。 |
| `mnq_2026-07-17_1856_test.nqx` | `0` | 従来どおりPASS。 |

Python `py_compile`もexit `0`。

### 残る既知の限界

1. O.cの時刻文字列が実在時刻か、昇順か、重複していないかはHTML Receiverも検証しない。Pythonは互換性を優先し、与えられた受理順を時系列として使う。
2. OHLC geometryは検証できるが、それが本当に市場から観測されたデータかというprovenance自体はパケット単体では証明できない。`O`へモデル/予測値を入れない運用契約は引き続き必須。
3. 第1追補で固定された構造候補集合は`S/R/RBS/SBR/QML/OCL/A/V`のまま。今回の変更はrange unitのOHLC経路追加であり、候補集合を拡張していない。
4. 成果粉飾禁止: PASS。Task Fの前提誤り、合成fixtureで構造エラーが発火しなかった事実、不正行warning、短テープfallbackをすべて記録した。
5. 数値を発明しない: PASS。12本・3件条件・True Range式・単純平均はHTML既存実装の移植。fixture値は合成テスト専用と明記し、市場分析へ混入させていない。
6. 既存安全条件を弱めない: PASS。無効生成物で既存分析を上書きする経路は追加していない。

---

## OHLC抽出軽量モード完了 (2026-07-24)

### 実装結果

`app/nq-nightwatch-nqx-final.html`のAI INTELLIGENCE UPLINKへ、既存NQXパケットを保持したまま単一15M画像から`O|c=...`だけを補完する`MODE: OHLC / 15M CANDLE EXTRACT ONLY`を追加した。FAST/DEEPのパケット全体生成経路、Claude/OpenAI API送信関数、NQX Validator、Risk Gateは変更していない。

| 完了条件 | 結果 | 実装・確認内容 |
|---|---|---|
| 第3モード追加 | PASS | `#upMode`へ`ohlc`を追加し、説明を`SINGLE 15M FRAME · O.c ONLY · NO SCENARIO GENERATION`へ切替 |
| 単一画像制約 | PASS | OHLC時の上限を1枚へ限定。2枚同時投入は先頭1枚だけ受理し、以後の追加を拒否 |
| 無関係入力の抑制 | PASS | `SOURCE FRAMES`と`FRAME ROLES`をdisabled表示。FAST/DEEP復帰時に再有効化 |
| イベント検索抑制 | PASS | OHLC時は非表示かつチェックを一時退避してOFF。退出時に以前のチェック状態を復元 |
| 専用プロンプト | PASS | `upPromptOhlc()`を独立追加し、既存`upEvidenceProtocol()`を継承。出力を単一`O|c=`行へ限定 |
| 軽量実行枠 | PASS | 出力上限600 tokens、timeout 40,000 ms |
| 安全なOレコード更新 | PASS | `upMergeOhlcLine()`が既存Oを置換し、未存在時はNQX record orderに沿って挿入 |
| 不正応答時の保全 | PASS | ヘッダー欠損、O行欠損、受理可能バー0本では`#src`を変更せず拒否 |
| 解析反映 | PASS | 正常マージ後だけ`runParse(false)`を実行 |
| 保存設定 | PASS | localStorageから`ohlc`を復元可能 |

### 600 tokens / 40秒の決定

指示書の目安値をそのまま採用した。最大出力は12本の`TIME,OPEN,HIGH,LOW,CLOSE[,VOLUME]`を含む単一行であり、600 output tokensには十分な余裕がある。40秒は単一高精細画像のOCRを許容しつつ、FASTの75秒より短い専用上限である。実API課金を伴う計測は指示どおり実施していない。

### 純粋関数5ケース

| ケース | 結果 |
|---|---|
| 空の`srcText` | PASS — `PASTE OR COMPILE AN NQX/1 PACKET...` |
| 既存`O|c=MISSING`を有効2本で置換 | PASS — `replaced=true`, `barCount=2` |
| Oなしパケットへ挿入 | PASS — `G`直前、`replaced=false`, `barCount=2` |
| `O|c=MISSING` | PASS — 有効な欠損表現として`barCount=0`、エラーなし |
| 高値<安値の不正バーのみ | PASS — `NO VALID OHLC BAR ACCEPTED...`、元入力不変 |

追加で、モック応答にO行がない場合と不正geometryの場合の双方について、`runParse`が呼ばれず、`#src`が不変で、busy/button状態が復旧することを確認した。

### モックE2E・UI状態遷移

- 固定`O|c=`応答を返すClaude APIモックで、プロンプト生成 → 応答受信 → O行置換 → `runParse(false)`まで完走。
- OHLC選択時に入力2項目のdisabled、drop文言、file inputのmultiple解除、web検索非表示/OFFを確認。
- DEEPへ戻した際に入力、drop文言、multiple、web検索の以前のチェック状態が復元されることをDOMモックで確認。
- ローカル`file://`ページの実ブラウザ読込はブラウザ安全ポリシーにより拒否されたため、迂回せず、構文検査・DOMモック・関数モックで代替した。

### FAST/DEEP・安全監査回帰

変更前に固定したSHA-256と変更後の関数ソースを比較した。

| 対象 | 結果 | SHA-256 |
|---|---|---|
| `upPromptNQX()` | byte-for-byte PASS | `1eb6bcc029868d2f514330323cc947d537319c0acfe6a930ca51713d25c2737f` |
| `upPromptFast()` | byte-for-byte PASS | `fa1dd417e28f992fb88e6fdedc80d53690dfb939d97b7b273ba55b11ebc8c969` |
| `upCallClaude()` | byte-for-byte PASS | `314b54f67a30da4064abf40dd009082a8f6a7afa1836bc0203c61e89df65931b` |
| `upCallOpenAI()`周辺 | byte-for-byte PASS | `ef1bf8d8dccb59ff2d8549d28adc848a3c7d1fa9cacd50e75afe9641f02501b5` |

HTML内2 script blockはNode構文検査PASS。`validate_nqx.py`は`py_compile` PASS。fixture結果は以下のとおりで、変更前の期待を維持した。

| fixture | exit | 結果 |
|---|---:|---|
| `valid-sample.nqx` | 0 | PASS |
| `invalid-sample.nqx` | 1 | 既存どおり22 errorsで拒否 |
| `test-sl-structural-distance.nqx` | 1 | 既存どおり`SL_STRUCTURAL_DISTANCE` |
| `test-sl-too-close.nqx` | 0 | PASS |
| `test-sl-supplied-ohlc.nqx` | 0 | PASS |
| `mnq_2026-07-17_1856_test.nqx` | 0 | PASS |

### 自己監査・既知の限界

1. 画像が本当に15M表示か、バー時刻が時系列順かをブラウザコードだけで証明する手段はない。専用プロンプトとモデルの画像認識に依存する。
2. 画像から正確なOHLCを読めない場合は`O|c=MISSING`を受理する。価格の補間、クラウドや移動平均からの復元、形成中バーの推定はしない。
3. `upMergeOhlcLine()`は既存の`nqxResolveRaw`、`nqxSplitEscaped`、`parseCandleTape`を再利用する。受理バーが1本以上あれば不正行が混在していても既存receiverと同じく有効行を受理するため、全行完全一致を強制する新規規則は追加していない。
4. API実呼び出しはユーザーキーと課金を必要とするため未実施。API境界は固定応答モックで検証した。
5. FAST/DEEPの値は不変。`upIsFast()`はOHLCをFAST扱いしない厳密判定へ変更したが、FAST=true、DEEP=false、DOM欠損時=trueという既存2モードの結果は維持される。OHLC画像だけは高精細経路を使う。
6. 無効な生成物で既存分析を上書きしない: PASS。O行の構文・geometry検証を通過した場合だけ入力を書き換える。

---

## OHLC抽出運用訂正 — Codex手動ハンドオフ (2026-07-24)

### 訂正理由と最終方式

ユーザー指示「APIじゃなくてあなたにやってもらいます」に従い、直前節の**OHLCモードに限るAPI実行方式を廃止**した。FAST/DEEPは従来どおり任意のClaude/OpenAI API経路を維持する。OHLCは以下のローカル運用へ変更した。

1. Nightwatchで`COPY CODEX REQUEST`を押す。
2. 現在のCodexタスクへ、要求文と判読可能な15Mチャート画像1枚を添付する。
3. Codexが返す単一の`O|c=...`または`O|c=MISSING`行を専用欄へ貼る。
4. `APPLY CODEX O.c`を押し、ローカル検証とNQX全体監査を通過した場合だけ既存パケットのOレコードを置換または挿入する。

ページからCodexへ直接接続するAPIは追加していない。OHLCモードではAPI provider、model、key、remember、ページ内画像drop、event、web search、additional intelを非表示にし、ネットワーク呼び出しを行わない。

### UI・安全実装

| 項目 | 結果 |
|---|---|
| 手動ハンドオフUI | PASS — 3段階の操作案内、`#upOhlcResult`、専用ボタン文言を追加 |
| モバイル | PASS — 900px以下で3段階案内を1列化し、貼付欄を16px文字・112px高へ拡張 |
| API分離 | PASS — `runUplinkOhlc()`からprovider/model/key、画像、timeout、Claude/OpenAI呼び出しを全て撤去 |
| FAST/DEEP復元 | PASS — OHLC退出時にAPI行、drop、event、hint、ボタン文言、webチェック状態を復元 |
| 単一行契約 | PASS — 空、複数行、`O|c=`以外をローカルで拒否 |
| 全バーgeometry | PASS — 有効行と不正行が混在する応答も全体拒否へ強化。前節の既知の限界3を解消 |
| transactional merge | PASS — O行単体検証後、マージ済みNQXへ`validateNQX()`を実行し、errorが1件でもあれば`#src`を変更しない |
| シナリオ保全 | PASS — 更新対象はOレコードだけ。M/C/Z/E/G/S1..S3、Risk Gate、R:R、Invalidationを変更しない |

### 回帰検証

- HTML内script構文: PASS。
- `upMergeOhlcLine()` 7ケース: 既存O置換、Oなし挿入、MISSING、単独不正geometry、混在不正geometry、非NQX入力、複数O行を確認。
- DOMモック: OHLC選択時の専用UI表示とAPI入力非表示、FAST復帰時の全復元を確認。
- transactionモック: 有効Oのみ`runParse(false)`へ進み、不正O・複数行・NQX全体監査errorでは入力と既存分析が不変。
- 静的ネットワーク監査: `runUplinkOhlc()`に`upCallClaude`、`upCallOpenAI`、`fetch`、API key/provider/model参照なし。
- FAST/DEEP経路: `runUplink()`内のOHLC早期分岐後に、従来のClaude/OpenAI API選択が残ることを確認。
- NQX validator: `py_compile` PASS。`valid-sample`、`test-sl-too-close`、`test-sl-supplied-ohlc`、`mnq_2026-07-17_1856_test`はexit 0。`invalid-sample`と`test-sl-structural-distance`は意図どおりexit 1。
- 静的DOM ID: 142個すべて一意。追加IDの重複なし。

### 運用上の境界

Codexは画像に印刷されたOHLCまたは明確に判読できるローソク形状だけを観測値として扱う。形成中バー、隠れたヒゲ、オーバーレイで覆われた価格、補間値は生成しない。3本未満しか確信を持って読めない場合は`O|c=MISSING`を返す。NightwatchはCodexの画像判読そのものを証明できないため、provenanceの最終確認はユーザーが行う。

---
