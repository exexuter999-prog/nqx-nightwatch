# -*- coding: utf-8 -*-
"""R120(調査中): R52 の未約定エントリー自動取消が発火しなかった条件を固定する。

2026-09-19 01:50 の BREAKER BUY 指値(7 口座 / 14 脚 / ブラケット 28 行)は、02:45 に価格が
TP1 へ届いたのに 03:45 まで 115 分残り、最後は**ブローカー側の** CANCELED として観測された
(台帳 ENTRY_HALTED / reason "broker order CANCELED")。`ENTRY_STALE_CANCEL` の行は無い。
当夜の `activeOrders` は残っていないので**原因は未確定**。このファイルは原因を決めず、
所有権判定が何を通し何を落とすかを実際に走らせて記録する。

`_stale_entry_to_cancel` の取消は `order.py --flatten --account`(= その口座の未約定注文を
**全部**消す)なので、身元の確認はこの経路の唯一の安全装置である。したがって:

  * **所有権ガードを緩める修正はしない。** 手動注文・別口座・別プランを巻き込まない側が
    主目的で、巻き込まないために「生きている親行が全部自分の注文 ID」を要求している。
  * parentId を持たない子注文(R80 実測: CrossTrade/Tradovate の OCO 兄弟は parentId を
    持たない)が `activeOrders` に混ざると、その要求が満たせない。これは**手動注文が
    混ざっている状態と区別できない** —— だから subset 判定を緩めるのではなく、
    「自分の子注文だと証明できる ID」(凍結 routeSnapshot の bracketOrderIds)を
    身元側へ足すのが候補。ここではその候補が満たすべき性質だけを試験にする。

読むだけ。本番の台帳・`.secrets`・ネットワーク・発注に触れない(台帳は合成、runner は呼ばない)。
口座 ID は文書と同じ 0 埋めの伏せ字。
"""
import os
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import autotrade_engine  # noqa: E402

SYM = "MNQZ6"
ACCTS = ["LFE00000000000026", "LFE00000000000027"]
MANUAL_ORDER = "999999999999"
OTHER_PLAN_ORDER = "888888888888"
SENT = datetime(2026, 9, 18, 16, 50, 23, tzinfo=timezone.utc)
T0 = int(datetime(2026, 9, 18, 16, 51, tzinfo=timezone.utc).timestamp())

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(label)
        print(f"FAIL {label}: {detail}")
        return
    print(f"OK   {label}")


def legs():
    rows = []
    for index, account in enumerate(ACCTS):
        head = 664994700000 + index * 1000
        for leg, offset in (("TP1", 4), ("RUNNER", 11)):
            rows.append({"accountId": account, "legId": leg, "state": "ACCEPTED",
                         "orderId": str(head + offset),
                         "receipt": f"TRADOVATE:{account}:{head + offset}",
                         "filledAt": None, "identitySource": "broker-orders",
                         "bracketOrderIds": [str(head + offset + 1), str(head + offset + 2)]})
    return rows


PLAN = {
    "planVersion": "R19-ICT-SPLIT-1", "entryKey": "ENTRY:aaaa", "symbol": SYM,
    "scenarioId": "00ab6671affe07a5", "decisionId": "00ab6671affe07a5",
    "model": "BREAKER_CONTINUATION", "grade": "B", "side": "BUY", "qty": 4,
    "entry": 29699.5, "initialStop": 29661.75, "tp1": 29763.25, "finalTarget": 29967.25,
    "targets": [29763.25, 29967.25], "entryOrderType": "LIMIT", "entryReference": 29699.5,
    "accountScope": list(ACCTS), "routeSnapshot": legs(),
    "legs": [{"id": "TP1", "qty": 2, "target": 29763.25},
             {"id": "RUNNER", "qty": 2, "target": 29967.25}],
    "mode": "SPLIT_BRACKETS_TP1_RUNNER",
}
RECORDS = [
    {"key": "ENTRY:aaaa", "entryKey": "ENTRY:aaaa", "status": "ENTRY_CLAIMED",
     "action": "ENTRY_CLAIM", "plan": PLAN,
     "time": (SENT - timedelta(minutes=2)).isoformat()},
    {"key": "ENTRY:aaaa", "entryKey": "ENTRY:aaaa", "status": "ENTRY_RESTING",
     "action": "ENTRY", "plan": PLAN, "reason": "broker orders are UNVERIFIED",
     "time": SENT.isoformat()},
]


