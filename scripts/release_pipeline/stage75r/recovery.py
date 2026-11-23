"""Post-switch recovery boundary for stage75r.

Two strictly separated concerns:
  * recognize_core_switch: READ-ONLY recognition that the core switch already
    succeeded (single marker + activated apply + clean readback + PID continuity +
    live manifest + migration). It NEVER re-runs core apply. Missing or unactivated
    core evidence refuses recovery.
  * post_switch: the recoverable post boundary — re-pin the authorized file
    sync, atomically install (skip already-installed files so a partial post can
    resume), prove taskcards paths unchanged, compile the full same-source runtime
    set, then run the smoke (restart via synced scripts + roster + retest + task).

Ported by purpose from the accepted v1.6.14 post_switch_recovery1.py (0c28bd0c…)
and post-switch-v1.6.14-v1614-r7.py (49ea00ed…). No whole-tree backup; failed new
content staging is retained for the lead under _待删/.
"""
import json
import os
import stat
import time
from pathlib import Path

from common import StopError, atomic_dump, clean_env, read_json, sha, sha_bytes
from context_adapter import live_monitor_dir, monitor_source_dir, post_file_declaration, \
    post_files, run_migration_check
from monitor_contract import compare, sync_precheck
from post_lib import atomic_sync, classify_existing, compile_runtime_set, create_absent, \
    readback_synced, taskcards_proof
from post_smoke import resolve_base_url, run_smoke, validate_restart_before_claim
from switch_core import live_db_path, verify_ui_postimage

# B07 dependency order, bound to the AUTHORIZED identities, never to input order.
# Evidence (stable A02 draft, read-only): xiaoba_monitor.py:29 imports
# monitor_core.lifecycle; :38 imports monitor_core.modules.system (SystemModule);
# both .command entries invoke xiaoba_monitor.py (启动:13/25/32, 关闭:18/22).
_SYNC_RANK = {
    'monitor_core/lifecycle.py': 0,
    'monitor_core/modules/system.py': 1,
    'xiaoba_monitor.py': 2,
    '启动监看中心.command': 3,
    '关闭监看中心.command': 4,
}
_SYNC_EVIDENCE = {
    'monitor_core/lifecycle.py': 'xiaoba_monitor.py:29 from monitor_core import lifecycle',
    'monitor_core/modules/system.py': 'xiaoba_monitor.py:38 from monitor_core.modules.system import SystemModule',
    'xiaoba_monitor.py': 'main entry; both .command scripts invoke it (启动:13/25/32, 关闭:18/22)',
    '启动监看中心.command': 'invokes xiaoba_monitor.py; depends on module + main entry',
    '关闭监看中心.command': 'invokes xiaoba_monitor.py; depends on module + main entry',
}
_OLD_SYSTEM_NAME = 'monitor_core/modules/system.py'


def dependency_order(files):
    """B07: order the authorized sync set by dependency, not input iteration.

    A deterministic rank is assigned per declared identity; equal ranks fall back
    to the identity string. The old system.py authorization is retained and its
    position is evidenced by the real import above. Returns (ordered_files, proof).
    """
    items = list(files)
    names = [f['name'] for f in items]
    if len(set(names)) != len(names):
        raise StopError('STOP: duplicate authorized identity in sync order')
    if 'monitor_core/lifecycle.py' not in names:
        return items, {'order': names, 'legacy_input_order': True}
    unknown = set(names) - set(_SYNC_RANK)
    if unknown:
        raise StopError('STOP: sync order has no dependency rule for: %s' % sorted(unknown))
    if _OLD_SYSTEM_NAME not in names:
        raise StopError('STOP: old system.py authorization missing from sync set')
    ordered = sorted(items, key=lambda f: (_SYNC_RANK[f['name']], f['name']))
    return ordered, {'order': [f['name'] for f in ordered],
                     'rank': {n: _SYNC_RANK[n] for n in names},
                     'evidence': {n: _SYNC_EVIDENCE[n] for n in names},
                     'independent_of_input_order': True,
                     'old_system_authorization_retained': True}


