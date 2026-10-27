"""checks/vstats.py — 看不了整段视频？让机器先量。工具只负责测量，不负责审美；被标记的时刻要自己用 sheet.mjs 去看。

用法：python3 checks/vstats.py out/final.mp4 [--json 结果.json]

测三件事：
1. 静止段：任意 1 秒（2 拍）以上画面几乎不变 → WARN。规则：超过 2 拍的停留必须有视觉变化；例外是重音前设计好的「停顿」（全片最多 2 处、每处 ≤ 1.5 秒）。
2. 危险区内容：会被遮挡/裁切的区域里出现文字或细节 → WARN。
   - 竖版（抖音）：左右各 45px 手机全屏会被裁掉；右侧 x>920 点赞/评论栏；底部 y>1480 标题和按钮；顶部 y<220 顶栏
   - 横版：没有平台遮挡，只查四边各 5% 的边距
   画幅从视频本身读（宽 > 高 就按横版）。
3. 卡点率：画面的「突变」（切换、进场、闪）有多少落在声音起音点 ±2 帧内。
"""
import sys, json, subprocess, numpy as np

FPS = 30

def frames(path, w, h):
    p = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-vf', f'scale={w}:{h}:flags=area,format=gray', '-f', 'rawvideo', '-'],
                       capture_output=True, check=True)
    a = np.frombuffer(p.stdout, np.uint8)
    return a.reshape(-1, h, w).astype(np.int16)

def audio(path, sr=22050):
    p = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-ac', '1', '-ar', str(sr), '-f', 's16le', '-'], capture_output=True)
    return np.frombuffer(p.stdout, np.int16).astype(np.float32) / 32768, sr

def static_spans(v, lag=15, pix=25, frac_th=0.004):
    n = len(v); fr = np.ones(n)
    for f in range(lag, n):
        fr[f] = (np.abs(v[f] - v[f - lag]) > pix).mean()
    st = fr < frac_th
    spans, f = [], lag
    while f < n:
        if st[f]:
            g = f
            while g < n and st[g]: g += 1
            dur = (g - f + lag) / FPS  # 从 f-lag 到 g 都几乎不变
            if dur >= 1.0: spans.append((round((f - lag) / FPS, 2), round(g / FPS, 2), round(dur, 2)))
            f = g
        else: f += 1
    return spans

def size(path):
    p = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=p=0', path],
                       capture_output=True, text=True, check=True)
    w, h = (int(x) for x in p.stdout.strip().split(',')[:2])
    return w, h

def danger(vq, landscape=False):
    # vq: 1/4 分辨率（竖版 270×480，横版 480×270）。梯度大于阈值的像素占比 = 「细节/文字」密度
    gx = np.abs(np.diff(vq, axis=2))
    if landscape:
        mx, my = 24, 13  # 1/4 分辨率下的 5% 边距
        zones = {
            '左右边距 5%': (lambda g: np.concatenate([g[:, :, :mx], g[:, :, -mx:]], 2)),
            '上下边距 5%': (lambda g: np.concatenate([g[:, :my, :], g[:, -my:, :]], 1)),
        }
    else: zones = {
        '左右45px(手机裁切)': (lambda g: np.concatenate([g[:, :, :11], g[:, :, -11:]], 2)),
        '右侧点赞栏 x>920': (lambda g: g[:, 55:370, 230:]),
        '底部 y>1480': (lambda g: g[:, 370:, :230]),
        '顶部 y<220': (lambda g: g[:, :55, :230]),
    }
    out = {}
    for name, sl in zones.items():
        d = (sl(gx[:, :-1, :]) > 40).mean(axis=(1, 2))
        bad = d > 0.02
        out[name] = {'frames_pct': round(100 * bad.mean(), 1), 'worst_t': round(float(np.argmax(d)) / FPS, 2), 'worst_density': round(float(d.max()), 3)}
    return out

def empty_lower(vq, landscape=False):
    # 竖版：y 960–1480、x 90–920 这一块（1/4 分辨率）几乎没有细节 = 下半屏空着
    # 横版：y 540–1026、x 96–1824
    g = np.abs(np.diff(vq[:, 135:256, 24:456] if landscape else vq[:, 240:370, 22:230], axis=2))
    d = (g > 30).mean(axis=(1, 2))
    e = d < 0.004
    return round(e.mean() * len(vq) / FPS, 2)

def carry(v):
    """整屏替换次数 + 镜头长度差距。整屏替换 = 前后两帧几乎没有共同的东西（像幻灯片翻页）。"""
    n = len(v)
    d = np.array([0] + [np.abs(v[f] - v[f - 1]).mean() for f in range(1, n)])
    med = np.median(d) + 1e-6
    cuts = [f for f in range(2, n - 2) if d[f] > max(6 * med, 12) and d[f] >= d[f - 1] and d[f] >= d[f + 1]]
    c2 = []
    for f in cuts:
        if not c2 or f - c2[-1] > 6: c2.append(f)
    replaced = []
    for f in c2:
        a, b = v[max(0, f - 2)], v[min(n - 1, f + 2)]
        bg = np.median(a)
        fg = np.abs(a - bg) > 20
        if fg.mean() < 0.01: continue
        keep = (np.abs(b - a) < 15)[fg].mean()
        if keep < 0.1: replaced.append(round(f / FPS, 2))
    shots = np.diff([0] + c2 + [n]) / FPS
    ratio = float(round(shots.max() / max(shots.min(), 1 / FPS), 1)) if len(shots) > 1 else None
    return {'cuts': len(c2), 'replaced': replaced, 'shot_ratio': ratio}

