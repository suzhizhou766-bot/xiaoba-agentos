"""Core switch orchestration for stage75r: read-only gates, ONE single-use marker,
then real plan/preflight/apply/readback subprocesses against the accepted package.

Ported by purpose from the accepted v1.6.14 run_switch.py (42ca2bad…). The actual
switch logic lives in the package's scripts/switch_release.py (plan/preflight/apply/
readback/recover/resume/abandon); this module never re-implements it, only drives it
through real subprocesses and verifies every result. Nothing release-specific is
hardcoded: package/BASE/version/attempt all arrive via ctx + the ONE attempt record.

Boundary required by the card:
  * core apply runs ONCE, only after every read-only gate passes (STEP4-5 evidence,
    package hash, source/tool hashes, two independent votes, read-only plan,
    migration declaration, authorized-post-file syntax). A syntax failure refuses BEFORE
    the marker is claimed and BEFORE apply.
  * the core SWITCH marker is separate from the recoverable post-switch boundary;
    recognizing an already-switched state never re-runs core apply (see recovery.py).
  * a failed apply preserves the original output verbatim; never resent.
"""
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

from common import StopError, atomic_dump, clean_env, read_json, run_cli_json, sha, sha_bytes, PIN
from context_adapter import (ASSISTANT_ROOT, attempt_paths, bound_helpers, live_monitor_dir,
                             monitor_source_dir, post_file_declaration, post_files,
                             review_evidence, run_migration_check, verify_manifests)
from monitor_contract import sync_precheck
from post_lib import resolved_taskcards, syntax_check

FOURTEEN_KEYS = ('card43', 'card44', 'card45', 'card46', 'card47', 'card51', 'card52',
                 'card52b', 'card57', 'void', 'ui_install', 'workday', 'reasoning_effort',
                 'event_chain')


def live_db_path(ctx):
    return Path(ctx['live']) / '.runtime/agentos.sqlite3'


def _candidate_module(candidate):
    """Load the FROZEN candidate's own scripts/build_release.py once (no copy, no
    .pyc under -B), with the candidate src/scripts on sys.path so its official
    xiaoba_agentos imports resolve. Both the verdict predicate and the official
    acceptance-database validator come from this same real product module."""
    root = Path(candidate).resolve()
    path = root / 'scripts/build_release.py'
    if path.is_symlink() or not path.is_file():
        raise StopError('STOP: candidate build_release.py missing: %s' % path)
    name = '_stage75r_build_release_' + hashlib.sha256(str(path).encode()).hexdigest()[:16]
    if name not in sys.modules:
        sys.path[:0] = [str(root / 'src'), str(root / 'scripts')]
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = previous
    return sys.modules[name]


def verdict_passes(text, candidate):
    """Re-verify one review conclusion with the CANDIDATE's own build_release
    predicate (same rule as 75P/75Q); never rewrite or fabricate acceptance."""
    return bool(_candidate_module(candidate)._passed_verdict(str(text or '')))


