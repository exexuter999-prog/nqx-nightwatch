# R102 — 限月ロールの価格乖離で誤発注・裸建玉になった事故の修正指示

作成: 2026-09-15 15:30 JST(監視ループのセッションが起票)。実装は別ワークツリーで行う。
**本番の建玉・注文・`.secrets`・Worker には触らない。** ロール(NQX_SYMBOL の切替と deploy)は人が別途行う。

---

## 0. 何が起きたか(証拠つき)

| 時刻(JST) | 事実 | 出所 |
|---|---|---|
| 09-14 まで | MNQ1!(TradingView 連続足)と発注先 MNQU6 は同じ 9 月限。09-14 の約定は 28,985 で束縛済み | `.secrets/autotrade_ledger.jsonl` 09-14 `ENTRY_OWNERSHIP_BOUND` |
| 09-15 07:00 | セッション開始と同時に MNQ1! が **12 月限(MNQZ2026)へロール**。監視の価格が 29,4xx へ跳ぶ(9 月限は 29,08x) | `monitor_cycle_0704.json` 以降 / ユーザーのスマホ画面 MNQU2026 = 29,085.50 |
| 15:10:04 | `OTE_FVG_PULLBACK BUY B qty2 entry 29,363.75 SL 29,329.75` を `ENTRY_CLAIMED`(decisionId `9336f7a703bf76fe`)。全部 **12 月限の値** | 台帳 |
| 15:10:40 | MNQU6 の市場は 29,078 付近。買い指値 29,363.75 は市場より **285pt 上**なので通過指値として即時約定。`avgEntry 29,078.5 / orderId 651929062505 / contractId 4399654` | `broker_status.py --account LFF05062316710006 --json` |
| 同時 | SL 29,329.75 は買い建玉の売り逆指値なのに市場より上 → ブローカーが置けず**裸**。TP1 29,495.75 / runner 29,603 は置かれた(9 月限では建値 +417 / +524pt) | スマホ画面(`1 指 29,603` / `1 指 29,495.75`、SL 無し) |
| 15:11〜 | engine は `AUTOTRADE HALT: accepted entry ownership UNKNOWN (qty2 aggregate position is not bound to a filled split leg)` → 以後 `hold`。**SL 欠落を検知も修復も通知もしない** | `nqx_cycle` 出力 15:11 / 15:13 / 15:16 / 15:20 |
| 15:16 | ユーザーが手で SL 29,012 を置いた | スマホ画面 `2 逆 29,012.00` |

損失は出ていない(+$56 含み)が、**SL 無しの実弾が 6 分以上放置**され、しかもシステムは「所有権不明」で沈黙した。

### 通ってしまった門(ここを塞ぐ)

1. `tv_fetch.py:54` `EXPECTED_SYMBOL_PART = "MNQ"` と `tv_snapshot.py:803` `if "MNQ" not in symbol.upper()` — **銘柄に MNQ が含まれていれば何でも通す**。`CME_MINI:MNQ1!`(連続足)と `MNQU6`(発注先)の限月一致を検査していない。`TRADING_CONTEXT.md` §2 は「シンボル MNQU6(CME_MINI:MNQU2026)」と限月まで書いているのに、チャートは MNQ1! のままだった。
2. `order.py:1478-1487` の「指値が現在値を通過していないか」検査は `--last` が渡されたときだけ動き、その `--last` は engine が**チャート(= 12 月限)の価格**を渡す。指値も同じ 12 月限の値なので、9 月限に対する通過は検出できない。`modify_reference_price()`(`order.py:239`)の quote も `tv_fetch._cli_json(["quote"])` = **チャート銘柄の quote** で同じ穴。
3. `order.py:1490-1495` の SL 向き検査は指値のとき `ref = a.entry`(凍結建値)基準。**ブローカー側の市場価格に対して SL が有効な側か**は見ていない。
4. `ownership_binder.py:614` — 両脚 FILLED で建玉の `orderId` が台帳の注文 ID と一致しても、ブラケット構造(SL 子行)が欠けていると `UNKNOWN` に落とす。R79(台帳の注文は nightwatch の所有物)の趣旨に反し、**「所有しているが裸」を表現できない**ので修復経路に入れない。
5. 限月の満期(`MNQU6` = 2026-09-18 最終取引日)をどこも持っていない。満期 3 日前でも新規を送る。

