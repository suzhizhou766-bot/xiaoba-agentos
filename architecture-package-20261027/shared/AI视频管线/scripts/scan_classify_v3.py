#!/usr/bin/env python3
"""
音效深度指纹扫描 + 自动分类 v3 (线程并行版)
修复: ffprobe astats 键名（Overall → per-channel）
"""
import argparse, subprocess, json, csv, sys, time, os
from pathlib import Path
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

ASSISTANT_ROOT = Path("/Users/suzhizhou/Documents/私人助理")
LIBRARY = ASSISTANT_ROOT / "音效素材库"
FFPROBE = ASSISTANT_ROOT / ".runtime/bin/ffprobe"
OUT_DB = Path("/Users/suzhizhou/Documents/私人助理/AI视频管线/音效指纹库_v3.csv")
OUT_CLASSIFY = Path("/Users/suzhizhou/Documents/私人助理/AI视频管线/音效分类报告_v3.csv")
SUPPORTED_EXTENSIONS = {".wav", ".mp3", ".aiff", ".aif", ".flac", ".m4a", ".ogg"}
PROFILES = {
    "whoosh": {
        "tags": ["whoosh", "transition", "wind", "swish", "swoosh", "唰", "风声", "呼", "风"],
        "crest_range": (1.5, 8), "zcr_range": (0.01, 0.3), "flat_range": (0.001, 0.05),
        "dur_min": 0.1, "dur_max": 8,
        "cat_boost": {"转场": 3, "魔法": 2, "运动": 1.5, "科幻": 1.5, "紧张": 1},
    },
    "impact": {
        "tags": ["impact", "hit", "boom", "explos", "撞击", "敲", "咚", "爆炸", "冲击", "武器", "穿刺", "破碎", "泼溅"],
        "crest_range": (6, 30), "zcr_range": (0.0, 0.2), "flat_range": (0.0, 0.01),
        "dur_min": 0.02, "dur_max": 5,
        "cat_boost": {"机械": 3, "打斗": 2, "武器": 3, "科幻": 1.5, "金属": 3},
    },
    "ambient": {
        "tags": ["ambient", "atmos", "drone", "pad", "tension", "氛围", "空间", "紧张", "环境", "drones", "sustain", "气氛"],
        "crest_range": (1, 6), "zcr_range": (0.0, 0.15), "flat_range": (0.0, 0.03),
        "dur_min": 1, "dur_max": 999,
        "cat_boost": {"环境音": 3, "紧张": 3, "悬疑": 2, "科幻": 1.5, "Tension": 3},
    },
    "metal": {
        "tags": ["metal", "mechanic", "gear", "machine", "steam", "金属", "齿轮", "机械", "蒸汽", "朋克"],
        "crest_range": (3, 20), "zcr_range": (0.01, 0.3), "flat_range": (0.0, 0.02),
        "dur_min": 0.05, "dur_max": 10,
        "cat_boost": {"机械": 3, "蒸汽": 3, "金属": 3, "科幻": 1.5},
    },
    "click": {
        "tags": ["click", "beep", "bleep", "tone", "ui", "button", "confirm", "type", "按键", "叮", "提示", "gesture", "calculat", "micro", "solo", "data"],
        "crest_range": (4, 25), "zcr_range": (0.0, 0.2), "flat_range": (0.0, 0.01),
        "dur_min": 0.01, "dur_max": 3,
        "cat_boost": {"手机": 3, "UI": 5, "Solo": 5, "数字": 4, "界面": 4},
    },
    "magic": {
        "tags": ["magic", "sparkle", "fantasy", "transform", "魔法", "变身", "出现", "能量", "电力", "电源", "冲击波", "赛博", "科幻", "未来"],
        "crest_range": (2, 15), "zcr_range": (0.005, 0.3), "flat_range": (0.0, 0.04),
        "dur_min": 0.1, "dur_max": 8,
        "cat_boost": {"魔法": 5, "科幻": 2, "能量": 4, "赛博": 3, "电力": 3, "未来": 3},
    },
    "animal_life": {
        "tags": ["animal", "bird", "insect", "footstep", "walk", "动物", "鸟", "虫", "脚步", "走路", "开门", "关门", "生活", "美食", "交通", "运动"],
        "crest_range": (1.5, 15), "zcr_range": (0.0, 0.25), "flat_range": (0.0, 0.03),
        "dur_min": 0.1, "dur_max": 30,
        "cat_boost": {"动物": 5, "生活": 3, "美食": 2, "交通": 3, "运动": 3, "手机": 2},
    },
    "music": {
        "tags": ["music", "bgm", "instrument", "melody", "乐器", "音乐", "song"],
        "crest_range": (1, 8), "zcr_range": (0.0, 0.2), "flat_range": (0.0, 0.03),
        "dur_min": 1, "dur_max": 999,
        "cat_boost": {"BGM": 5, "乐器": 5, "音乐": 4},
    },
    "vocal": {
        "tags": ["voice", "vocal", "laugh", "人声", "笑声", "综艺", "游戏"],
        "crest_range": (2, 15), "zcr_range": (0.01, 0.25), "flat_range": (0.0, 0.02),
        "dur_min": 0.1, "dur_max": 60,
        "cat_boost": {"人声": 5, "笑声": 5, "综艺": 3, "游戏": 2},
    },
}


