# R11 指令 — ICT 文脈層の完成と昇格判断のための観測基盤

宛先: Codex。作業ディレクトリはこのリポジトリのルート。
発注経路・運用ルールを持つ実トレードシステムなので、**§0 を読み終えるまでコードに触れない**こと。

---

## 0. まず読む(この順で・作業前に必須)

| 順 | ファイル | 読む目的 |
|---|---|---|
| 1 | `CLAUDE.md` | 運用ルールの正本。特に §1 読み分け表 / §3「ICT 層」「Index SMT」/ §7 ループ指示 |
| 2 | `TRADING_CONTEXT.md` | 口座条件の正本(Apex 1口座・リスク $240・**枚数2枚固定**・SL上限 60pt) |
| 3 | **`docs/history/STRATEGY_EVOLUTION_R10.md`** | **★最重要。ICT は既に ADVISORY として実装済み。** 対応表(§1)・不変条件(§2.2)・未実装リスト(§6)がこの指令の前提 |
| 4 | `HANDOFF.md` | 直近の実測状態・踏んだ罠(R10 節と 8/21 Apex 初戦の検死) |
| 5 | `msnr_gate.py` + `tests/test_msnr_gate.py` | 実装の現状。R10 ブロック(約210行・53検証)が今回の拡張の土台 |

## 1. この依頼の出発点 — 「ICT をゼロから実装する」のではない

当初の依頼文は「YouTube で ICT コンセプトを学んで、画面のインジケータとマッチする戦略を
分析ルールに設定する」だった。**その大半は既に済んでいる**:

- ICT Core Content(Month 1〜12・213ページのノート)は読解済みで、R10 として
  `msnr_gate.py` に実装済み: **Premium/Discount/EQ・OTE 帯、DOL + LRLR/HRLR、
  キルゾーン(JST=ET+13)、FVG スキャン、Index SMT(ES 単独)**
- **MSNR 連鎖そのものが ICT の Turtle Soup / Breaker の機械化**(R10 §1)。再実装は禁止
- ICT 層は **ADVISORY**(観測専用)。武装可否 `allowed` / `grade` / `promotion` には
  一切影響しない。この設計は意図的で、昇格(ENFORCED 化)は3セッションの実測後に
  **ユーザーだけが決める**(R4 §1.3 と同じ手順)

したがって今回の仕事は「作り直す」ではなく、**(A) 残りの ICT 概念を同じ流儀で埋める /
(B) 画面にあるのに経路に乗っていないインジケータの受け口を作る /
(C) 昇格判断に必要な観測集計ツールを作る** の3点、
これにユーザー要望の **(E) チャレンジ突破ペース計算機(REPORT-ONLY)** を加えた4点である。

## 2. 実画面のインジケータ棚卸し(2026-08-22 スクリーンショット実測)

「画面のインジケータとマッチさせる」の対象は以下。配線済み/未配線を区別する。

| ペイン | インジケータ | データ経路 | bundle フィールド | 状態 |
|---|---|---|---|---|
| 0 (15m) | NQX SwingArm Pressure V2 1.1.0 | study_values `NQX_DATA_*` | `evaluation.htf`(手動転記) | 配線済み |
| 0/1 | Key Levels & Session Highs | pine_labels | `snapshot.levels` | 配線済み |
| 0/1 | Institutional Session VWAP Bands (Zeilerman) | study_values | `vwap` / `vwap_lo` / `vwap_hi` | 配線済み |
| 0/1 | VP & Opens | pine_lines "VP" | `snapshot.levels`(VAH/VAL/POC) | 配線済み |
| 1 (3m) | CVD-U 1 Session | study_values | `cvd` / `cvdFast` / `cvdSlow` | 配線済み |
| 1 (3m) | **MapleStax CBC**(CBC/Opening range/EMA cloud/VWAP/EMA200 の状態テーブル) | pine_tables(想定) | 無し | **未配線 → タスクB** |
| 1 (3m) | **Flux Charts FVGs**(High Dynamic) | 未確認 | 無し(R10 の `fvg_scan` は bars からの自前計算) | **未配線 → タスクB** |

## 3. スコープ

### タスクA — R10 §6 の未実装3件を ADVISORY で実装(`msnr_gate.py`)

