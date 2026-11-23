"""Stage 75V Review: real submit --material, Orchestrator.run_once, official validator.

Owns only this module (+ optional review helpers / test_75v_review.py). Reuses the
nodes-stage isolated config/database/workspace from nodes-prepared.json. Does not
cross-write prepare/freeze/nodes/release_context/CLI.

Isolation may inject a resource factory at the adapter boundary and may patch
release_binding_status as an explicitly marked isolation boundary. Formal runs
leave both unset. Never fabricates run_identity / agent_runs.entry|model columns,
never stubs validate_acceptance_database, never approves experience for the user.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

import release_context as rc
from workday_isolation import assert_config

PASS_BODY_TEMPLATE = (
    '证据引用：task:{task_id}\n'
    '审查结论：PASS\n'
    '评分：8\n'
    '重复经验：无\n'
    '适用条件：隔离验收夹具\n'
    'FINAL_ACCEPTANCE: PASS\n'
)


def _sha256_file(path: Path) -> str:
    return rc._sha256_file(path)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _load_product(ctx: dict[str, Any]) -> dict[str, Any]:
    candidate = Path(ctx['candidate'])
    ingress = Path(ctx['source']) / 'development' / 'review-ingress'
    core_src = candidate / 'src'
    if not core_src.is_dir() or not ingress.is_dir():
        raise SystemExit(f'STOP: product source missing for review: {core_src} / {ingress}')
    for p in (str(core_src), str(ingress)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from xiaoba_agentos.cli import _submit_task
    from xiaoba_agentos.config import AppConfig
    from xiaoba_agentos.orchestrator import Orchestrator
    from xiaoba_agentos.store import EventStore
    return {
        'AppConfig': AppConfig,
        'EventStore': EventStore,
        'Orchestrator': Orchestrator,
        '_submit_task': _submit_task,
    }


def _load_build_release(candidate: Path):
    """Load build_release from a trusted candidate tree only (fixture assemble).

    Must never be pointed at an unverified release_candidate package agentos.
    """
    path = Path(candidate) / 'scripts' / 'build_release.py'
    if not path.is_file():
        raise SystemExit(f'STOP: build_release.py missing at {path}')
    name = '_75v_build_release_' + hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
    if name not in sys.modules:
        sys.path[:0] = [str(Path(candidate) / 'src'), str(Path(candidate) / 'scripts')]
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise SystemExit(f'STOP: cannot load build_release from {path}')
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _load_switch_release(trusted_candidate: Path):
    """Load switch_release.verify_package from frozen trusted ctx.candidate scripts.

    Never load switch_release (or any binder helper) from the incoming package tree.
    """
    path = Path(trusted_candidate) / 'scripts' / 'switch_release.py'
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f'STOP: trusted switch_release.py missing at {path}')
    name = '_75v_switch_release_' + hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise SystemExit(f'STOP: cannot load switch_release from {path}')
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def resolve_nodes_prepared(
    ctx: dict[str, Any],
    *,
    nodes_evidence_dir: Path | str | None = None,
) -> Path:
    """Locate nodes-prepared.json.

    Explicit nodes_evidence_dir is exclusive (no silent fallback). Otherwise try
    reports/nodes, then the legacy CLI default driver75p-r2/nodes.
    """
    if nodes_evidence_dir is not None:
        path = Path(nodes_evidence_dir) / 'nodes-prepared.json'
        if path.is_file() and not path.is_symlink():
            return path.resolve()
        raise SystemExit(
            f'STOP: nodes-prepared.json missing under explicit nodes_evidence_dir: {path}'
        )
    reports = Path(ctx['reports'])
    candidates: list[Path] = [
        reports / 'nodes' / 'nodes-prepared.json',
        reports / 'driver75p-r2' / 'nodes' / 'nodes-prepared.json',
    ]
    state_hint = (ctx.get('state') or {}).get('nodes_prepared_path')
    if state_hint:
        candidates.insert(0, Path(state_hint))
    for path in candidates:
        if path.is_file() and not path.is_symlink():
            return path.resolve()
    raise SystemExit(
        'STOP: nodes-prepared.json missing; need nodes stage evidence under '
        'reports/nodes/ (or explicit nodes_evidence_dir) before review'
    )


def verify_nodes_prepared(ctx: dict[str, Any], prepared_path: Path) -> dict[str, Any]:
    """Reject before claim when nodes evidence is missing, drifted, or not two passed tests."""
    report = json.loads(prepared_path.read_text(encoding='utf-8'))
    if not isinstance(report, dict):
        raise SystemExit('STOP: nodes-prepared.json root must be an object')
    prepared_sha = _sha256_file(prepared_path)
    if report.get('base') != ctx['assigned']['base']:
        raise SystemExit(
            f'STOP: nodes-prepared base {report.get("base")} != assigned BASE {ctx["assigned"]["base"]}'
        )
    if report.get('round') != ctx['round'] or report.get('version') != ctx['version']:
        raise SystemExit('STOP: nodes-prepared round/version mismatch with manager context')
    manager_ids = ctx['state'].get('review_node_ids')
    if not isinstance(manager_ids, list) or len(manager_ids) != 2 or len(set(manager_ids)) != 2:
        raise SystemExit('STOP: MANAGEMENT-STATE.review_node_ids 必须明确选定两个不同节点；不设默认')
    if list(report.get('review_node_ids') or []) != list(manager_ids):
        raise SystemExit('STOP: nodes-prepared review_node_ids != manager.review_node_ids')
    if report.get('node_tests_passed') is not True:
        raise SystemExit('STOP: nodes-prepared.node_tests_passed is not true; refuse review claim')
    tests = report.get('tests') or {}
    nodes = report.get('nodes') or []
    if len(nodes) != 2 or len(tests) != 2:
        raise SystemExit('STOP: nodes-prepared must carry exactly two nodes and two test records')
    for node in nodes:
        nid = node.get('node_id')
        test = tests.get(nid) if isinstance(tests, dict) else None
        if not isinstance(test, dict) or test.get('status') != 'passed':
            raise SystemExit(f'STOP: node {nid} missing this-round passed test evidence')
        if not node.get('model') or not node.get('entry'):
            raise SystemExit(f'STOP: node {nid} missing preserved model/entry from nodes copy')
        if not node.get('spec_digest') or not node.get('runner_digest') or not node.get('evidence_digest'):
            raise SystemExit(f'STOP: node {nid} missing absolute-entry readiness digests')
    for key in ('isolated_config', 'isolated_database', 'workspace_root', 'isolated_root'):
        path = Path(report.get(key) or '')
        if not path.exists():
            raise SystemExit(f'STOP: nodes isolated site missing {key}: {path}')
    cfg = Path(report['isolated_config'])
    db = Path(report['isolated_database'])
    if cfg.is_symlink() or db.is_symlink():
        raise SystemExit('STOP: nodes isolated config/database must not be symlinks')
    return {
        'report': report,
        'prepared_path': str(prepared_path),
        'prepared_sha256': prepared_sha,
        'isolated_config': str(cfg.resolve()),
        'isolated_database': str(db.resolve()),
        'workspace_root': str(Path(report['workspace_root']).resolve()),
        'isolated_root': str(Path(report['isolated_root']).resolve()),
        'review_node_ids': list(manager_ids),
        'nodes': nodes,
        'tests': tests,
    }


def _path_under_temp(path: Path) -> bool:
    """Product acceptance_mode requires index/runtime under tempfile.gettempdir() or /tmp."""
    resolved = Path(path).resolve()
    temp_roots = {Path(tempfile.gettempdir()).resolve(), Path('/tmp').resolve()}
    return any(resolved == root or root in resolved.parents for root in temp_roots)


def require_candidate_package(state: dict[str, Any]) -> Path:
    """Freeze must leave STATE.candidate_package pointing at a real release_candidate tree."""
    raw = state.get('candidate_package')
    if not raw:
        raise SystemExit(
            'STOP: STATE.candidate_package missing；正式 review 需要 freeze 留下的真实 '
            'release_candidate 包路径（含 RELEASE.json / FILES.sha256 / agentos）。'
            '缺该字段请主力在 FREEZE/STATE 赋值后再派正式阶段。'
        )
    pkg = Path(raw)
    if pkg.is_symlink() or not pkg.is_dir():
        raise SystemExit(f'STOP: STATE.candidate_package unavailable or symlink: {pkg}')
    return pkg.resolve()


def verify_candidate_package_readonly(
    *,
    trusted_candidate: Path,
    candidate_package: Path,
    version: str,
    expected_manifest_sha256: str,
) -> dict[str, Any]:
    """Read-only package gate using trusted switch_release.verify_package.

    Must run before SUBMIT.started, model dispatch, and any package-code import.
    Does not copy, rewrite FILES, or load build_release from the package.
    """
    trusted_candidate = Path(trusted_candidate).resolve()
    pkg = Path(candidate_package).resolve()
    release_json = pkg / 'RELEASE.json'
    files_manifest = pkg / 'FILES.sha256'
    agentos_src = pkg / 'agentos'
    if release_json.is_symlink() or not release_json.is_file():
        raise SystemExit(f'STOP: candidate_package missing RELEASE.json: {release_json}')
    if files_manifest.is_symlink() or not files_manifest.is_file():
        raise SystemExit(f'STOP: candidate_package missing FILES.sha256: {files_manifest}')
    if not (agentos_src / 'SOURCE_MANIFEST.json').is_file():
        raise SystemExit('STOP: candidate_package missing agentos/SOURCE_MANIFEST.json')

    decl = json.loads(release_json.read_text(encoding='utf-8'))
    if decl.get('status') != 'release_candidate':
        raise SystemExit(
            f'STOP: candidate_package RELEASE.json status must be release_candidate, got {decl.get("status")}'
        )
    if str(decl.get('version') or '') != version:
        raise SystemExit(
            f'STOP: candidate_package version {decl.get("version")} != manager version {version}'
        )
    man_sha = _sha256_file(agentos_src / 'SOURCE_MANIFEST.json')
    if man_sha != expected_manifest_sha256:
        raise SystemExit('STOP: candidate_package agentos SOURCE_MANIFEST != assigned manifest')
    if decl.get('agentos_source_manifest_sha256') != man_sha:
        raise SystemExit('STOP: RELEASE.json agentos_source_manifest_sha256 mismatch with packaged agentos')

    sw = _load_switch_release(trusted_candidate)
    try:
        verified_release = sw.verify_package(pkg)
    except Exception as exc:
        # SwitchError and any verify failure must refuse before claim/model/package load.
        raise SystemExit(
            f'STOP: candidate_package failed trusted verify_package before SUBMIT: {exc}'
        ) from exc

    files_sha = _sha256_file(files_manifest)
    return {
        'package': str(pkg),
        'files_manifest_sha256': files_sha,
        'agentos_source_manifest_sha256': man_sha,
        'release': verified_release if isinstance(verified_release, dict) else decl,
        'trusted_switch_release': str(
            (trusted_candidate / 'scripts' / 'switch_release.py').resolve()
        ),
    }


def build_isolation_release_candidate(
    candidate: Path,
    dest: Path,
    version: str,
    *,
    expected_manifest_sha256: str,
) -> Path:
    """Materialize a release_candidate via product assemble leaves (not a hand-forged RELEASE).

    Mirrors build_release._assemble_package agentos projection: present_source_directories
    + SOURCE_ROOT_FILES + copy_review, then product manifest()/sha256 for FILES/RELEASE.
    """
    br = _load_build_release(candidate)
    candidate = Path(candidate)
    dest = Path(dest)
    if dest.exists():
        raise SystemExit(f'STOP: release_candidate dest already exists: {dest}')
    dest.mkdir(parents=True, exist_ok=False)
    agentos = dest / 'agentos'
    agentos.mkdir(parents=True, exist_ok=False)
    for name in br.present_source_directories(candidate):
        br.copy_tree(candidate / name, agentos / name)
    for name in br.SOURCE_ROOT_FILES:
        src = candidate / name
        if src.is_symlink() or not src.is_file():
            raise SystemExit(f'STOP: release source file unavailable: {src}')
        shutil.copy2(src, agentos / name)
    br.copy_review(candidate, agentos)
    man = agentos / 'SOURCE_MANIFEST.json'
    if not man.is_file():
        raise SystemExit(f'STOP: packaged agentos missing SOURCE_MANIFEST.json: {man}')
    man_sha = br.sha256(man)
    if man_sha != expected_manifest_sha256:
        raise SystemExit(
            f'STOP: packaged SOURCE_MANIFEST sha256 {man_sha} != assigned {expected_manifest_sha256}'
        )
    release = {
        'product': '小八 AgentOS',
        'version': version,
        'status': 'release_candidate',
        'verified_date': '1970-01-01',
        'agentos_source_manifest_sha256': man_sha,
        'source_files': len(json.loads(man.read_text(encoding='utf-8')).get('files') or {}),
        'note': '75V isolation release_candidate via product build_release assemble leaves',
    }
    (dest / 'RELEASE.json').write_text(
        json.dumps(release, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    # FILES must be written only after the tree is complete and must not list itself.
    files_path = dest / 'FILES.sha256'
    if files_path.exists():
        files_path.unlink()
    files_path.write_text(br.manifest(dest), encoding='utf-8')
    return dest.resolve()


def bind_release_candidate_package(
    *,
    isolated_root: Path,
    cfg_path: Path,
    version: str,
    candidate_package: Path,
    expected_manifest_sha256: str,
    require_temp_layout: bool,
    trusted_candidate: Path,
    incoming_verify: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind freeze's real release_candidate into the nodes isolated site.

    Uses trusted ctx.candidate switch_release.verify_package for incoming and dest.
    Preserves original FILES.sha256 bytes (never rewrites via package build_release).
    Same-hash complete dest is reused read-only; drifted dest is kept in place and refused.
    """
    isolated_root = Path(isolated_root).resolve()
    cfg_path = Path(cfg_path).resolve()
    trusted_candidate = Path(trusted_candidate).resolve()
    pkg_src = Path(candidate_package).resolve()

    if incoming_verify is None:
        incoming_verify = verify_candidate_package_readonly(
            trusted_candidate=trusted_candidate,
            candidate_package=pkg_src,
            version=version,
            expected_manifest_sha256=expected_manifest_sha256,
        )
    incoming_files_sha = str(incoming_verify['files_manifest_sha256'])
    man_sha = str(incoming_verify['agentos_source_manifest_sha256'])
    incoming_files_bytes = (pkg_src / 'FILES.sha256').read_bytes()

    dest = isolated_root / 'releases' / version
    reused = False
    if dest.resolve() == pkg_src:
        reused = True
    elif dest.exists():
        dest_files = dest / 'FILES.sha256'
        dest_files_sha = (
            _sha256_file(dest_files)
            if dest_files.is_file() and not dest_files.is_symlink()
            else ''
        )
        if dest_files_sha != incoming_files_sha:
            raise SystemExit(
                f'STOP: existing bound package dest drifted (FILES.sha256 hash mismatch); '
                f'kept in place and refused overwrite: {dest}'
            )
        sw = _load_switch_release(trusted_candidate)
        try:
            sw.verify_package(dest)
        except Exception as exc:
            raise SystemExit(
                f'STOP: existing bound package dest failed verify_package; '
                f'kept in place and refused: {dest}: {exc}'
            ) from exc
        reused = True
    else:
        shutil.copytree(pkg_src, dest, symlinks=False)

    dest_files = dest / 'FILES.sha256'
    if dest_files.read_bytes() != incoming_files_bytes:
        raise SystemExit(
            f'STOP: FILES.sha256 bytes must be preserved from incoming package; drifted at {dest_files}'
        )
    files_sha = _sha256_file(dest_files)
    if files_sha != incoming_files_sha:
        raise SystemExit('STOP: dest FILES.sha256 hash != incoming FILES.sha256 hash')

    # Re-verify the actual bound copy with the same trusted verifier (no package-code load).
    sw = _load_switch_release(trusted_candidate)
    try:
        sw.verify_package(dest)
    except Exception as exc:
        raise SystemExit(
            f'STOP: bound package dest failed trusted verify_package: {dest}: {exc}'
        ) from exc

    index_path = isolated_root / 'releases' / 'index.json'
    index_data = {
        'format_version': 2,
        'current_release': version,
        'releases': [{
            'version': version,
            'path': version,
            'status': 'release_candidate',
            'files_manifest_sha256': files_sha,
        }],
    }
    _write_json(index_path, index_data)

    project_dir = dest / 'agentos'
    raw = json.loads(cfg_path.read_text(encoding='utf-8'))
    raw['project_dir'] = str(project_dir)
    raw['runtime_dir'] = str(isolated_root / 'runtime')
    raw.setdefault('release', {})
    raw['release'].update(
        index_path=str(index_path),
        acceptance_mode=True,
        expected_version=version,
    )
    raw.setdefault('memory', {})
    raw['memory']['publish_enabled'] = False
    # Point skill/knowledge fixtures at the packaged agentos (same as freeze).
    skills_root = project_dir / 'tests' / 'fixtures' / 'content' / 'skills'
    if skills_root.is_dir():
        raw.setdefault('skills', {})
        raw['skills'].update(
            root_dir=str(skills_root),
            index_path=str(skills_root / 'index.json'),
        )
    knowledge_index = project_dir / 'tests' / 'fixtures' / 'content' / 'knowledge' / 'index.json'
    if knowledge_index.is_file():
        raw.setdefault('knowledge', {})
        raw['knowledge']['index_path'] = str(knowledge_index)
    cfg_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    cfg_path.chmod(0o600)
    assert_config(cfg_path, isolated_root)

    binding = {
        'candidate_package_source': str(pkg_src),
        'bound_package': str(dest),
        'project_dir': str(project_dir),
        'index_path': str(index_path),
        'files_manifest_sha256': files_sha,
        'incoming_files_manifest_sha256': incoming_files_sha,
        'agentos_source_manifest_sha256': man_sha,
        'files_bytes_preserved': True,
        'dest_reused_readonly': reused,
        'require_temp_layout': require_temp_layout,
        'tmpdir': tempfile.gettempdir(),
        'TMPDIR_env': os.environ.get('TMPDIR'),
        'trusted_switch_release': incoming_verify.get('trusted_switch_release'),
    }

    if require_temp_layout:
        if not _path_under_temp(index_path) or not _path_under_temp(isolated_root / 'runtime'):
            raise SystemExit(
                'STOP: acceptance_mode 要求 index/runtime 落在 tempfile.gettempdir()（或 /tmp）下；'
                f'当前 index={index_path} runtime={isolated_root / "runtime"} gettempdir={tempfile.gettempdir()}。'
                '正式执行请由主力设置受控 TMPDIR，并把 freeze/nodes 场地放在其下（使用现有临时目录契约，不改产品）。'
            )
        # Import after product path is loadable.
        from xiaoba_agentos.config import AppConfig
        from xiaoba_agentos.release import release_binding_status
        status = release_binding_status(AppConfig.load(cfg_path))
        binding['release_binding_status'] = status
        if not status.get('ok'):
            raise SystemExit(
                'STOP: 未 patch 的真实 release_binding_status 拒绝派工前继续: '
                + json.dumps(status.get('problems') or status, ensure_ascii=False)
            )
    else:
        binding['release_binding_status'] = {
            'skipped_check': True,
            'reason': 'narrow isolation case with explicit patch_release_binding=True',
        }
    return binding


