"""Weekly Agent + configured-model assessments; no lifetime score or costs."""
from datetime import date,datetime,timedelta
import json
import re
import time
from zoneinfo import ZoneInfo

from .store import Conflict,canonical,digest
from xiaoba_agentos import task_scores

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

    def assess(self,node_id,day,task_id,task_type,outcome,evidence_refs,expected_revision,assessment_id,role=None):
        if outcome not in {'good','not_good'} or type(expected_revision) is not int or expected_revision<0:
            raise ValueError('任务评价只接受good或not_good及当前评价修订号')
        if (not isinstance(task_type,str) or not task_type.strip() or len(task_type)>128
                or not isinstance(assessment_id,str) or not assessment_id or len(assessment_id)>128):
            raise ValueError('需要明确任务类型与稳定评价ID')
        if not isinstance(evidence_refs,list) or not evidence_refs or any(not isinstance(v,str) or not v.strip() or len(v)>2048 for v in evidence_refs):
            raise ValueError('评分必须提供主入口实际核验的依据，不能直接采用Agent自评分')
        # Native tasks are automatically scored at settlement. This endpoint
        # corrects that same row, even if the node was renamed/reconfigured.
        if self.store.native.get_task(task_id):
            return self._correct_native(node_id,task_id,outcome,evidence_refs,expected_revision,assessment_id,role)
        if role is not None and role not in {'review','collaborate'}:raise ValueError('更正必须使用审查或协作执行角色')
        facts=[v for v in self.inputs(node_id,day) if v['task_id']==task_id and not v['health_check'] and
               (role is None or ('review' if v.get('is_review') else 'collaborate')==role)]
        if not facts:raise ValueError('找不到已接收的业务任务回执；健康探针不能计为业务能力分')
        models={v['model'] for v in facts}
        if len(models)!=1 or None in models:raise ValueError('同一任务的模型归属不明确')
        if outcome=='good':
            if any(v['status']!='completed' for v in facts):
                raise ValueError('受阻或失败的工作回执不能记为完成得好')
            if any(v.get('is_review') and not v.get('review_qualified') for v in facts):
                raise ValueError('审查不合格的回执不能记为完成得好')
        model=next(iter(models))
        roles={'review' if v.get('is_review') else 'collaborate' for v in facts}
        if len(roles)!=1:raise ValueError('多角色任务更正必须明确角色')
        role=next(iter(roles))
        key=digest([task_id,node_id,role])
        # The day/weekly bucket comes from host completion time, not a date
        # invented in the agent's prose or a later assessment timestamp.
        completed_day=datetime.fromtimestamp(max(v['completed_at'] for v in facts),ZONE).date().isoformat()
        week,_=week_of(completed_day)
        value={'node_id':node_id,'role':role,'source_ref':facts[0]['source_ref'],'model':model,'task_id':task_id,
            'task_type':task_type.strip(),'date':day,'completed_date':completed_day,'week_start':week,'outcome':outcome,
            'points':1 if outcome=='good' else 0,'evidence_refs':evidence_refs,
            'tasks':1,'success_rate':1.0 if outcome=='good' else 0.0,
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

    def _correct_native(self,node_id,task_id,outcome,evidence_refs,expected_revision,assessment_id,role):
        from xiaoba_agentos.memory import MemoryBridge
        if MemoryBridge._sensitive_reason(canonical(evidence_refs)):raise ValueError('评分依据不能含敏感资料')
        with self.store.transaction() as db:
            task_scores.rows_tx(db)
            choices=[r for r in db.execute('SELECT * FROM ri_task_assessments')
                if (v:=json.loads(r['data'])).get('task_id')==task_id and v.get('node_id')==node_id and
                (role is None or v.get('role')==role)]
            if len(choices)!=1:raise ValueError('需要已结算的任务评分；多角色任务更正必须明确角色')
            old=choices[0];previous=json.loads(old['data'])
            if outcome=='good' and not (previous.get('receipt_accepted') and previous.get('task_completed')):
                raise ValueError('未接收的回执或未完成任务不能更正为1分')
            value=dict(previous,outcome=outcome,points=int(outcome=='good'),evidence_refs=evidence_refs,
                       tasks=1,success_rate=float(outcome=='good'),evaluator='trusted_host_correction')
            if old['assessment_id']==assessment_id:
                if value!=previous:raise Conflict('同一评价ID不能写入不同内容')
                return dict(value,revision=old['revision'])
            if old['revision']!=expected_revision:raise Conflict('任务评价已变化，请读取当前修订号')
            revision=old['revision']+1
            db.execute('UPDATE ri_task_assessments SET data=?,revision=?,assessment_id=?,updated_at=? WHERE id=?',
                       (canonical(value),revision,assessment_id,time.time(),old['id']))
            self.store.native._append_event_tx(db,task_id,'task_scoring.assessed',
                {'assessment_id':assessment_id,'key':old['id'],'revision':revision,'previous':previous,
                 'current':value,'delta':value['points']-previous['points']})
        return dict(value,revision=revision)

    def weekly(self,day,task_type=None):
        with self.store.native.read_only() as db:
            rows=task_scores.rows_tx(db,raise_on_inconsistent=True)
        for value in rows:
            if value.get('run_id'):continue
            if value['week_start']!=week_of(day)[0] or (task_type and value['task_type']!=task_type):continue
            facts=self.inputs(value['node_id'],value['date'])
            digests={v['receipt_sha256'] for v in facts if v['task_id']==value['task_id'] and not v['health_check']}
            if not set(value['receipt_digests']).issubset(digests):raise ValueError('历史评分的任务证据已失效')
        return task_scores.weekly(rows,day,task_type)
