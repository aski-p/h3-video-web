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
    return {k:s.get(k) for k in ('id','status','progress','error','policy','sourceSha256','portraitSha256','verification','createdAt','wardrobe','sourceReleasedAt','start','duration','requestedStart','requestedDuration','repairHistory')}
def status(jid):
    state=read(folder(jid)/'state.json')
    result=public(state)
    if state.get('status')=='running' and state.get('policy')==wardrobe_video.POLICY:
        generation=wardrobe_video.generation_progress(folder(jid))
        if generation:result['generation']=generation
    return result
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
        if state.get('sourceReleasedAt'):continue
        # Failed jobs still consume the source until the owner explicitly releases it.
        if state['status']=='cancelled':continue
        if state.get('sourceSha256')==candidate['sha256']:return True
        prior=path.parent/'candidate.json'
        if source_key(candidate) and prior.exists() and source_key(read(prior))==source_key(candidate):return True
    return False

def catalog_sources():
    hashes=set();posts=set()
    for path in ROOT.glob('*/state.json'):
        state=read(path)
        if state.get('status')=='cancelled' or state.get('sourceReleasedAt'):continue
        hashes.add(state.get('sourceSha256'))
        prior=path.parent/'candidate.json'
        if prior.exists():posts.add(source_key(read(prior)))
    return [{**{k:v[k] for k in ('sha256','sourceUrl','duration','width','height','fps','username')},
             'start':v.get('start',0),'used':v['sha256'] in hashes or bool(source_key(v) and source_key(v) in posts)} for v in catalog()]

