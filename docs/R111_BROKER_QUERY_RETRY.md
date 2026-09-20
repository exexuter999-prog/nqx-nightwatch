# R111: 一過性の照会失敗でサイクルを落とさない

2026-09-18。`broker_status.py` のみ(deploy 不要・次のサイクルから効く)。

## 目的

`autotrade blocked: broker position is UNVERIFIED for <口座>` が繰り返し出て、
そのサイクルの新規・変更が全部止まっていた。**照会 1 本が一過性に落ちるだけ**で
経路全体が fail-closed になるのが原因。

## 何が起きていたか

発注先が 1 口座から 7 口座になり、1 サイクルの照会が **29 本**へ増えた(名簿 1 + 残高 7 +
建玉 7 + 注文 7 + fills ほか)。ところが `broker_status._get_json` には **retry も 429 の
扱いも無く**、1 本でも落ちれば `Unavailable` → `verified=false` → engine が fail-closed。

`fill_watch` は元から 429 を `rateLimitBackoffSec`(15 秒)待つ設計で、docstring にも
「3 分ループの照会と送信後照会を 429 で落とさないため」と書いてある。**守られていたのは
fill_watch 側だけで、守られる側のループには何も無かった。**

## 何が変わるか

### 1. 読み取り専用の GET だけ retry する

`_get_json` に有限回の retry を入れた。

| 失敗 | 扱い | 回数 |
|---|---|---|
| 429 / 500 / 502 / 503 / 504 | retry(`Retry-After` があれば尊重、5 秒で丸め) | 3 |
| 接続不能・タイムアウト | retry(1 回が最大 15 秒なので控えめ) | 2 |
| **400 / 401 / 403 / 404** | **retry しない** | 1 |

400 を retry しないのは意図的。CrossTrade は「スコープ外の口座」「口座から消えた注文 ID」に
400 を返し、**R53 の `_order_unresolvable` はそれが終端であることに依存している**。

**retry してよいのは冪等な GET だけ。** `_get_json` の呼び出し元は建玉・注文・残高・名簿・
fills の照会しかない。送信は `order.py` の別経路で、この関数を通らない ——
CLAUDE.md §7 の「部分成功・タイムアウト後の無条件リトライ」の禁止はそのまま。
`_post_json`(Tradovate 認証のみ)にも入れていない。テストで固定してある。

回数が尽きたら例外をそのまま上げる。呼び出し側は従来どおり `Unavailable` にして
**建玉が無いとは言わない**。fail-closed の性質は変えていない。

### 2. 照会の間引き(`_pace_request`)

同一プロセス内の照会に最小間隔 `HTTP_MIN_INTERVAL_SEC`(既定 0.35 秒 ≈ 2.9 req/s)を置いた。
`NQX_BROKER_MIN_INTERVAL_SEC` で上書きでき、0 で無効。

**実測: 29 本の照会で待ちの合計は 1.0 秒。** 照会 1 本の往復が約 1.37 秒あり、
既に 0.73 req/s しか出ていないので、間引きはほとんど発動しない。つまり 429 の原因は
**自前のバースト速度ではない**(fill_watch との合算か、CrossTrade 側の事情)。
間引きは将来レイテンシが縮んだときの床として置いてあるだけで、**効いている対策は retry**。

## 戻し方

`_get_json` の `while True` を消して 1 回だけ呼ぶ形へ戻す。間引きだけ切るなら
`NQX_BROKER_MIN_INTERVAL_SEC=0`(または `HTTP_MIN_INTERVAL_SEC = 0`)。

## 検証コマンド

```powershell
python tests/test_r111_broker_query_retry.py
python tests/test_broker_crosstrade.py
python tests/test_r53_unresolvable_order.py
python tests/test_account_balance.py
python tests/test_r102_guards.py
```

送信しない(`urlopen` を差し替える)。テストで多数の擬似照会を回すと間引きの待ちが乗るので、
計測系のテストは先頭で `broker_status.HTTP_MIN_INTERVAL_SEC = 0` にしてよい。

## 併せて分かったこと —— サイクル時間の余裕

7 口座での実測: ブローカー照会だけで **約 40 秒**、1 サイクル全体で **117 秒**
(3 分間隔の予算 180 秒。CVD 再取得が要る周期はさらに伸びる)。口座を増やすと
ここが先に詰まる。20 口座計画([[twenty-account-90day-plan]])では照会の一括化
(R87 の「一括照会」)が前提になる。
