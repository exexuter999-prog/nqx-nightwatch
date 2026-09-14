# -*- coding: utf-8 -*-
"""認証ロジックの検証: 許可ID以外は handle() に到達しないこと。"""
allowed = "999"
cases = [("999", True, "本人"), ("1000", False, "他人"), ("-999", False, "符号違い"),
         ("9999", False, "前方一致の別ID"), ("99", False, "部分一致"), ("", False, "空")]
print("chat_id      期待   実際   判定")
ok_all = True
for cid, expect, label in cases:
    actual = (cid == allowed)          # bot 内の判定式と同一
    mark = "OK" if actual == expect else "*** NG ***"
    if actual != expect: ok_all = False
    print(f"{cid:<12} {str(expect):<6} {str(actual):<6} {mark}  ({label})")
print()
print("★ 認証OK: 完全一致のみ通過" if ok_all else "★ 認証に穴あり")