def recognize_core_switch(ctx, switch_dir):
    """Read-only proof the core switch succeeded. Never applies anything."""
    switch_dir = Path(switch_dir)
    marker = switch_dir / 'SWITCH.started'
    if not marker.is_file():
        raise StopError('STOP: no core SWITCH marker; cannot recover without a succeeded core switch')
    result = read_json(switch_dir / 'switch-result.json')
    if result.get('ok') is not True or result.get('activated') is not True:
        raise StopError('STOP: core switch result not activated; recovery refused')
    if result.get('version') != ctx['version']:
        raise StopError('STOP: core switch version != manager version')
    applied = read_json(switch_dir / 'apply.json')
    if applied.get('ok') is not True or applied.get('activated') is not True:
        raise StopError('STOP: apply evidence not activated')
    readback = read_json(switch_dir / 'readback.json')
    if readback.get('ok') is not True or readback.get('writes_performed') != 0:
        raise StopError('STOP: readback evidence not clean')
    if readback.get('version') != ctx['version']:
        raise StopError('STOP: readback version != manager version')
    new_pid = result.get('new_pid')
    if (readback.get('worker') or {}).get('pid') != new_pid or applied.get('new_pid') != new_pid:
        raise StopError('STOP: PID continuity broken across apply/readback/result')
    if sha(Path(ctx['live']) / 'SOURCE_MANIFEST.json') != ctx['assigned']['source_manifest_sha256']:
        raise StopError('STOP: live source manifest differs from BASE_ASSIGNED; not the switched release')
    migration = run_migration_check(ctx, live_db_path(ctx))
    if not migration.get('ok'):
        raise StopError('STOP: migration readback failed; recovery refused')
    return {'ok': True, 'version': ctx['version'], 'old_pid': result.get('old_pid'),
            'new_pid': new_pid, 'migration': migration,
            'live_source_manifest_sha256': sha(Path(ctx['live']) / 'SOURCE_MANIFEST.json')}


def _sync_authorized(ctx, monitor_pin, out_dir, *, fault=None):
    """Atomically install the authorized files in DEPENDENCY order (B07), with a
    three-state decision per file and incremental, verifiable evidence (B08).

      * absent + first_install authorization -> exclusive atomic create (B05);
      * already at the export postimage -> skip, never re-write (resume);
      * at the pinned before-hash -> atomic replace, preserving installed mode (B06);
      * any third hash / wrong object type -> hard STOP; drifted bytes untouched.

    A readback (B07) is required after each step before the next step begins.
    Records are persisted after EACH step so an interruption leaves the exact
    completed set readable; re-entry reads the same declaration and pin (never a
    re-frozen authorization or a new first-install ticket) and only fills gaps.
    'fault' is a rehearsal seam: 'interrupt_after_<N>' stops after N steps with
    no real service or subprocess. Core switch and smoke are NEVER replayed here.
    """
    files = post_files(ctx)
    declaration = post_file_declaration(ctx)
    install = {row['name']: row['install'] for row in declaration['files']}
    ordered, order_proof = dependency_order(files)
    live_before = monitor_pin['live_before_sha256']
    export = monitor_pin['export_sha256']
    declared_export = {f['name']: f['expected_sha256'] for f in files}
    if export != declared_export or set(live_before) != set(declared_export):
        raise StopError('STOP: saved synchronization pin differs from authorized source')
    if any((live_before[name] is None) != (row['policy'] == 'first_install')
           for name, row in install.items()):
        raise StopError('STOP: saved synchronization preimages differ from installation policy')
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    records_path = out_dir / 'sync-records.json'
    records = []

    def persist():
        atomic_dump(records_path, {'records': records, 'order': order_proof,
                                   'completed_steps': len(records),
                                   'total_steps': len(ordered)}, overwrite=True)

    for step, f in enumerate(ordered, 1):
        name = f['name']
        target = f['live_path']
        after = export[name]
        is_first = install[name]['policy'] == 'first_install'
        try:
            target.lstat()
            present = True
        except FileNotFoundError:
            present = False
        if target.is_symlink():
            raise StopError('STOP: live post file symlinked: %s' % target)
        if not present:
            if not is_first:
                raise StopError('STOP: existing target missing before replace: %s' % target)
            data = f['source_path'].read_bytes()
            if sha_bytes(data) != after:
                raise StopError('STOP: exported post file drifted from record: %s' % name)
            declared_mode = int(install[name]['mode'], 8)
            record = create_absent(target, data, declared_mode, out_dir)
            record.update(file=name, status='synced_created')
        else:
            state = classify_existing(target, live_before.get(name), after)
            if state['state'] == 'target':
                record = {'file': name, 'status': 'already_synced',
                          'sha256': after, 'mode': '0o%o' % state['mode']}
            else:
                data = f['source_path'].read_bytes()
                if sha_bytes(data) != after:
                    raise StopError('STOP: exported post file drifted from record: %s' % name)
                record = atomic_sync(target, data, live_before[name], out_dir,
                                     expected_after=after)
                record.update(file=name, status='synced')
        expected_mode = int(install[name]['mode'], 8) if is_first else None
        record['readback'] = readback_synced(target, after, expected_mode)
        records.append(record)
        persist()                                  # evidence survives interruption
        if fault == 'interrupt_after_%d' % step:
            raise StopError('STOP: rehearsal interruption after step %d; %d/%d persisted'
                            % (step, len(records), len(ordered)))
    return records


