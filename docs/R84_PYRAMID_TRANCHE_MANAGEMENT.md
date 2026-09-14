# R84 — 追撃ポジション管理: 構造トランシェ台帳 (Structural Tranche Ledger)

**状態**: **実装済み(影運転)**。2026-09-12 に M0〜M3 のコードを入れ、契約は
`pyramid.enabled=true` / **`dryRun=true`** —— 判定はサイクル注記と台帳に出るが、
**一枚も送らない**。実弾へ回す(`dryRun=false`)前に必要なのは §11 の M3/M4、つまり
**人による Worker のデプロイ**(`cd cloudflare && npm run deploy`)と、そのあとの
実サイクル観測である。この文書が引き続き R83 追撃経路の正本。

実装物:

| 層 | 物 |
|---|---|
| 純関数 | `tranche.py`(三状態・期待枚数・位相・合成プラン・`bind_composite`・`management_action_composite`)、`pyramid.py`(既存) |
| engine | `autotrade_engine.py` の「R84 追撃(pyramid)」節 + `_reconcile_one` の配線 |
| 機械ゲート | `order.py` の `--pyramid` / `--pyramid-base` / `--pyramid-consolidate` |
| 正本 | `cloudflare/src/state_machine.js` の `pyramidClaimGate` / `pyramidBaseVerified` / `pyramidSupersedable` / 追撃 stale release |
| 記録 | `trade_journal.py` のトランシェ帰属、`execution_intent.py` の任意 `pyramid` |
| テスト | `tests/test_r84_*.py`(6 本)、`tests/_r84_fixtures.py`、`cloudflare/test/r84_pyramid_claim.test.mjs` |

出口条件の確認: `python tests/run_all.py` = **ALL PASS (99 files)**、
`cd cloudflare && npm test` = **198 pass / 0 fail**。
**単一プラン経路のテストには 1 件も手を入れていない**(`ownership_binder.bind()` 無改変の検証)。
`ownership_binder.py` への変更は公開エイリアス 2 行(`bracket_structure` / `generation_wrapper`)だけで、
`bind()` の本体は 1 文字も変えていない。

---

## 0-a. 実装時に仕様から意図的にずらした点(と、その理由)

設計の骨格(§1 の 3 本柱)は変えていない。以下は**実装してみて初めて成立しないと分かった
ところ**だけで、いずれも安全側へずらしてある。

1. **`add_price` は `_fresh_price()` ではなく publish された正本価格**(§3.1-5)。
   Worker は claim 時に `market.price` で `executionIntent` を組み、CONSUME で **byte 比較**する。
   送信の `--last` に別の値(送信直前の quote)を渡すと `ENTRY_CLAIM_INTENT_MISMATCH` で
   claim が宙吊りになる(R52 の 15 分ロックと同じ形)。現在値は**鮮度ゲート**として使い、
   正本価格との乖離が `maxDeviationPoints` を超える周期は `ADD_PRICE_DEVIATION` で見送る。
2. **基礎建玉は `OWNED_FULL` / `OWNED_COMPOSITE` のときだけ追撃する**(§3.1-1 は
   `PARTIAL_FILL` も許していた)。部分約定中は未約定の脚が残っており、その上に足すと
   どの脚がどの建玉かを構造で追えなくなる。`BASE_NOT_FULLY_FILLED` で見送る。
3. **「blocking order なし」(§3.1-2)は文字どおりには成立しない**。建玉があれば保護注文が
   必ず生きているので集約 `order.state` は常に `PENDING` になる。代わりに
   `bind_composite` の「同方向の親行(`parentId` 無し)が他にあれば所有しない」で
   **未約定エントリーの不在**を構造で要求する(こちらの方が強い)。
4. **追撃 claim の stale release は「送信前の集合の外に未終端行が無い」**(§7-5 は
   「intent と同方向の未終端注文行がゼロ」)。DO の観測行は
   `{orderId, receipt, accountId, symbol, status}` しか持たず **side が無い**
   (`normalizeBrokerObservation`)。side を足すと `BROKER_OBSERVATION_FIELDS` が変わって
   既存の `bo_` ハッシュが全部変わり、Python と Worker を同時に差し替えられない
   (Worker のデプロイは手動)。集合の外か中かで見る形は実装可能で、しかも**同方向に
   限らず新しい行を一切許さない**ぶん強い。engine 側 §9 の不在の証明と同じ述語になる。
5. **§7 に無いが必須だった Worker 変更を 3 つ足した**。どれも欠けると追撃が
   「claim できない」か「consume できない」で必ず止まる:
   * `CONSUME` の `flatVerified(state)` → `pyramidBaseVerified(state, claim)`。
   * **claim の枠は 1 つしかない**ので、追撃はその建玉を作った claim を
     `pyramidSupersedable`(同一銘柄・同方向・注文が出た claim・建玉が宣言どおりの枚数、
     全部 DO 自身の state で再検証)のときだけ引き継ぎ、`entrySupersededClaims`(上限 8)に
     記録を残す。引き継がないと、建玉がある限り RECOVER も stale release も原理的に
     通らないので保有中の追撃は永久に不可能。
   * `projectState` の `entryClaim` 投影に `pyramid` を載せる(storage にしか無い値は
     エンジンから見えない —— `entryStaleRelease` が実際そうなっている)。
   `projectState` 自体は**緩めていない**。緩めると Mini App の手動発注ボタンが建玉中にも
   出て、通常経路の CONSUME 封印まで一緒に弱まる。
6. **統合(§5.3)に 3 つ足した**。
   * `order.py --pyramid-consolidate`: `--modify --ultra` は逆方向の live 行が 2 本を
     **超える**と拒否する(R52/R78)。runner が 2 トランシェ生きていれば 4 本なので、
     統合はそのままでは送れない。宣言されたときだけ「2 本 × 組数」を許す。
   * `order.py` の `NQX_MODIFY_BRACKET` 行: 張り替え後の新しい 1 組の身元を機械可読で返す。
     **これが無いと次の周期に所有権が落ちる** —— `cancelandbracket` は古いブラケットを
     全部取り消すので、凍結 bracket id は存在しなくなり、構造導出枚数が 0 になる。
   * 脚の第 4 状態 `PENDING`: 張り替えは成功したが新しい 1 組を束縛できなかった区間。
     CLOSED と読むと所有権ごと落ちるので「生きている」と数え、管理は FLATTEN だけに絞る。
     次の周期が `oco_sibling_pair` で束縛し直して畳み直す。
7. **`executionIntent.pyramid` は任意フィールド**(§7-4)。常に `pyramid: null` を載せる形は
   既存の `xi_` ハッシュを**全部**壊す。キーが無い intent のバイト列は 1 文字も変わらない
   ことを `tests/test_r84_worker_conformance.py` が固定している。
8. **`_repair_unprotected_runner` に `expected_pairs` を足した**(§5.2 の注が指摘していた穴)。
   合成建玉では「守られている本数」は 2 本ではなく **生きている脚の数 × 2** 本。1 に
   固定したままだと、2 トランシェのうち片方が裸になっても「ブラケットはある」と読んで
   修復せずに HALT する。単一プランは `expected_pairs=1` で従来どおり。
