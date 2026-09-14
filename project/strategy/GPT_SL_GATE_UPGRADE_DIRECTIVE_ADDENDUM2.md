# GPT改良指示書 — 第2追補（`validate_nqx.py` の `proxy_unit` 縮退問題の修正）

- 宛先: GPT（実装担当）
- 発行: Fable（設計担当）/ 承認: ユーザー
- 上位文書: `project/strategy/GPT_SL_GATE_UPGRADE_DIRECTIVE.md`（原指示書）、`project/strategy/GPT_SL_GATE_UPGRADE_DIRECTIVE_ADDENDUM.md`（第1追補）。本書は第1追補の2-4節（`SL_STRUCTURAL_DISTANCE`監査）の`proxy_unit`計算方法のみを差し替える。他の変更点（Z構造レベル抽出、SL_INVALIDATION_TOO_CLOSE、タスクA〜C・E）はそのまま有効。
- 発行理由: `GPT_IMPLEMENTATION_REPORT.md`「SLゲート強化・タスクD再設計完了」節の報告（3-1節、296-307行目相当）を検証した結果、**第1追補が指定した`proxy_unit = |anchor - invalidation|`という計算方法自体に、想定より重大な欠陥があった**ことが判明した。実装（`validate_nqx.py:609-654`）はあなたが受け取った指示に対して正確であり、あなたの実装作業に誤りはない。責任は指示書側（第1追補）にある。

---

## 0. 何が間違っていたか（正直な経緯開示、2回目）

第1追補は「Python側にはOHLCがないので、HTML側の`rangeModel()`（直近12本OHLCのTrue Range平均、無ければレベル間隔中央値×0.42）と同じ計算はできない」と判断し、代替として`proxy_unit = |anchor - invalidation|`を指定した。

しかしこの判断は誤りだった。実トレード事例で検証すると：

- 今回のケースでは`O|c=MISSING`（観測OHLCなし）であり、**HTML側の`rangeModel()`もOHLCを使えず、`MAPPED LEVEL SPACING`フォールバック（`Z`レコード全レベルの間隔中央値×0.42）を使っていた**。
- このフォールバック計算は**OHLCを一切必要とせず、`Z`レコードのレベル一覧だけで完結する**。つまりPython側の`validate_nqx.py`は、第2-3節（第1追補）で追加した`structural_levels()`関数によって既に`Z`レコードの全レベルを抽出できているのだから、**同じ計算をそのまま再現できたはずだった**。
- 「アンカーとInvalidationの距離」という代替指標は、不要などころか**有害な簡略化**だった。今回のように「Invalidation価格が構造レベル（アンカー）と一致する」——これは決して珍しいケースではなく、シナリオ設計として自然に起こりうる——場合に、この代替指標は`0`に縮退し、監査そのものが無力化する。

GPTの報告（3-1節、306-307行目）はこの縮退を正確に検知し、数値を偽装せず正直に報告した。これは正しい対応だった。しかし今回、Claude側で手計算により、**HTML側の`rangeModel`の`MAPPED LEVEL SPACING`計算式を、`Z`レコードのレベル一覧だけを使ってPython側に完全に移植すれば、実トレード事例で正しく`tooTight`相当の判定を再現できる**ことを確認した（4章参照）。

したがって第2-3節「Python側は簡略化されており、HTML側の完全な代替にはならない」という限界の記述そのものは撤回しない（依然としてTrue Range平均によるOHLC実測レンジは再現できないため）が、**`O|c=MISSING`の場合に限っては、Python側とHTML側が完全に同じ計算式・同じ結果に到達できる**ことが分かった。この場合分けを反映する。

---

## 1. 絶対規範（原指示書・第1追補と同一、再掲）

1. 数値を発明しない。本書が指定する定数（`0.42`、`0.32`、`0.72`、`1.12`、最小フロア`2`/`4`）は全てHTML側`rangeModel()`・`structuralStopPlan()`に既に存在する定数の移植であり、新規の数値ではない。
2. 既存の安全条件を弱めない。
3. 後付け検証の禁止。
4. 成果の粉飾禁止。
5. 質問で作業を止めない。
6. 日本語報告。