def deep_scan(fpath):
    try:
        r1 = subprocess.run(
            [str(FFPROBE), "-v", "quiet", "-print_format", "json", "-show_format", str(fpath)],
            capture_output=True, text=True, timeout=5
        )
        fmt = json.loads(r1.stdout).get("format", {})
        duration = float(fmt.get("duration", 0))
        if duration <= 0:
            return None

        interval = "%+3" if duration > 10 else None
        args = [
            str(FFPROBE), "-v", "quiet", "-print_format", "json",
            "-f", "lavfi",
            "-i", f"amovie={fpath},astats=metadata=1:reset=1",
            "-show_entries", "frame_tags",
        ]
        if interval:
            args.extend(["-read_intervals", interval])
        
        r2 = subprocess.run(args, capture_output=True, text=True, timeout=10)
        tags = {}
        frames = json.loads(r2.stdout).get("frames", [])
        if frames:
            tags = frames[0].get("tags", {})

        rms = float(tags.get("lavfi.astats.Overall.RMS_level", 
                             tags.get("lavfi.astats.1.RMS_level", -100)))
        peak = float(tags.get("lavfi.astats.Overall.Peak_level",
                              tags.get("lavfi.astats.1.Peak_level", -100)))
        crest = float(tags.get("lavfi.astats.1.Crest_factor", 0))
        flat = float(tags.get("lavfi.astats.1.Flat_factor", 0))
        zcr = float(tags.get("lavfi.astats.1.Zero_crossings_rate", 0))

        centroid = 0
        try:
            r3 = subprocess.run([
                str(FFPROBE), "-v", "quiet", "-print_format", "json",
                "-f", "lavfi",
                "-i", f"amovie={fpath},aspectralstats=measure=centroid",
                "-show_entries", "frame_tags",
                "-read_intervals", "%+#2"
            ], capture_output=True, text=True, timeout=8)
            sf = json.loads(r3.stdout).get("frames", [])
            if sf:
                centroid = float(sf[0].get("tags", {}).get("lavfi.aspectralstats.1.centroid", 0))
        except:
            centroid = zcr * 8000

        name = fpath.name.lower()
        path_str = str(fpath).lower()
        keywords = []
        all_kw = [
            "whoosh", "唰", "风声", "呼", "风", "swish", "swoosh",
            "撞击", "hit", "impact", "敲", "boom", "咚", "爆炸", "冲击", "武器", "穿刺", "破碎", "泼溅",
            "空间", "氛围", "atmos", "ambient", "drone", "pad", "tension", "紧张", "悬疑", "气氛",
            "金属", "metal", "齿轮", "机械", "machine", "蒸汽", "steam", "朋克",
            "魔法", "magic", "变身", "出现", "能量", "电力", "电源", "冲击波", "赛博", "未来", "科幻",
            "提示", "beep", "click", "按键", "叮", "confirm", "bleep", "tone", "gesture",
            "脚步", "走路", "开门", "关门", "动物", "鸟", "虫", "水", "rain", "雨",
            "音乐", "bgm", "乐器", "instrument", "melody",
            "转场", "transition", "过场", "过渡",
            "笑声", "综艺", "游戏", "美食", "手机", "交通", "运动",
            "人声", "vocal", "voice",
        ]
        for kw in all_kw:
            if kw in name or kw in path_str:
                keywords.append(kw)
        
        name_parts = fpath.stem[:60].lower().replace("_", " ").replace("-", " ").split()
        for part in name_parts:
            if len(part) >= 3 and part not in keywords and not part.isdigit():
                keywords.append(part)

        try:
            rel = fpath.relative_to(LIBRARY)
            cat = str(Path(*rel.parent.parts[:3])) if rel.parent.parts else "根目录"
        except:
            cat = "unknown"

        return {
            "name": fpath.name,
            "path": str(fpath),
            "category": cat,
            "duration": round(duration, 3),
            "rms_dB": round(rms, 2),
            "peak_dB": round(peak, 2),
            "crest": round(crest, 3),
            "flatness": round(flat, 4),
            "zcr": round(zcr, 4),
            "centroid_hz": round(centroid, 1),
            "keywords": "|".join(set(keywords)),
        }
    except:
        return None


def in_range(val, lo, hi):
    if val is None: return 0.5
    if lo <= val <= hi: return 1.0
    dist = min(abs(val - lo), abs(val - hi))
    span = max(hi - lo, 0.01)
    return max(0, 1.0 - dist / (span * 2))

