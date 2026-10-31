import json
import hashlib
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.memory import MemoryBridge
from xiaoba_agentos.store import EventStore


def approve_staged_fixture(config, task_id):
    """Synthetic approval in an isolated test DB; never an application shortcut."""
    store = EventStore(config.database_path)
    store.initialize()
    if store.get_task(task_id) is None:
        with patch('xiaoba_agentos.store.uuid.uuid4', return_value=task_id):
            store.submit('isolated publication fixture', 1)
    path = MemoryBridge(config).staged_path(task_id)
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    store.stage_memory_candidate(task_id, str(path), sha256)
    for stage in ('delivery', 'final'):
        store.review_memory(task_id, stage, 'approve', 'isolated fixture', sha256)


class MemoryBridgeTest(unittest.TestCase):
    def config(self, root: Path) -> AppConfig:
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
                "knowlp_enabled": True, "knowlp_python": "/python", "knowlp_module": "/knowlp.py",
                "obsidian_vault": str(root), "obsidian_subdir": "AgentOS", "publish_enabled": False,
            },
            "integrations": {},
            "agents": [{"name": "mock", "type": "mock", "enabled": True}],
            "evolution": {"minimum_samples": 1, "promotion_margin": 0.05, "latency_penalty": 0.01},
        }
        path = root / "config.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return AppConfig.load(path)

    def test_probe_executes_query_and_reports_warning_without_content(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = MemoryBridge(self.config(Path(directory)))
            completed = subprocess.CompletedProcess([], 0, '{"hits":[{"content":"secret"}]}', "compat warning")
            with patch("xiaoba_agentos.memory.subprocess.run", return_value=completed):
                result = bridge.probe()
            self.assertEqual({"ok", "enabled", "hit_count", "warning"}, set(result))
            self.assertEqual(1, result["hit_count"])
            self.assertNotIn("secret", str(result))

    def test_retrieve_opens_verified_vault_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rule.md").write_text(
                '---\nid: "rule"\nstatus: "active"\n---\n\n# Rule\n\n问题不清楚必须追问，不许猜。',
                encoding="utf-8",
            )
            bridge = MemoryBridge(self.config(root))
            completed = subprocess.CompletedProcess(
                [], 0, '{"hits":[{"title":"rule","path":"rule.md","snippet":"rule"}]}', ""
            )
            with patch("xiaoba_agentos.memory.subprocess.run", return_value=completed):
                result = bridge.retrieve("问题不清楚")
            self.assertIn("问题不清楚必须追问", result)

    def test_retrieve_rejects_source_outside_vault(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = MemoryBridge(self.config(Path(directory)))
            completed = subprocess.CompletedProcess(
                [], 0, '{"hits":[{"title":"outside","path":"/etc/hosts","snippet":"safe"}]}', ""
            )
            with patch("xiaoba_agentos.memory.subprocess.run", return_value=completed):
                result = bridge.retrieve("outside")
            self.assertEqual("", result)

    def test_retrieve_never_falls_back_to_snippet_for_missing_or_inactive_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "inactive.md").write_text(
                '---\nid: "inactive"\nstatus: "revalidation_required"\n---\n\n# Inactive\n',
                encoding="utf-8",
            )
            bridge = MemoryBridge(self.config(root))
            completed = subprocess.CompletedProcess(
                [],
                0,
                json.dumps({
                    "hits": [
                        {"title": "missing", "path": "missing.md", "snippet": "DO NOT USE"},
                        {"title": "inactive", "path": "inactive.md", "snippet": "DO NOT USE"},
                        {"title": "pathless", "snippet": "DO NOT USE"},
                    ]
                }),
                "",
            )
            with patch("xiaoba_agentos.memory.subprocess.run", return_value=completed):
                self.assertEqual([], bridge.retrieve_hits("query"))
                self.assertEqual({}, bridge.agent_contexts("query", ["codex", "claude"])["contexts"])

    def test_staged_experience_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = MemoryBridge(self.config(Path(directory)))
            target = bridge.stage_experience("task", "prompt", "result", [])
            self.assertEqual(0o700, os.stat(target.parent).st_mode & 0o777)
            self.assertEqual(0o600, os.stat(target).st_mode & 0o777)

    def test_agent_contexts_diversify_verified_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = MemoryBridge(self.config(Path(directory)))
            hits = [
                {"title": "one", "path": "one.md", "excerpt": "first", "source_task_id": "1"},
                {"title": "two", "path": "two.md", "excerpt": "second", "source_task_id": "2"},
            ]
            with patch.object(bridge, "retrieve_hits", return_value=hits):
                result = bridge.agent_contexts("query", ["codex", "claude"])
            self.assertIn("first", result["contexts"]["codex"])
            self.assertNotIn("second", result["contexts"]["codex"])
            self.assertIn("first", result["contexts"]["claude"])
            self.assertIn("second", result["contexts"]["claude"])

    def test_note_names_are_readable_and_keep_the_task_prefix(self):
        task_id = "de03126a-a26c-444b-8f93-d2ea27fdf116"
        authored = "---\nx: 1\n---\n\n# 已审核经验 %s\n\n# 大工程拆分/按模型派工：第 [2] 版？\n\n正文" % task_id
        self.assertEqual("大工程拆分 按模型派工：第 2 版？-de03126a.md".replace("？", "？"), MemoryBridge.note_filename(authored, task_id).replace("?", "？"))
        auto = "---\nx: 1\n---\n\n# AgentOS 经验候选\n\n## 任务\n\n检索重复文件并输出清单\n\n## 结果\n\n略"
        self.assertEqual("检索重复文件并输出清单-de03126a.md", MemoryBridge.note_filename(auto, task_id))
        self.assertEqual("经验-de03126a.md", MemoryBridge.note_filename("没有任何标题", task_id))
        self.assertLessEqual(len(MemoryBridge.note_filename("# " + "长" * 200, task_id)), 40 + len("-de03126a.md"))

    def test_old_style_note_is_renamed_with_its_catalog_link_and_found_afterwards(self):
        task_id = "22222222-2222-2222-2222-222222222222"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = self.config(root)
            vault = root / "vault"
            config.raw["project_dir"] = str(Path(__file__).resolve().parents[1])
            config.raw["memory"].update({"publish_enabled": True, "obsidian_vault": str(vault), "knowlp_vault": str(vault),
                                         "obsidian_subdir": "70_复利规则/经验", "catalog_note": "70_复利规则/经验索引.md",
                                         "knowlp_graph_dir": str(root / "runtime" / "graph")})
            notes = vault / "70_复利规则" / "经验"
            notes.mkdir(parents=True)
            (vault / "root.md").write_text('---\nid: "root"\ntype: "root"\nparent: ""\nstatus: "active"\n'
                                           'authority: true\n---\n\n# Root\n\n- [[70_复利规则/经验索引]]\n', encoding="utf-8")
            catalog = vault / "70_复利规则" / "经验索引.md"
            catalog.write_text('---\nid: "experiences"\ntype: "branch"\nparent: "root"\nstatus: "active"\nauthority: true\n---\n\n'
                               '# Experiences\n- [[70_复利规则/经验/%s]]\n' % task_id, encoding="utf-8")
            old = notes / (task_id + ".md")
            old.write_text('---\nid: "experience-%s"\ntype: "experience"\nparent: "experiences"\nstatus: "active"\n'
                           'authority: false\nsource_task_id: "%s"\n---\n\n# 已审核经验 %s\n\n# 验收防误判规则\n\n正文一行\n'
                           % (task_id, task_id, task_id), encoding="utf-8")
            bridge = MemoryBridge(config)
            self.assertEqual(old, bridge.find_note(vault, task_id))
            preview = bridge.rename_note(task_id, dry_run=True)
            self.assertTrue(preview["changed"] and old.is_file())                 # 预览不动文件
            result = bridge.rename_note(task_id)
            new = notes / "验收防误判规则-22222222.md"
            self.assertEqual((str(new), True), (result["to"], result["index"]["ok"]))
            self.assertFalse(old.exists())
            text = new.read_text(encoding="utf-8")
            self.assertIn('source_task_id: "%s"' % task_id, text)
            self.assertEqual(1, text.count("# 验收防误判规则"))                  # 标题只出现一次，旧的编号标题去掉了
            self.assertNotIn("已审核经验", text)
            self.assertIn("正文一行", text)
            self.assertEqual(["- [[70_复利规则/经验/验收防误判规则-22222222]]"],
                             [line for line in catalog.read_text(encoding="utf-8").splitlines() if line.startswith("- [[")])
            self.assertEqual(new, bridge.find_note(vault, task_id))
            self.assertFalse(bridge.rename_note(task_id)["changed"])              # 再跑一次什么都不改

    def test_stage_mark_on_the_notes_own_title_is_removed_in_place(self):
        task_id = "33333333-3333-3333-3333-333333333333"
        body = "# 验收防误判规则（宿主整理，待审核）\n\n正文一行"
        self.assertEqual("# 验收防误判规则\n\n正文一行", MemoryBridge._titled_body(task_id, body))   # 不叠出第二个标题
        self.assertEqual("# 验收防误判规则\n\n正文一行", MemoryBridge._titled_body(task_id, "# 验收防误判规则\n\n正文一行"))
        auto = "# AgentOS 经验候选\n\n## 任务\n\n检索重复文件（待审核）\n\n## 结果\n\n略"
        self.assertEqual("检索重复文件", MemoryBridge.note_title(auto, task_id))

    def test_auto_candidate_does_not_stack_two_level_one_titles(self):
        task_id = "35555555-5555-5555-5555-555555555555"
        auto = "# AgentOS 经验候选\n\n## 任务\n\n检索重复文件（待审核）\n\n## 结果\n\n略"
        titled = MemoryBridge._titled_body(task_id, auto)
        self.assertEqual("# 检索重复文件\n\n## 任务\n\n检索重复文件（待审核）\n\n## 结果\n\n略", titled)
        self.assertEqual(1, len([line for line in titled.splitlines() if line.startswith("# ")]))
        self.assertEqual(titled, MemoryBridge._titled_body(task_id, titled))          # 再调一次结果不变
        self.assertEqual("经验", MemoryBridge.note_title("# AgentOS 经验候选\n\n## 任务\n\n## 结果\n\n略", task_id))
        plain = "# 验收防误判规则\n\n正文一行"
        self.assertEqual(plain, MemoryBridge._titled_body(task_id, plain))            # 普通正文行为不变

    def test_rename_preview_reports_a_name_conflict_and_changes_nothing(self):
        task_id = "44444444-4444-4444-4444-444444444444"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = self.config(root)
            vault = root / "vault"
            config.raw["memory"].update({"obsidian_vault": str(vault), "obsidian_subdir": "70_复利规则/经验",
                                         "catalog_note": "70_复利规则/经验索引.md"})
            notes = vault / "70_复利规则" / "经验"
            notes.mkdir(parents=True)
            (vault / "70_复利规则" / "经验索引.md").write_text("- [[70_复利规则/经验/%s]]\n" % task_id, encoding="utf-8")
            old = notes / (task_id + ".md")
            old.write_text('---\nsource_task_id: "%s"\n---\n\n# 验收防误判规则\n\n正文\n' % task_id, encoding="utf-8")
            taken = notes / "验收防误判规则-44444444.md"
            taken.write_text("别的笔记", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                MemoryBridge(config).rename_note(task_id, dry_run=True)
            self.assertEqual(("别的笔记", True), (taken.read_text(encoding="utf-8"), old.is_file()))

    def test_secret_candidate_is_rejected(self):
        self.assertEqual(
            "authorization",
            MemoryBridge._sensitive_reason("Authorization: Bearer abcdefghijklmnop"),
        )
        self.assertEqual(
            "oauth_token",
            MemoryBridge._sensitive_reason("refresh_token=FAKE_ONLY_12345678901234567890"),
        )

    def test_common_bare_tokens_are_rejected(self):
        self.assertEqual(
            "bearer_jwt",
            MemoryBridge._sensitive_reason(
                "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
                "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
            ),
        )
        self.assertEqual(
            "google_oauth_token",
            MemoryBridge._sensitive_reason("ya29.a0AfH6SMB0123456789_SYNTHETIC_TOKEN"),
        )
        self.assertEqual(
            "aws_access_key_id",
            MemoryBridge._sensitive_reason("AKIAIOSFODNN7EXAMPLE"),
        )
        self.assertEqual(
            "aws_access_key_id",
            MemoryBridge._sensitive_reason("ASIAIOSFODNN7EXAMPLE"),
        )

    def test_short_token_examples_are_not_rejected(self):
        for text in ("Bearer a.b.c", "ya29.example", "AKIA1234", "ASIAEXAMPLE"):
            with self.subTest(text=text):
                self.assertIsNone(MemoryBridge._sensitive_reason(text))

    def test_publication_rejects_candidate_changed_after_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            config.raw["memory"]["publish_enabled"] = True
            bridge = MemoryBridge(config)
            candidate = bridge.stage_experience("task", "prompt", "result", [])
            approved_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
            approve_staged_fixture(config, 'task')
            candidate.write_text(candidate.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
            with self.assertRaisesRegex((RuntimeError, ValueError), "changed|hash"):
                bridge.publish_candidate("task", approved_hash)

    def test_final_publication_updates_tree_and_verified_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            vault = root / "vault"
            vault.mkdir()
            config.raw["project_dir"] = str(Path(__file__).resolve().parents[1])
            config.raw["memory"].update(
                {
                    "publish_enabled": True,
                    "obsidian_vault": str(vault),
                    "knowlp_vault": str(vault),
                    "obsidian_subdir": "70_复利规则/经验",
                    "catalog_note": "70_复利规则/经验索引.md",
                    "knowlp_graph_dir": str(root / "runtime" / "graph"),
                }
            )
            catalog = vault / "70_复利规则" / "经验索引.md"
            catalog.parent.mkdir(parents=True)
            (vault / "root.md").write_text(
                '---\nid: "root"\ntype: "root"\nparent: ""\nstatus: "active"\n'
                'authority: true\n---\n\n# Root\n\n- [[70_复利规则/经验索引]]\n',
                encoding="utf-8",
            )
            catalog.write_text(
                '---\nid: "experiences"\ntype: "branch"\nparent: "root"\nstatus: "active"\n'
                'authority: true\n---\n\n# Experiences\n',
                encoding="utf-8",
            )
            bridge = MemoryBridge(config)
            bridge.stage_experience("11111111-1111-1111-1111-111111111111", "task", "result", [])
            approve_staged_fixture(config, "11111111-1111-1111-1111-111111111111")
            published = bridge.publish_candidate("11111111-1111-1111-1111-111111111111")
            self.assertEqual(1, published["published"])
            self.assertTrue(Path(published["path"]).is_file())
            self.assertEqual("task-11111111.md", Path(published["path"]).name)   # 可读标题 + 编号前 8 位
            self.assertIn("- [[70_复利规则/经验/task-11111111]]", catalog.read_text(encoding="utf-8"))
            self.assertTrue(Path(published["path"]).read_text(encoding="utf-8").split("---\n\n", 1)[1].startswith("# task\n"))
            self.assertTrue(published["index"]["ok"])
            original_note = Path(published["path"]).read_text(encoding="utf-8")
            original_catalog = catalog.read_text(encoding="utf-8")
            with patch.object(
                bridge, "_rebuild_and_verify_index", side_effect=RuntimeError("index failed")
            ):
                with self.assertRaisesRegex(RuntimeError, "index failed"):
                    bridge.set_validation_status(
                        "11111111-1111-1111-1111-111111111111",
                        published["path"],
                        "revalidation_required",
                    )
            self.assertEqual(original_note, Path(published["path"]).read_text(encoding="utf-8"))
            self.assertEqual(original_catalog, catalog.read_text(encoding="utf-8"))
            bridge.set_validation_status(
                "11111111-1111-1111-1111-111111111111",
                published["path"],
                "revalidation_required",
            )
            self.assertIn(
                'status: "revalidation_required"',
                Path(published["path"]).read_text(encoding="utf-8"),
            )
            self.assertNotIn(
                "11111111-1111-1111-1111-111111111111",
                catalog.read_text(encoding="utf-8"),
            )
            meta = json.loads((root / "runtime" / "graph" / "meta_index.json").read_text(encoding="utf-8"))
            self.assertNotIn(
                "11111111-1111-1111-1111-111111111111.md",
                {Path(item["path"]).name for item in meta},
            )
            bridge.set_validation_status(
                "11111111-1111-1111-1111-111111111111",
                published["path"],
                "active",
            )
            self.assertEqual(
                1,
                sum(
                    "task-11111111" in line
                    for line in catalog.read_text(encoding="utf-8").splitlines()
                ),
            )
            retired = bridge.retire_candidate(
                "11111111-1111-1111-1111-111111111111", published["path"]
            )
            self.assertFalse(Path(published["path"]).exists())
            self.assertTrue(Path(retired["archive_path"]).is_file())
            self.assertNotIn("task-11111111", catalog.read_text(encoding="utf-8"))
            self.assertTrue(retired["index"]["ok"])
            retried = bridge.retire_candidate(
                "11111111-1111-1111-1111-111111111111", published["path"]
            )
            self.assertTrue(retried["idempotent"])
            self.assertEqual(retired["archive_path"], retried["archive_path"])

    def test_lifecycle_audit_detects_active_revalidation_note(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            config.raw["memory"].update({
                "obsidian_vault": str(root),
                "obsidian_subdir": "experiences",
                "catalog_note": "catalog.md",
            })
            task_id = "22222222-2222-2222-2222-222222222222"
            target = root / "experiences" / f"{task_id}.md"
            target.parent.mkdir()
            target.write_text(
                f'---\nsource_task_id: "{task_id}"\nstatus: "active"\n---\n',
                encoding="utf-8",
            )
            (root / "catalog.md").write_text(f"- [[experiences/{task_id}]]\n", encoding="utf-8")
            result = MemoryBridge(config).audit_lifecycle([{
                "task_id": task_id,
                "status": "revalidation_required",
                "published_path": str(target),
            }])
            self.assertFalse(result["ok"])
            self.assertIn(f"{task_id}:revalidation_file_still_active", result["problems"])
            self.assertIn(f"{task_id}:revalidation_catalog_link_present", result["problems"])

    def test_retrieve_with_receipt_canonical_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "verified_rule.md"
            target.write_text(
                '---\nid: "verified-rule"\nstatus: "active"\nauthority: true\nsource_task_id: "55555555-5555-5555-5555-555555555555"\n---\n\n# Verified\n\n必须先确认目标再操作。\n',
                encoding="utf-8",
            )
            store = Mock()
            store.get_memory_candidate.return_value = {"status": "published"}
            bridge = MemoryBridge(self.config(root), store)
            completed = subprocess.CompletedProcess(
                [], 0, json.dumps({"hits": [{"title": "Verified Rule", "path": "verified_rule.md"}]}), ""
            )
            with patch("xiaoba_agentos.memory.subprocess.run", return_value=completed):
                receipt = bridge.retrieve_with_receipt("确认目标")
            self.assertEqual("canonical_match", receipt["retrieval_status"])
            self.assertEqual(1, len(receipt["hits"]))
            hit = receipt["hits"][0]
            self.assertEqual("verified_rule.md", hit["path"])
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), hit["source_sha256"])
            self.assertEqual("55555555-5555-5555-5555-555555555555", hit["source_task_id"])
            self.assertTrue(hit["is_active"])
            self.assertTrue(hit["authority"])
            self.assertIn("必须先确认目标再操作", hit["excerpt"])
            self.assertTrue(receipt["verified_at"])

    def test_retrieve_with_receipt_disabled_when_knowlp_off(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            config.raw["memory"]["knowlp_enabled"] = False
            bridge = MemoryBridge(config)
            receipt = bridge.retrieve_with_receipt("any query")
            self.assertEqual("disabled", receipt["retrieval_status"])
            self.assertEqual([], receipt["hits"])

    def test_retrieve_with_receipt_degraded_on_locator_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = MemoryBridge(self.config(root))
            failed = subprocess.CompletedProcess([], 1, "", "locator crashed with syntax error")
            with patch("xiaoba_agentos.memory.subprocess.run", return_value=failed):
                receipt = bridge.retrieve_with_receipt("test")
            self.assertEqual("degraded", receipt["retrieval_status"])
            self.assertEqual([], receipt["hits"])
            self.assertIn("problems", receipt)

            # Invalid JSON also degrades fail closed
            invalid_json = subprocess.CompletedProcess([], 0, "not json", "")
            with patch("xiaoba_agentos.memory.subprocess.run", return_value=invalid_json):
                receipt_invalid = bridge.retrieve_with_receipt("test")
            self.assertEqual("degraded", receipt_invalid["retrieval_status"])
            self.assertEqual([], receipt_invalid["hits"])

    def test_retrieve_with_receipt_tracks_source_sha256_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "evolving.md"
            target.write_text(
                '---\nid: "evolving"\nstatus: "active"\n---\n\nInitial version\n',
                encoding="utf-8",
            )
            bridge = MemoryBridge(self.config(root))
            completed = subprocess.CompletedProcess(
                [], 0, json.dumps({"hits": [{"title": "Evolving", "path": "evolving.md"}]}), ""
            )
            with patch("xiaoba_agentos.memory.subprocess.run", return_value=completed):
                first = bridge.retrieve_with_receipt("version")
            initial_hash = first["hits"][0]["source_sha256"]

            target.write_text(
                '---\nid: "evolving"\nstatus: "active"\n---\n\nUpdated version after fix\n',
                encoding="utf-8",
            )
            with patch("xiaoba_agentos.memory.subprocess.run", return_value=completed):
                second = bridge.retrieve_with_receipt("version")
            updated_hash = second["hits"][0]["source_sha256"]
            self.assertNotEqual(initial_hash, updated_hash)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), updated_hash)

    def test_retrieve_with_receipt_redacts_sensitive_query(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = MemoryBridge(self.config(root))
            captured = []

            def fake_run(argv, *args, **kwargs):
                captured.append(list(argv))
                return subprocess.CompletedProcess([], 0, '{"hits":[]}', "")

            secret = "Authorization: Bearer sk-supersecrettokenvalue12345"
            with patch("xiaoba_agentos.memory.subprocess.run", side_effect=fake_run):
                receipt = bridge.retrieve_with_receipt(f"query with {secret}")
            self.assertEqual("[REDACTED_SENSITIVE_QUERY]", receipt["query"])
            self.assertNotIn("sk-supersecrettokenvalue", json.dumps(captured))
            self.assertIn("[REDACTED_SENSITIVE_QUERY]", captured[-1])


class MemoryRetrievalLifecycleGateTest(unittest.TestCase):
    """只有数据库状态为 published 的任务来源经验才允许进入活动检索。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        (self.vault / "AgentOS").mkdir(parents=True)
        self.store = EventStore(self.root / "agentos.sqlite3")
        self.store.initialize()
        self.sha = hashlib.sha256(b"candidate-bytes").hexdigest()
        self.task_id = self.store.submit("source prompt", 1)
        self.store.complete(self.task_id, "source result")
        self.note = self.vault / "AgentOS" / f"{self.task_id}.md"
        self.note.write_text(
            "---\n"
            f'source_task_id: "{self.task_id}"\n'
            'status: "active"\n'
            'authority: false\n'
            "---\n\n# 经验\n\n必须先把候选状态写进数据库。\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _config(self, knowlp_enabled: bool = True) -> AppConfig:
        raw = {
            "schema_version": 1,
            "runtime_dir": str(self.root / "runtime"),
            "project_dir": str(self.root),
            "worker": {
                "poll_seconds": 1, "lease_seconds": 60, "max_attempts": 2,
                "retry_base_seconds": 1, "retry_max_seconds": 2, "parallelism": 1,
                "minimum_successful_agents": 1,
            },
            "memory": {
                "knowlp_enabled": knowlp_enabled,
                "knowlp_python": "/python",
                "knowlp_module": "/knowlp.py",
                "knowlp_graph_dir": str(self.root / "graph"),
                "obsidian_vault": str(self.vault),
                "knowlp_vault": str(self.vault),
                "obsidian_subdir": "AgentOS",
                "catalog_note": "70_复利规则/经验索引.md",
                "publish_enabled": False,
            },
            "integrations": {},
            "agents": [{"name": "mock", "type": "mock", "enabled": True}],
            "evolution": {"minimum_samples": 1, "promotion_margin": 0.05, "latency_penalty": 0.01},
        }
        path = self.root / "config.json"
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        return AppConfig.load(path)

    def _stage(self) -> None:
        self.store.stage_memory_candidate(self.task_id, str(self.note), self.sha)

    def _approve_and_publish(self) -> None:
        self._stage()
        self.store.review_memory(self.task_id, "delivery", "approve", "交付通过", self.sha)
        self.store.review_memory(self.task_id, "final", "approve", "终审通过", self.sha)
        self.store.mark_memory_published(self.task_id, str(self.note))

    def _hits(self, bridge: MemoryBridge) -> list:
        completed = subprocess.CompletedProcess(
            [], 0,
            json.dumps({"hits": [{"title": "note", "path": f"AgentOS/{self.task_id}.md"}]}),
            "",
        )
        with patch("xiaoba_agentos.memory.subprocess.run", return_value=completed):
            return bridge.retrieve_hits("查询")

    def test_published_memory_is_retrievable(self):
        self._approve_and_publish()
        bridge = MemoryBridge(self._config(), self.store)
        self.assertEqual(1, len(self._hits(bridge)))

    def test_unpublished_candidate_never_reaches_retrieval(self):
        self._stage()
        bridge = MemoryBridge(self._config(), self.store)
        self.assertEqual([], self._hits(bridge))
        self.assertEqual(
            {}, bridge.agent_contexts("查询", ["agy_review", "dsh_review"])["contexts"]
        )

    def test_delivery_approved_candidate_never_reaches_retrieval(self):
        self._stage()
        self.store.review_memory(self.task_id, "delivery", "approve", "交付通过", self.sha)
        bridge = MemoryBridge(self._config(), self.store)
        self.assertEqual([], self._hits(bridge))

    def test_pending_final_review_candidate_never_reaches_retrieval(self):
        self._stage()
        self.store.review_memory(self.task_id, "delivery", "approve", "交付通过", self.sha)
        self.store.review_memory(self.task_id, "final", "approve", "终审通过", self.sha)
        bridge = MemoryBridge(self._config(), self.store)
        self.assertEqual([], self._hits(bridge))

    def test_retired_memory_never_returns_even_if_file_still_active(self):
        self._approve_and_publish()
        bridge = MemoryBridge(self._config(), self.store)
        self.assertEqual(1, len(self._hits(bridge)))
        self.store.mark_memory_retired(self.task_id, str(self.root / "archive.md"), "用户退休")
        self.assertIn('status: "active"', self.note.read_text(encoding="utf-8"))
        self.assertEqual(
            [], self._hits(bridge), "数据库已退休的经验不得因为文件仍是 active 而回到检索"
        )

    def test_revalidation_required_memory_is_withheld(self):
        self._approve_and_publish()
        reuse_task = self.store.submit("reuse", 1)
        self.store.record_memory_reuse(reuse_task, self.task_id, str(self.note))
        self.store.decide_reuse_outcomes(reuse_task, False)
        self.assertEqual(
            "revalidation_required", self.store.get_memory_candidate(self.task_id)["status"]
        )
        bridge = MemoryBridge(self._config(), self.store)
        self.assertEqual([], self._hits(bridge))

    def test_publication_failed_memory_is_withheld_until_recovered(self):
        self._stage()
        self.store.review_memory(self.task_id, "delivery", "approve", "交付通过", self.sha)
        self.store.review_memory(self.task_id, "final", "approve", "终审通过", self.sha)
        self.store.mark_memory_publication_failed(self.task_id, "index rebuild failed")
        bridge = MemoryBridge(self._config(), self.store)
        self.assertEqual([], self._hits(bridge), "发布未结算的经验必须停在活动检索之外")
        self.store.mark_memory_published(self.task_id, str(self.note))
        self.assertEqual(1, len(self._hits(bridge)), "重试发布成功后应恢复可检索")

    def test_untracked_task_source_is_withheld_fail_closed(self):
        orphan = self.root / "vault" / "AgentOS" / "33333333-3333-3333-3333-333333333333.md"
        orphan.write_text(
            '---\nsource_task_id: "33333333-3333-3333-3333-333333333333"\n'
            'status: "active"\n---\n\n# 无数据库记录\n',
            encoding="utf-8",
        )
        bridge = MemoryBridge(self._config(), self.store)
        completed = subprocess.CompletedProcess(
            [], 0,
            json.dumps({"hits": [{"title": "orphan", "path": "AgentOS/" + orphan.name}]}),
            "",
        )
        with patch("xiaoba_agentos.memory.subprocess.run", return_value=completed):
            self.assertEqual(
                [], bridge.retrieve_hits("查询"),
                "claim 了 source_task_id 却没有数据库记录的经验必须拒绝放行",
            )

    def test_vault_native_note_without_task_id_keeps_frontmatter_gate(self):
        native = self.vault / "native.md"
        native.write_text(
            '---\nid: "native"\nstatus: "active"\nauthority: true\n---\n\n# 原生规则\n',
            encoding="utf-8",
        )
        bridge = MemoryBridge(self._config(), self.store)
        completed = subprocess.CompletedProcess(
            [], 0, json.dumps({"hits": [{"title": "native", "path": "native.md"}]}), ""
        )
        with patch("xiaoba_agentos.memory.subprocess.run", return_value=completed):
            hits = bridge.retrieve_hits("规则")
        self.assertEqual(1, len(hits), "没有 source_task_id 的 Vault 原生页面不受数据库门禁影响")
        self.assertIsNone(hits[0]["source_task_id"])

    def test_file_only_bridge_without_store_fails_closed_for_task_bound_memory(self):
        self._approve_and_publish()
        self.store.mark_memory_retired(self.task_id, str(self.root / "archive.md"), "用户退休")
        legacy_bridge = MemoryBridge(self._config())
        self.assertEqual(
            [], self._hits(legacy_bridge),
            "未绑定数据库时，声明 source_task_id 的经验必须故障关闭",
        )

    def test_lookup_failure_fails_closed(self):
        self._approve_and_publish()
        bridge = MemoryBridge(self._config(), self.store)
        with patch.object(
            self.store, "get_memory_candidate", side_effect=sqlite3.OperationalError("db locked")
        ):
            self.assertEqual(
                [], self._hits(bridge), "数据库不可读时必须故障关闭，不能放行未验证的经验"
            )


if __name__ == "__main__":
    unittest.main()
