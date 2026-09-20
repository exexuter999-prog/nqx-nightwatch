# R117: Broker Gateway —— 観測を O(N) から O(1) へ

2026-09-19。新規 `broker_gateway.py` + `tests/test_r117_broker_gateway.py`。
**既定 OFF**(`NQX_GATEWAY=1` のときだけ常駐)。既存経路は 1 行も変えていない。

指示は `docs/CLAUDE_20_ACCOUNT_LOW_LATENCY_DIRECTIVE_2026-09-19.md`。

---

## 1. 前提が変わった —— CrossTrade の WebSocket は Tradovate を押してくる

`fill_watch.py` の冒頭には「本当のプッシュは使えない。CrossTrade の WebSocket は
NinjaTrader 8 口座だけ」とある。**これは古い。** 2026-09-19 に公式仕様を読み、
この環境で実地検証した。

### 公式仕様(2026-09-19 閲覧)

| | |
|---|---|
| 接続 | `wss://app.crosstrade.io/ws/stream`、`Authorization: Bearer <key>` |
| Tradovate | `"origin": "tradovate"` で `orderUpdate` / `executionUpdate` / `positionUpdate` / `executionReportUpdate` |
| 購読 | `{"action":"subscribe","origin":"tradovate","events":["orders","executions","positions"]}` |
| **プッシュの枠** | **消費しない**("Incoming frames never count against any budget") |
| RPC の枠 | REST と共通。3 req/s・バースト 20・**利用者単位** |
| 購読の枠 | 別枠。約 20/分・バースト 5 |
| 接続数 | **利用者あたり 1 本**。2 本目は 1 本目を "Session active elsewhere" で切る |
| 順序 | 全フレームに `seq`。欠落は `dropped`。`{"type":"resync"}` で再取得要求 |
| 再取得 | `Tv_ListPositions` / `Tv_ListOrders` |

### この環境で検証したこと(推測ではない)

```text
接続 OK 0.83〜0.86 秒(既存の CROSSTRADE_KEY で認証成功)
subscribe → {"status":"subscribed","events":["orders","executions","positions"],"origin":"tradovate"}
status  → state=syncing → ready / resync フレームも受信
Tv_ListOrders args:{} → 1 リクエストで 5 接続グループ・42 注文行
   各行が accountId=66248423 等を持ち、我々の CROSSTRADE_ACCOUNT_ID_* と一致
Tv_ListPositions args:{} → 同じく全口座ぶん
```

**`args:{}` の一覧は全リンク口座を 1 リクエストで返す。** これが O(1) の根拠。

副産物: 行の `contractId` が全部 `4470324` だったので、限月ロール(09-15)以降ずっと
空だった `CROSSTRADE_CONTRACT_ID_MNQZ6` を実データで埋めた。

### 仕様と実測が食い違う点(そのまま残す)

公式は「超過は 429 + `Retry-After`」と書くが、R113 の実測は**応答の遅延**だった
(同じ照会 5 連で 0.69 → 5.05 → 16.26 秒)。どちらも起きうるものとして扱う。

---

## 2. 実測 —— REST と Gateway を同じ日・同じコード版で

### 観測(全口座の建玉・注文・残高が揃うまで)

| | REST(R114 `prime_cycle_snapshot`) | Gateway(WebSocket) |
|---|---|---|
| 7 口座 | **16.27 秒 / 実 HTTP 28 本** | **1.16 秒 / RPC 4 本** |
| 1 口座あたりの実 HTTP | **4.00 本** | **0 本**(全口座で 1 リクエスト) |
| 内訳 | positions 7 / orders 7 / balance 7 / position 7 | `Tv_ListPositions` 1 + `Tv_ListOrders` 1(+ broker の resync で 2) |
| 定常(状態変化なし)の口座別ポーリング | 3N〜4N 本/周期 | **0 本**(45 秒観測で実測) |

**「3N 本」は論理照会の数で、実 HTTP は 4N だった**(指示 §1 の指摘どおり)。
`query_position` が FLAT のとき複数形 `positions` で裏取りするため、1 論理照会 = 2 HTTP。

### 口座数を変えたときの見積り

REST は実測(N=7)からの外挿、Gateway は仕様と実測(全口座 1 リクエスト)から。

