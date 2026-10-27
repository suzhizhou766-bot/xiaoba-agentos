#!/usr/bin/env python3
"""Validate the agent-neutral work-system registry and its file contracts."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

ROOT = Path(os.environ.get("XIAOBA_ASSISTANT_ROOT", Path.home() / "Documents/私人助理")).resolve()
SYSTEM = ROOT / "系统/小八工作体系"
REGISTRY = SYSTEM / "registry.json"


def resolve_ref(value: str) -> Path:
    value = value.replace("${ROOT}", str(ROOT)).replace("${KNOWLP_ROOT}", os.environ.get("KNOWLP_ROOT", str(Path.home() / "Documents/Codex/KnowLP")))
    value = value.replace("${HOME}", str(Path.home()))
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def validate() -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    if not REGISTRY.exists():
        return {"schema_version": 1, "status": "FAIL", "errors": [f"missing registry: {REGISTRY}"], "warnings": []}
    try:
        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"schema_version": 1, "status": "FAIL", "errors": [f"invalid registry: {exc}"], "warnings": []}
    if registry.get("schema_version", 0) < 2:
        warnings.append("registry schema is older than 2")
    modules = registry.get("modules", [])
    if not isinstance(modules, list) or not modules:
        errors.append("registry.modules must be a non-empty array")
        return {"schema_version": 1, "status": "FAIL", "errors": errors, "warnings": warnings}
    seen: set[str] = set()
    manifests = []
    for entry in modules:
        module_id = entry.get("id") if isinstance(entry, dict) else None
        ref = entry.get("manifest") if isinstance(entry, dict) else None
        if not module_id or module_id in seen:
            errors.append(f"duplicate or missing module id: {module_id!r}")
            continue
        seen.add(module_id)
        if not ref:
            errors.append(f"{module_id}: missing manifest reference")
            continue
        path = resolve_ref(ref)
        if not path.is_file():
            errors.append(f"{module_id}: manifest missing: {path}")
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{module_id}: invalid manifest: {exc}")
            continue
        if manifest.get("id") != module_id:
            errors.append(f"{module_id}: manifest id mismatch")
        for key in ("implementation_status", "implementation_paths", "data_contract_version", "capabilities", "permissions", "health_checks"):
            if key not in manifest:
                errors.append(f"{module_id}: missing manifest field {key}")
        status = manifest.get("implementation_status", "unknown")
        if status not in {"implemented", "partial", "planned", "retired"}:
            errors.append(f"{module_id}: invalid implementation_status {status!r}")
        for raw in manifest.get("implementation_paths", []):
            path_value = resolve_ref(raw)
            if not path_value.exists():
                if manifest.get("implementation_status") == "partial" and module_id == "index" and "KNOWLP_ROOT" in raw:
                    warnings.append(f"{module_id}: external implementation unavailable: {path_value}")
                else:
                    errors.append(f"{module_id}: implementation missing: {path_value}")
        for raw in manifest.get("required_paths", []):
            path_value = resolve_ref(raw)
            if not path_value.exists():
                warnings.append(f"{module_id}: required path missing: {path_value}")
        for raw in manifest.get("external_runtime_paths", []):
            path_value = resolve_ref(raw)
            if not path_value.exists():
                warnings.append(f"{module_id}: external runtime unavailable: {path_value}")
        manifests.append({"id": module_id, "status": status, "manifest": str(path)})
    return {"schema_version": 1, "status": "FAIL" if errors else "WARN" if warnings else "PASS", "registry": str(REGISTRY), "modules": manifests, "errors": errors, "warnings": warnings}


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate 小八工作体系 module contracts")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    report = validate()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"module contract: {report['status']}")
        for item in report["errors"]: print(f"FAIL: {item}")
        for item in report["warnings"]: print(f"WARN: {item}")
    return 1 if report["status"] == "FAIL" else 0

if __name__ == "__main__": raise SystemExit(main())