---

## 1. 直すもの(優先順)

### A. 限月の正本を 1 か所にする(`contract.py` + `execution_contract.json`)

- `execution_contract.json` に `contract` ブロックを足す:
  `{"symbol": "MNQU6", "tvSymbol": "CME_MINI:MNQU2026", "expiry": "2026-09-18", "lastEntryDaysBeforeExpiry": 3, "next": {"symbol": "MNQZ6", "tvSymbol": "CME_MINI:MNQZ2026", "expiry": "2026-12-18"}}`。
  値は**今の本番(MNQU6)のまま**入れる。ロール自体はこのタスクではやらない。
- `contract.py` を新設: `current()` / `tv_symbol()` / `days_to_expiry(now)` / `entry_allowed(now)` / `--status`(満期までの日数と、下の全参照サイトが正本と一致しているかを一覧で印字。不一致は非 0 で終了)。
- 参照サイトを正本読みに寄せる(既定値 `"MNQU6"` のハードコードを消すか、正本と食い違えば**起動時に落ちる**ようにする):
  `order.py:40 SYMBOL`、`autotrade_arm.py:125`、`autotrade_engine.py:414/3279/4205/4345`、`broker_status.py:382`、`fill_watch.py:626`、`telegram_bot.py:49`、`nqx_state.py:85/371/1765/1939`、`monitor_config.json:3`、`monitor_pipeline.py:618`、`monitor_publish.py:1670/1732/1918/1945`、`position_card.py`、`result_card.py:223`、`trade_journal.py`(複数)、`cloudflare/wrangler.toml:25 NQX_SYMBOL`、`cloudflare/src/nightwatch_do.js:145/180/199`、`.secrets/nqx_cloud.env NQX_SYMBOL`(**中身は読まない。キー名だけ `contract.py --status` で照合**)。
- `docs/CONTRACT_ROLL_CHECKLIST.md`: ロール手順(FLAT のときだけ / 正本を `next` に差し替え / `contract.py --status` 緑 / Worker `NQX_SYMBOL` 変更 + `npm run deploy`【人】/ AUTO 再武装【人】/ TV の pane 0・pane 1 を新限月へ【人】/ 最初の周期で `symbol=CME_MINI:MNQZ2026` を確認)。

### B. チャート銘柄は「その限月」でなければ BLOCK(連続足 MNQ1! を禁止)

- `tv_fetch.py:152-154` と `:299`、`tv_snapshot.py:802-804`: `symbol == contract.tv_symbol()` を要求。`MNQ1!` / `MNQ2!` / 別限月は `AcquireError("CHART_SYMBOL_MISMATCH: chart=<x> contract=<y>")` → サイクル BLOCKED。`monitor_pipeline.py:485` も同じ判定に置き換える。
- pane 0 / pane 1 の両方を検査する(`pane_layout.json` の各 pane の symbol)。
- **自動で `chart_set_symbol` を叩いて直さない**。pane 構成が壊れる実績がある(メモリ `tv-pane-layout`)。BLOCK して人に直させる。
- `chart_state*.json` / `pane_layout.json` の期待値 `CME_MINI:MNQ1!` を正本から生成する。

### C. 価格基準(basis)ガード — ロール検知の保険

- 毎周期、`tv_snapshot` がチャート価格と **発注先限月の quote** を比べる。quote は `tv_fetch._cli_json(["quote", "--symbol", contract.tv_symbol()])`(CLI に symbol 引数が無ければ足す。MCP の `quote_get(symbol=...)` と同じ JSON)。
- `|chart − quote| > max(2 × noiseFloor, 40pt)` なら `BLOCKED: SYMBOL_BASIS_MISMATCH chart=29,376.75 quote=29,078.50 (+298.25pt)` で publish も発注もしない。`acquisitionReceipt` に両方の値と時刻を残す。
- B が正しく動けばここは常に 0pt 付近。B の抜け(TradingView 側の勝手なロール、pane の張り替え忘れ)に対する二重化。