| 項目 | 内容 | 必要データ |
|---|---|---|
| CBDR / Asian Range STD | 14:00–20:00 ET のレンジと Asian Range を標準偏差単位で投影(ICT M8 EP3/4) | bars3m か、足りなければ optional な `snapshot.dailyBars` |
| ADR 投影 1.27 / 1.62 | 5日 ADR とその拡張(M12 EP4 Step9)。ランナー最終目標の根拠(CLAUDE.md §2 TP到達性の補強) | optional な `snapshot.dailyBars`(無ければ `None`=中立) |
| Power of 3 | 日足始値からの expansion 方向で日足バイアスを定型化(accumulation→manipulation→distribution) | 同上 |

実装の流儀は R10 と完全に同じにする:
- 純粋関数として追加。**`evaluate()` の promotion 確定後**に計算(`rrPotential` / `ict` と同じ位置)
- 出力は `--summary` 末尾と `--card` の `advisory` 配下。縮小順(rrPotential → ict → advisory 全体)に新要素を組み込む
- 入力欠落・壊れた値では**例外を出さず `None`**(= 中立。`peers` と同じ思想)
- bundle スキーマへの追加は全て **optional**(無くても既存経路が一切壊れない)

### タスクB — 未配線インジケータの受け口(ADVISORY・欠落=中立)

1. `snapshot.fvgLines`(optional): Flux Charts FVG の帯を `[{hi, lo, kind}]` で受け、
   R10 `fvg_scan`(自前計算)との**照合結果**を advisory に出す(一致/不一致は観測材料)
2. `snapshot.cbc`(optional): MapleStax CBC のテーブル状態
   (`{market, cbc, openingRange, emaCloud, vwap, ema200}` 各 `bullish|bearish|inside`)を受け、
   提案 side との整合を advisory 1行で出す

**取得側の実装はしない**(TradingView MCP は PC 側 Claude Code 専用)。代わりに
「PC 側がどのツールでどう取得して bundle に入れるか」の手順書ドラフトを R11 ドキュメントに書く。
pine_tables で CBC テーブルが実際に取れるかは未実測なので、**手順書には未実測である旨を明記**する。

### タスクC — 昇格判断のための観測集計ツール(新規 `ict_observe.py`)

R10 §4.1 の「観測すべき問い」に答える集計スクリプト。入力は
`.secrets/monitor_cycle_*.json`(ローカルに約400件)と `.secrets/day_ledger.jsonl`。

出力(1コマンドで表1枚):
1. `PREMIUM_DISCOUNT_WRONG_SIDE` の有無 × 実現 R の差
2. `HRLR` 付き武装の伸び(`rb1R` との相関)
3. キルゾーン別(特に LUNCH 帯)の成績分布
4. (タスクA実装後)新 ADVISORY の発火率と成績の対応

これが揃うと「3セッション分の観測を HANDOFF に貯めてユーザーが昇格判断」が
手作業の転記なしで回る。**集計するだけで、昇格の判定・推奨はしない。**

### タスクE — チャレンジ突破ペース計算機(新規 `challenge_calc.py`・REPORT-ONLY)

背景: ユーザーの目標は Apex 評価フェーズ(利益目標 $9,000 / 残機 $4,000)を
**できるだけ速く**通過すること。「1トレードのリスクをいくらにすれば、
合格確率と所要トレード数がどう変わるか」を実測分布から数字で出す。

- 入力: 残機・利益目標(既定は TRADING_CONTEXT.md の $4,000 / $9,000)、
  勝率と R 分布(`.secrets/day_ledger.jsonl` の実績から推定。
  データ不足時は `--wr` / `--avg-win-r` / `--avg-loss-r` で手動指定)
