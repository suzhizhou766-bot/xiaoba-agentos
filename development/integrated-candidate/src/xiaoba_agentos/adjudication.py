"""Prompt-derived output rules for the deterministic judge.

The judge only ever sees the prompt and the candidate answers, so the only output
rules it can legitimately enforce are the ones the prompt itself states.  Two
families are mechanically checkable without inventing a threshold: an answer that
must be nothing but a numeral, and an answer that must be an exact number of
lines.  Vague asks such as "简短" or "brief" are deliberately not detected --
guessing a character budget for them would dress an arbitrary number up as a rule.

`asks_for_restatement` is the separate question of whether echoing the prompt is
even a valid quality signal.  It is kept narrow and high precision because a
false positive silently re-enables the bias this module exists to remove.
"""
from __future__ import annotations

import re
from typing import List, NamedTuple, Optional

_RESTATEMENT_MARKERS = (
    "复述",
    "逐字",
    "照抄",
    "原样",
    "一字不差",
    "引用原文",
    "原文输出",
    "原文照录",
    "verbatim",
    "word for word",
    "word-for-word",
    "quote exactly",
    "repeat exactly",
    "exactly as written",
    "copy the following",
)

_NUMERAL_ASK = re.compile(
    r"(?:只|仅)\s*(?:回答|给出|输出|返回)[^。；\n]{0,20}数字"
    r"|(?:only\s+)?(?:output|print|return|give|respond\s+with)\s+(?:only\s+)?[^.;\n]{0,30}(?:number|numeral|digit)"
    r"|(?:arabic\s+)?numeral\s+only"
    r"|answer\s+with\s+(?:a|the)\s+(?:single\s+)?number",
    re.IGNORECASE,
)
_NO_EXTRA_TEXT = re.compile(
    r"不要输出任何其他文字|不得输出其他文字|不要输出其他文字|没有其他文字|不要有任何其他文字"
    r"|不要附带任何说明|不要有任何说明|别的都不要写"
    r"|不要输出任何解释、推理过程或其他文字|不要输出任何解释、推理或其他文字"
    r"|no\s+other\s+(?:text|words|characters|content)"
    r"|nothing\s+else"
    r"|without\s+(?:any\s+)?(?:extra|additional)\s+text",
    re.IGNORECASE,
)
_LINE_COUNT = re.compile(
    r"exactly\s+(?P<en>\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:non-?\s?empty\s+)?lines?"
    r"|(?:恰好|正好|刚好|只输出|仅输出|限)\s*(?P<cn>[一二三四五六七八九十\d]+)\s*行",
    re.IGNORECASE,
)
_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_EN_DIGITS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_NUMERAL_ONLY = re.compile(r"[0-9]+")


class OutputConstraint(NamedTuple):
    kind: str
    detail: str

    def violation(self, text: str) -> Optional[str]:
        """Return a human-readable violation, or None when text complies."""
        body = (text or "").strip()
        if self.kind == "numeral_only":
            if _NUMERAL_ONLY.fullmatch(body):
                return None
            return f"题面要求只输出阿拉伯数字，实际输出 {len(body)} 字且含非数字内容"
        if self.kind == "line_count":
            expected = int(self.detail)
            actual = len([line for line in body.splitlines() if line.strip()])
            if actual == expected:
                return None
            return f"题面要求恰好 {expected} 行非空输出，实际 {actual} 行"
        return None


class ConstraintDetection(NamedTuple):
    status: str
    constraints: List[OutputConstraint]
    numeral_signal: bool
    no_extra_signal: bool


def asks_for_restatement(prompt: str) -> bool:
    """True only when the prompt explicitly asks for the question to be echoed."""
    lowered = (prompt or "").lower()
    return any(marker in lowered for marker in _RESTATEMENT_MARKERS)


def detect_output_constraints(prompt: str) -> List[OutputConstraint]:
    """Extract the output rules the prompt states in mechanically checkable form."""
    return detect_output_constraint_state(prompt).constraints


def detect_output_constraint_state(prompt: str) -> ConstraintDetection:
    """Return both detected rules and a deterministic partial-match signal.

    The partial state is intentionally conservative: it never invents a length
    or semantic rule, but it prevents a prompt that looks like a strict output
    contract from silently taking the generic review-format path.
    """
    text = prompt or ""
    numeral_signal = bool(_NUMERAL_ASK.search(text))
    no_extra_signal = bool(_NO_EXTRA_TEXT.search(text))
    found: List[OutputConstraint] = []
    if numeral_signal and no_extra_signal:
        found.append(OutputConstraint("numeral_only", "[0-9]+"))
    match = _LINE_COUNT.search(text)
    if match:
        raw = (match.group("en") or match.group("cn") or "").strip().lower()
        count = _EN_DIGITS.get(raw) or _CN_DIGITS.get(raw)
        if count is None and raw.isdigit():
            count = int(raw)
        if count:
            found.append(OutputConstraint("line_count", str(count)))
    if found:
        status = "constraints_detected"
    elif numeral_signal or no_extra_signal:
        status = "unresolved_partial"
    else:
        status = "no_constraint"
    return ConstraintDetection(status, found, numeral_signal, no_extra_signal)


def constraint_violations(text: str, constraints: List[OutputConstraint]) -> List[str]:
    return [reason for reason in (item.violation(text) for item in constraints) if reason]
