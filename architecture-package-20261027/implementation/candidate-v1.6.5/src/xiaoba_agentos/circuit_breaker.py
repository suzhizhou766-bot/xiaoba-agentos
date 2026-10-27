from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Tuple


@dataclass
class StepState:
    """Track consecutive execution misses for one (task_id, step_id)."""
    task_id: str
    step_id: str
    consecutive_misses: int = 0
    consecutive_failures: int = 0
    error_signature: str = ""
    tripped: bool = False
    reason: str = ""


class CircuitBreaker:
    """Execution circuit breaker.

    A miss is recorded when a step produces no real tool_call or a tool_call
    without a corresponding tool_result. After a configurable threshold of
    consecutive misses the breaker trips and the step must fail closed
    (blocked / failed_closed) instead of retrying with more natural language.
    """

    def __init__(self, threshold: int = 2):
        self.threshold = max(1, int(threshold))
        self._steps: Dict[Tuple[str, str], StepState] = {}

    def _key(self, task_id: str, step_id: str) -> Tuple[str, str]:
        return (str(task_id), str(step_id))

    def record_step(self, task_id: str, step_id: str, tool_calls: int, tool_results: int, *, exit_code: int = 0, error_signature: str = "") -> StepState:
        """Record the outcome of one step execution.

        A real execution must have at least one tool_call AND a matching
        tool_result. Anything else counts as a miss.
        """
        key = self._key(task_id, step_id)
        state = self._steps.get(key) or StepState(task_id=task_id, step_id=step_id)
        if state.tripped:
            return state  # Explicit reset is required; narration/success cannot reopen it.
        executed = tool_calls >= 1 and tool_results >= tool_calls
        if executed and (exit_code != 0 or error_signature):
            state.consecutive_misses = 0
            state.consecutive_failures += 1
            same_error = bool(error_signature) and error_signature == state.error_signature
            state.error_signature = error_signature
            if state.consecutive_failures >= self.threshold:
                state.tripped = True
                state.reason = "repeated_tool_error" if same_error else "repeated_step_failure"
        elif executed:
            state.consecutive_failures = 0
            state.error_signature = ""
            state.consecutive_misses = 0
            state.tripped = False
            state.reason = ""
        else:
            state.consecutive_misses += 1
            if state.consecutive_misses >= self.threshold:
                state.tripped = True
                state.reason = "missing_real_tool_execution"
        self._steps[key] = state
        return state

    def is_tripped(self, task_id: str, step_id: str) -> bool:
        state = self._steps.get(self._key(task_id, step_id))
        return bool(state and state.tripped)

    def reason(self, task_id: str, step_id: str) -> str:
        state = self._steps.get(self._key(task_id, step_id))
        return state.reason if state else ""

    def state(self, task_id: str, step_id: str) -> StepState:
        return self._steps.get(self._key(task_id, step_id)) or StepState(task_id=task_id, step_id=step_id)

    def reset(self, task_id: str, step_id: str) -> None:
        self._steps.pop(self._key(task_id, step_id), None)
