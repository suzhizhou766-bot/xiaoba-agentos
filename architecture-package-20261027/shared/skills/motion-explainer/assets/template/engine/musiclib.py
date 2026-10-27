"""engine/musiclib.py — 用代码合成配乐和音效。

核心思想：配乐和画面读同一份时间表。score.py 先排好每一拍要发生什么，
写出 music.wav 和 cues.json（beats / kicks / 自定义事件），film.mjs 读 cues.json 让画面卡在同一拍上。

用法（score.py 放在项目根目录）：
    import sys; sys.path.insert(0, 'engine')
    from musiclib import *
    m = Mix(dur=30.0)
    groove_bar(m, 4.0, 'Am')                 # 一小节律动（2 秒）
    m.sfx(whoosh(0.7), cut - 0.35, 0.7)       # 转场 whoosh：中心对齐切点
    m.cue('title_slam', 2.0)                  # 记一个画面事件，film.mjs 里用 CUES.events.title_slam
    m.render('music.wav', 'cues.json')        # 侧链、混响、响度 -14 LUFS、写文件

节拍：120 BPM，1 拍 = 0.5 秒，1 小节 = 2 秒。所有重音都落在拍点上。
"""
import numpy as np, wave, json, subprocess, re

SR = 44100
BPM = 120
BEAT = 60 / BPM
BAR = BEAT * 4
rng = np.random.default_rng(7)

def T(s): return int(round(s * SR))

# ---------------------------------------------------------------- 基础
def env(n, a=0.002, d=0.3, curve=1.0):
    t = np.arange(n) / SR
    e = np.exp(-t / max(d, 1e-4)) ** curve
    na = max(1, int(a * SR)); e[:na] *= np.linspace(0, 1, na)
    return e

def fft_filter(x, lo=None, hi=None):
    X = np.fft.rfft(x); f = np.fft.rfftfreq(len(x), 1 / SR)
    m = np.ones_like(f)
    if lo: m *= 1 / (1 + (lo / np.maximum(f, 1)) ** 4)
    if hi: m *= 1 / (1 + (f / hi) ** 4)
    return np.fft.irfft(X * m, len(x))

def noise(sec): return rng.standard_normal(T(sec))

def saw(freq, sec, detune=(0.0,)):
    t = np.arange(T(sec)) / SR; out = np.zeros_like(t)
    for d in detune:
        f = freq * (2 ** (d / 1200)); out += 2 * ((t * f + rng.random()) % 1) - 1
    return out / len(detune)

def sine(freq, sec):
    return np.sin(2 * np.pi * freq * np.arange(T(sec)) / SR)

# ---------------------------------------------------------------- 鼓
def kick(punch=1.0):
    n = T(0.45); t = np.arange(n) / SR
    f = 46 + 120 * np.exp(-t / 0.035)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, 0.001, 0.32)
    click = fft_filter(noise(0.45), 1500, 8000) * env(n, 0.0005, 0.006) * 0.5
    return np.tanh((body + click) * 1.6 * punch)

def clap():
    n = T(0.35); x = fft_filter(noise(0.35), 900, 7000)
    e = np.zeros(n); t = np.arange(n) / SR
    for k, off in enumerate([0, 0.011, 0.022]):
        e += np.where(t >= off, np.exp(-(t - off) / (0.012 if k < 2 else 0.11)), 0)
    return x * e * 0.55 + sine(190, 0.35) * env(n, 0.001, 0.05) * 0.4

def hat(open_=False):
    sec = 0.28 if open_ else 0.06
    x = fft_filter(noise(sec), 7000, 16000)
    return x * env(len(x), 0.0005, 0.09 if open_ else 0.018)

def snare_hit():
    n = T(0.22)
    return sine(210, 0.22) * env(n, 0.001, 0.04) * 0.5 + fft_filter(noise(0.22), 1200, 9000) * env(n, 0.001, 0.07) * 0.6

def crash():
    x = fft_filter(noise(2.2), 3500, 15000); return x * env(len(x), 0.002, 0.7) * 0.5

def boom():
    n = T(1.6); t = np.arange(n) / SR
    f = 38 + 60 * np.exp(-t / 0.15)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, 0.002, 0.6) * 1.1

