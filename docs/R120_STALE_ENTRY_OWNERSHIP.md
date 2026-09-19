# R120: 未約定指値が 115 分残った件 — 利用者申告により運用事由としてクローズ

**2026-09-19 ユーザー申告: この 115 分の空白は問題ない。当該ループのセッションを別の作業に
使っていたため。** これを受けて**運用事由としてクローズ**する。

**技術的な原因は確定していない。** 02:50〜03:44 の 9 周期が
`autotrade_engine._stale_entry_to_cancel` まで**到達したかどうかは未確認**で、到達確認が
済むまで「身元検査で落ちた」とは書かない。到達していなければ(reconcile がその枝に
入っていなければ)、所有権判定の話はそもそも当夜の説明にならない。到達確認の手順は §5。

このファイルは記録として残す —— (1) 当夜の事実、(2) `_stale_entry_to_cancel` の所有権判定が
何を通し何を落とすか(再現試験で固定した。**当夜の説明ではなく、この関数の性質の記録**)、
(3) もし将来同じ形が**運用事由なしに**出たときの確定手順。再現試験は
`python tests/test_r120_stale_entry_ownership.py`(読むだけ)。
**所有権ガードを単に緩める修正はしない**(§4)。

## 1. 起きたこと(JST)

対象は `decisionId 00ab6671affe07a5` / BREAKER_CONTINUATION BUY B / MNQZ6 /
指値 29,699.5 / SL 29,661.75 / TP1 29,763.25 / runner 最終 TP 29,967.25 / 4 枚 × 7 口座。

| 時刻 | 出来事 | 出所 |
|---|---|---|
| 01:48:32 | `ENTRY_CLAIMED` | 台帳 |
| 01:50:07 | `route: ALL_CONFIRMED`(7 口座に指値が乗った) | `order_log.json` |
| 01:50:23 | `ENTRY_RESTING`(`routeSnapshot` 14 脚・ブラケット 28 行) | 台帳 |
| 02:45 | 価格が TP1 29,763.25 に到達(高値 29,765.50) | 確定 3 分足 |
| 02:50〜03:44 | 周期は 9 回走っている(監査コピーが残っている) | `.secrets/monitor_cycle_*.json` |
| 03:45:03 | `ENTRY_HALTED` / reason **`broker order CANCELED`** | 台帳 |

`ENTRY_STALE_CANCEL` の行は**無い**。指値は 01:50:23 → 03:45:03 の **115 分**、新規 ENTRY の
経路を押さえた(取りこぼしの中央値は 8 分。`docs/R119_LIMIT_GATE.md` §3.5)。
最後の `CANCELED` は engine が**観測した**終端状態で、nightwatch が送った取消ではない
(`stopLogic.restingStopRecheck` は SHADOW、`FLATTEN_SENT` の行も無い)。

## 2. 述語の入力は揃っていた(**関数を単体で当てた結果**)

当夜の台帳(02:50 時点まで)と実バンドル `monitor_cycle_0252.json` を
`autotrade_engine._stale_entry_to_cancel` に**直接**当てると:

* `_frozen_plan_record` は 7 口座すべてで `ENTRY_RESTING` / `decisionId 00ab6671affe07a5` を返す
* `_extreme(bundle, "BUY", since=01:50:23)` = **29,765.50** ≥ TP1 29,763.25
* `activeOrders` が **自分のエントリー脚 2 本だけ**なら → **発火する**
  (`TP1 reached before entry (BUY entry 29699.5 / tp1 29763.25; high since … = 29765.5)`)

言えるのは「価格側・台帳側の入力は揃っていた」までである。**当夜の 9 周期でこの関数が
呼ばれたかは確認していない**ので、「落ちたのは身元検査だ」とは言えない。呼ばれる前段には
少なくとも `open_qty <= 0` / `blocking_order` / `not kill_requested` /
`NQX_STALE_ENTRY_CANCEL` / 多口座分岐で当該口座が `targets` に入ること、があり、どれも
当夜の値を確認していない。到達確認は §5。

## 3. 所有権判定(いま何を見ているか。**当夜の説明ではなく関数の性質**)

`_stale_entry_to_cancel` の取消は `order.py --flatten --account`、つまり **その口座の未約定
注文を全部消す**。だから身元の検査がこの経路の唯一の安全装置になっている:

```
ours    = 凍結 routeSnapshot の state=ACCEPTED の orderId(= エントリー脚だけ)
parents = activeOrders のうち parentId を持たない行
発火条件: parents が空でない かつ parents ⊆ ours
```

