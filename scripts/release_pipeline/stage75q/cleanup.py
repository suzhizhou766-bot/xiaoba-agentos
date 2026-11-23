"""Cleanup only this attempt's copy-scene services; never touch production."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from bind import Bind


def cleanup_scene(bind: Bind) -> dict[str, Any]:
    scene_path = bind.O / "SCENE.json"
    if not scene_path.exists():
        return {"ok": True, "service_created": False}
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    root = Path(scene["root"]).resolve()
    label = scene["label"]
    rehearsal_root = (bind.root / "rehearsal").resolve()
    if not root.is_relative_to(rehearsal_root):
        raise RuntimeError("STOP: scene root escapes rehearsal/")
    if not label.startswith("com.xiaoba.agentos.rehearsal.") or label == "com.xiaoba.agentos":
        raise RuntimeError("STOP: refusing non-rehearsal label: " + label)

    state = json.loads((bind.A / "STATE.json").read_text(encoding="utf-8"))
    package = Path(state["verified_package"])
    switch = package / "agentos/scripts/switch_release.py"
    spec = importlib.util.spec_from_file_location("stage75q_cleanup_switch", switch)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    layout = mod.Layout(
        Path(scene["runtime"]),
        Path(scene["config"]),
        package,
        Path(scene["vault"]),
        Path(scene["graph"]),
        tuple(map(Path, scene["entries"])),
        Path(scene["plist"]),
        label,
    )
    system = mod.System()
    if system.registered(layout):
        pid = json.loads(layout.worker.read_text()).get("pid", 0) if layout.worker.exists() else 0
        system.stop(layout, int(pid))
    absent = not system.registered(layout)
    pids = system.worker_pids(layout)
    processes = subprocess.check_output(["/bin/ps", "-axo", "pid=,command="], text=True)
    remaining = [
        line
        for line in processes.splitlines()
        if str(root) in line and int(line.strip().split()[0]) != os.getpid()
    ]
    listing = subprocess.run(["/bin/launchctl", "list"], capture_output=True, text=True)
    labels = [
        line.split()[-1]
        for line in listing.stdout.splitlines()
        if "com.xiaoba.agentos.rehearsal." in line
    ]
    return {
        "ok": absent and not pids and not remaining,
        "label": label,
        "service_absent": absent,
        "worker_pids": pids,
        "scene_processes": remaining,
        "all_rehearsal_labels_remaining": labels,
        "production_untouched": True,
    }