9. **`PYRAMID_SKIPPED` は連続する同じ理由を台帳へ積まない**(§3.1-6 の「1 サイクル 1 行」は
   守る)。建玉を数時間持つ間ずっと同じ理由が出るため。注記そのものは毎周期出るので
   観測は落ちない。

### 実装後のレビューで見つけて直した欠陥

* **AUTO OFF の管理専用経路から追撃が出る穴**(2026-09-12 修正)。
  `_disarmed_single_account_management`(R68)と `_reconcile_locked` の多口座ループは、
  AUTO が切れていても建玉管理と未約定取消だけは通すために `NQX_AUTOTRADE=1` /
  `NQX_LIVE_ORDERS=1` を差し込んで `_reconcile_one` を呼ぶ。その安全性は
  **「`_reconcile_one` は建玉があるとき ENTRY へ進まない」**という不変条件に
  乗っていたが、R84 は open_qty>0 の枝の中に送信経路を足したので、その不変条件は
  もう暗黙には成立しない。**追撃は新規 ENTRY** なので AUTO OFF では出してはならない
  (CLAUDE.md §3)。`NQX_ENTRY_DISARMED` の印を両方の注入箇所に立て、
  `_pyramid_consider` が先頭で `ENTRY_DISARMED` として見送る。
  `dryRun=true` の間は表に出ないが、**`dryRun=false` にした初日に飛ぶ**種類の穴だった。
  固定は `tests/test_r84_disarmed_no_add.py`(`dryRun=false` で回す)。
* **pending 統合が凍結できず所有権を落とす穴**(同日修正)。`collapse_to_consolidated`
  が身元未束縛の統合を `PYRAMID_CONSOLIDATION_IDS_INVALID` で弾いていたため、張り替えは
  成功したのにプランを再凍結できず、次の周期に「対が消えた」= 全脚 CLOSED と読まれた。
  pending を受けて畳むよう修正し、`consolidationPair.pending` を伝播させた。
* **送信 UNKNOWN の HALT がコミット後も残る穴**(同日修正)。`_clears_halt` は
  `ENTRY_RECOVERED` / `HALT_CLEARED` か truthy な `haltResolution` しか見ないので、
  コミット行(`status=ENTRY_SENT`)では解けず、**回復して正しく管理していても以後の
  新規 FLAT ENTRY が全部塞がる**。コミットは「注文が出てブローカーの構造で束縛できた」
  証明そのものなので、R76 の rebound と同じ形で
  `haltResolution=PYRAMID_COMMITTED_FROM_BROKER` を書く。
* **WAL が口座で絞られていない穴**(同日修正)。多口座で A 口座の WAL を B 口座の周期が
  拾い、B の建玉へ A のプランを束縛しようとして「scope 外」の hold を返していた。それが
  R46 の手動建玉除外(`_is_manual_position_account` の「全 note が既知集合」判定)を壊し、
  経路全体を止める。`_pyramid_wal` を口座で絞り、併せて R46 の判定から追撃の注記
  (`pyramid` で始まる行)を除外した —— 追撃は所有と無関係な付随情報である。

* **統合の後にもう一度追撃すると所有権を丸ごと失う穴**(同日修正・critical)。
  `oco_sibling_pair` は「scope に未終端の逆方向行が**ちょうど 2 本**」を要求するので、
  判定器へ全行を渡していると「統合済み 1 組 + 新トランシェ 2 脚 = 6 行」で
  INCONSISTENT へ倒れ、合成建玉まるごと管理外になる。**契約の `maxAdds: 2` で到達する。**
  凍結した 2 行だけを `rows` に、子ブラケットの検査用に全行を `all_rows` に渡す形へ修正
  (`route_identity.bind_replacement_bracket` と同じ渡し方。判定器は R80 のまま 1 か所)。
* **R78 修復のあと合成プランを再凍結していなかった穴**(同日修正)。修復も
  `cancelandbracket` なので、成功した時点で凍結していた対はもう存在しない。合成は
  対の身元だけで脚の生死を判定するため、再凍結しないと次の周期に全脚 CLOSED と読まれ
  「修復したのに管理外」になる。`_repair_unprotected_runner` に `outcome` を足して
  何を張り替えたかを返し、合成側で `_freeze_consolidation` を通す。
  (単一プランは親行で照合できるので同じ問題を持たない。)
* **基礎が合成として読めないまま足すと恒久回廊になる穴**(同日修正)。R72 の /fills
  束縛で身元を取った脚は `bracketOrderIds` を持たない —— 単一プラン経路は親行で照合
  できるので所有は成立するが、合成は対の身元しか見ない。そこへ足すとコミットが
  INCONSISTENT になり、不在の証明も「建玉が増えた」で通らず、**恒久的に遷移回廊
  (FLATTEN のみ)**へ落ちる。`_pyramid_consider` が送信前に基礎を合成として読み直し
  (`_attach_bracket_ids` で子行から補える分は補う)、読めなければ
  `BASE_STRUCTURE_UNVERIFIABLE` / `BASE_STRUCTURE_QTY_MISMATCH` で見送る。
  見送れば基礎はこれまでどおり単一プラン経路で管理され続ける。
* **Worker: 通らない claim を取ってしまう非対称**(同日修正)。通常経路は
  `projectState` の `orderLive` で未終端注文を塞ぐが、`pyramidClaimGate` はそこを
  見ていなかった。CLAIM は通るのに CONSUME の `ENTRY_CLAIM_ORDER_CONFLICT` で必ず
  落ちる claim を取り、stale 解放(最大 180 秒)まで枠を潰す。同じ条件を門にも入れた。
* **Worker: 決済後の追撃 claim が永久に解けない穴**(同日修正)。追撃の stale release を
  「建玉 == baseQty」で固定していたため、**合成建玉が決済されて 0 になった後**に残った
  claim はどちらの証明にも当たらず解けない —— 以後の新規が全部止まる、この repo の事故
  第 1 位の形(`entry-claim-deadlock` / R50 / R53 / R69)。建玉が baseQty でないときは
  通常の不在証明(建玉 0 + 全注文終端)へ落ちるようにした。

固定は `tests/test_r84_recovery.py` / `tests/test_r84_disarmed_no_add.py` /
`tests/test_r84_bind_composite.py` / `cloudflare/test/r84_pyramid_claim.test.mjs`、
**1 本の台帳で最初から最後まで通す** `tests/test_r84_lifecycle.py`
(基礎 → 追撃 → 各 TP1 → 統合 → トレール → 撤退 → 戦績)、および性質で縛る
`tests/test_r84_invariants.py`(ランダム構造 4000 通りに対して、所有・枚数・回廊・
ACCUMULATION の MODIFY 禁止・逆側 SL の不在を確認)。

### 敵対的レビューで見つけて直した欠陥(2026-09-13)