# ---------------------------------------------------------------- 过渡音效
def riser(sec, top=9000):
    """上升噪声 + 扫频，结束点 = 下一个重音。"""
    n = T(sec); t = np.arange(n) / SR; x = noise(sec); out = np.zeros(n); blk = 2048
    for s in range(0, n, blk):
        k = min(n, s + blk); p = s / n
        out[s:k] = fft_filter(x[s:k], 200 + p * top * 0.6, 1500 + p * top)[: k - s]
    tone = np.sin(2 * np.pi * np.cumsum(220 * 2 ** (3 * (t / sec) ** 2)) / SR) * 0.15
    return (out * 0.35 + tone) * (t / sec) ** 2.2

def whoosh(sec=0.7):
    """甩镜音效：放在 切点 - sec/2，最响的地方正好是切点。"""
    n = T(sec); t = np.arange(n) / SR; x = noise(sec); out = np.zeros(n); blk = 1024
    for s in range(0, n, blk):
        k = min(n, s + blk); c = 400 + 4000 * np.sin(np.pi * s / n)
        out[s:k] = fft_filter(x[s:k], c * 0.5, c * 2)[: k - s]
    return out * np.sin(np.pi * t / sec) ** 2 * 0.6

# ---------------------------------------------------------------- 和声
CHORDS = {  # 四音排列（Hz）和贝斯根音
    'Am': ([220.0, 261.63, 329.63, 440.0], 110.0),
    'F':  ([174.61, 220.0, 261.63, 349.23], 87.31),
    'C':  ([196.0, 261.63, 329.63, 392.0], 130.81),
    'G':  ([196.0, 246.94, 293.66, 392.0], 98.0),
    'E':  ([207.65, 246.94, 329.63, 415.3], 82.41),
    'Dm': ([174.61, 220.0, 293.66, 349.23], 73.42),
}
PHRASE = ['Am', 'F', 'C', 'G']  # 四小节句子；第五小节可以接 'E' 做回转
PENTA = [220.0, 261.63, 293.66, 329.63, 392.0, 440.0, 523.25, 587.33, 659.25, 880.0]  # 依次上行，用在逐个出现的元素上

def pad(chord, sec, bright=2200):
    v, _ = CHORDS[chord]
    x = sum(saw(f, sec, (-9, 0, 8)) for f in v) / len(v)
    x = fft_filter(x, 120, bright); n = len(x); e = np.ones(n); a = T(0.05); r = T(0.15)
    e[:a] = np.linspace(0, 1, a); e[-r:] = np.linspace(1, 0, r)
    return x * e

def stab(chord, bright=4500, dec=0.16):
    v, _ = CHORDS[chord]
    x = sum(saw(f * 2, 0.4, (-7, 7)) for f in v) / len(v)
    return fft_filter(x, 300, bright) * env(T(0.4), 0.002, dec)

def bass(chord, sec):
    _, f = CHORDS[chord]
    x = fft_filter(saw(f, sec, (-4, 4)) * 0.7 + sine(f / 2, sec) * 0.6, 30, 900)
    n = len(x); e = np.ones(n); r = T(0.04); e[-r:] = np.linspace(1, 0, r); e[:T(0.005)] = np.linspace(0, 1, T(0.005))
    return x * e

def brass(f, dec=0.35):
    x = saw(f, 0.8, (-8, 0, 7)) * 0.6 + saw(f * 1.5, 0.8, (-6, 6)) * 0.25 + saw(f / 2, 0.8) * 0.4
    return fft_filter(x, 120, 3800) * env(T(0.8), 0.004, dec)

def pluck(freq, dec=0.12):
    x = fft_filter(saw(freq, 0.3, (-5, 5)) * 0.6 + sine(freq * 2, 0.3) * 0.3, 400, 6000)
    return x * env(len(x), 0.001, dec)

def bell(freq, dec=0.6):
    n = T(1.2)
    x = sine(freq, 1.2) + 0.5 * sine(freq * 2.76, 1.2) * env(n, 0.001, 0.2) + 0.3 * sine(freq * 5.4, 1.2) * env(n, 0.001, 0.08)
    return x * env(n, 0.001, dec) * 0.5

# ---------------------------------------------------------------- 小音效（每个可见动作都要有声音）
def tick(freq=3200):
    return sine(freq, 0.03) * env(T(0.03), 0.0005, 0.006)

def keyclick():
    x = fft_filter(noise(0.04), 2000, 9000)
    return x * env(len(x), 0.0003, 0.008) * 0.7 + sine(1800 + rng.random() * 600, 0.04) * env(T(0.04), 0.0003, 0.004) * 0.2