def _read_smoke_ledger(switch_dir):
    """The smoke-attempt ledger is anchored to the CORE SWITCH dir (stable evidence
    of the one core switch), NOT to the arbitrary post out_dir. Changing out_dir
    therefore cannot hide a prior failed retest/submit and launder a retry."""
    path = Path(switch_dir) / 'smoke-attempts.json'
    if path.is_symlink():
        raise StopError('STOP: smoke attempt ledger symlinked: %s' % path)
    if not path.is_file():
        return {'attempts': []}
    return read_json(path)


def _read_claim(path):
    """Tolerant read of the pre-call smoke claim. A SIGKILL mid-write can leave the
    claim present but empty/partial; existence alone is the lock, so a parse failure
    still yields a refusal — never an unlock."""
    try:
        return read_json(path)
    except StopError:
        return {}


def _claim_smoke(switch_dir, out_dir, ctx):
    """Atomically claim the ONE smoke attempt for this core switch BEFORE any
    run_smoke action (card r2.1). Exclusive O_EXCL create anchored to the stable
    switch_dir, persisting this round's identity and the original out_dir:

      * two concurrent posts cannot both enter run_smoke — only one creates the claim;
      * a SIGKILL during run_smoke still leaves the claim, so a different out_dir
        cannot re-enter even though the attempt ledger is still empty;
      * an existing claim refuses before any smoke, even if the original process exited.

    The claim is NEVER deleted to unlock a retry; success/failure only appends to the
    ledger. Maintenance (run_smoke_tests=False) never reaches this and never claims."""
    path = Path(switch_dir) / 'smoke-claim.json'
    if path.is_symlink():
        raise StopError('STOP: smoke claim symlinked: %s' % path)
    identity = {'round': ctx['round'], 'version': ctx['version'],
                'switch_dir': str(switch_dir), 'out_dir': str(out_dir),
                'pid': os.getpid(), 'claimed_at': time.time()}
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        prior = _read_claim(path)
        raise StopError('STOP: smoke already claimed for this core switch (out_dir=%s pid=%s); '
                        'a prior smoke call started (possibly interrupted). Preserve original '
                        'output, no retry/re-retest/resubmit'
                        % (prior.get('out_dir'), prior.get('pid'))) from None
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(identity, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    return identity


def _smoke_attempt_facts(smoke_report):
    restart = smoke_report.get('restart') or {}
    node_tests = [{'node_id': t.get('node_id'), 'status': t.get('status'), 'test_id': t.get('test_id')}
                  for t in (smoke_report.get('node_tests') or []) if not t.get('skipped')]
    return {'restart_attempted': bool(restart.get('stop') or restart.get('start')),
            'node_retests_attempted': node_tests,
            'ordinary_task': smoke_report.get('ordinary_task'),
            'ok': smoke_report.get('ok') is True, 'error': smoke_report.get('error')}


def _record_smoke_attempt(switch_dir, out_dir, smoke_output, smoke_report):
    ledger = _read_smoke_ledger(switch_dir)
    facts = _smoke_attempt_facts(smoke_report)
    facts.update(seq=len(ledger['attempts']) + 1, at=time.time(),
                 out_dir=str(out_dir), smoke_output=str(smoke_output))
    ledger['attempts'].append(facts)
    atomic_dump(Path(switch_dir) / 'smoke-attempts.json', ledger, overwrite=True)
    return facts


def post_switch(ctx, switch_dir, out_dir, *, env=None, run_smoke_tests=True, xiaoba_entry=None):
    """The recoverable post boundary. Requires a succeeded core switch (read-only
    recognition); never re-applies the core.

    Completion semantics (card r2 point 2):
      * run_smoke_tests=False is maintenance sync-only: it writes an explicit
        PARTIAL state (ok/completed False, sync_completed True, smoke_pending True).
        It is NOT a completed delivery and records NO smoke attempt.
      * a later default post/recover supplements ONE real smoke (never redoing core,
        skipping already-synced files) and only then marks ok/completed True.
      * once a smoke has been attempted for this core switch, it is never repeated:
        the attempt ledger (anchored to switch_dir) refuses a retry even under a
        different out_dir, preserving the original failed retest/submit output.
      * a pre-call exclusive claim (smoke-claim.json, anchored to switch_dir) is
        persisted BEFORE run_smoke, so a concurrent post or a SIGKILL mid-smoke cannot
        leave an unclaimed window that a different out_dir could re-enter; an existing
        claim refuses even when the ledger is still empty. Maintenance never claims.
      * a completed post is never re-run.
    """
    switch_dir = Path(switch_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    done_path = out_dir / 'post-switch-result.json'
    if done_path.is_file():
        prior = read_json(done_path)
        if prior.get('ok') is True and prior.get('completed') is True:
            raise StopError('STOP: post-switch already completed; nothing to recover')

    recognition = recognize_core_switch(ctx, switch_dir)
    monitor_pin = read_json(switch_dir / 'monitor-before.json')
    live = live_monitor_dir(ctx)
    ui_postimage = verify_ui_postimage(ctx, monitor_pin, live)

    marker = out_dir / 'POST_SWITCH.started'
    if not marker.exists():
        with marker.open('x') as handle:
            handle.write('Post-switch boundary (recoverable). Core switch is separate.\n')

    env = dict(env if env is not None else clean_env())
    env.setdefault('XIAOBA_MONITOR_NO_BROWSER', '1')

    files = post_files(ctx)
    repository = monitor_source_dir(ctx)
    sync_files = [f['name'] for f in files]
    live_before = monitor_pin['live_before_sha256']
    install = {row['name']: row['install'] for row in post_file_declaration(ctx)['files']}
    installed_modes = {}
    for f in files:
        try:
            observed = f['live_path'].lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(observed.st_mode):
            installed_modes[f['name']] = '0o%o' % stat.S_IMODE(observed.st_mode)
    precheck = sync_precheck(repository, live, sync_files=sync_files,
                             expected_export=monitor_pin['export_sha256'], live_before=live_before,
                             install=install, installed_modes=installed_modes)
    if not precheck['ok']:
        drift = [str(live / name) for name in precheck['live_drifted_files']]
        details = {key: precheck[key] for key in ('missing', 'extra', 'changed')}
        details['live_drift'] = drift
        raise StopError('STOP: post-switch sync precheck failed: %s'
                        % json.dumps(details, ensure_ascii=False))
    atomic_dump(out_dir / 'sync-precheck.json', precheck, overwrite=True)

    attempt_dir = out_dir / 'sync-attempts' / str(time.time_ns())
    sync_records = _sync_authorized(ctx, monitor_pin, attempt_dir)
    atomic_dump(out_dir / 'sync-records.json', sync_records, overwrite=True)

    proof = taskcards_proof(live, monitor_pin['taskcards'])
    atomic_dump(out_dir / 'taskcards-proof.json', proof, overwrite=True)

    checks = compile_runtime_set(repository, live)
    atomic_dump(out_dir / 'runtime-compile.json', checks, overwrite=True)

    comparison = compare(repository, live)
    if not comparison['ok']:
        raise StopError('STOP: live monitor runtime differs from same-source set after sync: %s'
                        % json.dumps({k: comparison[k] for k in ('missing', 'extra', 'changed')},
                                     ensure_ascii=False))
    atomic_dump(out_dir / 'runtime-compare.json', comparison, overwrite=True)

    report = {'ok': False, 'completed': False, 'core_recognition': recognition,
              'sync': sync_records, 'taskcards_proof': proof,
              'user_ui_asset_postimage': ui_postimage,
              'runtime_file_count': comparison['expected_file_count'],
              'runtime_compile_checks': len(checks), 'smoke': None}

    if not run_smoke_tests:
        report.update(status='partial_sync_only', sync_completed=True,
                      smoke_pending=True, smoke_attempted=False)
        atomic_dump(done_path, report, overwrite=True)
        return report

    ledger = _read_smoke_ledger(switch_dir)
    if ledger['attempts']:
        prior = ledger['attempts'][-1]
        raise StopError('STOP: smoke already attempted for this core switch (seq=%s out_dir=%s ok=%s); '
                        'preserve original output, no retry/re-retest/resubmit'
                        % (prior.get('seq'), prior.get('out_dir'), prior.get('ok')))

    base_url = resolve_base_url(live, env)
    validate_restart_before_claim(live, base_url, env)
    # Pre-call exclusive claim (card r2.1): persisted BEFORE any run_smoke action so
    # concurrency / SIGKILL cannot leave an unclaimed window a new out_dir could re-enter.
    _claim_smoke(switch_dir, out_dir, ctx)

    if xiaoba_entry is None:
        xiaoba_entry = Path.home() / '.agents/skills/xiaoba-agentos/scripts/xiaoba'
    smoke_output = out_dir / 'smoke'
    try:
        smoke = run_smoke(base_url=base_url, monitor_dir=live, live_db=live_db_path(ctx),
                          neutral_cwd=Path(ctx['work']), env=env, output=smoke_output,
                          round_name=ctx['round'], xiaoba_entry=xiaoba_entry)
    except BaseException as exc:
        partial = smoke_output / 'post-switch-smoke.json'
        smoke = read_json(partial) if partial.is_file() else {'ok': False, 'error': str(exc)}
        _record_smoke_attempt(switch_dir, out_dir, smoke_output, smoke)
        report.update(status='smoke_failed', sync_completed=True, smoke_pending=False,
                      smoke_attempted=True, smoke=smoke)
        atomic_dump(done_path, report, overwrite=True)
        raise StopError('STOP: post-switch smoke failed; original preserved, never retried: %s' % exc) from exc

    _record_smoke_attempt(switch_dir, out_dir, smoke_output, smoke)
    report['smoke'] = smoke
    if not smoke.get('ok'):
        report.update(status='smoke_failed', sync_completed=True, smoke_pending=False, smoke_attempted=True)
        atomic_dump(done_path, report, overwrite=True)
        raise StopError('STOP: post-switch smoke failed')
    report.update(ok=True, completed=True, status='completed', sync_completed=True,
                  smoke_pending=False, smoke_attempted=True)
    atomic_dump(done_path, report, overwrite=True)
    return report


def recover(ctx, switch_dir, out_dir, *, env=None, run_smoke_tests=True, xiaoba_entry=None):
    """Recovery entry: recognize the succeeded core switch (read-only), then run
    ONLY the post boundary. Identical post path; never re-applies the core."""
    recognize_core_switch(ctx, switch_dir)
    return post_switch(ctx, switch_dir, out_dir, env=env, run_smoke_tests=run_smoke_tests,
                       xiaoba_entry=xiaoba_entry)
