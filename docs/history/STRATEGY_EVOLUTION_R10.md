# R10 — ICT 戦略の取り込み

> **Historical record — superseded for operation.** R10's advisory-only design
> is preserved to explain the transition, but it is not an active rule.
> Use `docs/R12_ICT_EXECUTION_CONTRACT.md` and `CLAUDE.md` for current ICT,
> CVD, split-management, event, risk, and session behavior.

出典: `ICT Core Content Months 1 to 12 by GatieTrades -notes-.pdf`(213ページ)。
既存の NQX 戦略(MSNR 連鎖 / ボラ予算 / SwingArm / VP・VWAP)に対して、
**重複しない部分だけ**を ADVISORY として実装した。

実装は `msnr_gate.py` のみ。`monitor_publish.py` / `order.py` / `telegram_bot.py` /
`nqx_state.py` / Mini App / Worker は**無変更**。
`python tests/run_all.py` → **ALL PASS(11ファイル・PASS 75)** +
`tests/test_msnr_gate.py` に R10 セクション(**53検証** = ICT 層31 + Index SMT 22)を追加。

---

## 1. 対応表 — ICT の何が既にあり、何が無かったか

| ICT 概念 | 出典 | 現行戦略での対応 | R10 の扱い |
|---|---|---|---|
| Turtle Soup(流動性狩り→反転) | M4 EP6 | **MSNR SWEEP 連鎖**(sweep→MSS→retest) | 実装済み・再実装しない |
| Breaker / MSS | M4 EP5 | **MSNR FLIP 連鎖**(break→acceptance→hold) | 実装済み・再実装しない |
| Mitigation Block | M4 EP4 | FLIP 連鎖の一形態 | 実装済み |
| Displacement | M4 EP3 | `dispBody` / `DISPLACEMENT_MULT` | 実装済み |
| Consolidation / 回転相場の回避 | M1 EP1 | `rotation.verdict` + 8/17 の教訓 | 実装済み |
| Liquidity Pool(旧高安) | M4 EP11 | `snapshot.levels`(PDH/Asia/VAH…) | 部分的 → **R10 で拡張** |
| **Premium / Discount / EQ** | **M1 EP4/5** | **無し** | **★ R10 で新設** |
| **Draw on Liquidity / LRLR·HRLR** | **M1 EP7** | `rrPotential`(距離のみ・抵抗を数えない) | **★ R10 で新設** |
| **Killzone / AM·PM Trend** | **M10 EP11/12** | 無し(時間帯の概念が無かった) | **★ R10 で新設** |
| **FVG(未充填)** | **M4 EP12** | 無し | **★ R10 で新設** |
| **Index SMT**(NQ/ES/YM 乖離) | **M10 EP11** | **無し** | **★ R10 で新設(§5)** |
| CBDR / Asian Range STD | M8 EP3/4 | 無し | 未実装(§6) |
| ADR 投影・1.27/1.62 | M12 EP4 Step9 | 無し | 未実装(§6) |
| COT / Open Interest | M10 EP1 / M12 | 無し | 対象外(日足以上の話) |

**結論**: MSNR ゲートは ICT の Turtle Soup / Breaker をほぼそのまま機械化したもので、
**エントリー成立の判定は既に ICT 的**だった。欠けていたのは全て
「**どちら側で・どこへ向かって・いつ**取るか」という**文脈**の層。
これは 2026-08-19 の反省 S1〜S5 とほぼ同じ結論に、別経路から到達している。

---

## 2. 実装(ADVISORY・`promotion` に一切影響しない)

`msnr_gate.py` に純粋関数として追加。**`evaluate()` の最後、promotion 確定後**に
計算する(R8 の `rrPotential` と同じ位置・同じ思想)。

| 関数 | 返すもの |
|---|---|
| `ict_session(at)` | JST→ET 変換とキルゾーン/指数プロファイル |
| `dealing_range(bars, price)` | 高安・EQ・premium/discount・OTE 帯 |
| `swing_liquidity(bars, price, tol)` | 未回収の旧高値(BSL)/旧安値(SSL) |
| `draw_on_liquidity(...)` | 次の流動性プールと **LRLR / HRLR** |
| `fvg_scan(bars, price, tol)` | 未充填 FVG(直近2件) |
| `index_smt(bars, peers, at)` | **Index SMT**(§5) |
| `ict_context(...)` | 上記をまとめて `result["ict"]` |
| `ict_side_check(ict, side)` | side 別の注意点(**参考値**) |