def validate_acceptance_db(ctx, state):
    """Authoritative two-vote gate. Reuse the frozen candidate's OFFICIAL
    build_release.validate_acceptance_database against the real acceptance SQLite
    database that 75S records in STATE.database / STATE.task_id. The official
    validator reads immutable task facts and enforces task completion, the
    production loop, frozen-source binding, >=2 independent qualified votes and
    full material binding; a rewritable summary alone is never trusted. Missing
    database / task / snapshot, a single vote, the same entry+model under two
    names, FAIL or non-answer all refuse HERE, before any SWITCH marker or apply.
    The lightweight independence re-check below uses ONLY the official returned
    conclusions / pool_nodes / execution; it fabricates no entry/model or identity.
    """
    database = state.get('database')
    task_id = state.get('task_id')
    if not database or not task_id:
        raise StopError('STOP: STATE has no acceptance database/task_id; real votes cannot be verified')
    module = _candidate_module(ctx['candidate'])
    source_root = Path(ctx['candidate'])
    try:
        result = module.validate_acceptance_database(Path(database), str(task_id), source_root)
    except RuntimeError as exc:
        raise StopError('STOP: official acceptance database gate failed: %s' % exc) from exc
    if not isinstance(result, dict):
        raise StopError('STOP: official acceptance validator returned no record')
    if result.get('task_id') != str(task_id):
        raise StopError('STOP: acceptance database task_id differs from STATE')
    assigned_manifest = ctx['assigned']['source_manifest_sha256']
    if result.get('source_manifest_sha256') != assigned_manifest:
        raise StopError('STOP: acceptance frozen source differs from BASE_ASSIGNED')
    prompt = result.get('frozen_prompt_sha256')
    record_sha = result.get('database_record_sha256')
    if not prompt or not record_sha:
        raise StopError('STOP: acceptance material binding (frozen prompt / database record) missing')
    conclusions = result.get('conclusions') or []
    passed = [c for c in conclusions if isinstance(c, dict) and c.get('passed') is True]
    minimum = int(result.get('minimum_successful_agents', 0))
    if len(passed) < 2 or len(passed) < minimum:
        raise StopError('STOP: fewer than two qualified acceptance conclusions')
    for c in passed:
        if c.get('prompt_sha256') != prompt or c.get('manifest_sha256') != result.get('source_manifest_sha256'):
            raise StopError('STOP: acceptance conclusion not bound to the frozen prompt/manifest')
    by_id = {str(n.get('node_id')): n for n in (result.get('pool_nodes') or []) if isinstance(n, dict)}
    execution = result.get('execution') or {}
    authors = {str(x) for x in execution.get('author_ids') or []}
    executors = {str(x) for x in execution.get('executor_ids') or []}
    keys, pool_votes, host_votes = [], 0, 0
    for c in passed:
        if c.get('source') == 'host':
            host_votes += 1
            host_id = str(c.get('host_id') or '')
            if host_id in authors or host_id in executors:
                raise StopError('STOP: host vote is an author/executor of this release')
            keys.append(('host:' + host_id, str(c.get('model') or '')))
            continue
        node_id = str(c.get('node_id') or '')
        node = by_id.get(node_id)
        if node is None or not node.get('enabled') or not node.get('test_passed'):
            raise StopError('STOP: acceptance pool node not enabled/tested: %s' % node_id)
        if node_id in authors or node_id in executors:
            raise StopError('STOP: acceptance pool node worked on this release')
        pool_votes += 1
        keys.append(tuple(node.get('independence') or ()))
    if host_votes > 1 or pool_votes < 1:
        raise StopError('STOP: acceptance needs >=1 pool vote and <=1 host vote')
    if len(set(keys)) < len(keys):
        raise StopError('STOP: acceptance votes are not entry/model independent')
    if len(result.get('successful_agents') or []) < 2:
        raise StopError('STOP: acceptance database has fewer than two successful agents')
    return {'task_id': str(task_id), 'successful_agents': result.get('successful_agents'),
            'minimum_successful_agents': minimum, 'qualified_conclusions': len(passed),
            'pool_votes': pool_votes, 'host_votes': host_votes,
            'independence_keys': [list(k) for k in keys],
            'source_manifest_sha256': result.get('source_manifest_sha256'),
            'frozen_prompt_sha256': prompt, 'database_record_sha256': record_sha,
            'candidate_status': result.get('candidate_status')}