def shutter():
    a = fft_filter(noise(0.05), 1500, 9000) * env(T(0.05), 0.0003, 0.006)
    b = fft_filter(noise(0.05), 800, 5000) * env(T(0.05), 0.0003, 0.01)
    out = np.zeros(T(0.09)); out[:len(a)] += a; out[T(0.035):T(0.035) + len(b)] += b * 0.8
    return out

def pop():
    n = T(0.12); t = np.arange(n) / SR
    return np.sin(2 * np.pi * np.cumsum(500 + 900 * np.exp(-t / 0.02)) / SR) * env(n, 0.001, 0.04)

def ding():
    return bell(1318.5, 0.5) + bell(1975.5, 0.4) * 0.5

# ---------------------------------------------------------------- 混音台
class Mix:
    """两条总线：music（会被 kick 侧链压下去）和 sfx（不压）。"""
    def __init__(self, dur):
        self.dur = dur; self.N = T(dur)
        self.SL = np.zeros(self.N); self.SR_ = np.zeros(self.N)
        self.ML = np.zeros(self.N); self.MR = np.zeros(self.N)
        self.kicks = []; self.events = {}; self.mutes = []

    def _add(self, L, R, sig, t, gain, pan):
        i = T(t)
        if i >= self.N or i < 0: return
        sig = sig[: self.N - i]
        l = np.cos((pan + 1) * np.pi / 4); r = np.sin((pan + 1) * np.pi / 4)
        L[i:i + len(sig)] += sig * gain * l * 1.414; R[i:i + len(sig)] += sig * gain * r * 1.414

    def sfx(self, sig, t, gain=1.0, pan=0.0): self._add(self.SL, self.SR_, sig, t, gain, pan)
    def music(self, sig, t, gain=1.0, pan=0.0): self._add(self.ML, self.MR, sig, t, gain, pan)
    def kick(self, t, punch=1.0, gain=1.0):
        self.kicks.append(round(t, 4)); self.sfx(kick(punch), t, 0.95 * gain)
    def cue(self, name, t):
        self.events.setdefault(name, []).append(round(t, 4))
    def mute(self, a, b):
        """硬静音（制造「呼吸」：大转折前留 0.5 秒空白，之后第一个声音最重）。"""
        self.mutes.append((a, b))

    def render(self, path='music.wav', cues_path='cues.json', reverb=0.16, target_lufs=-14.0):
        N = self.N
        sc = np.ones(N)  # kick 侧链
        for kt in self.kicks:
            i = T(kt); n = min(N - i, T(0.4))
            if n <= 0: continue
            tt = np.arange(n) / SR
            sc[i:i + n] = np.minimum(sc[i:i + n], 1 - 0.75 * np.exp(-tt / 0.09))
        ML, MR = self.ML * sc, self.MR * sc
        L, R = self.SL, self.SR_
        for a, b in self.mutes:
            for arr in (L, R, ML, MR):
                i, j = T(a), T(b); arr[i:j] = 0; f = T(0.01)
                if i - f > 0: arr[i - f:i] *= np.linspace(1, 0, f)
        irn = T(1.8); ir = rng.standard_normal(irn) * np.exp(-np.arange(irn) / SR / 0.45)
        ir = fft_filter(ir, 200, 6000); ir /= np.sqrt(np.sum(ir ** 2))
        def conv(x):
            n = len(x) + irn - 1; nf = 1 << (n - 1).bit_length()
            return np.fft.irfft(np.fft.rfft(x, nf) * np.fft.rfft(ir, nf), nf)[: len(x)]
        outL = L + ML + reverb * conv(ML + 0.3 * L); outR = R + MR + reverb * conv(MR + 0.3 * R)
        for a, b in self.mutes:
            i, j = T(a), T(b); outL[i:j] = 0; outR[i:j] = 0
        fe = T(min(1.0, self.dur * 0.05)); outL[-fe:] *= np.linspace(1, 0, fe); outR[-fe:] *= np.linspace(1, 0, fe)
        pk = max(np.abs(outL).max(), np.abs(outR).max(), 1e-9)
        outL, outR = outL / pk, outR / pk
        gain = 1.0
        for _ in range(4):  # 调响度到目标 LUFS（tanh 软限幅，峰值约 -1.5 dB）
            self._write(path, outL, outR, gain)
            lufs = measure_lufs(path)
            if lufs is None or abs(lufs - target_lufs) < 0.4: break
            gain *= 10 ** ((target_lufs - lufs) / 20)
        beats = [round(k * BEAT, 4) for k in range(int(self.dur / BEAT) + 1)]
        json.dump({'bpm': BPM, 'beat': BEAT, 'bar': BAR, 'beats': beats, 'kicks': sorted(self.kicks), 'events': self.events},
                  open(cues_path, 'w'), ensure_ascii=False)
        print(f'wrote {path} ({self.dur}s) LUFS≈{lufs} kicks={len(self.kicks)} events={list(self.events)}')

    @staticmethod
    def _write(path, L, R, gain):
        drive = 1.2 * gain
        oL = np.tanh(L * drive) / np.tanh(max(drive, 1e-3)) * 0.84 if drive > 1 else L * drive * 0.84
        oR = np.tanh(R * drive) / np.tanh(max(drive, 1e-3)) * 0.84 if drive > 1 else R * drive * 0.84
        pcm = (np.clip(np.stack([oL, oR], 1), -1, 1) * 32767).astype(np.int16)
        with wave.open(path, 'wb') as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())

