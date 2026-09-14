# ICT 入力の取得手順（TradingView MCP → エンジン）

2026-08-24。実サイクル 403 本を調べたところ、ICT の独立確認に使う入力が
**一つも取得されていなかった**。手順が文章として存在しなかったのが原因なので、
ここに固定する。

## 0. 現状（実測）

| フィールド | 403 本中の取得数 |
|---|---|
| `levels` | 403 |
| `bars3m` | 393（各 140 本） |
| `cvd` | 401 — **ただし裸の `int`。dict ではない** |
| `rangeAnchor` | **0**（403/403 が `RANGE_ANCHOR_REQUIRED`） |
| `peers` / `peerMeta` / `sessionId` | **0** |
| `po3` | **0** |
| `cvdMeta` | **0** |
| `bars15m` | **0** |
| `eventGate` | **0** |

**CVD は数字が画面に出ていても得点していない。** `_cvd_score`
（`msnr_gate.py:1558`）は `isinstance(value, dict)` を要求し `value["bias"]` を読む。
401 本すべてが整数なので `return 0, None`。**取得しているつもりで 0%**。

R13 パイプライン（`monitor_pipeline.py`）は**一度も走っていない**。
入力ファイル `.secrets/monitor_snapshot.json` も出力も存在しない。
403 本は旧 §6.2/§6.3 経路（生 bundle → `monitor_publish.py`）の産物。

## 1. 取得順（`monitor_pipeline.py:452-465` が正本）

| 順 | ステップ | 必要フィールド |
|---|---|---|
| 1 | `CHART_STATE` | MNQ symbol / visible timestamp / last price / source |
| 2 | `BARS_3M` | 12 本以上の**確定** OHLCV / bar timestamp / 0.25 tick |
| 3 | `BARS_15M` | 確定 OHLCV / HTF 構造の文脈 |
| 4 | `VP_PD_DOL` | VP levels / PD・DOL ラベル / as-of time |
| 5 | `RANGE_ANCHOR` | `rangeTf` `rangeStart` `rangeEnd` `anchorType` `freshness` `high` `low` |
| 6 | `CVD_INITIAL` | CVD の値と**向き** / timestamp / status / provider |
| 6b | `CVD_RETRY_ONCE`（条件付き） | 再取得は**一度だけ** / `cvdAttempts=2` |
| 7 | `SMT_PEERS` | 同一時刻の peer 足 / `sessionId` / peer freshness |
| 8 | `EVENT_CONTEXT` | event state / source / checked_at |
| 9 | `NORMALIZE_VALIDATE` | schema / freshness / hash / missing evidence |
| 10 | `MSNR_ICT_EVALUATE` | LONG / SHORT / FLAT の比較 |
| 11 | `PUBLISH_PREFLIGHT` | compact bundle / market payload の検証 |
| 12 | `AUTOTRADE_DRY_RUN_HANDOFF` | 管理計画のみ / reconcile 無し / 発注無し |

## 2. ペインとツールの対応

`pane_list` は**嘘をつく**（2026-08-12 に観測）。`chart_get_state.resolution`
だけが信用できる確認手段。

| データ | ペイン | ツール | 格納先 |
|---|---|---|---|
| 確定足 3m ×140 | 1 (3m) | `data_get_ohlcv count=140` | `snapshot.bars3m` |
| VWAP と band | 1 (3m) | `data_get_study_values` | `vwap` / `vwap_lo` / `vwap_hi` |
| CVD, fast/slow | 1 (3m) | `data_get_study_values` | `cvd` / `cvdFast` / `cvdSlow` |
| レベル | 1 (3m) | `data_get_pine_labels study_filter="Key Levels"` + `data_get_pine_lines study_filter="VP"` | `snapshot.levels` |
| CT_ATR / CT_TREND / CT_TRAIL / CT_FIB618 | **0 (15m)** | `data_get_study_values` `NQX_DATA_*` | 初期 SL 検算・シナリオ設計 |
| VIX | 別タブ | タブ切替 → 読む（`quote_get` の `symbol` 引数は効かない） | 文脈のみ |

**VAH と VAL が `snapshot.levels` に無いと value-area 経路は永久に発火しない**
（`operational-gates.md:565-569`、165 bundle で発火 0）。

## 3. 各フィールドの正しい形

### `rangeAnchor`（最重要 — これが 403/403 の欠落）

```json
{"rangeTf": "15m",
 "rangeStart": "2026-08-21T20:00:00+09:00",
 "rangeEnd":   "2026-08-21T21:00:00+09:00",
 "anchorType": "HTF_DIRECTIONAL_LEG",
 "freshness":  "FRESH",
 "high": 29650.0, "low": 29480.0}
```

