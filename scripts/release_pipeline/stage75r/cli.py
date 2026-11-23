"""stage75r command-line entry: real switch + post-switch recovery tools.

Read-only subcommands (plan / check / migration-check / syntax-check) are safe to
run at any time. Mutating subcommands (switch / post-switch / recover) refuse to
touch production unless --allow-real is given explicitly after manager acceptance;
this card only prepares tools and runs synthetic isolation, never a real release.

All paths/version/attempt come from the shared release context + the ONE attempt
record. Unified interpreter 3.14.7 -B; optimization is rejected before any write.
"""
import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import context_adapter as ca
from common import StopError, guard_runtime, read_json, sha


def _work_dir(args):
    if args.work:
        return Path(args.work).resolve()
    return Path(__file__).resolve().parent.parent


def _emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def cmd_plan(args):
    ctx = ca.load_context(_work_dir(args))
    paths = ca.attempt_paths(ctx)
    live = ca.live_monitor_dir(ctx)
    source = Path(ctx['source'])
    resolved = [{'name': f['name'], 'kind': f['kind'],
                 'repo_path': str(f['repo_path']), 'repo_exists': f['repo_path'].is_file(),
                 'intended_source_path': str(f['source_path']),
                 'intended_live_path': str(f['live_path']),
                 'expected_sha256': f['expected_sha256']}
                for f in ca.post_file_layout(ctx)]
    _emit({'ok': True, 'read_only': True, 'writes': 0, 'round': ctx['round'],
           'version': ctx['version'], 'attempt': paths['attempt'],
           'acceptance_step4_dir': str(paths['acceptance_step4_dir']),
           'reports_step4_dir': str(paths['reports_step4_dir']),
           'releases_package_dir': str(paths['package_dir']),
           'helpers_dir': str(paths['helpers_dir']),
           'candidate': str(ctx['candidate']), 'live': str(ctx['live']),
           'previous_package': str(ctx['previous_package']),
           'live_monitor_dir': str(live),
           'source_exported': source.is_dir(),
           'post_files': resolved,
           'switch_script': str(paths['package_dir'] / 'agentos/scripts/switch_release.py'),
           'note': 'plan only; no formal switch / node retest / task / production start-stop executed'})


def cmd_check(args):
    import switch_core
    ctx = ca.load_context(_work_dir(args))
    ca.validate_context(ctx, 'switch')
    gates = switch_core.gate_check(ctx)
    _emit({'ok': True, 'read_only': True, 'gates': gates})


def cmd_migration_check(args):
    ctx = ca.load_context(_work_dir(args))
    db = Path(args.db) if args.db else switch_core_live_db(ctx)
    result = ca.run_migration_check(ctx, db)
    _emit(result)


def switch_core_live_db(ctx):
    from switch_core import live_db_path
    return live_db_path(ctx)


def cmd_syntax_check(args):
    import switch_core
    ctx = ca.load_context(_work_dir(args))
    _emit({'ok': True, 'read_only': True, 'syntax': switch_core.syntax_gate(ctx)})


def _require_real(args):
    if not args.allow_real:
        raise StopError('STOP: real execution requires explicit --allow-real after manager acceptance; '
                        'this tool only prepares code and runs synthetic isolation by default')


def cmd_switch(args):
    import switch_core
    _require_real(args)
    ctx = ca.load_context(_work_dir(args))
    ca.validate_context(ctx, 'switch')
    switch_dir = Path(args.switch_dir) if args.switch_dir else Path(ctx['reports']) / 'switch'
    summary = switch_core.core_switch(ctx, switch_dir)
    if args.core_only:
        _emit({'ok': True, 'switch': summary, 'core_only': True,
               'switch_dir': str(switch_dir), 'post_switch': None,
               'post_switch_pending': True})
        return
    import recovery
    post_dir = Path(args.post_dir) if args.post_dir else Path(ctx['reports']) / 'post-switch'
    post = recovery.post_switch(ctx, switch_dir, post_dir, run_smoke_tests=not args.no_smoke)
    _emit({'ok': True, 'switch': summary, 'post_switch': post})


def cmd_post_switch(args):
    import recovery
    _require_real(args)
    ctx = ca.load_context(_work_dir(args))
    ca.validate_context(ctx, 'post-switch')
    switch_dir = Path(args.switch_dir) if args.switch_dir else Path(ctx['reports']) / 'switch'
    post_dir = Path(args.post_dir) if args.post_dir else Path(ctx['reports']) / 'post-switch'
    _emit(recovery.post_switch(ctx, switch_dir, post_dir, run_smoke_tests=not args.no_smoke))


def cmd_recover(args):
    import recovery
    _require_real(args)
    ctx = ca.load_context(_work_dir(args))
    ca.validate_context(ctx, 'post-switch')
    switch_dir = Path(args.switch_dir) if args.switch_dir else Path(ctx['reports']) / 'switch'
    post_dir = Path(args.post_dir) if args.post_dir else Path(ctx['reports']) / 'post-switch'
    _emit(recovery.recover(ctx, switch_dir, post_dir, run_smoke_tests=not args.no_smoke))


def cmd_ui_install(args):
    import switch_core
    if args.apply:
        _require_real(args)
    ctx = ca.load_context(_work_dir(args))
    _emit(switch_core.install_ui_assets(ctx, apply=args.apply))


