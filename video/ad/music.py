"""The ad cut's sound: an original underscore (video/out/ad/music.wav) and its effects (video/out/ad/sfx.wav), both
48 kHz stereo 16-bit and exactly the film's length (timeline.json `total`), plus music_preview.mp3 (the two summed).

    uv run python video/ad/music.py

Music: 96 BPM, D major / B minor, one chord plan per scene (PLAN) and one arrangement per scene (STYLE). Layers:
low-passed detuned saw + triangle pads with slow attacks (a dark and a bright filtering crossfaded by `bright`), a
high sine air pad, a sine sub on the bass, a soft 8th-note pulse, felt-piano plucks (stretched partials, two strings
a cent apart), a soft kick and muted hats (ad-loop, ad-outcome; a heartbeat in ad-stakes), all through a convolution
reverb with a generated decaying-noise IR, gentle tanh saturation and a slow stereo width. ad-stakes ends in a riser;
the dry sound stops at ad-reveal's first frame so only the reverb tail rings under "Until now.", then D major swells
with a low boom where the narration starts "This is Vani Lab" (the page's cue if timeline.py has one, else the
speech onset after that caption's boundary). Walk scenes are a lower bed with a chord change at every boundary.

Effects: each scene page's sfx, video/out/ad/sfx/<id>.json = [[t, name, gain?], ...], t in seconds from the scene
start, plus a soft whoosh into every walk scene (peaking on the cut). Where t sits in each sound:
  whoosh   0.6 s band-passed noise pass-by, panned left to right; starts at t, loudest ~0.25 s later
  swoosh   0.35 s, softer and higher; starts at t
  hit      low thump + short noise; impact at t
  boom     deep sub drop, ~2.4 s; impact at t
  riser    2 s noise + pitch rise; ENDS at t
  tick     tiny high click, very quiet; at t
  click    UI click; at t
  swell    soft pad swell, 2.6 s; starts at t, fullest ~1.2 s later
  shimmer  high sparkle, quiet, ~1.4 s; starts at t
An unknown name plays a soft tick and prints a warning. The music already has the stakes riser and the reveal boom.
The report at the end (per scene RMS / peak, largest sample-to-sample jump, DC, length) fails the run on a problem.
"""
import bisect
import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from scipy.signal import butter, istft, oaconvolve, sosfilt, sosfiltfilt

VIDEO = Path(__file__).resolve().parent.parent
OUT = VIDEO / "out" / "ad"
FFMPEG = "/opt/homebrew/bin/ffmpeg"
SR, CTRL = 48000, 200  # audio rate; automation control rate
BEAT = 60 / 96
BAR = 4 * BEAT


def lp(fc, order=2):
    return butter(order, fc, "low", fs=SR, output="sos")


def hp(fc, order=2):
    return butter(order, fc, "high", fs=SR, output="sos")


def bp(a, b, order=2):
    return butter(order, [a, b], "band", fs=SR, output="sos")


# ------------------------------------------------------------------------------------------------ the harmony
# name: (root pitch class, bass pitch class, pad voicing in MIDI notes), voiced to move by step from chord to chord
CH = {
    "Dmaj9":    (2, 2, [52, 54, 57, 61]),
    "D/F#":     (2, 6, [50, 54, 57, 64]),
    "Bm9":      (11, 11, [50, 54, 57, 61]),
    "Bm(add9)": (11, 11, [50, 54, 59, 61]),
    "Gmaj9":    (7, 7, [50, 54, 57, 59]),
    "Gmaj7#11": (7, 7, [50, 54, 59, 61]),
    "Em9":      (4, 4, [50, 54, 55, 59]),
    "Asus4":    (9, 9, [50, 52, 57, 59]),
    "A/C#":     (9, 1, [52, 57, 59, 64]),
    "F#sus4":   (6, 6, [49, 54, 59, 61]),
    "F#":       (6, 6, [49, 54, 58, 61]),
}
# ("caps", [(chord, caption index)]): changes where the narration's chunks start; ("bars", [(chord, bars)]) on the
# scene's bar grid, the last to the scene's end; a list (walk scenes): 2 bars each on the run's bar grid, cycled
PLAN = {
    "ad-open":    ("caps", [("Bm9", 0), ("Gmaj7#11", 1), ("Em9", 3)]),
    "ad-stakes":  ("caps", [("Bm(add9)", 0), ("Gmaj7#11", 1), ("Em9", 2), ("F#sus4", 3), ("F#", 4)]),
    "ad-reveal":  ("caps", [("Dmaj9", 1)]),  # from the cue
    "ad-loop":    ("bars", [("Gmaj9", 2), ("D/F#", 2), ("Bm9", 2), ("Asus4", 2)]),
    "walk-home":  ["Dmaj9", "Gmaj9"],
    "walk-call":  ["Bm9", "Gmaj9", "D/F#", "Asus4"],
    "walk-audit": ["Gmaj9", "Em9", "D/F#", "Asus4"],
    "walk-rubric": ["Em9", "Asus4"],
    "walk-issues": ["Bm9", "Gmaj9", "D/F#", "Asus4"],
    "walk-fix":   ["Gmaj9", "D/F#", "Em9", "Asus4"],
    "walk-engine": ["D/F#", "Gmaj9", "Bm9", "Asus4"],
    "ad-split":   ("caps", [("Bm9", 0), ("Gmaj7#11", 1), ("F#sus4", 2)]),
    "walk-approve": ["Gmaj9", "D/F#", "Em9", "Asus4"],
    "walk-live":  ["Dmaj9", "Bm9", "Gmaj9", "Asus4"],
    "ad-outcome": ("bars", [("Dmaj9", 2), ("A/C#", 1), ("Bm9", 1), ("Gmaj9", 1), ("Asus4", 1)]),
    "ad-close":   ("caps", [("Gmaj9", 0), ("Asus4", 1), ("Dmaj9", 2)]),
}
WALK = ["Bm9", "Gmaj9", "D/F#", "Asus4"]

