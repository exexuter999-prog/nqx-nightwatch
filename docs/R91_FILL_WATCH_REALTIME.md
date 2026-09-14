# R91 約定監視の常駐保証と高速化(fill_watch)

2026-09-15。ユーザー相談「fill_watch が止まっていると TP1 検知が 3 分遅れに戻る。リアルタイム検知に
アップグレードできるか」への回答。実装は `fill_watch.py`・`nqx_cycle.py`(1 行)、設定は
`execution_contract.json` の `fillWatch`、検証は `tests/test_r91_fill_watch_realtime.py`。

## 0. 結論

1. **この経路で本当のプッシュ(約定の即時通知)は使えない。** CrossTrade の WebSocket は
   NinjaTrader 8 口座だけで、Tradovate 口座は REST のみ・約定の送信通知も無い(§1)。
2. だから最速は「上限の中で照会を詰める + 止まらせない」。建玉がある間 1.5 秒、FLAT は 5 秒。
3. **実際の穴は間隔ではなく常駐していなかったこと。** R78 導入後、heartbeat は 09-11 02:20 の
   1 回だけで `lastTrigger` も空。TP1 検知はずっと 3 分遅れのままだった。R91 で監視ループが
   止まった fill_watch を毎周期起動し直せるようにした(契約 `fillWatch.autostart`)。
4. **2026-09-15 04:10 JST ユーザー決定で `autostart=true`。** 04:12:53 に `nqx_cycle` が起動(`startedBy=nqx_cycle`)、
   FLAT を正常に照会。定常の照会時間は FLAT(2 本)で中央値 1.24 秒(1.17〜1.31 秒、初回だけ接続確立で 5.7 秒)、
   429 は 0 回、ループの reconcile 中の一時停止も記録された。検知の最悪値は建玉ありで約 2 秒(照会 ≈0.6 秒 +
   間隔 1.5 秒)、FLAT で約 6 秒。検知後の建玉管理(Worker 同期・quote・reconcile・order.py)は別に数秒かかる。
   なお番号は、同じ夜に別セッションが「初期 SL の穴」を R90 として入れたため R91 とした。

## 1. ブローカー側の事実(CrossTrade 公式)

- WebSocket は NT8 の RPC・相場購読・P&L 配信だけ。Tradovate の `/v1/api/tv` は REST のみ。
  約定・建玉変化の外向き通知は無い(TradingView → CrossTrade の受信 webhook だけ)。
- API 上限は **利用者ごと毎秒 3 回、溜め 20 回**(トークンバケット)。REST と WebSocket で共有。
  超えると HTTP 429 + `Retry-After`。Tradovate 側の制限も `broker_rate_limited` の 429 で返る。
- Tradovate 用に全口座をまとめて返す `GET /v1/api/tv/accounts/snapshot` がある
  (残高・建玉・working order)。**応答の形は未実測**なので今回は使っていない(§5)。
- 出典: https://crosstrade.io/blog/tradovate-api-automation-rest-websocket-webhooks-mcp /
  https://crosstrade.io/docs/api/rate-limiting / https://crosstrade.io/blog/understanding-tradovate-api-rate-limits

照会 1 回の本数(`broker_status.query_position`): 建玉ありは 1 本、**FLAT は R41 の裏取りで 2 本**。

## 2. 変えたこと

| 項目 | R78 | R91 |
|---|---|---|
| 常駐 | 人が別端末で起動(実際は一度も常駐していなかった) | `autostart=true` なら `nqx_cycle` が毎周期 heartbeat を見て、止まっていれば切り離したプロセスとして起動し直す |
| 多重起動 | 防げない | `.secrets/fill_watch.lock` を生存中ずっと握る。2 つ目は終了コード 3 |
| 照会間隔 | 固定 5 秒 | 建玉あり `fastIntervalSec` 1.5 秒 / FLAT `idleIntervalSec` 5 秒 |
| 自前の上限 | なし | `maxRequestsPerSec` 1.0(ループの照会・送信後照会の分を残す)。口座数 × 本数で間隔を広げる |
| 429 | 未検証として指数待ちのみ | 失敗理由の 429 を見分けて `rateLimitBackoffSec` 15 秒以上空け、回数を heartbeat に残す |
| ループの reconcile 中 | 照会を続ける | ロックを**待たずに覗き**、握られていれば照会しない(1 秒後に覗き直す) |
| 表示 | alive / STALE | alive にモード(FAST / IDLE / BACKOFF / PAUSED)と 429 回数、自動起動の結果を同じ 1 行に |

変えていないこと: 検知の対象(口座ごとの建玉の枚数・方向の変化)、検知後の処理(`reconcile` を最大 2 回、
scenario 無しの bundle なので新規 ENTRY は出ない)、reconcile ロックの排他。

## 3. 自動起動の安全弁

- 起動は `supervise()` の中だけ。表示と起動の失敗で監視周期を止めない(例外を投げない)。
- 生きていれば起動しない。ロックを握ったまま heartbeat が止まったプロセスがあれば起動せず注記する
  (応答なし。人が止める)。
- 起動しても heartbeat が出ない状態が 3 回続いたら 1 時間は起動を止め、`.secrets/fill_watch.log` を
  見るよう注記する(設定誤りで 3 分ごとに起動し続けない)。
- 監視窓の外(05:45〜07:00)は周期が回らないので、その間に落ちたら 07:00 に起動し直す。
- Claude のツールから切り離したプロセスは、起動したツール呼び出しの終了後も生き残ることを実測済み
  (ダミープロセスで確認)。

## 4. 設定

```json
"fillWatch": {"version": "R91-FILL-WATCH-1", "autostart": false,
              "fastIntervalSec": 1.5, "idleIntervalSec": 5.0,
              "maxRequestsPerSec": 1.0, "rateLimitBackoffSec": 15.0}
```

- `autostart` は JSON の `true` のときだけ有効。範囲外・型違いの数値はその項目だけ既定値
  (fast 0.5〜60 / idle 1〜300 / rps 0.1〜2.5 / backoff 5〜300)。
- 手で起動する場合: `python fill_watch.py`(`--fast-interval` / `--interval` / `--max-rps` で上書き)。
- 止める: `autostart` を `false` にしたうえで、プロセスを止める(`.secrets/fill_watch_heartbeat.json` の pid)。

## 5. これ以上速くするには(今回はやっていない)

- **全口座スナップショット**(`/v1/api/tv/accounts/snapshot`): 口座数に比例しない 1 本の照会になる。
  R87 の多口座化ではこれが要る。応答の形を 1 回実測(読むだけ)してから実装する。
- **Tradovate 本体の WebSocket**(user/syncrequest): 本当のプッシュだが、API アクセスの契約と資格情報が要り、
  `broker_status` も記すとおりプロップ/評価口座には基本的にキーが出ない。
- **NinjaTrader 8 経由**: CrossTrade の WebSocket が使えるが、発注経路そのものの入れ替えになる。
- 検知後の反応(Worker 同期・quote・reconcile 内の照会と order.py)は数秒かかる。短縮は別途計測してから。
