#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentos_version import software_version
from xiaoba_agentos.content_notes import CONTENT_BRANCHES, frontmatter, render
from xiaoba_agentos.user_rules import USER_RULES_TEXT


DATE = "2026-09-26"

VERSION = software_version()

# 版本控制模板：第二大脑新增叶子与受限正文更新的唯一来源。
TEMPLATE_FILE = Path("docs") / "second_brain" / "v152_extra_nodes.json"
TEMPLATE_NODE_KEYS = ("path", "id", "type", "parent", "authority", "title", "body", "source_path", "last_verified")
TEMPLATE_UPDATE_IDS = ("recovery-policy",)
# 发布只生成规则与发布状态两类笔记；内容分支归笔记库，模板不能往里加笔记。
# 模板必须恰好包含以下身份各一次：缺项、重复、额外项、身份漂移都拒绝。
TEMPLATE_REQUIRED_IDS = ("recovery-policy",)
TEMPLATE_FROZEN_IDENTITY = {
    "recovery-policy": {
        "path": "10_治理与裁决/故障自愈与任务恢复.md",
        "type": "memory",
        "parent": "governance",
        "authority": True,
    },
}


def _load_extra_nodes(agentos_root: Path) -> list:
    path = agentos_root / TEMPLATE_FILE
    if not path.is_file():
        raise SystemExit(f"缺少版本控制模板：{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取版本控制模板 {path}：{exc}") from exc
    if not isinstance(value, list) or not value:
        raise SystemExit(f"版本控制模板必须是非空条目数组：{path}")
    for item in value:
        if not isinstance(item, dict):
            raise SystemExit("版本控制模板条目必须是对象")
    ids = [str(item.get("id", "")) for item in value]
    duplicates = sorted({item_id for item_id in ids if ids.count(item_id) > 1})
    if duplicates:
        raise SystemExit("版本控制模板条目重复：" + ", ".join(duplicates))
    missing = [item_id for item_id in TEMPLATE_REQUIRED_IDS if item_id not in ids]
    if missing:
        raise SystemExit("版本控制模板缺少必需条目：" + ", ".join(missing))
    extra = [item_id for item_id in ids if item_id not in TEMPLATE_REQUIRED_IDS]
    if extra:
        raise SystemExit("版本控制模板含未规划条目：" + ", ".join(sorted(extra)))
    for item in value:
        expected = TEMPLATE_FROZEN_IDENTITY[str(item.get("id"))]
        for key, frozen in expected.items():
            if item.get(key) != frozen:
                raise SystemExit(
                    f"模板条目身份与冻结项不一致：{item.get('id')}: {key} 应为 {frozen!r}，实为 {item.get(key)!r}"
                )
    return value


def _check_update_identity(node: dict, item: dict) -> None:
    """受限覆盖：除 body 外每个字段必须与现有静态节点完全一致，否则明确报错。"""
    node_id = node["id"]
    for key in ("path", "type", "parent", "authority"):
        if item[key] != node[key]:
            raise SystemExit(f"模板正文更新身份不合规：{node_id}: {key} 不允许变化")
    if item["title"] != node["title"]:
        raise SystemExit(f"模板正文更新身份不合规：{node_id}: title 不允许变化")
    if item["source_path"] != node.get("source_path", ""):
        raise SystemExit(f"模板正文更新身份不合规：{node_id}: source_path 不允许变化")
    if item["last_verified"] != node.get("last_verified", DATE):
        raise SystemExit(f"模板正文更新身份不合规：{node_id}: last_verified 不允许变化")


