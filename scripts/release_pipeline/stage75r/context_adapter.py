"""Adapter over the 75P shared release_context + stage75r parameter resolution.

The real branch imports the landed, accepted shared module (driver75p/
release_context.py) and uses its fixed interface: load_context / validate_context
/ clean_env / check_interpreter / verify_manifests. Leaf functions below take the
ctx dict so isolation fixtures can inject a synthetic ctx without the manager.

Production roots (PROJECT_ROOT / ASSISTANT_ROOT) are stable deployment
infrastructure, NOT release-specific values; round/version/attempt/package/BASE
all come from ctx and the ONE attempt record.
"""
import importlib.util
import json
import sqlite3
from pathlib import Path

from common import StopError, read_json, sha
from monitor_contract import relative_name, rooted_path

PROJECT_ROOT = Path('/Users/suzhizhou/Documents/小八AgentOS')
ASSISTANT_ROOT = Path('/Users/suzhizhou/Documents/私人助理')
_MONITOR_TAIL = Path('工具/小八监看')


def shared_module():
    """Import the landed shared module by file path (no copy, no .pyc under -B)."""
    path = Path(__file__).resolve().parent.parent / 'driver75p' / 'release_context.py'
    if path.is_symlink() or not path.is_file():
        raise StopError('STOP: shared module driver75p/release_context.py not landed yet (75P): %s' % path)
    spec = importlib.util.spec_from_file_location('stage75r_release_context', path)
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def load_context(work_dir=None):
    return shared_module().load_context(work_dir)


def validate_context(ctx, stage):
    return shared_module().validate_context(ctx, stage)


def check_interpreter():
    return shared_module().check_interpreter()


def clean_env():
    return shared_module().clean_env()


def verify_manifests(ctx):
    return shared_module().verify_manifests(ctx)


def validate_baseline_evidence(ctx, *, require_freeze=True):
    return shared_module().validate_baseline_evidence(ctx, require_freeze=require_freeze)


def attempt_paths(ctx):
    """The ONE record is the only consumer source; never re-derive the attempt."""
    rec = ctx['record']
    return {
        'attempt': rec['attempt'],
        'acceptance_step4_dir': Path(rec['acceptance_step4_dir']),
        'reports_step4_dir': Path(rec['reports_step4_dir']),
        'package_dir': Path(rec['releases_package_dir']),
        'helpers_dir': Path(rec['step4_helpers_dir']),
    }


def _repo_relative(ctx, value):
    """Use the recorded absolute path, never its basename, as source identity."""
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise StopError('STOP: post file path must be absolute: %r' % value)
    if ('\\' in value or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(p in ('', '.', '..') for p in value.split('/')[1:])):
        raise StopError('STOP: non-canonical post file path: %r' % value)
    repo = Path(ctx['repo'])
    try:
        rel = Path(value).relative_to(repo)
    except ValueError as exc:
        raise StopError('STOP: post file outside repo: %s' % value) from exc
    relative_name(rel.as_posix())
    rooted_path(repo, rel.as_posix())
    return rel


def _post_paths(ctx):
    """Resolve one shared monitor deployment root, independent of input order."""
    raw = ctx['parameters'].get('post_files') or []
    if not raw:
        raise StopError('STOP: PREPARE-PARAMETERS.post_files is empty; nothing authorized to sync')
    prefix = None
    rows = []
    seen = set()
    marker = ('shared', *_MONITOR_TAIL.parts)
    for entry in raw:
        rel = _repo_relative(ctx, entry)
        parts = rel.parts
        matches = [i for i in range(len(parts) - len(marker))
                   if parts[i:i + len(marker)] == marker]
        if len(matches) != 1:
            raise StopError('STOP: unexpected monitor deployment path: %s' % rel)
        end = matches[0] + len(marker)
        current_prefix = Path(*parts[:end])
        if prefix is not None and prefix != current_prefix:
            raise StopError('STOP: post files have mixed monitor deployment roots')
        prefix = current_prefix
        name = relative_name('/'.join(parts[end:]))
        if name in seen:
            raise StopError('STOP: duplicate post file: %s' % name)
        seen.add(name)
        if Path(name).suffix not in ('.py', '.command'):
            raise StopError('STOP: unknown post file suffix; report, do not guess: %s' % name)
        rows.append((rel, name))
    return prefix, rows


