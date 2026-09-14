# GPT改良指示書 — 追補（SLゲート強化・タスクD再設計）

- 宛先: GPT（実装担当）
- 発行: Fable（設計担当）/ 承認: ユーザー
- 上位文書: `project/strategy/GPT_SL_GATE_UPGRADE_DIRECTIVE.md`（以下「原指示書」。本追補は原指示書のタスクDのみを差し替える。他のタスクA/B/C/Eは原指示書のまま有効）
- 発行理由: `GPT_IMPLEMENTATION_REPORT.md`「SLゲート強化完了 (2026-07-23)」節の報告を精査した結果、**原指示書のタスクD自体に要件定義ミスがあった**ことが判明した。実装（`validate_nqx.py:564-578`）はあなたが受け取った指示に対して正確であり、あなたの実装作業に誤りはない。責任は指示書側にある。

---

## 0. 何が間違っていたか（正直な経緯開示）

原指示書は「2026-07-20実トレード事例のInvalidation(28,939.75)とSL(28,927.75)の間が12ptしかない」ことを問題視し、その距離をチェックするタスクDを指示した。

しかし実際に計算すると：
- Entry中央値 = `(28951+28964)/2 = 28957.5`
- Risk（Entry中央値からSLまで） = `|28957.5 - 28927.75| = 29.75pt`
- Invalidation-SL間 = `|28939.75 - 27927.75| = 12.00pt`
- 比率 = `12.00 / 29.75 = 40.34%`

原指示書が指定した閾値「15%未満でエラー」に対し、実際の比率は40.34%であり、**そもそもこの数値例は閾値に抵触しない**。あなたの実装（`validate_nqx.py:567`の`abs(inv - sl) < risk_to_sl * 0.15`）はこの閾値を正確に実装しており、`test-sl-too-close.nqx`がexit 0になったのは実装不良ではなく、**指示書が渡した数値例と、指示書が渡した閾値が数学的に矛盾していた**ためである。あなたの5-1節での報告（「この結果は実装不良ではなく〜」）は完全に正確だった。

### 今回の実トレードの本当の問題

今回のトレードで実際に起きていた問題は「Invalidationがどれだけ近いか」ではなく、**「Hard SL(28,927.75)が、直近の構造レベル（安値28,928、および今回のパケットではRレベルの28,939.75）に対してあまりに近い」**という、全く別の軸の問題だった。これはHTML側の`structuralStopPlan`（原指示書1-1節で説明した既存ロジック）が検出するべき種類の違反であり、実際に手計算で検証したところ正しく機能することを確認した（3章参照）。

`validate_nqx.py`はパケットのテキストを読むだけの検証器であり、`Z`レコード（`s=`/`r=`等）の中身を構造化データとしてパースしていない（`records`リストに`(lineno, "Z", fields)`として積まれるだけで、以降の検証ループでは`Z`タグのフィールドは一度も参照されない）。したがって「SLが直近の構造レベルに対して近すぎる」という判定は、**現状のvalidate_nqx.pyには原理的に実行不可能**だった。これを見落としたまま「Invalidation-SL距離」という代替の軸をチェックさせる指示を出したのが、原指示書の設計ミスである。

---

## 1. 絶対規範（原指示書と同一、再掲）

1. 数値を発明しない。本追補が指定する定数（後述）以外を書き込まない。
2. 既存の安全条件を弱めない。原指示書のタスクA〜C・Eで既に入っている変更は一切戻さない。
3. 後付け検証の禁止。
4. 成果の粉飾禁止。今回のように指示書側に誤りがあった場合、それを次回以降も正直に指摘してよい（減点対象ではない）。
5. 質問で作業を止めない。
6. 日本語報告。

---

## 2. タスクD再設計: `validate_nqx.py` にZレコード構造レベルとのSL距離チェックを追加する

### 2-1. 現状の実装（そのまま残す。削除しない）

`validate_nqx.py:564-578`に追加済みの`SL_INVALIDATION_TOO_CLOSE`チェックは**そのまま残す**。これは「Invalidationがどれだけ早く確定するか」という、独立して意味のある別のチェックであり、今回発見した設計ミスとは無関係に有効な安全条件である。削除・弱体化はしない。