def derive_r_vote_fields(
    runs: list[dict[str, Any]],
    official: dict[str, Any],
) -> dict[str, Any]:
    """Top-level R-compatible vote counts derived from real runs + official conclusions."""
    conclusions = official.get('conclusions') or []
    pool_passed = [
        c for c in conclusions
        if isinstance(c, dict) and c.get('source') == 'pool' and c.get('passed') is True
    ]
    pool_failed = [
        c for c in conclusions
        if isinstance(c, dict) and c.get('source') == 'pool' and c.get('passed') is False
    ]
    # Prefer official successful_agents; fall back to completed run agent names that passed.
    distinct = list(official.get('successful_agents') or [])
    if len(distinct) < 2:
        distinct = sorted({
            str(r.get('agent_name'))
            for r in runs
            if r.get('status') == 'completed' and r.get('agent_name')
        })
    return {
        'passed_votes': len(pool_passed),
        'failed_votes': len(pool_failed),
        'distinct_agents': distinct,
    }


def _materials(ctx: dict[str, Any], state: dict[str, Any]) -> list[Path]:
    prompt = Path(state['prompt'])
    diff = Path(state['diff'])
    release_doc = Path(ctx['candidate']) / 'docs' / f'RELEASE_{ctx["version"].upper()}.md'
    source_root = Path(ctx['source'])
    materials = [prompt, diff, release_doc, source_root]
    for path in materials:
        if not path.exists():
            raise SystemExit(f'STOP: review material missing: {path}')
        if path.is_file() and state.get('prompt') == str(prompt) and path == prompt:
            if _sha256_file(path) != state.get('prompt_sha256'):
                raise SystemExit('STOP: prompt hash drifted from STATE.json')
        if path == diff and _sha256_file(path) != state.get('diff_sha256'):
            raise SystemExit('STOP: diff hash drifted from STATE.json')
    return materials


