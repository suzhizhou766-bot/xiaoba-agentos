#!/usr/bin/env python3
"""固定发布切换入口。版本和专属步骤来自被哈希验证的发布包。

plan/preflight/readback 只读；apply 仅由用户在个人终端执行。
恢复日志只保存阶段和哈希；硬退出后 recover 查询，resume 从已核验阶段继续。
"""
from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import importlib.util
import json
import os
import plistlib
import re
import sqlite3
import subprocess
import sys
import time
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

sys.dont_write_bytecode = True
SOURCE_DIRS = ('.agents', 'bin', 'config', 'docs', 'knowledge', 'launchd', 'macos', 'scripts', 'skills', 'src', 'tests')
SOURCE_FILES = ('README.md', 'pyproject.toml', '.gitignore', 'CAPABILITY.json')
DEFAULT_LINE = re.compile(r'^DEFAULT_CONFIG = Path\("[^"\n]+"\)$', re.M)
LABEL = 'com.xiaoba.agentos'
SPEC = 'docs/DEPLOYMENT.json'
TTL = 300


class SwitchError(RuntimeError):
    pass


def require(value: Any, reason: str) -> None:
    if not value:
        raise SwitchError(reason)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def atomic_bytes(path: Path, data: bytes, mode: int = 0o600, expected: Optional[dict] = None) -> None:
    """A failed/short write can only damage a private new-byte temporary."""
    safe(path)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'wb') as output:
            require(output.write(data) == len(data), 'short recovery write')
            output.flush(); os.fsync(output.fileno())
        os.chmod(temp, mode)
        require(temp.read_bytes() == data, 'recovery temporary readback mismatch')
        if expected is not None:
            require(state(path) == expected, f'concurrent recovery change: {path}')
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if temp.exists(): temp.unlink()


def relative(value: str) -> Path:
    p = Path(value)
    require(value and not p.is_absolute() and '..' not in p.parts and p != Path('.'), 'unsafe relative path')
    return p


def safe(path: Path) -> None:
    for p in (path, *path.parents):
        require(not p.is_symlink(), f'symlink is forbidden: {p}')


def state(path: Path) -> dict:
    safe(path)
    if not path.exists():
        return {'sha256': None, 'mode': None}
    require(path.is_file(), f'expected regular file: {path}')
    return {'sha256': digest(path.read_bytes()), 'mode': path.stat().st_mode & 0o777}


def load(path: Path) -> dict:
    safe(path)
    value = json.loads(path.read_bytes())
    require(isinstance(value, dict), f'expected JSON object: {path}')
    return value


def files(root: Path, exclude_generated: bool = False) -> dict:
    safe(root)
    require(root.is_dir(), f'directory unavailable: {root}')
    result = {}
    for p in sorted(root.rglob('*')):
        safe(p)
        if exclude_generated and ('__pycache__' in p.parts or p.name == '.DS_Store'):
            continue
        if p.is_file():
            result[p.relative_to(root).as_posix()] = state(p)['sha256']
    return result


def source_files(root: Path) -> dict:
    result = {}
    for name in SOURCE_DIRS:
        safe(root / name)
        if not (root / name).exists():
            continue  # 受管目录清单是上限：包里不再带的目录（如已出包的 skills、knowledge）就是不存在
        for rel, sha in files(root / name, exclude_generated=True).items():
            result[name + '/' + rel] = sha
    for name in SOURCE_FILES:
        result[name] = state(root / name)['sha256']
        require(result[name], f'source root file unavailable: {name}')
    return result


def verify_source(root: Path) -> dict:
    manifest = load(root / 'SOURCE_MANIFEST.json')
    require(manifest == {'algorithm': 'sha256', 'files': source_files(root)}, 'source manifest mismatch')
    return manifest['files']


def verify_package(root: Path) -> dict:
    listed = {}
    for line in (root / 'FILES.sha256').read_text().splitlines():
        sha, sep, rel = line.partition('  ./')
        require(sep and re.fullmatch('[0-9a-f]{64}', sha), 'invalid package manifest line')
        relative(rel)
        require(rel != 'FILES.sha256' and rel not in listed, 'duplicate or self manifest entry')
        listed[rel] = sha
    actual = files(root)
    actual.pop('FILES.sha256', None)
    require(actual == listed, 'package files/hash mismatch')
    release = load(root / 'RELEASE.json')
    require(re.fullmatch(r'v\d+\.\d+\.\d+', str(release.get('version', ''))), 'invalid release version')
    require(release.get('agentos_source_manifest_sha256') == state(root / 'agentos/SOURCE_MANIFEST.json')['sha256'], 'package source binding mismatch')
    verify_source(root / 'agentos')
    return release


def release_tier(previous: Path, package: Path, old: dict, new: dict) -> str:
    """档位由上一版包里的工具重算；包内记录与重算不一致即拒绝。上一版没有工具时为 full。"""
    spec = importlib.util.spec_from_file_location('release_tier', Path(__file__).with_name('release_tier.py'))
    require(spec is not None and spec.loader is not None, 'release tier helper unavailable')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    try:
        computed = helper.compute_tier(previous / 'agentos', package / 'agentos')
    except helper.TierError as exc:
        raise SwitchError(f'release tier cannot be recomputed: {exc}') from exc
    computed['baseline_version'] = old['version']
    recorded = new.get('release_tier')
    if recorded is None:
        require(computed['tier'] == helper.FULL, 'target package records no release tier')
    else:
        require(recorded == computed, 'recorded release tier differs from the recomputed tier')
    return computed['tier']


def interface_scripts(root: Path) -> dict:
    return {rel: sha for rel, sha in files(root / 'interface').items() if rel != 'SKILL.md'}


def code_fingerprint(root: Path) -> str:
    value = hashlib.sha256()
    for p in sorted((root / 'src/xiaoba_agentos').rglob('*.py')):
        safe(p)
        value.update(str(p.relative_to(root)).encode())
        value.update(b'\0')
        value.update(hashlib.sha256(p.read_bytes()).digest())
    return value.hexdigest()


def default_config(home: Path) -> Path:
    tree = ast.parse((home / 'scripts/verify-release').read_text())
    declarations = [n for n in tree.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == 'DEFAULT_CONFIG' for t in n.targets)]
    require(len(declarations) == 1, 'entrypoint DEFAULT_CONFIG declaration is not unique')
    value = declarations[0].value
    require(isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id == 'Path' and len(value.args) == 1
            and isinstance(value.args[0], ast.Constant) and isinstance(value.args[0].value, str), 'invalid entrypoint DEFAULT_CONFIG')
    return Path(value.args[0].value)


@dataclass(frozen=True)
class Layout:
    runtime: Path
    config: Path
    package: Path
    vault: Path
    graph: Path
    entries: tuple
    plist: Path
    label: str = LABEL

    @property
    def index(self) -> Path:
        return self.runtime / '.runtime/releases/index.json'

    @property
    def database(self) -> Path:
        return self.runtime / '.runtime/agentos.sqlite3'

    @property
    def worker(self) -> Path:
        return self.runtime / '.runtime/worker-state.json'

    @property
    def history(self) -> Path:
        return self.runtime / '.runtime/release-steps.json'

    @property
    def assistant(self) -> Path:
        return self.vault.parent


