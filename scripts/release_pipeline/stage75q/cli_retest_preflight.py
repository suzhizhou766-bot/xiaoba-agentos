"""Read-only CLI retest preflight, no model/credential/config access."""
from contextlib import closing
import json
import sqlite3
from pathlib import Path


def _source_node(node_id, raw):
    """Validate stored source shape before interpreting lifecycle switches.

    Four-field legacy sources have no node switches. A missing/renamed modern
    wrapper must not silently turn a disabled CLI into one of those sources.
    Node runtime metadata may extend the modern record; the routing and switch
    fields below are the required JSON contract for this read-only preflight.
    """
    def refuse():
        raise RuntimeError('STOP: invalid Agent source contract: '+str(node_id))

    try:
        source = json.loads(raw)
    except (TypeError, ValueError):
        refuse()
    fields = {'reviewer_id', 'source_ref', 'provenance', 'kind'}
    if not isinstance(source, dict) or set(source) not in (fields, fields | {'node'}):
        refuse()
    if (not isinstance(node_id, str) or not node_id.strip()
            or any(not isinstance(source.get(key), str) or not source[key].strip()
                   for key in fields)
            or source['reviewer_id'] != node_id):
        refuse()
    if 'node' not in source:
        if source['kind'] not in {'dsh', 'peer'}:
            refuse()
        return None
    node = source['node']
    if (source['kind'] not in {'dsh', 'peer', 'review', 'collaborate', 'both', 'collaborator'}
            or not isinstance(node, dict)
            or type(node.get('version')) is not int or node['version'] != 1
            or type(node.get('revision')) is not int or node['revision'] < 1
            or type(node.get('enabled')) is not bool
            or type(node.get('removed')) is not bool
            or not isinstance(node.get('interface'), str)
            or node.get('interface') not in {'cli', 'desktop', 'web', 'tmux'}
            or any(not isinstance(node.get(key), str) or not node[key].strip()
                   for key in ('name', 'agent', 'entry'))
            or not isinstance(node.get('model'), str)
            or (not node['model'].strip() and node['interface'] != 'cli')):
        refuse()
    return node


def require_testable_cli(live):
    database=Path(live)/'.runtime/agentos.sqlite3'
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as db:
        rows=db.execute('SELECT id,data FROM ri_sources').fetchall()
    blockers=[]
    for node_id, raw in rows:
        node = _source_node(node_id, raw)
        if node is not None and node['interface']=='cli' and not node['removed'] and not node['enabled']:
            blockers.append(node_id)
    if blockers:raise RuntimeError('STOP: disabled CLI nodes require user decision: '+', '.join(blockers))
    return {'ok':True,'disabled_cli_blockers':[],'mode':'read_only'}
