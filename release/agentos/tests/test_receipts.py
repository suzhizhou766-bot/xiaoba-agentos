import tempfile
import unittest
from pathlib import Path

from xiaoba_agentos.receipts import ExecutionReceiptStore, ReceiptError


class ReceiptStoreTest(unittest.TestCase):
    def test_real_call_plus_result_is_success(self):
        with tempfile.TemporaryDirectory() as d:
            store = ExecutionReceiptStore(Path(d) / "receipts.json")
            store.record_call({
                "task_id": "t", "step_id": "s", "agent": "dsh_review", "tool": "bash",
                "tool_call_id": "c1", "command_hash": ExecutionReceiptStore.command_hash("ls"),
            })
            linked = store.associate_result("c1", "r1", exit_code=0)
            self.assertTrue(linked)
            self.assertTrue(store.is_success("c1"))

    def test_call_without_result_is_not_success(self):
        with tempfile.TemporaryDirectory() as d:
            store = ExecutionReceiptStore(Path(d) / "receipts.json")
            store.record_call({
                "task_id": "t", "step_id": "s", "agent": "a", "tool": "bash",
                "tool_call_id": "c2", "command_hash": "h",
            })
            self.assertFalse(store.is_success("c2"))

    def test_text_markup_generates_no_valid_receipt(self):
        store = ExecutionReceiptStore(Path("/tmp/receipts-x.json"))
        markup = '<invoke name="bash"><parameter name="cmd">ls</parameter></invoke>'
        self.assertTrue(store.is_text_markup(markup))
        self.assertIsNone(store.get("c9"))

    def test_command_hash_is_deterministic(self):
        self.assertEqual(ExecutionReceiptStore.command_hash("ls -la"),
                         ExecutionReceiptStore.command_hash("ls -la"))

    def test_empty_result_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            r=ExecutionReceiptStore(Path(d)/'receipt.json')
            r.record_call(dict(task_id='t',step_id='s',tool='bash',tool_call_id='c',command_hash='h'))
            with self.assertRaises(ReceiptError): r.associate_result('c','')
            self.assertFalse(r.is_success('c'))


if __name__ == "__main__":
    unittest.main()