---

## 2. 修正対象: `validate_nqx.py` の `SL_STRUCTURAL_DISTANCE` 監査（第1追補2-4節で追加したコード）

### 2-1. 現状（第1追補2-4節の実装、`validate_nqx.py:609-654`相当）

```python
proxy_unit = abs(anchor - (inv if inv is not None else sl))
buffer = max(proxy_unit * 0.32, 2)
recommended = anchor - buffer if side == "L" else anchor + buffer
beyond = sl <= recommended if side == "L" else sl >= recommended
risk_to_sl = abs(e_mid - sl)
minimum = max(proxy_unit * 0.72, 4)
```

この`proxy_unit`の計算式**全体を次節の計算に置き換える**。

### 2-2. 新しい`proxy_unit`（＝HTML側`rangeModel`の`MAPPED LEVEL SPACING`の移植）

`structural_levels(zone, aliases)`（第1追補2-3節で追加済み、変更不要）が返す全レベルの`(low, high)`タプル一覧から、以下を計算する新しいヘルパー関数を追加する:

```python
def median(values):
    """Same convention as the receiver's median(): sorted midpoint, average of
    the two central values on an even count. Returns None on an empty list."""
    vals = sorted(v for v in values if v is not None)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2


def level_spacing_unit(levels, pivot):
    """Mirrors the receiver's rangeModel() MAPPED LEVEL SPACING fallback: the
    median absolute distance from a pivot price to every declared level's low
    and high, scaled by 0.42, floored at 2. Used only when no OHLC tape is
    available — the same condition under which the receiver itself falls back
    to this identical calculation (rangeModel() cannot use True Range without
    O.c bars either)."""
    if pivot is None:
        return None
    gaps = []
    for lo, hi in levels:
        for price in (lo, hi):
            d = abs(pivot - price)
            if d > 0:
                gaps.append(d)
    gap = median(gaps)
    if gap is None:
        return None
    return max(gap * 0.42, 2)
```

`pivot`には、HTML側`rangeModel()`と同じ優先順位（`market.now` → 無ければ Decision Zone 中央値）を使う。`M`レコードの`px=`（`market["px"]`相当）を`scalar()`で読み、それが取れなければ`Z`レコードの`dz=`（Decision Zone）があればその中央値、どちらも無ければ`None`を返す（`None`の場合は本チェック自体をスキップし、既存の`SL_INVALIDATION_TOO_CLOSE`のみに委ねる）。

### 2-3. 監査本体の書き換え

第1追補2-4節の`SL_STRUCTURAL_DISTANCE`チェック全体を、次のロジックに置き換える:

```python
# Structural distance audit: mirror the receiver's structuralStopPlan logic.
# When the packet carries no OHLC tape (O.c is MISSING), the receiver itself
# falls back to rangeModel()'s MAPPED LEVEL SPACING — the median gap between
# a pivot price and every declared Z-record level, scaled by 0.42. That
# fallback needs no OHLC, so this validator can reproduce it exactly from the
# same Z-record levels it already extracts via structural_levels(). This
# replaces the addendum-1 proxy (|anchor - invalidation|), which degenerated
# to zero whenever the invalidation price coincided with the anchor level —
# a common, not rare, scenario-design pattern.
if entry is not None and sl is not None:
    e_lo, e_hi = entry
    levels = structural_levels(zone, aliases)
    if side == "L":
        candidates = [lo for lo, hi in levels if hi < e_lo - 1e-7]
        anchor = max(candidates) if candidates else None
    elif side == "S":
        candidates = [hi for lo, hi in levels if lo > e_hi + 1e-7]
        anchor = min(candidates) if candidates else None
    else:
        anchor = None
    if anchor is not None:
        pivot = scalar(market.get("px")) if market.get("px") else None
        if pivot is None and zone.get("dz"):
            dz_range = price_range(expand(zone["dz"], aliases))
            if dz_range:
                pivot = (dz_range[0] + dz_range[1]) / 2
        unit = level_spacing_unit(levels, pivot)
        if unit is not None:
            buffer = max(unit * 0.32, 2)
            recommended = anchor - buffer if side == "L" else anchor + buffer
            beyond = sl <= recommended if side == "L" else sl >= recommended
            risk_to_sl = abs(e_mid - sl)
            inv_dist = abs(e_mid - inv) * 1.12 if inv is not None else 0
            minimum = max(unit * 0.72, inv_dist, 4)
            if not beyond or risk_to_sl < minimum:
                report.error(
                    lineno,
                    "SL_STRUCTURAL_DISTANCE",
                    f"{tag}: SL ({sl}) sits inside the declared structural level "
                    f"at {anchor} with only {risk_to_sl:.2f} pt of risk to SL "
                    f"(recommended beyond {recommended:.2f}, minimum risk "
                    f"{minimum:.2f}, range unit {unit:.2f} from MAPPED LEVEL "
                    "SPACING)",
                    "move the stop beyond the nearest declared S/R/RBS/SBR/"
                    "QML/OCL level with a buffer sized to the market's own "
                    "declared level spacing",
                )
```

