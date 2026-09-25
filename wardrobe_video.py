"""Explicit opt-in wardrobe edits. Separate receipts; never an original-pixel fallback."""
import asyncio,json,math,re,shutil,subprocess,threading,time,urllib.error,urllib.parse,urllib.request,uuid
from pathlib import Path
POLICY='wardrobe-h3-ref2va-v2-20260922'
MODEL='minimax_h3_ref2va_pruned_int8_convrot.safetensors'
CHOICES={'dress':'an opaque navy blue knee-length short-sleeved dress with a crew neckline',
         'sportswear':'an opaque dark teal athletic crew-neck T-shirt and full-length black leggings',
         'casual':'an opaque white crew-neck T-shirt and relaxed blue jeans',
         'bikini':'an opaque navy blue two-piece bikini with secure straps and standard-coverage bottoms, ordinary swimwear',
         'swimsuit':'an opaque navy blue one-piece swimsuit with secure shoulder straps and standard coverage',
         'yoga':'an opaque muted sage fitted sleeveless yoga top and full-length high-waisted charcoal yoga leggings',
         'portrait_hair':None}
COMFY=Path('/home/aski/ComfyUI');OUTPUT=Path('/home/aski/minimax-h3/output');URL='http://127.0.0.1:8188'
def normalize(value):
    if value is None:return 'original'
    if not isinstance(value,str) or value not in ('original',*CHOICES):raise ValueError('invalid_wardrobe')
    return value

def size(width,height):
    scale=min(1,1280/max(width,height))
    return max(16,math.floor(width*scale/16+.5)*16),max(16,math.floor(height*scale/16+.5)*16)

def hair_size(width,height):
    scale=min(1,1280/max(width,height))
    target_width,target_height=width*scale,height*scale
    aligned_width=max(32,math.floor(target_width/32)*32)
    scale*=aligned_width/target_width
    return aligned_width,max(32,math.floor(height*scale/32)*32)

def frame_plan(meta):
    seconds=meta['frames']/meta['fps']
    if not math.isfinite(seconds) or not 2<=seconds<=15.001:raise ValueError('invalid_wardrobe_duration')
    frames=math.floor(seconds*24+1e-6)
    return {'frames':frames,'generatedFrames':5+17*math.ceil((frames-5)/17),'fps':24}

def motion_graph(repo,image,video,choice,width,height,length,prefix):
    choice=normalize(choice)
    if choice in ('original','portrait_hair'):raise ValueError('wardrobe_motion_choice_required')
    g=json.loads((repo/'ops/wardrobe-h3/graph.json').read_text())
    prompt=("<Picture 1> defines the exact adult facial identity. <Video 1> defines only the body movement, timing, full head-to-knee framing, background and stationary camera. Generate the same motion with the face from Picture 1. Exactly one adult woman, age 28. Her full face remains visible, unobstructed and large enough to recognize in every frame. No other people, faces, portraits, reflections or face-like background details. CHANGE the video outfit to "+CHOICES[choice]+". No cardigan. Non-sexual ordinary fashion. Natural skin with soft highlights, visible subtle fabric texture and realistic folds. Preserve the entire head with margin above the hair. No zoom, no reframing, no additional action. Do not copy the video person's face. No text or logo.")
    g['5']['inputs'].update(prompt=prompt,width=width,height=height,length=length)
    g['15']['inputs']['image']=image;g['16']['inputs']['file']=video
    g['14']['inputs']['filename_prefix']=prefix
    return g