def cmd_legacy_monitor(args):
    from post_smoke import load_lifecycle, resolve_base_url

    if args.stop_evidence:
        _require_real(args)
    ctx = ca.load_context(_work_dir(args))
    ca.validate_context(ctx, 'switch')
    declaration = ca.post_file_declaration(ctx)
    sources = [row for row in declaration['files'] if row['install']['policy'] == 'first_install']
    if len(sources) != 1:
        raise StopError('STOP: legacy handoff requires the authorized lifecycle source')
    monitor = ca.live_monitor_dir(ctx)
    run_dir = monitor / 'run'
    port = urlsplit(resolve_base_url(monitor, ca.clean_env())).port
    lifecycle = load_lifecycle(sources[0]['source_path'])
    check = lifecycle.preflight_legacy_migration(run_dir, monitor, port, write_binding=False)
    entry = monitor / 'xiaoba_monitor.py'
    if entry.is_symlink() or not entry.is_file():
        raise StopError('STOP: installed predecessor monitor entry is missing or symlinked')
    result = {'read_only': True, 'legacy': check, 'before_entry_sha256': sha(entry),
              'lifecycle_source_sha256': sources[0]['expected_sha256']}
    if not args.stop_evidence:
        _emit(result)
        return

    proof = Path(args.stop_evidence)
    if (proof.is_symlink() or not proof.is_file()
            or not proof.resolve().is_relative_to(Path(ctx['root']).resolve())):
        raise StopError('STOP: legacy evidence must be a regular file inside the release root')
    prior = read_json(proof)
    fields = ('pid', 'port', 'root', 'run_dir', 'entry', 'fingerprint', 'children')
    prior_binding = (prior.get('legacy') or {}).get('binding')
    observed = check.get('binding')
    if (prior.get('read_only') is not True or not isinstance(prior_binding, dict)
            or not isinstance(observed, dict)
            or prior.get('before_entry_sha256') != result['before_entry_sha256']
            or prior.get('lifecycle_source_sha256') != result['lifecycle_source_sha256']
            or any(prior_binding.get(key) != observed.get(key) for key in fields)):
        raise StopError('STOP: legacy monitor identity changed since the read-only evidence')
    issued = lifecycle.preflight_legacy_migration(run_dir, monitor, port, write_binding=True)
    binding = issued.get('binding')
    if (not isinstance(binding, dict)
            or any(observed.get(key) != binding.get(key) for key in fields)):
        raise StopError('STOP: legacy monitor identity changed while preparing the stop')
    stopped = lifecycle.migration_stop(run_dir, monitor, port, issued['binding_path'])
    _emit({'read_only': False, 'legacy_stop': stopped})
    if stopped.get('status') not in ('stopped', 'already_stopped'):
        raise StopError('STOP: legacy monitor stop did not complete')


def build_parser():
    parser = argparse.ArgumentParser(description='stage75r real switch + post-switch recovery tools')
    parser.add_argument('--work', help='release work dir (default: parent of this tool)')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('plan', help='read-only resolved paths/argv; no writes').set_defaults(func=cmd_plan)
    sub.add_parser('check', help='read-only gate check; no writes').set_defaults(func=cmd_check)
    p = sub.add_parser('migration-check', help='read-only migration declaration check')
    p.add_argument('--db', help='database path (default: live runtime db)')
    p.set_defaults(func=cmd_migration_check)
    sub.add_parser('syntax-check', help='read-only authorized-post-file syntax gate').set_defaults(func=cmd_syntax_check)
    for name, func, helptext in (
            ('switch', cmd_switch, 'core switch then post, or core only (requires --allow-real)'),
            ('post-switch', cmd_post_switch, 'post boundary only (requires --allow-real)'),
            ('recover', cmd_recover, 'recognize succeeded core, run post only (requires --allow-real)')):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument('--allow-real', action='store_true', help='authorize real production execution')
        mode = sp
        if name == 'switch':
            mode = sp.add_mutually_exclusive_group()
            mode.add_argument('--core-only', action='store_true',
                              help='complete core switch and readback, then leave post-switch pending')
        mode.add_argument('--no-smoke', action='store_true', help='skip the post-switch smoke')
        sp.add_argument('--switch-dir', help='core switch evidence dir')
        sp.add_argument('--post-dir', help='post-switch evidence dir')
        sp.set_defaults(func=func)
    ui = sub.add_parser('ui-install', help='read-only UI-asset prepare; --apply installs (requires --allow-real)')
    ui.add_argument('--apply', action='store_true', help='atomically install the source-bound UI assets')
    ui.add_argument('--allow-real', action='store_true', help='authorize real production installation')
    ui.set_defaults(func=cmd_ui_install)
    legacy = sub.add_parser('legacy-monitor', help='inspect predecessor monitor or stop a previously inspected instance')
    legacy.add_argument('--stop-evidence', help='read-only inspection JSON inside release root; enables a controlled stop')
    legacy.add_argument('--allow-real', action='store_true', help='authorize stop after manager acceptance')
    legacy.set_defaults(func=cmd_legacy_monitor)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    guard_runtime()
    try:
        args.func(args)
    except StopError as exc:
        _emit({'ok': False, 'error': str(exc)})
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