**この`minimum`の第2引数`inv_dist`は、第1追補が指定した「Invalidationまでの距離×1.12」という項目をそのまま保持している**（HTML側`structuralStopPlan`の`minimum = max(unit*.72, |entry-inv|*1.12, 4)`と完全一致させるため）。今回の実トレード事例では`inv == anchor`のため`inv_dist`自体は`unit*0.72`より小さい値になり、実質的に効かないが、`inv`が`anchor`と異なる別のケースでは意味を持つため保持する。

**`O.c`が観測OHLCを持つ場合（`MISSING`でない場合）の扱い**: この`level_spacing_unit`はあくまで`MAPPED LEVEL SPACING`フォールバックの再現であり、HTML側が真のTrue Range平均（`SUPPLIED OHLC RANGE`）を使うケースの完全な代替ではない。`O.c`にバーが存在する場合、Python側の`unit`はHTML側より粗い推定になりうる。これは残る既知の限界として報告書に明記する（完全な移植は本書の範囲外——`O`レコードのOHLC解析自体を`validate_nqx.py`に追加する変更は行わない）。

---

## 3. 検証手順

### 3-1. 実トレードfixtureでの再検証

第1追補で作成した`test-sl-structural-distance.nqx`（変更不要、そのまま使う）に対し、修正後のロジックで再実行する:

```bash
python project/engine-contract/validate_nqx.py project/engine-contract/test-sl-structural-distance.nqx
```

**期待結果（Claudeによる事前手計算）**:
- pivot = `28905.5`（`M|px=28905.5`より）
- レベル間隔（pivotから全15レベルへの絶対距離）の中央値 = `141.5`
- `unit = max(141.5*0.42, 2) = 59.43`
- アンカー = `28939.75`（R、Entry安値28951より下で最も近い）
- `buffer = max(59.43*0.32, 2) = 19.02`
- `recommended = 28939.75 - 19.02 = 28920.73`
- `risk_to_sl = |28957.5 - 28927.75| = 29.75`
- `inv_dist = |28957.5 - 28939.75| * 1.12 = 19.88`
- `minimum = max(59.43*0.72, 19.88, 4) = max(42.79, 19.88, 4) = 42.79`
- `beyond: 28927.75 <= 28920.73` → **False**
- `risk_to_sl(29.75) < minimum(42.79)` → **True**
- **`SL_STRUCTURAL_DISTANCE`エラーが発火するはず（exit 1）**

この結果が実際に得られるかを確認し、得られない場合は`pivot`の取得元（`M.px`の読み取り、`scalar()`の挙動）または`median()`の実装差異を切り分けて報告する。数値を一致させるためにロジック側を恣意的に調整しない。

### 3-2. 既存回帰の再確認

第1追補で行った以下のテストを、修正後のコードで再実行し、結果が変わっていないか（意図しない副作用がないか）を確認する:

```bash
python project/engine-contract/validate_nqx.py project/engine-contract/valid-sample.nqx
python project/engine-contract/validate_nqx.py project/engine-contract/invalid-sample.nqx
python project/engine-contract/validate_nqx.py project/engine-contract/test-sl-too-close.nqx
python project/engine-contract/validate_nqx.py project/engine-contract/mnq_2026-07-17_1856_test.nqx
```

