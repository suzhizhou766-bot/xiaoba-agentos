"""W2-I & W2-J regression tests: release docs hygiene, preflight gates, parallel suites,
review fault isolation/convergence, and <30s browser smoke.

测试 ← 踩坑日期映射（防复发十条）：
- W2-I1（第二大脑当前态去 AGY+DSH、动态日期、清理旧 v1.5.2 确认尾段）← 2026-11-22 系统当前态长期残留旧口径
- W2-I2（RELEASE.json 摆脱硬编码双后台 + 版本文档统一打标）← 2026-11-22 RELEASE.json 仍写死 Antigravity/DeepSeekHarness
- W2-J1（发布前静态预检：语法/字节码/禁词/池节点/推迟项对账）← 2026-11-22 多次带着过时表述或 .pyc 进审查
- W2-J2（构包默认支持并行 4 套基线回归）← 2026-11-22 串行回归耗时过长
- W2-J3（审查节点失败分级、隔离目录退避重试、后台心跳）← 2026-11-22 瞬时 EOF 与硬配置错误混为一谈
- W2-J4（第 2+ 轮增量差异审查包 + 3 轮未收敛熔断 + 终轮 P0-only）← 2026-11-22 多轮全仓重审导致审查发散
- W2-J5（<30 秒 5 路径前端冒烟）← 2026-11-22 监看页面回归靠人工肉眼兜底
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

CANDIDATE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = CANDIDATE_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import browser_smoke_v1618 as smoke  # noqa: E402
import build_release  # noqa: E402
import build_second_brain  # noqa: E402
import release_preflight  # noqa: E402
import release_review_orchestrator as rro  # noqa: E402
import switch_release  # noqa: E402


class W2IReleaseDocsAndSecondBrainTests(unittest.TestCase):
    def test_i1_second_brain_uses_agent_pool_and_dynamic_verified_date(self):
        """W2-I1 (← 2026-11-22): 00/系统当前态/历史版本记录 drop AGY+DSH and use dynamic date."""
        rendered = build_second_brain.build(CANDIDATE_ROOT, verified_date="2026-11-23")
        root_md = rendered["00_小八AgentOS.md"]
        state_md = rendered["60_当前上下文/系统当前态.md"]
        self.assertIn("插件化 Agent 池", root_md)
        self.assertNotIn("AGY+DSH双后台", root_md)
        self.assertIn("插件化 Agent 池", state_md)
        self.assertNotIn("AGY+DSH双后台", state_md)
        self.assertIn('last_verified: "2026-11-23"', state_md)
        self.assertNotIn('last_verified: "2026-10-11"', state_md)
        self.assertEqual(build_second_brain.validate(rendered), [])

    def test_i1_switch_release_strips_obsolete_2026_10_10_tail_but_preserves_user_appendix(self):
        """W2-I1 (← 2026-11-22): switch_release removes obsolete 2026-10-10 confirmation while keeping real user notes."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td).resolve()
            prev = tmp / "prev"
            pkg = tmp / "pkg"
            live_sb = tmp / "vault"
            rel = "60_当前上下文/系统当前态.md"
            for base in (prev / "second-brain", pkg / "second-brain", live_sb):
                (base / "60_当前上下文").mkdir(parents=True, exist_ok=True)

            orig = "# 系统当前态\n\n- 旧正文\n"
            new_gen = "# 系统当前态\n\n- 新正文（插件化 Agent 池）\n"
            obsolete_tail = (
                "\n## 2026-10-10 上线确认\n\n"
                "用户确认后，已提升为实际活动版本。\n\n"
                "源码清单：`c38573ad09256300bd1cccc498ff461b606ad5ebd99beace336154d69ec4a8d3`。\n"
            )
            user_note = "\n## 2026-11-23 用户补充说明\n\n保留这条真实人工备注。\n"

            (prev / "second-brain" / rel).write_text(orig, encoding="utf-8")
            (pkg / "second-brain" / rel).write_text(new_gen, encoding="utf-8")
            (live_sb / rel).write_text(orig + obsolete_tail + user_note, encoding="utf-8")

            prev_sha = hashlib.sha256(orig.encode("utf-8")).hexdigest()
            new_sha = hashlib.sha256(new_gen.encode("utf-8")).hexdigest()
            (prev / "SECOND_BRAIN_MANIFEST.sha256").write_text(f"{prev_sha}  ./{rel}\n", encoding="utf-8")
            (pkg / "SECOND_BRAIN_MANIFEST.sha256").write_text(f"{new_sha}  ./{rel}\n", encoding="utf-8")

            layout = types.SimpleNamespace(package=pkg, vault=live_sb)
            actions = switch_release.vault_actions(layout, prev)
            self.assertEqual(len(actions), 1)
            merged_text = actions[0][1].decode("utf-8")
            self.assertIn("新正文（插件化 Agent 池）", merged_text)
            self.assertNotIn("2026-10-10 上线确认", merged_text)
            self.assertIn("2026-11-23 用户补充说明", merged_text)
            self.assertIn("保留这条真实人工备注。", merged_text)

    def test_i2_dynamic_provider_priority_and_stamp_release_docs(self):
        """W2-I2 (← 2026-11-22): RELEASE.json providers come from acceptance evidence; docs stamping works."""
        self.assertEqual(
            build_release._release_provider_names(None),
            ["AgentPool"],
        )
        self.assertEqual(
            build_release._release_provider_names(
                {"conclusions": [{"model": "qoder-auto", "node_id": "n1"}, {"node_id": "CodeBuddy"}]}
            ),
            ["qoder-auto", "CodeBuddy"],
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "docs").mkdir()
            rel_doc = root / "docs" / "RELEASE_V1.6.18.md"
            rel_doc.write_text("# 发布说明\n\n状态：候选未发布；旧口径：AGY+DSH双后台\n", encoding="utf-8")
            touched = build_release.stamp_release_docs(
                root,
                version="v1.6.18",
                verified_date="2026-11-23",
                review_nodes=["Qoder", "CodeBuddy"],
            )
            self.assertEqual(touched, ["docs/RELEASE_V1.6.18.md"])
            stamped = rel_doc.read_text(encoding="utf-8")
            self.assertIn("已正式发布（2026-11-23）", stamped)
            self.assertIn("Qoder + CodeBuddy", stamped)
            self.assertNotIn("候选未发布", stamped)
            self.assertNotIn("AGY+DSH双后台", stamped)