def verify_step4_result(ctx):
    paths = attempt_paths(ctx)
    result_path = paths['reports_step4_dir'] / 'STEP4-5-result.json'
    result = read_json(result_path)
    if result.get('ok') is not True:
        raise StopError('STOP: STEP4-5 result not ok: %s' % result_path)
    if result.get('production_switch') is not False:
        raise StopError('STOP: STEP4-5 evidence claims a production switch')
    if result.get('step4_passed') is not True or result.get('step5_passed') is not True:
        raise StopError('STOP: STEP4-5 step4/step5 not both passed')
    items = result.get('fourteen_items') or {}
    if set(items) != set(FOURTEEN_KEYS) or len(items) != 14 or not all(items.values()):
        raise StopError('STOP: fourteen items incomplete or failed: %s' % sorted(items))
    if not (result.get('cleanup') or {}).get('ok'):
        raise StopError('STOP: STEP4-5 cleanup not ok')
    if not (result.get('real_workspace_guard') or {}).get('ok'):
        raise StopError('STOP: STEP4-5 real workspace guard not ok')
    package = result.get('package') or {}
    if not package.get('files_sha256'):
        raise StopError('STOP: STEP4-5 result missing package.files_sha256')
    if Path(package.get('path', '')) != paths['package_dir']:
        raise StopError('STOP: STEP4-5 package path differs from the ONE attempt record')
    return result


def verify_package(ctx, step4):
    paths = attempt_paths(ctx)
    package = paths['package_dir']
    files_manifest = package / 'FILES.sha256'
    if files_manifest.is_symlink() or not files_manifest.is_file():
        raise StopError('STOP: package FILES.sha256 missing: %s' % files_manifest)
    if sha(files_manifest) != step4['package']['files_sha256']:
        raise StopError('STOP: package FILES.sha256 hash differs from STEP4-5 evidence')
    release = read_json(package / 'RELEASE.json')
    if release.get('version') != ctx['version']:
        raise StopError('STOP: package RELEASE version != manager version')
    if release.get('status') != 'verified_immutable_release':
        raise StopError('STOP: package is not an accepted immutable release')
    if release.get('final_publish_requires_human') is not True:
        raise StopError('STOP: package does not require human final publish')
    if (release.get('tests') or {}).get('result') != 'passed':
        raise StopError('STOP: package lacks a passed test record')
    source_manifest = package / 'agentos/SOURCE_MANIFEST.json'
    if sha(source_manifest) != ctx['assigned']['source_manifest_sha256']:
        raise StopError('STOP: package source manifest differs from BASE_ASSIGNED')
    if release.get('agentos_source_manifest_sha256') != sha(source_manifest):
        raise StopError('STOP: package source binding mismatch')
    return release


def _switch_release_module(candidate):
    """Load the FROZEN candidate's own scripts/switch_release.py once (no copy, no
    .pyc under -B). Its files/safe/relative/require engine is the trusted per-file
    package verifier; the package never proves itself with its own bytes."""
    root = Path(candidate).resolve()
    path = root / 'scripts/switch_release.py'
    if path.is_symlink() or not path.is_file():
        raise StopError('STOP: candidate switch_release.py missing: %s' % path)
    name = '_stage75r_switch_release_' + hashlib.sha256(str(path).encode()).hexdigest()[:16]
    if name not in sys.modules:
        sys.path[:0] = [str(root / 'src'), str(root / 'scripts')]
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = previous
    return sys.modules[name]


def verify_package_integrity(ctx):
    """Per-file package integrity using the FROZEN candidate's own switch_release
    engine (files/safe/relative/require) and its official manifest-set comparison:
    every FILES.sha256 entry must equal the actual file bytes, the listed set must
    equal the actual file set (no missing / unlisted), and no path may be a symlink.
    This catches 'package script bytes changed while FILES.sha256 unchanged' BEFORE
    any SWITCH marker or apply. Full source-tree binding is covered separately by the
    SOURCE_MANIFEST hash gate and verify_source_tool_hashes; this gate is the per-file
    integrity axis the card requires, reused from the trusted verifier (not the package).
    """
    package = attempt_paths(ctx)['package_dir']
    sw = _switch_release_module(ctx['candidate'])
    manifest_path = package / 'FILES.sha256'
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise StopError('STOP: package FILES.sha256 missing: %s' % manifest_path)
    listed = {}
    try:
        for line in manifest_path.read_text(encoding='utf-8').splitlines():
            digest, sep, rel = line.partition('  ./')
            sw.require(bool(sep) and re.fullmatch('[0-9a-f]{64}', digest), 'invalid package manifest line')
            sw.relative(rel)
            sw.require(rel != 'FILES.sha256' and rel not in listed, 'duplicate or self manifest entry')
            listed[rel] = digest
        actual = sw.files(package)
        actual.pop('FILES.sha256', None)
        sw.require(actual == listed, 'package files/hash mismatch')
    except sw.SwitchError as exc:
        raise StopError('STOP: package per-file integrity failed: %s' % exc) from exc
    return {'ok': True, 'package_files_verified': len(listed),
            'files_manifest_sha256': sha(manifest_path)}