def sync(v, path):
    # 画面突变：相邻帧差的局部峰值
    d = np.array([0] + [np.abs(v[f] - v[f - 1]).mean() for f in range(1, len(v))])
    med = np.median(d) + 1e-6
    ev = [f for f in range(2, len(d) - 1) if d[f] > max(4 * med, 3.0) and d[f] >= d[f - 1] and d[f] >= d[f + 1]]
    # 去掉连续动画里的小峰：相邻 4 帧内只留最大的
    ev2 = []
    for f in ev:
        if ev2 and f - ev2[-1] < 4:
            if d[f] > d[ev2[-1]]: ev2[-1] = f
        else: ev2.append(f)
    x, sr = audio(path)
    if len(x) == 0: return {'visual_events': len(ev2), 'on_beat_pct': None, 'note': '没有音轨'}
    hop = sr // FPS; n = len(x) // hop
    S = np.abs(np.fft.rfft(x[: n * hop].reshape(n, hop) * np.hanning(hop), axis=1))
    flux = np.r_[0, np.maximum(0, np.diff(np.log1p(S), axis=0)).sum(1)]
    th = np.convolve(flux, np.ones(15) / 15, 'same') * 1.5 + np.median(flux) * 0.5
    on = [f for f in range(1, n - 1) if flux[f] > th[f] and flux[f] >= flux[f - 1] and flux[f] >= flux[f + 1]]
    on = np.array(on)
    hit = [f for f in ev2 if len(on) and np.min(np.abs(on - f)) <= 2]
    miss = [round(f / FPS, 2) for f in ev2 if f not in hit]
    return {'visual_events': len(ev2), 'audio_onsets': int(len(on)), 'on_beat_pct': round(100 * len(hit) / max(1, len(ev2)), 1),
            'off_beat_times': miss[:20]}

def main():
    path = sys.argv[1]
    w, h = size(path); land = w > h
    v = frames(path, 192, 108) if land else frames(path, 108, 192)
    vq = frames(path, 480, 270) if land else frames(path, 270, 480)
    sp = static_spans(v)
    res = {
        'duration_s': round(len(v) / FPS, 2),
        'static_spans': sp, 'static_total_s': round(sum(s[2] for s in sp), 2),
        'danger_zones': danger(vq, land),
        'sync': sync(v, path),
        'empty_lower_s': empty_lower(vq, land),
        'carry': carry(v),
    }
    if '--json' in sys.argv: json.dump(res, open(sys.argv[sys.argv.index('--json') + 1], 'w'), ensure_ascii=False, indent=1)
    print(f"时长 {res['duration_s']}s")
    bad = [x for x in sp if x[2] > 2.0]
    flag = 'FAIL' if (bad or len(sp) > 4) else ('WARN' if sp else 'PASS')
    print(flag + f"  静止段（≥1 秒几乎不变）：{len(sp)} 处，共 {res['static_total_s']}s " + (str(sp[:8]) if sp else '') + ('（如果是重音前设计好的停顿可以保留，否则要加变化）' if flag == 'WARN' else ''))
    m = np.array([np.abs(v[f] - v[f - 1]).mean() for f in range(1, len(v))])
    sec = [m[i*FPS:(i+1)*FPS].mean() for i in range(len(m)//FPS)]
    med = float(np.median(sec)) + 1e-6
    peaks = [(i, float(round(sec[i]/med, 1))) for i in range(len(sec)) if sec[i] > 5*med]
    print(f"INFO  能量曲线：峰值/中位数 = {float(round(max(sec)/med,1))} 倍；超过 5 倍的秒：{peaks[:10]}（目标：3–5 个高峰对准剧情节点）")
    for k, z in res['danger_zones'].items():
        flag = 'WARN' if z['frames_pct'] > 1 else 'PASS'
        print(f"{flag}  {k}：{z['frames_pct']}% 的帧有细节，最严重在 t={z['worst_t']}s")
    cr = res['carry']
    print(f"INFO  连续性（粗略，只作参考）：明显切换 {cr['cuts']} 次，其中整屏替换（前后几乎没有共同的东西）{len(cr['replaced'])} 次 {cr['replaced'][:8]}；最长/最短镜头 = {cr['shot_ratio']} 倍（目标 ≥ 4）")
    el = res['empty_lower_s']
    print(f"INFO  下半屏（{'y 540–1026' if land else 'y 960–1480'}）空着的总时长：{el}s（超过 3 秒要处理）")
    s = res['sync']
    print(f"INFO  卡点率：{s['on_beat_pct']}%（{s['visual_events']} 个画面突变）；没卡上的：{s.get('off_beat_times')}")

if __name__ == '__main__':
    main()