def hair_graph(repo,image,video,mask,width,height,prefix):
    """Ref2VA identity conditioning over a masked source-video AV latent."""
    g=json.loads((repo/'ops/wardrobe-h3/graph.json').read_text())
    g['5']['inputs'].update(
        prompt=("<Picture 1> is the sole identity and hair reference. Video editing: regenerate ONLY the masked head and hair region of the source video. Replace the original person's face and every visible hair strand with Picture 1's face and hair, including its length, part, hairline and color. The source person's original hairstyle and hair color must disappear completely; do not copy blonde or light hair from the source when Picture 1 has dark hair. The new hair follows the original head motion throughout the shot. Preserve the unmasked clothing, body, hands, background, camera, lighting and audio exactly. One adult woman, no extra people, text or logos."),
        width=width,height=height,length=['18',2])
    g['5']['inputs'].pop('ref_videos.ref_video_1',None)
    g['15']['inputs']['image']=image;g['16']['inputs']['file']=video
    g['18']={'class_type':'MiniMaxH3TrimSourceAV','inputs':{'frames':['17',0],'audio':['17',1]}}
    g['19']={'class_type':'ImageScale','inputs':{'image':['18',0],'upscale_method':'lanczos','width':width,'height':height,'crop':'center'}}
    g['20']={'class_type':'LoadVideo','inputs':{'file':mask}}
    g['21']={'class_type':'GetVideoComponents','inputs':{'video':['20',0]}}
    g['22']={'class_type':'ImageToMask','inputs':{'image':['21',0],'channel':'red'}}
    g['23']={'class_type':'ThresholdMask','inputs':{'mask':['22',0],'value':.5}}
    g['24']={'class_type':'MiniMaxH3MaskGridPreview','inputs':{'image':['19',0],'mask':['23',0],
        'cell_selection':'runtime exact (latent max)','cell_adjust':0,'overlay_opacity':.38,
        'show_grid':True,'show_source_outline':True}}
    g['25']={'class_type':'VAEEncode','inputs':{'pixels':['19',0],'vae':['3',0]}}
    g['26']={'class_type':'VAEEncodeAudio','inputs':{'audio':['18',1],'vae':['4',0]}}
    g['27']={'class_type':'LTXVConcatAVLatent','inputs':{'video_latent':['25',0],'audio_latent':['26',0]}}
    g['28']={'class_type':'MiniMaxH3SetGenerationMask','inputs':{'av_latent':['27',0],
        'mask':['24',0],'mask_meaning':'white = generate','audio_mode':'preserve source audio'}}
    g['29']={'class_type':'MiniMaxH3PerRowMaskPatch','inputs':{'model':['1',0]}}
    g['30']={'class_type':'LTXVSeparateAVLatent','inputs':{'av_latent':['10',0]}}
    g['8']['inputs']['model']=['29',0];g['9']['inputs']['model']=['29',0]
    g['10']['inputs']['latent_image']=['28',0]
    g['11']['inputs']['samples']=['30',0];g['12']['inputs']['samples']=['30',1]
    g['14']['inputs']['filename_prefix']=prefix
    return g

def has_audio(path):
    result=subprocess.run(['ffprobe','-v','error','-select_streams','a:0',
        '-show_entries','stream=index','-of','csv=p=0',str(path)],capture_output=True,text=True,timeout=30,check=True)
    return bool(result.stdout.strip())

def prepare_hair_source(source,video,width,height,frames,child,folder):
    command=['ffmpeg','-v','error','-y','-i',str(source)]
    audio=has_audio(source)
    if not audio:command+=['-f','lavfi','-i','anullsrc=channel_layout=stereo:sample_rate=48000']
    command+=['-map','0:v:0','-map','0:a:0' if audio else '1:a:0',
        '-vf',f'fps=24,scale={width}:{height}:flags=lanczos,tpad=stop_mode=clone:stop_duration=1',
        '-frames:v',str(frames),'-r','24','-c:v','libx264','-crf','18','-pix_fmt','yuv420p']
    if audio:command+=['-af','apad']
    command+=['-c:a','aac','-ar','48000','-b:a','128k','-t',f'{frames/24:.6f}',str(video)]
    child(command,folder,'hair-source-prepare.log',18)
    if probe(video)!={'width':width,'height':height,'fps':24.0,'frames':frames}:
        raise ValueError('hair_source_prepare_mismatch')

def api(path,data=None):
    req=urllib.request.Request(URL+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=60) as r:return json.load(r)

def _clock_seconds(value):
    parts=value.split(':')
    if not 1<=len(parts)<=3 or any(not p.isdigit() for p in parts):return None
    seconds=0
    for part in parts:seconds=seconds*60+int(part)
    return seconds

