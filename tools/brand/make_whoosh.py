"""A short right-to-left whoosh for the swipe between games in a story-join.
Synthesised here (filtered noise with a rising-then-falling cutoff, panned
right to left), so it is original and free to use.
usage: python make_whoosh.py <out.wav>
Also writes <out>.json with the loudest moment (`peak_s`), which story-join
lines up with the middle of the swipe."""

import json
import sys
import wave
from pathlib import Path

import numpy as np

SR = 48000
DUR = 0.7
PEAK = 0.32  # seconds: loudest, brightest moment

out = Path(sys.argv[1])
out.parent.mkdir(parents=True, exist_ok=True)
rng = np.random.default_rng(7)
n = int(SR * DUR)
t = np.arange(n) / SR
noise = rng.standard_normal(n)

# Cutoff sweeps 300 Hz -> 5 kHz at the peak -> 600 Hz; a one-pole low-pass
# minus a slower one gives a moving band.
rise = np.clip(t / PEAK, 0, 1)
fall = np.clip((t - PEAK) / (DUR - PEAK), 0, 1)
cut = np.where(t < PEAK, 300 + 4700 * rise**2, 5000 - 4400 * fall**0.7)


def lowpass(x: np.ndarray, fc: np.ndarray) -> np.ndarray:
    a = 1 - np.exp(-2 * np.pi * fc / SR)
    y = np.empty_like(x)
    acc = 0.0
    for i in range(len(x)):
        acc += a[i] * (x[i] - acc)
        y[i] = acc
    return y


band = lowpass(noise, cut) - lowpass(noise, cut / 6)
env = np.where(t < PEAK, (t / PEAK) ** 2.2, np.exp(-(t - PEAK) / 0.12))
env *= np.clip((DUR - t) / 0.05, 0, 1)
sig = band * env
sig /= np.max(np.abs(sig)) + 1e-9
sig *= 0.5  # about -6 dBFS: under the game audio, not over the narration

pan = np.clip(t / DUR, 0, 1)  # starts right, ends left (the swipe direction)
left = sig * np.sqrt(pan)
right = sig * np.sqrt(1 - pan)
pcm = (np.stack([left, right], axis=1) * 32767).astype("<i2")
with wave.open(str(out), "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(pcm.tobytes())
out.with_suffix(".json").write_text(
    json.dumps({"peak_s": PEAK, "source": "make_whoosh.py (original, synthesised)"})
)
print("ok", out)