class IsolationCliResource:
    """External-CLI stub for nodes test_run AND review dispatch.

    resource_identity must stay stable across both stages so absolute-entry
    readiness digests written by nodes still match during Orchestrator selection.
    """

    supports_resource_plan = True
    persistent_resource = False
    test_budget_seconds = 60
    pending_action = None

    def __init__(self, source, workspace=None, *, fail_body: str | None = None, **_k):
        self.source = source if isinstance(source, dict) else {}
        self.workspace = workspace
        self.node = self.source.get('node') if isinstance(self.source.get('node'), dict) else {}
        self.kind = self.source.get('kind')
        self.source_ref = self.source.get('source_ref')
        self._fail_body = fail_body

    def resource_identity(self):
        return {
            'protocol': 'cli-worklog-v1',
            'agent_type': self.node.get('agent_type') or 'other',
            'implementation_sha256': '0' * 64,
            'workspace': 'isolated-stub',
        }

    def test_resource(self, plan, challenge):
        return {
            'provider': 'stub_provider',
            'route_source': 'native_cli_per_step',
            'model': plan.get('model'),
            'output': challenge,
            'execution_mode': 'native_cli_per_step',
        }

    def run_task(self, task_id, step_code, instruction, materials, attempt=0):
        body = self._fail_body or PASS_BODY_TEMPLATE.format(task_id=task_id)
        return {
            'status': 'completed',
            'body': body,
            'execution_evidence': {
                'provider': 'fake',
                'model': self.node.get('model'),
                'route_source': 'test',
            },
        }

    def retire(self):
        pass

    def close(self):
        pass


