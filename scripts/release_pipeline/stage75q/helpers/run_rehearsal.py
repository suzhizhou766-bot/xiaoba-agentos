"""Real launchd + Worker rehearsal using the frozen package's switch implementation."""

# --- rebound by stage75q ---
"""stage75q helper — rebound from mature v1614-r7 source; paths via STAGE75Q_BIND_JSON."""
from pathlib import Path
import os, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bind import load_bind
_BIND = load_bind()
_BIND.require_ready()
R = _BIND.root
O = _BIND.O
A = _BIND.A
W = _BIND.W
C = _BIND.C
PKG = _BIND.PKG
VERSION = _BIND.version
LIVE = _BIND.live
P = LIVE
os.chdir(R)
# --- end bind header; original body follows with path overrides ---
import os
from pathlib import Path
# Pending v1614-r7 must never consume historical evidence or run before new-BASE preparation.
assert (_BIND.work / 'BASE_READY.json').is_file(), 'STOP: BASE_READY missing'
os.chdir(R)
import hashlib
import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = _BIND.root
from leaf_contracts import require_acceptance_passed, resolve_fake_dispatch


def require_review_task(state):
    # This release's STATE maps the acceptance review, not the fourteen later
    # scenario results (those are checked by Orchestrator.assert_fourteen).
    tasks = state.get('tasks')
    if (not isinstance(tasks, dict) or set(tasks) != {'review'}
            or not isinstance(tasks['review'], str) or not tasks['review'].strip()):
        raise RuntimeError(('STOP: expected exactly one acceptance review task', tasks))


def require_interpreter_observations(observations, pin):
    assert (observations['worker_on_pinned']
            and observations['doctor_interpreter']['running_is_pinned']
            and observations['scene_under_documents']
            and observations['worker_state_written']), observations
    assert all(v == '#!' + pin for v in observations['entry_first_lines'].values()), observations


state=json.loads((A/'STATE.json').read_text())
scene=json.loads((O/('SCENE-formal.json' if '--formal-confirmation' in sys.argv else 'SCENE.json')).read_text())
require_acceptance_passed(state)
require_review_task(state)
_review = _BIND.review_report
assert _review is not None and json.loads(_review.read_text()).get('ok') is True
package=Path(state['verified_package'])
spec=importlib.util.spec_from_file_location('frozen_switch_rehearsal',package/'agentos/scripts/switch_release.py')
sw=importlib.util.module_from_spec(spec);sys.modules[spec.name]=sw;spec.loader.exec_module(sw)
assert sw.state(package/'agentos/SOURCE_MANIFEST.json')['sha256']==state['source_manifest_sha256']
layout=sw.Layout(Path(scene['runtime']),Path(scene['config']),package,Path(scene['vault']),Path(scene['graph']),tuple(map(Path,scene['entries'])),Path(scene['plist']),scene['label'])
assert layout.label.startswith('com.xiaoba.agentos.rehearsal.')
assert Path(scene['root']) in layout.runtime.parents
system=sw.System()
formal_confirmation='--formal-confirmation' in sys.argv
FAKE_DISPATCH=resolve_fake_dispatch(_BIND)
report_path=O/('30-正式包复制现场确认.json' if formal_confirmation else '30-复制现场演练.json')
report_path.parent.mkdir(parents=True, exist_ok=True)
def dump(p,obj):p.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+chr(10))
def evidence():
    with sqlite3.connect(layout.database.as_uri()+'?mode=ro',uri=True) as c:
        rows={name:[list(x) for x in c.execute('SELECT * FROM '+name+' ORDER BY rowid')] for name in ('tasks','memory_candidates','memory_reviews','events','strategies','evaluation_cases','evaluations','evaluation_runs','promotion_gates','strategy_audit')}
        rows['__user_version']=[[c.execute('PRAGMA user_version').fetchone()[0]]]
    retained_facts={p:{'sha256':sw.state(Path(p))['sha256'],'dev':Path(p).stat().st_dev,'ino':Path(p).stat().st_ino} for p in scene['retained_fixture_objects']}
    return {'retained_facts':retained_facts,'database_content_sha256':hashlib.sha256(sw.encoded(rows)).hexdigest(),
            'vault':sw.files(layout.vault),'config':sw.state(layout.config),'source':sw.state(layout.runtime/'SOURCE_MANIFEST.json')}

report={'source_manifest_sha256':state['source_manifest_sha256'],'package':str(package),
        'switch_sha256':sw.state(package/'agentos/scripts/switch_release.py')['sha256'],
        'real_service_label':layout.label,'system_substitution':False,'fault_injection':'existing phase callback only',
        'scene':str(scene['root']),'cases':[],'production_switch':False,'formal_package_confirmation':formal_confirmation,'package_acceptance_status':'verified' if state.get('verified_package') else 'code_review_accepted_rehearsal_only_final_integration_pending','started_at':time.time()}
