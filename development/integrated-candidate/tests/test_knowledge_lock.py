"""正确页面与相邻错误页面：知识误引用回归测试。

借鉴 clone_25（大量近重复克隆页）与 lock_vs_neighbor（权威锁页 vs 措辞相近的相邻页）的思路，
证明系统不会因为页面相似而引用错误规则。全部基于 tests/fixtures/knowledge_lock/ 的离线语料，
不读取真实私人 Vault，不使用真实模型、网络或动态 embedding。

断言按三层区分：
  L1 检索命中    —— locator（KnowLP）返回的候选，或价值文件索引里被匹配到的条目
  L2 权威源验证  —— 命中之后能否在允许边界内回读到 status: active 的原文
  L3 上下文准入  —— 只有通过 L2 的内容才允许进入 Agent context
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.knowledge import KnowledgeCatalog
from xiaoba_agentos.memory import MemoryBridge

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "knowledge_lock"
CLONE_COUNT = 25

LOCK_TASK_ID = "11111111-1111-1111-1111-111111111111"
NEIGHBOR_TASK_ID = "22222222-2222-2222-2222-222222222222"
ALIAS_TASK_ID = "33333333-3333-3333-3333-333333333333"
ARCHIVED_TASK_ID = "44444444-4444-4444-4444-444444444444"

LOCK_PATH = "70_复利规则/经验/lock.md"
NEIGHBOR_PATH = "70_复利规则/经验/neighbor.md"
ALIAS_PATH = "70_复利规则/经验/alias.md"
BAD_ID_PATH = "70_复利规则/经验/bad_source_task_id.md"
ARCHIVED_PATH = "99_归档/lock_v2024.md"
SUPERSEDED_PATH = "99_归档/lock_pre_v14.md"

LOCK_MARKER = "LOCK_ACTIVE_MARKER"
NEIGHBOR_MARKER = "NEIGHBOR_WRONG_MARKER"
ALIAS_MARKER = "ALIAS_MARKER"
ARCHIVED_MARKER = "ARCHIVED_MARKER"
SUPERSEDED_MARKER = "SUPERSEDED_MARKER"
OUTSIDE_MARKER = "OUTSIDE_OF_VAULT_MARKER"


class _PublishedSourceStore:
    """Minimal lifecycle stub for Vault-lock tests.

    These tests exercise source identity and page admission.  Marking every
    UUID-backed fixture as published keeps that concern explicit while still
    exercising the production database gate.
    """

    def get_memory_candidate(self, task_id: str) -> dict:
        return {"task_id": task_id, "status": "published"}

SHARED_PHRASE = "先追问再执行"
SYNONYM_QUERY = "拿不准就先问一下"

INDEX_BUILDER_SPEC = importlib.util.spec_from_file_location(
    "knowledge_lock_build_knowlp_index",
    Path(__file__).resolve().parents[1] / "scripts" / "build_knowlp_index.py",
)
INDEX_BUILDER = importlib.util.module_from_spec(INDEX_BUILDER_SPEC)
assert INDEX_BUILDER_SPEC and INDEX_BUILDER_SPEC.loader
INDEX_BUILDER_SPEC.loader.exec_module(INDEX_BUILDER)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _expand_clones(vault: Path, count: int = CLONE_COUNT) -> dict:
    """把 clone_template.md 展开成 count 份近重复页面，并登记到 root.md。"""
    experience_dir = LOCK_PATH.rsplit("/", 1)[0]
    template = vault / experience_dir / "clone_template.md"
    text = template.read_text(encoding="utf-8")
    template.unlink()
    clones = {}
    for index in range(count):
        relative = f"{experience_dir}/clone_{index:02d}.md"
        target = vault / relative
        rendered = text.replace("{index}", f"{index:02d}").replace(
            "{uuid}", f"cccccccc-{index:02d}-4444-8888-00000000000{index % 10}"
        )
        target.write_text(rendered, encoding="utf-8")
        clones[relative] = target
    root = vault / "root.md"
    root.write_text(
        root.read_text(encoding="utf-8")
        + "".join(f"- [[{relative.rsplit('.md', 1)[0]}]]\n" for relative in sorted(clones)),
        encoding="utf-8",
    )
    return clones


def _materialize(dest: Path, clones: int = 0) -> dict:
    """把语料复制到临时目录，返回各层需要的路径与配置。

    路径统一 resolve()：macOS 的 /var → /private/var 会让索引声明路径与断言路径字面不同，
    而 verify() 的重复路径判定是字面的。
    """
    corpus = (dest / "lock").resolve()
    shutil.copytree(FIXTURE_ROOT, corpus)
    vault = corpus / "vault"
    clone_paths = _expand_clones(vault, clones) if clones else {}
    return {
        "corpus": corpus,
        "vault": vault,
        "clones": clone_paths,
        "index": corpus / "knowledge" / "index.json",
    }


def _catalog_config(layout: dict) -> SimpleNamespace:
    return SimpleNamespace(
        knowledge_index=layout["index"],
        knowledge_allowed_roots=[layout["corpus"].resolve()],
    )


def _write_catalog_index(layout: dict, entries: list) -> None:
    target = layout["index"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {"schema_version": 1, "release": "v1.4.2", "entries": entries},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _index_entries_from_template(layout: dict, extra: list = ()) -> list:
    """按 scripts/create_knowledge_index.py 的形状生成条目，但保留模板声明的 status。"""
    template = json.loads(
        (layout["corpus"] / "knowledge_index_template.json").read_text(encoding="utf-8")
    )
    entries = []
    for source in template["entries"]:
        entry = {key: value for key, value in source.items() if key != "path_ref"}
        path = (layout["corpus"] / source["path_ref"]).resolve()
        entry["path"] = str(path)
        entry["sha256"] = _file_sha256(path)
        entry["bytes"] = path.stat().st_size
        entries.append(entry)
    return entries + list(extra)


def _memory_config(root: Path, vault: Path) -> AppConfig:
    raw = {
        "schema_version": 1,
        "runtime_dir": str(root / "runtime"),
        "project_dir": str(root),
        "worker": {
            "poll_seconds": 1, "lease_seconds": 60, "max_attempts": 2,
            "retry_base_seconds": 1, "retry_max_seconds": 2, "parallelism": 1,
            "minimum_successful_agents": 1,
        },
        "memory": {
            "knowlp_enabled": True,
            "knowlp_python": "/python",
            "knowlp_module": "/knowlp.py",
            "knowlp_graph_dir": str(root / "graph"),
            "obsidian_vault": str(vault),
            "knowlp_vault": str(vault),
            "obsidian_subdir": "70_复利规则/经验",
            "catalog_note": "70_复利规则/经验索引.md",
            "publish_enabled": False,
        },
        "integrations": {},
        "agents": [{"name": "mock", "type": "mock", "enabled": True}],
        "evolution": {"minimum_samples": 1, "promotion_margin": 0.05, "latency_penalty": 0.01},
    }
    path = root / "config.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return AppConfig.load(path)


def _locator_payload(hits: list) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, json.dumps({"hits": hits}, ensure_ascii=False), "")


class KnowledgeCatalogLockTest(unittest.TestCase):
    """价值文件索引层：条目身份是 (id, path, sha256)，标题只是展示字段。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.layout = _materialize(self.root)
        self.config = _catalog_config(self.layout)

    def tearDown(self):
        self.tmp.cleanup()

    def _clean_index(self, extra: list = ()) -> None:
        _write_catalog_index(self.layout, _index_entries_from_template(self.layout, extra))

    def _lock_path(self) -> Path:
        return self.layout["corpus"] / "vault" / LOCK_PATH

    def test_correct_active_page_is_the_hit(self):
        self._clean_index()
        receipt = KnowledgeCatalog(self.config).retrieve_with_receipt("LOCK_ONLY_TOKEN")
        self.assertEqual(
            "canonical_match", receipt["retrieval_status"], f"索引已篡改：{receipt.get('problems')}"
        )
        self.assertEqual(
            ["lock-clarify-first"],
            [hit["id"] for hit in receipt["hits"]],
            "正确页面必须是唯一命中",
        )
        hit = receipt["hits"][0]
        self.assertEqual(str(self._lock_path()), hit["path"], "命中必须绑定到锁页自己的路径")
        self.assertEqual(_file_sha256(self._lock_path()), hit["source_sha256"])
        self.assertTrue(hit["is_active"], "active 条目的 is_active 必须为真")

    def test_adjacent_page_with_the_same_title_never_replaces_the_correct_page(self):
        self._clean_index()
        catalog = KnowledgeCatalog(self.config)
        lock_hash = _file_sha256(self._lock_path())
        neighbor_hash = _file_sha256(self.layout["corpus"] / "vault" / NEIGHBOR_PATH)
        alias_hash = _file_sha256(self.layout["corpus"] / "vault" / ALIAS_PATH)

        ambiguous = catalog.retrieve_with_receipt("需求不清时必须先追问")
        binding = {hit["id"]: (hit["path"], hit["source_sha256"]) for hit in ambiguous["hits"]}
        self.assertEqual(
            {
                "lock-clarify-first": (str(self._lock_path()), lock_hash),
                "neighbor-clarify-first": (
                    str(self.layout["corpus"] / "vault" / NEIGHBOR_PATH),
                    neighbor_hash,
                ),
                "alias-clarify-first": (
                    str(self.layout["corpus"] / "vault" / ALIAS_PATH),
                    alias_hash,
                ),
            },
            binding,
            "同名/别名条目必须各自绑定到自己的路径与哈希；相邻页不得接管锁页 id",
        )

        targeted = catalog.retrieve_with_receipt("NEIGHBOR_ONLY_TOKEN")
        self.assertEqual(["neighbor-clarify-first"], [hit["id"] for hit in targeted["hits"]])
        self.assertEqual(
            neighbor_hash,
            targeted["hits"][0]["source_sha256"],
            "按相邻页专属标记查询时，返回的哈希必须是相邻页自己的",
        )

    def test_archived_entries_never_enter_active_retrieval(self):
        self._clean_index()
        catalog = KnowledgeCatalog(self.config)
        query_hits = {
            query: [hit["id"] for hit in catalog.retrieve_with_receipt(query)["hits"]]
            for query in (SHARED_PHRASE, "先追问再执行 澄清优先", "需求不清时必须先追问")
        }
        for query, ids in query_hits.items():
            with self.subTest(query=query):
                self.assertNotIn(
                    "archived-clarify-first-2024",
                    ids,
                    "索引条目 status=archived，却被活动检索命中",
                )
                self.assertNotIn(
                    "superseded-clarify-first-pre-v14",
                    ids,
                    "索引条目 status=superseded，却被活动检索命中",
                )
        shared = catalog.retrieve_with_receipt(SHARED_PHRASE)
        self.assertEqual(
            {"lock-clarify-first", "neighbor-clarify-first"},
            {hit["id"] for hit in shared["hits"]},
            "锁短语应当命中锁页与相邻页，且绝不命中归档/被取代条目",
        )
        self.assertEqual("canonical_match", shared["retrieval_status"])
        self.assertEqual(
            {"lock-clarify-first", "neighbor-clarify-first", "alias-clarify-first"},
            {
                hit["id"]
                for hit in catalog.retrieve_with_receipt("需求不清时必须先追问")["hits"]
            },
            "同标题查询的活动命中集合发生变化",
        )
        leaked = json.dumps(shared, ensure_ascii=False)
        for token in ("ARCHIVED_ONLY_TOKEN", "SUPERSEDED_ONLY_TOKEN"):
            self.assertNotIn(token, leaked, f"归档条目的摘要泄漏进检索回执：{token}")

    def test_tampered_adjacent_page_degrades_the_whole_catalog(self):
        self._clean_index()
        catalog = KnowledgeCatalog(self.config)
        self.assertEqual("canonical_match", catalog.retrieve_with_receipt("LOCK_ONLY_TOKEN")["retrieval_status"])

        neighbor = self.layout["corpus"] / "vault" / NEIGHBOR_PATH
        neighbor.write_text(
            neighbor.read_text(encoding="utf-8") + "\n未同步索引的改写\n", encoding="utf-8"
        )
        receipt = catalog.retrieve_with_receipt("LOCK_ONLY_TOKEN")
        self.assertEqual("degraded", receipt["retrieval_status"], "相邻页哈希漂移后锁页仍在服务")
        self.assertEqual([], receipt["hits"])
        self.assertIn(
            "价值文件哈希不匹配：neighbor-clarify-first",
            "；".join(receipt["problems"]),
            "降级原因必须是相邻页哈希不匹配",
        )

    def test_alias_entry_cannot_impersonate_the_canonical_lock(self):
        self._clean_index()
        catalog = KnowledgeCatalog(self.config)
        alias_path = self.layout["corpus"] / "vault" / ALIAS_PATH
        receipt = catalog.retrieve_with_receipt("ALIAS_ONLY_TOKEN")
        self.assertEqual(["alias-clarify-first"], [hit["id"] for hit in receipt["hits"]])
        hit = receipt["hits"][0]
        self.assertEqual(str(alias_path), hit["path"], "别名命中必须落在别名页面自己的路径上")
        self.assertEqual(_file_sha256(alias_path), hit["source_sha256"])
        self.assertEqual("需求不清时必须先追问（别名）", hit["title"])
        self.assertNotIn(
            "LOCK_ONLY_TOKEN",
            json.dumps(hit, ensure_ascii=False),
            "别名条目的 canonical id 与摘要不得继承锁页",
        )

    def test_twenty_five_clones_keep_their_own_identity_beside_the_lock(self):
        clones = _expand_clones(self.layout["vault"], CLONE_COUNT)
        lock_entry = next(
            entry
            for entry in _index_entries_from_template(self.layout)
            if entry["id"] == "lock-clarify-first"
        )
        _write_catalog_index(
            self.layout,
            [lock_entry]
            + [
                {
                    "id": f"clone-{index:02d}",
                    "title": lock_entry["title"],
                    "category": "clarify-rule",
                    "index_mode": "semantic_source",
                    "path": str(clones[relative].resolve()),
                    "summary": f"澄清规则的克隆副本 {index:02d}，与锁页同标题。",
                    "sha256": _file_sha256(clones[relative]),
                    "bytes": clones[relative].stat().st_size,
                    "status": "active",
                }
                for index, relative in enumerate(sorted(clones))
            ],
        )

        receipt = KnowledgeCatalog(self.config).retrieve_with_receipt("需求不清时必须先追问")
        self.assertEqual("canonical_match", receipt["retrieval_status"])
        self.assertEqual(
            1 + CLONE_COUNT,
            len(receipt["hits"]),
            "25 份克隆必须各自独立命中，既不能被合并，也不能被锁页吞掉",
        )
        hashes = [hit["source_sha256"] for hit in receipt["hits"]]
        self.assertEqual(len(hashes), len(set(hashes)), "克隆页之间出现了相同哈希，说明身份绑定被复用")
        by_id = {hit["id"]: hit for hit in receipt["hits"]}
        self.assertEqual(
            _file_sha256(self._lock_path()),
            by_id["lock-clarify-first"]["source_sha256"],
            "25 份相似页面把锁页哈希替换掉了",
        )
        for index in range(CLONE_COUNT):
            self.assertEqual(
                _file_sha256(clones[sorted(clones)[index]]),
                by_id[f"clone-{index:02d}"]["source_sha256"],
                f"clone-{index:02d} 绑定到了别的内容",
            )

    def test_clone_reusing_the_lock_path_or_id_fails_closed(self):
        lock_path = str(self._lock_path())
        self._clean_index(
            [
                {
                    "id": "clone-shadowing-lock-path",
                    "title": "需求不清时必须先追问",
                    "category": "clarify-rule",
                    "index_mode": "semantic_source",
                    "path": lock_path,
                    "summary": "克隆复用锁页路径。SHADOW_PATH_TOKEN",
                    "sha256": _file_sha256(self._lock_path()),
                    "bytes": self._lock_path().stat().st_size,
                    "status": "active",
                }
            ]
        )
        catalog = KnowledgeCatalog(self.config)
        receipt = catalog.retrieve_with_receipt("SHADOW_PATH_TOKEN")
        self.assertEqual("degraded", receipt["retrieval_status"])
        self.assertEqual([], receipt["hits"])
        self.assertIn("价值文件路径为空或重复", "；".join(receipt["problems"]))

        self._clean_index(
            [
                {
                    "id": "lock-clarify-first",
                    "title": "需求不清时必须先追问",
                    "category": "clarify-rule",
                    "index_mode": "semantic_source",
                    "path": str(self.layout["corpus"] / "vault" / NEIGHBOR_PATH),
                    "summary": "克隆复用锁页 canonical id。SHADOW_ID_TOKEN",
                    "sha256": _file_sha256(self.layout["corpus"] / "vault" / NEIGHBOR_PATH),
                    "bytes": (self.layout["corpus"] / "vault" / NEIGHBOR_PATH).stat().st_size,
                    "status": "active",
                }
            ]
        )
        receipt = catalog.retrieve_with_receipt("SHADOW_ID_TOKEN")
        self.assertEqual("degraded", receipt["retrieval_status"])
        self.assertIn("价值文件 id 为空或重复", "；".join(receipt["problems"]))

    def test_short_and_chinese_queries_do_not_fall_back_to_a_similar_page(self):
        self._clean_index()
        catalog = KnowledgeCatalog(self.config)
        cases = {
            "锁": ["lock-clarify-first"],
            "别名": ["alias-clarify-first"],
            "归档": [],
            "相邻": ["neighbor-clarify-first"],
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                receipt = catalog.retrieve_with_receipt(query)
                self.assertEqual(expected, [hit["id"] for hit in receipt["hits"]])

    def test_synonym_query_is_a_miss_not_a_best_guess(self):
        self._clean_index()
        receipt = KnowledgeCatalog(self.config).retrieve_with_receipt(SYNONYM_QUERY)
        self.assertEqual("miss", receipt["retrieval_status"], "同义词查询被降级成了别的东西")
        self.assertEqual([], receipt["hits"], "没有字面命中时不得猜测一个相似页面")
        self.assertNotIn("problems", receipt, "miss 与 degraded 必须可区分：miss 不应带降级原因")

    def test_missing_declared_source_fails_closed(self):
        entries = _index_entries_from_template(self.layout)
        ghost = self.layout["corpus"] / "vault" / "99_归档" / "removed.md"
        ghost.write_text("临时存在\n", encoding="utf-8")
        entries.append(
            {
                "id": "removed-page",
                "title": "需求不清时必须先追问（已删除）",
                "category": "clarify-rule",
                "index_mode": "semantic_source",
                "path": str(ghost),
                "summary": "文件已消失。MISSING_FILE_TOKEN",
                "sha256": _file_sha256(ghost),
                "bytes": ghost.stat().st_size,
                "status": "active",
            }
        )
        _write_catalog_index(self.layout, entries)
        ghost.unlink()
        catalog = KnowledgeCatalog(self.config)
        receipt = catalog.retrieve_with_receipt("MISSING_FILE_TOKEN")
        self.assertEqual("degraded", receipt["retrieval_status"])
        self.assertIn("价值文件缺失或为符号链接：removed-page", "；".join(receipt["problems"]))
        lock = catalog.retrieve_with_receipt("LOCK_ONLY_TOKEN")
        self.assertEqual("degraded", lock["retrieval_status"], "源文件缺失时必须整体停止而不是继续服务")
        self.assertEqual([], lock["hits"])

    def test_path_outside_allowed_roots_fails_closed(self):
        outside = self.root / "outside" / "other.md"
        outside.parent.mkdir()
        outside.write_text("# 外部页面\n\n越界条目内容\n", encoding="utf-8")
        _write_catalog_index(
            self.layout,
            _index_entries_from_template(self.layout)
            + [
                {
                    "id": "outside-root",
                    "title": "需求不清时必须先追问（越界）",
                    "category": "clarify-rule",
                    "index_mode": "semantic_source",
                    "path": str(outside),
                    "summary": "路径在允许根目录之外。OUTSIDE_ROOT_TOKEN",
                    "sha256": _file_sha256(outside),
                    "bytes": outside.stat().st_size,
                    "status": "active",
                }
            ],
        )
        receipt = KnowledgeCatalog(self.config).retrieve_with_receipt("OUTSIDE_ROOT_TOKEN")
        self.assertEqual("degraded", receipt["retrieval_status"])
        self.assertIn("价值文件越过允许根目录：outside-root", "；".join(receipt["problems"]))

    def test_traversal_and_symlink_entries_are_rejected(self):
        escape = self.layout["corpus"] / "vault" / "99_归档" / "escape.md"
        escape.write_text("# 越界\n\n符号链接目标\n", encoding="utf-8")
        link = self.layout["corpus"] / "vault" / "70_复利规则" / "经验" / "symlinked.md"
        os.symlink(escape, link)
        _write_catalog_index(
            self.layout,
            _index_entries_from_template(self.layout)
            + [
                {
                    "id": "symlink-entry",
                    "title": "需求不清时必须先追问（符号链接）",
                    "category": "clarify-rule",
                    "index_mode": "semantic_source",
                    "path": str(link),
                    "summary": "符号链接条目。SYMLINK_TOKEN",
                    "sha256": _file_sha256(escape),
                    "bytes": escape.stat().st_size,
                    "status": "active",
                },
                {
                    "id": "traversal-entry",
                    "title": "需求不清时必须先追问（相对越界）",
                    "category": "clarify-rule",
                    "index_mode": "semantic_source",
                    "path": str(self.layout["corpus"]) + "/knowledge/../../outside/other.md",
                    "summary": "相对路径越界。TRAVERSAL_TOKEN",
                    "sha256": "0" * 64,
                    "bytes": 1,
                    "status": "active",
                },
            ],
        )
        outside = self.root / "outside" / "other.md"
        outside.parent.mkdir(exist_ok=True)
        outside.write_text("# 越界\n\n符号链接目标\n", encoding="utf-8")
        receipt = KnowledgeCatalog(self.config).retrieve_with_receipt("SYMLINK_TOKEN")
        self.assertEqual("degraded", receipt["retrieval_status"])
        self.assertIn("价值文件缺失或为符号链接：symlink-entry", "；".join(receipt["problems"]))
        receipt = KnowledgeCatalog(self.config).retrieve_with_receipt("TRAVERSAL_TOKEN")
        self.assertIn("价值文件越过允许根目录：traversal-entry", "；".join(receipt["problems"]))
        self.assertEqual("degraded", receipt["retrieval_status"])

    def test_authority_flag_is_currently_hardcoded_and_is_not_a_gate(self):
        self._clean_index()
        hit = KnowledgeCatalog(self.config).retrieve_with_receipt("NEIGHBOR_ONLY_TOKEN")["hits"][0]
        self.assertEqual(
            True,
            hit["authority"],
            "knowledge.py 的 authority 若改为读取索引字段，这条特征测试需要同步更新",
        )
        self.assertEqual(str(self.layout["corpus"] / "vault" / NEIGHBOR_PATH), hit["path"])
        self.assertEqual(
            _file_sha256(self.layout["corpus"] / "vault" / NEIGHBOR_PATH),
            hit["source_sha256"],
            "authority 标志可变，但内容必须始终绑定到自己回读的文件",
        )

    def test_credential_looking_text_in_one_page_degrades_the_entire_catalog(self):
        """特征测试：相邻页的凭据误报会连带击落整份目录，故障范围不限于该页。"""
        self._clean_index()
        poisoned = self.layout["corpus"] / "vault" / "70_复利规则" / "经验" / "poisoned.md"
        poisoned.write_text(
            "# 需求不清时必须先追问（含合成凭据）\n\n"
            "示例占位符 sk-testcredentialnotreal0000，不是真实密钥。\n",
            encoding="utf-8",
        )
        _write_catalog_index(
            self.layout,
            _index_entries_from_template(self.layout)
            + [
                {
                    "id": "poisoned-clarify-first",
                    "title": "需求不清时必须先追问（合成凭据）",
                    "category": "clarify-rule",
                    "index_mode": "semantic_source",
                    "path": str(poisoned),
                    "summary": "相邻页含合成凭据样式文本。POISONED_ONLY_TOKEN",
                    "sha256": _file_sha256(poisoned),
                    "bytes": poisoned.stat().st_size,
                    "status": "active",
                }
            ],
        )
        report = KnowledgeCatalog(self.config).verify()
        self.assertFalse(report["ok"])
        self.assertIn(
            "价值文件疑似含凭据：poisoned-clarify-first",
            "；".join(report["problems"]),
            "凭据门应当只指控被污染的那一页，指控了别页说明归因错误",
        )
        receipt = KnowledgeCatalog(self.config).retrieve_with_receipt("LOCK_ONLY_TOKEN")
        self.assertEqual("degraded", receipt["retrieval_status"])
        self.assertEqual(
            [],
            receipt["hits"],
            "目录一旦 degraded 就必须完全不放行，不能只降级掉被污染的那一页",
        )

    def test_same_page_under_two_path_spellings_evades_the_duplicate_check(self):
        """特征测试：重复路径判定是字面的，同一文件换一种写法即可绕过。"""
        real = (self.layout["corpus"] / "vault" / LOCK_PATH).resolve()
        shadow = self.layout["corpus"] / "vault" / "70_复利规则" / "经验" / ".." / "经验" / "lock.md"
        self.assertEqual(real, shadow.resolve())
        self.assertNotEqual(str(real), str(shadow))
        entry = next(
            item
            for item in _index_entries_from_template(self.layout)
            if item["id"] == "lock-clarify-first"
        )
        twin = dict(entry)
        twin.update({"id": "shadow-clone-clarify-first", "path": str(shadow)})
        _write_catalog_index(self.layout, [entry, twin])
        report = KnowledgeCatalog(self.config).verify()
        self.assertTrue(
            report["ok"],
            "两条条目指向同一个文件，verify() 若已改为按真实身份判重，本测试需要同步更新："
            + "；".join(report["problems"]),
        )
        receipt = KnowledgeCatalog(self.config).retrieve_with_receipt("LOCK_ONLY_TOKEN")
        self.assertEqual("canonical_match", receipt["retrieval_status"])
        self.assertEqual(
            {"lock-clarify-first", "shadow-clone-clarify-first"},
            {item["id"] for item in receipt["hits"]},
            "同一文件的两条身份都应命中，否则说明放行了其中一种拼写",
        )
        first, second = receipt["hits"]
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(
            first["source_sha256"],
            second["source_sha256"],
            "两条 canonical id 回读到的是同一份内容 —— 身份重复而系统不报错，误引用时无法归因",
        )

    def test_receipt_is_repeatable_except_for_verified_at(self):
        self._clean_index()
        catalog = KnowledgeCatalog(self.config)
        first = catalog.retrieve_with_receipt("LOCK_ONLY_TOKEN")
        second = catalog.retrieve_with_receipt("LOCK_ONLY_TOKEN")
        self.assertIsNotNone(first["index_sha256"])
        self.assertEqual(
            _file_sha256(self.layout["index"]),
            first["index_sha256"],
            "回执必须绑定当前索引文件的哈希，否则无法事后复核",
        )
        self.assertNotEqual(first["verified_at"], "", "时间戳缺失")
        for key in set(first) - {"verified_at"}:
            self.assertEqual(first[key], second[key], f"同输入两次回执不一致：{key}")


class VaultRetrievalLockTest(unittest.TestCase):
    """KnowLP/Vault 层：locator 只负责定位，内容与身份来自回读原文。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.layout = _materialize(self.root, clones=CLONE_COUNT)
        self.vault = self.layout["vault"]
        self.bridge = MemoryBridge(_memory_config(self.root, self.vault), _PublishedSourceStore())

    def tearDown(self):
        self.tmp.cleanup()

    def _retrieve(self, hits: list, query: str = "追问") -> dict:
        with patch("xiaoba_agentos.memory.subprocess.run", return_value=_locator_payload(hits)):
            return self.bridge.retrieve_with_receipt(query)

    def test_correct_page_hit_is_verified_then_admitted(self):
        hits = [
            {"title": "任意 locator 标题", "path": LOCK_PATH, "snippet": "LOCATOR_SNIPPET_MUST_NOT_LEAK"}
        ]
        with patch("xiaoba_agentos.memory.subprocess.run", return_value=_locator_payload(hits)):
            receipt = self.bridge.retrieve_with_receipt("追问")
            contexts = self.bridge.agent_contexts("追问", ["agy_review", "dsh_review"])
        self.assertEqual("canonical_match", receipt["retrieval_status"])
        hit = receipt["hits"][0]
        self.assertEqual(LOCK_PATH.replace("/", os.sep), hit["path"])
        self.assertEqual(_file_sha256(self.vault / LOCK_PATH), hit["source_sha256"])
        self.assertEqual(LOCK_TASK_ID, hit["source_task_id"], "锁页身份必须来自它自己的 frontmatter")
        self.assertTrue(hit["is_active"])
        self.assertIn(LOCK_MARKER, hit["excerpt"])
        self.assertNotIn("LOCATOR_SNIPPET_MUST_NOT_LEAK", json.dumps(receipt, ensure_ascii=False))

        self.assertEqual(
            {"agy_review", "dsh_review"},
            set(contexts["contexts"]),
            "L3：两个后台都应拿到 context，否则下面的逐 Agent 断言会空转",
        )
        for name, text in contexts["contexts"].items():
            with self.subTest(agent=name):
                self.assertIn(LOCK_MARKER, text, "L3：已验证的锁页没有进入 Agent context")
                self.assertNotIn("LOCATOR_SNIPPET_MUST_NOT_LEAK", text)

    def test_adjacent_page_admits_only_its_own_bytes(self):
        receipt = self._retrieve([{"title": "需求不清时必须先追问", "path": NEIGHBOR_PATH}])
        self.assertEqual("canonical_match", receipt["retrieval_status"])
        hit = receipt["hits"][0]
        self.assertIn(NEIGHBOR_MARKER, hit["excerpt"])
        self.assertNotIn(LOCK_MARKER, hit["excerpt"], "相邻页命中被静默替换成了锁页内容")
        self.assertEqual(NEIGHBOR_TASK_ID, hit["source_task_id"])
        self.assertFalse(hit["authority"], "相邻页 frontmatter 声明 authority: false")

    def test_locator_title_becomes_the_label_the_agent_reads(self):
        """特征测试：render_hits 的标题行来自 locator，未与回读页面核对，可被冒名。"""
        claimed = "需求不清时必须先追问（权威锁页）"
        receipt = self._retrieve([{"title": claimed, "path": NEIGHBOR_PATH}])
        hit = receipt["hits"][0]
        self.assertIn(NEIGHBOR_MARKER, hit["excerpt"], "正文必须是相邻页自己的字节")
        self.assertNotIn(LOCK_MARKER, hit["excerpt"])
        self.assertEqual(
            claimed,
            hit["title"],
            "若 memory.py 已改为使用回读页面的标题/文件名，这条特征测试需要同步更新",
        )
        rendered = MemoryBridge.render_hits(receipt["hits"])
        self.assertTrue(
            rendered.startswith(f"- {claimed}:"),
            "L3：Agent 读到的标题行应当是 locator 声称的那句，否则本测试的缺口结论不成立",
        )
        self.assertNotIn(
            NEIGHBOR_PATH,
            rendered,
            "标题行若已带上真实路径，locator 冒名就不再是缺口，需同步更新本测试",
        )

    def test_archived_page_hit_fails_closed_instead_of_substituting_the_active_twin(self):
        hits = [{"title": "需求不清时必须先追问", "path": ARCHIVED_PATH, "snippet": ARCHIVED_MARKER}]
        with patch("xiaoba_agentos.memory.subprocess.run", return_value=_locator_payload(hits)):
            receipt = self.bridge.retrieve_with_receipt("追问")
            contexts = self.bridge.agent_contexts("追问", ["agy_review", "dsh_review"])
        self.assertEqual("miss", receipt["retrieval_status"], "归档命中必须整体不落，而不是换一页")
        self.assertEqual([], receipt["hits"])
        dumped = json.dumps(receipt, ensure_ascii=False)
        self.assertNotIn(ARCHIVED_MARKER, dumped, "归档页面内容泄漏进回执")
        self.assertNotIn(LOCK_MARKER, dumped, "系统用相似的 active 页面替代了错误命中")

        self.assertEqual({}, contexts["contexts"], "L3：归档命中不得伪装成任何活动页面进入 context")
        self.assertEqual([], contexts["hits"])
        self.assertEqual("miss", contexts["receipt"]["retrieval_status"])

    def test_superseded_page_hit_is_rejected(self):
        receipt = self._retrieve([{"path": SUPERSEDED_PATH}])
        self.assertEqual([], receipt["hits"])
        self.assertNotIn(SUPERSEDED_MARKER, json.dumps(receipt, ensure_ascii=False))

    def test_alias_page_cannot_inherit_the_lock_identity(self):
        receipt = self._retrieve(
            [{"title": "需求不清时必须先追问", "path": ALIAS_PATH, "snippet": "看起来就是锁页"}]
        )
        hit = receipt["hits"][0]
        self.assertEqual(ALIAS_PATH.replace("/", os.sep), hit["path"])
        self.assertEqual(_file_sha256(self.vault / ALIAS_PATH), hit["source_sha256"])
        self.assertEqual(
            ALIAS_TASK_ID, hit["source_task_id"], "别名页的 source_task_id 被替换成了锁页"
        )
        self.assertNotIn(LOCK_MARKER, hit["excerpt"])
        self.assertEqual("需求不清时必须先追问", hit["title"], "locator 标题直通，见报告中的归因风险")

    def test_non_uuid_source_task_id_is_not_recorded_as_a_reuse(self):
        receipt = self._retrieve([{"path": BAD_ID_PATH}])
        self.assertEqual(1, len(receipt["hits"]), "内容合法但身份非法的页面应保留命中")
        self.assertIsNone(receipt["hits"][0]["source_task_id"], "非法 source_task_id 必须置空，不能冒充可追溯复用")

    def test_twenty_five_clones_admit_only_their_own_pages(self):
        clone_relative = sorted(str(path.relative_to(self.vault)) for path in self.layout["clones"].values())
        receipt = self._retrieve([{"path": relative} for relative in clone_relative])
        self.assertEqual(CLONE_COUNT, len(receipt["hits"]), "25 份克隆应各自独立通过验证，不能被去重成一份")
        lock_hash = _file_sha256(self.vault / LOCK_PATH)
        paths = {hit["path"] for hit in receipt["hits"]}
        self.assertNotIn(LOCK_PATH.replace("/", os.sep), paths, "克隆命中被锁页替代")
        self.assertTrue(
            all(hit["source_sha256"] != lock_hash for hit in receipt["hits"]),
            "某份克隆命中带上了锁页哈希",
        )
        for index, hit in enumerate(receipt["hits"]):
            self.assertIn(f"CLONE_MARKER_{index:02d}", hit["excerpt"], f"第 {index} 份克隆内容绑定错误")

    def test_locator_wrong_path_variants_all_fail_closed(self):
        outside = self.root / "outside" / "secret.md"
        outside.parent.mkdir()
        outside.write_text(
            '---\nid: "outside"\nstatus: "active"\n---\n\n' + OUTSIDE_MARKER + "\n", encoding="utf-8"
        )
        os.symlink(outside, self.vault / "link_to_outside.md")
        (self.vault / "not_markdown.txt").write_text("# txt\n", encoding="utf-8")

        receipt = self._retrieve(
            [
                {"path": "../outside/secret.md"},
                {"path": "link_to_outside.md"},
                {"path": "not_markdown.txt"},
                {"path": "99_归档"},
                {"path": "does_not_exist.md"},
                {"path": ""},
                {"snippet": "没有路径"},
                "不是对象",
                {"path": LOCK_PATH},
            ]
        )
        self.assertEqual(1, len(receipt["hits"]), "只有真实存在的锁页可以通过验证")
        self.assertEqual(LOCK_PATH.replace("/", os.sep), receipt["hits"][0]["path"])
        self.assertNotIn(OUTSIDE_MARKER, json.dumps(receipt, ensure_ascii=False))

    def test_only_verified_hits_reach_agent_context(self):
        outside = self.root / "outside" / "secret.md"
        outside.parent.mkdir()
        outside.write_text(
            '---\nid: "outside"\nstatus: "active"\n---\n\n' + OUTSIDE_MARKER + "\n", encoding="utf-8"
        )
        os.symlink(outside, self.vault / "link_to_outside.md")
        raw_hits = [
            {"path": LOCK_PATH, "snippet": LOCK_MARKER},
            {"path": ARCHIVED_PATH, "snippet": "LOCATED_SNIPPET_A"},
            {"path": "missing.md", "snippet": "LOCATED_SNIPPET_B"},
            {"path": "link_to_outside.md", "snippet": "LOCATED_SNIPPET_C"},
            {"path": "../outside/secret.md", "snippet": "LOCATED_SNIPPET_D"},
        ]
        with patch("xiaoba_agentos.memory.subprocess.run", return_value=_locator_payload(raw_hits)):
            contexts = self.bridge.agent_contexts("追问", ["agy_review", "dsh_review"])

        self.assertEqual(5, len(raw_hits), "L1：locator 返回了 5 条候选")
        self.assertEqual(1, len(contexts["hits"]), "L2：只有 1 条通过权威源验证")
        self.assertEqual(LOCK_TASK_ID, contexts["hits"][0]["source_task_id"])
        for name, text in contexts["contexts"].items():
            with self.subTest(agent=name):
                self.assertIn(LOCK_MARKER, text)
                for leaked in ("LOCATED_SNIPPET_A", "LOCATED_SNIPPET_B", "LOCATED_SNIPPET_C", "LOCATED_SNIPPET_D", OUTSIDE_MARKER):
                    self.assertNotIn(leaked, text, f"L3：未验证内容进入了 {name} 的 context")
        self.assertEqual("canonical_match", contexts["receipt"]["retrieval_status"])

    def test_secret_query_is_redacted_before_the_locator_runs(self):
        secret = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"
        captured = []

        def fake_run(argv, *args, **kwargs):
            captured.append(list(argv))
            return _locator_payload([{"path": LOCK_PATH}])

        with patch("xiaoba_agentos.memory.subprocess.run", side_effect=fake_run):
            receipt = self.bridge.retrieve_with_receipt(f"规则问题 {secret}")

        self.assertEqual("[REDACTED_SENSITIVE_QUERY]", receipt["query"])
        self.assertEqual(1, len(captured))
        self.assertNotIn(secret, json.dumps(captured, ensure_ascii=False), "凭据泄漏给 KnowLP 子进程")
        self.assertIn("[REDACTED_SENSITIVE_QUERY]", captured[-1])

    def test_locator_command_keeps_short_chinese_and_long_queries_predictable(self):
        captured = []

        def fake_run(argv, *args, **kwargs):
            captured.append(list(argv))
            return _locator_payload([{"path": LOCK_PATH}])

        with patch("xiaoba_agentos.memory.subprocess.run", side_effect=fake_run):
            short = self.bridge.retrieve_with_receipt("锁")
            self.assertEqual("锁", short["query"])
            long_query = "追问" * 400
            long = self.bridge.retrieve_with_receipt(long_query)
            passed = captured[-1][captured[-1].index("--query") + 1]
            self.assertEqual(long_query[:500], passed, "查询必须在 500 字符处有界截断")
            self.assertEqual(long_query[:500], long["query"])
            self.assertIn(LOCK_MARKER, long["hits"][0]["excerpt"])

    def test_receipt_hash_tracks_content_and_graph_index(self):
        graph = self.root / "graph"
        graph.mkdir()
        note = self.vault / LOCK_PATH
        first = self._retrieve([{"path": LOCK_PATH}])
        self.assertEqual(_file_sha256(note), first["hits"][0]["source_sha256"])
        self.assertIsNone(first["index_sha256"], "graph 目录还不存在时索引哈希必须为空")

        (graph / "meta_index.json").write_text('{"nodes": 1}', encoding="utf-8")
        plain = self.bridge.index_sha256()
        self.assertIsNotNone(plain)
        (graph / ".hidden.json").write_text('{"ignore": true}', encoding="utf-8")
        os.symlink(graph / "meta_index.json", graph / "linked.json")
        self.assertEqual(
            plain,
            self.bridge.index_sha256(),
            "隐藏文件与符号链接必须被索引哈希排除，否则绑定可被旁路",
        )

        note.write_text(
            note.read_text(encoding="utf-8") + "\n补充一句现行说明\n", encoding="utf-8"
        )
        after = self._retrieve([{"path": LOCK_PATH}])
        self.assertNotEqual(first["hits"][0]["source_sha256"], after["hits"][0]["source_sha256"])
        self.assertEqual(_file_sha256(note), after["hits"][0]["source_sha256"])

        (graph / "meta_index.json").write_text('{"nodes": 2}', encoding="utf-8")
        self.assertNotEqual(plain, self.bridge.index_sha256(), "graph 内容变了但索引哈希没变")

    def test_receipt_is_repeatable_except_for_verified_at(self):
        first = self._retrieve([{"path": LOCK_PATH}, {"path": NEIGHBOR_PATH}])
        second = self._retrieve([{"path": LOCK_PATH}, {"path": NEIGHBOR_PATH}])
        for key in set(first) - {"verified_at"}:
            self.assertEqual(first[key], second[key], f"同输入两次回执不一致：{key}")

    def test_graph_excludes_archived_pages_from_active_retrieval_substrate(self):
        built = INDEX_BUILDER.build(self.vault)
        indexed = {item["path"] for item in built["meta_index.json"]}
        self.assertIn(LOCK_PATH, indexed)
        self.assertIn(NEIGHBOR_PATH, indexed)
        self.assertEqual(CLONE_COUNT, len([p for p in indexed if "clone_" in p]))
        for archived in (ARCHIVED_PATH, SUPERSEDED_PATH):
            self.assertNotIn(archived, indexed, f"归档页面 {archived} 成了活动图节点")
        self.assertEqual({}, built["dual_graph.json"]["similarity"], "近重复页面之间不得自动生成相似边")
        chunks = json.dumps(built["meta_index.json"], ensure_ascii=False)
        for marker in (ARCHIVED_MARKER, SUPERSEDED_MARKER):
            self.assertNotIn(marker, chunks, f"归档内容进入图分块：{marker}")

    def test_archive_mirrored_into_active_dir_is_detected_by_lifecycle_audit(self):
        stale = self.vault / "70_复利规则" / "经验" / f"{ARCHIVED_TASK_ID}.md"
        shutil.copyfile(self.vault / ARCHIVED_PATH, stale)
        stale.write_text(
            stale.read_text(encoding="utf-8").replace('status: "archived"', 'status: "active"'),
            encoding="utf-8",
        )
        catalog = self.vault / "70_复利规则" / "经验索引.md"
        catalog.write_text(
            catalog.read_text(encoding="utf-8")
            + f"- [[70_复利规则/经验/{ARCHIVED_TASK_ID}]]\n",
            encoding="utf-8",
        )

        result = self.bridge.audit_lifecycle(
            [{"task_id": ARCHIVED_TASK_ID, "status": "retired", "published_path": str(stale)}]
        )
        self.assertFalse(result["ok"])
        self.assertIn(f"{ARCHIVED_TASK_ID}:retired_target_still_active", result["problems"])
        self.assertIn(f"{ARCHIVED_TASK_ID}:retired_catalog_link_present", result["problems"])

        receipt = self._retrieve([{"path": f"70_复利规则/经验/{ARCHIVED_TASK_ID}.md"}])
        self.assertEqual(1, len(receipt["hits"]), "status 仍是 active 的归档副本在检索层无法被拦住")


if __name__ == "__main__":
    unittest.main()
