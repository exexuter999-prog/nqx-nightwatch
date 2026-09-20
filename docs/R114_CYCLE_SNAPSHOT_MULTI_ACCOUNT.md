# R114〜R116: 一括照会と、多口座で初めて踏んだ照合の穴

2026-09-19 未明。Python のみ(`broker_status` / `autotrade_engine` / `monitor_publish` /
`nqx_state` / `trade_journal`)。**Worker の deploy は不要。**

ユーザー決定: 「七口座でも二〇口座でも一口座と同じ瞬発力が欲しい」→ 根本設計を見直す。

## 実測(推測を置かないための数字)

| 周期 | 照会本数 | サイクル | 備考 |
|---|---|---|---|
| 03:03(R113 まで) | 73 | 258 秒 | `trade_journal` だけで 20 本 107 秒 |
| 03:30(R114) | 52 | **47 秒** | 一括照会 21 本 8.0 秒。ただし `broker order is UNVERIFIED` |
| 03:38(R114+R115) | 47 | **57 秒** | 照合の穴が塞がり、残りは RECOVERY_UNVERIFIED |
| 03:45(R114〜R116) | 47 | **40 秒** | HALT なし。**相場取得 → 終了 33 秒**(成行の門 60 秒に +27 秒) |

03:45 の周期で engine は 01:50 の 14 本の注文を口座ごとに正しく観測し、**CANCELED** と確定して
凍結プランを終端した(`ENTRY_HALTED | ENTRY_TERMINAL | broker order CANCELED`)。約定 0・実現損益 0。
R116 は `ENTRY_RECOVERED | ENTRY_CLAIM_STALE_RELEASABLE_ALL_FLAT` として台帳に残り、DO の claim は
次の ARMED の CLAIM で解放される(それまで Mini App の ENGINE は STALE SLOT = 正しい表示)。
残る最大の照会は `trade_journal` の fills 7 本 12 秒。FLAT が続く周期では省ける余地がある。

段階別では TradingView 取得 5.2 秒。時間は**ほぼ全部ブローカー照会**で、同じデータを
表示・engine・戦績記録・ULTRA サイジングが別々に取り直していたのが正体
(呼び出し元を記録して確定: 残高 7 口座 × 2 回、建玉が 3 回)。

### CrossTrade は「遅延」で絞る(決定的)

同じ照会を 5 回続けただけ: 0.69 → 0.62 → **5.05 → 4.99 → 16.26 秒**。429 を返すのではなく
応答を遅らせる。よって並列にしても速くならず(サーバ側で待たされる)、retry も間引きも
効かず、**照会の本数だけが効く**。夜通し叩いた後ほど遅い(20:57 に 1.37 秒 → 03:00 に 3〜5 秒)。

## R114 一括照会 —— 1 サイクルで各(口座, データ種別)を一度だけ観測する

`monitor_publish.main()` の先頭で `broker_status.prime_cycle_snapshot(symbol, accounts)` が
口座ごとに **建玉・注文(ID 無し)・残高を 1 回ずつ**取り、共有へ入れる。以後の消費者
(`live_position_line` / `build_accounts_payload` / engine の観測 / `trade_journal` /
`sync_position`)はその共有を読む。1 口座あたり 3 本が床で、7 口座なら 21 本(実測 8 秒)。

**共有を読まない経路(安全条件。`tests/test_r114_cycle_snapshot.py` で固定):**

1. **`order.py` を起動したら捨てる**(`autotrade_engine._run_order`)。ドライランでも捨てる —
   送ったかどうかを親は断定できない。送信後の再照会は必ず生になる。
2. **証明用の二度読みは `live_reads()` の中**(`_stable_broker_snapshot`、R52 の一瞬 FLAT の
   再確認)。共有に当たると before/after が同じオブジェクトになり、DO への不在証明が偽物になる。
   読んだ結果は共有へ書き戻す。
3. 注文の共有鍵は `known_order_ids` を含む。ID 付きの照会は終端した注文の詳細を持つので、
   ID 無しの結果で代用しない。

TTL は 1 サイクルを覆う 240 秒(`NQX_BROKER_READ_CACHE_SEC`)。25 秒ではサイクルより短く
共有にならなかった。長くして安全なのは上の 1 と 2 があるから。

## R115 注文 ID は照会する口座で絞る —— 多口座で初めて踏んだ穴

01:50、7 口座への ENTRY(BUY 4 @29,699.5)が CrossTrade で **14/14 受理**された直後、送信後照合が
`crosstrade order row account is outside configured scope` で UNVERIFIED になり HALT。
以後の全サイクルの観測と RECOVER も同じ理由で止まった(約定は 0、実現損益 0、未約定 0)。

原因: routeSnapshot は行ごとに `accountId` を持つのに、engine が **14 件全部の注文 ID を
各口座の照会に渡していた**。アダプタは他口座の注文行を(正しく)弾く。1 口座では起きない。

