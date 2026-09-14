# Edge Ledger — 証拠台帳

本ファイルは `NQX_EVIDENCE_BASED_STRATEGY_UPGRADE_BLUEPRINT.md` 7章の運用規則に従う。1モジュール=1エントリ。状態機械: `CANDIDATE → REPRODUCING → VERIFIED → LIVE-MONITOR`、失敗時は`REJECTED`。**VERIFIEDへの遷移はユーザー承認が必須**（GPT指示書8-1）。

最終更新: 2026-08-30（R48: ICT/Alchemist/RTM リサーチ第2弾の転記）

---

## M1: Opening Range Breakout (ORB)

- 格付け: E1(暫定) [Zarattini & Aziz 2023 / SSRN 4416622] + E1反証(確定) [Mesfin 2026 / arXiv 2605.04004] + E2(暫定)×3 + E3×3
- 出典報告値 [SRC]:
  - Zarattini & Aziz 2023（QQQ/TQQQ, 2016-2023）: 総リターン675%(QQQ)/1,484%(TQQQ)、年率アルファ33%（原文未読、二次資料集約）
  - **Mesfin 2026（MNQ, 2021-2025, N=447/428/83）: ORB Long bar+15 T=1.50・平均+2.82pt・勝率55.5%で全バリアントFAIL。年次分解で2022年-1.42/2023年+2.43/2024年+7.04と単年依存が判明**（全文精読済み、確定値）
  - TradeThatSwing（NQ、単年）: 平均勝ち$846/平均負け$987（PF・勝率は本セッションで再確認できず）
  - QuantifiedStrategies（株価指数系）: 198取引・勝率65%・平均利益0.27%、著者自身がエッジ経年劣化を明記
  - TradingView tkey1（NQ、作者報告）: ショート19取引・勝率63.2%・PF 2.34（単一トレンド期間）
- 当方再現 [TV-ST/BOOT]: **未実施。ハーネス(`nqx_bt_m1_orb.pine`)はR2で実装・コンパイル済みだが、TradingView Essentialプランのインジケーター枠上限によりチャートへの追加が阻まれ、実測(IS/OOS/CI)は2026-07-18時点でユーザー判断によりスコープ外**（GPT_IMPLEMENTATION_REPORT.md R2完了節参照）
- コスト前提: 未実施（ハーネス側はBASELINE $3.00/RT・STRESS $4.48/RTの切替に対応済みだが未実行）
- 試行数: TRIAL_REGISTRY該当行=M1-00〜M1-05（6件、全て「未実行」。数値は一切埋めていない）
- 状態: **CANDIDATE**（変更日: 2026-07-18 根拠: R1精読完了。反証文献がMNQ実データで全ORBバリアントFAILと報告しており、当方コスト環境での再現前から強い懐疑的事前予想を持つべき。R2でハーネスは完成したが実測未実施のためCANDIDATEから進めない）
- 備考: Mesfin(2026)のMNQ結果とTRIAL_REGISTRY実行後の自分の結果を必ず突合すること（設計書6-5要求、将来実測を再開する場合に適用）。同一著者(Zarattini)系論文は1系統としてカウント。実測を再開する手順はGPT_IMPLEMENTATION_REPORT.mdのR2節「次フェーズへの引き継ぎ事項」に記載。

## M2: Noise-Band Intraday Momentum

- 格付け: E1(暫定) [Zarattini/Aziz/Barbon 2024 / SSRN 4824172、原文未読] + E2反証(暫定) [Quant Macro Substackレビュー]
- 出典報告値 [SRC]: SPY 2007-2026、総リターン+1,985%（net）、年率19.6%、Sharpe 1.33（原文未読、二次資料集約のみ）
- 当方再現: 未実施
- コスト前提: 未実施
- 試行数: なし
- 状態: **CANDIDATE**（変更日: 2026-07-18 根拠: 原文未読。SFI/alexandria.unisg.chへのアクセスが本セッションのWebFetchでは失敗(405)。反証レビューの論点(執行想定の暗黙性・予測性vs同時相関性)を確認済み）
- 備考: R4検証時、執行想定（スプレッド・キュー位置）の明示化をTRIAL_REGISTRY登録時の必須チェック項目とする。

