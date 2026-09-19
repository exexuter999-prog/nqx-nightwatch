# R122 §1 — 4 主戦略の知識仕様表(現行コードからの棚卸し)

2026-09-19 JST。固定した版:

| 対象 | 値 |
|---|---|
| コード版 | git `9db4e5980e7818a02bd3215922f66518efb8201c`(master、作業開始時) |
| 設定版 | `execution_contract.json` `version=R22-EXECUTION-CONTRACT-1` / `ictStdv=R121-ICT-STDV-1`(mode=LIVE, targets=LIVE, participation=OFF) / `limitGate=R119-LIMIT-GATE-1`(gapCap LIVE 1.5, targetPassed LIVE) / `modelGate.disabled=[]`(TURTLE 復活) / `entryDepth=LIVE` / `stopLogic.vwapClearance=LIVE, poolClearance=LIVE, sweepGate=SHADOW, flipOrigin=OFF` |
| 入力期間 | `.secrets/monitor_cycle_*.json` 1,359 本(2026-09-08 〜 2026-09-18 UTC) |
| Python | 3.12.10 |

**この表は現行コードが実際に何を要求しているかの棚卸しで、ICT/Alchemist 等の理論の完全な再現ではない。**
空欄は推測や動画用語で埋めず「未実装」「入力なし」と書く。埋めるべきものは §12 で R122 の実装対象へ落とした。

---

## 1. VP80_REVERSION

| 列 | 内容 |
|---|---|
| 適用局面 | バリューエリア(VAH/VAL)の外で受容に失敗し、VA 内へ戻った局面。`_scan_vp` が「外側終値 ≥ `vp_outside`(既定 2)本 → 内側終値」を確認した後だけ |
| 必要な証拠 | `snapshot.levels` に **VAH と VAL の両方**(`VAH_RE`/`VAL_RE` の正規表現一致)かつ `vah > val`。無ければ経路ごと成立しない(`vp_path` が空を返す) |
| 観測順序 | 外側で `vp_outside` 本の終値 → 内側終値で `VP_RE_ENTRY` → `flip_accept`(既定 3)本以内にもう一度内側終値で `VP_ACCEPTED`。途中で外側終値が出たらそのエピソードは消滅 |
| アンカー | 失敗した側の VA エッジ(SELL=VAH / BUY=VAL)。**建値は直近確定足の終値**でエッジではない(`candidate_vp80`) |
| 参加トリガー | 建値 = 直近終値なので、実運用では**ほぼ常に成行**(`entryMode=AGGRESSIVE_ACCEPT_CLOSE`)。R86 `entryDepth` が LIVE のとき建値と SL を 0.25N 平行移動する |
| 否定条件 | エピソード起点(`reEntryBarT`)以降の VA 外極値 ± `model_stop_buffer(nf)`。建値が既に目標側(反対エッジ)を抜けていれば候補自体を作らない |
| 寿命 | `chain_ttl`(既定 15 本 = 45 分)。`_finalize_path` が `barsLeft<=0` で `EXPIRED` |
| 利確根拠 | 反対側 VA エッジを `VA_TARGET` として `target_pool` へ渡し、既存の最小 R(1.5)・重複除去・runner 選定を通す。R121 STDV が LIVE なので投影も候補に入る |
| 出典 | Market Profile の VA 受容失敗。**「80」は実測勝率ではない**(名前だけ) |
| 実装箇所 | `msnr_gate.vp_path` / `_scan_vp` / `candidate_vp80` / `entry_depth.annotate` |
| 未検証部分 | (a) トレンド継続中の一時的な VA 復帰と本物の回帰の弁別が**コードに無い**(受容失敗の回数しか見ない)。(b) 上位足の方向との関係は `HTF_ALIGNED/CONFLICT` の ±1 点だけ。(c) 名前の 80% に対応する実測なし |

## 2. TURTLE_SOUP_REVERSAL

