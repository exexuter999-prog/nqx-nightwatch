# R110: 消えた口座の ENTRY claim を名簿 publish の時点で捨てる

2026-09-18。`cloudflare/src/state_machine.js` のみ(deploy 済み)。

## 目的

口座が入れ替わると、旧口座を `accountScope` に持つ古い ENTRY claim が残り、Mini App の
SYSTEM タブが **`STALE SLOT`**(ENGINE の WARN)を出し続ける。説明文は
「old reservation — the broker is empty, it clears on the next entry」だが、
**次の新規 ENTRY が来るまで何時間でも居座る**。

## 何が起きていたか

R69 の stale release(消えた scope を口座名簿で不在証明にする)は
`applyEntryClaimEvent` の **CLAIM 到達時にしか走らない**。名簿はその前から
「その口座はブローカーに居ない」と証明できているのに、解放はずっと先送りされる。

2026-09-18 の口座入替(funded `LFF…0006` 消滅 → 評価 7 口座)で実測: 09-17 06:13 の claim が
翌日まで `CONSUMED` / `staleReleasable=true` のまま残った。R108 で engine 側の塞ぎは
取れていたので発注は止まっていないが、WARN が常時点灯していると**本物の stale claim を
見分けられなくなる**。

## 何が変わるか

`applyAccountEvent`(= 口座名簿の publish)の最後で、現在の claim が

* `CLAIMED` / `CONSUMED`(= まだ枠を握っている)であり、
* `claimScopeVanishedFromBroker` が真(名簿が verified・900 秒以内・**scope の全口座**が
  `configured かつ not missing` でも `unknown` でもない)であり、
* `staleEntryClaimReleasable` も真(claim が `staleReleaseSec` 超)

のときだけ、その場で `entryClaim = null` にする。記録は既存の形に合わせて
`entryStaleRelease = {entryKey, from, releasedAt, revision, reason: "SCOPE_VANISHED_FROM_BROKER"}`
と transition `entry_claim: … → STALE_RELEASED` を残し、publish の `reason` にも載せる
(黙って解くと「なぜ消えたのか」を後から追えない)。

**判定器は CLAIM 側とまったく同じものを使い、条件は一切緩めていない。** 加えて解放理由を
「scope が消えた」に限定している —— `staleEntryClaimReleasable` は新鮮なブローカー観測でも
真を返すが、それは CLAIM 側で扱う話で、名簿が運んでくる証拠ではない。

`entryClaim = null` は `emptyState()` の初期値と同じで、読み手はすべて null 安全
(`state.entryClaim?.` か `if (claim …)` のガード付き)。

## 戻し方

`applyAccountEvent` の末尾に足した `if (claim && …)` ブロックを消す。R69 の CLAIM 時
解放は残るので、「次の新規 ENTRY まで STALE SLOT が残る」挙動へ戻る。

## 検証コマンド

```powershell
cd cloudflare
node --test test/r110_roster_stale_release.test.mjs
npm test
```

`r110_roster_stale_release.test.mjs` は「捨ててよい形」と「捨ててはいけない形」の境界を
固定している: 生存口座・混在 scope・broker-only(unknown)・新しい claim・未検証の名簿・
900 秒より古い名簿・`RECOVERED` はいずれも**捨てない**。

R69 の 2 件(`r69_vanished_scope_release.test.mjs`)は、解放が CLAIM ではなく名簿 publish で
起きるようになったため期待値を更新した。判定器そのものの検査(`claimScopeVanishedFromBroker`)は
名簿が載る前の claim を取っておいて、これまでどおり直接呼んで確かめている。

## 現場での見分け方

SYSTEM タブが `STALE SLOT` のとき:

```powershell
python -c "import json,nqx_state; v=nqx_state.fetch_state_quiet(); c=(v or {}).get('entryClaim') or {}; print(c.get('state'), c.get('staleReleasable'), (c.get('executionIntent') or {}).get('accountScope'))"
```

`accountScope` が今の `CROSSTRADE_ACCOUNTS` に無い口座なら、次の名簿 publish(= 次の
3 分サイクル)で消える。消えないなら名簿が verified でないか古い。
engine 側の塞ぎは `docs/R108_VANISHED_SCOPE_RECOVERY_DEFER.md`。
