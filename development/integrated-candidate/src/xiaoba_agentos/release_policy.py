"""发布分档（release_policy）：只做源码布局的分档，规则取自上一版。

本版收窄后的口径（任务卡「分档瘦身与结构指纹」）：

- 只支持源码布局：两侧都传 ``<发布包>/agentos`` 这一层源码树，各自按
  ``SOURCE_MANIFEST.json`` 实算校验，不一致即失败关闭（``ok=false``）。
  正式包一级的 ``RELEASE.json``、``interface/``、``second-brain/`` 每版必变，
  由构包时的校验步骤负责，不再进入分档。
- 规则来源：``rules_path`` 为空时读取 ``baseline_root`` 下的
  ``config/release_policy.json``，即上一版的规则；候选改自己的规则或工具
  影响不到自己这一版的档位。
- 不做内容分析：不提取迁移语句、不比对 JSON 格式骨架、不检测目录内字节
  不变的移动或改名。这些情况一律按普通新增/删除/修改走路径规则。
- 迁移档只认五样：数据库 schema 常量、``docs/DEPLOYMENT.json`` 的 ``steps``、
  ``contract_declarations`` 版本号、受管顶层目录或根文件集合的增减、
  ``tests/fixtures/schema_fingerprint.json`` 的改动。表结构是否真变交给
  ``tests/test_schema_fingerprint.py`` 实建库比对指纹来拦。

输出三档 ``patch`` / ``regular`` / ``migration``；失败关闭时 ``ok=false`` 且档位为
``None``，不猜档位放行。
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1
TIERS: Tuple[str, ...] = ("patch", "regular", "migration")
_TIER_RANK: Dict[str, int] = {"patch": 0, "regular": 1, "migration": 2}
_CHANGE_ADDED = "added"
_CHANGE_MODIFIED = "modified"
_CHANGE_DELETED = "deleted"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DEF_FUNC_RE = r"^\s*(?:async\s+)?def\s+%s\s*\("
_VERSION_LINE_RE = re.compile(r"^\s*version\s*=")
_RULES_LOGICAL = "config/release_policy.json"


class PolicyError(Exception):
    """失败关闭：输入损坏、缺失、布局未知或验证不通过。"""


# 固定底线（实现内置，不随规则文件变化）。候选自带规则或外部 rules_path 只能
# 细分归属或提高档位，不能把下列文件类别降到更低档位。
_BUILTIN_TIER_FLOOR_EXACT: Dict[str, str] = {
    # 分档实现与规则自身
    "src/xiaoba_agentos/release_policy.py": "regular",
    "config/release_policy.json": "regular",
    "tests/test_release_policy.py": "regular",
    # 结构指纹护栏
    "tests/test_schema_fingerprint.py": "regular",
    "tests/fixtures/schema_fingerprint.json": "regular",
    # 底线标记载体
    "tests/guardrail_marks.json": "regular",
}
_BUILTIN_TIER_FLOOR_PREFIXES: Tuple[Tuple[str, str], ...] = (
    (".agents/", "regular"), ("artifacts/", "regular"), ("bin/", "regular"),
    ("config/", "regular"), ("docs/", "patch"), ("knowledge/", "regular"),
    ("launchd/", "regular"), ("macos/", "regular"), ("scripts/", "regular"),
    ("skills/", "regular"), ("src/", "regular"), ("tests/", "patch"),
)
# version_bump_only 只对内置固定允许清单生效；候选改 version_bump_only_paths
# 不能把别的运行文件降成「仅版号变化」。
_BUILTIN_VERSION_BUMP_PATHS: Tuple[str, ...] = ("pyproject.toml",)

# 迁移判定固定输入：读取位置写死在实现里，候选规则改不动。
#   1) 数据库结构版本：src/xiaoba_agentos/store.py 的 DATABASE_SCHEMA_VERSION
#   2) 部署步骤：docs/DEPLOYMENT.json 的 steps（按规范化 JSON 比较）
_BUILTIN_DATABASE: Tuple[str, str] = ("src/xiaoba_agentos/store.py",
                                      "DATABASE_SCHEMA_VERSION")
_BUILTIN_DEPLOYMENT_STEPS: Tuple[str, str] = ("docs/DEPLOYMENT.json", "steps")

# 契约声明与底线标记取「并集」保护：候选规则可以增加声明，但删不掉内置条目。
_BUILTIN_CONTRACT_SPECS: Tuple[Dict[str, str], ...] = (
    {"path": "src/xiaoba_agentos/store.py", "kind": "python_constant",
     "name": "DATABASE_SCHEMA_VERSION", "label": "database_schema_version"},
    {"path": "config/agentos.json", "kind": "json_key", "key": "schema_version",
     "label": "config_schema_version"},
    {"path": "config/inventory_exclusions.json", "kind": "json_key",
     "key": "schema_version", "label": "inventory_exclusions_schema_version"},
    {"path": "skills/index.json", "kind": "json_key", "key": "schema_version",
     "label": "skills_index_schema_version"},
    {"path": "skills/catalog.json", "kind": "json_key", "key": "schema_version",
     "label": "skills_catalog_schema_version"},
    {"path": "knowledge/catalog.json", "kind": "json_key", "key": "schema_version",
     "label": "knowledge_catalog_schema_version"},
    {"path": "knowledge/index.json", "kind": "json_key", "key": "schema_version",
     "label": "knowledge_index_schema_version"},
    {"path": "CAPABILITY.json", "kind": "json_key", "key": "schema_version",
     "label": "capability_schema_version"},
    {"path": "docs/DEPLOYMENT.json", "kind": "json_key", "key": "schema_version",
     "label": "deployment_schema_version"},
    {"path": "tests/guardrail_marks.json", "kind": "json_key", "key": "schema_version",
     "label": "guardrail_marks_schema_version"},
)
_BUILTIN_GUARDRAIL_MARKS_PATH = "tests/guardrail_marks.json"

# 每一版都必然变动的发布簿记。不识别它们，任何真实的新版本都到不了补丁档。
# 两份索引由别的文件生成，构包时按重新生成的结果校验；来源文件各自按路径定档。
_BUILTIN_DERIVED_INDEXES: Tuple[str, ...] = ("skills/index.json", "knowledge/index.json")
_BUILTIN_HISTORY_PATH = "docs/HISTORY_VERSIONS.json"
_BUILTIN_DEPLOYMENT_BASE_KEY = "from_source_manifest_sha256"
_DECLARED_VERSION_RE = re.compile(r'^version\s*=\s*"(\d+\.\d+\.\d+)"\s*$', re.MULTILINE)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_json(text: str) -> Optional[str]:
    try:
        return _canonical(json.loads(text))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_json_file(path: Path, label: str) -> Any:
    try:
        return json.loads(_read_text(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyError(f"{label}_unparsable: {type(exc).__name__}") from exc


def _raise_tier(tier: str) -> str:
    index = TIERS.index(tier) if tier in TIERS else 0
    return TIERS[min(index + 1, len(TIERS) - 1)]


def _max_tier(*tiers: str) -> str:
    result = "patch"
    for tier in tiers:
        if tier in _TIER_RANK and _TIER_RANK[tier] > _TIER_RANK[result]:
            result = tier
    return result


def _resolve_root(value: Any, label: str) -> Path:
    if isinstance(value, bool) or not isinstance(value, (str, os.PathLike)):
        raise PolicyError(f"{label}_root_invalid_type: {type(value).__name__}")
    text = os.fspath(value)
    if not text.strip():
        raise PolicyError(f"{label}_root_empty")
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        raise PolicyError(f"{label}_root_not_absolute: {text}")
    if path.is_symlink():
        raise PolicyError(f"{label}_root_is_symlink: {text}")
    if not path.is_dir():
        raise PolicyError(f"{label}_root_not_directory: {text}")
    return path.resolve()


def _validate_rules(rules: Any) -> None:
    if not isinstance(rules, dict):
        raise PolicyError("rules_not_object")
    for key in ("tiers", "layouts", "path_rules", "guardrail_marks",
                "contract_declarations", "database"):
        if key not in rules:
            raise PolicyError(f"rules_missing_field: {key}")
    if list(rules["tiers"]) != list(TIERS):
        raise PolicyError("rules_tiers_mismatch")
    layouts = rules["layouts"]
    if not isinstance(layouts, dict) or not isinstance(layouts.get("source"), dict):
        raise PolicyError("rules_layouts_incomplete: 需要layouts.source")
    layout = layouts["source"]
    if not layout.get("detect_file") or not isinstance(layout.get("manifest"), dict):
        raise PolicyError("rules_layout_invalid: source")
    if not isinstance(rules["path_rules"], list) or not rules["path_rules"]:
        raise PolicyError("rules_path_rules_empty")
    for rule in rules["path_rules"]:
        if not isinstance(rule, dict) or rule.get("tier") not in TIERS:
            raise PolicyError("rules_path_rule_invalid")
    marks = rules["guardrail_marks"]
    if not isinstance(marks, dict) or not marks.get("path"):
        raise PolicyError("rules_guardrail_marks_invalid")
    if not isinstance(rules["contract_declarations"], list):
        raise PolicyError("rules_contract_declarations_invalid")
    database = rules["database"]
    if not isinstance(database, dict) or not database.get("schema_source") \
            or not database.get("constant"):
        raise PolicyError("rules_database_invalid")


def _load_rules(rules_path: Any, baseline_root: Path) -> Tuple[Dict[str, Any], str]:
    """加载规则：显式 rules_path，否则读 baseline_root 下的上一版规则文件。"""
    if rules_path is None:
        path = baseline_root / _RULES_LOGICAL
    else:
        if isinstance(rules_path, bool) or not isinstance(rules_path, (str, os.PathLike)):
            raise PolicyError("rules_path_invalid_type")
        path = Path(os.path.expanduser(os.fspath(rules_path)))
    if not path.is_absolute():
        raise PolicyError("rules_path_not_absolute")
    if path.is_symlink() or not path.is_file():
        raise PolicyError("rules_path_unavailable")
    raw = path.read_bytes()
    try:
        rules = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyError(f"rules_unparsable: {type(exc).__name__}") from exc
    _validate_rules(rules)
    return rules, _sha256_bytes(raw)


def _builtin_tier_floor(logical: str) -> str:
    if logical in _BUILTIN_TIER_FLOOR_EXACT:
        return _BUILTIN_TIER_FLOOR_EXACT[logical]
    for prefix, tier in _BUILTIN_TIER_FLOOR_PREFIXES:
        if logical.startswith(prefix):
            return tier
    return "patch"


def _effective_contract_specs(rules: Dict[str, Any]) -> List[Dict[str, Any]]:
    """规则声明与内置底线的并集：按 (path, kind, key|name) 去重，内置在后补充。"""
    declared = rules.get("contract_declarations") or []
    merged: List[Dict[str, Any]] = []
    seen = set()
    for spec in list(declared) + list(_BUILTIN_CONTRACT_SPECS):
        if not isinstance(spec, dict) or not spec.get("path"):
            continue
        token = (spec["path"], spec.get("kind"), spec.get("key") or spec.get("name"))
        if token in seen:
            continue
        seen.add(token)
        merged.append(spec)
    return merged


def _effective_marks_paths(rules: Dict[str, Any]) -> List[str]:
    declared = rules.get("guardrail_marks", {}).get("path")
    paths = [declared] if declared else []
    for path in [_BUILTIN_GUARDRAIL_MARKS_PATH]:
        if path not in paths:
            paths.append(path)
    return paths


class SideTree:
    """一侧（基线或候选）的扫描结果：逻辑路径 -> 文件信息。"""

    def __init__(self, root: Path, layout_cfg: Dict[str, Any],
                 files: Dict[str, Dict[str, Any]], out_of_scope: List[str],
                 label: str = "side"):
        self.root = root
        self.label = label
        self.layout = "source"
        self.layout_cfg = layout_cfg
        self.files = files
        self.out_of_scope = out_of_scope
        # 顶层目录与根文件集合：实际看到的所有文件（含范围外）都算，
        # 用于「受管顶层目录或根文件集合有增减」这一迁移条件。
        self.top_level = sorted({rel.split("/", 1)[0]
                                 for rel in list(files) + list(out_of_scope)})

    def physical(self, logical: str) -> Path:
        entry = self.files.get(logical)
        if entry is None:
            raise PolicyError(f"path_not_in_scope: {logical}")
        return self.root / entry["physical"]

    def has(self, logical: str) -> bool:
        return logical in self.files

    def sha(self, logical: str) -> str:
        return self.files[logical]["sha256"]

    def side_digest(self) -> str:
        return _sha256_bytes(_canonical(
            {rel: self.files[rel]["sha256"] for rel in sorted(self.files)}).encode("utf-8"))


def _walk_regular_files(root: Path, excluded_names: List[str], label: str
                        ) -> Dict[str, str]:
    excluded = set(excluded_names or [])
    hashes: Dict[str, str] = {}
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in excluded)
        for name in sorted(filenames):
            if name in excluded:
                continue
            path = Path(current) / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise PolicyError(f"{label}_symlink: {rel}")
            if not path.is_file():
                raise PolicyError(f"{label}_not_regular_file: {rel}")
            hashes[rel] = _sha256_file(path)
    return hashes


def _scan_files(root: Path, wanted_roots: List[str], wanted_root_files: List[str],
                excluded_names: List[str], label: str
                ) -> Tuple[Dict[str, str], List[str]]:
    all_files = _walk_regular_files(root, excluded_names, label)
    wanted_roots = set(wanted_roots or [])
    wanted_root_files = set(wanted_root_files or [])
    scoped: Dict[str, str] = {}
    out_of_scope: List[str] = []
    for rel, digest in sorted(all_files.items()):
        head = rel.split("/", 1)[0]
        if head in wanted_roots or rel in wanted_root_files:
            scoped[rel] = digest
        else:
            out_of_scope.append(rel)
    return scoped, out_of_scope


def _detect_layout(root: Path, rules: Dict[str, Any]) -> Dict[str, Any]:
    """只认源码布局；未匹配即失败关闭（正式包布局本版不再支持）。"""
    layout = rules["layouts"]["source"]
    if not (root / layout["detect_file"]).is_file():
        raise PolicyError("layout_unknown: 未匹配源码布局，需存在 "
                          f"{layout['detect_file']}")
    also = layout.get("reject_if_also_present")
    if also and (root / also).exists():
        raise PolicyError(f"layout_ambiguous: {also}")
    return layout


def _build_source_side(root: Path, rules: Dict[str, Any], label: str) -> SideTree:
    layout = rules["layouts"]["source"]
    manifest_cfg = layout["manifest"]
    scanned, out_of_scope = _scan_files(
        root, layout.get("compared_roots"), layout.get("compared_root_files"),
        manifest_cfg.get("excluded_names"), label)
    files = {rel: {"sha256": digest, "physical": rel, "presence": "manifest_scope"}
             for rel, digest in scanned.items()}
    side = SideTree(root, layout, files, out_of_scope, label)
    _verify_source_manifest(side, manifest_cfg, label)
    return side


def _recompute_source_manifest(side: SideTree, manifest_cfg: Dict[str, Any]
                               ) -> Dict[str, str]:
    """按 scripts/create_manifest.py 的纳入规则独立重算清单，不读清单文件本身。"""
    included_roots = set(manifest_cfg.get("included_roots") or [])
    root_files = set(manifest_cfg.get("root_files") or [])
    entries: Dict[str, str] = {}
    for rel in sorted(side.files):
        if rel == manifest_cfg["path"]:
            continue
        if rel in root_files:
            entries[rel] = side.sha(rel)
        elif "/" in rel and rel.split("/", 1)[0] in included_roots:
            entries[rel] = side.sha(rel)
    return entries


def _validate_manifest_paths(entries: List[str], label: str) -> None:
    for rel in entries:
        if not rel or rel.startswith("/") or "\\" in rel:
            raise PolicyError(f"{label}_path_escape: {rel!r}")
        parts = rel.split("/")
        if any(part in ("..", ".") for part in parts) or any(not part for part in parts):
            raise PolicyError(f"{label}_path_escape: {rel!r}")


def _verify_source_manifest(side: SideTree, manifest_cfg: Dict[str, Any], label: str
                            ) -> None:
    """SOURCE_MANIFEST.json 必须与真实文件逐项一致，不一致即失败关闭。"""
    logical = manifest_cfg["path"]
    if not side.has(logical):
        raise PolicyError(f"{label}_source_manifest_missing")
    doc = _parse_json_file(side.physical(logical), f"{label}_source_manifest")
    if not isinstance(doc, dict) or doc.get("algorithm") != "sha256" \
            or not isinstance(doc.get("files"), dict):
        raise PolicyError(f"{label}_source_manifest_invalid")
    recorded = doc["files"]
    for rel in recorded:
        _validate_manifest_paths([rel], f"{label}_source_manifest")
    recomputed = _recompute_source_manifest(side, manifest_cfg)
    missing = sorted(set(recomputed) - set(recorded))
    extra = sorted(set(recorded) - set(recomputed))
    mismatch = sorted(rel for rel in set(recomputed) & set(recorded)
                      if recorded[rel] != recomputed[rel])
    if missing or extra or mismatch:
        raise PolicyError(f"{label}_source_manifest_mismatch: "
                          f"missing={missing} extra={extra} hash_mismatch={mismatch}")


def _load_guardrail_marks(side: SideTree, rules: Dict[str, Any], label: str
                          ) -> Tuple[List[str], List[str], Dict[str, Any]]:
    """读取并校验一侧 guardrail_marks.json，返回 (受保护文件, 逐条标记引用, 原始文档)。

    失败关闭条件：文件缺失、JSON 损坏、结构不符、引用格式非法、
    引用文件不在受控集、引用函数在文件中不存在。
    """
    cfg = rules["guardrail_marks"]
    logical = cfg["path"]
    if not side.has(logical):
        raise PolicyError(f"{label}_guardrail_marks_missing")
    doc = _parse_json_file(side.physical(logical), f"{label}_guardrail_marks")
    if not isinstance(doc, dict):
        raise PolicyError(f"{label}_guardrail_marks_not_object")
    if doc.get("schema_version") != cfg["expected_schema_version"]:
        raise PolicyError(f"{label}_guardrail_marks_schema_version")
    baselines = doc.get("baselines")
    if not isinstance(baselines, dict) or not baselines:
        raise PolicyError(f"{label}_guardrail_marks_baselines")
    id_pattern = re.compile(cfg["baseline_id_pattern"])
    allowed_statuses = set(cfg["allowed_statuses"])
    protected: List[str] = []
    refs: List[str] = []
    tests_dir = logical.split("/", 1)[0]
    for bid, entry in sorted(baselines.items()):
        if not id_pattern.match(bid):
            raise PolicyError(f"{label}_guardrail_marks_baseline_id: {bid}")
        if not isinstance(entry, dict):
            raise PolicyError(f"{label}_guardrail_marks_entry: {bid}")
        if not isinstance(entry.get("name"), str) or not entry["name"]:
            raise PolicyError(f"{label}_guardrail_marks_name: {bid}")
        if entry.get("status") not in allowed_statuses:
            raise PolicyError(f"{label}_guardrail_marks_status: {bid}")
        tests = entry.get("tests")
        if not isinstance(tests, list):
            raise PolicyError(f"{label}_guardrail_marks_tests: {bid}")
        for ref in tests:
            if not isinstance(ref, str) or ref.count("::") != 1:
                raise PolicyError(f"{label}_guardrail_marks_ref_format: {bid}:{ref!r}")
            rel_file, func = ref.split("::")
            if not rel_file.endswith(".py") or not func.startswith("test_"):
                raise PolicyError(f"{label}_guardrail_marks_ref_format: {bid}:{ref!r}")
            target = f"{tests_dir}/{rel_file}"
            _validate_manifest_paths([target], f"{label}_guardrail_marks")
            if not side.has(target):
                raise PolicyError(f"{label}_guardrail_marks_dangling_file: {ref}")
            text = _read_text(side.physical(target))
            if not re.search(_DEF_FUNC_RE % re.escape(func), text, re.M):
                raise PolicyError(f"{label}_guardrail_marks_dangling_function: {ref}")
            protected.append(target)
            refs.append(f"{target}::{func}")
    return sorted(set(protected)), sorted(set(refs)), doc


def _load_guardrail_marks_all(side: SideTree, rules: Dict[str, Any], label: str
                              ) -> Tuple[List[str], List[str], Dict[str, Any]]:
    """对每个有效标记路径取并集，返回 (受保护文件, 引用, 每路径文档)。"""
    protected: List[str] = []
    refs: List[str] = []
    docs: Dict[str, Any] = {}
    for logical in _effective_marks_paths(rules):
        variant = dict(rules)
        variant["guardrail_marks"] = dict(rules["guardrail_marks"])
        variant["guardrail_marks"]["path"] = logical
        path_protected, path_refs, doc = _load_guardrail_marks(side, variant, label)
        protected.extend(path_protected)
        refs.extend(path_refs)
        docs[logical] = {"protected": path_protected, "refs": path_refs,
                         "sha256": side.sha(logical) if side.has(logical) else None}
    return sorted(set(protected)), sorted(set(refs)), docs


def _test_support_files(side: SideTree) -> List[str]:
    """tests/ 下不是独立测试的文件：夹具、共用代码，以及被别的测试文件导入的测试文件。

    改它们可以在不碰带标记测试文件的情况下改变那些测试的行为，所以不能算补丁。
    """
    tests = sorted(rel for rel in side.files if rel.startswith("tests/"))
    support = {rel for rel in tests
               if not (rel.count("/") == 1 and rel.endswith(".py") and rel.split("/")[1].startswith("test_"))}
    modules = {rel.split("/")[1][:-3]: rel for rel in tests if rel.count("/") == 1 and rel.endswith(".py")}
    for rel in tests:
        if not rel.endswith(".py"):
            continue
        try:
            tree = ast.parse(_read_text(side.physical(rel)))
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
            support.add(rel)  # 读不懂的测试文件不当作可随意改动的补丁
            continue
        for node in ast.walk(tree):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for name in names:
                target = modules.get(name.split(".")[0])
                if target and target != rel:
                    support.add(target)
    return sorted(support)


def _extract_python_constant(text: str, name: str) -> Tuple[bool, Any]:
    match = re.search(r"(?m)^\s*%s\s*=\s*(.+?)\s*$" % re.escape(name), text)
    if not match:
        return False, None
    try:
        return True, ast.literal_eval(match.group(1))
    except (ValueError, SyntaxError):
        return False, None


def _extract_json_key(text: str, key: str) -> Tuple[bool, Any]:
    try:
        doc = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False, None
    if not isinstance(doc, dict) or key not in doc:
        return False, None
    return True, doc[key]


def _extract_declaration(text: str, spec: Dict[str, Any]) -> Tuple[bool, Any]:
    kind = spec.get("kind")
    if kind == "python_constant":
        return _extract_python_constant(text, str(spec.get("name", "")))
    if kind == "json_key":
        return _extract_json_key(text, str(spec.get("key", "")))
    return False, None


def _normalized_json_key(text: str, key: str) -> Tuple[bool, Any]:
    """按规范化 JSON 提取字段：sort_keys 序列化，保证比较确定性。"""
    ok, value = _extract_json_key(text, key)
    if not ok:
        return False, None
    try:
        return True, _canonical(value)
    except (TypeError, ValueError):
        return False, None


def _extract_side_value(side: SideTree, logical: str, tag: str, extract, failures: List[str]
                        ) -> Any:
    """从一侧真实源码提取一个声明值；提取不到记录失败并返回 None（保守按迁移）。"""
    if not side.has(logical):
        failures.append(f"{side.label}:{tag}:source_missing")
        return None
    try:
        text = _read_text(side.physical(logical))
    except (OSError, UnicodeDecodeError):
        failures.append(f"{side.label}:{tag}:source_unreadable")
        return None
    ok, value = extract(text)
    if not ok:
        failures.append(f"{side.label}:{tag}:extraction_failed")
        return None
    return value


def _extract_migration_inputs(side: SideTree, rules: Dict[str, Any]
                              ) -> Tuple[Dict[str, Any], List[str]]:
    """提取迁移判定所需输入：数据库结构版本、契约声明版本、部署步骤。"""
    failures: List[str] = []
    inputs: Dict[str, Any] = {}

    database = rules["database"]
    db_source = str(database["schema_source"])
    db_constant = str(database["constant"])
    db_value = _extract_side_value(
        side, db_source, "database_schema_version",
        lambda text: _extract_python_constant(text, db_constant), failures)
    inputs["database_schema_version"] = {
        "source": db_source, side.label: db_value}

    contracts: Dict[str, Any] = {}
    for spec in _effective_contract_specs(rules):
        logical = spec["path"]
        value = _extract_side_value(
            side, logical, f"contract:{spec['label']}",
            lambda text, spec=spec: _extract_declaration(text, spec), failures)
        contracts[spec["label"]] = {"path": logical, side.label: value}
    inputs["contract_declarations"] = contracts

    steps_path, steps_key = _BUILTIN_DEPLOYMENT_STEPS
    steps = _extract_side_value(
        side, steps_path, "deployment_steps",
        lambda text: _normalized_json_key(text, steps_key), failures)
    inputs["deployment_steps"] = {"source": steps_path, "key": steps_key,
                                  side.label: steps}
    return inputs, failures


def _match_rule(logical: str, rules: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """按声明顺序取第一条匹配的路径规则；都不中返回 None（未识别归属）。"""
    for rule in rules["path_rules"]:
        paths = rule.get("paths") or []
        prefixes = rule.get("prefixes") or []
        suffixes = rule.get("suffixes") or []
        if logical in paths:
            return rule
        for prefix in prefixes:
            if logical.startswith(prefix):
                if suffixes and not any(logical.endswith(s) for s in suffixes):
                    continue
                return rule
    return None


def _version_bump_only(baseline: SideTree, candidate: SideTree, logical: str) -> bool:
    """仅版本号行变化（可重算验证的纯派生）。任一侧缺失或读失败返回 False。"""
    if not baseline.has(logical) or not candidate.has(logical):
        return False
    try:
        old_lines = _read_text(baseline.physical(logical)).splitlines()
        new_lines = _read_text(candidate.physical(logical)).splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    if len(old_lines) != len(new_lines):
        return False
    changed = 0
    for old, new in zip(old_lines, new_lines):
        if old == new:
            continue
        changed += 1
        if not (_VERSION_LINE_RE.match(old) and _VERSION_LINE_RE.match(new)):
            return False
    return changed > 0


def _declared_version(side: SideTree) -> Optional[str]:
    try:
        found = _DECLARED_VERSION_RE.findall(_read_text(side.physical("pyproject.toml")))
    except (OSError, UnicodeDecodeError):
        return None
    return found[0] if len(found) == 1 else None


def _release_bookkeeping(path: str, baseline: SideTree, candidate: SideTree) -> Optional[str]:
    """已修改的文件若只动了发布簿记，返回理由；否则 None。任何读不出、对不上的情况都返回 None。"""
    try:
        old_text = _read_text(baseline.physical(path))
        new_text = _read_text(candidate.physical(path))
    except (OSError, UnicodeDecodeError):
        return None
    old_version, new_version = _declared_version(baseline), _declared_version(candidate)
    if old_version and new_version and old_version != new_version \
            and new_text.replace(new_version, old_version) == old_text:
        return f"仅版本戳变化（{old_version} -> {new_version}），其余字节相同"
    if path in _BUILTIN_DERIVED_INDEXES:
        return "派生索引：由其它文件生成，构包时按重新生成结果校验"
    if path not in (_BUILTIN_DEPLOYMENT_STEPS[0], _BUILTIN_HISTORY_PATH):
        return None
    try:
        old, new = json.loads(old_text), json.loads(new_text)
    except ValueError:
        return None
    if not isinstance(old, dict) or not isinstance(new, dict):
        return None
    if path == _BUILTIN_HISTORY_PATH:
        old_records, new_records = old.pop("records", None), new.pop("records", None)
        if old == new and isinstance(old_records, list) and isinstance(new_records, list) \
                and len(new_records) > len(old_records) and new_records[:len(old_records)] == old_records:
            return "版本历史只追加了新记录，既有记录未动"
        return None
    old.pop(_BUILTIN_DEPLOYMENT_BASE_KEY, None)
    declared_base = new.pop(_BUILTIN_DEPLOYMENT_BASE_KEY, None)
    if old == new and declared_base == _sha256_file(baseline.physical("SOURCE_MANIFEST.json")):
        return "部署声明只更新了所基于的上一版清单哈希，且与上一版实际清单一致"
    return None


def _tier_change(path: str, change_type: str, baseline: SideTree, candidate: SideTree,
                 rules: Dict[str, Any], protected: List[str], baseline_refs: List[str],
                 candidate_refs: List[str], db_values: Dict[str, Any],
                 deployment_values: Dict[str, Any],
                 contract_values: Dict[str, Dict[str, Any]],
                 gaps: List[str]) -> Tuple[str, List[str], bool]:
    """给一个具体路径定档：路径规则起步，再叠加各迁移条件与固定底线。"""
    reasons: List[str] = []
    known = True
    floor_exempt = False
    rule = _match_rule(path, rules)
    if rule is None:
        known = False
        tier = "patch"
        reasons.append("未识别归属（无路径规则匹配）")
    else:
        tier = rule["tier"]
        reasons.append(f"路径规则 {rule['id']}: {rule.get('reason', '')}")
        conditional = rule.get("conditional")
        if conditional == "version_bump_only":
            if path in _BUILTIN_VERSION_BUMP_PATHS and change_type == _CHANGE_MODIFIED \
                    and _version_bump_only(baseline, candidate, path):
                tier = rule.get("tier_if_condition_met", "patch")
                floor_exempt = True
                reasons.append("仅版本号行变化，按可重算纯派生处理")
            else:
                reasons.append("非纯版本号变化，按路径规则定档")
        elif conditional == "derived_recomputable":
            # 源码布局下唯一可重算的派生清单是 SOURCE_MANIFEST.json：建侧阶段
            # 已按真实文件逐项实算校验，不一致会先失败关闭，走不到这里。
            reasons.append("SOURCE_MANIFEST.json 已按真实文件重算校验一致")
        if change_type == _CHANGE_MODIFIED and tier != "patch":
            bookkeeping = _release_bookkeeping(path, baseline, candidate)
            if bookkeeping is not None:
                tier = "patch"
                floor_exempt = True
                reasons.append(bookkeeping)

    if path in protected:
        if _TIER_RANK[tier] < _TIER_RANK["regular"]:
            tier = "regular"
        reasons.append("受保护底线测试文件（两侧 guardrail_marks 并集），或夹具与被其它测试共用的代码")

    if path in (rules.get("_marks_paths") or [rules["guardrail_marks"]["path"]]):
        if _TIER_RANK[tier] < _TIER_RANK["regular"]:
            tier = "regular"
        removed = sorted(set(baseline_refs) - set(candidate_refs))
        added = sorted(set(candidate_refs) - set(baseline_refs))
        if path != _BUILTIN_GUARDRAIL_MARKS_PATH:
            reasons.append(f"规则把底线标记清单重指到 {path}（内置清单仍在并集内，保守）")
        if removed:
            reasons.append("移除底线标记: " + ", ".join(removed))
        if added:
            reasons.append("新增底线标记: " + ", ".join(added))

    spec = (rules.get("_contracts_by_path") or {}).get(path)
    if spec is not None:
        values = contract_values.get(path, {})
        old_value, new_value = values.get("baseline"), values.get("candidate")
        if old_value is None or new_value is None:
            tier = "migration"
            gaps.append(f"无法从真实源码提取或比对契约声明 {spec['label']}"
                        f"（{path} {change_type}），不能排除既有契约变化")
            reasons.append(f"契约声明不可比对（{spec['label']}），保守按 migration")
        elif old_value != new_value:
            tier = "migration"
            reasons.append(f"契约声明版本变化 {spec['label']}: {old_value} -> {new_value}")
        else:
            reasons.append(f"契约声明 {spec['label']} 两侧一致（{old_value}）")

    db_path = rules["database"]["schema_source"]
    db_constant = rules["database"]["constant"]
    if path == db_path and change_type in (_CHANGE_MODIFIED, _CHANGE_ADDED,
                                           _CHANGE_DELETED):
        if db_values.get("baseline") is None or db_values.get("candidate") is None:
            tier = "migration"
            gaps.append(f"无法从 {db_path} 提取 {db_constant}，不能排除数据库结构变化")
            reasons.append("数据库结构版本不可提取，保守按 migration")
        elif db_values["baseline"] != db_values["candidate"]:
            tier = "migration"
            reasons.append(
                f"数据库结构版本变化: {db_values['baseline']} -> {db_values['candidate']}")
        else:
            reasons.append(f"数据库结构版本两侧一致（{db_values['baseline']}）")

    steps_path, _steps_key = _BUILTIN_DEPLOYMENT_STEPS
    if path == steps_path and change_type in (_CHANGE_MODIFIED, _CHANGE_ADDED,
                                              _CHANGE_DELETED):
        old_steps, new_steps = deployment_values.get("baseline"), \
            deployment_values.get("candidate")
        if old_steps is None or new_steps is None:
            tier = "migration"
            gaps.append(f"无法读取或规范化 {steps_path} 的部署步骤，不能排除迁移步骤变化")
            reasons.append("部署步骤不可比对，保守按 migration")
        elif old_steps != new_steps:
            tier = "migration"
            reasons.append("部署步骤内容变化（docs/DEPLOYMENT.json steps）")
        else:
            reasons.append("部署步骤两侧一致")

    floor = _builtin_tier_floor(path)
    if not floor_exempt and _TIER_RANK[tier] < _TIER_RANK[floor]:
        tier = floor
        reasons.append(f"固定底线（实现内置，不随规则降低）：{path} 至少 {floor}")
    return tier, reasons, known


def _apply_human_raise(human_raise: Any, calculated_tier: str
                       ) -> Tuple[str, Dict[str, Any], Optional[str]]:
    """处理人的明确上调。返回 (effective_tier, 记录, 问题或None)。

    仅接受带用户明确授权依据的结构化记录；普通字符串或调用者参数不构成
    人的授权证据。只能上调，降档请求拒绝。
    """
    record: Dict[str, Any] = {"applied": False, "requested": human_raise is not None}
    if human_raise is None:
        record["reason"] = "未提出上调"
        return calculated_tier, record, None
    if isinstance(human_raise, str):
        return calculated_tier, {**record, "reason": "普通字符串不构成人的明确授权证据"}, \
            "human_raise_rejected: 普通字符串不是人的授权证据"
    if not isinstance(human_raise, dict):
        return calculated_tier, {**record,
                                 "reason": f"不支持的类型: {type(human_raise).__name__}"}, \
            "human_raise_rejected: 类型不支持"
    authorized_by = human_raise.get("authorized_by")
    basis = human_raise.get("basis")
    target_tier = human_raise.get("target_tier")
    if authorized_by != "user":
        return calculated_tier, {**record, "reason": "authorized_by 必须为 user"}, \
            "human_raise_rejected: 缺少用户授权身份"
    if not isinstance(basis, str) or not basis.strip():
        return calculated_tier, {**record, "reason": "basis 必须是非空的用户明确授权依据"}, \
            "human_raise_rejected: 缺少授权依据"
    if target_tier not in TIERS:
        return calculated_tier, {**record, "reason": "target_tier 必须是三档之一"}, \
            "human_raise_rejected: target_tier 非法"
    if _TIER_RANK[target_tier] < _TIER_RANK[calculated_tier]:
        record.update({"reason": "只允许上调，降档请求拒绝",
                       "requested_tier": target_tier, "calculated_tier": calculated_tier})
        return calculated_tier, record, (
            f"human_raise_rejected: 降档请求 {target_tier} 低于重算档 {calculated_tier}")
    for optional in ("recorded_at", "reference"):
        if optional in human_raise:
            record[optional] = human_raise[optional]
    if _TIER_RANK[target_tier] == _TIER_RANK[calculated_tier]:
        record.update({"reason": "目标档与重算档一致，无需调整", "authorized_by": authorized_by,
                       "basis": basis, "target_tier": target_tier})
        return calculated_tier, record, None
    record.update({"applied": True, "authorized_by": authorized_by, "basis": basis,
                   "target_tier": target_tier, "raised_from": calculated_tier,
                   "limitation": "工具无法独立验证人的授权，仅按调用方声明记录"})
    return target_tier, record, None


def _build_fingerprints(baseline: SideTree, candidate: SideTree, rules_sha: str,
                        marks: Dict[str, Any], migration_inputs: Dict[str, Any],
                        release_declaration_check: Dict[str, Any]) -> Dict[str, Any]:
    def side_fp(side: SideTree) -> Dict[str, Any]:
        manifest_rel = side.layout_cfg["manifest"]["path"]
        return {"layout": side.layout, "manifest": manifest_rel,
                "manifest_sha256": side.sha(manifest_rel) if side.has(manifest_rel) else None,
                "compared_files": len(side.files), "side_digest": side.side_digest()}

    payload = {
        "schema_version": SCHEMA_VERSION,
        "rules_sha256": rules_sha,
        "baseline": side_fp(baseline),
        "candidate": side_fp(candidate),
        "guardrail_marks": marks,
        "migration_inputs": migration_inputs,
        "release_declaration_check": release_declaration_check,
    }
    payload["policy_fingerprint"] = _sha256_bytes(_canonical(payload).encode("utf-8"))
    return payload


def _coverage_statement(side: SideTree, label: str) -> Dict[str, Any]:
    return {
        "label": label,
        "layout": side.layout,
        "root": str(side.root),
        "manifest": side.layout_cfg["manifest"]["path"],
        "compared_files": len(side.files),
        "top_level": side.top_level,
        "out_of_scope_paths": sorted(side.out_of_scope),
        "scope_note": ("源码布局：比较 build_release 受控复制集合（artifacts/、skills/、knowledge/ "
                       "按存在才复制，SOURCE_MANIFEST.json 必在），SOURCE_MANIFEST.json 已按真实文件重算验证"),
    }



def _check_release_declaration(candidate: SideTree, rules: Dict[str, Any],
                               source_schema_version: Any) -> Dict[str, Any]:
    """RELEASE.json 只记录不采信：自报数据库结构版本与真实源码不符时记为不一致。"""
    cfg = rules.get("release_declaration") or {}
    logical = cfg.get("path")
    if not logical or not candidate.has(logical):
        return {"present": False, "consistent": True}
    doc = _parse_json_file(candidate.physical(logical), "release_declaration")
    if not isinstance(doc, dict):
        return {"present": True, "consistent": False, "reason": "not_object"}
    declared = doc.get(cfg.get("database_schema_key", "database_schema_version"))
    consistent = declared == source_schema_version
    result: Dict[str, Any] = {"present": True, "consistent": consistent,
                              "declared": declared,
                              "source": source_schema_version}
    if not consistent:
        result["reason"] = "release_declaration_schema_mismatch"
    return result


def _summary_reasons(changes: List[Dict[str, Any]], calculated_tier: str,
                 gaps: List[str]) -> List[str]:
    """把逐路径档位与缺口收敛成一段人可读的结论。"""
    reasons = [f"calculated_tier={calculated_tier}，共 {len(changes)} 处变更"]
    counts: Dict[str, int] = {}
    for entry in changes:
        counts[entry["tier"]] = counts.get(entry["tier"], 0) + 1
    if counts:
        reasons.append("变更档位分布: " + ", ".join(
            f"{tier}={counts[tier]}" for tier in TIERS if tier in counts))
    for gap in gaps:
        reasons.append(f"gap: {gap}")
    return reasons

def classify_release(baseline_root: Any, candidate_root: Any, *,
                     rules_path: Any = None, human_raise: Any = None) -> Dict[str, Any]:
    """给一次发布变更定档。只接受源码布局，规则默认取自基线（上一版）。"""
    try:
        baseline_path = _resolve_root(baseline_root, "baseline")
        candidate_path = _resolve_root(candidate_root, "candidate")
        if baseline_path == candidate_path:
            raise PolicyError("roots_identical: 基线与候选不得为同一目录")
        rules, rules_sha = _load_rules(rules_path, baseline_path)
        _detect_layout(baseline_path, rules)
        _detect_layout(candidate_path, rules)
        baseline = _build_source_side(baseline_path, rules, "baseline")
        candidate = _build_source_side(candidate_path, rules, "candidate")

        contracts_by_path: Dict[str, Dict[str, Any]] = {}
        for spec in _effective_contract_specs(rules):
            contracts_by_path.setdefault(spec["path"], spec)
        marks_paths = _effective_marks_paths(rules)
        rules_view = dict(rules)
        rules_view["_contracts_by_path"] = contracts_by_path
        rules_view["_marks_paths"] = marks_paths

        protected_b, baseline_refs, marks_docs_b = _load_guardrail_marks_all(
            baseline, rules, "baseline")
        protected_c, candidate_refs, marks_docs_c = _load_guardrail_marks_all(
            candidate, rules, "candidate")
        marked = sorted(set(protected_b) | set(protected_c))
        protected = sorted(set(marked) | set(_test_support_files(baseline))
                           | set(_test_support_files(candidate)))

        inputs_b, failures_b = _extract_migration_inputs(baseline, rules)
        inputs_c, failures_c = _extract_migration_inputs(candidate, rules)
        db_values = {"baseline": inputs_b["database_schema_version"].get("baseline"),
                     "candidate": inputs_c["database_schema_version"].get("candidate")}
        deployment_values = {"baseline": inputs_b["deployment_steps"].get("baseline"),
                             "candidate": inputs_c["deployment_steps"].get("candidate")}
        contract_values: Dict[str, Dict[str, Any]] = {}
        for spec in _effective_contract_specs(rules):
            label = spec["label"]
            contract_values[spec["path"]] = {
                "baseline": inputs_b["contract_declarations"].get(label, {}).get("baseline"),
                "candidate": inputs_c["contract_declarations"].get(label, {}).get("candidate")}
        extraction_failures = sorted(set(failures_b) | set(failures_c))
        release_check = _check_release_declaration(candidate, rules, db_values["candidate"])

        gaps: List[str] = []
        changes: List[Dict[str, Any]] = []
        for logical in sorted(set(baseline.files) | set(candidate.files)):
            if logical in baseline.files and logical in candidate.files:
                if baseline.sha(logical) == candidate.sha(logical):
                    continue
                change_type = _CHANGE_MODIFIED
            elif logical in candidate.files:
                change_type = _CHANGE_ADDED
            else:
                change_type = _CHANGE_DELETED
            tier, reasons, known = _tier_change(
                logical, change_type, baseline, candidate, rules_view, protected,
                baseline_refs, candidate_refs, db_values, deployment_values,
                contract_values, gaps)
            changes.append({
                "path": logical,
                "change_type": change_type,
                "old_sha256": baseline.sha(logical) if baseline.has(logical) else None,
                "new_sha256": candidate.sha(logical) if candidate.has(logical) else None,
                "tier": tier,
                "reasons": reasons,
                "attribution_known": known,
            })

        unknown = [entry for entry in changes if not entry["attribution_known"]]
        if unknown:
            max_known = "patch"
            for entry in changes:
                if entry["attribution_known"]:
                    max_known = _max_tier(max_known, entry["tier"])
            raised = _raise_tier(max_known)
            for entry in unknown:
                if _TIER_RANK[entry["tier"]] < _TIER_RANK[raised]:
                    entry["tier"] = raised
                    entry["reasons"].append(
                        f"未识别归属，按已知最高档 {max_known} 再升一档到 {raised}（保守）")

        calculated_tier = "patch"
        for entry in changes:
            calculated_tier = _max_tier(calculated_tier, entry["tier"])

        if baseline.top_level != candidate.top_level:
            added_tops = sorted(set(candidate.top_level) - set(baseline.top_level))
            removed_tops = sorted(set(baseline.top_level) - set(candidate.top_level))
            detail = []
            if added_tops:
                detail.append("新增 " + "、".join(added_tops))
            if removed_tops:
                detail.append("移除 " + "、".join(removed_tops))
            gaps.append("受管顶层结构段（目录或根文件集合）有增减：" + "；".join(detail)
                        + "，不能排除既有目录契约变化")
            calculated_tier = "migration"

        if release_check.get("present") and not release_check.get("consistent", True):
            calculated_tier = "migration"
            gaps.append("RELEASE.json 自报数据库结构版本与真实源码不一致，保守按 migration")

        problems: List[str] = []
        if candidate.out_of_scope:
            problems.append("candidate_out_of_declared_scope: "
                            + ", ".join(sorted(candidate.out_of_scope)))
        if candidate.has(_RULES_LOGICAL):
            applied = _canonical(rules)
            carried = _canonical_json(_read_text(candidate.physical(_RULES_LOGICAL)))
            if carried != applied:
                problems.append(
                    "candidate_rules_differ_from_applied_rules: 候选自带的 "
                    f"{_RULES_LOGICAL} 与实际采用的规则不一致（本版规则来自基线，"
                    "候选规则不影响档位）")

        effective_tier, human_record, human_problem = _apply_human_raise(
            human_raise, calculated_tier)
        if human_problem:
            problems.append(human_problem)

        marks = {
            "paths": marks_paths,
            "baseline_sha256": marks_docs_b.get(_BUILTIN_GUARDRAIL_MARKS_PATH, {}).get("sha256"),
            "candidate_sha256": marks_docs_c.get(_BUILTIN_GUARDRAIL_MARKS_PATH, {}).get("sha256"),
            "protected_test_files": marked,
            "documents": {"baseline": marks_docs_b, "candidate": marks_docs_c},
        }
        migration_inputs = {
            "database_schema_version": {"source": rules["database"]["schema_source"],
                                        **db_values},
            "deployment_steps": {"source": _BUILTIN_DEPLOYMENT_STEPS[0],
                                 "key": _BUILTIN_DEPLOYMENT_STEPS[1], **deployment_values},
            "contract_declarations": contract_values,
            "extraction_failures": extraction_failures,
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "calculated_tier": calculated_tier,
            "effective_tier": effective_tier,
            "changes": changes,
            "reasons": _summary_reasons(changes, calculated_tier, gaps),
            "fingerprints": _build_fingerprints(
                baseline, candidate, rules_sha, marks, migration_inputs, release_check),
            "problems": problems,
            "gaps": gaps,
            "human_raise": human_record,
            "coverage": {
                "baseline": _coverage_statement(baseline, "baseline"),
                "candidate": _coverage_statement(candidate, "candidate"),
            },
        }
    except PolicyError as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "calculated_tier": None,
            "effective_tier": None,
            "changes": [],
            "reasons": [],
            "fingerprints": {},
            "problems": [str(exc)],
            "gaps": [],
            "human_raise": {},
            "coverage": {},
        }