def production(package: Path) -> Layout:
    entries = (Path.home() / '.agents/skills/xiaoba-agentos', Path.home() / '.codex/skills/xiaoba-agentos')
    configs = [default_config(e) for e in entries]
    require(configs[0] == configs[1], 'installed entrypoint config paths differ')
    raw = load(configs[0])
    runtime = Path(raw['project_dir'])
    require(Path(raw['release']['index_path']) == runtime / '.runtime/releases/index.json', 'nonstandard release index')
    require(Path(raw.get('runtime_dir', str(runtime / '.runtime'))) == runtime / '.runtime', 'nonstandard runtime directory')
    require(Path(raw['paths']['obsidian_vault']) == Path(raw['memory']['obsidian_vault']), 'vault config differs')
    layout = Layout(runtime, configs[0], package, Path(raw['paths']['obsidian_vault']),
                    Path(raw['paths']['knowlp_graph_dir']), entries,
                    Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist'))
    for p in (layout.runtime, layout.config, layout.package, layout.vault, layout.graph, layout.plist, *entries):
        require(p.is_absolute(), 'layout paths must be absolute')
        safe(p)
    return layout


class System:
    def command(self, argv: list) -> subprocess.CompletedProcess:
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        return subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False, env=env)

    def alive(self, pid: int) -> bool:
        require(pid > 0, 'invalid worker PID')
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            raise SwitchError('cannot inspect worker PID')

    def process(self, pid: int) -> str:
        r = self.command(['ps', '-p', str(pid), '-o', 'command='])
        require(r.returncode == 0, 'worker process inspection failed')
        return r.stdout.strip()

    def registered(self, layout: Layout) -> bool:
        r = self.command(['launchctl', 'print', f'gui/{os.getuid()}/{layout.label}'])
        if r.returncode == 0:
            return True
        # A missing service is an explicit state, other failures are not.
        require(r.returncode == 113 and 'Could not find service' in r.stderr,
                f'cannot inspect worker service: returncode={r.returncode}')
        return False

    def worker_pids(self, layout: Layout) -> list:
        r = self.command(['ps', '-ax', '-o', 'pid=,command='])
        require(r.returncode == 0, 'cannot enumerate bound worker processes')
        found = []
        binding = re.compile(r'--config\s+' + re.escape(str(layout.config)) + r'\s+run\s*$')
        for line in r.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and 'xiaoba_agentos.cli' in parts[1] and binding.search(parts[1]):
                found.append(int(parts[0]))
        return sorted(found)

    def wait(self, predicate: Callable, reason: str, timeout: float = 45) -> None:
        deadline = time.monotonic() + timeout
        while True:
            if predicate():
                return
            require(time.monotonic() < deadline, reason)
            time.sleep(0.2)

    def stop(self, layout: Layout, pid: Optional[int]) -> None:
        # launchctl bootout uses a service target, not a domain plus bare label.
        r = self.command(['launchctl', 'bootout', f'gui/{os.getuid()}/{layout.label}'])
        require(r.returncode == 0, f'worker bootout failed: returncode={r.returncode}')
        if pid is not None:
            self.wait(lambda: not self.alive(pid), 'old worker did not exit')
        self.wait(lambda: not self.registered(layout) and not self.worker_pids(layout),
                  'worker service or bound child survived bootout')

    def start(self, layout: Layout) -> None:
        r = self.command(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(layout.plist)])
        require(r.returncode == 0, f'worker bootstrap failed: returncode={r.returncode}')

    def json_command(self, argv: list) -> dict:
        r = self.command(argv)
        require(r.returncode == 0, f'external verification failed: returncode={r.returncode}')
        result = json.loads(r.stdout)
        require(isinstance(result, dict) and result.get('ok') is True and not result.get('skipped'), 'external verifier did not pass')
        return result

    def doctor(self, argv: list) -> tuple:
        """(returncode, parsed report) of `xiaoba doctor --json`; a failing doctor still prints its report."""
        r = self.command(argv)
        try:
            result = json.loads(r.stdout)
        except ValueError:
            result = None
        require(isinstance(result, dict), f'doctor did not return a report: returncode={r.returncode}')
        return r.returncode, result


@dataclass
class Change:
    path: Path
    original: Optional[bytes]
    mode: Optional[int]
    after: dict


class Writer:
    """Only this object mutates files; undo refuses to overwrite concurrent changes."""
    def __init__(self):
        self.changes = []
        self.dirs = []
        self.write_count = 0

    def _parents(self, path: Path) -> None:
        if path.is_dir():
            return
        require(not path.exists(), f'parent is not a directory: {path}')
        self._parents(path.parent)
        path.mkdir(mode=0o700)
        self.dirs.append(path)

    def write(self, path: Path, data: Optional[bytes], expected: dict, mode: Optional[int] = None) -> None:
        before = state(path)
        require(before == expected, f'concurrent file change: {path}')
        old = path.read_bytes() if before['sha256'] is not None else None
        self._parents(path.parent)
        # Record before mutation, including chmod/replace failures.
        change = Change(path, old, before['mode'], before)
        self.changes.append(change)
        self.write_count += 1
        if data is None:
            path.unlink()
            change.after = state(path)
        else:
            # Temporary contains NEW bytes only; never persists old bytes.
            temporary = path.with_name('.' + path.name + f'.switch-{os.getpid()}')
            require(not temporary.exists() and not temporary.is_symlink(), 'new-byte temporary already exists')
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode or before['mode'] or 0o600)
                with os.fdopen(fd, 'wb') as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(temporary, mode or before['mode'] or 0o600)
                require(state(path) == before, f'concurrent file change before replacement: {path}')
                os.replace(temporary, path)
                change.after = state(path)
            finally:
                if temporary.exists():
                    temporary.unlink()

    def undo(self, since: int = 0) -> list:
        errors = []
        for change in reversed(self.changes[since:]):
            try:
                require(state(change.path) == change.after, f'undo refused concurrent change: {change.path}')
                if change.original is None:
                    if change.path.exists():
                        change.path.unlink()
                else:
                    atomic_bytes(change.path, change.original, change.mode, change.after)
                require(state(change.path) == {'sha256': digest(change.original) if change.original is not None else None,
                                               'mode': change.mode}, f'undo readback mismatch: {change.path}')
            except BaseException as exc:
                errors.append(str(exc))
        if not errors:
            del self.changes[since:]
        return errors

    def remove_dirs(self) -> list:
        errors = []
        for p in reversed(self.dirs):
            try:
                p.rmdir()
            except OSError:
                if p.exists():
                    errors.append(f'created directory not empty: {p}')
        return errors