- 出力(表1枚): リスク/トレード = **$120 / $240(現行)/ $480 / $720 / $1,200 /
  フルポート(口座上限枚数)** の各行について
  - 合格確率(gambler's ruin の解析解 + モンテカルロで相互検算)
  - 期待トレード数・破綻確率
  - **その行の SL 上限(pt)が直近ノイズ床の何倍か**
    (フルポート行は SL 上限がノイズ床を下回る事実がそのまま数字に出る)
- テスト: エッジゼロの解析解 `P = b/(a+b)` と一致すること(既知分布での検算)
- **制約**: `order.py` / `dayguard.py` / 枚数・リスクの現行値には一切触れない。
  これは**意思決定の材料**であり、リスク量の変更は TRADING_CONTEXT.md の改訂として
  ユーザーだけが行う。計算機が何を出力しても、発注経路の $240 / 2枚 / 60pt は不変

### タスクD — ドキュメント

- `STRATEGY_EVOLUTION_R11.md`(R10 と同じ構成: 対応表 / 実装 / 不変条件 / 実測 / 変更ファイル)
- `CLAUDE.md` §3 / §7 に**最小限の追記**(新 ADVISORY の1行転記ルール。既存文体・既存の表形式に合わせる)
- `HANDOFF.md` 冒頭に引き継ぎ節
- `docs/reports/CODEX_IMPLEMENTATION_REPORT.md` の流儀で実装報告(**実装しなかったこと・その理由**を必ず含む)

## 4. 変えてはいけないもの(1つでも破ったら不合格)

1. **ADVISORY 不変条件**: 実データ全バンドルで `allowed` / `grade` / `blockers` / `promotion` の
   差分 **0件**(R10 §2.2 と同じ回帰。変更前バックアップとの比較で検証)
2. **Phase 2(OTE への Entry 置き直し)は実装しない。** CLAUDE.md §2「Entry は構造に置く」の
   変更に当たり、ユーザー承認待ち(R10 §4.2)。設計メモを R11 に書くまでは可
3. **ENFORCED 化・昇格をしない**(LUNCH 帯停止などの Phase 3 候補も含む)
4. `monitor_publish.py` / `order.py` / `dayguard.py` / `nqx_state.py` / `telegram_bot.py` /
   Mini App / Worker は**無変更**(R10 と同じ制約。タスクBの受け口も `msnr_gate.py` 側で完結させる)
5. 評価カードは **4096 バイト以下**(超えると Worker が evaluation を丸ごと null にする)。
   最大サイズを実測して報告する
6. 口座数値($240・2枚・60pt)をコードに再定義しない。既存の定数・環境変数を参照する
7. 時刻は既存機構(`ict_session` / `ICT_ET_OFFSET_H`)を使う。JST=ET+13(夏時間)を新たに書かない
8. Windows 前提: 実行は `PYTHONUTF8=1`、JSON 読みは `utf-8-sig`(BOM)許容。
   `open(p,'w').write(open(p).read())` の1行版はファイルを空にする(CLAUDE.md §7 の実害)
9. チャート・TradingView 側には一切触れない(そもそも Codex からは触れない)

## 5. 受け入れ基準(全て満たして完了)

- [ ] `PYTHONUTF8=1 python tests/run_all.py` → **ALL PASS**(既存 75 + R10 53 を退行させない)
- [ ] 新機能ごとにテスト追加(R10 と同粒度: 正常系 / 入力欠落系 / **promotion 不変**)
- [ ] 実データ回帰 diff 0(`.secrets/monitor_cycle_*.json` 全件。無い環境で作業した場合は
      その旨を報告書に書き、PC 側で実行する回帰コマンドを1行で提示する)
- [ ] カード最大サイズ実測 ≤ 4096 バイト
- [ ] `challenge_calc.py` が解析解との検算テストを通り、発注経路に無影響であること
- [ ] タスクDのドキュメント4点
- [ ] 変更ファイル一覧と無変更ファイル一覧(R10 §7 の形式)

## 6. 検証コマンド

```
PYTHONUTF8=1 python tests/run_all.py
PYTHONUTF8=1 python msnr_gate.py --summary < .secrets/monitor_cycle_XXXX.json
PYTHONUTF8=1 python msnr_gate.py --card    < .secrets/monitor_cycle_XXXX.json
PYTHONUTF8=1 python ict_observe.py
PYTHONUTF8=1 python challenge_calc.py
```

## 7. 不明点の扱い

推測で実装しない。判断が割れる点は報告書の「未裁定の論点」に列挙して止める
(`HANDOFF.md` の流儀)。特に ICT の解釈が R10 の対応表と食い違うと感じた場合は、
**R10 を正とし**、異論は論点として書く。
