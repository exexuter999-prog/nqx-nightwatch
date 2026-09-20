# -*- coding: utf-8 -*-
"""R111: 読み取り専用の照会だけ、一過性の失敗を数回 retry する。

発注先が 7 口座になり 1 サイクルの照会が 14 本を超えたため、CrossTrade の
レート制限(概ね 3 req/s)に触れて **1 本落ちるだけ**で
`broker position is UNVERIFIED for <口座>` になり、そのサイクルの新規・変更が
全部止まっていた(2026-09-18 に何度も実測)。

ここで固定するのは境界:
  * 429 / 5xx と接続不能は retry する(冪等な GET なので安全)
  * **400 は retry しない**(スコープ外の口座・消えた注文 ID が終端であることに
    R53 の `_order_unresolvable` が依存している)
  * 回数は有限。尽きたら例外をそのまま上げ、呼び出し側は従来どおり
    `Unavailable` = 「照会できない」にする。**建玉ゼロとは言わない。**

ネットワークを使わない(urlopen を差し替える)。

    python tests/test_r111_broker_query_retry.py
"""
import io
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in ("stdout", "stderr"):
    _file = getattr(sys, _stream, None)
    if _file is not None and hasattr(_file, "reconfigure"):
        try:
            _file.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import broker_status as bs  # noqa: E402

# 間引き(_pace_request)の待ちは retry のバックオフとは別物。ここでは retry だけを
# 測りたいので切っておく。間引き自体は下の「5. 照会の間引き」で見る。
bs.HTTP_MIN_INTERVAL_SEC = 0

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


def _no_wait(module):
    """間引きが 1 本も待たせないことを確かめる(既定の確認用)。"""
    seen = []
    original = module.time.sleep
    try:
        module.time.sleep = seen.append
        module._LAST_REQUEST_AT[0] = 0.0
        module._pace_request()
        module._pace_request()
    finally:
        module.time.sleep = original
    return seen == []


class FakeResponse:
    def __init__(self, body):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, retry_after=None):
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError(
        "https://example/api", code, "err", headers, io.BytesIO(b""))