def default_pass_resource_factory(
    *,
    fail_body_by_node: dict[str, str] | None = None,
) -> Callable[[Any, Any], Any]:
    """Isolation-only external CLI stub. Preserves node model; does not bypass the official validator."""

    def factory(source, workspace=None, **kwargs):
        nid = str((source or {}).get('reviewer_id') or '')
        fail_body = (fail_body_by_node or {}).get(nid)
        return IsolationCliResource(source, workspace, fail_body=fail_body, **kwargs)

    return factory


@contextmanager
def _isolation_dispatch_boundary(
    *,
    resource_factory: Callable[..., Any] | None,
    patch_release_binding: bool,
):
    """Patch only the external CLI / release-binding edges. Orchestrator + validator stay real."""
    with ExitStack() as stack:
        if patch_release_binding:
            stack.enter_context(patch(
                'xiaoba_agentos.orchestrator.release_binding_status',
                return_value={
                    'ok': True,
                    'isolation_boundary': True,
                    'reason': '75V isolation: release_binding_status patched; source manifest still real',
                },
            ))
        if resource_factory is not None:
            stack.enter_context(patch(
                'xiaoba_agentos.adapters._pool_cli_factory',
                return_value=resource_factory,
            ))
            # Absolute-entry readiness imports json_stdio_factory directly; keep the
            # same stub identity that nodes used when writing this-round test digests.
            stack.enter_context(patch(
                'xiaoba_review.resource_stdio.json_stdio_factory',
                resource_factory,
            ))
        yield