### D. `order.py` は発注先限月の quote で指値・SL を検査する

- `--symbol` に対応する `tvSymbol` の quote を送信直前に取り、`--last` の有無に関係なく:
  - 買い指値 ≥ quote / 売り指値 ≤ quote → `ERROR: ENTRY_LIMIT_THROUGH_MARKET`(今日の 29,363.75 vs 29,078.5 はここで止まる)。
  - 買いの SL ≥ quote / 売りの SL ≤ quote → `ERROR: STOP_WRONG_SIDE_OF_MARKET`(29,329.75 vs 29,078.5 はここで止まる)。
  - `|--last − quote| > 40pt`(engine が渡した現在値がブローカー側と食い違う) → `ERROR: LAST_PRICE_DIVERGENT`。
- quote が取れないときは**指値でも**見送り(`QUOTE_UNAVAILABLE`)。成行の `--min-stop-pt`(R90)も同じ quote を使う。
- ドライランと `--confirm` は同じ門を通す(R52 の教訓: 門は dry-run の return より前)。

### E. 裸エントリーの検知と修復(R78 を ENTRY に拡張)

- `ownership_binder.py:614` の経路: 建玉の `orderId` が台帳の送信注文 ID と一致し、口座・銘柄・方向・枚数が一致すれば **OWNED**(R79)。ブラケット子行の欠落は所有権を落とす理由にせず、`protection: {"tp": n, "sl": m}` として返す。
- `autotrade_engine`(3 分ループ)と `fill_watch`(FLAT→建玉の遷移)の両方で、所有した建玉の各脚に **live な SL 子行が OCO 兄弟として存在する**(`broker_status.oco_sibling_pair`、R80)ことを確認する。欠けていれば**同じ周期**で:
  1. 凍結構造 SL が quote の有効側(買いなら quote より下、`NQX_STOP_MARKET_BUFFER_PT` 以上離れている)なら、その価格で SL を置く。
  2. 有効側でなければ `quote ∓ max(1.0 × noiseFloor, NQX_STOP_MARKET_BUFFER_PT)` に置く。
  3. 置けなければ `--flatten --account`。
  台帳 `ENTRY_NAKED_REPAIRED` / `ENTRY_NAKED_FLATTENED`、Telegram に 1 行(`裸建玉を検知: SL <価格> を置いた`)。
- 修復用の送信は既存の `order.py --modify`(`cancelandbracket`)を使わない — 取消対象が無いので、SL だけを置く経路(CrossTrade の stop 単独注文)を `order.py --protect --account --sl` として足す。ペイロードは CrossTrade 公式の契約の範囲で。
- 今日のケース(TP 2 本あり・SL 0 本・建値乖離 285pt)を fixture にした回帰テスト。

### F. 満期ガード

- `contract.entry_allowed(now)` が False(満期まで `lastEntryDaysBeforeExpiry` 日未満、または `next` が未設定で満期 8 日前を過ぎた)なら `msnr_gate` / `monitor_publish` で `WATCH(CONTRACT_EXPIRY_NEAR)`、engine は ENTRY を出さない。建玉管理と撤退は止めない。
- `nqx_cycle` の行頭に `contract: MNQU6 exp 09-18 (3d)` を毎周期 1 行出す。

### G. テスト(隔離。`tests/_hermetic.py` の流儀で、本番パス・ネットに触れない)

- `tests/test_r102_contract.py`:
  - B: `chart_state.json` の symbol が `CME_MINI:MNQ1!` → `AcquireError CHART_SYMBOL_MISMATCH`。
  - C: chart 29,376.75 / quote 29,078.5 / noiseFloor 16 → `SYMBOL_BASIS_MISMATCH`。
  - D: `order.py` dry-run `--entry 29363.75 --sl 29329.75 --side buy`、quote 29,078.5 → `ENTRY_LIMIT_THROUGH_MARKET`。quote 無し → `QUOTE_UNAVAILABLE`。
  - E: 建玉 LONG 2 `orderId` 一致、子行 TP×2 / SL×0 → OWNED + naked → `ENTRY_NAKED_REPAIRED` が台帳に 1 行、送信 argv に `--protect` が 1 回だけ。
  - F: `now = expiry − 2d` → ENTRY 不可、管理は可。
  - A: `contract.py --status` が全参照サイト一致で 0、`order.py` の既定値だけ変えると非 0。
