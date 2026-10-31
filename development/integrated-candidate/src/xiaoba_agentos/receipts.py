from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

RECEIPT_FIELDS = (
    "task_id",
    "step_id",
    "agent",
    "model",
    "tool",
    "tool_call_id",
    "tool_result_id",
    "command_hash",
    "status",
    "exit_code",
    "started_at",
    "finished_at",
    "artifacts",
    "error",
)


class ReceiptError(RuntimeError, ValueError):
    """Raised when a receipt cannot be recorded or verified.

    Also a ValueError: evidence checks such as workflow's collaboration
    contributions report bad evidence that way, and a corrupt receipt must fail
    the same clean path, not escape as a worker error on every poll.
    """


class ExecutionReceiptStore:
    """Persist and audit execution receipts.

    A receipt is only considered a success when BOTH a real tool_call and its
    corresponding tool_result are present. Text-only tool_call markup never
    yields a valid receipt.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._receipts: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                raw = self.path.read_text(encoding="utf-8")
                data = json.loads(raw)
            except Exception as exc:
                raise ReceiptError(f"receipt file corrupt or unreadable: {self.path}") from exc
            if not isinstance(data, dict) or not isinstance(data.get("receipts"), dict):
                raise ReceiptError(f"receipt structure invalid: {self.path}")
            self._receipts = data["receipts"]

    def _persist(self) -> None:
        if self.path.exists():
            try:
                raw = self.path.read_text(encoding="utf-8")
                data = json.loads(raw)
                if not isinstance(data, dict) or not isinstance(data.get("receipts"), dict):
                    raise ReceiptError(f"receipt structure invalid on disk: {self.path}")
            except Exception as exc:
                if isinstance(exc, ReceiptError):
                    raise
                raise ReceiptError(f"receipt file corrupt on disk: {self.path}") from exc

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, pending = tempfile.mkstemp(prefix=".receipt-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(json.dumps({"receipts": self._receipts}, ensure_ascii=False, indent=2) + "\n")
            os.replace(pending, self.path)
        finally:
            if os.path.exists(pending):
                os.unlink(pending)

    @staticmethod
    def command_hash(command: str) -> str:
        return hashlib.sha256(command.encode("utf-8")).hexdigest()

    def record_call(self, receipt: Dict[str, Any]) -> str:
        """Record a real tool_call. Missing required fields raise ReceiptError."""
        for field in ("tool_call_id", "task_id", "step_id", "tool", "command_hash"):
            if not receipt.get(field):
                raise ReceiptError(f"receipt missing required field: {field}")
        receipt.setdefault("agent", "")
        receipt.setdefault("model", "")
        receipt.setdefault("status", "called")
        receipt.setdefault("exit_code", None)
        receipt.setdefault("started_at", time.time())
        receipt.setdefault("finished_at", None)
        receipt.setdefault("tool_result_id", "")
        receipt.setdefault("artifacts", [])
        receipt.setdefault("error", "")
        call_id = str(receipt["tool_call_id"])
        self._receipts[call_id] = dict(receipt)
        self._persist()
        return call_id

    def associate_result(
        self,
        tool_call_id: str,
        tool_result_id: str,
        exit_code: Optional[int] = 0,
        model: Optional[str] = None,
    ) -> bool:
        """Link a real tool_result to a previously recorded call.

        Returns True if the association succeeded.
        """
        receipt = self._receipts.get(str(tool_call_id))
        if receipt is None:
            return False
        if not tool_result_id:
            raise ReceiptError("tool_result_id must be non-empty")
        receipt["tool_result_id"] = str(tool_result_id)
        receipt["status"] = "succeeded" if exit_code == 0 else "failed"
        receipt["exit_code"] = exit_code
        receipt["finished_at"] = time.time()
        if model is not None:
            receipt["model"] = str(model)
        self._persist()
        return True

    def record_progress(self, tool_call_id: str, progress: Dict[str, Any]) -> None:
        """An observable heartbeat is never a tool result or successful receipt."""
        receipt = self._receipts.get(str(tool_call_id))
        if receipt is None:
            # Progress may only follow a recorded call; say so in this module's
            # own error type rather than a bare KeyError.
            raise ReceiptError(f"progress for unknown tool_call_id: {tool_call_id}")
        receipt["progress"] = {key: progress[key] for key in (
            "stage", "pid", "elapsed_seconds", "stdout_bytes", "activity_events",
            "no_progress_seconds", "idle_timeout_seconds"
        ) if key in progress}
        receipt["heartbeat_at"] = time.time()
        self._persist()

    def is_success(self, tool_call_id: str) -> bool:
        """A receipt is a success only when call + result are both present.

        A missing tool_result_id means the call never completed and must not be
        treated as successful.
        """
        receipt = self._receipts.get(str(tool_call_id))
        if receipt is None:
            return False
        return bool(receipt.get("tool_result_id")) and receipt.get("status") == "succeeded" and receipt.get("exit_code") == 0

    def get(self, tool_call_id: str) -> Optional[Dict[str, Any]]:
        return self._receipts.get(str(tool_call_id))

    def count(self) -> int:
        return len(self._receipts)

    @staticmethod
    def is_text_markup(text: str) -> bool:
        """Detect unexecuted tool_call markup in a candidate answer.

        Returns True when the text only contains tool invocation markup and no
        real executed tool_result, meaning it must not generate a valid receipt.
        """
        return bool(re.search(r"<invoke |</invoke>|<parameter |</parameter>|tool_call", text, re.IGNORECASE)) and not re.search(
            r"tool_result", text, re.IGNORECASE
        )
