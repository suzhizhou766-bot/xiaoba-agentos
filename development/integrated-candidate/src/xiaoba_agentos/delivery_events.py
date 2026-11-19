"""Read individual business payloads without weakening the event-chain gate."""
import json


def rows(conn, kinds, validate=None, *, relevant=None):
    valid, problems = [], []
    marks = ','.join('?' for _ in kinds)
    for row in conn.execute(f'SELECT * FROM events WHERE event_type IN ({marks}) ORDER BY sequence', tuple(kinds)):
        value = None
        try:
            value = json.loads(row['payload_json'])
            if not isinstance(value, dict):
                raise ValueError('payload must be an object')
            if validate:
                validate(row['event_type'], value)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            if relevant and not relevant(row, value):
                continue
            problems.append({'event_id': row['event_id'], 'sequence': row['sequence'],
                             'event_type': row['event_type'], 'reason': str(exc)[:240]})
        else:
            if relevant is None or relevant(row, value):
                valid.append(dict(row, value=value))
    return valid, problems