def _sampler_log(lines,total,submitted_at):
    """Read the last ComfyUI tqdm receipt after this prompt was submitted.

    The HTTP queue has no sampler counter. Journald is read-only evidence and may
    lag; callers must display its timestamp and never call it overall completion.
    """
    latest=None
    for row in lines:
        try:
            timestamp=int(row['__REALTIME_TIMESTAMP'])/1000000
            message=row.get('MESSAGE','')
            if isinstance(message,list):message=bytes(message).decode(errors='replace')
            if timestamp+1<submitted_at:continue
            if 'Prompt executed' in message:latest=None
            matches=list(re.finditer(r'(\d{1,3})%[^\r\n]*?(\d{1,3})/(\d{1,3}) \[(\d+:\d+(?::\d+)?)<(?:(\d+:\d+(?::\d+)?)|\?)',message))
            for match in matches:
                percent,step,maximum=map(int,match.group(1,2,3))
                if maximum!=total or not 0<=step<=total or percent!=round(step*100/total):continue
                latest={'step':step,'steps':total,'percent':percent,'remainingSeconds':_clock_seconds(match.group(5)) if match.group(5) else None,'observedAt':timestamp}
        except (KeyError,TypeError,ValueError):continue
    return latest

def _progress_event(message,prompt_id,node_id,total,now,previous=None):
    if message.get('type')!='progress_state':return None
    data=message.get('data') or {}
    if data.get('prompt_id')!=prompt_id:return None
    node=(data.get('nodes') or {}).get(str(node_id)) or {}
    try:
        step=int(node['value']);maximum=int(node['max'])
        if maximum!=total or not 0<=step<=total or step!=float(node['value']):return None
    except (KeyError,TypeError,ValueError):return None
    rate=None
    if previous and step>previous.get('step',0):
        elapsed=now-previous.get('observedAt',now)
        if 0<elapsed<14400:rate=elapsed/(step-previous['step'])
    if rate is None and previous:rate=previous.get('secondsPerStep')
    return {'promptId':prompt_id,'status':'running','step':step,'steps':total,'percent':round(step*100/total),
            'remainingSeconds':round((total-step)*rate) if rate else None,
            'secondsPerStep':rate,'observedAt':now}

def _sampler_progress_config(graph):
    for node_id,node in graph.items():
        if node.get('class_type')!='SamplerCustomAdvanced':continue
        sigmas=node.get('inputs',{}).get('sigmas')
        if not isinstance(sigmas,list) or not sigmas:continue
        scheduler=graph.get(str(sigmas[0]),{})
        if scheduler.get('class_type')!='BasicScheduler':continue
        try:steps=int(scheduler['inputs']['steps'])
        except (KeyError,TypeError,ValueError):continue
        if steps>0:return node_id,steps
    return None

def _watch_progress(record,prompt_id,client_id,node_id,total,stop):
    """Persist ComfyUI's per-step WebSocket events; tqdm is buffered on PGX."""
    try:import aiohttp
    except ImportError:return
    progress_file=record.with_suffix('.progress.json')
    previous=None
    if progress_file.exists():
        try:
            saved=json.loads(progress_file.read_text())
            if saved.get('promptId')==prompt_id:previous=saved
        except (OSError,ValueError):pass
    async def listen():
        nonlocal previous
        url='ws://127.0.0.1:8188/ws?clientId='+urllib.parse.quote(client_id,safe='')
        while not stop.is_set():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url,heartbeat=30,timeout=5) as ws:
                        while not stop.is_set():
                            try:message=await ws.receive(timeout=10)
                            except asyncio.TimeoutError:continue
                            if message.type==aiohttp.WSMsgType.CLOSED:break
                            if message.type!=aiohttp.WSMsgType.TEXT:continue
                            try:receipt=_progress_event(json.loads(message.data),prompt_id,node_id,total,time.time(),previous)
                            except (ValueError,TypeError):continue
                            if not receipt:continue
                            previous=receipt
                            temporary=progress_file.with_suffix('.progress.tmp')
                            temporary.write_text(json.dumps(receipt));temporary.replace(progress_file)
            except (OSError,aiohttp.ClientError,asyncio.TimeoutError):pass
            await asyncio.sleep(2)
    try:asyncio.run(listen())
    except (OSError,RuntimeError):pass

