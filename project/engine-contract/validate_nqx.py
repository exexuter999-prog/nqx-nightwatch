#!/usr/bin/env python3
"""NQX/1 validator.

Checks a packet for structural and semantic contradictions before it is handed to the
Nightwatch front end. Reports errors with line numbers and a repair reason.

Usage:
    python3 validate_nqx.py packet.nqx
    cat packet.nqx | python3 validate_nqx.py -

Exit codes:
    0  clean (warnings allowed)
    1  one or more errors
    2  usage / unreadable input
"""

from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime

SUPPORTED_VERSION = "1"
RECORD_TAGS = {"D", "M", "C", "Z", "E", "O", "G", "P", "S1", "S2", "S3"}
SCENARIO_TAGS = ("S1", "S2", "S3")

QUANT_NUMERIC_KEYS = ("pp", "br", "ge", "ne", "ce", "sz", "bp")
NA_TOKENS = {"", "N/A", "NA", "U", "MISSING", "UNVERIFIED", "NOT TESTED", "NONE"}
WIDE_POLICY_VERSION = "WG-H1"
WIDE_REQUIRED_BARS = 15
WIDE_ATR_LENGTH = 14
WIDE_BUFFER_MULT = 0.50
WIDE_MINIMUM_RANGE_MULT = 1.50
WIDE_TARGET_FLOORS = (1.80, 2.80, 4.00)

REQUIRED_SCENARIO_KEYS = {
    "n": "Name",
    "d": "Direction",
    "st": "Scenario State",
    "su": "Setup",
    "ha": "HTF Alignment",
    "lt": "Level Type",
    "zq": "Zone Quality",
    "fr": "Freshness",
    "tr": "Trigger",
    "co": "Confirmation",
    "en": "Entry",
    "sl": "SL",
    "tp": "TP1/TP2/TP3",
    "rr": "R:R",
    "iv": "Invalidation",
    "od": "Outcome Definition",
    "vu": "Valid Until",
    "eh": "Event Handling",
    "fm": "Failure Mode",
    "sd": "Stand Down",
    "ef": "Evidence For",
    "ea": "Evidence Against",
    "cc": "Causal Chain",
}

CODE_SETS = {
    "dq": {"H", "M", "L"},
    "qm": {"H", "E", "C"},
    "ds": {"I", "E", "P", "A"},
    "rg": {"TR", "BA", "TX", "EX", "EC", "ER", "MX"},
    "d": {"L", "S", "N"},
    "st": {"W", "A", "T", "C", "X", "P", "B", "D", "I"},
    "su": {"BC", "PC", "RR", "SR", "FA"},
    "ha": {"A", "N", "M", "C"},
    "zq": {"S", "M", "W", "U"},
    "fr": {"F", "WT", "BT", "M", "C", "B", "FL", "R", "U"},
    "em": {"R", "C", "A", "N"},
}


@dataclass
class Finding:
    line: int
    code: str
    message: str
    fix: str

    def render(self) -> str:
        where = f"line {self.line}" if self.line else "packet"
        return f"  [{self.code}] {where}: {self.message}\n         fix: {self.fix}"


@dataclass
class Report:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def error(self, line, code, message, fix):
        self.errors.append(Finding(line, code, message, fix))

    def warn(self, line, code, message, fix):
        self.warnings.append(Finding(line, code, message, fix))

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------- lexical helpers

def split_escaped(text: str, sep: str = "|"):
    out, cur, esc = [], "", False
    for ch in text:
        if esc:
            cur += "\n" if ch == "n" else ch
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == sep:
            out.append(cur)
            cur = ""
            continue
        cur += ch
    if esc:
        cur += "\\"
    out.append(cur)
    return out


def field_map(parts):
    out = {}
    for part in parts:
        at = part.find("=")
        if at < 1:
            continue
        out[part[:at].strip().lower()] = part[at + 1:].strip()
    return out


def is_na(value: str) -> bool:
    return str(value or "").strip().upper() in NA_TOKENS


def numbers_in(value: str):
    """Numbers in a string. Commas are separators, never thousands marks."""
    out = []
    for token in re.findall(r"-?\d+(?:\.\d+)?", str(value or "")):
        number = float(token)
        if math.isfinite(number):
            out.append(number)
    return out


def canonical_scalar_token(value) -> bool:
    raw = str(value or "").strip()
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
        return False
    return math.isfinite(float(raw))


def canonical_price_token(value, allow_range=True) -> bool:
    raw = str(value or "").strip()
    if canonical_scalar_token(raw):
        return True
    if not allow_range:
        return False
    parts = raw.split("..")
    return (
        len(parts) == 2
        and canonical_scalar_token(parts[0])
        and canonical_scalar_token(parts[1])
    )


def canonical_price_list(value, separator=",", allow_range=True, expected_count=None) -> bool:
    items = [item.strip() for item in split_escaped(str(value or ""), separator)]
    if not items or any(not item for item in items):
        return False
    if expected_count is not None and len(items) != expected_count:
        return False
    return all(canonical_price_token(item, allow_range) for item in items)


def within_wide_scale(value, reference) -> bool:
    return (
        value is not None
        and reference is not None
        and math.isfinite(value)
        and math.isfinite(reference)
        and value > 0
        and reference > 0
        and reference * 0.5 <= value <= reference * 1.5
    )


def canonical_field_prices(value, separator=",", allow_range=True):
    raw = str(value or "").strip()
    if not raw or is_na(raw) or not canonical_price_list(raw, separator, allow_range):
        return []
    out = []
    for item in split_escaped(raw, separator):
        rng = price_range(item)
        if rng:
            out.append(rng[0])
            if abs(rng[1] - rng[0]) > 1e-9:
                out.append(rng[1])
    return out


def is_nq_mnq_symbol(value) -> bool:
    return bool(re.fullmatch(
        r"(?:(?:CME|CME_MINI):)?(?:MNQ|NQ)(?:1!?|[FGHJKMNQUVXZ]\d{1,2})?",
        str(value or "").strip(),
        re.I,
    ))


def kv_map(value):
    out = {}
    for part in str(value or "").split(";"):
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        if key.strip():
            out[key.strip().lower()] = val.strip()
    return out


def regime_evidence_audit(regime, metrics, source_frames=""):
    regime = str(regime or "").strip().upper()
    aliases = {"TREND": "TR", "EXPANSION": "EX"}
    regime = aliases.get(regime, regime)
    values = kv_map(metrics)
    mode = values.get("mode", "").upper()
    policy = values.get("policy", "").upper()

    def number(key):
        raw = values.get(key)
        if raw is None or not re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
            return None
        result = float(raw)
        return result if math.isfinite(result) else None

    comparable = number("cmp")
    stable = number("stable")
    bias = values.get("bias", "").upper()
    structure = values.get("structure", "").upper()
    if policy != "RG-H1":
        return False, "M.rm MUST DECLARE policy=RG-H1"
    if mode == "METRIC":
        required = ("rvr", "rar", "gar", "rvp", "rap", "dep", "ovp")
        if (
            comparable is None
            or comparable < 20
            or any(number(key) is None for key in required)
            or not values.get("session")
            or not values.get("clock")
            or not values.get("prior")
            or bias not in {"UP", "DOWN"}
            or not structure
            or stable is None
            or stable < 2
        ):
            return False, (
                "RG-H1 METRIC MODE REQUIRES cmp>=20, ALL RATIOS/PERCENTILES, "
                "SESSION/CLOCK/PRIOR/BIAS/STRUCTURE, stable>=2"
            )
        if regime == "EX" and not (
            number("rvp") >= 80
            and number("rap") >= 80
            and number("dep") >= 60
            and structure == "BREAK_HOLD"
        ):
            return False, "EX DOES NOT MEET RG-H1 METRIC THRESHOLDS"
        if regime == "TR" and not (
            number("dep") >= 70
            and number("ovp") <= 40
            and structure in {"TREND", "BREAK_HOLD"}
            and values.get("htf", "").upper() == "ALIGNED"
        ):
            return False, "TR DOES NOT MEET RG-H1 METRIC THRESHOLDS"
        return True, "PASS"
    if mode == "SCREENSHOT":
        if regime == "EX":
            return False, "EX CANNOT BE ASSIGNED FROM SCREENSHOT EVIDENCE"
        frames = str(source_frames or "").upper()
        aligned = (values.get("aligned") or values.get("htf") or "").upper() in {
            "YES", "TRUE", "ALIGNED",
        }
        acceptance = values.get("acceptance", "").upper() in {
            "CONFIRMED", "ACCEPTED", "YES", "TRUE",
        }
        if (
            regime == "TR"
            and all(frame in frames for frame in ("3M", "15M", "45M"))
            and aligned
            and acceptance
            and stable is not None
            and stable >= 2
            and bias in {"UP", "DOWN"}
            and structure in {"TREND", "BREAK_HOLD"}
        ):
            return True, "PASS"
        return False, (
            "TR SCREENSHOT FALLBACK REQUIRES 3M/15M/45M, ALIGNED STRUCTURE, "
            "CONFIRMED ACCEPTANCE, BIAS, AND stable>=2"
        )
    return False, "M.rm mode MUST BE METRIC OR SCREENSHOT"


def rr_claims(value: str):
    """R:R is a comma-separated list; parse each element independently."""
    out = []
    for chunk in split_escaped(str(value or ""), ","):
        nums = numbers_in(chunk)
        out.append(nums[0] if nums else None)
    return (out + [None, None, None])[:3]


def price_like(value: str, scale: float):
    """Pick the number in `value` that is plausibly a price at this instrument's scale.

    Narrative invalidation text mixes timeframes with prices ("15M実体が29580を割る"),
    so a naive first-number read would return 15. Keep only candidates within an order
    of magnitude of the scenario's own entry/SL scale.
    """
    lo, hi = scale * 0.5, scale * 1.5
    for n in numbers_in(value):
        if lo <= abs(n) <= hi:
            return n
    return None