多エージェントの敵対的レビューを 2 回走らせた。1 回目は全エージェントが利用制限で
落ちて **所見ゼロ**(コードが綺麗だったのではない)。2 回目は 106 体中 23 体が完走し、
3/3 の投票で確定した 2 件と、自分で精査して本物と判断した 9 件を直した。

* **A) `PYRAMID_CLAIMED` が `claimJournal` を持たなかった**(critical)。claim を取る
  **前**に WAL を書いていたので、`order.py` の門で落ちた周期の claim がローカルに
  残らないまま DO に CLAIMED で居座る。建玉が決済されて FLAT 経路へ落ちた瞬間、
  毎周期「authoritative ENTRY lock has no durable local claim journal」を返して
  **以後の新規・管理が全部止まる**(CLAIM を送らないので DO の stale 解放も動かない
  = 恒久ロック。この repo の事故第 1 位の形)。claim → `claimJournal` 付きで WAL、の
  順序へ直した(単一プラン経路は最初からこの順序)。CLAIM 拒否では WAL を書かない
  —— 解くべきものが無いのに書くと次の周期が回廊へ入る。
* **B) `order.py` が `--pyramid` でも追撃脚だけのリスクを口座上限に当てていた**(high)。
  engine が渡す `--ultra-drawdown` は**合成**の上限なので、基礎が利益側にある
  (= 新しい構造 SL が基礎建値を越えている)普通の形で落ちる。仕様 §6-2 のとおり
  合成リスクへ差し替えた。落ちる場所が claim の**後**なので、A の引き金でもあった。
* **合成リスクから成行の滑りが落ちていた**(high・money-math)。単一脚の経路は昔から
  `(|現在値 − SL| + 滑り緩衝) × 枚数` で見積もっていたのに、合成へ差し替えたとき緩衝の
  項が消えていた —— **成行が不利側へ滑った分だけ上限を超えうる**(追撃 30 枚・2pt で
  $120、$500 上限の 24%)。基礎はもう約定済みで滑るのは**追撃脚だけ**なので、合計距離へ
  一律に足す旧式より正確に当てられる: `pyramid.adverse_add_price(side, price)` が契約の
  `marketOrder.maxDeviationPoints` を不利側(SELL は安く・BUY は高く)へ寄せ、engine /
  `order.py` / Worker がその価格で上限を見る。`.secrets` の `MARKET_SLIPPAGE_PT` を
  使わないのは Worker が読めず三重検証が割れるため(単一脚の経路は従来どおり)。
* **C) Worker が「上限で刻んだ追撃」を拒否していた**。門が `addQty == qty − baseQty` の
  完全一致を要求していたので、`_fit_to_cap` が刻んだ追撃は必ず claim を拒否される
  = **刻む機構が丸ごと死んでいた**。減らす方向(1 以上・目標との差以下)だけ許す。
* **D) 未解決の HALT の下でも追撃を出していた**。`_has_halt` のゲートは FLAT 経路に
  しかなく、open_qty>0 の枝はその手前で return する。追撃は**新規 ENTRY** なので、
  送信結果が不明なまま残る HALT の下で新しい成行を出してはならない(管理と撤退は従来
  どおり HALT の影響を受けない)。
* **E) 送信後検証が失敗した合成 MODIFY を再凍結していなかった**。送信は通っている
  = `cancelandbracket` は走り、凍結していた対はもう無い。HALT を書いて戻ると次の周期が
  全脚 CLOSED と読み、**HALT の上に所有権喪失が重なる**。`_freeze_consolidation` を
  通してから HALT を書く。
* **F) 統合のたびに `initialStop` をトレール後の SL で上書きしていた**。
  `trade_journal.build_trade_result` は `plan["initialStop"]` を R の分母にするので、
  **リスクが縮んだことにされて R が実際より大きく記録される**(スコアカードが甘くなる)。
  単一プランは `initialStop` を動かさない(トレールは `MANAGEMENT_SENT` の `action.sl`)
  ので合成もそれに合わせ、張り替えた保護水準は `consolidation.sl` /
  `pyramid.protectiveStop` に持つ。R78 修復が戻す「直前の stop」もそちらから引く。
* **G) 見送り注記が毎周期 1 行ずつ積まれていた**。台帳キーに `entryKey` を使っていたが
  これはサイクルごとに変わる。見送りは口座+銘柄の安定キーで 1 行に畳み、WAL を閉じる
  終端印だけ WAL と同じ `entryKey` にする。
* **H) 追撃の CONSUME が執行契約を見直していなかった**。CLAIM から CONSUME までは最大
  180 秒あり、その間にシナリオが期限切れになる・イベント封鎖が始まることがある。通常
  経路は `display.orderable` で見直しているので、追撃も同じ門を通す(外すのは建玉・
  注文・単体リスク = 合成リスクで置き換え済みの分だけ)。
* **I) コミットできないまま回廊に居続けても黙っていた**。送ったのに経路 identity を
  束縛できない状態が続くと、建値移動もトレールも掛からない建玉が一日残る(R74/R78 と
  同じ形)。600 秒を越えたら 1 度だけ HALT を記録して人へ見せる —— 回廊は残るので撤退と
  KILL は引き続き動く。
* **統合済みの合成建玉に R52 の一過性 FLAT 保護が効かなかった**。統合後の 1 組は
  **親を持たない** OCO 兄弟なので `brokerParentId` では辿れず、`_owned_brackets_active`
  が「所有ブラケット無し」と読んで、生きている建玉に FLAT 行を書きうる(= 管理が丸ごと
  落ちる、R52 そのものの事故)。`consolidationPair.orderIds` / `consolidation.orderIds`
  は id 自体が「建玉を守っている注文」の証拠なので、直接照合する。

* **同じシグナルの再掲で建て増していた**(critical・レビュー後に自分で発見、2026-09-13)。
  `decisionId`(= `scenarioId`)は msnr_gate が「同じセットアップなら 3 分ごとに変わらない」
  ように作るハッシュで、実サイクルでは同じ構造が**最長 14 連続**で再武装していた。
  ところが追撃の重複防止は `entryKey`(`marketCycleId` を含む = 毎周期変わる)しか
  見ていなかった。結果、(1) **基礎を作ったシグナル自身**が次の周期も ARMED のまま出て、
  ULTRA 枚数の焼き直しで今の建玉との差が出れば `--confirm` で建て増す、(2) 一度足した
  シグナルが再掲されると二度目を足す(`maxAdds` を 1 つのシグナルで食い潰す)——
  再現テストで両方とも実際に送信まで進むことを確認した。R83 の定義は「同方向の
  **新規**シグナル」。`_pyramid_consumed_decisions` が「このトレードで既に建玉を作った・
  足した決定」を所有プラン・境界以降の台帳・送信まで進んだ WAL・影運転の
  `PYRAMID_DRYRUN` から集め、含まれる決定は評価対象にしない。送信前に落ちた claim は
  何も出していないので消費しない(次の周期に再挑戦できる)。統合で 1 本に畳むときは
  `sourceScenarioIds` に全決定を残す。
  **影運転も同じ穴を持っていた**: 同じ決定の判定を毎周期出し直し、しかも連続行の重複
  排除が「別の決定の同じ理由(PYRAMID_ADD)」まで潰していたので、**台帳を見るだけでは
  この穴に気付けなかった**(注記にだけ出ていた)。重複排除を決定ごとにした。
