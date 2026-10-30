"""Render AGENT_READER.md from topology.json and changes.json (same text as the page's Agent 读本)."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATUS = {"done": "已上线（v1.6.4）", "dev": "候选已开发", "todo": "待实现", "old": "旧执行链", "decide": "待用户裁定"}
KINDS = {"normal": "普通", "old": "旧链", "pending": "待接通"}
OPS = {"add": "新增", "edit": "修改", "del": "删除", "link": "接入", "unlink": "断开", "submit": "提交", "agent": "Agent 整理"}
SHANGHAI = timezone(timedelta(hours=8))


def lines(text):
    return [x.strip() for x in str(text or "").split("\n") if x.strip()]


def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, SHANGHAI).strftime("%Y-%m-%d %H:%M")


def render(topo, changes, submissions=()):
    nodes, edges, groups = topo["nodes"], topo["edges"], topo["groups"]
    names = {n["id"]: n["title"] for n in nodes}
    name = lambda i: names.get(i, "（已删除：%s）" % i)
    saved = stamp(topo["savedAt"]) if topo.get("savedAt") else "未知"
    out = ["# 小八架构拓扑 · Agent 读本",
           "快照保存时间：%s（Asia/Shanghai）　节点 %d · 连线 %d · 分组 %d" % (saved, len(nodes), len(edges), len(groups)), "",
           "## 读前须知",
           "- 本页是用户维护的小八架构设计底座。“用户意图”是用户想要的样子，代码与之不符时以用户意图为准并向用户报告差异，不自行改图。",
           "- 状态：已上线＝节点的基础能力 v1.6.4 已有，新意图是否实现看“问题/待办”；候选已开发＝v1.6.5 候选代码有、未上线；待实现＝设计有、代码无；旧执行链＝保留但不是新设计；待用户裁定＝有分歧，未定前不要实现。",
           "- 动手前先看“问题与待办”和“变更历史”，避免重做用户已删除或已改掉的东西。",
           "- “用户原话”是用户亲笔写的大白话，按时间列出，优先级最高；专业描述是 Agent 整理的，两者冲突时以原话为准。标“待整理”的节点，专业描述还没跟上用户最新原话。",
           "- “待确认”是还没定的事，括号里的建议不算用户决定；“裁决 N”的完整题目见 DECISIONS.md。", ""]
    pending = [s for s in submissions if s.get("status") != "done"]
    if pending:
        out.append("## 待 Agent 整理的提交（%d）" % len(pending))
        for s in sorted(pending, key=lambda s: s["at"], reverse=True):
            out.append("### " + stamp(s["at"]))
            out += ["- %s（%s）原话：%s" % (x["title"], x["node"], x["text"]) for x in s.get("notes", [])]
            out += ["- 期间改动：" + x["text"] for x in s.get("changes", [])]
        out.append("")
    for g in groups + [{"id": "", "title": "未分组"}]:
        members = [n for n in nodes if (n.get("g") or "") == g["id"]]
        if not members and not g["id"]:
            continue
        out.append("## " + g["title"])
        for n in members:
            sub = "：“%s”" % n["sub"] if n.get("sub") else ""
            mark = "　［待整理］" if n.get("pending") else ""
            out.append("- 【%s】%s（%s）%s%s" % (STATUS.get(n["status"], n["status"]), n["title"], n["id"], sub, mark))
            out += ["  - 用户原话（%s）：%s" % (stamp(h["at"]), h["text"]) for h in n.get("history", []) if h.get("by") == "user"]
            if (n.get("note") or "").strip():
                out.append("  - 用户草稿（未提交）：" + n["note"].strip())
            if n.get("intent"):
                out.append("  - 用户意图：" + n["intent"])
            if n.get("desc"):
                out.append("  - 说明：" + n["desc"])
            out += ["  - 位置：" + r for r in lines(n.get("refs"))]
            out += ["  - 问题/待办：" + r for r in lines(n.get("issues"))]
        out.append("")
    out.append("## 连线（起点 → 终点）")
    for e in edges:
        label = "：" + e["label"] if e.get("label") else ""
        kind = "［%s］" % KINDS[e["kind"]] if e.get("kind", "normal") != "normal" else ""
        out.append("- %s → %s%s%s" % (name(e["from"]), name(e["to"]), label, kind))
    out += ["", "## 变更历史（按日期，新→旧）"]
    last = None
    for c in sorted(changes, key=lambda c: c["at"], reverse=True):
        day, clock = stamp(c["at"]).split(" ")
        if day != last:
            out.append("### " + day)
            last = day
        out.append("- %s ［%s］%s" % (clock, OPS.get(c["op"], c["op"]), c["text"]))
    if not changes:
        out.append("- 暂无")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    here = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent
    topo = json.loads((here / "topology.json").read_text(encoding="utf-8"))
    changes = json.loads((here / "changes.json").read_text(encoding="utf-8"))
    subs_file = here / "submissions.json"
    submissions = json.loads(subs_file.read_text(encoding="utf-8")) if subs_file.exists() else []
    (here / "AGENT_READER.md").write_text(render(topo, changes, submissions), encoding="utf-8")
