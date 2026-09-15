# R105 セッション VWAP の累積を周期をまたいで持ち越す(2026-09-16)

ユーザー質問「VWAP はセッション VWAP か」への回答から見つかった穴の修正。実装は `tv_snapshot.py`
(`session_vwap_accumulate` と状態ファイル)、`msnr_gate._apply_vwap_clearance`(欠損時のガード)、
検証は `tests/test_r105_session_vwap.py`。

## 0. 何が起きていたか

- `bundle.vwap` は **取引日開始(ET 18:00)アンカーのセッション VWAP**(確定 3 分足の hlc3×出来高、
  ±1σ バンド)を `tv_snapshot` が自前で計算している。チャートの VWAP インジケータは使っていない。
  RTH(09:30 ET)アンカーではない。
- **穴**: `tv_fetch` は 3 分足を 240 本(12 時間)しか取らず、`session_vwap` は窓の中のアンカー以降だけを
  積んでいた。窓がアンカーに届いていないことを検出しないので、**06:00 ET(19:00 JST)以降は「直近 12 時間の
  VWAP」に退化**していた。監査コピーの足を全部つないで真のセッション VWAP を再計算した実測:

  | JST | 出ていた値 | 真のセッション VWAP | ずれ |
  |---|---|---|---|
  | 09-15 16:49 | 29,413.03 | 29,413.03 | 0(窓が届いている) |
  | 09-15 21:20 | 29,376.77 | 29,386.26 | 9.5pt |
  | 09-15 22:31 | 29,380.90 | 29,395.06 | 14.2pt |
  | 09-16 05:43 | 29,319.14 | 29,335.38 | 16.2pt |

- 影響: R90 `vwapClearance`(LIVE。VWAP が SL の ±1N 以内なら外側 0.25N へ)の参照値が NY 時間帯で
  ノイズ床の 0.5〜1 倍ずれていた。`vwapPath` 表示と Mini App の VWAP 帯も同じ値。

## 1. 直し方

`session_vwap_accumulate(bars, now_epoch, state_path, symbol)`:

- 取引日ごとに `(Σ v·hlc3, Σ v, オフセット付きの Σ v·(hlc3−c0), Σ v·(hlc3−c0)², 最終足, 本数)` を
  `.secrets/vwap_session_state.json` に保存し、毎周期は**前回の最終足より新しい確定足だけを足す**。
  窓の長さ(240 本)に依存しない。σ は `Σ v·(hlc3−c0)²/Σ v − mean²`(オフセットで桁落ちを避ける。
  直接計算との差 1e-6 未満)。
- **欠損は埋めない**: 前回の最終足と今回の最初の足が連続していなければ `gapBars` に数え、
  `vwapComplete=false`。窓がアンカーに届かない位置から始まった取引日も同様。値は表示用に出す。
- アンカーが変わった(新しい取引日)/ 銘柄が変わった / 窓が状態より過去 → 窓から新規に始める(`reset`)。
- 状態ファイルが読めない・壊れている・書けない → 窓だけの値(従来どおり)で `vwapSource=WINDOW_ONLY`。
  例外で周期を落とさない。
- snapshot に足すキー: `vwapComplete`(bool)/ `vwapSource`(`SESSION_ACCUMULATED` | `WINDOW_ONLY`)/
  `vwapGapBars` / `vwapFirstT`。既存の `vwap`・`vwap_lo`・`vwap_hi`・`vwapAnchorT`・`vwapSessionPv/Vv/
  ThroughT/Bars` はそのまま(値が全セッション分になる)。msnr_gate の系列復元(R45 の seed)も
  そのまま正しい手前ぶんを得る。
- `msnr_gate._apply_vwap_clearance`: `vwapComplete` が False の周期は SL を動かさず、監査に
  `VWAP_PARTIAL`(`gapBars` 付き)を残す。キーが無い旧バンドルは従来どおり(後方互換)。

## 2. 運用

- 状態ファイルは `.secrets/vwap_session_state.json`(git 管理外)。消しても次周期に窓から作り直す
  (その取引日は窓がアンカーに届いていなければ `complete=false`)。
- ループを止めた時間帯の足は誰も足さないので、**再開後のその取引日は `vwapComplete=false`** になり、
  VWAP 逃がしは動かない(表示は出る)。翌 18:00 ET から完全に戻る。取引日は 07:00 JST に切り替わるので、
  監視窓(07:00〜05:45)の中でループを止めなければ毎日 complete になる。
- `tv_fetch` の本数(240)は変えていない。実機で 480 本以上取れるなら、それは別途。

## 3. 戻し方

この PR を revert する。状態ファイルは残っていても無害(読まれない)。

## 4. 検証コマンド

```powershell
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"
python tests/test_r105_session_vwap.py
python tests/test_r28_tv_snapshot.py
```

`tests/test_r105_session_vwap.py` は合成した 1 取引日(400 本)で、15 時間後(窓がアンカーに届かない)に
従来の計算がずれること(穴の再現)と累積が真のセッション VWAP・σ と一致すること、二重加算しないこと、
欠損 40 本で `gapBars=40 / complete=false`、新セッション・壊れた状態・銘柄変更のリセット、
`build_bundle` が snapshot に新キーを載せること、`msnr_gate` が `VWAP_PARTIAL` で SL を動かさないことを
固定する。すべて tempdir、broker_status / nqx_state / autotrade_arm への到達は 0 件。