固定は上記のテスト群に加えて `tests/test_r52_transient_flat.py`(統合済みの対)、
`tests/test_r84_order_gates.py`(滑りの分だけで枠を割る幾何)、
`tests/test_r84_worker_conformance.py`(滑りを当てた価格の Python / Worker 一致)、
`tests/test_r84_wal_transitions.py`(同じ決定の再掲・二度目・影運転の台帳)。

**受け入れて直さなかった所見**(理由つき):

* `pyramidSupersedable` は建玉を特定の claim に帰属させられない —— `validatePosition` が
  `accountId` を落とすため。同一 symbol / 同一 scope で動く前提の引き継ぎであり、
  誤って引き継いでも枚数・向き・等級の門は全部通る必要がある。
* 保有サイクルごとに DO の状態取得が 1 回増える。追撃は建玉があるときしか評価しない
  ので上限は 3 分に 1 回で、`cf-do-free-tier-rows-read-1101` の閾値には遠い。

### 人がやること(残り)

**2026-09-13 ユーザー決定: 出口条件は日数ではなく件数で区切る。** §11 M0 の「1 週間」は
暦の長さであって確認の量ではない(追撃のシグナルが出なければ 1 週間でも 0 件で、何も
確かめていない)。しかも影運転で確かめられるのは**判定**だけで、ブローカーの応答・
コミット・統合は実際に送らないと確かめられない。

1. **Worker をデプロイする**(`cd cloudflare && npm run deploy`。人が叩く)。デプロイする
   まで追撃 claim は Worker に拒否され、engine は `PYRAMID_SKIPPED` で安全に素通りする
   —— 順序依存は無い。デプロイは通常エントリーの claim 経路にも触るので、**追撃なしの
   普通のトレードが 1 回、エントリーから決済まで問題なく回る**(claim が詰まらない)ことを
   先に確かめる。
2. **影運転で、別々の決定(`scenarioId`)の `PYRAMID_DRYRUN` を 3〜5 件、1 件ずつ手で
   確かめる。**向き・等級・枚数・合計リスク(滑り込み)・「同じ決定の再掲ではないこと」が
   全部正しいこと。件数が集まらない間は待つ(0 件の 1 週間は何も証明しない)。
3. **初回の実弾は最小構成で**: `maxAdds=1` / `minGrade="A+"` にして `dryRun=false`、
   人が画面を見ている時間帯に。送った後、台帳が `PYRAMID_CLAIMED → PYRAMID_SENT →
   コミット` と揃い、ブローカーの注文行と合っていること。そのトレードが統合・決済・
   戦績まで全段揃ったら(M4 の出口)、`maxAdds=2` / `minGrade="A"` へ戻す(M5)。

---

## 0. 何が壊れていて、なぜ従来の延長では直らないか

- `pyramid.evaluate()`(pyramid.py)は純関数として完成済みだが、**engine から一度も
  呼ばれていない**(2026-09-12 時点で参照は tests/test_r83_pyramid.py のみ)。
- 追撃後の建玉は CrossTrade/Tradovate が**ネッティング**する(2026-09-12 実測:
  4+6 → SHORT 10、平均建値は加重平均)。ところが `ownership_binder.bind()` は
  建玉枚数を凍結プランの `{0, TP1脚, RUNNER脚, 合計}`(ownership_binder.py:447
  `lifecycle_quantities`)との**集合照合**でしか認めない。追撃した瞬間に枚数が
  集合の外へ出て所有権が落ち、建値移動もトレールも FLATTEN も全部止まる
  (2026-09-12 02:15〜05:01 の 2時間45分がこれ)。
- 集合照合をトランシェ 2 本へ素直に拡張すると、認めるべき枚数は
  「各トランシェの脚の生死の組合せの和」= **2^脚数 の羃集合**になり、
  枚数だけからはどの脚が生きているのか一意に決まらない(TP1 脚同士が同枚数なら
  減少の帰属が曖昧)。枚数の列挙という発想自体が追撃と両立しない。
- 建玉を守る注文の張り替えは `cancelandbracket`(口座×銘柄の保護注文を**全部**取消して
  SL/TP **1 組**を張る)しか実証済み経路が無い(R80)。追撃時に全量を張り替える設計は、
  片脚拒否で裸のポジションを作る R78 の窓を追撃のたびに開くことになる。

## 1. 設計原則 — 3 本柱

### 1.1 加算型ブラケット(既存の保護に触れない)

追撃は**それ自体が完結した分割ブラケット付きエントリー**(新シグナルの構造 SL と
TP1/RUNNER)として送る。既存トランシェの OCO には**一切触れない**。

- ブローカーは建玉をネッティングするが**注文はネッティングしない**。追撃後の保護構造は
  「旧トランシェの OCO 対 + 新トランシェの OCO 対」の加算になり、全枚数が常に
  ブローカー側 OCO で守られている。
- 追撃時に `cancelandbracket` を使わない = 取消→新規の 2 段が存在しない = R78 の
  「取消だけ成立して裸」が**構造的に起きない**。
- 各トランシェは trade 1 と自己相似(同じ `--split-tp` 形、同じ route identity 束縛、
  同じ R76/R80 構造照合)。新しい照合器を発明しない。

### 1.2 構造導出枚数(枚数の列挙をやめ、脚の生死から導く)

合成プランでは `lifecycle_quantities` の集合照合を**廃止**し、脚ごとの三状態から
期待枚数を導出する。

```
脚の状態(トランシェの各脚、凍結 orderId / receipt / bracketOrderIds で判定):
  OPEN   : エントリー約定済み かつ ブラケット対が PRESENT(_bracket_structure / R80)
  CLOSED : ブラケット対が ABSENT(TP/SL どちらかが約定し OCO で対が消えた)
  INCONSISTENT : 片割れだけ残る・receipt 不一致・非 live など → 所有しない(fail closed)

期待枚数 expected_qty = Σ leg.qty for 脚 in OPEN
所有条件: broker position qty == expected_qty(遷移回廊中は §4.3)
```

- どの脚が閉じたかは**枚数の引き算ではなく注文行の消失**で決まる。TP1 同士が同枚数でも
  曖昧にならない。TP 約定・SL 約定・トレール後の決済、すべて「対の消失」という同じ
  観測に落ちる。
- 判定器は既存の 1 か所を使い回す: 対の生死は `ownership_binder._bracket_structure`
  (R76)と `broker_status.oco_sibling_pair`(R80)。**新しい構造判定器を書かない。**

### 1.3 追撃 WAL(二相コミットのプラン再凍結)

追撃は「送ってから考える」のではなく、**送る前に**遷移の前後像を台帳へ書く。

```
PYRAMID_CLAIMED  (送信前)  : prePlan / postPlanDraft / preSendOrderIds / baseQty を凍結
PYRAMID_SENT     (送信後)  : route envelope・route snapshot を追記
PYRAMID_COMMITTED(束縛後)  : 検証済み合成プランを plan として凍結(正本切替)
```