def generation_progress(folder):
    record=folder/'render/wardrobe/generation.json'
    if not record.is_file():return None
    try:
        generation=json.loads(record.read_text())
        prompt_id=generation.get('promptId')
        if not isinstance(prompt_id,str) or not re.fullmatch(r'[a-f0-9-]{36}',prompt_id):return None
        req=urllib.request.Request(URL+'/queue')
        with urllib.request.urlopen(req,timeout=3) as response:queue=json.load(response)
        running=any(entry[1]==prompt_id for entry in queue.get('queue_running',[]))
        pending=next((index+1 for index,entry in enumerate(queue.get('queue_pending',[])) if entry[1]==prompt_id),None)
        if not running:return {'status':'queued','queuePosition':pending} if pending is not None else None
        entry=next(entry for entry in queue['queue_running'] if entry[1]==prompt_id)
        submitted_at=entry[3].get('create_time',0)/1000 if len(entry)>3 and isinstance(entry[3],dict) else record.stat().st_mtime
        graph=json.loads(record.with_suffix('.graph.json').read_text())
        totals={int(node['inputs']['steps']) for node in graph.values() if 'steps' in node.get('inputs',{})}
        if len(totals)!=1:return {'status':'running'}
        total=totals.pop()
        if not 1<=total<=100:return {'status':'running'}
        progress_file=record.with_suffix('.progress.json')
        if progress_file.exists():
            try:
                receipt=json.loads(progress_file.read_text())
                if receipt.get('promptId')==prompt_id and receipt.get('steps')==total and 0<=receipt.get('step',-1)<=total:
                    return {k:receipt.get(k) for k in ('status','step','steps','percent','remainingSeconds','observedAt')} | {'stale':time.time()-receipt['observedAt']>5400}
            except (OSError,ValueError,TypeError,KeyError):pass
        result=subprocess.run(['journalctl','-u','comfyui-minimax-h3.service','--since','@'+str(max(0,int(submitted_at)-1)),'--no-pager','-o','json'],capture_output=True,text=True,timeout=3,check=False)
        if result.returncode:return {'status':'running'}
        lines=[]
        for line in result.stdout.splitlines():
            try:lines.append(json.loads(line))
            except json.JSONDecodeError:continue
        receipt=_sampler_log(lines,total,submitted_at)
        if not receipt:return {'status':'running','steps':total}
        return {'status':'running',**receipt,'stale':time.time()-receipt['observedAt']>5400}
    except (OSError,ValueError,KeyError,TypeError,IndexError,subprocess.TimeoutExpired,urllib.error.URLError):
        return None

