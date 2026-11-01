"""Weekly Agent + configured-model assessments; no lifetime score or costs."""
from datetime import date,datetime,timedelta
import json
import re
import time
from zoneinfo import ZoneInfo

from .store import Conflict,canonical,digest

ZONE=ZoneInfo('Asia/Shanghai')


def week_of(day):
    value=date.fromisoformat(day)
    monday=value-timedelta(days=value.weekday())
    return monday.isoformat(),(monday+timedelta(days=6)).isoformat()


class TaskScoring:
    def __init__(self,runtime):self.runtime,self.store=runtime,runtime.store

    def inputs(self,node_id,day):
        with self.store.native.read_only() as db:source=self.store.source(db,node_id)
        adapter=self.runtime.backends.adapter_for(source)
        workspace=getattr(adapter,'workspace',None)
        if workspace is None:raise ValueError('此节点尚无可核对的工作区台账')
        return workspace.workday_inputs(day)

    def assess(self,node_id,day,task_id,task_type,outcome,evidence_refs,expected_revision,assessment_id):
        if outcome not in {'good','not_good'} or type(expected_revision) is not int or expected_revision<0:
            raise ValueError('任务评价只接受good或not_good及当前评价修订号')
        if (not isinstance(task_type,str) or not task_type.strip() or len(task_type)>128
                or not isinstance(assessment_id,str) or not assessment_id or len(assessment_id)>128):
            raise ValueError('需要明确任务类型与稳定评价ID')
        if not isinstance(evidence_refs,list) or not evidence_refs or any(not isinstance(v,str) or not v.strip() or len(v)>2048 for v in evidence_refs):
            raise ValueError('评分必须提供主入口实际核验的依据，不能直接采用Agent自评分')
        facts=[v for v in self.inputs(node_id,day) if v['task_id']==task_id and not v['health_check']]
        if not facts:raise ValueError('找不到已接收的业务任务回执；健康探针不能计为业务能力分')
        models={v['model'] for v in facts}
        if len(models)!=1 or None in models:raise ValueError('同一任务的模型归属不明确')
        if outcome=='good':
            if any(v['status']!='completed' for v in facts):
                raise ValueError('受阻或失败的工作回执不能记为完成得好')
            if any(v.get('is_review') and not v.get('review_qualified') for v in facts):
                raise ValueError('审查不合格的回执不能记为完成得好')
        model=next(iter(models))
        from xiaoba_agentos.pool_dispatch import mapped_role
        with self.store.native.read_only() as db:
            role=mapped_role(self.store.source(db, node_id)['kind'])
        key=digest([facts[0]['source_ref'],model,task_id])
        # The day/weekly bucket comes from host completion time, not a date
        # invented in the agent's prose or a later assessment timestamp.
        completed_day=datetime.fromtimestamp(max(v['completed_at'] for v in facts),ZONE).date().isoformat()
        week,_=week_of(completed_day)
        value={'node_id':node_id,'role':role,'source_ref':facts[0]['source_ref'],'model':model,'task_id':task_id,
            'task_type':task_type.strip(),'date':day,'completed_date':completed_day,'week_start':week,'outcome':outcome,
            'points':1 if outcome=='good' else 0,'evidence_refs':evidence_refs,
            'receipt_digests':sorted({v['receipt_sha256'] for v in facts}),
            'evaluator':'trusted_host_after_verification','policy':'weekly-task-plus-one-otherwise-zero-v1'}
        from xiaoba_agentos.memory import MemoryBridge
        if MemoryBridge._sensitive_reason(canonical(value)):raise ValueError('评分依据不能含敏感资料')
        with self.store.transaction() as db:
            old=db.execute('SELECT * FROM ri_task_assessments WHERE id=?',(key,)).fetchone()
            old_value=json.loads(old['data']) if old else None
            if old and old['assessment_id']==assessment_id:
                if old_value!=value:raise Conflict('同一评价ID不能写入不同内容')
                return dict(value,revision=old['revision'])
            revision=old['revision'] if old else 0
            if revision!=expected_revision:raise Conflict('任务评价已变化，请读取当前修订号')
            if old_value and old_value['week_start']!=week:raise Conflict('不能把同一任务移到另一个周重复计分')
            db.execute('INSERT INTO ri_task_assessments VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,revision=excluded.revision,assessment_id=excluded.assessment_id,updated_at=excluded.updated_at',
                (key,canonical(value),revision+1,assessment_id,time.time()))
            self.store.native._append_event_tx(db,None,'task_scoring.assessed',
                {'assessment_id':assessment_id,'key':key,'revision':revision+1,
                 'previous':old_value,'current':value,'delta':value['points']-(old_value['points'] if old_value else 0)})
        return dict(value,revision=revision+1)

    def weekly(self,day,task_type=None):
        start,end=week_of(day)
        with self.store.native.read_only() as db:
            if not self.store.native.verify_event_chain_tx(db)['ok']:raise ValueError('事件链无效，不能使用评分择优')
            stored=list(db.execute('SELECT * FROM ri_task_assessments'))
            latest={}
            for event in db.execute("SELECT payload_json FROM events WHERE event_type='task_scoring.assessed' ORDER BY rowid"):
                value=json.loads(event[0]);latest[value['key']]=value
            if {v['id'] for v in stored}!=set(latest):raise ValueError('评分表与审计事件不一致')
            for value in stored:
                event=latest[value['id']]
                if value['revision']!=event['revision'] or json.loads(value['data'])!=event['current']:
                    raise ValueError('评分表内容与最新审计事件不一致')
            rows=[json.loads(v['data']) for v in stored]
        groups={}
        for value in rows:
            if value['week_start']!=start or (task_type and value['task_type']!=task_type):continue
            # Check the source work log again; historical rewrites cannot keep
            # supplying an apparently valid ability score.
            facts=self.inputs(value['node_id'],value['date'])
            digests={v['receipt_sha256'] for v in facts if v['task_id']==value['task_id'] and not v['health_check']}
            if not set(value['receipt_digests']).issubset(digests):raise ValueError('历史评分的任务证据已失效')
            role=value.get('role') or ''
            key=(role, value['source_ref'], value['model'])
            bucket=groups.setdefault(key,{'role':role,'source_ref':value['source_ref'],'model':value['model'],'node_ids':[],
                'points':0,'tasks':0,'good_tasks':0,'not_good_tasks':0,'task_types':{}})
            if value['node_id'] not in bucket['node_ids']:bucket['node_ids'].append(value['node_id'])
            bucket['points']+=value['points'];bucket['tasks']+=1
            bucket['good_tasks' if value['outcome']=='good' else 'not_good_tasks']+=1
            bucket['task_types'][value['task_type']]=bucket['task_types'].get(value['task_type'],0)+1
        ranking=list(groups.values())
        for value in ranking:value['success_rate']=value['good_tasks']/value['tasks']
        ranking.sort(key=lambda v:(-v['points'],-v['success_rate'],-v['tasks'],v.get('role') or '',v['source_ref'],v['model']))
        return {'week_start':start,'week_end':end,'timezone':'Asia/Shanghai','task_type':task_type,
            'policy':'good +1; not_good 0; one whole task counted once',
            'ranking':ranking,'lifetime_score':False,'resource_inputs_collected':False,
            'selection_requires_current_authorization_and_health':True}