def validate_baseline(ctx):
    """Read-only shared 75S baseline gate before any switch write: four log sets +
    source/BASE/runner-hash/freeze-chain (validate_baseline_evidence). Never modifies
    the shared 75P module; its SystemExit refusal is surfaced as a StopError. Core-only:
    post/recover recognize an already-succeeded core and are NOT blocked by pre-switch
    live old hashes."""
    from context_adapter import validate_baseline_evidence
    try:
        return validate_baseline_evidence(ctx, require_freeze=True)
    except SystemExit as exc:
        raise StopError('STOP: baseline evidence gate failed: %s' % exc) from exc


def _ui_install_module(candidate):
    """Load the FROZEN candidate's own xiaoba_agentos/review_ui_install.py (the real
    product UI-asset installer). Its prepare/run logic and output shape are reused
    verbatim; this tool never re-implements or reshapes them."""
    root = Path(candidate).resolve()
    path = root / 'src/xiaoba_agentos/review_ui_install.py'
    if path.is_symlink() or not path.is_file():
        raise StopError('STOP: candidate review_ui_install.py missing: %s' % path)
    name = '_stage75r_review_ui_install_' + hashlib.sha256(str(path).encode()).hexdigest()[:16]
    if name not in sys.modules:
        sys.path[:0] = [str(root / 'src')]
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = previous
    return sys.modules[name]


def install_ui_assets(ctx, *, apply=False, project_root=None, assistant_root=None,
                      runtime_dir=None, assets=None):
    """Install the source-bound review-UI assets via the candidate's OFFICIAL
    review_ui_install.run (read-only prepare by default; apply writes atomically with
    in-memory rollback, no backups). Same-hash targets are left untouched (no overwrite);
    a target drifted from both before/after hashes refuses (rebase required). The returned
    record is the official product shape verbatim: {ok, applied, up_to_date,
    restart_monitor_required, files:[{path, changed, after_sha256}]}.

    Isolation injects project_root/assistant_root/runtime_dir/assets to target temporary
    dirs; the real branch resolves the production project + assistant roots. The official
    installer is a separate user-terminal post-switch action, never part of core apply.
    """
    ui = _ui_install_module(ctx['candidate'])
    candidate = Path(ctx['candidate'])
    assets = Path(assets) if assets else candidate / 'src/xiaoba_agentos/review_ui_assets'
    project = Path(project_root) if project_root else Path(ctx['live'])
    assistant = Path(assistant_root) if assistant_root else ASSISTANT_ROOT
    runtime = Path(runtime_dir) if runtime_dir else Path(ctx['live']) / '.runtime'
    if apply:
        runtime.mkdir(parents=True, exist_ok=True)
    config = SimpleNamespace(project_dir=project, runtime_dir=runtime,
                             memory={'obsidian_vault': str(assistant / '第二大脑')})
    try:
        return ui.run(config, apply=apply, assets=assets)
    except (ValueError, RuntimeError) as exc:
        raise StopError('STOP: UI asset install failed: %s' % exc) from exc