`_route_rows_for_account(rows, account)` で **その口座の行だけ**にする。適用は 3 か所:
観測(`_reconcile_one` の `known_order_ids`)/ 送信後照合(`post_entry_orders`)/
回復(`_recover_entry_from_current_broker` の `frozen_ids`、観測口座は建玉照会の accountId で知る)。
口座が分からない旧経路は従来どおり全行。`tests/test_r115_route_ids_per_account.py`。

## R116 多口座 claim は DO の RECOVER 証明を構造的に通せない

DO の `entryRecoveryProof` は「1 回の観測で claim の全 receipt を照合」する設計。7 口座の
claim では 1 口座の観測に他 6 口座の注文が載らず、RECOVER は常に 409
`ENTRY_CLAIM_RECOVERY_UNVERIFIED`。一方 DO の stale release(`staleReleasable`)は 1 口座の
観測から出るので、多口座では「A が空 = 全部空」ではない。

engine は全口座を観測している。DO が `staleReleasable=true` と言い、かつ engine が
**全口座** の不在(verified FLAT・blocking 注文なし)を共有観測で確かめたときだけ CLAIM へ
委ねる(`_claim_scope_all_flat`)。**engine は何も解放しない** —— 解放は CLAIM 時に DO が
自分でやる。台帳には `ENTRY_CLAIM_STALE_RELEASABLE_ALL_FLAT` が残る。
`tests/test_r116_multi_account_stale_defer.py`。

**DO 側の設計そのもの(1 観測 = 1 口座)は残っている。** 多口座の RECOVER 証明を Worker で
受けるには observation を口座配列にする必要があり、それは R87(intent v2 / 口座別レーン)の
仕事。ここでは engine 側で安全に迂回した。

## 効かなかった対策(同じ道を通らないために)

* **並列化**: 6 並列で 1 本の往復が 1.4 → 4.2 秒へ伸びて相殺、7 口座中 3〜4 口座が UNVERIFIED。
  仕組みは `broker_status.parallel_map` に残す(`NQX_BROKER_PARALLEL`、**既定 1**)。
* **R111 の retry と間引き**: レート制限は全部の照会に同時にかかるので、「1 本の一過性 429」
  想定の retry は待ちを積むだけ。あり 300 秒超 / なし 198 秒。既定を
  `HTTP_MIN_INTERVAL_SEC=0`・`HTTP_RETRY_BUDGET_SEC=5` へ倒した。

## 併行して入った変更(Codex)

同時刻に別セッション(Codex)が `execution_contract.json` の `fillWatch.idleIntervalSec` を
5 → **45** に、`nqx_state._cached_balances`(残高 30 秒キャッシュ)を入れている。どちらも
本件と矛盾しない(fill_watch の待機中の照会が減る)。**同じファイルを 2 セッションで触る
のは事故の元**なので、以後はどちらか一方で。

## 戻し方

* R114 だけ: `NQX_BROKER_READ_CACHE_SEC=0`(共有なし・毎回生)。`_prime_broker_snapshot` は
  そのまま残るが害は無い。
* R115: `_route_rows_for_account` を全行返す関数に戻す(= 多口座で再び UNVERIFIED)。
* R116: `_reconcile_one` の `elif ... staleReleasable ...` 節を消す。

## 検証コマンド

```powershell
python tests/test_r114_cycle_snapshot.py
python tests/test_r115_route_ids_per_account.py
python tests/test_r116_multi_account_stale_defer.py
python tests/test_r113_read_cache_safety.py
python tests/test_r22_recovery.py
python tests/test_r53_unresolvable_order.py
python tests/test_r84_recovery.py
python tests/test_r40_multi_account.py
```

1 サイクルの照会を測る(送信はしない・AUTO の判断は従来どおり):

```powershell
$env:NQX_BROKER_PROFILE = "1"; python nqx_cycle.py
```

`.secrets/broker_profile.jsonl` に 1 本ごとの種別・呼び出し元・秒数が追記される。

## 残っていること —— 20 口座で「1 口座と同じ瞬発力」

いまの設計は **1 サイクル 3N 本**(N = 口座数)。7 口座で 21 本 8 秒、20 口座なら 60 本
≈ 25〜60 秒で、成行の `marketOrder.maxPriceAgeSec=60` に対して余裕が無い。

次の段階は「ブローカーと話すプロセスを 1 つにする」: `fill_watch`(既に常駐・口座ごとに
巡回している)を **唯一の観測者**にして、建玉・注文・残高のスナップショットをファイルへ
書き続け、サイクルはそれを読むだけにする。サイクルの観測が O(N) から O(1) になり、
CrossTrade への総リクエストも 1 本の流れに揃う。`order.py` の送信前検査だけを生で残す。
これは常駐プロセスの設計変更なので、建玉が無く相場が閉じているときにテストを先に書いてから。