def _merge_extra_nodes(nodes: list, agentos_root: Path) -> None:
    """读入版本控制模板：只允许替换列明笔记的 body，不能新增笔记。"""
    for item in _load_extra_nodes(agentos_root):
        if set(item) != set(TEMPLATE_NODE_KEYS):
            raise SystemExit("模板条目字段不合规：必须恰好包含 " + "/".join(TEMPLATE_NODE_KEYS) + f"：{item.get('id', '<unknown>')}")
        for key in ("path", "id", "type", "parent", "title", "body", "last_verified"):
            if not isinstance(item[key], str) or not item[key].strip():
                raise SystemExit(f"模板条目字段必须为非空字符串：{item.get('id', '<unknown>')}/{key}")
        if not isinstance(item["authority"], bool) or not isinstance(item["source_path"], str):
            raise SystemExit(f"模板条目 authority/source_path 类型不合规：{item.get('id', '<unknown>')}")
        existing = next((node for node in nodes if node["id"] == item["id"]), None)
        if existing is not None:
            if existing["id"] not in TEMPLATE_UPDATE_IDS:
                raise SystemExit(f"模板正文更新只允许：{', '.join(TEMPLATE_UPDATE_IDS)}")
            _check_update_identity(existing, item)
            existing["body"] = item["body"]
            continue
        raise SystemExit(f"模板不能新增笔记：{item['id']}（内容分支归笔记库，发布只生成规则与发布状态）")


