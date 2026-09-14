# TradingView 取得ループ（R43・固定2pane＋時間足ローテーション）

2026-08-25。実チャート `CME_MINI:MNQ1!` の2チャートpane／3視覚領域で
全ステップを確認した手順。左は15分、右上は3分、右下はCVD Unifiedである。

> **R51（2026-09-01）: この手順は `tv_fetch.py` が実行する。**
> `nqx_cycle.py` が毎サイクル最初に呼ぶので、**エージェントは以下を手で叩かない。**
> 本書は取得契約の定義と、実チャートで踏んだ罠の記録として残す。
> 手で確認したいときだけ `python tv_fetch.py`（取得のみ・publish しない）。

## なぜ手順を機械化したか

監視ループは **LLM のエージェント・ターン**であり、MCP を呼べるのはエージェント
だけ —— **だと思っていた。** 実際には MCP サーバ（`tradingview-mcp`）に同じ core を
呼ぶ CLI が同梱されており、Python から `node src/cli/index.js` を叩けば MCP ツールと
**同一の JSON** が stdout に出る。R51 でここへ寄せ、転記工程を消した（取得 3.5 秒 /
1周 36 秒。転記時代は 1 周 20 分で、3分間隔に間に合っていなかった）。

それまではエージェントがバンドル JSON を**手で書き写して**いた。その転記工程で
実サイクル 403 本のうち:

| フィールド | 取得率 |
|---|---|
| `rangeAnchor` | **0%** |
| `peers` / `po3` / `bars15m` / `cvdMeta` | **0%** |
| `cvd` | 401 本あるが**全て裸の int** → `_cvd_score` は dict の `bias` を要求するため**採点 0 点** |

結果、392 候補すべてが `NO_CONFIRMATION` で止まっていた。

`tv_snapshot.py` は MCP の**生出力だけ**から決定論的にバンドルを組む。
エージェントの裁量は「どのツールを呼ぶか」だけに縮み、ループ何周目でも
同じ入力から同じバンドルが出る。

**実測の効果（同一チャートで検証）: ICT 入力 0/7 → 5/7。**

---

## 毎サイクルの手順

### 0. 接続確認（1 回）

```
mcp__tradingview__tv_health_check
```

`cdp_connected: true` と `chart_symbol` に MNQ が含まれることを見る。

> **CDP はタブ 0 に固定束縛。** `tab_switch` を呼んでも `tv_health_check` の
> `target_id` は変わらず、読み取り先は常にタブ 0。実測で確認済み
> （タブ 2 へ切り替えても target_id 不変）。**ピアや VIX を別タブから取ることは
> できない。** 必要なら同一チャートの銘柄切替で取る。

### 1. 画面契約を固定する

`pane_list` を最初に1回だけ呼び、出力を `pane_layout.json` へ逐語保存する。
必須形は `layout=2h`、pane 0=`MNQ/15`、pane 1=`MNQ/3`。pane数・銘柄・時間足が
違えば取得を開始しない。右下CVDは第三のTradingView paneではなく、pane 1内の
study領域である。

この順序は、先に15分の構造とアンカーを確定し、後から3分のトリガーを評価するために
固定する。3分の見た目から先に方向を決め、15分のレベルを後付けしてはならない。

### 2. pane 0 の15分コンテキストを先に取る

```
pane_focus index=0
chart_get_state                 → chart_state_15m.json
data_get_ohlcv count=60         → bars15m.json
data_get_study_values           → study_15m.json
```

focus直後のstateで `CME_MINI:MNQ1! / resolution=15` と
`NQX SwingArm Pressure V2` を確認する。ここから `CT_TREND / CT_ATR / CT_TRAIL /`
`CT_FIB618 / CT_FIB786`、15分構造、名前付きセッションrangeを作る。OTEの
`rangeAnchor` はこの段階または次のHTF段階で固定し、ローリング3分高安から作らない。

### 3. pane 0 でHTFを期限駆動取得する

45分・1時間・4時間・日足を3分足から集約してはいけない。rawが無い場合、または
`now >= 前回rawの最終行の close(open + step) + 60秒` の場合に更新する。raw の
最終行は取得時点の**形成中足**なので、その close を過ぎたら閉じた足の最終値と
次の形成中足を取りに行く(旧規則 `最終確定足open + 2×step` は最終行を確定足と
誤認していて、毎本 1 本分遅れ、その間は途中値が確定足として採点されていた。
2026-09-08 に修正)。取り直しても同じ行が最終行のままなら(4h の 23:00 JST 足は
06:00 まで続く・休場・週末)`min(step, 1h)` ごとに再試行する。
`tv_snapshot.py` は HTF raw の**最終行を常に形成中扱い**にし、後続の行がある
行だけを確定足にする。

