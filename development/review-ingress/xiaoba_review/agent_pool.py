"""User-managed Agent resources, stored alongside immutable review identities.

Lifecycle metadata never edits a frozen task, deletes history, changes client
credentials, or imports executable code from a web-supplied entry point.
"""
import json
from pathlib import Path
from urllib.parse import urlsplit

from .store import Conflict, canonical, digest
from xiaoba_agentos.validation_evidence import independence_key


def node_state(source):
    value = source.get('node')
    if value is None:
        return {'version':1,'revision':0,'enabled':True,'removed':False,
                'name':source['reviewer_id'],'agent':source['reviewer_id'],
                'interface':None,'entry':None,'model':None,'legacy':True}
    if (not isinstance(value,dict) or value.get('version') != 1
            or type(value.get('revision')) is not int or value['revision'] < 1
            or type(value.get('enabled')) is not bool or type(value.get('removed')) is not bool):
        raise ValueError('Agent节点状态格式异常')
    return dict(value)


def require_enabled(source):
    state = node_state(source)
    if state['removed'] or not state['enabled']:
        raise ValueError('Agent节点已移除或停用，不能接收新的任务或调用')
    return state


def resource_plan(source):
    state = require_enabled(source)
    if not state.get('entry'):
        return None  # Existing four-field sources retain their old contract.
    return {k:state[k] for k in ('revision','name','agent','interface','entry','model')} | {
        'node_id':source['reviewer_id'],'source_ref':source['source_ref']}


def independence(source):
    """独立性键：入口程序＋模型。旧四字段来源没有模型资料，仍只看 source_ref。

    只读节点资料，不要求节点仍在开启状态：已冻结的任务卡和已交回执仍要核对。
    """
    state = node_state(source)
    return independence_key(source['source_ref'], state.get('model') if state.get('entry') else None)


class AgentPool:
    def __init__(self, store, backends):
        self.store, self.backends = store, backends
        from .node_runtime import NodeRuntime
        self.runtime = NodeRuntime(self)

    def state(self):
        with self.store.native.read_only() as db:
            nodes = []
            for row in db.execute('SELECT data FROM ri_sources ORDER BY rowid'):
                source = json.loads(row['data'])
                state = node_state(source)
                adapter = self.backends.adapter_for(source)
                direct = bool(adapter and (not state.get('entry') or getattr(adapter,'supports_resource_plan',False)))
                running = db.execute('SELECT count(*) FROM ri_assignments WHERE source_id=? AND running IS NOT NULL', (source['reviewer_id'],)).fetchone()[0]
                nodes.append(dict(state, node_id=source['reviewer_id'],kind=source['kind'],
                    source_ref=source['source_ref'],running=running,backend_available=direct,
                    execution_mode=getattr(adapter,'execution_mode',None),
                    pending_action=getattr(adapter,'pending_action',None),
                    readiness=self.runtime.readiness(db,source)))
            chain = self.store.native.verify_event_chain_tx(db)
        return {'ok':True,'source':'local_database','nodes':nodes,'event_chain':chain,
                'selection_scope':'open_review_tasks','client_settings_modified':False}

    def add(self, value):
        if set(value) != {'node_id','name','agent','kind','interface','entry','model'}:
            raise ValueError('请填写节点名称、Agent、角色、调用方式、入口及模型')
        for key in ('node_id','name','agent','entry','model'):
            if not isinstance(value[key],str) or not value[key].strip() or len(value[key]) > 2048 or any(c in value[key] for c in '\r\n\0'):
                raise ValueError('节点字段不完整或格式异常：'+key)
        if len(value['node_id']) > 128 or value['kind'] not in {'dsh','peer'} or value['interface'] not in {'cli','desktop','web'}:
            raise ValueError('未知节点角色或调用方式')
        from xiaoba_agentos.memory import MemoryBridge
        if MemoryBridge._sensitive_reason(canonical(value)):
            raise ValueError('Agent池只登记调用资料，请勿填写密钥、令牌或登录材料')
        clean = {k:v.strip() if isinstance(v,str) else v for k,v in value.items()}
        entry = clean['entry']
        if clean['interface'] == 'web':
            url = urlsplit(entry)
            from urllib.parse import parse_qsl
            credential_fields={'token','key','api_key','password','authorization','cookie','session'}
            if (url.scheme not in {'https','http'} or not url.netloc or url.username or url.password or url.fragment
                    or any(k.casefold() in credential_fields for k,v in parse_qsl(url.query))):
                raise ValueError('Web入口需要不含登录材料的HTTP地址')
            # One service and its conversation URLs share one entry program;
            # independence adds the model on top of it (see independence()).
            identity = url.scheme.lower()+'://'+url.netloc.lower()
        elif clean['interface'] == 'cli':
            path = Path(entry)
            if not path.is_absolute():
                raise ValueError('CLI入口请填写程序的绝对路径，不填写shell命令')
            identity = str(path.resolve())
        else:
            identity = entry.casefold()
        source = {'reviewer_id':clean['node_id'],'kind':clean['kind'],
            'source_ref':'agent-entry:'+digest([clean['interface'],identity]),
            'provenance':'用户在Agent池登记的调用资源；未探测或核实客户端设置',
            'node':{'version':1,'revision':1,'enabled':False,'removed':False,
                    **{k:clean[k] for k in ('name','agent','interface','entry','model')}}}
        with self.store.transaction() as db:
            old = db.execute('SELECT data FROM ri_sources WHERE id=?', (clean['node_id'],)).fetchone()
            if old:
                existing = json.loads(old[0])
                # Retry cannot reset a node's switches or removal state.
                fields = ('name','agent','interface','entry','model')
                if existing['kind'] != source['kind'] or any(node_state(existing).get(k) != source['node'][k] for k in fields):
                    raise Conflict('同一节点ID已用于不同资源')
                return existing
            db.execute('INSERT INTO ri_sources VALUES (?,?)', (clean['node_id'],canonical(source)))
            self.store.native._append_event_tx(db,None,'agent_pool.added',source)
        return source

    def change(self, node_id, expected_revision, operation, enabled=None):
        if operation not in {'toggle','remove','restore'} or type(expected_revision) is not int:
            raise ValueError('需要明确的节点动作和修订号')
        if operation == 'toggle' and type(enabled) is not bool:
            raise ValueError('启用状态必须为布尔值')
        with self.store.transaction() as db:
            source = self.store.source(db,node_id)
            state = node_state(source)
            if operation=='restore' and not state['removed']:
                return source
            target = (enabled if operation=='toggle' else False, operation=='remove')
            if operation=='restore': target=(False,False)
            if operation=='toggle' and state['removed']:
                raise Conflict('先恢复已移除的节点，再启用')
            if state['revision'] != expected_revision:
                if state['revision']==expected_revision+1 and (state['enabled'],state['removed'])==target:
                    return source  # Exact retry, not a new transition.
                raise Conflict('节点已变化，请刷新后重试')
            if (state['enabled'],state['removed'])==target:
                return source
            before = dict(state)
            state.update(enabled=target[0],removed=target[1],revision=state['revision']+1)
            source['node']=state
            db.execute('UPDATE ri_sources SET data=? WHERE id=?', (canonical(source),node_id))
            self.store.native._append_event_tx(db,None,'agent_pool.'+operation,{
                'node_id':node_id,'before':before,'after':state,
                'running_calls':'retain existing execution and its receipt'})
        if not state['enabled']:self.backends.retire_resource(node_id)
        return source