| N | REST 実 HTTP | REST 所要 | Gateway RPC | Gateway 所要 |
|---|---|---|---|---|
| 1 | 4 | 約 2.3 秒(外挿) | 2 | 約 1.2 秒 |
| 7 | 28 | **16.27 秒(実測)** | 2〜4 | **1.16 秒(実測)** |
| 20 | 80 | 約 46 秒(外挿)※ | 2〜4 | 約 1.2 秒(外挿) |

※ N=20 の REST は **本番口座が 7 つしか無いので未検証**。80 本 × 0.58 秒(今日の 1 本あたり
実測平均)= 46 秒。加えてトークン補充だけで (80−20)/3 ≒ 20 秒待つので、同オーダー。

---

## 3. 採用した構造

```text
ブローカーのイベント(プッシュ・枠を消費しない)
        ↓
broker_gateway.BrokerGateway   ← 接続を 1 本だけ所有・購読・resync・予算
        ↓
broker_gateway.GatewayState    ← 純粋な状態機械(I/O なし・時計は注入)
        ↓
.secrets/broker_gateway_snapshot.json(原子的書き出し)
        ↓
  消費者(表示・会計・戦略サイクル・engine の観測)
```

**Gateway は注文を送らない。** RPC は `Tv_ListPositions` / `Tv_ListOrders` の 2 つだけ。
送信は従来どおり `order.py` の全ゲートを通る(指示 §3 の禁止事項)。
テストで固定してある(「**注文を送っていない**」)。

### 整合性の三状態 —— FLAT を名乗れるのは VERIFIED のときだけ

| 状態 | いつ | 消費側の扱い |
|---|---|---|
| `VERIFIED` | 全体取得が完了し、以後 seq に欠落が無い | 建玉ゼロを FLAT として読んでよい |
| `RESYNCING` | seq 欠落 / `dropped` / `resync` フレーム / 無言 30 秒 | **FLAT と読んではならない** |
| `UNVERIFIED` | 未接続 / 切断 / `status=stalled|stopped` | **FLAT と読んではならない** |

全体だけでなく **口座ごとにも** 同じ状態を持つ。実装初版は全体だけ下げて口座行を
VERIFIED のまま残しており、`account_view` を読む側が「検証済みの FLAT」と誤読できた。
**テストが捕まえた**(`gap 中は FLAT を名乗らない`)。

### 扱っている競合・故障(全部テストで固定)

* 初期取得の応答前に届いたイベント → 捨てずに溜め、取得結果の上へ重ねる
* 同じ seq / 若い seq(重複・順序逆転)→ 無視。**古い値で新しい状態を上書きしない**
* seq の飛び / `dropped>0` → `RESYNCING` にして全体を取り直す(RPC 2 本)
* `resync` フレーム → 同上
* `status=stalled|stopped|disconnected` → `UNVERIFIED`
* 切断 → `UNVERIFIED`。再接続時は **前の接続の seq を引き継がない**
* 設定外の口座の行・イベント → 状態に入れず `foreignRows` に数える
* 別限月のイベント → 監視対象の判定に使わない
* **無言 30 秒**(`NQX_GATEWAY_SILENCE_SEC`)→ 届かないこと自体を FLAT の証明にしない
* 鮮度は**ブローカーの epoch**と**受信時刻**。ファイルを書き直しても更新されない

---

## 4. 目標に対する達成状況

指示 §4 の表に対して。**達成していないものを達成したと書かない。**

