# 限月ロールの手順(R102、2026-09-15)

正本は `execution_contract.json` の `contract` ブロック。`python contract.py --status` が
満期までの日数と、NQX_SYMBOL を別々に持っている場所の一致を機械で示す。
**FLAT のときだけ**行う(建玉があると管理の参照価格と発注先がずれる)。

## 0. 前提

- 監視ループを止める(`/loop` を止め、`python autotrade_arm.py --status` で AUTO を確認)。
- `python broker_status.py --account <口座> --json` で全口座 `qty=0`、未約定注文なし。
- `python contract.py --status` を見て、今の限月と `next` を確認する。

## 1. 正本を差し替える

`execution_contract.json` の `contract`:

```json
"symbol": "MNQZ6",
"tvSymbol": "CME_MINI:MNQZ2026",
"expiry": "2026-12-18",
"next": {"symbol": "MNQH7", "tvSymbol": "CME_MINI:MNQH2027", "expiry": "2027-03-19"}
```

`expiry` は最終取引日(第 3 金曜)。`python contract.py --status` の `third Friday ok` で検算される。

## 2. NQX_SYMBOL を持つ場所を全部同じ値にする

`python contract.py --status` の `sites` に出る行が全部 `OK` になるまで:

| 場所 | 直し方 |
|---|---|
| `.secrets/nqx_cloud.env` `NQX_SYMBOL` | 値を書き換える |
| `.secrets/crosstrade.env` `NQX_SYMBOL`(あれば) | 同上 |
| `monitor_config.json` `symbol` | 同上 |
| `cloudflare/wrangler.toml` `NQX_SYMBOL` | 書き換えて **`cd cloudflare && npm run deploy`**(人が叩く) |
| `TRADING_CONTEXT.md` §2 シンボル行 | 表示だけ。正本を指す文言のまま値を直す |

`nqx_cycle.py` は起動時に `contract_month.enforce_sites()` を通し、設定が食い違えば
`HALT: CONTRACT_SYMBOL_DISAGREE` で相場データを取る前に止まる。Worker の deploy 忘れは
Worker 側の scope 検査(限月変更で新規停止)が拾う。

## 3. TradingView の pane(人)

連続足 `MNQ1!` のままでよい。`tv_fetch` が毎周期 TradingView の symbolInfo(`front_contract`)で
「MNQ1! が今どの限月か」を解決し、発注先と一致しなければ `CHART_SYMBOL_MISMATCH` で BLOCK する。
**つまりロールの合図は TradingView 側が出す**: MNQ1! が次限月へ切り替わった周期から BLOCK が続くので、
その時点で §1〜§2 の順に発注先を切り替える(2026-09-15 は MNQ1! が 12 月限へ切り替わった日に 9 月限の
発注先へ 285pt 上の通過指値が飛んだ。今はここで止まる)。限月そのもの(`CME_MINI:MNQZ2026`)を表示しても
よく、その場合は厳密一致。`chart_set_symbol` を自動で叩かない(pane 構成が壊れる)。
切り替え後、`python tv_fetch.py` で `symbol_info.json` の `front_contract` が新限月であることを確認する。

## 4. 起動して確認する

1. `python contract.py --status` → 全行 `OK`、`entry: allowed`。
2. `python nqx_cycle.py` を 1 回 → 行頭に `contract: MNQZ6 (CME_MINI:MNQZ2026) exp 2026-12-18 (Nd) entry=ok`、
   BLOCKED が無いこと(チャートが MNQ1! なら `symbol=CME_MINI:MNQ1!` のままで、`.secrets/tv_raw/symbol_info.json` の
   `front_contract` が新限月)。
3. Mini App で AUTO を再武装する(限月変更で武装は切れる)。
4. `/loop mnq 3m` を再開する。

## 5. 満期の手前で起きること

- 満期まで `lastEntryDaysBeforeExpiry`(既定 3)日未満: `monitor_publish` が ARMED を WATCH に落とし
  (`contract expiry gate`)、engine は claim の前に `CONTRACT_EXPIRY_NEAR` で見送り、`order.py` も
  同じコードで拒否する。**管理と撤退は止まらない。**
- 満期を過ぎた: `CONTRACT_EXPIRED`。ロールしないと新規が出ない。
