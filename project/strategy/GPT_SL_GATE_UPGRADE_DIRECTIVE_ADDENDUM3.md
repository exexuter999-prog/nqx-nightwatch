# GPT改良指示書 — 第3追補（`O`レコード実OHLC供給時のTrue Range再現）

- 宛先: GPT（実装担当）
- 発行: Fable（設計担当）/ 承認: ユーザー
- 上位文書: `project/strategy/GPT_SL_GATE_UPGRADE_DIRECTIVE.md`（原指示書）、`_ADDENDUM.md`（第1追補）、`_ADDENDUM2.md`（第2追補）。本書はこれらの実装に対する**追加**であり、既存の変更点（`SL_INVALIDATION_TOO_CLOSE`、Z構造レベル抽出、`SL_STRUCTURAL_DISTANCE`のMAPPED LEVEL SPACING計算、HTMLタスクA〜C・E）は一切変更しない。
- 発行理由: 第2追補完了報告（`GPT_IMPLEMENTATION_REPORT.md`「SLゲート強化・proxy_unit修正完了」節、435-438行目）が明記した既知の限界——「`O.c`に実OHLCがある場合、Python validatorはTrue Range平均を再現せず、level-spacingの粗い監査のままである」——を解消する。

---

## 0. 本書で発見した副次的な不整合（実装前に必ず解消すること）

着手前に`project/engine-contract/nqx1-spec.md`の`O`レコード仕様（127-135行目）と、`project/app/nq-nightwatch-nqx-final.html`の`parseCandleRow`関数（約2265-2274行目）を読み比べること。

**仕様書は`O`レコードの1バーを`TIME,OPEN,HIGH,LOW,CLOSE[,VOLUME]`という**カンマ区切り**で記述している**（`nqx1-spec.md:130,133`）が、**実装（`parseCandleRow`）は`raw.split('|')`で**パイプ区切り**としてパースしている**。バー同士の区切り（セミコロン`;`）は両者一致している。

この食い違いは、既存のテストfixture（`valid-sample.nqx`・`invalid-sample.nqx`・`mnq_2026-07-17_1856_test.nqx`）が**全て`O|c=MISSING`のみで、実際のOHLCバー列を一度もテストしていなかった**ために、これまで誰にも検出されていなかった。

**方針**: HTML本体（`parseCandleRow`）を変更する権限は本追補にはない。実装が既に本番で使われている以上、**実装（パイプ区切り）を正とし、仕様書側の記述をパイプ区切りに訂正する**。この訂正は「新しい設計判断」ではなく「ドキュメントの実態への追従」であり、絶対規範2（既存の安全条件を弱めない）には抵触しない。訂正後の仕様書の記述例:

```
O|c=20:48|30018|30038|30008|30031;20:51|30031|30047|30022|30041
```

（`nqx1-spec.md:130`の例をこの形式に書き換え、133行目の`TIME,OPEN,...`という説明文を`TIME|OPEN|HIGH|LOW|CLOSE[|VOLUME]`に訂正する。他の記述内容——バー同士の区切りがセミコロンであること、`O|c=MISSING`の扱い、model/scenario/interpolatedバー禁止——は変更しない）

この訂正を最初のタスク（タスクF）として行い、その後にPython側の実装（タスクG）を行う。

---

## 1. 絶対規範（既存追補と同一、再掲）

1. 数値を発明しない。本書が指定する定数（TR算出式そのもの、`slice(-12)`の12本、`trs.length>=3`の最小本数条件）は全てHTML側`rangeModel()`に既に存在するものの移植であり、新規の数値ではない。
2. 既存の安全条件を弱めない。
3. 後付け検証の禁止。
4. 成果の粉飾禁止。
5. 質問で作業を止めない。
6. 日本語報告。

---

## 2. タスクF: `nqx1-spec.md` の `O` レコード記述を実装に合わせて訂正する

対象: `project/engine-contract/nqx1-spec.md` の `### O — exact observed OHLC only` 節（約127-135行目）

0章の指示通り、バー内の区切り文字の説明とサンプルを、カンマ区切りからパイプ区切りへ訂正する。他の文言（バー同士の区切りがセミコロンであること等）はそのまま維持する。

---

## 3. タスクG: `validate_nqx.py` に `O` レコードの実OHLCパースとTrue Range計算を追加する

### 3-1. 現状把握（読了必須）

`project/app/nq-nightwatch-nqx-final.html`の`rangeModel(market)`関数（約3264-3281行目）を再確認する:

```js
function rangeModel(market){
  market=market||{};
  var bars=market.candles||[],trs=[];
  bars.slice(-12).forEach(function(c,i,a){
    var prev=i?a[i-1].c:null;
    var tr=prev==null?c.h-c.l:Math.max(c.h-c.l,Math.abs(c.h-prev),Math.abs(c.l-prev));
    if(isFinite(tr)&&tr>0)trs.push(tr);
  });
  var atr=trs.length>=3?trs.reduce(function(s,v){return s+v;},0)/trs.length:null;
  if(atr!=null)return{unit:atr,source:'SUPPLIED OHLC RANGE',observed:true};
  /* ... MAPPED LEVEL SPACING フォールバック（第2追補で既にPython側へ移植済み、変更不要） ... */
}
```