- どの地点でクラッシュしても、台帳の最後の PYRAMID_* 行が「どの段まで進んだか」を
  一意に示す。回復は各段に対して定義済み(§9)。推測で埋める余地を残さない。
- `_frozen_plan_record()` は `record["plan"]` を持つ行しか採用しない(autotrade_engine.py:1480)。
  **PYRAMID_CLAIMED / PYRAMID_SENT は `plan` キーを持たない**(`prePlan` /
  `postPlanDraft` に入れる)ので、コミット前に下書きが凍結プランとして選ばれる事故が
  型の上で起きない。コミット行だけが `plan`(=合成プラン)を持つ。

## 2. データ構造

### 2.1 合成プラン(planKind=PYRAMID_COMPOSITE)

`build_management_plan()` の出力互換フィールドを**すべて保持**した上で追記する
(position_card / Mini App / journal が読む top-level は壊さない)。

```jsonc
{
  // ---- 既存互換(単一プランと同じキー) ----
  "planKind": "PYRAMID_COMPOSITE",
  "entryKey": "<追撃シグナルの entry key>",      // composite の台帳キー
  "accountScope": ["<1口座>"],                    // ULTRA と同じく単一
  "symbol": "MNQU6", "side": "SELL",
  "qty": 10,                                       // 凍結時点の Σ OPEN 脚
  "entry": 29601.25,                               // 合成建値(pyramid.combined_entry)
  "initialStop": 29650.0,                          // governing stop(§5.1)
  "tp1": 29560.0,                                  // 最新トランシェの TP1(表示互換)
  "finalTarget": 29510.0,                          // 統合後 TP の方針値(§5.3)
  "trailDistance": 18.75,                          // |合成建値-governing stop|×TRAIL_R とnoiseFloorの大きい方
  "riskCapDollars": 3100.0,
  "riskCapSource": "ACCOUNT_DRAWDOWN_BUFFER",
  "ultra": true, "ultraDrawdown": 3100.0,
  "mode": "SPLIT_BRACKETS_TP1_RUNNER",
  "legs": [ /* 全トランシェの OPEN 脚を平坦化した写し(表示・journal 互換) */ ],

  // ---- R84 追記 ----
  "pyramid": {
    "addsDone": 1, "maxAdds": 2,
    "combinedEntry": 29601.25, "combinedRisk": 975.0,
    "governingStop": 29650.0,
    "flattenBeyond": 29505.0                       // 最遠 live runner 目標(§5.2)
  },
  "tranches": [
    {
      "trancheId": "T1", "entryKey": "<元シグナル>", "entry": 29612.5,
      "initialStop": 29660.0, "orderType": "MARKET",
      "legs": [
        {"id": "TP1", "qty": 2, "target": 29580.0,
         "orderId": "...", "receipt": "...",
         "bracketOrderIds": ["...", "..."], "bracketReceipts": ["...", "..."]},
        {"id": "RUNNER", "qty": 2, "target": 29530.0, "...": "同上"}
      ],
      "routeSnapshot": [ /* trade1 の凍結経路そのまま */ ]
    },
    {"trancheId": "T2", "entryKey": "<追撃シグナル>", "entry": 29593.5, "...": "同形" }
  ],
  "consolidation": null                            // §5.3 で {orderIds, receipts, qty, sl, tp}
}
```

トランシェの脚の `id` は既存の `TP1` / `RUNNER` を保ち、トランシェ間の区別は
`trancheId` で行う(`LEGS` タプルを増やさない)。**統合済みトランシェ**(§5.3 後に
さらに追撃した場合の T1)は `legs=[{"id": "RUNNER", ...}]` の 1 脚 +
`consolidationPair` を持つだけで、同じ三状態判定に乗る(自己相似)。

### 2.2 台帳レコード(.secrets/autotrade_ledger.jsonl)

| status | 書く時点 | 主なフィールド | `plan` キー |
|---|---|---|---|
| `PYRAMID_DRYRUN` | dryRun 中の判定注記 | `verdict`(pyramid.evaluate の返り値全体) | 無し |
| `PYRAMID_SKIPPED` | 評価したが add=False | `verdict.reason` | 無し |
| `PYRAMID_CLAIMED` | claim 成功・送信前 | `prePlan`, `postPlanDraft`, `baseQty`, `baseAvgEntry`, `preSendOrderIds`, `claimJournal` | **無し** |
| `PYRAMID_SENT` | order.py 送信後 | `routeState`, `routeSnapshot`, `result` | 無し |
| (status=`ENTRY_SENT`, action=`PYRAMID_COMMIT`) | 束縛成功 | `plan`(検証済み合成プラン) | **有り** |
| `HALT` | 不明・部分成功 | `reason`(既存と同形) | 無し |

- キーは追撃シグナルの entryKey。`_latest(records, entry_key, {...})` の重複防止集合に
  `PYRAMID_CLAIMED` を加え、**同一シグナルの追撃は一度だけ**(§5 の既存規律と同じ)。
- コミット行を status=`ENTRY_SENT` にするのは意図的: `_frozen_plan_record()` の採用集合
  (autotrade_engine.py:1520)にそのまま乗り、走査が最新の合成プランを自然に選ぶ。
  R71 の FLAT 境界・R74 の ENTRY_HALTED 打ち切りも変更なしで正しく働く。

### 2.3 execution_contract.json への追記(pyramid 節)

```jsonc
"pyramid": {
  "enabled": false, "dryRun": true,
  "trigger": "SAME_SIDE_SIGNAL", "minGrade": "A", "maxAdds": 2,
  // ---- R84 追記 ----
  "orderType": "MARKET_ONLY",       // v1 は成行のみ(§3.2)。指値追撃は禁止
  "minAddQty": 2,                    // ultra_split 可能な最小(比率分割の下限)
  "finalTargetPolicy": "LATEST_TRANCHE",  // 統合後 TP の方針(§5.3)
  "commitSettleAttempts": 8, "commitSettleDelaySec": 0.5  // 束縛の短い待ち(R52 実測値と同じ)
}
```

Python(pyramid.py / engine)と Worker は**同じ節**を読む(ビルド時 import)。

## 3. サイクル内の実行順序(autotrade_engine の変更)

### 3.1 トリガー(OPEN 管理の後段に追加)

`_reconcile_one()` の open_qty > 0 分岐で、次の**すべて**が真のときだけ追撃評価へ進む。
評価と合成は新規純関数モジュール **`tranche.py`**(§4/§5 の関数群)に置き、engine は
配線だけを持つ(pyramid.py と同じ分離方針)。

1. 所有権束縛が `OWNED_FULL` / `PARTIAL_FILL` 相当で成立し(合成プランなら
   `bind_composite` が owned)、`management_action` が **None**(建値移動・トレール・
   FLATTEN のどれも要らない周期)。管理と撤退が常に追撃より優先。
