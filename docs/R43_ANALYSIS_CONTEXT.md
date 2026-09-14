# Nightwatch R43/R44 分析コンテキスト

> 受信箱機能は 2026-08-25 から無期限停止。受信箱の存在、未読、認証、通信失敗を
> `BLOCKED` / `HALT` の理由にしてはならず、監視周期から呼び出さない。

## 目的

Claude/Codexのどちらから実行しても、同じTradingView観測から同じ分析・ゲート・publish結果を再現する。エージェントは生データ取得だけを担当し、整形・分析・一発判定はコードへ渡す。手作業で数値や判定を書き直さない。

正本は `TRADING_CONTEXT.md`、`execution_contract.json`、本書、`nqx_cycle.py`。本書と古い監視記述が競合した場合は本書を使う。受信箱確認とループ冒頭の時間窓確認は行わない。時間窓は `nqx_cycle.py` が内部で判定する。

## 固定画面

- pane 0: MNQ 15分 Candles。HTF取得時だけ一時変更し、最後は必ず15分へ戻す。
- pane 1: MNQ 3分 + CVD Unified。
- 各paneをfocusした直後に `chart_get_state` でMNQ銘柄とresolutionを確認する。時間足だけ修復できる。銘柄は勝手に変更しない。
- VIXは取得・判定しない。形成中足を確定足として保存しない。

## 取得の自動化（R51）

**取得はエージェントの仕事ではない。** `nqx_cycle.py` が毎サイクル最初に
`tv_fetch.py` を呼び、MCPサーバ同梱CLI（`tradingview-mcp/src/cli/index.js`、
`console.log(JSON.stringify(result, null, 2))` で MCP ツールと同一 JSON を出す）の
stdout をそのまま `.secrets/tv_raw/*.json` へ原子的に落とす。エージェントの担当は
`python nqx_cycle.py` を **1 回叩くことだけ**になった。

以前はエージェントが MCP 応答を読んで書き写していた。実害は 2 つ:

* **鮮度窓を割る** —— bars3m 240本 + bars15m 60本の逐語出力だけで数分。必須8 raw の
  鮮度は 240 秒なので、書き終わる頃に先の raw が期限切れになる。2026-09-01 に
  `required raw acquisition is not fresh` が 2 サイクル連続。1 周 20 分かかっていた。
* **忠実性** —— 転記は要約・省略・書き間違いの入口で、403 サイクルで
  `rangeAnchor` / `peers` / `po3` / `cvdMeta` を 0% にした張本人。

実測: 取得 3.5 秒 / publish まで含めて 1 周 36 秒。3分間隔に収まる。
CLI パスは `NQX_TV_CLI`、node は `NQX_NODE` で差し替えられる。CLI が見つからない・
success=false・足の間隔が期待と違う場合は **保存せず BLOCKED**（古い raw を黙って
再利用させない）。`--no-fetch` で取得を飛ばせるが、これは配線検証専用。

## 毎周期の取得契約

MCPレスポンスを要約・抜粋・再構成せず、UTF-8 JSONのまま `.secrets/tv_raw/` へ保存する。

順序は **画面確認 → 左15m構造 → due HTF → 左15m復元 → 右3m実行足 →
CVD → SMT → event → 左15mへfocus復元**。構造を先に固定し、3mの方向へ15mレベルを
後付けしない。右下CVDはpane 1内のstudy領域であり、第三paneとして数えない。

**R51: この取得は `tv_fetch.py` が行う。** `nqx_cycle.py` が毎サイクル最初に呼ぶので、
エージェントはこの表を手で実行しない（下記「取得の自動化」）。表は契約の定義として残す。

| pane | 呼出し | 保存先 | 扱い |
|---|---|---|---|
| 全体 | `pane_list` | `pane_layout.json` | 必須。2pane、0=MNQ/15、1=MNQ/3 |
| 0 / 15m | `chart_get_state` | `chart_state_15m.json` | 必須。HTF後に復元stateを上書き |
| 0 / 15m | `data_get_ohlcv count=60` | `bars15m.json` | 必須。15m構造 |
| 0 / 15m | `data_get_study_values` | `study_15m.json` | 必須。CT値 |
| **0 / 15m** | `data_get_pine_labels study_filter="Sessions" max_labels=100` | `pine_labels.json` | 必須。**VPは pane 0 の study** |
| **0 / 15m** | `data_get_pine_lines study_filter="SMT" verbose=true` | `smt_lines.json` | SMT線。**pane 0** |
| **0 / 15m** | `data_get_pine_labels study_filter="SMT" max_labels=4` | `smt_labels.json` | SMTラベル。**pane 0** |
| 1 / 3m | `chart_get_state` | `chart_state.json` | 必須 |
| 1 / 3m | `data_get_ohlcv count=240` | `bars3m.json` | 必須 |
| 1 / 3m | `data_get_study_values` | `study_3m.json` | 必須。CVD/VWAP等 |
| 1 / 3m | `data_get_pine_tables study_filter="CVD"` | `cvd_table.json` | 取得できる場合 |
| 1 / 3m | `quote_get` | `quote.json` | 価格補助 |

**★ pine 系は pane 0 で読む（2026-09-01 訂正）。** `data_get_pine_*` は
**focus 中の pane の study しか見ない**。pane 1 には `CVD Unified` しか無いので、
`Sessions`（VPレベル）と `SMT` を pane 1 focus 中に呼ぶと `study_count: 0` が返る。
必須の `pine_labels.json` が空になれば levels 0 でサイクルごと BLOCKED になる。
0件を `SMT_SOURCE_MISSING` と早合点しない —— **focus した pane を先に疑う。**

