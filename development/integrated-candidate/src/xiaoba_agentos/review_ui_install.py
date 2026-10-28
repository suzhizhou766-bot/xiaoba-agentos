"""Install the source-bound portal adapter, with exact preimages and no backups.

This is a separate user-terminal action after the native release switch. The
four external files are not part of the AgentOS runtime source mirror.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile

ASSETS = Path(__file__).with_name("review_ui_assets")
TARGETS = {
    "workflow_query.py": ("project", "integrations/review-center/workflow_query.py"),
    "review_action.py": ("project", "integrations/review-center/review_action.py"),
    "server.py": ("project", "integrations/review-center/server.py"),
    "xiaoba-monitor.html": ("assistant", "工具/小八监看/xiaoba-monitor.html"),
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def current(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("UI target must be a regular file: " + str(path))
    return path.read_bytes() if path.exists() else None


def prepare(project, assistant, assets=ASSETS):
    manifest = json.loads((assets / "manifest.json").read_text())
    entries = manifest["files"]
    if manifest.get("version") != 1 or len(entries) != 4 or {e["asset"] for e in entries} != set(TARGETS):
        raise ValueError("invalid UI asset manifest")
    roots = {"project": Path(project).resolve(), "assistant": Path(assistant).resolve()}
    plan = []
    for entry in entries:
        if (entry["base"], entry["relative"]) != TARGETS[entry["asset"]]:
            raise ValueError("UI manifest contains an unsupported target")
        root = roots[entry["base"]]
        target = root / entry["relative"]
        for parent in [target] + list(target.parents):
            if parent == root:
                break
            if parent.is_symlink():
                raise ValueError("symlink in UI target path")
        new = (assets / entry["asset"]).read_bytes()
        if sha(new) != entry["after_sha256"]:
            raise ValueError("UI asset hash mismatch")
        old = current(target)
        old_sha = sha(old) if old is not None else None
        if old_sha not in (entry["before_sha256"], entry["after_sha256"]):
            raise ValueError("UI target changed since preparation; rebase required: " + str(target))
        if not target.parent.is_dir():
            raise ValueError("UI target parent is missing")
        plan.append({"path": target, "old": old, "new": new,
                     "changed": old_sha != entry["after_sha256"],
                     "mode": target.stat().st_mode & 0o777 if old is not None else 0o600})
    return plan


def atomic(path, data, mode):
    fd, name = tempfile.mkstemp(prefix=".review-ui-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run(config, *, apply=False, assets=ASSETS):
    project = config.project_dir
    assistant = Path(config.memory["obsidian_vault"]).resolve().parent
    plan = prepare(project, assistant, assets)
    if apply:
        # Old bytes live only in memory. Nothing is copied to a backup path.
        with (config.runtime_dir / "review-ui-install.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            plan = prepare(project, assistant, assets)
            changed = []
            try:
                for entry in plan:
                    if not entry["changed"]:
                        continue
                    if current(entry["path"]) != entry["old"]:
                        raise ValueError("UI target changed during installation")
                    changed.append(entry)
                    atomic(entry["path"], entry["new"], entry["mode"])
                if any(current(e["path"]) != e["new"] for e in plan):
                    raise RuntimeError("UI installation readback failed")
            except Exception as exc:
                errors = []
                for entry in reversed(changed):
                    if current(entry["path"]) == entry["old"]:
                        continue
                    if current(entry["path"]) != entry["new"]:
                        errors.append(str(entry["path"]))
                        continue
                    if entry["old"] is None:
                        entry["path"].unlink()
                    else:
                        atomic(entry["path"], entry["old"], entry["mode"])
                if errors:
                    raise RuntimeError("UI installation interrupted; concurrent changes retained: " + ", ".join(errors)) from exc
                raise
    return {"ok": True, "applied": apply,
            "up_to_date": all(not e["changed"] for e in plan) or apply,
            "restart_monitor_required": apply and any(e["changed"] for e in plan),
            "files": [{"path": str(e["path"]), "changed": e["changed"], "after_sha256": sha(e["new"])} for e in plan]}