| 指標 | 目標 | 結果 |
|---|---|---|
| イベント受信 → ローカル状態反映 | p95 ≤100ms / p99 ≤250ms | **達成(状態機械)**: N=1/7/20 各 2000 件で p95 ≤0.003ms、p99 ≤0.004ms。口座数で劣化しない |
| ブローカー epoch → ローカル受信 | (同上の一部) | **実測 p50 18ms / p95 20ms / max 20ms**。ただし **9 サンプルのみ**で、`status` / `resync` フレーム由来。**建玉・注文イベントでの実測は未取得**(後述) |
| 定常・状態変化なしのサイクルが追加する口座別ポーリング | 0 | **達成**。45 秒の実接続で RPC 追加 0 本 |
| 二重送信・偽 FLAT・他口座 ID 混入・保護漏れ・期限切れ送信 | 故障注入で 0 件 | **観測側は達成**(88 件の検査で偽 FLAT 0・混入 0)。**送信側は未着手** |
| 判定 → 全対象口座のブローカー受理 | p95 ≤5 秒 | **未達が確定**。下で算術 |
| 実行可能判定 → 最初の送信開始 | p95 ≤1 秒 | **未計測**(送信は本番注文になるため測っていない) |
| TP1 受信 → 保護変更の送信開始 | p95 ≤1 秒 | **未計測**(同上) |
| TP1 → ブローカーで有効 | p95 ≤3 秒 | **未計測**(同上) |
| 新しい市場証拠 → 戦略サイクル完了 | p95 ≤15 秒 | **未達**。現状 145 秒。Gateway で観測 16.3 → 1.2 秒ぶんが消えるが、残りは TradingView 取得・評価・publish |

### 測定不能を明記する

**建玉・注文イベントでの実測が取れていない。** 観測中は全口座 FLAT・生きた注文ゼロで、
`positionUpdate` / `orderUpdate` が 1 件も発生しなかった。届いたのは `status` /
`resync` / RPC 応答だけ。上の 18〜20ms はそれらの `epoch` から出した **9 サンプル**で、
目標が要求する 1000 サンプルには遠い。**速度測定のために注文を作らない**(指示 §0)ので、
この値は次に実弾が動いたときに採る。

**一覧取得の行には観測時刻が無い。** `Tv_ListOrders` の応答は上位に `epoch` を持たず、
行の `timestamp` は「その注文が出た時刻」であって観測時刻ではない。よって一覧由来の
`sourceObservedAt` は **None のまま**にしてある(ローカル時刻で埋めない)。鮮度が要る
判断はイベント由来の時刻か、受信時刻と整合性状態で行う。

また、フレームは `epoch`(ブローカー側)しか持たず **CrossTrade 側の時刻が無い**ので、
「ブローカー → CrossTrade」と「CrossTrade → ローカル」の内訳は**原理的に測れない**。
取れるのは合算と、ローカル時計とのずれ込みの値だけ。

---

## 5. 上流制約による下限 —— 5 秒目標は届かない

公式の発注経路は **口座ごと**。複数口座へ 1 リクエストで送る route は文書に無い
(2026-09-19 に確認)。2 枚を TP1 / runner の独立 OCO に分ける契約を保つので、
1 口座 2 リクエスト。

`broker_gateway.order_placement_floor_sec()` が指示 §4 の算術をコードにしてある:

| N | 送信本数 | バースト満杯からの補充待ちの**下限** |
|---|---|---|
| 1 | 2 | 0.00 秒 |
| 7 | 14 | 0.00 秒 |
| **20** | **40** | **6.67 秒** |

**これは応答時間・事前照会・ネットワークを含まない純粋な補充待ちなので、実測は必ず
これより大きい。5 秒目標は文書化された経路では不可能。**

考えられる逃げ道と、それぞれの代償:

1. **分割をやめて 1 口座 1 リクエスト**(qty=2 の単一ブラケット)→ 20 本でバースト内 =
   ほぼ 0 秒。**ただし R12 の TP1/runner 分割 OCO 契約を壊す**ので、指示 §3B の
   「分割を保持する」に反する。採らない。
2. **上限の引き上げ**(CrossTrade の上位プラン等)→ 費用と可否は未調査。要問い合わせ。
3. **サーバ側ミラー / 口座グループ発注**の有無 → 公式文書には無い。要問い合わせ。
4. **20 口座を 2 群に分けて時間差**で送る → 下限は変わらないが、群ごとの受理は速い。
   ただし「全口座同時」ではなくなるので、価格が動く分だけ後群が不利。

**いまの結論: N=20 での「1 口座と同じ瞬発力」は、観測は達成できるが送信は達成できない。**
送信の下限 6.67 秒は CrossTrade 側の制限であって、こちらの実装では縮まない。

