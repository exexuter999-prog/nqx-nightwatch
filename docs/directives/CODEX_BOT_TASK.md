# Codex 向けタスク: Telegram Bot の UI 改善

対象: [telegram_bot.py](telegram_bot.py)(515行・標準ライブラリのみ)

---

## ⚠️ 最優先: 壊してはいけないもの

このBotは**実口座に発注する**。UI を触る際、以下は絶対に変更しないこと。
変更するとユーザーの資金が危険にさらされる。

### 1. チャットID認証(2箇所)

```python
if chat_id != allowed:      # 通常メッセージ
if chat_id != allowed:      # callback_query(ボタン)
```

**完全一致比較を維持すること。** `startswith` や `in` に変えない。
ボタン経由の認証を消さないこと(ここを忘れると穴になる)。

### 2. 二段階確認と token 照合

```python
PENDING = {"argv":…, "desc":…, "at":…, "token":…}
```

- ドライラン(`--confirm` なし)→ 送信(`--confirm` 付き)の2段階
- 送信ボタンの `callback_data` は `go:<token>`
- **`do_confirm()` は run_order の前に `clear_pending()` する**
  → 二重タップで2回発注されないための仕組み
- token 不一致のボタンは無視する
  → 古いメッセージのボタンを押しても発注されない

**この仕組みは実測で検証済み。**(1回目 `--confirm` 1回 / 2回目 0回)

### 3. 発注ロジックを Bot に持たせない

すべて `order.py` にサブプロセスで委譲している。
これにより order.py の安全装置(2枚上限・SL必須・向きチェック・
回数上限)が Bot 経由でも効く。**Bot 側で発注判断をしないこと。**

### 4. 拒否時にボタンを出さない

`order.py` が非ゼロ終了したら `clear_pending()` して送信ボタンを出さない。
誤タップの余地を作らないため。

---

## 改善してほしいこと

### A. 見た目(ユーザーの主訴: 「ダサい」)

現状は絵文字を全廃してプレーンテキスト+`<pre>` ブロック。
素っ気なさすぎるので、**上品に情報を読みやすくしてほしい。**

制約:
- Telegram の `parse_mode="HTML"` で使えるタグのみ
  (`<b> <i> <u> <s> <code> <pre> <a>`)
- **絵文字は使わない**(ユーザーが明示的に嫌っている)
- 発注結果の成否は**一目で区別できること**
  (現在は `[送信完了]` / `[送信失敗]` の角括弧で表現)
- スマホの狭い画面で読む前提。横に長い表は崩れる

改善の方向性(例):
- 価格を見やすく揃える(現在は `<pre>` で等幅にしている)
- ドライランの Entry/SL/TP とリスク額の視認性を上げる
- `order.py` の生出力をそのまま流している箇所を整形する
  → ただし**情報を落とさないこと**。特に価格と拒否理由

### B. 文字化け(要調査)

**Claude Code 側で2箇所対策済み。ユーザーはまだ化けると言っている。**

対策済みの箇所:
```python
# 1. 自身の出力(コンソールログ)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 2. order.py を呼ぶときの子プロセス環境
env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
```

確認してほしいこと:
1. **Telegram に届くメッセージ**が化けるのか、
   **PCのコンソールログ**が化けるのか(切り分けが必要)
2. `notify.py` / `snapshot.py` からの出力経路
   (snapshot.py は対策済み、notify.py は telegram_bot の send を共用)
3. Windows のコードページ(`chcp` が 65001 でない環境)での挙動
4. `urllib.parse.urlencode` に非ASCIIを渡す際のエンコード

再現手順:
```bash
python telegram_bot.py --check     # order.py の出力が日本語で読めるか
python snapshot.py                 # 市況が日本語で読めるか
```

---

## 動作確認の方法

**実発注せずに検証できるテストがある。** 改修後は必ず通すこと。

```
tests/test_buttons.py   ボタン・二重送信・token照合(最重要)
tests/test_bot.py       コマンド解析・order.py 連携
tests/test_auth.py      チャットID認証
```

これらは `--confirm` 付きの呼び出しをスタブ化しているので、
CrossTrade には一切送信されない。検証項目(test_buttons.py):

1. ドライラン → 送信ボタンに token が埋まる
2. **二重タップで2回発注されない**
3. **古いボタンでは発注されない**
4. 全決済ボタンは2タップ
5. 拒否時に送信ボタンが出ない
6. 破棄・メニュー各種
7. 保留がある時の /status に送信ボタンが出る

実行:
```bash
cd tests
python test_buttons.py
```

`★ 全チェック通過` が出れば安全機構は壊れていない。
**失敗したら UI 改修が安全機構を壊している。** マージしないこと。

疎通確認(実際に Telegram へ1通送る):
```bash
python telegram_bot.py --check
```

---

## 環境

- Python 3.12.10 / Windows 11
- **標準ライブラリのみ**(`requests` は入っていない。この方針を維持)
- 設定: `.secrets/telegram.env`(TELEGRAM_TOKEN / TELEGRAM_CHAT_ID)
- **`.secrets/` の中身は絶対にコミット・共有しない**

## 関連ファイル

| ファイル | 役割 |
|---|---|
| [telegram_bot.py](telegram_bot.py) | 常駐Bot。**今回の改修対象** |
| [order.py](order.py) | 発注。**触らない**(安全装置の本体) |
| [notify.py](notify.py) | PC→Telegram の通知送信 |
| [snapshot.py](snapshot.py) | 市況の読み書き(`/price` が使う) |
| [CLAUDE.md](CLAUDE.md) | 運用ルール全体 |
| [TRADING_CONTEXT.md](TRADING_CONTEXT.md) | 口座条件・リスク制限 |

---

## 補足: なぜ安全機構がこの形なのか

ユーザーは過去に口座を複数回破綻させており、原因を
**「取り逃しへの反応」**と自己分析している。
2026-08-07 には2時間で66回約定・サイズ24枚まで膨らませて失格した。

Bot は「外出先から、チャートを見ずに、通知だけで発注できる」道具なので、
**摩擦を減らしすぎると破綻を加速する。**
二段階確認・token照合・全決済の2タップは、この文脈で入れている。

UI を綺麗にするのは歓迎だが、**タップ数を減らす方向の「改善」は
慎重に**。特に `/confirm` を1タップ化する変更は入れないこと。