### 2-2. 新規追加: `Z`レコードのパースと構造レベル抽出

`validate.py`の`validate()`関数内、現状`Z`タグが`records`に積まれるだけで個別処理されていない箇所（`elif tag == "E": event = fields`の並び、約388行目付近）に、以下を追加する:

```python
elif tag == "Z":
    if zone:
        report.error(lineno, "DUP_RECORD", "duplicate Z record",
                     "emit exactly one Z record")
    zone, zone_line = fields, lineno
```

（`zone = {}` / `zone_line = 0`を、`market = {}` 等と同じ場所で初期化する）

### 2-3. Zレコードから構造レベルの価格リストを抽出する関数を追加する

`validate_nqx.py`の関数群（`price_range`や`scalar`の近く）に、新しいヘルパー関数を追加する:

```python
def structural_levels(zone_fields, aliases):
    """Extract point/range prices from Z-record S/R/RBS/SBR/QML/OCL/A/V fields
    as a flat list of (low, high) tuples. Comma-separated lists inside one field
    are split independently so a list like s=28898,28835.38,28814 becomes three
    separate levels, mirroring the receiver's own nqxPriceList behaviour."""
    out = []
    for key in ("s", "r", "rbs", "sbr", "qml", "ocl", "a", "v"):
        raw = zone_fields.get(key)
        if not raw:
            continue
        for item in split_escaped(expand(raw, aliases), ","):
            item = item.strip()
            if not item:
                continue
            rng = price_range(item)
            if rng:
                out.append(rng)
                continue
            val = scalar(item)
            if val is not None:
                out.append((val, val))
    return out
```

**このコードはHTML側の`nqxPriceList`＋`setLevel`の挙動（`project/app/nq-nightwatch-nqx-final.html`の`parseNQX`関数内、約2713-2723行目）を、パケットテキストの検証という異なる文脈に合わせて移植したものである。HTML側のロジックを直接importまたはコピーするのではなく、Python側で独立に書く（clean-room。絶対規範2「保護されたソースコードを複製しない」はHTML側にも本来適用されるべきだが、ここは同一プロジェクト内の同一契約に対する2つの実装であるため、価格リストの分割という機械的な処理に限り、同じ考え方を採用してよい）**。

### 2-4. SL距離チェック本体を追加する

シナリオループ内、既存の`SL_INVALIDATION_TOO_CLOSE`チェック（2-1節、`validate_nqx.py:564-578`）の直後に追加する:

```python
# Structural distance audit: mirror the receiver's structuralStopPlan logic.
# The stop must sit meaningfully beyond the nearest declared structural level
# on the losing side of the trade, not merely satisfy the invalidation-order
# inequality. This catches the actual 2026-07-20 failure mode (SL parked just
# above a recent swing low / R-level), which the invalidation-distance check
# above cannot see because it only compares SL against the invalidation price,
# not against the market's own declared S/R levels.
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
        # Buffer and minimum-risk floor use the same directive-assigned
        # constants as the receiver's structuralStopPlan (0.32x / 0.72x / 1.12x
        # of a level-spacing proxy). validate_nqx.py has no OHLC tape and no
        # access to the receiver's rangeModel(), so the proxy unit here is the
        # distance from the anchor to the invalidation (or to SL if no
        # invalidation is readable) — a conservative, always-available
        # substitute for the receiver's ATR/level-spacing estimate.
        proxy_unit = abs(anchor - (inv if inv is not None else sl))
        buffer = max(proxy_unit * 0.32, 2)
        recommended = anchor - buffer if side == "L" else anchor + buffer
        beyond = sl <= recommended if side == "L" else sl >= recommended
        risk_to_sl = abs(e_mid - sl)
        minimum = max(proxy_unit * 0.72, 4)
        if not beyond or risk_to_sl < minimum:
            report.error(
                lineno,
                "SL_STRUCTURAL_DISTANCE",
                f"{tag}: SL ({sl}) sits inside the declared structural level at "
                f"{anchor} with only {risk_to_sl:.2f} pt of risk to SL "
                f"(recommended beyond {recommended:.2f}, minimum risk {minimum:.2f})",
                "move the stop beyond the nearest declared S/R/RBS/SBR/QML/OCL "
                "level with a buffer, not just outside the invalidation price",
            )
```

