# MSNR 確認連鎖ゲート — 実装指示書(Opus 5 向け)

**発行**: 2026-08-18(Fable 5 監査に基づく)
**実装者**: 新規セッションの Opus 5
**目的**: preview アプリのプロンプト生成器にしか存在しない MSNR
(Alchemist / Malaysian SNR)ドクトリンの確認連鎖を、ライブ3分監視ループに
**機械判定として**移植する。

---

## 0. 背景 — なぜやるか(1分で読める要約)

2026-08-18 の監査で判明した事実:

1. MSNR ドクトリンは
   `スイープ → リクレイム → displacement → 実体クローズMSS → 保持されたリテスト`
   を要求し、「ブレイクだけなら SWEEP CANDIDATE 止まり」と明記している
   ([preview/app-canonical.html:5592](preview/app-canonical.html))。
2. **8/17 の2敗はどちらも「確定足ブレイク→リテスト」だけで武装した。**
   MSNR 自身の基準ならどちらも CANDIDATE 止まりで武装できなかった。
3. つまりドクトリンは現行ルールより厳しいのに、ライブループに接続されて
   いない。この指示書はその接続を行う。

**設計方針(この3つが守られない実装は不合格)**:

- **暗黙知ゼロ**: すべての判定は本書 §3 の数値定義だけで再現できること。
  「明確な離脱」「十分な確認」のような形容詞を判定に使わない。
- **機械実行**: 判定は Python スクリプト `msnr_gate.py` が行い、監視中の
  Claude はその出力を読むだけ。Claude がチャートを見て連鎖の成否を
  判断する工程を残さない。
- **サイクルコスト増ゼロ**: 入力は毎サイクル既に書いている bundle JSON
  そのもの。追加の MCP 呼び出しは 0 回。追加コストは python 起動1回のみ。

---

## 1. 成果物一覧(この順で実装する)

| # | ファイル | 種別 | 内容 |
|---|---|---|---|
| 1 | `msnr_gate.py` | 新規 | 連鎖判定エンジン(§3〜§5) |
| 2 | `tests/test_msnr_gate.py` | 新規 | §7 のテスト一式 |
| 3 | `CLAUDE.md` | 編集 | §2/§3/§7 に §6 の文面を挿入 |
| 4 | `TRADING_CONTEXT.md` | 編集 | §6 に注記1段落(§6.4) |
| 5 | `~/.claude/skills/mnq-nightwatch-nqx/references/operational-gates.md` | 編集 | §9 新設(§6.5) |
| 6 | `project/claude-skills/mnq-nightwatch-nqx/references/operational-gates.md` | 同期 | 5 と完全同一に |
| 7 | `preview/app-canonical.html` / `preview/nq-nightwatch-nqx-final_1.html` | 編集 | Stand down 行の拡張(§6.6) |
| 8 | `HANDOFF.md` | 編集 | 完了記録を先頭セクションに追記 |

**触ってはいけないもの**: `order.py` / `telegram_bot.py` / `nqx_state.py` /
`monitor_publish.py`(読むのは可。ボラゲートのテスト
`test_market_feed.py` / `test_oneclick.py` の純度検査を壊さないため、
今回は monitor_publish への組み込みは行わない — msnr_gate は独立 CLI)。
チャート設定の変更・発注も一切行わない。

---

## 2. 入出力契約

### 入力(stdin)

`monitor_publish.py` に渡しているのと同じ bundle JSON。使用するキーは
以下のみ。他のキーは無視する(存在しなくてもよい)。

```jsonc
{
  "at": "2026-08-18T02:49:30+09:00",
  "priceAt": "2026-08-18T02:49:20+09:00",   // 確定足判定に使用。無ければ at
  "snapshot": {
    "levels": [ {"label": "PP", "price": 30166.92, "source": "..."} ],
    "bars3m": [ {"t": 1786978080, "o": 30220.5, "h": 30224.5,
                 "l": 30213.5, "c": 30219.5, "v": 8414} ]
    // bars3m が無ければ snapshot.bars を使う
  }
}
```

