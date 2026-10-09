"""One-shot runtime activation; never interrupt jobs or switch GPU models."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request


def read_json(url, secret=None):
    headers = {'X-H3-Origin-Token': secret} if secret else {}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as response:
        return json.load(response)


def jobs_idle(data):
    return (data.get('ok') is True and isinstance(data.get('jobs'), list)
            and 'active_job' in data and 'queue_len' in data
            and not data['active_job'] and not data['queue_len']
            and not any(job.get('status') in ('queued', 'starting', 'running', 'unavailable')
                        for job in data['jobs']))


def queues_idle(data):
    return (isinstance(data.get('queue_running'), list)
            and isinstance(data.get('queue_pending'), list)
            and not data['queue_running'] and not data['queue_pending'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--timeout', type=int, default=21600)
    args = parser.parse_args()
    secret = os.environ.get('H3_ORIGIN_SECRET')
    if not secret:
        raise SystemExit('Origin credential unavailable; no restart performed.')
    module = Path('/home/aski/h3-web/pgx_mode.py')
    deadline = time.monotonic() + args.timeout
    print('Waiting for H3 jobs and ComfyUI queue to become idle.', flush=True)
    while time.monotonic() < deadline:
        if hashlib.sha256(module.read_bytes()).hexdigest() != args.expected_sha256:
            raise SystemExit('Runtime changed after staging; activation aborted.')
        try:
            jobs = read_json('http://127.0.0.1:8300/api/jobs', secret)
            if jobs_idle(jobs):
                queue = read_json('http://127.0.0.1:8188/queue')
                if queues_idle(queue):
                    time.sleep(2)
                    if (jobs_idle(read_json('http://127.0.0.1:8300/api/jobs', secret))
                            and queues_idle(read_json('http://127.0.0.1:8188/queue'))):
                        subprocess.run(['systemctl', '--user', 'restart', 'h3-web-backend.service'], check=True)
                        for _ in range(30):
                            time.sleep(2)
                            try:
                                state = read_json('http://127.0.0.1:8300/api/pgx-mode', secret)['mode']
                                if 'selected_mode' in state:
                                    print('PGX admission fix activated; API contract verified.', flush=True)
                                    return
                            except (OSError, ValueError, KeyError):
                                continue
                        raise SystemExit('Backend restarted; activation verification failed.')
        except (OSError, ValueError, KeyError):
            # Unavailable telemetry is not evidence that the GPU is idle.
            pass
        time.sleep(30)
    raise SystemExit('Idle window not reached; no active jobs were interrupted.')


if __name__ == '__main__':
    main()