出力経路:
- `--summary` の末尾に `· ICT: ディスカウント(46%) · DOL上 29,614(9pt·LRLR) · ラストアワー`
- `--card` の `advisory.ict`(縮小順は **rrPotential → ict → advisory 全体**)

### 2.1 キルゾーン定義(指数先物・ET / JST)

Month 10 EP11/12 と Month 12 EP4 Step3 に基づく。**JST = ET + 13時間**(夏時間)。

| ET | JST | 窓 | ICT の見方 |
|---|---|---|---|
| 02:00–05:00 | 15:00–18:00 | LONDON_KZ | ロンドンKZ |
| 07:00–09:30 | 20:00–22:30 | NY_AM_KZ | NY 寄り前の仕込み |
| **09:30–10:30** | **22:30–23:30** | **TRUE_DAY_HL** | **真の日の高安がここで作られやすい** |
| 10:30–12:00 | 23:30–01:00 | AM_TREND_LATE | AM トレンド後半 |
| 12:00–13:00 | 01:00–02:00 | LUNCH | **浅い戻りの持ち合い。新規を探さない** |
| 13:00–15:00 | 02:00–04:00 | PM_TREND | 14:00ET 前後から動く |
| 15:00–16:00 | 04:00–05:00 | LAST_HOUR | 引けにかけての最終スイング |

冬時間は `ICT_ET_OFFSET_H` を `-5` に変える(コード内に明記済み)。

### 2.2 不変条件(テストで固定)

- **実データ 400 バンドル**で `allowed` / `grade` / `blockers` の差分 **0件**
- カード最大サイズ **1,172 バイト**(上限 4096)
- 壊れた `at` / `price` 欠落でも例外を出さず `None` を返す

---

## 3. 実測 — ICT を昨夜までのデータに当てるとどうなるか

`.secrets/monitor_cycle_*.json` 全400件、`allowed=true` の side-level **143件**に対して:

| ICT の注意 | 件数 | 割合 |
|---|---|---|
| `PREMIUM_DISCOUNT_WRONG_SIDE` | 104 | **73%** |
| `HIGH_RESISTANCE_RUN`(HRLR) | 70 | 49% |
| `OUTSIDE_KILLZONE` | 12 | 8% |
| **いずれかに該当** | **137** | **96%** |
| **3つとも通過** | **6** | **4%** |

### ★ この 96% は「ICT を強制ゲートにできない」という意味ではない

**73% が「逆側で売買している」と出るのは、システムの構造的な性質を測っている。**
MSNR は**レベルでの反応を取る型**なので、下落の脚の終わり(= レンジ下端 =
discount)で売り連鎖が完成する。ICT は同じ弱気でも「**戻りが premium に入るまで
待って売れ**」と言う。つまりこの 73% は、

> 2026-08-19 の反省 **S1(確認レイテンシ ≒ 回転の半周期)/ S2(構造固定 Entry の限界)**

を別の言葉で数値化したものであり、**ノイズではなく診断結果**である。
したがって R10 の使い方は「**拒否**」ではなく「**Entry の置き直し**」が本筋(§4.2)。

### 3.1 2026-08-21 Apex 初戦(−$115)の検死 — ICT で完全に説明できる

| 項目 | 実測 | ICT の読み |
|---|---|---|
| Entry / SL | SELL 29,327 / 29,355.75 | — |
| ディーリングレンジ | 29,240.25 – 29,470.25 | — |
| **EQ(equilibrium)** | **29,355.25** | **SL 29,355.75 は EQ の 0.5pt 上** |
| 最大逆行 | **29,356.50** | **EQ をわずかに超えて反転**(その後 29,309.75 まで下落) |
| Entry の位置 | **DISCOUNT 37〜39%** | **売ってはいけない側**(ICT: sell premium) |
| ICT の OTE 売り帯 | **29,383 – 29,422** | 本来のエントリー帯は **56〜95pt 上** |
| DOL(下の流動性) | 29,325(**残り4pt**) | 走路がほぼ無い(CLAUDE.md §VP到達余地と同じ結論) |