def queue(layout: Layout) -> dict:
    """The queue must hold nothing in flight.

    A pending task that was never leased (attempts 0, no lease) has no partial
    state anywhere, so it may wait across the switch and is run, or refused, by
    the new release. Anything that ever started -- running, retry, frozen,
    resumable, or any unreleased lease -- still blocks. (Round-45 rehearsal: the
    old release could neither run nor cancel queued tasks while its backend
    route was unreadable, so a strict empty queue made switching impossible.)
    """
    safe(layout.database)
    require(layout.database.is_file(), 'runtime database unavailable')
    with closing(sqlite3.connect(layout.database.as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
        conn.execute('PRAGMA query_only=ON')
        waiting = "status='pending' AND attempts=0 AND lease_until IS NULL"
        unfinished = conn.execute(
            f"SELECT COUNT(*) FROM tasks WHERE status NOT IN ('completed','failed') AND NOT ({waiting})").fetchone()[0]
        not_started = conn.execute(f'SELECT COUNT(*) FROM tasks WHERE {waiting}').fetchone()[0]
        leases = conn.execute('SELECT COUNT(*) FROM tasks WHERE lease_until IS NOT NULL').fetchone()[0]
        repairs = conn.execute("SELECT COUNT(*) FROM repair_jobs WHERE status NOT IN ('completed','failed','retired') OR lease_until IS NOT NULL").fetchone()[0]
    require(unfinished == 0 and leases == 0 and repairs == 0,
            f'queue is not idle: unfinished={unfinished}, unreleased_leases={leases}, repairs={repairs}')
    return {'unfinished': unfinished, 'unreleased_leases': leases, 'unfinished_repairs': repairs,
            'pending_not_started': not_started}


def worker(layout: Layout, system: System, expected_old: Optional[int] = None) -> dict:
    result = load(layout.worker)
    pid = int(result.get('pid') or 0)
    require(system.alive(pid), 'worker PID is not alive')
    command = system.process(pid)
    require('xiaoba_agentos.cli' in command and re.search(r'--config\s+' + re.escape(str(layout.config)) + r'\s+run\s*$', command), 'worker process is not bound to active config')
    require(system.registered(layout) and system.worker_pids(layout) == [pid], 'worker service/child state is inconsistent')
    require(result.get('code_fingerprint') == code_fingerprint(layout.runtime), 'worker code fingerprint differs from disk')
    require(result.get('manifest_fingerprint') == state(layout.runtime / 'SOURCE_MANIFEST.json')['sha256'], 'worker manifest fingerprint differs from disk')
    if expected_old is not None:
        require(pid != expected_old and not system.alive(expected_old), 'old worker survived or PID did not change')
    return result


# The binding review must be passed by two healthy, distinct backends: the two
# primaries, or -- only while AGY is unavailable (user 2026-10-25, AGY account
# disabled) -- DSH plus the standby Qoder, publicly recorded as covering AGY for
# an accepted reason. Every other combination is refused.
STANDBY_COVER_REASONS = ('external_failure', 'health_probe_failed')


def independent_acceptance(evidence: dict) -> bool:
    agents = set(evidence.get('successful_agents') or [])
    if agents == {'agy_review', 'dsh_review'}:
        return True
    return (agents == {'dsh_review', 'qoder_review'} and evidence.get('standby_substituted') is True
            and evidence.get('replaced') == 'agy_review' and evidence.get('reason') in STANDBY_COVER_REASONS)


# Doctor checks that never decide doctor's own `ok` (retained objects are reported,
# not gating, by the user's rule); they are ignored when reading a failing report.
DOCTOR_NON_GATING = ('retained_objects', 'retained_manual_review', 'release_read_only')
# The checks that decide doctor's `ok` besides `paths` (health.doctor).
DOCTOR_GATES = ('release_binding', 'config_consistency', 'database_schema', 'event_chain', 'local_cli_routes',
                'memory_probe', 'memory_lifecycle', 'skill_registry', 'knowledge_catalog')


def doctor_check(layout: Layout, system: System, allow_route_outage: bool) -> dict:
    """Run the installed doctor. The new release must pass it outright.

    Before switching, the old release may fail it for one reason only: its
    backend route readiness (`local_cli_routes`), which is what a release that
    reads backends differently can fix -- a release that cannot read the
    backend desktop's model can never pass, which would make the fix unreachable
    (round-45 rehearsal). Every other check must still pass, the outage is
    written into the ticket, and readback after the switch requires the new
    doctor to pass, rolling back otherwise.
    """
    returncode, report = system.doctor([str(layout.entries[0] / 'scripts/xiaoba'), 'doctor', '--json'])
    # Kept as evidence on every path even though they never decide doctor's `ok`
    # (round-46 review; round 47: also when doctor passes).
    non_gating = sorted(name for name in DOCTOR_NON_GATING
                        if isinstance(report.get(name), dict) and report[name].get('ok') is False)
    if returncode == 0 and report.get('ok') is True:
        return {'doctor_ok': True, 'old_non_gating_failures': non_gating}
    require(allow_route_outage, f'external verification failed: returncode={returncode}')
    # The exception needs positive evidence, not an absence of complaints: an
    # explicit failing report, every path present and true, and every gating
    # check present and passing except the route check (round-47 review).
    paths = report.get('paths')
    # Every gating check other than the route check must be present and pass
    # explicitly (`ok is True`); an `ok` of None, 0 or "false" is not a pass
    # (round-63 review).
    missing = sorted(name for name in DOCTOR_GATES
                     if name != 'local_cli_routes'
                     and (not isinstance(report.get(name), dict) or report[name].get('ok') is not True))
    if not isinstance(report.get('local_cli_routes'), dict) or report['local_cli_routes'].get('ok') is not False:
        missing.append('local_cli_routes')
    failing = sorted(name for name, check in report.items()
                     if isinstance(check, dict) and check.get('ok') is False and name not in DOCTOR_NON_GATING)
    require(report.get('ok') is False and isinstance(paths, dict) and paths and all(paths.values())
            and not missing and failing == ['local_cli_routes'],
            'old release doctor failed beyond backend route readiness: '
            + (', '.join(failing + ['缺少 ' + name for name in missing]) or 'paths 或无线路失败项'))
    problems = report['local_cli_routes'].get('problems') or []
    return {'doctor_ok': False, 'old_route_outage': [str(item)[:300] for item in problems],
            'old_non_gating_failures': non_gating}


def entry_checks(layout: Layout, system: System, version: str, allow_route_outage: bool = False) -> dict:
    results = []
    for home in layout.entries:
        require(default_config(home) == layout.config, 'entrypoint config binding differs')
        result = system.json_command([str(home / 'scripts/verify-release')])
        require(result.get('current_release') == version and result.get('expected_version') == version, 'entrypoint version differs')
        require(not result.get('acceptance_mode'), 'entrypoint is in acceptance mode')
        require(result.get('config_path') == str(layout.config), 'entrypoint uses a different config')
        require(result.get('release_source_manifest_sha256') == result.get('runtime_source_manifest_sha256'), 'entrypoint source binding differs')
        results.append(result)
    return {'entries': results, **doctor_check(layout, system, allow_route_outage)}


# 内容出包：配置里这三个路径只允许指到助理目录下的固定位置，别的值一律拒绝。
CONTENT_PATHS = {'skills.root_dir': 'skills', 'skills.index_path': 'skills/index.json',
                 'knowledge.index_path': '系统/价值文件索引/index.json'}


def config_get(raw: dict, dotted: str) -> Any:
    section, _, key = dotted.partition('.')
    return raw.get(section, {}).get(key)


def config_step(layout: Layout, step: dict) -> tuple:
    """(字段, 执行前应有的值, 执行后的值)。只认已审查的两类：退役自动动作、内容路径。"""
    execute, check = step['execute'], step['check']
    path = execute.get('path')
    require(check.get('kind') == 'config_value_equals' and check.get('path') == path, 'config step check is incomplete')
    if path == 'recovery.automatic_actions':
        require(execute.get('value') == [] and set(execute) == {'kind', 'path', 'value'} and set(check) == {'kind', 'path', 'value'},
                'step cannot change authentication/models/routes or revive automatic actions')
        return path, check['value'], []
    require(path in CONTENT_PATHS and set(execute) == {'kind', 'path', 'assistant_path'}
            and execute['assistant_path'] == CONTENT_PATHS[path],
            'step cannot change authentication/models/routes or revive automatic actions')
    target = layout.assistant / relative(execute['assistant_path'])
    safe(target)
    require(target.exists(), f'content is not in place for {path}: {target}')
    if set(check) == {'kind', 'path', 'runtime_path'}:
        before = str(layout.runtime / relative(check['runtime_path']))
    else:
        require(set(check) == {'kind', 'path', 'value'} and isinstance(check['value'], str), 'config step check is incomplete')
        before = check['value']
    return path, before, str(target)


def checked_spec(layout: Layout, old: dict, new: dict) -> dict:
    spec = load(layout.package / 'agentos' / SPEC)
    require(spec.get('schema_version') == 1 and isinstance(spec.get('steps'), list), 'unsupported deployment declaration')
    require(spec.get('from_source_manifest_sha256') == old['agentos_source_manifest_sha256'], 'deployment predecessor binding mismatch')
    require(old['database_schema_version'] == new['database_schema_version'], 'schema migration is outside this release')
    history = load(layout.history) if layout.history.exists() else {'steps': {}}
    require(isinstance(history.get('steps'), dict), 'invalid applied-step history')
    # Validate EVERY historical record against its original immutable package.
    index = load(layout.index)
    for step_id, record in history['steps'].items():
        matches = [r for r in index['releases'] if r['version'] == record['release']]
        require(len(matches) == 1, 'applied-step release disappeared')
        original = layout.index.parent / relative(matches[0]['path'])
        require(state(original / 'FILES.sha256')['sha256'] == matches[0]['files_manifest_sha256'], 'historical package manifest changed')
        verify_package(original)
        declaration = load(original / 'agentos' / SPEC)
        found = [s for s in declaration['steps'] if s['id'] == step_id]
        require(len(found) == 1 and digest(encoded(found[0])) == record['sha256'], 'previously executed step changed')
    ids = []
    for step in spec['steps']:
        require(isinstance(step, dict) and re.fullmatch(r'\d{4}-[a-z0-9-]+', str(step.get('id', ''))), 'invalid numbered step')
        require(step['id'] not in ids and all(k in step for k in ('check', 'execute', 'undo')), 'step is incomplete or duplicate')
        ids.append(step['id'])
        require(step['undo'] == {'kind': 'restore_in_memory'}, 'step undo must use in-memory journal')
        require(step['execute'].get('kind') in ('install_absent_file', 'config_set'), 'unreviewed step operation')
        destination = step_destination(layout, step)
        check = step['check']
        if step['execute']['kind'] == 'config_set':
            # Only reviewed handlers exist; arbitrary config patching is never accepted.
            path, before_value, after_value = config_step(layout, step)
            expected_value = after_value if step['id'] in history['steps'] else before_value
            require(config_get(load(layout.config), path) == expected_value, 'config step input/result differs')
        else:
            payload = step_payload(layout, step)
            require(check == {'kind': 'absent_or_identical', 'sha256': digest(payload)}, 'step check and execution differ')
        if step['id'] in history['steps']:
            require(digest(encoded(step)) == history['steps'][step['id']]['sha256'], 'executed step has been modified')
            if step['execute']['kind'] == 'install_absent_file':
                require(state(destination)['sha256'] == digest(payload), 'executed step result drifted')
        else:
            if step['execute']['kind'] == 'install_absent_file':
                require(state(destination)['sha256'] in (None, digest(payload)), 'step would overwrite existing content')
    require(ids == sorted(ids), 'steps must be in numbered order')
    return spec


def step_destination(layout: Layout, step: dict) -> Path:
    execute = step['execute']
    if execute.get('kind') == 'config_set':
        return layout.config
    require(execute.get('root') == 'assistant', 'step target root is outside implemented contract')
    path = relative(execute['path'])
    require(path.parts[0] in ('skills', '工具', '系统'), 'step destination is protected')
    require(not any(part.lower() in ('config', '配置', 'auth', 'credentials') for part in path.parts), 'step cannot write credentials/config')
    destination = layout.assistant / path
    safe(destination)
    return destination


def step_payload(layout: Layout, step: dict) -> bytes:
    path = layout.package / 'agentos' / relative(step['execute']['source'])
    safe(path)
    return path.read_bytes()


def vault_actions(layout: Layout, previous: Path) -> list:
    base = files(previous / 'second-brain')
    incoming = files(layout.package / 'second-brain')
    released = set(base) - set(incoming)
    if released:
        # 交还给笔记库的笔记：原地保留，此后发布不再生成、合并或覆盖。只认随包声明的清单。
        handover = load(layout.package / 'agentos/docs/second_brain/handover.json')
        require(handover.get('schema_version') == 1 and released <= set(handover.get('notes') or []),
                'deleted vault notes require a separately reviewed release step')
    actions = []
    for rel, sha in incoming.items():
        destination = layout.vault / relative(rel)
        live = state(destination)
        if base.get(rel) == sha or live['sha256'] == sha:
            continue  # Leave live user additions alone when generated content is unchanged.
        require(not rel.startswith('70_复利规则/经验/'), 'protected experience would be changed')
        if rel not in base:
            require(live['sha256'] is None, f'new generated note would overwrite live content: {rel}')
            payload = (layout.package / 'second-brain' / rel).read_bytes()
        else:
            require(not rel.startswith('历史版本记录/'), 'existing historical note would be changed')
            payload = (layout.package / 'second-brain' / rel).read_bytes()
            if live['sha256'] != base[rel]:
                original = (previous / 'second-brain' / rel).read_bytes()
                current = destination.read_bytes() if live['sha256'] is not None else b''
                require(original.endswith(b'\n') and current.startswith(original),
                        f'generated note was edited; refusing overwrite: {rel}')
                # Preserve appended human content byte-for-byte; never fuzzy-merge edited bodies.
                require(payload.endswith(b'\n'), 'generated note must end with newline')
                payload += current[len(original):]
        actions.append((destination, payload, live, None))
    return actions


def snapshot(layout: Layout, spec: dict, overrides=None, ignored=()) -> dict:
    # Virtual pre-phase view: only explicitly planned paths may be normalized.
    overrides = overrides or {}
    def file_state(path):
        return overrides.get(str(path), state(path))
    def tree(root, source=False):
        values = source_files(root) if source else files(root)
        for path in ignored:
            if path.is_relative_to(root): values.pop(path.relative_to(root).as_posix(), None)
        for name, value in overrides.items():
            path = Path(name)
            if not path.is_relative_to(root): continue
            rel = path.relative_to(root)
            if source and not (rel.parts[0] in SOURCE_DIRS or rel.as_posix() in SOURCE_FILES): continue
            if value['sha256'] is None: values.pop(rel.as_posix(), None)
            else: values[rel.as_posix()] = value['sha256']
        return digest(encoded(values))
    result = {name: tree(root, name == 'runtime') for name, root in
              [('runtime', layout.runtime), ('vault', layout.vault), ('graph', layout.graph)]}
    for name, path in [('index', layout.index), ('config', layout.config), ('plist', layout.plist), ('history', layout.history)]:
        result[name] = file_state(path)
    for i, home in enumerate(layout.entries): result['entry-' + str(i)] = tree(home)
    for step in spec['steps']: result['step-' + step['id']] = file_state(step_destination(layout, step))
    result['runtime-manifest'] = file_state(layout.runtime / 'SOURCE_MANIFEST.json')
    result['package'] = tree(layout.package)
    return result


def phase_actions(layout, data, name, intent=None):
    """Reconstruct NEW bytes only; the journal never stores config or old bytes."""
    actions = {}
    intent = intent or {}
    def add(path, payload, mode=None):
        before = intent.get(str(path), {}).get('before', state(path))
        actions[str(path)] = (path, payload, before, mode)
    if name == 'stage_package':
        for rel, sha in files(layout.package).items():
            source = layout.package / rel
            payload = source.read_bytes()
            require(digest(payload) == sha, 'package changed during staging')
            target = data['target'] / rel
            if not intent: require(not target.exists(), f'unexpected staged file: {target}')
            add(target, payload, source.stat().st_mode & 0o777)
    elif name == 'replace_source':
        source = data['target'] / 'agentos'
        incoming = verify_source(source)
        for rel in sorted(set(data['runtime_files']) | set(incoming)):
            path = layout.runtime / rel
            changed = data['runtime_files'].get(rel) != incoming.get(rel)
            mode = (source / rel).stat().st_mode & 0o777 if changed and rel in incoming else None
            add(path, (source / rel).read_bytes() if rel in incoming else None, mode)
        add(layout.runtime / 'SOURCE_MANIFEST.json', (source / 'SOURCE_MANIFEST.json').read_bytes())
    elif name == 'install_entries':
        for home in layout.entries:
            for entry in ('xiaoba', 'verify-release'):
                template = (data['target'] / 'interface/scripts' / entry).read_text()
                require(len(DEFAULT_LINE.findall(template)) == 1, 'entrypoint template config binding is ambiguous')
                payload = DEFAULT_LINE.sub(lambda _: f'DEFAULT_CONFIG = Path({json.dumps(str(layout.config), ensure_ascii=False)})', template).encode()
                add(home / 'scripts' / entry, payload, 0o755)
            add(home / 'SKILL.md', (data['target'] / 'interface/SKILL.md').read_bytes())
    elif name == 'bind_config':
        config = load(layout.config)
        config['release']['expected_version'] = data['new']['version']
        add(layout.config, encoded(config), 0o600)
    elif name == 'release_steps':
        history = load(layout.history) if layout.history.exists() else {'steps': {}}
        config = load(layout.config)
        for step in data['spec']['steps']:
            if step['id'] in history['steps']: continue
            target = step_destination(layout, step)
            if step['execute']['kind'] == 'config_set':
                path, before_value, after_value = config_step(layout, step)
                require(config_get(config, path) in (before_value, after_value), 'config step input changed')
                section, _, key = path.partition('.')
                config.setdefault(section, {})[key] = after_value
                add(target, encoded(config), 0o600)
            else:
                payload = step_payload(layout, step)
                require(state(target)['sha256'] in (None, digest(payload)), f'step would overwrite user content: {target}')
                if state(target)['sha256'] is None: add(target, payload)
            history['steps'][step['id']] = {'sha256': digest(encoded(step)), 'release': data['new']['version']}
        if data['spec']['steps']: add(layout.history, encoded(history), 0o600)
    elif name == 'vault_merge':
        # Already-replaced notes can include a human appendix. Exclude them from
        # merge planning, then reuse their verified new bytes below.
        pending = {key: value for key, value in intent.items() if state(Path(key)) != value['after']}
        if intent:
            for key in pending:
                # Plan one note against the previous generated prefix.
                path = Path(key); rel = path.relative_to(layout.vault)
                payload = (layout.package / 'second-brain' / rel).read_bytes()
                original = data['previous'] / 'second-brain' / rel
                if path.exists() and original.exists():
                    old = original.read_bytes(); current = path.read_bytes()
                    require(current.startswith(old), f'generated note was edited: {path}')
                    payload += current[len(old):]
                add(path, payload)
        else:
            for path, payload, before, mode in vault_actions(layout, data['previous']): add(path, payload, mode)
    elif name == 'rebuild_graph':
        for rel, value in build_graph(layout).items(): add(layout.graph / relative(rel), graph_bytes(value), 0o600)
    elif name == 'publish_index':
        index = load(layout.index)
        index['current_release'] = data['new']['version']
        if not any(r['version'] == data['new']['version'] for r in index['releases']):
            index['releases'].insert(0, {'version': data['new']['version'], 'path': data['new']['version'],
                                       'status': 'verified_immutable', 'files_manifest_sha256': state(data['target'] / 'FILES.sha256')['sha256']})
        add(layout.index, encoded(index))
    for key, value in intent.items():
        if key not in actions and state(Path(key)) == value['after']:
            path = Path(key)
            add(path, path.read_bytes() if path.exists() else None, value['after']['mode'])
    return list(actions.values())


def action_intent(actions):
    return {str(path): {'before': before, 'after':
            {'sha256': digest(payload) if payload is not None else None,
             'mode': (mode or before['mode'] or 0o600) if payload is not None else None}}
            for path, payload, before, mode in actions}


def recovery_scene(layout, record):
    intent = record.get('phase_files', {}) if record.get('active_phase') else {}
    leftovers = []
    for key, value in intent.items():
        path = Path(key)
        require(state(path) in (value['before'], value['after']), f'phase file differs from old/new content: {path}')
        for pid in record.get('writer_pids', []):
            temp = path.with_name('.' + path.name + f'.switch-{pid}')
            safe(temp)
            if temp.exists():
                require(temp.is_file(), f'not a regular switch temporary: {temp}')
                leftovers.append(temp)
    require(snapshot(layout, record['spec'], {key: value['before'] for key, value in intent.items()}, leftovers)
            == record['checkpoint'], 'scene differs from last durable checkpoint; manual reconciliation required')
    if record.get('active_phase') == 'stage_package':
        target = Path(record['target'])
        if target.exists():
            actual = set(files(target)) - {p.relative_to(target).as_posix() for p in leftovers}
            allowed = {Path(p).relative_to(target).as_posix() for p in intent}
            require(actual <= allowed, 'unexpected files in staged recovery package: ' + str(sorted(actual - allowed)))
    return leftovers


def preserve_leftovers(layout, paths):
    moved = []
    for path in paths:
        root = layout.runtime / '.runtime/switch-leftovers'
        safe(root)
        root.mkdir(mode=0o700, exist_ok=True)
        # Unique directory also makes a kill between moves safely repeatable.
        directory = Path(tempfile.mkdtemp(prefix='switch-', dir=root))
        target = directory / path.name
        os.rename(path, target)
        moved.append(str(target))
    return moved


def inspect(layout: Layout, system: System) -> dict:
    index = load(layout.index)
    matches = [r for r in index['releases'] if r['version'] == index['current_release']]
    require(len(matches) == 1, 'current release entry is not unique')
    previous = layout.index.parent / relative(matches[0]['path'])
    old = verify_package(previous)
    require(old['version'] == index['current_release'], 'previous release version differs from index')
    require(state(previous / 'FILES.sha256')['sha256'] == matches[0]['files_manifest_sha256'], 'previous package binding differs')
    new = verify_package(layout.package)
    require(new['status'] == 'verified_immutable_release' and new.get('final_publish_requires_human') is True, 'target is not an accepted immutable package')
    require(new['tests']['result'] == 'passed', 'target lacks a passed test record')
    if release_tier(previous, layout.package, old, new) == 'patch':
        # 补丁档不过两个后台；分档只比较 agentos 源码树，所以包里其余可执行内容必须与上一版逐字节相同。
        require(interface_scripts(layout.package) == interface_scripts(previous), 'patch-tier package changes interface scripts')
        require(state(layout.package / 'interface/SKILL.md')['sha256'] == state(layout.package / 'agentos/.agents/skills/xiaoba-agentos/SKILL.md')['sha256'], 'patch-tier interface card differs from reviewed source')
    else:
        require(independent_acceptance(new['acceptance'].get('end_to_end_evidence') or {}), 'target lacks required independent acceptance')
    require(new['version'] != old['version'], 'target is already active')
    require(not any(r['version'] == new['version'] for r in index['releases']), 'target version is already indexed')
    target = layout.index.parent / relative(new['version'])
    require(not target.exists(), f'target immutable release directory already exists: {target}; if an abandoned switch left it, check it and move it away before a new preflight')
    runtime_files = verify_source(layout.runtime)
    require(state(layout.runtime / 'SOURCE_MANIFEST.json')['sha256'] == old['agentos_source_manifest_sha256'], 'runtime is not bound to previous release')
    config = load(layout.config)
    require(config['release']['expected_version'] == old['version'] and not config['release'].get('acceptance_mode'), 'active config is not previous formal release')
    require(Path(config['project_dir']) == layout.runtime and Path(config['release']['index_path']) == layout.index, 'active config layout differs')
    spec = checked_spec(layout, old, new)
    plist = plistlib.loads(layout.plist.read_bytes())
    require(plist['Label'] == layout.label, 'worker plist label differs')
    require(plist.get('EnvironmentVariables', {}).get('XIAOBA_CONFIG') == str(layout.config), 'worker plist config differs')
    require(Path(plist['WorkingDirectory']) == layout.runtime, 'worker plist working directory differs')
    queue_state = queue(layout)
    current_worker = worker(layout, system)
    checks = entry_checks(layout, system, old['version'], allow_route_outage=True)
    verify_graph(layout)
    vault = vault_actions(layout, previous)
    return {'old': old, 'new': new, 'previous': previous, 'spec': spec, 'target': target,
            'runtime_files': runtime_files, 'worker': current_worker, 'queue': queue_state,
            'checks': checks, 'vault': vault}


def plan(layout: Layout, system: Optional[System] = None, now: Callable = time.time) -> dict:
    system = system or System()
    result = {'mode': 'plan', 'ok': False, 'writes_performed': 0, 'generated_at': now()}
    try:
        data = inspect(layout, system)
        result.update(ok=True, previous_version=data['old']['version'], version=data['new']['version'],
                      queue=data['queue'], worker=data['worker'], checks=data['checks'],
                      vault_changed_notes=[str(p.relative_to(layout.vault)) for p, *_ in data['vault']],
                      numbered_steps=[s['id'] for s in data['spec']['steps']],
                      fingerprint=snapshot(layout, data['spec']),
                      phases=['stage_package', 'stop_worker', 'replace_source', 'install_entries', 'bind_config',
                              'release_steps', 'vault_merge', 'rebuild_graph', 'publish_index', 'start_worker', 'readback'])
    except (SwitchError, OSError, ValueError, KeyError, sqlite3.Error) as exc:
        result['error'] = str(exc)
    return result


def preflight(layout: Layout, system: Optional[System] = None, now: Callable = time.time) -> dict:
    result = plan(layout, system, now)
    result['mode'] = 'preflight'
    if result['ok']:
        result['issued_at'] = now()
        result['expires_at'] = result['issued_at'] + TTL
        result['preflight_sha256'] = digest(encoded(result))
    return result


def readback(layout: Layout, system: Optional[System] = None, old_pid: Optional[int] = None,
             allow_route_outage: bool = False) -> dict:
    """Read the current release back. The newly activated release must pass doctor
    outright; reading the previous release back after an undo applies the same
    narrow route-outage rule as preflight did for it (round-48 rehearsal)."""
    system = system or System()
    result = {'mode': 'readback', 'ok': False, 'writes_performed': 0}
    try:
        index = load(layout.index)
        matches = [r for r in index['releases'] if r['version'] == index['current_release']]
        require(len(matches) == 1, 'current release entry is not unique')
        package = layout.index.parent / relative(matches[0]['path'])
        release = verify_package(package)
        require(release['version'] == index['current_release'], 'package/index version mismatch')
        require(state(package / 'FILES.sha256')['sha256'] == matches[0]['files_manifest_sha256'], 'current package binding mismatch')
        verify_source(layout.runtime)
        result['checks'] = entry_checks(layout, system, release['version'], allow_route_outage)
        for home in layout.entries:
            require(state(home / 'SKILL.md')['sha256'] == state(package / 'interface/SKILL.md')['sha256'], 'installed skill card mismatch')
        result['worker'] = worker(layout, system, old_pid)
        verify_graph(layout)
        result['knowlp_verified'] = True
        result.update(ok=True, version=release['version'], old_pid=old_pid)
        if old_pid is None:
            result['restart_verified'] = False
            result['restart_note'] = '未提供切换前 PID，只核验当前运行态'
        else:
            result['restart_verified'] = True
    except (SwitchError, OSError, ValueError, KeyError, sqlite3.Error) as exc:
        result['error'] = str(exc)
    return result


def build_graph(layout: Layout) -> dict:
    p = layout.runtime / 'scripts/build_knowlp_index.py'
    safe(p)
    # Read once, compare those bytes with the verified manifest, execute the
    # same in-memory object. Replacing the path after verification cannot run it.
    manifest = verify_source(layout.runtime)
    payload = p.read_bytes()
    require(digest(payload) == manifest['scripts/build_knowlp_index.py'], 'index builder changed after verification')
    namespace = {'__file__': str(p), '__name__': 'switch_knowlp'}
    exec(compile(payload, str(p), 'exec'), namespace)
    require(namespace['diagnose'](layout.vault)['ok'], 'vault tree validation failed')
    result = namespace['build'](layout.vault)
    require(result == namespace['build'](layout.vault), 'index builder is nondeterministic')
    return result


def graph_bytes(value: Any) -> bytes:
    # Keep the canonical renderer of build_knowlp_index.py; key sorting would
    # change its serialized format and fail the official --verify command.
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def verify_graph(layout: Layout) -> None:
    for rel, value in build_graph(layout).items():
        path = layout.graph / relative(rel)
        safe(path)
        require(path.read_bytes() == graph_bytes(value), f'KnowLP index differs: {rel}')


def recovery_path(layout: Layout) -> Path:
    return layout.runtime / '.runtime/switch-recovery.json'


def recovery_status(layout: Layout) -> dict:
    path = recovery_path(layout)
    if not path.exists():
        return {'ok': True, 'mode': 'recover', 'pending': False}
    record = load(path)
    return {'ok': True, 'mode': 'recover', 'pending': record.get('status') == 'pending',
            'status': record.get('status'), 'active_phase': record.get('active_phase'),
            'completed_steps': record['completed_steps'], 'version': record['new']['version'],
            'last_completed_phase': record['completed_steps'][-1] if record['completed_steps'] else 'prepared',
            'note': '仅记录阶段和哈希；resume 核对现场后继续，不自动执行或批准'}


def abandon(layout: Layout, system: Optional[System] = None) -> dict:
    system = system or System()
    fd = os.open(layout.runtime / '.runtime', os.O_RDONLY)
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'ok': False, 'mode': 'abandon', 'error': 'another switch is running', 'writes_performed': 0}
        path = recovery_path(layout)
        before = state(path)
        record = load(path)
        require(record.get('status') == 'pending', 'no interrupted switch to abandon')
        result = recovery_status(layout)
        pids = system.worker_pids(layout)
        result.update(mode='abandon', worker_running=bool(pids), worker_pids=pids,
                      worker_service_registered=system.registered(layout))
        known = {**record.get('written_files', {}), **record.get('phase_files', {})}
        result['new_content_paths'] = [key for key, value in known.items()
                                       if value['after']['sha256'] is not None and state(Path(key)) == value['after']]
        record['status'] = 'abandoned'
        atomic_bytes(path, encoded(record), expected=before)
        target = Path(record['target'])
        steps = []
        if not result['worker_running']:
            steps.append('Worker 已停，且不会自动启动；先确认运行目录和入口是哪个版本的内容，再决定启动。')
        if result['new_content_paths'] and record.get('completed_steps') != ['stage_package'] and record.get('active_phase') != 'stage_package':
            steps.append('上面这些文件已是新内容，没有回滚；预检通过前要把它们恢复一致，旧内容可从上一版发布包核对：' + record['previous'])
        if target.exists():
            steps.append('预检不接受已存在的目标目录，确认后把它移走：' + str(target))
        result.update(status='abandoned', pending=False, writes_performed=1, next_steps=steps,
                      note='仅放弃恢复记录；现场未回滚，新的 apply 仍须通过预检。')
        return result
    finally:
        os.close(fd)


def apply(layout: Layout, ticket: dict, system: Optional[System] = None,
          now: Callable = time.time, fault: Optional[Callable] = None) -> dict:
    return _locked_apply(layout, ticket, system, now, fault, False)


def resume(layout: Layout, system: Optional[System] = None,
           now: Callable = time.time, fault: Optional[Callable] = None) -> dict:
    return _locked_apply(layout, {}, system, now, fault, True)


def _locked_apply(layout, ticket, system, now, fault, recovering):
    # The lock is an existing runtime directory, so no extra lock file and no
    # backup is created. Both fresh apply and resume share this exclusion.
    fd = os.open(layout.runtime / '.runtime', os.O_RDONLY)
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'ok': False, 'error': 'another switch is running', 'writes_performed': 0}
        if recovering:
            record = load(recovery_path(layout))
            require(record.get('status') == 'pending', 'no interrupted switch to resume')
        else:
            if recovery_path(layout).exists() and load(recovery_path(layout)).get('status') == 'pending':
                return {'ok': False, 'error': 'interrupted switch pending; use recover / resume', 'writes_performed': 0}
            record = None
        return _apply(layout, ticket, system, now, fault, record)
    finally:
        os.close(fd)


