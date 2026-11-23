"""stage75q CLI — real --run entry after --allow-real + shared gates; --plan is memory-only zero-write."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from bind import PIN, Bind, load_bind, write_bind
from context_adapter import (
    import_release_context,
    load_live_context,
    load_live_context_plan,
    load_synthetic_context,
)
from orchestrator import HELPER_STEPS, Orchestrator, plan_only

STAGE = Path(__file__).resolve().parent


def _emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _hash_tree(paths: list[Path]) -> dict[str, str]:
    out = {}
    for p in paths:
        p = Path(p)
        if p.is_file() and not p.is_symlink():
            out[str(p.resolve())] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def cmd_help() -> None:
    text = """stage75q — 真实构包与复制演练工具（75Q）

正常入口（统一解释器 + -B）:
  {pin} -B {cli} --help
  {pin} -B {cli} --plan [--bind-json F]
  {pin} -B {cli} --check [--bind-json F]
  {pin} -B {cli} --isolate-verify --evidence-dir D

正式阶段入口（需 --allow-real；本卡禁止 Agent 对生产执行，但工具不得永久禁用）:
  {pin} -B {cli} --run --allow-real

共享接口: work/v1615-r1/driver75p/release_context.py
写范围: work/v1615-r1/stage75q + reports/v1615-r1/stage75q
""".format(pin=PIN, cli=STAGE / "cli.py")
    print(text)


def cmd_plan(args) -> int:
    """Memory bind only. Zero writes / zero markers / no manifest generation."""
    watched: list[Path] = []
    if args.bind_json:
        watched.append(Path(args.bind_json))
    watched.append(STAGE / ".plan-bind.json")
    before = _hash_tree([p for p in watched if p.exists()])
    before_listing = sorted(p.name for p in STAGE.iterdir()) if STAGE.is_dir() else []

    if args.bind_json:
        data = load_synthetic_context(Path(args.bind_json))  # read only
        bind = Bind(data)
        report = plan_only(bind)
        report["ready"] = True
        report["source"] = "bind-json-memory"
    else:
        payload, meta = load_live_context_plan()
        report = dict(meta)
        if payload is not None:
            bind = Bind(payload)
            detail = plan_only(bind)
            report.update({k: detail[k] for k in detail if k not in ("ok",)})
            report["expected_helper_argv"] = [
                [str(PIN), "-B", str(Path(bind.W if where == "helpers" else bind.stage_dir) / name)]
                for name, _out, where in HELPER_STEPS
            ]
            report["helpers_dir"] = str(bind.W)
            report["stage_dir"] = str(bind.stage_dir)
            report["attempt"] = bind.attempt
        else:
            report["expected_helper_argv"] = [
                [str(PIN), "-B", f"<record.step4_helpers_dir>/{'helpers/' if w == 'helpers' else ''}{n}"]
                for n, _o, w in HELPER_STEPS
            ]

    after = _hash_tree([p for p in watched if p.exists()])
    after_listing = sorted(p.name for p in STAGE.iterdir()) if STAGE.is_dir() else []
    report["writes"] = 0
    report["read_only"] = True
    report["filesystem_hash_unchanged"] = before == after
    report["stage_listing_unchanged"] = before_listing == after_listing
    if before != after or before_listing != after_listing:
        report["ok"] = False
        report["error"] = "STOP: --plan mutated filesystem"
        _emit(report)
        return 1
    _emit(report)
    return 0 if report.get("ok") is not False else 1


def cmd_check(args) -> int:
    report = {
        "ok": True,
        "interpreter": str(Path(sys.executable).resolve()) == str(PIN.resolve()),
        "dont_write_bytecode": bool(sys.dont_write_bytecode),
        "optimize": sys.flags.optimize,
        "shared_release_context": False,
        "notes": [],
    }
    if sys.flags.optimize != 0:
        report["ok"] = False
        report["notes"].append("optimize mode refused")
    if Path(sys.executable).resolve() != PIN.resolve():
        report["ok"] = False
        report["notes"].append("wrong interpreter")
    try:
        import_release_context()
        report["shared_release_context"] = True
    except RuntimeError as exc:
        report["shared_release_context"] = False
        report["notes"].append(str(exc))
    if args.bind_json:
        bind = Bind(load_synthetic_context(Path(args.bind_json)))
        report["bind_attempt"] = bind.attempt
        report["same_attempt_paths"] = {
            "acceptance": str(bind.A),
            "reports": str(bind.O),
            "package": str(bind.PKG),
            "helpers": str(bind.W),
            "stage_dir": str(bind.stage_dir),
        }
    _emit(report)
    return 0 if report["ok"] else 1


def cmd_isolate(args) -> int:
    from isolation.verify_r41 import main as isolate_main

    return isolate_main(args.evidence_dir)


def _is_isolation_runner(runner: Callable | None) -> bool:
    """Only an explicitly injected synthetic runner may use isolation fixture skips."""
    import subprocess as _sp

    if runner is None:
        return False
    if runner is _sp.run:
        return False
    return True


def run_stage(
    *,
    allow_real: bool,
    runner: Callable | None = None,
    bind_payload: dict[str, Any] | None = None,
    skip_live: bool = False,
    bind_write_dir: Path | None = None,
) -> dict[str, Any]:
    """Shared entry: baseline + prebuild gates BEFORE any bind/marker write; then Orchestrator."""
    if not allow_real:
        raise SystemExit("STOP: real stage requires --allow-real after manager acceptance")
    from gates import run_prebuild_gates

    baseline_evidence: dict[str, Any] | None = None
    ctx_live = None
    rc = None
    isolation_runner = _is_isolation_runner(runner)

    if bind_payload is not None:
        bind = Bind(bind_payload)
        payload = bind_payload
        if payload.get("isolation_skip_baseline_evidence") is True:
            if not isolation_runner:
                raise SystemExit(
                    "STOP: isolation_skip_baseline_evidence refused for real runner; "
                    "baseline+freeze required before bind write"
                )
            baseline_evidence = {
                "ok": True,
                "skipped": True,
                "isolation_only": True,
                "note": "marked isolation_skip_baseline_evidence with injected runner; not a production gate pass",
            }
        elif payload.get("baseline_ctx") is not None:
            # Real runner always require_freeze; isolation may mark False only with injected runner.
            require_freeze = True
            if isolation_runner and payload.get("baseline_require_freeze") is False:
                require_freeze = False
            elif payload.get("baseline_require_freeze") is False and not isolation_runner:
                raise SystemExit(
                    "STOP: baseline_require_freeze=False refused for real runner"
                )
            rc = import_release_context()
            baseline_evidence = rc.validate_baseline_evidence(
                payload["baseline_ctx"],
                require_freeze=require_freeze,
            )
        else:
            raise SystemExit(
                "STOP: validate_baseline_evidence required before bind write; "
                "provide baseline_ctx or (isolation injected runner + isolation_skip_baseline_evidence)"
            )
    else:
        rc, ctx_live, payload = load_live_context("step4")
        bind = Bind(payload)
        # Live --run: always full baseline + freeze; never honor isolation skip fields.
        if payload.get("isolation_skip_baseline_evidence") is True:
            raise SystemExit("STOP: isolation_skip_baseline_evidence refused on live context")
        baseline_evidence = rc.validate_baseline_evidence(ctx_live, require_freeze=True)

    # Known failures (votes/chain/manifests) must refuse before writing bind or markers.
    run_prebuild_gates(bind, skip_live=skip_live)

    if bind_write_dir is not None:
        bind_path = Path(bind_write_dir) / "STAGE75Q-BIND.json"
        write_bind(bind_path, payload)
        os.environ["STAGE75Q_BIND_JSON"] = str(bind_path)
        bind = load_bind()
    elif bind_payload is None:
        bind_path = Path(payload["reports_step4_dir"]) / "STAGE75Q-BIND.json"
        write_bind(bind_path, payload)
        os.environ["STAGE75Q_BIND_JSON"] = str(bind_path)
        bind = load_bind()
    else:
        # In-memory isolation without bind_write_dir: set a temp env only if already present
        if not os.environ.get("STAGE75Q_BIND_JSON"):
            # Orchestrator helpers need env; write only under bind_write_dir when provided
            pass

    orch = Orchestrator(bind, runner=runner, skip_live=skip_live, allow_real=True)
    result = orch.run()
    result["baseline_evidence"] = baseline_evidence
    return result


def cmd_run(args) -> int:
    """Real executable entry: gates + Orchestrator.run. This card forbids Agent production use,
    but the tool itself must not permanently SystemExit after --allow-real.
    """
    if not args.allow_real:
        raise SystemExit("STOP: real stage requires --allow-real after manager acceptance")
    # Optional isolation injection: STAGE75Q_FAKE_RUNNER=1 uses no production subprocess
    # (only for fixtures that set bind via STAGE75Q_BIND_JSON already). Normal lead runs omit this.
    runner = None
    if os.environ.get("STAGE75Q_FAKE_RUNNER") == "1":
        raise SystemExit(
            "STOP: STAGE75Q_FAKE_RUNNER is isolation-only; use --isolate-verify harness, not --run"
        )
    result = run_stage(allow_real=True, runner=runner, skip_live=False)
    _emit({"ok": result.get("ok"), "step4_passed": result.get("step4_passed"), "step5_passed": result.get("step5_passed")})
    return 0 if result.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stage75q.cli", add_help=False)
    p.add_argument("--help", "-h", action="store_true")
    p.add_argument("--plan", action="store_true")
    p.add_argument("--check", action="store_true")
    p.add_argument("--isolate-verify", action="store_true")
    p.add_argument("--run", action="store_true")
    p.add_argument("--allow-real", action="store_true")
    p.add_argument("--bind-json", type=Path)
    p.add_argument("--evidence-dir", type=Path)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.help or not any((args.plan, args.check, args.isolate_verify, args.run)):
        cmd_help()
        return 0
    if args.plan:
        return cmd_plan(args)
    if args.check:
        return cmd_check(args)
    if args.isolate_verify:
        if not args.evidence_dir:
            raise SystemExit("--isolate-verify requires --evidence-dir")
        return cmd_isolate(args)
    if args.run:
        return cmd_run(args)
    cmd_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