## M3: VWAP Bias / Pullback

- 格付け: E1(暫定) [Zarattini & Aziz 2023 VWAP / SSRN 4631351、原文未読] + E3×1
- 出典報告値 [SRC]: QQQ 2018-2023、671%リターン（net）、最大DD 9.4%、Sharpe 2.1（原文未読、二次資料集約のみ）
- 当方再現: 未実施
- コスト前提: 未実施
- 試行数: なし
- 状態: **CANDIDATE**（変更日: 2026-07-18 根拠: 原文未読。同一著者(Zarattini)系のためORB/VWAP系との独立性は限定的）
- 備考: MNQ ORB Strategy - VWAP + Bias（TradingView E3）が追試素材として最も対象市場が一致。

## M4: IBS / RSI(2) スイングバイアス

- 格付け: E2(暫定)×2（うち1件NQ先物直接） + E3×1
- 出典報告値 [SRC]:
  - QuantifiedStrategies IBS（**NQ先物**）: 86取引・平均利益$600/枚・**PF 4.13**（原文未読、bot壁でBLOCKED）
  - QuantifiedStrategies RSI Mean Reversion（QQQ）: CAGR 12.7%・232取引・勝率75%・PF 3.0・最大DD 19.5%・Sharpe 2.85（同上、未読）
- 当方再現: 未実施
- コスト前提: 未実施
- 試行数: なし
- 状態: **CANDIDATE**（変更日: 2026-07-18 根拠: IBS PF 4.13はN=86の小標本であり設計書J-5の取引数≥200基準を満たさない。統計条件(95%CI)通過まで宣言不可を再確認）
- 備考: 日中モジュールではなくNightwatch日次バイアスパネルとして統合予定（設計書5-3 M4の指示通り）。

## M5: セッション/オーバーナイト統計パネル

- 格付け: E2(暫定)
- 出典報告値 [SRC]: 「S&P500利益の大半が1993年以降オーバーナイトセッション由来」という趣旨（本セッションで一次スニペットを再確認できず、BLOCKED-SOURCE）
- 当方再現: 対象外（表示のみのパネル、トレードしない）
- 状態: **CANDIDATE**（変更日: 2026-07-18 根拠: 出典の再確認が必要）

## M6: FVG反応（ICT Fair Value Gap）

- 格付け: E2/E3 [Dhaval Barot / MPM Markets "Does the Fair Value Gap Strategy Work?"]
- 出典報告値 [SRC]: ES/NQ/GC/SI、1分約250万本、2019-04〜2026-05。36条件中34条件で反応率がランダムより中央値約5pt高い。**単純時間足出口では WR≈73% / PF 2.2–2.5 に見えるが、正確な1分順序で検証すると WR≈50% / PF≈1.0 に崩壊**（時間足OHLCのバー内順序が見えないことによる見かけの成績）
- 当方への含意: FVG は**反応はあるがエッジではない** → 現行の「加点のみ・ハードゲートにしない」設計を維持する直接根拠。バー内順序の罠は当方バックテスト設計(J章)の必須チェックに追加
- 状態: **CANDIDATE（advisory恒久）**（変更日: 2026-08-30 根拠: R48リサーチ転記）

## M7: Turtle Soup 原典（Raschke/Connors, Street Smarts）

- 格付け: 原著定義（高確信）。NQ 3分足への性能移植は MISSING
- 定義 [SRC]: 新20日安値 + 旧20日安値が**4セッション以上古い** + 旧安値上への買いストップ、当日極値下へのSL（売りは逆）。日足ルール
- 当方への含意: `TURTLE_SOUP_REVERSAL` に `CLASSIC_TS` 特徴量タグを追加（20日極値・旧極値4セッション以上）。スコアカードで classic 条件の有無別に成績を分離集計する
- 状態: **CANDIDATE**（変更日: 2026-08-30）