- 既存 `tests/run_all.py` に載せる。

### H. 文書

- `TRADING_CONTEXT.md` §2 のシンボル行を「正本は `execution_contract.json.contract`」に書き換える。
- `CLAUDE.md` §1(データ取得)に「チャート銘柄は限月固定。連続足は BLOCK」、§6.1 に `contract: ...` 行の読み方を 1 行ずつ足す。
- `docs/R102_CONTRACT_ROLL_GUARD.md`(この文書)の末尾に「実装で変えたファイル一覧」を追記。

---

## 2. 貼り付け用プロンプト(ワークツリー用)

```text
タスク R102: docs/R102_CONTRACT_ROLL_GUARD.md を読み、§1 の A→B→C→D→E→F→G→H の順に実装する。
本番の .secrets / 建玉 / 注文 / Worker には一切触らない。order.py を live で叩かない。
chart_set_symbol など TradingView の状態を変える MCP は呼ばない(読むだけも不要。fixture で書く)。
execution_contract.json の contract ブロックは今の本番値(MNQU6 / CME_MINI:MNQU2026 / 2026-09-18)で入れ、
ロールはしない。テストは tests/_hermetic.py の流儀で隔離し、python tests/run_all.py が緑であること。
完了時に「変えたファイル / 新しい門と拒否コード / 人がやる手順(deploy・pane 張り替え・AUTO 再武装)」を
docs/R102_CONTRACT_ROLL_GUARD.md 末尾に追記して報告する。
```

## 3. 完了条件

- 今日の数字(chart 29,376.75 / quote 29,078.5 / 指値 29,363.75 / SL 29,329.75)を入れると **B・C・D の 3 か所で独立に止まる**。
- SL 子行の無い所有建玉が、3 分ループでも fill_watch でも同じ周期で SL を得るか、FLATTEN される。
- `python contract.py --status` が全参照サイトの一致を機械で示す。
- `python tests/run_all.py` 緑。本番コードに「テスト時は読み飛ばす」分岐を足していない。

## 4. やらないこと

- 建玉 LONG 2 @29,078.5(MNQU6)の操作。手動 SL 29,012 の変更。
- MNQZ6 へのロール(人が FLAT のときに `docs/CONTRACT_ROLL_CHECKLIST.md` で行う)。
- CrossTrade 送信ペイロードの契約変更(R80 の判定器・`cancelandbracket` の形は不変)。
- `chart_set_symbol` の自動実行。

---

## 5. 実装記録(2026-09-15、監視ループのセッションで実装)

### 実装で分かったこと(指示書からの変更)

- **C(basis ガード)は quote では作れない。** TradingView CLI/MCP の `quote <symbol>` は銘柄引数を
  ラベルにしか使わず、価格は常に**アクティブチャートの最終足**を返す(`tradingview-mcp/src/core/data.js`
  `getQuote`)。発注先限月の quote と比べても常に 0pt。代わりに**価格の出所(チャート銘柄)を全消費点で
  検査**する: `tv_snapshot`(quote.json の symbol が別銘柄なら価格に使わない)、engine の fresh quote
  (`_default_fresh_price_query` は別限月の quote を捨てる)、`order.py`(`--price-symbol` / quote の symbol /
  `--last` と quote の乖離 `LAST_PRICE_DIVERGENT`)。契約の `basisGuard` は `priceProvenance` に置き換えた。
- **CrossTrade に SL 単独の注文は無い**(place / cancelandbracket / change / cancel / cancelreplace /
  closeposition / flatten / reverse / flatplace)。裸修復は `order.py --modify --repair-naked`
  (= `cancelandbracket` で SL + TP1 の **1 組**)。孤立した TP は取り消されて 1 組に畳まれる
  (TP1 で全量が利確し runner は残らない。裸の建玉の応急処置としてこれを受け入れる)。置けなければ FLATTEN。
