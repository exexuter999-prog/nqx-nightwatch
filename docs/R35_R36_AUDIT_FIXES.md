# R35/R36 監査で見つかった不具合の修正

2026-08-24。発注経路・データ整合性・運用の3方向から監査し、見つかった欠陥を直した。

## 実損に直結していたもの

### 1. 「注文は出ているのに誰も管理しない建玉」

`order.py` 子プロセスの HTTP 予算は最悪 114 秒だが、呼び出し側は **30 秒で kill**
（`autotrade_engine.py:464`）。leg1 送信済み・resolve 前で落ちると、台帳には
送信**前**に書かれる `ENTRY_CLAIMED` だけが残る。この状態が

- **冪等スキップ集合に無かった** → 次サイクルで同じ発注を再試行。
  CrossTrade のペイロードには冪等キーが無いので二重発注に直結
- **凍結プラン採用集合にも無かった** → `management_action` が一度も呼ばれず、
  トレールも建値移動も `reached_stop` の FLATTEN も動かない

両方に `ENTRY_CLAIMED` を追加。注文が実際に出たかは分からないが、
**出たかもしれない建玉を管理下に置く方が安全**。建玉が無ければ何もしない。

### 2. TP1 約定後に建値移動が発動しない窓

`management_action` は `qty != runner_qty` で「建玉が runner 分まで減った」
= TP1 約定済み、を確定させた**後**に、さらに `if not reached_tp1: return None`
としていた。`reached_tp1` は**現在価格**から計算されるので、TP1 約定後に価格が
TP1 を割り込んで戻ると False になり、runner の SL が初期構造 SL のまま残る。
TP1 で確定した利益を runner の満額リスクが打ち消す窓が、警告もログも無しに
開いていた。**建玉の枚数は約定の事実**なので、価格に再確認させない。

### 3. RESOLVE 失敗で ENTRY が恒久ロック（R36）

CrossTrade への POST 後に `resolve_entry_claim` が失敗すると、claim は
`CONSUMED` のまま `routeSnapshot` 無しで残る。`entryRecoveryProof` は ACCEPTED 行を
必須にするので永久に false。`ENTRY_CLAIM_ALREADY_HELD` で再 CLAIM もできず、
**以後すべての新規発注が止まる**。MANAGEMENT には年齢による再取得があるのに
ENTRY だけ非対称だった。

ENTRY は「注文が実際に出ているかもしれない」ので時間だけでは解けない。
**ブローカーが空だと証明できたときだけ**解く（`staleEntryClaimReleasable`）:

| 条件 | 理由 |
|---|---|
| claim が `staleReleaseSec`(900秒) より古い | 通信の一時断と区別する |
| 新鮮なブローカー観測がある | 「何も起きなかった」ことを**証明**する |
| 建玉 0 | 約定していない |
| **未終端の注文が 1 本も無い** | 板に残っていれば二重発注になる |
| 口座と銘柄が claim の意図と一致 | 別スコープの観測で解かない |

解放は `entryStaleRelease` に記録し、`STALE_RELEASED` 遷移と publish の
`reason` にも載せる。黙って解くと「なぜ再発注できたのか」が追えない。

### 4. SL 上限ゲートの fail-open

`_sl_caps` は設定が読めないと `(None, None)` を返し、`vol_gate` を無効化して
WATCH 降格をスキップしていた。契約の既定
（`defaultCapDollars / pointValue / fixedQty` = 60pt）へ落とす。
**自分で数字を決めない** —— 最初 15pt を書いたら契約より厳しく、既存テスト
3 件が落ちた。

## 静かに間違っていたもの

### 5. ICT の時刻が最大 9 時間ずれる

`_et_minutes` は ISO 文字列の文字位置 [11:13] を切り出し `+09:00` 決め打ちで
`hh - 4 - 9` していた。オフセットを一切見ないので、**同じ瞬間でも表記が違えば
別の ET になる**:

| 表記（すべて同一瞬間） | 旧 | 新 |
|---|---|---|
| `2026-08-24T07:46:00+00:00` | 18:46 OFF_HOURS | 03:46 LONDON_KZ |
| `2026-08-24T16:46:00+09:00` | 03:46 LONDON_KZ | 03:46 LONDON_KZ |
| `2026-08-24T03:46:00-04:00` | 14:46 PM_TREND | 03:46 LONDON_KZ |

R28 の取得経路（`tv_snapshot`）は UTC を出すので、**切り替えた瞬間に
キルゾーンと SMT 窓が 9 時間ずれる**ところだった。`quarterly_theory` は最初から
`ZoneInfo` で正しく読んでいたので、同じバンドルを両者が別の時刻として扱う
食い違いも既に存在していた。

`ZoneInfo("America/New_York")` で解釈するようにし、**夏時間の手動切替
（`ICT_ET_OFFSET_H`）も不要**になった。tz が無い時刻は推測せず `None`。

### 6. イベント封鎖が現在無効・警告も無し

