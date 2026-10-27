#!/usr/bin/env python3
"""Read only the agreed review artifacts; never dispatch or release software."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from datetime import datetime, timezone

ROOT = Path('/Users/suzhizhou/Documents/私人助理')
DESKTOP = ROOT / '工作区/小八AgentOS-v1.6.5-修复/desktop-review-20261026'
MANUAL = DESKTOP / 'manual-continuation-20261026'
REPORT = MANUAL / '主持进度检查.json'
NAMES = ('read-progress.json', 'dispositions-progress.json', 'final-receipt.json', 'blockers.md')
INDEX_SHA = '883149941f29bc46b33de66299f9d278614d75befe7ed752f56089bb145f2058'
SCHEMA_SHA = 'a74497bf8410891f76ee7e7c090154563f53413dffadcf6b6281cbb2e88f6c41'
MAX_BYTES = 8 * 1024 * 1024


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('duplicate JSON key')
        obj[key] = value
    return obj


def parse(raw):
    return json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite number')))


def read_artifact(path):
    """Bounded regular-file read, with concurrent-write and symlink checks."""
    info = {'path': str(path)}
    try:
        if any(p.is_symlink() for p in (path, *path.parents)):
            return {**info, 'state': 'UNSAFE_SYMLINK'}, None
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode):
            return {**info, 'state': 'NOT_REGULAR_FILE'}, None
        if st.st_size > MAX_BYTES:
            return {**info, 'state': 'TOO_LARGE', 'bytes': st.st_size}, None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                return {**info, 'state': 'NOT_REGULAR_FILE'}, None
            raw = stream.read(MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        info.update(bytes=len(raw), sha256=digest(raw),
                    modified_at=datetime.fromtimestamp(after.st_mtime, timezone.utc).isoformat())
        if len(raw) > MAX_BYTES:
            return {**info, 'state': 'TOO_LARGE'}, None
        if not signature(st) == signature(before) == signature(after) == signature(current):
            return {**info, 'state': 'CHANGING_DURING_READ'}, None
        return {**info, 'state': 'STABLE_BYTES'}, raw
    except FileNotFoundError:
        return {**info, 'state': 'MISSING'}, None
    except OSError as exc:
        return {**info, 'state': 'READ_ERROR', 'error_type': type(exc).__name__}, None


def json_artifact(path):
    info, raw = read_artifact(path)
    if raw is None:
        return info, None
    try:
        value = parse(raw)
    except (UnicodeError, ValueError, RecursionError) as exc:
        info.update(state='UNPARSEABLE', error_type=type(exc).__name__)
        if isinstance(exc, json.JSONDecodeError):
            info.update(error_line=exc.lineno, error_column=exc.colno, error_character=exc.pos)
        return info, None
    return {**info, 'state': 'PARSED'}, value


def schema_errors(value, rule, where='$'):
    """Validate the vocabulary used by this exact, hash-pinned receipt schema."""
    supported = {'$schema', 'title', 'description', 'type', 'const', 'enum', 'properties',
                 'required', 'additionalProperties', 'items', 'minItems', 'maxItems',
                 'minLength', 'maxLength', 'pattern', 'minimum', 'allOf', 'if', 'then'}
    errors = []
    if set(rule) - supported:
        return [where + ': unsupported schema keyword']
    types = {'object': lambda v: isinstance(v, dict), 'array': lambda v: isinstance(v, list),
             'string': lambda v: isinstance(v, str), 'boolean': lambda v: type(v) is bool,
             'integer': lambda v: type(v) is int, 'null': lambda v: v is None}
    wanted = rule.get('type', [])
    wanted = [wanted] if isinstance(wanted, str) else wanted
    if wanted and not any(types.get(k, lambda _: False)(value) for k in wanted):
        return [where + ': wrong type']
    if 'const' in rule and (type(value) is not type(rule['const']) or value != rule['const']):
        errors.append(where + ': wrong constant')
    if 'enum' in rule and not any(type(value) is type(x) and value == x for x in rule['enum']):
        errors.append(where + ': outside enum')
    if isinstance(value, dict):
        props = rule.get('properties', {})
        errors += [where + '.' + k + ': missing' for k in rule.get('required', []) if k not in value]
        if rule.get('additionalProperties') is False:
            errors += [where + '.' + k + ': extra field' for k in value if k not in props]
        for key, child in props.items():
            if key in value:
                errors += schema_errors(value[key], child, where + '.' + key)
    if isinstance(value, list):
        if len(value) < rule.get('minItems', 0) or len(value) > rule.get('maxItems', float('inf')):
            errors.append(where + ': wrong item count')
        if 'items' in rule:
            for n, item in enumerate(value):
                errors += schema_errors(item, rule['items'], f'{where}[{n}]')
    if isinstance(value, str):
        if len(value) < rule.get('minLength', 0) or len(value) > rule.get('maxLength', float('inf')):
            errors.append(where + ': wrong string length')
        if 'pattern' in rule and re.search(rule['pattern'], value) is None:
            errors.append(where + ': pattern mismatch')
    if type(value) is int and value < rule.get('minimum', value):
        errors.append(where + ': below minimum')
    for child in rule.get('allOf', []):
        errors += schema_errors(value, child, where)
    if 'if' in rule and not schema_errors(value, rule['if'], where) and 'then' in rule:
        errors += schema_errors(value, rule['then'], where)
    return errors


def contract():
    info, index = json_artifact(DESKTOP / 'composite-review-index.json')
    if info.get('sha256') != INDEX_SHA or not isinstance(index, dict):
        raise ValueError('canonical index unavailable or changed')
    sinfo, schema = json_artifact(DESKTOP / index['final_receipt_schema'])
    if sinfo.get('sha256') != SCHEMA_SHA or not isinstance(schema, dict):
        raise ValueError('receipt schema unavailable or changed')
    metadata = {}
    for rel, expected in index['files'].items():
        parts = Path(rel)
        if parts.is_absolute() or '..' in parts.parts:
            raise ValueError('unsafe frozen path')
        item, raw = read_artifact(DESKTOP / rel)
        if raw is None or item.get('sha256') != expected:
            raise ValueError('frozen input unavailable or changed: ' + rel)
        try:
            lines = raw.decode('utf-8').splitlines()
        except UnicodeError:
            lines = None
        nonblank = [line for line in (lines or []) if line.strip()]
        metadata[rel] = {'sha256': expected, 'byte_read_through': len(raw),
                         'line_read_through': len(lines) if lines is not None else None,
                         'last_visible_anchor': nonblank[-1][-180:] if nonblank else ''}
    bindings = {k: schema['properties'][k]['const'] for k in
                ('source_manifest_sha256', 'freeze_descriptor_sha256', 'integration_material_bundle_sha256')}
    bindings.update(workflow_manifest_sha256=digest(canonical({p: index['files'][r] for p, r in index['workflow_sources'].items()})),
                    integration_review_set_sha256=digest(canonical({r: index['files'][r] for r in sorted(index['integration_review_files'])})))
    for field, key in [('rehearsal_report_sha256', 'rehearsal_report'),
                       ('limitations_adjudication_sha256', 'limitations_adjudication'),
                       ('final_integration_card_sha256', 'final_integration_card')]:
        bindings[field] = index['files'][index[key]]
    return index, schema, metadata, bindings


def coverage_summary(value, expected):
    if not isinstance(value, dict):
        return {'recognized': False, 'reason': 'coverage object unavailable'}
    matched = set(value) & set(expected)
    return {'recognized': True, 'recorded': len(matched),
            'author_claimed_eof': sum(isinstance(value[k], dict) and value[k].get('reached_eof') is True
                                      and value[k].get('truncated') is False for k in matched),
            'unrecorded_count': len(set(expected) - matched),
            'extra_keys_count': len(set(value) - set(expected)),
            'basis': 'author_records_not_verified_reading'}


def progress_summary(value, expected):
    if not isinstance(value, dict):
        return {'recognized': False}
    for key in ('integration_read_coverage', 'coverage', 'files', 'read_coverage'):
        if isinstance(value.get(key), dict) and set(value[key]) & set(expected):
            return {'field': key, **coverage_summary(value[key], expected)}
    if set(value) & set(expected):
        return {'field': '$', **coverage_summary(value, expected)}
    return {'recognized': False, 'reason': 'progress format not recognized; do not infer zero progress'}


def evidence_errors(items, metadata, where, allow_empty=False):
    if not isinstance(items, list) or (not items and not allow_empty):
        return [where + ': nonempty evidence list required']
    errors = []
    for n, row in enumerate(items):
        label = f'{where}[{n}]'
        if not isinstance(row, dict) or set(row) != {'path', 'line_start', 'line_end', 'observation'}:
            errors.append(label + ': wrong evidence fields')
            continue
        rel = row['path']
        if not isinstance(rel, str) or rel not in metadata or metadata[rel]['line_read_through'] is None:
            errors.append(label + ': unbound text path')
            continue
        start, end = row['line_start'], row['line_end']
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= metadata[rel]['line_read_through']:
            errors.append(label + ': invalid line range')
        if not isinstance(row['observation'], str) or not row['observation'].strip():
            errors.append(label + ': empty observation')
    return errors


def final_check(value, who, index, schema, metadata, bindings, session_reference):
    errors = schema_errors(value, schema)
    if not isinstance(value, dict):
        return {'errors': errors, 'static_checks_passed': False}
    for key, expected in {**bindings, 'reviewer': who, 'desktop_session_reference': session_reference}.items():
        if value.get(key) != expected:
            errors.append(key + ': binding/identity mismatch')
    expected = set(index['integration_review_files'])
    coverage = value.get('integration_read_coverage')
    if not isinstance(coverage, dict):
        coverage = {}
    if set(coverage) != expected:
        errors.append('integration_read_coverage: exact 232-key set mismatch')
    incomplete = []
    for rel in expected & set(coverage):
        row = coverage[rel]
        required = {*metadata[rel], 'reached_eof', 'truncated'}
        if not isinstance(row, dict) or set(row) != required:
            errors.append('coverage fields: ' + rel)
            continue
        if (type(row['reached_eof']) is not bool or type(row['truncated']) is not bool
                or type(row['line_read_through']) is not int
                or type(row['byte_read_through']) is not int
                or not isinstance(row['last_visible_anchor'], str)
                or len(row['last_visible_anchor']) > 180):
            errors.append('coverage types: ' + rel)
        elif not (0 <= row['line_read_through'] <= metadata[rel]['line_read_through']
                  and 0 <= row['byte_read_through'] <= metadata[rel]['byte_read_through']):
            errors.append('coverage range: ' + rel)
        if row['reached_eof'] is not True or row['truncated'] is not False:
            incomplete.append(rel)
        if row['reached_eof'] is True and row['truncated'] is False:
            if any(type(row[k]) is not type(v) or row[k] != v for k, v in metadata[rel].items()):
                errors.append('coverage metadata mismatch: ' + rel)
        elif row['sha256'] != metadata[rel]['sha256']:
            errors.append('coverage sha mismatch: ' + rel)
    dispositions = value.get('nonblocking_dispositions')
    if not isinstance(dispositions, dict):
        dispositions = {}
    if set(dispositions) != set(index['nonblocking_findings']):
        errors.append('nonblocking_dispositions: exact 69-key set mismatch')
    rejected = []
    for key in set(dispositions) & set(index['nonblocking_findings']):
        row = dispositions[key]
        if not isinstance(row, dict) or set(row) != {'accepted', 'reason', 'evidence'}:
            errors.append('disposition fields: ' + key)
            continue
        if type(row['accepted']) is not bool or not isinstance(row['reason'], str) or not row['reason'].strip():
            errors.append('disposition type/reason: ' + key)
        if row['accepted'] is False:
            rejected.append(key)
        errors += evidence_errors(row['evidence'], metadata, key, allow_empty=row['accepted'] is False)
    results = value.get('results')
    verdict = None
    missing = None
    if isinstance(results, list) and len(results) == 1 and isinstance(results[0], dict):
        row = results[0]
        verdict = row.get('verdict')
        missing = len(row['missing_evidence']) if isinstance(row.get('missing_evidence'), list) else None
        rel = index['prompt_files']['integration_release']
        prompt = str(DESKTOP.parent / 'acceptance/review-integration_release.txt')
        for key, target in {'path': prompt, 'prompt_sha256': index['files'][rel],
                            'material_chunk_sha256': bindings['final_integration_card_sha256']}.items():
            if row.get(key) != target:
                errors.append('results.' + key + ': mismatch')
        read = row.get('read_coverage', {})
        if isinstance(read, dict) and read.get('reached_eof') is True:
            for key, target in metadata[rel].items():
                if key != 'sha256' and read.get(key) != target:
                    errors.append('results.read_coverage.' + key + ': mismatch')
        if row.get('evidence'):
            errors += evidence_errors(row['evidence'], metadata, 'results.evidence')
    return {'static_checks_passed': not errors, 'errors': errors,
            'coverage': coverage_summary(coverage, expected), 'incomplete_paths': sorted(incomplete),
            'rejected_ids': sorted(rejected), 'disposition_count': len(dispositions),
            'batch_state': value.get('batch_state'), 'author_verdict': verdict,
            'missing_evidence_count': missing,
            'unresolved_count': len(value['unresolved']) if isinstance(value.get('unresolved'), list) else None,
            'source_and_semantic_review_passed': False, 'formal_gate_passed': False}


def inspect_reviewer(who, index, schema, metadata, bindings, session_reference):
    directory = Path(f'/private/tmp/xiaoba-v165-{who}-FINAL-manual-20261026')
    info, values = {}, {}
    for name in NAMES:
        if name.endswith('.json'):
            info[name], values[name] = json_artifact(directory / name)
        else:
            info[name], raw = read_artifact(directory / name)
            values[name] = bool(raw and raw.strip())
    output = {'reviewer': who, 'files': info,
              'progress': progress_summary(values['read-progress.json'], index['integration_review_files']),
              'desktop_running_state': 'not_determined_by_files',
              'inactivity_300_seconds_proven': False, 'formal_gate_passed': False}
    if info['final-receipt.json']['state'] == 'PARSED':
        check = final_check(values['final-receipt.json'], who, index, schema, metadata, bindings, session_reference)
        output['final'] = check
        progress_newer = (info['read-progress.json'].get('modified_at', '')
                          > info['final-receipt.json'].get('modified_at', ''))
        output['final_predates_progress_file'] = progress_newer
        if not check['static_checks_passed']:
            status, action = 'FINAL_NEEDS_REPAIR', '核对桌面作者与错误，要求本端原作者修正新稿；主持不代改'
        elif check['batch_state'] != 'COMPLETE_WITH_RESULTS' or check['author_verdict'] != 'PASS' or check['rejected_ids'] or check['incomplete_paths'] or check['missing_evidence_count']:
            status, action = 'SUBSTANTIVE_BLOCKERS', '审查具体缺口，决定固定范围内补读补证或记录阻断'
        elif values['blockers.md']:
            status, action = 'FINAL_HAS_OPEN_ITEMS', '最终稿与非空阻塞记录并存，核实阻塞是否已解；不自动忽略'
        elif info['blockers.md']['state'] not in ('MISSING', 'STABLE_BYTES'):
            status, action = 'UNSTABLE_OR_UNPARSEABLE', '阻塞记录不可稳定读取，先复核再决定下一步'
        elif check['unresolved_count']:
            status, action = 'FINAL_HAS_OPEN_ITEMS', '逐项判断未决项及模型身份披露，核验来源与真实阅读'
        else:
            status, action = 'READY_FOR_SOURCE_REVIEW', '核验桌面来源、实际全文阅读及18项义务；格式齐备不等于通过'
        if progress_newer and check.get('batch_state') in ('BLOCKED', 'PARTIAL'):
            status, action = 'AUTHOR_PROGRESS_AFTER_BLOCKED_FINAL', '旧阻断稿保留，核对后续分段阅读进展；不把旧稿当最新完成结论'
    elif any(v['state'] not in ('MISSING', 'PARSED', 'STABLE_BYTES') for v in info.values()):
        status, action = 'UNSTABLE_OR_UNPARSEABLE', '稍后重读并核对桌面；稳定坏JSON不直接判成仍在写'
    elif values['blockers.md']:
        status, action = 'AUTHOR_REPORTED_BLOCKER', '读取本端blockers.md并核实具体缺口'
    elif all(v['state'] == 'MISSING' for v in info.values()):
        status, action = 'NO_ARTIFACTS', '先查桌面真实状态，必要时提示本端分段落盘；不认定未开始或卡死'
    else:
        status, action = 'AUTHOR_PROGRESS', '按本端实际进度继续观察；未知格式不推定为零'
    output.update(status=status, next_action=action)
    return output


def build_report():
    report = {'kind': 'desktop_review_progress_observation_v1',
              'observed_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'agreed_artifacts_only', 'formal_gate_passed': False,
              'desktop_messages_sent': False, 'reviews_started': False,
              'file_silence_is_not_desktop_inactivity': True}
    try:
        index, schema, metadata, bindings = contract()
        _, context = json_artifact(MANUAL / '当前桌面会话观察.json')
        if not isinstance(context, dict):
            raise ValueError('current desktop identity observation unavailable')
        report['fixed_inputs_verified'] = len(index['files'])
        report['reviewers'] = {who: inspect_reviewer(who, index, schema, metadata, bindings,
                               context['reviewers'][who]['expected_reference']) for who in ('DSH', 'Qoder')}
        _, previous = json_artifact(REPORT)
        for who, current in report['reviewers'].items():
            observed = context['reviewers'][who]
            current['last_desktop_observation'] = {
                'observed_at': observed.get('last_desktop_observed_at', context.get('observed_at')),
                'state': observed.get('observed_state'),
                'external_error': observed.get('external_error'),
                'operator_next_action': observed.get('next_action'),
                'is_live_desktop_check': False,
            }
            old = previous.get('reviewers', {}).get(who, {}) if isinstance(previous, dict) else {}
            changed = []
            for name, item in current['files'].items():
                before = old.get('files', {}).get(name, {})
                if (item.get('state'), item.get('sha256')) != (before.get('state'), before.get('sha256')):
                    changed.append(name)
            current['artifact_changes_since_previous_observation'] = changed if old else None
            current['status_changed_since_previous_observation'] = current['status'] != old.get('status') if old else None
            current['previous_observed_at'] = previous.get('observed_at') if isinstance(previous, dict) else None
    except (ValueError, KeyError, TypeError) as exc:
        report.update(status='CONTRACT_BLOCKED', error=str(exc), next_action='先核实固定输入或会话身份，不派发后续动作')
    return report


def save_report(report):
    if any(p.is_symlink() for p in (REPORT, *REPORT.parents)):
        raise ValueError('refuse symlink report path')
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK
    if REPORT.exists() and not stat.S_ISREG(REPORT.lstat().st_mode):
        raise ValueError('report must be a regular file')
    with os.fdopen(os.open(REPORT, flags, 0o600), 'wb') as stream:
        stream.write(canonical(report))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-report', action='store_true', help='update only the fixed operator report')
    parser.add_argument('--json', action='store_true', help='print full report instead of compact summary')
    args = parser.parse_args()
    report = build_report()
    if args.write_report:
        save_report(report)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(report['observed_at'])
        if report.get('status') == 'CONTRACT_BLOCKED':
            print('CONTRACT_BLOCKED:', report['error'])
        for who, result in report.get('reviewers', {}).items():
            print(who, result['status'], json.dumps(result['progress'], ensure_ascii=False))
            if 'final' in result:
                check = result['final']
                print('  final:', check.get('author_verdict'), 'errors:', len(check['errors']))
            print(' ', result['next_action'])
        if args.write_report:
            print('报告:', REPORT)
    return 2 if report.get('status') == 'CONTRACT_BLOCKED' else 0


if __name__ == '__main__':
    raise SystemExit(main())
