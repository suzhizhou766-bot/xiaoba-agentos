#!/usr/bin/env python3
"""Check the topology snapshot on every sync. Read-only; exits 1 when something is wrong."""
import json
import re
import sys
from pathlib import Path

from render_reader import render

HERE = Path(__file__).resolve().parent


def load(name):
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def main():
    topo, changes, submissions = load("topology.json"), load("changes.json"), load("submissions.json")
    scenarios, module_map = load("scenarios.json"), load("module-map.json")
    nodes = {n["id"]: n for n in topo["nodes"]}
    groups = {g["id"] for g in topo["groups"]}
    edges = {}
    problems = []

    if len(nodes) != len(topo["nodes"]):
        problems.append("节点 ID 重复")
    for node in topo["nodes"]:
        if node.get("g") not in groups:
            problems.append("节点不属于任何分组：" + node["id"])
    for edge in topo["edges"]:
        pair = (edge["from"], edge["to"])
        if pair in edges:
            problems.append("连线重复：%s → %s" % pair)
        edges[pair] = edge
        if edge["from"] not in nodes or edge["to"] not in nodes:
            problems.append("连线指向不存在的节点：%s → %s" % pair)
        if edge["from"] == edge["to"]:
            problems.append("连线自环：" + edge["from"])

    # A fork without a condition cannot be implemented from the picture.
    outgoing = {}
    for edge in topo["edges"]:
        outgoing.setdefault(edge["from"], []).append(edge)
    for source, items in outgoing.items():
        if len(items) > 1:
            for edge in items:
                if not edge.get("label"):
                    problems.append("分叉连线没有说明：%s → %s" % (nodes[source]["title"], nodes[edge["to"]]["title"]))

    # Foundation nodes have no flow edges; their dependencies must still resolve.
    for node in topo["nodes"]:
        touched = node["id"] in outgoing or any(e["to"] == node["id"] for e in topo["edges"])
        uses = node.get("uses")
        if not touched and uses is None:
            problems.append("孤立节点没有声明 uses：" + node["id"])
        if isinstance(uses, list):
            for target in uses:
                if target not in nodes:
                    problems.append("uses 指向不存在的节点：%s → %s" % (node["id"], target))

    # Scenarios are the architecture's executable examples.
    for scenario in scenarios:
        for step in scenario["steps"]:
            for a, b in step["e"]:
                if (a, b) not in edges:
                    problems.append("场景走不通：%s：%s → %s" % (scenario["name"], a, b))

    page = (HERE / "topology-page.html").read_text(encoding="utf-8")
    match = re.search(r"const SCEN=(\[.*?\]);\n", page, re.S)
    if not match or json.loads(match.group(1)) != scenarios:
        problems.append("topology-page.html 里的 SCEN 和 scenarios.json 不一致")

    mapped = [node for module in module_map["modules"].values() for node in module]
    if sorted(mapped) != sorted(nodes):
        problems.append("module-map.json 和节点不一一对应：%s" % sorted(set(mapped) ^ set(nodes)))

    reader = (HERE / "AGENT_READER.md").read_text(encoding="utf-8")
    if reader != render(topo, changes, submissions):
        problems.append("AGENT_READER.md 不是由当前数据生成的，请运行 render_reader.py")

    # The cloud document is limited to 256 KiB; warn while there is still room to act.
    size = len(json.dumps(topo, ensure_ascii=False, separators=(",", ":")).encode())
    if size > 230 * 1024:
        problems.append("拓扑文档 %d 字节，接近云端 256 KiB 上限，要把旧历史移到变更记录" % size)

    pending = [s["id"] for s in submissions if s.get("status") != "done"]
    open_items = [(nodes[n["id"]]["title"], line) for n in topo["nodes"] for line in str(n.get("issues") or "").split("\n") if line.startswith("待确认")]
    print(json.dumps({"nodes": len(nodes), "edges": len(edges), "groups": len(groups), "scenarios": len(scenarios),
                      "pending_submissions": len(pending), "open_questions": len(open_items), "document_bytes": size, "problems": problems},
                     ensure_ascii=False, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
