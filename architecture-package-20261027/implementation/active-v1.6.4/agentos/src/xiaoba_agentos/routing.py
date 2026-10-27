from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from .capabilities import CapabilityRegistry, capable_agents, web_capable_agents


DIRECT = "direct"
AGENTOS_MULTI = "agentos_multi"
REVIEW = "review"
ASK_USER = "ask_user"
ROUTES = {DIRECT, AGENTOS_MULTI, REVIEW, ASK_USER}


def adjudicated_label(agent_count: int) -> str:
    count = {2: "双", 3: "三"}.get(agent_count, "多")
    return f"本回答：小八 AgentOS {count} Agent 裁判"


@dataclass(frozen=True)
class RouteDecision:
    route: str
    reason_code: str
    confidence: float
    answer_provenance: str

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


_REVIEW_PATTERNS = (
    "下班了小八助理",
    "下班",
    "最终裁决",
    "最终审核",
    "交付审核",
    "交付裁决",
)

_EXPLICIT_MULTI_PATTERNS = (
    "走小八",
    "使用小八agentos",
    "调用小八agentos",
    "双agent",
    "三agent",
    "三方审查",
    "三个agent",
    "多agent",
    "两个agent",
    "后台的2个agent",
    "经过裁判",
)

_ACTION_PATTERNS = (
    "修改",
    "修复",
    "实现",
    "升级",
    "创建",
    "删除",
    "发布",
    "部署",
    "安装",
    "运行",
    "执行",
    "写入",
    "重构",
    "迁移",
    "生成文件",
    "提交代码",
    "改成",
    "换成",
    "购买",
    "买下",
    "下单",
    "付款",
    "检索全网",
    "深度研究",
)

_ENGLISH_ACTION_PATTERNS = (
    r"\bedit\b",
    r"\bmodify\b",
    r"\boverwrite\b",
    r"\brefactor\b",
    r"\bpurchase\b",
    r"\bbuy\b",
    r"\bplace\s+an\s+order\b",
)

_MULTISTEP_PATTERNS = (
    "第一步",
    "第二步",
    "分别",
    "全面",
    "详细分析",
    "对比",
    "验证并",
    "然后",
    "最后",
)

_SOCIAL_PATTERNS = (
    "你好",
    "早上好",
    "晚上好",
    "谢谢",
    "非常好",
    "辛苦了",
    "继续",
)

_AMBIGUOUS_PATTERNS = (
    "这个怎么办",
    "那个呢",
    "按之前的",
    "照旧",
    "处理一下",
)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def _has_action_language(text: str, compact: str) -> bool:
    return any(pattern in compact for pattern in _ACTION_PATTERNS) or any(
        re.search(pattern, text, re.IGNORECASE) for pattern in _ENGLISH_ACTION_PATTERNS
    )