def render(graph,output_node,record,check):
    graph_record=record.with_suffix('.graph.json')
    if record.exists():
        if not graph_record.exists() or json.loads(graph_record.read_text())!=graph:
            raise ValueError('existing_generation_requires_review')
        state=json.loads(record.read_text())
        if state.get('status')=='done':
            path=Path(state['output']).resolve()
            if path.is_relative_to(OUTPUT.resolve()) and path.is_file():return path
            raise ValueError('wardrobe_output_missing')
    else:
        state={'status':'submitting','client':str(uuid.uuid4()),'attempts':0}
        record.write_text(json.dumps(state))
        graph_record.write_text(json.dumps(graph))
    pid=state.get('promptId');watcher=None;watcher_stop=None;watcher_pid=None
    missing=0
    try:
        while True:
            check()
            if not pid:
                if state.get('attempts',0)>=3:raise ValueError('wardrobe_submission_retries_exhausted')
                response=api('/prompt',{'prompt':graph,'client_id':state['client']})
                pid=response['prompt_id']
                state.update(status='submitted',promptId=pid,attempts=state.get('attempts',0)+1)
                record.write_text(json.dumps(state))
            if pid!=watcher_pid:
                if watcher_stop:watcher_stop.set()
                sampler=_sampler_progress_config(graph)
                if sampler:
                    watcher_stop=threading.Event()
                    watcher=threading.Thread(target=_watch_progress,args=(record,pid,state['client'],*sampler,watcher_stop),daemon=True)
                    watcher.start();watcher_pid=pid
            h=api('/history/'+pid).get(pid)
            if h:
                if h['status']['status_str']!='success':raise ValueError('wardrobe_generation_failed')
                entries=h.get('outputs',{}).get(str(output_node),{}).get('images',[])
                if not entries or entries[0].get('type')!='output':raise ValueError('wardrobe_output_missing')
                e=entries[0];path=(OUTPUT/e.get('subfolder','')/e['filename']).resolve()
                if not path.is_relative_to(OUTPUT.resolve()) or not path.is_file():raise ValueError('wardrobe_output_path')
                state.update(status='done',output=str(path))
                record.write_text(json.dumps(state));return path
            queue=api('/queue')
            if any(entry[1]==pid for key in ('queue_running','queue_pending') for entry in queue.get(key,[])):
                missing=0
            else:
                missing+=1
                if missing>=3:
                    outputs=list((OUTPUT/'wardrobe-h3').glob(record.parent.parent.parent.name+'_*.mp4'))
                    if len(outputs)==1:
                        state.update(status='done',output=str(outputs[0].resolve()))
                        record.write_text(json.dumps(state));return outputs[0]
                    if outputs:raise ValueError('wardrobe_output_ambiguous')
                    state.update(status='submitting',promptId=None)
                    record.write_text(json.dumps(state))
                    pid=None;missing=0
            time.sleep(5)
    except ValueError as error:
        if str(error)!='cancelled':raise
        if pid:
            try:
                queue=api('/queue')
                if any(entry[1]==pid for entry in queue.get('queue_pending',[])):api('/queue',{'delete':[pid]})
                if any(entry[1]==pid for entry in queue.get('queue_running',[])):api('/interrupt',{'prompt_id':pid})
            except Exception:pass
        raise
    finally:
        if watcher_stop:watcher_stop.set()

def probe(path):
    from fractions import Fraction
    s=json.loads(subprocess.check_output(['ffprobe','-v','error','-count_frames','-select_streams','v:0','-show_entries','stream=width,height,avg_frame_rate,nb_read_frames','-of','json',str(path)]))['streams'][0]
    return {'width':s['width'],'height':s['height'],'fps':float(Fraction(s['avg_frame_rate'])),'frames':int(s['nb_read_frames'])}

def verify(report,stats,source,output,choice):
    if source!=output:raise ValueError('wardrobe_timing_failed')
    expected=max(5,math.ceil(source['frames']/source['fps']))
    scores=report.get('output',{}).get('referenceSimilaritySamples',[])
    if len(scores)!=expected or any(s is None or s<.45 for s in scores) or sum(scores)/len(scores)<.65:raise ValueError('wardrobe_identity_failed')
    if not __import__('original_video').face_coverage(stats,source['frames']) or stats.get('model')!='hyperswap_1b_256' or stats.get('expressionFactor')!=0 or stats.get('appliedLabDelta') is not None:raise ValueError('wardrobe_face_coverage_failed')
    return {'policy':POLICY,'wardrobe':choice,'timingVerified':True,'faceCoverage':1,'sampleIdentityMean':sum(scores)/len(scores),'sampleIdentityMin':min(scores),'regeneratedFrames':True,'originalPixelsPreserved':False,'visualReview':'required','hairReferenceRequested':choice=='portrait_hair','hairVisualReview':'required' if choice=='portrait_hair' else None,'publishApproved':False,'dimensions':output,'engine':'minimax_h3_ref2va','model':MODEL,'steps':20,'faceModel':'hyperswap_1b_256','nativeMotionPreserved':False}

