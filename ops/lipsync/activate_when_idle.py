"""One-shot activation of a verified frozen bundle; never stop an active job."""
import argparse, json, pathlib, subprocess, time, urllib.request, shutil, hashlib, os

def health():
    pid=subprocess.check_output(['systemctl','--user','show','h3-web-backend','-p','MainPID','--value'],text=True).strip()
    env=dict(x.split('=',1) for x in pathlib.Path('/proc/'+pid+'/environ').read_text().split('\0') if '=' in x)
    request=urllib.request.Request('http://127.0.0.1:8300/api/health',headers={'X-H3-Origin-Token':env.get('H3_ORIGIN_SECRET','')})
    return json.load(urllib.request.urlopen(request,timeout=10))

def main(bundle,deadline):
    bundle=pathlib.Path(bundle);runtime=pathlib.Path('/home/aski/h3-web');quiet=None
    receipt=json.loads((bundle/'verified.json').read_text())
    if not receipt.get('sample_verified'):raise RuntimeError('No verified sample')
    checks=json.loads((bundle/'checksums.json').read_text())
    for name,digest in checks.items():
        if hashlib.sha256((bundle/name).read_bytes()).hexdigest()!=digest:raise RuntimeError('Bundle changed: '+name)
    while time.time()<deadline:
        h=health()
        if 'korean-script-lipsync-v2' in h.get('audio_policies',[]):print('Lip-sync capability already active',flush=True);return
        queue=json.load(urllib.request.urlopen('http://127.0.0.1:8188/queue',timeout=10))
        idle=not h.get('active_job') and not h.get('queue_len') and not queue.get('queue_running') and not queue.get('queue_pending')
        if not idle:quiet=None
        elif quiet is None:quiet=time.time();print('Idle interval observed; checking dispatch races',flush=True)
        elif time.time()-quiet>=130:
            patch=(bundle/'server.patch').read_bytes()
            subprocess.run(['patch','--dry-run','-p1'],input=patch,cwd=runtime,check=True)
            shutil.copy2(runtime/'server.py',runtime/f'server.before-lipsync-{int(time.time())}.py')
            subprocess.run(['patch','-p1'],input=patch,cwd=runtime,check=True)
            for name in ('studio_audio.py','studio_lipsync_worker.py'):shutil.copy2(bundle/name,runtime/name)
            compile((runtime/'server.py').read_text(),'server.py','exec')
            # A request can arrive while files are copied; check both queues again.
            latest=health()
            comfy=json.load(urllib.request.urlopen('http://127.0.0.1:8188/queue',timeout=10))
            if latest.get('active_job') or latest.get('queue_len') or comfy.get('queue_running') or comfy.get('queue_pending'):
                raise RuntimeError('New generation arrived; activation paused before restart')
            subprocess.run(['systemctl','--user','restart','h3-web-backend'],check=True)
            for _ in range(30):
                try:
                    if 'korean-script-lipsync-v2' in health().get('audio_policies',[]):
                        (bundle/'activated.json').write_text(json.dumps({'activated_at':time.time(),'policy':'korean-script-lipsync-v2'}));print('Lip-sync runtime verified; queued Studio jobs can proceed',flush=True);return
                except Exception:pass
                time.sleep(1)
            raise RuntimeError('Runtime capability verification failed')
        time.sleep(15)
    raise TimeoutError('Activation timed out without restarting an active service')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--bundle',required=True);p.add_argument('--timeout-seconds',type=int,default=28800);a=p.parse_args()
    try:main(a.bundle,time.time()+a.timeout_seconds)
    except Exception as exc:
        pathlib.Path(a.bundle,'activation-error.txt').write_text(str(exc));raise