def source_bound_ui_state(ctx, monitor):
    """Check all official installer targets without writing, and record digests only."""
    candidate = Path(ctx['candidate'])
    files = read_json(candidate / 'SOURCE_MANIFEST.json')['files']
    # Legacy callers before the whole shared monitor was projected retain
    # their existing monitor check. A projected source requires this proof.
    if not any(name.startswith('src/xiaoba_monitor_shared/') for name in files):
        return []
    installer = 'src/xiaoba_agentos/review_ui_install.py'
    assets = 'src/xiaoba_agentos/review_ui_assets'
    for relative in (installer, assets + '/manifest.json'):
        path = candidate / relative
        if path.is_symlink() or not path.is_file() or sha(path) != files.get(relative):
            raise StopError('STOP: UI installer source binding failed: %s' % relative)
    ui = _ui_install_module(candidate)
    try:
        assistant = Path(ctx.get('assistant_root', ASSISTANT_ROOT))
        if 'assistant_root' in ctx and not assistant.resolve().is_relative_to(Path(ctx['root']).resolve()):
            raise StopError('STOP: isolated UI assistant root escapes the rehearsal scene')
        plan = ui.prepare(Path(ctx['live']), assistant, candidate / assets)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StopError('STOP: product UI asset precheck failed: %s' % exc) from exc
    monitor = Path(monitor)
    rows = []
    for asset in plan:
        target = asset['path']
        after_sha = sha_bytes(asset['new'])
        observed_sha = sha_bytes(asset['old']) if asset['old'] is not None else None
        monitor_name = target.relative_to(monitor).as_posix() if target.is_relative_to(monitor) else None
        if monitor_name and files.get('src/xiaoba_monitor_shared/' + monitor_name) != after_sha:
            raise StopError('STOP: product UI and shared monitor are not source-identical: %s' % monitor_name)
        rows.append({'path': str(target), 'monitor_name': monitor_name,
                     'observed_sha256': observed_sha, 'after_sha256': after_sha})
    if not any(row['monitor_name'] for row in rows):
        raise StopError('STOP: no product UI assets map to the shared monitor')
    return rows


def verify_ui_postimage(ctx, monitor_pin, monitor):
    """Require the user's separate installer action to match the before-switch plan."""
    rows = source_bound_ui_state(ctx, monitor)
    expected = monitor_pin.get('user_ui_assets', [])
    current_binding = [(row['path'], row['monitor_name'], row['after_sha256']) for row in rows]
    pinned_binding = [(row['path'], row['monitor_name'], row['after_sha256']) for row in expected]
    if current_binding != pinned_binding:
        raise StopError('STOP: product UI target/after hashes changed since the switch')
    pending = [row['path'] for row in rows if row['observed_sha256'] != row['after_sha256']]
    if pending:
        raise StopError('STOP: separate user-terminal UI installation is still pending: %s'
                        % ', '.join(pending))
    return rows


def verify_two_votes(ctx):
    evidence = review_evidence(ctx)
    state = evidence['state']
    runs = read_json(evidence['runs_path'])
    if runs.get('ok') is not True:
        raise StopError('STOP: review acceptance report not ok')
    if runs.get('task_status') != 'completed':
        raise StopError('STOP: review task not completed')
    if runs.get('passed_votes', 0) < 2 or runs.get('failed_votes', 0) != 0:
        raise StopError('STOP: two-vote count gate failed: %s' % runs)
    distinct = runs.get('distinct_agents') or []
    if len(set(distinct)) < 2:
        raise StopError('STOP: fewer than two distinct reviewing agents')
    if state.get('acceptance_passed') is not True:
        raise StopError('STOP: STATE.acceptance_passed is not True')
    freeze_path = Path(ctx['acceptance']) / 'FREEZE.json'
    if sha(freeze_path) != state.get('freeze_sha256'):
        raise StopError('STOP: FREEZE.json hash differs from STATE.json')
    if state.get('source_manifest_sha256') != ctx['assigned']['source_manifest_sha256']:
        raise StopError('STOP: STATE source manifest differs from BASE_ASSIGNED')
    db_gate = validate_acceptance_db(ctx, state)
    candidate = Path(ctx['candidate'])
    passed_agents = []
    for agent in sorted(set(distinct)):
        output_path = evidence['review_dir'] / ('acceptance-%s.txt' % agent)
        if output_path.is_symlink() or not output_path.is_file():
            raise StopError('STOP: per-agent review output missing: %s' % output_path)
        if verdict_passes(output_path.read_text(encoding='utf-8'), candidate):
            passed_agents.append(agent)
    if len(passed_agents) < 2:
        raise StopError('STOP: fewer than two outputs re-verified by candidate predicate')
    return {'distinct_agents': sorted(set(distinct)), 'reverified_passed': passed_agents,
            'passed_votes': runs['passed_votes'], 'failed_votes': runs['failed_votes'],
            'acceptance_database': db_gate}