def source_thumbnail(source_sha):
    """Return a small cached frame from an authenticated archived source."""
    if not re.fullmatch(r'[a-f0-9]{64}',source_sha):raise ValueError('invalid_source_hash')
    cache=ROOT/'source-thumbnails'/f'{source_sha}.jpg'
    if cache.is_file():
        image=cache.read_bytes()
        if image.startswith(b'\xff\xd8\xff') and len(image)<500000:return image
    candidate=next((v for v in catalog() if v['sha256']==source_sha),None)
    if candidate is None:raise FileNotFoundError('source_not_archived')
    root=Path(read(CONFIG/'workflow.json')['nasRoot']).resolve()
    manifest=(root/candidate['manifest']).resolve()
    if not manifest.is_relative_to(root):raise ValueError('source_path_invalid')
    asset=read(manifest)['assets'][0]
    source=(root/asset['path']).resolve()
    if not source.is_relative_to(root) or not source.is_file() or sha(source)!=source_sha:
        raise ValueError('source_integrity_failed')
    frame=subprocess.run(['ffmpeg','-v','error','-ss','0.1','-i',str(source),'-frames:v','1',
                          '-vf','scale=320:-2','-q:v','5','-f','image2','pipe:1'],
                         capture_output=True,timeout=20,check=True).stdout
    if not frame.startswith(b'\xff\xd8\xff') or len(frame)>500000:raise ValueError('thumbnail_failed')
    cache.parent.mkdir(parents=True,exist_ok=True)
    tmp=cache.with_name(cache.name+'.'+str(os.getpid())+'.tmp')
    tmp.write_bytes(frame);tmp.replace(cache)
    return frame

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
    if wardrobe in ('portrait_hair','portrait_face') and candidate['duration']<5:raise ValueError('portrait_edit_requires_five_seconds')
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
            if any(state.get('requestedWardrobe' if k=='wardrobe' and 'requestedWardrobe' in state else
                             'requestedStart' if k=='start' and 'requestedStart' in state else
                             'requestedDuration' if k=='duration' and 'requestedDuration' in state else k,
                             stored.get(k,'original' if k=='wardrobe' else 0 if k=='start' else None))!=v
                   for k,v in binding.items()):raise ValueError('request_input_conflict')
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
def retry_wardrobe(jid,repair=None):
    ROOT.mkdir(parents=True,exist_ok=True)
    with (ROOT/'.submit.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        f=folder(jid);s=read(f/'state.json')
        if s.get('policy')!=wardrobe_video.POLICY or s.get('sourceReleasedAt') or (f/'cancel').exists():
            raise ValueError('wardrobe_retry_unavailable')
        if s['status'] in ('queued','running'):return {'ok':True,'job':public(s)}
        if s['status']!='error':
            raise ValueError('wardrobe_retry_unavailable')
        history=list(s.get('repairHistory',[]))
        if str(s.get('error','')).endswith('wardrobe_timeout') and not repair:
            record=f/'render/wardrobe/generation.json'
            if not record.is_file() or not record.with_suffix('.graph.json').is_file():
                raise ValueError('wardrobe_generation_record_missing')
            history.append({'reason':s.get('error'),'at':time.time(),'action':'resume_existing_generation'})
        else:
            if not isinstance(repair,dict) or s.get('wardrobe')!='portrait_hair' or len(history)>=3:
                raise ValueError('wardrobe_retry_unavailable')
            log=f/'hair-mask.log'
            if not log.is_file() or not any('ValueError: '+code in log.read_text(errors='replace')
                                            for code in ('hair_multiple_faces','hair_tracking_incomplete',
                                                         'hair_tracking_gap','hair_head_out_of_frame','hair_mask_too_wide')):
                raise ValueError('hair_mask_repair_unavailable')
            if (f/'render/wardrobe/generation.json').exists():
                raise ValueError('hair_mask_repair_after_generation')
            candidate=read(f/'candidate.json')
            original={'start':s.get('requestedStart',s['start']),
                      'duration':s.get('requestedDuration',s['duration'])}
            changed=requested_segment({**candidate,**original},repair)
            if changed['duration']<5 or (changed['start'],changed['duration'])==(s['start'],s['duration']):
                raise ValueError('hair_mask_repair_interval_invalid')
            destination=f/f'repair-attempt-{len(history)+1}'
            if destination.exists() or not (f/'render').is_dir():
                raise ValueError('hair_mask_repair_evidence_missing')
            (f/'render').rename(destination)
            save(f/'candidate.json',changed)
            history.append({'reason':s.get('error'),'at':time.time(),'action':'retry_source_interval',
                            'start':changed['start'],'duration':changed['duration']})
            s.update(requestedStart=original['start'],requestedDuration=original['duration'],
                     start=changed['start'],duration=changed['duration'])
        s.update(status='queued',progress=0,error=None,repairHistory=history)
        save(f/'state.json',s)
        return {'ok':True,'job':public(s)}
def retry_visual_hair(jid,reason):
    if reason!='hair_reference_not_applied':raise ValueError('hair_visual_reason_invalid')
    ROOT.mkdir(parents=True,exist_ok=True)
    with (ROOT/'.submit.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        f=folder(jid);s=read(f/'state.json')
        if s.get('policy')!=wardrobe_video.POLICY or s.get('wardrobe')!='portrait_hair' or s.get('sourceReleasedAt') or (f/'cancel').exists():
            raise ValueError('hair_visual_retry_unavailable')
        if s['status'] in ('queued','running'):return {'ok':True,'job':public(s)}
        if s['status']=='done':
            if s.get('verification',{}).get('hairVisualReview')!='required':
                raise ValueError('hair_visual_retry_unavailable')
        elif s['status']=='error':
            log=f/'hair-reference.log'
            if not log.is_file() or 'hair_reference_color_mismatch' not in log.read_text(errors='replace'):
                raise ValueError('hair_visual_retry_unavailable')
        else:raise ValueError('hair_visual_retry_unavailable')
        history=list(s.get('repairHistory',[]))
        if len(history)>=3:raise ValueError('hair_visual_retry_limit')
        destination=f/f'repair-attempt-{len(history)+1}'
        if destination.exists() or not (f/'render').is_dir():raise ValueError('hair_visual_evidence_missing')
        (f/'render').rename(destination)
        history.append({'reason':reason,'at':time.time(),'action':'regenerate_hair_reference',
                        'previousVerification':s.get('verification')})
        s.update(status='queued',progress=0,error=None,verification=None,repairHistory=history)
        save(f/'state.json',s)
        return {'ok':True,'job':public(s)}
def retry_face_only(jid):
    """Reprocess a portrait-hair job with a face-only mask, retaining its source."""
    ROOT.mkdir(parents=True,exist_ok=True)
    with (ROOT/'.submit.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        f=folder(jid);s=read(f/'state.json')
        if s.get('policy')!=wardrobe_video.POLICY or s.get('sourceReleasedAt') or (f/'cancel').exists():
            raise ValueError('face_only_retry_unavailable')
        if s.get('wardrobe')=='portrait_face' and s['status'] in ('queued','running'):
            return {'ok':True,'job':public(s)}
        if s.get('wardrobe')!='portrait_hair' or s['status'] not in ('done','error'):
            raise ValueError('face_only_retry_unavailable')
        history=list(s.get('repairHistory',[]))
        destination=f/f'repair-attempt-{len(history)+1}'
        if destination.exists() or not (f/'render').is_dir():raise ValueError('face_only_evidence_missing')
        (f/'render').rename(destination)
        history.append({'reason':'user_requested_face_only','at':time.time(),
                        'action':'regenerate_face_only','previousVerification':s.get('verification')})
        s.update(status='queued',progress=0,error=None,verification=None,
                 requestedWardrobe=s.get('requestedWardrobe',s['wardrobe']),
                 wardrobe='portrait_face',repairHistory=history)
        save(f/'state.json',s)
        return {'ok':True,'job':public(s)}
def retry_face_segment(jid,start,duration):
    """Move a failed face-only edit to a source-bounded segment with a visible face."""
    ROOT.mkdir(parents=True,exist_ok=True)
    with (ROOT/'.submit.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        f=folder(jid);s=read(f/'state.json')
        if s.get('policy')!=wardrobe_video.POLICY or s.get('wardrobe')!='portrait_face' or s.get('sourceReleasedAt') or (f/'cancel').exists():
            raise ValueError('face_segment_retry_unavailable')
        if s['status'] in ('queued','running'):return {'ok':True,'job':public(s)}
        if s['status']!='error' or 'wardrobe_face_coverage_failed' not in s.get('error',''):
            raise ValueError('face_segment_retry_unavailable')
        candidate=next((item for item in catalog() if item['sha256']==s['sourceSha256']),None)
        if candidate is None:raise ValueError('source_not_archived')
        changed=requested_segment(candidate,{'start':start,'duration':duration})
        if changed['duration']<5:raise ValueError('portrait_edit_requires_five_seconds')
        history=list(s.get('repairHistory',[]))
        destination=f/f'repair-attempt-{len(history)+1}'
        if destination.exists() or not (f/'render').is_dir():raise ValueError('face_segment_evidence_missing')
        (f/'render').rename(destination)
        old=read(f/'candidate.json')
        save(f/'candidate.json',{**old,'start':changed['start'],'duration':changed['duration']})
        history.append({'reason':'wardrobe_face_coverage_failed','at':time.time(),
                        'action':'retry_face_visible_segment','start':changed['start'],
                        'duration':changed['duration']})
        s.update(status='queued',progress=0,error=None,verification=None,
                 requestedStart=s.get('requestedStart',s['start']),
                 requestedDuration=s.get('requestedDuration',s['duration']),
                 start=changed['start'],duration=changed['duration'],repairHistory=history)
        save(f/'state.json',s)
        return {'ok':True,'job':public(s)}
def retry_face_artifact(jid):
    """Regenerate an uncovered face when the previous H3 render failed identity QC."""
    ROOT.mkdir(parents=True,exist_ok=True)
    with (ROOT/'.submit.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        f=folder(jid);s=read(f/'state.json')
        if s.get('policy')!=wardrobe_video.POLICY or s.get('wardrobe')!='portrait_face' or s.get('sourceReleasedAt') or (f/'cancel').exists():
            raise ValueError('face_artifact_retry_unavailable')
        if s['status'] in ('queued','running'):return {'ok':True,'job':public(s)}
        if s['status']!='error' or not any(code in s.get('error','') for code in
                ('wardrobe_identity_failed','wardrobe_face_coverage_failed')):
            raise ValueError('face_artifact_retry_unavailable')
        history=list(s.get('repairHistory',[]))
        if sum(item.get('action')=='regenerate_uncovered_face' for item in history)>=2:
            raise ValueError('face_artifact_retry_limit')
        destination=f/f'repair-attempt-{len(history)+1}'
        if destination.exists() or not (f/'render').is_dir():raise ValueError('face_artifact_evidence_missing')
        (f/'render').rename(destination)
        history.append({'reason':s['error'],'at':time.time(),'action':'regenerate_uncovered_face'})
        s.update(status='queued',progress=0,error=None,verification=None,repairHistory=history)
        save(f/'state.json',s)
        return {'ok':True,'job':public(s)}
def restore_verified_face_result(jid):
    """Recover a previously quality-passed render after the user waives hair matching."""
    ROOT.mkdir(parents=True,exist_ok=True)
    with (ROOT/'.submit.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        f=folder(jid);s=read(f/'state.json')
        if s.get('status')=='done' and s.get('verification',{}).get('restoredVerifiedFace'):
            return {'ok':True,'job':public(s)}
        if s.get('status')!='error' or s.get('wardrobe')!='portrait_face' or s.get('policy')!=wardrobe_video.POLICY or s.get('sourceReleasedAt') or (f/'cancel').exists():
            raise ValueError('verified_face_restore_unavailable')
        history=list(s.get('repairHistory',[]))
        originals=[(index+1,item['previousVerification']) for index,item in enumerate(history)
                   if item.get('action')=='regenerate_face_only' and item.get('previousVerification')]
        if len(originals)!=1:raise ValueError('verified_face_evidence_missing')
        attempt,receipt=originals[0]
        if (receipt.get('policy')!=wardrobe_video.POLICY or receipt.get('wardrobe')!='portrait_hair' or
            receipt.get('faceCoverage')!=1 or receipt.get('sampleIdentityMin',0)<.45 or
            receipt.get('sampleIdentityMean',0)<.65 or receipt.get('faceModel')!='hyperswap_1b_256' or
            receipt.get('steps')!=20 or receipt.get('publishApproved') is not False):
            raise ValueError('verified_face_quality_missing')
        old=f/f'repair-attempt-{attempt}'
        if not all((old/name).is_file() for name in ('source.mp4','output.mp4','comparison.mp4')):
            raise ValueError('verified_face_files_missing')
        expected=receipt.get('dimensions')
        if wardrobe_video.probe(old/'output.mp4')!=expected or wardrobe_video.probe(old/'source.mp4')!=expected:
            raise ValueError('verified_face_timing_mismatch')
        for name in ('source.mp4','output.mp4'):
            subprocess.run(['ffmpeg','-v','error','-i',str(old/name),'-f','null','-'],
                           stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=120,check=True)
        destination=f/f'repair-attempt-{len(history)+1}'
        if destination.exists() or not (f/'render').is_dir():raise ValueError('verified_face_archive_conflict')
        (f/'render').rename(destination)
        old.rename(f/'render')
        recovered={**receipt,'hairVisualReview':'waived_by_user','publishApproved':False,
                   'effectiveRequest':'portrait_face','restoredVerifiedFace':True,
                   'restoredFromAttempt':attempt}
        history.append({'at':time.time(),'action':'restore_verified_face_result',
                        'sourceAttempt':attempt,'failedAttempt':destination.name,
                        'reason':'user_accepted_original_hair'})
        s.update(status='done',progress=100,error=None,verification=recovered,repairHistory=history)
        save(f/'state.json',s)
        return {'ok':True,'job':public(s)}
def release_source(jid):
    ROOT.mkdir(parents=True,exist_ok=True)
    with (ROOT/'.submit.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        f=folder(jid);s=read(f/'state.json')
        if s.get('status') not in TERMINAL:raise ValueError('source_release_requires_terminal_job')
        if not s.get('sourceReleasedAt'):
            s['sourceReleasedAt']=time.time();save(f/'state.json',s)
        return {'ok':True,'job':public(s)}
def handle(handler,path,send_json,post=False):
    if not authorized(handler.headers):send_json(handler,{'ok':False,'error':'unauthorized'},401);return
    try:
        parts=path.strip('/').split('/')
        if path=='/api/original-video/catalog' and not post:
            send_json(handler,{'ok':True,'policy':POLICY,'workerOnline':healthy(),'wardrobePolicy':wardrobe_video.POLICY,'wardrobeChoices':list(wardrobe_video.CHOICES),'sources':catalog_sources()});return
        if len(parts)==4 and parts[2]=='source-thumbnail' and not post:
            image=source_thumbnail(parts[3])
            handler.send_response(200)
            handler.send_header('Content-Type','image/jpeg')
            handler.send_header('Content-Length',str(len(image)))
            handler.send_header('Cache-Control','private, max-age=86400')
            handler.end_headers();handler.wfile.write(image);return
        if path=='/api/original-video/generate' and post:
            size=int(handler.headers.get('Content-Length',0))
            if not 0<size<750000:raise ValueError('invalid_request_size')
            send_json(handler,{'ok':True,'job':submit(json.loads(handler.rfile.read(size)))},202);return
        if len(parts) not in (4,5) or parts[2]!='jobs':raise ValueError('invalid_route')
        jid=parts[3];f=folder(jid)
        if len(parts)==4 and not post:send_json(handler,{'ok':True,'job':status(jid)});return
        if len(parts)==5 and parts[4]=='cancel' and post:send_json(handler,cancel(jid));return
        if len(parts)==5 and parts[4]=='retry-wardrobe' and post:
            size=int(handler.headers.get('Content-Length',0))
            if size<0 or size>512:raise ValueError('invalid_request_size')
            repair=json.loads(handler.rfile.read(size)) if size else None
            send_json(handler,retry_wardrobe(jid,repair));return
        if len(parts)==5 and parts[4]=='retry-visual-hair' and post:
            size=int(handler.headers.get('Content-Length',0))
            if not 0<size<=128:raise ValueError('invalid_request_size')
            data=json.loads(handler.rfile.read(size))
            send_json(handler,retry_visual_hair(jid,data.get('reason')));return
        if len(parts)==5 and parts[4]=='retry-face-only' and post:
            send_json(handler,retry_face_only(jid));return
        if len(parts)==5 and parts[4]=='retry-face-segment' and post:
            size=int(handler.headers.get('Content-Length',0))
            if not 0<size<=128:raise ValueError('invalid_request_size')
            data=json.loads(handler.rfile.read(size))
            send_json(handler,retry_face_segment(jid,data.get('start'),data.get('duration')));return
        if len(parts)==5 and parts[4]=='retry-face-artifact' and post:
            send_json(handler,retry_face_artifact(jid));return
        if len(parts)==5 and parts[4]=='restore-verified-face' and post:
            send_json(handler,restore_verified_face_result(jid));return
        if len(parts)==5 and parts[4]=='release-source' and post:send_json(handler,release_source(jid));return
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
            if child.returncode:
                path=f/logname
                with path.open('rb') as reader:
                    reader.seek(max(0,path.stat().st_size-16384))
                    tail=reader.read().decode(errors='replace')
                codes=re.findall(r'ValueError: ((?:hair|wardrobe|face)_[a-z0-9_]+|content_blocked)',tail)
                raise ValueError(codes[-1] if codes else 'quality_pipeline_failed')
        finally:
            if child.poll() is None:
                os.killpg(child.pid,signal.SIGTERM)
                try:child.wait(timeout=10)
                except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait()

def face_coverage(stats,frames):
    counts=stats.get('frameSwapCounts')
    # Old receipts lack per-frame instrumentation; new runs must record every frame.
    return (len(counts)==frames and all(n==1 for n in counts)) if counts is not None else len(stats.get('rawSkinDeltas',[]))==frames

def repairable(code):
    return code in ('identity_gate_failed','face_coverage_gate_failed','expression_gate_failed')

def prepare_wardrobe_source(f,c,cfg,manifest):
    """Extract motion without running the unrelated source-identity gate."""
    asset=manifest['assets'][0]
    original=(Path(cfg['nasRoot'])/asset['path']).resolve()
    root=Path(cfg['nasRoot']).resolve()
    if not original.is_relative_to(root) or not original.is_file() or sha(original)!=asset['sha256']:
        raise ValueError('source_integrity_failed')
    render=f/'render';render.mkdir()
    run_child(['ffmpeg','-v','error','-y','-ss',str(c.get('start',0)),'-i',str(original),'-t',str(c['duration']),'-map','0:v:0','-map','0:a?','-c:v','libx264','-crf','18','-preset','fast','-c:a','aac','-movflags','+faststart',str(render/'source.mp4')],f,'wardrobe-source-extract.log',20)
    meta=wardrobe_video.probe(render/'source.mp4')
    if abs(meta['frames']/meta['fps']-c['duration'])>1/meta['fps']+1e-6:
        raise ValueError('wardrobe_source_timing_failed')

def quality_gate(report,workflow,stats):
    result=report.get('output',{})
    scores=result.get('referenceSimilaritySamples',[])
    if not workflow.get('timingVerified') or workflow.get('source')!=workflow.get('output'):raise ValueError('timing_gate_failed')
    expected=max(5,math.ceil(workflow['source']['frames']/workflow['source']['fps']))
    if len(scores)!=expected or any(v is None or v<.45 for v in scores) or sum(scores)/len(scores)<.65:raise ValueError('identity_gate_failed')
    if not face_coverage(stats,workflow['source']['frames']):raise ValueError('face_coverage_gate_failed')
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
        wardrobe=wardrobe_video.normalize(s.get('wardrobe'))
        if wardrobe!='original':
            prepare_wardrobe_source(f,c,cfg,m)
            def check():
                (ROOT/'heartbeat').touch()
                if (f/'cancel').exists():raise ValueError('cancelled')
                current=read(f/'state.json');current['progress']=70;save(f/'state.json',current)
            verification=wardrobe_video.process(f,repo,cfg,wardrobe,run_child,check)
            if (f/'cancel').exists():raise ValueError('cancelled')
            s={**s,**read(f/'state.json')};s.update(status='done',progress=100,verification=verification,error=None)
            save(f/'state.json',s);return
        command=[str(python),str(script/'workflow.py'),'--manifest',str(manifest),'--config',str(f/'config.json'),'--output-dir',str(f/'render'),'--start',str(c.get('start',0)),'--duration',str(c['duration'])]
        command+=['--overlay-roi',*map(str,c['overlayROI'])] if c.get('overlayROI') else ['--auto-account-overlay']
        # Keep failed artifacts and adjust matching; never lower output acceptance thresholds.
        import shutil
        for attempt,distance in enumerate((.3,.45,.6)):
            if (f/'cancel').exists():raise ValueError('cancelled')
            current=read(f/'state.json');current.update(repairAttempt=attempt+1,repairLimit=3);save(f/'state.json',current)
            run_child(command+['--reference-distance',str(distance)],f,f'worker-{attempt+1}.log',35)
            run_child([str(python),str(script/'evaluate.py'),'--engine',cfg['engine'],'--source',str(f/'render/source.mp4'),'--portrait',str(f/'portrait.jpg'),'--folder',str(f/'render')],f,f'quality-{attempt+1}.log',85)
            try:
                verification=quality_gate(read(f/'render/metrics.json'),read(f/'render/workflow.json'),read(f/'render/swapped.stats.json'))
                break
            except ValueError as error:
                if not repairable(str(error)) or attempt==2:raise
                shutil.move(str(f/'render'),str(f/f'repair-attempt-{attempt+1}'))
                current=read(f/'state.json');history=current.get('repairHistory',[])
                history.append({'attempt':attempt+1,'reason':str(error),'nextReferenceDistance':(.45,.6)[attempt]})
                current.update(repairHistory=history,error='얼굴 매칭 설정 조정 후 자동 재처리 중');save(f/'state.json',current)
        if (f/'cancel').exists():raise ValueError('cancelled')
        s={**s,**read(f/'state.json')};s.update(status='done',progress=100,verification=verification,error=None)
    except Exception as e:
        s={**s,**read(f/'state.json')}
        code=str(e) if isinstance(e,ValueError) else type(e).__name__
        s.update(status='cancelled' if code=='cancelled' else 'error',error='원본 기반 품질 검사 미통과 · '+code,
                 progress=s.get('progress',0))
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
