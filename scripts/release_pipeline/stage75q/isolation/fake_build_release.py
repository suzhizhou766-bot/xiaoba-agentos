"""Controllable fake build_release CLI for leaf verification (not FakeRunner orchestration)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--agentos-root", type=Path)
    p.add_argument("--interface", type=Path)
    p.add_argument("--stable-scripts", type=Path)
    p.add_argument("--second-brain", type=Path)
    p.add_argument("--published-agentos-root", type=Path)
    p.add_argument("--target", type=Path, required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--verified-date", required=True)
    p.add_argument("--test-count", type=int, required=True)
    p.add_argument("--test-result", default="passed")
    p.add_argument("--acceptance-complete", action="store_true")
    p.add_argument("--acceptance-database", type=Path)
    p.add_argument("--acceptance-task-id")
    p.add_argument("--previous-release", type=Path)
    p.add_argument("--fail", action="store_true")
    p.add_argument("--omit-release", action="store_true")
    p.add_argument("--omit-files", action="store_true")
    p.add_argument("--tier", default="regular")
    ns = p.parse_args(argv)
    # Echo argv proof for leaf tests
    print(json.dumps({"fake_build_release": True, "argv": sys.argv[1:]}, ensure_ascii=False))
    if ns.fail:
        print("FAKE_BUILD_NONZERO", flush=True)
        return 7
    target = Path(ns.target)
    target.mkdir(parents=True, exist_ok=True)
    if not ns.omit_release:
        release = {
            "status": "verified_immutable_release",
            "version": ns.version,
            "release_tier": {"tier": ns.tier, "calculated_tier": ns.tier},
            "acceptance": {
                "end_to_end_evidence": {
                    "conclusions": {"ok": True},
                    "pool_nodes": {"ok": True},
                    "execution": {"ok": True},
                }
            },
        }
        (target / "RELEASE.json").write_text(json.dumps(release, ensure_ascii=False, indent=2) + "\n")
    if not ns.omit_files:
        (target / "FILES.sha256").write_text("deadbeef  fake\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