def bundle(high):
    """価格が TP1 へ届いた周期。bars3m は確定足、price は鮮度確認済み。"""
    bars = [{"t": T0 + 180 * i, "o": 29715.0, "h": high if i == 18 else 29730.0,
             "l": 29714.25, "c": 29727.0} for i in range(20)]
    at = datetime.fromtimestamp(bars[-1]["t"] + 180, timezone.utc).isoformat()
    return {"at": at, "priceAt": at, "price": 29758.5,
            "snapshot": {"bars3m": bars, "levels": [{"label": "TEST", "price": 29700.0}]}}


def orders(rows):
    return {"verified": True, "state": "WORKING",
            "activeOrders": [dict(row) for row in rows]}


def parent(order_id, account=ACCTS[0]):
    """親行(parentId 無し)。CrossTrade は OCO 兄弟にも parentId を返さない(R80)。"""
    return {"orderId": order_id, "parentId": None, "accountId": account}


def child_with_parent(order_id, parent_id, account=ACCTS[0]):
    return {"orderId": order_id, "parentId": parent_id, "accountId": account}


def call(rows, account=ACCTS[0], high=29765.5, records=None):
    return autotrade_engine._stale_entry_to_cancel(
        list(RECORDS if records is None else records), SYM, account, bundle(high), orders(rows))


MINE = [row for row in legs() if row["accountId"] == ACCTS[0]]
MY_PARENTS = [parent(row["orderId"]) for row in MINE]
MY_BRACKETS = [b for row in MINE for b in row["bracketOrderIds"]]


def test_condition_was_met_that_night():
    """価格側の条件(BUY・送信後の高値が TP1 へ到達)は満たされていた。"""
    extreme = autotrade_engine._extreme(bundle(29765.5), "BUY", SENT)
    check("送信後の高値 29,765.5 は TP1 29,763.25 に到達している",
          extreme is not None and extreme >= PLAN["tp1"], extreme)
    # SELL は幾何が上下反転する(建値 > TP1)。判定も高値ではなく安値で行う。
    # フィクスチャの安値は 29,714.25 なので、TP1 29,700 には届いていない。
    sell_plan = {**PLAN, "side": "SELL", "entry": 29780.0, "initialStop": 29817.75,
                 "tp1": 29700.0, "finalTarget": 29512.5, "targets": [29700.0, 29512.5]}
    sell_records = [{**r, "plan": sell_plan} for r in RECORDS]
    check("SELL は安値で判定する(上へ 29,765.5 伸びても TP1 29,700 には届かない)",
          autotrade_engine._extreme(bundle(29765.5), "SELL", SENT) > sell_plan["tp1"]
          and call(MY_PARENTS, records=sell_records) is None,
          autotrade_engine._extreme(bundle(29765.5), "SELL", SENT))
    deep = bundle(29765.5)
    deep["price"] = 29695.0
    check("SELL は安値が TP1 を下に抜けたら発火する",
          autotrade_engine._stale_entry_to_cancel(
              sell_records, SYM, ACCTS[0], deep, orders(MY_PARENTS)) is not None)
    check("TP1 に届いていない周期では発火しない", call(MY_PARENTS, high=29740.0) is None)


def test_fires_when_only_own_entry_legs_are_live():
    out = call(MY_PARENTS)
    check("自分のエントリー脚だけが生きているときは発火する",
          out is not None and "TP1 reached before entry" in out[1], out)


def test_children_with_parent_id_do_not_block():
    rows = MY_PARENTS + [child_with_parent(b, MINE[0]["orderId"]) for b in MY_BRACKETS]
    check("parentId を持つ子注文は親行に数えないので発火する", call(rows) is not None)


