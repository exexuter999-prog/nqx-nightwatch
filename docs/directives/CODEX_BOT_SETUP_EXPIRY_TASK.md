# Codex 向けタスク: 失効したセットアップ通知から発注できてしまう穴を塞ぐ

対象: [telegram_bot.py](telegram_bot.py) / [notify.py](notify.py)

**2026-08-12 に実損が発生した。** 机上の懸念ではなく再現済みの不具合。

---

## 発生した事故

```
14:13  notify.py が SHORT 29,697 / SL 29,704 / TP 29,684.5 を通知
       → PREVIEW ボタン付き
14:16  価格が TP を通過したため「取消」をテキストで通知
       → だが 14:13 の通知のボタンは押せるまま
14:28  古い通知の PREVIEW を誤タップ → ドライラン → 送信
       当時の価格 29,685。29,697 の売り指値が待機状態に
14:33  29,688 → 29,706 の陽線で 29,697 約定、同じ足で SL 29,704 ヒット
```

**損失 −$28.00**(7pt × 2枚 × $2.00)。金額は小さいが、**15分前に無効と判断したセットアップが何の抵抗もなく実発注まで到達した**という事実が問題。

---

## 根本原因

`go:<token>` の送信ボタンには token 照合があるのに、**その手前の PREVIEW ボタンには何の検証もない。**

```python
def scenario_callback(order_side, qty, entry, sl, tp=None):
    values = [order_side.lower(), str(int(qty)), f"{float(entry):g}", f"{float(sl):g}"]
    if tp is not None:
        values.append(f"{float(tp):g}")
    return "scenario:order:" + ":".join(values)
    # → "scenario:order:sell:2:29697:29704:29684.5"
```

注文条件が callback_data に丸ごと入っているので、**このボタンは永久に有効**。
Telegram の履歴を遡れば何時間前の通知でも発注できる。

---

## ⚠️ 壊してはいけないもの

[CODEX_BOT_TASK.md](CODEX_BOT_TASK.md) の「壊してはいけないもの」は全て継続。特に:

1. **チャットID認証(通常メッセージ・callback_query の2箇所)** — 完全一致比較を維持
2. **二段階確認**: ドライラン → `go:<token>` 送信。`do_confirm()` は run_order の前に `clear_pending()`
3. **発注ロジックは order.py に委譲**。Bot 側で発注判断をしない
4. **拒否時に送信ボタンを出さない**

**今回の修正はガードを1枚増やすもので、既存のガードを置き換えるものではない。**

---

## 実装するもの

### A. セットアップに発行時刻と TTL を持たせる

`scenario_callback` に発行時刻(UNIX秒)を先頭で埋め込む。

```
scenario:order:<issued_epoch>:<side>:<qty>:<entry>:<sl>[:<tp>]
```

押された時点で `now - issued_epoch` を計算し、**TTL 超過なら発注経路に入らない**。

- **TTL = 600秒(10分)**。[snapshot.py](snapshot.py) の鮮度警告と同じ基準に揃える
- 超過時は「⟦失効⟧ このセットアップは N分前のものです。最新の市況は /price で確認してください」とだけ返す
- **ドライランも走らせない。** 失効したものは表示すらしない

callback_data は Telegram の仕様で **64バイト上限**。現在 `scenario:order:sell:2:29697:29704:29684.5` で42バイト、epoch(10桁)+区切りで53バイト。収まるが余裕は少ないので、**長さを assert するテストを入れること**。

### B. 明示的な無効化

TTL 内でも「もう無効」と判断される場面がある(今回がそれ。3分で価格が TP を通過した)。

`notify.py` に無効化フラグを追加する。

```bash
python notify.py --void-setups "理由を1行"
```

- `.secrets/setups.json` に `{"voided_before": <epoch>, "reason": "..."}` を書く
- Bot は scenario ボタン押下時に `issued_epoch <= voided_before` なら拒否
- 併せて Telegram に取消メッセージも送る(現在は手書きで送っている運用を置き換える)

**`.secrets/` はコミット・共有しない。** 既存の規約どおり。

### C. snapshot.json との突き合わせ

Bot から TradingView MCP は使えない(§6 の制約)が、**`.secrets/snapshot.json` の最新価格は読める。**
`/price` が既に読んでいるので配管は存在する。

scenario ボタン押下時に以下をチェックする:

| 条件 | 挙動 |
|---|---|
| snapshot が10分以上古い | **警告を出したうえでドライランは許可**(価格情報がないだけで、セットアップ自体は生きているかもしれない) |
| `side=sell` かつ `snapshot.price <= tp` | **拒否**。利確水準を既に通過している |
| `side=buy` かつ `snapshot.price >= tp` | **拒否** |
| `side=sell` かつ `snapshot.price >= sl` | **拒否**。SL 水準を既に超えている |
| `side=buy` かつ `snapshot.price <= sl` | **拒否** |

拒否メッセージには**必ず現在値とセットアップ価格の両方を出す**。何が起きたか読めないと同じ誤操作を繰り返す。

> 今回のケース: 14:16 時点の snapshot は 29,683.50、TP は 29,684.50。
> `sell` かつ `price <= tp` に該当し、**このチェックだけでも拒否できていた。**

### D. 新しい通知が出たら古いものを失効させる

`notify.py --setup` で新規セットアップを送るとき、`setups.json` の `voided_before` を
**その1秒前**に更新する。**同時に有効なセットアップは常に1つだけ**にする。

今回は SHORT 通知の15分後に LONG 通知を出しており、この規則があれば SHORT は自動失効していた。

---

## 動作確認

**実発注せずに検証できるテストがある。改修後は必ず通すこと。**

```bash
cd tests
python test_buttons.py
```

`★ 全チェック通過` が出ること。既存の7項目(token照合・二重タップ・古いボタン・全決済2タップ・
拒否時のボタン非表示 など)が**1つでも落ちたら、この改修が安全機構を壊している。マージしないこと。**

### 追加するテスト

`tests/test_setup_expiry.py` を新規作成し、最低限これらを検証する:

1. TTL 内の scenario ボタン → ドライランが走る
2. **TTL 超過の scenario ボタン → 発注経路に入らない**(今回の事故そのもの)
3. `--void-setups` 後のボタン → 拒否される
4. 新しい `--setup` 発行後に古いボタンを押す → 拒否される
5. `snapshot.price` が TP を通過している → 拒否される
6. `snapshot.price` が SL を通過している → 拒否される
7. snapshot が存在しない / 壊れている → **落ちずに**ドライランは許可(警告付き)
8. `scenario_callback()` の戻り値が **64バイト以内**
9. 拒否時に送信ボタンが出ない

既存テストと同じくサブプロセスをスタブ化し、**CrossTrade には一切送信しないこと。**

疎通確認:

```bash
python telegram_bot.py --check
```

---

## 環境

- Python 3.12.10 / Windows 11
- **標準ライブラリのみ**(`requests` は入っていない。この方針を維持)
- 設定: `.secrets/telegram.env`
- **`.secrets/` の中身は絶対にコミット・共有しない**

---

## 補足: なぜここまでやるのか

ユーザーは過去に口座を複数回破綻させており、原因を**「取り逃しへの反応」**と自己分析している。
2026-08-07 には2時間で66約定・24枚まで膨らませて失格した。

Bot は「外出先から、チャートを見ずに、通知だけで発注できる」道具なので、
**古い通知が生きていること自体が破綻の入口になる。**

今回の損失は $28 で済んだが、同じ構造で以下が起こりうる:

- 数時間前の通知を遡って押し、まったく違う相場付きで建玉を持つ
- 「取り逃した」と感じた直後に、手近な古い通知を押して入り直す
  ← §5 と §6 原則2 が名指しで禁じている行動を、UI が手助けしてしまう

**タップ数を減らす方向の改善は入れないこと。** 摩擦は意図的に置かれている。

---

## 関連ファイル

| ファイル | 役割 |
|---|---|
| [telegram_bot.py](telegram_bot.py) | `scenario_callback` / `scenario_keyboard` / callback ハンドラ。**改修対象** |
| [notify.py](notify.py) | `--void-setups` 追加。**改修対象** |
| [snapshot.py](snapshot.py) | 読むだけ。**書式を変えないこと**(`/price` が依存) |
| [order.py](order.py) | **触らない**(安全装置の本体) |
| [CODEX_BOT_TASK.md](CODEX_BOT_TASK.md) | Bot 改修時の共通規約。**先に読むこと** |
| [CLAUDE.md](CLAUDE.md) | 運用ルール全体(§5 危険信号 / §6 外出時の運用 / §8 既知の問題) |