# Each scene's arrangement: layer levels (a pair ramps across the scene), pad brightness and stereo width, the pad's
# attack / release, drums, the pulse's register and the plucks (positions in beats within every `every` bars).
STYLE = {
    "ad-open":    dict(pad=(.3, .75), bright=(.05, .4), sub=(0, .7), pulse=(0, .28), kick=0, hat=0, pluck=.6, air=(0, .5),
                       width=(.7, 1.15), att=3.0, rel=3.0,
                       pl=dict(pos=(0, 2), every=1, p=.45, lo=66, hi=81, vel=.5, skip=3.2)),
    "ad-stakes":  dict(pad=.7, bright=(.2, .5), sub=.9, pulse=(.45, .9), kick=.45, hat=0, pluck=.5, air=.25, width=1.0,
                       att=1.2, rel=1.5, low=True, drums="heart",
                       pl=dict(pos=(0,), every=1, p=.7, lo=57, hi=71, vel=.5)),
    "ad-reveal":  dict(pad=1.0, bright=.7, sub=1.0, pulse=0, kick=0, hat=0, pluck=.8, air=.7, width=1.3, att=.8, rel=3.0),
    "ad-loop":    dict(pad=.7, bright=.55, sub=.9, pulse=.75, kick=.75, hat=.55, pluck=.6, air=.45, width=1.1, att=.8,
                       rel=1.6, drums="beat", kpat=((0, 1.0), (2, .85)), kodd=((3.5, .35),), p16=.3, hat_from=1,
                       pl=dict(pos=(0, 1.5, 3), every=1, p=.85, lo=64, hi=81, vel=.55)),
    "walk":       dict(pad=.42, bright=.18, sub=.45, pulse=.3, kick=0, hat=0, pluck=.35, air=.15, width=.95, att=1.6,
                       rel=2.0, pl=dict(pos=(0, 2.5), every=2, p=.45, lo=62, hi=76, vel=.4)),
    "ad-split":   dict(pad=.3, bright=.12, sub=.4, pulse=0, kick=0, hat=0, pluck=.45, air=.25, width=1.0, att=1.0, rel=1.5,
                       pl=dict(pos=(0, 1, 2, 3), every=1, p=1.0, lo=69, hi=83, vel=.32, tight=True, short=True, arp=True)),
    "ad-outcome": dict(pad=.85, bright=.65, sub=1.0, pulse=.8, kick=.8, hat=.6, pluck=.7, air=.5, width=1.2, att=.6, rel=1.5,
                       drums="beat", kpat=((0, 1.0), (1, .45), (2, .9), (3, .45)), kodd=((3.5, .3),), p16=.5, hat_from=0,
                       pl=dict(pos=(0, 1, 1.5, 2.5, 3), every=1, p=.65, lo=64, hi=83, vel=.55)),
    "ad-close":   dict(pad=(.85, .6), bright=(.6, .35), sub=.8, pulse=(.45, .2), kick=0, hat=0, pluck=.7, air=.5,
                       width=(1.2, .9), att=1.0, rel=3.0, stop=2, pl=dict(pos=(0, 2), every=1, p=.55, lo=64, hi=79, vel=.45)),
}
PARAMS = ("pad", "bright", "sub", "pulse", "kick", "hat", "pluck", "air", "width")
LEVEL = dict(pad=.11, air=.03, sub=.08, pulse=.1, pluck=.2, kick=.27, hat=.08)
SEND = dict(pad=.45, air=.9, sub=0, pulse=.22, pluck=.6, kick=.04, hat=.2)


def style(s):
    return STYLE.get(s["id"]) or STYLE["walk"]


def mtof(m):
    return 440.0 * 2 ** ((m - 69) / 12)


# ------------------------------------------------------------------------------------------------ oscillators
def phase(f, n, ph0=0.0):
    inc = np.broadcast_to(np.asarray(f, float) / SR, (n,))
    return (ph0 + np.cumsum(inc)) % 1.0, inc


def saw(ph, inc):
    """Band-limited saw (PolyBLEP)."""
    y = 2 * ph - 1
    m = ph < inc
    x = ph[m] / inc[m]
    y[m] -= 2 * x - x * x - 1
    m = ph > 1 - inc
    x = (ph[m] - 1) / inc[m]
    y[m] -= x * x + 2 * x + 1
    return y


def tri(ph):
    return 4 * np.abs(ph - 0.5) - 1