STATIC_NODES = [{'path': '00_小八AgentOS.md',
  'id': 'root',
  'type': 'root',
  'parent': '',
  'authority': True,
  'title': '小八 AgentOS {version}',
  'body': '本 Vault 是 AGY+DSH {version} 当前知识入口。仅活动且有证据的规则参与检索，软件版本发布不替代用户对经验的两阶段审批。目录名与根笔记文件名不含版本号，不随版本变化，不得再改名，以免破坏 '
          'Obsidian 绑定。',
  'children': ['10_治理与裁决', '20_能力与技能', '30_方法与知识', '40_工作流', '50_资产与索引', '60_当前上下文', '70_复利规则']},
 {'path': '10_治理与裁决.md',
  'id': 'governance',
  'type': 'branch',
  'parent': 'root',
  'authority': True,
  'title': '治理与裁决',
  'body': '用户是唯一规范制定者；系统状态以当前宿主机证据和可重复验证为准。',
  'children': ['10_治理与裁决/运行宪法', '10_治理与裁决/模型链路与认证', '10_治理与裁决/故障自愈与任务恢复', '10_治理与裁决/记忆与发布边界']},
 {'path': '10_治理与裁决/运行宪法.md',
  'id': 'constitution',
  'type': 'memory',
  'parent': 'governance',
  'authority': True,
  'title': '运行宪法',
  'last_verified': '2026-10-05',
  'body': '- 用户决定目标、权限、发布、付费、删除和创意最终验收。\n'
          '- AgentOS 负责可恢复任务队列、多 Agent 执行、裁判、审计和受控进化，不自行扩大权限。\n'
          '- 当前直接检查优先于缓存、推断和单次历史错误；未知即说明证据缺口。\n'
          '- 技术通过不等于业务通过，模型输出不自动成为权威事实。\n'
          '- 任何正式变更先在候选区验证，再原子切换；现行发布内容保持可校验。\n'
          '\n'
          '用户公共规则（备份授权）：\n'
          '- 没有用户直接、明确的备份要求，不得自行新建备份文件、备份目录或备份快照。\n'
          '- 可以提醒用户是否需要备份；提醒、一般操作授权或发布授权都不等于备份授权。\n'
          '- 本用户规则优先于工作流、技能和发布脚本的默认备份建议；未经明确授权，不得执行内置备份步骤。\n'
          '- 本规则不授权自动删除已有备份。'},
 {'path': '10_治理与裁决/模型链路与认证.md',
  'id': 'model-chain',
  'type': 'memory',
  'parent': 'governance',
  'authority': True,
  'title': '模型链路与认证',
  'body': '现行审查与协作只来自用户在 Agent 池里开启并测试通过的节点。\n'
          '\n'
          '没有默认审查名单。至少两份彼此独立的合格结论才能过审查；宿主只有在这次没干活、也不是作者时才能补一票。凭据由各节点自己的客户端持有，不进入知识库。\n'
          '\n'
          '上班不向节点发真实探针。节点是否可派，看它是否开启、测试是否通过、入口是否还在。不得改客户端登录材料或自选模型来凑审查。'},
 {'path': '10_治理与裁决/故障自愈与任务恢复.md',
  'id': 'recovery-policy',
  'type': 'memory',
  'parent': 'governance',
  'authority': True,
  'title': '故障自愈与任务恢复',
  'body': 'session-start '
          '在本地doctor通过后执行AGY+DSH真实健康标记探针，不能用缓存健康代替。两侧合格才接单。外部额度/认证/429/503/断连/超时报告并有界退避，不反复改代码或切账号。单侧成功保存输出/回执/冻结快照，失败侧修复并重探后，由用户显式task-resume恢复；只补跑失败侧。发布绑定失配立即停止。'},
 {'path': '10_治理与裁决/记忆与发布边界.md',
  'id': 'memory-policy',
  'type': 'memory',
  'parent': 'governance',
  'authority': True,
  'title': '记忆与发布边界',
  'body': '- 本 Vault 只有一个根；每个节点除根外只有一个结构父节点。\n'
          '- KnowLP 只负责定位，AgentOS 回读本 Vault 原文后才使用。\n'
          '- 经验先进入私有 outbox；交付初审和下班终审均通过后，仍须通过证据、冲突、敏感信息和索引复验门禁才可发布。\n'
          '- 两次批准必须绑定同一个 `candidate_sha256`；候选变化使旧批准失效，最终批准缺少同哈希交付批准时拒绝发布。\n'
          '- 已发布经验保留复用与复验计数；失败达到规则阈值时移出索引并进入可恢复退役归档。\n'
          '- 当前退役阈值为连续失败复用 2 次；发布成功证据必须包含候选哈希、经验路径和 KnowLP 索引复验状态。\n'
          '- 可恢复退役只保留私有原文，不提供自动恢复捷径；重新启用必须作为新候选完成两阶段裁决和索引复验。\n'
          '- 技能正文留在技能目录，Vault 只保留技能卡；媒体本体留在资产库，Vault 只保留资产卡。\n'
          '- 凭据、Cookie、OAuth 内容、日志、缓存、原始聊天和未核验模型输出永久排除。'},
 {'path': '60_当前上下文.md',
  'id': 'current',
  'type': 'branch',
  'parent': 'root',
  'authority': True,
  'title': '当前上下文',
  'body': '本分支只保存一个当前系统状态入口。',
  'children': ['60_当前上下文/系统当前态']},
 {'path': '60_当前上下文/系统当前态.md',
  'id': 'current-state',
  'type': 'memory',
  'parent': 'current',
  'authority': True,
  'title': '系统当前态',
  'last_verified': '2026-10-11',
  'body': '- 活动方案：小八 AgentOS {version}，AGY+DSH双后台，成功门槛2。\n'
          '- 唯一跨 Agent '
          '入口：`~/.agents/skills/xiaoba-agentos/scripts/xiaoba`。Codex技能发现入口：`~/.codex/skills/xiaoba-agentos`。\n'
          '- 运行源码：`/Users/suzhizhou/Documents/小八AgentOS`；不可变发布内容由 `.runtime/releases/index.json` 的当前指针指定，具体目录以 '
          '`verify-release` 返回的 release_dir 为准，本笔记不写死版本号。\n'
          '- 当前 Obsidian Vault：`/Users/suzhizhou/Documents/私人助理/第二大脑`（目录名不含版本号，不随版本变化，正文协议为{version}）。\n'
          '- Worker、发布哈希和最新真实探针以status/doctor/session-start现场结果为准。健康有时效，旧completed不代表现在可用。\n'
          '- Intake决定直答/任务/审核/追问；交付须区分宿主回答、后台建议和已执行事实。\n'
          '- 双阶段同哈希审核、敏感检查和索引复验保持；发布不批量批准记忆。'},
 {'path': '70_复利规则.md',
  'id': 'learning',
  'type': 'branch',
  'parent': 'root',
  'authority': True,
  'title': '复利规则',
  'body': '只有有证据、可重复、跨任务有价值的结论才进入长期复用规则。',
  'children': ['70_复利规则/长期复用门禁', '70_复利规则/经验索引', '70_复利规则/审核入口']},
 {'path': '70_复利规则/长期复用门禁.md',
  'id': 'reuse-gate',
  'type': 'memory',
  'parent': 'learning',
  'authority': True,
  'title': '长期复用门禁',
  'body': '候选经验必须同时满足：来源可追溯、事实已核验、交付初审通过、下班终审通过、适用边界明确、与当前规则不冲突、不会携带凭据或项目隐私，并在发布后通过索引复验。\n'
          '\n'
          '检索命中只表示找到候选，不表示它仍然正确。复用结果由新任务的用户终审确认；失败进入复验，连续失败达到配置阈值后从活动索引退役，但保留可恢复归档。\n'
          '\n'
          '技能更新必须重建哈希索引并通过结构、功能、安全和回归测试。价值文件变化必须重建目录哈希；哈希失配时只报告过期，不继续调用。'},
 {'path': '70_复利规则/经验索引.md',
  'id': 'experiences',
  'type': 'branch',
  'parent': 'learning',
  'authority': True,
  'title': '已审核经验索引',
  'body': '只有完成交付初审、下班终审、敏感信息检查并通过索引复验的任务经验才会列在这里。终审拒绝或复验失败的候选不进入本索引。',
  'children': []},
 {'path': '70_复利规则/审核入口.md',
  'id': 'review-entry',
  'type': 'memory',
  'parent': 'learning',
  'authority': True,
  'title': '记忆审核入口',
  'body': '正式审核状态以活动AgentOS数据库为准，不复制未审核候选正文入库。统一入口执行 `review-list --stage '
          'all`。软件发布前检查点：17条待初审、1条已初审待终审、2条拒绝，已发布0；这些是带日期的盘点，不是新增批准。后续状态以命令实时读取。重建索引不修改候选哈希、审核签名和审计事件。'}]


