"""Authenticated, durable original-motion jobs. No synthesis fallback."""
import math
import wardrobe_video
import base64, fcntl, hashlib, hmac, json, os, re, signal, subprocess, sys, time
from pathlib import Path

CONFIG = Path.home()/'.config/aski-face'
ROOT = Path('/mnt/comfyui_videos/comfyui/instagram-originals/daily')
POLICY = 'original-face-v1-hyperswap1b-20260921'
JOB = re.compile(r'^orig_[a-f0-9]{32}$')
TERMINAL = ('done','error','cancelled')

def read(p): return json.loads(p.read_text())
def save(p,v):
    tmp=p.with_name(p.name+'.'+str(os.getpid())+'.tmp')
    with tmp.open('w') as f: json.dump(v,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
    tmp.replace(p)
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def authorized(headers):
    try: token=(CONFIG/'api-token').read_text().strip()
    except OSError:return False
    return bool(token) and hmac.compare_digest(headers.get('X-Aski-Original-Token',''),token)
def catalog():
    # User-approved archive eligibility, independent of manual source reviews.
    from fractions import Fraction
    from urllib.parse import urlparse
    root=Path(read(CONFIG/'workflow.json')['nasRoot']).resolve()
    legacy={v['sha256']:v for v in read(CONFIG/'catalog.json')} if (CONFIG/'catalog.json').exists() else {}
    sources={}
    for manifest in (root/'instagram').glob('*/*/*/manifest.json'):
        try:
            m=read(manifest);a=m['assets'][0];path=(root/a['path']).resolve()
            if not manifest.resolve().is_relative_to(root) or not path.is_relative_to(root) or not path.is_file() or not a.get('decodeVerified'):continue
            if path.stat().st_size!=a['bytes'] or not re.fullmatch(r'[a-f0-9]{64}',a['sha256']):continue
            fps=float(Fraction(a['fps']));duration=min(float(a['duration']),int(a['frames'])/fps,15)
            if not math.isfinite(duration) or duration<2 or fps<=0:continue
            url=urlparse(m['sourceUrl']);parts=url.path.strip('/').split('/')
            if url.hostname not in ('instagram.com','www.instagram.com') or len(parts)<3 or parts[1] not in ('reel','p','tv'):continue
            c={'policy':POLICY,'selectionBasis':'registered_archive','manifest':str(manifest.relative_to(root)),
               'sha256':a['sha256'],'sourceUrl':m['sourceUrl'],'username':parts[0],
               'duration':duration,'start':0,'width':int(a['width']),'height':int(a['height']),'fps':fps,'overlayROI':None}
            old=legacy.get(a['sha256'],{})
            if old.get('overlayROI'):
                c.update(overlayROI=old['overlayROI'],start=old.get('start',0),duration=min(duration,old['duration']))
            sources[a['sha256']]=c
        except (ValueError,KeyError,TypeError,OSError,ZeroDivisionError):continue
    return list(sources.values())
def folder(jid):
    if not JOB.fullmatch(jid): raise ValueError('invalid_job')
    return ROOT/jid
def public(s):
    return {k:s.get(k) for k in ('id','status','progress','error','policy','sourceSha256','portraitSha256','verification','createdAt','wardrobe')}
def status(jid): return public(read(folder(jid)/'state.json'))
def healthy():
    try:return time.time()-(ROOT/'heartbeat').stat().st_mtime<90
    except OSError:return False

def source_key(c):
    match=re.search(r'/(?:reel|p|tv)/([^/?#]+)',c.get('sourceUrl',''))
    return match.group(1) if match else None

def used_source(candidate, exclude=None):
    for path in ROOT.glob('*/state.json'):
        if path.parent.name==exclude:continue
        state=read(path)
        if state['status'] not in ('queued','running','done'):continue
        if state.get('sourceSha256')==candidate['sha256']:return True
        prior=path.parent/'candidate.json'
        if source_key(candidate) and prior.exists() and source_key(read(prior))==source_key(candidate):return True
    return False

def requested_segment(candidate,data):
    start=float(data.get('start',candidate.get('start',0)))
    duration=float(data.get('duration',candidate['duration']))
    reviewed_start=float(candidate.get('start',0));reviewed_end=reviewed_start+float(candidate['duration'])
    if not math.isfinite(start) or not math.isfinite(duration) or start<reviewed_start or not 2<=duration<=15 or start+duration>reviewed_end+1e-6:raise ValueError('unreviewed_segment')
    return {**candidate,'start':start,'duration':duration}

def submit(data):
    wardrobe=wardrobe_video.normalize(data.get('wardrobe'))
    if wardrobe!='original' and data.get('wardrobePolicy')!=wardrobe_video.POLICY:raise ValueError('wardrobe_policy_update_required')
    request=data.get('requestId','')
    if not re.fullmatch(r'[a-f0-9-]{36}:\d{1,6}',request):raise ValueError('invalid_request')
    candidate=next((v for v in catalog() if v['sha256']==data.get('sourceSha256')),None)
    if not candidate:raise ValueError('source_not_archived')
    candidate=requested_segment(candidate,data)
    portrait=data.get('portrait','')
    if not isinstance(portrait,str) or not portrait.startswith('data:image/jpeg;base64,') or len(portrait)>700000:raise ValueError('fixed_portrait_required')
    image=base64.b64decode(portrait.split(',')[1],validate=True)
    if not image.startswith(b'\xff\xd8\xff'):raise ValueError('invalid_portrait')
    image_hash=hashlib.sha256(image).hexdigest()
    jid='orig_'+hashlib.sha256(request.encode()).hexdigest()[:32]
    ROOT.mkdir(parents=True,exist_ok=True)
    with (ROOT/'.submit.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        f=folder(jid);binding={'sourceSha256':candidate['sha256'],'portraitSha256':image_hash,'start':candidate['start'],'duration':candidate['duration'],'wardrobe':wardrobe}
        if (f/'state.json').exists():
            state=read(f/'state.json')
            stored=read(f/'candidate.json')
            if any(state.get(k,stored.get(k,'original' if k=='wardrobe' else 0 if k=='start' else None))!=v for k,v in binding.items()):raise ValueError('request_input_conflict')
            return public(state)
        if used_source(candidate):raise ValueError('source_already_used')
        if not healthy():raise ValueError('original_worker_offline')
        if sum(read(p).get('status') not in TERMINAL for p in ROOT.glob('*/state.json'))>=12:raise ValueError('original_queue_full')
        f.mkdir();(f/'portrait.jpg').write_bytes(image)
        save(f/'candidate.json',candidate)
        state={'id':jid,'status':'queued','progress':0,'policy':POLICY if wardrobe=='original' else wardrobe_video.POLICY,'createdAt':time.time(),**binding}
        save(f/'state.json',state)
        return public(state)

def cancel(jid):
    f=folder(jid);s=read(f/'state.json')
    if s['status'] not in TERMINAL:(f/'cancel').touch()
    return {'ok':True,'job':public(s)}
def handle(handler,path,send_json,post=False):
    if not authorized(handler.headers):send_json(handler,{'ok':False,'error':'unauthorized'},401);return
    try:
        parts=path.strip('/').split('/')
        if path=='/api/original-video/catalog' and not post:
            send_json(handler,{'ok':True,'policy':POLICY,'workerOnline':healthy(),'wardrobePolicy':wardrobe_video.POLICY,'wardrobeChoices':list(wardrobe_video.CHOICES),'sources':[{**{k:v[k] for k in ('sha256','sourceUrl','duration','width','height','fps','username')},'start':v.get('start',0),'used':used_source(v)} for v in catalog()]});return
        if path=='/api/original-video/generate' and post:
            size=int(handler.headers.get('Content-Length',0))
            if not 0<size<750000:raise ValueError('invalid_request_size')
            send_json(handler,{'ok':True,'job':submit(json.loads(handler.rfile.read(size)))},202);return
        if len(parts) not in (4,5) or parts[2]!='jobs':raise ValueError('invalid_route')
        jid=parts[3];f=folder(jid)
        if len(parts)==4 and not post:send_json(handler,{'ok':True,'job':status(jid)});return
        if len(parts)==5 and parts[4]=='cancel' and post:send_json(handler,cancel(jid));return
        files={'video':'output.mp4','comparison':'comparison.mp4','source':'source.mp4'}
        if len(parts)==5 and parts[4] in files and not post:
            if status(jid)['status']!='done':raise ValueError('quality_gate_not_passed')
            handler._serve_video(str(f/'render'/files[parts[4]]),jid,'inline',cache_control='private, no-store');return
        raise ValueError('invalid_route')
    except FileNotFoundError:send_json(handler,{'ok':False,'error':'not_found'},404)
    except (ValueError,KeyError,TypeError):send_json(handler,{'ok':False,'error':'original_video_rejected'},409)

def run_child(command,f,logname,progress):
    with (f/logname).open('w') as log:
        child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        started=time.time()
        try:
            while child.poll() is None:
                (ROOT/'heartbeat').touch()
                if (f/'cancel').exists():raise ValueError('cancelled')
                if time.time()-started>3600:raise ValueError('render_timeout')
                s=read(f/'state.json');s['progress']=progress;save(f/'state.json',s)
                time.sleep(3)
            if child.returncode:raise ValueError('quality_pipeline_failed')
        finally:
            if child.poll() is None:
                os.killpg(child.pid,signal.SIGTERM)
                try:child.wait(timeout=10)
                except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait()

def quality_gate(report,workflow,stats):
    result=report.get('output',{})
    scores=result.get('referenceSimilaritySamples',[])
    if not workflow.get('timingVerified') or workflow.get('source')!=workflow.get('output'):raise ValueError('timing_gate_failed')
    expected=max(5,math.ceil(workflow['source']['frames']/workflow['source']['fps']))
    if len(scores)!=expected or any(v is None or v<.45 for v in scores) or sum(scores)/len(scores)<.65:raise ValueError('identity_gate_failed')
    if len(stats.get('rawSkinDeltas',[]))!=workflow['source']['frames']:raise ValueError('face_coverage_gate_failed')
    if stats.get('model')!='hyperswap_1b_256' or stats.get('expressionFactor')!=0 or stats.get('appliedLabDelta') is not None:raise ValueError('profile_gate_failed')
    if result.get('lowerBodyMAE',999)>8 and not workflow.get('overlayROI'):raise ValueError('original_pixels_gate_failed')
    source_expressions=report.get('source',{}).get('expressionSamples',[])
    output_expressions=result.get('expressionSamples',[])
    pairs=[(a,b) for a,b in zip(source_expressions,output_expressions) if a and b and a['eyeAspectRatio']<.6]
    # Profile landmarks are unreliable; compare only usable frontal samples.
    if len(source_expressions)!=expected or len(output_expressions)!=expected or len(pairs)<math.ceil(expected*.6) or any(abs(a['eyeAspectRatio']-b['eyeAspectRatio'])>.05 or abs(a['mouthAspectRatio']-b['mouthAspectRatio'])>.15 for a,b in pairs):raise ValueError('expression_gate_failed')
    return {'policy':POLICY,'timingVerified':True,'faceCoverage':1,'expressionVerified':True,'sampleIdentityMean':sum(scores)/len(scores),'sampleIdentityMin':min(scores),'visualReview':'required','publishApproved':False}

def process(f,repo):
    s=read(f/'state.json')
    if (f/'cancel').exists():s.update(status='cancelled',error='사용자가 작업을 취소했습니다.');save(f/'state.json',s);return
    s.update(status='running',progress=5);save(f/'state.json',s)
    try:
        if s.get('wardrobe','original')!='original' and s.get('policy')!=wardrobe_video.POLICY:raise ValueError('legacy_wardrobe_job_requires_new_run')
        c=read(f/'candidate.json');cfg=read(CONFIG/'workflow.json')
        if sha(f/'portrait.jpg')!=s['portraitSha256']:raise ValueError('portrait_integrity_failed')
        manifest=Path(cfg['nasRoot'])/c['manifest']
        m=read(manifest)
        if m['assets'][0]['sha256']!=s['sourceSha256']:raise ValueError('source_integrity_failed')
        cfg.update(portrait=str(f/'portrait.jpg'),portraitSha256=s['portraitSha256']);save(f/'config.json',cfg)
        script=repo/'ops/face-quality';python=Path(cfg['engine'])/'.venv/bin/python'
        command=[str(python),str(script/'workflow.py'),'--manifest',str(manifest),'--config',str(f/'config.json'),'--output-dir',str(f/'render'),'--start',str(c.get('start',0)),'--duration',str(c['duration'])]
        command+=['--overlay-roi',*map(str,c['overlayROI'])] if c.get('overlayROI') else ['--no-account-overlay']
        run_child(command,f,'worker.log',35)
        run_child([str(python),str(script/'evaluate.py'),'--engine',cfg['engine'],'--source',str(f/'render/source.mp4'),'--portrait',str(f/'portrait.jpg'),'--folder',str(f/'render')],f,'quality.log',85)
        verification=quality_gate(read(f/'render/metrics.json'),read(f/'render/workflow.json'),read(f/'render/swapped.stats.json'))
        wardrobe=wardrobe_video.normalize(s.get('wardrobe'))
        if wardrobe!='original':
            def check():
                (ROOT/'heartbeat').touch()
                if (f/'cancel').exists():raise ValueError('cancelled')
                current=read(f/'state.json');current['progress']=70;save(f/'state.json',current)
            verification=wardrobe_video.process(f,repo,cfg,wardrobe,run_child,check)
        if (f/'cancel').exists():raise ValueError('cancelled')
        s.update(status='done',progress=100,verification=verification,error=None)
    except Exception as e:
        code=str(e) if isinstance(e,ValueError) else type(e).__name__
        s.update(status='cancelled' if code=='cancelled' else 'error',error='원본 기반 품질 검사 미통과 · '+code,progress=0)
    save(f/'state.json',s)

def main():
    ROOT.mkdir(parents=True,exist_ok=True)
    with (ROOT/'.worker.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        for p in ROOT.glob('*/state.json'):
            s=read(p)
            if s['status']=='running':s.update(status='error',error='작업자 재시작 · 자동 재생성하지 않았습니다.');save(p,s)
        while True:
            (ROOT/'heartbeat').touch()
            for p in sorted(ROOT.glob('*/state.json'),key=lambda p:p.stat().st_mtime):
                if read(p)['status']=='queued':process(p.parent,Path(__file__).resolve().parent)
            time.sleep(3)
if __name__=='__main__':main()