def _collect_receipt_hashes(workspace_root: Path, task_id: str) -> dict[str, Any]:
    receipts_dir = Path(workspace_root).parent / 'runtime' / 'receipts'
    # Product ExecutionReceiptStore writes under runtime; also scan workspace receipts.
    found: list[dict[str, str]] = []
    for base in (receipts_dir, Path(workspace_root)):
        if not base.is_dir():
            continue
        for path in sorted(base.rglob('*.json')):
            try:
                text = path.read_text(encoding='utf-8')
            except OSError:
                continue
            if task_id in text or task_id in path.name:
                found.append({'path': str(path), 'sha256': _sha256_file(path)})
    return {'count': len(found), 'items': found[:20]}


def _official_validate(candidate: Path, database: Path, task_id: str) -> dict[str, Any]:
    build_release = _load_build_release(candidate)
    return build_release.validate_acceptance_database(Path(database), task_id, Path(candidate))


def _write_acceptance_artifacts(
    review_reports: Path,
    *,
    task_id: str,
    database: Path,
    config: Path,
    runs: list[dict[str, Any]],
    task_row: dict[str, Any],
    official: dict[str, Any],
    nodes_meta: dict[str, Any],
    submit_meta: dict[str, Any],
    material_hashes: dict[str, str],
    isolation: dict[str, Any],
) -> dict[str, Any]:
    for run in runs:
        agent = str(run.get('agent_name') or 'unknown')
        (review_reports / f'acceptance-{agent}.txt').write_text(
            str(run.get('output') or ''), encoding='utf-8')
    vote_fields = derive_r_vote_fields(runs, official)
    payload = {
        'ok': True,
        'database': str(database),
        'config': str(config),
        'task_id': task_id,
        'task_status': task_row.get('status'),
        # R verify_two_votes reads these top-level fields (not nested under official).
        'passed_votes': vote_fields['passed_votes'],
        'failed_votes': vote_fields['failed_votes'],
        'distinct_agents': vote_fields['distinct_agents'],
        'runs': [
            {
                'id': r.get('id'),
                'agent_name': r.get('agent_name'),
                'status': r.get('status'),
                'output_sha256': _sha256_text(str(r.get('output') or '')),
                'output_path': str(review_reports / f'acceptance-{r.get("agent_name") or "unknown"}.txt'),
            }
            for r in runs
        ],
        'official': {
            'task_id': official.get('task_id'),
            'successful_agents': official.get('successful_agents'),
            'minimum_successful_agents': official.get('minimum_successful_agents'),
            'candidate_status': official.get('candidate_status'),
            'source_manifest_sha256': official.get('source_manifest_sha256'),
            'frozen_prompt_sha256': official.get('frozen_prompt_sha256'),
            'conclusions': official.get('conclusions'),
            'pool_nodes': official.get('pool_nodes'),
            'execution': official.get('execution'),
            'database_record_sha256': official.get('database_record_sha256'),
        },
        'nodes_prepared': {
            'path': nodes_meta.get('prepared_path', ''),
            'sha256': nodes_meta.get('prepared_sha256', ''),
            'review_node_ids': nodes_meta.get('review_node_ids') or [],
            'isolated_config': nodes_meta.get('isolated_config', ''),
            'isolated_database': nodes_meta.get('isolated_database', ''),
            'workspace_root': nodes_meta.get('workspace_root', ''),
        },
        'submit': submit_meta,
        'material_hashes': material_hashes,
        'isolation': isolation,
        'resubmission': False,
        'retry': False,
        'experience_approved_by_tool': False,
    }
    _write_json(review_reports / '11-acceptance.json', payload)
    return payload