- **BOM 許容必須**: `sys.stdin.buffer` を読み `utf-8-sig` でデコードする
  (Write ツール製 JSON は BOM 付き。既知の実害あり)
- キー `h/l` と `high/low` の両対応(monitor_publish の `_num` と同方針)。
  bool は数値として拒否する

### 出力(stdout、UTF-8、ensure_ascii=False)

```jsonc
{
  "at": "2026-08-18T02:49:30+09:00",
  "closedBars": 59,
  "noiseFloor": 12.25,          // §3.1
  "touchTol": 2.0,              // §3.2 実効値
  "rotation": { "signals": 5, "negations": 2, "ratio": 0.40, "verdict": "OK" },
  "levels": [
    {
      "label": "PP", "price": 30166.92,
      "mergedWith": ["CPR-TC"],          // §3.3 で吸収したラベル
      "dynamic": false,                  // true = VWAP系。連鎖評価対象外
      "freshness": "WICK_TESTED",        // §3.4 の6状態
      "bodyTouches": 1,
      "anchorOk": true,                  // freshness ∉ {BROKEN, CONSUMED}
      "chains": [
        { "side": "BUY", "state": "MSS_CONFIRMED",
          "sweepBarT": 1786987620, "mssBarT": 1786988160,
          "retestBarT": null, "barsLeft": 8,
          "blockers": ["RETEST_NOT_HELD"] }
      ],
      "promotion": {
        "BUY":  { "allowed": false, "blockers": ["RETEST_NOT_HELD"] },
        "SELL": { "allowed": false, "blockers": ["NO_CHAIN"] }
      }
    }
  ],
  "summary": "MSNR: PP30167 BUY連鎖 MSS確認·リテスト待ち(残8本) · 回転 2/5 OK"
}
```

### CLI

```
python msnr_gate.py                 # フル JSON
python msnr_gate.py --summary       # summary 1行のみ(監視報告に貼る用)
python msnr_gate.py --level 30166.92 --side buy   # 該当レベルの promotion のみ
```

- 終了コード: 正常 0(promotion の可否に関わらず)。入力が JSON として
  壊れている・bars が list でない等の構造異常のみ 2 +
  `{"error": "..."}` を stderr
- stdlib のみ使用。ファイル書き込みなし。ネットワークなし
- コメントは日本語(リポジトリの流儀に合わせる)

### 促進判定の合成式(これがこのツールの結論)

```
promotion[side].allowed =
      chain(level, side).state == RETEST_HELD
  AND anchorOk
  AND rotation.verdict == "OK"
  AND NOT dynamic
```

**msnr_gate はボラ予算ゲート・CT_TREND 整合・イベント封鎖を代替しない。**
それらは従来どおり monitor_publish と CLAUDE.md §3 が担う。msnr_gate は
既存ゲート群への **AND 条件の追加**である(§6.2 の文面がそれを明文化する)。

### blocker コード(この列挙以外を出力しない)

`NO_CHAIN` / `SWEEP_ONLY` / `NO_DISPLACEMENT` / `MSS_NOT_CONFIRMED` /
`RETEST_NOT_HELD` / `CHAIN_EXPIRED` / `ANCHOR_BROKEN` / `ANCHOR_CONSUMED` /
`ROTATION_REGIME` / `INSUFFICIENT_BARS` / `DYNAMIC_LEVEL`

---

## 3. 用語の機械的定義(暗黙知の置換表)

すべて **確定足のみ** で評価する。

**確定足の定義**: bar の `t + 180 ≤ epoch(priceAt)`。最終 bar が形成中
(この条件を満たさない)なら落とす。`priceAt` が無ければ `at` を使う。

パラメータは環境変数で上書き可能(既定値は本表が正):

