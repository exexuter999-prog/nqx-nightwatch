# R108: 消えた口座の ENTRY claim が startup recovery で新規を全部止めていた

2026-09-18。`autotrade_engine.py` のみの変更(Worker の deploy は不要)。

## 目的

口座がブローカーから消えた直後、その口座を `accountScope` に持つ古い ENTRY claim が
**毎周期あらゆる新規発注を止めていた**。Worker 側には既に解放経路(R69)があるのに、
engine がそこへ到達する前に `blocked` を返していた穴を塞ぐ。

## 何が起きていたか

2026-09-18 07:20、funded 口座 `LFF…0006` が CrossTrade から消え(口座は黙って消える: 4 度目)、
発注経路を資金提供前の評価 7 口座へ入れ替えた。09-17 06:13 に置かれた claim が残った:

```
state=CONSUMED  staleReleasable=true  accountScope=['LFF…0006']  claimedAt=2026-09-17T06:13:43Z
```

engine は毎周期こう出して止まった:

```
autotrade blocked: ENTRY startup recovery failed
({'reason': 'ENTRY_RECOVERY_CURRENT_BROKER_PROOF_INVALID'})
```

連鎖はこうなっている。

1. `_recover_entry_from_current_broker` は claim の口座へ建玉・注文を照会して
   「空である」ことを証明しようとする。
2. その口座はもう存在しないので、照会は永久に `verified=false` を返す
   (`requested crosstrade account is outside configured scope` / HTTP 400)。
3. よって理由は必ず `ENTRY_RECOVERY_CURRENT_BROKER_PROOF_INVALID` になる。
4. ところが `deferrable` は `ENTRY_CLAIM_RECOVERY_UNVERIFIED` だけを想定していたため、
   **CLAIM へ到達する前に `blocked` を返していた**。
5. DO 側の R69(`claimScopeVanishedFromBroker` → `staleEntryClaimReleasable`)は
   まさにこの状況のために作られていて `staleReleasable=true` を立てているのに、
   その解放は CLAIM 到達時にしか走らないので**一度も動かない**。

R40(identity を束縛できなかった経路)と R53(注文 ID が解決不能になった経路)で
「終端証明を構造的に作れない経路は DO へ委ねる」と決めたのと、同じ形の穴だった。
口座ごと消えた場合は identity による終端証明も同じく永久に作れないので、
identity の有無で分けない。

Mini App の SYSTEM タブが出す **`STALE SLOT`**(ENGINE ノードの WARN)がこの状態の目印。
`engineState()` は `claim.state ∈ {CLAIMED, CONSUMED}` かつ `staleReleasable === true` で
`STALE` を返す。「old reservation — clears on the next entry」と説明されるが、
この穴があると**次の entry 自体が永久に来ない**ので、自然には消えなかった。

## 何が変わるか

`autotrade_engine._claim_scope_vanished_from_broker(view, claim)` を新設し、
`deferrable` に 1 条件だけ足した。claim の `accountScope` の**全口座**が
ブローカーの口座名簿から消えていれば、回復の失敗理由に関わらず CLAIM へ進む。

判定器は Worker の `claimScopeVanishedFromBroker`(`cloudflare/src/state_machine.js`)の
写しで、条件は同じ:

* `accountScope` が空でない
* 名簿(`accounts.sync`)が `verified=true`
* 名簿が 900 秒以内(未来方向は 60 秒まで許容)
* scope の全口座が `(configured かつ not missing)` でも `unknown`(broker-only)でもない

**片方だけ緩めないこと。** engine が「解放してよい」と思った claim を DO が握り続けると、
理由の分からない `ENTRY_CLAIM_ALREADY_HELD` になる。

engine はこれで**何も解放しない**。CLAIM まで進むだけで、解放してよいかは DO が
自分の state(名簿の verified・鮮度・claim の年齢 900 秒・建玉 0・未終端注文なし)で
再検証する。名簿が無い・古い・未検証なら推測せず従来どおり止まる。

## 戻し方

`autotrade_engine.py` の `deferrable` の直後に足した 2 行を消す:

```python
if not deferrable and _claim_scope_vanished_from_broker(view, claim_view):
    deferrable = True
```

`_claim_scope_vanished_from_broker` は他から呼ばれていないので残しても無害。
戻すと、口座入替のたびに古い claim が新規を全部止める状態へ戻る。

## 検証コマンド

```powershell
python tests/test_r108_vanished_scope_recovery_defer.py
python tests/test_r22_recovery.py
python tests/test_r53_unresolvable_order.py
python tests/test_r40_multi_account.py
python tests/test_r84_recovery.py
```

いずれも送信しない。監視窓の中で回すときは `.secrets` を抜いた隔離コピーで
(テストが本番の台帳・CrossTrade へ落ちる穴は R92 で塞ぐ予定)。

Worker 側のミラーは `cloudflare/test/r69_vanished_scope_release.test.mjs`。

## 現場での見分け方

`python autotrade_arm.py --status` が `ARMED/LIVE` なのに毎周期
`autotrade blocked: ENTRY startup recovery failed` が出るときは、まず claim の
`accountScope` を見る:

```powershell
python -c "import json,nqx_state; v=nqx_state.fetch_state_quiet(); c=(v or {}).get('entryClaim') or {}; print(c.get('state'), c.get('staleReleasable'), (c.get('executionIntent') or {}).get('accountScope'))"
```

scope に今の `CROSSTRADE_ACCOUNTS` に無い口座が出ていれば、これ。
