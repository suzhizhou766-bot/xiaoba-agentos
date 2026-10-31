"""Ask for missing user facts; audited tests permit subsequent direct dispatch.

Resource readiness is never a human experience approval.
"""
import json
import time
from .agent_pool import independence, node_state, require_enabled, resource_plan
from .store import Conflict, canonical, digest, identifier
from xiaoba_agentos.output_hygiene import contains_non_answer_markers, detect_reasoning_leak


FIELDS = {'name':'Agent 如何命名？', 'interface':'使用哪种调用方式：CLI、桌面端还是 Web？',
          'entry':'该 Agent 的调用入口是什么？', 'model':'该 Agent 使用哪个明确模型？'}


def spec(source):
    node = node_state(source)
    return {'node_id':source['reviewer_id'], 'source_ref':source['source_ref'], 'kind':source['kind'],
            **{k:node.get(k) for k in ('name','agent','interface','entry','model')}}


class NodeRuntime:
    def __init__(self, pool):
        self.pool, self.store, self.backends = pool, pool.store, pool.backends
        from .task_scoring import TaskScoring
        self.scoring = TaskScoring(self)

    def start_workday(self,user_message,message_id,required_kinds,selections=None):
        if user_message!='上班了小八助理':raise ValueError('只有用户真正发出的完整开工口令才能执行开工检测')
        if not isinstance(message_id,str) or not message_id or len(message_id)>128:raise ValueError('需要稳定的用户开工消息ID')
        if not isinstance(required_kinds,list) or not required_kinds or set(required_kinds)-{'dsh','peer'} or len(set(required_kinds))!=len(required_kinds):
            raise ValueError('需要明确当天工作链路的后台槽位')
        selections=selections or {}
        if not isinstance(selections,dict) or set(selections)-set(required_kinds):raise ValueError('选择与当天链路不符')
        chosen={};questions=[];blockers=[]
        with self.store.native.read_only() as db:
            sources=[json.loads(v[0]) for v in db.execute('SELECT data FROM ri_sources')]
            for kind in required_kinds:
                candidates=[v for v in sources if v['kind']==kind and node_state(v).get('enabled') and not node_state(v).get('removed')]
                if kind in selections:candidates=[v for v in candidates if v['reviewer_id']==selections[kind]]
                if len(candidates)!=1:
                    questions.append({'kind':kind,'question':'请明确本次开工链路使用的已开启节点','node_ids':[v['reviewer_id'] for v in candidates]});continue
                source=candidates[0];node=node_state(source)
                if any(not node.get(k) for k in FIELDS):
                    blockers.append({'node_id':source['reviewer_id'],'reason':'missing_configuration'});continue
                adapter=self.backends.adapter_for(source)
                if not getattr(adapter,'supports_workday_receipts',False):
                    blockers.append({'node_id':source['reviewer_id'],'reason':'workday_adapter_required'});continue
                chosen[kind]=source
        if questions or blockers:return {'route':'ask_user' if questions else 'blocked','questions':questions,'blockers':blockers,'model_tasks_sent':0}
        if len({independence(v) for v in chosen.values()})!=len(chosen):
            return {'route':'blocked','blockers':[{'reason':'independent_source_required'}],'model_tasks_sent':0}
        fingerprint=digest([required_kinds,selections])
        with self.store.transaction() as db:
            old=db.execute('SELECT * FROM ri_workday_starts WHERE message_id=?',(message_id,)).fetchone()
            if old:
                if old['plan_digest']!=fingerprint:raise Conflict('同一开工消息不能更换工作链路')
                return json.loads(old['data']) if old['data'] else {'route':'running','model_tasks_sent':0}
            db.execute('INSERT INTO ri_workday_starts VALUES (?,?,NULL)',(message_id,fingerprint))
            self.store.native._append_event_tx(db,None,'workday.started',{'message_id':message_id,'plan_digest':fingerprint})
        session=None;results=[];error_type=None
        try:
            session=self.store.native.start_session('Asia/Shanghai')
            for kind,source in chosen.items():
                node_id=source['reviewer_id']
                with self.store.native.read_only() as db:revision=node_state(self.store.source(db,node_id))['revision']
                result=self.test_run(node_id,revision,'workday-'+digest([message_id,node_id])[:48])
                results.append({'kind':kind,'node_id':node_id,'test':result})
                if result['status']!='passed':break
                adapter=self.backends.adapter_for(source)
                results[-1]['worklog_initialization']=adapter.initialize_workday(result['execution_evidence'],
                    'workday-init-'+digest([message_id,node_id])[:48])
        except Exception as exc:error_type=type(exc).__name__
        ready=error_type is None and len(results)==len(required_kinds) and all(v['test']['status']=='passed' and v.get('worklog_initialization') for v in results)
        value={'route':'ready' if ready else 'blocked','session':session,'nodes':results,
                'work_logs_initialized':ready,'automatic_retry':False,'experience_approval':False,
                'health_scope':'selected_backend_chain_and_owned_workspaces; not production doctor'}
        if error_type:value['error_type']=error_type
        with self.store.transaction() as db:
            db.execute('UPDATE ri_workday_starts SET data=? WHERE message_id=?',(canonical(value),message_id))
            self.store.native._append_event_tx(db,None,'workday.finished',{'message_id':message_id,'result':value})
        return value

    def workday_inputs(self,node_id,day):
        return {'node_id':node_id,'date':day,'tasks':self.scoring.inputs(node_id,day),
                'resource_inputs_collected':False,'raw_log_is_score':False}

    def execute_work(self,node_id,expected_revision,task_id,step_code,instruction,materials):
        with self.store.native.read_only() as db:
            # B20：事件链坏了不派活。先校验再调适配器，避免干完才发现坏链。
            if not self.store.native.verify_event_chain_tx(db)['ok']:
                raise Conflict('事件链校验失败，停止派工')
            source=self.store.source(db,node_id);node=require_enabled(source)
            if type(expected_revision) is not int or node['revision']!=expected_revision:raise Conflict('节点已变化，请核对当前计划')
            if node.get('interface') == 'tmux':
                # Attended work bypasses automated health probes, never review gates.
                attended = source
            else:attended = None
            if attended is None and not self.readiness(db,source)['ready']:raise ValueError('节点尚未通过当前配置健康检测，未派发工作')
            adapter=None if attended is not None else self.backends.adapter_for(source)
            if attended is None and not callable(getattr(adapter,'run_task',None)):raise ValueError('该节点尚未接入工作文件回执')
        if attended is not None:
            return self.pool.tmux.submit(attended,task_id,step_code,instruction,materials)
        return adapter.run_task(task_id,step_code,instruction,materials)

    def preview(self, node_id):
        if not isinstance(node_id,str) or not node_id or len(node_id)>128:
            raise ValueError('需要明确的节点ID')
        with self.store.native.read_only() as db:
            source = self.store.source(db,node_id)
            if node_state(source).get('removed'):raise ValueError('节点已移除')
        adapter = self.backends.peek_resource(node_id)
        snapshot = getattr(adapter,'terminal_snapshot',None)
        return snapshot() if callable(snapshot) else {
            'supported':False,'state':'not_connected','text':'','read_only':True}

    def onboard(self, draft, kind='peer'):
        if not isinstance(draft,dict) or set(draft)-set(FIELDS)-{'node_id','agent'} or kind not in {'dsh','peer'}:
            raise ValueError('对话配置只接受用户提供的节点名称、调用方式、入口和模型')
        from xiaoba_agentos.memory import MemoryBridge
        if MemoryBridge._sensitive_reason(canonical(draft)):
            raise ValueError('节点资料不能包含登录材料或密钥')
        for key,value in draft.items():
            if not isinstance(value,str) or len(value)>2048 or any(c in value for c in '\r\n\0'):
                raise ValueError('节点资料格式异常：'+key)
        missing = [k for k in FIELDS if not draft.get(k,'').strip()]
        if missing:
            return {'route':'ask_user','draft':draft,'kind':kind,
                    'questions':[{'field':k,'question':FIELDS[k]} for k in missing]}
        source = self.pool.add({'node_id':draft.get('node_id') or 'chat-'+digest([kind,draft])[:32], 'kind':kind,
            'agent':draft.get('agent') or draft['name'], **{k:draft[k] for k in FIELDS}})
        return {'route':'test_required','node_id':source['reviewer_id'],
                'message':'配置已保存；在原运行总览开启节点并测试运行，测试通过后可直接调用。'}

    def readiness(self, db, source):
        node=node_state(source)
        if node.get('removed'):return {'ready':False,'reason':'removed'}
        if not node.get('enabled'):return {'ready':False,'reason':'disabled'}
        if any(not node.get(k) for k in FIELDS):return {'ready':False,'reason':'missing_configuration'}
        if node.get('interface') == 'tmux':return {'ready':False,'reason':'tmux_collaborator_only'}
        if node.get('test_running'):return {'ready':False,'reason':'testing'}
        adapter=self.backends.adapter_for(source)
        if not adapter or not getattr(adapter,'supports_resource_plan',False):
            return {'ready':False,'reason':'adapter_required'}
        if getattr(adapter,'pending_action',None):return {'ready':False,'reason':'native_confirmation_required'}
        try:identity=adapter.resource_identity()
        except (OSError,ValueError,AttributeError,TypeError):return {'ready':False,'reason':'entry_changed_or_unavailable'}
        test=node.get('test',{})
        if test.get('status')!='passed' or test.get('spec_digest')!=digest(spec(source)) or test.get('runner_digest')!=digest(identity):
            return {'ready':False,'reason':'test_required'}
        if not isinstance(test.get('execution_evidence'),dict) or digest(test['execution_evidence'])!=test.get('evidence_digest'):
            return {'ready':False,'reason':'test_evidence_invalid'}
        event=db.execute("SELECT payload_json FROM events WHERE event_id=? AND event_type='agent_pool.test_finished'",(test.get('event_id'),)).fetchone()
        if not event or json.loads(event[0])!={'node_id':source['reviewer_id'],'test':{k:v for k,v in test.items() if k!='event_id'}}:
            return {'ready':False,'reason':'test_evidence_invalid'}
        return {'ready':True,'reason':'tested','test_id':test['test_id'],'spec_digest':test['spec_digest']}

    def test_run(self, node_id, expected_revision, test_id):
        with self.store.native.read_only() as db:
            if node_state(self.store.source(db,node_id)).get('interface') == 'tmux':
                raise ValueError('用户在场节点不运行自动探针；请显式派发协作任务卡')
        if not isinstance(test_id,str) or not test_id or len(test_id)>128 or type(expected_revision) is not int:
            raise ValueError('测试需要稳定请求ID和当前节点修订号')
        with self.store.transaction() as db:
            source=self.store.source(db,node_id);node=require_enabled(source)
            if node.get('test',{}).get('test_id')==test_id and not node.get('test_running'):
                if node['test'].get('status')=='passed' and not self.readiness(db,source)['ready']:
                    raise Conflict('原测试已失效，请使用新测试请求ID')
                return node['test']
            if node.get('test_running') and node.get('test_deadline',0)>time.time():raise Conflict('该节点正在测试，请等待原测试结果')
            if node['revision']!=expected_revision:raise Conflict('节点已变化，请刷新后测试')
            adapter=self.backends.adapter_for(source)
            if not adapter or not getattr(adapter,'supports_resource_plan',False) or not callable(getattr(adapter,'test_resource',None)):
                return {'status':'blocked','reason':'adapter_required','message':'此调用方式尚未接入受信调用器；不会把配置登记当成测试通过。'}
            try:identity=adapter.resource_identity()
            except (OSError,ValueError,AttributeError):
                return {'status':'blocked','reason':'entry_unavailable','message':'调用入口不可用或缺少受信适配器，请检查入口；未标记测试通过。'}
            plan=resource_plan(source);plan_digest=digest(spec(source))
            if node.get('test_running'):
                self.store.native._append_event_tx(db,None,'agent_pool.test_abandoned',{'node_id':node_id,'test_id':node['test_running']})
            node['test_running']=test_id;node['test_deadline']=time.time()+getattr(adapter,'test_budget_seconds',60);node['test']={'status':'running','test_id':test_id};source['node']=node
            db.execute('UPDATE ri_sources SET data=? WHERE id=?',(canonical(source),node_id))
            self.store.native._append_event_tx(db,None,'agent_pool.test_started',{'node_id':node_id,'test_id':test_id,'spec_digest':plan_digest})
        challenge=identifier();status='failed';reason='execution_failed';proof=None;evidence=None;message=None;model_tasks_sent=None
        try:
            result=adapter.test_resource(plan,challenge)
            text=result.get('output')
            if (not isinstance(text,str) or text.strip()!=challenge or contains_non_answer_markers(text) or detect_reasoning_leak(text)
                    or result.get('model')!=plan['model'] or not result.get('provider') or not result.get('route_source')):
                raise ValueError('测试响应或实际模型与节点计划不符')
            if digest(adapter.resource_identity())!=digest(identity):raise ValueError('测试期间入口发生变化')
            evidence={k:result[k] for k in ('provider','model','route_source','output')}
            for key in ('model_id','model_display','model_evidence_scope','model_source','model_verification','selection','session_id','turn','execution_mode','reply_source','worklog'):
                if key in result:evidence[key]=result[key]
            evidence['challenge']=challenge
            from xiaoba_agentos.memory import MemoryBridge
            if MemoryBridge._sensitive_reason(canonical(evidence)):raise ValueError('测试元数据含敏感资料，不保存')
            proof=digest(evidence);status='passed';reason='challenge_and_model_verified'
            if result.get('model_source')=='node_configuration':reason='challenge_completed_configured_node'
        except Exception as exc:
            reason=type(exc).__name__
            from .resource_agy import InteractiveError
            from .agent_workspace import WorkspaceError
            if isinstance(exc,(InteractiveError,WorkspaceError)):reason=exc.code;message=exc.user_message
            model_tasks_sent=getattr(exc,'model_tasks_sent',None)
            if reason=='native_workspace_trust_required':status='blocked'
        with self.store.transaction() as db:
            current=self.store.source(db,node_id);node=node_state(current)
            if node.get('test_running')!=test_id:raise Conflict('测试执行权已失效')
            if node['removed'] or not node['enabled'] or node['revision']!=expected_revision or digest(spec(current))!=plan_digest:
                status='failed';reason='node_changed_during_test'
            test={'status':status,'reason':reason,'test_id':test_id,'spec_digest':plan_digest,
                  'runner_digest':digest(identity),'evidence_digest':proof,'execution_evidence':evidence if status=='passed' else None,'finished_at':time.time()}
            if message:test['message']=message
            if model_tasks_sent is not None:test['model_tasks_sent']=model_tasks_sent
            event=self.store.native._append_event_tx(db,None,'agent_pool.test_finished',{'node_id':node_id,'test':test})
            node['test']={**test,'event_id':event};node.pop('test_running',None);node.pop('test_deadline',None);node['revision']+=1;current['node']=node
            db.execute('UPDATE ri_sources SET data=? WHERE id=?',(canonical(current),node_id))
            return node['test']

    def confirm_interactive_workspace(self,node_id,expected_revision,action_id,session_id,decision):
        if decision!='trust' or type(expected_revision) is not int:
            raise ValueError('需要用户明确确认本次原生隔离目录')
        with self.store.transaction() as db:
            source=self.store.source(db,node_id);node=require_enabled(source)
            if node['revision']!=expected_revision:raise Conflict('节点已变化，请刷新后确认')
            adapter=self.backends.adapter_for(source)
            pending=getattr(adapter,'pending_action',None)
            if not pending or pending['action_id']!=action_id or pending['session_id']!=session_id:
                raise Conflict('本次原生目录确认已失效')
            self.store.native._append_event_tx(db,None,'agent_pool.native_workspace_consent_requested',{
                'node_id':node_id,'action_id':action_id,'session_id':session_id,
                'directory':pending['directory'],'scope':'native_workspace_only','experience_approval':False})
        try:result=adapter.confirm_workspace(action_id,session_id)
        except Exception:
            with self.store.transaction() as db:
                self.store.native._append_event_tx(db,None,'agent_pool.native_workspace_consent_failed',{
                    'node_id':node_id,'action_id':action_id,'model_tasks_sent':0})
            raise
        with self.store.transaction() as db:
            current=self.store.source(db,node_id);current_node=require_enabled(current)
            if current_node['revision']!=expected_revision:raise Conflict('确认期间节点已变化；未发送模型任务')
            current_node['revision']+=1;current['node']=current_node
            db.execute('UPDATE ri_sources SET data=? WHERE id=?',(canonical(current),node_id))
            self.store.native._append_event_tx(db,None,'agent_pool.native_workspace_consent_finished',{
                'node_id':node_id,'action_id':action_id,'result':result,'experience_approval':False})
        return result

    def prepare(self, required_kinds, selections=None, task_type=None):
        if task_type is not None and (not isinstance(task_type,str) or not task_type.strip() or len(task_type)>128):raise ValueError('任务类型格式异常')
        if not isinstance(required_kinds,list) or not required_kinds or len(set(required_kinds))!=len(required_kinds) or set(required_kinds)-{'dsh','peer'}:
            raise ValueError('需要明确当前任务的后台槽位')
        selections=selections or {}
        if not isinstance(selections,dict) or set(selections)-set(required_kinds):raise ValueError('节点选择与当前任务槽位不符')
        chosen={};questions=[];blockers=[]
        with self.store.native.read_only() as db:
            if not self.store.native.verify_event_chain_tx(db)['ok']:
                return {'route':'blocked','reason':'event_chain_invalid','questions':[]}
            sources=[json.loads(r[0]) for r in db.execute('SELECT data FROM ri_sources ORDER BY rowid')]
            for kind in required_kinds:
                candidates=[s for s in sources if s['kind']==kind and not node_state(s)['removed']]
                explicit=selections.get(kind)
                if explicit:candidates=[s for s in candidates if s['reviewer_id']==explicit]
                ready=[s for s in candidates if self.readiness(db,s)['ready']]
                if len(ready)==1:chosen[kind]=ready[0];continue
                if len(ready)>1:
                    from datetime import datetime
                    from zoneinfo import ZoneInfo
                    rankings=self.scoring.weekly(datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat(),task_type)['ranking']
                    scores={(v['source_ref'],v['model']):(v['points'],v['success_rate'],v['tasks']) for v in rankings}
                    ranked=[(scores.get((v['source_ref'],node_state(v).get('model')),(0,0,0)),v) for v in ready]
                    best=max(v[0] for v in ranked)
                    winners=[v for score,v in ranked if score==best]
                    if best[0]>0 and len(winners)==1:chosen[kind]=winners[0];continue
                    questions.append({'kind':kind,'field':'node_id','question':'本任务使用哪个已测试节点？','options':[{'node_id':s['reviewer_id'],'name':node_state(s)['name']} for s in ready]});continue
                if not candidates:
                    questions.extend({'kind':kind,'field':k,'question':v} for k,v in FIELDS.items())
                else:
                    blockers.extend({'node_id':s['reviewer_id'],'name':node_state(s)['name'],**self.readiness(db,s)} for s in candidates)
                    missing={k for s in candidates for k in FIELDS if not node_state(s).get(k)}
                    if missing:questions.extend({'kind':kind,'field':k,'question':FIELDS[k]} for k in FIELDS if k in missing)
                    else:questions.append({'kind':kind,'field':'readiness','question':'当前节点尚不可直接运行，请在运行总览开启并完成测试；只补处理列出的缺项。'})
            if len(chosen)==len(required_kinds):
                if len({independence(s) for s in chosen.values()})!=len(chosen):
                    return {'route':'ask_user','questions':[{'field':'independent_source','question':'需要不同入口程序的后台节点：同一入口换模型算两个，改名称或换会话不算。'}],'blockers':[]}
                return {'route':'run','questions':[],'selections':{k:s['reviewer_id'] for k,s in chosen.items()}}
        return {'route':'ask_user','questions':questions,'blockers':blockers,'selections':{k:s['reviewer_id'] for k,s in chosen.items()}}
