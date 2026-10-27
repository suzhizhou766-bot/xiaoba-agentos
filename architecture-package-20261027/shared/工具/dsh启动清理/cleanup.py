#!/usr/bin/env python3
"""DSH 启动清理：清空洞项目目录 + 陈旧会话投影缓存，修复"点图标后窗口很久不出来"。

背景（2026-10-24 实测）：
  DSH 启动时 profile 初始化会对 session_projcache 的每条缓存走一次校验，而每次校验的
  findLog(id) 都要把 ~/.dsh/sessions 下所有项目目录扫一遍（readdir + 2 次 open 探测 +
  realpath）。项目目录攒到 2000+、陈旧缓存 800+ 条时约为 480 万次文件操作，启动 apply
  实测 188 秒；清理后 3.7 秒。详见同目录 README.md。

用法：
  python3 cleanup.py                # 只预览（dry-run），不移动任何文件
  python3 cleanup.py --apply        # 实际执行：移动到 ~/.dsh/_cleanup-<日期时间>/
  python3 cleanup.py --apply --keep-days 0   # 同上（默认即全部干净移走，保留所有有日志会话）

安全约定：
  - 只移动，不删除；移走的东西整体保留在 ~/.dsh/_cleanup-<时间戳>/ 里，确认无碍后可整目录清空。
  - 只处理"内部没有任何 *.jsonl / *.jsonl.zstd 会话日志"的项目目录；
  - 只处理 id 已不存在对应会话目录的 projcache 记录（存活会话的缓存一律保留）；
  - 不动 session_projcache_archive_manager_v2（第三方插件自己的缓存）；
  - 移动是 rename，DSH 运行中执行也安全（不会破坏已打开的会话）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

SESSION_SUFFIXES = (".jsonl", ".jsonl.zstd")


def dsh_home() -> str:
    return os.environ.get("DSH_HOME", os.path.expanduser("~/.dsh"))


def session_ids(sessions_root: str) -> set[str]:
    """存活会话 id：sessions/<项目目录>/session-<id>/ 的目录名部分。"""
    ids: set[str] = set()
    if not os.path.isdir(sessions_root):
        return ids
    for project in os.listdir(sessions_root):
        project_path = os.path.join(sessions_root, project)
        if not os.path.isdir(project_path):
            continue
        try:
            entries = os.listdir(project_path)
        except OSError:
            continue
        for entry in entries:
            if entry.startswith("session-") and os.path.isdir(os.path.join(project_path, entry)):
                ids.add(entry[len("session-"):])
    return ids


def has_session_log(project_path: str) -> bool:
    for _root, _dirs, files in os.walk(project_path):
        if any(name.endswith(SESSION_SUFFIXES) for name in files):
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="清理 DSH 空项目目录与陈旧会话投影缓存")
    parser.add_argument("--apply", action="store_true", help="实际移动文件（默认只预览）")
    parser.add_argument("--dsh-home", default=None, help="DSH_HOME，默认 ~/.dsh")
    args = parser.parse_args()

    home = args.dsh_home or dsh_home()
    sessions_root = os.path.join(home, "sessions")
    cache_root = os.path.join(home, "storages", "session_projcache", "sessions")
    if not os.path.isdir(sessions_root):
        print(f"[error] 找不到 {sessions_root}", file=sys.stderr)
        return 1

    live = session_ids(sessions_root)
    empty_projects: list[str] = []
    kept_projects = 0
    for name in sorted(os.listdir(sessions_root)):
        project_path = os.path.join(sessions_root, name)
        if not os.path.isdir(project_path):
            continue
        if has_session_log(project_path):
            kept_projects += 1
        else:
            empty_projects.append(name)

    stale_records: list[str] = []
    if os.path.isdir(cache_root):
        for name in sorted(os.listdir(cache_root)):
            if not (name.startswith("session-") and name.endswith(".json")):
                continue
            sid = name[len("session-"):-len(".json")]
            if sid not in live:
                stale_records.append(name)

    print(f"DSH_HOME      : {home}")
    print(f"存活会话      : {len(live)} 个（保留 {kept_projects} 个有日志的项目目录）")
    print(f"空项目目录    : {len(empty_projects)} 个")
    print(f"陈旧缓存记录  : {len(stale_records)} 条（共 {len(live) + len(stale_records)} 条缓存）")

    if not args.apply:
        print("\n[dry-run] 未移动任何文件；加 --apply 执行。")
        return 0

    if not empty_projects and not stale_records:
        print("\n无需清理。")
        return 0

    trash = os.path.join(home, f"_cleanup-{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(os.path.join(trash, "sessions-empty"), exist_ok=True)
    os.makedirs(os.path.join(trash, "projcache-stale"), exist_ok=True)

    moved_dirs = moved_records = 0
    for name in empty_projects:
        shutil.move(os.path.join(sessions_root, name), os.path.join(trash, "sessions-empty", name))
        moved_dirs += 1
    for name in stale_records:
        shutil.move(os.path.join(cache_root, name), os.path.join(trash, "projcache-stale", name))
        moved_records += 1

    with open(os.path.join(trash, "说明.md"), "w", encoding="utf-8") as fh:
        fh.write(
            "# DSH 启动清理记录\n\n"
            f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"- 移动空项目目录：{moved_dirs} 个（内部无任何会话日志）\n"
            f"- 移动陈旧缓存记录：{moved_records} 条（对应会话已不存在）\n"
            f"- 保留有日志项目目录：{kept_projects} 个 / 存活会话：{len(live)} 个\n"
            "- 全部为移动而非删除；确认 DSH 正常后可清空本目录。\n"
        )

    print(f"\n已移动：空项目目录 {moved_dirs} 个，陈旧缓存记录 {moved_records} 条")
    print(f"备份位置：{trash}")
    print("下次启动 DSH 观察窗口是否秒开；若有异常，把备份目录原样移回即可回滚。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