**ストップを「ディーリングレンジの EQ」に置いていた。** ICT は
「価格は EQ に吸い寄せられてから拡大する」と言う — つまり
**最も引き寄せられやすい1点に SL を置いた**ことになる。
方向(下)は正しく、価格は実際に 29,309.75 まで落ちた。
`PREMIUM_DISCOUNT_WRONG_SIDE` と `DOL 残り4pt` の2つは、
**この1敗を事前に説明できていた**。

### 3.2 時間帯の分布(S5 の検証)

`allowed` が出た143件の時間帯:

| 窓 | 件数 |
|---|---|
| NY_AM_KZ(20:00–22:30 JST) | 64 |
| TRUE_DAY_HL(22:30–23:30) | 28 |
| PM_TREND(02:00–04:00) | 23 |
| LUNCH(01:00–02:00) | **11** |
| LAST_HOUR(04:00–05:00) | 8 |
| AM_TREND_LATE / LONDON / 時間外 | 9 |

**ランチ帯(ICT が「新規を探さない」と明言する時間)で 11件**の武装可が出ていた。
S5「02:00 以降の期待値が低い」は、より正確には
**「01:00–02:00 JST のランチ帯を避ける」**が第一候補。

---

## 4. 運用への取り込み方(段階)

### 4.1 Phase 1 — ADVISORY(**今回実装済み・即日から観測開始**)

毎サイクルの報告と Telegram バナー詳細に ICT の1行が載る。
**武装可否には一切影響しない。** 3セッション分の観測を HANDOFF に貯める
(R4 §1.3 / R8 §4 と同じ昇格手順)。

観測すべき問い:
1. `PREMIUM_DISCOUNT_WRONG_SIDE` が付いた武装と付かない武装で、実現Rに差が出るか
2. `HRLR` の武装は本当に伸びないか(rb1R との相関)
3. ランチ帯の武装は他の時間帯より成績が悪いか

### 4.2 Phase 2 — Entry の置き直し(**S2 への回答・要ユーザー承認**)

ICT の本来の使い方。連鎖が完成しても Entry が discount 側なら、
**そのトレードを捨てるのではなく、Entry を OTE 帯に移して指値で待つ**。

```
売りの連鎖が完成 かつ 現在値が DISCOUNT
  → Entry を oteSell 帯(安値からの 62-79% 戻し)へ移す
  → 到達距離 |現在値−E| が 1.0R を超えるなら W のまま置く(CLAUDE.md §2 のまま)
  → 帯に来なければ約定しない = 論拠が試されてから建玉になる
```

8/21 の例では Entry 29,327 → **29,383–29,422** に移る。
到達距離が 1.0R を超えるので **W 止まり**になり、あの1敗は発生しなかった。
ただし **その戻りが来なかった可能性**も同じだけあり、
「取れたはず」ではなく「**取らずに済んだ**」が正しい評価。

**⛔ 未承認。** これは §2 の「Entry は構造に置く」の変更にあたるので、
Phase 1 の観測を経てユーザーが決める。

### 4.3 Phase 3 — 昇格候補(**観測後に判断**)

| 候補 | 提案する扱い | 理由 |
|---|---|---|
| **LUNCH 帯の武装停止** | ENFORCED 化の最有力 | ICT が明言・実データで11件・機械判定が単純 |
| **HRLR の A+ 限定** | ボラゲートと同型の降格 | 走路に抵抗が2つ以上ある型を等級で絞る |
| Premium/Discount | **ENFORCED にしない** | 73% を止めると系が動かない。Entry 置き直し(4.2)で使う |
| DOL 残距離 < 0.25×NF | 既存 `vpHeadroom` と統合 | CLAUDE.md §VP到達余地の一般化 |

---

## 5. Index SMT(実装済み・ADVISORY)

ICT Month 10 EP11/12。NQ / ES / YM の相対的な高安を比べ、**1つだけが更新しない**
状態を検出する。**価格だけで計算できる**ので、2026-08-19 の `data_get_study_values`
キャッシュ事故(27分間 CVD が読めなかった)と同じ事故では止まらない —
これが本システムに入れる第一の理由。

