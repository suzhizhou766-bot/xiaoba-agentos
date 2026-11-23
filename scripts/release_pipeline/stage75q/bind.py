"""Path bind for stage75q helpers. Consumes STAGE75Q_BIND_JSON or in-memory dict; no v14 hardcodes."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

PIN = Path(
    "/Users/suzhizhou/Documents/私人助理/配置/python/"
    "cpython-3.14.7-macos-aarch64-none/bin/python3.14"
)
LIVE_DEFAULT = Path("/Users/suzhizhou/Documents/小八AgentOS")


def _load() -> dict:
    raw = os.environ.get("STAGE75Q_BIND_JSON")
    if not raw:
        raise RuntimeError("STOP: STAGE75Q_BIND_JSON missing; helpers refuse unbound run")
    path = Path(raw)
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("STOP: bind json missing or symlink: " + str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    return data


def sha(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Bind:
    def __init__(self, data: dict | None = None):
        self.data = data if data is not None else _load()
        for key in (
            "root",
            "work",
            "reports_step4_dir",
            "acceptance_step4_dir",
            "releases_package_dir",
            "helpers_dir",
            "stage_dir",
            "source",
            "candidate",
            "version",
            "round",
            "attempt",
            "live",
            "previous_package",
            "generated_brain",
        ):
            if key not in self.data:
                raise RuntimeError("STOP: bind missing key: " + key)
        self.root = Path(self.data["root"]).resolve()
        self.work = Path(self.data["work"]).resolve()
        self.O = Path(self.data["reports_step4_dir"]).resolve()
        self.A = Path(self.data["acceptance_step4_dir"]).resolve()
        self.PKG = Path(self.data["releases_package_dir"]).resolve()
        # helpers_dir = record.step4_helpers_dir / "helpers"
        self.W = Path(self.data["helpers_dir"]).resolve()
        # stage_dir = record.step4_helpers_dir (case13/14/build_verified live here)
        self.stage_dir = Path(self.data["stage_dir"]).resolve()
        self.source = Path(self.data["source"]).resolve()
        self.C = Path(self.data["candidate"]).resolve()
        self.version = str(self.data["version"])
        self.round = str(self.data["round"])
        self.attempt = int(self.data["attempt"])
        self.live = Path(self.data.get("live") or LIVE_DEFAULT).resolve()
        self.previous_package = Path(self.data["previous_package"]).resolve()
        self.generated_brain = Path(self.data["generated_brain"]).resolve()
        self.acceptance_root = Path(
            self.data.get("acceptance_root") or (self.root / f"acceptance-{self.round}")
        ).resolve()
        self.review_report = Path(self.data["review_report"]).resolve() if self.data.get("review_report") else None
        self.integrity_report = (
            Path(self.data["integrity_report"]).resolve() if self.data.get("integrity_report") else None
        )
        self.previous_resource_vendor_cli = (
            Path(self.data["previous_resource_vendor_cli"]).resolve()
            if self.data.get("previous_resource_vendor_cli")
            else None
        )
        self.previous_resource_vendor_cli_sha256 = self.data.get("previous_resource_vendor_cli_sha256")
        self.online_manifest_sha256 = self.data.get("online_manifest_sha256")
        self.preparation_path = (
            Path(self.data["preparation_path"]).resolve()
            if self.data.get("preparation_path")
            else (self.work / "PREPARATION.json")
        )
        self.tool_manifests = [Path(p) for p in self.data.get("tool_manifests") or []]
        self.migration_checks = Path(self.data["migration_checks"]).resolve() if self.data.get("migration_checks") else None
        self.migration_checks_sha256 = self.data.get("migration_checks_sha256")
        self.scene_version_dir = f".runtime/releases/{self.version}"

    def require_ready(self) -> None:
        ready = self.work / "BASE_READY.json"
        if not ready.is_file():
            raise RuntimeError("STOP: BASE_READY.json missing; no build/rehearsal")
        if Path(sys.executable).resolve() != PIN.resolve():
            raise RuntimeError("STOP: must run under unified 3.14.7 interpreter")
        if sys.flags.optimize != 0:
            raise RuntimeError("STOP: optimize mode (-O/-OO) refused before any write")


def load_bind(data: dict | None = None) -> Bind:
    return Bind(data)


def clean_env(base: dict | None = None) -> dict:
    env = dict(base or os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONOPTIMIZE", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def write_bind(path: Path, payload: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return path