**重要な注意**: このPython側の`proxy_unit`計算は、HTML側の`rangeModel()`（直近12本OHLCのTrue Range平均、無ければレベル間隔中央値×0.42）を再現**しない**。パケット単体の検証器はOHLC配列を持たないため、同一の計算はできない。ここでは意図的に簡略化した代替指標（アンカーからInvalidationまでの距離）を使う。この簡略化により、**validate_nqx.py側のBLOCKED判定はHTML側より粗く、HTML側でBLOCKEDになるケースの一部を見逃す可能性がある**。これは既知の限界として報告書に明記すること。両者を完全に一致させることは本追補の目的ではない——validate_nqx.pyは「明らかに近すぎる」ケースの安全網であり、精密な判定はHTML側の`structuralStopPlan`に委ねる、という役割分担を維持する。

### 2-5. `nqx1-spec.md`への追記

原指示書タスクEで追加したinvariant 2aの直後に、以下を追加する:

```
2b. The validator additionally flags SL_STRUCTURAL_DISTANCE when the hard stop
    does not clear the nearest declared S/R/RBS/SBR/QML/OCL level (on the
    losing side of the trade) by a reasonable buffer. This check uses a
    simplified proxy for market range since the transport-only validator has
    no OHLC tape; the receiver's own structuralStopPlan (which does have
    access to observed range) remains the authoritative distance check.
```

---

## 3. 検証手順（本追補固有）

### 3-1. 実トレード事例パケットでの回帰

新規fixture `project/engine-contract/test-sl-structural-distance.nqx` を作成する。内容は2026-07-20実トレード事例のScenario 1相当を最小構成で再現する:

```text
!NQX/1
M|sy=MNQ1!|tf=3M/15M/45M|at=2026-07-20 18:37 JST|ss=OTHER|sf=3M,15M,45M|dq=M|px=28905.5|qm=H
Z|s=28898,28835.38,28814,28797.75,28706.75,28683,28558.75,28408.25|r=28939.75,28951,28964,29047,29059.5,29089,29220
G|n=1|ip=FLAT / NO TRADE|mx=NOT REQUIRED|io=S1 PASS|rn=TEST FIXTURE ONLY
S1|n=上側価値受容ロングTEST|d=L|st=W|su=BC|ha=M|lt=multi-session resistance cluster|zq=S|fr=BT|en=28951~28964|sl=28927.75|tp=29047,29059.5,29089|rr=3.01,3.43,4.42|iv=28939.75|od=TEST|vu=2026-07-20 19:30 JST|eh=no new entry until event clock verified|fm=false acceptance|sd=15M closes below 28898|ef=OBSERVED bullish rebound|ea=OBSERVED HTF bearish
```

実行:
```bash
python project/engine-contract/validate_nqx.py project/engine-contract/test-sl-structural-distance.nqx
```