| パラメータ | 既定値 | 環境変数 |
|---|---|---|
| TICK | 0.25 | (固定) |
| TOUCH_TOL(ゾーン半幅) | `max(2.0, 0.10 × noiseFloor)` pt | `NQX_MSNR_TOUCH_PT`(下限側の 2.0 を置換) |
| DISPLACEMENT_MULT | 1.0 | `NQX_MSNR_DISP_MULT` |
| MSS_LOOKBACK | 5 本 | `NQX_MSNR_MSS_LOOKBACK` |
| MSS_WINDOW | スイープ後 6 本以内 | `NQX_MSNR_MSS_WINDOW` |
| RETEST_WINDOW | MSS 後 10 本以内 | `NQX_MSNR_RETEST_WINDOW` |
| CHAIN_TTL | スイープから 15 本 | `NQX_MSNR_CHAIN_TTL` |
| CONSUMED_TOUCHES | 実体タッチ 3 回 | `NQX_MSNR_CONSUMED` |
| ROTATION_WINDOW / 閾値 | 10 本 / 比率 0.5(シグナル4本以上のとき) | `NQX_MSNR_ROT_WINDOW` |

### 3.1 noiseFloor(ノイズ床)

直近 12 本の確定足レンジ `(h − l)` の中央値。
**`monitor_publish.vol_gate` と同一の定義**。monitor_publish の import が
副作用なしで可能ならそこから流用してよいが、迷ったら 15 行程度の
複製 + 「vol_gate と同期必須」コメントで独立させる(monitor_publish を
編集しないという制約が優先)。確定足が 12 本未満なら
`INSUFFICIENT_BARS`(§3.8)。

### 3.2 レベルとゾーン

- レベル = `snapshot.levels[].price`。ゾーン = `[P − TOUCH_TOL, P + TOUCH_TOL]`
- **dynamic レベル**: `label` が正規表現 `/vwap/i` に一致するもの。
  freshness は `null`、連鎖評価をせず、promotion は常に
  `{allowed: false, blockers: ["DYNAMIC_LEVEL"]}`(VWAP は毎足動くため、
  静的スナップショット価格に対する連鎖評価が定義できない)

### 3.3 レベルの重複統合

価格差 **2.0pt 以内**のレベルは1つに統合する。残すのは配列で先に現れた
方の label と price。吸収したラベルは `mergedWith` に列挙する。

### 3.4 freshness(6状態の状態機械)

60 本ウィンドウの先頭から確定足を時系列に処理する。初期状態 `FRESH`。

| 事象(下側レベル=支持の例。抵抗は上下鏡像) | 定義 | 遷移 |
|---|---|---|
| ウィックテスト | `l < P` かつ `c > P` | FRESH → `WICK_TESTED` |
| 実体タッチ | `c` がゾーン内 | → `BODY_TESTED`、bodyTouches += 1 |
| 破壊 | `c < P − TOUCH_TOL` | → `BROKEN` |
| 奪還 | BROKEN 後に `c > P + TOUCH_TOL` | → `RECLAIMED` |
| 消費 | bodyTouches ≥ 3 | → `CONSUMED`(終端) |

- `anchorOk = freshness ∉ {BROKEN, CONSUMED}`
- ドクトリンの FLIPPED / MITIGATED は今回のスコープ外(実装しない)。
  operational-gates.md §9 にもスコープ外と明記すること

### 3.5 確認連鎖(BUY = 支持スイープの例。SELL は鏡像)

状態: `NONE → SWEEP_CANDIDATE → SWEEP_CONFIRMED → MSS_CONFIRMED → RETEST_HELD`
(終端 PASS)。途中で TTL 切れなら `EXPIRED`。