2. HALT なし・KILL なし・`manualHalt` なし(R82。`autotrade_enabled()` が先頭で
   落とすので追加コード不要)・blocking order なし。
3. `pyramid.enabled=true`(dryRun は verdict に載せて後段で分岐)。
4. 正本サイクルの封印: FLAT 経路と同じく `nqx_state.fetch_state_quiet()` →
   `_authoritative_cycle_seal(view, proposal)` を通し、封印済み authoritative
   scenario を得る。**bundle の提案を直接使わない**(publish 後の正本だけが等級・
   状態・ULTRA 枚数を持つ。R77 と同じ理由)。
5. `pyramid.evaluate()` を呼ぶ。入力の出所を固定する:
   - `owned_plan`: binder が返した ownedPlan
   - `scenario`: 封印済み authoritative scenario(state ∈ ARMED/ACTIVE、
     grade は contract の allowedGrades かつ pyramid.minGrade 以上)
   - `position`: 検証済み broker position(qty / avgEntry / side)
   - `adds_done`: 直近 FLAT 境界以降の PYRAMID_COMMIT 行数
   - `add_price`: `_fresh_price()`(R78。取れなければ bundle price)
   - `stop`: scenario.stop(新シグナルの構造 SL)
   - `target_total_qty`: scenario.qty(monitor_publish の ULTRA 焼き直し済み枚数。
     これが「ULTRA で合計建玉を引き直す」の実体 — 別計算を持たない)
   - `risk_cap_dollars`: min(scenario.executionContract.riskCapDollars,
     contract ultra.maxRiskDollarsPerAccount)
6. `add=False` → `PYRAMID_SKIPPED`(理由つき、1 サイクル 1 行)。
   `add=True` かつ dryRun → `PYRAMID_DRYRUN` を台帳とサイクル注記に出して終わり。
   `add=True` かつ qty < `minAddQty` → `PYRAMID_SKIPPED`(`ADD_QTY_BELOW_SPLIT_MIN`)。
7. 口座条件: 追撃先は**現建玉の口座 1 口座のみ**(合成プランの accountScope)。
   scenario の ULTRA 口座と建玉口座が違えば `PYRAMID_SKIPPED`(`ACCOUNT_MISMATCH`)。

### 3.2 送信(成行のみ・加算型ブラケット)

1. **WAL**: `PYRAMID_CLAIMED` を書く。`preSendOrderIds` = 現在の注文照会の全 orderId、
   `baseQty` / `baseAvgEntry` = 現建玉。`postPlanDraft` = `tranche.build_composite_plan()`
   の出力(§2.1。新トランシェの identity は空のまま)。
2. **Worker claim**: 追撃シグナルの entry claim を取る(§7 の pyramid 対応が前提)。
   claim payload に `pyramid: {baseQty, baseAvgEntry, positionGeneration, addsDone}` を
   載せ、DO 側の再検証と突き合わせる。claim 不成立は `PYRAMID_SKIPPED`(送信前なので
   HALT にしない)。
3. **order.py**: `_command_for_entry` と同形の引数に `--pyramid` /
   `--pyramid-base=<qty>@<avgEntry>` を足して一度だけ送る(§6)。**必ず
   `--market --last <観測値>`**。指値追撃は v1 で禁止(未約定追撃脚の取消手段が
   `--flatten` しかなく、保有中に叩くと**建玉ごと消える**ため。ここが v1 を成行に
   限定する唯一にして十分な理由)。
4. dry-run → `--confirm` の 2 段は既存 `_execute_action` のまま。

### 3.3 コミット(束縛 → 正本切替)

1. `PYRAMID_SENT` を書く(route envelope の snapshot 込み)。
2. 建玉と注文を再照会し、`postPlanDraft` の新トランシェへ route identity を焼き込む
   (成行なので R76 のブラケット子行束縛 / R72 の /fills 束縛をそのまま使う)。
3. `tranche.bind_composite()`(§4)で合成プラン全体を検証:
   期待枚数(= 旧 OPEN 脚 + 新トランシェ全脚)と建玉一致、全対 PRESENT、衝突なし。
4. 成立 → status=`ENTRY_SENT` / action=`PYRAMID_COMMIT` で合成プランを凍結。
   以後の全サイクルはこのプランが正本。`_observe_position_generation` は avgEntry
   変化で**世代を進める**ことがあるが、コミット時の束縛が新世代の ownership を
   焼き込むので R71 境界とは衝突しない(FLAT 行は書かれない)。
5. 束縛が数秒で揃わないのは正常(送信直後の行未出現。R52 実測)。
   `commitSettleAttempts × commitSettleDelaySec` の短い待ちで再照会し、
   なお揃わなければ**そのサイクルは PYRAMID_SENT のまま終わる**(次サイクルと
   fill_watch の reconcile が再試行)。この間の所有権は §4.3 の回廊が守る。

### 3.4 失敗系の遷移表

| 地点 | 観測 | 記録 | 次サイクルの動き |
|---|---|---|---|
| claim 前 | evaluate=False | PYRAMID_SKIPPED | 再評価(新シグナルで) |
| claim | 拒否/不達 | PYRAMID_SKIPPED | 再評価 |
| 送信 | dry-run 拒否 | PYRAMID_SKIPPED(理由) | 再評価 |
| 送信 | live 失敗が**明確**(HTTP エラー・全脚 reject) | HALT | §9 の不在証明で自動回復 |
| 送信 | 部分成功/不明 | HALT + PYRAMID_SENT(判明分) | §9。自動再送**禁止** |
| 束縛 | 数周期越えて未成立 | PYRAMID_SENT のまま注記 | 回廊内なら保持、外なら HALT |
| コミット後 | — | ENTRY_SENT(PYRAMID_COMMIT) | 通常管理(§5) |

## 4. 所有権束縛の合成経路(bind_composite)

### 4.1 分岐と不変条件

- `ownership_binder.bind()` は**一行も変えない**。engine が
  `plan.get("planKind") == "PYRAMID_COMPOSITE"` のときだけ
  `tranche.bind_composite(plan, position, orders, ...)` を呼ぶ。
  単一プランの実戦検証済み経路を追撃実装の巻き添えにしない。
- fail closed は既存と同じ: INCONSISTENT・identity 欠落・口座/銘柄/方向不一致・
  receipt 衝突は所有しない。**構造が読めない建玉を枚数の辻褄で救済しない。**

### 4.2 判定手順

1. 口座・銘柄・方向・verified の前提検査(既存 bind と同一)。
2. 各トランシェ各脚を三状態に落とす(§1.2)。判定材料は脚の凍結
   `orderId / receipt / bracketOrderIds / bracketReceipts` と現在の注文行のみ。
   実装は `ownership_binder._bracket_structure()` をそのまま呼ぶ
   (public エイリアス `bracket_structure()` を追加して import する)。
   統合済みトランシェは `consolidation.orderIds` を `oco_sibling_pair` で照合する。
3. `expected_qty = Σ OPEN 脚 qty`。`position_qty == expected_qty` を要求。
4. 全脚 CLOSED(expected 0)なのに建玉が残る → 所有しない(既存の
   「bracket-verified legs have no live structure」と同じ帰結)。