def stage_review_output(output,review):
    # The durable job directory is on SMB, where symbolic links are unsupported.
    review.mkdir(exist_ok=True)
    destination=review/'output.mp4'
    shutil.copy2(output,destination)
    return destination

def process(folder,repo,cfg,choice,child,check):
    choice=normalize(choice);r=folder/'render';work=r/'wardrobe';work.mkdir(exist_ok=True)
    source=r/'source.mp4';identity=r/'output.mp4';meta=probe(source)
    width,height=(hair_size if choice=='portrait_hair' else size)(meta['width'],meta['height'])
    if choice=='portrait_hair' and meta['frames']/meta['fps']<5:raise ValueError('portrait_hair_requires_five_seconds')
    plan=frame_plan(meta);duration=plan['frames']/24;ident=folder.name
    # Use the original cleaned source for motion, exactly as in the approved H3 trial.
    # workflow.py's verified face-only output is kept separately, never used as fallback.
    ref=COMFY/'input'/(ident+'-h3-portrait.jpg')
    if choice=='portrait_hair':
        python=Path(cfg['engine'])/'.venv/bin/python'
        child([str(python),str(repo/'ops/wardrobe-h3/crop_head_reference.py'),
               '--source',str(folder/'portrait.jpg'),'--output',str(ref)],folder,'hair-reference-crop.log',18)
    else:shutil.copy2(folder/'portrait.jpg',ref)
    video=COMFY/'input'/(ident+'-h3-motion.mp4')
    if choice=='portrait_hair':
        prepare_hair_source(source,video,width,height,plan['generatedFrames'],child,folder)
        mask=COMFY/'input'/(ident+'-h3-hair-mask.mp4')
        mask_report=work/'hair-mask.json'
        child([str(python),str(repo/'ops/wardrobe-h3/build_hair_mask.py'),
               '--source',str(video),'--output',str(mask),'--report',str(mask_report)],
              folder,'hair-mask.log',18)
        if probe(mask)!={'width':width,'height':height,'fps':24.0,'frames':plan['generatedFrames']}:
            raise ValueError('hair_mask_timing_mismatch')
    else:shutil.copy2(source,video)
    check()
    graph=(hair_graph(repo,ref.name,video.name,mask.name,width,height,'wardrobe-h3/'+ident)
           if choice=='portrait_hair' else
           motion_graph(repo,ref.name,video.name,choice,width,height,plan['generatedFrames'],'wardrobe-h3/'+ident))
    path=render(graph,'14',work/'generation.json',check)
    expected={'width':width,'height':height,'fps':24.0,'frames':plan['generatedFrames']}
    if probe(path)!=expected:raise ValueError('wardrobe_h3_generation_mismatch')
    if choice=='portrait_hair':
        child([str(python),str(repo/'ops/wardrobe-h3/check_hair_preservation.py'),
               '--source',str(video),'--mask',str(mask),'--result',str(path),
               '--report',str(work/'hair-preservation.json')],folder,'hair-preservation.log',80)
    raw=work/'motion.mp4'
    child(['ffmpeg','-v','error','-y','-i',str(path),'-an','-frames:v',str(plan['frames']),'-c:v','libx264','-crf','16','-movflags','+faststart',str(raw)],folder,'wardrobe-trim.log',80)
    python=Path(cfg['engine'])/'.venv/bin/python'
    if identity.exists():identity.rename(r/'original-face.mp4')
    source.rename(r/'original-source.mp4')
    # Comparison uses the same 24fps grid. Preserve archived originals and do not stretch time.
    child(['ffmpeg','-v','error','-y','-i',str(r/'original-source.mp4'),'-vf',f'fps=24,scale={width}:{height}','-frames:v',str(plan['frames']),'-an','-c:v','libx264','-crf','16',str(source)],folder,'wardrobe-source.log',90)
    source_meta=probe(source);receipt=None
    for attempt,detector_score in enumerate((.5,.35,.2),1):
        out=work/f'face-{attempt}.mp4';review=work/f'review-{attempt}'
        child([str(python),str(repo/'ops/face-quality/trial.py'),'--engine',cfg['engine'],'--source',str(raw),'--portrait',str(folder/'portrait.jpg'),'--output',str(out),'--model','hyperswap_1b_256','--selector-mode','one','--detector-score',str(detector_score)],folder,f'wardrobe-face-{attempt}.log',84+attempt)
        stage_review_output(out,review)
        child([str(python),str(repo/'ops/face-quality/evaluate.py'),'--engine',cfg['engine'],'--source',str(source),'--portrait',str(folder/'portrait.jpg'),'--folder',str(review)],folder,f'wardrobe-quality-{attempt}.log',88+attempt)
        try:
            receipt=verify(json.loads((review/'metrics.json').read_text()),json.loads(out.with_suffix('.stats.json').read_text()),source_meta,probe(out),choice)
            receipt.update(faceRepairAttempt=attempt,faceDetectorScore=detector_score,faceSelectorMode='one')
            if choice=='portrait_hair':
                receipt['hairMaskTracking']=json.loads(mask_report.read_text())
                receipt['hairScenePreservation']=json.loads((work/'hair-preservation.json').read_text())
            break
        except ValueError as error:
            if str(error) not in ('wardrobe_identity_failed','wardrobe_face_coverage_failed') or attempt==3:raise
    child(['ffmpeg','-v','error','-y','-i',str(out),'-i',str(r/'original-source.mp4'),'-map','0:v:0','-map','1:a?','-c','copy','-t',str(duration),'-movflags','+faststart',str(identity)],folder,'wardrobe-mux.log',92)
    candidate=json.loads((folder/'candidate.json').read_text())
    username=candidate['username']
    def scan(video):
        try:
            result=subprocess.run([cfg['inpaintPython'],str(repo/'ops/face-quality/detect_account_overlay.py'),
                                   '--source',str(video),'--username',username],
                                  capture_output=True,text=True,timeout=90,check=True)
            return json.loads(result.stdout)
        except (subprocess.CalledProcessError,subprocess.TimeoutExpired,json.JSONDecodeError) as error:
            raise ValueError('wardrobe_account_overlay_review_required') from error
    overlay=scan(identity)
    receipt['overlayReview']=overlay
    if overlay['overlayROI']:
        clean=work/'face-no-account.mp4'
        child([cfg['inpaintPython'],str(repo/'ops/face-quality/remove_overlay.py'),
               '--source',str(identity),'--output',str(clean),'--model',cfg['inpaintModel'],
               '--roi',*map(str,overlay['overlayROI']),'--full-roi'],folder,'wardrobe-account-restoration.log',93)
        receipt['overlayOutputReview']=scan(clean)
        if receipt['overlayOutputReview']['overlayROI']:raise ValueError('wardrobe_account_overlay_remains')
        identity.rename(work/'face-with-account.mp4')
        clean.rename(identity)
    child(['ffmpeg','-v','error','-i',str(identity),'-f','null','-'],folder,'wardrobe-decode.log',94)
    output=probe(identity)
    if output!={'width':width,'height':height,'fps':24.0,'frames':plan['frames']}:raise ValueError('wardrobe_output_mismatch')
    if choice=='portrait_hair':
        hair_report=work/'hair-reference.json'
        child([str(python),str(repo/'ops/wardrobe-h3/check_hair_reference.py'),
               '--portrait',str(folder/'portrait.jpg'),'--result',str(identity),
               '--report',str(hair_report)],folder,'hair-reference.log',95)
        receipt['hairReferenceColor']=json.loads(hair_report.read_text())
    receipt.update(sourceDimensions=meta,sourceDuration=meta['frames']/meta['fps'],outputDuration=duration,generatedFrames=plan['generatedFrames'],sourceTimingPreserved=False,comparisonTimingVerified=True)
    child(['ffmpeg','-v','error','-y','-i',str(source),'-i',str(identity),'-filter_complex','hstack=inputs=2','-an','-c:v','libx264','-crf','18','-movflags','+faststart',str(r/'comparison.mp4')],folder,'wardrobe-comparison.log',98)
    check()
    return receipt
