"""NQX 音声キューの仕上げ —— **予備手段(SAPI 経路)の DSP**(R65, 2026-09-07)。

本番は neural.py(Edge のニューラル TTS)。ここの関数群(trim / high_pass / leveler / beep /
write_wav)は neural.py からも使う。

**方針は「聞き取りやすさが最優先」**。2026-09-07 の最初の版は 112Hz 固定ピッチのチャネル
ボコーダで機械の声を作ったが、ユーザー評価は「ロボットすぎる上に何を言っているか全然
聞き取れない」だった。ボコーダは破棄し、素の音声を**明瞭にする**方向へ作り替えた
(ユーザー指示「ロボットって固定観念にとらわれず聞きやすい音声を」)。

    powershell -File tools\\voice\\synth.ps1 -OutDir .\\raw
    python tools\\voice\\voicefx.py --raw .\\raw --out public\\voice

鎖(順序に意味がある):
  1. 前後の無音を落とす —— SAPI は語尾に長い間を付ける。
  2. 再標本化 0.92 倍 —— 1.4 半音下げ、8% ゆっくり。素の Windows 音声そのままの軽さを
     抜き、落ち着いた声にする。**これ以上下げない**(下げるほど不明瞭になる)。
  3. 低域を切る(HP 110Hz)—— こもりを取る。小さなスピーカーでは低域は鳴らない。
  4. 子音帯(1.9〜4.8kHz)を持ち上げる —— 語の輪郭は子音で決まる。ここが明瞭さの本体。
  5. 緩いレベラー(音節ごとの起伏をならす)—— 端末の音量が小さくても語尾が消えない。
  6. 柔らかく頭を潰して正規化 —— 歪ませずに芯を出す。
  7. 頭に 2 音の合図(1050Hz → 1575Hz)+ 120ms の間。合図だけが「通信」の色を出し、
     声そのものには金属質な加工(コム・リングモジュレーション)を **一切しない**。

出力は 16kHz / 16bit / mono。生 WAV は出荷しない(public/voice に置くのは加工済みだけ)。
"""

from __future__ import annotations

import argparse
import pathlib
import wave

import numpy as np

SR = 16000
RESAMPLE = 0.92            # 1.4 半音下げ。これ以上は明瞭さを失う
HP_HZ = 110.0              # こもりを取る
PRESENCE = (1900.0, 4800.0, 0.55)   # 子音帯(下限, 上限, 足す量)
LEVEL_TARGET = 0.34        # レベラーの狙い(RMS)。R65 追補3 で 0.22 から上げた(音量)
LEVEL_RANGE = (0.80, 4.5)  # かけ過ぎない(息やノイズを持ち上げない)
LEVEL_ATTACK_MS, LEVEL_RELEASE_MS = 25.0, 180.0
BEEP = ((1050.0, 0.055), (1575.0, 0.055))
BEEP_GAP = 0.02
BEEP_LEVEL = 0.38   # R65 追補3: 声を RMS で持ち上げた分、合図も上げる
LEAD_IN = 0.12
PEAK = 0.80


def read_wav(path: pathlib.Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit(f"{path}: 16bit mono を想定している")
        if w.getframerate() != SR:
            raise SystemExit(f"{path}: {SR}Hz を想定している(実際は {w.getframerate()})")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0


def write_wav(path: pathlib.Path, x: np.ndarray) -> None:
    data = np.clip(x, -1.0, 1.0)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((data * 32767.0).astype("<i2").tobytes())


def trim(x: np.ndarray, floor: float = 0.012, pad_s: float = 0.03) -> np.ndarray:
    """前後の無音を落とす。そのままだと合図と声が離れて間延びする。"""
    loud = np.flatnonzero(np.abs(x) > floor)
    if loud.size == 0:
        return x
    pad = int(pad_s * SR)
    return x[max(0, loud[0] - pad):min(len(x), loud[-1] + pad)]


def resample(x: np.ndarray, factor: float) -> np.ndarray:
    """線形補間の再標本化。factor < 1 で少し低く・少しゆっくりになる。"""
    n = int(round(len(x) / factor))
    src = np.arange(n) * factor
    left = np.clip(src.astype(int), 0, len(x) - 1)
    right = np.clip(left + 1, 0, len(x) - 1)
    frac = src - left
    return x[left] * (1 - frac) + x[right] * frac


def one_pole_lp(x: np.ndarray, hz: float) -> np.ndarray:
    a = np.exp(-2 * np.pi * hz / SR)
    y = np.empty_like(x)
    prev = 0.0
    for i, v in enumerate(x):
        prev = a * prev + (1 - a) * v
        y[i] = prev
    return y


def high_pass(x: np.ndarray, hz: float) -> np.ndarray:
    return x - one_pole_lp(x, hz)


def presence(x: np.ndarray) -> np.ndarray:
    """子音帯だけを足す。語の輪郭(t / s / k)はここで決まる。"""
    lo, hi, gain = PRESENCE
    band = one_pole_lp(x, hi) - one_pole_lp(x, lo)
    return x + gain * band


def leveler(x: np.ndarray) -> np.ndarray:
    """音節ごとの起伏をならす。潰さずに、小さい所だけ持ち上げる。"""
    atk = np.exp(-1.0 / (LEVEL_ATTACK_MS * 0.001 * SR))
    rel = np.exp(-1.0 / (LEVEL_RELEASE_MS * 0.001 * SR))
    env = np.empty_like(x)
    e = 0.0
    for i, v in enumerate(np.abs(x)):
        e = (atk if v > e else rel) * e + (1 - (atk if v > e else rel)) * v
        env[i] = e
    gain = np.clip(LEVEL_TARGET / np.maximum(env, 1e-4), *LEVEL_RANGE)
    return x * one_pole_lp(gain, 12.0)


def beep() -> np.ndarray:
    """2 音の合図(上行 5 度)。声には触れず、ここだけが「通信」の色を出す。"""
    parts = []
    for i, (hz, dur) in enumerate(BEEP):
        n = int(dur * SR)
        t = np.arange(n) / SR
        env = np.minimum(1.0, np.arange(n) / (0.004 * SR)) * np.exp(-t * 26.0)
        parts.append(np.sin(2 * np.pi * hz * t) * env * BEEP_LEVEL)
        if i == 0:
            parts.append(np.zeros(int(BEEP_GAP * SR)))
    return np.concatenate(parts)


def polish(voice: np.ndarray) -> np.ndarray:
    y = resample(trim(voice), RESAMPLE)
    y = high_pass(y, HP_HZ)
    y = presence(y)
    y = leveler(y)
    y = np.tanh(y * 1.35) / np.tanh(1.35)     # 柔らかい頭打ち(歪ませない)
    y = trim(y, floor=0.01, pad_s=0.02)
    peak = float(np.abs(y).max())
    return y * (PEAK / peak) if peak > 1e-6 else y


def main() -> None:
    ap = argparse.ArgumentParser(description="SAPI の生 WAV を聞き取りやすいキューに仕上げる")
    ap.add_argument("--raw", required=True, type=pathlib.Path, help="synth.ps1 の出力ディレクトリ")
    ap.add_argument("--out", required=True, type=pathlib.Path, help="public/voice")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    lead = np.zeros(int(LEAD_IN * SR))
    tone = beep()
    for src in sorted(args.raw.glob("*.wav")):
        y = np.concatenate([tone, lead, polish(read_wav(src))])
        dst = args.out / src.name
        write_wav(dst, y)
        print(f"{dst}  {len(y) / SR:.2f}s")


if __name__ == "__main__":
    main()