def ar(n, a, r):
    """n samples: a raised-cosine attack of a s, hold, a raised-cosine release over the last r s (ending at 0)."""
    e = np.ones(n)
    na = min(n, int(a * SR))
    nr = min(n - na, int(r * SR))
    if na:
        e[:na] = 0.5 - 0.5 * np.cos(np.pi * np.arange(na) / na)
    if nr:
        e[n - nr:] *= 0.5 + 0.5 * np.cos(np.pi * np.arange(1, nr + 1) / nr)
    return e


def fade_end(y, s=0.03):
    k = min(y.shape[-1], int(s * SR))
    y[..., -k:] *= np.linspace(1, 0, k)
    return y


def pan(x, p):
    a = (p + 1) * np.pi / 4
    return np.vstack([x * np.cos(a), x * np.sin(a)])


def norm(y):
    return y / (np.abs(y).max() + 1e-12)


SOS = dict(dark=lp(600), bright=lp(3800), pdark=lp(650), plow=lp(380), pbright=lp(2000), felt=lp(900),
           click=lp(2500), hat=bp(5000, 9000), lp160=lp(160), lp1500=lp(1500), lp1800=lp(1800), lp7k=lp(7000),
           lp8k=lp(8000), snap=bp(900, 3500), hp28=hp(28), lp9k=lp(9000))


# ------------------------------------------------------------------------------------------------ instruments
def pad_note(m, dur, att, rel, rng):
    n = int((dur + rel) * SR)
    t = np.arange(n) / SR
    f = mtof(m) * (1 + 0.0011 * np.sin(2 * np.pi * (0.11 + 0.08 * rng.random()) * t + 6.283 * rng.random()))
    out = np.zeros((2, n))
    for cents, p in ((-9, -0.7), (0, 0.0), (8, 0.7)):
        ph, inc = phase(f * 2 ** (cents / 1200), n, rng.random())
        out += pan(0.55 * saw(ph, inc) + 0.6 * tri(ph), p)
    return out * ar(n, att, rel) / 3


def air_note(m, dur, att, rel, rng):
    n = int((dur + rel) * SR)
    t = np.arange(n) / SR
    x = np.sin(2 * np.pi * mtof(m) * t + 6.283 * rng.random())
    x *= 0.75 + 0.25 * np.sin(2 * np.pi * 0.23 * t + 6.283 * rng.random())
    return pan(x * ar(n, att, rel), rng.uniform(-0.5, 0.5))


def sub_note(m, dur, att, rel):
    n = int((dur + rel) * SR)
    t = np.arange(n) / SR
    f = mtof(m)
    return pan((np.sin(2 * np.pi * f * t) + 0.25 * np.sin(4 * np.pi * f * t)) * ar(n, att, rel), 0)  # a 2nd harmonic for small speakers


def pulse_note(m, vel, low, rng):
    n = int(0.45 * SR)
    t = np.arange(n) / SR
    f = mtof(m)
    v = sum(saw(*phase(f * 2 ** (c / 1200), n, rng.random())) for c in (-6, 6)) / 2 + 0.5 * np.sin(np.pi * f * t)
    y = sosfilt(SOS["plow" if low else "pdark"], v) + 0.7 * sosfilt(SOS["pbright"], v) * np.exp(-t / 0.045)
    y *= (1 - np.exp(-t / 0.003)) * np.exp(-t / (0.2 if low else 0.13))
    return fade_end(y, 0.02) * vel


def pluck_note(m, vel, rng, decay=1.0):
    """A felt piano: stretched partials, each a pair of strings a cent apart, upper ones dying first; a felt thump."""
    f = mtof(m)
    tau = float(np.interp(m, [50, 86], [2.4, 0.8])) * decay
    n = int(min(7.0, 5 * tau) * SR)
    t = np.arange(n) / SR
    y = np.zeros(n)
    for k in range(1, 8):
        fk = k * f * np.sqrt(1 + 0.0004 * k * k)
        if fk > 6500:
            break
        tk = tau / (1 + 0.9 * (k - 1))
        nk = min(n, int(7 * tk * SR))
        a = np.exp(-0.8 * (k - 1)) * (1.3 if k == 2 else 1.0)
        for d in (-0.6, 0.6):
            y[:nk] += 0.5 * a * np.sin(2 * np.pi * fk * 2 ** (d / 1200) * t[:nk] + 6.283 * rng.random()) * np.exp(-t[:nk] / tk)
    if 2.76 * f < 6500:  # a faint inharmonic shimmer
        y += 0.06 * np.sin(2 * np.pi * 2.76 * f * t) * np.exp(-t / (0.25 * tau))
    y += 0.1 * norm(sosfilt(SOS["felt"], rng.standard_normal(n))) * np.exp(-t / 0.01)
    y *= 1 - np.exp(-t / 0.0035)
    return fade_end(norm(y), 0.04) * vel


def kick_note(vel, rng):
    n = int(0.6 * SR)
    t = np.arange(n) / SR
    y = np.sin(2 * np.pi * np.cumsum(43 + 78 * np.exp(-t / 0.03)) / SR) * np.exp(-t / 0.25)
    y += 0.15 * norm(sosfilt(SOS["click"], rng.standard_normal(n))) * np.exp(-t / 0.004)
    y *= 1 - np.exp(-t / 0.0012)
    return fade_end(np.tanh(1.5 * y) / np.tanh(1.5), 0.05) * vel