```powershell
python htf_context.py --raw-dir .secrets/tv_raw
```

出力 `{"schema":"NQX_HTF_REFRESH/1","due":[...],"frames":{...}}` の `due` の
frameだけ取得する(`frames` は各 frame の `lastOpen / settleAt / writtenAt /
nextDueAt / reason` で、なぜ due か・次はいつかの根拠)。目視で境界時刻を判断しない。

| timeframe | TradingView値 | count | 保存先 | maxAgeSec |
|---|---:|---:|---|---:|
| 45分 | `45` | 80 | `bars45m.json` | 5,400 |
| 1時間 | `60` | 80 | `bars1h.json` | 7,200 |
| 4時間 | `240` | 80 | `bars4h.json` | 28,800 |
| 日足 | `1D` | 80 | `bars1d.json` | 172,800 |

各対象について `chart_set_timeframe` → `chart_get_state`（MNQとresolutionを再確認）→
`data_get_ohlcv count=80` の順に行う。全対象の終了後は必ず
`chart_set_timeframe 15` → `chart_get_state` でpane 0を15分へ戻し、復元stateを
`chart_state_15m.json` に上書きする。途中失敗時も復元を先に行い、取れなかったframeは
`MISSING/STALE` のままにする。

`htf_context.py` は各frameの確定足から `HH/HL・LH/LL`、不足時はEMA20と5本傾き、
ATR14、20本レンジ、確定時刻と鮮度を作る。2frame以上が同方向で重み付き優位なら
`HTF_ALIGNED`、反対なら `HTF_CONFLICT`。どちらも候補へ一度だけ±1し、HTF単独で
構造ゲートを解除しない。

### 4. pane 1 の3分実行足を取る

`pane_list` で固定2paneの存在を確認し、`pane_focus index=1`。focus直後の
`chart_get_state` で `CME_MINI:MNQ1! / resolution=3` と CVD Unified を確認する。
時間足だけが違えば3へ戻す。銘柄は変更しない。

| # | ツール | 保存先 |
|---|---|---|
| 1 | `chart_get_state` | `chart_state.json` |
| 2 | `data_get_ohlcv count=240` | `bars3m.json` |
| 3 | `data_get_study_values` | `study_3m.json` |
| 4 | `data_get_pine_tables study_filter="CVD"` | `cvd_table.json` |
| 5 | `quote_get` | `quote.json` |

**★ pine の Sessions / SMT はここではなく pane 0 で取る（2026-09-01 訂正）。**
`data_get_pine_*` は**focus 中の pane の study しか見ない**。pane 1 には
`CVD Unified` しか無いので、pane 1 focus 中に `Sessions` や `SMT` を読むと
`study_count: 0` が返る。必須の `pine_labels.json` が空になれば levels 0 で
サイクルごと BLOCKED になる。旧版のこの表は 3 件を pane 1 に並べていた。

| # | ツール（**pane 0 / 15m で実行**） | 保存先 |
|---|---|---|
| a | `data_get_pine_labels study_filter="Sessions" max_labels=100` | `pine_labels.json` |
| b | `data_get_pine_lines study_filter="SMT" verbose=true` | `smt_lines.json` |
| c | `data_get_pine_labels study_filter="SMT" max_labels=4` | `smt_labels.json` |

**出力は逐語で保存する。** 要約・整形・抜粋をしない。整形は `tv_snapshot.py` の仕事。

取得後、CVD/SMTを独立証拠として読む。

- CVD: `study_3m.json` のCVD/EMA fast/slowと `cvd_table.json` のEMA優勢が一致した時だけ方向を付ける。
- SMT: stateにSMT studyが見えた時だけ線・ラベルを読む。study自体が無ければ空JSONを保存して
  `SMT_SOURCE_MISSING` とし、チャート上にあると推測しない。
- MapleStax等のシグナルは `INDICATOR_CLAIM`。OHLC・VP・CVDと独立に裏付けられた時だけ
  `strategyEvidence` へ入り、表示ラベル単独でA/A+へ昇格させない。

最後は `pane_focus index=0` → `chart_get_state` でMNQ/15を再確認する。

### 5. 組み立て