def _apply(layout: Layout, ticket: dict, system: Optional[System] = None,
           now: Callable = time.time, fault: Optional[Callable] = None, recovery=None) -> dict:
    """Only the user invokes this against production; tests inject isolated Layout/System."""
    system = system or System()
    result = {'mode': 'apply', 'ok': False, 'activated': False, 'writes_performed': 0,
              'completed_steps': [], 'rollback': None}
    writer = Writer()
    data = None
    stopped = False
    attempted_start = False
    old_pid = None
    boundary = None
    original_snapshot = None
    journal = recovery
    resumed = recovery is not None
    completed = list(recovery['completed_steps']) if resumed else []

    def done(name):
        return name in completed

    def begin(name):
        saved = journal.get('phase_files', {}) if journal.get('active_phase') == name else {}
        actions = phase_actions(layout, data, name, saved)
        intent = action_intent(actions)
        if saved: require(intent == saved, 'reconstructed phase plan differs')
        journal['active_phase'] = name
        journal['phase_files'] = intent
        journal['writer_pids'] = sorted(set(journal.get('writer_pids', []) + [os.getpid()]))
        atomic_bytes(recovery_path(layout), encoded(journal))
        for path, payload, before, mode in actions:
            current = state(path)
            require(current in (before, intent[str(path)]['after']), f'phase file changed: {path}')
            if current != intent[str(path)]['after']:
                writer.write(path, payload, before, mode)


    def phase(name: str) -> None:
        result['completed_steps'].append(name)
        if name.startswith('step:'):
            if fault: fault(name)
            return
        if name not in completed: completed.append(name)
        journal['completed_steps'] = list(completed)
        journal.setdefault('written_files', {}).update(journal.get('phase_files', {}))
        journal['phase_files'] = {}
        journal['active_phase'] = None
        journal['checkpoint'] = snapshot(layout, data['spec'])
        atomic_bytes(recovery_path(layout), encoded(journal))
        if fault:
            fault(name)

    try:
        if resumed:
            require(recovery['package'] == str(layout.package), 'recovery package path differs')
            require(recovery['layout'] == [str(p) for p in (layout.config, layout.vault, layout.graph, layout.plist, *layout.entries)], 'recovery layout differs')
            verify_package(layout.package)
            data = {k: recovery[k] for k in ('old', 'new', 'spec', 'runtime_files', 'worker')}
            data.update(previous=Path(recovery['previous']), target=Path(recovery['target']))
            verify_package(data['previous'])
            if done('stage_package'):
                verify_package(data['target'])
                require((data['target'] / 'FILES.sha256').read_bytes() == (layout.package / 'FILES.sha256').read_bytes(), 'staged recovery package differs')
            leftovers = recovery_scene(layout, recovery)
            original_snapshot = recovery['original_snapshot']
            old_pid = data['worker']['pid']
            stopped = done('stop_worker')
            attempted_start = done('start_worker')
            if stopped and not attempted_start:
                require(not system.registered(layout) and not system.worker_pids(layout),
                        'worker restarted during interrupted switch; manual reconciliation required')
            queue(layout)
            result['old_pid'] = old_pid
            result['completed_steps'] = list(completed)
            result['preserved_leftovers'] = preserve_leftovers(layout, leftovers)
        else:
            require(ticket.get('mode') == 'preflight' and ticket.get('ok') is True, 'successful preflight required')
            signed = dict(ticket)
            sha = signed.pop('preflight_sha256', None)
            require(digest(encoded(signed)) == sha, 'preflight record was modified')
            require(0 <= now() - ticket['issued_at'] <= TTL and ticket['expires_at'] == ticket['issued_at'] + TTL, 'preflight expired or timestamp invalid')
            data = inspect(layout, system)
            require(ticket['fingerprint'] == snapshot(layout, data['spec']), 'scene changed after preflight')
            require(ticket['worker'] == data['worker'], 'worker changed after preflight')
            require(ticket['version'] == data['new']['version'], 'preflight target mismatch')
            require(now() <= ticket['expires_at'], 'preflight expired during final inspection')
            original_snapshot = snapshot(layout, data['spec'])
            old_pid = data['worker']['pid']
            result['old_pid'] = old_pid

            journal = {k: data[k] for k in ('old', 'new', 'spec', 'runtime_files', 'worker')}
            journal.update(package=str(layout.package), previous=str(data['previous']), target=str(data['target']),
                           layout=[str(p) for p in (layout.config, layout.vault, layout.graph, layout.plist, *layout.entries)],
                           original_snapshot=original_snapshot, checkpoint=original_snapshot, completed_steps=[], status='pending')
            atomic_bytes(recovery_path(layout), encoded(journal))

        if not done('stage_package'):
            begin('stage_package')
            verify_package(data['target'])
            phase('stage_package')
        if not done('stop_worker'):
            begin('stop_worker')
            require(snapshot(layout, data['spec']) == original_snapshot, 'scene changed before stopping worker')
            queue(layout)
            stopped = True  # Set before stop: a successful bootout followed by timeout needs restoration.
            # A kill can land between bootout and its checkpoint; a second bootout
            # of an unloaded service fails, so resume only stops what is still up.
            if not resumed or system.registered(layout) or system.worker_pids(layout):
                system.stop(layout, old_pid)
            require(not system.alive(old_pid), 'old worker remained after stop')
            queue(layout)
            phase('stop_worker')
        for name in ('replace_source', 'install_entries', 'bind_config', 'release_steps',
                     'vault_merge', 'rebuild_graph', 'publish_index'):
            if done(name): continue
            # Before starting a fresh phase, all previous checkpoint data remains strict.
            if journal.get('active_phase') != name:
                require(snapshot(layout, data['spec']) == journal['checkpoint'],
                        'scene changed after checkpoint before ' + name)
            begin(name)
            if name == 'release_steps':
                for step in data['spec']['steps']: phase('step:' + step['id'])
            if name == 'rebuild_graph': verify_graph(layout)
            phase(name)
        if not done('start_worker'):
            begin('start_worker')
            require(state(layout.plist) == original_snapshot['plist'], 'worker service template changed after preflight')
            queue(layout)
            attempted_start = True
            boundary = now()
            system.start(layout)

            def ready() -> bool:
                try:
                    value = worker(layout, system, old_pid)
                    return value['started_at'] >= boundary
                except (SwitchError, OSError, ValueError, KeyError):
                    return False

            system.wait(ready, 'new worker did not become ready')
            phase('start_worker')
        # Always verify the running release, including a resumed readback.
        begin('readback')
        result['read_back'] = readback(layout, system, old_pid)
        require(result['read_back']['ok'], 'post-switch readback failed: ' + str(result['read_back'].get('error')))
        require(result['read_back']['version'] == data['new']['version'], 'readback is healthy but not the requested target version')
        phase('readback')
        result.update(ok=True, activated=True, new_pid=result['read_back']['worker']['pid'])
        # 切换已成功、不会再退回。下面两步无论抛出什么异常都只报告：落到外层
        # except 会给已激活的结果再写一条 error，甚至回滚已成功的切换（第31/35轮审查）。
        # 包含用户中断或进程退出异常；只有成功读回前的失败仍进入下方回滚。
        # 新包不再带的受管目录此时只剩空壳，清掉。只删空目录，删不掉就留着并报告。
        try:
            result['removed_empty_directories'] = prune_empty(layout.runtime, data['target'] / 'agentos')
        except BaseException as exc:
            result['removed_empty_directories'] = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
        # 发布目录设为只读：之后任何进程都不能往里写东西（例如 Python 的字节码缓存），
        # 多一个文件就会让入口的发布校验失败。封不上只报告，不退回。
        try:
            result['release_sealed'] = seal_release(data['target'])
        except BaseException as exc:
            result['release_sealed'] = {'ok': False, 'entries': 0, 'errors': [f'{type(exc).__name__}: {exc}']}
    except BaseException as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
        if resumed:
            result['rollback'] = {'ok': False, 'status': '恢复未完成，需要人工核对或重新 resume', 'errors': [str(exc)]}
        elif writer.changes or stopped:
            errors = []
            # Stop new code before restoring files. Do not launch mixed code after undo failure.
            if attempted_start:
                try:
                    known_pid = None
                    try:
                        current = load(layout.worker)
                        if system.alive(int(current['pid'])):
                            known_pid = int(current['pid'])
                    except (OSError, ValueError, KeyError, SwitchError):
                        pass
                    if system.registered(layout):
                        system.stop(layout, known_pid)
                    require(not system.worker_pids(layout), 'unregistered new worker survived; undo is unsafe')
                except BaseException as stop_exc:
                    errors.append('stop new worker before undo failed: ' + str(stop_exc))
            if not errors:
                errors += writer.undo()
                errors += writer.remove_dirs()
            if not errors and data and original_snapshot:
                try:
                    require(snapshot(layout, data['spec']) == original_snapshot, 'restored scene fingerprint differs')
                    if stopped:
                        if not system.alive(old_pid):
                            system.start(layout)
                            system.wait(lambda: rollback_worker_ready(layout, system), 'previous worker did not recover')
                        else:
                            worker(layout, system)
                    old_readback = readback(layout, system, allow_route_outage=True)
                    require(old_readback['ok'] and old_readback['version'] == data['old']['version'], 'previous release readback failed')
                    result['rollback_read_back'] = old_readback
                except BaseException as rollback_exc:
                    errors.append(str(rollback_exc))
            result['rollback'] = {'ok': not errors, 'status': '已退回' if not errors else '退回失败，需要人工处理', 'errors': errors}
        else:
            result['rollback'] = {'ok': True, 'status': '未开始写入，无需退回', 'errors': []}
    finally:
        result['writes_performed'] = writer.write_count
        if journal is not None and (result['ok'] or (not resumed and result.get('rollback', {}).get('ok'))):
            journal['status'] = 'complete' if result['ok'] else 'rolled_back'
            try:
                atomic_bytes(recovery_path(layout), encoded(journal))
            except OSError as journal_error:
                result['recovery_journal_error'] = str(journal_error)
                result['requires_manual_readback'] = True
        result['recovery_limit'] = '阶段与哈希持久化；原内容只在内存，未创建备份。仅活动阶段允许计划内旧/新文件混合，其它偏离拒绝续跑。'
        # Never retain old bytes beyond this invocation.
        writer.changes.clear()
    return result


