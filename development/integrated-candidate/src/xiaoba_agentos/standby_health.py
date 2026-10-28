"""Shared provenance check for pre-dispatch standby cover and release evidence."""
import json

HEALTH_MARKER = 'XIAOBA_AGENT_HEALTH_OK'
EXTERNAL_FAILURES = frozenset({'external_upstream', 'external_auth'})


def health_evidence(row):
    """Keep only non-secret fields needed to distinguish a probe from a task."""
    if not isinstance(row, dict):
        row = {}
    probe = row.get('probe')
    if not isinstance(probe, dict):
        try:
            probe = json.loads(row.get('details_json') or '{}')
        except (TypeError, ValueError):
            probe = {}
    if not isinstance(probe, dict):
        probe = {}
    return {'status': row.get('status') if isinstance(row.get('status'), str) else None,
            'failure_class': row.get('failure_class') if isinstance(row.get('failure_class'), str) else None,
            'probe': {key: probe[key] for key in ('health_marker', 'health_probe_state', 'marker_received') if key in probe}}


def coverable_health(row):
    """Actual failed probe OR persisted external failure; unprobed internal degradation is neither."""
    evidence = health_evidence(row)
    if evidence['status'] not in ('degraded', 'external_blocked'):
        return False
    # A persisted external task failure is independently sufficient: it need
    # not pretend to be a probe. The two allowed sources are an OR (user policy).
    # The status name alone never promotes an internal/unknown failure.
    if evidence['failure_class'] in EXTERNAL_FAILURES:
        return True
    probe = evidence['probe']
    # Only recovery.live_agent_probe writes a probe with the health marker; a
    # task-level degradation carries none. Its 'unknown' state means the probe
    # call itself raised without a more specific state -- a real failed probe,
    # covered under the user's rule -- never "not probed".
    return (probe.get('health_marker') == HEALTH_MARKER
            and probe.get('health_probe_state') in ('unknown', 'process_unavailable', 'response_without_health_marker')
            and probe.get('marker_received') is not True)