def post_file_layout(ctx):
    """Read-only path/hash plan, including pre-export contexts with absent files.

    Each syntax hash is matched by the full recorded repo path. The displayed
    identity and the live target use the path relative to the monitor root, so
    equal basenames in different subdirectories remain distinct.
    """
    prefix, rows = _post_paths(ctx)
    syntax = {}
    for item in ctx['record'].get('syntax', []):
        rel = _repo_relative(ctx, item.get('path'))
        if rel in syntax:
            raise StopError('STOP: duplicate recorded post syntax path: %s' % rel)
        expected = item.get('sha256')
        if (not isinstance(expected, str) or len(expected) != 64
                or any(c not in '0123456789abcdef' for c in expected)):
            raise StopError('STOP: invalid recorded post syntax hash: %s' % rel)
        syntax[rel] = expected
    if set(syntax) != {rel for rel, _ in rows}:
        raise StopError('STOP: authorized post paths do not match recorded syntax paths')
    live = live_monitor_dir(ctx)
    return [{'name': name, 'kind': 'python' if Path(name).suffix == '.py' else 'shell',
             'repo_path': rooted_path(ctx['repo'], rel.as_posix()),
             'source_path': rooted_path(ctx['source'], rel.as_posix()),
             'live_path': rooted_path(live, name),
             'expected_sha256': syntax[rel]}
            for rel, name in rows]


def post_files(ctx):
    """Bind the authorized sync set to existing repo/export bytes and record hashes."""
    files = post_file_layout(ctx)
    for f in files:
        for key, label in (('repo_path', 'repo'), ('source_path', 'exported source')):
            path = f[key]
            if not path.is_file():
                raise StopError('STOP: post file missing in %s: %s' % (label, path))
            if sha(path) != f['expected_sha256']:
                raise StopError('STOP: %s post file hash differs from record: %s' % (label, f['name']))
    return files


_R9_AUTHORIZED = frozenset({
    'xiaoba_monitor.py', '启动监看中心.command',
    '关闭监看中心.command', 'monitor_core/modules/system.py',
})
_NEW_MODULE = 'monitor_core/lifecycle.py'
_FIRST_INSTALL_MODE = '0o644'


def post_file_declaration(ctx):
    """Read-only, compatibility-safe declaration of the out-of-package mapping.

    post_file_layout()/post_files() keep their original signature and output:
    full repo-path syntax matching, single 工具/小八监看 tail, .py/.command suffix,
    syntax set == authorized set, and byte-equal repo AND export hashes.

    Mapping attaches only minimal install metadata:
      * every authorized path not explicitly first-install -> existing replacement
        (mode follows the installed object, resolved in B02-B08);
      * the new module, only when explicitly declared -> first_install at fixed 0o644.

    No object-preimage / installed / drift / parent-directory state gate runs here
    (B02-B08). An already-postimage new module therefore stays readable, so a
    recovery pass can consume this same declaration. There is no write entry.
    """
    files = post_files(ctx)
    authorized = {f['name'] for f in files}
    policy = ctx['parameters'].get('post_install_policy', {})
    expected = {_NEW_MODULE: {'install': 'first_install', 'mode': _FIRST_INSTALL_MODE}}
    if policy != {} and policy != expected:
        raise StopError('STOP: post_install_policy must authorize only %s at %s'
                        % (_NEW_MODULE, _FIRST_INSTALL_MODE))
    if _NEW_MODULE in authorized and not policy:
        raise StopError('STOP: %s must be explicitly declared first-install' % _NEW_MODULE)
    if policy and _NEW_MODULE not in authorized:
        raise StopError('STOP: first-install declaration not in authorized set: %s' % _NEW_MODULE)
    if policy and authorized != _R9_AUTHORIZED | {_NEW_MODULE}:
        raise StopError('STOP: first-install authorization must retain all r9 deployment paths')
    declared = []
    for f in files:
        name = f['name']
        target = f['live_path']
        entry = {'name': name, 'kind': f['kind'],
                 'repo_path': str(f['repo_path']),
                 'source_path': str(f['source_path']),
                 'live_path': str(target),
                 'expected_sha256': f['expected_sha256']}
        if name == _NEW_MODULE:
            entry['install'] = {'policy': 'first_install', 'mode': _FIRST_INSTALL_MODE,
                                'mode_source': 'explicit_fixed'}
        else:
            entry['install'] = {'policy': 'existing', 'mode': None,
                                'mode_source': 'current_installed'}
        declared.append(entry)
    n_first = sum(1 for e in declared if e['install']['policy'] == 'first_install')
    return {
        'declares': 'package-outside monitor file mapping',
        'writes': 0,
        'count': len(declared),
        'first_install_count': n_first,
        'existing_count': len(declared) - n_first,
        'binding': {
            'sha_source': 'record.syntax [path] matched by full repo-relative path',
            'two_source_evidence': ('repo_path bytes and source_path(export) bytes '
                                    'are each hashed and compared to record.syntax'),
            'syntax_set_equals_authorized_set': True,
            'install_mode_source': ('optional explicit first-install declaration only; '
                                    'never inferred from candidate/export source bits'),
        },
        'files': declared,
    }