def verify_source_tool_hashes(ctx):
    candidate_manifest = Path(ctx['candidate']) / 'SOURCE_MANIFEST.json'
    if sha(candidate_manifest) != ctx['assigned']['source_manifest_sha256']:
        raise StopError('STOP: candidate SOURCE_MANIFEST differs from BASE_ASSIGNED')
    verify_manifests(ctx)
    bound = bound_helpers(ctx)
    return {'candidate_source_manifest_sha256': sha(candidate_manifest),
            'bound_helpers': {k: v['sha256'] for k, v in bound.items()}}


def cli_blockers(live_db):
    """Disabled, non-removed CLI nodes need a user decision before any retest."""
    from context_adapter import roster
    blockers = []
    for row_id, entry in roster(live_db).items():
        node = entry['node']
        if node.get('interface') == 'cli' and not node.get('removed') and not node.get('enabled'):
            blockers.append(row_id)
    return blockers


def _live_parent_chain(target, live_root):
    """Real-directory chain from target.parent up to the live monitor root.

    Every parent must be an existing non-symlink directory; the chain must end
    exactly at the live root. This is the B02 parent-type gate, independent of
    the object at the target itself.
    """
    root = Path(live_root)
    if root.is_symlink() or not root.is_dir():
        raise StopError('STOP: live monitor root missing or symlinked: %s' % root)
    chain = []
    cur = Path(target).parent
    while True:
        chain.append(cur)
        if cur == root:
            break
        if cur == cur.parent or not str(cur).startswith(str(root) + '/'):
            raise StopError('STOP: target parent outside live monitor root: %s' % target)
        cur = cur.parent
    for part in chain:
        if part.is_symlink() or not part.is_dir():
            raise StopError('STOP: target parent not a real regular directory: %s' % part)
    return [str(p) for p in chain]


def _target_kind(path):
    """Explicit target observation: absent / regular / link / special."""
    if path.is_symlink():
        return 'link'
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 'absent'
    if stat.S_ISREG(info.st_mode):
        return 'regular'
    return 'special'


def capture_post_preimages(ctx, files=None):
    """B02 read-only preimage capture for the authorized out-of-package set.

    The new module's target must be ABSENT with a real-directory parent chain and
    an explicit status; an already-installed postimage is a post-switch state and
    must never be captured as a pre-switch absent preimage. Every existing target
    must be a regular file; missing / symlink / special objects are refused.
    Counts are computed from the actual authorized set, never a constant.
    """
    declaration = post_file_declaration(ctx)
    files = files if files is not None else post_files(ctx)
    install = {row['name']: row['install'] for row in declaration['files']}
    live = live_monitor_dir(ctx)
    preimages, states, parents = {}, {}, {}
    for f in files:
        name, target = f['name'], f['live_path']
        parents[name] = _live_parent_chain(target, live)
        kind = _target_kind(target)
        states[name] = kind
        if install[name]['policy'] == 'first_install':
            if kind == 'absent':
                preimages[name] = None
            elif kind == 'regular':
                raise StopError('STOP: new module already at postimage; cannot capture an '
                                'absent pre-switch preimage (recover/post-switch only): %s' % target)
            else:
                raise StopError('STOP: new module target is %s (expected absent): %s' % (kind, target))
        else:
            if kind != 'regular':
                raise StopError('STOP: existing target is %s (expected regular file): %s' % (kind, target))
            preimages[name] = sha(target)
    return {'preimages': preimages, 'states': states, 'parent_chains': parents,
            'install': install, 'declaration_count': declaration['count']}