要点:
- `bars.slice(-12)`: バー列の**末尾12本**（12本未満なら全件）を対象にする。
- 各バーのTrue Range: `prev`（直前バーの終値、先頭バーは`null`）を使い、`prev==null`なら単純に`高値-安値`、それ以外は`max(高値-安値, |高値-prev|, |安値-prev|)`。
- `tr>0`の値のみを`trs`配列に採用（0以下やNaNは除外）。
- `trs`が**3件未満なら`null`を返し、MAPPED LEVEL SPACINGへフォールバック**（第2追補で実装済みのロジックがそのまま使われる）。
- 3件以上あれば、単純算術平均（Wilder平滑化ではない）を`unit`とし、`source:'SUPPLIED OHLC RANGE'`を返す。

### 3-2. Python側の実装

`validate_nqx.py`に以下を追加する:

#### (a) `O`レコードのパース

`validate()`関数内、`Z`レコードの処理（第1追補2-2節で追加済み）と同様の場所に、`O`レコードの保持を追加する:

```python
elif tag == "O":
    if observed_raw:
        report.error(lineno, "DUP_RECORD", "duplicate O record",
                     "emit exactly one O record")
    observed_raw, observed_line = fields, lineno
```

（`observed_raw = {}` / `observed_line = 0`を、他のレコード変数と同じ場所で初期化する）

#### (b) バー列のパースとバリデーション

新しいヘルパー関数を追加する:

```python
def parse_observed_candles(raw):
    """Parse O.c into a chronological list of {t, o, h, l, c, v} dicts, mirroring
    the receiver's parseCandleRow/parseCandleTape (project/app/nq-nightwatch-
    nqx-final.html, ~2265-2284行目). Bar fields are pipe-delimited
    (TIME|OPEN|HIGH|LOW|CLOSE[|VOLUME]); bars are semicolon-delimited. Returns
    (accepted, rejected) — rejected entries are (raw_row, error_message) tuples
    and do not raise; the caller decides whether to report them."""
    accepted, rejected = [], []
    if raw is None:
        return accepted, rejected
    text = raw.strip()
    if not text or re.match(r"^(?:missing|unverified|n/a)$", text, re.I):
        return accepted, rejected
    for row in re.split(r"\s*;\s*", text):
        row = row.strip()
        if not row:
            continue
        parts = [p.strip() for p in row.split("|")]
        if len(parts) < 5:
            rejected.append((row, "EXPECTED TIME|OPEN|HIGH|LOW|CLOSE"))
            continue
        try:
            o, h, l, c = (float(parts[i]) for i in range(1, 5))
        except ValueError:
            rejected.append((row, "NON-NUMERIC OHLC"))
            continue
        v = None
        if len(parts) > 5 and parts[5] != "":
            try:
                v = float(parts[5])
            except ValueError:
                v = None
        if h < max(o, c) or l > min(o, c) or h < l:
            rejected.append((row, "INVALID OHLC GEOMETRY"))
            continue
        accepted.append({"t": parts[0] or "—", "o": o, "h": h, "l": l, "c": c, "v": v})
    return accepted, rejected
```

**このパースの検証条件（`h < max(o,c)` 等）は`parseCandleRow`（HTML側、約2272行目）の`if(h<Math.max(o,c)||l>Math.min(o,c)||h<l)`と完全に同一のロジックである。新しい許容範囲を発明していない。**

#### (c) True Rangeユニット計算

```python
def supplied_ohlc_unit(bars):
    """Mirrors the receiver's rangeModel() SUPPLIED OHLC RANGE branch: simple
    mean of True Range over the trailing 12 bars (fewer if unavailable),
    requiring at least 3 finite positive TR values. Returns None (falls back
    to level-spacing) when the tape is absent or too short — exactly the same
    condition under which the receiver itself falls back."""
    trs = []
    window = bars[-12:]
    for i, bar in enumerate(window):
        prev_close = window[i - 1]["c"] if i > 0 else None
        if prev_close is None:
            tr = bar["h"] - bar["l"]
        else:
            tr = max(bar["h"] - bar["l"], abs(bar["h"] - prev_close), abs(bar["l"] - prev_close))
        if tr > 0:
            trs.append(tr)
    if len(trs) < 3:
        return None
    return sum(trs) / len(trs)
```

### 3-3. `level_spacing_unit` 呼び出し箇所への接続

第2追補で追加した`SL_STRUCTURAL_DISTANCE`監査（`validate_nqx.py`内、第2追補2-3節で書き換えた箇所）で、`unit`の決定ロジックを以下のように変更する:

**変更前（第2追補時点）**:
```python
unit = level_spacing_unit(levels, pivot)
```

**変更後**:
```python
observed_bars, observed_rejected = parse_observed_candles(observed_raw.get("c") if observed_raw else None)
for row, err in observed_rejected:
    report.warn(observed_line or lineno, "OBSERVED_CANDLE_REJECTED",
                f"O.c row {row!r} rejected: {err}",
                "fix the bar or omit it; rejected bars are not used in range calculations")
unit = supplied_ohlc_unit(observed_bars)
unit_source = "SUPPLIED OHLC RANGE"
if unit is None:
    unit = level_spacing_unit(levels, pivot)
    unit_source = "MAPPED LEVEL SPACING"
```