def _frontmatter(node: dict, source_path: str = "") -> str:
    return frontmatter(node, source_path)


def _render(node: dict, source_path: str = "") -> str:
    return render(node, source_path, VERSION)


def build(agentos_root: Path, published_agentos_root: Path | None = None) -> dict:
    """只生成规则与发布状态两类笔记。published_agentos_root 仅为兼容既有调用而保留，不再使用。"""
    nodes = json.loads(json.dumps(STATIC_NODES))
    _merge_extra_nodes(nodes, agentos_root)
    history_path = agentos_root / "docs" / "HISTORY_VERSIONS.json"
    if history_path.is_file():
        history = json.loads(history_path.read_text(encoding="utf-8"))["records"]
        next(node for node in nodes if node["id"] == "root")["children"] = list(
            next(node for node in nodes if node["id"] == "root")["children"]
        ) + ["历史版本记录"]
        nodes.append({
            "path": "历史版本记录.md", "id": "release-history", "type": "branch",
            "parent": "root", "authority": False, "title": "历史版本记录",
            "body": "当前正式方案为 {version}（AGY+DSH）。旧版说明已退出活动知识树并保留私有归档。以下记录版本边界；现场健康以最新检查为准。",
            "children": ["历史版本记录/" + item["version"] for item in history],
        })
        for item in history:
            nodes.append({
                "path": "历史版本记录/" + item["version"] + ".md",
                "id": "release-history-" + item["version"], "type": "release-record",
                "parent": "release-history", "authority": False,
                "title": item["version"] + " 历史版本记录",
                "body": (
                    "用途：仅用于追溯，不作为当前操作指令。\n\n"
                    + "发布记录日期：" + str(item["release_date"]) + "\n\n"
                    + "发布时说明：\n\n" + item["summary"] + "\n\n"
                    + "发布时测试记录：`" + json.dumps(item["tests"], ensure_ascii=False) + "`\n\n"
                    + "完整清单指纹：`" + item["files_manifest_sha256"] + "`\n\n"
                    + "源码清单指纹：`" + item["source_manifest_sha256"] + "`\n\n"
                    + item["file_disposition"]
                ),
            })
    return {node["path"]: _render(node, node.get("source_path", "")) for node in nodes}


