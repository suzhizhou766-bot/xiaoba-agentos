"""读入 JSON 设置 → 合并 hooks.PreToolUse 与 permissions.deny → 原子写回。只打印新增的键，不打印文件内容。
用法：merge_hooks.py <settings.json> <adapter.json>   （adapter 里的“说明”键忽略）"""
import json, os, sys, tempfile

target, adapter = sys.argv[1], sys.argv[2]
add = json.load(open(adapter, encoding="utf-8"))
add.pop("说明", None)
cur = json.load(open(target, encoding="utf-8")) if os.path.exists(target) else {}

added = []
for event, groups in (add.get("hooks") or {}).items():
    have = cur.setdefault("hooks", {}).setdefault(event, [])
    for g in groups:
        if g not in have:
            have.append(g)
            added.append(f"hooks.{event}[matcher={g.get('matcher')}]")
for rule in (add.get("permissions") or {}).get("deny", []):
    deny = cur.setdefault("permissions", {}).setdefault("deny", [])
    if rule not in deny:
        deny.append(rule)
        added.append(f"permissions.deny += {rule}")

if not added:
    print("无新增")
    sys.exit(0)
mode = os.stat(target).st_mode & 0o777 if os.path.exists(target) else 0o600
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".merge-")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(cur, f, ensure_ascii=False, indent=2)
    f.write("\n")
os.chmod(tmp, mode)
os.replace(tmp, target)
print("新增：\n  " + "\n  ".join(added))
print("顶层键：", sorted(cur.keys()))