def hat_note(vel, rng):
    n = int(0.1 * SR)
    t = np.arange(n) / SR
    y = sosfilt(SOS["hat"], rng.standard_normal(n)) * np.exp(-t / 0.02) * (1 - np.exp(-t / 0.0006))
    return fade_end(norm(y), 0.01) * vel


# ------------------------------------------------------------------------------------------------ effects
def noise_band(n, fc, bw, rng):
    """n samples of noise whose spectrum is a log-Gaussian band (bw octaves) around fc (Hz, an array over the sound),
    built in the STFT domain."""
    nper, hop = 1024, 256
    frames = n // hop + 6
    fcs = np.interp(np.linspace(0, 1, frames), np.linspace(0, 1, len(fc)), fc)
    fr = np.maximum(np.fft.rfftfreq(nper, 1 / SR), 10.0)
    mag = np.exp(-0.5 * (np.log2(fr[:, None] / fcs[None, :]) / bw) ** 2)
    _, x = istft(mag * np.exp(2j * np.pi * rng.random(mag.shape)), fs=SR, nperseg=nper, noverlap=nper - hop)
    x = x[2 * hop: 2 * hop + n]
    return np.pad(x, (0, n - len(x))) / (np.std(x) + 1e-12)


def whoosh(rng, dur=0.6, f0=280.0, peak=6.0, bw=0.8):
    n = int(dur * SR)
    shape = np.sin(np.pi * np.linspace(0, 1, n) ** 0.75) ** 2
    fc = f0 * peak ** shape[::64]
    a = np.linspace(-0.6, 0.6, n) * np.pi / 4 + np.pi / 4
    y = np.vstack([noise_band(n, fc, bw, rng) * np.cos(a), noise_band(n, fc, bw, rng) * np.sin(a)]) * shape
    return sosfilt(SOS["lp7k"], y, axis=1)


def swoosh(rng):
    return whoosh(rng, 0.35, 600.0, 3.5, 0.6)


def riser(rng, dur=2.0, f0=110.0):
    n = int(dur * SR)
    u = np.linspace(0, 1, n)
    noise = np.vstack([noise_band(n, 300 * 9 ** u[::64], 0.6, rng) for _ in range(2)])
    f = f0 * 4 ** u
    tone = sosfilt(SOS["lp1800"], saw(*phase(f, n)) + saw(*phase(f * 1.004, n, 0.37))) * 0.3
    trem = 1 - 0.25 * (0.5 + 0.5 * np.sin(2 * np.pi * np.cumsum(3 + 9 * u) / SR))
    y = (0.45 * noise + tone * u ** 0.6) * u ** 2.2 * trem
    return fade_end(sosfilt(SOS["lp7k"], y, axis=1), 0.012)


def boom(rng):
    n = int(2.4 * SR)
    t = np.arange(n) / SR
    y = np.sin(2 * np.pi * np.cumsum(30 + 38 * np.exp(-t / 0.35)) / SR) * np.exp(-t / 0.75)
    y += 0.35 * norm(sosfilt(SOS["lp160"], rng.standard_normal(n))) * np.exp(-t / 0.12)
    y *= 1 - np.exp(-t / 0.002)
    return pan(fade_end(np.tanh(1.8 * y) / np.tanh(1.8), 0.1), 0)


def hit(rng):
    n = int(0.55 * SR)
    t = np.arange(n) / SR
    y = np.sin(2 * np.pi * np.cumsum(48 + 60 * np.exp(-t / 0.025)) / SR) * np.exp(-t / 0.16)
    y += 0.25 * norm(sosfilt(SOS["snap"], rng.standard_normal(n))) * np.exp(-t / 0.018)
    y *= 1 - np.exp(-t / 0.001)
    return pan(fade_end(np.tanh(1.4 * y), 0.05), 0)


def tick(rng):
    t = np.arange(int(0.03 * SR)) / SR
    return pan(fade_end(np.sin(2 * np.pi * 3100 * t) * np.exp(-t / 0.004) * (1 - np.exp(-t / 0.0003)), 0.005), 0.2)


def click(rng):
    t = np.arange(int(0.06 * SR)) / SR
    y = (np.sin(2 * np.pi * 1500 * t) * np.exp(-t / 0.005) + 0.6 * np.sin(2 * np.pi * 2900 * t) * np.exp(-t / 0.003)
         + 0.8 * np.sin(2 * np.pi * 190 * t) * np.exp(-t / 0.012))
    return pan(fade_end(y * (1 - np.exp(-t / 0.0004)), 0.01), 0)


def swell(rng):
    n = int(2.6 * SR)
    y = sum(pad_note(m, 1.2, 1.2, 1.4, rng) for m in (50, 57, 64, 66))[:, :n]
    return sosfilt(SOS["lp1500"], y, axis=1) * ar(y.shape[1], 0.01, 0.05)


