#!/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14
"""Bounded, read-only display-link observation. No screen/file-content capture."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import threading
import time
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
PREDICATE = r'''(process == "WindowServer" AND (eventMessage CONTAINS[c] "Display:Hotplug" OR eventMessage CONTAINS[c] "hot plug" OR eventMessage CONTAINS[c] "set_display_state" OR eventMessage CONTAINS[c] "complete_display_state_transition")) OR (process == "kernel" AND (eventMessage CONTAINS[c] "Upstream_Reset" OR eventMessage CONTAINS[c] "A5_Authenticated" OR eventMessage CONTAINS[c] "H0_No_Rx_Attached" OR eventMessage CONTAINS[c] "Thunderbolt" OR eventMessage CONTAINS[c] "NVMe" OR eventMessage CONTAINS[c] "overcurrent" OR eventMessage CONTAINS[c] "power fault" OR eventMessage CONTAINS[c] "link down" OR eventMessage CONTAINS[c] "link reset" OR eventMessage CONTAINS[c] "link train")) OR (process == "powerd" AND (eventMessage CONTAINS[c] "Entering Sleep" OR eventMessage CONTAINS[c] "Wake reason" OR eventMessage CONTAINS[c] "Display is turned" OR eventMessage CONTAINS[c] "System Sleep" OR eventMessage CONTAINS[c] "System Wake")) OR (process == "diskarbitrationd" AND (eventMessage CONTAINS[c] "appeared" OR eventMessage CONTAINS[c] "disappeared" OR eventMessage CONTAINS[c] "ejected")) OR (process == "corebrightnessd" AND subsystem CONTAINS[c] "CBAnalyticsExtDisplayManager")'''


def now():
    return dt.datetime.now(TZ).isoformat(timespec='milliseconds')


def parse_time(value):
    if not value:
        return dt.datetime.now(TZ)
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))


def write_json(path, value):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def append_json(path, value):
    with path.open('a') as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + '\n')


def topology():
    result = subprocess.run(
        ['/usr/sbin/system_profiler', 'SPDisplaysDataType', 'SPThunderboltDataType',
         'SPUSBHostDataType', '-json'], capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError('system_profiler failed: ' + result.stderr[:200])
    data = json.loads(result.stdout)
    displays = []
    for gpu in data.get('SPDisplaysDataType', []):
        for device in gpu.get('spdisplays_ndrvs', []):
            displays.append({k: device[k] for k in (
                '_name', '_spdisplays_displayID', '_spdisplays_pixels',
                '_spdisplays_resolution', 'spdisplays_online',
                'spdisplays_display-fw-version') if k in device})
    devices = []

    def walk(items, parents, kind):
        for device in items:
            name = device.get('device_name_key', device.get('_name', 'unknown')).strip()
            row = {'kind': kind, 'path': parents + [name]}
            for key in ('mode_key', 'switch_version_key', 'USBDeviceKeyLinkSpeed',
                        'USBDeviceKeyVendorName'):
                if key in device:
                    row[key] = device[key]
            row['ports'] = {
                key: {k: v for k, v in value.items()
                      if k in ('current_speed_key', 'receptacle_status_key')}
                for key, value in device.items()
                if key.startswith('receptacle') and isinstance(value, dict)}
            devices.append(row)
            walk(device.get('_items', []), row['path'], kind)
    walk(data.get('SPThunderboltDataType', []), [], 'Thunderbolt')
    walk(data.get('SPUSBHostDataType', []), [], 'USB')
    return {'displays': displays, 'devices': devices}


def acasis_modes(topo):
    return [{'path': row['path'], 'kind': row['kind'],
             'speed': row.get('USBDeviceKeyLinkSpeed'), 'ports': row['ports']}
            for row in topo['devices']
            if any('acasis' in name.lower() or 'tb501' in name.lower()
                   for name in row['path'][-1:])]


class Detector:
    def __init__(self, ids):
        self.ids = set(ids)
        self.incidents = []
        self.pending = {}

    def consume(self, record):
        message = record.get('eventMessage', '')
        match = re.search(r'Display (\d+) hot plug ([01])\b', message)
        if match:
            display_id, state = int(match[1]), match[2] == '1'
        else:
            match = re.search(r'Processing hotplug "(out|in)" for display id: (\d+)', message)
            if not match:
                return None
            display_id, state = int(match[2]), match[1] == 'in'
        if display_id not in self.ids:
            return None
        timestamp = record.get('timestamp') or now()
        event_time = parse_time(timestamp)
        if not state:
            if display_id in self.pending:
                return None  # The raw and processed hotplug messages describe one event.
            incident = {'number': len(self.incidents) + 1, 'display_id': display_id,
                        'out_at': timestamp, 'in_at': None, 'signal_gap_ms': None,
                        'classification': 'display_disconnect_candidate',
                        'cause': 'unknown; manual unplug and sleep must be checked'}
            self.incidents.append(incident)
            self.pending[display_id] = incident
            return {'kind': 'display_out', 'incident': incident['number']}
        if display_id not in self.pending:
            return None
        incident = self.pending.pop(display_id)
        incident['in_at'] = timestamp
        incident['signal_gap_ms'] = round(
            (event_time - parse_time(incident['out_at'])).total_seconds() * 1000, 3)
        return {'kind': 'display_in', 'incident': incident['number']}


def self_test():
    detector = Detector([5])
    fixture = [
        ('2026-10-22 16:09:53.392918+0800', 'Display 5 hot plug 0'),
        ('2026-10-22 16:09:53.393349+0800', '[ Display:Hotplug ] Processing hotplug "out" for display id: 5 (CA)'),
        ('2026-10-22 16:09:53.540529+0800', 'Display 5 hot plug 1'),
        ('2026-10-22 16:09:53.731135+0800', '[ Display:Hotplug ] Processing hotplug "in" for display id: 5 (CA)'),
        ('2026-10-22 16:09:54.000000+0800', 'Display 99 hot plug 0'),
    ]
    events = [detector.consume({'timestamp': t, 'eventMessage': m}) for t, m in fixture]
    assert len(detector.incidents) == 1 and not detector.pending
    assert [event['kind'] for event in events if event] == ['display_out', 'display_in']
    assert abs(detector.incidents[0]['signal_gap_ms'] - 147.611) < 0.01
    assert detector.consume({'eventMessage': 'ordinary brightness update'}) is None
    processed_only = Detector([5])
    for t, m in [fixture[1], fixture[3]]:
        processed_only.consume({'timestamp': t, 'eventMessage': m})
    assert len(processed_only.incidents) == 1 and not processed_only.pending
    print('PASS: incident deduplication, timestamp arithmetic, target filtering, processed-only events.')


def report(out, state):
    incidents = state['incidents']
    warnings = state['warnings']
    complete = state['state'] == 'completed' and not warnings
    lines = ['# Studio Display 连续日志观察', '',
             f"- 状态：{state['state']}",
             f"- 开始：{state['started_at']}",
             f"- 计划结束：{state['planned_end_at']}",
             f"- 实际结束／最近状态：{state.get('finished_at', state['updated_at'])}",
             f"- 已观察秒数：{state['elapsed_seconds']}",
             f"- 显示掉线候选事件：{len(incidents)}",
             f"- 已处理日志记录：{state['stream_records']}",
             f"- 采集完整性：{'已完成计划窗口；未记录到采集故障' if complete else '运行中或存在限制，见状态及告警'}", '',
             '## 基线及限制', '',
             '- 基线以 topology.jsonl 的首条记录为准；ACASIS 接入模式可能在 USB 10Gb/s 与雷雳 80Gb/s 间变化。',
             '- 这是系统日志观测，无法保证捕捉没有写入日志的肉眼闪屏。',
             '- hotplug out/in 是显示连接状态变化，不自动证明线缆、ACASIS 或显示器损坏。',
             '- 人工拔线、显示器断电、睡眠／唤醒也可能产生相关记录，必须结合用户操作判断。',
             '- signal_gap_ms 是两条连接状态日志的时间差，不是实测屏幕黑屏时长。',
             '- 不读取硬盘文件、不抓屏、不进行磁盘测速、不修改固件或显示设置。', '']
    if incidents:
        lines += ['## 候选事件', '']
        for event in incidents:
            lines.append(f"- #{event['number']}：断开 {event['out_at']}；接入 {event['in_at']}；信号状态间隔 {event['signal_gap_ms']} ms。原因待核实。")
    else:
        lines += ['本轮尚未捕捉到目标显示器 hotplug out/in；不能据此宣称没有可见闪屏。', '']
    if warnings:
        lines += ['## 采集告警', ''] + ['- ' + warning for warning in warnings]
    lines += ['', '详细记录：events.jsonl、topology.jsonl；运行状态：status.json。', '']
    (out / 'report.md').write_text('\n'.join(lines))


def run(args):
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'status.json').exists():
        raise SystemExit('Refusing to overwrite an existing observation directory.')
    os.chmod(out, 0o700)
    started = time.time()
    deadline = started + args.seconds
    state = {'state': 'starting', 'pid': os.getpid(), 'script': str(Path(__file__).resolve()),
             'started_at': dt.datetime.fromtimestamp(started, TZ).isoformat(),
             'planned_end_at': dt.datetime.fromtimestamp(deadline, TZ).isoformat(),
             'duration_seconds': args.seconds, 'stream_records': 0,
             'incidents': [], 'warnings': [], 'stream_pid': None,
             'awake_assertion_pid': None, 'topology_samples': 0}
    events = queue.Queue()
    stop = threading.Event()
    stream = None
    awake = None
    detector = Detector([])
    last_topology = None
    next_sample = 0
    next_status = 0
    stream_error = False
    stderr_lines = []
    reader_threads = []
    previous_loop_at = started

    def save():
        state['updated_at'] = now()
        state['elapsed_seconds'] = round(min(time.time(), deadline) - started, 1)
        state['incidents'] = detector.incidents
        write_json(out / 'status.json', state)
        report(out, state)

    def read_stdout():
        try:
            for line in stream.stdout:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    events.put(record)
        finally:
            events.put({'_stream_eof': True})

    def read_stderr():
        for line in stream.stderr:
            if len(stderr_lines) < 20:
                stderr_lines.append(line.strip()[:500])

    def handle(record):
        nonlocal stream_error
        if record.get('_stream_eof'):
            if time.time() < deadline - 1 and not stop.is_set():
                state['warnings'].append('Continuous log stream ended early; observation is incomplete.')
                stream_error = True
                stop.set()
            return
        state['stream_records'] += 1
        message = record.get('eventMessage', '')
        if 'extdisplay.als.colorsample' in message:
            return  # Receipt proves the stream is live; routine sensor values are not retained.
        if 'registered callback' in message:
            return
        process = record.get('processImagePath', '').rsplit('/', 1)[-1]
        if process == 'powerd' and 'Entering Sleep' in message:
            warning = 'System sleep was logged; continuous observation across sleep is not guaranteed.'
            if warning not in state['warnings']:
                state['warnings'].append(warning)
        row = {k: record.get(k) for k in ('timestamp', 'subsystem', 'category', 'messageType')}
        row['process'] = record.get('processImagePath', '').rsplit('/', 1)[-1]
        row['message'] = re.sub(r'(?i)(serial(?:number)?\s*[:=]\s*)\S+', r'\1[redacted]', message)[:2000]
        detected = detector.consume(record)
        if detected:
            row['detection'] = detected
            print(json.dumps({'at': now(), **detected}, ensure_ascii=False), flush=True)
        append_json(out / 'events.jsonl', row)

    def cancel(*_):
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, cancel)
    save()
    try:
        stream = subprocess.Popen(['/usr/bin/log', 'stream', '--style', 'ndjson',
                                   '--level', 'debug', '--predicate', PREDICATE],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, bufsize=1)
        state['stream_pid'] = stream.pid
        reader_threads = [threading.Thread(target=read_stdout, daemon=True),
                          threading.Thread(target=read_stderr, daemon=True)]
        for reader in reader_threads:
            reader.start()
        awake = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        state['awake_assertion_pid'] = awake.pid
        state['state'] = 'running'
        print(json.dumps({'started': state['started_at'], 'end': state['planned_end_at'],
                          'pid': os.getpid()}, ensure_ascii=False), flush=True)
        while time.time() < deadline and not stop.is_set():
            current = time.time()
            if current - previous_loop_at > 30:
                state['warnings'].append(
                    f'Observer scheduling gap {current - previous_loop_at:.1f}s; check manual sleep or system stall.')
            previous_loop_at = current
            if current >= next_sample:
                try:
                    observed = topology()
                    state['topology_samples'] += 1
                    state['last_topology_sample_at'] = now()
                    state['acasis_modes'] = acasis_modes(observed)
                    for display in observed['displays']:
                        if 'studio display' in display.get('_name', '').lower():
                            value = display.get('_spdisplays_displayID')
                            if value is not None:
                                detector.ids.add(int(str(value), 0))
                    if not detector.ids:
                        raise RuntimeError('No target Studio Display ID available.')
                    if observed != last_topology or last_topology is None:
                        append_json(out / 'topology.jsonl', {'at': now(), 'topology': observed})
                        last_topology = observed
                except Exception as error:
                    warning = 'Topology sampling failed: ' + str(error)[:300]
                    if warning not in state['warnings']:
                        state['warnings'].append(warning)
                next_sample = time.time() + args.sample_interval
            try:
                record = events.get(timeout=min(0.5, max(0.01, deadline - time.time())))
                handle(record)
                for _ in range(min(events.qsize(), 500)):
                    handle(events.get_nowait())
            except queue.Empty:
                pass
            if current >= next_status:
                save()
                next_status = current + 5
        state['state'] = ('failed' if stream_error else
                          'completed' if time.time() >= deadline else 'stopped')
    except Exception as error:
        state['state'] = 'failed'
        state['warnings'].append(type(error).__name__ + ': ' + str(error)[:500])
    finally:
        if stream and stream.poll() is None:
            stream.terminate()
            try:
                stream.wait(timeout=3)
            except subprocess.TimeoutExpired:
                stream.kill()
                stream.wait(timeout=3)
        for reader in reader_threads:
            reader.join(timeout=2)
        while not events.empty():
            handle(events.get_nowait())
        if awake and awake.poll() is None:
            awake.terminate()
            awake.wait(timeout=3)
        if not state['stream_records']:
            state['warnings'].append('No JSON log records received; log visibility was not verified.')
        if stderr_lines:
            state['log_stream_messages'] = stderr_lines
        state['finished_at'] = now()
        save()
        print(json.dumps({'finished': state['finished_at'], 'state': state['state'],
                          'incidents': len(detector.incidents), 'warnings': state['warnings']},
                         ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=3600)
    parser.add_argument('--sample-interval', type=float, default=60)
    parser.add_argument('--out')
    parser.add_argument('--self-test', action='store_true')
    options = parser.parse_args()
    if options.self_test:
        self_test()
    else:
        if not options.out or options.seconds <= 0 or options.sample_interval <= 0:
            parser.error('--out and positive durations are required')
        run(options)
