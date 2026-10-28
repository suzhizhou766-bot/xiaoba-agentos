#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""当前软件版本的唯一声明入口。

本项目有四类版本号。只有第一类由本模块给出；第二类与它同号，随软件发布一起变；
第三、四类不随软件发布变化：

- 软件版本 `software_version()`：由 `pyproject.toml` 的 `[project] version` 单点声明，
  对外统一加 `v` 前缀（例如 `1.4.x` -> `v1.4.x`）。正文标题、技能卡、索引 `release`
  字段、发布绑定 `expected_version` 都必须与它一致。
- 知识协议版本：第二大脑内容协议，与软件版本同号，因为知识树由本仓库生成器产出。
- 索引格式版本：`skills/index.json` 与 `knowledge/index.json` 的 `schema_version`，
  以及数据库 `user_version`。它们描述结构，只有真正迁移结构时才变。
- 历史版本：`docs/HISTORY_VERSIONS.json` 与 `docs/RELEASE_V*.md`，只追加不改写。

兼容标识（Vault 物理目录 `第二大脑`、KnowLP 图目录 `knowlp`、
Obsidian 根笔记 `00_小八AgentOS.md`）不是版本号，目录名不含版本号，不随版本变化，
不得因为软件发布而重命名，否则会切断 Obsidian 绑定与既有链接。
唯一的例外是 v1.5.1：它把这三个名字里原有的版本号一次性去掉，切换程序见
`docs/RELEASE_V1.5.1.md`；此后这三个名字固定不变。
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

_PROJECT_SECTION = re.compile(r"^\[project\][ \t]*\r?\n(.*?)(?=^\[|\Z)", re.MULTILINE | re.DOTALL)
_VERSION_FIELD = re.compile(r'^[ \t]*version[ \t]*=[ \t]*"([^"]+)"', re.MULTILINE)


def pep440_version(root: Path = ROOT) -> str:
    """读取 `pyproject.toml` 的 `[project] version`，不做任何猜测或默认值兜底。"""
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    section = _PROJECT_SECTION.search(text)
    if section is None:
        raise RuntimeError("pyproject.toml 缺少 [project] 段，无法确定当前软件版本")
    match = _VERSION_FIELD.search(section.group(1))
    if match is None:
        raise RuntimeError("pyproject.toml 的 [project] 段缺少 version，无法确定当前软件版本")
    return match.group(1)


def software_version(root: Path = ROOT) -> str:
    """当前软件版本，形如 `vX.Y.Z`。"""
    return "v" + pep440_version(root)