class Driver:
    """urlopen を差し替えて、順番どおりに結果を返す。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        item = self.script.pop(0) if self.script else self.script_default()
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)

    def script_default(self):
        return '{"ok": true}'


def run(script):
    # retry 予算はプロセス全体で共有される。ケースごとに戻して、
    # 前のケースの消費が次のケースの結果を変えないようにする。
    bs._RETRY_SPENT[0] = 0.0
    slept = []
    original_urlopen = urllib.request.urlopen
    original_sleep = bs.time.sleep
    driver = Driver(script)
    try:
        urllib.request.urlopen = driver
        bs.time.sleep = slept.append
        try:
            result = bs._get_json("https://example/api")
            error = None
        except Exception as exc:  # noqa: BLE001
            result, error = None, exc
    finally:
        urllib.request.urlopen = original_urlopen
        bs.time.sleep = original_sleep
    return result, error, driver.calls, slept


# ================================================================
print("=" * 68)
print("1. 一過性の失敗は retry して通る")
print("=" * 68)

result, error, calls, slept = run([http_error(429), '{"ok": 1}'])
check("429 の次で成功する", result == {"ok": 1} and error is None, f"{error}")
check("429 は 2 回目で通る(呼び出し 2 回)", calls == 2, f"calls={calls}")
check("待ってから再試行する", len(slept) == 1 and slept[0] > 0, f"slept={slept}")

result, error, calls, _ = run([http_error(503), http_error(502), '{"ok": 2}'])
check("5xx も retry する", result == {"ok": 2} and error is None, f"{error}")
check("3 回目で成功(上限ちょうど)", calls == 3, f"calls={calls}")

result, error, calls, _ = run([urllib.error.URLError("timed out"), '{"ok": 3}'])
check("接続不能も 1 回だけ retry する", result == {"ok": 3} and error is None, f"{error}")
check("接続不能は 2 回まで", calls == 2, f"calls={calls}")

_, _, _, slept = run([http_error(429, retry_after=2), '{"ok": 1}'])
check("Retry-After を尊重する", slept == [2.0], f"slept={slept}")

_, _, _, slept = run([http_error(429, retry_after=9999), '{"ok": 1}'])
check("Retry-After は上限で丸める",
      slept == [bs.HTTP_RETRY_MAX_SLEEP_SEC], f"slept={slept}")

_, _, _, slept = run([http_error(429, retry_after="Wed, 21 Oct 2026 07:28:00 GMT"), '{"ok": 1}'])
check("HTTP-date の Retry-After は既定のバックオフに落とす",
      len(slept) == 1 and slept[0] == bs.HTTP_RETRY_BACKOFF_SEC[0], f"slept={slept}")

# ================================================================
print("=" * 68)
print("2. retry してはいけないものは 1 回で諦める")
print("=" * 68)

result, error, calls, slept = run([http_error(400), '{"ok": 9}'])
check("400 は retry しない", isinstance(error, urllib.error.HTTPError) and error.code == 400,
      f"{error}")
check("400 は 1 回だけ呼ぶ", calls == 1, f"calls={calls}")
check("400 では待たない", slept == [], f"slept={slept}")

for code in (401, 403, 404):
    _, error, calls, _ = run([http_error(code), '{"ok": 9}'])
    check(f"{code} も retry しない", calls == 1 and getattr(error, "code", None) == code,
          f"calls={calls} error={error}")

# ================================================================
print("=" * 68)
print("3. 回数は有限。尽きたら例外をそのまま上げる")
print("=" * 68)

result, error, calls, _ = run([http_error(429)] * 5)
check("429 が続けば諦める", isinstance(error, urllib.error.HTTPError) and error.code == 429,
      f"{error}")
check("試行は HTTP_RETRY_ATTEMPTS 回まで", calls == bs.HTTP_RETRY_ATTEMPTS, f"calls={calls}")

result, error, calls, _ = run([urllib.error.URLError("down")] * 5)
check("接続不能が続けば諦める", isinstance(error, urllib.error.URLError), f"{error}")
check("接続不能は HTTP_RETRY_TRANSPORT_ATTEMPTS 回まで",
      calls == bs.HTTP_RETRY_TRANSPORT_ATTEMPTS, f"calls={calls}")

# ================================================================
print("=" * 68)
print("4. 呼び出し側の性質は変えない(照会できなければ Unavailable)")
print("=" * 68)

adapter = bs.CrossTradeAdapter({
    "CROSSTRADE_KEY": "k", "CROSSTRADE_ACCOUNTS": "ACC-A",
    "CROSSTRADE_API_BASE": "https://example/v1/api/tv",
})
slept = []
original_urlopen = urllib.request.urlopen
original_sleep = bs.time.sleep
try:
    urllib.request.urlopen = Driver([http_error(429)] * 5)
    bs.time.sleep = slept.append
    try:
        adapter.query("MNQZ6", account="ACC-A")
        raised = None
    except bs.Unavailable as exc:
        raised = exc
finally:
    urllib.request.urlopen = original_urlopen
    bs.time.sleep = original_sleep
check("429 が尽きたら Unavailable(建玉ゼロとは言わない)", raised is not None, str(raised))
check("理由に 429 が残る", "429" in str(raised), str(raised))

# 送信経路(_post_json)には retry を入れていない。
import inspect  # noqa: E402
source = inspect.getsource(bs._post_json)
check("_post_json は retry しない(送信の無条件リトライは禁止)",
      "while True" not in source and "HTTP_RETRY" not in source, source[:120])

# ================================================================
print("=" * 68)
print("5. 照会の間引き(そもそも 429 を出さない)")
print("=" * 68)

import importlib  # noqa: E402
paced = importlib.reload(bs)
# 2026-09-19 実測: 照会 1 本の往復が 1.4〜4.2 秒あり、逐次で既に 0.7 req/s しか
# 出ていない。間引きは発動しないのに retry と重なって待ち時間だけ積むので既定は 0。
check("既定は間引きなし(0)", paced.HTTP_MIN_INTERVAL_SEC == 0,
      f"{paced.HTTP_MIN_INTERVAL_SEC}")
check("既定では 1 本も待たせない",
      (lambda: (setattr(paced, "_LAST_REQUEST_AT", [0.0]) or True)
       and _no_wait(paced))(), "既定で待ちが入った")

# 以下は「入れたときに効くこと」を見る。照会本数を減らして 3 req/s へ近づいたら入れる。
paced.HTTP_MIN_INTERVAL_SEC = 0.35
waits = []
original_sleep = paced.time.sleep
original_mono = paced.time.monotonic
clock = [1000.0]
try:
    paced.time.sleep = lambda s: (waits.append(s), clock.__setitem__(0, clock[0] + s))[0]
    paced.time.monotonic = lambda: clock[0]
    paced._LAST_REQUEST_AT[0] = 0.0
    paced._pace_request()                      # 1 本目は待たない
    first = list(waits)
    paced._pace_request()                      # 直後の 2 本目は待つ
finally:
    paced.time.sleep = original_sleep
    paced.time.monotonic = original_mono
check("1 本目は待たない", first == [], f"{first}")
check("直後の 2 本目は最小間隔ぶん待つ",
      len(waits) == 1 and abs(waits[0] - paced.HTTP_MIN_INTERVAL_SEC) < 1e-9, f"{waits}")

waits.clear()
try:
    paced.time.sleep = waits.append
    paced.time.monotonic = lambda: clock[0] + 10.0   # 十分に間隔が空いた
    paced._pace_request()
finally:
    paced.time.sleep = original_sleep
    paced.time.monotonic = original_mono
check("間隔が空いていれば待たない", waits == [], f"{waits}")

paced.HTTP_MIN_INTERVAL_SEC = 0
waits.clear()
try:
    paced.time.sleep = waits.append
    paced._pace_request()
    paced._pace_request()
finally:
    paced.time.sleep = original_sleep
check("0 にすれば間引きを切れる", waits == [], f"{waits}")

print("=" * 68)
if FAIL[0]:
    print(f"FAILED {FAIL[0]} / {PASS[0] + FAIL[0]}")
    sys.exit(1)
print(f"ALL PASS ({PASS[0]})")