def decide_route(
    prompt: str,
    *,
    active_task_id: Optional[str] = None,
    relation: str = "auto",
    risk: str = "auto",
    requires_action: bool = False,
    memory_worthy: bool = False,
    explicit_agentos: bool = False,
    force_route: Optional[str] = None,
) -> RouteDecision:
    """Apply the auditable intake policy without calling a model.

    The harness may provide observable conversation signals (relation, risk and
    action requirements), but the final route is selected here. This keeps the
    routing rule shared across Codex, Claude and other compatible harnesses.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if relation not in {"auto", "related", "unrelated"}:
        raise ValueError("relation must be auto, related or unrelated")
    if risk not in {"auto", "low", "high"}:
        raise ValueError("risk must be auto, low or high")
    compact = _compact(prompt)
    if force_route is not None:
        if force_route not in ROUTES:
            raise ValueError("invalid forced route")
        if force_route == DIRECT:
            raise ValueError("direct route cannot be forced")
        provenance = {
            DIRECT: "session_model_direct",
            AGENTOS_MULTI: "agentos_multi_adjudicated",
            REVIEW: "agentos_review",
            ASK_USER: "agentos_clarification",
        }[force_route]
        return RouteDecision(force_route, "user_or_harness_override", 1.0, provenance)

    if explicit_agentos or any(pattern in compact for pattern in _EXPLICIT_MULTI_PATTERNS):
        return RouteDecision(AGENTOS_MULTI, "explicit_multi_agent_request", 1.0, "agentos_multi_adjudicated")
    if any(pattern in compact for pattern in _REVIEW_PATTERNS):
        return RouteDecision(REVIEW, "review_or_shutdown_boundary", 1.0, "agentos_review")
    if risk == "high":
        return RouteDecision(AGENTOS_MULTI, "high_impact_request", 0.98, "agentos_multi_adjudicated")
    if requires_action:
        return RouteDecision(AGENTOS_MULTI, "workspace_or_external_action_required", 0.97, "agentos_multi_adjudicated")
    if memory_worthy:
        return RouteDecision(AGENTOS_MULTI, "candidate_for_cross_task_reuse", 0.96, "agentos_multi_adjudicated")
    if _has_action_language(prompt, compact):
        return RouteDecision(AGENTOS_MULTI, "action_or_mutation_language", 0.93, "agentos_multi_adjudicated")
    if relation == "unrelated":
        return RouteDecision(DIRECT, "unrelated_conversation_aside", 0.98, "session_model_direct")
    if len(compact) >= 280 or sum(pattern in compact for pattern in _MULTISTEP_PATTERNS) >= 2:
        return RouteDecision(AGENTOS_MULTI, "complex_or_multistep_request", 0.90, "agentos_multi_adjudicated")
    if active_task_id and any(pattern in compact for pattern in _AMBIGUOUS_PATTERNS):
        return RouteDecision(ASK_USER, "ambiguous_reference_to_active_task", 0.86, "agentos_clarification")
    if any(compact == pattern or compact.startswith(pattern) for pattern in _SOCIAL_PATTERNS):
        return RouteDecision(DIRECT, "social_or_conversation_control", 0.99, "session_model_direct")
    if relation == "related" and active_task_id and len(compact) >= 120:
        return RouteDecision(AGENTOS_MULTI, "substantive_active_task_followup", 0.84, "agentos_multi_adjudicated")
    return RouteDecision(DIRECT, "low_impact_explanation_or_question", 0.80, "session_model_direct")

BLOCKED = "blocked"


def capability_gate_result(
    *,
    needs_web: bool,
    required_tokens=(),
    agents=(),
    registry=None,
) -> Dict[str, Any]:
    """Gate agent selection on declared capabilities.

    Fails closed: an agent whose capability is "unknown" is never treated as
    true. A needs_web task with no native_web_search-capable agent, or a task
    requiring a capability no enabled agent has, returns route "blocked"
    immediately instead of waiting for an upstream HTTP 400.
    """
    if registry is None:
        registry = CapabilityRegistry()
    pool = list(agents)
    if needs_web:
        pool, reason = web_capable_agents(pool, registry, needs_web=True)
        if not pool:
            return {
                "route": BLOCKED,
                "reason_code": "insufficient_capability",
                "confidence": 1.0,
                "capability": "native_web_search",
                "reason": reason or "no web-capable agent",
            }
    if required_tokens:
        pool, missing = capable_agents(pool, registry, required_tokens)
        if not pool:
            lacked = sorted({tok for _, toks in missing for tok in toks})
            return {
                "route": BLOCKED,
                "reason_code": "insufficient_capability",
                "confidence": 1.0,
                "capability": lacked[0] if lacked else "unknown",
                "reason": "missing capability: " + ", ".join(lacked),
            }
    return {
        "route": AGENTOS_MULTI,
        "reason_code": "capability_satisfied",
        "confidence": 1.0,
        "agents": [getattr(a, "name", str(a)) for a in pool],
    }