def capture_monitor_state(ctx):
    """Read-only capture of the authorized sync set: pinned live before-hashes,
    exported hashes, computed runtime counts, resolved taskcards, sync precheck.
    No writes; the pin is persisted by core_switch after the marker is claimed."""
    files = post_files(ctx)
    repository = monitor_source_dir(ctx)
    live = live_monitor_dir(ctx)
    ui_rows = source_bound_ui_state(ctx, live)
    ui_files = {row['monitor_name']: row for row in ui_rows if row['monitor_name']}
    sync_files = [f['name'] for f in files]
    expected_export = {f['name']: f['expected_sha256'] for f in files}
    capture = capture_post_preimages(ctx, files)
    live_before = capture['preimages']
    installed_modes = {}
    for f in files:
        target = f['live_path']
        if target.is_file() and not target.is_symlink():
            installed_modes[f['name']] = '0o%o' % (target.stat().st_mode & 0o777)
    precheck = sync_precheck(repository, live, sync_files=sync_files,
                             expected_export=expected_export, live_before=live_before,
                             ui_files=ui_files, install=capture['install'],
                             installed_modes=installed_modes)
    if not precheck['ok']:
        raise StopError('STOP: monitor sync precheck failed: %s'
                        % json.dumps({k: precheck[k] for k in precheck if k.endswith('match')
                                      or k in ('only_authorized_differences', 'sources_regular',
                                               'monitor_json_regular', 'monitor_json_taskcards_absent',
                                               'ui_assets_match', 'ui_assets_allowed_missing')},
                                     ensure_ascii=False))
    taskcards, resolver_file, resolver_sha = resolved_taskcards(live)
    return {'sync_files': sync_files, 'live_before_sha256': live_before,
            'export_sha256': expected_export, 'repository': str(repository), 'live': str(live),
            'runtime_file_count': precheck['expected_file_count'],
            'target_states': capture['states'], 'install': capture['install'],
            'installed_modes': installed_modes,
            'declaration_count': capture['declaration_count'],
            'taskcards': taskcards, 'resolver_file': resolver_file, 'resolver_sha256': resolver_sha,
            'user_ui_assets': ui_rows,
            'precheck': precheck}


def syntax_gate(ctx):
    """Language-split syntax check of the authorized exported post files, bound to the
    record hashes. A failure here refuses the switch BEFORE any apply."""
    checks = []
    for f in post_files(ctx):
        data = f['source_path'].read_bytes()
        if sha_bytes(data) != f['expected_sha256']:
            raise StopError('STOP: post file drifted before syntax gate: %s' % f['name'])
        checks.append(syntax_check(f['name'], data, f['source_path']))
    return checks


def gate_check(ctx):
    """All read-only gates. Pure reads + read-only migration; NO production writes,
    NO marker claim. Returns the evidence bundle core_switch persists."""
    step4 = verify_step4_result(ctx)
    release = verify_package(ctx, step4)
    integrity = verify_package_integrity(ctx)
    baseline = validate_baseline(ctx)
    votes = verify_two_votes(ctx)
    hashes = verify_source_tool_hashes(ctx)
    migration = run_migration_check(ctx, live_db_path(ctx))
    if not migration.get('ok'):
        raise StopError('STOP: migration declaration gate failed: %s' % migration)
    syntax = syntax_gate(ctx)
    blockers = cli_blockers(live_db_path(ctx))
    if blockers:
        raise StopError('STOP: disabled CLI nodes require user decision: %s' % blockers)
    monitor = capture_monitor_state(ctx)
    return {'step4_result_sha256': sha(attempt_paths(ctx)['reports_step4_dir'] / 'STEP4-5-result.json'),
            'package_files_sha256': step4['package']['files_sha256'],
            'release_tier': release.get('release_tier'), 'package_integrity': integrity,
            'baseline': baseline, 'votes': votes, 'hashes': hashes,
            'migration': migration, 'syntax': syntax, 'monitor': monitor}


