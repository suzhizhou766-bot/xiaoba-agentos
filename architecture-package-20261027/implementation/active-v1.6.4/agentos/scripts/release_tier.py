#!/usr/bin/env python3
"""用上一版发布包里的分档工具和规则，给候选源码树算发布档位。只读。

档位由已上线的那一版的工具决定，候选改动自己的工具或规则影响不到本版档位。
上一版包里还没有分档工具时返回 full：按现行全套验收，不接受任何参数关闭。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

FULL = "full"
TIERS = ("patch", "regular", "migration")
TOOL = "src/xiaoba_agentos/release_policy.py"
RULES = "config/release_policy.json"


class TierError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def compute_tier(previous_agentos: Path, candidate_agentos: Path) -> dict[str, Any]:
    previous = Path(previous_agentos).resolve()
    candidate = Path(candidate_agentos).resolve()
    baseline_manifest = previous / "SOURCE_MANIFEST.json"
    if not _regular_file(baseline_manifest):
        raise TierError("previous release source manifest is unavailable")
    record: dict[str, Any] = {
        "tier": FULL,
        "baseline_source_manifest_sha256": _sha256(baseline_manifest),
    }
    tool, rules = previous / TOOL, previous / RULES
    if not _regular_file(tool) or not _regular_file(rules):
        record["reasons"] = ["previous release carries no tier tool; full acceptance applies"]
        return record
    spec = importlib.util.spec_from_file_location("_previous_release_policy", tool)
    if spec is None or spec.loader is None:
        raise TierError("previous release tier tool cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    keep = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # the previous release directory is immutable
    try:
        spec.loader.exec_module(module)
        result = module.classify_release(previous, candidate, rules_path=str(rules))
    except Exception as exc:  # the tool is foreign code: any failure refuses the tier
        raise TierError(f"tier tool failed: {exc}") from exc
    finally:
        sys.dont_write_bytecode = keep
    if not isinstance(result, dict) or result.get("ok") is not True:
        problems = result.get("problems") if isinstance(result, dict) else None
        raise TierError(f"tier tool refused the comparison: {json.dumps(problems, ensure_ascii=False)}")
    tier = result.get("effective_tier")
    calculated = result.get("calculated_tier")
    if tier not in TIERS or calculated not in TIERS or TIERS.index(tier) < TIERS.index(calculated):
        raise TierError("tier tool returned an invalid tier")
    changes = result.get("changes") if isinstance(result.get("changes"), list) else []
    record.update(
        tier=tier,
        calculated_tier=calculated,
        tool_sha256=_sha256(tool),
        rules_sha256=_sha256(rules),
        changed_paths={
            name: sorted(str(item.get("path")) for item in changes if isinstance(item, dict) and item.get("tier") == name)
            for name in TIERS
        },
        reasons=[str(item) for item in result.get("reasons") or []][:50],
    )
    return record


def main() -> int:
    if len(sys.argv) != 3:
        print(json.dumps({"ok": False, "error": "usage: release_tier.py PREVIOUS_AGENTOS CANDIDATE_AGENTOS"}))
        return 2
    try:
        record = compute_tier(Path(sys.argv[1]), Path(sys.argv[2]))
    except (TierError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **record}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
