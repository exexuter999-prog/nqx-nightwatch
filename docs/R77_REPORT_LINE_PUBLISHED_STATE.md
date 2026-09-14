# R77 — 報告行は publish 後の正本(ゲート後 state)から作る(2026-09-11)

2026-09-10 23:41 JST、`python nqx_cycle.py` の publish 段は

```text
- vol gate stand-down (noise 42.12pt / ratio 0.84 > 0.60) — scenario demoted to WATCH
```

を出した(`monitor_publish.apply_volatility_grade_gate()`)。載った正本は WATCH。ところが最終行は

```text
[23:41] MNQU6 29,262.75 | primary=VP80_REVERSION SELL A ARMED | E=29,250 SL=29,297 TP=29,173.5/29,042.5 | published
```

だった。`report_line()` が `.secrets/monitor_pipeline_cycle.json` の `decision`(publish **前** の
pipeline 状態)だけを読んでいたためで、この行は Telegram の先頭行であり、エージェントが
`docs/MONITOR_LOOP_PROMPT.md` に従って逐語で返す行でもある。誤報がそのまま人に届いていた。

## 何を直したか(`nqx_cycle.py`)

| 箇所 | 変更 |
| --- | --- |
| `PUBLISHED_STATE` | `.secrets/monitor_last_sent.json`(= `monitor_publish.STATE_FILE`)。publish が Telegram 送信まで終えた bundle で、ゲート後の scenario が `_published_scenario` に入る(None = 武装なし)。乖離はテストが止める。 |
| `read_published_state(sent_at)` | 送った bundle と同じ `at` を持つ正本だけを採り、`{"at","source","scenario"}` を返す。未書き込み・別サイクル・壊れた正本は None。 |
| `stage_publish()` | 戻り値が `(ok, detail, published_state)` の 3 組に。失敗時は `None`。 |
| `report_line(..., published_state=)` | publish 成功時は正本の scenario(model / side / grade / state / E / SL / TP)で書く。等級が欠けていれば `executionContract.effectiveGrade`。publish が走らなかった(BLOCKED / DRY / 失敗)ときだけ decision に戻る。publish は通ったのに正本が読めなければ `post-gate state unread — showing pipeline decision` を末尾に付ける。 |
| `publish_demotion_notes(detail)` | publish 出力の `… — scenario demoted to WATCH` 行から理由だけを抜き、最終行に `demoted: …` として添える。 |

修正後の同じサイクルの行:

```text
[23:41] MNQU6 29,262.75 | primary=VP80_REVERSION SELL A WATCH | E=29,250 SL=29,297 TP=29,173.5/29,042.5 | published | demoted: vol gate stand-down (noise 42.12pt / ratio 0.84 > 0.60)
```

R39 の規律は変えない。エージェントは値を再計算せず、行は成果物(正本 + publish 出力)から
`nqx_cycle.py` が組む。order / engine のロジックには触れていない。

## 触れていないこと(注意)

- 監査コピー `.secrets/monitor_cycle_HHMM.json` は `save_audit()` が pipeline bundle を
  そのまま写したもので、**publish 前**の状態のまま(`scenarios` は `{"primary": …}` の dict で
  ARMED)。ゲート後の状態を遡って読むなら `monitor_last_sent.json` の `_published_scenario`。
- `monitor_publish.py` は Telegram 送信に失敗すると正本を書かずに落ちる。その場合 `stage_publish`
  は失敗を返すので、行は `no-publish | HALT publish failed` になり、decision が出る(従来どおり)。
- `skipped duplicate monitor bundle` のサイクルは前回の正本が同じ `at` を持つので、同じ状態が出る。

## テスト

`python tests/test_r77_report_line_published_state.py`(ネットワーク・本番 .secrets 不使用)。
「pipeline ARMED / publish が WATCH に降格 → 行は WATCH」を `report_line` 単体と `run_cycle` の
最終行の両方で固定した。