def _monitor_tail(ctx):
    _post_paths(ctx)
    return _MONITOR_TAIL


def monitor_source_dir(ctx):
    post_files(ctx)
    prefix, _ = _post_paths(ctx)
    return rooted_path(ctx['source'], prefix.as_posix())


def live_monitor_dir(ctx):
    """Live monitor root derived from the post-file deployment tail (工具/小八监看)
    under the production assistant root; cross-checked against the candidate UI
    manifest's assistant-base monitor entry when present.

    Isolation fixtures may inject ctx['live_monitor_dir'] to target a temporary
    monitor copy; the real branch leaves it unset and resolves the production dir.
    """
    override = ctx.get('live_monitor_dir')
    if override:
        return Path(override)
    live = ASSISTANT_ROOT / _monitor_tail(ctx)
    manifest = Path(ctx['candidate']) / 'src/xiaoba_agentos/review_ui_assets/manifest.json'
    if manifest.is_file() and not manifest.is_symlink():
        for item in read_json(manifest).get('files', []):
            if item.get('base') == 'assistant' and Path(item['relative']).parent == _MONITOR_TAIL:
                if (ASSISTANT_ROOT / Path(item['relative']).parent) != live:
                    raise StopError('STOP: UI manifest monitor dir differs from derived live dir')
                break
    return live


def migration_spec(ctx):
    """Read-only migration declaration assigned by the lead (75H convention:
    work/<round>/migration-spec.json). v1.6.15 declares schema_version 7 and ZERO
    new indexes; the two v1.6.14 idx_events_* are existing objects, NOT new here.
    The 75C extended tables are verified at new-package init/copy (75Q), never
    required in the pre-upgrade live database."""
    path = Path(ctx['work']) / 'migration-spec.json'
    spec = read_json(path)
    if not isinstance(spec.get('schema_version'), int):
        raise StopError('STOP: migration-spec.json needs integer schema_version')
    if not isinstance(spec.get('new_indexes'), dict):
        raise StopError('STOP: migration-spec.json needs object new_indexes {name: sql}')
    return spec