5. 成立時は ownedPlan に per-leg 状態(`legStates`)と現世代 ownership を焼き込んで返す。

### 4.3 遷移回廊(PYRAMID_SENT 中だけの緩和)

未コミットの PYRAMID_SENT が最後の PYRAMID_* 行であるときに限り:

```
base_expected(旧 OPEN 脚の合計) <= position_qty <= base_expected + add_qty
```

を「所有(遷移中)」として認め、管理は **FLATTEN 判定のみ**(MODIFY・追撃の再評価は
しない)。回廊の外の枚数は従来どおり所有しない。回廊は**コミットで閉じる** —
恒久的な緩和ではなく、WAL の 2 行に挟まれた区間にだけ存在する。

## 5. 管理位相機械(management_action_composite)

### 5.1 governing stop

- 合成プランの `initialStop` = **最新トランシェの構造 SL**(pyramid.evaluate が
  リスク計算に使った stop と同一)。R83 のリスク算術(合成建値→この stop×合計枚数)と
  engine の挙動を一致させる。
- 旧トランシェのブローカー側 OCO SL はそのまま残る。governing より狭ければ先に
  約定して脚が閉じるだけ(構造導出枚数が吸収)、広ければ engine の governing
  FLATTEN が先に走り OCO は保険になる。**どちらでも実損 ≤ 凍結 combinedRisk。**

### 5.2 位相と許可される手

```
ACCUMULATION : TP1 脚が 1 本でも OPEN
  - MODIFY 禁止(cancelandbracket は全保護を消すため、TP1 を殺さずに張り替える
    手段が無い。CLAUDE.md §4-1「TP1 を消さない」を合成でも守る)
  - FLATTEN のみ: 価格が governing stop を突破 / 価格が flattenBeyond
    (最遠 live runner 目標)を超えて建玉が残る / forceFlatten / KILL / session cutoff
  - 追撃の再評価は可(§3.1 の条件下)
  - TP1 約定済みトランシェの runner は自分の構造 SL の OCO が守っている。建値移動は
    次位相まで**明示的に見送り**、注記 `pyramid: BE deferred (TP1 legs live)` を毎周期出す
RUNNERS      : 全 TP1 脚が CLOSED、RUNNER 脚が 1 本以上 OPEN
  - 最初の MODIFY = 統合(§5.3)。以後は通常の runner 管理
CLOSED       : expected 0 → journal へ(§8)
```

### 5.3 統合(consolidation)

全 TP1 解決後、既存の建値移動/トレール条件(`management_action` の BE 床・単調性・
protective 判定をそのまま流用。entry := 合成建値)が最初に MODIFY を要求した周期に、
一度だけ全量を張り替える:

- 送信は既存の `--modify` 経路そのもの(`cancelandbracket`、qty=現建玉、
  sl=BE床/トレール値、tp=`finalTarget`)。R78 の stop バッファ・同周期修復・
  R80 の OCO 兄弟照合・R52 の構造照合を**全部そのまま**通る。
- `finalTarget` は `finalTargetPolicy=LATEST_TRANCHE` = 最新トランシェの runner 目標
  (最新シグナルの幾何が最新の相場観。FARTHEST は方針値の差し替えで選べる形にしておく)。
- 成功したら合成プランを更新して再凍結(action=`PYRAMID_CONSOLIDATED`、
  status=`ENTRY_SENT`): `consolidation={orderIds, receipts, qty, sl, tp}`、
  トランシェ群は 1 脚の統合トランシェへ畳む。以後の binder 照合は
  `oco_sibling_pair` 1 組(既存 runner 管理と完全に同形)。
- 統合後のトレールは既存コードがそのまま面倒を見る(plan の qty / RUNNER 脚 qty を
  統合値に揃えておけば `management_action` の既存分岐に一致する)。

### 5.4 位相判定の実装

`tranche.phase(plan, leg_states)` の純関数。判定材料は §4 の三状態だけで、
価格を使わない(R35 の教訓: 約定の事実は枚数と構造で確定し、価格に再確認させない)。

## 6. order.py の変更(--pyramid ゲート)

新フラグ(すべて `--opt=value` 形。R52 の argparse 罠):

- `--pyramid` : 追撃モード。`--split-tp` / `--market` / `--last` / `--ultra` /
  `--ultra-drawdown` / 単一 `--accounts` を必須にする。
- `--pyramid-base=<qty>@<avgEntry>` : engine が観測した基礎建玉。

ゲートの差し替え(それ以外の既存ゲートは**全部そのまま**):

1. `require_verified_flat` を呼ばない。代わりに `require_verified_pyramid_base`:
   対象口座の position が verified・side が `--side` と同方向・qty が
   `--pyramid-base` の宣言と一致。1 つでも違えば送信前に ERROR で落ちる
   (engine の観測と送信瞬間の実態がズレた周期は見送りになるだけ)。
2. リスクゲートを合成リスクに差し替え: `pyramid.combined_risk(baseQty, baseAvgEntry,
   addQty, last, sl, POINT_VALUE) <= --ultra-drawdown`(pyramid.py を import して
  **同じ関数**で検査する。engine / order.py / Worker の三重検証を同一算術にする)。
3. 枚数上限は**追撃後の合計**へ掛ける: `baseQty + addQty <= ultra.maxQtyPerAccount`。
   addQty 自体も `ultra_split(addQty)` が成立すること(minAddQty)。
4. SL/TP の向き・滑り・tick 整合・claim consume は既存のまま。**新しい門は dry-run
   の return より前に置く**(R52 の教訓)。

## 7. Worker の変更(state_machine.js)— デプロイは人に依頼

`applyEntryClaimEvent` の CLAIM 分岐(state_machine.js:1731 付近):

1. payload に `pyramid` オブジェクトがあり、かつバンドル済み contract の
   `pyramid.enabled=true` のときだけ **pyramid claim** として扱う。
2. `POSITION_OPEN` blocker(state_machine.js:305)を免除する条件(全部 DO 自身の
   state で再検証。クライアント申告を信じない):
   - `state.position` が OPEN・銘柄一致・**scenario と同方向**
   - `state.position.qty == payload.pyramid.baseQty`
   - `payload.pyramid.addsDone < contract.pyramid.maxAdds`
   - grade が `contract.pyramid.minGrade` 以上
3. リスク検査を合成リスクへ差し替え: `combinedRisk(baseQty, baseAvgEntry(DO の
   position 値を優先), scenario.qty - baseQty, scenario.entry, scenario.stop)
   <= min(frozenCap, ultra.maxRiskDollarsPerAccount)`。addQty <= 0 は拒否。
   算術は pyramid.py §combined_risk の逐語移植とし、conformance テスト(§10)で固定。
4. executionIntent に `pyramid: {baseQty, addQty}` を焼き込む(intent hash が追撃で
   あることを固定し、order.py の consume 検証に乗る)。
