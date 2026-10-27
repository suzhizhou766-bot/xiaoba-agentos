#!/usr/bin/env python3
"""Read-only Jianying 11.6 experiment. All generated files stay beside this script."""
import argparse
import collections
import csv
import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import resource
import subprocess
import uuid

ROOT = Path(__file__).resolve().parent
APP = Path('/Applications/VideoFusion-macOS.app')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def child_limits():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def summarize(data, csv_path):
    if not isinstance(data, dict):
        raise ValueError('decoded JSON is not an object')
    result = {'root_keys': sorted(data)}
    if 'tracks' not in data:
        result['kind'] = 'metadata'
        result['draft_name'] = data.get('draft_name')
        result['duration_seconds'] = data.get('tm_duration', 0) / 1e6
        return result
    tracks = data['tracks']
    materials = data.get('materials', {})
    all_materials = {
        m['id']: (kind, m)
        for kind, items in materials.items() if isinstance(items, list)
        for m in items if isinstance(m, dict) and isinstance(m.get('id'), str)
    }
    result.update({
        'kind': 'timeline',
        'duration_seconds': data.get('duration', 0) / 1e6,
        'fps': data.get('fps'), 'canvas': data.get('canvas_config'),
        'track_types': dict(collections.Counter(t.get('type', 'unknown') for t in tracks)),
        'track_count': len(tracks),
        'segment_count': sum(len(t.get('segments', [])) for t in tracks),
        'material_counts': {k: len(v) for k, v in materials.items()
                            if isinstance(v, list) and v},
        'missing_primary_material_refs': [], 'missing_extra_material_refs': [],
        'invalid_ranges': [], 'segments_with_keyframes': 0,
        'referenced_local_media': 0, 'missing_local_media': [],
        'maximum_segment_end_seconds': 0,
    })
    seen_paths = set()
    rows = []
    for ti, track in enumerate(tracks):
        for si, segment in enumerate(track.get('segments', [])):
            sid = segment.get('id', str(si))
            mid = segment.get('material_id')
            if mid and mid not in all_materials:
                result['missing_primary_material_refs'].append({'track': ti, 'segment': sid, 'id': mid})
            for extra in segment.get('extra_material_refs', []):
                if extra not in all_materials:
                    result['missing_extra_material_refs'].append({'track': ti, 'segment': sid, 'id': extra})
            target = segment.get('target_timerange') or {}
            source = segment.get('source_timerange') or {}
            start, duration = target.get('start', 0), target.get('duration', 0)
            if not isinstance(start, (int, float)) or not isinstance(duration, (int, float)) or start < 0 or duration < 0:
                result['invalid_ranges'].append({'track': ti, 'segment': sid})
                continue
            end = (start + duration) / 1e6
            result['maximum_segment_end_seconds'] = max(result['maximum_segment_end_seconds'], end)
            result['segments_with_keyframes'] += bool(segment.get('common_keyframes'))
            material_kind, material = all_materials.get(mid, ('unknown', {}))
            path = material.get('path') or ''
            if path.startswith('/') and path not in seen_paths:
                seen_paths.add(path)
                result['referenced_local_media'] += 1
                if not Path(path).exists():
                    result['missing_local_media'].append(path)
            text = ''
            if material_kind == 'texts':
                content = material.get('content', '')
                try:
                    parsed = json.loads(content) if isinstance(content, str) else content
                    text = parsed.get('text', '') if isinstance(parsed, dict) else ''
                except (ValueError, TypeError):
                    text = content if isinstance(content, str) else ''
            rows.append({
                'track_index': ti, 'track_type': track.get('type'),
                'segment_index': si, 'segment_id': sid,
                'start_seconds': start / 1e6, 'end_seconds': end,
                'duration_seconds': duration / 1e6,
                'source_start_seconds': source.get('start', 0) / 1e6,
                'source_duration_seconds': source.get('duration', 0) / 1e6,
                'material_type': material_kind, 'material_id': mid,
                'material_name': material.get('material_name') or material.get('name', ''),
                'media_path': path, 'text': text,
                'speed': segment.get('speed'), 'volume': segment.get('volume'),
                'keyframe_groups': len(segment.get('common_keyframes') or []),
            })
    result['timeline_end_matches_duration'] = abs(
        result['maximum_segment_end_seconds'] - result['duration_seconds']) <= 1 / (data.get('fps') or 30)
    result['text_segment_count'] = sum(row['material_type'] == 'texts' for row in rows)
    result['nonempty_text_segments'] = sum(bool(row['text']) for row in rows)
    result['primary_refs_resolved'] = not result['missing_primary_material_refs']
    result['ranges_valid'] = not result['invalid_ranges']
    if rows:
        with csv_path.open('x', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(sorted(rows, key=lambda row: (row['start_seconds'], row['track_index'])))
    return result


def run(projects):
    os.umask(0o077)
    run_id = datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    run_dir = ROOT / 'results' / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    info = plistlib.loads((APP / 'Contents/Info.plist').read_bytes())
    build = json.loads((ROOT / 'results/build.json').read_text())
    library = APP / 'Contents/Frameworks/libvideoeditor.dylib'
    if info.get('CFBundleShortVersionString') != build['app_version'] or digest(library) != build['library_sha256']:
        raise SystemExit('Application changed since experiment build; stop')
    records = []
    seen_inputs = set()
    for project in projects:
        project = project.expanduser().resolve(strict=True)
        candidates = [project / 'draft_info.json', project / 'draft_meta_info.json']
        candidates += sorted(project.glob('Timelines/*/draft_info.json'))
        for source in candidates:
            if not source.is_file() or source.is_symlink() or source in seen_inputs:
                continue
            seen_inputs.add(source)
            relative = source.relative_to(project)
            destination = run_dir / project.name / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            before = digest(source)
            command = ['/usr/bin/sandbox-exec', '-f', str(ROOT / 'sandbox.sb'),
                       str(ROOT / 'bin/draft_codec'), 'decode', str(source), str(destination)]
            record = {'source': str(source), 'project': project.name,
                      'relative_path': str(relative), 'source_bytes': source.stat().st_size,
                      'source_sha256_before': before, 'output': str(destination),
                      'network_denied': True, 'writes_confined_to_tool_directory': True}
            logfile = destination.with_suffix('.process.log')
            try:
                with logfile.open('x') as log:
                    proc = subprocess.run(command, stdout=log, stderr=log, cwd=ROOT,
                                          timeout=45, preexec_fn=child_limits)
                record['returncode'] = proc.returncode
                if proc.returncode == 0 and destination.is_file():
                    raw = destination.read_bytes()
                    parsed = json.loads(raw)
                    record['output_sha256'] = hashlib.sha256(raw).hexdigest()
                    record['output_bytes'] = len(raw)
                    record['summary'] = summarize(parsed, destination.with_suffix('.timeline.csv'))
                    record['json_parsed'] = True
                else:
                    record['json_parsed'] = False
            except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
                record['json_parsed'] = False
                record['error'] = type(exc).__name__ + ': ' + str(exc)
            record['source_sha256_after'] = digest(source)
            record['source_unchanged'] = before == record['source_sha256_after']
            records.append(record)
            print(json.dumps({'project': project.name, 'file': str(relative),
                              'json_parsed': record['json_parsed'],
                              'source_unchanged': record['source_unchanged'],
                              'summary': record.get('summary')}, ensure_ascii=False), flush=True)
            if not record['source_unchanged']:
                raise RuntimeError('Source changed during read; stop experiment')
    for record in records:
        record['source_unchanged_at_end'] = digest(Path(record['source'])) == record['source_sha256_before']
    report = {'run_id': run_id, 'app_version': info.get('CFBundleShortVersionString'),
              'library_sha256': build['library_sha256'], 'records': records,
              'all_json_parsed': bool(records) and all(r['json_parsed'] for r in records),
              'all_sources_unchanged': all(r['source_unchanged_at_end'] for r in records)}
    (run_dir / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    (ROOT / 'results/latest-run.txt').write_text(str(run_dir) + '\n')
    print(json.dumps({'run_dir': str(run_dir), 'tested': len(records),
                      'all_json_parsed': report['all_json_parsed'],
                      'all_sources_unchanged': report['all_sources_unchanged']}, ensure_ascii=False), flush=True)
    return 0 if report['all_json_parsed'] and report['all_sources_unchanged'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('projects', nargs='+', type=Path)
    args = parser.parse_args()
    raise SystemExit(run(args.projects))