- 「裸」の定義は **保護注文の OCO 組(相互 `ocoId`・live・親なし)が 0**(`broker_status.oco_pairs`)。
  TP と SL は種別が返らないので、片方だけ残った行は組にならない = 守っていない。2 組中 1 組だけ欠けた
  半裸は対象外(R78 の runner 修復が従来どおり見る)。
- 約定直後は子行が見えないことがあるので `graceSec`(既定 30 秒)の猶予を置き、再照会で 0 組を確かめてから動く。
- 所有権: 建玉の `orderId` が台帳の脚と一致し、ブローカーが receipt を返さない(CrossTrade/Tradovate)なら
  `ledgerBackedOwnership` の下で **receipt と建値乖離を要求せず**所有する(R79 の趣旨)。凍結した子 2 行のうち
  1 本だけ残る脚は `INCONSISTENT` ではなく `PARTIAL`(所有は落とさない)。NT8 など receipt を返す
  ブローカーは従来どおり厳格。

### 変えたファイル

| ファイル | 変更 |
|---|---|
| `contract.py`(新規) | 限月の正本: `symbol / tv_symbol / expiry / next / entry_allowed / chart_symbol_matches / check_sites / enforce_sites / summary_line`、`--status` / `--json` |
| `execution_contract.json` | `contract` ブロック(`symbol / tvSymbol / expiry / lastEntryDaysBeforeExpiry / next / priceProvenance / nakedRepair`) |
| `tv_fetch.py` | `_require_window` と pane 検査を `contract.chart_symbol_matches` に(連続足・別限月は AcquisitionError) |
| `tv_snapshot.py` | `validate_window_layout` と `build_bundle` の銘柄検査を同上。quote.json の symbol が別銘柄なら価格に使わない |
| `monitor_pipeline.py` | `SOURCE_SYMBOL_NOT_MNQ` → `CHART_SYMBOL_CONTINUOUS / _MISMATCH / _MISSING` |
| `monitor_publish.py` | 満期ガード(`contract expiry gate` で ARMED→WATCH、契約が壊れていれば fail-closed) |
| `nqx_cycle.py` | 行頭に `contract: …` 行。`enforce_sites()` の不一致は `HALT: CONTRACT_SYMBOL_DISAGREE` |
| `order.py` | `SYMBOL` を正本から。新規の門: `ORDER_SYMBOL_NOT_CONTRACT / CONTRACT_EXPIRY_NEAR / CONTRACT_EXPIRED / LAST_SYMBOL_MISMATCH / LAST_PRICE_DIVERGENT / QUOTE_UNAVAILABLE / ENTRY_LIMIT_THROUGH_MARKET / STOP_WRONG_SIDE_OF_MARKET`(ドライランと `--confirm` の両方、送信前)。`--price-symbol`。`--modify --repair-naked`(OCO 組 0 のときだけ `MODIFY_SPLIT_PLAN_PROTECTED` を外す。組があれば `REPAIR_NOT_NAKED`)。quote は発注先限月のときだけ採用 |
| `autotrade_engine.py` | `_contract_entry_guard`(claim の前の見送り)、`_command_for_entry` が指値でも `--last` と `--price-symbol=` を渡す、order.py の R102 拒否は HALT でなく見送り(`_r102_reject_code`)、`_default_fresh_price_query` の出所検査、`_guard_naked_position`(管理判断の前。OFF / SHADOW / LIVE) |
| `ownership_binder.py` | orderId 一致(receipt 無しブローカー)での所有、子 1 本残りは `PARTIAL` |
| `broker_status.py` | `oco_pairs(rows, account, expected_action, symbol)` |
| `autotrade_arm.py / telegram_bot.py / nqx_state.py / position_card.py / result_card.py / trade_journal.py / monitor_pipeline.py / monitor_publish.py` | `"MNQU6"` の直書きを `contract_month.symbol()` に |
| `telegram_bot.py` | Mini App 一回押しはサーバー正本の market から `--last` / `--price-symbol` を渡す |
| `tests/_pin_contract.py`(新規) | テストを本番の `manualHalt` と限月から切り離す(nakedRepair は既定 OFF) |
| `tests/test_r102_contract.py` / `tests/test_r102_guards.py`(新規) | §1 A/B/D/D'/E/F の検証(今日の数字で 3 か所独立に止まる) |
| `tests/_hermetic.py` | サンドボックスに `contract.py` を複製 |
| `docs/CONTRACT_ROLL_CHECKLIST.md`(新規) | ロール手順 |
| `TRADING_CONTEXT.md` §2 | シンボル行が正本を指す |