## M8: ストップ・カスケード機構（Osler）

- 格付け: E1 [Carol Osler "Stop-Loss Orders and Price Cascades in Currency Markets", NY Fed SR150]
- 出典報告値 [SRC]: FX分足 1996–2000、注文9,655件。ストップ注文はラウンドナンバー付近に集中し、発動時の変動は take-profit より速く長い
- 当方への含意: スイープ反転の**市場機構の裏付け**（売買優位性の証明ではない）。指数先物の価格クラスタリング研究（ap Gwilym et al. / Chung）も同方向
- 状態: **機構文献として常設**（変更日: 2026-08-30）

## M9: NQ 記述統計パネル（時間帯・PDH/PDL・ギャップ）

- 格付け: E3 [TradingStats HOD/LOD 3,034セッション] + E2/E3 [Hawaii Trading Academy NQ Research] + E3 [fractiz Day-of-Week]
- 出典報告値 [SRC]（すべて記述統計・費用なし・方向性シグナルではない）:
  - HOD/LOD 累積形成率: 09:30 で H36%/L42%、12:00 で H60%/L68%、15:00 で H77%/L85%。15:30 が単一時刻の最頻
  - PDH タッチ 56.8% / PDL 43.4% / 両方 12.7% / どちらも無し 12.5%
  - 夜間(18:00–09:30)高安の少なくとも一方を RTH で破る日 94.2%
  - London range ブレイク後のレンジ内回帰: 5分以内 67.2% / 30分以内 83.0%
  - 09:30 の5分レンジは典型値の 4.69倍、出来高 30.36倍
  - ギャップ埋め率: 全体 61.1%、前日レンジ比 0.25以上 42.4%、0.50以上 34.2%
- 当方への含意: `DAY_MATURITY` / `LONDON_RANGE_SWEEP` / ギャップ bucket を**注釈特徴量**として記録（R48）。スコアカードのNが貯まるまで採点重みには使わない
- 状態: **CANDIDATE（特徴量記録中）**（変更日: 2026-08-30）

## M10: Market Profile 80%ルール / VA回帰

- 格付け: E2 [pedrobraiti/volume-profile-trading 公開コード検証]
- 出典報告値 [SRC]: SPY/QQQ 日足・30分、費用込み。QQQ Edge-to-Edge PF 2.06 等。ただし**80%ルールは通過率27–67%と不安定で、信頼区間が1を跨ぐ**
- 当方への含意: `VP80_REVERSION` を「確定優位」と呼ばない。VA回帰・POC回帰・Edge-to-Edge は別仮説として将来の Strategy Tester 検証(J/K章)対象
- 状態: **CANDIDATE**（変更日: 2026-08-30）

## M11: Quarterly Theory（Daye / ICT派生の時間分割 + true open Judas）

- 格付け: E4/一次概念のみ（Daye のコミュニティ教材。公開バックテスト MISSING）。R48リサーチでも独立PFは確認できず
- 定義 [SRC]: 取引日(18:00起点)を 6h×4(A/M/D/X)→90分×4→22.5分×4 へ分割。true daily open = NY 00:00。Q2(London)は誘導(Judas)の位相
- 当方実装（R49・2026-08-30）:
  - **時刻は位相を決めるが方向は決めない**原則を維持。方向は `judas_from_true_open` が**価格から**導く: London窓で trueOpen±minExc（当日3分レンジ中央値、下限1tick）を超えて誘導し、直近確定足が trueOpen の反対側で閉じたときだけ SELL/BUY
  - 票決: `detect_quarterly_theory` が Judas成立 + 90分クォーター D 位相のときだけ CONFIRMED（既存の ±2票しきい値の中の1票。単独では武装を動かせない）
  - 記録タグ: `QT_M_PHASE_SWEEP`（M位相で起きたスイープ）/ `QT_JUDAS_ALIGNED`（候補方向とJudas方向の一致）→ スコアカードで分離集計
  - 文脈: `decision.context.quarterly`（session/quarter/micro の位相グリッド）