SMT Divergences V2 webhookが使える場合だけ、受信した `NQX_SMT_ALERT/1` を `smt_alert.json` へ逐語保存する。`BROKEN` は残存線より優先する。webhookが無いときはJSONを作らず、線・ラベル経路を使う。

## HTF期限駆動取得

先に `python htf_context.py --raw-dir .secrets/tv_raw` を実行し、JSONの `due` に出たframeだけ取得する。目視時刻で全frameを毎回取り直さない。

- 45m: pane 0を45分 → state再確認 → OHLCV 80本 → `bars45m.json`
- 1h: 60分 → state再確認 → OHLCV 80本 → `bars1h.json`
- 4h: 240分 → state再確認 → OHLCV 80本 → `bars4h.json`
- 1D: 日足 → state再確認 → OHLCV 80本 → `bars1d.json`
- 完了後はpane 0を15分へ戻し、stateで復元を確認する。

HTFを3分足から合成しない。欠落frameは `MISSING/INSUFFICIENT` の無得点で進める。

## 取得受領書ゲート

`tv_snapshot.py` は `NQX_ACQUISITION_RECEIPT/1` を生成し、画面・15m・3mの必須raw
（pane layout / 15m state・bars・study / 3m state・bars・study / session labels）の
hash・mtime・freshnessを凍結する。さらに2paneのsymbol/resolutionと3m側の
`CVD Unified` studyを値で検証する。

- 受領書なし、schema不正、必須rawなし、必須rawが非FRESH: pipelineを `BLOCKED`。
- 取得途中でrawが変化: torn cycleとして `BLOCKED`、次周期で取り直す。
- Mini Appの `DATA NO RECEIPT` は相場判断ではなく公開契約不一致。A/A+を許可しない。
- `DATA FRESH` でもモデル成立を意味しない。データ取得ゲートと戦略ゲートは別物。

## 分析と一発判定

`python nqx_cycle.py` が次を固定順序で実行する。

1. `tv_snapshot.py`: rawを正規化し、確定3m/15m/HTF、VP・session levels、range anchor、CVD、SMT、PO3、event、受領書を一つのbundleへ統合。
2. `monitor_ingest.py`: 同一snapshotを原子的に投入し、order/network/reconcileが呼ばれていないreceiptを検査。
3. `monitor_pipeline.py`: 銘柄、0.25tick、価格/足freshness、受領書、range、CVD、SMT、event、HTFを検査。
4. `msnr_gate.evaluate()`: LONG/SHORT/FLATを同時比較し、VP80反転、Turtle Soup、Breaker継続、OTE+FVG pullbackと画像戦略カタログを実データへ反映。
5. `select_primary`: 最上位1件だけを `A+ / A / B / FAIL` で決着(2026-09-04 に B を追加)。別モデルへ逃げない。
6. `monitor_publish.py`: READYかつpublish preflight通過時だけ、検証済みbundleをWorkerへ原子的にpublish。

判定材料は、MSNR連鎖、構造否定SL、VP/PD/DOL、明示range anchor、OTE、FVG/IFVG、SMT、PO3、CVD、ICT時間窓、45m/1h/4h/1D、ボラ、イベント、口座リスク。取得した材料は `strategyEvidence` と `evaluation` に残す。証拠に残らない情報は採用理由にしない。

SLは構造否定位置の外側へnoise floor緩衝を置いて先に決める。60pt/2枚/$240に収めるためSLを内側へ縮めない。ボラ比率 `<=0.60` はA+/A/B、`>0.60` は全停止（2026-09-01 に「0.40..0.60 はA+のみ」を撤廃）。

CVDが欠落・staleなら一度だけCVD関連を再取得し、`$cvdRetryJson | python nqx_cycle.py --cvd-retry`。価格、足、VP、range、SMT、eventは書き換えない。2回目でも不健康ならA+をAへ上限化し、分析自体は続ける。

**CVD 観測時刻の出所(R52・2026-09-01)**: `tv_snapshot` は時刻を **`cvdMeta.at` にしか
書かない**(トップレベル `cvdAt` は作らない)。`build_market_payload` は `cvdAt` →
`cvdMeta.at` → `snapshot.cvdAt` の順で拾い、`execution_contract._cvd_at()` と
`msnr_gate` が同じ値を見る。**これを繋ぐまで market payload の `cvdAt` は常に null で、
毎サイクル `CVD_TIMESTAMP_MISSING` → `CVD_A_PLUS_PROHIBITED` が立ち、A+ は一度も
武装できていなかった**(取れている値を捨てていた = CLAUDE.md §1 違反)。
なお `cvdMeta.at` は **取得時のシステム時計**(`priceAt` と同じ `now_iso`)なので、
契約の CVD 鮮度 600 秒は「そのサイクルで実際に読んだか」の検査であり、
サンプル自体の足時刻を独立に測るものではない。

`DATA NO RECEIPT / MSNR NO CHAIN / CVD NO STATUS / ICT TIME N/A` が同時に出た場合は、順に調べる。まず公開された `market.evaluation` がWorkerで削られていないか、次にローカルbundleの受領書、最後に個々の戦略不成立を見る。後段のN/Aを先に相場不成立と断定しない。

## AUTOと発注境界

Mini App AUTOがWorkerでLIVEのときだけ、READYかつA+/A/B(2026-09-04 に B 追加)の同一cycleが `autotrade_engine` へ渡る。エージェントはAUTOを変更せず、`--arm`、`order.py --confirm`、個別stage、生bundleの直接publishを実行しない。AUTOや発注経路の変更は本分析ループの仕事ではない。

## 報告

`nqx_cycle.py` の最終行を改変せず返す。BLOCKED/HALT時だけ最重要理由を1行追加する。取得値、Entry、SL、TP、gradeを手で再計算・転記しない。