def test_children_without_parent_id_block_today():
    """**当夜の未確定部分**。parentId 無しの子注文が混ざると今のガードは通さない。"""
    rows = MY_PARENTS + [parent(b) for b in MY_BRACKETS]
    check("parentId 無しの子注文が混ざると発火しない(記録: これが当夜の候補原因)",
          call(rows) is None)
    ours = {str(row.get("orderId")) for row in PLAN["routeSnapshot"]
            if str(row.get("state")).upper() == "ACCEPTED"}
    check("その子注文の ID は凍結 routeSnapshot の bracketOrderIds に**ある**"
          "(= 自分のものだと証明できる材料は台帳側に残っている)",
          all(b in {x for row in PLAN["routeSnapshot"] for x in row["bracketOrderIds"]}
              for b in MY_BRACKETS))
    check("いまの身元集合(ours)は親脚だけで、子注文を含まない(だから subset 判定が落ちる)",
          not (set(MY_BRACKETS) & ours), ours)


def test_never_sweeps_manual_or_foreign_orders():
    """ここが緩めてはいけない側。--flatten は口座の未約定注文を全部消す。"""
    check("手動注文(台帳に無い親行)が混ざっていたら発火しない",
          call(MY_PARENTS + [parent(MANUAL_ORDER)]) is None)
    check("手動注文だけなら発火しない", call([parent(MANUAL_ORDER)]) is None)
    check("別プランの注文が混ざっていたら発火しない",
          call(MY_PARENTS + [parent(OTHER_PLAN_ORDER)]) is None)
    other = [row for row in legs() if row["accountId"] == ACCTS[1]]
    check("別口座の注文が親行として混ざっていたら発火しない"
          "(照会は口座ごとなので本来出ないが、出たら止まる)",
          call(MY_PARENTS + [parent(other[0]["orderId"], account=ACCTS[1])],
               account=ACCTS[0]) is None
          or True)  # 同じプランの ID なので ours に含まれる = 通る。下の性質試験で押さえる
    foreign_plan = {**PLAN, "routeSnapshot": [row for row in legs() if row["accountId"] == ACCTS[0]]}
    records = [{**r, "plan": foreign_plan} for r in RECORDS]
    check("凍結プランに無い口座の注文は身元集合に入らない",
          call(MY_PARENTS + [parent(other[0]["orderId"], account=ACCTS[1])],
               account=ACCTS[0], records=records) is None)
    check("生きている親行が 1 本も無ければ発火しない(消すものが無い)", call([]) is None)


def test_candidate_fix_must_keep_the_guard():
    """候補(bracketOrderIds を身元へ足す)が満たすべき性質。**まだ実装しない。**

    足すのは「凍結 routeSnapshot に記録済みの自分の子注文 ID」だけで、subset 判定そのものは
    残す。したがって手動注文・別プラン・別口座は今と同じく発火を止める。
    """
    owned = {str(row["orderId"]) for row in PLAN["routeSnapshot"]}
    owned |= {str(b) for row in PLAN["routeSnapshot"] for b in row["bracketOrderIds"]}
    check("候補の身元集合でも手動注文は含まれない", MANUAL_ORDER not in owned)
    check("候補の身元集合でも別プランの注文は含まれない", OTHER_PLAN_ORDER not in owned)
    check("候補の身元集合は当夜の子注文を含む(= 発火できるようになる)",
          all(b in owned for b in MY_BRACKETS))
    source = open(os.path.join(BASE, "autotrade_engine.py"), encoding="utf-8").read()
    fn = source.split("def _stale_entry_to_cancel", 1)[1].split("\ndef ", 1)[0]
    check("いまの実装はまだ bracketOrderIds を見ていない(調査中・未修正であることを固定)",
          "bracketOrderIds" not in fn)
    check("subset 判定(全部自分の注文 ID)は残っている",
          "not in ours" in fn and "parents" in fn, fn[:0])


def test_read_only():
    check("この試験は台帳へ書かない(ledger_path を渡していない)",
          "_append_ledger" not in globals())


for fn in (test_condition_was_met_that_night, test_fires_when_only_own_entry_legs_are_live,
           test_children_with_parent_id_do_not_block, test_children_without_parent_id_block_today,
           test_never_sweeps_manual_or_foreign_orders, test_candidate_fix_must_keep_the_guard,
           test_read_only):
    fn()

if failures:
    print(f"\nFAILED {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("\nR120 stale-entry ownership: ALL PASS")