def shimmer(rng):
    n = int(1.4 * SR)
    t = np.arange(n) / SR
    y = np.zeros((2, n))
    for m in (81, 86, 88, 90, 93, 98, 86):
        k = int(rng.uniform(0, 0.5) * SR)
        tt, tau = t[: n - k], rng.uniform(0.3, 0.55)
        x = sum(np.sin(2 * np.pi * mtof(m) * 2 ** (d / 1200) * tt + 6.283 * rng.random()) for d in (-1.5, 1.5)) / 2
        y[:, k:] += pan(x * np.exp(-tt / tau) * (1 - np.exp(-tt / 0.003)), rng.uniform(-0.7, 0.7))
    y += 0.15 * np.vstack([noise_band(n, [5200.0], 0.4, rng) for _ in range(2)]) * np.exp(-t / 0.3) * (1 - np.exp(-t / 0.01))
    return fade_end(sosfilt(SOS["lp8k"], y, axis=1), 0.05)


SFX = {  # name: (synth, gain, anchor: 0 = it starts at t, 1 = it ends at t)
    "whoosh": (whoosh, 0.32, 0), "swoosh": (swoosh, 0.2, 0), "hit": (hit, 0.5, 0), "boom": (boom, 0.7, 0),
    "riser": (riser, 0.35, 1), "tick": (tick, 0.05, 0), "click": (click, 0.14, 0), "swell": (swell, 0.2, 0),
    "shimmer": (shimmer, 0.12, 0),
}


def make_ir(rt_lo, rt_hi, length, seed, pre=0.02):
    """A stereo room: decaying noise, its highs dying faster than its lows, a soft onset, after a pre-delay."""
    rng = np.random.default_rng(seed)
    n = int(length * SR)
    t = np.arange(n) / SR
    ir = np.zeros((2, int(pre * SR) + n))
    for c in range(2):
        w = rng.standard_normal(n)
        lo = sosfilt(SOS["lp1500"], w)
        x = (lo * np.exp(-6.9 * t / rt_lo) + 0.5 * (w - lo) * np.exp(-6.9 * t / rt_hi)) * (1 - np.exp(-t / 0.015))
        ir[c, int(pre * SR):] = sosfilt(SOS["lp7k"], x)
    return ir / np.sqrt((ir ** 2).sum(axis=1, keepdims=True))


def reverb(x, ir):
    return np.stack([oaconvolve(x[c], ir[c])[: x.shape[1]] for c in range(2)])


