# CrossTrade への問い合わせ(下書き・**未送信**)

作成 2026-09-19(R118)。**送信していない。** 送るかどうかと、いつ送るかは運用者の判断。

## なぜ要るか

20 口座へ同時に発注するときの下限が、**公式文書だけでは決まらない**。決まらない部分を
推測で埋めると、その推測の上に「5 秒目標は不可能/可能」という結論が乗ってしまう。

確認済み(`https://crosstrade.io/docs/api/rate-limiting`、2026-09-19 閲覧):

> All API requests, whether over HTTP or WebSocket, share a single rate-limit budget of
> **3 requests per second**

* 対象として挙がっているのは **HTTP REST(`/v1/api/*`)と WebSocket RPC(`action: "rpc"`)**
* バーストは 20、購読は別枠で約 20/分・バースト 5
* **受信フレームは枠を消費しない**("Incoming frames never count against any budget")
* 超過は HTTP なら 429 + `Retry-After`、WebSocket RPC はそのメッセージへのエラー応答
* 連続 10 回の違反で close code 1008 で切断

**このページに `/v1/send/` は一度も出てこない。** 我々の発注はそこへ POST している。
「All API requests」がそれを含むのかどうかで、20 口座の補充待ちの下限が
**14.0 秒**(含む)と **0.67 秒**(含まない)に分かれる —— つまり
**5 秒目標に届くかどうかがこの 1 点で決まる**。**実測するには本番の注文を作るしかない**
ので、こちらでは確かめられない。

もうひとつ、`https://crosstrade.io/docs/webhooks/advanced-options/rate-limiting` の
`rate_limit` が「似た命令(similar commands)をどれだけ通すか」と読めるが、**何をもって
「似ている」とするか**が書かれていない。20 口座へ同じ方向・同じ枚数・同じ SL の脚を
連続で送ると、それが 1 つの命令として間引かれる可能性がある(既定は 60 秒に 1 回)。
7 口座では現に通っているが、口座も TP も違うので「似ていない」判定になっているだけかも
しれない。

## こちらで確かめようとして、やめたこと

問い合わせる前に自分で決着させられないか検討した。**どれも採らない**:

| 案 | やめた理由 |
|---|---|
| `/v1/send/` へ GET を投げて 429 を見る | 文書上 webhook は POST だが、GET がクエリ付きで alert として解釈されない保証がない。**注文が生まれうる経路を試しに叩かない** |
| 壊れたペイロードを POST して拒否させ、枠の消費だけ見る | 同上。部分的に解釈される可能性を排除できない |
| `/v1/api/*` を意図的に使い切って 429 と `Retry-After` を観測し、上限の数字そのものを裏取り | 読み取りだけなので注文は生まれないが、**「連続 10 回の違反で close code 1008 で切断」**が文書に明記されている。API アクセスそのものを失う代償に見合わない |
| 別アカウントで試す | 上限が**利用者単位**なので、別アカウントでは同じ枠を測れない |

結論として、**注文を作らずに決着させる方法が無い**。だから問い合わせる。

## 送る文面(英語)

> **Subject:** Rate-limit scope for `/v1/send/` and the `rate_limit` similarity rule
>
> Hello,
>
> We run an automated futures strategy through CrossTrade on Tradovate, currently
> across 7 linked accounts, and we are sizing it up to 20. Each signal places two
> independent bracket legs per account (a TP1 leg and a runner leg), so 20 accounts
> means 40 POSTs to our webhook URL in one burst, plus the REST calls we make to
> verify positions and orders before and after.
>
> We want to size our client-side pacing correctly rather than discover the limits by
> tripping them, so three questions:
>
> 1. **Does the alert endpoint `https://app.crosstrade.io/v1/send/...` draw on the same
>    3 requests/second (burst 20) per-user budget as the REST API?** The rate-limiting
>    page says "All API requests, whether over HTTP or WebSocket, share a single
>    rate-limit budget", and lists the HTTP REST API and WebSocket RPC, but it does not
>    mention `/v1/send/`. If `/v1/send/` has its own budget, what is it?
>
> 2. **How is "similar commands" defined for the `rate_limit` option?** We do not set
>    `rate_limit` in our payloads. If a default applies, we need to know whether 40 legs
>    that differ only by `account` and `take_profit` count as similar to each other —
>    that is, whether any of them could be silently dropped or deferred.
>
> 3. **Is there a higher tier or an allowance we can request for the REST budget?** Our
>    pre-send and post-send verification is 6 REST calls per account per signal (a flat
>    check, an order-list snapshot, a fills snapshot, and one order-list re-read per leg
>    to bind the broker order id). At 20 accounts that is 120 calls, which is 34 seconds
>    of pure refill wait at 3/second before any network time. We have moved our
>    steady-state observation onto the WebSocket stream already (which we understand is
>    free for pushed frames), and we can source most of the pre/post-send checks from it
>    too — this question is only about the calls that remain.
>
> A related question if the answer to (3) is no: **is there any server-side way to place
> the same order into several linked accounts in one request?** We could not find one in
> the docs, and we would rather ask than assume.
>
> Thank you.

## 回答が来たら変えるところ

| 回答 | 変えるもの |
|---|---|
| `/v1/send/` は同じ予算 | `execution_contract.json` の `gateway.coordinator.commsBudget.sendCountsAgainstApiBudget` を `true` に。下限は N=20 で **14.0 秒**(Gateway 併用時)。**5 秒目標は届かない**ことが確定する。そのときは上位枠か口座グループ発注(下の 2 行)しか道が無い |
| `/v1/send/` は別予算 | 同じ欄を `false` に。下限は N=20 で **0.67 秒**(Gateway 併用時)。**5 秒目標は経路上は届く**(実測はまだ) |
| `rate_limit` が口座をまたいで「似ている」と判定する | 脚ごとに `rate_limit` を明示して間引きを無効化する必要がある。**無言で落ちる**経路なので、判明したら最優先で塞ぐ |
| 上位枠がある | 費用と枚数を見て判断。判断材料は `docs/R118_GATEWAY_INTEGRATION.md` §5 の表 |
| 口座グループ発注がある | 送信側も O(1) になる。設計をやり直す価値がある |

**回答待ちで実装を止めていない。** 現契約で達成できる最速は R118 で実装済みで、
未確認の部分は「どちらの読みでも成り立つ設計」(安全側=送信も予算を食う前提で確保)に
してある。回答が来たら上の 1 行を書き換えるだけで効く。