original=evidence()
original_route=json.loads(Path(layout.config).read_text())['cli_routes']['local_agy']
expected_route=original_route.copy()
assert original_route.get('selection')
report['local_agy_before']=original_route
try:
    if not system.registered(layout):
        system.start(layout)
        system.wait(lambda: layout.worker.exists() and len(system.worker_pids(layout))==1,'copied old worker did not start')
    sw.worker(layout,system)
    # Exercise all major completed phase boundaries, including after the real new worker/readback.
    phases=['stage_package','stop_worker','replace_source','install_entries','bind_config','release_steps',
            'vault_merge','rebuild_graph','publish_index','start_worker','readback']
    if formal_confirmation:phases=['readback']
    for target in phases:
        ticket=sw.preflight(layout,system)
        assert ticket['ok'],ticket
        def fault(name):
            if name==target:raise sw.SwitchError('deliberate rehearsal failure after '+target)
        result=sw.apply(layout,ticket,system,fault=fault)
        after=evidence()
        case={'fault_after':target,'result':result,'protected_database_unchanged':after['database_content_sha256']==original['database_content_sha256'],
              'vault_bytes_restored':after['vault']==original['vault'],'config_restored':after['config']==original['config'],
              'local_agy_restored':json.loads(Path(layout.config).read_text())['cli_routes']['local_agy']==original_route,
              'source_restored':after['source']==original['source'],'claimed_inodes_and_fd_bytes_preserved':after['retained_facts']==original['retained_facts']}
        report['cases'].append(case);dump(report_path,report)
        print(json.dumps({'fault_after':target,'rollback':result.get('rollback'),'protected_database_unchanged':case['protected_database_unchanged']},ensure_ascii=False),flush=True)
        assert not result['ok'] and result['rollback']['ok'] and result['rollback']['status']=='已退回',result
        assert all(case[k] for k in ('protected_database_unchanged','vault_bytes_restored','config_restored','local_agy_restored','source_restored','claimed_inodes_and_fd_bytes_preserved')),case
    ticket=sw.preflight(layout,system);assert ticket['ok'],ticket
    result=sw.apply(layout,ticket,system)
    after=evidence()
    report['success']={'result':result,'protected_database_unchanged':after['database_content_sha256']==original['database_content_sha256'],'claimed_inodes_and_fd_bytes_preserved':after['retained_facts']==original['retained_facts']}
    assert result['ok'] and result['activated'] and result['read_back']['restart_verified'],result
    assert result['read_back']['version']==VERSION and report['success']['protected_database_unchanged'] and report['success']['claimed_inodes_and_fd_bytes_preserved']
    report['local_agy_after']=json.loads(Path(layout.config).read_text())['cli_routes']['local_agy']
    assert report['local_agy_after']==expected_route
    # 本版专项：交还笔记库的笔记经过切换后逐字节不变；变化的笔记只能是仍由发布生成的那些。
    handover=set(json.loads((package/'agentos/docs/second_brain/handover.json').read_text())['notes'])
    generated=set(sw.files(package/'second-brain'))
    changed={p for p in set(original['vault'])|set(after['vault']) if original['vault'].get(p)!=after['vault'].get(p)}
    # v1.6.4 专项：6 张策略表与 user_version 已并入 protected_database_unchanged 的比对范围。
    import sqlite3 as _sq
    with _sq.connect(layout.database.as_uri()+'?mode=ro',uri=True) as _c:
        report['schema_after_success']={'user_version':_c.execute('PRAGMA user_version').fetchone()[0],'strategy_rows':{t:_c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('strategies','evaluation_cases','evaluations','evaluation_runs','promotion_gates','strategy_audit')}}
    assert report['schema_after_success']['user_version']==7,report['schema_after_success']
    # v1.6.7 专项：切换成功后前版发布目录只读，doctor 如实报告；版本取 previous_package RELEASE，不写死。
    from leaf_contracts import previous_package_version
    sealed_pkg=layout.index.parent/previous_package_version(_BIND)
    writable=[str(p) for p in [sealed_pkg,*sealed_pkg.rglob('*')] if p.stat().st_mode & 0o222]
    report['release_sealed_after_success']={'switch_report':result.get('release_sealed'),'writable_entries':len(writable)}
    assert result.get('release_sealed',{}).get('ok') and not writable,report['release_sealed_after_success']
    # v1.6.3 起：新 Worker 由签名 Runner 经 launchd 拉起，命令行必须是固定的 3.14 解释器；现场在 Documents 下。
    PIN='/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14'
    pids=system.worker_pids(layout);assert len(pids)==1,pids
    cmd=system.process(pids[0])
    doctor=system.json_command([str(layout.entries[0]/'scripts/xiaoba'),'doctor','--json'])
    first_lines={str(h):(h/'scripts/xiaoba').read_text().splitlines()[0] for h in layout.entries}
    err=Path(scene['root'])/'worker.err.log'
    err_bytes=err.read_bytes() if err.exists() else b''
    err_tail=err_bytes.decode(errors='replace')[-3000:]
    report['interpreter_after_success']={'worker_pid':pids[0],'worker_command':cmd,'worker_on_pinned':cmd.startswith(PIN+' '),
        'doctor_interpreter':doctor.get('interpreter'),'entry_first_lines':first_lines,'scene_under_documents':str(layout.runtime).startswith('/Users/suzhizhou/Documents/'),
        'worker_state_written':layout.worker.exists(),'worker_err_tail':err_tail,
        'worker_err_bytes':len(err_bytes),
        'stderr_scope':'diagnostic tail only; platform wording does not determine rehearsal result',
        'behavior_scope':'unique worker PID, pinned command and doctor, Documents path, worker state, entry shebangs; successful restart/readback checked above'}
    ia=report['interpreter_after_success']
    require_interpreter_observations(ia, PIN)
    handover=set(json.loads((package/'agentos/docs/second_brain/handover.json').read_text())['notes'])
    generated=set(sw.files(package/'second-brain'))
    changed={p for p in set(original['vault'])|set(after['vault']) if original['vault'].get(p)!=after['vault'].get(p)}
    report['vault_after_success']={'changed_notes':sorted(changed),'handed_over_notes':len(handover),
        'handed_over_all_present':all(p in after['vault'] for p in handover),
        'handed_over_unchanged':all(original['vault'].get(p)==after['vault'].get(p) for p in handover),
        'changed_only_generated':changed<=generated,'notes_before':len(original['vault']),'notes_after':len(after['vault'])}
    assert report['vault_after_success']['handed_over_all_present'] and report['vault_after_success']['handed_over_unchanged'] and report['vault_after_success']['changed_only_generated'],report['vault_after_success']
    import subprocess,os
    def content_sync(*extra):
        p=subprocess.run(['/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14','-B','-m','xiaoba_agentos.cli','--config',str(layout.config),'content-notes-sync',*extra],cwd=layout.runtime,env=dict(os.environ,PYTHONPATH=str(layout.runtime/'src'),PYTHONDONTWRITEBYTECODE='1'),capture_output=True,text=True)
        return {'exit':p.returncode,'output':json.loads(p.stdout) if p.stdout.strip().startswith('{') else p.stdout+p.stderr}
    cards_before={str(p.relative_to(layout.vault)):p.read_text() for p in sorted((layout.vault/'20_能力与技能').glob('*.md'))}
    # Card31 limits production interaction to explicit read-only plan/preflight later.
    report['content_notes']={'verify_before':content_sync('--verify'),'sync':content_sync(),'verify_after':content_sync('--verify'),
        'verify_before_explanation':'现场笔记是线上笔记的原样拷贝，技能卡的 source_path 指向线上 私人助理/skills；现场配置的技能目录在现场助理目录下，所以同步前核对不一致是预期的'}
    assert report['content_notes']['sync']['exit']==0 and report['content_notes']['verify_after']['exit']==0,report['content_notes']
    synced=sw.files(layout.vault)
    report['content_notes']['notes_changed_by_sync']=sorted(p for p in synced if synced[p]!=after['vault'].get(p))
    LIVE_SKILLS='/Users/suzhizhou/Documents/私人助理/skills/';SCENE_SKILLS=str(layout.assistant/'skills')+'/'
    card_checks={}
    for rel in report['content_notes']['notes_changed_by_sync']:
        old=cards_before.get(rel);new=(layout.vault/rel).read_text()
        assert old is not None,('content sync changed a note that is not a skill card',rel)
        a,b=old.splitlines(),new.splitlines()
        pairs=[(x,y) for x,y in zip(a,b) if x!=y]
        card_checks[rel]={'same_line_count':len(a)==len(b),'changed_lines':len(pairs),
            'only_skill_root_remapped':len(a)==len(b) and bool(pairs) and all(x.replace(LIVE_SKILLS,SCENE_SKILLS)==y for x,y in pairs),'diff':pairs}
    report['content_notes']['skill_card_changes']=card_checks;report['content_notes']['skill_card_changes_in_handover']=sorted(r for r in card_checks if r in handover)
    assert all(c['only_skill_root_remapped'] for c in card_checks.values()),card_checks
    # Formal index builder independently verifies the exact graph written by switch_release.
    verify=system.command(['/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14','-B',str(layout.runtime/'scripts/build_knowlp_index.py'),'--vault',str(layout.vault),'--graph-dir',str(layout.graph),'--verify'])
    report['official_graph_verify']={'exit':verify.returncode,'stdout':verify.stdout,'stderr':verify.stderr}
    assert verify.returncode==0
    # The runner above has no generic PYTHONPATH override; run source-bound CLI through explicit fresh interpreter.
    import subprocess,os
    check=subprocess.run(['/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14','-B','-m','xiaoba_agentos.cli','--config',str(layout.config),'memory-retained-list'],cwd=layout.runtime,env=dict(os.environ,PYTHONPATH=str(layout.runtime/'src'),PYTHONDONTWRITEBYTECODE='1'),capture_output=True,text=True)
    view=json.loads(check.stdout)
    dump(O/('30-正式包重启保留清单.json' if formal_confirmation else '30-重启保留清单.json'),view)
    assert view.get('enumeration_ok') is True,view
    listed={obj['retained_path'] for obj in view['objects']}
    assert set(scene['retained_fixture_objects']).issubset(listed),view
    report['post_switch_retained_inventory']={'exit_code':check.returncode,'retained_count':view['retained_count'],'all_fixture_paths_listed':True,'manual_action_required':view['manual_action_required'],'fresh_process':True,'exit_code_meaning':'有待人工核对的保留物时 ok=false、退出码 1；演练故意放了 3 个保留物验证切换会保住它们，所以 1 是预期','ok':view.get('ok'),'enumeration_ok':view.get('enumeration_ok'),'anomalies':view.get('anomalies'),'problems':view.get('problems')}
    # Card31 does not register standby or replace routes after numbered migration.
    report['doctor_after_success']=doctor
    report['review_nodes_after_success']=doctor.get('agent_pool') or doctor.get('review_nodes')
    
    # Leaf-safe: load pointer r5 + apply manager migration-spec to INDEXES/EXPECTED_INDEX_SQL
    # (same effect as prepare_round-r4.run_migration). expected{} alone does not parameterize.
    from leaf_contracts import load_migration_checks_module, run_declared_migration_checks
    PREVIOUS_PACKAGE=Path(_BIND.previous_package)
    pkg_release=json.loads((package/'RELEASE.json').read_text())
    EXPECTED_RELEASE_TIER=pkg_release['release_tier']['tier']
    if _BIND.data.get('expected_release_tier') and EXPECTED_RELEASE_TIER!=_BIND.data['expected_release_tier']:
        raise RuntimeError(('STOP: package release_tier mismatch', EXPECTED_RELEASE_TIER, _BIND.data['expected_release_tier']))
    _mc_probe=load_migration_checks_module(_BIND)
    report['migration_before_indexes']=sorted(_mc_probe.list_indexes(layout.database))
    report['migration_declared']=run_declared_migration_checks(
        _BIND, layout.database, previous_package=PREVIOUS_PACKAGE, scene_root=Path(scene['root']))
    report['migration_checks']=report['migration_declared']['migration_checks']
    report['migration_indexes_after_success']=report['migration_declared']['indexes_present']
    report['migration_fingerprint_after_success']=report['migration_declared']['fingerprint']
    report['migration_expected']=report['migration_declared']['declaration']
    report['migration_parameterized']=report['migration_declared']['parameterized']
    report['migration_rollback_previous_package']=report['migration_declared']['migration_rollback_previous_package']
    report['release_tier']=EXPECTED_RELEASE_TIER
    
    assert doctor.get('ok') is True,doctor
    import subprocess,os
    q=subprocess.run([sys.executable,'-B',str(FAKE_DISPATCH)],cwd=ROOT,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'),capture_output=True,text=True)
    report['fake_dispatch']={'exit':q.returncode,'stdout':q.stdout,'stderr':q.stderr}
    dump(report_path,report)
    assert q.returncode==0,report['fake_dispatch']
    report['ok']=True
finally:
    # Stop only this task's isolated service; production stays untouched.
    cleanup={'service_label':layout.label}
    try:
        current=sw.load(layout.worker) if layout.worker.exists() else {'pid':0}
        if system.registered(layout):system.stop(layout,int(current['pid']))
        cleanup['service_absent']=not system.registered(layout)
        cleanup['bound_worker_absent']=not system.worker_pids(layout)
    except Exception as exc:cleanup['error']=str(exc)
    cleanup['ok']=cleanup.get('service_absent') is True and cleanup.get('bound_worker_absent') is True
    report['ok']=bool(report.get('ok')) and cleanup['ok']
    report['cleanup']=cleanup;report['finished_at']=time.time()
    dump(report_path,report)
    print(json.dumps({'ok':report.get('ok',False),'cleanup':cleanup},ensure_ascii=False),flush=True)
assert report.get('ok') is True, 'rehearsal or isolated-service cleanup did not fully pass'
