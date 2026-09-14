# R38 LTA 2口座への移行（Phase 1 / 2 完了）

2026-08-24。運用先を Apex 100K 1口座から LTA 2口座へ移す。
新条件は各口座とも **EOD トレーリング DD $3,000 / 利益目標 $6,000 /
一貫性ルール無し / 最低5日・期限無制限**。

## 見つかったこと（先に）

**`CROSSTRADE_ACCOUNTS` に設定されていた `APEX0000000000010` は、CrossTrade の
口座一覧に存在しなかった。** 設定にはそのまま残っており、発注していれば
宛先不明で失敗していた。

これは 2026-08-21 と**同型の事故**である。当時も 3口座のうち2つが
`unknown_account` になっており、`HANDOFF.md` に「気付かずに発注していれば
3件中2件が失敗していた」と記録されている。

**にもかかわらず、口座一覧を取る手段はリポジトリに実装されていなかった。**
過去2回とも手作業（curl）で叩いて結果を Markdown へ転記しただけで、
再現手段が残っていない。だから同じ事故が繰り返された。

## Phase 1 — 口座一覧の取得

`broker_status.py` に読み取り専用の照会を足した。

- `CrossTradeAdapter.list_accounts()` — `GET {base}/accounts`。既存の
  `_url()` / `_orders_url()` と同じ組み立て規則、認証も同じ Bearer。
- `query_accounts()` — `query_position()` と同じ規約。**失敗を「口座が無い」と
  誤解させない**（`verified=False` を返し `accounts` は空のまま）。
- `broker_status.py --accounts [--json]`

設計上の注意を2つ入れてある。

**`configured` に依存しない。** 口座一覧が要るのは**まさに口座がまだ分からない
とき**で、`configured` は `account` の存在を要求するため使えない。
必要なのはトークンと妥当な base だけ。

**認識できない形は推測で埋めない。** 応答形が確定していないので、正規化した
`id`/`accountId`/`status`/`environment` と併せて `raw` を必ず残す。
ラベルが取れない行は `usable=False` として扱う（推測した文字列を発注経路へ入れない）。

### 実測結果（2026-08-24）

| 口座 | accountId | env | 判定 |
|---|---|---|---|
| `LFE00000000000003` | 90000103 | demo | 生存（旧 Lucid・不使用） |
| `LTATANOBA1000000000001` | 90000001 | demo | **LTA-A（発注先）** |
| `LTATANOBA1000000000002` | 90000002 | demo | **LTA-B（R40で発注先へ追加）** |
| `APEX0000000000010` | — | — | **一覧に無い** |

LTA 2口座はいずれも建玉照会が `verified=true / qty=0` で通ることを確認済み。

応答に残高・ドローダウン・目標は**含まれない**ので、`LIFELINE_*` の手動更新は
引き続き必須（下記）。

## Phase 2 — 新条件の設定

R40で `.secrets/crosstrade.env` を LTA-A/B 2口座ミラー経路へ切り替えた。

```
CROSSTRADE_ACCOUNTS=LTATANOBA1000000000001,LTATANOBA1000000000002
RISK_LTATANOBA1000000000001=240
LIFELINE_LTATANOBA1000000000001=3000
TARGET_LTATANOBA1000000000001=6000
DAYGOAL_LTATANOBA1000000000001=900
RISK_LTATANOBA1000000000002=240
LIFELINE_LTATANOBA1000000000002=3000
TARGET_LTATANOBA1000000000002=6000
DAYGOAL_LTATANOBA1000000000002=900
NQX_DAY_LOSS_LIMIT=-960
```

`TARGET_*` は**このリポジトリで初めて設定された**。これが無い間、ULTRA は
全口座 `PROFIT_TARGET_MISSING` で INELIGIBLE だった。Phase 3 の可変枚数も
この値を入力に取る。

### B 口座と日次ガード

Bの `RISK/LIFELINE/TARGET/DAYGOAL` は経路追加と同時に有効化した。
1シグナルの最大損失は2口座合算$480なので、2連敗停止を維持するため
`NQX_DAY_LOSS_LIMIT=-960` とする。

### DAYGOAL の意味が変わった

旧 Apex は一貫性ルールがあり `DAYGOAL = min(利益目標 × consistency上限, 残機)` で
決めていた。**新条件に一貫性ルールは無いので、この算式は根拠が消えた。**

代わりに DAYGOAL は **EOD トレーリング DD に対する利確ブレーキ**として意味を持つ。
床が終値で切り上がるので、大きく勝った翌日に同額を吐き出すと開始時より不利になる。

### 検証

```
configured_accounts       : ['LTATANOBA1000000000001', 'LTATANOBA1000000000002']
risk_cap()                : $240.00 (execution_contract.defaultCapDollars)
  → SL 上限 = 60.0 pt
dayguard の口座集合        : LTA-A/B  ← 経路と一致
build_accounts_payload    : buffer 3000.0 / profitTarget 6000.0
```

`python tests/run_all.py` **43 ファイル通過**（`tests/test_broker_crosstrade.py` に
口座一覧の検証を 11 件追加。本物の CrossTrade への接続は 0 回・全てローカルモック）。

## R40 — 2口座ミラー実装

通常モードは `accountMode=MIRRORED / maxAccounts=2`。ENTRY claimは口座別に
複製せず、1シグナルにつき1つのまま4脚を凍結する。照会・position generation・
所有権・MODIFY・FLATTENだけを口座別に分離した。これによりAの建玉確認でBを
変更する旧問題を除去しつつ、同じシグナルの二重発注防止を維持する。

### EOD トレーリングの実装が無い

`LIFELINE_*` は手動更新の env 値。コードにトレーリングの概念は存在しない
（`EOD` / `trailing drawdown` はリポジトリ全体で `challenge_calc.py` が
「別ゲート」と未実装を告白している1箇所のみ）。
Phase 3 で枚数がこの値に比例するようになるため、**更新漏れがそのまま
サイズ誤りになる**。起動前チェックリスト（`CLAUDE.md` §6.1）に明記した。

### 1R $300 には契約 JSON の変更が要る

`execution_contract.py` の `risk_cap()` は `defaultCapDollars`（$240）を**天井**と
して扱い、`RISK_*` も `MAX_RISK_DOLLARS` も**狭める方向にしか効かない**。
`RISK_...=300` と書いても 240 に丸められる。Phase 3 で契約側を上げる。

## 次（Phase 3 / 4）

計画は `C:\Users\exexu\.claude\plans\melodic-twirling-firefly.md`。

- **Phase 3**: リスク額から枚数を逆算する可変枚数。`ultra_mode.py` に
  `required_qty_risk_budget()` を足し、2枚固定のリテラル13箇所を契約由来へ。
  `monitor_publish._sl_caps()` の単位不整合（正常経路は1枚あたりpt、fallback は
  2枚合計pt）を**先に**直すこと —— 可変枚数にするとこのズレがサイズ誤りになる。
- **Phase 4**: claim key の口座次元。安全核なので単独で。
