"""demo/score.py — 6 秒小样片的配乐：每个画面事件都在这里有对应的声音和拍点。"""
import sys; sys.path.insert(0, 'engine')
from musiclib import *

m = Mix(dur=6.0)
halftime_bar(m, 0.0, 'Am')                  # 0–2：半速，读字
groove_bar(m, 2.0, 'F')                     # 2–4：进律动
groove_bar(m, 4.0, 'C', section='B')        # 4–6：更满
m.sfx(snare_hit(), 0.5, 0.5); m.music(brass(220.0, 0.25), 0.5, 0.3); m.cue('title_slam', 0.5)   # 标题 slam
m.sfx(tick(2400), 1.0, 0.15)                                                               # 说明进场
m.sfx(whoosh(0.25), 1.38, 0.2)                                                             # 高亮扫过
for k in range(10): m.sfx(tick(2600 + 80 * k), 1.5 + (k / 10) ** 0.7, 0.12)                 # 数字滚动
m.sfx(ding(), 2.5, 0.34); m.cue('lock', 2.5)                                               # 数字锁定
m.sfx(whoosh(0.7), 3.0 - 0.35, 0.75)                                                       # 甩镜：中心 = 切点
for i, t in enumerate((3.5, 4.0, 4.5)): m.sfx(pop(), t, 0.4); m.music(brass(PENTA[3 + i], 0.3), t, 0.3)  # 列表逐拍，音高上行
impact(m, 5.0, 'Am')                                                                       # 收尾重音
m.render('music.wav', 'cues.json')