def price_range(value: str):
    """Return (low, high) for a scalar or `a..b` range, else None."""
    raw = str(value or "").replace("~", "").strip()
    if not raw or is_na(raw):
        return None
    if ".." in raw:
        nums = numbers_in(raw)
        if len(nums) >= 2:
            return (min(nums[0], nums[1]), max(nums[0], nums[1]))
        return None
    nums = numbers_in(raw)
    if not nums:
        return None
    return (nums[0], nums[0])


def scalar(value: str):
    r = price_range(value)
    return None if r is None else r[0]


def level_ranges(zone_fields, aliases, keys, exact_only=False):
    """Extract point/range prices from selected Z-record fields as a flat list
    of (low, high) tuples. Comma-separated lists inside one field
    are split independently so a list like s=28898,28835.38,28814 becomes three
    separate levels, mirroring the receiver's own nqxPriceList behaviour."""
    out = []
    for key in keys:
        raw = zone_fields.get(key)
        if not raw:
            continue
        for item in split_escaped(expand(raw, aliases), ","):
            item = item.strip()
            if not item:
                continue
            if exact_only and not canonical_price_token(item, allow_range=True):
                continue
            rng = price_range(item)
            if rng:
                out.append(rng)
                continue
            val = scalar(item)
            if val is not None:
                out.append((val, val))
    return out


def structural_levels(zone_fields, aliases, exact_only=False):
    """Protective stop anchors named by the NQX safety invariant."""
    return level_ranges(
        zone_fields, aliases, ("s", "r", "rbs", "sbr", "qml", "ocl"),
        exact_only=exact_only,
    )


def mapped_levels(zone_fields, aliases, exact_only=False):
    """Mapped stop-spacing/target levels represented as explicit Z prices."""
    return level_ranges(
        zone_fields,
        aliases,
        ("poc", "vah", "val", "s", "r", "rbs", "sbr", "qml", "ocl", "a", "v"),
        exact_only=exact_only,
    )


def median(values):
    """Same convention as the receiver's median(): sorted midpoint, average of
    the two central values on an even count. Returns None on an empty list."""
    vals = sorted(v for v in values if v is not None)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2


def level_spacing_unit(levels, pivot):
    """Mirror rangeModel()'s MAPPED LEVEL SPACING fallback.

    Measure the absolute distance from the pivot to every extracted level
    boundary, take the median, scale it by 0.42, and floor the result at 2.
    This needs no OHLC tape and therefore exactly covers O.c=MISSING packets.
    """
    if pivot is None:
        return None
    gaps = []
    for lo, hi in levels:
        d = min(abs(pivot - lo), abs(pivot - hi))
        if d > 0:
            gaps.append(d)
    gap = median(gaps)
    if gap is None:
        return None
    return max(gap * 0.42, 2)


def receiver_number(value):
    """Mirror the receiver's toNum() numeric extraction for OHLC fields."""
    if value is None:
        return None
    text = re.sub(r",(?=\d{3})", "", str(value))
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group(0))
    return number if math.isfinite(number) else None


def parse_observed_candles(raw):
    """Parse O.c exactly as the NQX receiver does end to end.

    Canonical NQX rows are comma-delimited because an unescaped pipe separates
    record fields. parseNQX converts each row to the strict internal
    TIME|OPEN|HIGH|LOW|CLOSE[|VOLUME] grammar consumed by parseCandleRow. An
    escaped-pipe row is also compatible after the lexical layer unescapes it.
    Returns (accepted, rejected); rejected rows never enter range calculations.

    Any OHLC field may carry a leading `~` (axis/grid-line estimate rather than
    an exact reading). A `~`-marked bar is still an OBSERVED bar — it came from
    the chart, not a model — but its precision is lower, so each accepted bar
    carries an `approx` flag (true if ANY of its four prices was `~`-marked).
    Geometry and True Range calculations treat approx and exact bars alike;
    only the flag distinguishes them for reporting.
    """
    accepted, rejected = [], []
    if raw is None:
        return accepted, rejected
    text = str(raw).strip()
    if not text or re.match(r"^(?:missing|unverified|u|n/a)$", text, re.I):
        return accepted, rejected

    for row in re.split(r"\s*;\s*", text):
        row = row.strip()
        if not row:
            continue

        # Mirror parseNQX's comma transport -> pipe-internal adapter, then
        # parseCandleRow's strict pipe split.
        internal_row = "|".join(split_escaped(row, ","))
        parts = [part.strip() for part in internal_row.split("|")]
        if len(parts) < 5:
            rejected.append((row, "EXPECTED TIME|OPEN|HIGH|LOW|CLOSE"))
            continue

        ohlc_fields = parts[1:5]
        o, h, low, c = (receiver_number(v) for v in ohlc_fields)
        if any(value is None or not math.isfinite(value) for value in (o, h, low, c)):
            rejected.append((row, "NON-NUMERIC OHLC"))
            continue

        token_grammar_ok = all(
            re.fullmatch(r"~?-?\d+(?:\.\d+)?", str(value).strip())
            for value in ohlc_fields
        )
        approx = any(str(v).strip().startswith("~") for v in ohlc_fields)
        volume = receiver_number(parts[5]) if len(parts) > 5 else None
        if h < max(o, c) or low > min(o, c) or h < low:
            rejected.append((row, "INVALID OHLC GEOMETRY"))
            continue

        accepted.append({
            "t": parts[0] or "—",
            "o": o,
            "h": h,
            "l": low,
            "c": c,
            "v": volume,
            "approx": approx,
            "token_grammar_ok": token_grammar_ok,
        })

    return accepted, rejected


def supplied_ohlc_unit(bars):
    """Mirror rangeModel()'s SUPPLIED OHLC RANGE branch.

    Use the trailing 12 accepted bars, calculate True Range against the prior
    close within that window, retain finite positive values, and return their
    simple mean only when at least three values remain.
    """
    trs = []
    window = list(bars or [])[-12:]
    for index, bar in enumerate(window):
        prev_close = window[index - 1]["c"] if index > 0 else None
        if prev_close is None:
            tr = bar["h"] - bar["l"]
        else:
            tr = max(
                bar["h"] - bar["l"],
                abs(bar["h"] - prev_close),
                abs(bar["l"] - prev_close),
            )
        if math.isfinite(tr) and tr > 0:
            trs.append(tr)
    if len(trs) < 3:
        return None
    return sum(trs) / len(trs)


def parse_timeframe_minutes_strict(value):
    """Return an exact NQX timeframe in minutes; do not guess ambiguous labels."""
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{1,6})\s*(M|MIN|MINS|MINUTE|MINUTES)", text, re.I)
    if match:
        return int(match.group(1)) if int(match.group(1)) > 0 else None
    match = re.fullmatch(r"(\d{1,6})\s*(H|HR|HRS|HOUR|HOURS)", text, re.I)
    if match:
        return int(match.group(1)) * 60 if int(match.group(1)) > 0 else None
    match = re.fullmatch(r"(\d{1,6})\s*(D|DAY|DAYS)", text, re.I)
    if match:
        return int(match.group(1)) * 1440 if int(match.group(1)) > 0 else None
    return None


def parse_wall_clock_minute_strict(value, base_date_value=""):
    """Parse a JST wall-clock minute without silently normalizing bad dates."""
    text = str(value or "").strip()
    full = re.fullmatch(
        r"(\d{4})-(\d{2})-(\d{2})[ T]([01]\d|2[0-3]):([0-5]\d)\s+JST",
        text,
        re.I,
    )
    if full:
        year, month, day, hour, minute = map(int, full.groups())
    else:
        time_only = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", text)
        base = re.fullmatch(
            r"(\d{4})-(\d{2})-(\d{2})[ T]([01]\d|2[0-3]):([0-5]\d)\s+JST",
            str(base_date_value or "").strip(),
            re.I,
        )
        if not time_only or not base:
            return None
        year, month, day = map(int, base.groups()[:3])
        hour, minute = map(int, time_only.groups())
    try:
        stamp = datetime(year, month, day, hour, minute)
    except ValueError:
        return None
    return stamp.toordinal() * 1440 + stamp.hour * 60 + stamp.minute


def wide_candle_time_audit(bars, timeframe, as_of):
    """Block WG-H1 unless its 15 bars form one fresh, closed, exact-time window."""
    window = list(bars or [])[-WIDE_REQUIRED_BARS:]
    tf_minutes = parse_timeframe_minutes_strict(timeframe)
    as_of_minute = parse_wall_clock_minute_strict(as_of)
    if tf_minutes is None:
        return False, "M.tf MUST BE AN EXACT MINUTE/HOUR/DAY TIMEFRAME (E.G. 15M)"
    if as_of_minute is None:
        return False, "M.at MUST BE YYYY-MM-DD HH:MM JST"
    if len(window) < WIDE_REQUIRED_BARS:
        return False, f"FEWER THAN {WIDE_REQUIRED_BARS} ACCEPTED BARS"
    if any(not bar.get("token_grammar_ok") for bar in window):
        return False, "O.c OHLC TOKENS MUST BE BARE DECIMALS OR A LEADING ASCII ~ APPROXIMATION"
    if any(bar.get("approx") for bar in window):
        return False, "WG-H1 REQUIRES EXACT O.c OHLC TOKENS WITHOUT ~"

    stamps = []
    for bar in window:
        stamp = parse_wall_clock_minute_strict(bar.get("t"), as_of)
        if stamp is None:
            return False, "O.c TIME MUST BE HH:MM OR YYYY-MM-DD HH:MM JST"
        if stamps and stamp <= stamps[-1]:
            return False, "O.c TIMES MUST BE UNIQUE AND STRICTLY INCREASING"
        if stamps and stamp - stamps[-1] != tf_minutes:
            return False, f"O.c CADENCE MUST EXACTLY MATCH M.tf={str(timeframe).strip()}"
        stamps.append(stamp)

    last_close = stamps[-1] + tf_minutes
    age = as_of_minute - last_close
    if age < 0:
        return False, "LATEST O.c BAR IS NOT CLOSED AT M.at"
    if age >= tf_minutes:
        return False, "LATEST O.c BAR IS STALE BY ONE OR MORE M.tf INTERVALS"
    return True, "PASS"