> **訂正(R118、2026-09-19)。この節の 6.67 秒は取り下げる。** 根拠にしていた
> 「40 本の送信が 3 req/s・バースト 20 の枠を使う」は**公式文書に書かれていない仮定**
> だった。`https://crosstrade.io/docs/api/rate-limiting` が挙げる対象は HTTP REST
> (`/v1/api/*`)と WebSocket RPC で、**発注先の `/v1/send/` は一度も出てこない**。
> 正しい下限は読みごとに分けて `docs/R118_GATEWAY_INTEGRATION.md` §0 の表にある
> (N=20 で 14.0 秒 / 0.67 秒)。どちらかは CrossTrade への問い合わせ待ち
> (`docs/R118_CROSSTRADE_INQUIRY.md`、**未送信**)。**予算の問題は送信ではなく検証**
> だったことも同文書 §0。

---

## 6. まだ実装していないもの(正直に)

指示 §3 の構造のうち、**実装したのは Gateway と状態集約だけ**。

* **実行 Coordinator(§3B)は未着手。** 口座間の並行送信、世代・fencing による二重所有の
  防止、部分成功の脚別記録、共通の通信予算は設計だけ。
* **既存経路との接続は未着手。** `broker_status` の消費者を Gateway のスナップショットへ
  向ける差し替えはしていない(既定 OFF のまま)。
* **fill_watch の置き換えは未着手。** Gateway が動けば fill_watch の巡回は不要になるが、
  切り替えは実行 Coordinator と同時でないと TP1 検知に穴が空く。
* **2 時間の持続負荷試験は未実施。**

---

## 7. 導入の段取り(まだ実行していない)

1. **SHADOW 運転**: `NQX_GATEWAY=1` で常駐させ、スナップショットを書くだけ。既存経路は
   従来どおり REST で観測する。両者の食い違いを毎周期記録する(食い違い 0 が条件)。
2. **消費者を切り替える**: 表示・会計(`live_position_line` / `build_accounts_payload` /
   `trade_journal`)から順に、Gateway が `VERIFIED` のときだけ読む。**engine の送信前
   判定は最後**。`UNVERIFIED` なら従来の REST へ落ちる。
3. **fill_watch を止める**: Gateway が TP1 を押してくるので巡回が不要になる。
   `fillWatch.autostart=false` → heartbeat の pid を止める、の順。
4. **接続の所有**: 利用者あたり 1 本なので、Gateway 以外が WebSocket を張らないこと。
   2 本目は 1 本目を切る。起動世代とプロセス所有を管理する(孤児が接続を奪う事故を防ぐ)。

### 戻し方

* `NQX_GATEWAY` を未設定にする(既定 OFF)。常駐を止めればスナップショットは古くなるが、
  消費者は `VERIFIED` と鮮度を見るので勝手に使わない。
* `broker_gateway.py` と `tests/test_r117_broker_gateway.py` を消す。既存経路は
  この 2 ファイルに依存していない。

---

## 8. 検証コマンド

```powershell
python tests/test_r117_broker_gateway.py
```

88 件。ネットワークを使わない(transport と時計を注入する)。内訳は
初期取得の競合 / seq 欠落・重複・逆転 / resync / 切断・再接続 / 口座混入 / 別限月 /
鮮度 / 通信予算 / 1000 件以上の性能 / 故障を混ぜた 3000 件の持続。

実環境の読み取り検証(**注文は送らない**):

```powershell
python broker_gateway.py --probe 45
```

出力は「揃うまでの秒数 / RPC 本数 / 受信フレーム / 無言回数 / 整合性 / 欠落・混入 /
ブローカー epoch → 受信の分位 / 口座別の見え方」。**注文は送らない。**

2026-09-19 の実行例(7 口座・全口座 FLAT):

```text
検証済み状態が揃うまで: 1.30 秒
RPC 8 本 / 受信 18 フレーム / 無言 1 回
整合性 VERIFIED  欠落 0 / dropped 0 / 混入 0 / 補充待ち 0.00 秒
ブローカー epoch → 受信(秒・時計ずれ込み): 件数 9 p50=0.018 p95=0.020 max=0.020
```

RPC が 8 本なのは、broker が `resync` を送るたびに全体を取り直しているため
(1 回 2 本)。**口座数ではなく resync の回数で決まる**ので、N が増えても変わらない。