def seal_release(package: Path) -> dict:
    """Make an activated release read-only: files 0444 (0555 if executable), directories 0555."""
    sealed, errors = 0, []
    try:
        entries = sorted(package.rglob('*'), key=lambda p: len(p.parts), reverse=True)
        for path in entries + [package]:
            if path.is_symlink():
                errors.append('symlink in release: ' + str(path.relative_to(package)))
                continue
            mode = path.stat().st_mode
            target = 0o555 if path.is_dir() or mode & 0o111 else 0o444
            # Compare all permission bits, setuid/setgid/sticky included: comparing
            # only the low nine let a 04555 entry pass as sealed (round-63 review).
            if (mode & 0o7777) != target:
                os.chmod(path, target)
            if (path.stat().st_mode & 0o7777) != target:
                errors.append('mode not sealed: ' + str(path.relative_to(package)))
                continue
            sealed += 1
    except OSError as exc:
        errors.append(f'{type(exc).__name__}: {exc}')
    return {'ok': not errors, 'entries': sealed, 'errors': errors}


def prune_empty(runtime: Path, source: Path) -> list:
    removed = []
    for name in SOURCE_DIRS:
        top = runtime / name
        if (source / name).exists() or top.is_symlink() or not top.is_dir():
            continue
        if any(p.is_symlink() or not p.is_dir() for p in top.rglob('*')):
            continue  # 还有文件：不属于空壳，不碰（发布校验会把它报出来）
        for directory in sorted([top, *top.rglob('*')], key=lambda p: len(p.parts), reverse=True):
            try:
                directory.rmdir()
                removed.append(str(directory.relative_to(runtime)))
            except OSError:
                pass
    return sorted(removed)


