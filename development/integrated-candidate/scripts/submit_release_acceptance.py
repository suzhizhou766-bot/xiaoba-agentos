#!/usr/bin/env python3
"""Release tooling entry: submit frozen acceptance context explicitly.

Usage: submit_release_acceptance.py --config CONFIG PROMPT [submit options]
  --preflight-expected-version VERSION
  --preflight-predecessor-sha256 SHA256
  --preflight-project-root PATH --preflight-assistant-root PATH
  --preflight-reviewer ID [--preflight-reviewer ID ...]
  --preflight-author ID ... | --preflight-no-author
  --preflight-executor ID ... | --preflight-no-executor
The task is only submitted when this script is explicitly run.

C11-02: before dispatch this runs the read-only release_preflight against the
integrated-candidate source tree. The preflight flags (--preflight-*) are
consumed locally and never forwarded to cli submit. If preflight reports any
issue, nothing is submitted and no model is dispatched (fail closed).
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys


def _extract_preflight(parser, argv):
    """Pull C11-02 preflight flags out of argv before cli-submit parsing."""
    pf = {
        'expected_version': None, 'predecessor_sha256': None,
        'project_root': None, 'assistant_root': None,
        'reviewer': [], 'author': [], 'executor': [],
        'no_author': False, 'no_executor': False, 'preflight_only': False,
    }
    rest = []
    index = 0
    simple = {
        '--preflight-expected-version': ('expected_version', True),
        '--preflight-predecessor-sha256': ('predecessor_sha256', True),
        '--preflight-project-root': ('project_root', True),
        '--preflight-assistant-root': ('assistant_root', True),
        '--preflight-only': ('preflight_only', False),
        '--preflight-no-author': ('no_author', False),
        '--preflight-no-executor': ('no_executor', False),
    }
    repeatable = {
        '--preflight-reviewer': 'reviewer',
        '--preflight-author': 'author',
        '--preflight-executor': 'executor',
    }
    while index < len(argv):
        item = argv[index]
        option, sep, inline = item.partition('=')
        if option in simple:
            key, takes_value = simple[option]
            if takes_value:
                value = inline if sep else (argv[index + 1] if index + 1 < len(argv) else None)
                if not value or (not sep and value.startswith('--')):
                    parser.error(f'{option} requires a value')
                pf[key] = value
                index += 1 if sep else 2
            else:
                if sep:
                    parser.error(f'{option} does not take a value')
                pf[key] = True
                index += 1
            continue
        if option in repeatable:
            value = inline if sep else (argv[index + 1] if index + 1 < len(argv) else None)
            if not value or (not sep and value.startswith('--')):
                parser.error(f'{option} requires a value')
            pf[repeatable[option]].append(value)
            index += 1 if sep else 2
            continue
        if item.startswith('--preflight-'):
            parser.error('unknown preflight flag: ' + item)
        rest.append(item)
        index += 1
    return rest, pf


def arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    rest, _pf = _extract_preflight(parser, argv)
    args, rest = parser.parse_known_args(rest)
    if '--no-review' in rest:
        parser.error('release acceptance requires review')
    rest = _force_value(parser, rest, '--memory-kind', 'audit')
    if any(item == '--task-purpose' or item.startswith('--task-purpose=') for item in rest):
        parser.error('release acceptance purpose is derived from --release-acceptance')
    rest = [item for item in rest if item != '--release-acceptance']
    return ['--config', args.config, 'submit', *rest, '--memory-kind', 'audit', '--release-acceptance']


def _force_value(parser, argv, option, value):
    result = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == option:
            if index + 1 >= len(argv):
                parser.error(f'{option} requires a value')
            supplied = argv[index + 1]
            if supplied != value:
                parser.error(f'{option} must be {value}')
            index += 2
            continue
        if item.startswith(option + '='):
            if item.split('=', 1)[1] != value:
                parser.error(f'{option} must be {value}')
            index += 1
            continue
        result.append(item)
        index += 1
    return result


def _dispatch_context_gaps(pf):
    missing = []
    for key, label in (
        ('expected_version', 'target version'),
        ('predecessor_sha256', 'active predecessor manifest SHA-256'),
        ('project_root', 'project root for reverse UI hash checks'),
        ('assistant_root', 'assistant root for reverse UI hash checks'),
    ):
        if not pf.get(key):
            missing.append(label)
    if not pf.get('reviewer'):
        missing.append('at least one actual reviewer ID')
    if pf.get('no_author') and pf.get('author'):
        missing.append('author IDs conflict with --preflight-no-author')
    elif not pf.get('author') and not pf.get('no_author'):
        missing.append('author IDs or explicit --preflight-no-author')
    if pf.get('no_executor') and pf.get('executor'):
        missing.append('executor IDs conflict with --preflight-no-executor')
    elif not pf.get('executor') and not pf.get('no_executor'):
        missing.append('executor IDs or explicit --preflight-no-executor')
    return missing


def run_preflight(pf, *, require_dispatch_context=True):
    """Build the C11-02 report for this source tree; returns (report, did_run)."""
    scripts = Path(__file__).resolve().parent
    sys.path.insert(0, str(scripts))
    from release_preflight import preflight
    report = preflight(
        scripts.parent,
        expected_version=pf['expected_version'],
        predecessor_sha256=pf['predecessor_sha256'],
        project=Path(pf['project_root']) if pf.get('project_root') else None,
        assistant=Path(pf['assistant_root']) if pf.get('assistant_root') else None,
        reviewers=pf['reviewer'],
        authors=pf['author'],
        executors=pf['executor'],
    )
    missing = _dispatch_context_gaps(pf)
    report['checks']['dispatch_context'] = {
        'required': require_dispatch_context,
        'complete': not missing,
        'missing': missing,
    }
    if require_dispatch_context and missing:
        report['ok'] = False
        report['issues'].append({
            'code': 'dispatch_preflight_context_missing',
            'message': '派审前预检上下文不完整：' + '；'.join(missing),
        })
    report.pop('report_sha256', None)
    report['report_sha256'] = hashlib.sha256(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()
    return report


def main(argv=None):
    raw = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(add_help=False)
    rest, pf = _extract_preflight(parser, raw)
    report = run_preflight(pf, require_dispatch_context=not pf['preflight_only'])
    print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)
    if not report['ok'] or pf['preflight_only']:
        # Fail closed: no task submitted, no model dispatched, no retrieval event.
        return 0 if report['ok'] else 1
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from xiaoba_agentos.cli import main as cli_main
    return cli_main(arguments(raw))


if __name__ == '__main__':
    raise SystemExit(main())
