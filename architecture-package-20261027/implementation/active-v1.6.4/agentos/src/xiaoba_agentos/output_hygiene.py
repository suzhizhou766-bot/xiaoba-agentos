"""Output hygiene: strip reasoning markup and detect leaked chain-of-thought.

Two independent layers are needed because a model can leak its reasoning either
as tagged markup or as untagged first-person prose.  Tagged markup is removed
mechanically; untagged prose cannot be rewritten safely, so it is only detected
and the contaminated candidate is quarantined by the caller.
"""

from __future__ import annotations

import re

_REASONING_TAG_NAMES = r"think|thinking|thought|reasoning|reasoning_content|chain_of_thought|cot"

_REASONING_BLOCK = re.compile(
    rf"<\s*(?:{_REASONING_TAG_NAMES})\b[^>]*>.*?<\s*/\s*(?:{_REASONING_TAG_NAMES})\s*>",
    re.DOTALL | re.IGNORECASE,
)

_REASONING_TAG = re.compile(rf"<\s*/?\s*(?:{_REASONING_TAG_NAMES})\s*>", re.IGNORECASE)

_UNPAIRED_CLOSER = re.compile(rf"<\s*/\s*(?:{_REASONING_TAG_NAMES})\s*>", re.IGNORECASE)

# Only the wire field name is treated as a label.  Prose headings such as
# "Reasoning:" are legitimate answer structure and must survive.
_REASONING_LABEL = re.compile(
    r"^[ \t]*reasoning_content[ \t]*[:：][ \t]*\S.*$",
    re.MULTILINE | re.IGNORECASE,
)

# Markup that must never survive into a delivered answer.  A single hit is
# enough: these strings have no legitimate use in a review result.
HARD_LEAK_SIGNATURES = (
    "<think",
    "</think",
    "<thinking",
    "</thinking",
    "<reasoning",
    "</reasoning",
    "reasoning_content",
    "<｜dsml｜reasoning",
    "<|dsml|reasoning",
    "chain_of_thought",
)

# First-person meta-commentary about the request instead of an answer, plus
# verbatim echo of the instructions AgentOS itself injects.  Individually these
# can appear in legitimate prose, so contamination requires two distinct hits.
SOFT_LEAK_SIGNATURES = (
    "the user is asking",
    "the user wants me to",
    "the user asked",
    "the user's request",
    "the prompt says",
    "the system prompt",
    "as an ai",
    "let me think",
    "let me check",
    "let me re-read",
    "let me work",
    "let me first",
    "i need to figure",
    "i should probably",
    "i'll start by",
    "so the answer is",
    "but wait",
    "wait, let me",
    "hmm,",
    "okay, so",
    "ok, so",
    "你当前只产出独立候选答案",
    "最终来源标签由 agentos 统一添加",
    "请给出明确结论、依据、风险和可验证步骤",
    "任务上下文与参考资料（按各段来源区分当前证据、技能和历史记忆）",
)

# Unexecuted tool invocation markup and serialization leaks that must never
# appear in candidate answers or acceptance payloads.
NON_ANSWER_MARKERS = (
    "<invoke ",
    "</invoke>",
    "<parameter ",
    "</parameter>",
    "<｜dsml｜",
    "<|dsml|",
    "tool-call serialization",
    "tool call serialization",
)


def contains_non_answer_markers(text: str) -> bool:
    """Return True if text contains unexecuted tool markup or serialization markers."""
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in NON_ANSWER_MARKERS)


def detect_non_answer_markers(text: str) -> list[str]:
    """Return the non-answer tool markers found in text; an empty list means clean."""
    if not text:
        return []
    lowered = text.lower()
    return [marker for marker in NON_ANSWER_MARKERS if marker in lowered]


def strip_reasoning_markup(text: str) -> str:
    """Remove reasoning blocks, stray reasoning tags and reasoning labels."""
    if not text:
        return text
    stripped = _REASONING_BLOCK.sub("", text)
    # A closer with no matching opener means the model started reasoning before
    # the captured window; everything up to the last such closer is reasoning
    # and the deliverable answer follows it.
    unpaired = list(_UNPAIRED_CLOSER.finditer(stripped))
    if unpaired:
        tail = stripped[unpaired[-1].end():]
        # Keep the tail only when there is one; a trailing stray closer with
        # nothing after it means the preceding text is the answer itself.
        stripped = tail if tail.strip() else _UNPAIRED_CLOSER.sub("", stripped)
    stripped = _REASONING_TAG.sub("", stripped)
    stripped = _REASONING_LABEL.sub("", stripped)
    if stripped == text:
        return text
    return re.sub(r"\n{3,}", "\n\n", stripped).strip()


def detect_reasoning_leak(text: str) -> list[str]:
    """Return the leak signatures found in text; an empty list means clean."""
    if not text:
        return []
    lowered = text.lower()
    hard = [marker for marker in HARD_LEAK_SIGNATURES if marker in lowered]
    if hard:
        return hard
    soft = [marker for marker in SOFT_LEAK_SIGNATURES if marker in lowered]
    return soft if len(soft) >= 2 else []