### 新しい門と拒否コード

| 場所 | コード | 意味 |
|---|---|---|
| tv_fetch / tv_snapshot / monitor_pipeline | `CHART_SYMBOL_CONTINUOUS` | チャートが連続足(MNQ1!)。BLOCKED |
| 同上 | `CHART_SYMBOL_MISMATCH` / `CHART_SYMBOL_MISSING` | チャートが別限月 / 銘柄不明。BLOCKED |
| nqx_cycle | `CONTRACT_SYMBOL_DISAGREE` | env / wrangler / monitor_config が正本と別限月。HALT |
| monitor_publish / engine / order.py | `CONTRACT_EXPIRY_NEAR` / `CONTRACT_EXPIRED` | 満期の手前・満期後。新規だけ止める |
| engine / order.py | `ORDER_SYMBOL_NOT_CONTRACT` | プラン/`--symbol` が正本の限月でない |
| order.py | `LAST_SYMBOL_MISMATCH` | `--last` の出所(`--price-symbol`)が別限月 |
| order.py | `LAST_PRICE_DIVERGENT` | `--last` と送信直前 quote の乖離 > 40pt(`NQX_LAST_DIVERGENCE_PT`) |
| order.py | `QUOTE_UNAVAILABLE` | 参照価格なし(quote 不可・`--last` なし)。新規は送らない |
| engine / order.py | `ENTRY_LIMIT_THROUGH_MARKET` | 指値が参照価格を通過(即約定) |
| engine / order.py | `STOP_WRONG_SIDE_OF_MARKET` | SL が参照価格の逆側(ブローカーが置けず裸になる) |
| order.py | `REPAIR_NOT_NAKED` | `--repair-naked` なのに OCO 組がある |
| engine 台帳 | `MANAGEMENT_SENT`(`naked`)/ `FLATTEN_SENT`(`naked`)/ `HALT` | 裸修復の送信・撤退・失敗。key `…:NAKED_REPAIR:<qty>` |

### 人がやる手順

1. **今すぐ**: TradingView の pane 0 / pane 1 を `CME_MINI:MNQU2026`(または次限月へロールするなら
   `docs/CONTRACT_ROLL_CHECKLIST.md` の順で `MNQZ6` / `CME_MINI:MNQZ2026`)にする。連続足 MNQ1! のままだと
   全周期 `BLOCKED: CHART_SYMBOL_CONTINUOUS`。
2. `execution_contract.json` の `manualHalt.autotrade` は 2026-09-15 15:29 に **true** にした(裸建玉の事故の
   再発防止)。ロールと pane の張り替えが終わり、`python contract.py --status` が緑になったら `false` に戻す。
3. `python nqx_cycle.py` を 1 回叩いて `contract:` 行と `symbol=` を確認してから `/loop mnq 3m` を再開する。
4. Worker の `NQX_SYMBOL` を変えるロールでは `cd cloudflare && npm run deploy` と AUTO の再武装が要る。
5. 9 月限 `MNQU6` は 09-18(金)が最終取引日。満期 3 日前(= 今日)から新規は `CONTRACT_EXPIRY_NEAR` で止まる
   (`lastEntryDaysBeforeExpiry` を変えれば調整できる)。

### 検証

