"""J11c historical direct evidence and mixed legacy-candidate regression cases."""
import json
import unittest
from pathlib import Path

import test_validation_basis as basis_tests
from fixtures_review import _admit_authored_candidate, _run_cli
from xiaoba_agentos.review_flow import execute_review
from xiaoba_agentos.validation_basis import propose_historical_basis_tx, validate_basis


class HistoricalBasisFlowTests(unittest.TestCase):
    # Share fixture operations without inheriting or rediscovering its test cases.
    setUp = basis_tests.ValidationBasisTests.setUp
    file_ref = basis_tests.ValidationBasisTests.file_ref
    candidate = basis_tests.ValidationBasisTests.candidate
    event_count = basis_tests.ValidationBasisTests.event_count
    final_review_event_count = basis_tests.ValidationBasisTests.final_review_event_count
    confirm = basis_tests.ValidationBasisTests.confirm
    final_approval = basis_tests.ValidationBasisTests.final_approval

    def historical_proposal(self, index, *, claims=None):
        work = self.file_ref(f"history-work-{index}.json", json.dumps({"task": f"TEST historical artwork {index}"}))
        acceptance = self.file_ref(f"history-acceptance-{index}.txt", f"TEST original user adopted artwork {index}")
        artifact = self.file_ref(f"history-artifact-{index}.txt", f"TEST adopted artwork {index}") | {"version": "v1"}
        execution = self.file_ref(f"history-execution-{index}.txt", f"TEST recorded execution {index}")
        return {
            "version": 1,
            "history_pack": {
                "version": 1, "workspace": str(self.project), "task_id": f"TEST-historical-{index}",
                "original_task_ref": work["path"], "accepted_at": "TEST original 2026-10-15",
                "work_record": work, "original_acceptance": acceptance,
                "artifact": artifact, "execution_evidence": [execution],
            },
            "method_scope": "TEST artwork adjustments recorded in the original task",
            "method_claims": claims if claims is not None else [
                {"claim": "TEST inspect the adopted artwork", "class": "artifact"},
            ],
            "application_scope": {
                "description": "TEST historical artwork task", "task_type": "TEST artwork editing",
                "authorization_ref": acceptance["path"], "application_location": str(self.project),
                "steps_applied": "TEST applied the recorded adjustments", "deviations": "TEST none recorded",
                "conditions_checked": {
                    "applicability": "TEST same artwork family", "exclusions": "TEST none omitted",
                    "environment": "TEST original project environment",
                },
            },
        }

    def enable_temporary_publication(self):
        project_root = Path(__file__).resolve().parents[1]
        self.config.raw["project_dir"] = str(project_root)
        self.config.raw["memory"].update({
            "publish_enabled": True,
            "obsidian_subdir": "70_复利规则/经验",
            "catalog_note": "70_复利规则/经验索引.md",
            "knowlp_graph_dir": str(self.root / "runtime" / "graph"),
        })
        self.config.path.write_text(json.dumps(self.config.raw, ensure_ascii=False), encoding="utf-8")
        vault = self.root / "vault"
        catalog = vault / "70_复利规则" / "经验索引.md"
        catalog.parent.mkdir(parents=True)
        (vault / "root.md").write_text(
            '---\nid: "root"\nparent: ""\nstatus: "active"\n---\n\n'
            '# TEST root\n\n- [[70_复利规则/经验索引]]\n', encoding="utf-8",
        )
        catalog.write_text(
            '---\nid: "experiences"\nparent: "root"\nstatus: "active"\n---\n\n# TEST 经验索引\n',
            encoding="utf-8",
        )
        return vault, catalog

    def test_historical_direct_proposal_confirms_and_publishes_into_temporary_vault(self):
        vault, catalog = self.enable_temporary_publication()
        candidate = self.candidate()
        proposal = self.historical_proposal("full-flow")
        proposal["candidate_id"] = candidate["task_id"]
        receipt = self.root / "history-pack.json"
        receipt.write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")
        with self.store.read_only() as conn:
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        code, proposed = _run_cli([
            "--config", str(self.config.path), "memory-validation-basis-propose", candidate["task_id"],
            "--receipt", str(receipt),
        ])
        self.assertEqual(0, code, proposed)
        self.assertEqual("historical_direct", proposed["task_origin"])
        self.assertEqual("proposed", proposed["status"])
        self.assertEqual(0, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))
        self.assertEqual([], list((vault / "70_复利规则" / "经验").glob("*.md")))

        code, published = execute_review(
            self.config, self.store, candidate["task_id"], "final", "approve", "TEST final user decision",
            expected_sha256=candidate["evidence_sha256"], validation_basis_confirmation=self.confirm(proposed),
        )

        self.assertEqual(0, code, published)
        self.assertTrue(published["publication"]["index"]["ok"])
        current = self.store.get_memory_candidate(candidate["task_id"])
        self.assertEqual("published", current["status"])
        note = Path(current["published_path"])
        self.assertTrue(note.is_file())
        self.assertTrue(note.is_relative_to(vault))
        self.assertIn("TEST reusable method and bounded claims", note.read_text(encoding="utf-8"))
        self.assertIn(note.stem, catalog.read_text(encoding="utf-8"))
        self.assertTrue(any((self.root / "runtime" / "graph").iterdir()))
        self.assertEqual(1, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))
        self.assertEqual(1, self.final_review_event_count(candidate["task_id"]))
        self.assertEqual(1, self.event_count(candidate["task_id"], "memory.published"))
        with self.store.read_only() as conn:
            basis = validate_basis(conn, current, require_confirmed=True)
            self.assertEqual(task_count, conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
            self.assertIsNone(conn.execute("SELECT 1 FROM tasks WHERE id='TEST-historical-full-flow'").fetchone())
        self.assertEqual("confirmed", basis["status"])
        self.assertEqual("historical_direct", basis["task_origin"])
        self.assertTrue(self.store.verify_event_chain()["ok"])

    def test_28_legacy_candidates_in_four_evidence_categories_never_auto_confirm_or_publish(self):
        categories = ("adopted", "not_adopted", "mixed", "unclear_scope")
        candidates = []
        before_snapshots = {}
        proposed_by_category = {category: 0 for category in categories}
        for index in range(28):
            category = categories[index % len(categories)]
            created = self.store.create_authored_candidate(
                f"TEST historical {category} {index}", f"TEST {category} method evidence case {index}",
                self.config.runtime_dir / "memory-outbox", author="test", experience_kind="method",
                problem_key="parse-fixture",
            )
            candidate = _admit_authored_candidate(self.store, created)
            self.store.review_memory(
                candidate["task_id"], "delivery", "approve", "TEST prior initial user decision",
                candidate["evidence_sha256"],
            )
            candidates.append(candidate)
            with self.store.read_only() as conn:
                before_snapshots[candidate["task_id"]] = tuple(conn.execute(
                    "SELECT execution_snapshot_json,execution_snapshot_hash FROM tasks WHERE id=?",
                    (candidate["task_id"],),
                ).fetchone())
            proposal = self.historical_proposal(index)
            if category == "not_adopted":
                # A non-adopted case has no original acceptance to bind into a history pack.
                proposal["history_pack"].pop("original_acceptance")
            if category == "mixed":
                proposal["method_claims"].append({"claim": "TEST execute the artwork export command", "class": "code"})
            if category == "unclear_scope":
                proposal["method_claims"] = []

            with self.store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if category in {"not_adopted", "unclear_scope"}:
                    with self.assertRaises(ValueError):
                        propose_historical_basis_tx(self.store, conn, candidate, proposal)
                else:
                    basis = propose_historical_basis_tx(self.store, conn, candidate, proposal)
                    self.assertEqual("proposed", basis["status"])
                    self.assertEqual("combined" if category == "mixed" else "real_task_acceptance", basis["route"])
                    proposed_by_category[category] += 1

            if category == "mixed":
                # Adopted output still leaves the command claim subject to sandbox validation.
                with self.assertRaises(ValueError):
                    self.final_approval(candidate, basis, self.confirm(basis))
            with self.store.read_only() as conn:
                current = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
            self.assertEqual("missing" if category in {"not_adopted", "unclear_scope"} else "proposed", current["status"])

        self.assertEqual({"adopted": 7, "not_adopted": 0, "mixed": 7, "unclear_scope": 0}, proposed_by_category)
        with self.store.read_only() as conn:
            for candidate in candidates:
                task_id = candidate["task_id"]
                snapshot = tuple(conn.execute(
                    "SELECT execution_snapshot_json,execution_snapshot_hash FROM tasks WHERE id=?", (task_id,),
                ).fetchone())
                self.assertEqual(before_snapshots[task_id], snapshot)
                self.assertEqual(0, self.event_count(task_id, "memory.validation_basis_confirmed"))
                self.assertEqual(0, self.final_review_event_count(task_id))
                self.assertEqual(0, self.event_count(task_id, "memory.published"))
                row = self.store.get_memory_candidate(task_id)
                self.assertIsNone(row["published_path"])
                self.assertNotEqual("published", row["status"])
        self.assertTrue(self.store.verify_event_chain()["ok"])


if __name__ == "__main__":
    unittest.main()
