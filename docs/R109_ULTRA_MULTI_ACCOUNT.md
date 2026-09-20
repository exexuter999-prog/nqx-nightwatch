# R109: ULTRA を複数口座で張れるようにする

2026-09-18 ユーザー決定。Python(`monitor_publish` / `execution_contract`)、
Worker(`cloudflare/src/state_machine.js`)、Mini App(`telegram_mini_app/app.js`)の 3 層。
**Worker と Mini App は deploy が要る**(人が叩く)。

## 目的

R47 の口座別 ULTRA は「同時に 1 口座だけ」だった。評価口座が 7 つになり、
1 口座ずつしか ULTRA を張れないのは運用上の制約でしかないので、複数口座へ広げる。

## 前提 —— なぜ「同じ利益目標の口座だけ」なのか

`execution_intent` は **scope 全体で枚数を 1 つしか持たない**(`qty` + `accountScope`)。
口座別に違う枚数を運ぶのは R87 の直接発注経路(intent v2)の仕事で、別セッションで進行中。

ULTRA の枚数は `ultra_mode.required_qty_split(profitTarget, ...)` —— **その口座の利益目標に
届く枚数**である。目標が割れている口座を同時に選ぶと、1 つの枚数では必ずどれかの口座が
目標に届かない。それを黙って出すのは「達成不能なら見送り」(2026-08-30 決定)と矛盾するので、
**設定側で弾く**。目標が同じなら同じ幾何に対して同じ枚数が出るので、1 つの intent で
全口座が自分の目標に届く。

## 何が変わるか

### 規則 1: 同じ利益目標の口座だけ同時に ULTRA にできる

`monitor_publish._apply_ultra_prefs` が、`ultra: true` の口座の `profitTarget` を集めて
1 種類でなければシナリオを WATCH へ落とす(注記 `ULTRA accounts must share one profit target`)。
Mini App も同じ規則で、目標が違う行を同時に ON にできず、保存前にも検証する
(目標未設定の行を ON のまま保存することもできない)。

### 規則 2: 条件を満たさない口座は外して残りで出す

R45(口座別リスク上限)と同じ考え方。次の口座は **その口座だけ落として** 残りで発注する。

| 落ちる理由 | 記録される値 |
|---|---|
| 実行スコープ(契約の `accountScope`)に居ない | `NOT_IN_EXECUTABLE_SCOPE` |
| 残機データが取れない | `NO_LIFELINE_DATA` |
| 残ドローダウン不足・目標に届かない | `DRAWDOWN_EXCEEDED` / `QTY_NOT_COMPUTABLE` など |

落とした口座は `executionContract.excludedAccounts`(`{account, reason, source: "ULTRA_PREFS"}`)と
サイクル注記 `ULTRA excluded: …` に必ず残す。**1 口座も残らないときだけ**シナリオを見送る
(R47 の「達成不能なら見送り」はそのまま)。

### リスク上限は一番薄い口座で凍結する

scope 全体に同じ枚数が飛ぶので、`riskCapDollars` は残った口座の **最小の残ドローダウン**
(アプリの `maxDrawdown` があればその狭い側)で凍結する。Worker の cap 判定はこの凍結値を
見るので、一番薄い口座が払える額が正本になる。`riskCapSource` は従来どおり
`ACCOUNT_DRAWDOWN_BUFFER`。

### 保存時の検証(Worker の `validateAccountPrefs`)

**2026-09-18 追記 — 最初の実装で漏らしていた 5 つ目のゲート。** `POST /api/accountPrefs` の
検証が `ultraCount > 1` を拒否していたため、UI を複数選択にしても保存の時点で
`PREFS SAVE FAILED — ULTRA can be enabled for at most one account (claim scope)` になった。
今は上限を `ultra.maxAccounts` にし、併せて **目標一致**と**目標必須**も保存時に弾く
(publish 側は WATCH へ落とすだけなので、それでは「設定したのに出ない」になる)。

ULTRA の制限は **5 か所**にある。触るときは全部そろえること:

1. `monitor_publish._apply_ultra_prefs`(publish 前のサイジング)
2. `execution_contract.evaluate` の ULTRA 分岐(`ULTRA_SCOPE_OUT_OF_ENVELOPE`)
3. `state_machine.js` の `evaluateExecutionContract`(同名のミラー)
4. `state_machine.js` の `validateAccountPrefs`(**保存時**)
5. `telegram_mini_app/app.js` のトグルと保存前検証

### スコープ検査の名前が変わった

Python と Worker の両方で `ULTRA_SCOPE_NOT_SINGLE` → **`ULTRA_SCOPE_OUT_OF_ENVELOPE`**。
弾くのは **0 口座** と **`ultra.maxAccounts`(現在 8)超え**だけ。

## 戻し方

`execution_contract.json` の `ultra.maxAccounts` を **1** にする。Python も Worker も
同じ値を見るので、それだけで R47 と同じ「1 口座のみ」に戻る(Worker は deploy が要る)。
Mini App 側は 2 口座目を ON にしようとすると目標一致の検査に通っても publish 側で
`ULTRA_SCOPE_OUT_OF_ENVELOPE` になるため、戻すときは UI も合わせて戻すのが分かりやすい。

コードごと戻す場合は次の 4 か所:
`monitor_publish._apply_ultra_prefs` / `execution_contract.evaluate` の ULTRA 分岐 /
`state_machine.js` の `ultraScope` 判定 / `app.js` のトグルと保存検証。

## 段取り(この順でないと ULTRA が黙って止まる)

1. **Worker を deploy する**(`cd cloudflare && npm run deploy`)。古い Worker は
   `ULTRA_SCOPE_NOT_SINGLE` で 2 口座以上を弾く。fail-closed なので誤発注にはならないが、
   **理由が画面に出ないまま ULTRA が出なくなる**。
2. **Mini App を deploy する**。UI が複数選択に変わる。
3. アプリの設定で、同じ利益目標の口座を複数 ON にして保存する。

deploy 前でも **1 口座の ULTRA は従来どおり動く**(`ultraScope.length === 1` は新旧どちらの
Worker でも通る)。

## 検証コマンド

```powershell
python tests/test_r109_ultra_multi_account.py
python tests/test_r47_ultra_scenario.py
python tests/test_ultra_mode.py
python tests/test_ultra_envelope.py
python tests/test_r30_ultra_challenge.py
python tests/test_r52_ultra_dryrun_display.py
```

```powershell
cd cloudflare
npm test
```

```powershell
cd telegram_mini_app
npm run ci
```

いずれも送信しない。`test_r109` と `test_r47` は node の子プロセスで **本物の JS 検証器**
(`validateScenario` / `evaluateExecutionContract`)に通して Python↔JS の整合を固定している。
ULTRA を触ったら **必ず両方**回すこと(片方だけ緩むと、producer が出した計画を Worker が
黙って弾く)。監視窓の中で回すときは `.secrets` を抜いた隔離コピーで。

## 2026-09-18 時点の実測値

評価 7 口座はいずれも残高 $50,000 / 残ドローダウン $2,000。アプリの利益目標は
LFE 4 口座が $250、MFFU 3 口座が $350 で **割れている**ので、このままでは 7 口座まとめて
ULTRA にはできない。まとめたい場合は目標を揃える(揃えた側の口座数ぶんだけ ULTRA になる)。