| 段階 | 機械的定義 |
|---|---|
| **スイープ(1本型)** | 確定足 i: `l ≤ P − 1 tick` かつ `c > P`。sweepBar = i |
| **スイープ(2本型)** | 足 i: `c < P − TOUCH_TOL`(破壊)、その後 **2本以内**の足 j: `c > P`(奪還)。sweepBar = j。2本を超えたら破壊のまま(連鎖不成立、freshness は BROKEN) |
| **displacement** | 奪還した足の実体 `|c − o| ≥ DISPLACEMENT_MULT × noiseFloor` かつ方向一致(BUY なら `c > o`)。満たせば SWEEP_CONFIRMED、満たさなければ SWEEP_CANDIDATE 止まり(blocker `NO_DISPLACEMENT`) |
| **MSS** | sweepBar の後 MSS_WINDOW 本以内の確定足: `c > max(h of 直前 MSS_LOOKBACK 本)`。参照窓は sweepBar の**前**の 5 本(5 本未満なら 3 本以上でよい。3 本未満なら MSS 判定不能 = blocker `MSS_NOT_CONFIRMED`) |
| **保持リテスト** | MSS 後 RETEST_WINDOW 本以内の確定足 j: `l ≤ P + TOUCH_TOL`(ゾーン帰還)かつ `c > P`(保持)。満たせば RETEST_HELD |
| **連鎖リセット** | MSS 後に `c < P − TOUCH_TOL` が出たら連鎖は消滅し freshness は BROKEN |
| **TTL** | sweepBar から 15 本で EXPIRED。`barsLeft` = 残本数を常に出力 |

- 各レベルにつき BUY / SELL の両連鎖を独立に走査する(レベルが現在値の
  上か下かで事前に絞らない — スイープ後は位置関係が反転しているため)
- 同一レベル・同一 side で複数の連鎖候補があるときは
  **最も進んだ状態**の1本のみ出力する

### 3.6 rotation(回転相場の機械判定 — CLAUDE.md §9 教訓の数値化)

直近 ROTATION_WINDOW+1 本の確定足で:

- **シグナル足**: 実体 `|c − o| ≥ 0.5 × noiseFloor` の足
- **否定**: シグナル足 i の次の足 i+1 が反対方向で、i の**始値を超えて**
  引ける(`c_i > o_i` なら `c_{i+1} < o_i`、逆も鏡像)= 実体の全戻し
- `signals ≥ 4` かつ `negations / signals ≥ 0.5` → verdict `ROTATION`
  (全レベルの promotion に blocker `ROTATION_REGIME`)。それ以外は `OK`

### 3.7 summary 行(1行・全角換算 60 文字以内目安)

```
MSNR: <最有望レベル+side+状態(+残本数)> · 回転 <negations>/<signals> <OK|回転>
```

- 「最有望」= 全レベル・全 side の中で状態が最も進んだ連鎖。
  該当なしなら `MSNR: 連鎖なし · 回転 2/5 OK`
- RETEST_HELD がある場合は `✔昇格可` を付ける(ただし anchorOk と
  回転 OK も満たす場合のみ)

### 3.8 データ不足時の挙動(fail-closed)

確定足 12 本未満・levels 空・全レベル dynamic のときは、判定を
でっち上げず **promotion 全部 false + blocker `INSUFFICIENT_BARS`**
(levels 空なら levels: [] のまま)+ summary にその旨を書く。
ボラゲート(fail-open)とは逆であることに注意 —
**連鎖は「証明できたら許可」なので、証明不能 = 不許可が正しい。**

---

## 4. アルゴリズム(擬似コード)

```
main:
  raw = stdin.buffer.read().decode("utf-8-sig")
  bundle = json.loads(raw)                    # 失敗 → exit 2
  bars   = closed_bars(bundle)                # §3 確定足定義
  nf     = noise_floor(bars)                  # 12本中央値。不足→fail-closed
  tol    = max(TOUCH_PT, 0.10 * nf)
  rot    = rotation(bars, nf)                 # §3.6
  levels = dedupe(bundle.snapshot.levels)     # §3.3
  for lv in levels:
      lv.dynamic  = regex vwap
      if not lv.dynamic:
          lv.freshness, lv.bodyTouches = freshness_scan(bars, lv.price, tol)
          lv.chains = [best_chain(bars, lv.price, tol, nf, side)
                       for side in (BUY, SELL) if chain exists]
      lv.promotion = compose(lv, rot)         # §2 合成式
  print(json / summary / level-filtered view)
```

