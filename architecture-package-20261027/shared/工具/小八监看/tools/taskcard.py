#!/usr/bin/env python3
"""任务卡登记：宿主 Agent 发卡、收回执、核对时各跑一行，监看中心“任务卡”页 10 秒内就能看到。

  cd "/Users/suzhizhou/Documents/私人助理/工具/小八监看"
  /usr/bin/python3 tools/taskcard.py list                                  # 看所有卡的状态
  /usr/bin/python3 tools/taskcard.py issue 43 --to "Codex" --by "小克"      # 发卡：发给谁
  /usr/bin/python3 tools/taskcard.py receipt 43 --path 回执文件.md          # 收到回执（文件）
  /usr/bin/python3 tools/taskcard.py receipt 43 --note "回执贴在对话里：…"   # 收到回执（没有文件）
  /usr/bin/python3 tools/taskcard.py returned 43 --note "反例没跑出修复前失败"  # 核对不过，退回返工
  /usr/bin/python3 tools/taskcard.py verified 43 --by "小克"               # 主力核对通过
  /usr/bin/python3 tools/taskcard.py done 43                               # 这张卡结束
  /usr/bin/python3 tools/taskcard.py note 43 --note "等用户提交"            # 只记一句，不改状态

新卡不用登记：放进任务卡目录（文件名“卡N-标题.md”，第一行“# 小八修复任务卡 N：标题”）就会出现在页面上。
台账只追加、不改写；登记错了就再登记一条正确的，最新的一条为准。
目录和台账位置取自监看中心配置（monitor.json 的 taskcards 一节，没有就用默认值）。
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from monitor_core import config as cfg  # noqa: E402
from monitor_core import taskcards  # noqa: E402


def board(config):
    settings = config.get("taskcards") or {}
    dirs = [str(cfg.resolve(config, item)) for item in (settings.get("dirs") or [])]
    ledger = settings.get("ledger") or ""
    receipt_root = settings.get("receipt_root") or ""
    if ledger:
        ledger = str(cfg.resolve(config, ledger))
    if receipt_root:
        receipt_root = str(cfg.resolve(config, receipt_root))
    return taskcards.Board(dirs, ledger, receipt_root, settings.get("receipt_depth", 3))


def when(value):
    return time.strftime("%m-%d %H:%M", time.localtime(value)) if value else "—"


def main(argv=None):
    parser = argparse.ArgumentParser(description="任务卡登记（写进发卡台账，监看中心任务卡页显示）")
    parser.add_argument("--config", help="monitor.json 路径（默认用监看中心目录里的）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="列出所有卡的状态")
    for name, text in (("issue", "发卡"), ("receipt", "收到回执"), ("returned", "核对不过，退回返工"),
                       ("verified", "核对通过"), ("done", "结束这张卡"), ("note", "只记一句备注")):
        item = sub.add_parser(name, help=text)
        item.add_argument("card", type=int, help="卡号")
        item.add_argument("--by", default="", help="谁登记的（宿主 Agent 的名字）")
        item.add_argument("--note", default="", help="说明")
        if name == "issue":
            item.add_argument("--to", default="", help="发给哪个 Agent")
        if name == "receipt":
            item.add_argument("--path", default="", help="回执文件路径")
    args = parser.parse_args(argv)
    config = cfg.load(args.config)
    b = board(config)

    if args.command == "list":
        data = b.snapshot()
        for warning in data["warnings"]:
            print("注意：" + warning, file=sys.stderr)
        for row in data["cards"]:
            print("卡%-4d %-10s %-22s 发给 %-10s 回执 %d 份（最新 %s）  %s" % (
                row["id"], row["state_text"], row["version"] or "", row["to"] or "—",
                row["receipt_count"], when(row["latest_receipt"]), row["title"]))
        return 0

    if b.find_card(args.card) is None:
        print("找不到卡 %d：任务卡目录里没有“卡%d-….md”（目录：%s）" % (
            args.card, args.card, "、".join(str(d) for d in b.card_dirs)), file=sys.stderr)
        return 2
    if args.command == "note" and not args.note.strip():
        print("note 要带 --note", file=sys.stderr)
        return 2
    try:
        record = b.ledger.append(args.card, args.command, by=args.by, to=getattr(args, "to", ""),
                                 note=args.note, path=getattr(args, "path", ""))
    except (ValueError, OSError) as exc:
        print("没有登记：%s" % exc, file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "ledger": str(b.ledger.path), "record": record}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