```powershell
python tv_snapshot.py --out .secrets/tv_bundle.json
```

`BLOCKED: <理由>` なら**そのサイクルは publish しない**。理由をそのまま記録する。

R42ではbundleに `acquisitionReceipt=NQX_ACQUISITION_RECEIPT/1` が入る。各rawの
`modifiedAt / ageSec / bytes / sha256 / status` を保存し、必須8ファイル
（`pane_layout / chart_state_15m / bars15m / study_15m / chart_state / bars3m /`
`study_3m / pine_labels`）のどれかが240秒を超えていれば
ファイルが残っていてもBLOCKする。これにより「取得コマンドを飛ばし、前回rawを再利用」
したサイクルを後から特定できる。

### 6. 固定パイプラインへ流す

```powershell
python nqx_cycle.py
```

`monitor_publish.py`へ生bundleを直接渡さない。`nqx_cycle.py`だけが
取得→ingest→pipeline→READY時publish→AUTO判定の順序を実行できる。

---

## 保存先

`.secrets/tv_raw/` — MCP 出力の逐語保存。毎サイクル上書き。

| ファイル | 必須 | 内容 |
|---|---|---|
| `pane_layout.json` | ○ | 2pane、銘柄、15m/3mの配置 |
| `chart_state_15m.json` | ○ | pane 0がMNQ/15へ復元済みか |
| `chart_state.json` | ○ | シンボルと解像度の検証 |
| `bars3m.json` | ○ | 3 分足 240 本 |
| `study_3m.json` | ○ | CVD Unified の生値と EMA |
| `pine_labels.json` | ○ | VP レベル |
| `cvd_table.json` | — | CVD bias の裏取り |
| `quote.json` | — | 無ければ最終足の終値 |
| `study_15m.json` | ○ | `NQX_DATA_*` |
| `bars15m.json` | ○ | 15 分足60本（HTF/アンカー/鮮度検証） |
| `bars45m.json` | — | 45 分足80本（期限駆動） |
| `bars1h.json` | — | 1 時間足80本（期限駆動） |
| `bars4h.json` | — | 4 時間足80本（期限駆動） |
| `bars1d.json` | — | 日足80本（期限駆動） |
| `peers_es.json` | — | SMT 用 ES 足 |
| `smt_alert.json` | — | `NQX_SMT_ALERT/1` の最新Formation/Breakage |

---

## 実チャートで踏んだ罠（すべてコードで処理済み）

| 罠 | 実例 | 対処 |
|---|---|---|
| 負号が **U+2212** | `"NQX_DATA_CT_TREND": "−1.00"` | `_num()` が `−` を `-` に置換 |
| 数値がカンマ入り文字列 | `"CVD": "59,999"` | 同上 |
| epoch の単位が混在 | bars は秒、`*_SOURCE_TIME` は **ms** | `_epoch_sec()` が 40 億超を ms と判定 |
| 同名ラベルが多重に返る | 52 ラベル中 `"C: POC"` が 10 回 | **最後の出現**を採る |
| 形成中足が混ざる | 実測 90% のサイクルで混入 | `t + step <= now` で落とす |
| 市場が閉じていると足が古い | 週末に **47.8 時間**前 | `bars3m stale` で fail-closed |
| `pane_list` が嘘をつく | — | `chart_get_state.resolution` だけを信じる |
| `data_get_ohlcv` の `symbol` 引数が効かない | — | 銘柄は `chart_set_symbol` で変える |

---

## 導出しているもの（インジケータに無いので計算する）

| 値 | 導出元 | 根拠 |
|---|---|---|
| `New York High/Low` 等 | 確定足を ET セッションで切って高安 | VP インジは出さない。`derive_range_anchor` が必要とする**名前付きセッション極値**。ローリング 3M 高安ではない |
| `vwap` / `vwap_lo` / `vwap_hi` | 取引日開始からの hlc3×出来高 ±1σ | 現チャートに VWAP インジが無い |
| `cvd.bias` | 3m の EMA fast vs slow **＋** pine table の「EMA \| 買い/売り優勢」 | 二重取り。**食い違えば bias を付けない**（誤った方向で ±1 されるより 0 が安全） |
| `po3` | 取引日始値に対する掃引と現在位置 | 生成器がどこにも無かった。分類できなければ **None**（当て推量しない） |
| `sessionId` | `ET-YYYYMMDD-AM/PM` | 採番するコードが無かった |
| `cvdMeta.status` | 履歴 3 標本が同値 **かつ**価格が 1 tick 以上動いた → `STALLED` | 2026-08-19 のキャッシュ事故の教訓 |
| `htfContext` | 45m/1h/4h/1Dの確定OHLC | 3分足から合成せず、構造と鮮度を候補採点へ接続 |
| `ifvg` fallback | 3本FVG→body breach→inverse retest/hold | provider欠落時でも確定OHLCでlifecycleを再現 |
| `fibSd` fallback | 解決済み明示range | ローリング3分高安を使わず比率ladderを再現 |