def rollback_worker_ready(layout: Layout, system: System) -> bool:
    try:
        worker(layout, system)
        return True
    except (SwitchError, OSError, ValueError, KeyError):
        return False


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', nargs='?', default='plan', choices=('plan', 'preflight', 'apply', 'readback', 'recover', 'resume', 'abandon'))
    parser.add_argument('--package', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--preflight', type=Path, help='用户保存的只读预检 JSON，仅 apply 必需')
    parser.add_argument('--old-pid', type=int, help='readback 核验停启时提供切换前 PID')
    args = parser.parse_args(argv)
    try:
        layout = production(args.package)
        if args.mode == 'apply':
            require(args.preflight is not None, 'apply requires --preflight')
            # Require this command to be the exact command in the accepted target package.
            verify_package(args.package)
            require(state(Path(__file__))['sha256'] == state(args.package / 'agentos/scripts/switch_release.py')['sha256'], 'running command differs from accepted package')
            result = apply(layout, load(args.preflight))
        elif args.mode == 'abandon':
            result = abandon(layout)
        elif args.mode == 'recover':
            result = recovery_status(layout)
        elif args.mode == 'resume':
            verify_package(args.package)
            require(state(Path(__file__))['sha256'] == state(args.package / 'agentos/scripts/switch_release.py')['sha256'], 'running command differs from accepted package')
            result = resume(layout)
        elif args.mode == 'preflight':
            result = preflight(layout)
        elif args.mode == 'readback':
            result = readback(layout, old_pid=args.old_pid)
        else:
            result = plan(layout)
    except (SwitchError, OSError, ValueError, KeyError) as exc:
        result = {'mode': args.mode, 'ok': False, 'error': str(exc), 'writes_performed': 0}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