期待結果: `SL_STRUCTURAL_DISTANCE`エラーが出ること。手計算による事前確認（本追補作成者による）:
- Entry中央値=28957.5、SL=28927.75、risk=29.75
- アンカー候補（LONGなのでEntry安値28951より下のレベル）: R=28939.75（最も近い）
- `proxy_unit = |28939.75 - 28939.75(inv)| = 0`（Invalidation自体がアンカー価格と一致するため、この特定のfixtureでは`proxy_unit`が0になり、`buffer`は`max(0, 2)=2`、`minimum`は`max(0, 4)=4`という最小値フロアに落ちる可能性が高い。この場合、`recommended = 28939.75 - 2 = 28937.75`となり、`sl(28927.75) <= 28937.75`は真＝`beyond=True`となるため、この特定の簡略化ロジックでは**エラーにならない可能性がある**。
- **この結果が出た場合**、それは実装不良ではなく、2-4節で明記した「Python側の簡略化により一部ケースを見逃す既知の限界」の実例である。数値を偽装して無理にエラーを出させようとしない。その代わり、報告書に「このfixtureではproxy_unitがInvalidation価格との一致により縮退し、簡略化ロジックの限界を実例として確認した」と正直に記録すること。
- 代替案として、`proxy_unit`が極端に小さい場合のフロア値を`max(proxy_unit*0.72, 4)`ではなく、Entry帯の幅（`e_hi - e_lo`）も候補に加えた`max(proxy_unit*0.72, (e_hi-e_lo)*0.5, 4)`に変更することを許可する。ただしこの変更を行う場合は、変更後の計算式と根拠を報告書に明記し、`0.5`という係数も「本追補が指定する値」として扱う（絶対規範1）。

**この検証で实際にエラーが出るか出ないかに関わらず、両方の結果を正直に報告すること。エラーが出なかった場合、それは「validate_nqx.py側の簡略化されたstructural distance監査は、HTML側のより精密な`structuralStopPlan`の完全な代替ではない」という事実を裏付ける重要な発見であり、隠すべきものではない。**

### 3-2. HTML側の実データ回帰（既存タスクAの追加検証）

原指示書5-2節で使った「既存デモの`minimum=50.00pt`」という代替値ではなく、**本追補作成者が手計算で確認した実トレード事例そのものの数値**を使って、`structuralStopPlan`が実際にBLOCKED相当の`TOO TIGHT`＋`risk<minimum*0.70`を返すことを確認する。

手計算による事前結果（本追補作成者、`rangeModel`のMAPPED LEVEL SPACINGフォールバックを想定、pivot=market.now=28905.5、Z record levelsは3-1節のfixtureと同一）:
- `risk (entry_mid to SL) = 29.75`
- `minimum = 42.79`（`max(unit*0.72, |entry-inv|*1.12, 4)`、unit≈59.43）
- `minimum * 0.70 = 29.95`
- `risk(29.75) < minimum*0.70(29.95)` → **真（僅差だがBLOCKED条件を満たす）**

3-1節のfixtureをNightwatch本体（`validateAll()`経由）で読み込ませ、この手計算結果と実際の出力が一致するかを確認する。前回のGPT実装検証（`GPT_IMPLEMENTATION_REPORT.md`5-2節）はブラウザの`file://`ポリシーによりDOM実行ができなかったため、前回同様Node上でのスクリプト抽出＋関数直接呼び出しによる検証で構わない。ただし今回は「既存デモパケット」ではなく、**3-1節のfixtureパケットをそのまま`parseNQX`→`validateAll`相当の経路に通す**こと。

一致しない場合（手計算とコード出力が食い違う場合）は、どちらが正しいかを再検証し、食い違いの原因（`pivot`の選び方の違い、`median`の偶数/奇数境界処理の違いなど）を特定して報告する。数値を一致させるために手計算側・コード側のどちらかを恣意的に調整しない。

---

## 4. 報告様式

`GPT_IMPLEMENTATION_REPORT.md`に追記専用で新しい節 `## SLゲート強化・タスクD再設計完了 (日付)` を追加する。原指示書と同じ記法（完了条件対照表・検証結果・自己監査節）を踏襲する。加えて、以下を明記する:

- 3-1節の検証で`SL_STRUCTURAL_DISTANCE`が実際に出たか出なかったか、出なかった場合はその理由（proxy_unit縮退等）
- 3-2節の手計算とコード出力の一致・不一致
- 原指示書のタスクDが指示書側のミスであったことに対する自己監査（「今回は実装ミスではなく指示側の要件定義ミスだったため、絶対規範4は該当しないが、次回以降、指示書の数値例自体が指示書の閾値と整合するか、実装前に検算するプロセスがあれば防げた」等の振り返りがあれば記録してよい）

---

*本追補は原指示書のタスクDのみを差し替える。タスクA/B/C/Eは既に正しく実装されており、変更・後退させない。*