- `valid-sample.nqx`（第1追補でZレコードに`29580`/`29532`を補完済み）が引き続き`exit 0`であることを確認する。新しい`level_spacing_unit`計算により、このサンプルのS1/S2で`minimum`がどう変わるか（大きくなって新たにFAILする可能性がある）を確認し、FAILした場合は既存のGPT_IMPLEMENTATION_REPORT.mdの記録（`stopStatus=STRUCTURAL`とHTML側で確認済み）と整合するか照合する。もし食い違いがあれば、`valid-sample.nqx`側のSLが本来HTML側でも`TOO TIGHT`寄りだった可能性を疑い、数値をそのまま報告する（隠さない）。
- 他の3ファイルについても、新しいエラーが増えていないか（増えている場合はその内容が正当か）を確認して報告する。

### 3-3. HTML側との再照合

第1追補3-2節と同じ手順（Node VM上での`parseNQX`＋`validateAll()`直接実行）で、`test-sl-structural-distance.nqx`をHTML側に通した際の`stopPlan.unit`・`stopPlan.minimum`・`stopPlan.recommended`の値と、本書3-1節でのPython側の値を突き合わせる。両者が一致することを表形式で報告する（第1追補の3-2節と同じ形式）。

**一致しない場合**、以下を優先的に疑って切り分ける:
- `pivot`の選択（HTML側は`market.now`優先、無ければ`market.dz`中央値、それも無ければ全既知価格の中央値——本書2-3節はDecision Zoneまでしかフォールバックしていないため、最後の「全既知価格の中央値」フォールバックが必要な場合はHTML側の`declaredLevelRows`の`anchorPrice()`関数（`project/app/nq-nightwatch-nqx-final.html`内、約3316-3321行目）を確認し、同等のフォールバックをPython側にも追加してよい。追加する場合はその旨と実装箇所を報告書に明記する。
- `median()`の偶数/奇数境界の扱い（本書2-2節のPython実装とHTML側`median()`関数、約3255-3258行目、で計算方法が完全に一致しているか再確認する）。

---

## 4. Claudeによる事前検証の記録（参考情報、GPTは再現しなくてよい）

本書作成前に、Claude側で以下の手計算をPythonで実行し、改良案が機能することを確認済み:

```
median(gaps) = 141.5
unit = max(141.5*0.42, 2) = 59.43
buffer = max(59.43*0.32, 2) = 19.02
recommended = 28939.75 - 19.02 = 28920.73
minimum = max(59.43*0.72, 17.75*1.12, 4) = max(42.79, 19.88, 4) = 42.79
beyond = 28927.75 <= 28920.73 = False
risk(29.75) < minimum(42.79) = True
tooTight相当 = True
```

この値は、GPTが第1追補3-2節で報告したHTML側の実測値（`unit=59.43`、`recommended=28920.7324`、`minimum=42.7896`）と完全に一致する。これは偶然ではなく、**`O|c=MISSING`の場合、HTML側の`rangeModel()`もこの同じ計算式（`MAPPED LEVEL SPACING`）に到達するため**であり、本書が指示する移植が正しい方向であることの直接的な根拠である。

---

## 5. 報告様式

`GPT_IMPLEMENTATION_REPORT.md`に追記専用で新しい節`## SLゲート強化・proxy_unit修正完了 (日付)`を追加する。原指示書・第1追補と同じ記法を踏襲する。加えて:

- 3-1節の結果が本書4章の事前手計算と一致したか
- 3-2節で既存fixture（特に`valid-sample.nqx`）に新たな挙動変化がなかったか、あった場合はその内容
- 3-3節のHTML側再照合結果
- 依然として残る既知の限界（`O.c`に実OHLCがある場合、True Range平均をPython側は再現できない、という点は変わらず有効）

---

*本書は第1追補の`SL_STRUCTURAL_DISTANCE`監査における`proxy_unit`計算方法のみを差し替える。`SL_INVALIDATION_TOO_CLOSE`・Z構造レベル抽出・タスクA/B/C/Eには一切手を加えない。*