def _switch_script(ctx):
    script = attempt_paths(ctx)['package_dir'] / 'agentos/scripts/switch_release.py'
    if script.is_symlink() or not script.is_file():
        raise StopError('STOP: package switch_release.py missing: %s' % script)
    return script


def _run_mode(ctx, mode, out_dir, env, extra=()):
    script = _switch_script(ctx)
    package = attempt_paths(ctx)['package_dir']
    argv = [str(PIN), '-B', str(script), mode, '--package', str(package), *extra]
    log = out_dir / ('%s.json' % mode)
    code, value, text = run_cli_json(argv, cwd=Path(ctx['live']), env=env, log_path=log)
    if code != 0 or not isinstance(value, dict):
        raise StopError('STOP: switch_release %s failed (exit %d); original output kept: %s'
                        % (mode, code, log))
    return value


def core_switch(ctx, out_dir, *, env=None):
    """Claim the ONE core marker, then drive plan -> preflight -> apply -> readback.
    Refuses if the marker already exists (never re-apply). Post-switch is separate."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    marker = out_dir / 'SWITCH.started'
    if marker.exists():
        raise StopError('STOP: core SWITCH already attempted; never re-apply. Use recover/post-switch.')

    gates = gate_check(ctx)
    env = env if env is not None else clean_env()

    with marker.open('x') as handle:
        handle.write('One core switch attempt only. Do not remove.\n')
    atomic_dump(out_dir / 'gates.json', gates)
    atomic_dump(out_dir / 'monitor-before.json', gates['monitor'])

    plan = _run_mode(ctx, 'plan', out_dir, env)
    if plan.get('ok') is not True or plan.get('writes_performed') != 0:
        raise StopError('STOP: read-only plan failed or wrote: %s' % plan.get('error'))
    if plan.get('version') != ctx['version']:
        raise StopError('STOP: plan target version != manager version')

    preflight = _run_mode(ctx, 'preflight', out_dir, env)
    if preflight.get('ok') is not True or preflight.get('writes_performed') != 0:
        raise StopError('STOP: preflight failed or wrote: %s' % preflight.get('error'))

    applied = _run_mode(ctx, 'apply', out_dir, env,
                        extra=('--preflight', str(out_dir / 'preflight.json')))
    if applied.get('ok') is not True or applied.get('activated') is not True:
        raise StopError('STOP: apply did not activate; original output preserved, never resent: %s'
                        % applied.get('error'))
    old_pid = applied.get('old_pid')
    new_pid = applied.get('new_pid')

    readback = _run_mode(ctx, 'readback', out_dir, env, extra=('--old-pid', str(old_pid)))
    if readback.get('ok') is not True or readback.get('writes_performed') != 0:
        raise StopError('STOP: readback failed or wrote: %s' % readback.get('error'))
    if readback.get('version') != ctx['version']:
        raise StopError('STOP: readback version != manager version')
    if (readback.get('worker') or {}).get('pid') != new_pid:
        raise StopError('STOP: readback worker PID differs from applied new PID')
    if readback.get('restart_verified') is not True:
        raise StopError('STOP: readback did not verify restart against old PID')

    if sha(Path(ctx['live']) / 'SOURCE_MANIFEST.json') != ctx['assigned']['source_manifest_sha256']:
        raise StopError('STOP: live source manifest after switch differs from BASE_ASSIGNED')
    migration_after = run_migration_check(ctx, live_db_path(ctx))
    if not migration_after.get('ok'):
        raise StopError('STOP: post-switch migration readback failed')

    summary = {'ok': True, 'activated': True, 'version': ctx['version'], 'old_pid': old_pid,
               'new_pid': new_pid, 'writes_performed': applied.get('writes_performed'),
               'completed_steps': applied.get('completed_steps'),
               'release_tier': gates['release_tier'],
               'migration_after': migration_after,
               'live_source_manifest_sha256': sha(Path(ctx['live']) / 'SOURCE_MANIFEST.json')}
    atomic_dump(out_dir / 'switch-result.json', summary)
    return summary