実装量の目安: 250〜300 行。1 関数 1 定義(freshness_scan / find_chains /
rotation / dedupe / summarize)。**判定関数はすべて純粋関数**にし、テストから
bars 配列を直接渡せる形にする。

---

## 5. 実データでの検証(必須 — 合成データだけで済ませない)

`.secrets/` に 8/17〜8/18 の実サイクル bundle が **165 ファイル**残っている。

1. `.secrets/monitor_cycle_0249.json`(8/18 02:49)を食わせて例外なく
   走ること・`closedBars ≥ 55`・noiseFloor が 20〜26pt に入ることを確認
2. **8/17 の敗戦再現**: 00:05 前後と 00:30 前後のサイクルファイル
   (Trade 1: Entry 30,238 / Trade 2: Entry 30,268)を探し、その時点の
   bundle で該当方向の promotion が **false** になり、blocker に連鎖系
   コード(`SWEEP_ONLY` / `MSS_NOT_CONFIRMED` / `NO_CHAIN` 等)が出ることを
   確認する。**これがこの実装の存在理由の実証。** 該当時刻のファイルが
   無い場合はその旨を報告し、§7 の合成フィクスチャで代替する
3. 検証結果(どのファイルで何が出たか)を HANDOFF.md の完了記録に
   数行で残す

---

## 6. ドキュメント編集(挿入文はこのまま使う)

### 6.1 CLAUDE.md §2 — 緩衝式の優先順位(「★ SL は金額から逆算しない」の直後に挿入)

```markdown
### ★ 緩衝式が2系統あるときは大きい方(2026-08-18 追記)

ライブ式(§1: 3分は直近3本の平均ヒゲ / 15分構造は CT_ATR)と NQX 検証器の
WG-H1(`max(0.50U, 2pt)`、U = TR14 平均)が食い違う場合は**大きい方を採用**する。
小さい方に合わせて SL を狭めることは、検証器合格を理由にした予算オーバーの
逆(=構造否定前の刈られ)を招く。
```

### 6.2 CLAUDE.md §3 — 昇格条件への追加(「必須」リストの末尾に挿入)

```markdown
- **MSNR 確認連鎖**(反応型 = 戻り売り・反発・15分レベルでの3分反応は必須):
  `PYTHONUTF8=1 python msnr_gate.py < bundle` の該当レベル行が
  `RETEST_HELD` / `anchorOk` / 回転 OK の3つを満たすこと。
  ブレイクだけ・リテストだけは SWEEP/MSS CANDIDATE であり武装しない
  (8/17 の2敗はこの状態で武装したもの)。ゲートの定義はスキル
  operational-gates.md §9 が正
```

### 6.3 CLAUDE.md §7 — /loop 定型文への追加(ボラ予算ゲートの段落の直後に挿入)

```
【毎サイクル】MSNR 連鎖ゲート
- bundle を書いた後 `PYTHONUTF8=1 python msnr_gate.py --summary < .secrets/monitor_cycle_HHMM.json`
  を実行し、出力1行をそのまま報告に載せる
- シナリオを A/X に昇格させる前に、該当レベル・該当方向の promotion が
  allowed=true であることを確認する(false なら blocker を watching に書いて W 止まり)
```

### 6.4 TRADING_CONTEXT.md §6 — 注記(§6 の末尾に挿入)

```markdown
### MSNR フルドクトリンと $60 枠の相性(2026-08-18 注記)

NQX パケットの MSNR ドクトリン(TP1 ≥ 1.80R)は MAX_RISK $200 時代
(8/3〜8/6)の設計。SL 上限 25pt では TP1 に 45pt 以上が必要で、
validator-clean な MSNR シナリオが成立するのは実質ノイズ床 15pt 未満の
薄い時間帯だけになる。これは異常ではなく仕様。NY 時間帯に MSNR シナリオが
出ないことをゲートの故障と誤診しない。
```