`bracketOrderIds`(SL / TP の子注文 ID)は `routeSnapshot` に記録されているが `ours` に
**入っていない**。

再現試験で固定した挙動:

| `activeOrders` の中身 | 発火するか |
|---|---|
| 自分のエントリー脚 2 本だけ | する |
| + 子注文(`parentId` 有り) | する(親行に数えない) |
| **+ 子注文(`parentId` 無し)** | **しない** |
| + 手動注文(台帳に無い親行) | しない(正しい) |
| + 別プランの注文 | しない(正しい) |
| + 凍結プランに無い口座の注文 | しない(正しい) |
| 親行が 1 本も無い | しない(消すものが無い) |

R80(`docs/R80_OCO_SIBLING_VERIFICATION.md`)は **CrossTrade/Tradovate の OCO 兄弟が
`parentId` を持たない**ことを実測している。指値を保持している間の子注文がその形で
`activeOrders` に出ていれば、`parents ⊆ ours` は成立しない —— これは**この関数の性質**で、
当夜そうだったという主張ではない。

**当夜については何も確定していない。** `activeOrders` が残っていない上に、そもそも
9 周期がこの関数まで到達したかも未確認(§2)。反証もある —— 09-10 / 09-11 / 09-15 の
`ENTRY_STALE_CANCEL` はブラケット 4 行を持つプランでも発火している。当夜とそれらの
唯一の構造的な違いは**口座数**(7 口座 / 14 脚 / 28 ブラケット vs すべて 1 口座 / 2 脚)で、
口座数が身元判定に効く経路は今のところ見つかっていない。

## 4. なぜガードを緩めないか

`parentId` 無しの子注文で落ちる条件は、**手動注文が混ざっている状態と区別できない**。
`parents ⊆ ours` を緩めると、同じ緩みが手動注文・別プラン・別口座を `--flatten` で
巻き込む側にも効く。2026-09-05 01:4x に利用者が同一口座へ手で 18 枚入れた直後、
まさにこれが問題になってこの判定が入った。

したがって候補は **subset 判定を緩めるのではなく、身元集合を完成させる**方向:
`ours` に凍結 `routeSnapshot` の `bracketOrderIds` を足す。これは「台帳に記録済みの
自分の子注文 ID」だけを足すので、

* 手動注文は入らない(試験で固定)
* 別プランの注文は入らない(試験で固定)
* 当夜の形の子注文は入る(= その形なら発火できるようになる)
* subset 判定はそのまま残る

**まだ実装していない。** 試験は「いまの実装が `bracketOrderIds` を見ていない」ことも
固定しているので、実装したらその 1 行が落ちて気付く。

## 5. 次に確定させる手順(**運用事由なしに**同じ形が出たときだけ)

2026-09-19 の件は冒頭のとおり**利用者申告により運用事由としてクローズ**した。以下は
再発時の手順で、**順番に意味がある** —— 1 を飛ばして 2 から始めると、到達していない周期の
話を身元検査のせいにしてしまう。

1. **到達確認を先にやる。** その周期の reconcile が `_stale_entry_to_cancel` まで来たかを
   確かめる。前段の条件は `open_qty <= 0` / `blocking_order`(注文の集約 state が
   `blockingOrderStates`)/ `not kill_requested` / `NQX_STALE_ENTRY_CANCEL` /
   多口座分岐で当該口座が `targets`(= `open_accounts ∪ blocking_accounts`)に入ること。
   **いまはここが台帳に残らない**(§5-4)。残していない間は 2 以降へ進まない。
2. 到達していたら、指値が残っている周期に一度だけ
   `python broker_status.py --account <口座> --json` を取り、`activeOrders` の各行の
   `parentId` / `orderId` を記録する。これで §3 の表のどの行に当たっているかが決まる。
3. 当たっていれば §4 の候補を実装し、`tests/test_r120_stale_entry_ownership.py` の
   「まだ見ていない」1 行を差し替える。
4. 口座数が効く経路が見つかった場合はこちらを先に直す。
5. **先に入れるべきはこれ**: `_stale_entry_to_cancel` が見送った理由を台帳に 1 行残す
   (いまは `None` を返すだけで、到達したのか・到達して落ちたのかの区別が**どこにも
   残らない**。2026-09-19 の件で原因を確定できなかったのはこの欠落のため)。