def run_review(
    ctx: dict[str, Any],
    *,
    nodes_evidence_dir: Path | str | None = None,
    resource_factory: Callable[..., Any] | None = None,
    patch_release_binding: bool | None = None,
    fail_body_by_node: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Execute the review stage.

    Formal default: bind STATE.candidate_package into the nodes site and call the
    real release_binding_status (no patch). resource_factory may stub only the
    external model CLI. patch_release_binding=True is a narrow isolation contract
    case and must not be presented as the formal entry.
    """
    rc.check_interpreter()

    acceptance = Path(ctx['acceptance'])
    candidate = Path(ctx['candidate'])
    reports = Path(ctx['reports'])
    state_path = acceptance / 'STATE.json'
    freeze_path = acceptance / 'FREEZE.json'
    if not state_path.is_file() or not freeze_path.is_file():
        raise SystemExit('STOP: STATE.json or FREEZE.json missing before review')

    state = json.loads(state_path.read_text(encoding='utf-8'))
    if _sha256_file(freeze_path) != state.get('freeze_sha256'):
        raise SystemExit('STOP: FREEZE.json hash mismatch with STATE.json')

    existing_task = None
    tasks = state.get('tasks')
    if isinstance(tasks, dict) and tasks.get('review'):
        existing_task = str(tasks['review'])

    review_reports = reports / 'review'
    review_reports.mkdir(parents=True, exist_ok=True)

    # Resume: already claimed — never resubmit; only wait/read-only validate.
    if existing_task:
        database = Path(state.get('database') or '')
        config = Path(state.get('config') or '')
        if not database.is_file() or not config.is_file():
            raise SystemExit('STOP: resume requires STATE.database/config from nodes site')
        prod = _load_product(ctx)
        store = prod['EventStore'](database)
        task_row = store.get_task(existing_task) or {}
        if task_row.get('status') != 'completed':
            raise SystemExit(
                f'STOP: existing review task {existing_task} status={task_row.get("status")}; '
                'waiting/read-only only — never resubmit'
            )
        runs = store.task_runs(existing_task) or []
        try:
            official = _official_validate(candidate, database, existing_task)
        except Exception as exc:
            raise SystemExit(f'STOP: official validate_acceptance_database failed on resume: {exc}') from exc
        report = _write_acceptance_artifacts(
            review_reports,
            task_id=existing_task,
            database=database,
            config=config,
            runs=runs,
            task_row=task_row,
            official=official,
            nodes_meta={
                'prepared_path': state.get('nodes_prepared_path', ''),
                'prepared_sha256': state.get('nodes_prepared_sha256', ''),
                'review_node_ids': state.get('review_node_ids') or [],
                'isolated_config': str(config),
                'isolated_database': str(database),
                'workspace_root': state.get('workspace_root', ''),
            },
            submit_meta={'resumed': True, 'task_id': existing_task, 'stdout_path': str(review_reports / '11-submit.log')},
            material_hashes={},
            isolation={'resumed': True},
        )
        state['acceptance_passed'] = True
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return report

    # New claim: write-before preflight, then nodes evidence, then single submit.
    preflight = rc.preflight_write_gate(ctx, 'review')
    prepared_path = resolve_nodes_prepared(ctx, nodes_evidence_dir=nodes_evidence_dir)
    nodes_meta = verify_nodes_prepared(ctx, prepared_path)

    prompt_file = Path(state.get('prompt') or '')
    diff_file = Path(state.get('diff') or '')
    if not prompt_file.is_file():
        raise SystemExit(f'STOP: review material missing: {prompt_file}')
    if not diff_file.is_file():
        raise SystemExit(f'STOP: review material missing: {diff_file}')
    if _sha256_file(prompt_file) != state.get('prompt_sha256'):
        raise SystemExit('STOP: prompt file hash drifted from STATE.json')
    if _sha256_file(diff_file) != state.get('diff_sha256'):
        raise SystemExit('STOP: diff file hash drifted from STATE.json')
    materials = _materials(ctx, state)
    material_hashes = {str(p): (_sha256_file(p) if p.is_file() else 'DIR') for p in materials}

    # Formal default does not patch release_binding; only an explicit True opts into the narrow case.
    if patch_release_binding is None:
        patch_release_binding = False
    require_temp = not bool(patch_release_binding)

    pkg = require_candidate_package(state)
    # Bad packages must be refused before SUBMIT claim, model dispatch, or package-code load.
    incoming_verify = verify_candidate_package_readonly(
        trusted_candidate=candidate,
        candidate_package=pkg,
        version=str(ctx['version']),
        expected_manifest_sha256=ctx['assigned']['source_manifest_sha256'],
    )
    _write_json(review_reports / '11-package-verify.json', incoming_verify)

    submit_started = acceptance / 'SUBMIT.started'
    if submit_started.exists():
        raise SystemExit('STOP: SUBMIT.started already exists; only one submission attempt allowed')
    with submit_started.open('x') as marker:
        marker.write('One submission attempt only. Do not remove.\n')

    cfg_path = Path(nodes_meta['isolated_config'])
    # Product modules must be importable before release_binding_status check inside bind.
    prod = _load_product(ctx)
    package_bind = bind_release_candidate_package(
        isolated_root=Path(nodes_meta['isolated_root']),
        cfg_path=cfg_path,
        version=str(ctx['version']),
        candidate_package=pkg,
        expected_manifest_sha256=ctx['assigned']['source_manifest_sha256'],
        require_temp_layout=require_temp,
        trusted_candidate=candidate,
        incoming_verify=incoming_verify,
    )
    _write_json(review_reports / '11-release-binding.json', package_bind)

    cfg = prod['AppConfig'].load(cfg_path)
    store = prod['EventStore'](cfg.database_path)
    # Nodes already initialized the schema and ri_sources; do not re-initialize emptily.
    prompt_text = prompt_file.read_text(encoding='utf-8')

    pin_bin = str(rc.PIN_PATH if rc.PIN_PATH.is_file() else sys.executable)
    cli_argv = [
        pin_bin, '-B', '-m', 'xiaoba_agentos.cli',
        '--config', str(cfg_path),
        'submit', prompt_text,
        '--memory-kind', 'audit',
        '--release-acceptance',
        '--no-work-task',
    ]
    for m in materials:
        cli_argv.extend(['--material', str(m)])
    _write_json(review_reports / '11-submit-command.json', {
        'argv': cli_argv,
        'cwd': str(candidate),
        'note': 'Formal CLI equivalent; isolation executes the same _submit_task product path in-process',
        'config': str(cfg_path),
        'database': str(cfg.database_path),
    })

    try:
        task_id = prod['_submit_task'](
            cfg,
            store,
            prompt_text,
            experience_kind='audit',
            release_acceptance=True,
            no_work_task=True,
            material_files=tuple(str(m) for m in materials),
            needs_review=True,
        )
    except Exception as exc:
        (review_reports / '11-submit.log').write_text(f'SUBMIT_FAILED: {exc}\n', encoding='utf-8')
        raise SystemExit(f'STOP: product submit failed; log preserved: {exc}') from exc

    submit_stdout = json.dumps({'task_id': task_id, 'status': 'pending'}, ensure_ascii=False, indent=2) + '\n'
    (review_reports / '11-submit.log').write_text(submit_stdout, encoding='utf-8')

    # Bind STATE to the nodes site (not freeze's earlier empty site).
    state['tasks'] = {'review': task_id}
    state['task_id'] = task_id
    state['config'] = str(cfg_path.resolve())
    state['database'] = str(Path(cfg.database_path).resolve())
    state['workspace_root'] = nodes_meta['workspace_root']
    state['root'] = nodes_meta['isolated_root']
    state['review_node_ids'] = list(nodes_meta['review_node_ids'])
    state['nodes_prepared_path'] = nodes_meta['prepared_path']
    state['nodes_prepared_sha256'] = nodes_meta['prepared_sha256']
    state['candidate_package'] = str(pkg)
    state['acceptance_passed'] = False
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    effective_factory = resource_factory
    if fail_body_by_node:
        if resource_factory is None:
            effective_factory = default_pass_resource_factory(fail_body_by_node=fail_body_by_node)
        else:
            inner = resource_factory

            def _wrapped(source, workspace=None, **kwargs):
                resource = inner(source, workspace, **kwargs)
                nid = str((source or {}).get('reviewer_id') or '')
                if nid in fail_body_by_node and hasattr(resource, 'run_task'):
                    original = resource.run_task

                    def run_task(task_id, step_code, instruction, materials, attempt=0):
                        result = original(task_id, step_code, instruction, materials, attempt=attempt)
                        result = dict(result or {})
                        result['body'] = fail_body_by_node[nid]
                        return result

                    resource.run_task = run_task  # type: ignore[method-assign]
                return resource

            effective_factory = _wrapped

    isolation_active = effective_factory is not None

    with _isolation_dispatch_boundary(
        resource_factory=effective_factory,
        patch_release_binding=bool(patch_release_binding),
    ):
        orch_result = prod['Orchestrator'](cfg, store).run_once()

    _write_json(review_reports / '11-orchestrator.json', {
        'result': orch_result,
        'isolation_boundary': {
            'resource_factory_injected': isolation_active,
            'release_binding_patched': bool(patch_release_binding),
            'note': 'Only external CLI / release_binding edges; Orchestrator and validator are real',
        },
    })

    task_row = store.get_task(task_id) or {}
    runs = store.task_runs(task_id) or []
    receipt_index = _collect_receipt_hashes(Path(nodes_meta['workspace_root']), task_id)
    _write_json(review_reports / '11-receipts-index.json', receipt_index)

    if task_row.get('status') != 'completed':
        for run in runs:
            agent = str(run.get('agent_name') or 'unknown')
            (review_reports / f'acceptance-{agent}.txt').write_text(
                str(run.get('output') or ''), encoding='utf-8')
        fail_report = {
            'ok': False,
            'task_id': task_id,
            'task_status': task_row.get('status'),
            'orchestrator': orch_result,
            'runs_preserved': len(runs),
            'gate_error': f'review task did not complete: status={task_row.get("status")}',
        }
        _write_json(review_reports / '11-acceptance.json', fail_report)
        raise SystemExit(f'STOP: review task incomplete; outputs preserved: {fail_report["gate_error"]}')

    try:
        official = _official_validate(candidate, Path(cfg.database_path), task_id)
    except Exception as exc:
        for run in runs:
            agent = str(run.get('agent_name') or 'unknown')
            (review_reports / f'acceptance-{agent}.txt').write_text(
                str(run.get('output') or ''), encoding='utf-8')
        fail_report = {
            'ok': False,
            'task_id': task_id,
            'database': str(cfg.database_path),
            'official_error': str(exc),
            'orchestrator': orch_result,
            'preflight_stage': preflight.get('stage'),
        }
        _write_json(review_reports / '11-acceptance.json', fail_report)
        raise SystemExit(f'STOP: official validate_acceptance_database rejected: {exc}') from exc

    conclusions = official.get('conclusions') or []
    passed = [c for c in conclusions if c.get('passed') is True and c.get('source') == 'pool']
    if len(passed) < 2:
        raise SystemExit(
            f'STOP: official validator returned fewer than 2 passed pool conclusions: {len(passed)}'
        )

    report = _write_acceptance_artifacts(
        review_reports,
        task_id=task_id,
        database=Path(cfg.database_path),
        config=cfg_path,
        runs=runs,
        task_row=task_row,
        official=official,
        nodes_meta=nodes_meta,
        submit_meta={
            'task_id': task_id,
            'stdout': submit_stdout,
            'stdout_path': str(review_reports / '11-submit.log'),
            'stdout_sha256': _sha256_text(submit_stdout),
            'command_path': str(review_reports / '11-submit-command.json'),
            'product_path': 'xiaoba_agentos.cli._submit_task',
        },
        material_hashes=material_hashes,
        isolation={
            'resource_factory_injected': isolation_active,
            'release_binding_patched': bool(patch_release_binding),
            'package_bind': package_bind,
        },
    )

    state['acceptance_passed'] = True
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report