### 6.5 operational-gates.md — §9 新設(§8 ボラ予算ゲートの後)

タイトル: `## 9. MSNR confirmation chain — reaction setups arm only on a completed chain`

内容は本書 §2(合成式・blocker 列挙)と §3(全定義表・パラメータ表)を
**数値を一切変えずに**転記する。加えて:

- 冒頭に「8/17 の2敗は SWEEP/MSS CANDIDATE 状態での武装。完成した連鎖
  だけが反応型の武装資格を持つ」の1段落
- 末尾に「FLIPPED / MITIGATED は未実装(スコープ外)」
- **編集後、project コピー
  (`project/claude-skills/mnq-nightwatch-nqx/references/operational-gates.md`)
  へファイルごと複製し、ハッシュ一致を確認する**(§8 検収基準)

### 6.6 preview 2ファイル — Stand down 行の拡張

対象(完全一致で1行ずつ、**両方**):

- `preview/app-canonical.html` 5596 行付近
- `preview/nq-nightwatch-nqx-final_1.html` 4375 行付近

現行:
```
'- Stand down in low-edge conditions: value-area center, conflicting evidence, consumed level, first roadblock too close, HTF/LTF conflict, or major data risk.',
```
置換後(1行のまま):
```
'- Stand down in low-edge conditions: value-area center, conflicting evidence, consumed level, first roadblock too close, HTF/LTF conflict, major data risk, a noise-floor-to-stop-budget ratio above 0.60 (median 12-bar closed range vs account stop budget), or a rotation regime where at least half of the last 10 closed-bar signals were fully retraced by the next bar.',
```

---

## 7. テスト(`tests/test_msnr_gate.py`)

`run_all.py` はファイル名 `test_*.py` で自動発見し **1ファイル=1プロセス**で
走らせる。既存テストと同じ**自走式**(import 時に実行して
`sys.exit(failures)`)で書く。`_hermetic.py` の流儀を1つ読んでから書くこと。
bars はヘルパー `mk_bars(spec)` で合成する(t は 180 秒刻み、価格は
0.25 整合)。**ネットワーク・ファイル書き込みなし**。

必須ケース(最低この12本。名前もこのまま使う):

| # | テスト名 | 期待 |
|---|---|---|
| 1 | `test_single_bar_sweep_full_chain_buy` | スイープ→displacement→MSS→リテスト保持 → RETEST_HELD / allowed=true |
| 2 | `test_two_bar_sweep_reclaim` | 破壊→2本以内奪還で連鎖成立 |
| 3 | `test_reclaim_too_late_is_broken` | 奪還が3本後 → 連鎖なし・freshness=BROKEN |
| 4 | `test_break_retest_without_sweep_blocked` | **8/17 型**: 実体ブレイク→即リテストのみ → allowed=false、blocker に連鎖系コード |
| 5 | `test_no_displacement_stays_candidate` | 奪還足の実体 < 1.0×NF → SWEEP_CANDIDATE / `NO_DISPLACEMENT` |
| 6 | `test_rotation_blocks_all` | 全戻し交互 bars → verdict=ROTATION、全レベル `ROTATION_REGIME` |
| 7 | `test_consumed_level_not_anchor` | 実体タッチ3回 → CONSUMED / `ANCHOR_CONSUMED` |
| 8 | `test_chain_ttl_expires` | スイープ後 15 本でリテスト来ず → EXPIRED / `CHAIN_EXPIRED` |
| 9 | `test_vwap_level_excluded` | label "VWAP Lower Band" → `DYNAMIC_LEVEL` |
| 10 | `test_forming_bar_dropped` | 最終 bar の t+180 > priceAt → closedBars が 1 少ない |
| 11 | `test_insufficient_bars_fail_closed` | 確定足 11 本 → 全 false / `INSUFFICIENT_BARS` |
| 12 | `test_bom_and_longkey_input` | BOM 付き入力+ `high/low` キーで正常動作 |

