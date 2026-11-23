"""Stage 75S nodes: real node preparation and test subject (isolated, read-only).

Reads the manager's two explicitly selected review node IDs from
MANAGEMENT-STATE.review_node_ids (no default, no hardcoded fallback), copies those
two existing enabled configurations read-only out of the live ri_sources table
(preserving model / reasoning_effort / entry), and registers them into a fully
isolated pool built on the REAL AppConfig / EventStore / ReviewStore / AgentPool /
NodeRuntime. The old ``test`` record is never copied as a new test.

A real node-test subject (NodeRuntime.test_run) is provided. For this card's
isolated verification an explicit resource_factory may inject a heavy-CLI stub.
When resource_factory is None (formal / CLI default), prepare_nodes wires the
product-trusted factory the same way as review-ingress/run.py:
``lambda source: json_stdio_factory(source, review.workspace_root)``, binding the
same ReviewStore workspace that writes ``agent_pool.workspace_bound``.
``node_tests_passed`` is written only after two genuine passed tests this round;
a failure preserves the original text and never retries or swaps the model.

Nothing here mutates production, reads authentication material, or writes git.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

import release_context as rc
from isolated_site import build_isolated_config, open_isolated_stores


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()


def load_product(ctx: dict[str, Any]) -> dict[str, Any]:
    """Import the REAL product modules from the (exported or read-only) source."""
    candidate = Path(ctx['candidate'])
    ingress = Path(ctx['source']) / 'development' / 'review-ingress'
    core_src = candidate / 'src'
    if not core_src.is_dir() or not ingress.is_dir():
        raise SystemExit(f'STOP: product source missing for nodes stage: {core_src} / {ingress}')
    for p in (str(core_src), str(ingress)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from xiaoba_review.agent_pool import AgentPool
    from xiaoba_review.backends import BackendRegistry
    from xiaoba_review.store import identifier
    from xiaoba_agentos import pool_dispatch
    return {'AgentPool': AgentPool, 'BackendRegistry': BackendRegistry,
            'identifier': identifier, 'pool_dispatch': pool_dispatch}


def read_production_nodes(ctx: dict[str, Any], node_ids: list[str]) -> list[dict[str, Any]]:
    """Read-only copy of the two enabled live node configs; old test is dropped."""
    live_db = Path(ctx['live']) / '.runtime' / 'agentos.sqlite3'
    if not live_db.is_file():
        raise SystemExit(f'STOP: live pool database missing: {live_db}')
    conn = sqlite3.connect(f'file:{live_db}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    copied: list[dict[str, Any]] = []
    try:
        for nid in node_ids:
            row = conn.execute('SELECT data FROM ri_sources WHERE id=?', (nid,)).fetchone()
            if row is None:
                raise SystemExit(f'STOP: manager review node {nid} not present in live ri_sources')
            source = json.loads(row['data'])
            if source.get('reviewer_id') != nid:
                raise SystemExit(f'STOP: live ri_sources SQL id {nid} != data.reviewer_id {source.get("reviewer_id")}')
            node = source.get('node') or {}
            if node.get('removed') or not node.get('enabled'):
                raise SystemExit(f'STOP: manager review node {nid} is not an enabled live node')
            model = node.get('model')
            if not isinstance(model, str) or not model.strip():
                raise SystemExit(f'STOP: 节点 {nid} 未读到明确模型；停下如实报告，不猜模型、不派活')
            entry = node.get('entry')
            interface = node.get('interface')
            if interface != 'cli' or not isinstance(entry, str) or not Path(entry).is_absolute():
                raise SystemExit(f'STOP: 节点 {nid} 入口不是绝对路径 CLI，无法只读复制')
            config: dict[str, Any] = {
                'node_id': nid,
                'name': node.get('name') or nid,
                'agent': node.get('agent') or node.get('name') or nid,
                'kind': source.get('kind'),
                'interface': interface,
                'entry': entry,
                'model': model,
            }
            for opt in ('agent_type', 'no_progress_seconds', 'reasoning_effort'):
                if node.get(opt) is not None:
                    config[opt] = node[opt]
            copied.append({
                'config': config,
                'production_source_ref': source.get('source_ref'),
                'production_revision': node.get('revision'),
                'had_old_test': isinstance(node.get('test'), dict),
                'copied_config_sha256': _digest(config),
            })
    finally:
        conn.close()
    return copied


def prepare_nodes(
    ctx: dict[str, Any],
    *,
    evidence_dir: Path | str,
    resource_factory: Callable[[dict[str, Any]], Any] | None = None,
    run_tests: bool = True,
) -> dict[str, Any]:
    # Write-before preflight (shared with the CLI normal entry): pinned interpreter,
    # two tool manifests, frozen source per-file content vs assigned BASE,
    # PREP/READY/FREEZE, and this round's explicit active-config hash (B09).
    # Missing/drifted formal inputs reject here with zero marker, zero isolated
    # pool initialization and zero real CLI. Absent formal inputs are a process
    # gap, never a reason to weaken this gate.
    preflight = rc.preflight_write_gate(ctx, 'nodes')
    state = ctx['state']

    node_ids = state.get('review_node_ids')
    if not isinstance(node_ids, list) or len(node_ids) != 2 or len(set(node_ids)) != 2:
        raise SystemExit('STOP: MANAGEMENT-STATE.review_node_ids 必须明确选定两个不同节点；不设默认、不硬编码 fallback')

    # Read-only copy of the two live configs (SQL id must equal data.reviewer_id).
    copied = read_production_nodes(ctx, node_ids)

    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    marker = evidence_dir / 'NODE_TESTS.started'
    if marker.exists():
        raise SystemExit('STOP: node tests already claimed (NODE_TESTS.started exists); do not rerun')

    prod = load_product(ctx)
    isolated_root = evidence_dir / 'nodes-isolated'
    cfg_file = build_isolated_config(Path(ctx['candidate']), isolated_root, ctx['version'])
    # Workspace isolation gate before any pool initialization.
    from workday_isolation import assert_config
    assert_config(cfg_file, isolated_root)
    cfg, native, review = open_isolated_stores(
        cfg_file,
        workspace_root=isolated_root / 'agent-workspaces',
        outbox_root=isolated_root / 'outbox',
    )
    # Formal default: product-trusted factory + bound workspace (run.py shape).
    # Explicit stub injection keeps the prior isolation contract unchanged.
    injected = resource_factory is not None
    if injected:
        effective_factory = resource_factory
    else:
        from xiaoba_review.resource_stdio import json_stdio_factory
        workspace_root = review.workspace_root
        if workspace_root is None:
            raise SystemExit('STOP: ReviewStore.workspace_root missing before trusted factory bind')
        effective_factory = (
            lambda source, _root=workspace_root: json_stdio_factory(source, _root)
        )
    backends = prod['BackendRegistry'](review, resource_factory=effective_factory)
    pool = prod['AgentPool'](review, backends)

    registered: list[dict[str, Any]] = []
    for item in copied:
        source = pool.add(item['config'])
        nid = source['reviewer_id']
        pool.change(nid, source['node']['revision'], 'toggle', enabled=True)
        with review.transaction() as db:
            current = review.source(db, nid)
        registered.append({
            'node_id': nid,
            'name': current['node'].get('name'),
            'agent': current['node'].get('agent'),
            'kind': current['kind'],
            'interface': current['node'].get('interface'),
            'entry': current['node'].get('entry'),
            'model': current['node'].get('model'),
            'agent_type': current['node'].get('agent_type'),
            'reasoning_effort': current['node'].get('reasoning_effort'),
            'no_progress_seconds': current['node'].get('no_progress_seconds'),
            'source_ref': current['source_ref'],
            'revision': current['node']['revision'],
            'copied_config_sha256': item['copied_config_sha256'],
            'production_revision': item['production_revision'],
            'old_test_copied': False,
        })

    # Claim the test attempt only after the isolated pool is registered.
    marker.mkdir(exist_ok=False)

    tests: dict[str, Any] = {}
    readiness: dict[str, Any] = {}
    if run_tests:
        for item in registered:
            nid = item['node_id']
            with review.transaction() as db:
                source = review.source(db, nid)
            test = pool.runtime.test_run(nid, source['node']['revision'], prod['identifier']())
            tests[nid] = test
            item['test_status'] = test.get('status')
            item['test_id'] = test.get('test_id')
            item['spec_digest'] = test.get('spec_digest')
            item['runner_digest'] = test.get('runner_digest')
            item['evidence_digest'] = test.get('evidence_digest')
        with native.read_only() as db:
            for item in registered:
                source = review.source(db, item['node_id'])
                readiness[item['node_id']] = pool.runtime.readiness(db, source)

    node_tests_passed = bool(
        run_tests and len(tests) == 2 and all(t.get('status') == 'passed' for t in tests.values())
    )

    report = {
        'round': ctx['round'],
        'version': ctx['version'],
        'base': ctx['assigned']['base'],
        'review_node_ids': list(node_ids),
        'isolated_root': str(isolated_root),
        'isolated_config': str(cfg_file),
        'isolated_database': str(cfg.database_path),
        'workspace_root': str(isolated_root / 'agent-workspaces'),
        'preflight': preflight,
        'live_config_path': preflight['active_config']['path'],
        'live_config_sha256_before': preflight['active_config']['expected'],
        'live_config_sha256_actual': preflight['active_config']['actual'],
        'nodes': registered,
        'tests': tests,
        'readiness': readiness,
        'node_tests_passed': node_tests_passed,
        'heavy_cli_injected': injected,
        'default_trusted_factory': not injected,
        'bound_workspace_root': str(review.workspace_root) if review.workspace_root else None,
        'no_production_mutation': True,
        'no_auth_material_read': True,
        'no_git_write': True,
        'old_test_copied_as_new': False,
    }
    (evidence_dir / 'nodes-prepared.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    if run_tests and not node_tests_passed:
        raise SystemExit('STOP: 两个节点的真实测试未全部通过；保留原文，不重试、不换模型；见 nodes-prepared.json')

    return report