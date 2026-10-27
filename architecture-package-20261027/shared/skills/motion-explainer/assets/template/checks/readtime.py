"""checks/readtime.py — 文字够不够时间读完。

需要 film.mjs 里 export TIMELINE = [{ text, in, out, kind }]：
  text 屏幕上的字；in 进场开始时间；out 出场开始时间；kind: 'main'（主句）/ 'sub'（说明）/ 'word'（≤4 字的冲击词）
规则（中文）：
  main / sub：停留（out - in - 0.3 秒进场）≥ 字数 ÷ 5 + 0.8 秒
  word：≥ 0.5 秒
用法：python3 checks/readtime.py out/timeline.json
"""
import sys, json, re
items = json.load(open(sys.argv[1]))
bad = 0
for it in items:
    s = it['text']; n = len(re.sub(r'\s', '', s))
    hold = it['out'] - it['in'] - 0.3
    need = 0.5 if it.get('kind') == 'word' else n / 5 + 0.8
    ok = hold >= need - 1e-6
    bad += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {s[:24]:<24}  停留 {hold:.2f}s / 需要 {need:.2f}s")
print(f"\n{'FAIL' if bad else 'PASS'}  读字时间：{bad} 处不够")