def validate(rendered: dict) -> list:
    problems = []
    ids = {}
    parents = {}
    links = {}
    banned = (
        "historical memory:", "migration note:", "deprecated deployment:", "legacy route:",
        "历史记忆：", "迁移说明：", "弃用部署：", "旧路由：",
    )
    for path, content in rendered.items():
        match_id = re.search(r'^id: "([^"]+)"$', content, re.MULTILINE)
        match_parent = re.search(r'^parent: "([^"]*)"$', content, re.MULTILINE)
        if not match_id or not match_parent:
            problems.append(f"frontmatter 缺失：{path}")
            continue
        node_id = match_id.group(1)
        if node_id in ids:
            problems.append(f"重复 id：{node_id}")
        ids[node_id] = path
        parents[node_id] = match_parent.group(1)
        links[path] = re.findall(r"\[\[([^\]]+)\]\]", content)
        lowered = content.casefold()
        found = [item for item in banned if item in lowered]
        if found:
            problems.append(f"非当前态信息：{path}: {', '.join(found)}")
    roots = [node_id for node_id, parent in parents.items() if not parent]
    if roots != ["root"]:
        problems.append(f"根节点必须唯一：{roots}")
    path_to_id = {str(Path(path).with_suffix("")): node_id for node_id, path in ids.items()}
    for node_id, parent in parents.items():
        if node_id == "root":
            continue
        if parent not in ids:
            problems.append(f"父节点不存在：{node_id} -> {parent}")
            continue
        parent_path = ids[parent]
        child_link = str(Path(ids[node_id]).with_suffix(""))
        if links[parent_path].count(child_link) != 1:
            problems.append(f"父节点没有唯一链接子节点：{parent} -> {node_id}")
    expected_links = {
        str(Path(ids[node_id]).with_suffix(""))
        for node_id in ids
        if node_id != "root"
    }
    # 根笔记仍链接四个内容分支；这些分支笔记在笔记库里，不在生成结果中。
    expected_links |= set(CONTENT_BRANCHES)
    for branch in CONTENT_BRANCHES:
        if links.get("00_小八AgentOS.md", []).count(branch) != 1 or (branch + ".md") in rendered:
            problems.append(f"内容分支必须由根笔记唯一链接且不由发布生成：{branch}")
    actual_links = {link for values in links.values() for link in values}
    if actual_links != expected_links:
        problems.append("WikiLink 集合与父子树不一致")
    for node_id in ids:
        seen = set()
        current = node_id
        while current:
            if current in seen:
                problems.append(f"父链成环：{node_id}")
                break
            seen.add(current)
            current = parents.get(current, "")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agentos-root", type=Path, required=True)
    parser.add_argument("--published-agentos-root", type=Path)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    published_root = args.published_agentos_root.resolve() if args.published_agentos_root else None
    rendered = build(args.agentos_root.resolve(), published_root)
    problems = validate(rendered)
    if problems:
        raise SystemExit("\n".join(problems))
    target = args.target.resolve()
    if args.verify:
        actual = {
            str(path.relative_to(target)): path.read_text(encoding="utf-8")
            for path in target.rglob("*.md")
        } if target.is_dir() else {}
        if actual != rendered:
            raise SystemExit("second brain differs from generated tree")
        print(json.dumps({"ok": True, "notes": len(rendered), "root": 1, "edges": len(rendered) - 1}, ensure_ascii=False))
        return 0
    if target.exists() and any(target.iterdir()):
        raise SystemExit(f"target must be absent or empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    for relative, content in rendered.items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    print(json.dumps({"ok": True, "notes": len(rendered), "root": 1, "edges": len(rendered) - 1}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