5. **stale release の追撃対応**: 既存の `staleEntryClaimReleasable` は建玉 FLAT を
   要求するため、追撃 claim が置き去りになると**保有中は永久に解けない**。追撃 claim
   に限り解放条件を「staleReleaseSec 超過 + position.qty == 凍結 baseQty(増えていない
   = 追撃は届いていない) + **intent と同方向の**未終端注文行がゼロ」に差し替える。
   逆方向の行(基礎建玉のブラケット子)は解放を妨げない。

デプロイと動作確認は従来どおり人が行う(`npx wrangler deploy` は分類器が止めるため。
2026-09-04 の入替時と同じ手順)。engine 側は Worker 未対応の間、claim 拒否 →
`PYRAMID_SKIPPED` で安全に素通りする(順序依存が無い)。

## 8. fill_watch / trade_journal / 表示

- **fill_watch**: 変更不要。枚数変化 → reconcile 呼び出しの既存動作が、追撃の
  約定検知・TP1 検知・統合トリガーをそのまま 5 秒粒度に載せる。
- **trade_journal(R56)**: 決済価格の帰属候補に合成プランの全トランシェ脚
  (各 TP1/RUNNER/構造 SL)+ `consolidation.sl/tp` + トレール後 SL を加える。
  /fills の orderId → トランシェ脚の帰属は R72 の束縛済み identity で解決する。
  どの水準にも触れない減少の**保留規律は不変**。result には
  `pyramid: {addsDone, tranches: [entry, qty]}` を焼き込み、スコアカードの
  R48 タグに `PYRAMID_ADD`(**記録専用**。採点重み付けはしない — B 等級と同じく
  実測が貯まるまでエッジを語らない)を足す。
- **Mini App / position_card**: 合成プランは top-level 互換キー(§2.1)を保つので
  最低限は壊れない。トランシェ内訳の表示は別 R(任意マイルストーン)。
- **nqx_cycle.report_line(R77)**: 変更不要。追撃の注記は reconcile notes 経由で
  行末に載る。

## 9. HALT と回復

- 追撃送信の HALT は**保有中**に起きるため、既存 ENTRY 回復の「ブローカー空の不在証明」
  (FLAT 前提)が原理的に通らない。追撃専用の不在証明を engine に足す:

```
pyramid 不在証明 =
  position.qty == PYRAMID_CLAIMED の baseQty(増えていない)
  かつ 現注文照会の非終端行の orderId 集合 ⊆ preSendOrderIds(新しい行が無い)
```

  成立 → `ENTRY_RECOVERED`(既存と同形)を追記して HALT を解く。claim は §7-5 の
  DO 側 stale release に委ねる(エンジンは自己申告しない。R22 の規律)。
- 証明が作れない(枚数が増えた・未知の行がある) → 実際に部分的に入っている可能性。
  自動では解かず、人が建玉を照会して `--clear-halt`(既存 R52 手順)。
- 回廊(§4.3)があるため、**HALT 中も基礎建玉の FLATTEN 判定と KILL は生きている**
  (HALT が塞ぐのは新規と追撃だけ。既存の原則どおり)。

## 10. テスト(実装と同時に書く)

- `tests/test_r84_tranche_states.py` : 三状態と期待枚数。対 PRESENT/ABSENT/
  INCONSISTENT × トランシェ 1〜3 本、TP1 同枚数の曖昧ケースが**構造で**一意に
  解けること、全 CLOSED + 建玉残の fail closed。
- `tests/test_r84_bind_composite.py` : 回廊の境界(base-1 / base / base+add / base+add+1)、
  統合後の oco_sibling_pair 照合、世代交代(avgEntry 変化)を跨ぐ束縛。
- `tests/test_r84_phase_machine.py` : ACCUMULATION で MODIFY が出ないこと、
  governing stop / flattenBeyond の FLATTEN、全 TP1 解決 → 統合 MODIFY 1 回、
  統合後トレールが既存 runner 管理と一致すること。
- `tests/test_r84_wal_transitions.py` : §3.4 の遷移表を 1 行ずつ。特に
  「PYRAMID_CLAIMED/SENT が `plan` キーを持たない」「コミット行だけが
  `_frozen_plan_record` に選ばれる」「idempotency(同一 entryKey の二度目は skip)」。
- `tests/test_r84_order_gates.py` : `--pyramid` の必須フラグ、base 不一致 ERROR、
  合成リスク超過、合計枚数上限、門が dry-run return より前にあること。
- `tests/test_r84_recovery.py` : 不在証明の成立/不成立、HALT 中の FLATTEN 継続。
- `tests/test_r84_worker_conformance.py` : pyramid.py の combined_risk /_fit_to_cap と
  Worker 実装の数値一致(test_execution_contract_conformance.py の形式)。
- 既存回帰: `python tests/run_all.py` 全通し。**単一プラン経路のテストに 1 件も
  手を入れずに通ること**(bind() 無改変の検証)。

## 11. ロールアウト(この順で。各段の出口条件を満たすまで次へ進まない)

| 段 | 内容 | 出口条件 |
|---|---|---|
| M0 | engine に評価だけ配線(`PYRAMID_DRYRUN`/`PYRAMID_SKIPPED` 注記)。contract は enabled=true / **dryRun=true** に変更 | 実サイクルで同方向シグナル時に verdict が注記に出る。誤検知(逆方向・等級不足での add=True)ゼロを 1 週間 |
| M1 | tranche.py(三状態・回廊・位相・合成)+ 全純関数テスト | run_all 緑。既存テスト無改変 |
| M2 | order.py --pyramid + engine 送信/WAL/コミット配線(live=false の提案モードで実機確認) | dry-run 提案が実サイクルで正しい枚数・リスクを出す |
| M3 | Worker 変更(人がデプロイ)+ conformance + 実機で claim〜dry-run 全通し | pyramid claim が通り consume まで届く(--confirm なし) |
| M4 | **dryRun=false**。ただし `maxAdds=1`・minGrade は当面 `A+` に絞って初弾 | 初回の実追撃で: 束縛→COMMIT→(TP1)→統合→トレール→決済→journal まで台帳が全段揃う |
| M5 | maxAdds=2 / minGrade=A(R83 のユーザー決定値)へ戻す。CLAUDE.md R83 節の「未了」文をこの文書への参照に差し替え | スコアカードに PYRAMID_ADD タグが載る |

## 12. 禁止事項

- ACCUMULATION 位相での `cancelandbracket`(TP1 を消す全量張り替え)。
- 指値での追撃(v1)。未約定追撃の取消手段が撤退経路と分離できるまで成行のみ。
- トランシェ単位の部分 FLATTEN(実証済み経路が無い)。FLATTEN は常に口座全量。
- 構造 INCONSISTENT を枚数の辻褄・平均建値の近さで救済すること。
- PYRAMID_* の HALT 後の自動再送。回復は §9 の証明経由のみ。
- `lifecycle_quantities` 型の枚数集合を合成プランに持ち込むこと(退行の芽)。
- dryRun=false を M3 完了前に立てること。`enabled=false` の間に評価コードを
  呼ぶこと(`pyramid.settings()` の既定は安全側 — それを信頼して省略しない)。
