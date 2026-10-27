import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';

// Separate JSON parser and semantic checks, independent of the Python reader.
const root = path.dirname(fileURLToPath(import.meta.url));
const run = fs.readFileSync(path.join(root, 'results/latest-run.txt'), 'utf8').trim();
const report = JSON.parse(fs.readFileSync(path.join(run, 'report.json'), 'utf8'));
const sha = p => crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');
const checks = [];
for (const record of report.records) {
  const d = JSON.parse(fs.readFileSync(record.output, 'utf8'));
  const check = {
    project: record.project,
    file: record.relative_path,
    json_parsed: true,
    source_sha_matches: sha(record.source) === record.source_sha256_before,
    output_sha_matches: sha(record.output) === record.output_sha256,
  };
  if (record.relative_path.endsWith('draft_info.json')) {
    if (!Array.isArray(d.tracks) || !d.materials || typeof d.duration !== 'number')
      throw new Error('Missing timeline schema fields');
    const ids = new Set(Object.values(d.materials).filter(Array.isArray)
      .flat().filter(m => m && typeof m.id === 'string').map(m => m.id));
    const segments = d.tracks.flatMap(t => t.segments ?? []);
    check.segment_count = segments.length;
    check.segment_count_matches = segments.length === record.summary.segment_count;
    check.refs_resolved = segments.every(s => (!s.material_id || ids.has(s.material_id))
      && (s.extra_material_refs ?? []).every(id => ids.has(id)));
    check.ranges_valid = segments.every(s => {
      const t = s.target_timerange;
      // Native serialization omits numeric zero fields, including start=0.
      const start = t?.start ?? 0;
      const duration = t?.duration ?? 0;
      return t && Number.isFinite(start) && Number.isFinite(duration)
        && start >= 0 && duration >= 0 && start + duration <= d.duration + 1e6 / (d.fps || 30);
    });
    const end = Math.max(0, ...segments.map(s =>
      (s.target_timerange.start ?? 0) + (s.target_timerange.duration ?? 0)));
    check.end_matches = Math.abs(end - d.duration) <= 1e6 / (d.fps || 30);
  } else if (!Array.isArray(d.draft_materials) || typeof d.draft_name !== 'string') {
    throw new Error('Missing metadata schema fields');
  }
  check.passed = Object.entries(check).filter(([k]) => !['project', 'file', 'segment_count'].includes(k))
    .every(([, v]) => v === true);
  checks.push(check);
}
const result = {parser: 'Node.js JSON.parse + node:crypto',
  zero_field_semantics: 'Omitted numeric start/duration fields default to zero in native JSON',
  files: checks.length,
  timeline_files: checks.filter(x => 'segment_count' in x).length,
  all_passed: checks.every(x => x.passed), checks};
fs.writeFileSync(path.join(root, 'results/independent-verification-v2.json'),
  JSON.stringify(result, null, 2), {flag: 'wx', mode: 0o600});
console.log(JSON.stringify({files: result.files, timeline_files: result.timeline_files, all_passed: result.all_passed}));
if (!result.all_passed) process.exitCode = 1;
