from __future__ import annotations

import math
from typing import Any, Dict

from .config import AppConfig
from .store import EventStore


class EvolutionEngine:
    def __init__(self, config: AppConfig, store: EventStore):
        self.config = config
        self.store = store

    def run(self) -> Dict[str, Any]:
        metrics = self.store.metrics()
        minimum = int(self.config.evolution["minimum_samples"])
        usable = [m for m in metrics if m["samples"] >= minimum]
        if not usable:
            return {"decision": "insufficient_data", "required_samples_per_agent": minimum, "metrics": metrics}

        penalty = float(self.config.evolution["latency_penalty"])
        raw_scores: Dict[str, float] = {}
        total_samples = 0
        for item in usable:
            success_rate = item["successes"] / max(1, item["samples"])
            latency_seconds = (item["avg_latency_ms"] or 0) / 1000.0
            score = max(0.05, success_rate - penalty * math.log1p(latency_seconds))
            raw_scores[item["agent_name"]] = score
            total_samples += item["samples"]
        maximum = max(raw_scores.values())
        weights = {name: round(score / maximum, 4) for name, score in raw_scores.items()}
        candidate_score = sum(raw_scores.values()) / len(raw_scores)
        body = {"agent_weights": weights, "reason": "observed success rate adjusted by latency", "metrics": usable}
        version = self.store.add_candidate(body, candidate_score, total_samples)

        active = self.store.active_strategy()
        margin = float(self.config.evolution["promotion_margin"])
        baseline = float(active.get("score") or 0.0)
        promote = candidate_score >= baseline + margin
        reason = f"candidate={candidate_score:.4f}, active={baseline:.4f}, margin={margin:.4f}"
        # Evolution only proposes; activation requires evaluation gates and explicit approval.
        self.store.record_gate(version, "statistical_margin", "passed" if promote else "failed", {"candidate": candidate_score, "active": baseline, "margin": margin})
        return {"decision": "candidate", "version": version, "score": candidate_score, "weights": weights, "reason": reason, "requires": "eval gates plus explicit approve"}