def review_evidence(ctx):
    """Locate the two-vote evidence using 75P's actual layout: round-level
    acceptance/STATE.json + reports/review/11-acceptance.json (+ per-agent
    acceptance-<name>.txt outputs). No reviewer name or attempt subdir hardcoded.

    When STATE.review_evidence_path is present (75V same-task failure-side recovery
    publishes a NEW qualified report at an explicit absolute file, old failure kept),
    ONLY that explicit path is used: it must be a non-symlink regular file strictly
    inside ctx.reports (no escape); review_dir is its parent and runs_path is the file
    itself. There is NO directory search / vote guessing and NO fallback to the old
    failed report. Without the field, the original reports/review/11-acceptance.json
    contract holds. verify_two_votes' official STATE.database/task_id validator gate
    is unchanged — an explicit path never relaxes it."""
    state_path = Path(ctx['acceptance']) / 'STATE.json'
    state = read_json(state_path)
    explicit_raw = state.get('review_evidence_path')
    if explicit_raw:
        explicit = Path(explicit_raw)
        if not explicit.is_absolute():
            raise StopError('STOP: STATE.review_evidence_path must be an absolute file: %s' % explicit)
        if explicit.is_symlink() or not explicit.is_file():
            raise StopError('STOP: STATE.review_evidence_path missing or symlinked: %s' % explicit)
        reports_root = Path(ctx['reports']).resolve()
        try:
            explicit.resolve().relative_to(reports_root)
        except ValueError as exc:
            raise StopError('STOP: STATE.review_evidence_path escapes ctx.reports: %s' % explicit) from exc
        return {'state_path': state_path, 'state': state,
                'review_dir': explicit.parent, 'runs_path': explicit}
    review_dir = Path(ctx['reports']) / 'review'
    runs_path = review_dir / '11-acceptance.json'
    return {'state_path': state_path, 'state': state, 'review_dir': review_dir, 'runs_path': runs_path}


def bound_helpers(ctx):
    """Verify the ATTEMPT-POINTER-bound prepare tool + migration checks hashes
    (drift -> stop), then return their paths. The lead swaps r4->r5 by updating
    the pointer; this never hardcodes a revision."""
    pointer = read_json(Path(ctx['work']) / 'ATTEMPT-POINTER.json')
    bound = {}
    for key, hash_key in (('prepare_tool', 'prepare_tool_sha256'),
                          ('migration_checks', 'migration_checks_sha256')):
        path = Path(pointer[key])
        if path.is_symlink() or not path.is_file():
            raise StopError('STOP: bound helper missing or symlinked: %s' % path)
        digest = sha(path)
        if digest != pointer[hash_key]:
            raise StopError('STOP: bound helper hash drift (%s): pointer=%s actual=%s'
                            % (key, pointer[hash_key], digest))
        bound[key] = {'path': path, 'sha256': digest}
    return bound


def _import_file(path, name):
    import sys
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise StopError('STOP: module missing or symlinked: %s' % path)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def load_prepare_tool(ctx):
    bound = bound_helpers(ctx)
    return _import_file(bound['prepare_tool']['path'], 'stage75r_prepare_tool')


def run_migration_check(ctx, db_path):
    """Parameterized migration validation via the pointer-bound prepare tool's
    PUBLIC run_migration, loading the pointer-bound checks module (r5). v1.6.15
    declares schema 7 + zero new indexes; the checks module is hash-bound, never
    a fixed revision. Read-only connection; missing DB fails without creating."""
    spec = migration_spec(ctx)
    tool = load_prepare_tool(ctx)
    bound = bound_helpers(ctx)
    db = spec.get('db') or str(db_path)
    return tool.run_migration(db, spec['schema_version'], spec['new_indexes'],
                              spec.get('fresh_database_sha256', ''),
                              checks_module=str(bound['migration_checks']['path']))


def roster(db_path):
    """Read the CURRENT actual roster strictly read-only. Identity is the SQL id
    (== data.reviewer_id); data.node has NO node_id. Removed semantics preserved.
    Model / reasoning_effort are opaque data, never asserted to a fixed value."""
    db = Path(db_path)
    if db.is_symlink() or not db.is_file():
        raise StopError('STOP: roster database missing or symlinked: %s' % db)
    nodes = {}
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as conn:
        for row_id, raw in conn.execute('SELECT id,data FROM ri_sources ORDER BY rowid'):
            data = json.loads(raw)
            node = data.get('node')
            legacy = node is None
            if node is None:
                node = {'version': 1, 'revision': 0, 'enabled': True, 'removed': False,
                        'name': data['reviewer_id'], 'agent': data['reviewer_id'],
                        'interface': None, 'entry': None, 'model': None}
            if data.get('reviewer_id') != row_id:
                raise StopError('STOP: ri_sources id != data.reviewer_id: %s' % row_id)
            nodes[row_id] = {'node': node, 'kind': data.get('kind'), 'legacy': legacy}
    return nodes