- 状態: **CANDIDATE（1票+記録タグ）**（変更日: 2026-08-30 根拠: R49。票の重み昇格はスコアカードの QT_JUDAS_ALIGNED 別成績が N≥50 を超えてから検討）

## 却下リスト（R48確定。再提案には新証拠が必要）

- **Malaysian SNR の 4/2 使用回数ルール**: 出典が E4 のみ・定義が資料間で矛盾
- **FVG の固定失効（3取引日説）**: 一次根拠なし。年齢はバー数の特徴量として保存する
- **SMT の発注条件昇格**: 独立PFが MISSING。価格発見リード研究(Kurov & Lasser, E1)は乖離反転シグナルの根拠にならない
- **Silver Bullet 3窓の合算検証**: 窓ごとに別モデルとして扱う
- **Venom / Unicorn / MMXM 等の新モデル追加**: 既存4モデルのスコアカード実測(N≥200)が出るまで凍結
- **Alchemist / Malaysian SNR / RTM の PF 主張**: 公開検証ゼロ（2026-07-18 と 2026-08-30 の2回の走査で確認）。判定機構は当方実測でのみ磨く

## 反証・方法論文献（宣言の背骨、REJECTED相当の常設エントリ）

- **arXiv 2605.04004（Mesfin 2026）**: MNQ・14シグナルファミリー全てFAIL。ORBはT=1.17〜1.50で全バリアント基準未達（N=447/428/83）。全文精読済み・確定。**本設計の既定姿勢（懐疑）の直接的根拠**。
- **ICT Cameron Scalp Model 再現（hindsight-finance, E2/E3）**: NQ 30秒・OOS 2022-07〜2025-11・RT費用0.25pt。base N=11,391 WR48.7% **PF 0.81**、High-RR N=7,138 WR22.9% **PF 0.95**（ISのPF1.68がOOSで崩壊）。**ICT複合モデルの機械化がコスト後に負ける実例**（2026-08-30追加）。
- **MPM FVG 1分順序検証**: 時間足OHLC出口で PF 2.2–2.5 に見える成績が、正確な1分順序では PF≈1.0。**バー内順序を無視したバックテストを信用しない**（J章チェックリストへ反映、2026-08-30追加）。
- **Holmberg et al. 2013 (FRL)**: abstract水準のみ確認。反証としての結論を本セッションでは直接確認できず、次回完読必須（暫定維持）。
- **Bailey & López de Prado 2014 (DSR)**: 要旨確認済み、数式精読は未実施。J-6実装前に再読必要。
- **Quant Macro Substackレビュー / CXO Advisory批判**: 要旨確認済み。M2/M1検証時の必須チェック項目として反映済み。

## 状態変更ログ

| 日付 | モジュール | 変更 | 根拠 |
|---|---|---|---|
| 2026-07-18 | M1-M5 | 初期状態=CANDIDATE設定 | R1完了、EDGE_LEDGER初版作成 |
| 2026-08-30 | M6-M10 | 追加（ICT/Alchemist/RTM第2弾リサーチ転記） | R48。MSNR/RTM=MISSINGを再確認（2度目）。ICT Cameron Scalp の OOS 崩壊（PF 0.81/0.95, N=11k/7k, NQ 30秒, RT$0.25×2pt換算費用込み）を反証欄へ追加 |
| 2026-08-30 | M11 | 追加（Quarterly Theory の通電・R49） | 不活性だった quarterly を true open Judas（価格由来の方向）で票決へ接続。1票+記録タグに限定 |

---
*本台帳の全報告値は出典の主張または当方の再現値であり、過去実績は将来の収益を保証しない。*