def wide_ohlc_unit(bars, timeframe=None, as_of=None):
    """WG-H1 volatility unit.

    Require 15 chronological accepted bars so the first supplies the prior
    close and the remaining 14 produce a complete True Range sample. The
    simple mean is an explicitly frozen heuristic, not Wilder ATR.
    """
    window = list(bars or [])[-WIDE_REQUIRED_BARS:]
    if len(window) < WIDE_REQUIRED_BARS:
        return None
    if not wide_candle_time_audit(window, timeframe, as_of)[0]:
        return None
    trs = []
    for index in range(1, len(window)):
        bar = window[index]
        prev_close = window[index - 1]["c"]
        tr = max(
            bar["h"] - bar["l"],
            abs(bar["h"] - prev_close),
            abs(bar["l"] - prev_close),
        )
        if math.isfinite(tr) and tr > 0:
            trs.append(tr)
    if len(trs) < WIDE_ATR_LENGTH:
        return None
    return sum(trs) / len(trs)


# ---------------------------------------------------------------- alias resolution

ALIAS_REF = re.compile(r"\$([A-Za-z][A-Za-z0-9_]*)")


def check_aliases(aliases, records, report):
    """Validate aliases as a DAG, allowing shared dependencies but no cycles."""
    memo = {}
    visiting = set()
    reported = set()

    def problem(name, code, message, fix):
        key = (name, code)
        if key in reported:
            return
        reported.add(key)
        report.error(aliases_line.get(name, 0), code, message, fix)

    def resolve(name, depth=0):
        name = str(name or "").upper()
        if name in memo:
            return memo[name]
        if depth > 64:
            problem(
                name,
                "ALIAS_DEPTH",
                f"alias ${name} exceeds the 64-link expansion limit",
                "flatten the alias graph; unresolved deep aliases are rejected",
            )
            return f"${name}"
        if name in visiting:
            problem(
                name,
                "ALIAS_CYCLE",
                f"alias ${name} participates in a reference cycle",
                "break the cycle; shared acyclic dependencies are allowed",
            )
            return f"${name}"
        if name not in aliases:
            problem(
                name,
                "ALIAS_UNDEFINED",
                f"alias ${name} is not defined",
                f"define ${name} in a D record, or write the literal value",
            )
            return f"${name}"
        visiting.add(name)

        def replace(match):
            ref = match.group(1).upper()
            if ref not in aliases:
                problem(
                    name,
                    "ALIAS_UNDEFINED",
                    f"alias ${name} references undefined alias ${match.group(1)}",
                    f"define ${match.group(1)} in a D record, or write the literal value",
                )
                return match.group(0)
            return resolve(ref, depth + 1)

        value = ALIAS_REF.sub(replace, str(aliases[name]))
        visiting.discard(name)
        memo[name] = value
        return value

    for name in aliases:
        resolve(name)

    for lineno, tag, fields in records:
        for key, value in fields.items():
            for ref in ALIAS_REF.findall(value):
                if ref.upper() not in aliases:
                    report.error(
                        lineno, "ALIAS_UNDEFINED",
                        f"{tag}.{key} references undefined alias ${ref}",
                        f"define ${ref} in a D record, or write the literal value",
                    )


aliases_line = {}


def expand(value, aliases, depth=64):
    s = str(value or "")
    for _ in range(depth):
        nxt = ALIAS_REF.sub(lambda m: aliases.get(m.group(1).upper(), m.group(0)), s)
        if nxt == s:
            break
        s = nxt
    return s


def is_no_event_window(value) -> bool:
    """Canonical sentinel for a freshly verified empty ±30-minute event window."""
    return bool(re.fullmatch(r"NONE_WITHIN_30M", str(value or "").strip(), re.I))


def parse_cone_band(value):
    """Parse a `low..high` cone band into a (low, high) tuple.

    Cone prices are simulated, so every bound must carry `~` — the same
    marker `S.pc` uses. A bare number here would read as an observed or
    executable price, which a distribution bound never is.
    """
    raw = str(value or "").strip()
    parts = raw.split("..")
    if len(parts) != 2:
        return None
    out = []
    for part in parts:
        part = part.strip()
        if not part.startswith("~"):
            return None
        body = part[1:].strip()
        if not re.fullmatch(r"-?\d+(?:\.\d+)?", body):
            return None
        num = float(body)
        if not math.isfinite(num):
            return None
        out.append(num)
    if out[1] < out[0]:
        return None
    return (out[0], out[1])


def validate_forecast_cone(fields, aliases, lineno, report, observed_bars, market):
    """Validate the P record: a display-only market-dynamics distribution.

    The cone is not a scenario and not a forecast of price. It states the band
    the path would occupy given the volatility already printed. Because it is
    easy to dress a directional bet up as a distribution, this check is strict:
    quantile nesting, monotonic widening, agreement with the O.c tape's own
    volatility unit, and a drift ceiling on the median are all enforced.
    """
    def val(key):
        return expand(fields.get(key, ""), aliases).strip()

    horizon_raw = val("h")
    if not re.fullmatch(r"\d{1,2}", horizon_raw or ""):
        report.error(
            lineno, "CONE_HORIZON",
            f"P.h={horizon_raw!r} is not an integer bar horizon",
            "set P.h to the number of projected bars, 1..24",
        )
        return
    horizon = int(horizon_raw)
    if horizon < 1 or horizon > 24:
        report.error(
            lineno, "CONE_HORIZON",
            f"P.h={horizon} is outside the supported 1..24 bar horizon",
            "a cone beyond 24 bars is not supported; shorten the horizon",
        )
        return

    bands = {}
    for key, label in (("q50", "50%"), ("q80", "80%"), ("q95", "95%")):
        raw = val(key)
        if not raw:
            report.error(
                lineno, "CONE_BAND_MISSING",
                f"P.{key} ({label} band) is absent",
                "emit q50, q80 and q95 as ~low..~high; a cone without its "
                "bands cannot be audited",
            )
            return
        parsed = parse_cone_band(raw)
        if parsed is None:
            report.error(
                lineno, "CONE_BAND_INVALID",
                f"P.{key}={raw!r} is not a ~low..~high simulated range",
                "write both bounds with a leading ~, low first, e.g. "
                "q50=~28622..~28720",
            )
            return
        bands[key] = parsed

    med_raw = val("med")
    if not med_raw.startswith("~") or not re.fullmatch(
        r"-?\d+(?:\.\d+)?", med_raw[1:].strip() or ""
    ):
        report.error(
            lineno, "CONE_MEDIAN_INVALID",
            f"P.med={med_raw!r} is not a ~-marked simulated median",
            "write the distribution median as ~PRICE, e.g. med=~28671.7",
        )
        return
    med = float(med_raw[1:].strip())

    # Quantile nesting: each wider band must contain the narrower one.
    lo50, hi50 = bands["q50"]
    lo80, hi80 = bands["q80"]
    lo95, hi95 = bands["q95"]
    if not (lo95 <= lo80 <= lo50 <= med <= hi50 <= hi80 <= hi95):
        report.error(
            lineno, "CONE_QUANTILE_ORDER",
            "P bands are not nested: expected "
            f"q95lo<=q80lo<=q50lo<=med<=q50hi<=q80hi<=q95hi, got "
            f"{lo95}<={lo80}<={lo50}<={med}<={hi50}<={hi80}<={hi95}",
            "a wider confidence band must fully contain every narrower band",
        )

    # Volatility unit must agree with the tape the packet itself carries.
    unit_raw = val("u")
    if not unit_raw:
        report.error(
            lineno, "CONE_UNIT_MISSING",
            "P.u (volatility unit) is absent",
            "state the True Range unit the cone was generated from",
        )
    elif not re.fullmatch(r"-?\d+(?:\.\d+)?", unit_raw):
        report.error(
            lineno, "CONE_UNIT_INVALID",
            f"P.u={unit_raw!r} is not a bare decimal",
            "P.u is a measured quantity, not an estimate; write it unprefixed",
        )
    else:
        declared_unit = float(unit_raw)
        if len(observed_bars) >= 4:
            window = observed_bars[-15:]
            trs = []
            for i in range(1, len(window)):
                prev_close = window[i - 1]["c"]
                tr = max(
                    window[i]["h"] - window[i]["l"],
                    abs(window[i]["h"] - prev_close),
                    abs(window[i]["l"] - prev_close),
                )
                if math.isfinite(tr) and tr > 0:
                    trs.append(tr)
            if trs:
                actual = sum(trs) / len(trs)
                if abs(actual - declared_unit) > max(actual * 0.02, 0.25):
                    report.error(
                        lineno, "CONE_UNIT_MISMATCH",
                        f"P.u={declared_unit} disagrees with the O.c tape's own "
                        f"trailing True Range mean {actual:.2f}",
                        "regenerate the cone from this packet's observed bars, "
                        "or correct P.u; a cone scaled to a different tape is "
                        "not auditable",
                    )

    # Drift ceiling: a distribution centred far from the last observed close is
    # a directional call wearing a distribution's clothes.
    now = None
    if observed_bars:
        now = observed_bars[-1]["c"]
    elif market.get("px"):
        now = scalar(expand(market.get("px", ""), aliases))
    if now is not None and math.isfinite(now):
        span = max(hi95 - lo95, 1e-9)
        drift = abs(med - now)
        if drift > span * 0.25:
            report.error(
                lineno, "CONE_DRIFT",
                f"P.med={med} sits {drift:.1f}pt from the latest close {now}, "
                f"more than 25% of the 95% band width {span:.1f}pt",
                "centre the step distribution before simulating; an uncentred "
                "sample turns a spread into a direction forecast",
            )

    basis = val("b")
    if not basis or is_na(basis):
        report.error(
            lineno, "CONE_BASIS_MISSING",
            "P.b (generation basis) is absent",
            "state what the cone was generated from, e.g. "
            "b=observed TR bootstrap;level friction;no drift",
        )
    if re.search(r"\bDRIFT\b", basis, re.I) and not re.search(
        r"NO[-_ ]?DRIFT|DRIFT[-_ ]?FREE|ドリフトなし", basis, re.I
    ):
        report.error(
            lineno, "CONE_DRIFT_DECLARED",
            "P.b declares a drift term",
            "the cone must express spread only; remove the drift term",
        )

    limits = val("un")
    if not limits or is_na(limits):
        report.error(
            lineno, "CONE_LIMITS_MISSING",
            "P.un (limitations) is absent",
            "state explicitly that the cone is display-only and is not a "
            "prediction of price",
        )