class W2JPreflightAndParallelTests(unittest.TestCase):
    def test_j1_preflight_catches_syntax_pyc_forbidden_prose_pool_and_deferred(self):
        """W2-J1 (← 2026-11-22): static preflight catches .py syntax, .pyc pollution, forbidden prose, pool & ledger."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "development" / "integrated-candidate"
            (root / "src").mkdir(parents=True)
            (root / "docs").mkdir(parents=True)
            (root.parent / "interface").mkdir(parents=True)

            # 1) Syntax error + .pyc pollution
            (root / "src" / "bad.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
            (root / "src" / "leftover.pyc").write_bytes(b"pyc")
            rep = release_preflight._new_report()
            release_preflight.check_syntax_and_bytecode(rep, root)
            codes = {i["code"] for i in rep["issues"]}
            self.assertIn("python_syntax_error", codes)
            self.assertIn("bytecode_pollution", codes)

            # 2) Forbidden prose
            (root.parent / "interface" / "SKILL.md").write_text("当前为 AGY+DSH双后台 候选未发布", encoding="utf-8")
            rep2 = release_preflight._new_report()
            release_preflight.check_forbidden_release_prose(rep2, root, "1.6.18")
            self.assertIn("forbidden_release_prose", {i["code"] for i in rep2["issues"]})

            # 3) Pool DB check (<2 enabled nodes triggers issue, >=2 passes)
            pool_db = Path(td) / "pool.sqlite3"
            conn = sqlite3.connect(pool_db)
            conn.execute("CREATE TABLE ri_sources (id TEXT PRIMARY KEY, data TEXT)")
            conn.execute(
                "INSERT INTO ri_sources VALUES (?, ?)",
                ("n1", json.dumps({"kind": "review", "node": {"name": "Qoder", "enabled": True, "entry": "/bin/qoder", "model": "auto"}})),
            )
            conn.commit()
            rep3 = release_preflight._new_report()
            release_preflight.check_pool_nodes_live(rep3, pool_db)
            self.assertIn("pool_reviewers_insufficient", {i["code"] for i in rep3["issues"]})

            conn.execute(
                "INSERT INTO ri_sources VALUES (?, ?)",
                ("n2", json.dumps({"kind": "both", "node": {"name": "CodeBuddy", "enabled": True, "entry": "/bin/cb", "model": "glm-5"}})),
            )
            conn.commit()
            conn.close()
            rep3_ok = release_preflight._new_report()
            release_preflight.check_pool_nodes_live(rep3_ok, pool_db)
            self.assertTrue(rep3_ok["ok"])
            self.assertEqual(rep3_ok["checks"]["pool_nodes"]["enabled_review_count"], 2)

            # 4) Deferred ledger check
            ledger = Path(td) / "deferred.json"
            ledger.write_text(json.dumps({"items": [{"id": "W3-1", "reason": "defer"}]}), encoding="utf-8")
            rep4 = release_preflight._new_report()
            release_preflight.check_deferred_items(rep4, ledger)
            self.assertIn("deferred_items_incomplete", {i["code"] for i in rep4["issues"]})

            ledger.write_text(
                json.dumps({"items": [{"id": "W3-1", "reason": "wave3", "target_version": "v1.6.19", "owner": "host"}]}),
                encoding="utf-8",
            )
            rep4_ok = release_preflight._new_report()
            release_preflight.check_deferred_items(rep4_ok, ledger)
            self.assertTrue(rep4_ok["ok"])

    def test_j2_build_parser_exposes_parallel_suites_flag(self):
        """W2-J2 (← 2026-11-22): build_release exposes --parallel-suites CLI flag."""
        parser = build_release.build_parser()
        args = parser.parse_args([
            "--second-brain", "/tmp/sb",
            "--published-agentos-root", "/tmp/aos",
            "--target", "/tmp/out",
            "--version", "v1.6.18",
            "--verified-date", "2026-11-23",
            "--test-count", "100",
            "--parallel-suites",
        ])
        self.assertTrue(args.parallel_suites)


class W2JReviewOrchestratorTests(unittest.TestCase):
    def test_j3_failure_classification_hard_vs_transient_vs_unknown(self):
        """W2-J3 (← 2026-11-22): hard config errors stop immediately; transient EOF/TLS retry."""
        hard = rro.classify_review_failure("HTTP 404 model_not_found: invalid model", exit_code=1)
        self.assertEqual(hard["kind"], "hard_config")
        self.assertFalse(hard["retryable"])

        auth = rro.classify_review_failure("401 unauthorized: creditStatus unknown", exit_code=1)
        self.assertEqual(auth["kind"], "hard_config")
        self.assertFalse(auth["retryable"])

        eof = rro.classify_review_failure("TLS handshake failed: unexpected EOF while reading stream", exit_code=1)
        self.assertEqual(eof["kind"], "transient_network")
        self.assertTrue(eof["retryable"])
        self.assertEqual(eof["max_retries"], 2)
        self.assertEqual(eof["backoff_seconds"], 15.0)

        unk = rro.classify_review_failure("mysterious custom assertion", exit_code=2)
        self.assertEqual(unk["kind"], "unknown")
        self.assertFalse(unk["retryable"])

    def test_j3_isolated_retry_preserves_failed_dir_and_writes_heartbeat(self):
        """W2-J3 (← 2026-11-22): transient failures retry in fresh isolated dirs without wiping old receipts."""
        with tempfile.TemporaryDirectory() as td:
            work_root = Path(td)
            sleeps: list[float] = []

            def attempt_fn(attempt_dir: Path, attempt_idx: int) -> dict:
                (attempt_dir / "receipt.txt").write_text(f"attempt-{attempt_idx}", encoding="utf-8")
                if attempt_idx == 0:
                    return {"ok": False, "error": "unexpected EOF during TLS read", "exit_code": 1}
                return {"ok": True, "verdict": "PASS"}

            res = rro.execute_with_retry_isolation(
                attempt_fn,
                work_root=work_root,
                run_id="qoder-r1",
                max_retries=2,
                backoff_seconds=15.0,
                heartbeat_interval=0.05,
                sleep_fn=sleeps.append,
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["attempts_used"], 2)
            self.assertEqual(sleeps, [15.0])
            dir0 = Path(res["attempts"][0]["attempt_dir"])
            dir1 = Path(res["attempts"][1]["attempt_dir"])
            self.assertNotEqual(dir0, dir1)
            self.assertEqual((dir0 / "receipt.txt").read_text(encoding="utf-8"), "attempt-0")
            self.assertEqual((dir1 / "receipt.txt").read_text(encoding="utf-8"), "attempt-1")
            hb1 = json.loads((dir1 / "heartbeat.json").read_text(encoding="utf-8"))
            self.assertEqual(hb1["status"], "finished")
            self.assertGreaterEqual(hb1["beats"], 2)

    def test_j3_hard_config_never_retries(self):
        """W2-J3 (← 2026-11-22): hard config error aborts on attempt 0 with zero sleeps."""
        with tempfile.TemporaryDirectory() as td:
            sleeps: list[float] = []
            calls = 0

            def attempt_fn(_attempt_dir: Path, _idx: int) -> dict:
                nonlocal calls
                calls += 1
                return {"ok": False, "error": "engine_retired: workbuddy removed", "exit_code": 1}

            res = rro.execute_with_retry_isolation(
                attempt_fn,
                work_root=Path(td),
                run_id="hard-fail",
                sleep_fn=sleeps.append,
            )
            self.assertFalse(res["ok"])
            self.assertEqual(calls, 1)
            self.assertEqual(sleeps, [])
            self.assertEqual(res["stopped_reason"], "hard_config")

    def test_j4_incremental_packet_p0_only_and_3_round_circuit_breaker(self):
        """W2-J4 (← 2026-11-22): round 2+ sends only changed files + defect checklist; round 4 trips breaker."""
        m1 = {"a.py": "111", "b.py": "222", "c.py": "333"}
        m2 = {"a.py": "111", "b.py": "222-fixed", "d.py": "444"}
        defects = [{"id": "P0-1", "severity": "P0", "file": "b.py", "summary": "missing check"}]

        p1 = rro.build_incremental_review_packet(1, prev_manifest=None, curr_manifest=m1)
        self.assertTrue(p1["allow_dispatch"])
        self.assertEqual(p1["mode"], "full_review")
        self.assertEqual(p1["files_to_review"], ["a.py", "b.py", "c.py"])

        p2 = rro.build_incremental_review_packet(2, prev_manifest=m1, curr_manifest=m2, prev_defects=defects)
        self.assertTrue(p2["allow_dispatch"])
        self.assertEqual(p2["mode"], "incremental_recheck")
        self.assertEqual(p2["files_to_review"], ["b.py", "c.py", "d.py"])
        self.assertNotIn("a.py", p2["files_to_review"])
        self.assertEqual(len(p2["defect_checklist"]), 1)

        p3 = rro.build_incremental_review_packet(
            3, prev_manifest=m1, curr_manifest=m2, prev_defects=defects, final_p0_only=True
        )
        self.assertTrue(p3["allow_dispatch"])
        self.assertEqual(p3["mode"], "final_p0_only")
        self.assertEqual(p3["allowed_severities"], ["P0"])
        md = rro.render_packet_markdown(p3)
        self.assertIn("P0-only", md)

        p4 = rro.build_incremental_review_packet(4, prev_manifest=m1, curr_manifest=m2, prev_defects=defects)
        self.assertFalse(p4["allow_dispatch"])
        self.assertEqual(p4["status"], "circuit_breaker_user_decision")
        self.assertIn("熔断", p4["reason"])


class W2JBrowserSmokeTests(unittest.TestCase):
    def test_j5_browser_smoke_passes_all_five_paths_under_30s(self):
        """W2-J5 (← 2026-11-22): 5-path UI smoke completes within 30s with all paths green."""
        report = smoke.run_browser_smoke(CANDIDATE_ROOT)
        self.assertTrue(report["ok"], json.dumps(report, ensure_ascii=False, indent=2))
        self.assertTrue(report["within_budget"])
        self.assertLess(report["elapsed_seconds"], 30.0)
        self.assertEqual(len(report["paths"]), 5)


if __name__ == "__main__":
    unittest.main()
