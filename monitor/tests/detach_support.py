"""Observe supervisor readiness before accepting detached launch survival."""
import json
import os
import time


def wait_supervisor_ready(root, supervisor, seconds=10):
    deadline = time.monotonic() + seconds
    log = root / 'logs' / 'supervisor.log'
    while time.monotonic() < deadline:
        # A dead supervisor is a product failure, not a reason to retry a PID lookup.
        os.kill(supervisor, 0)
        try:
            lines = log.read_text().splitlines()
        except FileNotFoundError:
            lines = []
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # A concurrent append may not have completed its final line.
            if event.get('mod') == 'monitor' and event.get('msg') == 'ready':
                os.kill(supervisor, 0)
                return True
        time.sleep(.05)
    return False
