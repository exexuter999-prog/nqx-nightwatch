# R38 アプリ内から Claude と対話する

> **無期限停止 (2026-08-25)** — `/api/chat` は読み書きとも `410 Gone`、
> `chat_inbox.py` は通信せず exit 0。保存済み履歴は削除しない。受信箱の状態は
> 監視・分析・発注の `BLOCKED` / `HALT` 条件に使用しない。以下は履歴資料。

2026-08-24。Mini App に Claude との対話面を作った。Telegram は使わない。

**https://fb8f07c8.nqx-nightwatch.pages.dev/chat.html**

## 経路

```
Mini App (chat.html) --POST /api/chat (role=user)--> Worker --> DO の chat 表
Claude  (chat_inbox.py) --GET  /api/chat----------> Worker --> DO
Claude  (chat_inbox.py) --POST /api/chat (role=claude)--> Worker(署名検証) --> DO
```

## 発注経路とは完全に分離した

- `chat` 表は `state_doc` とも claim とも無関係。シナリオを読むことも書くこともしない
- `chat.js` は `app.js` / `ultra.js` / `scene3d.js` / `sendData` を**一切参照しない**
  （テストで固定）
- `chat_inbox.py` は `order.py` を呼ばない
- **2.08 kB の独立エントリ**。発注画面の起動チャンクに混ざらない

## 認証を role で分けた

| role | 誰が | 認証 |
|---|---|---|
| `user` | Mini App | 読み取り認証（launch token / initData） |
| `claude` | PC の `chat_inbox.py` | **publish と同じ署名** |

アプリ側の資格で `claude` を名乗れると、UI 上は Claude の発言に見える文字列を
誰でも作れてしまう。だから書き込みだけ認証を分ける。`chat.js` は送信 body に
`role` を入れず、Worker 側の既定（`user`）に委ねる。

## 使い方（PC 側）

```bash
python chat_inbox.py --poll                # 未読を表示(既読位置を進める)
python chat_inbox.py --poll --peek         # 未読を表示(既読位置は進めない)
python chat_inbox.py --reply "直しました"    # Claude として返信
python chat_inbox.py --history 20          # 直近のやり取り
```

既読位置は `.secrets/chat_cursor.json`。

Claude Code は常駐していないので、**返信は Claude がこの CLI を叩いた時に届く**。
アプリ側は 5 秒ごとにポーリングしている。

## 実装中に踏んだこと

**`Number(null)` が `0` になる穴を 2 箇所で踏んだ。** `mergeMessages` の seq 判定と
`stamp` の時刻判定で、どちらも `Number.isFinite` だけでは `null` を通してしまい、
`stamp(null)` が **1970-01-01 を「09:00」として描いていた**。seq は正の整数、
時刻は正の epoch ミリ秒と明示して弾く。

**予算チェックが壊れていた。** `assert-build-budget.mjs` は
`Object.keys(manifest).find((k) => manifest[k].isEntry)` で最初のエントリを拾う。
エントリが 3 つ（index / levels / chat）になったので `chat.html` を拾い、
「lazy renderer dynamic import が無い」で落ちた。**測るべきは発注画面の
`index.html`** なので明示する。エントリを増やすたびに壊れる作りだった。

## 保持と上限

DO 側で直近 **300 件**、1 通 **4000 文字**まで。無制限に伸ばさない。

## 検証

Python **43 ファイル** / Worker **117 件** / ミニアプリ **101 件** 通過。
Worker（`3e18a5aa`）と Pages ともデプロイ済み。実際に PC → DO → 読み戻しの
往復を確認した。

## 残っていること

- **Claude 側は手動起動** —— 常駐していないので、未読があっても自動では返信しない。
  監視ループに `--poll` を組み込むか、別途起動する必要がある
- **WebSocket ではなく 5 秒ポーリング** —— 既存の `/api/ws` は state 用で、
  chat は相乗りしていない。会話の頻度なら十分と判断した