| 列 | 内容 |
|---|---|
| 適用局面 | 静的レベルの外へ突き抜けて奪還した足(`_is_reclaim_bar`)から始まる反転 |
| 必要な証拠 | (1) 奪還足の実体 ≥ `disp_mult`(既定 1.0)× ノイズ床 かつ方向一致、(2) `mss_lookback`(5)本の参照窓が 3 本以上、(3) `mss_window`(6)本以内に参照極値を**終値で**破る MSS、(4) `retest_window`(10)本以内にレベル価格そのものへ戻って終値で保持 |
| 観測順序 | `SWEEP_CANDIDATE → SWEEP_CONFIRMED → MSS_CONFIRMED → RETEST_HELD`。順序は `scan_chain` が強制し、途中でレベルの反対側に終値で抜けたら連鎖ごと `None`(リセット) |
| アンカー | レベル価格(`level.price`、`dedupe` 後)。SL は奪還足の極値、R88 `NQX_TURTLE_SWEEP_STOP=LIVE` のときは掃引全体の極値 |
| 参加トリガー | 2 型。(a) **リテスト保持型** = `RETEST_HELD` で建値 = レベル価格(成行/指値は現値次第)。(b) **先回り指値型** = `MSS_CONFIRMED` 時点でレベルへ置く指値(`restingLimit`。`_resting_limit_ok` と R119 `gapCap` が距離を見る) |
| 否定条件 | レベルの反対側での終値(連鎖リセット)。SL は掃引極値 ± 緩衝 |
| 寿命 | 未完成は `chain_ttl` 15 本、完成後は `complete_ttl` 10 本(`barsSinceRetest`) |
| 利確根拠 | `model_targets`(方向側レベル・最小 R 1.5・runner は TP1 から `LADDER_MIN_SEP_R` 0.5R 以上離れた tier ≤3 のレベル)+ R121 STDV 投影 |
| 出典 | Street Smarts(Raschke/Connors)の Turtle Soup。R48 の `CLASSIC_TS` タグは原典条件(新 20 日極値・旧極値が 4 セッション以上古い)を**記録専用**で判定する |
| 実装箇所 | `msnr_gate.scan_chain` / `best_chain` / `build_candidates`(`LIMIT_ENTRY_STATES`)/ `_resting_limit_ok` / `limit_gate_audit` |
| 未検証部分 | (a) 継続ブレイクとの弁別は「奪還足が出たか」だけで、上位足がどちら向きかは ±1 点。(b) 先回り型と確認型の費用込み比較は n=2 で未成立(`docs/R89_MODEL_GATE.md` / `docs/R121_ICT_STDV.md` §11)。(c) 原典の 20 日条件は採点に効いていない |

## 3. BREAKER_CONTINUATION

| 列 | 内容 |
|---|---|
| 適用局面 | 支持/抵抗を実体で抜けた後、外側で受容し、戻って保持した継続(RBS/SBR) |
| 必要な証拠 | (1) ブレイク足が `tol` を超えて実体で抜け、かつ**その前**が反対側に居たこと(`_origin_side`)、(2) `flip_accept`(3)本以内にゾーン外の終値 = 受容、(3) `retest_window`(10)本以内にレベル価格へ戻って保持終値 |
| 観測順序 | `FLIP_BREAK → FLIP_ACCEPTED → FLIP_HELD`。受容前の保持は読まない。奪還終値(反対側)が出たら連鎖ごと `None` |
| アンカー | レベル価格。SL はブレイク足の極値 ± 緩衝(R90 `flipOrigin=LIVE` なら上昇/下降の起点まで下げる。**現在 OFF**) |
| 参加トリガー | `FLIP_HELD`(= `COMPLETE_STATES`)だけ。**先回り指値型は無い**(`LIMIT_ENTRY_STATES` は SWEEP 型のみ)。建値がレベルなので現値次第で成行/指値 |
| 否定条件 | レベルの反対側での終値。SL はブレイク足極値 |
| 寿命 | `chain_ttl` 15 本 / 完成後 `complete_ttl` 10 本(`barsSinceHold`) |
| 利確根拠 | `model_targets` + STDV。継続方向のレベル階段 |
| 出典 | ICT の breaker/OB 継続。**実装は FLIP チェーン(受容+リテスト保持)であって、ICT の breaker block 定義そのものではない** |
| 実装箇所 | `msnr_gate.scan_flip` / `best_flip` / `_is_flip_break` / `stop_logic.flip_origin_index` |
| 未検証部分 | (a) **押しが浅くてレベルまで戻らない継続に参加する経路が無い**(戻らなければ `FLIP_HELD` にならず、候補が生成されない)。(b) 遠い建値の指値が R119 前は距離無制限だった(R119 gapCap で 1.5R に制限済み)。(c) 上位足の仮説が生きているかは ±1 点 |

## 4. OTE_FVG_PULLBACK

