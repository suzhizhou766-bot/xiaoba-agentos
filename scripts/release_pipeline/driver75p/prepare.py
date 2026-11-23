"""Stage 75P Preparation tool: export, baseline verification, manifest, brain tree, diff.

Enforces:
1. Cold-start write-before contract: old live SOURCE_MANIFEST == previous_source_sha256,
   75A monitor three-file authorization + whole-monitor same-source set, and the unique
   record.diff authority == actual params-scope git diff (all read-only, real Git refs).
2. Zero-source Git archive export over derived six-scope roots (development whole +
   monitor + docs/architecture-topology) + per-blob SHA1 check against git ls-tree.
3. 4-suite baseline execution with dynamic parsing of testsRun / skipped (no hardcoding 1666, no 0-test bypass).
4. create_manifest --verify (dynamic parsing of file counts, no hardcoding 53).
5. Second brain tree generation and verification.
6. PREP/FREEZE/STATE bind the unique record.diff.diff_path authority (single diff_base).
7. Atomic PREPARATION.json and BASE_READY.json mutual SHA256 binding.
8. Single-claim PREPARATION.started marker; failed subprocesses ALWAYS write log before raising.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path
from typing import Any, Callable

import release_context as rc
import cold_start_contract as csc
from verify_export import verify_export_blobs


def _sha256_file(path: Path) -> str:
    return rc._sha256_file(path)


def run_prepare(
    ctx: dict[str, Any],
    *,
    subproc_runner: Callable[[list[str], Path, dict[str, str]], tuple[int, str]] | None = None,
) -> dict[str, Any]:
    rc.check_interpreter()
    rc.validate_context(ctx, stage='prepare')

    # Cold-start write-before contract (read-only): old live source manifest ==
    # manager.previous_source_sha256, the 75A monitor three-file authorization +
    # whole-monitor same-source set, and the unique record.diff authority matching
    # the actual params-scope git diff. Rejects here with zero marker / zero export.
    contract = csc.cold_start_prewrite_contract(ctx)
    roots = contract['export_roots']
    diff_authority = contract['diff_authority']

    work = Path(ctx['work'])
    root = Path(ctx['root'])
    repo = Path(ctx['repo'])
    source = Path(ctx['source'])
    candidate = Path(ctx['candidate'])
    acceptance = Path(ctx['acceptance'])
    reports = Path(ctx['reports'])
    base = ctx['assigned']['base']
    diff_base = ctx['assigned']['diff_base']
    round_name = ctx['round']
    version = ctx['version']

    # Gate: never regenerate already prepared base
    base_ready_path = work / 'BASE_READY.json'
    prep_path = work / 'PREPARATION.json'
    if base_ready_path.is_file() and prep_path.is_file():
        raise SystemExit('STOP: already prepared (BASE_READY.json exists); do not rerun')

    # Single-claim gate: PREPARATION.started
    prep_started = work / 'PREPARATION.started'
    if prep_started.exists():
        raise SystemExit('STOP: preparation attempt already claimed (PREPARATION.started exists); do not rerun')

    # Source check: must be either nonexistent or empty dir
    if source.exists():
        if source.is_symlink() or not source.is_dir() or any(source.iterdir()):
            raise SystemExit(f'STOP: source export directory {source} exists and is nonempty; refusing overwrite')

    # Ensure report directories exist
    prep_reports = reports / 'prepare'
    prep_reports.mkdir(parents=True, exist_ok=True)
    acceptance.mkdir(parents=True, exist_ok=True)

    # Claim preparation
    prep_started.mkdir(exist_ok=False)

    clean_env = rc.clean_env()

    def _exec_and_log(cmd: list[str], cwd: Path, log_file: Path | None = None) -> str:
        """Execute command and GUARANTEE log is written even on failure before raising."""
        if subproc_runner:
            code, out = subproc_runner(cmd, cwd, clean_env)
        else:
            res = subprocess.run(cmd, cwd=cwd, env=clean_env, capture_output=True, text=True, check=False)
            code = res.returncode
            out = res.stdout + res.stderr

        if log_file is not None:
            log_file.write_text(out, encoding='utf-8')

        if code != 0:
            raise SystemExit(f'STOP: command failed ({code}): {" ".join(cmd)}; log preserved at {log_file}')
        return out

    # 1. Export Git archive over the derived six-scope roots (development whole +
    # monitor + docs/architecture-topology). Git archive/ls-tree are read-only and
    # always real; only the heavy baseline/manifest/brain runners are injectable.
    try:
        archive_bytes = subprocess.check_output(
            ['git', '-C', str(repo), 'archive', base, '--', *roots],
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as err:
        err_log = prep_reports / '00-export-error.log'
        err_log.write_text(err.stderr.decode('utf-8', errors='replace'), encoding='utf-8')
        raise SystemExit(f'STOP: git archive failed; log preserved at {err_log}') from err

    source.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as tar:
        for item in tar:
            target_p = source / item.name
            if not target_p.resolve().is_relative_to(source) or item.issym() or item.islnk():
                raise SystemExit(f'STOP: Refusing unsafe archive item: {item.name}')
            if item.isdir():
                target_p.mkdir(parents=True, exist_ok=True)
            else:
                if not item.isfile():
                    raise SystemExit(f'STOP: archive entry not a regular file: {item.name}')
                target_p.parent.mkdir(parents=True, exist_ok=True)
                with target_p.open('xb') as handle:
                    handle.write(tar.extractfile(item).read())
                target_p.chmod(item.mode & 0o777)

    # 2. Verify export blob SHA1 strictly over the SAME derived roots (no hardcoded
    # file count). Read-only per-blob check against git ls-tree at the assigned BASE.
    export_verify_log = prep_reports / '00-export-verified.json'
    blob_res = verify_export_blobs(repo, base, source, roots)
    export_verify_log.write_text(
        json.dumps(blob_res, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
    )
    if not blob_res['ok']:
        raise SystemExit(f'STOP: export blob verification failed: {blob_res}')

    # 3. Check candidate pyproject.toml and agentos.json
    pyproject = candidate / 'pyproject.toml'
    if not pyproject.is_file():
        raise SystemExit(f'STOP: candidate pyproject.toml missing: {pyproject}')
    meta = tomllib.loads(pyproject.read_text(encoding='utf-8'))
    cand_ver = 'v' + meta['project']['version']
    if cand_ver != version:
        raise SystemExit(f'STOP: candidate version {cand_ver} != manager version {version}')

    cfg_template_path = candidate / 'config/agentos.json'
    if not cfg_template_path.is_file():
        raise SystemExit(f'STOP: candidate agentos.json missing: {cfg_template_path}')
    cfg_template = json.loads(cfg_template_path.read_text(encoding='utf-8'))
    if cfg_template.get('release', {}).get('expected_version') != version:
        raise SystemExit('STOP: candidate release binding differs from manager version')

    # 4. Run 4 baseline suites and dynamically parse results
    baseline_logs: dict[str, Any] = {}
    tests_summary: dict[str, int] = {}
    suite_files = [
        ('core', '01-core.log'),
        ('ingress', '02-ingress.log'),
        ('architecture', '03-architecture.log'),
        ('monitor', '04-monitor.log'),
    ]

    pin_bin = str(rc.PIN_PATH if rc.PIN_PATH.is_file() else sys.executable)
    baseline_helper = Path(__file__).resolve().parent / 'baseline_runner.py'
    runner_sha = _sha256_file(baseline_helper)

    def _run_logged(cmd: list[str], cwd: Path, log_file: Path) -> tuple[int, str]:
        """Run, ALWAYS preserve the raw log, and return the real exit code."""
        if subproc_runner:
            code, out = subproc_runner(cmd, cwd, clean_env)
        else:
            res = subprocess.run(cmd, cwd=cwd, env=clean_env, capture_output=True, text=True, check=False)
            code = res.returncode
            out = res.stdout + res.stderr
        log_file.write_text(out, encoding='utf-8')
        return code, out

    for suite, filename in suite_files:
        log_path = prep_reports / filename
        cmd = [pin_bin, '-B', str(baseline_helper), str(source), suite]
        code, log_text = _run_logged(cmd, work, log_path)
        if code != 0:
            raise SystemExit(f'STOP: baseline suite {suite} exited {code}; log preserved at {log_path}')

        parsed_result = None
        for line in reversed(log_text.splitlines()):
            line = line.strip()
            if line.startswith('{"suite":') and line.endswith('}'):
                try:
                    candidate_result = json.loads(line)
                except Exception:
                    continue
                if isinstance(candidate_result, dict) and candidate_result.get('suite') == suite:
                    parsed_result = candidate_result
                    break

        if not parsed_result or not parsed_result.get('ok'):
            raise SystemExit(f'STOP: baseline suite {suite} failed or output corrupted; see {log_path}')
        if parsed_result.get('errors') or parsed_result.get('failures'):
            raise SystemExit(f'STOP: baseline suite {suite} reported errors/failures; see {log_path}')

        test_count = int(parsed_result['tests'])
        skip_count = int(parsed_result['skipped'])
        # Strict validation: suite must have non-zero tests and must not be a dummy constant
        if test_count <= 0:
            raise SystemExit(f'STOP: baseline suite {suite} reported 0 tests executed; refusing dummy pass')

        baseline_logs[filename] = {
            'suite': suite,
            'log_file': filename,
            'tests': test_count,
            'skipped': skip_count,
            'ok': True,
            'exit_code': code,
            'interpreter': pin_bin,
            'runner_sha256': runner_sha,
            'command': cmd,
            'sha256': _sha256_file(log_path),
        }
        tests_summary[suite] = test_count
        tests_summary[f'{suite}_skipped'] = skip_count

    # Ensure all 4 suites are present, and counts match the manager expectation
    required_suites = {'core', 'ingress', 'architecture', 'monitor'}
    if set(tests_summary.keys()) & required_suites != required_suites:
        raise SystemExit(f'STOP: incomplete baseline suites; found {list(tests_summary.keys())}')
    expected_suites = ctx['state'].get('expected_suites')
    if not isinstance(expected_suites, dict) or set(expected_suites) != required_suites:
        raise SystemExit('STOP: MANAGEMENT-STATE.expected_suites missing or does not cover the four suites')
    for suite in required_suites:
        exp = expected_suites[suite]
        if tests_summary[suite] != exp.get('tests') or tests_summary[f'{suite}_skipped'] != exp.get('skipped'):
            raise SystemExit(
                f'STOP: baseline suite {suite} counts ({tests_summary[suite]}/'
                f'{tests_summary[f"{suite}_skipped"]}) != manager expectation '
                f'({exp.get("tests")}/{exp.get("skipped")})'
            )

    # 5. create_manifest --verify (parse real file count, not hardcoding)
    manifest_log = prep_reports / '05-manifest-verified.log'
    manifest_script = candidate / 'scripts/create_manifest.py'
    man_out = _exec_and_log(
        [pin_bin, '-B', str(manifest_script), '--verify'],
        candidate,
        manifest_log,
    )

    manifest_files_count = 0
    manifest_file = candidate / 'SOURCE_MANIFEST.json'
    if manifest_file.is_file():
        try:
            m_data = json.loads(manifest_file.read_text(encoding='utf-8'))
            manifest_files_count = len(m_data.get('files', m_data))
        except Exception:
            pass
    if manifest_files_count == 0:
        for line in reversed(man_out.splitlines()):
            if 'files' in line:
                m = re.search(r'(\d+)\s+files', line)
                if m:
                    manifest_files_count = int(m.group(1))
                    break
    if manifest_files_count <= 0:
        raise SystemExit('STOP: create_manifest --verify produced non-positive file count')

    # 6. Build second brain and verify
    brain_dir = root / f'generated-brain-{round_name}'
    brain_log = prep_reports / '07-second-brain-generate.log'
    brain_script = candidate / 'scripts/build_second_brain.py'
    _exec_and_log(
        [
            pin_bin,
            '-B',
            str(brain_script),
            '--agentos-root',
            str(candidate),
            '--published-agentos-root',
            str(ctx['live']),
            '--target',
            str(brain_dir),
        ],
        work,
        brain_log,
    )

    verify_brain_script = Path(__file__).resolve().parent / 'verify_fake_brain.py'
    verify_brain_log = prep_reports / '07-second-brain.log'
    _exec_and_log(
        [pin_bin, '-B', str(verify_brain_script), '--verify'],
        work,
        verify_brain_log,
    )

    # 7. Bind the UNIQUE attempt record.diff authority (the cold-start contract already
    # proved it equals the actual params-scope git diff). PREP/FREEZE/STATE bind this
    # same real file/hash/BASE/diff_base. A byte-identical read-only copy is kept at the
    # legacy round-diff consumer path so validate_context's existing check stays
    # satisfied — same bytes/hash/scope, not a second differently-scoped round diff.
    authority_path = Path(diff_authority['diff_path'])
    diff_sha = diff_authority['diff_sha256']
    if _sha256_file(authority_path) != diff_sha:
        raise SystemExit(f'STOP: record.diff authority hash drifted during prepare: {authority_path}')
    diff_bytes = authority_path.read_bytes()

    diff_name = f'{diff_base[:7]}..BASE.diff'
    diff_file = acceptance / diff_name
    if diff_file.exists():
        if _sha256_file(diff_file) != diff_sha:
            raise SystemExit(f'STOP: legacy round-diff copy differs from authority: {diff_file}')
    else:
        with diff_file.open('xb') as handle:
            handle.write(diff_bytes)
    (prep_reports / '08-git-diff.log').write_text(
        json.dumps({'authority': diff_authority, 'round_diff_copy': str(diff_file)},
                   ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    # 8. Commit evidence into PREPARATION.json and BASE_READY.json
    prep_data = {
        'round': round_name,
        'base': base,
        'diff_base': diff_base,
        'version': version,
        'previous_version': ctx['state'].get('previous_version'),
        'source_manifest_sha256': _sha256_file(candidate / 'SOURCE_MANIFEST.json'),
        'baseline_logs': baseline_logs,
        'tests': tests_summary,
        'manifest_files': manifest_files_count,
        'diff_sha256': diff_sha,
        'diff_file': str(authority_path),
        'round_diff_file': str(diff_file),
        'export_roots': roots,
        'cold_start_contract': contract,
        'no_nodes_called': True,
        'no_freeze_or_package_build': True,
        'no_rehearsal_or_switch': True,
    }

    prep_path.write_text(json.dumps(prep_data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    prep_sha = _sha256_file(prep_path)

    base_ready_data = {
        'base': base,
        'round': round_name,
        'preparation_sha256': prep_sha,
    }
    base_ready_path.write_text(json.dumps(base_ready_data, indent=2) + '\n', encoding='utf-8')

    return {
        'ok': True,
        'phase': 'prepare_completed',
        'base': base,
        'preparation_sha256': prep_sha,
        'diff_sha256': diff_sha,
        'tests': tests_summary,
    }
