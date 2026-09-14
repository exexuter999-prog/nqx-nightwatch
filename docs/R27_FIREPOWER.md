# R27 火力を上げる

2026-08-24。実サイクル 403 本(221時間)の再生に基づく。

## 効果

| 指標 | 修正前 | 修正後 |
|---|---|---|
| **武装 (ARMED)** | **0** | **101** |
| うち A+ | 0 | **68 (67%)** |
| 1日あたり | 0 | **11.0 本** |
| `rangeAnchor` valid | **0 / 403** | **236 / 403 (59%)** |
| SL 中央値(全モデル) | 23.8pt | **20.5pt** |
| TP1 の R 中央値 | — | 1.92（最小 1.51） |
| 最大 SL | 278pt | **58.8pt**(上限 60pt 内) |

想定日次リスク $658（1トレード $60 × 11.0本）。口座の日次上限 $600 にほぼ一致する。
**本数の天井は慎重さではなく口座規則**で、ここで既に飽和している。
これ以上は本数ではなく 1 本あたりの枚数（ULTRA / リスク引き上げ）で伸ばす。

## 何が止めていたか

392 候補すべてが `NO_CONFIRMATION` で止まり、確認要素 7 種の取得率は**全て 0%**
だった。うち **232 本(59%)は `NO_CONFIRMATION` だけ**が理由で、**156 本は A/A+**。
つまり「安全に止めていた」のではなく**壊れて止まっていた**。

各ゲートを外したときの武装数（実測）:

| 設定 | 武装 | A+ 比率 |
|---|---|---|
| 現状（当時） | 0 | — |
| **導出レンジを確認に数える** | **86→実装後 101** | **67%** |
| `NO_CONFIRMATION` 単純撤廃 | 156 | 5% |
| 全撤廃（B 等級も発注） | 392 | — |

**単純撤廃より本数が少ないのに A+ が 12 倍**。ゲートを外すのではなく、実データで
加点を足すため。

## 1. レベル集合からディーリングレンジを導出する

`msnr_gate.derive_range_anchor()`。明示 `rangeAnchor` が無いときだけ落ちてくる。

レベル集合には ICT が dealing range に使う当のアンカーが入っている
(London Low 347 / New York High 303 / Previous Day High 175 …)。外部
(TradingView)由来・上位時間軸・明示的に命名されており、`resolve_range_anchor` が
禁じる「ローリング 3M 高安の後付け」とは別物。

条件（すべて満たしたときだけ）:
- 高安の両方が存在し `high > low`
- `lo <= price <= hi` — 現値がレンジ内。外なら別のレンジが働いている
- `span >= 40pt` — 実測 min 16.2pt の退化レンジを弾く

出力に `source: "levels"` と `anchorType: "SESSION_NY_DERIVED"` 等を刻み、
手入力アンカーと**絶対に混ぜない**。既存の検証経路（`ROLLING_3M_OTE_FORBIDDEN`
を含む）は手入力アンカー専用のまま変えていない。

**R26 での判断を反転した。** 当時は「エンジンが自分で計算できる値で自分の確認要件を
満たすのは筋が悪い」として確認に数えなかった。火力優先の指示を受けて反転している。

副次効果として **OTE_FVG_PULLBACK が初めて発火した**（12 本）。OTE は
`rangeAnchor` に依存しており、それが 0% だったため一度も動いていなかった。

## 2. SL の下限を足した（火力を守るための制約）

それまで全経路の risk チェックが**上限のみ**だった（`msnr_gate` /
`execution_contract` / `state_machine.js` / `monitor_publish` / `autotrade_engine`
のいずれも `risk > cap` しか見ない）。実効的な下限は `model_stop_buffer` の
2.0pt 定数だけ。

**risk は R の分母なので、退化した SL ほど R が膨らんで高得点になる**という逆転が
起きていた。OTE は `risk = 0.085 × span + buffer` なので、狭いレンジで簡単にそこへ落ちる。

```python
MIN_RISK_PT = 4.0            # 絶対下限
MIN_RISK_NF_MULT = 0.5       # model_stop_buffer と同じ倍率
```

`_geometry_blockers` に `RISK_BELOW_NOISE` を追加。判定に使うノイズフロアは
**候補に持たせる**（`candidate["noiseFloor"]`）。引数で渡すと、entry/stop を上書きして
から `_finalize_candidate` を再実行する経路（VP80 / OTE）で落ちる —— R25 の欠陥と同じ形。

倍率は 0.5。**1.0 にすると実測で正当な BREAKER/TURTLE まで 27 本落ちた**
（武装 98 → 71、SL 中央値 23.1 → 33.8pt）。狙いは 2pt 級の退化を潰すこと。

併せて OTE にレンジ幅の下限 `max(40pt, 6 × noise_floor)` を追加した。

## 3. VP80 の SL を今回のエピソードに限った

`candidate_vp80` は VA edge の外に出た足を**窓 60 本（= 3 時間）**分走査して極値を
取っていた。3 時間前の無関係な突出まで SL に取り込んでいた。