追加で SELL 鏡像を最低1本(#1 の鏡像)。§5-2 の実データ検証は
テストではなく手動検証として実施し結果を HANDOFF に記す
(実データを fixtures に**コピーしない** — .secrets を汚さない・依存しない)。

---

## 8. 検収基準(全部 YES で完了。1つでも NO なら完了と報告しない)

```
[ ] python tests/run_all.py → ALL PASS(10 ファイル: 既存9 + test_msnr_gate)
[ ] test_market_feed.py / test_oneclick.py に変更を加えていない
[ ] .secrets/monitor_cycle_0249.json を通して例外なし・summary 1行が出る
[ ] 8/17 実データ(見つかった場合)で該当方向 allowed=false を確認し
    HANDOFF に記録した
[ ] msnr_gate.py は stdlib のみ・ファイル書き込みなし・exit 0/2 のみ
[ ] blocker コードが §2 の列挙以外に存在しない(grep で確認)
[ ] CLAUDE.md に 6.1 / 6.2 / 6.3 の3箇所が挿入されている
[ ] TRADING_CONTEXT.md に 6.4 が挿入されている
[ ] operational-gates.md §9 が両コピーに存在しハッシュ一致
    (PowerShell: Get-FileHash で比較)
[ ] preview 2ファイルの Stand down 行が両方置換済み(grep "fully retraced" が2件)
[ ] HANDOFF.md 先頭に完了記録(何を入れたか・実データ検証の結果・
    パラメータ既定値表へのポインタ)
[ ] order.py / telegram_bot.py / nqx_state.py / monitor_publish.py は無変更
```

---

## 9. 既知の罠(この作業で実際に踏まれたことがあるものだけ)

1. **Write ツールの JSON は BOM 付き**。msnr_gate は `utf-8-sig` で読む設計に
   したので入力側は安全だが、テストフィクスチャを Write で作って別経路で
   読む場合は同じ罠がある
2. **PowerShell の `Get-Content | python` は使えない**(BOM で拒否)。
   検証はすべて Bash ツール + `PYTHONUTF8=1 PYTHONIOENCODING=utf-8`
3. **Bash コンソールへの日本語 print は文字化けする**(表示のみの問題)。
   診断出力は ASCII にするか、化けても慌てない
4. **`open(p,'w')` を読み込みと同じ式に混ぜない**(書き込み側が先に評価され
   ファイルを空にした実害 2026-08-14)
5. **スキルは2コピー**(`~/.claude/skills/` が実行される正、`project/` は
   引継ぎ用)。片方だけ直すと次のセッションで乖離する
6. **preview の HTML は2ファイルに同文が重複**。片方だけ直すと分岐する
7. テストは **run_all.py 経由でのみ**信用できる(1プロセス集約 import は
   2026-08-15 に誤検知を起こした)
8. `pane_list` / チャート操作系はこの作業では一切不要。TradingView MCP を
   呼ぶ必要はない

---

## 10. 実装順序(推奨)

```
1. 本書と operational-gates.md §1〜§8、monitor_publish.py の vol_gate、
   tests/_hermetic.py と既存テスト1本を読む(30分ぶんの前提知識)
2. msnr_gate.py 実装(§3 の表を関数に1対1で写す)
3. tests/test_msnr_gate.py → run_all.py ALL PASS まで
4. 実データ検証(§5)
5. ドキュメント6点(§6)+ スキル同期
6. 検収チェックリスト(§8)を1行ずつ実測で埋めて HANDOFF に記録
```

判断に迷う点が出たら、**本書の定義を勝手に変更せず**、迷った内容と
採った仮定を HANDOFF の完了記録に明記すること(黙って解釈を変えるのが
一番高くつく。8/17 の教訓と同型)。