セッション境界は ET 18:00 起点で Asia 18-02 / London 02-08 / New York 08-17。
**ラベル名は `msnr_gate.DERIVED_RANGE_PAIRS` と一致していることが要件** — 変えるなら両方。

---

## 検証記録（2026-08-24、実チャート）

取得した実データから組んだバンドル:

```
symbol=CME_MINI:MNQ1!  price=29370.0  bars3m=240  levels=10
cvd=59999 bias=BEARISH status=FRESH  po3=MANIPULATION_UP  eventGate=NONE
rangeAnchor: valid=True SESSION_NY_DERIVED source=levels
  hi=29533.75 lo=29220.25 span=313.5 position=DISCOUNT favors={BUY:True}
ictCoverage: 5/7 (rangeAnchor, dol, killzone, cvd, po3)  ← 従来 0/7
```

欠けている 2 つは `smt`（ES ピア未取得、かつ SMT 窓の外）と `fvg`（このデータに
適格な空隙が無い）。候補 0 はこの金曜引けデータに連鎖が無いだけで、配線は健全。

`python tests/run_all.py` **37 ファイル通過**（`tests/test_r28_tv_snapshot.py` が新規、
41 項目）。

---

## SMT はチャートの指標またはJSON alertから取る（R43）

ES 足を自前で揃える経路は CDP がタブ 0 固定のため実環境で使えない。
一方 `SMT Divergences V3` は **MNQ vs MES を毎足計算して線を描いており**、
その幾何が `data_get_pine_lines verbose=true` でそのまま読める。

実チャートで確認した構造:

- 線は `{x1,x2,y1,y2,color}`。`x` は指標内の連番で**バー番号ではない**（最大が最新）
- `color` は 2 枠に分かれる。**ラベル色を Bullish/Bearish とも白にしていても
  線の color 枠は 1 と 2 に分かれたまま**
- 重なる x 区間(78-88)で `color 2` は y≈29516/29488（窓の高値 29539 付近）、
  `color 1` は y≈29257/**29220.25**（**窓の安値そのもの**）

→ 安値の乖離 = BULLISH、高値の乖離 = BEARISH。

**色枠の番号は決め打ちしない。** 指標設定で入れ替わりうるので、毎回
「y の中央値が高い方の色枠」を BEARISH と導く。さらに最新線が窓の値幅の
上半分か下半分かを**幾何でも**判定し、**両者が一致したときだけ bias を返す**
（CVD bias と同じ規律）。0.45〜0.55 の中腹は判定不能として保留する。

エンジン側は `external_smt()`（`msnr_gate.py`）で受ける。**peers から自前計算
できているときはそちらが勝つ** —— 観測の取り込みで自前の判定を上書きしない。
SMT 窓（ET AM 05:00-09:30 / PM 12:00-15:00）の関門は変えていない。

`SMT Divergences V2 [OutOfOptions]` のFormation/Breakage alertを使う場合は
`docs/SMT_ALERT_JSON_SETUP.md` のJSONを指定する。最新payloadを
`.secrets/tv_raw/smt_alert.json` へ原子的に保存すれば、Formationは方向付き観測、
Breakageは明示的な無効化になる。Breakageが届いたサイクルは、チャートに古い線が
残っていてもその線へfallbackしない。Webhookをまだ置かない場合は従来のline/label
読取だけで動くため、JSON設定は必須ではない。

検証時の実データでは `colorBias=BULLISH` だったが最新線が `position 0.472`（中腹）
だったため **bias は保留**された。正しい挙動。

## 現在の境界

- **gamma** — 認可済みデータ源がまだ無いので `U` のまま。将来routeは
  `docs/GAMMA_DATA_ROUTE.md` に固定した。OIだけからdealer signを推測しない。
- **FVG/IFVG** — FVGは3分確定足、IFVGはbreach/retest/holdまで成立した時だけ有効。
  適格な空隙が無い相場では `MISSING/OBSERVE` が正しい。
