"""Shared release context for 小八 AgentOS v1.6.15 release pipeline (75P).

This module provides common context loading, validation, environment sanitation,
interpreter checking, and tool manifest verification used across 75P, 75Q, and 75R.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

PIN_PATH = Path('/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14')
HEX40_RE = re.compile(r'^[0-9a-f]{40}$')
HEX64_RE = re.compile(r'^[0-9a-f]{64}$')

VALID_STAGES = {
    'export',
    'prepare',
    'freeze',
    'nodes',
    'review',
    'step4',
    'switch',
    'post-switch',
}

# macOS system top-level aliases that cannot be modified by user
MACOS_SYSTEM_SYMLINKS = {Path('/var'), Path('/tmp'), Path('/etc')}


def _check_no_symlinks(path: Path, label: str = 'path') -> None:
    """Strictly verify that path and ALL of its parents are not symlinks.

    Must check before calling resolve(), so symlinks are not concealed.
    macOS top-level aliases (/var, /tmp, /etc) are permitted at root only.
    """
    curr = path
    # If path doesn't exist yet, check from closest existing parent
    while not curr.exists() and curr != curr.parent:
        curr = curr.parent
    p = curr
    while True:
        if p.is_symlink() and p not in MACOS_SYSTEM_SYMLINKS:
            raise SystemExit(f'STOP: {label} or parent contains symlink: {p}')
        if p == p.parent:
            break
        p = p.parent


def check_interpreter() -> None:
    """Strict interpreter enforcement:

    - Python 3.14.7 exactly
    - Bytecode writing disabled (-B flag)
    - Optimization explicitly rejected (sys.flags.optimize == 0)
    - Pinned interpreter binary
    - No PYTHONPATH or PYTHONOPTIMIZE in environment
    """
    if sys.version_info[:3] != (3, 14, 7):
        raise SystemExit(f'STOP: Python 3.14.7 required, found {sys.version}')

    if not sys.dont_write_bytecode:
        raise SystemExit('STOP: bytecode writing enabled; rerun with -B flag')

    if getattr(sys.flags, 'optimize', 0) != 0:
        raise SystemExit('STOP: python optimization (-O/-OO) detected; must run with optimize=0')

    current_bin = Path(sys.executable).resolve()
    if PIN_PATH.exists() and current_bin != PIN_PATH.resolve():
        raise SystemExit(f'STOP: non-pinned interpreter binary: {current_bin} != {PIN_PATH}')

    if 'PYTHONOPTIMIZE' in os.environ:
        raise SystemExit('STOP: PYTHONOPTIMIZE present in environment')

    if 'PYTHONPATH' in os.environ:
        raise SystemExit('STOP: PYTHONPATH present in environment')


def clean_env() -> dict[str, str]:
    """Return a clean environment dictionary copy:

    - PYTHONPATH and PYTHONOPTIMIZE removed
    - PYTHONDONTWRITEBYTECODE set to '1'
    """
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONOPTIMIZE')}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    return env


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _read_json_file(path: Path) -> dict[str, Any]:
    _check_no_symlinks(path, 'json file')
    if not path.is_file():
        raise SystemExit(f'STOP: required JSON file missing: {path}')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:
        raise SystemExit(f'STOP: invalid JSON in {path}: {exc}') from exc
    if not isinstance(data, dict):
        raise SystemExit(f'STOP: JSON root must be dict in {path}')
    return data


def load_context(work_dir: Path | str | None = None) -> dict[str, Any]:
    """Read-only context loader. Reads latest:

    - MANAGEMENT-STATE.json
    - PREPARE-PARAMETERS.json
    - ATTEMPT-POINTER.json -> attempt-record.json
    - BASE_ASSIGNED.json
    Does NOT create or modify any file.
    """
    if work_dir is None:
        work_path = Path(__file__).resolve().parent.parent
    else:
        work_path = Path(work_dir)

    _check_no_symlinks(work_path, 'work directory')
    work_path = work_path.resolve()

    state_path = work_path / 'MANAGEMENT-STATE.json'
    params_path = work_path / 'PREPARE-PARAMETERS.json'
    pointer_path = work_path / 'ATTEMPT-POINTER.json'
    assigned_path = work_path / 'BASE_ASSIGNED.json'

    state = _read_json_file(state_path)
    parameters = _read_json_file(params_path)
    pointer = _read_json_file(pointer_path)
    assigned = _read_json_file(assigned_path)

    # 1. Manager consistency checks
    round_name = state.get('round')
    version = state.get('version')
    if not round_name or not version:
        raise SystemExit('STOP: MANAGEMENT-STATE missing round or version')

    if assigned.get('round') != round_name or assigned.get('version') != version:
        raise SystemExit('STOP: BASE_ASSIGNED round/version mismatch with MANAGEMENT-STATE')

    if parameters.get('round') != round_name or parameters.get('version') != version:
        raise SystemExit('STOP: PREPARE-PARAMETERS round/version mismatch with MANAGEMENT-STATE')

    # 2. Assigned source format checks
    base = assigned.get('base', '')
    diff_base = assigned.get('diff_base', '')
    manifest_sha = assigned.get('source_manifest_sha256', '')

    if not HEX40_RE.match(base):
        raise SystemExit(f'STOP: assigned base is not valid 40-hex SHA1: {base}')
    if not HEX40_RE.match(diff_base):
        raise SystemExit(f'STOP: assigned diff_base is not valid 40-hex SHA1: {diff_base}')
    if not HEX64_RE.match(manifest_sha):
        raise SystemExit(f'STOP: assigned source_manifest_sha256 is not valid 64-hex SHA256: {manifest_sha}')

    # Cross check assigned vs params
    diff_spec = parameters.get('diff', {})
    if diff_spec.get('base') != base:
        raise SystemExit('STOP: PREPARE-PARAMETERS diff.base mismatch with BASE_ASSIGNED')
    if diff_spec.get('diff_base') != diff_base:
        raise SystemExit('STOP: PREPARE-PARAMETERS diff.diff_base mismatch with BASE_ASSIGNED')

    # 3. Pointer and unique attempt record provenance
    record_path_str = pointer.get('record_path')
    if not record_path_str:
        raise SystemExit('STOP: ATTEMPT-POINTER missing record_path')
    record_path = Path(record_path_str)
    _check_no_symlinks(record_path, 'record_path')
    record = _read_json_file(record_path)

    if record.get('round') != round_name or record.get('version') != version:
        raise SystemExit('STOP: attempt-record round/version mismatch with MANAGEMENT-STATE')

    record_attempt = record.get('attempt')
    # Positive integer attempt check
    if not isinstance(record_attempt, int) or record_attempt <= 0:
        raise SystemExit(f'STOP: attempt must be a positive integer, got: {record_attempt}')

    current_attempt = state.get('current_attempt')
    if current_attempt is not None and record_attempt != current_attempt:
        raise SystemExit(f'STOP: record attempt ({record_attempt}) != current_attempt ({current_attempt})')

    if record.get('record_path') and Path(record['record_path']).resolve() != record_path.resolve():
        raise SystemExit('STOP: attempt-record internal record_path differs from pointer')

    # Verify pointer tools exist and match sha256
    for tool_key in ('prepare_tool', 'migration_checks'):
        t_path_str = pointer.get(tool_key)
        exp_sha = pointer.get(f'{tool_key}_sha256')
        if t_path_str and exp_sha:
            t_path = Path(t_path_str)
            _check_no_symlinks(t_path, tool_key)
            if not t_path.is_file():
                raise SystemExit(f'STOP: {tool_key} missing at: {t_path}')
            act_sha = _sha256_file(t_path)
            if act_sha != exp_sha:
                raise SystemExit(f'STOP: {tool_key} sha256 drifted: expected {exp_sha}, got {act_sha}')

    # 4. Release root boundaries and path security
    root_str = parameters.get('release_root')
    if not root_str:
        raise SystemExit('STOP: PREPARE-PARAMETERS missing release_root')
    root = Path(root_str)
    _check_no_symlinks(root, 'release_root')
    root = root.resolve()
    if not root.is_dir():
        raise SystemExit(f'STOP: release_root must be existing directory: {root}')

    # Validate consumer paths inside attempt record
    for field in ('acceptance_step4_dir', 'reports_step4_dir', 'releases_package_dir', 'step4_helpers_dir'):
        p_str = record.get(field)
        if not p_str:
            raise SystemExit(f'STOP: attempt-record missing {field}')
        p = Path(p_str)
        _check_no_symlinks(p, field)
        if not p.resolve().is_relative_to(root):
            raise SystemExit(f'STOP: {field} escapes release_root: {p}')

    # 5. Previous package check
    prev_pkg_str = state.get('previous_package')
    if not prev_pkg_str:
        raise SystemExit('STOP: MANAGEMENT-STATE missing previous_package')
    previous_package = Path(prev_pkg_str)
    _check_no_symlinks(previous_package, 'previous_package')
    previous_package = previous_package.resolve()

    # 6. Build context dictionary with strictly normalized keys
    source = root / f'source-{round_name}'
    candidate = source / 'development' / 'integrated-candidate'
    acceptance = root / f'acceptance-{round_name}'
    reports = root / f'reports/{round_name}'

    repo_str = diff_spec.get('repo', '/Users/suzhizhou/Documents/小八AgentOS/xiaoba-agentos')
    repo = Path(repo_str)
    _check_no_symlinks(repo, 'repo')
    repo = repo.resolve()

    live = Path('/Users/suzhizhou/Documents/小八AgentOS')
    _check_no_symlinks(live, 'live')
    live = live.resolve()

    ctx = {
        'root': root,
        'work': work_path,
        'live': live,
        'repo': repo,
        'source': source,
        'candidate': candidate,
        'acceptance': acceptance,
        'reports': reports,
        'previous_package': previous_package,
        'round': str(round_name),
        'version': str(version),
        'record': record,
        'assigned': assigned,
        'parameters': parameters,
        'state': state,
    }
    return ctx


def verify_manifests(ctx: dict[str, Any]) -> None:
    """Verify tool manifests specified in state.tool_manifests.

    - Must specify at least two DISTINCT manifest paths.
    - Each manifest must exist and be nonempty.
    - ALL manifested entries must be strictly within release_root.
    - NO symlinks allowed anywhere in the paths.
    - SHA256 of each manifested file must match exactly.
    """
    state = ctx.get('state', {})
    manifests = state.get('tool_manifests')
    if not manifests or not isinstance(manifests, list) or len(manifests) < 2:
        raise SystemExit('STOP: state.tool_manifests must specify at least two manifest paths')

    # Two distinct manifests check
    if len(set(str(Path(m).resolve()) for m in manifests)) < 2:
        raise SystemExit('STOP: state.tool_manifests paths must be distinct files')

    root = Path(ctx['root']).resolve()
    for m_entry in manifests:
        m_path = Path(m_entry)
        _check_no_symlinks(m_path, 'manifest path')
        m_path = m_path.resolve()
        if not m_path.is_file():
            raise SystemExit(f'STOP: tool manifest missing: {m_path}')
        data = _read_json_file(m_path)
        if not data:
            raise SystemExit(f'STOP: tool manifest is empty: {m_path}')

        entries: dict[str, str] = {}
        if 'tools' in data and isinstance(data['tools'], dict):
            entries = data['tools']
        elif 'files' in data and isinstance(data['files'], dict):
            entries = data['files']
        else:
            for k, v in data.items():
                if isinstance(v, str):
                    entries[k] = v
                elif isinstance(v, dict) and 'sha256' in v:
                    entries[k] = v['sha256']

        if not entries:
            raise SystemExit(f'STOP: no file entries parsed in tool manifest: {m_path}')

        for rel_str, exp_hash in entries.items():
            raw_path = Path(rel_str)
            if raw_path.is_absolute():
                f_path = raw_path
            else:
                f_path = root / raw_path

            # Crucial: verify that the path does NOT escape release_root!
            # Reject any entry outside release_root (reproduces & fixes Probe 1)
            try:
                resolved_f = f_path.resolve()
                if not resolved_f.is_relative_to(root):
                    raise SystemExit(f'STOP: manifest entry escapes release_root: {rel_str}')
            except Exception as exc:
                raise SystemExit(f'STOP: manifest entry path invalid {rel_str}: {exc}') from exc

            # Verify no symlinks in path
            _check_no_symlinks(f_path, f'manifested entry {rel_str}')

            if not f_path.is_file():
                raise SystemExit(f'STOP: manifested tool file missing: {f_path}')
            act_hash = _sha256_file(f_path)
            if act_hash != exp_hash:
                raise SystemExit(
                    f'STOP: tool file hash drifted for {f_path}: expected {exp_hash}, got {act_hash}'
                )


def validate_context(ctx: dict[str, Any], stage: str) -> None:
    """Stage pre-flight validation gate.

    Throws explicit exceptions before any writes or subprocesses.
    """
    if stage not in VALID_STAGES:
        raise SystemExit(f'STOP: unknown pipeline stage: {stage}')

    # Every stage requires interpreter and manifest sanity
    check_interpreter()
    verify_manifests(ctx)

    repo = Path(ctx['repo'])
    _check_no_symlinks(repo, 'repo')
    repo = repo.resolve()
    if not repo.is_dir():
        raise SystemExit(f'STOP: repo directory invalid: {repo}')

    base = ctx['assigned']['base']
    # 1. Verify Git commit object exists
    try:
        git_check = subprocess.run(
            ['git', '-C', str(repo), 'rev-parse', f'{base}^{{commit}}'],
            capture_output=True,
            text=True,
            check=False,
        )
        if git_check.returncode != 0 or git_check.stdout.strip() != base:
            raise SystemExit(f'STOP: BASE commit {base} not found in repo {repo}')
    except Exception as exc:
        raise SystemExit(f'STOP: failed to verify git object {base}: {exc}') from exc

    # 2. Cold-start preflight check of Git candidate contents before ANY directory or marker write:
    # Reproduces & fixes Probe 2 (wrong Git source version accepted by export preflight)
    # Check pyproject.toml inside Git base object
    try:
        pyproj_raw = subprocess.check_output(
            ['git', '-C', str(repo), 'cat-file', '-p', f'{base}:development/integrated-candidate/pyproject.toml'],
            text=True,
            stderr=subprocess.PIPE,
        )
        meta = tomllib.loads(pyproj_raw)
        cand_ver = 'v' + meta['project']['version']
        if cand_ver != ctx['version']:
            raise SystemExit(f'STOP: git candidate version {cand_ver} != expected manager version {ctx["version"]}')
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(f'STOP: git candidate pyproject.toml missing or invalid in {base}: {exc}') from exc

    # Check config binding inside Git base object
    try:
        cfg_raw = subprocess.check_output(
            ['git', '-C', str(repo), 'cat-file', '-p', f'{base}:development/integrated-candidate/config/agentos.json'],
            text=True,
            stderr=subprocess.PIPE,
        )
        cfg_data = json.loads(cfg_raw)
        if cfg_data.get('release', {}).get('expected_version') != ctx['version']:
            raise SystemExit(f'STOP: git candidate release binding differs from manager version {ctx["version"]}')
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(f'STOP: git candidate config/agentos.json missing or invalid in {base}: {exc}') from exc

    # Check SOURCE_MANIFEST.json sha256 inside Git base object matches BASE_ASSIGNED
    try:
        man_bytes = subprocess.check_output(
            ['git', '-C', str(repo), 'cat-file', '-p', f'{base}:development/integrated-candidate/SOURCE_MANIFEST.json'],
            stderr=subprocess.PIPE,
        )
        man_sha = hashlib.sha256(man_bytes).hexdigest()
        if man_sha != ctx['assigned']['source_manifest_sha256']:
            raise SystemExit(
                f'STOP: git candidate SOURCE_MANIFEST.json sha256 mismatch: expected {ctx["assigned"]["source_manifest_sha256"]}, got {man_sha}'
            )
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(f'STOP: git candidate SOURCE_MANIFEST.json missing or invalid in {base}: {exc}') from exc

    # Check release doc exists inside Git base object
    rel_doc_git_path = f'{base}:development/integrated-candidate/docs/RELEASE_{ctx["version"].upper()}.md'
    try:
        subprocess.run(
            ['git', '-C', str(repo), 'cat-file', '-e', rel_doc_git_path],
            check=True,
            capture_output=True,
        )
    except Exception as exc:
        raise SystemExit(f'STOP: git candidate release doc missing in {base}: {rel_doc_git_path}') from exc

    source = Path(ctx['source'])
    candidate = Path(ctx['candidate'])
    work = Path(ctx['work'])

    if stage in ('export', 'prepare'):
        # Cold start: source may be nonexistent or empty; if nonempty, verify it
        if source.exists():
            _check_no_symlinks(source, 'source directory')
        return

    # All stages after prepare (freeze, review, step4, switch, post-switch) require:
    # 1. source exported and candidate intact
    _check_no_symlinks(source, 'source directory')
    _check_no_symlinks(candidate, 'candidate directory')
    if not source.is_dir():
        raise SystemExit(f'STOP: source directory missing: {source}')
    if not candidate.is_dir():
        raise SystemExit(f'STOP: candidate directory missing: {candidate}')

    # 2. candidate version matching manager version
    pyproject = candidate / 'pyproject.toml'
    if not pyproject.is_file() or pyproject.is_symlink():
        raise SystemExit(f'STOP: candidate pyproject.toml missing: {pyproject}')
    try:
        meta = tomllib.loads(pyproject.read_text(encoding='utf-8'))
        cand_ver = 'v' + meta['project']['version']
        if cand_ver != ctx['version']:
            raise SystemExit(f'STOP: candidate version {cand_ver} != manager version {ctx["version"]}')
    except Exception as exc:
        raise SystemExit(f'STOP: failed to read candidate version: {exc}') from exc

    # 3. candidate manifest matching assigned manifest
    cand_manifest = candidate / 'SOURCE_MANIFEST.json'
    if not cand_manifest.is_file() or cand_manifest.is_symlink():
        raise SystemExit(f'STOP: candidate SOURCE_MANIFEST.json missing: {cand_manifest}')
    if _sha256_file(cand_manifest) != ctx['assigned']['source_manifest_sha256']:
        raise SystemExit('STOP: candidate SOURCE_MANIFEST.json sha256 mismatch with BASE_ASSIGNED')

    # 4. BASE_READY and PREPARATION matching
    base_ready_path = work / 'BASE_READY.json'
    prep_path = work / 'PREPARATION.json'
    if not base_ready_path.is_file() or not prep_path.is_file():
        raise SystemExit('STOP: preparation evidence missing (BASE_READY.json / PREPARATION.json)')

    base_ready = _read_json_file(base_ready_path)
    prep = _read_json_file(prep_path)

    if base_ready.get('base') != base:
        raise SystemExit('STOP: BASE_READY.json base does not match assigned BASE')
    if base_ready.get('preparation_sha256') != _sha256_file(prep_path):
        raise SystemExit('STOP: BASE_READY.json preparation_sha256 mismatch')
    if prep.get('base') != base:
        raise SystemExit('STOP: PREPARATION.json base does not match assigned BASE')

    # 5. Diff check for post-prepare stages
    diff_name = f'{ctx["assigned"]["diff_base"][:7]}..BASE.diff'
    diff_file = Path(ctx['acceptance']) / diff_name
    if not diff_file.is_file() or _sha256_file(diff_file) != prep.get('diff_sha256'):
        raise SystemExit(f'STOP: round diff missing or hash drifted: {diff_file}')

    # Stage-specific gates
    if stage == 'freeze':
        state_file = Path(ctx['acceptance']) / 'STATE.json'
        if state_file.exists():
            raise SystemExit('STOP: freeze already completed (STATE.json exists)')
    elif stage == 'nodes':
        freeze_file = Path(ctx['acceptance']) / 'FREEZE.json'
        state_file = Path(ctx['acceptance']) / 'STATE.json'
        if not freeze_file.is_file() or not state_file.is_file():
            raise SystemExit('STOP: nodes requires a frozen site (FREEZE.json / STATE.json)')
    elif stage == 'review':
        freeze_file = Path(ctx['acceptance']) / 'FREEZE.json'
        state_file = Path(ctx['acceptance']) / 'STATE.json'
        if not freeze_file.is_file() or not state_file.is_file():
            raise SystemExit('STOP: review requires freeze evidence (FREEZE.json / STATE.json)')
        state_data = _read_json_file(state_file)
        if state_data.get('tasks'):
            raise SystemExit('STOP: review tasks already claimed in STATE.json')


# --- 75S Gap 3: read-only baseline raw-log evidence gate (callable by 75Q) -----

_RAN_RE = re.compile(r'^Ran (\d+) tests? in ', re.M)
_OK_RE = re.compile(r'^OK(?: \(skipped=(\d+)\))?$', re.M)
_FAILED_RE = re.compile(r'^FAILED\b', re.M)


def _parse_unittest_log(content: str) -> dict[str, Any]:
    """Extract genuine unittest completion facts from a raw baseline log.

    Never trusts a caller-supplied tests.result parameter: the Ran/OK/FAILED
    lines must actually be present in the log text.
    """
    ran_matches = _RAN_RE.findall(content)
    ok_match = _OK_RE.search(content)
    failed = bool(_FAILED_RE.search(content))
    ran = int(ran_matches[-1]) if ran_matches else None
    ok_skipped = int(ok_match.group(1)) if ok_match and ok_match.group(1) else 0
    return {'ran': ran, 'ok': ok_match is not None, 'ok_skipped': ok_skipped, 'failed': failed}


def _result_line(content: str, suite: str) -> dict[str, Any] | None:
    """Find the baseline_runner JSON result line for a suite (last one wins)."""
    found = None
    for line in content.splitlines():
        line = line.strip()
        if line.startswith('{"suite":') and line.endswith('}'):
            try:
                value = json.loads(line)
            except Exception:
                continue
            if isinstance(value, dict) and value.get('suite') == suite:
                found = value
    return found


def validate_baseline_evidence(ctx: dict[str, Any], *, require_freeze: bool = True) -> dict[str, Any]:
    """Read-only gate over BASE_READY / PREPARATION / FREEZE baseline evidence.

    Callable by 75Q (or any consumer) BEFORE writing markers/bindings or starting
    heavy work. Raises SystemExit on the first inconsistency; never writes.

    Checks:
      * BASE_READY <-> PREPARATION <-> FREEZE <-> STATE mutual hashes and base.
      * Exactly the manager's expected_suites are present (dynamic, no hardcoded
        counts); each suite's tests/skipped equal the manager expectation.
      * Each raw log's sha256 equals both PREPARATION.baseline_logs and
        FREEZE.test_logs; the log genuinely contains Ran N / OK / no FAILED, and
        the embedded JSON result line agrees with the recorded counts.
      * PREPARATION records a real exit_code, execution interpreter and runner
        hash for every suite; the runner hash still matches baseline_runner.py.
      * candidate SOURCE_MANIFEST.json sha256 equals the assigned manifest.
    """
    work = Path(ctx['work'])
    acceptance = Path(ctx['acceptance'])
    candidate = Path(ctx['candidate'])
    reports = Path(ctx['reports'])
    base = ctx['assigned']['base']
    assigned_manifest = ctx['assigned']['source_manifest_sha256']
    state = ctx['state']

    expected = state.get('expected_suites')
    if not isinstance(expected, dict) or not expected:
        raise SystemExit('STOP: MANAGEMENT-STATE.expected_suites missing; cannot hardcode suite counts')
    for suite, spec in expected.items():
        if not isinstance(spec, dict) or not isinstance(spec.get('tests'), int) or not isinstance(spec.get('skipped'), int):
            raise SystemExit(f'STOP: expected_suites[{suite}] must declare integer tests and skipped')

    base_ready_path = work / 'BASE_READY.json'
    prep_path = work / 'PREPARATION.json'
    if not base_ready_path.is_file() or not prep_path.is_file():
        raise SystemExit('STOP: baseline evidence missing (BASE_READY.json / PREPARATION.json)')
    base_ready = _read_json_file(base_ready_path)
    prep = _read_json_file(prep_path)

    if base_ready.get('base') != base or prep.get('base') != base:
        raise SystemExit('STOP: BASE_READY/PREPARATION base does not match assigned BASE')
    if base_ready.get('preparation_sha256') != _sha256_file(prep_path):
        raise SystemExit('STOP: BASE_READY.preparation_sha256 does not match PREPARATION.json')
    if prep.get('source_manifest_sha256') != assigned_manifest:
        raise SystemExit('STOP: PREPARATION.source_manifest_sha256 does not match assigned manifest')

    baseline_logs = prep.get('baseline_logs')
    if not isinstance(baseline_logs, dict):
        raise SystemExit('STOP: PREPARATION.baseline_logs missing or malformed')
    # baseline_logs is keyed by log filename; index it by the suite it records.
    by_suite: dict[str, tuple[str, dict[str, Any]]] = {}
    for fname, rec in baseline_logs.items():
        if not isinstance(rec, dict):
            raise SystemExit(f'STOP: baseline_logs[{fname}] is not a record dict')
        s = rec.get('suite')
        if not isinstance(s, str) or s in by_suite:
            raise SystemExit(f'STOP: baseline_logs[{fname}] has missing or duplicate suite')
        by_suite[s] = (fname, rec)
    if set(by_suite) != set(expected):
        raise SystemExit(
            f'STOP: baseline suites incomplete; expected {sorted(expected)}, found {sorted(by_suite)}'
        )

    runner_path = Path(__file__).resolve().parent / 'baseline_runner.py'
    runner_sha = _sha256_file(runner_path) if runner_path.is_file() else None
    prep_reports = reports / 'prepare'
    totals: dict[str, Any] = {}
    total_tests = 0
    total_skipped = 0

    for suite in sorted(expected):
        filename, record = by_suite[suite]
        log_path = prep_reports / filename
        if not log_path.is_file() or log_path.is_symlink():
            raise SystemExit(f'STOP: baseline log missing for suite {suite}: {log_path}')

        exp_tests = expected[suite]['tests']
        exp_skipped = expected[suite]['skipped']
        rec_tests = record.get('tests')
        rec_skipped = record.get('skipped')
        if rec_tests != exp_tests or rec_skipped != exp_skipped:
            raise SystemExit(
                f'STOP: suite {suite} recorded tests/skipped ({rec_tests}/{rec_skipped}) '
                f'!= manager expectation ({exp_tests}/{exp_skipped})'
            )
        if record.get('ok') is not True:
            raise SystemExit(f'STOP: suite {suite} not recorded ok')
        if record.get('exit_code') != 0:
            raise SystemExit(f'STOP: suite {suite} did not record a real zero exit_code')
        interpreter = record.get('interpreter')
        if not interpreter:
            raise SystemExit(f'STOP: suite {suite} did not record the execution interpreter')
        if runner_sha is not None and record.get('runner_sha256') != runner_sha:
            raise SystemExit(f'STOP: suite {suite} runner_sha256 does not match baseline_runner.py')

        actual_sha = _sha256_file(log_path)
        if actual_sha != record.get('sha256'):
            raise SystemExit(f'STOP: baseline log sha256 drifted for suite {suite}: {log_path}')
        content = log_path.read_text(encoding='utf-8')

        result = _result_line(content, suite)
        if not isinstance(result, dict):
            raise SystemExit(f'STOP: suite {suite} log has no parseable JSON result line')
        if result.get('tests') != exp_tests or result.get('skipped') != exp_skipped:
            raise SystemExit(f'STOP: suite {suite} JSON result counts disagree with manager expectation')
        if result.get('ok') is not True or result.get('errors') or result.get('failures'):
            raise SystemExit(f'STOP: suite {suite} JSON result is not a clean pass')

        facts = _parse_unittest_log(content)
        if facts['ran'] != exp_tests:
            raise SystemExit(f'STOP: suite {suite} log does not genuinely report Ran {exp_tests} tests')
        if not facts['ok'] or facts['failed']:
            raise SystemExit(f'STOP: suite {suite} log lacks a genuine OK or contains FAILED')
        if facts['ok_skipped'] != exp_skipped:
            raise SystemExit(f'STOP: suite {suite} OK skipped count != expected {exp_skipped}')

        totals[suite] = {'tests': exp_tests, 'skipped': exp_skipped, 'sha256': actual_sha}
        total_tests += exp_tests
        total_skipped += exp_skipped

    # candidate manifest must still equal the assigned manifest
    cand_manifest = candidate / 'SOURCE_MANIFEST.json'
    if not cand_manifest.is_file() or cand_manifest.is_symlink():
        raise SystemExit(f'STOP: candidate SOURCE_MANIFEST.json missing: {cand_manifest}')
    if _sha256_file(cand_manifest) != assigned_manifest:
        raise SystemExit('STOP: candidate SOURCE_MANIFEST.json sha256 != assigned manifest')

    freeze_summary: dict[str, Any] = {}
    if require_freeze:
        freeze_path = acceptance / 'FREEZE.json'
        if not freeze_path.is_file():
            raise SystemExit('STOP: FREEZE.json missing for baseline evidence gate')
        freeze = _read_json_file(freeze_path)
        if freeze.get('base') != base:
            raise SystemExit('STOP: FREEZE.base does not match assigned BASE')
        if freeze.get('source_manifest_sha256') != assigned_manifest:
            raise SystemExit('STOP: FREEZE.source_manifest_sha256 does not match assigned manifest')
        if freeze.get('tests') != prep.get('tests'):
            raise SystemExit('STOP: FREEZE.tests does not match PREPARATION.tests')
        freeze_logs = freeze.get('test_logs')
        if not isinstance(freeze_logs, dict):
            raise SystemExit('STOP: FREEZE.test_logs missing or malformed')
        for suite in sorted(expected):
            filename, record = by_suite[suite]
            if freeze_logs.get(filename) != record.get('sha256'):
                raise SystemExit(f'STOP: FREEZE.test_logs[{filename}] != PREPARATION baseline sha256')
        freeze_summary = {'freeze_sha256': _sha256_file(freeze_path)}
        state_path = acceptance / 'STATE.json'
        if state_path.is_file():
            state_data = _read_json_file(state_path)
            if state_data.get('freeze_sha256') != _sha256_file(freeze_path):
                raise SystemExit('STOP: STATE.freeze_sha256 does not match FREEZE.json')

    return {
        'ok': True,
        'base': base,
        'suites': totals,
        'total_tests': total_tests,
        'total_skipped': total_skipped,
        'source_manifest_sha256': assigned_manifest,
        **freeze_summary,
    }


# --- 75S correction: write-before preflight (no weakening for absent inputs) ---

def validate_active_config(ctx: dict[str, Any]) -> dict[str, Any]:
    """Read-only B09 active-config binding for the current (pre-switch) round.

    The manager declares live_config_path + live_config_sha256_before. The actual
    file hash MUST equal the declared before-hash; a mismatch is rejected, never
    merely recorded. No config content is printed or copied. Post-switch stages
    legitimately rebind the config and use a different gate; nodes/review here are
    pre-switch preparation, so the before-hash applies.
    """
    state = ctx['state']
    path_str = state.get('live_config_path')
    expected = state.get('live_config_sha256_before')
    if not path_str or not expected:
        raise SystemExit('STOP: 缺少本轮显式活动配置 live_config_path/hash（B09）；不猜 active.json、不按目录名推断')
    if not HEX64_RE.match(str(expected)):
        raise SystemExit(f'STOP: live_config_sha256_before 不是合法 64-hex：{expected}')
    path = Path(path_str)
    _check_no_symlinks(path, 'live_config')
    if not path.is_file():
        raise SystemExit(f'STOP: 本轮活动配置不存在：{path}')
    actual = _sha256_file(path)
    if actual != expected:
        raise SystemExit(
            f'STOP: 活动配置 hash 与本轮声明不符（B09），拒绝写前继续：expected {expected}, got {actual}'
        )
    return {'path': str(path), 'expected': expected, 'actual': actual}


def preflight_write_gate(ctx: dict[str, Any], stage: str) -> dict[str, Any]:
    """Unified read-only gate before ANY marker / isolated pool / real CLI.

    Shared by the nodes and review normal entries and their directly-called
    subjects. Missing or drifted formal inputs reject here with zero marker, zero
    pool initialization and zero CLI invocation. Checks: pinned interpreter, the
    two tool manifests, the stage's frozen-source contract (per-file export blobs
    against the assigned BASE, candidate manifest, PREP/READY/FREEZE as applicable),
    and this round's explicit active-config hash (B09).
    """
    check_interpreter()
    verify_manifests(ctx)
    validate_context(ctx, stage)
    active = validate_active_config(ctx)
    from verify_export import verify_export_blobs
    blobs = verify_export_blobs(ctx['repo'], ctx['assigned']['base'], ctx['source'])
    if not blobs.get('ok'):
        raise SystemExit(f'STOP: source 逐文件内容与指派 BASE 不符，拒绝写前继续：{blobs}')
    result: dict[str, Any] = {'stage': stage, 'active_config': active, 'source_blobs': blobs}
    # Post-freeze preparation stages bind the raw baseline logs too, so a node-test
    # marker can never be claimed over drifted/incomplete suite evidence.
    if stage in ('nodes', 'review'):
        result['baseline_evidence'] = validate_baseline_evidence(ctx)
    return result