| 関数 | 役割 |
|---|---|
| `index_smt(bars, peers, at)` | 本体。`available` / `low` / `high` / `bias` / `window` |
| `_last_two_swings(bars, kind)` | fractal(左右2本)で直近2つのスイング点。最小間隔3本 |
| `_peer_extreme(rows, t, kind)` | peer 側の対応点(±2本の窓での最高/最安) |
| `_norm_peer_bars(rows)` | `t/h/l` と `time/high/low` の両方を受ける。壊れた行は捨てる |

判定:
- **安値で乖離** → `BULLISH`(更新しなかった側が強い)
- **高値で乖離** → `BEARISH`
- 両側で乖離 → `bias=None`(相殺)。ICT の「明らかでなければ無い」に合わせる
- 比較窓: **AM 05:00-09:30 ET(18:00-22:30 JST)/ PM 12:00-15:00 ET(01:00-04:00 JST)**

`ict_side_check` は bias と逆 side に **`SMT_AGAINST`** を付ける(ADVISORY)。

### 5.1 データ経路 — ここが唯一の制約

**`data_get_ohlcv` / `quote_get` の `symbol` 引数は無視される。**
2026-08-21 実測: `symbol=CME_MINI:ES1!` を渡すと MNQ の 29,270 台が返った。
`tab_new` も **success を返すがタブは増えない**(同日実測・2枚のまま)。

→ **既存の SMT 用タブで銘柄を切り替えるしかない。**
ユーザー承認のもと **タブ index 1(chart_id `2RiggmK6`)を SMT 用に確定**。
手順と罠は CLAUDE.md §3「Index SMT」。分析用タブ0(2ペイン MNQ)には触れない。

**2026-08-21 の実走で判明した3点:**

| 事象 | 対処 |
|---|---|
| `chart_set_symbol` 直後に**フォーカスがタブ0へ飛ぶ** | `tab_switch index=1` をやり直してから確認・取得 |
| `chart_ready: false` で**1回目の取得が失敗する** | `chart_get_state` を挟んで再試行 |
| **YM1! はデータが出ない**(チャートに赤✗・取得も失敗) | CBOT の配信権限が無いと思われる。**SMT は ES 単独で成立する** |

**実走結果**(MNQ vs ES / 2026-08-21 04:33 JST):

```
SMT ES: 乖離なし
安値側: NQ=HL / ES=HL / 乖離=False
高値側: NQ=LH / ES=LH / 乖離=False
```

2指数が同じ動き = 乖離なし。**タブ1は MNQ1!/3分/指標5つに完全復帰を確認**、
タブ0(pane0 MNQ 15分 / pane1 MNQ 3分)も無傷を確認した。

`snapshot.peers` が無い場合は `available=False` / `SMT: 未取得` で、
**中立**として扱う(弱気でも強気でもない)。

### 5.2 不変条件

- `peers` を足しても **promotion は不変**(テストで固定)
- peers 無し / 壊れた行 / 長いキー名 のいずれでも例外を出さない
- 実データ400件(peers 未供給)で `available=False`・カード最大 1,172 バイト

---

## 6. 未実装(次の R で検討)

| 項目 | 必要なもの | 価値 |
|---|---|---|
| CBDR / Asian Range STD | 14:00–20:00ET / アジア時間のレンジ計算 | 日中の高安投影(現行は目標が構造レベル頼み) |
| ADR 投影・1.27/1.62 | 5日 ADR | ランナー最終目標の根拠(§2 TP到達性の補強) |
| Power of 3(始値からの拡大) | 日足 O/H/L/C | 日足バイアスの定型化 |

---

## 7. 変更ファイル

| ファイル | 変更 |
|---|---|
| `msnr_gate.py` | R10 ブロック追加(+約210行)/ `evaluate` に `ict` / `_advisory_tail` / `build_card` / `_shrink_card` にフック。**OTE の売買が逆だったバグをテストが検出し修正** |
| `tests/test_msnr_gate.py` | R10 セクション(53検証)。中核は「ICT / SMT を足しても promotion が変わらない」 |
| `CLAUDE.md` | §3 に ICT ADVISORY の節 / §7 のループ指示に報告1行 |
| `msnr_gate.py.bak-r10` | 変更前のバックアップ(回帰比較に使用) |

**無変更**: `monitor_publish.py` / `order.py` / `dayguard.py` / `nqx_state.py` /
`telegram_bot.py` / Mini App / Worker。