# ------------------------------------------------------------------------------------------------ timing
def onset(s, phrase):
    """Seconds from the scene start when the narration starts saying phrase: the page's cue if timeline.py computed
    one, else the first speech in the wav after the boundary of the caption holding phrase, else that boundary."""
    if phrase in s["cues"]:
        return s["cues"][phrase]
    cap = next((c for c in s["caps"] if phrase in c[2]), None)
    if cap is None:
        return s["lead"]
    try:
        with wave.open(str(VIDEO / s["wav"])) as w:
            sr, ch = w.getframerate(), w.getnchannels()
            x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(float).reshape(-1, ch).mean(axis=1)
    except (FileNotFoundError, KeyError):
        return cap[0]
    hop = sr // 100
    db = 20 * np.log10(np.sqrt(np.mean(x[: len(x) // hop * hop].reshape(-1, hop) ** 2, axis=1)) / 32768 + 1e-9)
    k0 = max(0, int((cap[0] - s["lead"]) * 100))
    loud = np.nonzero(db[k0:] > -40)[0]
    return s["lead"] + (k0 + loud[0]) / 100 if len(loud) else cap[0]


def chord_slots(scenes, anchors, cue, total):
    """[t0, t1, chord, scene] for the whole film."""
    slots = []
    for s in scenes:
        a, b = s["start"], s["end"]
        plan = PLAN.get(s["id"], WALK if s["kind"] == "walk" else ["Bm9"])
        if isinstance(plan, list):
            g, ts = anchors[s["id"]], [a]
            for k in range(1, 99):
                t = g + round((a + 2 * k * BAR - g) / BAR) * BAR  # every 2 bars, on the run's bar lines
                if t >= b - BAR:
                    break
                if t > a + 1.5 * BAR:
                    ts.append(t)
            names = plan if not slots or slots[-1][2] != plan[0] else plan[1:] + plan[:1]  # a change at the cut
            slots += [[t, None, names[i % len(names)], s] for i, t in enumerate(ts)]
            continue
        mode, items = plan
        acc = 0
        for name, x in items:
            if mode == "caps":
                if x >= len(s["caps"]):
                    continue
                t = cue if s["id"] == "ad-reveal" else a + (0 if x == 0 else s["caps"][x][0])
            else:
                t, acc = a + acc * BAR, acc + x
            if t < b - 0.5:
                slots.append([t, None, name, s])
    for i, sl in enumerate(slots):
        sl[1] = slots[i + 1][0] if i + 1 < len(slots) else total
    return slots


def automation(scenes, total):
    """Each PARAMS curve at CTRL Hz: the scene's value (or ramp), each change crossfaded over the 0.3 s before the cut."""
    nc = int(total * CTRL) + 2
    ctrl = {p: np.zeros(nc) for p in PARAMS}
    for i, s in enumerate(scenes):
        a = round(s["start"] * CTRL)
        b = nc if i == len(scenes) - 1 else round(s["end"] * CTRL)
        for p in PARAMS:
            v = style(s)[p]
            ctrl[p][a:b] = np.linspace(v[0], v[1], b - a) if isinstance(v, tuple) else v
    w = int(0.3 * CTRL)
    k = np.hanning(w + 2)[1:-1]
    k /= k.sum()
    for p in PARAMS:
        x = np.convolve(np.pad(ctrl[p], w, mode="edge"), k, mode="same")[w:-w]
        ctrl[p] = np.concatenate([x[w // 2:], np.full(w // 2, x[-1])])
    return np.arange(nc) / CTRL, ctrl


# ------------------------------------------------------------------------------------------------ the score
def render_music(scenes, total, n_out, cut, cue):
    rng = np.random.default_rng(96)
    tc, ctrl = automation(scenes, total)
    at = lambda p, t: float(np.interp(t, tc, ctrl[p]))  # noqa: E731
    dry, padb, rev = (np.zeros((2, n_out), np.float32) for _ in range(3))
    cut_i = round(cut * SR)

    def place(layer, t0, sig, send=None, bus=None):
        """Mix sig in at t0 s, scaled by the layer's automation; a sound started before the reveal stops at its cut."""
        i0 = round(t0 * SR)
        if i0 < 0:
            sig, i0 = sig[:, -i0:], 0
        n = min(sig.shape[1], n_out - i0)
        if n <= 0:
            return
        sig = sig[:, :n] * (1.0 if layer == "fx" else np.interp((i0 + np.arange(n)) / SR, tc, ctrl[layer]))
        if i0 < cut_i < i0 + n:
            k, f = cut_i - i0, int(0.03 * SR)
            sig[:, max(0, k - f):k] *= np.linspace(1, 0, k - max(0, k - f))
            sig[:, k:] = 0
        (padb if bus == "pad" else dry)[:, i0:i0 + n] += sig
        g = SEND.get(layer, 0) if send is None else send
        if g:
            rev[:, i0:i0 + n] += g * sig

    anchors, prev = {}, None
    for s in scenes:
        run = lambda x: x is not None and (x["kind"] == "walk" or x["id"] == "ad-split")  # noqa: E731
        anchors[s["id"]] = anchors[prev["id"]] if run(s) and run(prev) else s["start"]
        prev = s
    anchors["ad-reveal"] = cue
    slots = chord_slots(scenes, anchors, cue, total)
    starts = [sl[0] for sl in slots]
    chord_at = lambda t: CH[slots[max(0, bisect.bisect_right(starts, t + 1e-6) - 1)][2]]  # noqa: E731

    # pads, air and sub: one note per chord tone per slot, overlapping into the next slot by the release
    for i, (t0, t1, name, s) in enumerate(slots):
        st, (root, bass, voicing) = style(s), CH[name]
        dur, rel = t1 - t0, st["rel"]
        if i == len(slots) - 1:  # the last chord rings out by the end
            dur, rel = max(1.0, dur - 3.2), 3.6
        for j, m in enumerate(voicing):
            d = rng.uniform(0, 0.06)
            place("pad", t0 + d, LEVEL["pad"] * (1.1 if j == 0 else 1.0) * pad_note(m, dur - d, st["att"], rel, rng), bus="pad")
        for m in voicing[-2:]:
            place("air", t0, LEVEL["air"] * air_note(m + 24, dur, st["att"] * 1.3, rel * 1.2, rng))
        place("sub", t0, LEVEL["sub"] * sub_note(30 + (bass - 6) % 12, dur, min(st["att"], 1.0), 1.2))

    for s in scenes:
        st, a, b, g = style(s), s["start"], s["end"], anchors[s["id"]]
        stop = a + s["caps"][st["stop"]][0] if "stop" in st and st["stop"] < len(s["caps"]) else b
        jit = (lambda sd: 0.0) if st.get("pl", {}).get("tight") else (lambda sd: rng.normal(0, sd))  # noqa: E731
        # the pulse: 8ths on the grid, root / octave / fifth / octave, accented on the beat
        step = BEAT / 2
        for i in range(int(np.ceil((a - g) / step - 1e-6)), int((b - g) / step) + 1):
            t = g + i * step
            if t >= min(b - 0.05, stop) or at("pulse", t) < 0.01:
                continue
            base = 43 + (chord_at(t)[0] - 7) % 12
            m = base - 12 if st.get("low") else base + (0, 12, 7, 12)[i % 4]
            vel = (1.0 if i % 2 == 0 else 0.7) * (1.08 if i % 8 == 0 else 1.0) * (1 + 0.06 * rng.standard_normal())
            place("pulse", t + jit(0.004), pan(LEVEL["pulse"] * pulse_note(m, vel, st.get("low"), rng), 0.15 if i % 2 else -0.15))
        # drums
        if st.get("drums"):
            for k in range(int(np.ceil((b - a) / BAR))):
                hits = [(0, 0.9), (0.5, 0.55)] if st["drums"] == "heart" else list(st["kpat"]) + (list(st["kodd"]) if k % 2 else [])
                for pos, v in hits:
                    t = a + (4 * k + pos) * BEAT
                    if t < b - 0.1:
                        place("kick", t + jit(0.003), pan(LEVEL["kick"] * kick_note(v * (1 + 0.05 * rng.standard_normal()), rng), 0))
                if st["drums"] == "beat" and k >= st["hat_from"]:
                    for j in range(16):
                        pos = j / 4
                        v = 0.75 if j % 4 == 2 else 0.25 if j % 2 else 0.0
                        if not v or (j % 2 and rng.random() > st["p16"]) or (t := a + (4 * k + pos) * BEAT) >= b - 0.05:
                            continue
                        place("hat", t + jit(0.006), pan(LEVEL["hat"] * hat_note(v * (0.8 + 0.4 * rng.random()), rng), 0.25))
        # plucks: chord tones, mostly by step from the last note, on the scene's beat positions
        pl = st.get("pl")
        if not pl:
            continue
        prev_m, idx = (pl["lo"] + pl["hi"]) // 2, 0
        for k in range(int(np.floor((a - g) / BAR)), int((b - g) / BAR) + 1):
            if k % pl["every"]:
                continue
            for pos in pl["pos"]:
                t = g + (4 * k + pos) * BEAT
                if t < a + pl.get("skip", 0) or t > min(b - 0.4, stop) or rng.random() > pl["p"]:
                    continue
                root, bass, voicing = chord_at(t)
                tones = {x % 12 for x in voicing} | {root, bass}
                cands = [x for x in range(pl["lo"], pl["hi"] + 1) if x % 12 in tones]
                if pl.get("arp"):
                    m = cands[idx % len(cands)]
                    idx += 1
                else:
                    mid = (pl["lo"] + pl["hi"]) / 2
                    cands = sorted((x for x in cands if x != prev_m), key=lambda x: abs(x - prev_m) + 0.15 * abs(x - mid) + 2.5 * rng.random())
                    m = cands[0] if rng.random() < 0.7 else cands[min(1, len(cands) - 1)]
                prev_m = m
                vel = pl["vel"] * (0.85 + 0.3 * rng.random()) * (1.1 if pos == 0 else 1.0)
                p = (-0.45 if idx % 2 else 0.45) if pl.get("arp") else float(np.clip((m - 70) / 18, -0.5, 0.5)) + rng.uniform(-0.15, 0.15)
                place("pluck", t + jit(0.008), pan(LEVEL["pluck"] * pluck_note(m, vel, rng, 0.35 if pl.get("short") else 1.0), p))

    # the moments: the opening dyad, the stakes riser into the cut, the reveal's boom and rolled chord, the logo
    by = {s["id"]: s for s in scenes}
    if "ad-open" in by and by["ad-open"]["caps"]:
        t = onset(by["ad-open"], by["ad-open"]["caps"][0][2])
        for m, v in ((71, .5), (78, .4)):
            place("pluck", by["ad-open"]["start"] + t, pan(LEVEL["pluck"] * pluck_note(m, v, rng, 1.3), (m - 74) / 20))
    place("fx", cut - 3.0, 0.08 * riser(rng, 3.0, mtof(42)), send=0.8)
    place("fx", cue, 0.45 * boom(rng), send=0.3)
    for j, (m, v) in enumerate(zip((62, 69, 74, 76, 81), (.75, .65, .6, .55, .5))):
        place("pluck", cue + 0.04 + 0.11 * j, pan(LEVEL["pluck"] * pluck_note(m, v, rng, 1.5), (m - 72) / 20))
    if "ad-close" in by:
        t = by["ad-close"]["start"] + onset(by["ad-close"], "Vani Lab.")
        place("fx", t, 0.3 * boom(rng), send=0.3)
        for j, m in enumerate((50, 57, 62, 66, 69, 76)):
            place("pluck", t + 0.14 * j, pan(LEVEL["pluck"] * pluck_note(m, 0.7 - 0.04 * j, rng, 1.8), (m - 64) / 24))

    # the bus: pad brightness, reverb, width, saturation, master filters and fades
    # (a channel at a time, in float32 where it can be: the machine also films the frames)
    tt = np.arange(n_out, dtype=np.float32) / SR
    br = np.interp(tt, tc, ctrl["bright"]).astype(np.float32)
    ir = make_ir(3.4, 1.6, 4.5, 7)
    for c in range(2):
        p = (1 - br) * sosfiltfilt(SOS["dark"], padb[c]) + br * sosfiltfilt(SOS["bright"], padb[c])
        dry[c] += p
        rev[c] += SEND["pad"] * p
        del p
        dry[c] += 0.5 * oaconvolve(rev[c], ir[c])[:n_out]
    del padb, rev, br
    w = (np.interp(tt, tc, ctrl["width"]) * (1 + 0.1 * np.sin(2 * np.pi * tt / 17))).astype(np.float32)
    mid, side = (dry[0] + dry[1]) / 2, (dry[0] - dry[1]) / 2 * w
    dry[0], dry[1] = mid + side, mid - side
    del mid, side, w, tt
    pk = np.abs(dry).max()
    mix = np.empty((2, n_out))
    for c in range(2):
        mix[c] = sosfiltfilt(SOS["lp9k"], sosfiltfilt(SOS["hp28"], np.tanh(1.6 * 0.8 * dry[c] / pk) / 1.6))
    return master(mix, end_fade=1.5)


def master(x, end_fade, peak_db=-1.5):
    x[:, : int(0.01 * SR)] *= np.linspace(0, 1, int(0.01 * SR))
    k = int(end_fade * SR)
    x[:, -k:] *= 0.5 + 0.5 * np.cos(np.pi * np.arange(1, k + 1) / k)
    return x * (10 ** (peak_db / 20) / (np.abs(x).max() + 1e-12))


def render_sfx(scenes, n_out):
    events = []
    for s in scenes:
        f = OUT / "sfx" / f"{s['id']}.json"
        if f.exists():
            events += [(s["start"] + float(e[0]), str(e[1]), float(e[2]) if len(e) > 2 else 1.0, s["id"]) for e in json.loads(f.read_text())]
    events += [(s["start"] - 0.25, "whoosh", 0.6, "(auto)") for i, s in enumerate(scenes) if i and s["kind"] == "walk"]
    print(f"sfx: {len(events)} events ({sum(e[3] == '(auto)' for e in events)} automatic)")
    x = np.zeros((2, n_out))
    for k, (t, name, gain, sid) in enumerate(sorted(events)):
        if name not in SFX:
            print(f"  warning: {sid}: unknown sfx {name!r} at {t:.2f} s; a soft tick instead")
        synth, g, anchor = SFX.get(name, (tick, 0.03, 0))
        y = g * gain * norm(synth(np.random.default_rng(1000 + k)))
        i0 = round(t * SR) - (y.shape[1] if anchor else 0)
        a, b = max(0, i0), min(n_out, i0 + y.shape[1])
        if b > a:
            x[:, a:b] += y[:, a - i0: b - i0]
    x += 0.25 * reverb(x, make_ir(1.4, 0.7, 2.0, 11))
    x = sosfiltfilt(SOS["hp28"], x, axis=1)
    if not events or np.abs(x).max() < 1e-9:
        return x
    pk = np.abs(x).max()
    if pk > 10 ** (-1.5 / 20):
        print(f"  sfx peaked at {20 * np.log10(pk):.1f} dBFS; scaled down to -1.5 dBFS")
        x *= 10 ** (-1.5 / 20) / pk
    return x


# ------------------------------------------------------------------------------------------------ out
def pcm16(x):
    return np.round(np.clip(x, -1, 1) * 32767).astype("<i2").T.copy()


def write(path, x):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm16(x).tobytes())


def report(name, x, scenes, n_out):
    db = lambda v: 20 * np.log10(max(float(v), 1e-9))  # noqa: E731
    print(f"\n{name}: {x.shape[1]} samples = {x.shape[1] / SR:.3f} s (timeline {n_out} = {n_out / SR:.3f} s)")
    for s in scenes:
        seg = x[:, round(s["start"] * SR): round(s["end"] * SR)]
        print(f"  {s['id']:<13} {s['kind']:<6} rms {db(np.sqrt(np.mean(seg ** 2))):6.1f}  peak {db(np.abs(seg).max()):6.1f} dBFS")
    d = np.abs(np.diff(x, axis=1))
    c, k = np.unravel_index(np.argmax(d), d.shape)
    dc, peak = x.mean(axis=1), np.abs(x).max()
    print(f"  all: rms {db(np.sqrt(np.mean(x ** 2))):.1f}, peak {db(peak):.2f} dBFS; largest jump {d[c, k]:.4f} at "
          f"{k / SR:.3f} s ({'LR'[c]}); DC {dc[0]:+.1e} / {dc[1]:+.1e}; last 10 ms peak {db(np.abs(x[:, -480:]).max()):.0f} dBFS")
    bad = [m for m, ok in [("length", x.shape[1] == n_out), ("peak above -1 dBFS", peak <= 10 ** (-1 / 20)),
                           ("DC", np.abs(dc).max() < 1e-3), ("a click (jump > 0.25)", d[c, k] < 0.25)] if not ok]
    return [f"{name}: {m}" for m in bad]


def main():
    tl = json.loads((OUT / "timeline.json").read_text())
    scenes, total = tl["scenes"], tl["total"]
    for s in scenes:
        s["end"] = s["start"] + s["dur"]
    n_out = round(total * SR)
    reveal = next(s for s in scenes if s["id"] == "ad-reveal")
    cut, cue = reveal["start"], reveal["start"] + onset(reveal, "This is Vani Lab")
    print(f"film {total:.3f} s; reveal cut {cut:.3f} s, boom on 'This is Vani Lab' at {cue:.3f} s")
    music = render_music(scenes, total, n_out, cut, cue)
    sfx = render_sfx(scenes, n_out)
    bad = report("music.wav", music, scenes, n_out) + report("sfx.wav", sfx, scenes, n_out)
    write(OUT / "music.wav", music)
    write(OUT / "sfx.wav", sfx)
    both = music + sfx
    both *= min(1.0, 10 ** (-1 / 20) / (np.abs(both).max() + 1e-12))
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "s16le", "-ar", str(SR), "-ac", "2", "-i", "-",
                    "-c:a", "libmp3lame", "-b:a", "192k", str(OUT / "music_preview.mp3")], input=pcm16(both).tobytes(), check=True)
    print(f"\n-> {OUT / 'music.wav'}, {OUT / 'sfx.wav'}, {OUT / 'music_preview.mp3'}")
    if bad:
        sys.exit("PROBLEMS: " + "; ".join(bad))


if __name__ == "__main__":
    main()