def classify(entry):
    results = []
    for pname, profile in PROFILES.items():
        s = 0.0
        kw_str = (entry.get("keywords") or "").lower()
        for t in profile.get("tags", []):
            if t.lower() in kw_str:
                s += 30
        cf = float(entry.get("crest", 0))
        z = float(entry.get("zcr", 0))
        fl = float(entry.get("flatness", 0))
        dur = float(entry.get("duration", 0))
        if cf > 0:
            s += in_range(cf, *profile.get("crest_range", (0, 99))) * 20
        if z > 0:
            s += in_range(z, *profile.get("zcr_range", (0, 99))) * 15
        s += in_range(fl, *profile.get("flat_range", (0, 99))) * 10
        cat = entry.get("category", "")
        for kw, w in profile.get("cat_boost", {}).items():
            if kw.lower() in cat.lower():
                s += w * 8
        if dur < profile.get("dur_min", 0) or dur > profile.get("dur_max", 999):
            s *= 0.3
        results.append((pname, min(100, round(s, 1))))
    results.sort(key=lambda x: -x[1])
    return results[:3]


def process_file(fpath):
    info = deep_scan(fpath)
    if info is None:
        return None
    return attach_classification(info)


def attach_classification(info):
    top3 = classify(info)
    info["profile_1"] = top3[0][0]
    info["score_1"] = top3[0][1]
    info["profile_2"] = top3[1][0] if len(top3) > 1 else ""
    info["score_2"] = top3[1][1] if len(top3) > 1 else 0
    info["profile_3"] = top3[2][0] if len(top3) > 2 else ""
    info["score_3"] = top3[2][1] if len(top3) > 2 else 0
    return info


def load_existing():
    if not OUT_DB.is_file():
        return {}
    normalized = {}
    with OUT_DB.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if not r.get("path"):
                continue
            p = Path(r["path"])
            try:
                rel = p.relative_to(LIBRARY)
                r["category"] = str(Path(*rel.parent.parts[:3])) if rel.parent.parts else "根目录"
            except ValueError:
                continue
            normalized[r["path"]] = attach_classification(r)
    return normalized


def main():
    parser = argparse.ArgumentParser(description="增量构建全音效素材库的 SFX 指纹与分类索引")
    parser.add_argument("--full", action="store_true", help="忽略现有指纹并全量重扫")
    args = parser.parse_args()
    t0 = time.time()
    all_files = sorted(
        p for p in LIBRARY.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    total = len(all_files)
    print(f"📂 全音效素材库: {total} 个文件")

    existing = {} if args.full else load_existing()
    current_paths = {str(p) for p in all_files}
    reused = [r for path, r in existing.items() if path in current_paths]
    pending = [p for p in all_files if str(p) not in existing]
    print(f"♻️  复用现有指纹: {len(reused)} | 待扫描: {len(pending)}")

    workers = 24
    print(f"⚡ 线程 workers: {workers}")

    results = list(reused)
    skipped = 0
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process_file, f): f for f in pending}
        for fut in as_completed(futures):
            done += 1
            info = fut.result()
            if info is None:
                skipped += 1
            else:
                results.append(info)
            if done % 2000 == 0:
                elapsed = time.time() - t0
                pct = done / len(pending) * 100
                eta = (elapsed / done) * (len(pending) - done)
                print(f"   {done}/{len(pending)} ({pct:.0f}%) | {elapsed:.0f}s | ETA {eta:.0f}s", flush=True)

    elapsed = time.time() - t0
    print(f"\n✅ 完成！{elapsed:.0f}s")
    results.sort(key=lambda r: r["path"])
    print(f"   总指纹: {len(results)} | 新增跳过: {skipped}")

    base_fields = ["name","path","category","duration","rms_dB","peak_dB","crest","flatness","zcr","centroid_hz","keywords"]
    classify_fields = ["profile_1","score_1","profile_2","score_2","profile_3","score_3"]

    with open(OUT_DB, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=base_fields + classify_fields)
        writer.writeheader()
        writer.writerows(results)
    print(f"📊 指纹库: {OUT_DB}")

    with open(OUT_CLASSIFY, "w", newline="") as f:
        cfields = ["name","path","category","duration","profile_1","score_1","profile_2","score_2","profile_3","score_3"]
        writer = csv.DictWriter(f, fieldnames=cfields)
        writer.writeheader()
        for r in sorted(results, key=lambda x: (x["profile_1"], -float(x["score_1"]))):
            writer.writerow({k: r[k] for k in cfields})
    print(f"📋 分类报告: {OUT_CLASSIFY}")

    dist = Counter(r["profile_1"] for r in results)
    print("\n📈 一级分类分布:")
    for p, c in dist.most_common():
        pct = c / len(results) * 100
        desc = PROFILES.get(p, {}).get("desc", p)
        print(f"   {p:<14} {c:>6} ({pct:>5.1f}%)  {desc}")

    print("\n📊 目录 × 分类 交叉表 (前30):")
    cp = Counter((r["category"].split("/")[0], r["profile_1"]) for r in results)
    for (cat, prof), cnt in cp.most_common(30):
        print(f"   {cat:<25} → {prof:<14} {cnt:>5}")

if __name__ == "__main__":
    main()