エラーメッセージ（`SL_STRUCTURAL_DISTANCE`の本文）にある`range unit {unit:.2f} from MAPPED LEVEL SPACING`という固定文言を、`range unit {unit:.2f} from {unit_source}`に変更し、実際に使われた出典を報告に反映する。

**`OBSERVED_CANDLE_REJECTED`は新規の警告コードである。既存の`RR_MISMATCH`等と同じ`report.warn`（エラーではなく警告）として扱う。理由: 個々のバーの拒否は、それ自体が致命的な安全違反ではなく、「一部データが使えなかった」という透明性のための情報だからである。ただし、この警告によってvalidatorがエラーを見逃す（拒否されたバーが多すぎてunitが不当に小さく/大きく計算される等）ことがないよう、3-4節の検証で確認すること。**

### 3-4. 検証手順

#### (a) 合成OHLC fixtureでの動作確認

新規fixture `project/engine-contract/test-sl-supplied-ohlc.nqx` を作成する。`test-sl-structural-distance.nqx`（第1追補で作成済み）の`M`行に`O|c=MISSING`があるはずなので、それを実OHLCバー列に差し替えた版を作る。バー列は本書4章のClaude事前検証で使った12本の合成データをそのまま使う（実市場データではない合成テスト値であることを、コメントまたはコミットメッセージ相当の記録に明記すること）。

```bash
python project/engine-contract/validate_nqx.py project/engine-contract/test-sl-supplied-ohlc.nqx
```

**期待結果（本書4章のClaude事前手計算）**: `unit ≈ 17.08`（`SUPPLIED OHLC RANGE`）。これは第2追補で確認したMAPPED LEVEL SPACING由来の`unit=59.43`より大幅に小さい。この`unit`を使うと`buffer`・`minimum`も小さくなるため、**同じEntry/SL/Invalidation数値でも、`SL_STRUCTURAL_DISTANCE`が発火するかどうかが変わりうる**。実際にどちらになるかを計算し、結果をそのまま報告すること（発火してもしなくても、それは正しい計算結果であり、閾値を調整して無理に一致させない）。

#### (b) 既存回帰の再確認

`test-sl-structural-distance.nqx`（`O|c=MISSING`のまま、変更しない）を含む既存の全fixtureが、本タスク追加後も同じ結果（第2追補完了報告の3-1・3-2節の値）を維持することを確認する。`O.c=MISSING`のケースはこれまで通り`level_spacing_unit`にフォールバックするはずであり、本タスクによって挙動が変わってはならない。

#### (c) 不正バーの拒否確認

`O.c`に意図的に不正な行（例: `INVALID OHLC GEOMETRY`に該当する高値<安値のバー、非数値のバー）を1行混入させたstdin fixtureを作り、`OBSERVED_CANDLE_REJECTED`警告が出ること、かつ残りの正当なバーだけで`unit`計算が継続されることを確認する。

#### (d) HTML側との再照合

第1・第2追補と同じ手順（Node VM上での純粋関数直接実行）で、3-4(a)の合成OHLC fixtureをHTML側の`rangeModel()`に通し、`unit`・`source`の値がPython側と一致するかを表形式で報告する。

---

## 4. Claudeによる事前検証の記録（参考情報）

本書作成前に、Claude側で以下の合成OHLCバー列（12本、MNQらしい値幅の3分足を想定した合成データ、実市場データではない）でTrue Range計算を検証済み:

```
バー(o,h,l,c): 12本、値幅はおよそ13〜27ptで推移する合成データ
TR series = [15, 14, 18, 27, 15, 17, 15, 17, 20, 13, 19, 15]
count = 12（全てtr>0のため採用）
ATR (simple mean) = 17.0833...
```

この`unit≈17.08`は、第2追補で確認したMAPPED LEVEL SPACING由来の`unit=59.43`とは大きく異なる。これはOHLC実測レンジとレベル間隔推定が本質的に別の情報源であることの裏付けであり、両方の経路を独立に検証する必要がある理由でもある。

---

## 5. 報告様式

`GPT_IMPLEMENTATION_REPORT.md`に追記専用で新しい節`## SLゲート強化・O-record True Range再現完了 (日付)`を追加する。既存追補と同じ記法を踏襲する。加えて:

- タスクF（仕様書訂正）の変更箇所
- 3-4(a)の結果が本書4章の事前手計算（`unit≈17.08`）と一致したか、および`SL_STRUCTURAL_DISTANCE`が実際に発火したかどうか（両方の結果をそのまま報告）
- 3-4(b)の既存回帰結果
- 3-4(c)の不正バー拒否確認結果
- 3-4(d)のHTML側再照合結果
- 残る既知の限界（あれば）

---

*本書は`O`レコードの実OHLC供給時のTrue Range再現を追加するものであり、`O|c=MISSING`時の既存ロジック（第2追補で完成済み）には一切手を加えない。*