検証（`resolve_range_anchor`, `msnr_gate.py:987`）:
- 5 フィールドのいずれかが falsy → `RANGE_ANCHOR_FIELDS_MISSING`
- `rangeTf` が `3m`/`3`/`180s`/`180` → **`ROLLING_3M_OTE_FORBIDDEN`**
- `rangeEnd <= rangeStart` → `RANGE_ANCHOR_TIME_INVALID`
- `freshness` が `FRESH`/`ACTIVE` 以外 → `RANGE_ANCHOR_NOT_FRESH`
- `high <= low` → `RANGE_ANCHOR_PRICE_INVALID`

**これを生成するツールは存在しない。** 15m/1H/セッションのディーリングレンジを
目で決めて手で書く。前サイクルの値をそのまま持ち越すと `freshness` が
腐って静かに落ちる。`ICT_LOCATION` は A/A+ に必要な確認要素なので、
**ここが空だと武装しない**。

### `cvd`（数字だけでは 0 点）

```json
"cvd": {"bias": "BULLISH", "value": -129151},
"cvdMeta": {"provider": "TradingView MCP", "attempts": 1,
            "status": "FRESH", "at": "...", "history": [...]}
```

`bias` / `side` / `trend` のいずれかに `BULLISH`|`BEARISH` が必要。
**裸の整数は `available` を通っても得点しない。**

停滞判定: 価格が 1 tick 以上動いたのに 3 サンプル同値なら停滞。
そのときだけ `data_get_pine_tables study_filter="CVD"` で生値を裏取りしてから
**一度だけ**再取得する（2026-08-19 に `data_get_study_values` が 27 分の
キャッシュを返した事故がある。指標ではなく読み出し経路が古かった）。
再取得の失敗は A+ → A であって、発注のリトライではない。

### `peers`（SMT）

```json
"peers": {"ES": [{"t": 1787000000, "h": 6420.5, "l": 6415.0}, ...]},
"peerMeta": {"ES": {"sessionId": "NY-2026-08-24"}},
"sessionId": "NY-2026-08-24"
```

- peer は 5 本以上。時刻は主足と**完全一致**（許容差ゼロ、ずれれば `SMT_STALE_PEER`）
- `sessionId` が無ければ `SMT_SESSION_ID_REQUIRED`。**採番するコードは無い**ので手で決める
- 窓は ET AM 05:00–09:30 / PM 12:00–15:00 の外なら `OUTSIDE_SMT_WINDOW`
- `YM1!` は CBOT の権限が無く取得できない。**ES のみで運用する**
- `data_get_ohlcv` の `symbol` 引数は効かないので、タブを切り替えて読む。
  `chart_set_symbol` はフォーカスをタブ 0 に飛ばすので `tab_switch` をやり直す

### `po3`

`DISTRIBUTION_UP` / `DISTRIBUTION_DOWN` / `MANIPULATION_UP` / `MANIPULATION_DOWN`
のいずれかの**文字列**。それ以外は 0 点。**生成する Pine も MCP 呼び出しも
取得ステップも検証も無い。** セッション構造を見て手で書く。

### `eventGate`

`events.py --refresh` と `events.current_gate()` は既にあるが、
**`eventGate` を snapshot に書き込むコードが無い**ため毎サイクル
`EVENT_CONTEXT_MISSING` になる。型も揺れている（テスト固定値は dict
`{"state":"NONE","checkedAt":...}`、`execution_contract.py:258` は裸の文字列を比較）。

## 4. 実行

```powershell
$initialSnapshotJson | python monitor_ingest.py --config monitor_config.json --kind snapshot
python monitor_pipeline.py --config monitor_config.json
```

受領書に `orderInvoked=false` と `networkInvoked=false` が無ければ止める。
`cvd.refreshRequired == true` のときだけ:

```powershell
$cvdRetryJson | python monitor_ingest.py --config monitor_config.json --kind cvd-retry
python monitor_pipeline.py --config monitor_config.json
```

`status=READY` かつ `publishPreflight.ready=true` のときだけ publish へ渡す。

## 5. 既知の落とし穴

- **`priceAt` を足の epoch から作らない。** 実時計より約 1 分先行して
  `priceAt is too far in the future` で弾かれた事故がある。毎サイクル
  システム時計を読む
- **ICT 入力の欠落はブロックしない設計**（`_validate_evidence` は `missing[]`
  に入れる）。ICT が全部空でも `status=READY` になって publish される。
  止めているのは `msnr_gate._finalize_candidate` の `NO_CONFIRMATION` だけ
- **`acquisitionRequest` は静的**。毎回同じ 12 ステップを出し、
  `validation["missing"]` を反映しない。何を再取得すべきかは
  `phaseLog` と `validation.missing` を見る
- `maxAgeSec` の `vp` / `peers` / `events` は定義されているが**適用されていない**
