"""NQX 音声キューの生成(R65 第 3 版, 2026-09-07)—— 実在の男性音声から作る。

経緯: この PC の SAPI には英語の女性(Zira)と日本語しか入っていない。女性音声を下げる方向は
2 度失敗した(ボコーダ = 聞き取れない / 再標本化 = 「呪われた野原しんのすけ」)。**ピッチを
下げても声道の長さは変わらない**ので、女性声から低い男性声は作れない。ユーザー合意のうえで
Microsoft Edge のニューラル TTS(`edge-tts`)へ切り替え、**本物の男性の声**を素材にする。

    python tools/voice/neural.py --list                       # 候補の声と基本周波数を測る
    python tools/voice/neural.py --voice en-US-ChristopherNeural --out public/voice

規律:
  - 生成は**開発時に一度だけ**。アプリの実行時にネットワークへ出ることはない(WAV を同梱)。
  - 送るのは `lines.json` の 8 語句だけ。口座・建玉・鍵など運用の情報は一切渡さない。
  - 台本の正本は `lines.json`(sound.js の CUE_TEXT と一致することをテストで縛る)。

仕上げ(素材が良いので最小限):
  24kHz mono → 16kHz へ落とす → 低域切り(HP 90Hz)→ 子音帯を少しだけ持ち上げ →
  緩いレベラー → 正規化 → 頭に 2 音の合図。金属質な加工はしない。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import pathlib
import tempfile

import numpy as np
import soundfile as sf

from voicefx import SR, beep, high_pass, leveler, one_pole_lp, trim, write_wav

# 深さ・落ち着きで選んだ候補(en-US の男性ニューラル)。--list で実測して選ぶ。
CANDIDATES = (
    "en-US-ChristopherNeural",   # Reliable, Authority
    "en-US-GuyNeural",           # Passion
    "en-US-AndrewNeural",        # Warm, Confident, Authentic
    "en-US-BrianNeural",         # Approachable, Casual, Sincere
    "en-US-EricNeural",          # Rational
    "en-US-SteffanNeural",       # Rational
    "en-US-RogerNeural",         # Lively
)
# 2026-09-07 追補(ユーザー「もっと自信に満ち溢れたキレのある声に」):
# 「キレ」は声色ではなく **立ち上がりと間** で決まる。子音を立て、語頭の勢いを潰さず、
# 合図と声の間を詰め、語尾の余韻を切る。速さは合成側(--rate)で稼ぐ。
# 2026-09-07 追補2(ユーザー「もう少し厚みを持たせて」): キレを保ったまま**胸の帯**を足す。
# 基音のすぐ上(110〜340Hz)が声の body。ここを削ると細く、上げ過ぎると籠もる。
BODY = (140.0, 420.0, 1.60)         # 胸の帯 = 厚み(飽和の**後**に足す。前だと潰される)
CHEST = 0.55                        # 胸の帯の倍音(小さなスピーカーでも「太く」聞こえる)
FUND = (70.0, 130.0, 0.45)          # 基音そのもの(ヘッドホンでの重さ)
PRESENCE = (2200.0, 5600.0, 0.50)   # 子音の輪郭(t/k/s)= 歯切れ
BITE = (3000.0, 0.26)               # 3kHz 以上の棚。厚みと釣り合う量に戻す
HP_HZ = 58.0                        # 基音の下だけ切る(高いほど胸が痩せる)
ATTACK_KEEP = 0.18                  # 語頭の勢いを残す量。大きいほど山が立ち、平均音量は下がる
LEAD_IN = 0.05                      # 合図 → 声。詰めるほど機敏に聞こえる
TAIL_PAD = 0.012                    # 語尾の余韻は切る
# 2026-09-07 追補3(ユーザー「もう少しボリュームを大きくして」): 山(peak)ではなく
# **平均(RMS)**で揃える。山だけ 0.86 に揃えると、間のある台詞(「Armed. Stand by.」)が
# 他より 3dB 小さく聞こえる。RMS を目標へ合わせてから、天井を超える所だけ潰す。
TARGET_RMS = 0.30                   # ≈ −10.5 dBFS(前は −14〜−17 dBFS)
CEILING = 0.97                      # 山の天井。1.0 に貼ると端末側で歪む


async def speak(text: str, voice: str, rate: str, pitch: str) -> np.ndarray:
    """edge-tts で 1 語句を合成し、16kHz mono の float 配列にして返す。"""
    import edge_tts

    chunks: list[bytes] = []
    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            chunks.append(chunk["data"])
    if not chunks:
        raise SystemExit(f"{voice}: 音声が返らなかった")
    # libsndfile は MP3 を読める(1.2 以降)。一時ファイル経由が最も素直。
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp.write(b"".join(chunks))
        path = tmp.name
    try:
        data, rate_in = sf.read(path, dtype="float64", always_2d=True)
    finally:
        pathlib.Path(path).unlink(missing_ok=True)
    mono = data.mean(axis=1)
    if rate_in != SR:                      # 24kHz → 16kHz(線形補間で十分)
        n = int(round(len(mono) * SR / rate_in))
        src = np.arange(n) * (rate_in / SR)
        left = np.clip(src.astype(int), 0, len(mono) - 1)
        right = np.clip(left + 1, 0, len(mono) - 1)
        frac = src - left
        mono = mono[left] * (1 - frac) + mono[right] * frac
    return mono


def body(x: np.ndarray) -> np.ndarray:
    """厚みを足す。3 層 —— 胸の帯・その倍音・基音。

    倍音(CHEST)を混ぜるのは、電話やノート PC のスピーカーが 200Hz 以下をほとんど鳴らさない
    ため。帯を上げるだけだと、そういう端末では厚みが消える。倍音なら上の帯にも weight が乗る。
    """
    lo, hi, gain = BODY
    band = one_pole_lp(x, hi) - one_pole_lp(x, lo)
    flo, fhi, fgain = FUND
    fund = one_pole_lp(x, fhi) - one_pole_lp(x, flo)
    return x + gain * band + CHEST * np.tanh(3.0 * band) * 0.5 + fgain * fund


def presence(x: np.ndarray) -> np.ndarray:
    lo, hi, gain = PRESENCE
    y = x + gain * (one_pole_lp(x, hi) - one_pole_lp(x, lo))
    hz, bite = BITE                     # 高域の棚(息と子音の縁)
    return y + bite * (y - one_pole_lp(y, hz))


def snap(x: np.ndarray) -> np.ndarray:
    """語頭の立ち上がりを残す。レベラーが最初の一撃まで均すと語気が抜ける。"""
    env = np.abs(x)
    smooth = one_pole_lp(env, 24.0)
    onset = np.maximum(0.0, env - smooth * 1.15)        # 平均を越えた分 = 立ち上がり
    return x + ATTACK_KEEP * np.sign(x) * onset


def limit(x: np.ndarray, ceiling: float) -> np.ndarray:
    """速い追従で天井を超える所だけ抑える(その分だけ全体を上げられる)。"""
    atk = np.exp(-1.0 / (0.0015 * SR))
    rel = np.exp(-1.0 / (0.060 * SR))
    env = np.empty_like(x)
    e = 0.0
    for i, v in enumerate(np.abs(x)):
        e = (atk if v > e else rel) * e + (1 - (atk if v > e else rel)) * v
        env[i] = e
    gain = np.minimum(1.0, ceiling / np.maximum(env, 1e-6))
    return x * one_pole_lp(gain, 90.0)


def loudness(y: np.ndarray) -> np.ndarray:
    """RMS を揃えてから天井で抑える。キュー同士の聞こえの大きさが揃う。"""
    voiced = y[np.abs(y) > 0.02]
    rms = float(np.sqrt((voiced ** 2).mean())) if voiced.size else 0.0
    if rms > 1e-6:
        y = y * min(6.0, TARGET_RMS / rms)
    y = limit(y, CEILING)
    peak = float(np.abs(y).max())
    return y * (CEILING / peak) if peak > CEILING else y


def finish(voice_audio: np.ndarray) -> np.ndarray:
    y = trim(voice_audio, floor=0.014, pad_s=0.012)
    y = high_pass(y, HP_HZ)
    y = presence(y)
    y = leveler(y)
    y = snap(y)
    y = np.tanh(y * 1.25) / np.tanh(1.25)
    # 胸の帯は **最後に** 足す。レベラーと飽和の前に足すと、山を均される側に回って
    # ゲインを上げても厚みが増えない(実測: 0.42 → 1.10 で 0.200 → 0.209 しか動かなかった)。
    y = body(y)
    y = trim(y, floor=0.014, pad_s=TAIL_PAD)
    return loudness(y)


def fundamental(x: np.ndarray) -> float:
    """中央値の基本周波数(声の低さの目安)。"""
    win = int(0.04 * SR)
    vals = []
    for i in range(0, len(x) - win, win // 2):
        seg = x[i:i + win]
        if np.sqrt((seg ** 2).mean()) < 0.06:
            continue
        seg = seg - seg.mean()
        ac = np.correlate(seg, seg, "full")[win - 1:]
        lo, hi = int(SR / 320), int(SR / 60)
        if hi >= len(ac) or ac[lo:hi].max() < 0.30 * ac[0]:
            continue
        vals.append(SR / (lo + int(np.argmax(ac[lo:hi]))))
    return float(np.median(vals)) if vals else float("nan")


def lines(path: pathlib.Path) -> dict[str, str]:
    table = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in table.items() if not k.startswith("_")}


def main() -> None:
    ap = argparse.ArgumentParser(description="Edge のニューラル TTS からキュー音声を作る")
    ap.add_argument("--voice", default="en-US-ChristopherNeural")
    ap.add_argument("--rate", default="+2%", help="話す速さ。キレを出すなら速め、重さを出すなら遅め")
    ap.add_argument("--pitch", default="-12Hz", help="低くするなら負の値。下げ過ぎると鈍る")
    ap.add_argument("--out", type=pathlib.Path, help="public/voice(省略時は書き出さない)")
    ap.add_argument("--lines", type=pathlib.Path,
                    default=pathlib.Path(__file__).with_name("lines.json"))
    ap.add_argument("--list", action="store_true", help="候補の声を 1 語句ずつ作って基本周波数を測る")
    ap.add_argument("--sample-dir", type=pathlib.Path, help="--list の試聴ファイルを置く場所")
    args = ap.parse_args()

    if args.list:
        text = "Armed. Stand by. Gain secured."
        for voice in CANDIDATES:
            audio = finish(asyncio.run(speak(text, voice, args.rate, args.pitch)))
            note = f"{voice:28s} F0 {fundamental(audio):5.1f}Hz  {len(audio) / SR:.2f}s"
            if args.sample_dir:
                args.sample_dir.mkdir(parents=True, exist_ok=True)
                dst = args.sample_dir / f"{voice}.wav"
                write_wav(dst, np.concatenate([beep(), np.zeros(int(LEAD_IN * SR)), audio]))
                note += f"  -> {dst.name}"
            print(note)
        return

    if not args.out:
        raise SystemExit("--out か --list を指定する")
    args.out.mkdir(parents=True, exist_ok=True)
    tone = beep()
    lead = np.zeros(int(LEAD_IN * SR))
    for name, text in lines(args.lines).items():
        audio = finish(asyncio.run(speak(text, args.voice, args.rate, args.pitch)))
        dst = args.out / f"{name}.wav"
        write_wav(dst, np.concatenate([tone, lead, audio]))
        print(f"{dst}  {len(audio) / SR + len(tone) / SR + LEAD_IN:.2f}s  F0 {fundamental(audio):.0f}Hz  \"{text}\"")


if __name__ == "__main__":
    main()