- `python tests/test_r102_contract.py`(49 件)/ `python tests/test_r102_guards.py`(今日の数字で B・D・D'・E)。
- `python tests/run_all.py` 緑(本番の `manualHalt=true` を engine テストが読まないよう `tests/_pin_contract.py` で切り離した)。
- `python contract.py --status`: 設定サイトは全部 OK、`last chart symbol` だけ `CHART_SYMBOL_CONTINUOUS`(pane が MNQ1! のまま)。

### 追記(同日 17:30): 連続足 MNQ1! のまま運用できるようにした(R102b)

ユーザー要望「MNQ1! を使い続けたい」への対応。TradingView のチャート内部モデル
`mainSeries().symbolInfo()` は連続足に対して `front_contract`(例 `MNQZ2026`)を返す
(`symbolExt()` には無い。MCP の `symbol_info` / CLI `info` はサーバー側の import 漏れで落ちるので
`tv ui eval` で直接読む)。これを毎周期 raw `symbol_info.json` / `symbol_info_15m.json` に逐語保存し、
`contract.chart_symbol_matches(symbol, front_contract)` で発注先の限月と照合する。

- 一致 → 通す(`sourceFrontContract` / `sourceContract` を bundle に載せ、`--price-symbol` は解決した限月そのもの)。
- TradingView がロールして別限月 → `CHART_SYMBOL_MISMATCH: chart=CME_MINI:MNQ1! front=MNQZ2026 contract=CME_MINI:MNQU2026`
  で BLOCK(今日の事故はここで止まる)。**ロールの合図は TradingView 側が出す**ので、BLOCK が続いたら
  `docs/CONTRACT_ROLL_CHECKLIST.md` の順で発注先を切り替える。
- 解決できない(eval 失敗・古い symbol_info)→ `CHART_SYMBOL_CONTINUOUS_UNRESOLVED` で BLOCK(fail-closed。
  前周期の front_contract は再利用しない: 取れなかった周期は raw を消し、FRESH な symbol_info だけを信じる)。
- `order.py` / engine の fresh quote(MNQ1! の quote)も同じ解決を通してから採用する。連続足で front が
  解決できない周期は `--price-symbol` を添えない(quote 側の門は掛かる)。
- pane 一覧の段階(front 未取得)は root(MNQ)の一致だけを見る `chart_symbol_plausible`。

変更: `contract.py`(`continuous_root / chart_symbol_plausible / chart_symbol_matches(front) / resolved_contract`)、
`tv_fetch.py`(`SYMBOL_INFO_EXPR / chart_symbol_info / _write_symbol_info`、`_require_window(front)`)、
`tv_snapshot.py`(symbol_info raw・`validate_window_layout(info15, info3)`・`sourceFrontContract / sourceContract`)、
`monitor_pipeline.py` / `monitor_publish.py`(passthrough)/ `autotrade_engine.py` / `order.py` / `telegram_bot.py`。
テスト: `tests/test_r102_contract.py` §3、`tests/test_r102_guards.py` B/D' に front 付きの判定を追加。

### 追記(同日 17:45 JST): MNQU6 → MNQZ6 へロール実施

ユーザー指示で、このセッションがローカルの限月切替(`execution_contract.json.contract` / `.secrets/nqx_cloud.env` /
`monitor_config.json` / `cloudflare/wrangler.toml` / `TRADING_CONTEXT.md` §2)と Worker の deploy
(Version `b2bdbb27-3db2-4e42-9e48-85ff4d9d35e0`、`NQX_SYMBOL="MNQZ6"`)を行い、`manualHalt.autotrade` を
false に戻した。`python contract.py --status` 全行 OK(チャートは MNQ1!、front MNQZ2026)。直後の 1 周期は
`[16:24] MNQZ6 29,356.5 | primary=NONE | published`。AUTO は「symbol changed since arming」で DISARMED
(Mini App での再武装は人)。残り: `broker_status.DEFAULT_SYMBOL` / `fill_watch` / engine の `"MNQU6"` 直書きも
正本読みに寄せた(§5 の表に追加)。次のロールは MNQ1! が MNQH2027 を指した周期に `CHART_SYMBOL_MISMATCH` で
BLOCK が続くのが合図(`contract.next` = MNQH7 / 2027-03-19)。

### 追記(同日 16:31〜16:41): ロール直後の旧限月 claim が新規を止めた(R102c)

ロール後の最初の周期で `autotrade blocked: ENTRY startup recovery failed (ENTRY_CLAIM_RECOVERY_UNVERIFIED 409)`。
旧限月 MNQU6 で置いた ENTRY claim(15:10、CONSUMED / routeState SENT / 注文 ID 2 本)は Worker の DO に
そのまま残り(DO 状態は限月ごとに分かれていない)、engine が**今の限月 MNQZ6** でブローカーを観測して
不在証明を作ったため、Worker の `entryRecoveryProof`(observation.symbol == intent.symbol)が通らなかった。
さらに、その MNQZ6 の観測が観測スロットに先着したことで、次周期に claim の限月 MNQU6 で観測し直しても
「broker observation cannot overwrite another scope/intent」で締め出された。

直したもの:
- `autotrade_engine._claim_recovery_symbol`: ENTRY の起動時回復は **claim の executionIntent.symbol** で観測する
  (MANAGEMENT の回復は元からそうだった。ENTRY だけ非対称)。
- Worker `state_machine.applyBrokerObservationEvent`: R43 の口座載せ替えと同じ形で **限月の載せ替え**
  (同じ intent・同じ口座・観測の symbol が intent 自身の銘柄)を許す(`intentSymbol`)。無関係な銘柄は従来どおり拒否。
  テスト `cloudflare/test/r102_broker_observation_symbol_handoff.test.mjs`。deploy Version `909cea48`。
- 16:41 の周期で `ENTRY_RECOVERED` が書かれ、新規の詰まりは解消。

### 追記(同日 16:44〜16:50): DO の state.symbol は作成時固定だった(R102d)

ロール後の初回 ARMED(16:44 VP80 BUY B)で `server state: UNAVAILABLE` → `autotrade blocked: frozen state was
not published` → HALT。Worker の拒否理由は `CYCLE_TOMBSTONED: scenario invalid: scenario.symbol MNQZ6 does not
match MNQU6`。DO の状態文書は `emptyState(accountId, symbol)` で作成時の限月に固定され、NQX_SYMBOL を変えて
deploy しても文書側は旧限月のまま(scenario / position / result の検証は state.symbol と比較する)。

直したもの: `state_machine.adoptSymbol(state, symbol, nowMs)` を `#loadState` で通す。建玉なし・ENTRY/MANAGEMENT
claim が CLAIMED/CONSUMED でない・blocking 注文なしのときだけ state.symbol を NQX_SYMBOL へ載せ替え、旧限月の
scenario / market / brokerObservation / position / cycle を捨てる(position は null = 照会未確認 = fail-closed。次の
position publish で埋まる)。終端した claim と tombstone は残す。`symbolRoll {from,to,at}` を記録。
テスト `cloudflare/test/r102_symbol_roll.test.mjs`。deploy Version `411ccfcb`。直後の view は `symbol: MNQZ6`。

**ロール手順への含意**: `docs/CONTRACT_ROLL_CHECKLIST.md` の deploy は、この載せ替えを含む。ロール時に旧限月の
claim が CLAIMED/CONSUMED のままなら載せ替えは拒否され(view の symbol が旧限月のまま)、先に R102c の回復を
待つ(FLAT + 注文終端で自動回復する)。

### 追記(同日 16:52): 裸修復の誤発火(実弾)と修正

ロール後初の ENTRY(VP80 BUY B、ULTRA 4 枚、16:50 約定 LONG 4 @29,316.25)の次周期で、`_guard_naked_position`
が「OCO 組 0 / 孤立 2 本」と判定し `--modify --repair-naked`(SL 29,285.25 / TP 29,382.75、4 枚 1 組)を送った。
実際は PLACE のブラケットが正常に働いていた(TP1 2 枚 @29,382.75 / runner 2 枚 @29,603)。原因は
`broker_status.oco_pairs` が **brokerParentId を持つ行を除外**していたこと: 約定後の PLACE ブラケットは
Tradovate の実測(R76)で「片方の子が約定した親の ID を名乗ったまま相互 ocoId で結ばれる」形なので、片方が
落ちて組が成立せず「孤立 2 本」に見えた。結果、分割ブラケットが 1 組(TP1 で 4 枚全部)へ畳まれた。
損失なし・建玉は保護されている。修正: 親 ID の除外をやめ、SUSPENDED(親待ちの子)だけ live 状態で外す。
テスト: `tests/test_r102_guards.py`(R76 実測形の組 1 / SUSPENDED の組 0)。
同時に、本番の reconcile は `now` を渡さないため `_fill_age_seconds(position, None)` が落ちて publish が失敗し
HALT になっていた(16:50)。`now` の既定を入れた。