| 列 | 内容 |
|---|---|
| 適用局面 | 明示アンカーのレンジに対する 0.62〜0.79 押し戻り(OTE)で、到達前に構造を保った FVG が残っている局面 |
| 必要な証拠 | (1) `rangeAnchor` が `valid` で `favors[side]`(= 現値が discount/premium の正しい側)、(2) 同方向の `eligible` かつ `preArrivalStructure=INTACT` な FVG、(3) レンジ幅 ≥ max(40pt, 6×nf)、(4) 同じレベルに生きた SWEEP/FLIP チェーンがある(候補は `_candidate_for_chain` にチェーンを渡して作る) |
| 観測順序 | レンジ確定(`resolve_range_anchor`: 明示 `rangeAnchor` → `DERIVED_RANGE_PAIRS` の順)→ FVG 生成 → 価格が OTE 帯へ戻る |
| アンカー | `rangeAnchor`(`rangeTf/rangeStart/rangeEnd/anchorType/freshness/high/low`)。**ローリング 3 分高安は使わない** |
| 参加トリガー | 建値 = OTE 帯の中点、SL = 帯の外側 ± 緩衝。ほぼ必ず指値 → R119 `gapCap` 1.5R の対象 |
| 否定条件 | OTE 帯の外側 ± 緩衝(構造否定)。`ANCHOR_CONSUMED`(レベル消費)も候補を落とす |
| 寿命 | 元チェーンの TTL + FVG の `FVG_MAX_AGE_BARS`(20 本)。`rangeAnchor.freshness` |
| 利確根拠 | `model_targets` + STDV。`+2` 点の `OTE_FVG_CONFLUENCE` 加点付き |
| 出典 | ICT OTE + FVG。係数 0.62/0.79 は `OTE_LO/OTE_HI` |
| 実装箇所 | `msnr_gate.build_candidates`(OTE 節)/ `resolve_range_anchor` / `derive_range_anchor` / `fvg_scan` |
| 未検証部分 | (a) **強いトレンドで深い押しを待ち続ける機会損失に対する代替経路が無い**。(b) アンカー選択の質(`DERIVED_RANGE_PAIRS` の順序)は実測していない。(c) 指値が届かないまま失効した件数は R119 で初めて数え始めた |

---

## 5. 4 モデル共通の層

| 層 | 実装 | 効き方 |
|---|---|---|
| 上位足 | `htf_context.build_context` → `snapshot.htfContext` | `status=ALIGNED` のときだけ `bias` を使い、同方向 +1 / 逆方向 −1 |
| ICT 位置 | `ict_context.range.favors` | 正しい側 +2 / 逆側 −2 / レンジ未取得 0(`RANGE_ANCHOR_MISSING`) |
| SMT | `index_smt` / `external_smt` | FRESH かつ bias 一致 +1 / 逆 −2 |
| CVD | `cvd_health` + `_cvd_score` | ±1、非 fresh は A+ を A へ上限化 |
| killzone / DOL | `ict_session` / `draw_on_liquidity` | 減点のみ(−1) |
| 画像カタログ | `strategy_models.build_strategy_matrix` | 差 ≥2 で +min(2,差) / ≤−2 で −1 |
| Gann | `_score_gann` | ノード +1 / 走路妨害 −1 |
| 等級 | `_finalize_candidate` | score ≥9 → A+、≥7 → A、それ以外 B。**確率ではない** |
| 選択 | `select_primary` | (等級, 点数, TP1 の R, モデル順位)の降順で 1 件 |

---

## 6. この棚卸しで確定した欠落(R122 の実装対象)

| # | 欠落 | 根拠(コード) | R122 での扱い |
|---|---|---|---|
| G1 | 上位足の**仮説**(方向・目標・否定水準・寿命)を保持する構造が無い。消費は `htfContext.status/bias` の ±1 点だけ | `_candidate_for_chain` の htf 節 | `market_structure_context.parent_thesis`(§2) |
| G2 | 親の否定水準を子が破った状態と、親が生きたままの押し目を区別できない | 同上(bias の一致/不一致しか見ない) | `childRelation` + `invalidatedAt` |
| G3 | `classify_amd` は直近 6 本の局所ラベルで、蓄積→掃引→構造変化の**順序**を保存しない | `strategy_models.py:131` | `phase` を観測できた段階だけで持ち、順序は `events` に残す。AMD/AMDX の語彙は名乗らない |
| G4 | 「待つ位置」「待つ期限」「何が来れば入れるか」がどこにも残らない | 候補にならないチェーンは `build_candidates` で `continue` されて消える | `participation_policy.plans()`(記録のみ・claim を取らない) |
| G5 | 浅い押しで継続に参加する経路が無い(BREAKER は `FLIP_HELD` 必須、OTE は 0.62 まで待つ) | `build_candidates` | `participation_policy.shallow_seed()`(§4.2) |
| G6 | 高得点の WATCH 候補が、同じ周期の ARMED 候補より先に primary になる | `select_primary` は等級・点数だけで並べ、`allowed` を見ない | `selection` 段(既定 OFF、実測してから判断) |
| G7 | 短期予測(`ict_stdv.nearTerm`)はアンカーの side をコピーするだけで、評価対象の時刻も基準価格も無い | `ict_stdv.py:496` | `near_term()` に 3 分/15 分の明示地平と基準価格・中立幅を持たせる。**表示・評価専用** |
| G8 | `ictStdv.participation` は未実装のまま契約に節がある(二重判定の入口) | `execution_contract.json` / `msnr_gate.ict_stdv_policy` | R122 を正式な入口にし、`ictStdv.participation` は OFF 固定を機械で強制 |
