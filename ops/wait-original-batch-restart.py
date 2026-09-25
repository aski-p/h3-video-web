#!/usr/bin/env python3
"""Activate staged H3 original-video code only after named jobs and ComfyUI idle."""
import argparse
import fcntl
import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path('/mnt/comfyui_videos/comfyui/instagram-originals/daily')
TERMINAL = {'done', 'error', 'cancelled'}

def idle(job_ids):
    for jid in job_ids:
        path = ROOT / jid / 'state.json'
        if not path.is_file() or json.loads(path.read_text()).get('status') not in TERMINAL:
            return False
    for path in ROOT.glob('*/state.json'):
        if json.loads(path.read_text()).get('status') not in TERMINAL:
            return False
    with urllib.request.urlopen('http://127.0.0.1:8188/queue', timeout=5) as response:
        queue = json.load(response)
    return not queue.get('queue_running') and not queue.get('queue_pending')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('job_ids', nargs='+')
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    if not all(re.fullmatch(r'orig_[a-f0-9]{32}', jid) for jid in args.job_ids):
        parser.error('invalid job ID')
    if args.check_only:
        print('idle' if idle(args.job_ids) else 'waiting', flush=True)
        return
    stable = 0
    while True:
        try:
            stable = stable + 1 if idle(args.job_ids) else 0
            if stable >= 3:
                with (ROOT / '.submit.lock').open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    if idle(args.job_ids):
                        subprocess.run(['systemctl', '--user', 'restart',
                                        'h3-web-backend.service', 'aski-original-video.service'],
                                       check=True, timeout=120)
                        print('Staged H3 original-video code activated after batch idle.', flush=True)
                        return
                    stable = 0
        except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            print(f'Waiting for safe activation: {type(error).__name__}', flush=True)
            stable = 0
        time.sleep(30)

if __name__ == '__main__':
    main()
