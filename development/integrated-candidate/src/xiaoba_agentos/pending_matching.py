"""Bounded, explainable lexical recall. No model calls, writes or adoption."""
import re
import unicodedata

LIMIT = 5
_NOISE = {"方法", "经验", "任务", "进行", "处理", "问题", "用户", "可以", "使用", "一个", "需要", "怎么", "如何", "完成", "流程", "检查", "请帮", "帮我", "现在"}
_ALIASES = {"颜色调整": "调色", "色彩调整": "调色", "抠像": "抠图", "移除背景": "抠图", "字幕条": "字幕", "命令行": "cli", "工作目录": "工作区"}


def normalize(text):
    value = unicodedata.normalize("NFKC", str(text or "")).casefold()[:8000]
    for source, target in _ALIASES.items():
        value = value.replace(source, target)
    return value


def terms(text):
    value = normalize(text)
    result = set(re.findall(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", value))
    for run in re.findall(r"[\u3400-\u9fff]+", value):
        result.update(run[i:i + 2] for i in range(len(run) - 1))
    return {item for item in result if item not in _NOISE and len(item) >= 2}


def explain(problem_key, query, title, policy):
    exact = bool(problem_key and problem_key.strip() == policy.get("problem_key"))
    metadata = " ".join(str(policy.get(k) or "") for k in ("task_type", "applicability_conditions", "environment"))
    source = normalize(title + " " + metadata)
    needle = normalize(query).strip()
    matched = sorted(terms(needle) & terms(source))
    substring = bool(needle and len(needle) >= 2 and needle in source)
    strong = any(re.search(r"[a-z0-9]", term) and len(term) >= 3 for term in matched)
    # A single shared Chinese bigram is too broad for full-sentence recall.
    lexical = substring or strong or len(matched) >= 2
    if (problem_key or needle) and not (exact or lexical):
        return None
    return {"match_basis": "problem_key" if exact else "keywords" if lexical else "inventory",
            "matched_keywords": matched, "match_score": 1000 if exact else 100 + len(matched) if substring else len(matched),
            "match_reason": "problem_key精确匹配" if exact else "关键词重合，适用条件仍需核实" if lexical else "待验证库存"}