def validate_projection(fields, aliases, lineno, report):
    """Validate optional display-only scenario candles.

    The relative +N clock and mandatory ~ on every price are an intentional
    anti-confusion boundary: this tape is never observed market data.
    """
    has_meta = any(str(fields.get(k, "")).strip() for k in ("pk", "pb", "pu"))
    raw = expand(fields.get("pc", ""), aliases).strip()
    if not has_meta and (not raw or is_na(raw)):
        return 0
    if not raw or is_na(raw):
        report.error(lineno, "PROJECTION_TAPE_MISSING",
                     "projection metadata is present without S.pc",
                     "add pc=+1,~O,~H,~L,~C or remove pk/pb/pu")
        return 0

    kind = str(fields.get("pk", "")).strip().upper()
    if kind not in ("SCENARIO", "MODEL"):
        report.error(lineno, "PROJECTION_KIND",
                     f"S.pk must be SCENARIO or MODEL, not {fields.get('pk', '')!r}",
                     "use pk=SCENARIO or pk=MODEL; OBSERVED/LIVE/ACTUAL are forbidden")
    if not str(fields.get("pb", "")).strip():
        report.error(lineno, "PROJECTION_BASIS", "S.pb is missing",
                     "state the model or scenario basis; do not imply observed data")
    if not str(fields.get("pu", "")).strip():
        report.error(lineno, "PROJECTION_UNCERTAINTY", "S.pu is missing",
                     "state limitations and that the tape is display-only")

    rows = [row.strip() for row in raw.split(";") if row.strip()]
    if len(rows) > 12:
        report.error(lineno, "PROJECTION_LENGTH",
                     f"S.pc contains {len(rows)} bars; maximum is 12",
                     "shorten the display-only scenario tape to 1..12 relative bars")
    price_token = re.compile(r"^~-?(?:\d+(?:\.\d+)?|\.\d+)$")
    for i, row in enumerate(rows, start=1):
        parts = [p.strip() for p in split_escaped(row, ",")]
        if len(parts) != 5:
            report.error(lineno, "PROJECTION_ROW",
                         f"projected row {row!r} does not have +N,~O,~H,~L,~C",
                         "emit exactly five comma-separated fields")
            continue
        if parts[0] != f"+{i}":
            report.error(lineno, "PROJECTION_STEP",
                         f"projected steps must be sequential +1..+N; got {parts[0]!r} at row {i}",
                         "remove timestamps and use relative steps beginning at +1")
        if any(not price_token.match(v) for v in parts[1:]):
            report.error(lineno, "PROJECTION_APPROX",
                         f"every projected OHLC price must begin with ~ in row {row!r}",
                         "prefix all four scenario prices with ~")
            continue
        o, h, low, c = (float(v[1:]) for v in parts[1:])
        if h < max(o, c) or low > min(o, c) or h < low:
            report.error(lineno, "PROJECTION_GEOMETRY",
                         f"invalid projected OHLC geometry in row {row!r}",
                         "require H >= max(O,C), L <= min(O,C), and H >= L")
    return len(rows)


# ---------------------------------------------------------------- main validation

