"""Same-task failed-side explicit recovery for 75V formal review.

Owns only this module (+ test_recover_review.py). review.py / nodes.py stay
read-only; reuses their public helpers. Does not submit a new task, does not
auto-loop retries, does not rewrite formal reports/review/ bytes.

Normal entry:
  python3.14 -B recover_review.py --plan
  python3.14 -B recover_review.py --run --allow-real \\
      --failed-node <qoder-node-id> --no-progress-seconds 1800

Isolation may inject resource_factory at the external CLI boundary only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

import release_context as rc
import review as review_mod
from isolated_site import open_isolated_stores

PASS_BODY_TEMPLATE = review_mod.PASS_BODY_TEMPLATE


def _sha256_file(path: Path) -> str:
    return rc._sha256_file(path)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def recovery_evidence_dir(ctx: dict[str, Any], explicit: Path | str | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    return Path(ctx['reports']) / 'driver75p-recovery1'


def recovery_reports_dir(ctx: dict[str, Any]) -> Path:
    return Path(ctx['reports']) / 'review-recovery1'


def preflight_recovery_write_gate(ctx: dict[str, Any]) -> dict[str, Any]:
    """Review-equivalent write-before gate that allows STATE.tasks.review already claimed.

    Recovery always targets an existing same task; release_context.validate_context(
    'review') would stop on that claim. Shared frozen-site / manifest / BASE /
    baseline checks are reused via stage 'nodes' plus the same export+baseline
    extras the review preflight runs.
    """
    rc.check_interpreter()
    rc.verify_manifests(ctx)
    rc.validate_context(ctx, 'nodes')
    active = rc.validate_active_config(ctx)
    from verify_export import verify_export_blobs
    blobs = verify_export_blobs(ctx['repo'], ctx['assigned']['base'], ctx['source'])
    if not blobs.get('ok'):
        raise SystemExit(f'STOP: source 逐文件内容与指派 BASE 不符，拒绝写前继续：{blobs}')
    baseline = rc.validate_baseline_evidence(ctx)
    return {
        'stage': 'recover',
        'active_config': active,
        'source_blobs': blobs,
        'baseline_evidence': baseline,
    }


def load_acceptance_state(ctx: dict[str, Any]) -> dict[str, Any]:
    """Read acceptance-site STATE.json (task/budget/paths). Never replaces manager ctx.state."""
    path = Path(ctx['acceptance']) / 'STATE.json'
    if not path.is_file():
        raise SystemExit(f'STOP: acceptance STATE.json missing: {path}')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise SystemExit('STOP: acceptance STATE.json root must be an object')
    return data


def require_manager_gate_state(manager_state: dict[str, Any]) -> None:
    """Shared preflight needs MANAGEMENT-STATE fields, not acceptance STATE."""
    manifests = manager_state.get('tool_manifests')
    if not isinstance(manifests, list) or len(manifests) < 2:
        raise SystemExit(
            'STOP: manager ctx.state.tool_manifests missing；'
            'recover 不得用验收 STATE 替换 manager 上下文供共享门禁'
        )
    if not manager_state.get('live_config_path') or not manager_state.get('live_config_sha256_before'):
        raise SystemExit(
            'STOP: manager ctx.state 缺少 live_config_path/live_config_sha256_before（B09）'
        )


def display_plan(ctx: dict[str, Any]) -> None:
    manager = ctx.get('state') or {}
    try:
        acceptance = load_acceptance_state(ctx)
    except SystemExit:
        acceptance = {}
    print('========================================')
    print('  75V same-task failed-side recovery plan')
    print('========================================')
    print(f'version: {ctx["version"]}')
    print(f'round: {ctx["round"]}')
    print(f'manager.tool_manifests: {len(manager.get("tool_manifests") or [])} paths')
    print(f'manager.live_config_path: {manager.get("live_config_path")}')
    print(f'acceptance.task_id: {acceptance.get("task_id") or (acceptance.get("tasks") or {}).get("review")}')
    print(f'acceptance.database: {acceptance.get("database")}')
    print(f'acceptance.config: {acceptance.get("config")}')
    print(f'acceptance.no_progress_seconds: {acceptance.get("no_progress_seconds")}')
    print(f'recovery evidence: {recovery_evidence_dir(ctx)}')
    print(f'recovery reports: {recovery_reports_dir(ctx)}')
    print('----------------------------------------')
    print('1. preflight_recovery_write_gate on manager ctx.state — zero write on --plan')
    print('2. Require tasks.status=frozen_paused + matching freeze_hash (acceptance STATE)')
    print('3. Require failures == {failed-node}; completed side output matches freeze')
    print('4. AgentPool.edit failed-node no_progress_seconds only (model/entry/effort kept)')
    print('5. freeze-after test_run via trusted json_stdio_factory + bound workspace')
    print('6. EventStore.resume_task(--freeze-hash) then Orchestrator.run_once once')
    print('7. On completed: validate_acceptance_database + review._write_acceptance_artifacts')
    print('   → reports/review-recovery1/11-acceptance.json; STATE.review_evidence_path')
    print('8. Never resubmit task; never re-dispatch completed side; no auto retry loop')
    print('========================================')


def _load_product(ctx: dict[str, Any]) -> dict[str, Any]:
    return review_mod._load_product(ctx)


def _load_pool_stack(
    ctx: dict[str, Any],
    acceptance_state: dict[str, Any],
    *,
    resource_factory: Callable[..., Any] | None,
):
    """Open ReviewStore/AgentPool on acceptance STATE nodes-isolated site."""
    import nodes as nodes_mod
    from xiaoba_review.resource_stdio import json_stdio_factory

    cfg_path = Path(acceptance_state['config'])
    isolated_root = Path(
        acceptance_state.get('root')
        or Path(acceptance_state['database']).resolve().parents[1]
    )
    workspace = Path(
        acceptance_state.get('workspace_root')
        or (isolated_root / 'agent-workspaces')
    )
    outbox = isolated_root / 'outbox'
    prod_nodes = nodes_mod.load_product(ctx)
    cfg, native, review = open_isolated_stores(
        cfg_path, workspace_root=workspace, outbox_root=outbox)
    if resource_factory is not None:
        effective = resource_factory
        injected = True
    else:
        root = review.workspace_root
        if root is None:
            raise SystemExit('STOP: ReviewStore.workspace_root missing before recovery factory bind')
        effective = lambda source, _root=root: json_stdio_factory(source, _root)
        injected = False
    backends = prod_nodes['BackendRegistry'](review, resource_factory=effective)
    pool = prod_nodes['AgentPool'](review, backends)
    return {
        'cfg': cfg,
        'native': native,
        'review': review,
        'pool': pool,
        'identifier': prod_nodes['identifier'],
        'pool_dispatch': prod_nodes['pool_dispatch'],
        'injected': injected,
        'workspace_root': str(review.workspace_root),
    }


@contextmanager
def _dispatch_boundary(*, resource_factory: Callable[..., Any] | None):
    """Patch only the external CLI edge for Orchestrator; validator stays real."""
    with ExitStack() as stack:
        if resource_factory is not None:
            stack.enter_context(patch(
                'xiaoba_agentos.adapters._pool_cli_factory',
                return_value=resource_factory,
            ))
            stack.enter_context(patch(
                'xiaoba_review.resource_stdio.json_stdio_factory',
                resource_factory,
            ))
        yield


def inspect_frozen_task(
    store: Any,
    task_id: str,
    *,
    failed_node: str,
    expected_freeze_hash: str | None,
) -> dict[str, Any]:
    """Read-only frozen-task gate. No writes."""
    task = store.get_task(task_id) or {}
    status = task.get('status')
    if status not in {'frozen_paused', 'resume_ready'}:
        raise SystemExit(
            f'STOP: recovery requires frozen_paused/resume_ready; got status={status!r} task={task_id}'
        )
    freeze_hash = task.get('freeze_hash')
    if not freeze_hash:
        raise SystemExit(f'STOP: task {task_id} missing freeze_hash')
    if expected_freeze_hash and expected_freeze_hash != freeze_hash:
        raise SystemExit('STOP: --freeze-hash does not match task.freeze_hash')
    try:
        frozen = json.loads(task.get('freeze_json') or '{}')
    except json.JSONDecodeError as exc:
        raise SystemExit(f'STOP: invalid freeze_json: {exc}') from exc
    if not isinstance(frozen, dict):
        raise SystemExit('STOP: freeze_json must be an object')

    # Re-verify product integrity helper when available.
    verify = getattr(store, '_verify_frozen_record_integrity', None)
    if callable(verify):
        # Need a Row-like object; re-read via connect for integrity.
        with store.read_only() as conn:
            row = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        try:
            frozen = verify(row)
        except ValueError as exc:
            raise SystemExit(f'STOP: frozen record integrity failed: {exc}') from exc

    failures = [f for f in (frozen.get('failures') or []) if isinstance(f, dict)]
    failed_agents = sorted({str(f.get('agent')) for f in failures if f.get('agent')})
    if failed_agents != [failed_node]:
        raise SystemExit(
            f'STOP: freeze failures must be exactly {{{failed_node}}}; got {failed_agents}'
        )

    completed = [c for c in (frozen.get('completed_runs') or []) if isinstance(c, dict)]
    if not completed:
        raise SystemExit('STOP: freeze record has no completed_runs to reuse')
    runs = {str(r.get('id')): r for r in (store.task_runs(task_id) or [])}
    preserved = []
    for item in completed:
        run_id = str(item.get('run_id') or '')
        agent = str(item.get('agent') or '')
        run = runs.get(run_id)
        if not run or run.get('status') != 'completed':
            raise SystemExit(f'STOP: frozen completed run missing/not completed: {run_id} ({agent})')
        if agent == failed_node:
            raise SystemExit(f'STOP: failed-node {failed_node} must not appear in completed_runs')
        actual = _sha256_text(str(run.get('output') or ''))
        expected = str(item.get('output_sha256') or '')
        if actual != expected:
            raise SystemExit(
                f'STOP: completed side output hash drifted for {agent}: {actual} != {expected}'
            )
        preserved.append({
            'run_id': run_id,
            'agent': agent,
            'output_sha256': actual,
            'status': run.get('status'),
        })
    if any(str(c.get('agent')) == failed_node for c in completed):
        raise SystemExit('STOP: failed-node listed in completed_runs')
    return {
        'task_id': task_id,
        'status': status,
        'freeze_hash': freeze_hash,
        'failed_node': failed_node,
        'failed_agents': failed_agents,
        'completed_preserved': preserved,
        'resume_generation': int(task.get('resume_generation') or 0),
        'frozen_resume_generation': int(frozen.get('resume_generation') or 0),
        'failure_class': frozen.get('failure_class') or task.get('failure_class'),
    }


def verify_budget_against_state(state: dict[str, Any], no_progress_seconds: int) -> int:
    raw = state.get('no_progress_seconds')
    if raw is None:
        raise SystemExit(
            'STOP: STATE.no_progress_seconds missing；恢复预算必须读 manager/STATE，禁止猜测'
        )
    try:
        expected = int(raw)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f'STOP: STATE.no_progress_seconds invalid: {raw!r}') from exc
    if int(no_progress_seconds) != expected:
        raise SystemExit(
            f'STOP: --no-progress-seconds {no_progress_seconds} != STATE.no_progress_seconds {expected}'
        )
    if not 1 <= expected <= 86400:
        raise SystemExit(f'STOP: budget out of product range: {expected}')
    return expected


def _preserve_original_review_hashes(ctx: dict[str, Any]) -> dict[str, Any]:
    original = Path(ctx['reports']) / 'review' / '11-acceptance.json'
    if not original.is_file():
        return {'present': False, 'path': str(original)}
    return {
        'present': True,
        'path': str(original),
        'sha256': _sha256_file(original),
        'bytes': original.stat().st_size,
    }


def edit_failed_node_budget(
    pool: Any,
    *,
    failed_node: str,
    no_progress_seconds: int,
) -> dict[str, Any]:
    with pool.store.transaction() as db:
        source = pool.store.source(db, failed_node)
    node = dict(source.get('node') or {})
    before = {
        'model': node.get('model'),
        'entry': node.get('entry'),
        'reasoning_effort': node.get('reasoning_effort'),
        'agent_type': node.get('agent_type'),
        'no_progress_seconds': node.get('no_progress_seconds'),
        'revision': node.get('revision'),
    }
    edited = pool.edit({
        'node_id': failed_node,
        'expected_revision': int(node['revision']),
        'no_progress_seconds': int(no_progress_seconds),
    })
    after_node = dict(edited.get('node') or {})
    after = {
        'model': after_node.get('model'),
        'entry': after_node.get('entry'),
        'reasoning_effort': after_node.get('reasoning_effort'),
        'agent_type': after_node.get('agent_type'),
        'no_progress_seconds': after_node.get('no_progress_seconds'),
        'revision': after_node.get('revision'),
    }
    for key in ('model', 'entry', 'reasoning_effort', 'agent_type'):
        if before.get(key) != after.get(key):
            raise SystemExit(
                f'STOP: edit must not change {key}: before={before.get(key)!r} after={after.get(key)!r}'
            )
    if after.get('no_progress_seconds') != int(no_progress_seconds):
        raise SystemExit('STOP: edit did not persist no_progress_seconds')
    return {'before': before, 'after': after}


def retest_failed_node(pool: Any, *, failed_node: str, identifier: Callable[[], str]) -> dict[str, Any]:
    with pool.store.transaction() as db:
        source = pool.store.source(db, failed_node)
    revision = int((source.get('node') or {})['revision'])
    result = pool.runtime.test_run(failed_node, revision, identifier())
    if result.get('status') != 'passed':
        raise SystemExit(
            'STOP: freeze-after test_run on failed-node did not pass; '
            f'status={result.get("status")} reason={result.get("reason")} message={result.get("message")}'
        )
    return result


def product_resume(
    store: Any,
    cfg: Any,
    *,
    task_id: str,
    freeze_hash: str,
    pool_dispatch: Any,
) -> dict[str, Any]:
    try:
        return store.resume_task(
            task_id,
            freeze_hash,
            receipt_root=Path(cfg.runtime_dir) / 'receipts',
            enabled_names=[agent.name for agent in cfg.agents if agent.enabled],
            health_max_age_seconds=float(cfg.worker.get('health_max_age_seconds', 86400)),
            retested_nodes=pool_dispatch.retested_nodes(store),
        )
    except (KeyError, ValueError) as exc:
        raise SystemExit(f'STOP: product resume_task refused: {exc}') from exc


def finalize_if_completed(
    ctx: dict[str, Any],
    acceptance_state: dict[str, Any],
    *,
    task_id: str,
    store: Any,
    cfg_path: Path,
    inspection: dict[str, Any],
    isolation: dict[str, Any],
) -> dict[str, Any] | None:
    task = store.get_task(task_id) or {}
    if task.get('status') != 'completed':
        return None
    official = review_mod._official_validate(
        Path(ctx['candidate']), Path(store.path), task_id)
    runs = store.task_runs(task_id) or []
    reports = recovery_reports_dir(ctx)
    reports.mkdir(parents=True, exist_ok=True)
    nodes_meta = {
        'prepared_path': acceptance_state.get('nodes_prepared_path', ''),
        'prepared_sha256': acceptance_state.get('nodes_prepared_sha256', ''),
        'review_node_ids': acceptance_state.get('review_node_ids') or [],
        'isolated_config': str(cfg_path),
        'isolated_database': str(store.path),
        'workspace_root': acceptance_state.get('workspace_root', ''),
    }
    payload = review_mod._write_acceptance_artifacts(
        reports,
        task_id=task_id,
        database=Path(store.path),
        config=cfg_path,
        runs=runs,
        task_row=task,
        official=official,
        nodes_meta=nodes_meta,
        submit_meta={
            'recovered': True,
            'task_id': task_id,
            'freeze_hash': inspection.get('freeze_hash'),
            'no_new_submit': True,
            'original_review_dir_preserved': True,
        },
        material_hashes={},
        isolation=isolation,
    )
    evidence_path = reports / '11-acceptance.json'
    state_path = Path(ctx['acceptance']) / 'STATE.json'
    state = json.loads(state_path.read_text(encoding='utf-8'))
    state['acceptance_passed'] = True
    state['review_evidence_path'] = str(evidence_path.resolve())
    state['task_id'] = task_id
    state.setdefault('tasks', {})['review'] = task_id
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    payload['review_evidence_path'] = str(evidence_path.resolve())
    return payload


def run_recover(
    ctx: dict[str, Any],
    *,
    failed_node: str,
    no_progress_seconds: int,
    allow_real: bool,
    freeze_hash: str | None = None,
    resource_factory: Callable[..., Any] | None = None,
    evidence_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Execute same-task failed-side recovery, or read-only re-entry after claim."""
    if not allow_real:
        raise SystemExit('STOP: --run requires --allow-real (formal recovery is explicit)')
    if not failed_node or not isinstance(failed_node, str):
        raise SystemExit('STOP: --failed-node is required')

    rc.check_interpreter()

    # Manager ctx.state feeds shared preflight (tool_manifests / live_config).
    # Acceptance STATE.json separately feeds task/budget/config/db paths.
    ctx = dict(ctx)
    manager_state = dict(ctx.get('state') or {})
    require_manager_gate_state(manager_state)
    ctx['state'] = manager_state
    acceptance_state = load_acceptance_state(ctx)

    # Budget gate before any marker / model / pool write.
    budget = verify_budget_against_state(acceptance_state, no_progress_seconds)
    task_id = str(
        acceptance_state.get('task_id')
        or (acceptance_state.get('tasks') or {}).get('review')
        or ''
    )
    if not task_id:
        raise SystemExit('STOP: acceptance STATE.task_id / tasks.review missing')
    cfg_path = Path(acceptance_state.get('config') or '')
    database = Path(acceptance_state.get('database') or '')
    if not cfg_path.is_file() or not database.is_file():
        raise SystemExit('STOP: acceptance STATE.config/database must point at nodes-isolated site')

    preflight = preflight_recovery_write_gate(ctx)

    original_review = _preserve_original_review_hashes(ctx)
    evidence = recovery_evidence_dir(ctx, evidence_dir)
    evidence.mkdir(parents=True, exist_ok=True)
    marker = evidence / 'RECOVERY.started'
    log_path = evidence / 'recovery.log'

    prod = _load_product(ctx)
    store = prod['EventStore'](database)

    # Re-entry: claim already taken — only read-only result check / finalize.
    if marker.exists():
        task = store.get_task(task_id) or {}
        inspection = {
            'task_id': task_id,
            'status': task.get('status'),
            'freeze_hash': task.get('freeze_hash'),
            'reentry': True,
        }
        if task.get('status') == 'completed':
            final = finalize_if_completed(
                ctx,
                acceptance_state,
                task_id=task_id,
                store=store,
                cfg_path=cfg_path,
                inspection=inspection,
                isolation={'reentry_readonly': True, 'resource_factory_injected': False},
            )
            report = {
                'ok': True,
                'reentry': True,
                'dispatched': False,
                'task_id': task_id,
                'task_status': 'completed',
                'acceptance': final,
                'original_review': original_review,
                'preflight_stage': preflight.get('stage'),
            }
            _write_json(evidence / 'recovery-result.json', report)
            return report
        raise SystemExit(
            f'STOP: RECOVERY.started already exists and task status={task.get("status")}; '
            'refusing second model dispatch — wait or finalize only when completed'
        )

    inspection = inspect_frozen_task(
        store, task_id, failed_node=failed_node, expected_freeze_hash=freeze_hash)

    with marker.open('x') as handle:
        handle.write(
            json.dumps({
                'task_id': task_id,
                'failed_node': failed_node,
                'freeze_hash': inspection['freeze_hash'],
                'no_progress_seconds': budget,
            }, ensure_ascii=False, indent=2) + '\n'
        )

    stack = _load_pool_stack(ctx, acceptance_state, resource_factory=resource_factory)
    edit_meta = edit_failed_node_budget(
        stack['pool'], failed_node=failed_node, no_progress_seconds=budget)
    _write_json(evidence / '01-edit-budget.json', edit_meta)

    test_meta = retest_failed_node(
        stack['pool'], failed_node=failed_node, identifier=stack['identifier'])
    _write_json(evidence / '02-retest.json', {
        'status': test_meta.get('status'),
        'test_id': test_meta.get('test_id'),
        'spec_digest': test_meta.get('spec_digest'),
        'runner_digest': test_meta.get('runner_digest'),
        'evidence_digest': test_meta.get('evidence_digest'),
        'external_io_note': 'test_run used trusted factory; isolation may mock only CLI boundary',
    })

    resume_meta = product_resume(
        store,
        stack['cfg'],
        task_id=task_id,
        freeze_hash=inspection['freeze_hash'],
        pool_dispatch=stack['pool_dispatch'],
    )
    _write_json(evidence / '03-resume.json', resume_meta)

    completed_before = {
        item['agent']: item['run_id'] for item in inspection['completed_preserved']
    }
    with _dispatch_boundary(resource_factory=resource_factory):
        orch = prod['Orchestrator'](stack['cfg'], store).run_once()
    _write_json(evidence / '04-orchestrator.json', {'result': orch})

    # Guard: completed side run ids must remain the reused originals.
    latest = store.latest_agent_runs(task_id)
    for agent, run_id in completed_before.items():
        current = latest.get(agent) or {}
        if str(current.get('id')) != run_id:
            raise SystemExit(
                f'STOP: completed side {agent} was re-dispatched or replaced; '
                f'expected run {run_id}, got {current.get("id")}'
            )
        if current.get('status') != 'completed':
            raise SystemExit(f'STOP: completed side {agent} no longer completed after recovery')

    task_after = store.get_task(task_id) or {}
    isolation = {
        'resource_factory_injected': resource_factory is not None,
        'default_trusted_factory': resource_factory is None,
        'bound_workspace_root': stack['workspace_root'],
        'edit_budget': edit_meta,
        'completed_side_reused': completed_before,
    }
    final = None
    if task_after.get('status') == 'completed':
        final = finalize_if_completed(
            ctx,
            acceptance_state,
            task_id=task_id,
            store=store,
            cfg_path=cfg_path,
            inspection=inspection,
            isolation=isolation,
        )
    else:
        # Preserve failure detail without auto-retry.
        (evidence / '05-incomplete.txt').write_text(
            f'task_status={task_after.get("status")}\norchestrator={json.dumps(orch, ensure_ascii=False)}\n',
            encoding='utf-8',
        )

    # Confirm original review acceptance bytes untouched.
    if original_review.get('present'):
        path = Path(original_review['path'])
        if _sha256_file(path) != original_review['sha256']:
            raise SystemExit('STOP: original reports/review/11-acceptance.json bytes changed')

    report = {
        'ok': task_after.get('status') == 'completed' and final is not None,
        'reentry': False,
        'dispatched': True,
        'task_id': task_id,
        'task_status': task_after.get('status'),
        'failed_node': failed_node,
        'freeze_hash': inspection['freeze_hash'],
        'budget': budget,
        'inspection': inspection,
        'resume': resume_meta,
        'orchestrator': orch,
        'acceptance': final,
        'original_review': original_review,
        'preflight_stage': preflight.get('stage'),
        'log_path': str(log_path),
    }
    _write_json(evidence / 'recovery-result.json', report)
    log_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if not report['ok']:
        raise SystemExit(
            f'STOP: recovery dispatched once but task status={task_after.get("status")}; '
            'no automatic retry — see recovery evidence'
        )
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog='recover_review.py',
        description='75V same-task failed-side explicit recovery (no new submit)',
    )
    parser.add_argument('--plan', action='store_true', help='Print plan; zero writes')
    parser.add_argument('--run', action='store_true', help='Execute recovery')
    parser.add_argument(
        '--allow-real',
        action='store_true',
        help='Required with --run; acknowledges real resume/Orchestrator path',
    )
    parser.add_argument('--failed-node', type=str, default=None,
                        help='Exact failed pool node id (Qoder)')
    parser.add_argument('--no-progress-seconds', type=int, default=None,
                        help='Must equal STATE.no_progress_seconds (manager budget)')
    parser.add_argument('--freeze-hash', type=str, default=None,
                        help='Optional explicit freeze hash; must match task')
    parser.add_argument('--work-dir', type=str, default=None)
    parser.add_argument('--evidence-dir', type=str, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if not args.plan and not args.run:
        print('Error: --plan or --run required.\n', file=sys.stderr)
        parse_args(['--help'])
        sys.exit(2)
    rc.check_interpreter()
    ctx = rc.load_context(args.work_dir)
    if args.plan:
        display_plan(ctx)
        return
    if args.no_progress_seconds is None or not args.failed_node:
        raise SystemExit('STOP: --run requires --failed-node and --no-progress-seconds')
    result = run_recover(
        ctx,
        failed_node=args.failed_node,
        no_progress_seconds=args.no_progress_seconds,
        allow_real=bool(args.allow_real),
        freeze_hash=args.freeze_hash,
        evidence_dir=args.evidence_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