def measure_lufs(path):
    r = subprocess.run(['ffmpeg', '-hide_banner', '-nostats', '-i', path, '-af', 'ebur128', '-f', 'null', '-'], capture_output=True, text=True)
    m = re.findall(r'I:\s+(-?[\d.]+) LUFS', r.stderr)
    return float(m[-1]) if m else None

# ---------------------------------------------------------------- 编曲积木
def groove_bar(m, t, chord, section='A', stabs=True):
    """一小节（2 秒）四拍律动：kick 每拍、clap 在 2/4 拍、反拍 hat、pad + bass + stab。section 'B' 更满（加开镲和琶音）。"""
    m.music(pad(chord, BAR, 2600 if section == 'A' else 3600), t, 0.22)
    m.music(bass(chord, BAR), t, 0.42)
    for k in range(4):
        m.kick(t + k * BEAT)
        if k in (1, 3): m.sfx(clap(), t + k * BEAT, 0.62)
        m.sfx(hat(), t + k * BEAT + BEAT / 2, 0.22, 0.3)
        if section == 'B': m.sfx(hat(True), t + k * BEAT + BEAT / 2, 0.12, -0.3)
    for k in range(16):
        if k % 4: m.sfx(hat(), t + k * BEAT / 4, 0.06 + 0.03 * (k % 2), -0.4)
    if stabs:
        for s_ in (0, 0.75, 1.5): m.music(stab(chord), t + s_, 0.24, 0.2)
    if section == 'B':
        v, _ = CHORDS[chord]; arp = [v[0] * 2, v[1] * 2, v[2] * 2, v[3] * 2, v[2] * 2, v[1] * 2]
        for k in range(16): m.music(pluck(arp[k % len(arp)]), t + k * BEAT / 4, 0.13, 0.5 * np.sin(k))

def halftime_bar(m, t, chord):
    """半速小节：给读字的段落用（不那么满，留出注意力）。"""
    m.music(pad(chord, BAR, 2200), t, 0.26); m.music(bass(chord, BAR), t, 0.3)
    m.kick(t, 0.9, 0.85); m.sfx(snare_hit(), t + 1.0, 0.35); m.sfx(clap(), t + 1.0, 0.3)
    for k in range(8): m.sfx(hat(), t + k * BEAT / 2, 0.12, 0.3)

def build(m, t0, t1, riser_gain=0.9):
    """t0→t1 的蓄力：军鼓滚奏越来越密 + riser，t1 是下一个重音。"""
    sec = t1 - t0
    m.sfx(riser(sec, 9000), t0, riser_gain)
    for k in range(16):
        m.sfx(snare_hit(), t0 + sec * (1 - (1 - k / 16) ** 1.6), 0.2 + 0.45 * k / 16)

def impact(m, t, chord='Am', big=True):
    """重音：段落第一拍 / 关键结论。"""
    m.sfx(crash(), t, 0.6 if big else 0.4); m.sfx(boom(), t, 0.75 if big else 0.5)
    m.kick(t, 1.2 if big else 1.0)
    m.music(stab(chord, 6000, 0.4), t, 0.4)
