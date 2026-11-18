"""Read individual business payloads without weakening the event-chain gate."""
import json


def rows(conn, kinds, validate=None):
    valid, problems = [], []
    marks = ','.join('?' for _ in kinds)
    for row in conn.execute(f'SELECT * FROM events WHERE event_type IN ({marks}) ORDER BY sequence', tuple(kinds)):
        try:
            value = json.loads(row['payload_json'])
            if not isinstance(value, dict):
                raise ValueError('payload must be an object')
            if validate:
                validate(row['event_type'], value)
            valid.append(dict(row, value=value))
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            problems.append({'event_id': row['event_id'], 'sequence': row['sequence'],
                             'event_type': row['event_type'], 'reason': str(exc)[:240]})
    return valid, problems