def validate(text: str) -> Report:
    report = Report()
    aliases_line.clear()
    raw_lines = text.replace("\ufeff", "").split("\n")

    # --- header / version gate
    header_idx = None
    for i, line in enumerate(raw_lines):
        if line.strip():
            header_idx = i
            break
    if header_idx is None:
        report.error(0, "EMPTY", "packet is empty", "emit a full NQX/1 packet")
        return report

    header = raw_lines[header_idx].strip()
    m = re.match(r"^!NQX/(\d+)$", header)
    if not m:
        report.error(
            header_idx + 1, "HEADER",
            f"first non-empty line must be !NQX/1, found {header!r}",
            "prepend the !NQX/1 header line",
        )
        return report
    if m.group(1) != SUPPORTED_VERSION:
        report.error(
            header_idx + 1, "VERSION",
            f"unsupported packet version NQX/{m.group(1)}",
            "this validator implements NQX/1 only; reject rather than guess",
        )
        return report

    # --- record scan
    aliases = {}
    records = []
    for i, raw in enumerate(raw_lines):
        lineno = i + 1
        line = raw.strip()
        if not line or line == header or line.startswith("```"):
            continue
        parts = split_escaped(line, "|")
        tag = (parts[0] or "").strip().upper()
        fields = field_map(parts[1:])
        if tag not in RECORD_TAGS:
            report.error(
                lineno, "UNKNOWN_RECORD",
                f"unknown record tag {tag!r}",
                "use one of D M C Z E O G S1 S2 S3",
            )
            continue
        if tag == "D":
            for part in parts[1:]:
                at = part.find("=")
                if at > 0:
                    key = part[:at].strip().upper()
                    aliases[key] = part[at + 1:].strip()
                    aliases_line[key] = lineno
            continue
        records.append((lineno, tag, fields))

    tags = [t for _, t, _ in records]
    for required in ("M", "G"):
        if required not in tags:
            report.error(
                0, "MISSING_RECORD",
                f"required record {required} is absent",
                f"emit the {required} record",
            )

    check_aliases(aliases, records, report)

    market = {}
    market_line = 0
    gate = {}
    gate_line = 0
    zone = {}
    zone_line = 0
    event = {}
    event_line = 0
    observed = {}
    observed_line = 0
    cone = {}
    cone_line = 0
    scenarios = []
    seen_scenario_tags = {}

    for lineno, tag, fields in records:
        if tag == "M":
            if market_line:
                report.error(lineno, "DUP_RECORD", "duplicate M record",
                             "emit exactly one M record")
            market, market_line = fields, lineno
        elif tag == "G":
            if gate_line:
                report.error(lineno, "DUP_RECORD", "duplicate G record",
                             "emit exactly one G record")
            gate, gate_line = fields, lineno
        elif tag == "Z":
            if zone_line:
                report.error(lineno, "DUP_RECORD", "duplicate Z record",
                             "emit exactly one Z record")
            zone, zone_line = fields, lineno
        elif tag == "E":
            if event_line:
                report.error(
                    lineno,
                    "DUP_RECORD",
                    "duplicate E record",
                    "emit exactly one E record containing the nearest material "
                    "event or NONE_WITHIN_30M",
                )
            else:
                event, event_line = fields, lineno
        elif tag == "O":
            if observed_line:
                report.error(lineno, "DUP_RECORD", "duplicate O record",
                             "emit exactly one O record")
            observed, observed_line = fields, lineno
        elif tag == "P":
            if cone_line:
                report.error(lineno, "DUP_RECORD", "duplicate P record",
                             "emit exactly one P record")
            cone, cone_line = fields, lineno
        elif tag in SCENARIO_TAGS:
            if tag in seen_scenario_tags:
                report.error(
                    lineno, "SCENARIO_DUP_ID",
                    f"scenario id {tag} appears twice (first at line {seen_scenario_tags[tag]})",
                    "give each scenario a unique id S1 / S2 / S3",
                )
            else:
                seen_scenario_tags[tag] = lineno
            scenarios.append((lineno, tag, fields))

    observed_bars, observed_rejected = parse_observed_candles(
        expand(observed.get("c", ""), aliases)
    )
    for row, error in observed_rejected:
        report.warn(
            observed_line,
            "OBSERVED_CANDLE_REJECTED",
            f"O.c row {row!r} rejected: {error}",
            "fix the bar or omit it; rejected bars are not used in range calculations",
        )
    approx_count = sum(1 for bar in observed_bars if bar.get("approx"))
    if approx_count:
        report.warn(
            observed_line,
            "OBSERVED_CANDLE_APPROX",
            f"{approx_count} of {len(observed_bars)} O.c bar(s) carry a ~-marked "
            "(axis/grid-line estimate) price rather than an exact reading",
            "confirm exact price labels are truly unavailable; approx bars may enter "
            "the legacy range calculation at reduced precision but cannot authorize WG-H1",
        )
    if cone_line:
        validate_forecast_cone(
            cone, aliases, cone_line, report, observed_bars, market
        )

    observed_unit = supplied_ohlc_unit(observed_bars)
    wide_time_ok, wide_time_reason = wide_candle_time_audit(
        observed_bars,
        expand(market.get("tf", ""), aliases),
        expand(market.get("at", ""), aliases),
    )
    wide_scale_reference = observed_bars[-1]["c"] if wide_time_ok and observed_bars else None
    wide_unit = wide_ohlc_unit(
        observed_bars,
        expand(market.get("tf", ""), aliases),
        expand(market.get("at", ""), aliases),
    )
    risk_note = expand(gate.get("rn", ""), aliases)
    wide_policy = bool(re.search(
        r"(?:^|[;\s])(?:POLICY|RISK(?:_POLICY)?)\s*[:=]\s*WG-H1(?:$|[;\s])",
        risk_note,
        re.I,
    ))
    if wide_policy and scenarios:
        if not is_nq_mnq_symbol(expand(market.get("sy", ""), aliases)):
            report.error(
                market_line,
                "WG_SYMBOL_UNSUPPORTED",
                "WG-H1 MNQ economics cannot be applied to "
                f"M.sy={expand(market.get('sy', ''), aliases)!r}",
                "use an NQ/MNQ futures symbol, or a separately versioned "
                "contract policy",
            )
        regime_ok, regime_reason = regime_evidence_audit(
            expand(market.get("rg", ""), aliases),
            expand(market.get("rm", ""), aliases),
            expand(market.get("sf", ""), aliases),
        )
        if not regime_ok:
            report.error(
                market_line,
                "WG_REGIME_EVIDENCE_MISSING",
                f"WG-H1 regime evidence failed: {regime_reason}",
                "encode a complete RG-H1 METRIC record, or the strict TR "
                "SCREENSHOT fallback; otherwise emit G.n=0",
            )
        bad_level_tokens = []
        for key in ("poc", "vah", "val", "s", "r", "rbs", "sbr", "qml", "ocl", "a", "v"):
            level_value = expand(zone.get(key, ""), aliases)
            if zone.get(key) and not is_na(level_value) and not canonical_price_list(
                level_value, ",", allow_range=True
            ):
                bad_level_tokens.append(f"Z.{key}")
        dz_value = expand(zone.get("dz", ""), aliases)
        if zone.get("dz") and not is_na(dz_value) and not canonical_price_token(
            dz_value, allow_range=True
        ):
            bad_level_tokens.append("Z.dz")
        tm_value = expand(zone.get("tm", ""), aliases)
        if zone.get("tm") and not is_na(tm_value) and not canonical_price_list(
            tm_value, ";", allow_range=True
        ):
            bad_level_tokens.append("Z.tm")
        if bad_level_tokens:
            report.error(
                zone_line,
                "WG_LEVEL_TOKEN_INVALID",
                "WG-H1 requires canonical exact numeric/range tokens in mapped "
                f"Z/TM fields; invalid: {', '.join(bad_level_tokens)}",
                "use bare decimal prices or low..high ranges only; remove ~, ≈, "
                "labels, prefixes, and suffixes",
            )
        if wide_scale_reference is not None:
            scale_bad = []
            market_price_raw = expand(market.get("px", ""), aliases)
            market_price = scalar(market_price_raw)
            if (
                not canonical_price_token(market_price_raw, allow_range=False)
                or not within_wide_scale(market_price, wide_scale_reference)
            ):
                scale_bad.append("M.px")
            mapped_scale_prices = []
            for key in ("poc", "vah", "val", "s", "r", "rbs", "sbr", "qml", "ocl", "a", "v"):
                mapped_scale_prices.extend(
                    canonical_field_prices(expand(zone.get(key, ""), aliases), ",", True)
                )
            dz_raw = expand(zone.get("dz", ""), aliases)
            if zone.get("dz") and canonical_price_token(dz_raw, allow_range=True):
                dz_rng = price_range(dz_raw)
                if dz_rng:
                    mapped_scale_prices.append(dz_rng[0])
                    if abs(dz_rng[1] - dz_rng[0]) > 1e-9:
                        mapped_scale_prices.append(dz_rng[1])
            mapped_scale_prices.extend(
                canonical_field_prices(expand(zone.get("tm", ""), aliases), ";", True)
            )
            if any(
                not within_wide_scale(price, wide_scale_reference)
                for price in mapped_scale_prices
            ):
                scale_bad.append("Z/TM")
            if scale_bad:
                report.error(
                    market_line,
                    "WG_PRICE_SCALE_MISMATCH",
                    "WG-H1 exact prices must share the latest O.c close scale "
                    f"({wide_scale_reference}); mismatch: {', '.join(scale_bad)}",
                    "use finite exact MNQ prices within 0.5x..1.5x of the latest "
                    "O.c close, or emit NO TRADE",
                )
    if wide_policy and not re.search(r"SIZE[-_ ]?DOWN|MNQ|REDUCE_OR_FLAT", risk_note, re.I):
        (report.error if scenarios else report.warn)(
            gate_line, "WG_SIZE_DISCIPLINE",
            "WG-H1 is declared without an explicit MNQ/size-down rule",
            "reduce quantity to the fixed dollar-risk budget; 1 MNQ over budget means NO TRADE",
        )
    if wide_policy and not re.search(
        r"NO[-_ ]?WIDEN|NEVER.{0,12}WIDEN|SL.{0,12}(?:遠ざけない|拡大しない)",
        risk_note,
        re.I,
    ):
        (report.error if scenarios else report.warn)(
            gate_line, "WG_NO_WIDEN_RULE",
            "WG-H1 is declared without a no-stop-widening rule",
            "state NO-WIDEN after fill; a protective stop may only stay or reduce risk",
        )
    if wide_policy and not re.search(
        r"BE\s*(?:>=|AFTER)\s*1\.25R|NO[-_ ]?AUTO[-_ ]?BE|1\.25R.{0,18}(?:BE|建値)",
        risk_note,
        re.I,
    ):
        (report.error if scenarios else report.warn)(
            gate_line, "WG_BE_RULE",
            "WG-H1 is declared without the +1.25R breakeven guard",
            "forbid mechanical BE before +1.25R and trail only after confirmed structure",
        )

    # --- controlled codes
    for lineno, tag, fields in records:
        for key, allowed in CODE_SETS.items():
            if key not in fields:
                continue
            if tag == "M" and key not in ("dq", "qm", "ds", "rg"):
                continue
            if tag in SCENARIO_TAGS and key in ("dq", "qm", "ds", "rg"):
                continue
            val = fields[key].strip().upper()
            if val and val not in allowed and not is_na(val):
                report.error(
                    lineno, "BAD_CODE",
                    f"{tag}.{key}={fields[key]!r} is not a valid code",
                    f"use one of {sorted(allowed)}",
                )

    # --- quant gate
    qm = (market.get("qm") or "").strip().upper()
    if not qm:
        report.error(market_line, "QUANT_MODE_MISSING", "M.qm (Quant Mode) is absent",
                     "screenshot-only analysis is qm=H (HEURISTIC)")
    if qm == "H":
        for key in QUANT_NUMERIC_KEYS:
            val = market.get(key)
            if val and not is_na(val) and numbers_in(val):
                report.error(
                    market_line, "HEURISTIC_NUMERIC",
                    f"qm=HEURISTIC but M.{key}={val!r} carries a number",
                    f"set M.{key}=N/A or declare it in na=; a screenshot cannot calibrate this",
                )

    # --- volatility-context provenance (VIX is context, never gamma)
    has_vix = any(str(market.get(k, "")).strip() for k in ("vx", "vxc", "vxa", "vxs", "vxf", "vxr"))
    if has_vix:
        vx = scalar(market.get("vx", ""))
        if vx is None or not 0 <= vx <= 200:
            report.error(market_line, "VIX_VALUE",
                         "M.vx must be a numeric VIX value in the defensible 0..200 range",
                         "supply the sourced value or omit every vx* field")
        for key in ("vxa", "vxs", "vxf"):
            if not str(market.get(key, "")).strip():
                report.error(market_line, "VIX_PROVENANCE", f"M.{key} is required whenever M.vx is present",
                             "include source, as-of time, and freshness")
        if market.get("vxf") and market["vxf"].strip().upper() not in ("LIVE", "DELAYED", "STALE", "UNVERIFIED"):
            report.error(market_line, "VIX_FRESHNESS", f"M.vxf={market['vxf']!r} is invalid",
                         "use LIVE, DELAYED, STALE, or UNVERIFIED")

    # --- scenario count
    declared = gate.get("n")
    if declared is None:
        report.error(gate_line, "COUNT_MISSING", "G.n (scenario count) is absent",
                     "set G.n to the number of S records")
    else:
        declared_text = str(declared).strip()
        if not re.fullmatch(r"[0-3]", declared_text):
            report.error(
                gate_line,
                "COUNT_BAD",
                f"G.n={declared!r} is not one integer in 0..3",
                "set G.n to one digit: 0, 1, 2, or 3",
            )
            n = None
        else:
            n = int(declared_text)
        if n is not None:
            if n != len(scenarios):
                report.error(
                    gate_line, "COUNT_MISMATCH",
                    f"G.n declares {n} scenario(s) but {len(scenarios)} S record(s) are present",
                    "make G.n equal the number of S records",
                )
            if n == 0 and not gate.get("nt"):
                report.error(gate_line, "NO_TRADE_REASON",
                             "G.n=0 without G.nt (NO TRADE reason)",
                             "state the missing condition or conflict in G.nt")
            if n > 0 and not gate.get("ip"):
                report.error(gate_line, "POSTURE_MISSING", "G.ip (Immediate Posture) is absent",
                             "state the current action and the next-30-minute watch")
            if n > 0 and gate.get("ip") and wide_policy:
                posture = expand(gate.get("ip", ""), aliases)
                posture_ok = all((
                    bool(re.search(r"(?:next|次|今後).{0,12}30\s*(?:m|min|minute|分)", posture, re.I)),
                    bool(re.search(
                        r"(?:action\s*[:=]?\s*)?(?:flat|wait|monitor|watch|hold|"
                        r"no trade|stand down|execute|armed|observe|close|reduce|"
                        r"待機|監視|様子見|見送り|実行|観察|全決済|縮小|何もしない)",
                        posture, re.I,
                    )),
                    bool(re.search(
                        r"\b(?:at|now|zone|current)\b.{0,24}-?\d+(?:\.\d+)?|"
                        r"(?:現在|現値|ゾーン|位置).{0,16}\d+(?:\.\d+)?",
                        posture, re.I,
                    )),
                    bool(re.search(
                        r"trigger|retest|accept|reject|break|hold|close|sweep|"
                        r"トリガー|再テスト|受容|拒否|ブレイク|維持|終値|スイープ",
                        posture, re.I,
                    )),
                    bool(re.search(
                        r"why|because|reason|pending|until|ため|理由|未確定|まで",
                        posture, re.I,
                    )),
                ))
                if not posture_ok:
                    report.error(
                        gate_line,
                        "POSTURE_SEMANTIC",
                        "G.ip must include current numeric location, action NOW, "
                        "next 30M observable, and a reason",
                        "state current numeric location, explicit action NOW, next "
                        "30M trigger/retest, and why the operator waits or acts",
                    )

    # --- event handling
    wide_market_at_minute = parse_wall_clock_minute_strict(
        expand(market.get("at", ""), aliases)
    )
    wide_event_release_minute = None
    wide_event_is_actual = False
    ev = expand(event.get("ev", ""), aliases)
    src = expand(event.get("src", ""), aliases).strip().upper()
    if ev and not is_na(ev) and re.search(r"\d{1,2}:\d{2}", ev) and src not in ("VERIFIED", "UNVERIFIED"):
        report.warn(
            0, "EVENT_SRC",
            "E.ev carries a clock time but E.src does not say VERIFIED or UNVERIFIED",
            "set E.src=VERIFIED when the release time was confirmed, otherwise UNVERIFIED",
        )
    if src == "VERIFIED" and not str(event.get("ref", "")).strip():
        report.warn(0, "EVENT_REFERENCE", "E.src=VERIFIED but E.ref is absent",
                    "attach an official primary-source URL or identifier in E.ref")
    if wide_policy and scenarios:
        event_ref = expand(event.get("ref", ""), aliases).strip()
        event_check_raw = expand(event.get("at", ""), aliases)
        event_release_raw = expand(event.get("rt", ""), aliases)
        market_at_minute = wide_market_at_minute
        event_check_minute = parse_wall_clock_minute_strict(event_check_raw)
        if src != "VERIFIED":
            report.error(
                event_line,
                "WG_EVENT_CLOCK_UNVERIFIED",
                "WG-H1 directional scenarios require a VERIFIED event-clock scan",
                "verify the nearest material event against a primary calendar, "
                "or emit G.n=0",
            )
        if not event_ref or is_na(event_ref):
            report.error(
                event_line,
                "WG_EVENT_PROVENANCE_MISSING",
                "WG-H1 event-clock scan has no primary reference",
                "set E.ref to the official primary calendar used for the scan",
            )
        if event_check_minute is None:
            report.error(
                event_line,
                "WG_EVENT_CHECK_TIME_INVALID",
                "WG-H1 requires E.at as the exact JST verification timestamp",
                "use E.at=YYYY-MM-DD HH:MM JST",
            )
        elif market_at_minute is not None:
            event_check_age = market_at_minute - event_check_minute
            if event_check_age < 0 or event_check_age > 30:
                report.error(
                    event_line,
                    "WG_EVENT_CHECK_STALE",
                    "WG-H1 event-clock verification must be no more than "
                    "30 minutes old and not from the future",
                    "refresh the primary event calendar immediately before analysis",
                )
        if not ev or is_na(ev):
            report.error(
                event_line,
                "WG_EVENT_CLOCK_UNVERIFIED",
                "WG-H1 directional scenarios require the nearest material event "
                "or the exact sentinel NONE_WITHIN_30M",
                "set E.ev=NONE_WITHIN_30M only after a fresh verified scan, "
                "otherwise emit G.n=0",
            )
        elif is_no_event_window(ev):
            if event_release_raw and not is_na(event_release_raw):
                report.error(
                    event_line,
                    "WG_EVENT_SENTINEL_CONFLICT",
                    "E.ev=NONE_WITHIN_30M conflicts with a numeric E.rt release time",
                    "set E.rt=N/A for the verified no-event sentinel",
                )
        else:
            release_minute = parse_wall_clock_minute_strict(event_release_raw)
            if release_minute is None:
                report.error(
                    event_line,
                    "WG_EVENT_RELEASE_TIME_MISSING",
                    "WG-H1 requires the nearest event release in E.rt",
                    "use E.rt=YYYY-MM-DD HH:MM JST, or emit G.n=0",
                )
            elif market_at_minute is not None:
                wide_event_release_minute = release_minute
                wide_event_is_actual = True
                event_delta = release_minute - market_at_minute
                if event_delta < -30:
                    report.error(
                        event_line,
                        "WG_EVENT_CLOCK_STALE",
                        "E.rt describes an event more than 30 minutes in the past "
                        "and cannot prove the current blackout state",
                        "supply the next material event or a freshly verified "
                        "NONE_WITHIN_30M sentinel",
                    )
                elif abs(event_delta) <= 30:
                    report.error(
                        event_line,
                        "WG_EVENT_BLACKOUT",
                        "material event falls inside the frozen ±30-minute "
                        "WG-H1 blackout window",
                        "emit G.n=0 and wait until the blackout window has passed",
                    )

    # --- per-scenario checks
    parsed = []
    for lineno, tag, f in scenarios:
        for key, label in REQUIRED_SCENARIO_KEYS.items():
            if key not in f or not str(f[key]).strip():
                report.error(
                    lineno, "SCENARIO_FIELD",
                    f"{tag} is missing required field {key} ({label})",
                    f"add {key}=... ; a scenario missing {label} is not emitted",
                )

        side = (f.get("d") or "").strip().upper()
        entry = price_range(expand(f.get("en", ""), aliases))
        sl = scalar(expand(f.get("sl", ""), aliases))
        tps = [scalar(x) for x in split_escaped(expand(f.get("tp", ""), aliases), ",")]
        tps = (tps + [None, None, None])[:3]

        # scale for narrative price extraction: prefer entry, then SL, then TP1
        scale = None
        if entry:
            scale = (entry[0] + entry[1]) / 2
        elif sl is not None:
            scale = sl
        elif tps[0] is not None:
            scale = tps[0]
        inv = price_like(expand(f.get("iv", ""), aliases), scale) if scale else None

        parsed.append({
            "line": lineno, "tag": tag, "side": side,
            "entry": entry, "sl": sl, "inv": inv, "tps": tps, "f": f,
        })

        validate_projection(f, aliases, lineno, report)
        if f.get("en") and entry is None:
            report.error(
                lineno,
                "ENTRY_UNREADABLE",
                f"{tag}.en is present but has no finite readable entry price",
                "use a finite scalar or low..high entry token",
            )
        if f.get("sl") and sl is None:
            report.error(
                lineno,
                "SL_UNREADABLE",
                f"{tag}.sl is present but has no finite readable stop price",
                "use a finite scalar stop price",
            )
        raw_tp_items = split_escaped(expand(f.get("tp", ""), aliases), ",")
        if f.get("tp") and (
            len(raw_tp_items) != 3
            or any(tp is None or not math.isfinite(tp) for tp in tps)
        ):
            report.error(
                lineno,
                "TARGET_UNREADABLE",
                f"{tag}.tp must contain exactly three finite readable prices",
                "use exactly TP1,TP2,TP3 as comma-separated finite prices",
            )
        if wide_policy:
            bad_executable = []
            if not canonical_price_token(expand(f.get("en", ""), aliases), allow_range=True):
                bad_executable.append("en")
            if not canonical_price_token(expand(f.get("sl", ""), aliases), allow_range=False):
                bad_executable.append("sl")
            if not canonical_price_list(
                expand(f.get("tp", ""), aliases), ",", allow_range=False, expected_count=3
            ):
                bad_executable.append("tp")
            if not canonical_price_token(expand(f.get("iv", ""), aliases), allow_range=False):
                bad_executable.append("iv")
            if bad_executable:
                report.error(
                    lineno,
                    "WG_EXECUTABLE_TOKEN_INVALID",
                    f"{tag}: WG-H1 requires canonical exact executable/invalidation "
                    f"tokens; invalid: {', '.join(bad_executable)}",
                    "use bare decimal en/sl/iv and exactly three comma-separated bare "
                    "decimal TP values; no ~, ≈, prose, prefix, or suffix",
                )
            if wide_scale_reference is not None:
                executable_scale = [
                    *(entry or ()),
                    sl,
                    inv,
                    *tps,
                ]
                if any(
                    price is not None
                    and not within_wide_scale(price, wide_scale_reference)
                    for price in executable_scale
                ):
                    report.error(
                        lineno,
                        "WG_PRICE_SCALE_MISMATCH",
                        f"{tag}: entry/SL/TP/invalidation does not share latest "
                        f"O.c close scale {wide_scale_reference}",
                        "all executable and invalidation prices must be finite exact "
                        "MNQ prices within 0.5x..1.5x of the latest O.c close",
                    )

        if entry is None or sl is None:
            continue
        e_lo, e_hi = entry
        e_mid = (e_lo + e_hi) / 2

        # price order
        if side == "L" and not sl < e_lo:
            report.error(lineno, "PRICE_ORDER",
                         f"LONG requires SL < entry low, got SL={sl} entry low={e_lo}",
                         "place the stop below the entry band")
        if side == "S" and not sl > e_hi:
            report.error(lineno, "PRICE_ORDER",
                         f"SHORT requires SL > entry high, got SL={sl} entry high={e_hi}",
                         "place the stop above the entry band")

        # invalidation order
        iv_text = expand(f.get("iv", ""), aliases)
        sl_equals = bool(re.search(r"SL\s*=\s*Invalidation|Invalidation\s*=\s*SL|SL=無効化|無効化=SL", iv_text, re.I))
        if inv is None and not sl_equals:
            report.error(lineno, "INVALIDATION_UNREADABLE",
                         f"{tag}.iv has no numeric invalidation and does not declare SL = Invalidation",
                         "give a price, or declare the equality explicitly")
        elif inv is not None:
            if abs(inv - sl) < 1e-9:
                if not sl_equals:
                    report.error(lineno, "INVALIDATION_ORDER",
                                 "invalidation equals SL but the equality is not declared",
                                 "write SL = Invalidation in iv, or move invalidation inside the stop")
            elif side == "L" and not (sl < inv < e_lo):
                report.error(lineno, "INVALIDATION_ORDER",
                             f"LONG requires SL < invalidation < entry low; got {sl} / {inv} / {e_lo}",
                             "invalidation must protect before the hard stop")
            elif side == "S" and not (e_hi < inv < sl):
                report.error(lineno, "INVALIDATION_ORDER",
                             f"SHORT requires entry high < invalidation < SL; got {e_hi} / {inv} / {sl}",
                             "invalidation must protect before the hard stop")

        # Independent distance audit: preserve the order check above, then ensure the
        # invalidation is not so close to the hard stop that confirmation may lag it.
        risk_to_sl = abs(e_mid - sl)
        if inv is not None and risk_to_sl > 0 and abs(inv - sl) < risk_to_sl * 0.15:
            report.error(
                lineno,
                "SL_INVALIDATION_TOO_CLOSE",
                f"{tag}: invalidation ({inv}) sits only {abs(inv-sl):.2f} pt from SL ({sl}), "
                f"less than 15% of risk ({risk_to_sl:.2f} pt) — invalidation may not protect "
                "before the hard stop is hit",
                "widen the gap between invalidation and SL, or accept that the hard stop is "
                "the effective invalidation and declare SL = Invalidation",
            )

        regime_code = (market.get("rg") or "").strip().upper()
        setup_code = (f.get("su") or "").strip().upper()
        wide_eligible = regime_code in {"TR", "EX"} and setup_code in {"BC", "PC"}
        if wide_policy and not wide_eligible:
            report.error(
                lineno,
                "WG_REGIME_INELIGIBLE",
                f"{tag}: WG-H1 is restricted to TR/EX breakout or pullback "
                f"continuation; got rg={regime_code} su={setup_code}",
                "use G.n=0 / FLAT, or evaluate under a separately versioned non-wide policy",
            )
        regime_evidence_mode = kv_map(
            expand(market.get("rm", ""), aliases)
        ).get("mode", "").upper()
        if (
            wide_policy
            and regime_evidence_mode == "SCREENSHOT"
            and (f.get("ha") or "").strip().upper() != "A"
        ):
            report.error(
                lineno,
                "WG_SCREENSHOT_ALIGNMENT_MISSING",
                f"{tag}: RG-H1 SCREENSHOT fallback requires S.ha=A (ALIGNED)",
                "mark S.ha=A only when supplied 3M/15M/45M frames are actually "
                "aligned; otherwise emit G.n=0",
            )
        if wide_policy and wide_unit is None:
            report.error(
                lineno,
                "WG_RANGE_UNIT_MISSING",
                f"{tag}: WG-H1 requires {WIDE_REQUIRED_BARS} exact consecutive closed O.c bars "
                f"to calculate the trailing {WIDE_ATR_LENGTH} True Range simple mean; "
                f"window audit: {wide_time_reason}",
                "supply unique ascending bars at exactly M.tf cadence, ending on the latest "
                "closed bar at M.at, without ~-estimated prices; otherwise emit G.n=0",
            )

        # Structural distance audit: mirror structuralStopPlan. Protective
        # anchors are limited to S/R/RBS/SBR/QML/OCL. Level-spacing fallback
        # uses the nearest boundary of every explicit mapped Z range.
        if entry is not None and sl is not None:
            e_lo, e_hi = entry
            levels = structural_levels(zone, aliases, exact_only=wide_policy)
            if side == "L":
                candidates = [(lo, hi) for lo, hi in levels if hi < e_lo - 1e-7]
                anchor_range = max(candidates, key=lambda item: item[1]) if candidates else None
                anchor = anchor_range[0] if anchor_range else None
            elif side == "S":
                candidates = [(lo, hi) for lo, hi in levels if lo > e_hi + 1e-7]
                anchor_range = min(candidates, key=lambda item: item[0]) if candidates else None
                anchor = anchor_range[1] if anchor_range else None
            else:
                anchor = None
            if wide_policy and anchor is None:
                report.error(
                    lineno,
                    "WG_STRUCTURAL_ANCHOR_MISSING",
                    f"{tag}: WG-H1 requires a named S/R/RBS/SBR/QML/OCL "
                    "level on the losing side",
                    "map the protective structure in Z, or emit NO TRADE",
                )
            if anchor is not None:
                pivot = scalar(market.get("px")) if market.get("px") else None
                if pivot is None and zone.get("dz"):
                    dz_range = price_range(expand(zone["dz"], aliases))
                    if dz_range:
                        pivot = (dz_range[0] + dz_range[1]) / 2
                unit = wide_unit if wide_policy else observed_unit
                unit_source = "SUPPLIED OHLC ATR14-SMA" if wide_policy else "SUPPLIED OHLC RANGE"
                if unit is None and not wide_policy:
                    unit = level_spacing_unit(mapped_levels(zone, aliases), pivot)
                    unit_source = "MAPPED LEVEL SPACING"
                if unit is not None:
                    buffer = max(unit * (WIDE_BUFFER_MULT if wide_policy else 0.32), 2)
                    recommended = anchor - buffer if side == "L" else anchor + buffer
                    beyond = sl <= recommended if side == "L" else sl >= recommended
                    risk_to_sl = abs(e_mid - sl)
                    if wide_policy:
                        minimum = max(
                            unit * WIDE_MINIMUM_RANGE_MULT,
                            abs(e_mid - recommended),
                        )
                    else:
                        inv_dist = abs(e_mid - inv) * 1.12 if inv is not None else 0
                        minimum = max(unit * 0.72, inv_dist, 4)
                    if not beyond or risk_to_sl < minimum:
                        report.error(
                            lineno,
                            "SL_STRUCTURAL_DISTANCE",
                            f"{tag}: SL ({sl}) sits inside the declared structural level "
                            f"at {anchor} with only {risk_to_sl:.2f} pt of risk to SL "
                            f"(recommended beyond {recommended:.2f}, minimum risk "
                            f"{minimum:.2f}, range unit {unit:.2f} from {unit_source}, "
                            f"policy {WIDE_POLICY_VERSION if wide_policy else 'BASELINE'})",
                            "move the stop beyond the nearest declared S/R/RBS/SBR/"
                            "QML/OCL level with a buffer sized to the market's own "
                            "supplied observed range or declared level spacing",
                        )

        for price in [e_lo, e_hi, sl, inv, *tps]:
            scaled = price * 4 if price is not None else None
            if price is not None and (
                not math.isfinite(price) or not math.isfinite(scaled)
            ):
                report.error(
                    lineno,
                    "PRICE_OUT_OF_DOMAIN",
                    f"{tag}: executable price cannot be safely represented at "
                    "0.25-point tick scale",
                    "replace overflow or out-of-domain input with a finite MNQ price",
                )
            elif price is not None and abs(scaled - round(scaled)) > 1e-7:
                report.error(
                    lineno,
                    "TICK_ALIGNMENT",
                    f"{tag}: price {price} is not aligned to the 0.25-point NQ/MNQ tick",
                    "round every executable price to the nearest valid 0.25 tick",
                )

        # targets must be on the correct side and strictly monotonic
        for i, tp in enumerate(tps):
            if tp is None:
                continue
            if side == "L" and tp <= e_mid:
                report.error(lineno, "TARGET_SIDE",
                             f"LONG TP{i+1}={tp} is not above the entry midpoint {e_mid}",
                             "targets sit beyond the entry in the trade direction")
            if side == "S" and tp >= e_mid:
                report.error(lineno, "TARGET_SIDE",
                             f"SHORT TP{i+1}={tp} is not below the entry midpoint {e_mid}",
                             "targets sit beyond the entry in the trade direction")
        present_tps = [tp for tp in tps if tp is not None]
        for index in range(1, len(present_tps)):
            ordered = (
                present_tps[index - 1] < present_tps[index]
                if side == "L"
                else present_tps[index - 1] > present_tps[index]
                if side == "S"
                else True
            )
            if not ordered:
                report.error(
                    lineno,
                    "TARGET_ORDER",
                    f"{tag}: available TP values are not strictly ordered in the trade direction",
                    "require TP1, TP2, TP3 to progress monotonically away from entry",
                )

        # R:R recomputation from the entry midpoint
        risk = (e_mid - sl) if side == "L" else (sl - e_mid) if side == "S" else None
        claimed = rr_claims(f.get("rr", ""))
        if risk and risk > 0:
            actual = []
            for tp in tps:
                if tp is None:
                    actual.append(None)
                    continue
                reward = (tp - e_mid) if side == "L" else (e_mid - tp)
                actual.append(reward / risk)
            if wide_policy:
                road_prices = []
                for lo, hi in mapped_levels(zone, aliases, exact_only=True):
                    road_prices.append(lo)
                    if abs(hi - lo) > 1e-9:
                        road_prices.append(hi)
                dz_text = expand(zone.get("dz", ""), aliases)
                if dz_text and canonical_price_token(dz_text, allow_range=True):
                    dz_road = price_range(dz_text)
                    if dz_road:
                        road_prices.extend(dz_road)
                for part in re.split(r"\s*;\s*", expand(zone.get("tm", ""), aliases)):
                    if not canonical_price_token(part, allow_range=True):
                        continue
                    road = price_range(part)
                    if road:
                        road_prices.append(road[0])
                        if abs(road[1] - road[0]) > 1e-9:
                            road_prices.append(road[1])
                road_tolerance = 0.001
                directional_roads = sorted(
                    {
                        round(price, 6)
                        for price in road_prices
                        if (price > e_mid if side == "L" else price < e_mid)
                    },
                    reverse=(side == "S"),
                )
                previous_expected = None
                for index in range(3):
                    tp = tps[index]
                    floor = WIDE_TARGET_FLOORS[index]
                    candidates = [
                        price for price in directional_roads
                        if (
                            ((price - e_mid) if side == "L" else (e_mid - price)) / risk
                            + 1e-9 >= floor
                        )
                        and (
                            previous_expected is None
                            or (
                                price > previous_expected + road_tolerance
                                if side == "L"
                                else price < previous_expected - road_tolerance
                            )
                        )
                    ]
                    expected_road = candidates[0] if candidates else None
                    if expected_road is None:
                        report.error(
                            lineno,
                            "WG_TARGET_ROADBLOCK_MISSING",
                            f"{tag}: no successive exact mapped roadblock exists for "
                            f"TP{index+1} at or beyond {floor:.2f}R",
                            "map the next real roadblock or emit NO TRADE; never "
                            "invent an R-multiple target",
                        )
                    else:
                        previous_expected = expected_road
                    if tp is None:
                        report.error(
                            lineno,
                            "WG_TARGET_MISSING",
                            f"{tag}: WG-H1 requires TP{index+1} and all three mapped targets",
                            "map TP1/TP2/TP3 or emit NO TRADE",
                        )
                        continue
                    ratio = actual[index]
                    if ratio is None or ratio + 1e-9 < floor:
                        report.error(
                            lineno,
                            "WG_TARGET_FLOOR",
                            f"{tag}: TP{index+1} is "
                            f"{'N/A' if ratio is None else format(ratio, '.2f')}R, "
                            f"below WG-H1 floor {floor:.2f}R",
                            "use a farther named roadblock; if none exists, reject "
                            "the scenario instead of inventing a target",
                        )
                    road_match = any(
                        (price > e_mid if side == "L" else price < e_mid)
                        and abs(price - tp) <= road_tolerance
                        for price in road_prices
                    )
                    if not road_match:
                        report.error(
                            lineno,
                            "WG_TARGET_UNMAPPED",
                            f"{tag}: TP{index+1}={tp} does not exactly match "
                            "a declared Z/TM roadblock",
                            "declare the exact target level in Z or move TP to a "
                            "real mapped roadblock",
                        )
                    elif expected_road is not None and abs(tp - expected_road) > road_tolerance:
                        report.error(
                            lineno,
                            "WG_TARGET_NOT_SUCCESSIVE",
                            f"{tag}: TP{index+1}={tp} skips the required next "
                            f"roadblock {expected_road} at/after {floor:.2f}R",
                            "use the first eligible exact roadblock for each rung; "
                            "a skipped rung changes the setup version",
                        )
            for i, (a, c) in enumerate(zip(actual, claimed)):
                if a is None and c is None:
                    continue
                if a is None or c is None or abs(a - c) > 0.06:
                    report.error(
                        lineno, "RR_MISMATCH",
                        f"{tag}.rr TP{i+1} claims {c}, recomputation from entry midpoint {e_mid:.2f} gives "
                        f"{'N/A' if a is None else format(a, '.2f')}",
                        "recompute R:R from the entry-band midpoint",
                    )
            if actual and actual[0] is not None and actual[0] < 1.0:
                note = f.get("no", "")
                if not re.search(r"downgrade|格下げ|REJECT|1\.0R|1R", note, re.I):
                    report.error(
                        lineno, "RR_TP1_LOW",
                        f"TP1 is {actual[0]:.2f}R (below 1.00R) with no downgrade recorded",
                        "downgrade the grade one level or REJECT the scenario, and say why in no=",
                    )
        elif risk is not None and risk <= 0:
            report.error(lineno, "RISK_NONPOSITIVE",
                         f"risk from entry midpoint to SL is {risk}",
                         "the stop must sit on the losing side of the entry")

        # ASSUMED causal chain caps confidence and blocks PRIMARY
        cc = expand(f.get("cc", ""), aliases)
        if re.search(r"\bASSUMED\b", cc, re.I):
            cf = f.get("cf")
            if cf and numbers_in(cf) and numbers_in(cf)[0] > 64:
                report.error(
                    lineno, "ASSUMED_CONFIDENCE",
                    f"causal chain contains ASSUMED but cf={numbers_in(cf)[0]:.0f} exceeds the cap of 64",
                    "cap confidence at 64 until an observation replaces the assumption",
                )
            if (f.get("do") or "").strip().upper() == "PRIMARY":
                report.error(
                    lineno, "ASSUMED_PRIMARY",
                    "causal chain contains ASSUMED but the scenario is marked PRIMARY",
                    "an ASSUMED link cannot be PRIMARY; use SECONDARY until it is observed",
                )

        # confidence is not a probability
        cf = f.get("cf")
        if cf and numbers_in(cf):
            v = numbers_in(cf)[0]
            if not (0 <= v <= 100):
                report.error(lineno, "CONFIDENCE_RANGE", f"cf={v} is outside 0..100",
                             "confidence is a 0-100 setup-quality score")
        sp = f.get("sp")
        if sp and numbers_in(sp) and qm == "H":
            report.error(
                lineno, "HEURISTIC_PROBABILITY",
                f"{tag}.sp carries a probability while qm=HEURISTIC",
                "omit sp, or raise the Quant Mode with real out-of-sample data",
            )

        # event handling must name a position action, not a mood
        eh = expand(f.get("eh", ""), aliases)
        if eh and not re.search(
            r"\^FLAT|\^NOENT|flat|close|exit|reduce|half|partial|break.?even|\bBE\b|"
            r"no[ _]?new|stand\s*down|cancel|全決済|決済|撤退|半分|縮小|建値|新規.{0,8}(?:禁止|見送り)|持ち越さない",
            eh, re.I,
        ):
            report.error(
                lineno, "EVENT_HANDLING",
                f"{tag}.eh={eh!r} does not define a position action",
                "state a flatten deadline, a no-entry window, or an explicit carry rule",
            )

        # Strict WG-H1 expiry and event-crossing position action.
        vu = expand(f.get("vu", ""), aliases)
        vu_minute = parse_wall_clock_minute_strict(vu)
        if wide_policy:
            if vu_minute is None:
                report.error(
                    lineno,
                    "WG_VALID_UNTIL_INVALID",
                    f"{tag}.vu must be an exact JST timestamp under WG-H1",
                    "use vu=YYYY-MM-DD HH:MM JST",
                )
            elif (
                wide_market_at_minute is not None
                and vu_minute <= wide_market_at_minute
            ):
                report.error(
                    lineno,
                    "WG_VALID_UNTIL_NOT_FUTURE",
                    f"{tag}.vu must be later than M.at",
                    "set a finite future expiry before emitting the scenario",
                )
            if (
                wide_event_is_actual
                and wide_event_release_minute is not None
                and vu_minute is not None
                and vu_minute > wide_event_release_minute
            ):
                action = bool(re.search(
                    r"\^FLAT|flat|close|exit|reduce|half|partial|"
                    r"全決済|決済|撤退|半分|縮小",
                    eh,
                    re.I,
                ))
                deadline_match = re.search(
                    r"\d{4}-\d{2}-\d{2}[ T](?:[01]\d|2[0-3]):[0-5]\d\s+JST",
                    eh,
                    re.I,
                )
                deadline_minute = (
                    parse_wall_clock_minute_strict(deadline_match.group(0))
                    if deadline_match else None
                )
                if (
                    not action
                    or deadline_minute is None
                    or deadline_minute > wide_event_release_minute
                ):
                    report.error(
                        lineno,
                        "WG_EVENT_POSITION_ACTION_MISSING",
                        f"{tag} remains valid across E.rt but lacks a timed "
                        "risk-reducing position action before the release",
                        "state FLAT/CLOSE/REDUCE plus a YYYY-MM-DD HH:MM JST "
                        "deadline no later than E.rt",
                    )
        elif vu and not is_na(vu) and not re.search(r"JST", vu, re.I):
            report.error(lineno, "VALID_UNTIL_TZ", f"{tag}.vu={vu!r} has no timezone",
                         "write the expiry in JST, e.g. 2026-07-14 21:00 JST")

    # --- mutual exclusion on overlapping entry bands
    mx = expand(gate.get("mx", ""), aliases)
    overlaps = []
    for i in range(len(parsed)):
        for j in range(i + 1, len(parsed)):
            a, b = parsed[i], parsed[j]
            if not a["entry"] or not b["entry"]:
                continue
            lo = max(a["entry"][0], b["entry"][0])
            hi = min(a["entry"][1], b["entry"][1])
            if lo <= hi:
                overlaps.append((a, b))
    if overlaps and (not mx or re.match(r"^\s*NOT REQUIRED\s*$", mx, re.I)):
        for a, b in overlaps:
            report.error(
                gate_line, "EXCLUSION_MISSING",
                f"{a['tag']} and {b['tag']} have overlapping entry bands but G.mx is NOT REQUIRED",
                "order the triggers and name which scenario becomes STAND DOWN",
            )
    if overlaps and mx and not re.search(r"stand\s*down|見送り|停止|無効", mx, re.I):
        report.error(gate_line, "EXCLUSION_ACTION",
                     "G.mx does not name a STAND DOWN action",
                     "say which scenario stands down when the other trigger fires first")

    return report


def main(argv):
    # Required diagnostics include characters outside Windows' legacy CP932 range.
    # Keep CLI output deterministic instead of crashing while rendering a finding.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    if len(argv) != 2:
        print(__doc__)
        return 2
    path = argv[1]
    try:
        text = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    except OSError as exc:
        print(f"cannot read {path}: {exc}")
        return 2

    report = validate(text)
    label = path if path != "-" else "<stdin>"

    if report.warnings:
        print(f"NQX WARNINGS ({len(report.warnings)}) in {label}:")
        for w in report.warnings:
            print(w.render())
    if report.errors:
        print(f"NQX VALIDATION FAILED ({len(report.errors)} error(s)) in {label}:")
        for e in report.errors:
            print(e.render())
        print("\nPacket rejected. Repair and re-validate before sending to Nightwatch.")
        return 1

    print(f"NQX VALIDATION PASSED - {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