キャッシュは 8/19 取得で **8/16〜8/21 収録**、当日は 8/24。`fetchedAt` の年齢
（5日）だけを見て「新鮮」と判定し、**今週のイベントを 1 件も持たないまま
notes も空**だった。フィードは週単位なので取得時刻は鮮度の指標にならない。
**収録期間が今日を覆っているか**で判定する。

### 7. R28 取得経路が publish に到達しない

`monitor_publish.compact` は `snapshot.bars` を必須にしているが、`tv_snapshot` は
`bars3m` しか書いていなかった。ドキュメントの手順どおりに実行しても毎回
`snapshot.bars is required` で止まっていた。

## 自分で入れたコードの設計ミス

### 8. Gann 1×1 の向きと傾き（R32 で入れたもの）

**向き**: 上昇レッグ（安→高）で高値を起点に**下向き**に引いていた。実測で
現在値 30195 に対し 1×1 が 30214 —— 支持線のはずが上値に来る。Gann の 1×1 は
「上昇中は支持」なので、**安値起点で上向き**が正しい。

**傾き**: `priceUnit / timeUnit`（スイング中の平均速度）を使っていたが、これは
「スイング中の速度がその後も続く」前提。実測で 1×1 が 131pt 伸びる間に価格は
67pt しか動かず、線がすぐ価格を追い越して支持として機能したのは 188 本中 **1 本**。

足レンジ中央値そのものも試したが今度は緩すぎて 477pt 離れた。実測（372本）で
起点からの実効速度は足レンジ中央値の **0.12 倍**、スイング平均速度は **0.24 倍**で
一貫して 2 倍だったので、スイング速度の半分を採る（`GANN_SLOPE_DAMPING = 0.5`）。
修正後の現在値→1×1 距離は中央値 **25.9pt**。

**これは相場から測った値であって、当てはめて選んだ閾値ではない。**

なお線は予測ではなく基準なので、価格がどちら側にいるかは相場次第
（上昇レッグで線が支持側なのは 32%）。線を割っている状態も普通に起きる。

### 9. 導出レンジが R12 の検証を迂回していた（R27 で入れたもの）

`derive_range_anchor` は `resolve_range_anchor` の関門
（`ROLLING_3M_OTE_FORBIDDEN` / 時刻検証 / freshness）を**一つも通らない**。
そして材料の `session_levels_from_bars` は**進行中セッションの高安**を含み、
これは毎サイクル広がるローリング極値 —— R12 が OTE に使うことを禁じた
「ローリング 3M 高安」と実質同じ性質だった。名前が付いているだけで通すのは
筋が通らない。

`tv_snapshot` が `settled` を刻み、`derive_range_anchor` は
**終わったセッションだけ**をレンジの起点にする。進行中の高安は流動性プール
としては意味があるので、描画には引き続き使う。

## 壊れた入力への耐性（R34）

新規モジュールに異常入力を投げて 4 件の欠陥を発見・修正:

- `quarterly_theory._phase_behaviour` — 高安キー欠損の足で **KeyError 落ち**
- `msnr_gate._score_gann` — `gann` が dict でないと **AttributeError 落ち**
- `tv_snapshot._num` — **NaN と inf を通す**（`"1e400"` → `inf`）
- `ultra_mode.account_plan` — 早期 return で `routable` キーが消え、JS と形が食い違う

## 撤回した修正

`ultra_mode.required_qty_split` が極小 TP で 5000 億枚を返すのを関数側で `None` に
したところ、契約の `ULTRA_QTY_EXCEEDS_ACCOUNT_MAX` が `QTY_NOT_COMPUTABLE` に化けて
**どの上限で落ちたか分からなくなり**、既存テスト 2 件が落ちた。既に契約側が
止めているので、私の修正は理由を潰しただけだった。戻した。

## 検証

Python **41 ファイル** / Worker **117 件** / ミニアプリ **93 件** 通過。
Worker はデプロイ済み（Version ID `2fdd0df8`）。

## 未修正で残っているもの

監査は他にも挙げている。重いものから:

- **MODIFY 後の RESOLVE 失敗で建玉が二重ロック** — `managementRecoveryProof` は
  「保護注文が終端」を要求するが、MODIFY が成功していれば張り替えた SL/TP は
  WORKING なので原理的に成立しない
- **HALT レコードのキー空間が `ENTRY_RECOVERED` と一致しない** — キル flatten が
  一度でも検証に失敗すると、以後すべての新規 ENTRY が永久に止まる
- **`stderr` 1 バイトで成功送信が HALT** — `if confirm and stderr` は Python の
  DeprecationWarning でも発火しうる
- **Mini App 発注で作った建玉は engine 管理外** — 凍結プランが無いので
  トレールも建値移動も走らない
- **台帳にローテーションが無い** — 過去の HALT が永久に評価対象に残る
- **DST ハードコードが 3 系統残る** — `order.py:361` / `autotrade_engine.py:455` /
  `dayguard.py:41`。`msnr_gate` は R35 で解消済み