`vpPath` は今回のエピソードの起点 `reEntryBarT` を持っているのに使われていなかった。
走査をそこ以降に限っただけ:

| VP80 | 修正前 | 修正後 |
|---|---|---|
| SL 中央値 | **62.0pt**（上限 60pt 超） | **22.0pt** |
| SL 最大 | 278.5pt | 83.0pt |
| `RISK_CAP_EXCEEDED` で破棄 | **53 / 101 (52%)** | **1 / 52** |

候補数が 101 → 52 に減ったのは、正しい SL だと「復帰済み」判定や最小 R で落ちるため。
それらは元々成立しない建玉で、広すぎる SL で候補化されてから捨てられていた。

R25 は採点経路の統一だけを行い、SL の取得元は変えていなかった。

## 4. サーバが破棄したシナリオを「成功」と報告しなくした

`tombstoneCycle()` (`state_machine.js:2296-2321`) は `next.scenario = null` にしながら
**`accepted: true`** を返す。`nightwatch_do.js` は HTTP 200 の本文から `reason` を
落としていた（`reason` が入るのは 409 経路だけ）。`nqx_state.publish()` は 200 なら
本文を見ずに `True` を返していた。

結果、**シナリオがサーバ側で消えているのに Telegram は「ARMED — 発注ボタン
表示中」と表示**していた。Mini App には何も出ない。破棄の引き金は 9 種
（market/scenario 検証失敗・evidence のティック不整合・`evidenceHash` 不一致・
ingest 時点で失効 など）。evidence のティック不整合 1 つで丸ごと落ちる。

- `nightwatch_do.js`: 200 の本文に `reason` を含める
- `nqx_state.publish()`: `CYCLE_TOMBSTONED` で始まる `reason` を**失敗として返す**

情報は `view` として既に配線に載っていた —— 読んでいなかっただけ。

## 5. 本文を画像より先に送る

`_send_scenario_card` が本文より前にあった。`sendPhoto` のタイムアウトは **60 秒**で
本文の 45 秒より長く、しかも画像を送るのは **A/A+ かつ ARMED のときだけ** ——
最も急ぐサイクルだけが余計に待たされていた。順序を入れ替えた。

## 検証

`python tests/run_all.py` **36 ファイル通過**（`tests/test_r27_firepower.py` が新規、
21 項目）。`cloudflare` の **112 テスト通過**。Worker はデプロイ済み
（Version ID `38c3d16c`）。

新規テストが固定する不変条件:
- 導出レンジは現値がレンジ内・幅の下限を満たすときだけ成立し、`source="levels"` を刻む
- 明示アンカーがあれば導出へ落ちない（`ROLLING_3M_OTE_FORBIDDEN` は生きている）
- SL の下限は**採点後に entry/stop を上書きしても効く**（迂回テスト）
- OTE はレンジ幅の下限を満たさなければ候補化しない
- VP80 の SL は `reEntryBarT` 以降だけから取る（窓全体より必ず狭い）
- `CYCLE_TOMBSTONED` は publish の失敗として返り、理由が呼び出し側へ渡る

## まだ直していないもの

検査で見つかったが今回は着手していない。いずれも**発注機会を失う**側の欠陥:

- **`positionCheck` が無いとボタンが一度も出ない** — `display.orderable` は
  `positionCheck.verified === true` を要求し、これを書くのは `telegram_bot.py` の
  60 秒ポーリングだけ。bot が動いていなければどんな ARMED も発注不能
- **宙吊りの `CLAIMED` が以後すべてを永久ブロック** — `RELEASE` アクションが無く、
  `entryClaim` に TTL 掃除も無い。CLAIM 後 30 秒以内に CONSUME できずプロセスが
  死ぬと復旧不能
- **`ENTRY_RESTING` が無期限に全シナリオを止める** — 約定しない指値はポジション
  遷移を生まないので自動終端が発火しない。出口は `/flatten` だけ。
  指値を増やす（＝火力を上げる）ほど踏みやすい
- **2 本足型の掃引が「リクレイム足」を極値として記録する** — SL が観測済みの掃引
  ヒゲの内側に入る。R26 の指値モデルの根拠がこの型では成立しない
- **BREAKER が `holdBarT` を無視する** — ブレイク足を丸ごと飲み込むので SL が広い。
  縮めれば R が上がり、`MODEL_MIN_R` で落ちていた目標が通る
- **`maxPriceAgeSec: 5秒` が到達不能** — MARKET 経路は事実上死んでおり、
  すべて LIMIT を通る。LIMIT は価格鮮度チェックを丸ごとスキップするため、
  最大 10 分前の価格観測に対して約定させられる
- **確認要素は `ICT_LOCATION` だけ** — SMT / CVD bias / po3 は依然 0%。
  `docs/ICT_ACQUISITION_PROCEDURE.md` 参照。取れれば A+ 比率がさらに上がる
