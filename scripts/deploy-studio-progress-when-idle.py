#!/usr/bin/env python3
"""Apply the H3 dashboard's server-side Studio progress hook after video work ends."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request

REPO=Path('/home/aski/h3-video-web-src')
RUNTIME=Path('/home/aski/h3-web')
JOBS=Path('/mnt/comfyui_videos/comfyui/instagram-originals/daily')
RESULT=Path('/home/aski/.local/state/h3-studio-progress-deploy.json')
OLD='''        if p == "/api/active-progress":
            now = time.time()
            with LOCK:
                jobs_snapshot = {jid: dict(job) for jid, job in JOBS.items()}
            send_json(self, {
                "ok": True,
                "server_time": now,
                "workers": worker_progress_dashboard(jobs_snapshot, now=now),
            })'''
NEW='''        if p == "/api/active-progress":
            now = time.time()
            with LOCK:
                jobs_snapshot = {jid: dict(job) for jid, job in JOBS.items()}
            import original_video
            workers = worker_progress_dashboard(jobs_snapshot, now=now)
            workers["studio_original"] = original_video.active_progress(now=now)
            send_json(self, {
                "ok": True,
                "server_time": now,
                "workers": workers,
            })'''

def record(status, detail):
    RESULT.parent.mkdir(parents=True,exist_ok=True)
    RESULT.write_text(json.dumps({'status':status,'detail':detail,'at':time.time()}))

def git_file(revision,path):
    return subprocess.check_output(['git','show',f'{revision}:{path}'],cwd=REPO)

def http_json(path,headers=None):
    request=urllib.request.Request('http://127.0.0.1:8300'+path,headers=headers or {})
    with urllib.request.urlopen(request,timeout=12) as response:return json.load(response)

def idle(job_id):
    target=JOBS/job_id/'state.json'
    state=json.loads(target.read_text())
    if state.get('status') not in ('done','error','cancelled'):return False
    for path in JOBS.glob('*/state.json'):
        if json.loads(path.read_text()).get('status') not in ('done','error','cancelled'):return False
    with urllib.request.urlopen('http://127.0.0.1:8188/queue',timeout=8) as response:queue=json.load(response)
    if queue.get('queue_running') or queue.get('queue_pending'):return False
    env=dict(line.strip().split('=',1) for line in (RUNTIME/'.env').read_text().splitlines()
             if line and not line.startswith('#') and '=' in line)
    jobs=http_json('/api/jobs',{'X-H3-Origin-Token':env['H3_ORIGIN_SECRET']})
    queues=jobs.get('queues') or {}
    return all(not (queues.get(worker) or {}).get('active_job') and
               not (queues.get(worker) or {}).get('pending') for worker in ('pgx','rtx5080'))

def atomic_write(path,data):
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as tmp:
        tmp.write(data);tmp.flush();os.fsync(tmp.fileno());name=tmp.name
    os.chmod(name,path.stat().st_mode)
    os.replace(name,path)

def deploy(revision):
    old_original=git_file(revision+'^','original_video.py')
    new_original=git_file(revision,'original_video.py')
    original_path=RUNTIME/'original_video.py';server_path=RUNTIME/'server.py'
    if original_path.read_bytes()!=old_original:raise RuntimeError('runtime_original_changed')
    old_server=server_path.read_bytes()
    decoded=old_server.decode()
    if decoded.count(OLD)!=1:raise RuntimeError('runtime_server_changed')
    new_server=decoded.replace(OLD,NEW).encode()
    compile(new_original.decode(),str(original_path),'exec')
    compile(new_server.decode(),str(server_path),'exec')
    try:
        atomic_write(original_path,new_original)
        atomic_write(server_path,new_server)
        subprocess.run(['systemctl','--user','restart','h3-web-backend.service'],check=True,timeout=30)
        response=http_json('/api/active-progress',{'X-H3-Origin-Token':dict(
            line.strip().split('=',1) for line in (RUNTIME/'.env').read_text().splitlines()
            if line and not line.startswith('#') and '=' in line)['H3_ORIGIN_SECRET']})
        if 'studio_original' not in (response.get('workers') or {}):raise RuntimeError('progress_field_missing')
    except Exception:
        atomic_write(original_path,old_original)
        atomic_write(server_path,old_server)
        subprocess.run(['systemctl','--user','restart','h3-web-backend.service'],check=False,timeout=30)
        raise

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('job_id')
    parser.add_argument('--revision',default='4f1847a')
    args=parser.parse_args()
    if not args.job_id.startswith('orig_') or len(args.job_id)!=37:raise SystemExit('invalid job id')
    deadline=time.time()+7*86400
    record('waiting','video_job_active')
    try:
        while time.time()<deadline:
            try:
                if idle(args.job_id):
                    time.sleep(30)
                    if idle(args.job_id):
                        deploy(args.revision)
                        record('complete','h3_dashboard_studio_progress_enabled')
                        return
            except (OSError,KeyError,ValueError,TimeoutError):pass
            time.sleep(60)
        record('expired','idle_window_not_found')
    except Exception as exc:
        record('failed',type(exc).__name__)
        raise

if __name__=='__main__':main()
